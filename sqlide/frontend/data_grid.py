"""Data grid widgets.

ResultGrid: a Gtk.ColumnView whose columns are built at runtime from a
result set. Reused by table tabs and the query console. When editable,
cells are Gtk.EditableLabel and committed edits go through a callback.
Editing is locked until set_unlocked(True); mark_modified() highlights
cells with uncommitted changes.

Columns can be dragged to reorder and resized at their edges. Cells are
selectable for copying: click selects a cell, dragging (or Shift+click)
extends to a rectangular block, and the context menu selects a whole
row or column.
Ctrl+C (or the context menu's Copy) copies the selection as
tab-separated text; row and block selections include a header line with
the column names, following the current display order of the columns.
"Copy As" offers CSV, INSERT statements, pretty (ASCII table) and
Markdown. "Aggregate" computes a summary (count/sum/avg/min/max) of
the selected cells and hands it to the on_aggregate callback, which
the window routes to the Aggregate page of the right side panel.

TableTab: a ResultGrid bound to one table — paged loading, refresh, and
primary-key-based cell editing. Editing is opt-in: a toggle in the
action bar unlocks the cells, edits accumulate locally (highlighted in
the grid), and Save opens a review dialog showing the UPDATE statements
before they run through Connector.update_cell(). Refresh discards
pending edits.
"""

from __future__ import annotations

from typing import Any, Callable

