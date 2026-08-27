"""Data grid widgets.

ResultGrid: a Gtk.ColumnView whose columns are built at runtime from a
result set. Reused by table tabs and the query console. When editable,
cells are Gtk.EditableLabel and committed edits go through a callback.
Editing is locked until set_unlocked(True); mark_modified() highlights
cells with uncommitted changes.

Columns can be dragged to reorder and resized at their edges. Either
mouse button on a column header opens the same menu — sort (when the
grid is sortable through on_header_sort), copy and move — so there is
nothing to learn about which button does what; sorted columns show an
arrow in their title. The pointer says which of the three things a
header press will do: grab (reorder), col-resize (at the edges), cell
over the data.

Cells are selectable for copying: click selects a cell, dragging (or
Shift+click) extends to a rectangular block, a header click selects
the whole column, a click on the row-number stub column selects the
whole row, and the context menu selects a whole row or column.
The selection renders as a border around the selected region.
Ctrl+C (or the context menu's Copy) copies the selection as
tab-separated text; row and block selections include a header line with
the column names, following the current display order of the columns.
"Copy As" offers CSV, INSERT statements, pretty (ASCII table) and
Markdown. Every selection is also summarised (count/sum/avg/min/max)
and handed to the on_aggregate callback, which the window routes to
the Aggregate page of the right side panel — the menu's "Aggregate"
item only brings that page to the front, it is not what computes the
summary.

Geometry columns (PG-04) are rendered as a readable summary — *Point,
SRID 4326, 1 point* — instead of WKB hex, and the cell menu's "Show on
Map" opens them in the map view. Both are gated on the owner setting
`geo_enabled`, which a table tab does only once the server answers that
it has a spatial extension: on every other connection a column of hex
strings is still just a column of hex strings.

TableTab: a ResultGrid bound to one table — paged loading, refresh, and
primary-key-based cell editing. Editing is opt-in: a toggle in the
action bar unlocks the cells, edits accumulate locally (highlighted in
the grid), and Save opens a review dialog showing the UPDATE statements
before they run through Connector.update_cell(). Refresh discards
pending edits. A NULL and an empty string look the same once typed
into an EditableLabel, so setting a cell to NULL instead goes through
the cell's right-click menu ("Set Cell to NULL", enabled only while
unlocked) — it calls on_edit with None rather than "".

A table whose rows hold geometries grows a third side, Map, next to
Data and Properties (frontend/map_view.py): the loaded rows drawn on
OpenStreetMap tiles, with selection running both ways — clicking a
feature selects its row, selecting a row highlights its feature.
"""

from __future__ import annotations

from typing import Any, Callable

import csv
import io
import json
from decimal import Decimal

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango

from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.db import geo, objects, registry
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
from sqlide.backend.settings import max_map_features as settings_max_map_features
from sqlide.backend.settings import store as settings_store
from sqlide.frontend import confirm, feedback, keymap
from sqlide.frontend.object_info import TablePropertiesView
from sqlide.frontend.util import describe, run_async

PAGE_SIZE = 500

# "Copy As" clipboard formats, shared by the cell context menu and the
# header left-click menu: (label, format key for copy_selection).
COPY_FORMATS = (
    ("CSV", "csv"),
    ("INSERT Statement", "insert"),
    ("JSON", "json"),
    ("Pretty", "pretty"),
    ("Markdown", "markdown"),
)

# on_edit(row_item, column_index, new_text_or_None_for_NULL)
EditCallback = Callable[["RowItem", int, str | None], None]

