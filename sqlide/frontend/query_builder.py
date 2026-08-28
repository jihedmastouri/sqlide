"""Visual query builder tab.

Composes a SELECT statement without typing SQL: pick a base table,
add joins (prefilled from the schema's foreign keys when possible),
tick the columns to select, and add filter / sort lines (the same row
widgets the table tab uses). The generated SQL is always visible in a
read-only preview and updates live with every change.

Run executes the statement and shows the rows in a ResultGrid below;
Open in Console hands the SQL to a fresh query console for manual
tweaking. The whole catalog (tables, columns, relations) is loaded
once up front — like the relation graph tab — so join and column
choices never need another round trip.

Column identities are handled qualified ("table.column") internally;
the rendered SQL drops the table prefix while no join is present.
"""

from __future__ import annotations

from typing import Callable

from gi.repository import Gtk

from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.db.base import (
    NO_VALUE_OPERATORS,
    ColumnInfo,
    Connector,
    RelationInfo,
    ResultSet,
)
from sqlide.backend.settings import result_row_cap
from sqlide.backend.workspaces import TabState
from sqlide.frontend.data_grid import (
    AggregateCallback,
    ResultGrid,
    ValueCallback,
    _FilterRow,
    _selected_string,
    _sql_literal,
    _SortRow,
)
from sqlide.frontend.results_panel import ResultsPanel
from sqlide.frontend.util import describe, row_count, run_async
from sqlide.i18n import _

JOIN_KINDS = ("INNER JOIN", "LEFT JOIN", "RIGHT JOIN")
DEFAULT_LIMIT = 500