import csv
import io
import json

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango

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
    def __init__(
        self,
        on_edit: EditCallback | None = None,
        table_name: str | None = None,
        on_aggregate: Callable[[list[str]], None] | None = None,
        on_header_sort: Callable[[list[tuple[str, bool]]], None] | None = None,
    ) -> None:
        super().__init__(vexpand=True, hexpand=True)
        self.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self._on_edit = on_edit
        self._aggregate_cb = on_aggregate
        # When set, column headers become clickable sort toggles: the
        # grid never sorts locally (the model ignores the view sorter);
        # it reports the clicked-together column list as (name,
        # descending) pairs, primary first, so the owner can re-query.
        self._on_header_sort = on_header_sort
        self._updating_columns = False
        # Used by "Copy As > INSERT Statement"; falls back to a placeholder.
        self.table_name = table_name
        self._store = Gio.ListStore(item_type=RowItem)
        self._view = Gtk.ColumnView(
            model=Gtk.NoSelection(model=self._store), hexpand=True
        )
        self._view.add_css_class("data-table")
        self._view.set_show_row_separators(True)
        self._view.set_show_column_separators(True)
        self._view.set_reorderable(True)
        if on_header_sort is not None:
            self._view.get_sorter().connect("changed", self._sorter_changed)
        self.set_child(self._view)

        self._column_names: list[str] = []
        # ColumnViewColumn objects in data order; get_columns() gives the
        # display order after the user drags headers around.
        self._column_objs: list[Gtk.ColumnViewColumn] = []
        # Selected cells = _sel_rows × _sel_cols (data column indices).
        self._sel_rows: set[int] = set()
        self._sel_cols: set[int] = set()
        self._sel_kind: str | None = None  # "cell" | "block" | "row" | "column"
        self._editable_grid = False  # cells are EditableLabels
        self._unlocked = False  # edits allowed right now
        self._modified: set[tuple[int, int]] = set()  # (row, data col)
        self._anchor: tuple[int, int] | None = None
        # Currently bound cell widgets, for restyling on selection change.
        self._bound_cells: dict[Gtk.Widget, tuple[Gtk.ListItem, int]] = {}
        self._menu_cell: tuple[int, int] = (0, 0)
        self._menu_rect = Gdk.Rectangle()

        actions = Gio.SimpleActionGroup()
        for name, callback in (
            ("select-row", self._on_select_row),
            ("select-column", self._on_select_column),
            ("copy", lambda *_: self.copy_selection()),
            ("aggregate", self._on_aggregate),
            ("move-left", lambda *_: self._move_menu_column(-1)),
            ("move-right", lambda *_: self._move_menu_column(1)),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            actions.add_action(action)
        copy_as = Gio.SimpleAction.new("copy-as", GLib.VariantType.new("s"))
        copy_as.connect(
            "activate", lambda _a, param: self.copy_selection(param.get_string())
        )
        actions.add_action(copy_as)
        self._view.insert_action_group("grid", actions)

        menu = Gio.Menu()
        menu.append("Select Row", "grid.select-row")
        menu.append("Select Column", "grid.select-column")
        copy_section = Gio.Menu()
        copy_section.append("Copy", "grid.copy")
        copy_as_menu = Gio.Menu()
        for label, fmt in (
            ("CSV", "csv"),
            ("INSERT Statement", "insert"),
            ("JSON", "json"),
            ("Pretty", "pretty"),
            ("Markdown", "markdown"),
        ):
            copy_as_menu.append(label, f"grid.copy-as::{fmt}")
        copy_section.append_submenu("Copy As", copy_as_menu)
        copy_section.append("Aggregate", "grid.aggregate")
        menu.append_section(None, copy_section)
        # Columns can also be reordered by dragging their headers; the
        # menu items cover the cell-menu path.
        move_section = Gio.Menu()
        move_section.append("Move Column Left", "grid.move-left")
        move_section.append("Move Column Right", "grid.move-right")
        menu.append_section(None, move_section)
        self._popover = Gtk.PopoverMenu.new_from_model(menu)
        self._popover.set_parent(self._view)
        self._popover.set_has_arrow(False)

        self._view.connect("destroy", self._on_view_destroy)

        # Drag with the primary button to select a rectangular block
        # (mouse-only alternative to Shift+click). The gesture sits on
        # the view; it stays unclaimed until the pointer crosses into a
        # different cell, so plain clicks, in-cell edits and header
        # drags (reorder/resize) are unaffected.
        self._drag_anchor: tuple[int, int] | None = None
        self._drag_start = (0.0, 0.0)
        drag = Gtk.GestureDrag(button=Gdk.BUTTON_PRIMARY)
        drag.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        self._view.add_controller(drag)

        shortcuts = Gtk.ShortcutController()
        shortcuts.set_scope(Gtk.ShortcutScope.LOCAL)
        shortcuts.add_shortcut(
            Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string("<Control>c"),
                Gtk.CallbackAction.new(self._on_copy_shortcut),
            )
        )
        self._view.add_controller(shortcuts)

    def clear(self) -> None:
        self.set_result([], [])

    def set_result(
        self, columns: list[str], rows: list[tuple], editable: bool = False
    ) -> None:
        # Rebuilding columns disturbs the view sorter; those changes
        # are not header clicks, so keep them out of on_header_sort.
        self._updating_columns = True
        try:
            self._set_result(columns, rows, editable)
        finally:
            self._updating_columns = False

    def _set_result(
        self, columns: list[str], rows: list[tuple], editable: bool
    ) -> None:
        old = self._view.get_columns()
        for col in [old.get_item(i) for i in range(old.get_n_items())]:
            self._view.remove_column(col)
        self._store.remove_all()
        self._column_names = list(columns)
        self._column_objs = []
        self._bound_cells.clear()
        self._sel_rows = set()
        self._sel_cols = set()
        self._sel_kind = None
        self._anchor = None
        self._modified = set()

        editable = editable and self._on_edit is not None
        self._editable_grid = editable
        for index, name in enumerate(columns):
            factory = Gtk.SignalListItemFactory()
            if editable:
                factory.connect("setup", self._setup_editable, index)
                factory.connect("bind", self._bind_editable, index)
            else:
                factory.connect("setup", self._setup_label)
                factory.connect("bind", self._bind_label, index)
            factory.connect("unbind", self._unbind_cell)
            column = Gtk.ColumnViewColumn(title=name, factory=factory)
            column.set_resizable(True)
            column.set_expand(True)
            if self._on_header_sort is not None:
                # A no-op sorter: it only makes the header clickable and
                # the sort arrow visible; the data order comes from the
                # owner's re-query, never from a local sort.
                column.set_sorter(Gtk.CustomSorter.new(None))
            self._column_objs.append(column)
            self._view.append_column(column)

        for row in rows:
            self._store.append(RowItem(row))

    def set_sort_state(self, order: list[tuple[str, bool]]) -> None:
        """Show sort arrows matching (column name, descending) pairs,
        primary first — the owner calls this after a re-query so header
        state survives the column rebuild. Unknown names are skipped."""
        if self._on_header_sort is None:
            return
        self._updating_columns = True
        try:
            self._view.sort_by_column(None, Gtk.SortType.ASCENDING)
            # Least-significant first: each call makes its column the
            # primary and demotes the previous ones, like user clicks.
            for name, descending in reversed(order):
                if name not in self._column_names:
                    continue
                self._view.sort_by_column(
                    self._column_objs[self._column_names.index(name)],
                    Gtk.SortType.DESCENDING
                    if descending
                    else Gtk.SortType.ASCENDING,
                )
        finally:
            self._updating_columns = False

    def _sorter_changed(self, sorter, _change) -> None:
        if self._updating_columns:
            return
        pairs = [
            (
                column.get_title(),
                order == Gtk.SortType.DESCENDING,
            )
            for i in range(sorter.get_n_sort_columns())
            for column, order in (sorter.get_nth_sort_column(i),)
        ]
        self._on_header_sort(pairs)

    # Read-only cells

    def _setup_label(self, factory, list_item) -> None:
        label = Gtk.Label(xalign=0, hexpand=True)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.set_max_width_chars(50)
        self._attach_cell_gesture(label)
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
        self._register_cell(label, list_item, index)

    # Editable cells

    def _setup_editable(self, factory, list_item, index) -> None:
        widget = Gtk.EditableLabel(hexpand=True)
        # The ListItem is recycled across rows; resolve the current row
        # with get_item() at commit time, not here.
        widget.connect("notify::editing", self._on_editing_changed, list_item, index)
        self._attach_cell_gesture(widget)
        list_item.set_child(widget)

    def _bind_editable(self, factory, list_item, index) -> None:
        widget = list_item.get_child()
        value = list_item.get_item().values[index]
        widget.set_text("" if value is None else str(value))
        widget.set_editable(self._unlocked)
        self._register_cell(widget, list_item, index)

    def set_unlocked(self, unlocked: bool) -> None:
        """Allow or forbid starting cell edits (the lock is enforced in
        the click gesture; set_editable is a second layer so a stray
        edit cannot change text)."""
        self._unlocked = unlocked
        for widget in self._bound_cells:
            if isinstance(widget, Gtk.EditableLabel):
                widget.set_editable(unlocked)

    def mark_modified(self, row: RowItem, col: int) -> None:
        """Highlight a cell as locally edited but not yet saved."""
        found, position = self._store.find(row)
        if found:
            self._modified.add((position, col))
            self._restyle_cells()

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

    # Selection

    def _attach_cell_gesture(self, widget) -> None:
        # No user data on the connection: the handler resolves the cell
        # through _bound_cells. A closure ref back to the widget (or its
        # ListItem) from its own controller crashes GTK at teardown.
        click = Gtk.GestureClick(button=0)
        # Capture phase so a claimed press (Shift+click, right-click)
        # never reaches an EditableLabel and starts an edit.
        click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        click.connect("pressed", self._on_cell_pressed)
        widget.add_controller(click)

    def _register_cell(self, widget, list_item, index) -> None:
        self._bound_cells[widget] = (list_item, index)
        self._style_cell(widget, list_item.get_position(), index)

    def _unbind_cell(self, factory, list_item) -> None:
        self._bound_cells.pop(list_item.get_child(), None)

    def _style_cell(self, widget, row: int, col: int) -> None:
        if row in self._sel_rows and col in self._sel_cols:
            widget.add_css_class("cell-selected")
        else:
            widget.remove_css_class("cell-selected")
        if (row, col) in self._modified:
            widget.add_css_class("cell-modified")
        else:
            widget.remove_css_class("cell-modified")

    def _restyle_cells(self) -> None:
        for widget, (list_item, col) in self._bound_cells.items():
            self._style_cell(widget, list_item.get_position(), col)

    def _select(self, rows: set[int], cols: set[int], kind: str) -> None:
        self._sel_rows = rows
        self._sel_cols = cols
        self._sel_kind = kind
        self._restyle_cells()

    def _on_cell_pressed(self, gesture, _n_press, x, y) -> None:
        widget = gesture.get_widget()
        bound = self._bound_cells.get(widget)
        if bound is None:
            return
        list_item, index = bound
        row = list_item.get_position()
        if row == Gtk.INVALID_LIST_POSITION:
            return
        button = gesture.get_current_button()
        state = gesture.get_current_event_state()
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        if button == Gdk.BUTTON_SECONDARY:
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            if row not in self._sel_rows or index not in self._sel_cols:
                self._anchor = (row, index)
                self._select({row}, {index}, "cell")
            self._menu_cell = (row, index)
            self._popup_menu(widget, x, y)
        elif button == Gdk.BUTTON_PRIMARY:
            if shift and self._anchor is not None:
                gesture.set_state(Gtk.EventSequenceState.CLAIMED)
                self._select_block(self._anchor, (row, index))
            else:
                if self._editable_grid and not self._unlocked:
                    # Locked: swallow the press so the EditableLabel
                    # never sees it and cannot start an edit.
                    gesture.set_state(Gtk.EventSequenceState.CLAIMED)
                # When unlocked the press is not claimed, so the same
                # click still starts the edit.
                self._anchor = (row, index)
                self._select({row}, {index}, "cell")

    def _cell_at(self, x: float, y: float) -> tuple[int, int] | None:
        """(row, data column) of the bound cell at view coordinates."""
        widget = self._view.pick(x, y, Gtk.PickFlags.DEFAULT)
        while widget is not None and widget not in self._bound_cells:
            widget = widget.get_parent()
        if widget is None:
            return None
        list_item, index = self._bound_cells[widget]
        row = list_item.get_position()
        if row == Gtk.INVALID_LIST_POSITION:
            return None
        return (row, index)

    def _on_drag_begin(self, _gesture, x, y) -> None:
        self._drag_start = (x, y)
        self._drag_anchor = self._cell_at(x, y)

    def _on_drag_update(self, gesture, dx, dy) -> None:
        if self._drag_anchor is None:
            return
        x, y = self._drag_start
        cell = self._cell_at(x + dx, y + dy)
        if cell is None or cell == self._drag_anchor:
            return
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self._anchor = self._drag_anchor
        self._select_block(self._drag_anchor, cell)

    def _select_block(self, anchor: tuple[int, int], end: tuple[int, int]) -> None:
        rows = set(range(min(anchor[0], end[0]), max(anchor[0], end[0]) + 1))
        # Column span is visual: use display positions, then map the
        # covered range back to data indices.
        order = self._display_order()
        pos = {data_index: p for p, data_index in enumerate(order)}
        first = min(pos[anchor[1]], pos[end[1]])
        last = max(pos[anchor[1]], pos[end[1]])
        cols = set(order[first : last + 1])
        self._select(rows, cols, "block")

    def _on_select_row(self, *_args) -> None:
        row, _col = self._menu_cell
        self._select({row}, set(range(len(self._column_names))), "row")

    def _on_select_column(self, *_args) -> None:
        _row, col = self._menu_cell
        self._select(set(range(self._store.get_n_items())), {col}, "column")

    def _popup_menu(self, widget, x, y) -> None:
        ok, bounds = widget.compute_bounds(self._view)
        rect = Gdk.Rectangle()
        rect.x = int(bounds.origin.x + x) if ok else 0
        rect.y = int(bounds.origin.y + y) if ok else 0
        rect.width = rect.height = 1
        self._menu_rect = rect
        self._popover.set_pointing_to(rect)
        self._popover.popup()

    def _on_view_destroy(self, *_args) -> None:
        self._popover.unparent()

    # Copy

    def _display_order(self) -> list[int]:
        """Data column indices in current display order."""
        columns = self._view.get_columns()
        return [
            self._column_objs.index(columns.get_item(i))
            for i in range(columns.get_n_items())
        ]

    def _move_menu_column(self, delta: int) -> None:
        """Move the right-clicked column one display position left or
        right (same effect as dragging its header)."""
        _row, col = self._menu_cell
        if not 0 <= col < len(self._column_objs):
            return
        order = self._display_order()
        position = order.index(col)
        target = position + delta
        if not 0 <= target < len(order):
            return
        column = self._column_objs[col]
        # Re-inserting rebuilds header sort state; not a header click.
        self._updating_columns = True
        try:
            self._view.remove_column(column)
            self._view.insert_column(target, column)
        finally:
            self._updating_columns = False

    def _on_copy_shortcut(self, _widget, _args) -> bool:
        return self.copy_selection()

    def copy_selection(self, fmt: str = "default") -> bool:
        data = self._selection_data()
        if data is None:
            return False
        headers, rows = data
        formatter = {
            "default": self._format_default,
            "csv": _format_csv,
            "insert": self._format_insert,
            "json": _format_json,
            "pretty": _format_pretty,
            "markdown": _format_markdown,
        }[fmt]
        self.get_clipboard().set(formatter(headers, rows))
        return True

    def _selection_data(self) -> tuple[list[str], list[list[Any]]] | None:
        """Selected cells as (header names, row values), in display order."""
        if not self._sel_rows or not self._sel_cols:
            return None
        order = [i for i in self._display_order() if i in self._sel_cols]
        headers = [self._column_names[i] for i in order]
        rows = []
        for row in sorted(self._sel_rows):
            item = self._store.get_item(row)
            if item is not None:
                rows.append([item.values[i] for i in order])
        return headers, rows

    def _format_default(self, headers: list[str], rows: list[list[Any]]) -> str:
        lines = []
        if self._sel_kind in ("row", "block"):
            lines.append("\t".join(headers))
        lines.extend("\t".join(_cell_text(v) for v in row) for row in rows)
        return "\n".join(lines)

    def _format_insert(self, headers: list[str], rows: list[list[Any]]) -> str:
        table = self.table_name or "table_name"
        columns = ", ".join(headers)
        return "\n".join(
            f"INSERT INTO {table} ({columns}) "
            f"VALUES ({', '.join(_sql_literal(v) for v in row)});"
            for row in rows
        )

    # Aggregate

    def _on_aggregate(self, *_args) -> None:
        data = self._selection_data()
        if data is None or self._aggregate_cb is None:
            return
        _headers, rows = data
        values = [v for row in rows for v in row]
        numbers = []
        for value in values:
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                numbers.append(value)
            elif isinstance(value, str):
                try:
                    numbers.append(float(value))
                except ValueError:
                    pass
        lines = [
            f"Count\t{len(values)}",
            f"Not NULL\t{sum(1 for v in values if v is not None)}",
        ]
        if numbers:
            lines += [
                f"Sum\t{_format_number(sum(numbers))}",
                f"Avg\t{_format_number(sum(numbers) / len(numbers))}",
                f"Min\t{_format_number(min(numbers))}",
                f"Max\t{_format_number(max(numbers))}",
            ]
        self._aggregate_cb(lines)


