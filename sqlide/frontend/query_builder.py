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
name where it does not.

Columns, though, are keyed by *alias*, not by that key (CORE-20). Every
source in the query gets one — the table's own name by default,
suffixed when that is taken, and editable — so the same table can be
joined to itself and the two sides never merge: the checklist, the
filter and sort pickers and the ON dropdowns all spell a column
"alias.column". A join carries as many ON conditions as it needs, which
is what a composite foreign key prefills, and the kinds on offer are
the five the model knows intersected with the ones the dialect declares
— SQLite gained RIGHT and FULL only in 3.39, and that is a capability
flag the adapter reports rather than an engine name tested for here.
The rendered SQL drops the qualification while no join is present,
writes the schema only where there is one, and writes an alias only
where it says something the table name does not.

Beside the plain columns a query can summarise (CORE-21): an
aggregate row is a function from the dialect's own list over a column
(or `COUNT(*)`), optionally distinct and optionally aliased, and an
expression row is the escape hatch — free text, passed through
unchanged and never validated here. An aggregate beside a ticked plain
column needs a GROUP BY, so the tab derives one from the ticked
columns rather than emitting a statement PostgreSQL rejects, says in a
note what it grouped by, and lets you untick that. HAVING reuses the
filter row over the summary entries, and the sort picker offers them
too, so an aggregate result stays sortable — by its alias where the
dialect takes one, by the expression where it does not.

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
from sqlide.backend.db.relations import constraints as key_constraints
from sqlide.backend.db.query_model import (
    AGGREGATES,
    AGGREGATES_WITHOUT_COLUMN,
    Column,
    Condition,
    Dialect,
    GENERIC,
    JOINS_WITHOUT_ON,
    JOIN_KINDS as MODEL_JOIN_KINDS,
    Join,
    ON_OPERATORS,
    On,
    Order,
    Projection,
    QueryModel,
    TableRef,
    aggregate_label,
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
# before 3.39 has no RIGHT or FULL JOIN, MySQL 5.7 no FULL); the
# dialect says which, and the dropdown is filled from the intersection
# once the catalog loads — never from an `if engine == ...` here.
JOIN_KINDS = MODEL_JOIN_KINDS
DEFAULT_LIMIT = 500
#: What an aggregate row offers instead of a column when the function
#: takes none: COUNT(*).
ALL_ROWS = "*"


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


@dataclass(frozen=True)
class _Instance:
    """One appearance of a source in the query.

    A join is not "a table" but *a table under an alias*: joining
    `employees` to itself puts the same key in the query twice, and
    only the alias tells the two apart. Everything downstream — the
    column checklist, the filter and sort pickers, the ON dropdowns —
    therefore keys columns by `alias`, never by `key` (CORE-20).

    `row` is the join line this instance came from, or None for the
    base table; it is the stable identity an alias is remembered
    against across a rebuild.
    """

    alias: str
    key: str
    row: object | None = None


def _clean_alias(text: str) -> str:
    """An alias as typed, made safe to key columns by.

    A dot would make "a.b.c" ambiguous — the tab splits a qualified
    column at the last one — so it becomes an underscore rather than a
    silent mis-parse. Quoting is the renderer's job, not ours.
    """
    return text.strip().replace(".", "_")


def _unique_alias(candidate: str, taken: set[str]) -> str:
    """`candidate`, suffixed until it names only itself.

    Two sources may honestly want the same alias — the same table
    twice, two tables of a name in different schemas, or a user who
    typed one that is already spoken for. The query still has to be
    unambiguous, so the later one shifts.
    """
    alias = candidate or "t"
    index = 2
    while alias in taken:
        alias = f"{candidate}_{index}"
        index += 1
    taken.add(alias)
    return alias


class _OnRow(Gtk.Box):
    """One condition of a join's ON: [left column] [op] [right column].

    A join carries a list of these rather than a single equality, so a
    composite foreign key is one join with one condition per key column
    instead of a query that cannot be expressed.
    """

    def __init__(
        self,
        on_remove: Callable[["_OnRow"], None],
        on_change: Callable[[], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._on_change = on_change
        self._updating = False
        self._left = Gtk.DropDown(model=Gtk.StringList.new([]), hexpand=True)
        self._op = Gtk.DropDown(model=Gtk.StringList.new(list(ON_OPERATORS)))
        self._right = Gtk.DropDown(model=Gtk.StringList.new([]), hexpand=True)
        self._remove = Gtk.Button(icon_name="list-remove-symbolic")
        self._remove.add_css_class("flat")
        describe(self._remove, _("Remove condition"))
        self._remove.connect("clicked", lambda *_: on_remove(self))
        for widget in (self._left, self._op, self._right, self._remove):
            self.append(widget)
        for dropdown in (self._left, self._op, self._right):
            dropdown.connect("notify::selected", self._changed)

    def _changed(self, dropdown, *_args) -> None:
        if self._updating:
            return
        if dropdown is not self._op:
            self.touched = True
        self._on_change()

    #: Whether the user picked these columns themselves. A refilled
    #: DropDown auto-selects its first row, so "empty" cannot mean "not
    #: chosen yet"; this can.
    touched = False

    def set_removable(self, removable: bool) -> None:
        """The only condition of a join cannot be removed — a join
        without an ON is not a join."""
        self._remove.set_sensitive(removable)

    def left(self) -> str:
        return _selected_string(self._left)

    def op(self) -> str:
        return _selected_string(self._op) or "="

    def right(self) -> str:
        return _selected_string(self._right)

    def set_choices(
        self,
        left: list[str],
        right: list[str],
        rename: dict[str, str] | None = None,
    ) -> None:
        """Refill both dropdowns, keeping the current picks when they
        survive the refill. `rename` carries a pick through an alias
        the user has just renamed (guarded so this never loops)."""
        self._updating = True
        try:
            for dropdown, names in ((self._left, left), (self._right, right)):
                selected = _selected_string(dropdown)
                selected = (rename or {}).get(selected, selected)
                dropdown.set_model(Gtk.StringList.new(names))
                if selected in names:
                    dropdown.set_selected(names.index(selected))
        finally:
            self._updating = False

    def set_condition(self, left: str, right: str, op: str = "=") -> None:
        """Put a prefilled or restored condition in without firing the
        change callback."""
        self._updating = True
        try:
            for dropdown, wanted in (
                (self._left, left),
                (self._op, op),
                (self._right, right),
            ):
                model = dropdown.get_model()
                for i in range(model.get_n_items()):
                    if model.get_string(i) == wanted:
                        dropdown.set_selected(i)
                        break
        finally:
            self._updating = False


class _JoinRow(Gtk.Box):
    """One join: kind, source, alias, and one or more ON conditions.

    The alias is what makes a self-join expressible — the same source
    twice, under two names — and is editable, defaulting to the table's
    own name (suffixed when that is taken). The builder feeds the
    column choices (set_choices) as the sources before this line change.
    """

    def __init__(
        self,
        sources: Sequence[_Source],
        on_remove: Callable[["_JoinRow"], None],
        on_change: Callable[[], None],
        kinds: Sequence[str] = JOIN_KINDS,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._on_change = on_change
        # The dropdown shows labels ("crm.orders · view"); everything
        # else works in keys, so the two never get mixed up.
        self._keys = [source.key for source in sources]
        self._updating = False
        self._on_rows: list[_OnRow] = []

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._kind = Gtk.DropDown(model=Gtk.StringList.new(list(kinds)))
        self._table = Gtk.DropDown(
            model=Gtk.StringList.new([source.label for source in sources])
        )
        self._alias = Gtk.Entry(width_chars=10, max_width_chars=14)
        describe(self._alias, _("Alias for the joined table"))
        self._alias.connect("changed", self._alias_changed)
        header.append(self._kind)
        header.append(self._table)
        header.append(Gtk.Label(label=_("as")))
        header.append(self._alias)
        header.append(Gtk.Box(hexpand=True))
        self._add_condition = Gtk.Button(icon_name="list-add-symbolic")
        self._add_condition.add_css_class("flat")
        describe(self._add_condition, _("Add ON condition"))
        self._add_condition.connect(
            "clicked", lambda *_: self._add_on_row(notify=True)
        )
        header.append(self._add_condition)
        remove = Gtk.Button(icon_name="list-remove-symbolic")
        remove.add_css_class("flat")
        describe(remove, _("Remove join"))
        remove.connect("clicked", lambda *_: on_remove(self))
        header.append(remove)
        self.append(header)

        self._on_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=4,
            margin_start=24,
        )
        self.append(self._on_box)
        self._add_on_row()
        for dropdown in (self._kind, self._table):
            dropdown.connect("notify::selected", self._changed)
        self._sync_on_visibility()

    # Conditions

    def _add_on_row(self, *, notify: bool = False) -> _OnRow:
        row = _OnRow(self._remove_on_row, self._on_change)
        self._on_rows.append(row)
        self._on_box.append(row)
        self._update_removable()
        if notify and not self._updating:
            # A condition the user added themselves is theirs: the
            # foreign-key prefill must not overwrite the line now.
            row.touched = True
            self._on_change()
        return row

    def _remove_on_row(self, row: _OnRow) -> None:
        if len(self._on_rows) <= 1:
            return
        self._on_rows.remove(row)
        self._on_box.remove(row)
        self._update_removable()
        self._on_change()

    def _update_removable(self) -> None:
        for row in self._on_rows:
            row.set_removable(len(self._on_rows) > 1)

    def set_condition_count(self, count: int) -> None:
        """Grow or shrink the ON list to `count` rows, quietly."""
        while len(self._on_rows) < max(1, count):
            self._add_on_row()
        while len(self._on_rows) > max(1, count):
            row = self._on_rows.pop()
            self._on_box.remove(row)
        self._update_removable()

    def _sync_on_visibility(self) -> None:
        """A CROSS JOIN takes no ON clause, so it shows none."""
        wanted = self.kind() not in JOINS_WITHOUT_ON
        self._on_box.set_visible(wanted)
        self._add_condition.set_visible(wanted)

    def _changed(self, dropdown, *_args) -> None:
        if self._updating:
            return
        if dropdown is self._table:
            for row in self._on_rows:
                row.touched = False
        self._sync_on_visibility()
        self._on_change()

    def _alias_changed(self, *_args) -> None:
        if self._updating:
            return
        self._on_change()

    # Reading the line

    def on_touched(self) -> bool:
        return any(row.touched for row in self._on_rows)

    def kind(self) -> str:
        return _selected_string(self._kind)

    def table(self) -> str:
        """The joined source's key, not its label."""
        return _selected_key(self._table, self._keys)

    def alias(self) -> str:
        return _clean_alias(self._alias.get_text())

    def conditions(self) -> list[tuple[str, str, str]]:
        """The complete ON conditions, as (left, op, right)."""
        if self.kind() in JOINS_WITHOUT_ON:
            return []
        return [
            (row.left(), row.op(), row.right())
            for row in self._on_rows
            if row.left() and row.right()
        ]

    # Writing to the line

    def set_default_alias(self, alias: str) -> None:
        """Show the alias the builder derived.

        As the placeholder, never as the text: writing into an entry
        the user is typing in re-enters this callback halfway through
        GTK's own delete-then-insert, and an empty entry is exactly
        what "I have not named this one" means.
        """
        self._alias.set_placeholder_text(alias)

    def set_choices(
        self,
        left: list[str],
        right: list[str],
        rename: dict[str, str] | None = None,
    ) -> None:
        for row in self._on_rows:
            row.set_choices(left, right, rename)

    def restore_kind_and_table(
        self, kind: str, key: str, alias: str = ""
    ) -> None:
        """Put a saved line's join kind, source and alias back, without
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
            if alias:
                self._alias.set_text(alias)
        finally:
            self._updating = False
        self._sync_on_visibility()

    def restore_on(self, conditions: Sequence[tuple[str, str, str]]) -> None:
        """The saved ON conditions, once the choices have been filled.

        Marks the line as touched, so the foreign-key prefill never
        overwrites what the user actually built.
        """
        self.prefill(conditions)
        for row in self._on_rows:
            row.touched = True

    def prefill(self, conditions: Sequence[tuple[str, str, str]]) -> None:
        """Fill the ON list from `conditions` — one row each, so a
        composite key arrives whole rather than as its first column."""
        if not conditions:
            return
        self._updating = True
        try:
            self.set_condition_count(len(conditions))
        finally:
            self._updating = False
        for row, (left, op, right) in zip(self._on_rows, conditions):
            row.set_condition(left, right, op)


class _AggregateRow(Gtk.Box):
    """One aggregate projection: [function] [column] [distinct] as [alias].

    The functions on offer come from the dialect, not from a test on
    the engine's name (CORE-21) — the same route the join kinds take.
    `COUNT` may take `*` instead of a column; the others may not, and
    the column picker says so by dropping the entry.
    """

    def __init__(
        self,
        columns: Sequence[str],
        on_remove: Callable[["_AggregateRow"], None],
        on_change: Callable[[], None],
        functions: Sequence[str] = AGGREGATES,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._on_change = on_change
        self._updating = False
        self._functions = list(functions) or list(AGGREGATES)
        self._function = Gtk.DropDown(
            model=Gtk.StringList.new(self._functions)
        )
        self._column = Gtk.DropDown(model=Gtk.StringList.new([]), hexpand=True)
        self._distinct = Gtk.CheckButton(label=_("Distinct"))
        self._alias = Gtk.Entry(width_chars=10, max_width_chars=16)
        describe(self._alias, _("Name for the aggregate column"))
        remove = Gtk.Button(icon_name="list-remove-symbolic")
        remove.add_css_class("flat")
        describe(remove, _("Remove aggregate"))
        remove.connect("clicked", lambda *_: on_remove(self))
        for widget in (
            self._function,
            self._column,
            self._distinct,
            Gtk.Label(label=_("as")),
            self._alias,
            remove,
        ):
            self.append(widget)
        self.set_columns(columns)
        for dropdown in (self._function, self._column):
            dropdown.connect("notify::selected", self._changed)
        self._distinct.connect("toggled", self._changed)
        self._alias.connect("changed", self._changed)

    def _changed(self, *_args) -> None:
        if self._updating:
            return
        self._sync_column_choices()
        self._on_change()

    def set_columns(self, columns: Sequence[str]) -> None:
        """Refill the column picker, keeping the current pick."""
        self._all_columns = list(columns)
        self._sync_column_choices()

    def _sync_column_choices(self) -> None:
        wanted = list(self._all_columns)
        if self.function() in AGGREGATES_WITHOUT_COLUMN:
            wanted = [ALL_ROWS, *wanted]
        selected = _selected_string(self._column)
        if wanted == [
            self._column.get_model().get_string(i)
            for i in range(self._column.get_model().get_n_items())
        ]:
            return
        self._updating = True
        try:
            self._column.set_model(Gtk.StringList.new(wanted))
            if selected in wanted:
                self._column.set_selected(wanted.index(selected))
        finally:
            self._updating = False

    # Reading the line

    def function(self) -> str:
        return _selected_string(self._function) or self._functions[0]

    def column(self) -> str:
        name = _selected_string(self._column)
        return "" if name == ALL_ROWS else name

    def distinct(self) -> bool:
        return self._distinct.get_active()

    def alias(self) -> str:
        return self._alias.get_text().strip()

    def label(self) -> str:
        """How this aggregate reads elsewhere in the tab — in the
        HAVING picker and the sort picker. The alias where there is
        one, since that is what the result column will be called."""
        return self.alias() or aggregate_label(
            self.function(), self.column(), distinct=self.distinct()
        )

    def is_valid(self) -> bool:
        """A COUNT is complete on its own; the rest need a column."""
        return bool(
            self.column() or self.function() in AGGREGATES_WITHOUT_COLUMN
        )

    def restore(
        self, function: str, column: str, distinct: bool, alias: str
    ) -> None:
        self._updating = True
        try:
            if function in self._functions:
                self._function.set_selected(self._functions.index(function))
            self._distinct.set_active(distinct)
            self._alias.set_text(alias)
        finally:
            self._updating = False
        self._sync_column_choices()
        self._updating = True
        try:
            wanted = column or ALL_ROWS
            model = self._column.get_model()
            for i in range(model.get_n_items()):
                if model.get_string(i) == wanted:
                    self._column.set_selected(i)
                    break
        finally:
            self._updating = False


class _ExpressionRow(Gtk.Box):
    """A free-text computed column: the escape hatch.

    What is typed here is passed to the engine unchanged — the builder
    neither parses nor validates it — so the row says so, and an
    expression is never anywhere near a value that should have been
    bound.
    """

    def __init__(
        self,
        on_remove: Callable[["_ExpressionRow"], None],
        on_change: Callable[[], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._expression = Gtk.Entry(
            hexpand=True, placeholder_text=_("SQL expression")
        )
        self._expression.set_tooltip_text(
            _("Passed to the database unchanged and not checked here")
        )
        describe(self._expression, _("SQL expression (not validated)"))
        self._alias = Gtk.Entry(width_chars=10, max_width_chars=16)
        describe(self._alias, _("Name for the computed column"))
        remove = Gtk.Button(icon_name="list-remove-symbolic")
        remove.add_css_class("flat")
        describe(remove, _("Remove expression"))
        remove.connect("clicked", lambda *_: on_remove(self))
        for widget in (
            self._expression,
            Gtk.Label(label=_("as")),
            self._alias,
            remove,
        ):
            self.append(widget)
        for entry in (self._expression, self._alias):
            entry.connect("changed", lambda *_: on_change())

    def expression(self) -> str:
        return self._expression.get_text().strip()

    def alias(self) -> str:
        return self._alias.get_text().strip()

    def label(self) -> str:
        return self.alias() or self.expression()

    def is_valid(self) -> bool:
        return bool(self.expression())

    def restore(self, expression: str, alias: str) -> None:
        self._expression.set_text(expression)
        self._alias.set_text(alias)


class _GroupRow(Gtk.Box):
    """One GROUP BY column, chosen by hand."""

    def __init__(
        self,
        columns: Sequence[str],
        on_remove: Callable[["_GroupRow"], None],
        on_change: Callable[[], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._column = Gtk.DropDown(
            model=Gtk.StringList.new(list(columns)), hexpand=True
        )
        self._column.connect("notify::selected", lambda *_: on_change())
        remove = Gtk.Button(icon_name="list-remove-symbolic")
        remove.add_css_class("flat")
        describe(remove, _("Remove grouping column"))
        remove.connect("clicked", lambda *_: on_remove(self))
        self.append(self._column)
        self.append(remove)

    def column(self) -> str:
        return _selected_string(self._column)

    def set_columns(self, names: Sequence[str]) -> None:
        selected = self.column()
        names = list(names)
        self._column.set_model(Gtk.StringList.new(names))
        if selected in names:
            self._column.set_selected(names.index(selected))

    def set_column(self, name: str) -> None:
        model = self._column.get_model()
        for i in range(model.get_n_items()):
            if model.get_string(i) == name:
                self._column.set_selected(i)
                return


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
        self._aggregate_rows: list[_AggregateRow] = []
        self._expression_rows: list[_ExpressionRow] = []
        self._group_rows: list[_GroupRow] = []
        self._having_rows: list[_FilterRow] = []
        self._sort_rows: list[_SortRow] = []
        self._checked: set[str] = set()  # qualified "alias.column"
        self._column_checks: list[Gtk.CheckButton] = []
        self._loading = True
        # The alias each instance last went by, keyed by the join row it
        # belongs to (None for the base). Renaming an alias has to carry
        # every column that named it along, and this is what says which
        # name it used to have.
        self._alias_of: dict[object, str] = {}

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
        top.append(Gtk.Label(label=_("as")))
        self._base_alias = Gtk.Entry(width_chars=10, max_width_chars=14)
        describe(self._base_alias, _("Alias for the base table"))
        self._base_alias.connect("changed", self._base_alias_changed)
        top.append(self._base_alias)
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

        box.append(self._section_label("Summarise"))
        self._aggregates_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=6
        )
        box.append(self._aggregates_box)
        summarise = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        add_aggregate = Gtk.Button(label=_("Add aggregate"))
        add_aggregate.connect("clicked", lambda *_: self._add_aggregate_row())
        summarise.append(add_aggregate)
        add_expression = Gtk.Button(label=_("Add expression"))
        add_expression.set_tooltip_text(
            _("A computed column, passed to the database unchanged")
        )
        add_expression.connect(
            "clicked", lambda *_: self._add_expression_row()
        )
        summarise.append(add_expression)
        summarise.set_halign(Gtk.Align.START)
        box.append(summarise)

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

        box.append(self._section_label("Group by"))
        self._auto_group = Gtk.CheckButton(
            label=_("Group by the selected plain columns"), active=True
        )
        self._auto_group.set_tooltip_text(
            _(
                "An aggregate beside a plain column needs a GROUP BY; "
                "this adds one for every column you have ticked."
            )
        )
        self._auto_group.connect("toggled", lambda *_: self._sync_state())
        box.append(self._auto_group)
        self._groups_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=6
        )
        box.append(self._groups_box)
        add_group = Gtk.Button(
            label=_("Add grouping column"), halign=Gtk.Align.START
        )
        add_group.connect("clicked", lambda *_: self._add_group_row())
        box.append(add_group)
        self._group_note = Gtk.Label(xalign=0, wrap=True)
        self._group_note.add_css_class("dim-label")
        box.append(self._group_note)

        box.append(self._section_label("Having"))
        self._having_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=6
        )
        box.append(self._having_box)
        add_having = Gtk.Button(
            label=_("Add condition on an aggregate"), halign=Gtk.Align.START
        )
        add_having.connect("clicked", lambda *_: self._add_having_row())
        box.append(add_having)

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
        self, column: Column | None, aliases: dict[str, str], base: str
    ) -> str | None:
        """A saved model column as the qualified "alias.column" the tab
        works in, or None when its source or column is gone.

        The model qualifies a column by its source's key, which is the
        alias the tab gave it (CORE-20) — and a restored tab puts those
        same aliases back, so the saved string needs no translation. A
        column saved with no source at all belongs to the base.
        """
        if column is None or not column.name:
            return None
        alias = column.source or base
        key = aliases.get(alias, "")
        if not key:
            return None
        if column.name not in {c.name for c in self._columns.get(key, [])}:
            return None
        return f"{alias}.{column.name}"

    def _restore_model(self) -> None:
        """Rebuild the widgets from the query this tab was restored
        with, dropping whatever the database no longer has.

        A saved query is data about a schema that has moved on since:
        a table may have been dropped, a column renamed. Every part is
        therefore checked against the freshly loaded catalog and
        silently left out when it no longer resolves — the count is
        reported in the status label. Losing a filter must never cost
        you the rest of the query, and must never be an error dialog.

        The saved aliases are restored as the user's own, so a
        self-joined query comes back with its two sides still apart and
        every saved column still pointing at the side it named.
        """
        model, self._restore = self._restore, None
        if model is None or model.source is None:
            return
        # Saved alias -> the picker key it still resolves to.
        aliases = {
            ref.key: key
            for ref in model.sources
            if (key := self._picker_key(ref))
        }
        base_alias = model.source.key
        base = aliases.get(base_alias, "")
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
            self._base_alias.set_text(base_alias)
            self._distinct.set_active(model.distinct)
            if model.limit:
                self._limit.set_value(model.limit)
            kinds = self._join_kinds()
            restored: list[tuple[_JoinRow, list[tuple[str, str, str]]]] = []
            for join in model.joins:
                alias = join.source.key
                key = aliases.get(alias, "")
                conditions = []
                for on in join.on:
                    left = self._restored_column(on.left, aliases, base_alias)
                    right = self._restored_column(
                        on.right, aliases, base_alias
                    )
                    if left is None or right is None:
                        continue
                    conditions.append((left, on.op, right))
                needs_on = join.kind.upper() not in JOINS_WITHOUT_ON
                if not key or (needs_on and len(conditions) != len(join.on)):
                    aliases.pop(alias, None)
                    dropped += 1
                    continue
                row = _JoinRow(
                    self._sources,
                    self._remove_join_row,
                    self._sync_state,
                    kinds=kinds if join.kind in kinds else [join.kind, *kinds],
                )
                row.restore_kind_and_table(join.kind, key, alias)
                self._join_rows.append(row)
                self._joins_box.append(row)
                restored.append((row, conditions))
            instances = self._instances()
            qualified = set(self._qualified_columns(instances))
            checked: set[str] = set()
            for projection in model.projections:
                if projection.expression:
                    row = self._add_expression_row()
                    row.restore(projection.expression, projection.alias)
                    continue
                name = self._restored_column(
                    projection.column, aliases, base_alias
                )
                if projection.function:
                    # COUNT(*) has no column to lose; the rest go with
                    # the column they aggregate.
                    display = self._display_name(name, instances)
                    if projection.column is not None and (
                        display is None
                        or display
                        not in self._display_columns(instances)
                    ):
                        dropped += 1
                        continue
                    aggregate = self._add_aggregate_row()
                    aggregate.restore(
                        projection.function.upper(),
                        display or "",
                        projection.distinct,
                        projection.alias,
                    )
                    continue
                if name is None or name not in qualified:
                    dropped += 1
                    continue
                checked.add(name)
            self._checked = checked
            names = self._display_columns(instances)
            for item in model.group_by:
                if isinstance(item, str):
                    dropped += 1
                    continue
                name = self._restored_column(item, aliases, base_alias)
                display = self._display_name(name, instances)
                if display is None or display not in names:
                    dropped += 1
                    continue
                # A restored query groups exactly as it was saved:
                # every column is a row of its own and the derived
                # grouping stands aside rather than adding to it.
                self._auto_group.set_active(False)
                self._add_group_row().set_column(display)
            lines = unfold_group(model.where)
            if lines is None and model.where is not None:
                # A filter tree the flat panel cannot show (nothing
                # builds one yet — CORE-22 will).
                dropped += 1
            for conjunction, condition in lines or []:
                name = self._restored_column(
                    condition.column, aliases, base_alias
                )
                display = self._display_name(name, instances)
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
            labels = self._aggregate_labels()
            having = unfold_group(model.having)
            if having is None and model.having is not None:
                dropped += 1
            for conjunction, condition in having or []:
                label = self._label_for(condition, aliases, base_alias)
                if label is None or label not in labels:
                    dropped += 1
                    continue
                self._add_having_row().set_condition(
                    FilterCondition(
                        column=label,
                        op=condition.op,
                        value=(
                            ""
                            if condition.value is None
                            else str(condition.value)
                        ),
                        conjunction=conjunction,
                    )
                )
            choices = [*names, *labels]
            for order in model.order_by:
                if order.alias or order.function or order.expression:
                    display = self._label_for(order, aliases, base_alias)
                else:
                    name = self._restored_column(
                        order.column, aliases, base_alias
                    )
                    display = self._display_name(name, instances)
                if display is None or display not in choices:
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
        for row, conditions in restored:
            row.restore_on(conditions)
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

    def _label_for(
        self, item, aliases: dict[str, str], base: str
    ) -> str | None:
        """The label a saved aggregate or expression term goes by in
        the restored tab, so a HAVING condition or a sort finds the row
        it belongs to.

        Matched against the summary rows themselves rather than
        rebuilt, because a row that was given an alias goes by that
        alias everywhere. None when nothing in the tab answers to the
        term — which is how a condition on a dropped aggregate gets
        left out instead of naming a column that is not there.
        """
        alias = getattr(item, "alias", "")
        if alias:
            return alias if alias in self._aggregate_labels() else None
        if item.expression:
            for row in self._summary_rows():
                if (
                    isinstance(row, _ExpressionRow)
                    and row.expression() == item.expression
                ):
                    return row.label()
            return None
        if not item.function:
            return None
        column = ""
        if item.column is not None and item.column.name:
            name = self._restored_column(item.column, aliases, base)
            column = self._display_name(name, self._instances()) or ""
            if not column:
                return None
        for row in self._summary_rows():
            if (
                isinstance(row, _AggregateRow)
                and row.function() == item.function.upper()
                and row.column() == column
                and row.distinct() == item.distinct
            ):
                return row.label()
        return None

    @staticmethod
    def _display_name(
        qualified: str | None, instances: list[_Instance]
    ) -> str | None:
        """A qualified "alias.column" as the filter/sort panels spell
        it: qualified once a join is present, bare otherwise."""
        if qualified is None:
            return None
        return (
            qualified
            if len(instances) > 1
            else qualified.rpartition(".")[2]
        )

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

    def _default_alias(self, key: str) -> str:
        """What a source is called when nobody has said otherwise: its
        bare table name, so a single-table query reads exactly as it
        always did and `crm.orders` becomes `orders`."""
        source = self._source(key)
        return _clean_alias(source.name if source else key)

    def _instances(self) -> list[_Instance]:
        """Base source plus joined sources, in query order, each under
        a unique alias.

        This is the query's real shape: the same table joined to itself
        is two instances of one key, and every column, filter and sort
        in the tab is qualified by the alias rather than by the table,
        which is what keeps the two sides apart (CORE-20).
        """
        instances: list[_Instance] = []
        taken: set[str] = set()
        base = self._base_table()
        if base:
            typed = _clean_alias(self._base_alias.get_text())
            instances.append(
                _Instance(
                    _unique_alias(typed or self._default_alias(base), taken),
                    base,
                )
            )
        for row in self._join_rows:
            key = row.table()
            if not key:
                continue
            instances.append(
                _Instance(
                    _unique_alias(row.alias() or self._default_alias(key), taken),
                    key,
                    row,
                )
            )
        return instances

    def _renames(self, instances: list[_Instance]) -> dict[str, str]:
        """`{old alias: new alias}` for the instances that just changed
        name, and the new register of who is called what.

        An alias the user edits must not quietly drop the columns that
        named it, so every qualified string the tab holds is carried
        over rather than re-derived.
        """
        previous, self._alias_of = self._alias_of, {}
        renames: dict[str, str] = {}
        for instance in instances:
            self._alias_of[instance.row] = instance.alias
            was = previous.get(instance.row)
            if was and was != instance.alias:
                renames[was] = instance.alias
        return renames

    def _renamed(self, qualified: str, renames: dict[str, str]) -> str:
        """A qualified "alias.column" under the alias's new name."""
        if not renames or "." not in qualified:
            return qualified
        alias, _sep, column = qualified.rpartition(".")
        return f"{renames.get(alias, alias)}.{column}"

    def _qualified_columns(self, instances: list[_Instance]) -> list[str]:
        return [
            f"{instance.alias}.{column.name}"
            for instance in instances
            for column in self._columns.get(instance.key, [])
        ]

    def _base_changed(self) -> None:
        if self._loading:
            return
        self._sync_state()

    def _base_alias_changed(self, *_args) -> None:
        if self._loading:
            return
        self._sync_state()

    def _sync_state(self) -> None:
        """Re-derive aliases, join choices, the column checklist and the
        filter and sort column lists from the current base + joins, then
        refresh the SQL preview."""
        instances = self._instances()
        renames = self._renames(instances)
        by_row = {instance.row: instance for instance in instances}
        if instances:
            self._base_alias.set_placeholder_text(instances[0].alias)

        seen: list[_Instance] = instances[:1]
        for row in self._join_rows:
            joined = by_row.get(row)
            if joined is not None:
                row.set_default_alias(joined.alias)
            left_choices = self._qualified_columns(seen)
            right_choices = self._qualified_columns(
                [joined] if joined is not None else []
            )
            row.set_choices(left_choices, right_choices, renames)
            if joined is not None:
                if not row.on_touched():
                    prefill = self._relation_for(seen, joined)
                    if prefill:
                        row.prefill(prefill)
                seen.append(joined)

        names = self._display_columns(instances)
        if renames:
            self._checked = {
                self._renamed(name, renames) for name in self._checked
            }
        for row in self._filter_rows:
            condition = row.condition()
            row.set_columns(names)
            if renames:
                row.set_condition(
                    FilterCondition(
                        column=self._renamed(condition.column, renames),
                        op=condition.op,
                        value=condition.value,
                        conjunction=condition.conjunction,
                    )
                )
        sort_names = [*names, *self._aggregate_labels()]
        for row in self._sort_rows:
            spec = row.spec()
            row.set_columns(sort_names)
            if renames:
                row.set_spec(
                    SortSpec(
                        column=self._renamed(spec.column, renames),
                        descending=spec.descending,
                    )
                )
        for aggregate in self._aggregate_rows:
            aggregate.set_columns(names)
        for group in self._group_rows:
            group.set_columns(names)
        labels = self._aggregate_labels()
        for having in self._having_rows:
            having.set_columns(labels)
        self._rebuild_column_checks(instances)
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
        self, seen: list[_Instance], joined: _Instance
    ) -> list[tuple[str, str, str]]:
        """Foreign key connecting the joined source to a source already
        in the query, as the ON conditions it implies.

        A composite key is one constraint over several columns, so it
        prefills as several conditions rather than silently as its
        first one — and it stays a suggestion the user can overwrite.
        Both sides are named by alias, which is what lets a table's
        self-referencing key prefill a self-join.
        """
        for group in key_constraints(self._relations):
            first = group[0]
            here = self._relation_key(first.schema, first.table)
            there = self._relation_key(first.ref_schema, first.ref_table)
            if here == joined.key:
                other = next((i for i in seen if i.key == there), None)
                if other is not None:
                    return [
                        (
                            f"{other.alias}.{rel.ref_column or ''}",
                            "=",
                            f"{joined.alias}.{rel.column}",
                        )
                        for rel in group
                    ]
            if there == joined.key:
                other = next((i for i in seen if i.key == here), None)
                if other is not None:
                    return [
                        (
                            f"{other.alias}.{rel.column}",
                            "=",
                            f"{joined.alias}.{rel.ref_column or ''}",
                        )
                        for rel in group
                    ]
        return []

    def _display_columns(self, instances: list[_Instance]) -> list[str]:
        """Column names as shown in filters/sorts: qualified by alias as
        soon as a join is present."""
        if len(instances) > 1:
            return self._qualified_columns(instances)
        return [
            c.name
            for i in instances
            for c in self._columns.get(i.key, [])
        ]

    def _rebuild_column_checks(self, instances: list[_Instance]) -> None:
        while (child := self._columns_flow.get_first_child()) is not None:
            self._columns_flow.remove(child)
        self._column_checks = []
        qualified_names = self._qualified_columns(instances)
        self._checked &= set(qualified_names)
        for qualified in qualified_names:
            label = (
                qualified
                if len(instances) > 1
                else qualified.rpartition(".")[2]
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
            self._display_columns(self._instances()),
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
            self._sort_choices(),
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

    # Aggregate / expression / group / having rows (CORE-21)

    def _aggregate_functions(self) -> list[str]:
        """The aggregates this engine has, in the builder's own order.

        Straight from the dialect the catalog load handed over — which
        is where the provider's and the adapter's knowledge ends up —
        so the dropdown never needs to know an engine's name.
        """
        allowed = set(self._sql_dialect.aggregates)
        return [f for f in AGGREGATES if f in allowed] or list(AGGREGATES)

    def _add_aggregate_row(self) -> _AggregateRow:
        row = _AggregateRow(
            self._display_columns(self._instances()),
            self._remove_aggregate_row,
            self._sync_state,
            functions=self._aggregate_functions(),
        )
        self._aggregate_rows.append(row)
        self._aggregates_box.append(row)
        self._sync_state()
        return row

    def _remove_aggregate_row(self, row: _AggregateRow) -> None:
        self._aggregate_rows.remove(row)
        self._aggregates_box.remove(row)
        self._sync_state()

    def _add_expression_row(self) -> _ExpressionRow:
        row = _ExpressionRow(self._remove_expression_row, self._sync_state)
        self._expression_rows.append(row)
        self._aggregates_box.append(row)
        self._sync_state()
        return row

    def _remove_expression_row(self, row: _ExpressionRow) -> None:
        self._expression_rows.remove(row)
        self._aggregates_box.remove(row)
        self._sync_state()

    def _add_group_row(self) -> _GroupRow:
        row = _GroupRow(
            self._display_columns(self._instances()),
            self._remove_group_row,
            self._refresh_sql,
        )
        self._group_rows.append(row)
        self._groups_box.append(row)
        self._refresh_sql()
        return row

    def _remove_group_row(self, row: _GroupRow) -> None:
        self._group_rows.remove(row)
        self._groups_box.remove(row)
        self._refresh_sql()

    def _add_having_row(self) -> _FilterRow:
        row = _FilterRow(
            self._aggregate_labels(),
            self._remove_having_row,
            self.run_query,
            on_change=self._refresh_sql,
        )
        self._having_rows.append(row)
        self._having_box.append(row)
        for index, line in enumerate(self._having_rows):
            line.set_first(index == 0)
        self._refresh_sql()
        return row

    def _remove_having_row(self, row: _FilterRow) -> None:
        self._having_rows.remove(row)
        self._having_box.remove(row)
        for index, line in enumerate(self._having_rows):
            line.set_first(index == 0)
        self._refresh_sql()

    # Aggregates as the rest of the tab sees them

    def _summary_rows(self) -> list[object]:
        """Every complete select-list entry that is not a plain
        column, aggregates first — what HAVING and the sort picker can
        name, in the order the select list writes them."""
        return [
            *(r for r in self._aggregate_rows if r.is_valid()),
            *(r for r in self._expression_rows if r.is_valid()),
        ]

    def _sort_choices(self) -> list[str]:
        """What a sort row may order by: every plain column plus every
        aggregate or computed column, so an aggregate result stays
        sortable (by its alias where the dialect allows one)."""
        return [
            *self._display_columns(self._instances()),
            *self._aggregate_labels(),
        ]

    def _aggregate_labels(self) -> list[str]:
        return [row.label() for row in self._summary_rows()]

    def _row_for_label(self, label: str) -> object | None:
        for row in self._summary_rows():
            if row.label() == label:
                return row
        return None

    def _summary_term(self, label: str) -> dict:
        """The model fields a summary entry contributes, by the label
        it goes by in the HAVING and sort pickers. `{}` when the label
        names nothing (a row the user has since removed)."""
        row = self._row_for_label(label)
        if isinstance(row, _AggregateRow):
            return {
                "column": (
                    self._column_ref(row.column()) if row.column() else None
                ),
                "function": row.function(),
                "distinct": row.distinct(),
            }
        if isinstance(row, _ExpressionRow):
            return {"expression": row.expression()}
        return {}

    def _grouping_columns(self) -> list[str]:
        """The display columns the query groups by.

        The rows the user added by hand, plus — while "Group by the
        selected plain columns" is on and something is actually being
        aggregated — the plain columns they ticked. An aggregate beside
        a bare column without a GROUP BY is a statement PostgreSQL
        rejects outright and MySQL and SQLite answer arbitrarily, so
        the builder supplies the grouping rather than emitting it
        (CORE-21).
        """
        names: list[str] = []
        for row in self._group_rows:
            if row.column() and row.column() not in names:
                names.append(row.column())
        if self._auto_group.get_active() and self._summary_rows():
            for name in self._plain_projections():
                if name not in names:
                    names.append(name)
        return names

    def _plain_projections(self) -> list[str]:
        """The ticked plain columns, as the filter panel spells them
        (qualified once a join is in play), in schema order."""
        instances = self._instances()
        return [
            display
            for qualified in self._qualified_columns(instances)
            if qualified in self._checked
            and (display := self._display_name(qualified, instances))
        ]

    # SQL generation

    def _table_refs(
        self, instances: list[_Instance]
    ) -> dict[str, TableRef]:
        """Every instance in the query as a model TableRef, by alias.

        The schema rides along only where the engine has schemas, so
        the rendered SQL says `crm.orders` on PostgreSQL and plain
        `orders` on SQLite and MySQL. The alias is written into the
        statement only when it says something the table name does not —
        a self-join's second side, a renamed source — so a plain
        one-table query still reads `FROM "orders"`. Either way
        `TableRef.key` is the tab's alias, which is what the columns
        are qualified by.
        """
        refs: dict[str, TableRef] = {}
        for instance in instances:
            source = self._source(instance.key)
            name = source.name if source else instance.key
            schema = source.schema if source and self._caps.schemas else ""
            refs[instance.alias] = TableRef(
                name=name,
                schema=schema,
                alias="" if instance.alias == name else instance.alias,
            )
        return refs

    @staticmethod
    def _column_ref(name: str) -> Column:
        """A display column name ("alias.c" once a join is present, "c"
        otherwise) as a model reference.

        The alias is the model's own way of naming a source
        (`TableRef.key`), so nothing has to be translated: the two
        halves of a self-join stay two names here exactly as they are
        in the widgets.
        """
        if "." in name:
            alias, _sep, column = name.rpartition(".")
            return Column(name=column, source=alias)
        return Column(name=name)

    def query_model(self) -> QueryModel:
        """The query as data — what the widgets currently describe.

        This is the whole of the builder's SQL knowledge: everything
        past here is `query_model.render()`, which is engine-aware,
        pure, and unit-tested without a connection (CORE-17).
        """
        instances = self._instances()
        if not instances:
            return QueryModel()
        refs = self._table_refs(instances)
        joins = []
        for instance in instances[1:]:
            row = instance.row
            conditions = row.conditions()
            if row.kind() not in JOINS_WITHOUT_ON and not conditions:
                # A half-built join line is not yet part of the query.
                continue
            joins.append(
                Join(
                    kind=row.kind(),
                    source=refs[instance.alias],
                    on=tuple(
                        On(
                            left=self._column_ref(left),
                            right=self._column_ref(right),
                            op=op,
                        )
                        for left, op, right in conditions
                    ),
                )
            )
        # Keep the schema's column order rather than click order, and
        # put the summary entries after the plain ones, the way the
        # select list reads.
        projections = [
            Projection(column=self._column_ref(qualified))
            for qualified in self._qualified_columns(instances)
            if qualified in self._checked
        ]
        for row in self._aggregate_rows:
            if not row.is_valid():
                continue
            projections.append(
                Projection(
                    column=(
                        self._column_ref(row.column())
                        if row.column()
                        else None
                    ),
                    function=row.function(),
                    distinct=row.distinct(),
                    alias=row.alias(),
                )
            )
        for row in self._expression_rows:
            if not row.is_valid():
                continue
            projections.append(
                Projection(expression=row.expression(), alias=row.alias())
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
                        column=self._column_ref(cond.column),
                        op=cond.op,
                        value=(
                            None
                            if cond.op in NO_VALUE_OPERATORS
                            else cond.value
                        ),
                    ),
                )
            )
        having_lines = []
        for row in self._having_rows:
            cond = row.condition()
            term = self._summary_term(cond.column) if cond.column else {}
            if not term:
                # A condition on an aggregate that is no longer in the
                # select list is not part of the query.
                continue
            having_lines.append(
                (
                    cond.conjunction,
                    Condition(
                        op=cond.op,
                        value=(
                            None
                            if cond.op in NO_VALUE_OPERATORS
                            else cond.value
                        ),
                        **term,
                    ),
                )
            )
        return QueryModel(
            source=refs[instances[0].alias],
            joins=tuple(joins),
            projections=tuple(projections),
            distinct=self._distinct.get_active(),
            where=folded_group(lines),
            group_by=tuple(
                self._column_ref(name) for name in self._grouping_columns()
            ),
            having=folded_group(having_lines),
            order_by=tuple(self._orderings()),
            limit=int(self._limit.get_value()),
        )

    def _orderings(self) -> list[Order]:
        """The sort rows as model orderings.

        A row naming an aggregate orders by its alias where it has one
        — the renderer falls back to repeating the expression on a
        dialect that cannot sort by an alias — and by the aggregate
        itself where the user never named it.
        """
        orders: list[Order] = []
        for row in self._sort_rows:
            spec = row.spec()
            if not spec.column:
                continue
            summary = self._row_for_label(spec.column)
            if summary is None:
                orders.append(
                    Order(
                        column=self._column_ref(spec.column),
                        descending=spec.descending,
                    )
                )
            elif getattr(summary, "alias", lambda: "")():
                orders.append(
                    Order(alias=summary.alias(), descending=spec.descending)
                )
            else:
                orders.append(
                    Order(
                        descending=spec.descending,
                        **self._summary_term(spec.column),
                    )
                )
        return orders

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
        self._update_group_note()
        self._sql_view.get_buffer().set_text(self.build_sql())

    def _update_group_note(self) -> None:
        """Say what the derived grouping did, if anything.

        Applying a GROUP BY silently would be a statement the user did
        not write; the note is what makes it visible, and unticking the
        box is what takes it back.
        """
        derived = [
            name
            for name in self._plain_projections()
            if not any(row.column() == name for row in self._group_rows)
        ]
        if self._summary_rows() and self._auto_group.get_active() and derived:
            self._group_note.set_text(
                _("Grouping by %s so the aggregates are valid.")
                % ", ".join(derived)
            )
        elif self._summary_rows() and not self._grouping_columns():
            self._group_note.set_text(
                _("No grouping: the aggregates cover every row.")
            )
        else:
            self._group_note.set_text("")

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
