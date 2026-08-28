"""The Import dialog (CORE-37): this file, into that table.

Reachable from a table's context menu in the sidebar and from a table
tab's action bar — the two places somebody is already looking at the
table they want the file in.

The questions come in the order they have to be answered: which file
(and how it is read — delimiter, quoting, header, encoding), what the
first rows look like once parsed, which source column feeds which
table column, whether the table keeps its rows or is emptied first,
and what the statement will look like. Only then is there an Import
button worth pressing.

Nothing here parses or coerces anything: every field comes from
backend/importer.py, so what the preview shows and what the insert
binds are the same code. The dialog owns three things instead — the
thread (every read and the load itself go through run_async), the
confirmation ladder for a replace (backend/sql_risk.py, through
frontend/confirm.py, so emptying a production table is never one
click), and the report at the end: rows inserted, rows skipped, and
the first error with its line number.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from gi.repository import Adw, GLib, Gtk

from sqlide.backend import importer, settings as app_settings, sql_risk
from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.db.base import BulkCancelled
from sqlide.frontend import confirm
from sqlide.frontend.util import run_async
from sqlide.i18n import _

# Rows of the file shown in the preview, and rows of the mapped values
# under it. Small on purpose: this is a look at the file, not a grid.
PREVIEW_ROWS = 8

# Labels are translated where they are used, never here: a module-level
# _() would freeze the English string into the import.
SKIP_LABEL = "(skip)"

DELIMITERS = (
    ("Comma  ,", ","),
    ("Semicolon  ;", ";"),
    ("Tab", "\t"),
    ("Pipe  |", "|"),
)

MODES = (
    ("Append to the table", "append"),
    ("Empty the table first", "replace"),
)


class ImportDialog(Adw.Dialog):
    def __init__(
        self,
        profile: ConnectionProfile,
        table: str,
        connector_for: Callable[[], Any],
        on_imported: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(
            title=_("Import Into %s") % table,
            content_width=620,
            content_height=720,
        )
        self._profile = profile
        self._table = table
        self._connector_for = connector_for
        self._on_imported = on_imported
        self._path: Path | None = None
        self._dialect = importer.Dialect()
        self._source_names: list[str] = []
        self._target_columns: list[str] = []
        self._kinds: dict[str, str] = {}
        self._column_rows: list[Adw.ComboRow] = []
        self._cancel = threading.Event()
        self._running = False
        self._skip = _(SKIP_LABEL)

        page = Adw.PreferencesPage()

        source = Adw.PreferencesGroup(title=_("File"))
        self._file_row = Adw.ActionRow(
            title=_("CSV file"), subtitle=_("No file chosen")
        )
        choose = Gtk.Button(label=_("Choose…"), valign=Gtk.Align.CENTER)
        choose.connect("clicked", self._on_choose_file)
        self._file_row.add_suffix(choose)
        self._file_row.set_activatable_widget(choose)
        source.add(self._file_row)
        self._delimiter = Adw.ComboRow(
            title=_("Delimiter"),
            model=Gtk.StringList.new([_(label) for label, _v in DELIMITERS]),
        )
        self._delimiter.connect("notify::selected", self._on_dialect_changed)
        source.add(self._delimiter)
        self._encoding = Adw.ComboRow(
            title=_("Encoding"),
            model=Gtk.StringList.new(list(importer.ENCODINGS)),
        )
        self._encoding.connect("notify::selected", self._on_dialect_changed)
        source.add(self._encoding)
        self._header = Adw.SwitchRow(
            title=_("First row holds column names"), active=True
        )
        self._header.connect("notify::active", self._on_dialect_changed)
        source.add(self._header)
        self._null = Adw.EntryRow(title=_("Text that means NULL"))
        self._null.connect("changed", self._on_null_changed)
        source.add(self._null)
        page.add(source)

        self._preview_group = Adw.PreferencesGroup(
            title=_("The file as parsed"),
            description=_("The first rows, split the way they will be read"),
        )
        self._preview = _text_view()
        self._preview_group.add(_framed(self._preview, 120))
        page.add(self._preview_group)

        self._mapping_group = Adw.PreferencesGroup(
            title=_("Columns"),
            description=_(
                "Each column of the file, and the table column it fills"
            ),
        )
        page.add(self._mapping_group)

        target = Adw.PreferencesGroup(title=_("Import"))
        self._mode = Adw.ComboRow(
            title=_("Existing rows"),
            model=Gtk.StringList.new([_(label) for label, _v in MODES]),
        )
        self._mode.connect("notify::selected", self._refresh_statement)
        target.add(self._mode)
        self._batch = Adw.SpinRow(
            title=_("Rows per batch"),
            subtitle=_("The whole file is still one transaction"),
            adjustment=Gtk.Adjustment(
                lower=1, upper=100000, step_increment=100, page_increment=500
            ),
        )
        self._batch.set_value(app_settings.store.settings.import_batch_size)
        target.add(self._batch)
        page.add(target)

        self._statement_group = Adw.PreferencesGroup(
            title=_("What will run"),
            description=_(
                "Values are bound to the placeholders, never written "
                "into the statement"
            ),
        )
        self._statement = _text_view()
        self._statement_group.add(_framed(self._statement, 90))
        page.add(self._statement_group)

        header = Adw.HeaderBar()
        self._import_button = Gtk.Button(label=_("Import"))
        self._import_button.add_css_class("suggested-action")
        self._import_button.set_sensitive(False)
        self._import_button.connect("clicked", self._on_import_clicked)
        header.pack_end(self._import_button)

        self._status = Gtk.Label(
            xalign=0,
            margin_start=12,
            margin_end=12,
            margin_bottom=8,
            wrap=True,
            visible=False,
        )
        self._status.add_css_class("dim-label")
        self._cancel_button = Gtk.Button(
            label=_("Cancel import"),
            halign=Gtk.Align.START,
            margin_start=12,
            margin_bottom=8,
            visible=False,
        )
        self._cancel_button.connect(
            "clicked", lambda *_a: self._on_cancel_clicked()
        )

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.append(page)
        body.append(self._status)
        body.append(self._cancel_button)
        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(body)
        self.set_child(view)

        self._load_target_columns()

    # The table

    def _load_target_columns(self) -> None:
        """The table's columns and the kind of value each takes, read
        through the provider so no engine's type names are spelled
        here (backend/db/metadata.py)."""

        def work() -> tuple[list[str], dict[str, str]]:
            connector = self._connector_for()
            kinds = connector.column_kinds(self._table)
            return list(kinds), kinds

        def done(result) -> None:
            self._target_columns, self._kinds = result
            self._rebuild_mapping()

        run_async(work, done, lambda exc: self._say(str(exc), error=True))

    # The file

    def _on_choose_file(self, *_args) -> None:
        dialog = Gtk.FileDialog(title=_("Import a CSV File"))
        csv_filter = Gtk.FileFilter()
        csv_filter.set_name(_("Text tables (CSV, TSV)"))
        for pattern in ("*.csv", "*.tsv", "*.txt"):
            csv_filter.add_pattern(pattern)
        dialog.set_default_filter(csv_filter)

        def picked(chooser: Gtk.FileDialog, result) -> None:
            try:
                file = chooser.open_finish(result)
            except GLib.Error:
                return  # cancelled
            self._path = Path(file.get_path())
            self._file_row.set_subtitle(str(self._path))
            self._sniff()

        dialog.open(self._root_window(), None, picked)

    def _sniff(self) -> None:
        """Guess how the chosen file is written, then show it. The
        guess only fills the controls in: everything it decided stays
        editable, because a guess the user cannot overrule is worse
        than no guess."""
        path = self._path
        if path is None:
            return

        def done(dialect: importer.Dialect) -> None:
            self._dialect = dialect
            self._apply_dialect_to_controls(dialect)
            self._reload_file()

        run_async(
            lambda: importer.sniff(path),
            done,
            lambda exc: self._say(str(exc), error=True),
        )

    def _apply_dialect_to_controls(self, dialect: importer.Dialect) -> None:
        for index, (_label, value) in enumerate(DELIMITERS):
            if value == dialect.delimiter:
                self._delimiter.set_selected(index)
                break
        if dialect.encoding in importer.ENCODINGS:
            self._encoding.set_selected(
                importer.ENCODINGS.index(dialect.encoding)
            )
        self._header.set_active(dialect.has_header)

    def _dialect_from_controls(self) -> importer.Dialect:
        return importer.Dialect(
            delimiter=DELIMITERS[self._delimiter.get_selected()][1],
            quotechar=self._dialect.quotechar or '"',
            has_header=self._header.get_active(),
            encoding=importer.ENCODINGS[self._encoding.get_selected()],
        )

    def _on_dialect_changed(self, *_args) -> None:
        if self._path is None:
            return
        self._dialect = self._dialect_from_controls()
        self._reload_file()

    def _reload_file(self) -> None:
        """Re-read the head of the file with the current dialect: the
        column names, the parsed preview, and a mapping matched onto
        the table by name."""
        path = self._path
        dialect = self._dialect
        if path is None:
            return

        def work() -> tuple[list[str], str]:
            return (
                importer.header(path, dialect),
                importer.sample_text(path, dialect, PREVIEW_ROWS),
            )

        def done(result) -> None:
            self._source_names, sample = result
            self._preview.get_buffer().set_text(sample)
            self._rebuild_mapping()
            self._say("")

        run_async(work, done, lambda exc: self._say(str(exc), error=True))

    # Mapping

    def _rebuild_mapping(self) -> None:
        """One row per source column, each a list of the table's
        columns plus "(skip)". Defaults come from
        importer.default_mapping, so an unmatched name starts skipped
        rather than pointed at whatever column sits in that position.
        """
        for row in self._column_rows:
            self._mapping_group.remove(row)
        self._column_rows = []
        if not self._source_names or not self._target_columns:
            self._refresh_statement()
            return
        guess = importer.default_mapping(
            self._source_names, self._target_columns, self._null.get_text()
        )
        options = [self._skip] + self._target_columns
        for index, name in enumerate(self._source_names):
            row = Adw.ComboRow(
                title=name or _("Column %d") % (index + 1),
                model=Gtk.StringList.new(options),
            )
            target = guess.columns[index].target
            row.set_selected(
                options.index(target) if target in options else 0
            )
            row.set_subtitle(self._kind_note(target))
            row.connect("notify::selected", self._on_target_changed)
            self._mapping_group.add(row)
            self._column_rows.append(row)
        self._refresh_statement()

    def _kind_note(self, target: str) -> str:
        kind = self._kinds.get(target, "")
        return _("Read as %s") % kind if kind else ""

    def _on_target_changed(self, row: Adw.ComboRow, *_args) -> None:
        row.set_subtitle(self._kind_note(self._target_of(row)))
        self._refresh_statement()

    def _target_of(self, row: Adw.ComboRow) -> str:
        model = row.get_model()
        chosen = model.get_string(row.get_selected())
        return "" if chosen == self._skip else chosen

    def _on_null_changed(self, *_args) -> None:
        self._refresh_statement()

    def mapping(self) -> importer.Mapping:
        """The mapping the rows currently describe."""
        token = self._null.get_text()
        return importer.Mapping(
            tuple(
                importer.ColumnMap(
                    source=index,
                    target=self._target_of(row),
                    null_token=token,
                    skip=not self._target_of(row),
                )
                for index, row in enumerate(self._column_rows)
            )
        )

    def job(self) -> importer.Job:
        return importer.Job(
            path=str(self._path or ""),
            table=self._table,
            dialect=self._dialect,
            mapping=self.mapping(),
            kinds=dict(self._kinds),
            mode=MODES[self._mode.get_selected()][1],
            batch_size=int(self._batch.get_value()),
        )

    # The statement, and whether Import is worth offering

    def _refresh_statement(self, *_args) -> None:
        job = self.job()
        problem = job.mapping.problem() if self._column_rows else _(
            "Choose a file."
        )
        ready = bool(self._path) and not problem
        self._import_button.set_sensitive(ready and not self._running)
        if not ready:
            self._statement.get_buffer().set_text(problem or "")
            return

        def work() -> str:
            connector = self._connector_for()
            text = ""
            if job.mode == "replace":
                text += importer.truncate_statement(
                    connector, job.table
                ) + ";\n"
            return text + importer.preview_statement(connector, job) + ";"

        run_async(
            work,
            lambda text: self._statement.get_buffer().set_text(text),
            lambda exc: self._statement.get_buffer().set_text(str(exc)),
        )

    # Running it

    def _on_import_clicked(self, *_args) -> None:
        if self._running or self._path is None:
            return
        job = self.job()
        problem = job.mapping.problem()
        if problem:
            self._say(problem, error=True)
            return
        if job.mode != "replace":
            self._start(job)
            return
        # Emptying the table is a destructive statement like any
        # other, so it climbs the same ladder — and the statement the
        # user is shown is the one the adapter will actually run.
        def confirmed(statement: str) -> None:
            confirm.confirm_statements(
                self, [statement], self._profile, lambda: self._start(job)
            )

        run_async(
            lambda: importer.truncate_statement(
                self._connector_for(), job.table
            ),
            confirmed,
            lambda exc: self._say(str(exc), error=True),
        )

    def _start(self, job: importer.Job) -> None:
        self._cancel = threading.Event()
        self._running = True
        self._import_button.set_sensitive(False)
        self._cancel_button.set_visible(True)
        self._say(_("Importing…"))

        def progress(count: int) -> None:
            GLib.idle_add(
                lambda: (self._say(_("Inserted %d rows…") % count), False)[1]
            )

        def work() -> importer.Report:
            connector = self._connector_for()
            return importer.run(
                connector,
                job,
                on_progress=progress,
                cancelled=self._cancel.is_set,
            )

        run_async(work, self._done, self._failed)

    def _on_cancel_clicked(self) -> None:
        self._cancel.set()
        self._say(_("Cancelling…"))

    def _done(self, report: importer.Report) -> None:
        self._finished()
        message = _("Imported %(rows)d rows into %(table)s") % {
            "rows": report.inserted,
            "table": self._table,
        }
        if report.skipped:
            message += _(" (%d blank rows skipped)") % report.skipped
        self._say(message)
        if self._on_imported is not None:
            self._on_imported()

    def _failed(self, exc: Exception) -> None:
        self._finished()
        if isinstance(exc, BulkCancelled):
            self._say(
                _("Import cancelled; the table is unchanged."), error=True
            )
            return
        # Every failure rolled the whole load back, so the sentence can
        # say so without qualification.
        self._say(
            _("%s — nothing was imported; the table is unchanged.") % exc,
            error=True,
        )

    def _finished(self) -> None:
        self._running = False
        self._cancel_button.set_visible(False)
        self._import_button.set_sensitive(True)

    def _say(self, text: str, error: bool = False) -> None:
        self._status.set_visible(bool(text))
        self._status.set_text(text)
        if error:
            self._status.add_css_class("error")
        else:
            self._status.remove_css_class("error")

    def _root_window(self):
        root = self.get_root()
        return root if isinstance(root, Gtk.Window) else None


def _text_view() -> Gtk.TextView:
    return Gtk.TextView(
        editable=False,
        monospace=True,
        cursor_visible=False,
        left_margin=8,
        right_margin=8,
        top_margin=8,
        bottom_margin=8,
    )


def _framed(child: Gtk.Widget, height: int) -> Gtk.Widget:
    return Gtk.Frame(
        child=Gtk.ScrolledWindow(
            child=child, height_request=height, hexpand=True
        )
    )


def present_import_dialog(
    parent: Gtk.Widget,
    profile: ConnectionProfile,
    table: str,
    connector_for: Callable[[], Any],
    on_imported: Callable[[], None] | None = None,
) -> ImportDialog:
    """Open the dialog over `parent` — the one entry point the sidebar
    menu and the table tab's button both use."""
    dialog = ImportDialog(profile, table, connector_for, on_imported)
    dialog.present(parent)
    return dialog


# Kept next to the dialog so the risk of a replace can be asked about
# without opening one (the sidebar's menu, a test).
def replace_risk(connector, table: str) -> sql_risk.Risk:
    return sql_risk.classify(importer.truncate_statement(connector, table))
