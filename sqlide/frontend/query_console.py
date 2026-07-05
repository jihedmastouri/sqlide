"""Query console tab: SQL editor on top, results grid below.

A console is not tied to one connection: the toolbar has a dropdown
over the workspace's connection names (a Gtk.StringList shared by all
consoles, so added connections appear everywhere; each dropdown keeps
its own selection). Run resolves the selected name to a profile at
execution time and reports every run — success or failure — through
the on_ran callback so the window can record history.

One more session-scoped dropdown (not persisted in TabState):
- Database: for server connections (mysql/postgres) whose one server
  hosts many databases; hidden for sqlite, where one file is one
  database. Queries and completions run against the chosen database
  via a profile copy with `database` overridden.

Console-local settings live behind the gear MenuButton at the right
end of the toolbar; currently that's the LSP choice, which pins the
completion language server for this console — auto (plugin, then
defaults), off, or a specific plugin/PATH server.

The editor holds a script of any number of statements. Run (Ctrl+Enter)
executes the selection if there is one, otherwise the statement under
the cursor; Run All (Ctrl+Shift+Enter) executes the whole buffer.
Explain runs the same statements behind the adapter's explain prefix
(EXPLAIN QUERY PLAN on SQLite); each plan tab offers a Table and a
JSON rendering. Statements run sequentially through Connector.execute()
on a worker thread, stopping at the first failure. After a run the
bottom panel always shows at least two tabs: a Status tab first (each
statement's outcome, timing and SQL), then one result tab per
statement.

The toolbar also has Begin/Commit/Rollback buttons that run the bare
transaction statements over the console's connection, and open/save
buttons that load any text file into the editor and write the editor
back to a file (the first save asks where; later saves reuse it).
"""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from typing import Callable

from gi.repository import Gdk, GLib, Gtk

from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.db.base import Connector, ResultSet
from sqlide.backend.sql_split import split_statements, statement_at
from sqlide.backend.workspaces import TabState
from sqlide.frontend.data_grid import ResultGrid, _format_json
from sqlide.frontend.lsp_completion import LspCompletionProvider
from sqlide.frontend.sql_editor import SqlEditor
from sqlide.frontend.util import run_async
from sqlide.lsp import servers as lsp_servers

# Connection kinds where one server hosts multiple databases.
_MULTI_DB_KINDS = ("mysql", "postgres")


