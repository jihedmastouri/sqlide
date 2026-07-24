"""Table designer tab: a small form that generates CREATE TABLE.

The one create flow that earns a form (everything else gets a dialect
template in a query console): rows of (name, type, PK, nullable,
default), a table-name entry, and a live read-only preview of the
generated statement — built by the adapter's create_table_sql, so
quoting and dialect quirks stay in the backend. Create shows the
statement in an UpdatePreviewDialog before anything runs; on success
the window reloads the sidebar and opens the new table's data tab.

Session-only: tab_state() returns None, the tab is not restored.
"""

from __future__ import annotations

from typing import Callable

from gi.repository import Gtk

from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.db.base import ColumnInfo, Connector
from sqlide.frontend.data_grid import UpdatePreviewDialog
from sqlide.frontend.sql_editor import SqlEditor
from sqlide.frontend.util import run_async


class _ColumnRow(Gtk.ListBoxRow):
    """One column of the future table: name, type (dropdown + free
    text), PK, nullable, default expression, remove."""

    def __init__(
        self,
        types: list[str],
        on_changed: Callable[[], None],
        on_remove: Callable[["_ColumnRow"], None],
    ) -> None:
        super().__init__(activatable=False, selectable=False)
        self._on_changed = on_changed
        box = Gtk.Box(
            spacing=6,
            margin_top=4,
            margin_bottom=4,
            margin_start=6,
            margin_end=6,
        )
        self.name = Gtk.Entry(placeholder_text="column name", hexpand=True)
        self.name.connect("changed", lambda *_: on_changed())
        self.type = Gtk.Entry(placeholder_text="type", width_chars=14)
        self.type.connect("changed", lambda *_: on_changed())
        self._type_menu = Gtk.MenuButton(icon_name="pan-down-symbolic")
        self._type_menu.add_css_class("flat")
        self._type_menu.set_tooltip_text("Common types for this dialect")
        self.set_types(types)
        self.pk = Gtk.CheckButton(label="PK")
        self.pk.set_tooltip_text("Part of the primary key")
        self.pk.connect("toggled", lambda *_: on_changed())
        self.nullable = Gtk.CheckButton(label="NULL", active=True)
        self.nullable.set_tooltip_text("Allow NULL values")
        self.nullable.connect("toggled", lambda *_: on_changed())
        self.default = Gtk.Entry(placeholder_text="default (SQL)", width_chars=12)
        self.default.set_tooltip_text(
            "DEFAULT expression, inserted verbatim (quote strings)"
        )
        self.default.connect("changed", lambda *_: on_changed())
        remove = Gtk.Button(icon_name="user-trash-symbolic")
        remove.add_css_class("flat")
        remove.set_tooltip_text("Remove this column")
        remove.connect("clicked", lambda *_: on_remove(self))
        for child in (
            self.name, self.type, self._type_menu, self.pk, self.nullable,
            self.default, remove,
        ):
            box.append(child)
        self.set_child(box)

    def set_types(self, types: list[str]) -> None:
        popover = Gtk.Popover()
        listing = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        for name in types:
            button = Gtk.Button(label=name)
            button.add_css_class("flat")
            button.connect("clicked", self._pick_type, name, popover)
            listing.append(button)
        popover.set_child(listing)
        self._type_menu.set_popover(popover)

    def _pick_type(self, _button, name: str, popover: Gtk.Popover) -> None:
        self.type.set_text(name)
        popover.popdown()
        self._on_changed()

    def column(self) -> ColumnInfo | None:
        """The row as a ColumnInfo, or None while the name is empty."""
        name = self.name.get_text().strip()
        if not name:
            return None
        return ColumnInfo(
            name=name,
            type=self.type.get_text().strip(),
            is_pk=self.pk.get_active(),
            nullable=self.nullable.get_active(),
        )


