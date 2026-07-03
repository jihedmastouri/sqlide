"""Query console tab: SQL editor on top, results grid below.

A console is not tied to one connection: the toolbar has a dropdown
over the workspace's connection names (a Gtk.StringList shared by all
consoles, so added connections appear everywhere; each dropdown keeps
its own selection). Run resolves the selected name to a profile at
execution time and reports every run — success or failure — through
the on_ran callback so the window can record history.

Run button or Ctrl+Enter executes the buffer through Connector.execute()
on a worker thread. One statement at a time (SQLite limitation for now).
"""

from __future__ import annotations

from typing import Callable

from gi.repository import Gdk, Gtk

from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.db.base import Connector, ResultSet
from sqlide.backend.workspaces import TabState
from sqlide.frontend.data_grid import ResultGrid
from sqlide.frontend.sql_editor import SqlEditor
from sqlide.frontend.util import run_async


class QueryConsole(Gtk.Box):
    def __init__(
        self,
        connection_names: Gtk.StringList,
        find_connection: Callable[[str], ConnectionProfile | None],
        ensure_connector: Callable[[ConnectionProfile], Connector],
        sql: str = "",
        connection: str = "",
        on_ran: Callable[[str, str, bool], None] | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._find_connection = find_connection
        self._ensure = ensure_connector
        self._on_ran = on_ran
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
        self._run_button.connect("clicked", lambda *_: self._run())
        self._dropdown = Gtk.DropDown(model=connection_names)
        self._dropdown.set_tooltip_text("Connection to run against")
        hint = Gtk.Label(label="Ctrl+Enter")
        hint.add_css_class("dim-label")
        toolbar.append(self._run_button)
        toolbar.append(self._dropdown)
        toolbar.append(hint)
        self.append(toolbar)

        if connection:
            self.select_connection(connection)
        self._dropdown.connect(
            "notify::selected-item", self._connection_selected
        )

        self._editor = SqlEditor(sql=sql)
        self._editor.set_min_content_height(120)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key_pressed)
        self._editor.view.add_controller(keys)

        self._grid = ResultGrid()

        paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL, vexpand=True)
        paned.set_start_child(self._editor)
        paned.set_end_child(self._grid)
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

    def _connection_selected(self, *_args) -> None:
        if self.on_connection_changed is not None:
            self.on_connection_changed(self.selected_connection())

    def _on_key_pressed(self, _controller, keyval, _keycode, state) -> bool:
        is_enter = keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter)
        if is_enter and state & Gdk.ModifierType.CONTROL_MASK:
            self._run()
            return True
        return False

    def _run(self) -> None:
        sql = self._editor.get_text().strip()
        if not sql:
            return
        name = self.selected_connection()
        if not name:
            self._set_status("No connection selected", error=True)
            return
        profile = self._find_connection(name)
        if profile is None:
            self._set_status(f"Unknown connection: {name}", error=True)
            return
        self._run_button.set_sensitive(False)
        self._set_status("Running…", error=False)

        def work():
            return self._ensure(profile).execute(sql)

        def done(result):
            self._run_button.set_sensitive(True)
            if isinstance(result, ResultSet):
                self._grid.set_result(result.columns, result.rows)
                self._set_status(f"{len(result)} row(s)", error=False)
            else:
                self._grid.clear()
                self._set_status(f"{result} row(s) affected", error=False)
            if self._on_ran is not None:
                self._on_ran(sql, name, True)

        def failed(exc):
            self._run_button.set_sensitive(True)
            self._set_status(str(exc), error=True)
            if self._on_ran is not None:
                self._on_ran(sql, name, False)

        run_async(work, done, failed)

    def _set_status(self, text: str, error: bool) -> None:
        self._status.set_text(text)
        if error:
            self._status.add_css_class("error")
            self._status.remove_css_class("dim-label")
        else:
            self._status.remove_css_class("error")
            self._status.add_css_class("dim-label")