# on_aggregate(summary_lines, live). live=True is the running summary of
# whatever is selected — fill the panel but leave it where it is;
# live=False is the user asking for it, which may raise the panel.
AggregateCallback = Callable[[list[str], bool], None]


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
        on_aggregate: AggregateCallback | None = None,
        on_header_sort: Callable[[list[tuple[str, bool]]], None] | None = None,
        on_edge_reached: Callable[[Gtk.PositionType], None] | None = None,
        on_row_activated: Callable[[int], None] | None = None,
    ) -> None:
        super().__init__(vexpand=True, hexpand=True)
        self.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        if on_edge_reached is not None:
            self.connect("edge-reached", lambda _self, pos: on_edge_reached(pos))
        self._on_edit = on_edit
        self._aggregate_cb = on_aggregate
        # When set, the column-header menu grows a sort section: the
        # grid never sorts locally; it reports the composed column list
        # as (name, descending) pairs, primary first, so the owner can
        # re-query.
        self._on_header_sort = on_header_sort
        # When set, double-clicking a row hands its index over — the
        # object info view's tabular sections use it to open the child
        # a row stands for (CORE-49). Grids that answer no such
        # question leave it None and a double click only selects.
        self._on_row_activated = on_row_activated
        # Geometry rendering is off until the owner says the server has
        # a spatial extension (PG-04): without that gate a column of
        # ordinary hex strings would be summarised as geometries on
        # every engine. on_show_map(row, column name) opens the map.
        self.geo_enabled = False
        self.on_show_map: Callable[[int, str], None] | None = None
        # on_row_selected(row index or None) — the map's half of the
        # two-way selection.
        self.on_row_selected: Callable[[int | None], None] | None = None
        self._geo_columns: set[int] = set()
        self._sort_order: list[tuple[str, bool]] = []
        # Used by "Copy As > INSERT Statement"; falls back to a placeholder.
        self.table_name = table_name
        self._store = Gio.ListStore(item_type=RowItem)
        self._view = Gtk.ColumnView(
            model=Gtk.NoSelection(model=self._store), hexpand=True
        )
        self._view.add_css_class("data-table")
        self._view.set_show_row_separators(True)
        self._view.set_show_column_separators(True)
        # Column reordering is implemented by hand below (see the
        # header drag gesture); the built-in DnD never engages
        # reliably, so take over completely.
        self._view.set_reorderable(False)
        self.set_child(self._view)

        self._column_names: list[str] = []
        # ColumnViewColumn objects in data order; get_columns() gives the
        # display order after the user drags headers around.
        self._column_objs: list[Gtk.ColumnViewColumn] = []
        # Row-number stub column, always at display position 0 and never
        # part of _column_objs: display-order math skips it and header
        # drags neither move it nor cross it.
        self._rownum_col: Gtk.ColumnViewColumn | None = None
        self._rownum_cells: dict[Gtk.Widget, Gtk.ListItem] = {}
        self._row_offset = 0  # added to positions so paging keeps counting
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
        # What an empty result should say. "No rows" and "no rows match
        # this filter" are different situations with different fixes,
        # so the owner sets this before loading (see set_empty_state).
        self._empty: tuple[str, str, str, Callable[[], None] | None] = (
            "No rows",
            "The query returned no rows.",
            "",
            None,
        )
        self._menu_rect = Gdk.Rectangle()

        actions = Gio.SimpleActionGroup()
        self._set_null_action: Gio.SimpleAction | None = None
        for name, callback in (
            ("select-row", self._on_select_row),
            ("select-column", self._on_select_column),
            ("copy", lambda *_: self.copy_selection()),
            ("aggregate", self._on_aggregate),
            ("move-left", lambda *_: self._move_menu_column(-1)),
            ("move-right", lambda *_: self._move_menu_column(1)),
            ("set-null", self._on_set_null),
            ("show-map", self._on_show_map),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            actions.add_action(action)
            if name == "set-null":
                action.set_enabled(False)  # only while editing is unlocked
                self._set_null_action = action
            if name == "show-map":
                action.set_enabled(False)  # only on a geometry cell
                self._show_map_action = action
        copy_as = Gio.SimpleAction.new("copy-as", GLib.VariantType.new("s"))
        copy_as.connect(
            "activate", lambda _a, param: self.copy_selection(param.get_string())
        )
        actions.add_action(copy_as)
        header_sort = Gio.SimpleAction.new(
            "header-sort", GLib.VariantType.new("s")
        )
        header_sort.connect("activate", self._on_header_sort_action)
        actions.add_action(header_sort)
        self._view.insert_action_group("grid", actions)

        menu = Gio.Menu()
        menu.append("Select Row", "grid.select-row")
        menu.append("Select Column", "grid.select-column")
        copy_section = Gio.Menu()
        copy_section.append("Copy", "grid.copy")
        copy_as_menu = Gio.Menu()
        for label, fmt in COPY_FORMATS:
            copy_as_menu.append(label, f"grid.copy-as::{fmt}")
        copy_section.append_submenu("Copy As", copy_as_menu)
        copy_section.append("Aggregate", "grid.aggregate")
        copy_section.append("Show on Map", "grid.show-map")
        menu.append_section(None, copy_section)
        if on_edit is not None:
            edit_section = Gio.Menu()
            edit_section.append("Set Cell to NULL", "grid.set-null")
            menu.append_section(None, edit_section)
        # Columns can also be reordered by dragging their headers; the
        # menu items cover the cell-menu path.
        move_section = Gio.Menu()
        move_section.append("Move Column Left", "grid.move-left")
        move_section.append("Move Column Right", "grid.move-right")
        menu.append_section(None, move_section)
        self._popover = Gtk.PopoverMenu.new_from_model(menu)
        self._popover.set_parent(self._view)
        self._popover.set_has_arrow(False)

        # Popped up by either button on a column header; the model is
        # rebuilt per column (see _column_menu).
        self._header_popover = Gtk.PopoverMenu.new_from_model(Gio.Menu())
        self._header_popover.set_parent(self._view)
        self._header_popover.set_has_arrow(False)

        self._view.connect("destroy", self._on_view_destroy)

        # Hold the primary button and drag to select a rectangular
        # block (mouse-only alternative to Shift+click). A gesture
        # cannot do this: the cell's own click gesture claims the press
        # (that is what keeps a locked cell from starting an edit), and
        # claiming denies every other gesture in the chain — which is
        # why the drag used to do nothing. A motion controller is not a
        # gesture, so it keeps seeing the pointer either way; the press
        # only leaves the anchor behind for it.
        self._drag_anchor: tuple[int, int] | None = None
        self._drag_last: tuple[int, int] | None = None
        self._cursor_name = ""
        motion = Gtk.EventControllerMotion()
        motion.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        motion.connect("motion", self._on_motion)
        self._view.add_controller(motion)

        # Secondary button on a header: same menu as the left click, so
        # nothing depends on knowing which button to use.
        header_menu_click = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        header_menu_click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        header_menu_click.connect("pressed", self._on_header_secondary)
        self._view.add_controller(header_menu_click)

        # Drag a column header to reorder: begun over a header title
        # (away from its edges, which stay free for column resizing),
        # the dragged column live-swaps with the neighbor the pointer
        # crosses into. Inert over cells, so it coexists with the
        # block-selection drag above.
        self._header_drag_pos: int | None = None  # span position
        self._header_drag_start = (0.0, 0.0)
        self._header_moved = False  # a column moved during this drag
        header_drag = Gtk.GestureDrag(button=Gdk.BUTTON_PRIMARY)
        header_drag.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        header_drag.connect("drag-begin", self._on_header_drag_begin)
        header_drag.connect("drag-update", self._on_header_drag_update)
        header_drag.connect("drag-end", self._on_header_drag_end)
        self._view.add_controller(header_drag)
        # Data column the last header press was on, for its menu.
        self._header_click_col: int | None = None

        shortcuts = Gtk.ShortcutController()
        shortcuts.set_scope(Gtk.ShortcutScope.LOCAL)
        # Rebindable in Preferences, so it tracks the keymap registry
        # (frontend/keymap.py) live rather than a hardcoded trigger.
        self._copy_shortcut = Gtk.Shortcut.new(
            Gtk.ShortcutTrigger.parse_string(keymap.effective("grid.copy")),
            Gtk.CallbackAction.new(self._on_copy_shortcut),
        )
        shortcuts.add_shortcut(self._copy_shortcut)
        settings_store.subscribe(self._refresh_copy_shortcut)
        self._view.connect(
            "destroy",
            lambda *_: settings_store.unsubscribe(self._refresh_copy_shortcut),
        )
        # Everything in the cell menu must be reachable without a
        # mouse, so the platform's menu keys open it on the selection.
        # These are OS-standard context-menu keys, not user bindings.
        for trigger in ("Menu", "<Shift>F10"):
            shortcuts.add_shortcut(
                Gtk.Shortcut.new(
                    Gtk.ShortcutTrigger.parse_string(trigger),
                    Gtk.CallbackAction.new(self._on_menu_shortcut),
                )
            )
        self._view.add_controller(shortcuts)

    def _refresh_copy_shortcut(self, *_args) -> None:
        self._copy_shortcut.set_trigger(
            Gtk.ShortcutTrigger.parse_string(keymap.effective("grid.copy"))
        )

    def _on_menu_shortcut(self, _widget, _args) -> bool:
        """Pop the cell menu up over the selection (the top-left cell
        of it), or over the grid's corner when nothing is selected."""
        if self._sel_rows and self._sel_cols:
            self._menu_cell = (min(self._sel_rows), min(self._sel_cols))
        self._refresh_map_action()
        rect = Gdk.Rectangle()
        rect.x = rect.y = 8
        rect.width = rect.height = 1
        self._menu_rect = rect
        self._popover.set_pointing_to(rect)
        self._popover.popup()
        return True

    def clear(self) -> None:
        self.set_result([], [])

    def set_empty_state(
        self,
        title: str,
        description: str,
        action_label: str = "",
        on_action: Callable[[], None] | None = None,
    ) -> None:
        """What to show when a result has no rows at all. An empty
        table, an empty result and a filter that matches nothing have
        different fixes, so each caller says its own."""
        self._empty = (title, description, action_label, on_action)

    def _empty_page(self) -> Gtk.Widget:
        title, description, action_label, on_action = self._empty
        page = Adw.StatusPage(
            icon_name="edit-find-symbolic",
            title=title,
            description=description,
        )
        if action_label and on_action is not None:
            button = Gtk.Button(label=action_label, halign=Gtk.Align.CENTER)
            button.add_css_class("pill")
            button.connect("clicked", lambda *_: on_action())
            page.set_child(button)
        return page

    def set_result(
        self,
        columns: list[str],
        rows: list[tuple],
        editable: bool = False,
        row_offset: int = 0,
    ) -> None:
        # A re-query with the same columns (sort, filter, paging,
        # refresh) must not undo the user's drag-reorder: remember the
        # display order and rebuild the new columns in it.
        display_order: list[int] | None = None
        if (
            list(columns) == self._column_names
            and len(self._column_objs) == len(columns)
        ):
            display_order = self._display_order()
        old = self._view.get_columns()
        for col in [old.get_item(i) for i in range(old.get_n_items())]:
            self._view.remove_column(col)
        self._store.remove_all()
        self._column_names = list(columns)
        self._column_objs = []
        self._bound_cells.clear()
        self._rownum_cells.clear()
        self._row_offset = row_offset
        self._sel_rows = set()
        self._sel_cols = set()
        self._sel_kind = None
        self._anchor = None
        self._modified = set()
        self._sort_order = []

        editable = editable and self._on_edit is not None
        self._editable_grid = editable
        # Which columns hold WKB, decided from the values themselves
        # (a bare `SELECT geom` carries no type information) and only
        # where the connection actually has a spatial extension.
        self._geo_columns = set()
        if self.geo_enabled:
            names = geo.geometry_columns(list(columns), rows)
            self._geo_columns = {
                index for index, name in enumerate(columns) if name in names
            }

        # Row-number stub: untitled, fixed width (sized to the largest
        # number it will show), not resizable, never reordered. Clicking
        # a number selects its whole row.
        rownum_factory = Gtk.SignalListItemFactory()
        rownum_factory.connect("setup", self._setup_rownum)
        rownum_factory.connect("bind", self._bind_rownum)
        rownum_factory.connect("unbind", self._unbind_rownum)
        self._rownum_col = Gtk.ColumnViewColumn(title="", factory=rownum_factory)
        self._rownum_col.set_resizable(False)
        self._rownum_col.set_expand(False)
        digits = max(2, len(str(row_offset + len(rows))))
        self._rownum_col.set_fixed_width(20 + 9 * digits)
        self._view.append_column(self._rownum_col)

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
            # No set_header_menu(): GTK's own header menu would be a
            # second, different menu on the secondary button, and it
            # cannot tell the grid which column it was opened on. Both
            # buttons go through _column_menu instead.
            self._column_objs.append(column)

        for index in display_order or range(len(self._column_objs)):
            self._view.append_column(self._column_objs[index])

        for row in rows:
            self._store.append(RowItem(row))

        # An empty grid is a blank rectangle that teaches nothing; show
        # the state's own message and its fix instead.
        self.set_child(self._view if rows else self._empty_page())

    def append_rows(self, rows: list[tuple]) -> None:
        """Add more rows below the ones already shown (scrolled-to-bottom
        paging), without disturbing selection, columns or edits."""
        for row in rows:
            self._store.append(RowItem(row))

    def set_sort_state(self, order: list[tuple[str, bool]]) -> None:
        """Show sort arrows matching (column name, descending) pairs,
        primary first — the owner calls this after a re-query so header
        state survives the column rebuild. Unknown names are skipped."""
        if self._on_header_sort is None:
            return
        self._sort_order = [
            (name, descending)
            for name, descending in order
            if name in self._column_names
        ]
        self._update_header_titles()

    def _update_header_titles(self) -> None:
        """Decorate sorted columns' titles with a direction arrow (the
        headers carry no sorter, so GTK draws no indicator itself)."""
        arrows = {name: "↓" if desc else "↑" for name, desc in self._sort_order}
        for name, column in zip(self._column_names, self._column_objs):
            arrow = arrows.get(name)
            column.set_title(f"{name} {arrow}" if arrow else name)

    def _column_menu(self, index: int) -> Gio.Menu:
        """Everything one column header offers, on either button:
        sorting (this column made primary, previously sorted columns
        demoted; or dropped from the order), the copy/aggregate items
        for the column the press just selected, and moving the column.
        Unsortable grids simply have no sort section."""
        menu = Gio.Menu()
        if self._on_header_sort is not None:
            sort = Gio.Menu()
            sort.append("Sort Ascending", f"grid.header-sort::{index}|asc")
            sort.append("Sort Descending", f"grid.header-sort::{index}|desc")
            sort.append("Don't Sort", f"grid.header-sort::{index}|none")
            sort.append("Clear Sort", f"grid.header-sort::{index}|clear")
            menu.append_section(None, sort)
        copy_section = Gio.Menu()
        copy_section.append("Copy", "grid.copy")
        copy_as = Gio.Menu()
        for label, fmt in COPY_FORMATS:
            copy_as.append(label, f"grid.copy-as::{fmt}")
        copy_section.append_submenu("Copy As", copy_as)
        copy_section.append("Aggregate", "grid.aggregate")
        copy_section.append("Show on Map", "grid.show-map")
        menu.append_section(None, copy_section)
        move_section = Gio.Menu()
        move_section.append("Move Column Left", "grid.move-left")
        move_section.append("Move Column Right", "grid.move-right")
        menu.append_section(None, move_section)
        return menu

    def _on_header_sort_action(self, _action, param) -> None:
        if self._on_header_sort is None:
            return
        index_text, _, direction = param.get_string().partition("|")
        index = int(index_text)
        if not 0 <= index < len(self._column_names):
            return
        name = self._column_names[index]
        if direction == "clear":
            order = []
        elif direction == "none":
            order = [(n, d) for n, d in self._sort_order if n != name]
        else:
            order = [(name, direction == "desc")] + [
                (n, d) for n, d in self._sort_order if n != name
            ]
        self._sort_order = order
        # The owner re-queries and calls set_sort_state, which redraws
        # the header arrows.
        self._on_header_sort(order)

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
            label.set_text(self._cell_display(index, value))
            label.remove_css_class("dim-label")
        self._register_cell(label, list_item, index)

    # Row-number cells

    def _setup_rownum(self, factory, list_item) -> None:
        label = Gtk.Label(xalign=1.0)
        label.add_css_class("dim-label")
        label.add_css_class("row-number")
        # Same no-user-data rule as _attach_cell_gesture: the handler
        # resolves the row through _rownum_cells.
        click = Gtk.GestureClick(button=Gdk.BUTTON_PRIMARY)
        click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        click.connect("pressed", self._on_rownum_pressed)
        label.add_controller(click)
        list_item.set_child(label)

    def _bind_rownum(self, factory, list_item) -> None:
        label = list_item.get_child()
        label.set_text(str(self._row_offset + list_item.get_position() + 1))
        self._rownum_cells[label] = list_item
        parent = label.get_parent()
        if parent is not None:
            # Tighter padding than data cells (see style.css).
            parent.add_css_class("rownum-cell")

    def _unbind_rownum(self, factory, list_item) -> None:
        self._rownum_cells.pop(list_item.get_child(), None)

    def _on_rownum_pressed(self, gesture, _n_press, _x, _y) -> None:
        list_item = self._rownum_cells.get(gesture.get_widget())
        if list_item is None:
            return
        row = list_item.get_position()
        if row == Gtk.INVALID_LIST_POSITION:
            return
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        order = self._display_order()
        # Anchor on the first display column so Shift+click extends
        # from this row like any other selection.
        self._anchor = (row, order[0]) if order else None
        self._drag_anchor = None  # a row press starts no block drag
        self._select({row}, set(range(len(self._column_names))), "row")

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
        widget.set_text("" if value is None else self._cell_display(index, value))
        # A binary cell renders as an abbreviated hex summary, so
        # committing the box's text would replace the blob with that
        # summary. Show it, never edit it.
        # A geometry renders as a summary for the same reason a blob
        # does: committing the text on screen would replace the value
        # with a description of it.
        widget.set_editable(
            self._unlocked
            and not is_binary(value)
            and index not in self._geo_columns
        )
        self._register_cell(widget, list_item, index)

    def _cell_display(self, index: int, value: Any) -> str:
        """A cell's text: a geometry as its readable summary (type,
        SRID, point count — PG-04), anything else as usual."""
        if index in self._geo_columns:
            summary = geo.summarize(value)
            if summary:
                return summary
        return _display_text(value)

    def geometry_at(self, row: int, column: int):
        """The parsed geometry of one cell, or None if that cell holds
        no geometry. Used by the map and by the cell menu."""
        if column not in self._geo_columns:
            return None
        position = row - self._row_offset
        if position < 0 or position >= self._store.get_n_items():
            return None
        value = self._store.get_item(position).values[column]
        if value is None:
            return None
        try:
            return geo.parse(value)
        except geo.GeometryError:
            return None

    def geometry_columns(self) -> list[str]:
        """The names of the loaded result's geometry columns."""
        return [self._column_names[i] for i in sorted(self._geo_columns)]

    def _refresh_map_action(self) -> None:
        """"Show on Map" is live only over a geometry cell, and only
        where there is a map to show it on."""
        _row, column = self._menu_cell
        self._show_map_action.set_enabled(
            self.on_show_map is not None and column in self._geo_columns
        )

    def _on_show_map(self, *_args) -> None:
        row, column = self._menu_cell
        if self.on_show_map is None or column not in self._geo_columns:
            return
        self.on_show_map(row, self._column_names[column])

    def set_unlocked(self, unlocked: bool) -> None:
        """Allow or forbid starting cell edits (the lock is enforced in
        the click gesture; set_editable is a second layer so a stray
        edit cannot change text)."""
        self._unlocked = unlocked
        for widget in self._bound_cells:
            if isinstance(widget, Gtk.EditableLabel):
                widget.set_editable(unlocked)
        if self._set_null_action is not None:
            self._set_null_action.set_enabled(unlocked)

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
        if is_binary(old):
            return  # see _bind_editable: blobs are display-only
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

    def _style_cell(
        self, widget, row: int, col: int, order: list[int] | None = None
    ) -> None:
        # The selection is drawn as a border around the selected
        # region, not a fill: each selected cell gets a side class for
        # every edge with no selected neighbor, and the CSS draws that
        # side. Classes go on the cell widget (the label's parent) so
        # the lines tile seamlessly across cells.
        parent = widget.get_parent()
        target = parent if parent is not None else widget
        selected = row in self._sel_rows and col in self._sel_cols
        top = bottom = left = right = False
        if selected:
            if order is None:
                order = self._display_order()
            pos = order.index(col)
            top = (row - 1) not in self._sel_rows
            bottom = (row + 1) not in self._sel_rows
            left = pos == 0 or order[pos - 1] not in self._sel_cols
            right = pos == len(order) - 1 or order[pos + 1] not in self._sel_cols
        for name, on in (
            ("sel-top", top),
            ("sel-bottom", bottom),
            ("sel-left", left),
            ("sel-right", right),
        ):
            if on:
                target.add_css_class(name)
            else:
                target.remove_css_class(name)
        if (row, col) in self._modified:
            widget.add_css_class("cell-modified")
        else:
            widget.remove_css_class("cell-modified")

    def _restyle_cells(self) -> None:
        order = self._display_order()
        for widget, (list_item, col) in self._bound_cells.items():
            self._style_cell(widget, list_item.get_position(), col, order)

    def _select(self, rows: set[int], cols: set[int], kind: str) -> None:
        self._sel_rows = rows
        self._sel_cols = cols
        self._sel_kind = kind
        self._restyle_cells()
        # A selection is a question about those cells, so answer it
        # straight away: the side panel's Aggregate page is filled as
        # the selection changes and is simply there when it is opened.
        if self._aggregate_cb is not None and rows and cols:
            self._aggregate_cb(self._aggregate_lines(), True)
        # The map follows the grid's selection (PG-04): one row
        # selected highlights that row's feature, anything else clears
        # the highlight rather than guessing which row was meant.
        if self.on_row_selected is not None:
            self.on_row_selected(min(rows) if len(rows) == 1 else None)

    def _on_cell_pressed(self, gesture, n_press, x, y) -> None:
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
            self._drag_anchor = None
            if row not in self._sel_rows or index not in self._sel_cols:
                self._anchor = (row, index)
                self._select({row}, {index}, "cell")
            self._menu_cell = (row, index)
            self._popup_menu(widget, x, y)
        elif button == Gdk.BUTTON_PRIMARY:
            if n_press >= 2 and self._on_row_activated is not None:
                gesture.set_state(Gtk.EventSequenceState.CLAIMED)
                self._drag_anchor = None
                self._on_row_activated(self._row_offset + row)
            elif shift and self._anchor is not None:
                gesture.set_state(Gtk.EventSequenceState.CLAIMED)
                self._drag_anchor = self._anchor
                self._drag_last = (row, index)
                self._select_block(self._anchor, (row, index))
            else:
                if self._editable_grid and not self._unlocked:
                    # Locked: swallow the press so the EditableLabel
                    # never sees it and cannot start an edit.
                    gesture.set_state(Gtk.EventSequenceState.CLAIMED)
                # When unlocked the press is not claimed, so the same
                # click still starts the edit.
                self._anchor = (row, index)
                # Held down, this is the start of a block drag; the
                # motion controller takes it from here.
                self._drag_anchor = self._drag_last = (row, index)
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

    # Pointer (block drag + cursor)

    def _on_motion(self, controller, x, y) -> None:
        held = bool(
            controller.get_current_event_state()
            & Gdk.ModifierType.BUTTON1_MASK
        )
        if not held:
            # The button is up again: the drag, if there was one, ended.
            self._drag_anchor = None
        elif self._drag_anchor is not None and self._header_drag_pos is None:
            self._extend_drag(x, y)
        self._update_cursor(x, y, held)

    def _extend_drag(self, x: float, y: float) -> None:
        """Grow the selection to the cell under the pointer."""
        cell = self._cell_at(x, y)
        if cell is None or cell == self._drag_last:
            return
        self._drag_last = cell
        # An unlocked cell starts editing on the press that began this
        # drag; the drag is clearly not an edit, so drop out of it.
        self._stop_cell_editing()
        self._anchor = self._drag_anchor
        self._select_block(self._drag_anchor, cell)

    def _stop_cell_editing(self) -> None:
        for widget in self._bound_cells:
            if isinstance(widget, Gtk.EditableLabel) and widget.get_property(
                "editing"
            ):
                widget.stop_editing(False)

    def _update_cursor(self, x: float, y: float, held: bool) -> None:
        """Name the pointer after what a press would do here: reorder a
        column, resize it, or select cells."""
        if self._header_drag_pos is not None:
            name = "grabbing"
        elif self._header_title_at(x, y) is not None:
            name = "grab"
            spans = self._header_spans()
            position = self._span_at(spans, x)
            if position is None or position == 0:
                name = "default"  # the row-number stub moves nowhere
            else:
                start, end = spans[position]
                if x - start < self._RESIZE_EDGE or end - x < self._RESIZE_EDGE:
                    name = "col-resize"
        elif held and self._drag_anchor is not None:
            name = "cell"
        else:
            name = "cell" if self._cell_at(x, y) is not None else "default"
        if name == self._cursor_name:
            return
        self._cursor_name = name
        self._view.set_cursor(
            None if name == "default" else Gdk.Cursor.new_from_name(name)
        )

    # Header drag-reorder

    _RESIZE_EDGE = 8.0  # px near a header edge left to column resizing

    def _header_title_at(self, x: float, y: float) -> Gtk.Widget | None:
        """The GtkColumnViewTitle under view coordinates, if any (GTK
        has no public API for header widgets; matching the type name
        degrades to 'drag does nothing' if it ever changes)."""
        widget = self._view.pick(x, y, Gtk.PickFlags.DEFAULT)
        while widget is not None and widget is not self._view:
            if widget.__gtype__.name == "GtkColumnViewTitle":
                return widget
            widget = widget.get_parent()
        return None

    def _header_spans(self) -> list[tuple[float, float]]:
        """(start x, end x) of each column header in display order,
        in view coordinates."""
        title = self._header_title_at(2.0, 2.0)
        # Fall back to probing across the top edge: the first column
        # can start beyond x=2 when scrolled horizontally.
        if title is None:
            width = self._view.get_width()
            step = 40
            for x in range(step, max(width, step), step):
                title = self._header_title_at(float(x), 2.0)
                if title is not None:
                    break
        if title is None:
            return []
        spans = []
        sibling = title.get_parent().get_first_child()
        while sibling is not None:
            if sibling.__gtype__.name == "GtkColumnViewTitle":
                ok, bounds = sibling.compute_bounds(self._view)
                if ok:
                    spans.append(
                        (bounds.origin.x, bounds.origin.x + bounds.size.width)
                    )
            sibling = sibling.get_next_sibling()
        spans.sort()
        return spans

    @staticmethod
    def _span_at(spans: list[tuple[float, float]], x: float) -> int | None:
        for i, (start, end) in enumerate(spans):
            if start <= x < end:
                return i
        return None

    def _begin_header_drag(self, x: float, y: float) -> bool:
        """Start a reorder if (x, y) presses a header title away from
        its resize edges; returns whether the drag begins."""
        self._header_drag_pos = None
        if self._header_title_at(x, y) is None:
            return False
        spans = self._header_spans()
        position = self._span_at(spans, x)
        # Span 0 is the row-number stub: no reorder, no column select.
        if position is None or position == 0:
            return False
        start, end = spans[position]
        if x - start < self._RESIZE_EDGE or end - x < self._RESIZE_EDGE:
            return False  # leave the edge to GTK's column resize
        self._header_drag_pos = position
        self._header_drag_start = (x, y)
        return True

    def _update_header_drag(self, x: float) -> None:
        # _header_drag_pos is in span space, where 0 is the row-number
        # stub and data columns start at 1; that matches the view's
        # column positions, so insert_column takes it unshifted.
        if self._header_drag_pos is None:
            return
        spans = self._header_spans()
        target = self._span_at(spans, x)
        if target is None:
            target = 1 if spans and x < spans[0][0] else len(spans) - 1
        target = max(target, 1)  # never left of the row-number stub
        if target == self._header_drag_pos:
            return
        order = self._display_order()
        if not 1 <= self._header_drag_pos <= len(order):
            return
        column = self._column_objs[order[self._header_drag_pos - 1]]
        self._view.remove_column(column)
        self._view.insert_column(target, column)
        self._header_drag_pos = target
        self._header_moved = True
        # Selection borders depend on display neighbors.
        self._restyle_cells()

    _CLICK_SLOP = 4.0  # px of movement still counting as a plain click

    def _on_header_drag_begin(self, gesture, x, y) -> None:
        if self._begin_header_drag(x, y):
            # Claim the press, select the whole column (a plain click
            # ends in the column menu at drag-end), and let any
            # movement reorder it.
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            self._cursor_name = "grabbing"
            self._view.set_cursor(Gdk.Cursor.new_from_name("grabbing"))
            self._header_moved = False
            self._drag_anchor = None  # a header press selects no cell
            order = self._display_order()
            self._header_click_col = order[self._header_drag_pos - 1]
            self._select_whole_column(self._header_click_col)

    def _on_header_drag_update(self, _gesture, dx, _dy) -> None:
        self._update_header_drag(self._header_drag_start[0] + dx)

    def _on_header_drag_end(self, _gesture, dx, dy) -> None:
        was_click = (
            self._header_drag_pos is not None
            and not self._header_moved
            and abs(dx) < self._CLICK_SLOP
            and abs(dy) < self._CLICK_SLOP
        )
        self._header_drag_pos = None
        self._cursor_name = ""
        self._view.set_cursor(None)
        if was_click and self._header_click_col is not None:
            self._popup_header_menu(self._header_click_col)

    def _on_header_secondary(self, gesture, _n_press, x, y) -> None:
        """Secondary button on a header: select the column and open the
        same menu the left click opens."""
        if self._header_title_at(x, y) is None:
            return
        spans = self._header_spans()
        position = self._span_at(spans, x)
        order = self._display_order()
        if position is None or not 1 <= position <= len(order):
            return
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        column = order[position - 1]
        self._drag_anchor = None
        self._select_whole_column(column)
        self._header_drag_start = (x, y)
        self._popup_header_menu(column)

    def _select_whole_column(self, column: int) -> None:
        self._select(set(range(self._store.get_n_items())), {column}, "column")
        # Move Column Left/Right read the menu's column from here.
        self._menu_cell = (0, column)

    def _popup_header_menu(self, column: int) -> None:
        """Open one column's menu where the press landed."""
        self._header_popover.set_menu_model(self._column_menu(column))
        x, y = self._header_drag_start
        rect = Gdk.Rectangle()
        rect.x, rect.y = int(x), int(y)
        rect.width = rect.height = 1
        self._header_popover.set_pointing_to(rect)
        self._header_popover.popup()

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

    def select_row(self, row: int) -> None:
        """Select one whole row from outside — what a click on the map
        does to the grid."""
        position = row - self._row_offset
        if position < 0 or position >= self._store.get_n_items():
            return
        self._anchor = (position, 0)
        self._select(
            {position}, set(range(len(self._column_names))), "row"
        )
        self._view.scroll_to(
            position, None, Gtk.ListScrollFlags.NONE, None
        )

    def _on_select_row(self, *_args) -> None:
        row, _col = self._menu_cell
        self._select({row}, set(range(len(self._column_names))), "row")

    def _on_select_column(self, *_args) -> None:
        _row, col = self._menu_cell
        self._select(set(range(self._store.get_n_items())), {col}, "column")

    def _on_set_null(self, *_args) -> None:
        if self._on_edit is None or not self._unlocked:
            return
        row, col = self._menu_cell
        item = self._store.get_item(row)
        if item is None or item.values[col] is None:
            return  # already NULL
        self._on_edit(item, col, None)

    def _popup_menu(self, widget, x, y) -> None:
        self._refresh_map_action()
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
        self._header_popover.unparent()

    # Copy

    def _display_order(self) -> list[int]:
        """Data column indices in current display order (the row-number
        stub is not a data column and is skipped)."""
        columns = self._view.get_columns()
        return [
            self._column_objs.index(item)
            for i in range(columns.get_n_items())
            if (item := columns.get_item(i)) in self._column_objs
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
        self._view.remove_column(column)
        # View positions are display positions shifted by the
        # row-number stub at 0.
        self._view.insert_column(target + 1, column)
        self._restyle_cells()

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
        """The menu item: the summary is already computed for every
        selection, so this only asks for the panel showing it."""
        if self._aggregate_cb is not None:
            self._aggregate_cb(self._aggregate_lines(), False)

    def _aggregate_lines(self) -> list[str]:
        """count/sum/avg/min/max of the selected cells."""
        data = self._selection_data()
        if data is None:
            return []
        _headers, rows = data
        values = [v for row in rows for v in row]
        numbers = []
        for value in values:
            # bool is an int in Python; True+True=2 is not a sum anyone
            # asked for. Decimal covers the money columns — postgres
            # numeric and mysql DECIMAL arrive as Decimal, and leaving
            # them out made "Sum" silently mean "sum of the integer
            # columns you happened to select".
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float, Decimal)):
                numbers.append(float(value))
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
        return lines