class QueryConsole(Gtk.Box):
    def __init__(
        self,
        connection_names: Gtk.StringList,
        find_connection: Callable[[str], ConnectionProfile | None],
        ensure_connector: Callable[[ConnectionProfile], Connector],
        sql: str = "",
        connection: str = "",
        on_ran: Callable[[str, str, bool], None] | None = None,
        on_aggregate: Callable[[list[str]], None] | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._find_connection = find_connection
        self._ensure = ensure_connector
        # Public: the window rebinds it after the tab page exists so
        # history entries carry the panel (tab) name.
        self.on_ran = on_ran
        self._on_aggregate = on_aggregate
        # Set by the window after the tab page exists (tab title).
        self.on_connection_changed: Callable[[str], None] | None = None

        toolbar = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=6,
            margin_top=6,
            margin_bottom=6,
            margin_start=6,
            margin_end=6,
        )
        self._run_button = Gtk.Button(label="Run")
        self._run_button.add_css_class("suggested-action")
        self._run_button.set_tooltip_text(
            "Run the selection or the statement at the cursor (Ctrl+Enter)"
        )
        self._run_button.connect("clicked", lambda *_: self._run())
        self._run_all_button = Gtk.Button(label="Run All")
        self._run_all_button.set_tooltip_text(
            "Run every statement in the editor (Ctrl+Shift+Enter)"
        )
        self._run_all_button.connect(
            "clicked", lambda *_: self._run(run_all=True)
        )
        self._explain_button = Gtk.Button(label="Explain")
        self._explain_button.set_tooltip_text(
            "Show the plan of the selection or the statement at the "
            "cursor instead of running it"
        )
        self._explain_button.connect(
            "clicked", lambda *_: self._run(explain=True)
        )
        self._dropdown = Gtk.DropDown(model=connection_names)
        self._dropdown.set_tooltip_text("Connection to run against")
        self._db_dropdown = Gtk.DropDown(visible=False)
        self._db_dropdown.set_tooltip_text("Database on the server")
        self._db_seq = 0  # discards stale list_databases results
        self._lsp_choices = [
            lsp_servers.AUTO,
            lsp_servers.NONE,
            *lsp_servers.available_servers(),
        ]
        self._lsp_dropdown = Gtk.DropDown(
            model=Gtk.StringList.new(
                ["Automatic", "Off", *self._lsp_choices[2:]]
            )
        )
        self._lsp_dropdown.set_tooltip_text("Completion language server")
        hint = Gtk.Label(label="Ctrl+Enter")
        hint.add_css_class("dim-label")

        # Transaction statements over this console's connection.
        tx_box = Gtk.Box()
        tx_box.add_css_class("linked")
        for label, tooltip in (
            ("Begin", "Start a transaction (BEGIN)"),
            ("Commit", "Commit the open transaction"),
            ("Rollback", "Roll back the open transaction"),
        ):
            button = Gtk.Button(label=label)
            button.set_tooltip_text(tooltip)
            button.connect(
                "clicked",
                lambda _b, kw=label.upper(): self._run_statements([kw]),
            )
            tx_box.append(button)

        self._file_path: Path | None = None  # target of the Save button
        open_button = Gtk.Button(icon_name="document-open-symbolic")
        open_button.add_css_class("flat")
        open_button.set_tooltip_text("Open a file in the editor")
        open_button.connect("clicked", self._open_file)
        save_button = Gtk.Button(icon_name="document-save-symbolic")
        save_button.add_css_class("flat")
        save_button.set_tooltip_text(
            "Save the editor to a file (the first save asks where)"
        )
        save_button.connect("clicked", self._save_file)

        toolbar.append(self._run_button)
        toolbar.append(self._run_all_button)
        toolbar.append(self._explain_button)
        toolbar.append(self._dropdown)
        toolbar.append(self._db_dropdown)
        toolbar.append(tx_box)
        toolbar.append(hint)
        toolbar.append(Gtk.Box(hexpand=True))
        toolbar.append(open_button)
        toolbar.append(save_button)
        toolbar.append(self._settings_button())
        self.append(toolbar)

        if connection:
            self.select_connection(connection)
        self._dropdown.connect(
            "notify::selected-item", self._connection_selected
        )

        self._editor = SqlEditor(sql=sql)
        self._editor.set_min_content_height(120)
        self._lsp_provider = LspCompletionProvider()
        self._editor.add_completion_provider(self._lsp_provider)
        self._db_dropdown.connect(
            "notify::selected-item", self._database_selected
        )
        self._lsp_dropdown.connect(
            "notify::selected-item", self._lsp_selected
        )
        self._refresh_databases()
        self._lsp_provider.set_profile(self._active_profile())
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key_pressed)
        self._editor.view.add_controller(keys)

        self._results = Gtk.Notebook(vexpand=True)
        self._results.set_show_tabs(False)
        self._results.set_show_border(False)
        self._results.set_scrollable(True)
        self._append_result_page(
            ResultGrid(on_aggregate=on_aggregate), "Result"
        )

        paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL, vexpand=True)
        paned.set_start_child(self._editor)
        paned.set_end_child(self._results)
        paned.set_shrink_start_child(False)
        paned.set_shrink_end_child(False)
        paned.set_position(160)
        self.append(paned)

        self._status = Gtk.Label(
            xalign=0,
            margin_top=4,
            margin_bottom=4,
            margin_start=8,
            margin_end=8,
            selectable=True,
        )
        self._status.add_css_class("dim-label")
        self.append(self._status)

    def _settings_button(self) -> Gtk.MenuButton:
        """Gear menu at the right end of the toolbar: settings local to
        this console only (currently the completion language server)."""
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=6,
            margin_top=6,
            margin_bottom=6,
            margin_start=6,
            margin_end=6,
        )
        label = Gtk.Label(label="Completion language server", xalign=0)
        label.add_css_class("dim-label")
        box.append(label)
        box.append(self._lsp_dropdown)
        button = Gtk.MenuButton(
            icon_name="emblem-system-symbolic",
            popover=Gtk.Popover(child=box),
        )
        button.set_tooltip_text("Console settings (this console only)")
        return button

    def selected_connection(self) -> str:
        item = self._dropdown.get_selected_item()
        return item.get_string() if item is not None else ""

    def select_connection(self, name: str) -> None:
        model = self._dropdown.get_model()
        for i in range(model.get_n_items()):
            if model.get_string(i) == name:
                self._dropdown.set_selected(i)
                return

    def set_sql(self, sql: str) -> None:
        self._editor.set_text(sql)

    def tab_state(self) -> TabState:
        return TabState(
            kind="query",
            connection=self.selected_connection(),
            sql=self._editor.get_text(),
        )

    def _selected_database(self) -> str:
        item = self._db_dropdown.get_selected_item()
        return item.get_string() if item is not None else ""

    def _active_profile(self) -> ConnectionProfile | None:
        """The selected connection, with the database dropdown's choice
        applied (as a profile copy — the workspace's profile and the
        window's connector for it are untouched)."""
        profile = self._find_connection(self.selected_connection())
        if profile is None:
            return None
        database = self._selected_database()
        if database and database != profile.database:
            profile = replace(
                profile,
                name=f"{profile.name} · {database}",
                database=database,
            )
        return profile

    def _connection_selected(self, *_args) -> None:
        name = self.selected_connection()
        self._refresh_databases()
        self._lsp_provider.set_profile(self._active_profile())
        if self.on_connection_changed is not None:
            self.on_connection_changed(name)

    def _database_selected(self, *_args) -> None:
        self._lsp_provider.set_profile(self._active_profile())

    def _lsp_selected(self, *_args) -> None:
        index = self._lsp_dropdown.get_selected()
        if 0 <= index < len(self._lsp_choices):
            self._lsp_provider.set_choice(self._lsp_choices[index])

    def _refresh_databases(self) -> None:
        """Rebuild the database dropdown for the selected connection:
        immediately from the profile, then from the server's catalog
        (async; drivers that can't list — stubs, sqlite — leave it)."""
        self._db_seq += 1
        seq = self._db_seq
        profile = self._find_connection(self.selected_connection())
        multi = profile is not None and profile.kind in _MULTI_DB_KINDS
        names = [profile.database] if multi and profile.database else []
        self._set_databases(names, select=names[0] if names else "")
        if not multi:
            return

        def work():
            return self._ensure(profile).list_databases()

        def done(databases):
            if seq != self._db_seq or not databases:
                return
            selected = self._selected_database() or profile.database
            self._set_databases(databases, select=selected)

        run_async(work, done, lambda _exc: None)

    def _set_databases(self, names: list[str], select: str) -> None:
        self._db_dropdown.set_model(Gtk.StringList.new(names))
        if select in names:
            self._db_dropdown.set_selected(names.index(select))
        self._db_dropdown.set_visible(bool(names))

    def _on_key_pressed(self, _controller, keyval, _keycode, state) -> bool:
        is_enter = keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter)
        if is_enter and state & Gdk.ModifierType.CONTROL_MASK:
            self._run(run_all=bool(state & Gdk.ModifierType.SHIFT_MASK))
            return True
        return False

    def _statements_to_run(self, run_all: bool) -> list[str]:
        """What Run should execute: the selection when there is one,
        the statement under the cursor otherwise; Run All takes the
        whole buffer."""
        if run_all:
            source = self._editor.get_text()
        else:
            source = self._editor.get_selection()
            if not source:
                statement = statement_at(
                    split_statements(self._editor.get_text()),
                    self._editor.get_cursor_offset(),
                )
                return [statement.text] if statement else []
        return [s.text for s in split_statements(source)]

    def _run(self, run_all: bool = False, explain: bool = False) -> None:
        self._run_statements(
            self._statements_to_run(run_all), explain=explain
        )

    def _run_statements(
        self, statements: list[str], explain: bool = False
    ) -> None:
        """Execute statements (from the editor, a transaction button…)
        over the selected connection; with explain=True each one runs
        behind the adapter's explain prefix instead."""
        if not statements:
            return
        name = self.selected_connection()
        if not name:
            self._set_status("No connection selected", error=True)
            return
        profile = self._active_profile()
        if profile is None:
            self._set_status(f"Unknown connection: {name}", error=True)
            return
        self._run_button.set_sensitive(False)
        self._run_all_button.set_sensitive(False)
        self._explain_button.set_sensitive(False)
        self._set_status("Running…", error=False)

        def work():
            connector = self._ensure(profile)
            prefix = connector.explain_prefix() if explain else ""
            outcomes: list[tuple[str, ResultSet | int | Exception, float]] = []
            for sql in statements:
                sql = prefix + sql
                started = time.perf_counter()
                try:
                    result: ResultSet | int | Exception = connector.execute(sql)
                except Exception as exc:  # shown in the statement's tab
                    result = exc
                outcomes.append((sql, result, time.perf_counter() - started))
                if isinstance(result, Exception):
                    break
            return outcomes

        def done(outcomes):
            self._run_button.set_sensitive(True)
            self._run_all_button.set_sensitive(True)
            self._explain_button.set_sensitive(True)
            self._show_outcomes(
                outcomes, planned=len(statements), json_view=explain
            )
            if self.on_ran is not None:
                for sql, result, _elapsed in outcomes:
                    self.on_ran(sql, name, not isinstance(result, Exception))

        def failed(exc):
            # Connecting failed before any statement ran.
            self._run_button.set_sensitive(True)
            self._run_all_button.set_sensitive(True)
            self._explain_button.set_sensitive(True)
            self._set_status(str(exc), error=True)
            if self.on_ran is not None:
                self.on_ran(statements[0], name, False)

        run_async(work, done, failed)

    # Files

    def _open_file(self, *_args) -> None:
        dialog = Gtk.FileDialog(title="Open File")
        dialog.open(self.get_root(), None, self._open_finished)

    def _open_finished(self, dialog: Gtk.FileDialog, result) -> None:
        try:
            file = dialog.open_finish(result)
        except GLib.Error:
            return  # cancelled
        path = Path(file.get_path())
        try:
            self._editor.set_text(path.read_text())
        except (OSError, UnicodeDecodeError) as exc:
            self._set_status(f"Could not open {path}: {exc}", error=True)
            return
        self._file_path = path
        self._set_status(f"Opened {path}", error=False)

    def _save_file(self, *_args) -> None:
        if self._file_path is not None:
            self._write_file(self._file_path)
            return
        dialog = Gtk.FileDialog(title="Save File", initial_name="query.sql")
        dialog.save(self.get_root(), None, self._save_finished)

    def _save_finished(self, dialog: Gtk.FileDialog, result) -> None:
        try:
            file = dialog.save_finish(result)
        except GLib.Error:
            return  # cancelled
        self._write_file(Path(file.get_path()))

    def _write_file(self, path: Path) -> None:
        try:
            path.write_text(self._editor.get_text())
        except OSError as exc:
            self._set_status(f"Could not save {path}: {exc}", error=True)
            return
        self._file_path = path
        self._set_status(f"Saved to {path}", error=False)

    # Result panel

    def _append_result_page(self, child: Gtk.Widget, title: str) -> None:
        label = Gtk.Label(label=title)
        label.set_max_width_chars(24)
        self._results.append_page(child, label)

    def _show_outcomes(
        self,
        outcomes: list[tuple[str, ResultSet | int | Exception, float]],
        planned: int,
        json_view: bool = False,
    ) -> None:
        while self._results.get_n_pages():
            self._results.remove_page(-1)

        # Status tab first, then one result tab per statement — the
        # panel always has at least two tabs after a run.
        self._append_result_page(_status_page(outcomes, planned), "Status")

        counts = []
        error: Exception | None = None
        for i, (sql, result, _elapsed) in enumerate(outcomes):
            title = _tab_title(i, sql) if len(outcomes) > 1 else "Result"
            if isinstance(result, ResultSet):
                grid = ResultGrid(on_aggregate=self._on_aggregate)
                grid.set_result(result.columns, result.rows)
                # Explain plans also offer a JSON rendering of the rows.
                page: Gtk.Widget = (
                    _with_json_view(grid, result) if json_view else grid
                )
                counts.append(f"{len(result)} row(s)")
            elif isinstance(result, Exception):
                page = _message_page(str(result), error=True)
                error = result
            else:
                page = _message_page(f"{result} row(s) affected", error=False)
                counts.append(f"{result} row(s) affected")
            page.set_tooltip_text(sql)
            self._append_result_page(page, title)

        self._results.set_show_tabs(True)
        if error is not None:
            skipped = planned - len(outcomes)
            suffix = f" ({skipped} statement(s) skipped)" if skipped else ""
            self._results.set_current_page(len(outcomes))  # failing tab
            self._set_status(
                f"Statement {len(outcomes)} failed: {error}{suffix}",
                error=True,
            )
        else:
            self._results.set_current_page(1)  # first result
            if len(outcomes) == 1:
                self._set_status(counts[0], error=False)
            else:
                self._set_status(
                    f"{len(outcomes)} statements · " + " · ".join(counts),
                    error=False,
                )

    def _set_status(self, text: str, error: bool) -> None:
        self._status.set_text(text)
        if error:
            self._status.add_css_class("error")
            self._status.remove_css_class("dim-label")
        else:
            self._status.remove_css_class("error")
            self._status.add_css_class("dim-label")