class _JoinRow(Gtk.Box):
    """One join line: [join kind] [table] ON [left column] = [right
    column]. The builder feeds the column choices (set_choices) as the
    tables before this line change."""

    def __init__(
        self,
        tables: list[str],
        on_remove: Callable[["_JoinRow"], None],
        on_change: Callable[[], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._on_change = on_change
        self._updating = False
        # A refilled DropDown auto-selects its first item, so "left()
        # is empty" can't signal "not chosen yet" — track explicit ON
        # picks instead (reset when the joined table changes) and let
        # the builder prefill from a foreign key until then.
        self._on_touched = False
        self._kind = Gtk.DropDown(model=Gtk.StringList.new(list(JOIN_KINDS)))
        self._table = Gtk.DropDown(model=Gtk.StringList.new(tables))
        self._left = Gtk.DropDown(
            model=Gtk.StringList.new([]), hexpand=True
        )
        self._right = Gtk.DropDown(
            model=Gtk.StringList.new([]), hexpand=True
        )
        remove = Gtk.Button(icon_name="list-remove-symbolic")
        remove.add_css_class("flat")
        describe(remove, _("Remove join"))
        remove.connect("clicked", lambda *_: on_remove(self))
        for widget in (self._kind, self._table):
            self.append(widget)
        self.append(Gtk.Label(label="ON"))
        self.append(self._left)
        self.append(Gtk.Label(label="="))
        self.append(self._right)
        self.append(remove)
        for dropdown in (self._kind, self._table, self._left, self._right):
            dropdown.connect("notify::selected", self._changed)

    def _changed(self, dropdown, *_args) -> None:
        if self._updating:
            return
        if dropdown in (self._left, self._right):
            self._on_touched = True
        elif dropdown is self._table:
            self._on_touched = False
        self._on_change()

    def on_touched(self) -> bool:
        return self._on_touched

    def kind(self) -> str:
        return _selected_string(self._kind)

    def table(self) -> str:
        return _selected_string(self._table)

    def left(self) -> str:
        return _selected_string(self._left)

    def right(self) -> str:
        return _selected_string(self._right)

    def set_choices(self, left: list[str], right: list[str]) -> None:
        """Refill the ON dropdowns, keeping the current picks when they
        survive the refill (guarded so this never loops via _changed)."""
        self._updating = True
        try:
            for dropdown, names in ((self._left, left), (self._right, right)):
                selected = _selected_string(dropdown)
                dropdown.set_model(Gtk.StringList.new(names))
                if selected in names:
                    dropdown.set_selected(names.index(selected))
        finally:
            self._updating = False

    def prefill(self, left: str, right: str) -> None:
        self._updating = True
        try:
            for dropdown, name in ((self._left, left), (self._right, right)):
                model = dropdown.get_model()
                for i in range(model.get_n_items()):
                    if model.get_string(i) == name:
                        dropdown.set_selected(i)
                        break
        finally:
            self._updating = False


class QueryBuilderTab(Gtk.Box):
    """Content of one query-builder tab."""

    def __init__(
        self,
        profile: ConnectionProfile,
        ensure_connector: Callable[[ConnectionProfile], Connector],
        show_error: Callable[[str], None],
        table: str = "",
        on_aggregate: AggregateCallback | None = None,
        on_value: ValueCallback | None = None,
        on_open_console: Callable[[ConnectionProfile, str], None] | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.profile = profile
        self._ensure = ensure_connector
        self._show_error = show_error
        self._on_aggregate = on_aggregate
        self._on_value = on_value
        self._on_open_console = on_open_console
        self._initial_table = table
        # Rebound by the window once the tab page exists (like TableTab).
        self.on_ran: Callable[[str, bool], None] | None = None

        self._tables: list[str] = []
        self._columns: dict[str, list[ColumnInfo]] = {}
        self._relations: list[RelationInfo] = []
        self._quote: Callable[[str], str] = lambda name: name
        self._join_rows: list[_JoinRow] = []
        self._filter_rows: list[_FilterRow] = []
        self._sort_rows: list[_SortRow] = []
        self._checked: set[str] = set()  # qualified "table.column"
        self._column_checks: list[Gtk.CheckButton] = []
        self._loading = True

        paned = Gtk.Paned(
            orientation=Gtk.Orientation.VERTICAL,
            vexpand=True,
            resize_start_child=False,
            shrink_start_child=False,
            shrink_end_child=False,
        )
        paned.set_start_child(self._build_controls())
        paned.set_end_child(self._build_results(paned))
        self.append(paned)

        self._load_catalog()

    def tab_state(self) -> TabState:
        return TabState(
            kind="querybuilder",
            connection=self.profile.name,
            table=self._base_table(),
        )

    # UI construction

    def _build_controls(self) -> Gtk.Widget:
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=6,
            margin_top=6,
            margin_bottom=6,
            margin_start=6,
            margin_end=6,
        )

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        top.append(Gtk.Label(label=_("Table")))
        self._table_dropdown = Gtk.DropDown(model=Gtk.StringList.new([]))
        self._table_dropdown.connect(
            "notify::selected", lambda *_: self._base_changed()
        )
        top.append(self._table_dropdown)
        self._distinct = Gtk.CheckButton(label=_("Distinct"))
        self._distinct.connect("toggled", lambda *_: self._refresh_sql())
        top.append(self._distinct)
        top.append(Gtk.Box(hexpand=True))
        top.append(Gtk.Label(label=_("Limit")))
        self._limit = Gtk.SpinButton.new_with_range(1, 1_000_000, 100)
        self._limit.set_value(DEFAULT_LIMIT)
        self._limit.connect("value-changed", lambda *_: self._refresh_sql())
        top.append(self._limit)
        box.append(top)

        box.append(self._section_label("Joins"))
        self._joins_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=6
        )
        box.append(self._joins_box)
        add_join = Gtk.Button(label=_("Add join"), halign=Gtk.Align.START)
        add_join.connect("clicked", lambda *_: self._add_join_row())
        box.append(add_join)

        box.append(self._section_label("Columns (none checked = all)"))
        self._columns_flow = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            max_children_per_line=6,
            column_spacing=12,
            row_spacing=2,
        )
        box.append(self._columns_flow)

        box.append(self._section_label("Filters"))
        self._filters_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=6
        )
        box.append(self._filters_box)
        add_filter = Gtk.Button(
            label=_("Add condition"), halign=Gtk.Align.START
        )
        add_filter.connect("clicked", lambda *_: self._add_filter_row())
        box.append(add_filter)

        box.append(self._section_label("Sort"))
        self._sorts_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=6
        )
        box.append(self._sorts_box)
        add_sort = Gtk.Button(
            label=_("Add sort column"), halign=Gtk.Align.START
        )
        add_sort.connect("clicked", lambda *_: self._add_sort_row())
        box.append(add_sort)

        box.append(self._section_label("SQL"))
        self._sql_view = Gtk.TextView(
            editable=False,
            monospace=True,
            cursor_visible=False,
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
            left_margin=8,
            right_margin=8,
            top_margin=6,
            bottom_margin=6,
        )
        sql_frame = Gtk.Frame(child=self._sql_view)
        box.append(sql_frame)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        run = Gtk.Button(label=_("Run"))
        run.add_css_class("suggested-action")
        run.connect("clicked", lambda *_: self.run_query())
        actions.append(run)
        console = Gtk.Button(label=_("Open in Console"))
        console.set_tooltip_text(_("Edit this SQL in a new query console"))
        console.connect("clicked", self._open_in_console)
        actions.append(console)
        self._status = Gtk.Label(xalign=1, hexpand=True)
        self._status.add_css_class("dim-label")
        actions.append(self._status)
        box.append(actions)

        scroller = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True
        )
        scroller.set_max_content_height(420)
        scroller.set_child(box)
        return scroller

    def _build_results(self, paned: Gtk.Paned) -> Gtk.Widget:
        # Same collapsible panel as the query console, hidden until the
        # first run produces rows.
        self._grid = ResultGrid(
            on_aggregate=self._on_aggregate, on_value=self._on_value
        )
        self._results_panel = ResultsPanel(
            self._grid, paned, on_export=self._export_result
        )
        return self._results_panel

    def _export_result(self) -> None:
        """The results header's Export — the built rows, as a file."""
        from sqlide.frontend.export_dialog import ExportDialog

        ExportDialog.for_grid(self._grid).present(self)

    @staticmethod
    def _section_label(text: str) -> Gtk.Label:
        label = Gtk.Label(label=text, xalign=0)
        label.add_css_class("dim-label")
        return label

    # Catalog

    def _load_catalog(self) -> None:
        self._status.set_text(_("Loading schema…"))

        def work():
            connector = self._ensure(self.profile)
            tables = [
                t.name for t in connector.list_tables() if t.kind == "table"
            ]
            columns = {name: connector.list_columns(name) for name in tables}
            relations = connector.list_relations()
            # quote_ident is a pure string function; safe to keep for
            # main-thread SQL generation.
            return tables, columns, relations, connector.quote_ident

        def done(result) -> None:
            self._tables, self._columns, self._relations, self._quote = result
            self._loading = True
            try:
                self._table_dropdown.set_model(
                    Gtk.StringList.new(self._tables)
                )
                if self._initial_table in self._tables:
                    self._table_dropdown.set_selected(
                        self._tables.index(self._initial_table)
                    )
            finally:
                self._loading = False
            self._status.set_text("")
            self._base_changed()

        def failed(exc: Exception) -> None:
            self._status.set_text("")
            self._show_error(str(exc))

        run_async(work, done, failed)

    # State

    def _base_table(self) -> str:
        return _selected_string(self._table_dropdown)

    def _query_tables(self) -> list[str]:
        """Base table plus joined tables, in join order."""
        tables = [self._base_table()]
        for row in self._join_rows:
            if row.table():
                tables.append(row.table())
        return [t for t in tables if t]

    def _qualified_columns(self, tables: list[str]) -> list[str]:
        return [
            f"{table}.{column.name}"
            for table in tables
            for column in self._columns.get(table, [])
        ]

    def _base_changed(self) -> None:
        if self._loading:
            return
        self._sync_state()

    def _sync_state(self) -> None:
        """Re-derive join choices, the column checklist and the filter
        and sort column lists from the current base + joins, then
        refresh the SQL preview."""
        tables = self._query_tables()
        seen: list[str] = tables[:1]
        for row in self._join_rows:
            right_table = row.table()
            left_choices = self._qualified_columns(seen)
            right_choices = self._qualified_columns(
                [right_table] if right_table else []
            )
            row.set_choices(left_choices, right_choices)
            if right_table and not row.on_touched():
                prefill = self._relation_for(seen, right_table)
                if prefill is not None:
                    row.prefill(*prefill)
            if right_table:
                seen.append(right_table)

        names = self._display_columns(tables)
        self._rebuild_column_checks(tables)
        for row in self._filter_rows:
            row.set_columns(names)
        for row in self._sort_rows:
            row.set_columns(names)
        self._refresh_sql()

    def _relation_for(
        self, left_tables: list[str], right_table: str
    ) -> tuple[str, str] | None:
        """Foreign key connecting the joined table to any table already
        in the query, as ("t.col", "t.col") for the ON dropdowns."""
        for rel in self._relations:
            if rel.table == right_table and rel.ref_table in left_tables:
                return (
                    f"{rel.ref_table}.{rel.ref_column or ''}",
                    f"{rel.table}.{rel.column}",
                )
            if rel.ref_table == right_table and rel.table in left_tables:
                return (
                    f"{rel.table}.{rel.column}",
                    f"{rel.ref_table}.{rel.ref_column or ''}",
                )
        return None

    def _display_columns(self, tables: list[str]) -> list[str]:
        """Column names as shown in filters/sorts: qualified as soon as
        a join is present."""
        if len(tables) > 1:
            return self._qualified_columns(tables)
        return [
            c.name for t in tables for c in self._columns.get(t, [])
        ]

    def _rebuild_column_checks(self, tables: list[str]) -> None:
        while (child := self._columns_flow.get_first_child()) is not None:
            self._columns_flow.remove(child)
        self._column_checks = []
        valid = set(self._qualified_columns(tables))
        self._checked &= valid
        for qualified in self._qualified_columns(tables):
            label = qualified if len(tables) > 1 else qualified.split(".", 1)[1]
            check = Gtk.CheckButton(label=label)
            check.qualified = qualified
            check.set_active(qualified in self._checked)
            check.connect("toggled", self._column_toggled)
            self._column_checks.append(check)
            self._columns_flow.append(check)

    def _column_toggled(self, check: Gtk.CheckButton) -> None:
        if check.get_active():
            self._checked.add(check.qualified)
        else:
            self._checked.discard(check.qualified)
        self._refresh_sql()

    # Join / filter / sort rows

    def _add_join_row(self) -> None:
        if not self._tables:
            return
        row = _JoinRow(self._tables, self._remove_join_row, self._sync_state)
        self._join_rows.append(row)
        self._joins_box.append(row)
        self._sync_state()

    def _remove_join_row(self, row: _JoinRow) -> None:
        self._join_rows.remove(row)
        self._joins_box.remove(row)
        self._sync_state()

    def _add_filter_row(self) -> None:
        row = _FilterRow(
            self._display_columns(self._query_tables()),
            self._remove_filter_row,
            self.run_query,
            on_change=self._refresh_sql,
        )
        self._filter_rows.append(row)
        self._filters_box.append(row)
        for index, line in enumerate(self._filter_rows):
            line.set_first(index == 0)
        self._refresh_sql()

    def _remove_filter_row(self, row: _FilterRow) -> None:
        self._filter_rows.remove(row)
        self._filters_box.remove(row)
        for index, line in enumerate(self._filter_rows):
            line.set_first(index == 0)
        self._refresh_sql()

    def _add_sort_row(self) -> None:
        row = _SortRow(
            self._display_columns(self._query_tables()),
            self._remove_sort_row,
            self._move_sort_row,
            on_change=self._refresh_sql,
        )
        self._sort_rows.append(row)
        self._sorts_box.append(row)
        self._refresh_sql()

    def _remove_sort_row(self, row: _SortRow) -> None:
        self._sort_rows.remove(row)
        self._sorts_box.remove(row)
        self._refresh_sql()

    def _move_sort_row(self, row: _SortRow, delta: int) -> None:
        index = self._sort_rows.index(row)
        target = index + delta
        if not 0 <= target < len(self._sort_rows):
            return
        self._sort_rows.insert(target, self._sort_rows.pop(index))
        for widget in self._sort_rows:
            self._sorts_box.remove(widget)
        for widget in self._sort_rows:
            self._sorts_box.append(widget)
        self._refresh_sql()

    # SQL generation

    def _quote_column(self, name: str, multi: bool) -> str:
        """Quote a display column name; qualified names ("t.c") become
        quoted pairs, plain names a single identifier."""
        if "." in name:
            table, _, column = name.partition(".")
            if multi:
                return f"{self._quote(table)}.{self._quote(column)}"
            return self._quote(column)
        return self._quote(name)

    def build_sql(self) -> str:
        base = self._base_table()
        if not base:
            return ""
        joins = [
            row
            for row in self._join_rows
            if row.table() and row.left() and row.right()
        ]
        multi = bool(joins)
        if self._checked:
            # Keep the schema's column order rather than click order.
            columns = ", ".join(
                self._quote_column(q, multi)
                for q in self._qualified_columns(self._query_tables())
                if q in self._checked
            )
        else:
            columns = "*"
        sql = "SELECT "
        if self._distinct.get_active():
            sql += "DISTINCT "
        sql += f"{columns}\nFROM {self._quote(base)}"
        for row in joins:
            sql += (
                f"\n{row.kind()} {self._quote(row.table())}"
                f" ON {self._quote_column(row.left(), True)}"
                f" = {self._quote_column(row.right(), True)}"
            )
        where = ""
        for row in self._filter_rows:
            cond = row.condition()
            if not cond.column:
                continue
            clause = f"{self._quote_column(cond.column, multi)} {cond.op}"
            if cond.op not in NO_VALUE_OPERATORS:
                clause += f" {_sql_literal(cond.value)}"
            where = (
                f"({where}) {cond.conjunction} {clause}" if where else clause
            )
        if where:
            sql += f"\nWHERE {where}"
        order = [
            f"{self._quote_column(row.spec().column, multi)} "
            + ("DESC" if row.spec().descending else "ASC")
            for row in self._sort_rows
            if row.spec().column
        ]
        if order:
            sql += "\nORDER BY " + ", ".join(order)
        sql += f"\nLIMIT {int(self._limit.get_value())};"
        return sql

    def _refresh_sql(self) -> None:
        self._sql_view.get_buffer().set_text(self.build_sql())

    # Actions

    def run_query(self) -> None:
        sql = self.build_sql()
        if not sql:
            self._show_error("Pick a table first")
            return
        self._refresh_sql()
        self._status.set_text(_("Running…"))

        max_rows = result_row_cap()

        def work():
            connector = self._ensure(self.profile)
            return connector.execute(sql, max_rows=max_rows)

        def done(result) -> None:
            if isinstance(result, ResultSet):
                self._results_panel.reveal()
                self._grid.set_result(result.columns, result.rows)
                self._status.set_text(
                    _("first %s of a larger result") % row_count(len(result))
                    if result.truncated
                    else row_count(len(result))
                )
            else:
                self._status.set_text(
                    _("%s affected") % row_count(result)
                )
            if self.on_ran is not None:
                self.on_ran(sql, True)

        def failed(exc: Exception) -> None:
            self._status.set_text("")
            self._show_error(str(exc))
            if self.on_ran is not None:
                self.on_ran(sql, False)

        run_async(work, done, failed)

    def _open_in_console(self, *_args) -> None:
        sql = self.build_sql()
        if not sql:
            self._show_error("Pick a table first")
            return
        if self._on_open_console is not None:
            self._on_open_console(self.profile, sql)
