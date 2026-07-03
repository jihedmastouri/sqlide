"""Data grid widgets.

ResultGrid: a Gtk.ColumnView whose columns are built at runtime from a
result set. Reused by table tabs and the query console. When editable,
cells are Gtk.EditableLabel and committed edits go through a callback.

TableTab: a ResultGrid bound to one table — paged loading, refresh, and
primary-key-based cell editing via Connector.update_cell().
"""

from __future__ import annotations

from typing import Any, Callable

from gi.repository import Gio, GObject, Gtk, Pango

from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.db.base import ColumnInfo, Connector
from sqlide.backend.workspaces import TabState
from sqlide.frontend.util import run_async

PAGE_SIZE = 500

# on_edit(row_item, column_index, new_text)
EditCallback = Callable[["RowItem", int, str], None]


class RowItem(GObject.Object):
    """One result row; values indexed by column position."""

    def __init__(self, values: tuple) -> None:
        super().__init__()
        self.values: list[Any] = list(values)


class ResultGrid(Gtk.ScrolledWindow):
    def __init__(self, on_edit: EditCallback | None = None) -> None:
        super().__init__(vexpand=True, hexpand=True)
        self.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self._on_edit = on_edit
        self._store = Gio.ListStore(item_type=RowItem)
        self._view = Gtk.ColumnView(
            model=Gtk.NoSelection(model=self._store), hexpand=True
        )
        self._view.add_css_class("data-table")
        self._view.set_show_row_separators(True)
        self._view.set_show_column_separators(True)
        self.set_child(self._view)

    def clear(self) -> None:
        self.set_result([], [])

    def set_result(
        self, columns: list[str], rows: list[tuple], editable: bool = False
    ) -> None:
        old = self._view.get_columns()
        for col in [old.get_item(i) for i in range(old.get_n_items())]:
            self._view.remove_column(col)
        self._store.remove_all()

        editable = editable and self._on_edit is not None
        for index, name in enumerate(columns):
            factory = Gtk.SignalListItemFactory()
            if editable:
                factory.connect("setup", self._setup_editable, index)
                factory.connect("bind", self._bind_editable, index)
            else:
                factory.connect("setup", self._setup_label)
                factory.connect("bind", self._bind_label, index)
            column = Gtk.ColumnViewColumn(title=name, factory=factory)
            column.set_resizable(True)
            column.set_expand(True)
            self._view.append_column(column)

        for row in rows:
            self._store.append(RowItem(row))

    # Read-only cells

    def _setup_label(self, factory, list_item) -> None:
        label = Gtk.Label(xalign=0)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.set_max_width_chars(50)
        list_item.set_child(label)

    def _bind_label(self, factory, list_item, index) -> None:
        label = list_item.get_child()
        value = list_item.get_item().values[index]
        if value is None:
            label.set_text("NULL")
            label.add_css_class("dim-label")
        else:
            label.set_text(str(value))
            label.remove_css_class("dim-label")

    # Editable cells

    def _setup_editable(self, factory, list_item, index) -> None:
        widget = Gtk.EditableLabel()
        # The ListItem is recycled across rows; resolve the current row
        # with get_item() at commit time, not here.
        widget.connect("notify::editing", self._on_editing_changed, list_item, index)
        list_item.set_child(widget)

    def _bind_editable(self, factory, list_item, index) -> None:
        widget = list_item.get_child()
        value = list_item.get_item().values[index]
        widget.set_text("" if value is None else str(value))

    def _on_editing_changed(self, widget, _pspec, list_item, index) -> None:
        if widget.get_property("editing"):
            return  # editing just started
        row = list_item.get_item()
        if row is None:
            return
        old = row.values[index]
        old_text = "" if old is None else str(old)
        new_text = widget.get_text()
        if new_text != old_text:
            self._on_edit(row, index, new_text)


class TableTab(Gtk.Box):
    """Content of one "table data" tab: paged grid + action bar."""

    def __init__(
        self,
        profile: ConnectionProfile,
        table: str,
        ensure_connector: Callable[[ConnectionProfile], Connector],
        show_error: Callable[[str], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.profile = profile
        self.table = table
        self._ensure = ensure_connector
        self._show_error = show_error
        self._offset = 0
        self._columns: list[ColumnInfo] = []
        self._result_names: list[str] = []

        self._grid = ResultGrid(on_edit=self._commit_edit)
        self.append(self._grid)

        bar = Gtk.ActionBar()
        self._prev = Gtk.Button(icon_name="go-previous-symbolic")
        self._prev.set_tooltip_text("Previous page")
        self._prev.connect("clicked", self._on_prev)
        self._next = Gtk.Button(icon_name="go-next-symbolic")
        self._next.set_tooltip_text("Next page")
        self._next.connect("clicked", self._on_next)
        self._page_label = Gtk.Label()
        bar.pack_start(self._prev)
        bar.pack_start(self._page_label)
        bar.pack_start(self._next)

        refresh = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh.set_tooltip_text("Refresh")
        refresh.connect("clicked", lambda *_: self.reload())
        self._mode_label = Gtk.Label()
        self._mode_label.add_css_class("dim-label")
        bar.pack_end(refresh)
        bar.pack_end(self._mode_label)
        self.append(bar)

        self._prev.set_sensitive(False)
        self._next.set_sensitive(False)
        self.reload()

    def tab_state(self) -> TabState:
        return TabState(
            kind="table", connection=self.profile.name, table=self.table
        )

    def reload(self) -> None:
        offset = self._offset

        def work():
            connector = self._ensure(self.profile)
            columns = connector.list_columns(self.table)
            result = connector.fetch_rows(self.table, offset, PAGE_SIZE)
            return columns, result

        def done(loaded):
            self._columns, result = loaded
            self._result_names = result.columns
            editable = any(c.is_pk for c in self._columns)
            self._grid.set_result(result.columns, result.rows, editable=editable)
            count = len(result)
            self._page_label.set_text(
                f"{offset + 1}–{offset + count}" if count else "no rows"
            )
            self._prev.set_sensitive(offset > 0)
            self._next.set_sensitive(count == PAGE_SIZE)
            self._mode_label.set_text(
                "" if editable else "read-only (no primary key)"
            )

        run_async(work, done, lambda exc: self._show_error(str(exc)))

    def _on_prev(self, *_args) -> None:
        self._offset = max(0, self._offset - PAGE_SIZE)
        self.reload()

    def _on_next(self, *_args) -> None:
        self._offset += PAGE_SIZE
        self.reload()

    def _commit_edit(self, row: RowItem, index: int, new_text: str) -> None:
        column_name = self._result_names[index]
        try:
            pk_values = {
                c.name: row.values[self._result_names.index(c.name)]
                for c in self._columns
                if c.is_pk
            }
        except ValueError:
            self._show_error("Cannot edit: primary key column missing from result")
            return

        def work():
            self._ensure(self.profile).update_cell(
                self.table, pk_values, column_name, new_text
            )

        def done(_result):
            row.values[index] = new_text

        def failed(exc):
            self._show_error(str(exc))
            self.reload()

        run_async(work, done, failed)