def _tab_title(index: int, sql: str, max_chars: int = 20) -> str:
    snippet = " ".join(sql.split())
    if len(snippet) > max_chars:
        snippet = snippet[: max_chars - 1] + "…"
    return f"{index + 1}: {snippet}"


def _status_page(
    outcomes: list[tuple[str, ResultSet | int | Exception, float]],
    planned: int,
) -> Gtk.Widget:
    """The run's Status tab: one block per statement — outcome, timing
    and the SQL itself — plus a total line."""
    box = Gtk.Box(
        orientation=Gtk.Orientation.VERTICAL,
        spacing=8,
        margin_top=8,
        margin_bottom=8,
        margin_start=8,
        margin_end=8,
    )
    for i, (sql, result, elapsed) in enumerate(outcomes):
        if isinstance(result, ResultSet):
            outcome, failed = f"OK · {len(result)} row(s)", False
        elif isinstance(result, Exception):
            outcome, failed = f"Failed · {result}", True
        else:
            outcome, failed = f"OK · {result} row(s) affected", False
        head = Gtk.Label(
            label=f"Statement {i + 1} · {outcome} · {_format_elapsed(elapsed)}",
            xalign=0,
            wrap=True,
            selectable=True,
        )
        head.add_css_class("error" if failed else "heading")
        body = Gtk.Label(label=sql.strip(), xalign=0, wrap=True, selectable=True)
        body.add_css_class("monospace")
        body.add_css_class("dim-label")
        box.append(head)
        box.append(body)
    total = sum(elapsed for _sql, _result, elapsed in outcomes)
    skipped = planned - len(outcomes)
    summary = (
        f"{len(outcomes)} of {planned} statement(s) ran"
        f" · total {_format_elapsed(total)}"
    )
    if skipped:
        summary += f" · {skipped} skipped"
    footer = Gtk.Label(label=summary, xalign=0)
    footer.add_css_class("dim-label")
    box.append(footer)
    return Gtk.ScrolledWindow(child=box, vexpand=True, hexpand=True)


