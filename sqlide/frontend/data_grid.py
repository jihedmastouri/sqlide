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
from sqlide.backend.db.base import (
    CONJUNCTIONS,
    FILTER_OPERATORS,
    NO_VALUE_OPERATORS,
    ColumnInfo,
    Connector,
    FilterCondition,
    SortSpec,
)
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


class _FilterRow(Gtk.Box):
    """One line of the filter panel: [AND/OR] column operator value [–].

    The first line has nothing above it to join to, so its conjunction
    dropdown is kept but disabled (set_first) to preserve alignment.
    """

    def __init__(
        self,
        columns: list[str],
        on_remove: Callable[["_FilterRow"], None],
        on_activate: Callable[[], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._conjunction = Gtk.DropDown(
            model=Gtk.StringList.new(list(CONJUNCTIONS))
        )
        self._column = Gtk.DropDown(model=Gtk.StringList.new(columns))
        self._op = Gtk.DropDown(model=Gtk.StringList.new(list(FILTER_OPERATORS)))
        self._op.connect("notify::selected", self._on_op_changed)
        self._value = Gtk.Entry(hexpand=True, placeholder_text="value")
        self._value.connect("activate", lambda *_: on_activate())
        remove = Gtk.Button(icon_name="list-remove-symbolic")
        remove.add_css_class("flat")
        remove.set_tooltip_text("Remove condition")
        remove.connect("clicked", lambda *_: on_remove(self))
        for widget in (self._conjunction, self._column, self._op, self._value, remove):
            self.append(widget)

    def set_first(self, is_first: bool) -> None:
        self._conjunction.set_sensitive(not is_first)

    def set_columns(self, names: list[str]) -> None:
        selected = self.selected_column()
        self._column.set_model(Gtk.StringList.new(names))
        if selected in names:
            self._column.set_selected(names.index(selected))

    def selected_column(self) -> str:
        return _selected_string(self._column)

    def condition(self) -> FilterCondition:
        return FilterCondition(
            column=self.selected_column(),
            op=_selected_string(self._op),
            value=self._value.get_text(),
            conjunction=_selected_string(self._conjunction),
        )

    def _on_op_changed(self, *_args) -> None:
        self._value.set_sensitive(
            _selected_string(self._op) not in NO_VALUE_OPERATORS
        )


def _selected_string(dropdown: Gtk.DropDown) -> str:
    item = dropdown.get_selected_item()
    return item.get_string() if item is not None else ""


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
        self._column_names: list[str] = []
        self._filters: list[FilterCondition] = []
        self._order_by: list[SortSpec] = []
        self._filter_rows: list[_FilterRow] = []

        self._filter_revealer = Gtk.Revealer(child=self._build_filter_panel())
        self.append(self._filter_revealer)

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
        self._filter_toggle = Gtk.ToggleButton(icon_name="edit-find-symbolic")
        self._filter_toggle.set_tooltip_text("Filter and sort")
        self._filter_toggle.connect("toggled", self._on_filter_toggled)
        self._mode_label = Gtk.Label()
        self._mode_label.add_css_class("dim-label")
        bar.pack_end(refresh)
        bar.pack_end(self._filter_toggle)
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
        filters = self._filters
        order_by = self._order_by

        def work():
            connector = self._ensure(self.profile)
            columns = connector.list_columns(self.table)
            result = connector.fetch_rows(
                self.table, offset, PAGE_SIZE, filters=filters, order_by=order_by
            )
            return columns, result

        def done(loaded):
            self._columns, result = loaded
            self._result_names = result.columns
            self._set_column_names([c.name for c in self._columns])
            editable = any(c.is_pk for c in self._columns)
            self._grid.set_result(result.columns, result.rows, editable=editable)
            count = len(result)
            page = f"{offset + 1}–{offset + count}" if count else "no rows"
            if filters:
                page += " (filtered)"
            self._page_label.set_text(page)
            self._prev.set_sensitive(offset > 0)
            self._next.set_sensitive(count == PAGE_SIZE)
            self._mode_label.set_text(
                "" if editable else "read-only (no primary key)"
            )

        run_async(work, done, lambda exc: self._show_error(str(exc)))

    # Filter panel

    def _build_filter_panel(self) -> Gtk.Box:
        panel = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=6,
            margin_top=6,
            margin_bottom=6,
            margin_start=6,
            margin_end=6,
        )
        self._filter_rows_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=6
        )
        panel.append(self._filter_rows_box)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        add = Gtk.Button(label="Add condition")
        add.connect("clicked", lambda *_: self._add_filter_row())
        controls.append(add)

        order_label = Gtk.Label(label="Order by", margin_start=12)
        order_label.add_css_class("dim-label")
        controls.append(order_label)
        self._sort_column = Gtk.DropDown(model=Gtk.StringList.new(["(none)"]))
        controls.append(self._sort_column)
        self._sort_direction = Gtk.DropDown(
            model=Gtk.StringList.new(["Ascending", "Descending"])
        )
        controls.append(self._sort_direction)

        controls.append(Gtk.Box(hexpand=True))
        clear = Gtk.Button(label="Clear")
        clear.connect("clicked", lambda *_: self._clear_filters())
        controls.append(clear)
        apply = Gtk.Button(label="Apply")
        apply.add_css_class("suggested-action")
        apply.connect("clicked", lambda *_: self._apply_filters())
        controls.append(apply)
        panel.append(controls)
        return panel

    def _on_filter_toggled(self, toggle: Gtk.ToggleButton) -> None:
        if toggle.get_active() and not self._filter_rows:
            self._add_filter_row()
        self._filter_revealer.set_reveal_child(toggle.get_active())

    def _add_filter_row(self) -> None:
        row = _FilterRow(
            self._column_names, self._remove_filter_row, self._apply_filters
        )
        self._filter_rows.append(row)
        self._filter_rows_box.append(row)
        self._update_first_row()

    def _remove_filter_row(self, row: _FilterRow) -> None:
        self._filter_rows.remove(row)
        self._filter_rows_box.remove(row)
        self._update_first_row()

    def _update_first_row(self) -> None:
        for index, row in enumerate(self._filter_rows):
            row.set_first(index == 0)

    def _set_column_names(self, names: list[str]) -> None:
        if names == self._column_names:
            return
        self._column_names = names
        for row in self._filter_rows:
            row.set_columns(names)
        selected = _selected_string(self._sort_column)
        choices = ["(none)"] + names
        self._sort_column.set_model(Gtk.StringList.new(choices))
        if selected in choices:
            self._sort_column.set_selected(choices.index(selected))

    def _apply_filters(self) -> None:
        self._filters = [
            row.condition() for row in self._filter_rows if row.selected_column()
        ]
        sort_column = _selected_string(self._sort_column)
        self._order_by = (
            [
                SortSpec(
                    column=sort_column,
                    descending=self._sort_direction.get_selected() == 1,
                )
            ]
            if sort_column and sort_column != "(none)"
            else []
        )
        self._offset = 0
        self.reload()

    def _clear_filters(self) -> None:
        for row in list(self._filter_rows):
            self._remove_filter_row(row)
        self._sort_column.set_selected(0)
        self._sort_direction.set_selected(0)
        if self._filters or self._order_by:
            self._filters = []
            self._order_by = []
            self._offset = 0
            self.reload()

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
