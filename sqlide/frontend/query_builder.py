"""Visual query builder tab.

Composes a SELECT statement without typing SQL: pick a base table,
add joins (prefilled from the schema's foreign keys when possible),
tick the columns to select, and add filter / sort lines (the same row
widgets the table tab uses). The generated SQL is always visible in a
read-only preview and updates live with every change.

The SQL itself is not written here: the widgets describe a
`QueryModel` (backend/db/query_model.py) and the renderer turns that
into dialect-correct SQL, with filter values bound as parameters
rather than pasted in as literals (CORE-17). The preview shows the
same statement with its values written in, so what is shown is what
runs.

Run executes the statement and shows the rows in a ResultGrid below;
Open in Console hands the SQL to a fresh query console for manual
tweaking. The whole catalog (sources, columns, relations) is loaded
once up front — like the relation graph tab — so join and column
choices never need another round trip.

The catalog comes from the MetadataProvider, never from the connector
directly (CORE-18): that is what knows the engine has schemas, which
relations can be selected from — views and materialized views
included — and what it can do at all. A source is therefore identified
by its key, "schema.table" where the engine has schemas and the bare
name where it does not, and the same key qualifies its columns
("key.column") inside the tab; the rendered SQL drops the prefix while
no join is present, and writes the schema only where there is one.

Because the model is data, the tab persists it: `tab_state` writes the
whole query into the workspace and a restored tab rehydrates it once
the catalog is in, dropping any join, column, filter or sort whose
table or column the database no longer has and saying how many in the
status label (CORE-19). Nothing is parsed back out of SQL — generation
stays one-way, and the model is what survives a restart.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

from gi.repository import Gtk

from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.db import registry
from sqlide.backend.db.base import (
    NO_VALUE_OPERATORS,
    ColumnInfo,
    Connector,
    ConnectorError,
    FilterCondition,
    RelationInfo,
    ResultSet,
    SortSpec,
)
from sqlide.backend.db.metadata import Capabilities, NodeRef
from sqlide.backend.db.query_model import (
    Column,
    Condition,
    Dialect,
    GENERIC,
    Join,
    On,
    Order,
    Projection,
    QueryModel,
    TableRef,
    dialect_for,
    dump_state,
    folded_group,
    load_state,
    render,
    render_display,
    unfold_group,
)
from sqlide.backend.settings import result_row_cap
from sqlide.backend.workspaces import TabState
from sqlide.frontend.data_grid import (
    AggregateCallback,
    ResultGrid,
    ValueCallback,
    _FilterRow,
    _selected_string,
    _SortRow,
)
from sqlide.frontend.results_panel import ResultsPanel
from sqlide.frontend.util import describe, row_count, run_async
from sqlide.i18n import _, ngettext

# What the builder offers. The connected engine may have fewer (SQLite
# before 3.39 has no RIGHT JOIN); the dialect says which, and the
# dropdown is filled from the intersection once the catalog loads.
JOIN_KINDS = ("INNER JOIN", "LEFT JOIN", "RIGHT JOIN")
DEFAULT_LIMIT = 500


@dataclass(frozen=True)
class _Source:
    """One relation the builder can select from, as the provider
    handed it over: the node itself plus the two strings the UI needs.

    `key` is the identity everything else uses — the qualified name on
    an engine with schemas, the bare name elsewhere — so two tables of
    the same name in different schemas never collapse into one entry.
    """

    ref: NodeRef
    key: str
    label: str

    @property
    def name(self) -> str:
        return self.ref.name

    @property
    def schema(self) -> str:
        return self.ref.schema


def _source_of(ref: NodeRef, *, schemas: bool) -> _Source:
    """A provider node as a picker entry. The note says what is being
    selected when it is not a plain table — a view is a perfectly good
    source, but you should be able to see that it is one."""
    key = f"{ref.schema}.{ref.name}" if schemas and ref.schema else ref.name
    if ref.kind == "view":
        note = (
            _("materialized view")
            if ref.detail == "materialized"
            else _("view")
        )
    else:
        note = _(ref.detail) if ref.detail else ""
    return _Source(ref=ref, key=key, label=f"{key}  ·  {note}" if note else key)


def _selected_key(dropdown: Gtk.DropDown, keys: Sequence[str]) -> str:
    """The key behind the selected row — the dropdown shows labels."""
    index = dropdown.get_selected()
    if 0 <= index < len(keys):
        return keys[index]
    return ""


class _JoinRow(Gtk.Box):
    """One join line: [join kind] [table] ON [left column] = [right
    column]. The builder feeds the column choices (set_choices) as the
    tables before this line change."""

    def __init__(
        self,
        sources: Sequence[_Source],
        on_remove: Callable[["_JoinRow"], None],
        on_change: Callable[[], None],
        kinds: Sequence[str] = JOIN_KINDS,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._on_change = on_change
        # The dropdown shows labels ("crm.orders · view"); everything
        # else works in keys, so the two never get mixed up.
        self._keys = [source.key for source in sources]
        self._updating = False
        # A refilled DropDown auto-selects its first item, so "left()
        # is empty" can't signal "not chosen yet" — track explicit ON
        # picks instead (reset when the joined table changes) and let
        # the builder prefill from a foreign key until then.
        self._on_touched = False
        self._kind = Gtk.DropDown(model=Gtk.StringList.new(list(kinds)))
        self._table = Gtk.DropDown(
            model=Gtk.StringList.new([source.label for source in sources])
        )
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
        """The joined source's key, not its label."""
        return _selected_key(self._table, self._keys)

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

    def restore_kind_and_table(self, kind: str, key: str) -> None:
        """Put a saved line's join kind and joined source back, without
        firing the change callback: the builder re-syncs once, after
        every restored row is in place."""
        self._updating = True
        try:
            model = self._kind.get_model()
            for i in range(model.get_n_items()):
                if model.get_string(i) == kind:
                    self._kind.set_selected(i)
                    break
            if key in self._keys:
                self._table.set_selected(self._keys.index(key))
        finally:
            self._updating = False

    def restore_on(self, left: str, right: str) -> None:
        """The saved ON columns, once the choices have been filled.

        Marks the line as touched, so the foreign-key prefill never
        overwrites what the user actually built.
        """
        self.prefill(left, right)
        self._on_touched = True

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
        builder: str = "",
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
        # The saved query this tab is being restored from, if any
        # (TabState.builder); rehydrated once the catalog has loaded,
        # since only then do we know which tables and columns still
        # exist. Unreadable or from a future version reads as None.
        self._restore: QueryModel | None = load_state(builder)
        # Rebound by the window once the tab page exists (like TableTab).
        self.on_ran: Callable[[str, bool], None] | None = None

        self._sources: list[_Source] = []
        self._columns: dict[str, list[ColumnInfo]] = {}  # by source key
        self._relations: list[RelationInfo] = []
        self._caps = Capabilities()
        self._sql_dialect: Dialect = GENERIC
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
        """What the workspace saves: the whole query as data, plus the
        base table on its own so an older build (and this one, before
        the catalog is in) can still reopen the tab (CORE-19)."""
        base = self._base_table()
        if not base and self._restore is not None:
            # The catalog never arrived (offline restore): hand back
            # what we were given rather than dropping the query.
            return TabState(
                kind="querybuilder",
                connection=self.profile.name,
                table=self._initial_table,
                builder=dump_state(self._restore),
            )
        return TabState(
            kind="querybuilder",
            connection=self.profile.name,
            table=base,
            builder=dump_state(self.query_model()) if base else "",
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
            # Everything the builder knows about the database comes
            # through the provider (CORE-18): it is what knows about
            # schemas, which relations are selectable, and what this
            # engine can do. The connector is only asked for its
            # quoting, via dialect_for.
            provider = registry.create_provider(self.profile.kind, connector)
            caps = provider.capabilities()
            sources = [
                _source_of(ref, schemas=caps.schemas)
                for ref in provider.list_sources()
            ]
            columns = {
                source.key: provider.columns_of(source.ref)
                for source in sources
            }
            # A Dialect is pure data (quote_ident is a pure string
            # function); safe to keep for main-thread SQL generation.
            return (
                sources,
                columns,
                provider.relations(),
                caps,
                dialect_for(connector),
            )

        def done(result) -> None:
            (
                self._sources,
                self._columns,
                self._relations,
                self._caps,
                self._sql_dialect,
            ) = result
            self._loading = True
            try:
                self._table_dropdown.set_model(
                    Gtk.StringList.new(
                        [source.label for source in self._sources]
                    )
                )
                index = self._initial_index()
                if index is not None:
                    self._table_dropdown.set_selected(index)
            finally:
                self._loading = False
            self._status.set_text("")
            self._base_changed()
            self._restore_model()

        def failed(exc: Exception) -> None:
            self._status.set_text("")
            self._show_error(str(exc))

        run_async(work, done, failed)

    def _initial_index(self) -> int | None:
        """Where the table this tab was opened on sits in the picker.

        The name may arrive qualified (from a schema node, or from a
        restored tab) or bare (from a menu on an engine without
        schemas), so both are matched — the qualified one first, since
        it is the one that says which table it means.
        """
        wanted = self._initial_table
        if not wanted:
            return None
        keys = [source.key for source in self._sources]
        if wanted in keys:
            return keys.index(wanted)
        for index, source in enumerate(self._sources):
            if source.name == wanted:
                return index
        return None

    # Restoring a saved query (CORE-19)

    def _picker_key(self, ref: TableRef) -> str:
        """The picker key for a saved model source, or "" when that
        table is no longer in the catalog.

        The saved schema is matched first, since it is the half that
        says which table is meant; a saved bare name is accepted when
        exactly one source answers to it (the catalog gained schemas,
        or the workspace predates them).
        """
        candidate = (
            f"{ref.schema}.{ref.name}"
            if self._caps.schemas and ref.schema
            else ref.name
        )
        keys = [source.key for source in self._sources]
        if candidate in keys:
            return candidate
        matches = [s.key for s in self._sources if s.name == ref.name]
        return matches[0] if len(matches) == 1 else ""

    def _restored_column(
        self, column: Column | None, sources: dict[str, str], base: str
    ) -> str | None:
        """A saved model column as the qualified "key.column" the tab
        works in, or None when its table or column is gone."""
        if column is None or not column.name:
            return None
        key = sources.get(column.source, "") if column.source else base
        if not key:
            return None
        if column.name not in {c.name for c in self._columns.get(key, [])}:
            return None
        return f"{key}.{column.name}"

    def _restore_model(self) -> None:
        """Rebuild the widgets from the query this tab was restored
        with, dropping whatever the database no longer has.

        A saved query is data about a schema that has moved on since:
        a table may have been dropped, a column renamed. Every part is
        therefore checked against the freshly loaded catalog and
        silently left out when it no longer resolves — the count is
        reported in the status label. Losing a filter must never cost
        you the rest of the query, and must never be an error dialog.
        """
        model, self._restore = self._restore, None
        if model is None or model.source is None:
            return
        sources = {
            ref.key: key
            for ref in model.sources
            if (key := self._picker_key(ref))
        }
        base = sources.get(model.source.key, "")
        if not base:
            self._status.set_text(
                _("The saved query's table is gone; starting fresh.")
            )
            return
        dropped = 0
        keys = [source.key for source in self._sources]
        self._loading = True
        try:
            self._table_dropdown.set_selected(keys.index(base))
            self._distinct.set_active(model.distinct)
            if model.limit:
                self._limit.set_value(model.limit)
            kinds = self._join_kinds()
            restored_joins: list[tuple[_JoinRow, str, str]] = []
            for join in model.joins:
                key = sources.get(join.source.key, "")
                on = join.on[0] if join.on else None
                left = right = None
                if key and on is not None:
                    left = self._restored_column(on.left, sources, base)
                    right = self._restored_column(on.right, sources, base)
                if not key or left is None or right is None:
                    dropped += 1
                    continue
                row = _JoinRow(
                    self._sources,
                    self._remove_join_row,
                    self._sync_state,
                    kinds=kinds if join.kind in kinds else [join.kind, *kinds],
                )
                row.restore_kind_and_table(join.kind, key)
                self._join_rows.append(row)
                self._joins_box.append(row)
                restored_joins.append((row, left, right))
            tables = self._query_tables()
            qualified = set(self._qualified_columns(tables))
            checked: set[str] = set()
            for projection in model.projections:
                name = self._restored_column(projection.column, sources, base)
                if name is None or name not in qualified:
                    dropped += 1
                    continue
                checked.add(name)
            self._checked = checked
            names = self._display_columns(tables)
            lines = unfold_group(model.where)
            if lines is None and model.where is not None:
                # A filter tree the flat panel cannot show (nothing
                # builds one yet — CORE-22 will).
                dropped += 1
            for conjunction, condition in lines or []:
                name = self._restored_column(condition.column, sources, base)
                display = self._display_name(name, tables)
                if display is None or display not in names:
                    dropped += 1
                    continue
                self._add_filter_row()
                self._filter_rows[-1].set_condition(
                    FilterCondition(
                        column=display,
                        op=condition.op,
                        value=(
                            ""
                            if condition.value is None
                            else str(condition.value)
                        ),
                        conjunction=conjunction,
                    )
                )
            for order in model.order_by:
                name = self._restored_column(order.column, sources, base)
                display = self._display_name(name, tables)
                if display is None or display not in names:
                    dropped += 1
                    continue
                self._add_sort_row()
                self._sort_rows[-1].set_spec(
                    SortSpec(column=display, descending=order.descending)
                )
        finally:
            self._loading = False
        # The ON dropdowns are empty until the choices are derived, so
        # the saved columns go in after the first sync, not before.
        self._sync_state()
        for row, left, right in restored_joins:
            row.restore_on(left, right)
        self._sync_state()
        if dropped:
            self._status.set_text(
                ngettext(
                    "%d part of the saved query no longer exists and was dropped.",
                    "%d parts of the saved query no longer exist and were dropped.",
                    dropped,
                )
                % dropped
            )

    @staticmethod
    def _display_name(qualified: str | None, tables: list[str]) -> str | None:
        """A qualified "key.column" as the filter/sort panels spell it:
        qualified once a join is present, bare otherwise."""
        if qualified is None:
            return None
        return qualified if len(tables) > 1 else qualified.rpartition(".")[2]

    # State

    def _base_table(self) -> str:
        """The base source's key ("schema.table" where the engine has
        schemas), which is what the rest of the tab identifies a
        source by."""
        return _selected_key(
            self._table_dropdown, [source.key for source in self._sources]
        )

    def _source(self, key: str) -> _Source | None:
        for source in self._sources:
            if source.key == key:
                return source
        return None

    def _query_tables(self) -> list[str]:
        """Base source plus joined sources, by key, in join order."""
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

    def _relation_key(self, schema: str, table: str) -> str:
        """A foreign key's end as a source key. The engines with
        schemas fill `RelationInfo.schema` / `ref_schema` (PG-01), so a
        key that crosses a schema resolves to the right table; where
        the engine leaves them empty the name is the key, as long as
        it names exactly one source."""
        if self._caps.schemas and schema:
            return f"{schema}.{table}"
        matches = [s.key for s in self._sources if s.name == table]
        return matches[0] if len(matches) == 1 else table

    def _relation_for(
        self, left_tables: list[str], right_table: str
    ) -> tuple[str, str] | None:
        """Foreign key connecting the joined source to any source
        already in the query, as ("key.col", "key.col") for the ON
        dropdowns."""
        for rel in self._relations:
            here = self._relation_key(rel.schema, rel.table)
            there = self._relation_key(rel.ref_schema, rel.ref_table)
            if here == right_table and there in left_tables:
                return (
                    f"{there}.{rel.ref_column or ''}",
                    f"{here}.{rel.column}",
                )
            if there == right_table and here in left_tables:
                return (
                    f"{here}.{rel.column}",
                    f"{there}.{rel.ref_column or ''}",
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
            label = (
                qualified if len(tables) > 1 else qualified.rpartition(".")[2]
            )
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
        if not self._sources:
            return
        row = _JoinRow(
            self._sources,
            self._remove_join_row,
            self._sync_state,
            kinds=self._join_kinds(),
        )
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

    def _table_refs(self) -> dict[str, TableRef]:
        """Every source in the query as a model TableRef, by key.

        The schema rides along only where the engine has schemas, so
        the rendered SQL says `crm.orders` on PostgreSQL and plain
        `orders` on SQLite and MySQL. A bare name shared by two
        schemas gets the qualified key as its alias, which is what
        keeps the columns of the two apart in the statement.
        """
        keys = self._query_tables()
        names = [
            (source.name if (source := self._source(key)) else key)
            for key in keys
        ]
        refs: dict[str, TableRef] = {}
        taken: set[str] = set()
        for key in keys:
            source = self._source(key)
            name = source.name if source else key
            schema = (
                source.schema if source and self._caps.schemas else ""
            )
            alias = ""
            if names.count(name) > 1:
                # Two schemas, one table name: the statement needs an
                # alias to tell the columns of the two apart, and
                # "crm_users" reads better in it than the dotted key.
                alias = f"{schema}_{name}" if schema else name
                while alias in taken:
                    alias += "_"
                taken.add(alias)
            refs[key] = TableRef(name=name, schema=schema, alias=alias)
        return refs

    def _column_ref(
        self, name: str, refs: dict[str, TableRef] | None = None
    ) -> Column:
        """A display column name ("key.c" once a join is present, "c"
        otherwise) as a model reference. The source key is the picker's
        — possibly "schema.table" — so it is split from the right and
        translated into how the model qualifies that source."""
        if "." in name:
            key, _sep, column = name.rpartition(".")
            ref = (refs or {}).get(key)
            return Column(name=column, source=ref.key if ref else key)
        return Column(name=name)

    def query_model(self) -> QueryModel:
        """The query as data — what the widgets currently describe.

        This is the whole of the builder's SQL knowledge: everything
        past here is `query_model.render()`, which is engine-aware,
        pure, and unit-tested without a connection (CORE-17).
        """
        base = self._base_table()
        if not base:
            return QueryModel()
        refs = self._table_refs()
        joins = tuple(
            Join(
                kind=row.kind(),
                source=refs.get(row.table(), TableRef(name=row.table())),
                on=(
                    On(
                        left=self._column_ref(row.left(), refs),
                        right=self._column_ref(row.right(), refs),
                    ),
                ),
            )
            for row in self._join_rows
            if row.table() and row.left() and row.right()
        )
        # Keep the schema's column order rather than click order.
        projections = tuple(
            Projection(column=self._column_ref(qualified, refs))
            for qualified in self._qualified_columns(self._query_tables())
            if qualified in self._checked
        )
        lines = []
        for row in self._filter_rows:
            cond = row.condition()
            if not cond.column:
                continue
            lines.append(
                (
                    cond.conjunction,
                    Condition(
                        column=self._column_ref(cond.column, refs),
                        op=cond.op,
                        value=(
                            None
                            if cond.op in NO_VALUE_OPERATORS
                            else cond.value
                        ),
                    ),
                )
            )
        return QueryModel(
            source=refs.get(base, TableRef(name=base)),
            joins=joins,
            projections=projections,
            distinct=self._distinct.get_active(),
            where=folded_group(lines),
            order_by=tuple(
                Order(
                    column=self._column_ref(row.spec().column, refs),
                    descending=row.spec().descending,
                )
                for row in self._sort_rows
                if row.spec().column
            ),
            limit=int(self._limit.get_value()),
        )

    def _join_kinds(self) -> list[str]:
        """The join kinds this engine actually has, in the builder's
        own order — so a dropdown never offers SQLite a RIGHT JOIN."""
        allowed = set(self._sql_dialect.join_kinds)
        kinds = [k for k in JOIN_KINDS if k in allowed]
        return kinds or list(JOIN_KINDS[:1])

    def _dialect(self) -> Dialect:
        """The connected engine's dialect, or the generic one until the
        catalog load has handed us the connector's quoting."""
        return self._sql_dialect

    def build_sql(self) -> str:
        """The statement as shown: values inlined, for the preview and
        for Open in Console. `run_query` runs the bound form."""
        try:
            return render_display(self.query_model(), dialect=self._dialect())
        except ConnectorError as exc:
            # A query the engine cannot express (a join kind it lacks).
            # Say so in the preview rather than throwing out of a
            # widget callback.
            return f"-- {exc}"

    def _refresh_sql(self) -> None:
        self._sql_view.get_buffer().set_text(self.build_sql())

    # Actions

    def run_query(self) -> None:
        model = self.query_model()
        dialect = self._dialect()
        try:
            query = render(model, dialect=dialect)
            sql = render_display(model, dialect=dialect)
        except ConnectorError as exc:
            self._show_error(str(exc))
            return
        if not query.sql:
            self._show_error("Pick a table first")
            return
        self._refresh_sql()
        self._status.set_text(_("Running…"))

        max_rows = result_row_cap()

        def work():
            connector = self._ensure(self.profile)
            if not query.params:
                return connector.execute(query.sql, max_rows=max_rows)
            # Filter values travel as parameters, never written into
            # the text (CORE-17).
            return connector.run_bound(
                query.sql, query.params, max_rows=max_rows
            )

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