# Binary columns (BLOB, bytea, MySQL binary collations) arrive as
# bytes/memoryview. str() on those gives a Python repr — b'\x89PNG' —
# which is neither readable nor valid SQL, so every rendering path goes
# through _display_text instead.
_BINARY_TYPES = (bytes, bytearray, memoryview)

# Hex bytes shown in full before a blob is summarised by size instead.
_HEX_PREVIEW = 24


def is_binary(value: Any) -> bool:
    return isinstance(value, _BINARY_TYPES)


def _hex(value: Any) -> str:
    return bytes(value).hex().upper()


def _display_text(value: Any) -> str:
    """A cell's value as text. Binary becomes 0x-hex, truncated to a
    size summary once it is too long to read."""
    if is_binary(value):
        raw = bytes(value)
        if len(raw) <= _HEX_PREVIEW:
            return "0x" + raw.hex().upper()
        head = raw[:_HEX_PREVIEW].hex().upper()
        return f"0x{head}… ({len(raw)} bytes)"
    return str(value)


def _cell_text(value: Any) -> str:
    """A cell's value for the copy formats. Unlike the grid's own
    labels these keep a blob's full hex: what is copied has to be what
    the row holds."""
    if value is None:
        return "NULL"
    return "0x" + _hex(value) if is_binary(value) else str(value)