def _format_elapsed(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds * 1000:.1f} ms"
    return f"{seconds:.2f} s"


def _with_json_view(grid: ResultGrid, result: ResultSet) -> Gtk.Widget:
    """An explain result tab: Table/JSON switcher over the grid and
    the same rows pretty-printed as JSON."""
    stack = Gtk.Stack(vexpand=True)
    stack.add_titled(grid, "table", "Table")
    text = Gtk.TextView(
        editable=False,
        monospace=True,
        cursor_visible=False,
        left_margin=8,
        right_margin=8,
        top_margin=8,
        bottom_margin=8,
    )
    text.get_buffer().set_text(
        _format_json(result.columns, [list(row) for row in result.rows])
    )
    stack.add_titled(
        Gtk.ScrolledWindow(child=text, vexpand=True, hexpand=True),
        "json",
        "JSON",
    )
    switcher = Gtk.StackSwitcher(
        stack=stack,
        halign=Gtk.Align.START,
        margin_top=6,
        margin_bottom=6,
        margin_start=6,
    )
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    box.append(switcher)
    box.append(stack)
    return box


def _message_page(text: str, error: bool) -> Gtk.Widget:
    label = Gtk.Label(
        label=text,
        xalign=0,
        margin_top=8,
        margin_start=8,
        selectable=True,
        wrap=True,
        valign=Gtk.Align.START,
    )
    label.add_css_class("error" if error else "dim-label")
    return label