def _cell_text(value: Any) -> str:
    return "NULL" if value is None else str(value)


def _sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _format_number(value: float) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _format_json(headers: list[str], rows: list[list[Any]]) -> str:
    return json.dumps(
        [
            {
                header: value if _json_safe(value) else str(value)
                for header, value in zip(headers, row)
            }
            for row in rows
        ],
        indent=2,
        ensure_ascii=False,
    )


def _json_safe(value: Any) -> bool:
    return value is None or isinstance(value, (bool, int, float, str))


def _format_csv(headers: list[str], rows: list[list[Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(headers)
    for row in rows:
        writer.writerow("" if v is None else v for v in row)
    return buffer.getvalue().rstrip("\n")


def _format_pretty(headers: list[str], rows: list[list[Any]]) -> str:
    cells = [headers] + [[_cell_text(v) for v in row] for row in rows]
    widths = [max(len(line[i]) for line in cells) for i in range(len(headers))]
    rule = "+" + "+".join("-" * (w + 2) for w in widths) + "+"

    def line(values: list[str]) -> str:
        return "| " + " | ".join(v.ljust(w) for v, w in zip(values, widths)) + " |"

    body = [line(values) for values in cells[1:]]
    return "\n".join([rule, line(cells[0]), rule, *body, rule])


def _format_markdown(headers: list[str], rows: list[list[Any]]) -> str:
    def line(values: list[str]) -> str:
        return "| " + " | ".join(v.replace("|", "\\|") for v in values) + " |"

    lines = [
        line(headers),
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend(line([_cell_text(v) for v in row]) for row in rows)
    return "\n".join(lines)


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


class _SortRow(Gtk.Box):
    """One line of the sort panel: column, direction, and controls to
    move the line up/down (ORDER BY priority follows the line order)
    or remove it."""

    def __init__(
        self,
        columns: list[str],
        on_remove: Callable[["_SortRow"], None],
        on_move: Callable[["_SortRow", int], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._column = Gtk.DropDown(
            model=Gtk.StringList.new(columns), hexpand=True
        )
        self._direction = Gtk.DropDown(
            model=Gtk.StringList.new(["Ascending", "Descending"])
        )
        up = Gtk.Button(icon_name="go-up-symbolic")
        up.add_css_class("flat")
        up.set_tooltip_text("Sort by this column earlier")
        up.connect("clicked", lambda *_: on_move(self, -1))
        down = Gtk.Button(icon_name="go-down-symbolic")
        down.add_css_class("flat")
        down.set_tooltip_text("Sort by this column later")
        down.connect("clicked", lambda *_: on_move(self, 1))
        remove = Gtk.Button(icon_name="list-remove-symbolic")
        remove.add_css_class("flat")
        remove.set_tooltip_text("Remove sort column")
        remove.connect("clicked", lambda *_: on_remove(self))
        for widget in (self._column, self._direction, up, down, remove):
            self.append(widget)

    def set_columns(self, names: list[str]) -> None:
        selected = self.selected_column()
        self._column.set_model(Gtk.StringList.new(names))
        if selected in names:
            self._column.set_selected(names.index(selected))

    def selected_column(self) -> str:
        return _selected_string(self._column)

    def spec(self) -> SortSpec:
        return SortSpec(
            column=self.selected_column(),
            descending=self._direction.get_selected() == 1,
        )

    def set_spec(self, spec: SortSpec) -> None:
        model = self._column.get_model()
        for i in range(model.get_n_items()):
            if model.get_string(i) == spec.column:
                self._column.set_selected(i)
                break
        self._direction.set_selected(1 if spec.descending else 0)


def _selected_string(dropdown: Gtk.DropDown) -> str:
    item = dropdown.get_selected_item()
    return item.get_string() if item is not None else ""


class UpdatePreviewDialog(Adw.Dialog):
    """Review step before saving cell edits: shows the UPDATE statements
    that will run, with Cancel / Execute. Values are bound as parameters
    at execution time; the preview renders them as SQL literals."""

    def __init__(
        self,
        statements: list[str],
        on_execute: Callable[[], None],
        caption: str = "Values are bound as parameters when executed.",
    ) -> None:
        super().__init__(
            title=f"Review Changes ({len(statements)})",
            content_width=560,
            content_height=400,
        )
        header = Adw.HeaderBar()
        header.set_show_start_title_buttons(False)
        header.set_show_end_title_buttons(False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        execute = Gtk.Button(label="Execute")
        execute.add_css_class("destructive-action")
        execute.connect("clicked", self._on_execute_clicked)
        header.pack_start(cancel)
        header.pack_end(execute)
        self._on_execute = on_execute

        text = Gtk.TextView(
            editable=False,
            monospace=True,
            cursor_visible=False,
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
            left_margin=12,
            right_margin=12,
            top_margin=12,
            bottom_margin=12,
        )
        text.get_buffer().set_text("\n".join(statements))
        scroller = Gtk.ScrolledWindow(child=text, vexpand=True)

        caption = Gtk.Label(
            label=caption,
            xalign=0,
            margin_start=12,
            margin_end=12,
            margin_bottom=6,
        )
        caption.add_css_class("dim-label")

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(scroller)
        content.append(caption)
        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(content)
        self.set_child(view)

    def _on_execute_clicked(self, *_args) -> None:
        self.close()
        self._on_execute()


class TableTab(Gtk.Box):
    """Content of one "table data" tab: paged grid + action bar.

    Cell editing is locked until the pencil toggle is pressed. Edits are
    held locally (pending) and only hit the database after Save, which
    first shows the UPDATE statements in an UpdatePreviewDialog."""

    def __init__(
        self,
        profile: ConnectionProfile,
        table: str,
        ensure_connector: Callable[[ConnectionProfile], Connector],
        show_error: Callable[[str], None],
        on_aggregate: Callable[[list[str]], None] | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.profile = profile
        self.table = table
        self._ensure = ensure_connector
        self._show_error = show_error
        # Public: the window rebinds it after the tab page exists so
        # every grid load (select, filter, sort, paging) lands in the
        # query history under this tab's panel name.
        self.on_ran: Callable[[str, bool], None] | None = None
        self._offset = 0
        self._columns: list[ColumnInfo] = []
        self._result_names: list[str] = []
        self._column_names: list[str] = []
        self._filters: list[FilterCondition] = []
        self._order_by: list[SortSpec] = []
        self._filter_rows: list[_FilterRow] = []
        self._sort_rows: list[_SortRow] = []
        # Pending (unsaved) edits per row: pk values snapshotted at the
        # first edit of the row, then {column name: new text}.
        self._pending: dict[RowItem, tuple[dict[str, Any], dict[str, str]]] = {}

        # Filter and sort are separate panels behind separate toggles;
        # both can be revealed at the same time.
        self._filter_revealer = Gtk.Revealer(child=self._build_filter_panel())
        self.append(self._filter_revealer)
        self._sort_revealer = Gtk.Revealer(child=self._build_sort_panel())
        self.append(self._sort_revealer)

        self._grid = ResultGrid(
            on_edit=self._commit_edit,
            table_name=table,
            on_aggregate=on_aggregate,
            on_header_sort=self._on_header_sort,
        )
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
        refresh.set_tooltip_text("Refresh (discards unsaved edits)")
        refresh.connect("clicked", lambda *_: self.reload())
        self._filter_toggle = Gtk.ToggleButton(icon_name="edit-find-symbolic")
        self._filter_toggle.set_tooltip_text("Filter rows")
        self._filter_toggle.connect("toggled", self._on_filter_toggled)
        self._sort_toggle = Gtk.ToggleButton(
            icon_name="view-sort-descending-symbolic"
        )
        self._sort_toggle.set_tooltip_text("Sort rows")
        self._sort_toggle.connect("toggled", self._on_sort_toggled)
        self._edit_toggle = Gtk.ToggleButton(icon_name="document-edit-symbolic")
        self._edit_toggle.set_tooltip_text("Unlock editing")
        self._edit_toggle.set_sensitive(False)
        self._edit_toggle.connect("toggled", self._on_edit_toggled)
        self._save = Gtk.Button()
        self._save.add_css_class("suggested-action")
        self._save.set_visible(False)
        self._save.connect("clicked", self._on_save_clicked)
        self._mode_label = Gtk.Label()
        self._mode_label.add_css_class("dim-label")
        bar.pack_end(refresh)
        bar.pack_end(self._filter_toggle)
        bar.pack_end(self._sort_toggle)
        bar.pack_end(self._edit_toggle)
        bar.pack_end(self._save)
        bar.pack_end(self._mode_label)
        self.append(bar)

        self._prev.set_sensitive(False)
        self._next.set_sensitive(False)
        self.reload()

    def tab_state(self) -> TabState:
        return TabState(
            kind="table", connection=self.profile.name, table=self.table
        )

    def _describe_query(self, offset: int) -> str:
        """The SELECT this tab's current state stands for, with filter
        values inlined as literals — recorded in the query history (the
        adapters bind the real values as parameters)."""
        where = ""
        for cond in self._filters:
            clause = f"{cond.column} {cond.op}"
            if cond.op not in NO_VALUE_OPERATORS:
                clause += f" {_sql_literal(cond.value)}"
            where = (
                f"({where}) {cond.conjunction} {clause}" if where else clause
            )
        sql = f"SELECT * FROM {self.table}"
        if where:
            sql += f" WHERE {where}"
        if self._order_by:
            sql += " ORDER BY " + ", ".join(
                f"{s.column} {'DESC' if s.descending else 'ASC'}"
                for s in self._order_by
            )
        return sql + f" LIMIT {PAGE_SIZE} OFFSET {offset};"

    def reload(self) -> None:
        offset = self._offset
        filters = self._filters
        order_by = self._order_by
        history_sql = self._describe_query(offset)

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
            self._pending.clear()
            self._update_save_button()
            self._grid.set_result(result.columns, result.rows, editable=editable)
            self._grid.set_sort_state(
                [(s.column, s.descending) for s in order_by]
            )
            self._edit_toggle.set_sensitive(editable)
            self._grid.set_unlocked(editable and self._edit_toggle.get_active())
            count = len(result)
            page = f"{offset + 1}–{offset + count}" if count else "no rows"
            if filters:
                page += " (filtered)"
            if order_by:
                page += " (sorted)"
            self._page_label.set_text(page)
            self._prev.set_sensitive(offset > 0)
            self._next.set_sensitive(count == PAGE_SIZE)
            self._mode_label.set_text(
                "" if editable else "read-only (no primary key)"
            )
            if self.on_ran is not None:
                self.on_ran(history_sql, True)

        def failed(exc):
            self._show_error(str(exc))
            if self.on_ran is not None:
                self.on_ran(history_sql, False)

        run_async(work, done, failed)

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

    # Sort panel

    def _build_sort_panel(self) -> Gtk.Box:
        panel = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=6,
            margin_top=6,
            margin_bottom=6,
            margin_start=6,
            margin_end=6,
        )
        self._sort_rows_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=6
        )
        panel.append(self._sort_rows_box)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        add = Gtk.Button(label="Add sort column")
        add.connect("clicked", lambda *_: self._add_sort_row())
        controls.append(add)
        controls.append(Gtk.Box(hexpand=True))
        clear = Gtk.Button(label="Clear")
        clear.connect("clicked", lambda *_: self._clear_sort())
        controls.append(clear)
        apply = Gtk.Button(label="Apply")
        apply.add_css_class("suggested-action")
        apply.connect("clicked", lambda *_: self._apply_sort())
        controls.append(apply)
        panel.append(controls)
        return panel

    def _on_sort_toggled(self, toggle: Gtk.ToggleButton) -> None:
        if toggle.get_active() and not self._sort_rows:
            self._add_sort_row()
        self._sort_revealer.set_reveal_child(toggle.get_active())

    def _add_sort_row(self, spec: SortSpec | None = None) -> None:
        row = _SortRow(
            self._column_names, self._remove_sort_row, self._move_sort_row
        )
        if spec is not None:
            row.set_spec(spec)
        self._sort_rows.append(row)
        self._sort_rows_box.append(row)

    def _remove_sort_row(self, row: _SortRow) -> None:
        self._sort_rows.remove(row)
        self._sort_rows_box.remove(row)

    def _move_sort_row(self, row: _SortRow, delta: int) -> None:
        index = self._sort_rows.index(row)
        target = index + delta
        if not 0 <= target < len(self._sort_rows):
            return
        self._sort_rows.insert(target, self._sort_rows.pop(index))
        for widget in self._sort_rows:
            self._sort_rows_box.remove(widget)
        for widget in self._sort_rows:
            self._sort_rows_box.append(widget)

    def _set_sort_rows(self, specs: list[SortSpec]) -> None:
        """Mirror an order list (e.g. from header clicks) in the panel."""
        for row in list(self._sort_rows):
            self._remove_sort_row(row)
        for spec in specs:
            self._add_sort_row(spec)

    def _apply_sort(self) -> None:
        # Line order is the ORDER BY priority; duplicate columns keep
        # their first line.
        seen: set[str] = set()
        order = []
        for row in self._sort_rows:
            spec = row.spec()
            if spec.column and spec.column not in seen:
                seen.add(spec.column)
                order.append(spec)
        self._order_by = order
        self._offset = 0
        self.reload()

    def _clear_sort(self) -> None:
        for row in list(self._sort_rows):
            self._remove_sort_row(row)
        if self._order_by:
            self._order_by = []
            self._offset = 0
            self.reload()

    def _on_header_sort(self, pairs: list[tuple[str, bool]]) -> None:
        """A header click changed the view's sort columns: adopt them
        (primary first) as the order list and re-query."""
        self._order_by = [
            SortSpec(column=name, descending=descending)
            for name, descending in pairs
            if name in self._column_names
        ]
        self._set_sort_rows(self._order_by)
        self._offset = 0
        self.reload()

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
        for row in self._sort_rows:
            row.set_columns(names)

    def _apply_filters(self) -> None:
        self._filters = [
            row.condition() for row in self._filter_rows if row.selected_column()
        ]
        self._offset = 0
        self.reload()

    def _clear_filters(self) -> None:
        for row in list(self._filter_rows):
            self._remove_filter_row(row)
        if self._filters:
            self._filters = []
            self._offset = 0
            self.reload()

    def _on_prev(self, *_args) -> None:
        self._offset = max(0, self._offset - PAGE_SIZE)
        self.reload()

    def _on_next(self, *_args) -> None:
        self._offset += PAGE_SIZE
        self.reload()

    # Editing

    def _on_edit_toggled(self, toggle: Gtk.ToggleButton) -> None:
        # Locking with pending edits keeps them pending; Save (or a
        # discarding Refresh) is still available.
        self._grid.set_unlocked(toggle.get_active())

    def _commit_edit(self, row: RowItem, index: int, new_text: str) -> None:
        column_name = self._result_names[index]
        pending = self._pending.get(row)
        if pending is None:
            # Snapshot the pk before applying the edit, so a row stays
            # addressable in the database even if its pk cell is edited.
            try:
                pk_values = {
                    c.name: row.values[self._result_names.index(c.name)]
                    for c in self._columns
                    if c.is_pk
                }
            except ValueError:
                self._show_error(
                    "Cannot edit: primary key column missing from result"
                )
                return
            pending = self._pending[row] = (pk_values, {})
        pending[1][column_name] = new_text
        row.values[index] = new_text
        self._grid.mark_modified(row, index)
        self._update_save_button()

    def _update_save_button(self) -> None:
        count = sum(len(changes) for _pk, changes in self._pending.values())
        self._save.set_visible(count > 0)
        self._save.set_label(f"Save ({count})")

    def _pending_updates(self) -> list[tuple[dict[str, Any], str, str]]:
        """Flat (pk values, column, new value) list, one per edited cell."""
        return [
            (pk_values, column, value)
            for pk_values, changes in self._pending.values()
            for column, value in changes.items()
        ]

    def _on_save_clicked(self, *_args) -> None:
        updates = self._pending_updates()
        if not updates:
            return
        statements = [
            f"UPDATE {self.table} SET {column} = {_sql_literal(value)}"
            " WHERE "
            + " AND ".join(
                f"{name} = {_sql_literal(pk)}" for name, pk in pk_values.items()
            )
            + ";"
            for pk_values, column, value in updates
        ]
        dialog = UpdatePreviewDialog(
            statements, lambda: self._execute_updates(updates)
        )
        dialog.present(self)

    def _execute_updates(
        self, updates: list[tuple[dict[str, Any], str, str]]
    ) -> None:
        def work():
            connector = self._ensure(self.profile)
            for pk_values, column, value in updates:
                connector.update_cell(self.table, pk_values, column, value)

        def done(_result):
            self.reload()  # clears pending and modified marks

        def failed(exc):
            self._show_error(str(exc))
            self.reload()  # resync with whatever was applied

        run_async(work, done, failed)