def _sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    if is_binary(value):
        # X'..' is SQLite's and MySQL's blob literal; PostgreSQL reads
        # it as a bit string, so a bytea column needs the pasted
        # literal adjusted to '\x..'::bytea by hand.
        return "X'" + _hex(value) + "'"
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
                header: value if _json_safe(value) else _json_value(value)
                for header, value in zip(headers, row)
            }
            for row in rows
        ],
        indent=2,
        ensure_ascii=False,
    )


def _json_value(value: Any) -> str:
    """Anything JSON cannot hold, as text. Binary keeps its full hex —
    an export is not a preview, so it must not lose bytes."""
    return "0x" + _hex(value) if is_binary(value) else str(value)


def _json_safe(value: Any) -> bool:
    return value is None or isinstance(value, (bool, int, float, str))


def _format_csv(headers: list[str], rows: list[list[Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(headers)
    for row in rows:
        writer.writerow(
            "" if v is None else ("0x" + _hex(v) if is_binary(v) else v)
            for v in row
        )
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
        on_change: Callable[[], None] | None = None,
        on_close: Callable[[], None] | None = None,
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
        if on_change is not None:
            # Live consumers (the query builder's SQL preview, and the
            # table tab's remove/close button state) want to know about
            # every tweak, not just Apply/Enter.
            for dropdown in (self._conjunction, self._column, self._op):
                dropdown.connect("notify::selected", lambda *_: on_change())
            self._value.connect("changed", lambda *_: on_change())
        self._remove = Gtk.Button(icon_name="list-remove-symbolic")
        self._remove.add_css_class("flat")
        describe(self._remove, "Remove condition")
        self._remove.connect("clicked", lambda *_: on_remove(self))
        self._close = Gtk.Button(icon_name="window-close-symbolic")
        self._close.add_css_class("flat")
        describe(self._close, "Close filters")
        self._close.set_visible(False)
        if on_close is not None:
            self._close.connect("clicked", lambda *_: on_close())
        for widget in (
            self._conjunction,
            self._column,
            self._op,
            self._value,
            self._remove,
            self._close,
        ):
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

    def set_condition(self, cond: FilterCondition) -> None:
        _select_value(self._conjunction, cond.conjunction)
        _select_value(self._column, cond.column)
        _select_value(self._op, cond.op)
        self._value.set_text(cond.value)

    def is_empty(self) -> bool:
        """No value entered (operators that don't need one, like IS
        NULL, always count as a real condition)."""
        return (
            _selected_string(self._op) not in NO_VALUE_OPERATORS
            and not self._value.get_text().strip()
        )

    def clear(self) -> None:
        # Reset the operator too: an IS NULL / IS NOT NULL row is a
        # complete condition with no value, so clearing only the value
        # would never register as empty.
        self._op.set_selected(0)
        self._value.set_text("")

    def set_last(self, is_last: bool) -> None:
        """The sole remaining row can't be deleted (the panel always
        keeps at least one), so once it's empty there is nothing left
        to remove — hide that button and show a close button in its
        place instead."""
        hide_remove = is_last and self.is_empty()
        self._remove.set_visible(not hide_remove)
        self._close.set_visible(hide_remove)

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
        on_change: Callable[[], None] | None = None,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._column = Gtk.DropDown(
            model=Gtk.StringList.new(columns), hexpand=True
        )
        self._direction = Gtk.DropDown(
            model=Gtk.StringList.new(["Ascending", "Descending"])
        )
        if on_change is not None:
            for dropdown in (self._column, self._direction):
                dropdown.connect("notify::selected", lambda *_: on_change())
        up = Gtk.Button(icon_name="go-up-symbolic")
        up.add_css_class("flat")
        describe(up, "Sort by this column earlier")
        up.connect("clicked", lambda *_: on_move(self, -1))
        down = Gtk.Button(icon_name="go-down-symbolic")
        down.add_css_class("flat")
        describe(down, "Sort by this column later")
        down.connect("clicked", lambda *_: on_move(self, 1))
        self._remove = Gtk.Button(icon_name="list-remove-symbolic")
        self._remove.add_css_class("flat")
        describe(self._remove, "Remove sort column")
        self._remove.connect("clicked", lambda *_: on_remove(self))
        self._close = Gtk.Button(icon_name="window-close-symbolic")
        self._close.add_css_class("flat")
        describe(self._close, "Close sort")
        self._close.set_visible(False)
        if on_close is not None:
            self._close.connect("clicked", lambda *_: on_close())
        for widget in (
            self._column, self._direction, up, down, self._remove, self._close
        ):
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

    def is_empty(self) -> bool:
        """Untouched: first column, ascending — the state a fresh or
        cleared row starts in."""
        return self._column.get_selected() == 0 and self._direction.get_selected() == 0

    def clear(self) -> None:
        self._column.set_selected(0)
        self._direction.set_selected(0)

    def set_last(self, is_last: bool) -> None:
        """Mirrors _FilterRow.set_last: the sole remaining row can't be
        deleted, so once it's back to its blank state there is nothing
        left to remove — swap the remove button for a close button."""
        hide_remove = is_last and self.is_empty()
        self._remove.set_visible(not hide_remove)
        self._close.set_visible(hide_remove)


def _selected_string(dropdown: Gtk.DropDown) -> str:
    item = dropdown.get_selected_item()
    return item.get_string() if item is not None else ""


def _select_value(dropdown: Gtk.DropDown, text: str) -> None:
    model = dropdown.get_model()
    for i in range(model.get_n_items()):
        if model.get_string(i) == text:
            dropdown.set_selected(i)
            return


class UpdatePreviewDialog(Adw.Dialog):
    """Review step before saving cell edits: shows the UPDATE statements
    that will run, with Cancel / Execute. Values are bound as parameters
    at execution time; the preview renders them as SQL literals."""

    def __init__(
        self,
        statements: list[str],
        on_execute: Callable[[], None],
        caption: str = "Values are bound as parameters when executed.",
        width: int = 560,
        height: int = 400,
    ) -> None:
        super().__init__(
            title=f"Review Changes ({len(statements)})",
            content_width=width,
            content_height=height,
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
    """Content of one open table: a Data side and a Properties side.

    Data is the paged grid plus its action bar. Properties is the same
    read-only descriptor the info view renders (CORE-04), showing the
    sections this engine has — columns, constraints, keys, indexes,
    triggers, partitions, policies, the DDL — with every row opening
    that child object's own info view.

    The toggle at the top switches which is visible; both stay built,
    so unsaved edits, filters and the grid's scroll position survive a
    trip through Properties and back.

    Cell editing is locked until the pencil toggle is pressed. Edits are
    held locally (pending) and only hit the database after Save, which
    first shows the UPDATE statements in an UpdatePreviewDialog naming
    the connection they will run against.

    On a production connection (backend/identity.py) unlocking asks
    first, and the lock re-arms on every load — editing is never left
    open behind the user's back."""

    def __init__(
        self,
        profile: ConnectionProfile,
        table: str,
        ensure_connector: Callable[[ConnectionProfile], Connector],
        show_error: Callable[[str], None],
        on_aggregate: AggregateCallback | None = None,
        on_open_object: Callable[
            [ConnectionProfile, objects.ObjectRef], None
        ] | None = None,
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
        # first edit of the row, then {column name: new text or None
        # for NULL}.
        self._pending: dict[
            RowItem, tuple[dict[str, Any], dict[str, str | None]]
        ] = {}
        # Guards the re-entrant unlock on production connections: the
        # confirmation flips the toggle back on itself.
        self._unlock_confirmed = False
        # Read by the window's status bar after every load.
        self.read_only = False
        self._row_range = "loading…"

        # Data and Properties are two sides of one tab, held in a stack
        # so switching between them keeps both alive: the grid's rows,
        # its scroll position, its filters and any unsaved edits are
        # still there when the user comes back from Properties.
        self._stack = Gtk.Stack(vexpand=True)
        self._stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.append(self._view_switch())
        self.append(self._stack)
        data = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._stack.add_named(data, "data")

        # A table without a primary key stays read-only for as long as
        # it is open — a condition, so a banner (see feedback.py).
        self._banner = feedback.condition_banner()
        data.append(self._banner)

        # Filter and sort are separate panels behind separate toggles;
        # both can be revealed at the same time.
        self._filter_revealer = Gtk.Revealer(child=self._build_filter_panel())
        data.append(self._filter_revealer)
        self._sort_revealer = Gtk.Revealer(child=self._build_sort_panel())
        data.append(self._sort_revealer)

        self._grid = ResultGrid(
            on_edit=self._commit_edit,
            table_name=table,
            on_aggregate=on_aggregate,
            on_header_sort=self._on_header_sort,
            on_edge_reached=self._on_grid_edge_reached,
        )
        data.append(self._grid)
        # Scrolling to the bottom of the current page fetches the next
        # PAGE_SIZE rows and appends them, so browsing a big table reads
        # as one continuous scroll instead of manual paging. _base_offset
        # is where the loaded window starts (set by reload()); _loaded_rows
        # is how many rows are shown past it (grown by _load_more()).
        self._base_offset = 0
        self._loaded_rows = 0
        self._loading_more = False
        # The rows behind the current grid page, kept for the map.
        self._loaded_result_rows: list[tuple] = []
        self._map_column = ""

        bar = Gtk.ActionBar()
        self._prev = Gtk.Button(icon_name="go-previous-symbolic")
        describe(self._prev, "Previous page")
        self._prev.connect("clicked", self._on_prev)
        self._next = Gtk.Button(icon_name="go-next-symbolic")
        describe(self._next, "Next page")
        self._next.connect("clicked", self._on_next)
        self._page_label = Gtk.Label()
        bar.pack_start(self._prev)
        bar.pack_start(self._page_label)
        bar.pack_start(self._next)

        refresh = Gtk.Button(icon_name="view-refresh-symbolic")
        describe(refresh, "Refresh (discards unsaved edits)")
        refresh.connect("clicked", lambda *_: self.reload())
        self._filter_toggle = Gtk.ToggleButton(icon_name="edit-find-symbolic")
        describe(self._filter_toggle, "Filter rows")
        self._filter_toggle.connect("toggled", self._on_filter_toggled)
        self._sort_toggle = Gtk.ToggleButton(
            icon_name="view-sort-descending-symbolic"
        )
        describe(self._sort_toggle, "Sort rows")
        self._sort_toggle.connect("toggled", self._on_sort_toggled)
        self._edit_toggle = Gtk.ToggleButton(icon_name="document-edit-symbolic")
        describe(self._edit_toggle, "Unlock editing")
        self._edit_toggle.set_sensitive(False)
        self._edit_toggle.connect("toggled", self._on_edit_toggled)
        self._save = Gtk.Button()
        self._save.add_css_class("suggested-action")
        self._save.set_visible(False)
        self._save.connect("clicked", self._on_save_clicked)
        bar.pack_end(refresh)
        bar.pack_end(self._filter_toggle)
        bar.pack_end(self._sort_toggle)
        bar.pack_end(self._edit_toggle)
        bar.pack_end(self._save)
        data.append(bar)

        self._grid.on_show_map = self._on_show_map_requested
        self._grid.on_row_selected = self._on_grid_row_selected
        # The map is built the first time a result turns out to hold
        # geometries, so a table tab on a server without PostGIS never
        # pays for one (PG-04).
        self._map = None
        # None = not asked yet, "" = this server has no spatial
        # extension, otherwise its name.
        self._spatial = None

        self._properties = TablePropertiesView(
            profile, table, ensure_connector, show_error, on_open_object
        )
        self._stack.add_named(self._properties, "properties")

        self._prev.set_sensitive(False)
        self._next.set_sensitive(False)
        self.reload()

    def tab_state(self) -> TabState:
        return TabState(
            kind="table", connection=self.profile.name, table=self.table
        )

    def _view_switch(self) -> Gtk.Widget:
        """The Data | Properties toggle at the top of every table tab.

        Two linked toggles rather than a menu: which side is showing is
        part of the tab, and it should be readable without clicking
        anything."""
        row = Gtk.CenterBox(margin_top=6, margin_bottom=6)
        linked = Gtk.Box(spacing=0)
        linked.add_css_class("linked")
        self._data_toggle = Gtk.ToggleButton(label="Data", active=True)
        describe(self._data_toggle, "The table's rows")
        self._properties_toggle = Gtk.ToggleButton(label="Properties")
        describe(
            self._properties_toggle,
            "Everything else about the table: columns, keys, indexes, DDL",
        )
        self._properties_toggle.set_group(self._data_toggle)
        # Map is a third side, offered only once a load finds geometry
        # columns on a server that has a spatial extension.
        self._map_toggle = Gtk.ToggleButton(label="Map", visible=False)
        describe(
            self._map_toggle,
            "The rows' geometries drawn on a map",
        )
        self._map_toggle.set_group(self._data_toggle)
        self._data_toggle.connect("toggled", self._on_view_toggled)
        self._properties_toggle.connect("toggled", self._on_view_toggled)
        self._map_toggle.connect("toggled", self._on_view_toggled)
        linked.append(self._data_toggle)
        linked.append(self._properties_toggle)
        linked.append(self._map_toggle)
        row.set_center_widget(linked)
        return row

    def _on_view_toggled(self, button: Gtk.ToggleButton) -> None:
        # Grouped toggles fire the signal on the button that lost the
        # state as well; only the one that gained it is a switch.
        if not button.get_active():
            return
        if button is self._data_toggle:
            self._stack.set_visible_child_name("data")
            return
        if button is self._map_toggle:
            self._stack.set_visible_child_name("map")
            self._refresh_map()
            return
        self._stack.set_visible_child_name("properties")
        self._properties.ensure_loaded()

    def show_properties(self, section: str = "") -> None:
        """Switch this tab to Properties, optionally on one section.

        This is where a sidebar deep link lands (CORE-05): *Tables →
        orders → Indexes* opens (or reuses) the orders tab, flips it to
        Properties and scrolls to Indexes. The grid keeps everything it
        had, exactly as the toggle would leave it.
        """
        self._properties_toggle.set_active(True)
        # The toggle's handler does the switch; do it here too so a
        # deep link into a tab already showing Properties still works.
        self._stack.set_visible_child_name("properties")
        self._properties.ensure_loaded()
        if section:
            self._properties.select_section(section)

    def show_map(self, column: str = "", row: int | None = None) -> None:
        """Switch this tab to the Map side, optionally on one column
        and with one row's feature already highlighted — where the
        cell menu's "Show on Map" lands."""
        if not self._map_toggle.get_visible():
            return
        self._map_column = column or self._map_column
        self._map_toggle.set_active(True)
        self._stack.set_visible_child_name("map")
        self._refresh_map()
        if row is not None and self._map is not None:
            self._map.select_row(row)

    def _ensure_map(self):
        """The map widget, built on first use."""
        if self._map is None:
            from sqlide.frontend.map_view import MapView

            self._map = MapView(on_select=self._on_map_row_selected)
            self._stack.add_named(self._map, "map")
        return self._map

    def _refresh_map(self) -> None:
        """Rebuild the map's features from the rows now in the grid."""
        if not self._map_toggle.get_visible():
            return
        view = self._ensure_map()
        view.set_features(
            geo.build_features(
                self._result_names,
                self._loaded_result_rows,
                column=self._map_column,
                cap=settings_max_map_features(),
            )
        )

    def _on_show_map_requested(self, row: int, column: str) -> None:
        self.show_map(column=column, row=row - self._base_offset)

    def _on_map_row_selected(self, row: int) -> None:
        """A click on the map selects the feature's row in the grid."""
        self._grid.select_row(row + self._base_offset)

    def _on_grid_row_selected(self, row: int | None) -> None:
        if self._map is not None:
            self._map.select_row(row)

    def _update_map_availability(self) -> None:
        """Offer the Map side when this result has geometry columns and
        this server has the extension to make sense of them.

        The extension check runs once per tab, on a worker thread; until
        it answers, the grid renders geometry columns as it renders any
        other value and there is no Map toggle — a screen is never shown
        half-gated (PG-04).
        """
        if self._spatial is None:
            self._spatial = ""

            def work():
                connector = self._ensure(self.profile)
                provider = registry.create_provider(self.profile.kind, connector)
                return provider.spatial_extension()

            def done(name: str) -> None:
                self._spatial = name
                if name:
                    self._grid.geo_enabled = True
                    # Re-render the loaded page now that geometry cells
                    # can be summarised rather than shown as hex.
                    self.reload()

            run_async(work, done, lambda _exc: None)
            return
        if not self._spatial:
            return
        self._grid.geo_enabled = True
        has_geo = bool(self._grid.geometry_columns())
        self._map_toggle.set_visible(has_geo)
        if not has_geo:
            if self._map_toggle.get_active():
                self._data_toggle.set_active(True)
            return
        if not self._map_column:
            self._map_column = self._grid.geometry_columns()[0]
        if self._stack.get_visible_child_name() == "map":
            self._refresh_map()

    def unsaved_work(self) -> str:
        """The edits sitting in this grid that were never written, as a
        phrase for the confirmation that lists them — "" when there are
        none."""
        pending = sum(len(changes) for _pk, changes in self._pending.values())
        if not pending:
            return ""
        return f"{pending} unsaved edit(s)"

    def save_unsaved_work(self) -> None:
        """Write the pending edits before the tab goes. No preview
        dialog here: the confirmation that got us this far already
        listed the tabs and asked, so asking again per tab would be the
        same question twice."""
        updates = self._pending_updates()
        if not updates:
            return
        self._pending.clear()
        self._update_save_button()

        def work():
            connector = self._ensure(self.profile)
            for pk_values, column, value in updates:
                connector.update_cell(self.table, pk_values, column, value)

        run_async(work, lambda _r: None, lambda exc: self._show_error(str(exc)))

    def status_context(self) -> str:
        """This tab's line in the window's status bar: what is loaded
        and how much of it."""
        parts = [f"{self.table}: {self._row_range}"]
        if self._filters:
            parts.append(f"{len(self._filters)} filter(s)")
        if self._order_by:
            parts.append(
                "sorted by "
                + ", ".join(spec.column for spec in self._order_by)
            )
        pending = sum(len(changes) for _pk, changes in self._pending.values())
        if pending:
            parts.append(f"{pending} unsaved edit(s)")
        return " · ".join(parts)

    # Saved filters (listed in the side panel, stored per workspace)

    @property
    def filter_key(self) -> str:
        """Key of this tab's saved filters: connection.database.table
        ("-" for single-database connections like sqlite)."""
        return f"{self.profile.name}.{self.profile.database or '-'}.{self.table}"

    def current_filters(self) -> list[FilterCondition]:
        return list(self._filters)

    def apply_saved_filters(self, conditions: list[FilterCondition]) -> None:
        """Mirror a saved filter set in the panel and re-query with it."""
        self._clear_all_filter_rows()
        for cond in conditions:
            self._add_filter_row(cond)
        if not self._filter_rows:
            self._add_filter_row()
        self._filter_toggle.set_active(True)
        self._apply_filters()

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
        self._base_offset = offset
        self._loaded_rows = 0
        self._loading_more = False
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
            # Empty because the table is empty, empty because the page
            # ran past the end, and empty because a filter matched
            # nothing are three different problems.
            if filters:
                self._grid.set_empty_state(
                    "No rows match this filter",
                    "Change the conditions, or clear the filter to see "
                    "the whole table.",
                    "Clear Filter",
                    self._clear_filters,
                )
            elif offset:
                self._grid.set_empty_state(
                    "No rows on this page",
                    f"{self.table} has fewer than {offset + 1} rows.",
                    "Back to the First Page",
                    self._first_page,
                )
            else:
                self._grid.set_empty_state(
                    "No rows", f"{self.table} is empty."
                )
            self._set_column_names([c.name for c in self._columns])
            editable = any(c.is_pk for c in self._columns)
            self._pending.clear()
            self._update_save_button()
            self._grid.set_result(
                result.columns, result.rows, editable=editable, row_offset=offset
            )
            # The map draws the rows the grid is showing, so keep them.
            self._loaded_result_rows = list(result.rows)
            self._grid.set_sort_state(
                [(s.column, s.descending) for s in order_by]
            )
            self._edit_toggle.set_sensitive(editable)
            if self.profile.environment == "production":
                # Production re-arms the lock on every load, so editing
                # is never left open behind the user's back.
                self._edit_toggle.set_active(False)
            self._grid.set_unlocked(editable and self._edit_toggle.get_active())
            count = len(result)
            self._loaded_rows = count
            page = f"{offset + 1}–{offset + count}" if count else "no rows"
            if filters:
                page += " (filtered)"
            if order_by:
                page += " (sorted)"
            self._page_label.set_text(page)
            self._prev.set_sensitive(offset > 0)
            self._next.set_sensitive(count == PAGE_SIZE)
            # The read-only state and the row range belong to the
            # window's status bar; on_ran is what tells it to re-read.
            self.read_only = not editable
            feedback.set_condition(
                self._banner,
                ""
                if editable
                else f"{self.table} has no primary key, so its rows "
                "cannot be edited here",
            )
            self._row_range = page
            self._update_map_availability()
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
            self._column_names,
            self._remove_sort_row,
            self._move_sort_row,
            on_change=self._update_sort_controls,
            on_close=self._close_sort_panel,
        )
        if spec is not None:
            row.set_spec(spec)
        self._sort_rows.append(row)
        self._sort_rows_box.append(row)
        self._update_sort_controls()

    def _remove_sort_row(self, row: _SortRow) -> None:
        if len(self._sort_rows) == 1:
            # Always keep at least one row: emptying it is the closest
            # equivalent to removing it.
            row.clear()
            self._update_sort_controls()
            return
        self._sort_rows.remove(row)
        self._sort_rows_box.remove(row)
        self._update_sort_controls()

    def _clear_all_sort_rows(self) -> None:
        for row in list(self._sort_rows):
            self._sort_rows_box.remove(row)
        self._sort_rows.clear()

    def _update_sort_controls(self) -> None:
        is_last = len(self._sort_rows) == 1
        for row in self._sort_rows:
            row.set_last(is_last)

    def _close_sort_panel(self) -> None:
        self._sort_toggle.set_active(False)

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
        self._clear_all_sort_rows()
        for spec in specs:
            self._add_sort_row(spec)
        if not self._sort_rows:
            self._add_sort_row()

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
        self._clear_all_sort_rows()
        self._add_sort_row()
        if self._order_by:
            self._order_by = []
            self._offset = 0
            self.reload()

    def _on_header_sort(self, pairs: list[tuple[str, bool]]) -> None:
        """The header sort menu changed the sort columns: adopt them
        (primary first) as the order list and re-query."""
        self._order_by = [
            SortSpec(column=name, descending=descending)
            for name, descending in pairs
            if name in self._column_names
        ]
        self._set_sort_rows(self._order_by)
        self._offset = 0
        self.reload()

    def _add_filter_row(self, cond: FilterCondition | None = None) -> None:
        row = _FilterRow(
            self._column_names,
            self._remove_filter_row,
            self._apply_filters,
            on_change=self._update_filter_controls,
            on_close=self._close_filter_panel,
        )
        if cond is not None:
            row.set_condition(cond)
        self._filter_rows.append(row)
        self._filter_rows_box.append(row)
        self._update_first_row()
        self._update_filter_controls()

    def _remove_filter_row(self, row: _FilterRow) -> None:
        if len(self._filter_rows) == 1:
            # Always keep at least one row: emptying it is the closest
            # equivalent to removing it.
            row.clear()
            self._update_filter_controls()
            return
        self._filter_rows.remove(row)
        self._filter_rows_box.remove(row)
        self._update_first_row()
        self._update_filter_controls()

    def _clear_all_filter_rows(self) -> None:
        for row in list(self._filter_rows):
            self._filter_rows_box.remove(row)
        self._filter_rows.clear()

    def _update_first_row(self) -> None:
        for index, row in enumerate(self._filter_rows):
            row.set_first(index == 0)

    def _update_filter_controls(self) -> None:
        is_last = len(self._filter_rows) == 1
        for row in self._filter_rows:
            row.set_last(is_last)

    def _close_filter_panel(self) -> None:
        self._filter_toggle.set_active(False)

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
        self._clear_all_filter_rows()
        self._add_filter_row()
        if self._filters:
            self._filters = []
            self._offset = 0
            self.reload()

    def _first_page(self) -> None:
        self._offset = 0
        self.reload()

    def _on_prev(self, *_args) -> None:
        self._offset = max(0, self._offset - PAGE_SIZE)
        self.reload()

    def _on_next(self, *_args) -> None:
        self._offset += PAGE_SIZE
        self.reload()

    def _on_grid_edge_reached(self, position: Gtk.PositionType) -> None:
        if position == Gtk.PositionType.BOTTOM:
            self._load_more()

    def _load_more(self) -> None:
        if self._loading_more or not self._next.get_sensitive():
            return
        self._loading_more = True
        offset = self._base_offset + self._loaded_rows
        filters = self._filters
        order_by = self._order_by

        def work():
            connector = self._ensure(self.profile)
            return connector.fetch_rows(
                self.table, offset, PAGE_SIZE, filters=filters, order_by=order_by
            )

        def done(result):
            self._loading_more = False
            self._grid.append_rows(result.rows)
            # The map draws whatever the grid holds, appended pages
            # included, so a scroll grows the map with the grid.
            self._loaded_result_rows.extend(result.rows)
            if self._stack.get_visible_child_name() == "map":
                self._refresh_map()
            count = len(result)
            self._loaded_rows += count
            self._offset = offset
            self._next.set_sensitive(count == PAGE_SIZE)
            page = f"{self._base_offset + 1}–{self._base_offset + self._loaded_rows}"
            if filters:
                page += " (filtered)"
            if order_by:
                page += " (sorted)"
            self._page_label.set_text(page)
            self._row_range = page

        def failed(exc):
            self._loading_more = False
            self._show_error(str(exc))

        run_async(work, done, failed)

    # Editing

    def _on_edit_toggled(self, toggle: Gtk.ToggleButton) -> None:
        # Locking with pending edits keeps them pending; Save (or a
        # discarding Refresh) is still available.
        if (
            toggle.get_active()
            and self.profile.environment == "production"
            and not self._unlock_confirmed
        ):
            # Production re-arms the lock (see reload), so this asks
            # once per unlock rather than once per tab.
            toggle.set_active(False)
            confirm.present(
                self,
                heading=f"Edit “{self.table}” on production?",
                body="Cells you change here are written to "
                f"{confirm.describe_connection(self.profile)} when you "
                "save.",
                confirm_label="Unlock Editing",
                on_confirm=self._unlock_after_confirm,
            )
            return
        self._grid.set_unlocked(toggle.get_active())

    def _unlock_after_confirm(self) -> None:
        self._unlock_confirmed = True
        self._edit_toggle.set_active(True)
        self._unlock_confirmed = False

    def _commit_edit(
        self, row: RowItem, index: int, new_text: str | None
    ) -> None:
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

    def _pending_updates(self) -> list[tuple[dict[str, Any], str, str | None]]:
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
            statements,
            lambda: self._execute_updates(updates),
            caption="Values are bound as parameters when executed. "
            f"They are written to {confirm.describe_connection(self.profile)}.",
        )
        dialog.present(self)

    def _execute_updates(
        self, updates: list[tuple[dict[str, Any], str, str | None]]
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
