"""Connection/table sidebar.

A Gtk.ListBox of Adw.ExpanderRow, one per connection profile — tables
are grouped under their connection and several connections can be
expanded at once. Expanding a row connects (on a worker thread) and
lists tables/views underneath; activating a table row opens a data tab,
and each connection has a "new query console" button.
"""

from __future__ import annotations

from typing import Callable

from gi.repository import Adw, Gtk

from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.db.base import Connector
from sqlide.frontend.util import run_async


class Sidebar(Gtk.ScrolledWindow):
    def __init__(
        self,
        ensure_connector: Callable[[ConnectionProfile], Connector],
        on_open_table: Callable[[ConnectionProfile, str], None],
        on_new_query: Callable[[ConnectionProfile], None],
        show_error: Callable[[str], None],
    ) -> None:
        super().__init__(vexpand=True)
        self._ensure = ensure_connector
        self._on_open_table = on_open_table
        self._on_new_query = on_new_query
        self._show_error = show_error
        self._rows: dict[str, Adw.ExpanderRow] = {}

        self._list = Gtk.ListBox()
        self._list.set_selection_mode(Gtk.SelectionMode.NONE)
        self._list.add_css_class("navigation-sidebar")
        self.set_child(self._list)

    def add_profile(self, profile: ConnectionProfile) -> None:
        row = Adw.ExpanderRow(title=profile.name, subtitle=profile.kind)

        query_button = Gtk.Button(icon_name="utilities-terminal-symbolic")
        query_button.set_tooltip_text("New query console")
        query_button.add_css_class("flat")
        query_button.set_valign(Gtk.Align.CENTER)
        query_button.connect("clicked", lambda *_: self._on_new_query(profile))
        row.add_suffix(query_button)

        row.tables_loaded = False
        row.connect("notify::expanded", self._on_expanded, profile)
        self._list.append(row)
        self._rows[profile.name] = row

    def expand_profile(self, name: str) -> None:
        """Expand (and thereby connect/load) the row for a profile."""
        row = self._rows.get(name)
        if row is not None:
            row.set_expanded(True)

    def _table_row(self, profile: ConnectionProfile, table) -> Adw.ActionRow:
        child = Adw.ActionRow(title=table.name, activatable=True)
        child.add_css_class("sidebar-table-row")
        child.set_title_lines(1)
        if table.kind == "view":
            hint = Gtk.Label(label="view")
            hint.add_css_class("dim-label")
            hint.add_css_class("caption")
            child.add_suffix(hint)
        child.connect(
            "activated",
            lambda _r, name=table.name: self._on_open_table(profile, name),
        )
        return child

    def _on_expanded(self, row: Adw.ExpanderRow, _pspec, profile) -> None:
        if not row.get_expanded() or row.tables_loaded:
            return
        row.tables_loaded = True

        def work():
            return self._ensure(profile).list_tables()

        def done(tables):
            if not tables:
                empty = Adw.ActionRow(title="(no tables)")
                empty.add_css_class("sidebar-table-row")
                row.add_row(empty)
                return
            for table in tables:
                row.add_row(self._table_row(profile, table))

        def failed(exc):
            row.tables_loaded = False
            row.set_expanded(False)
            self._show_error(str(exc))

        run_async(work, done, failed)