class TableDesignerTab(Gtk.Box):
    def __init__(
        self,
        profile: ConnectionProfile,
        ensure_connector: Callable[[ConnectionProfile], Connector],
        show_error: Callable[[str], None],
        on_created: Callable[[str], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.profile = profile
        self._ensure = ensure_connector
        self._show_error = show_error
        self._on_created = on_created
        self.on_ran: Callable[[str, bool], None] | None = None
        self._connector: Connector | None = None
        self._types: list[str] = []
        self._rows: list[_ColumnRow] = []

        bar = Gtk.Box(
            spacing=6,
            margin_top=6,
            margin_bottom=6,
            margin_start=6,
            margin_end=6,
        )
        self._table_name = Gtk.Entry(
            placeholder_text="table_name", hexpand=True
        )
        self._table_name.connect("changed", lambda *_: self._refresh_preview())
        add = Gtk.Button(label="Add Column")
        add.connect("clicked", lambda *_: self._add_row())
        create = Gtk.Button(label="Create")
        create.add_css_class("suggested-action")
        create.set_tooltip_text(
            "Show the generated CREATE TABLE for review, then run it"
        )
        create.connect("clicked", self._on_create_clicked)
        bar.append(self._table_name)
        bar.append(add)
        bar.append(create)
        self.append(bar)

        self._list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self._list.add_css_class("boxed-list-separate")
        scroller = Gtk.ScrolledWindow(child=self._list, vexpand=True)
        self.append(scroller)

        caption = Gtk.Label(
            label="Generated statement",
            xalign=0,
            margin_start=6,
            margin_top=6,
        )
        caption.add_css_class("dim-label")
        caption.add_css_class("caption")
        self.append(caption)
        self._preview = SqlEditor(editable=False)
        self._preview.set_size_request(-1, 160)
        self.append(self._preview)

        self._add_row()

        def work():
            connector = self._ensure(self.profile)
            return connector, connector.column_types()

        def ready(loaded) -> None:
            self._connector, self._types = loaded
            for row in self._rows:
                row.set_types(self._types)
            self._refresh_preview()

        run_async(work, ready, lambda exc: self._show_error(str(exc)))

    def tab_state(self) -> None:
        return None  # session-only

    def _add_row(self) -> None:
        row = _ColumnRow(self._types, self._refresh_preview, self._remove_row)
        self._rows.append(row)
        self._list.append(row)
        self._refresh_preview()

    def _remove_row(self, row: _ColumnRow) -> None:
        if len(self._rows) == 1:
            self._show_error("A table needs at least one column")
            return
        self._rows.remove(row)
        self._list.remove(row)
        self._refresh_preview()

    def _build_sql(self) -> str:
        """The CREATE statement for the current form, or "" while the
        form is incomplete (no table name / no named column / adapter
        still connecting)."""
        if self._connector is None:
            return ""
        table = self._table_name.get_text().strip()
        columns = [c for c in (r.column() for r in self._rows) if c]
        if not table or not columns:
            return ""
        defaults = {
            row.name.get_text().strip(): row.default.get_text()
            for row in self._rows
            if row.name.get_text().strip() and row.default.get_text().strip()
        }
        return self._connector.create_table_sql(table, columns, defaults)

    def _refresh_preview(self) -> None:
        sql = self._build_sql()
        if sql:
            self._preview.set_text(sql + ";")
        elif self._connector is None:
            self._preview.set_text("-- Connecting…")
        else:
            self._preview.set_text(
                "-- Name the table and at least one column."
            )

    def _on_create_clicked(self, *_args) -> None:
        sql = self._build_sql()
        if not sql:
            self._show_error("Name the table and at least one column first")
            return
        UpdatePreviewDialog(
            [sql + ";"],
            lambda: self._execute(sql),
            caption="Runs one CREATE TABLE statement on "
            f"“{self.profile.name}”.",
        ).present(self)

    def _execute(self, sql: str) -> None:
        table = self._table_name.get_text().strip()

        def done(_result) -> None:
            if self.on_ran is not None:
                self.on_ran(sql, True)
            self._on_created(table)

        def failed(exc: Exception) -> None:
            self._show_error(str(exc))
            if self.on_ran is not None:
                self.on_ran(sql, False)

        run_async(
            lambda: self._ensure(self.profile).execute(sql), done, failed
        )
