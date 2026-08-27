"""Generic connector interface shared by all database adapters."""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from secrets import token_hex
from typing import Any

from sqlide.backend import sql_risk
from sqlide.backend.db.extensions import ExtensionState


@dataclass(frozen=True)
class TableInfo:
    name: str
    kind: str  # "table" | "view"
    #: A one-line note for the row, where the relation is a special
    #: case of its kind: a partitioned table, a foreign table, a
    #: materialized view. Empty for a plain table or view, which is
    #: what the sidebar shows nothing extra for.
    detail: str = ""


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    type: str
    is_pk: bool = False
    nullable: bool = True


@dataclass(frozen=True)
class FunctionInfo:
    name: str
    #: A one-line note for the row, where the routine is a special
    #: case: a built-in an engine ships rather than one somebody
    #: created (SQLite has only those). Empty for a stored routine,
    #: which is what the sidebar shows nothing extra for.
    detail: str = ""


@dataclass(frozen=True)
class IndexInfo:
    name: str
    table: str = ""  # owning table (MySQL's DROP INDEX … ON table)
    ddl: str = ""  # CREATE INDEX text, where the dialect can give one


@dataclass(frozen=True)
class TriggerInfo:
    name: str
    table: str = ""  # owning table (Postgres' DROP TRIGGER … ON table)
    ddl: str = ""  # CREATE TRIGGER text, where the dialect can give one


@dataclass(frozen=True)
class UserInfo:
    """One account on the server the connection reaches.

    `name` is the login name; `host` is the half of a MySQL account
    that is not the name ('app'@'10.0.%'), empty for dialects where an
    account is just a name. `detail` is a one-line summary for the
    sidebar row (superuser, locked, "no login"), and `can_login`
    separates real accounts from group roles.

    The rest are the attributes the overview table shows a column per
    (CORE-12). Every one of them is optional: an adapter fills what its
    catalog records and leaves the rest at the default, and the
    metadata provider decides which of them its engine has a column
    for — no screen ever reads a field the engine cannot fill.

    `kind` is "user", "role" or "group"; `member_of` are the roles this
    account inherits from; `valid_until` and `password_expiry` are
    already-formatted dates; `connection_limit` is empty where the
    engine sets none, and negative numbers mean unlimited.
    """

    name: str
    host: str = ""
    detail: str = ""
    can_login: bool = True
    kind: str = "user"
    superuser: bool = False
    create_db: bool = False
    create_role: bool = False
    member_of: tuple[str, ...] = ()
    valid_until: str = ""
    connection_limit: str = ""
    plugin: str = ""  # MySQL's authentication plugin
    locked: bool = False
    password_expiry: str = ""


@dataclass(frozen=True)
class PrivilegeInfo:
    """One thing an account is allowed to do, as the catalog reports it.

    `scope` is where the privilege applies, already human-readable
    ("server", "database sales", "table sales.orders", "role member of
    analysts"); `privilege` is the right itself (SELECT, CREATEDB);
    `grantable` says the account may pass it on (WITH GRANT OPTION);
    `grantor` is the account that handed it over where the catalog
    records one, empty where it does not.
    """

    scope: str
    privilege: str
    grantable: bool = False
    grantor: str = ""


@dataclass(frozen=True)
class GrantScope:
    """One entry of the grant dialog's "on what" list.

    `label` is what the user picks ("Database: sales"); `target` is the
    dialect's own text for it, dropped in after ON (MySQL's
    `sales`.*, PostgreSQL's DATABASE "sales"). Adapters build these
    from the catalog so the list only offers what exists.
    """

    label: str
    target: str


@dataclass(frozen=True)
class TypeSpec:
    """One entry of the table designer's type list.

    `params` names the arguments the type takes — ("length",) for
    VARCHAR, ("precision", "scale") for DECIMAL, ("values",) for the
    list types (MySQL ENUM/SET) — and `defaults` gives the value the
    designer prefills for each, so picking a type is never a blank
    form. A type with no params renders as its bare name.
    """

    name: str
    params: tuple[str, ...] = ()
    defaults: tuple[str, ...] = ()
    note: str = ""  # one-line hint shown next to the type in the UI

    def render(self, values: tuple[str, ...] | list[str] = ()) -> str:
        """The type as it goes into DDL: name plus the arguments that
        were filled in. Empty arguments drop the whole parenthesis, so
        an unfilled VARCHAR stays a valid (if unsized) type rather than
        becoming ``VARCHAR()``."""
        if not self.params:
            return self.name
        filled = [str(v).strip() for v in values[: len(self.params)]]
        filled = [v for v in filled if v]
        if not filled:
            return self.name
        return f"{self.name}({', '.join(filled)})"


@dataclass(frozen=True)
class RelationInfo:
    """One foreign-key column: table.column references ref_table.ref_column.

    `schema` and `ref_schema` are filled only by the engines that have
    schemas as a level of their own (PostgreSQL, PG-01); everywhere
    else they stay empty and the names read exactly as before. A
    foreign key that leaves its own schema is the reason they exist:
    `orders.customer_id -> customers.id` says nothing about *which*
    `customers`, and `crm.customers.id` does.
    """

    table: str
    column: str
    ref_table: str
    ref_column: str
    schema: str = ""
    ref_schema: str = ""

    @property
    def cross_schema(self) -> bool:
        """Whether this key points outside the schema it is declared
        in. False wherever the engine has no schema level to leave."""
        return bool(self.ref_schema) and self.ref_schema != self.schema

    @property
    def target(self) -> str:
        """The referenced table as a row should print it: qualified
        when the key crosses a schema boundary, bare when it does
        not — a schema prefix on every row is noise that hides the one
        row where it matters."""
        if self.cross_schema:
            return f"{self.ref_schema}.{self.ref_table}"
        return self.ref_table

    @property
    def source(self) -> str:
        """The referring table, qualified on the same rule — for the
        inbound view, where the interesting row is the one arriving
        from another schema."""
        if self.cross_schema:
            return f"{self.schema}.{self.table}" if self.schema else self.table
        return self.table


@dataclass(frozen=True)
class ConstraintInfo:
    """One constraint on a table: primary key, unique, foreign key or
    check. `definition` is the dialect's own rendering where it has
    one, `columns` the participating columns as a comma-separated
    list where the catalog can name them."""

    name: str
    kind: str  # "PRIMARY KEY", "UNIQUE", "FOREIGN KEY", "CHECK", …
    table: str = ""
    columns: str = ""
    definition: str = ""


@dataclass(frozen=True)
class ObjectSummary:
    """A row of one of the looser property sections — a partition, a
    rule, a policy, a dependent object, a function a table's triggers
    call. Deliberately shapeless: what these have in common is a name
    and a line of explanation, and the kind tells the info view what
    the row opens."""

    name: str
    kind: str = ""  # the object kind the row opens, "" for a note
    detail: str = ""
    definition: str = ""


@dataclass(frozen=True)
class TableStats:
    """The general-information block of a table's properties. Every
    field is optional: an engine that cannot answer one leaves it
    empty and the row is left out rather than shown as unknown."""

    kind: str = ""  # "table", "partitioned table", "view", …
    owner: str = ""
    size: str = ""
    rows: str = ""  # an estimate, as the catalog reports it
    comment: str = ""
    engine: str = ""  # storage engine, where the dialect has them


# Every object kind the create/drop DDL surface can talk about;
# adapters advertise their subset through Connector.ddl_kinds().
DDL_KINDS = (
    "table", "view", "index", "trigger", "function", "procedure", "event",
)

# Console skeletons for adapters without dialect knowledge (JDBC).
_GENERIC_TEMPLATES = {
    "table": (
        "-- New table: adjust the name and columns, then Run.\n"
        "CREATE TABLE table_name (\n"
        "  id INTEGER PRIMARY KEY,\n"
        "  name VARCHAR(255) NOT NULL\n"
        ");\n"
    ),
    "view": (
        "-- New view: adjust the name and the SELECT, then Run.\n"
        "CREATE VIEW view_name AS\n"
        "SELECT column_a, column_b\n"
        "FROM table_name;\n"
    ),
    "index": (
        "-- New index: adjust the name, table and columns, then Run.\n"
        "CREATE INDEX index_name\n"
        "ON table_name (column_a);\n"
    ),
}


FILTER_OPERATORS = (
    "=", "!=", "<", "<=", ">", ">=",
    "LIKE", "NOT LIKE", "IS NULL", "IS NOT NULL",
)
NO_VALUE_OPERATORS = ("IS NULL", "IS NOT NULL")
CONJUNCTIONS = ("AND", "OR")


@dataclass(frozen=True)
class FilterCondition:
    """One line of a composed row filter."""

    column: str
    op: str  # one of FILTER_OPERATORS
    value: str = ""  # ignored for NO_VALUE_OPERATORS
    conjunction: str = "AND"  # joins this line to the lines above it


@dataclass(frozen=True)
class SortSpec:
    column: str
    descending: bool = False


@dataclass(frozen=True)
class PageCursor:
    """Where the last page stopped, for the next one to carry forward.

    `columns` are the order columns the page was sorted by (the user's
    sort plus the key tiebreaker), `values` the last row's values for
    them, and `descending` the single direction they all share — a row
    comparison only works one way at a time.
    """

    columns: list[str]
    values: tuple[Any, ...]
    descending: bool = False


@dataclass(frozen=True)
class PagePlan:
    """How one relation can be paged through (CORE-40).

    `order_by` is the order the adapter will actually apply: the user's
    sort with the row key appended as a tiebreaker, so two pages of the
    same table never disagree about where a row belongs. `keyset` says
    the order is unique-prefixed and uniform in direction, which is what
    a `(k1, k2) > (v1, v2)` comparison needs; without it the adapter
    keeps OFFSET. `stable` is False only when the relation has no usable
    key at all (a view, a heap with no primary key), in which case
    `note` says so in words the status line can show.
    """

    order_by: list[SortSpec] = field(default_factory=list)
    keyset: bool = False
    stable: bool = True
    note: str = ""


@dataclass(frozen=True)
class PageQuery:
    """One page's statement: SQL with placeholders, its parameters, the
    plan behind it, and the same statement with the values inlined —
    which is what the tab shows, so what is shown is what ran."""

    sql: str
    params: list[Any]
    plan: PagePlan
    display: str


@dataclass
class ResultSet:
    columns: list[str]
    rows: list[tuple[Any, ...]] = field(default_factory=list)
    # True when the adapter stopped fetching at the caller's max_rows
    # and the statement had more rows to give. The UI must say so:
    # a silently short result reads as the whole answer.
    truncated: bool = False
    # Where this page stopped, when the adapter paged by key; None when
    # it paged by offset (see PagePlan). The caller hands it back to
    # fetch_rows() for the next page and drops it on any change of
    # filter, sort or a jump to an arbitrary page.
    cursor: PageCursor | None = None
    # False when the rows came back in an order the engine does not
    # guarantee, so paging may repeat or skip rows. `order_note` says
    # why, for the UI to repeat to the user.
    stable: bool = True
    order_note: str = ""
    # The statement that produced these rows, values inlined, for the
    # tab's "describe query" line and the query history.
    statement: str = ""

    def __len__(self) -> int:
        return len(self.rows)


class CatalogCache:
    """What one connection has already read from its own catalog.

    Entries are keyed by (kind, database, schema, name) — the name
    being the table a column listing belongs to, "" for the listings
    that cover a whole schema — so nothing a switch of database or
    schema invalidates can be answered from the scope before it.

    There is no TTL, deliberately. A clock cannot know when someone
    else ran an ALTER, so a short TTL would only make the stale window
    smaller while pretending it had closed; what makes an entry wrong
    is an event, and every event we can observe drops the entry (see
    Connector.invalidate_catalog). The one case nobody can observe —
    another session changing the schema — is handled where it matters,
    in validation: a name the cache does not know is re-read from the
    server before it is rejected (Connector._assert_known_columns), so the
    cache can never turn a real column into an error. It can still
    briefly *hold* an object another session dropped, which is why a
    statement built from cached names is checked by the server anyway:
    the cache decides what reaches the SQL text, the server decides
    what exists.
    """

    def __init__(self) -> None:
        # Reentrant: a loader may itself ask the cache for something
        # else (paging_strategy wants columns and the row key).
        self._lock = threading.RLock()
        self._entries: dict[tuple, Any] = {}

    def get(self, key: tuple, load) -> Any:
        """The cached value for `key`, calling `load()` for it once.

        `load` runs outside the lock: it goes to the server, and a
        catalog query is not something to hold a lock across. Two
        threads racing on a cold key both load and the second one's
        answer wins, which costs one extra round trip and never a
        wrong answer.
        """
        with self._lock:
            if key in self._entries:
                return self._entries[key]
        value = load()
        with self._lock:
            self._entries[key] = value
        return value

    def store(self, key: tuple, value: Any) -> None:
        with self._lock:
            self._entries[key] = value

    def drop(self, key: tuple) -> None:
        with self._lock:
            self._entries.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


class ConnectorError(Exception):
    """Raised by adapters for any database failure, wrapping the driver error."""


class BatchError(ConnectorError):
    """A batch of row operations that failed, naming the one that did.

    `index` is the position of the failing operation in the list handed
    to apply_changes(), so the grid can point at the row the user has
    to look at rather than only repeating the driver's message.
    """

    def __init__(self, message: str, index: int) -> None:
        super().__init__(message)
        self.index = index


@dataclass(frozen=True)
class RowOperation:
    """One row change from the grid, as apply_changes() takes it.

    Today the only kind is "update" — one cell of one row, addressed by
    that row's primary key. Inserts and deletes (CORE-38) take the same
    shape, which is why the kind is spelled out rather than implied.
    """

    pk_values: dict[str, Any]
    column: str
    value: Any
    kind: str = "update"


def describe_operation(op: RowOperation) -> str:
    """The operation as a phrase for an error message: which row, which
    column — not just the driver's complaint."""
    where = ", ".join(f"{name}={value!r}" for name, value in op.pk_values.items())
    return f"row ({where}) column {op.column}"


def build_filter_clauses(
    filters: list[FilterCondition] | None,
    order_by: list[SortSpec] | None,
    quote: Any,
    placeholder: str = "?",
) -> tuple[str, str, list[Any]]:
    """Render WHERE and ORDER BY fragments (leading space and keyword
    included, empty string when unused) plus the parameter list.

    Conditions fold left-associatively — ((line1 AND line2) OR line3) —
    so evaluation matches the visual line order in the filter panel
    rather than SQL's AND-before-OR precedence.

    Operators and conjunctions are checked against the whitelists above;
    column names must already be validated against the catalog by the
    caller, since only the adapter can do that.
    """
    params: list[Any] = []
    where = ""
    for cond in filters or []:
        if cond.op not in FILTER_OPERATORS:
            raise ConnectorError(f"Unsupported filter operator: {cond.op}")
        if cond.conjunction not in CONJUNCTIONS:
            raise ConnectorError(f"Unsupported conjunction: {cond.conjunction}")
        clause = f"{quote(cond.column)} {cond.op}"
        if cond.op not in NO_VALUE_OPERATORS:
            clause += f" {placeholder}"
            params.append(cond.value)
        where = f"({where}) {cond.conjunction} {clause}" if where else clause
    if where:
        where = f" WHERE {where}"
    order = ""
    if order_by:
        order = " ORDER BY " + ", ".join(
            f"{quote(s.column)} {'DESC' if s.descending else 'ASC'}"
            for s in order_by
        )
    return where, order, params


def build_keyset_clause(
    cursor: PageCursor | None,
    order_by: list[SortSpec],
    quote: Any,
    placeholder: str = "?",
) -> tuple[str, list[Any]]:
    """The row comparison that resumes after `cursor`, as
    ``(k1, k2) > (?, ?)`` — ``<`` for a descending page.

    Empty when there is no cursor, when it belongs to a different order
    (the sort changed under it) or when one of its values is NULL, which
    a row comparison cannot answer. Every engine here understands the
    comparison and uses the index the key already has for it, so the
    cost of a page does not grow with how far down it sits.
    """
    if cursor is None or not order_by:
        return "", []
    names = [s.column for s in order_by]
    if list(cursor.columns) != names or len(cursor.values) != len(names):
        return "", []
    if any(v is None for v in cursor.values):
        return "", []
    if cursor.descending != bool(order_by[0].descending):
        return "", []
    columns = ", ".join(quote(n) for n in names)
    marks = ", ".join([placeholder] * len(names))
    op = "<" if cursor.descending else ">"
    return f"({columns}) {op} ({marks})", list(cursor.values)


def inline_params(sql: str, params: list[Any], placeholder: str = "?") -> str:
    """`sql` with its bound values written in, for showing the user the
    statement that ran. Never sent to a server — the adapters bind the
    real values."""
    parts = sql.split(placeholder)
    out = parts[0]
    for index, rest in enumerate(parts[1:]):
        mark = sql_literal(params[index]) if index < len(params) else placeholder
        out += mark + rest
    return out


def sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


class Connector(ABC):
    """Everything the UI knows about a database goes through this interface.

    Adapters own all dialect differences: identifier quoting, catalog
    queries, pagination syntax. Driver exceptions must be re-raised as
    ConnectorError with a readable message.

    Catalog reads a connection makes on its own behalf — the ones
    behind validation and paging — go through the catalog_* methods
    below rather than through list_tables()/list_columns() directly,
    so they are answered from this connection's CatalogCache
    (CORE-41). The list_* methods stay the uncached truth: an adapter
    implements those, and a caller that must see the server (the
    reload behind a validation miss) asks them.
    """

    @abstractmethod
    def connect(self) -> None: ...

    # Catalog cache (CORE-41)
    #
    # Invalidation, in one place, because a stale catalog is worse
    # than a slow one. Everything that can change this connection's
    # catalog and that we can observe drops the whole cache:
    #
    #   * any statement run through execute() that is not a plain
    #     read, a row change or transaction control — see
    #     _note_statement(); every DDL path in the app (the definition
    #     tab, the table designer, drop dialogs, extension install and
    #     drop, the console) ends in execute(), so hooking it there
    #     covers them all rather than one screen at a time;
    #   * connect() and close(), so a reconnect starts empty;
    #   * a change of the schema names resolve in (set_search_path);
    #   * the sidebar's Refresh, through invalidate_catalog().
    #
    # A change of database is not in that list because it does not
    # need to be: the app holds one connector per (connection,
    # database, schema), so switching creates a different connector
    # with a cache of its own — and the scope is in the key anyway.

    @property
    def catalog_cache(self) -> CatalogCache:
        """This connection's cache, created on first use.

        A property rather than a constructor line: every adapter
        writes its own __init__ and none of them calls super(), so a
        base-class attribute would have to be added four times and
        would be missing from the fifth adapter somebody writes.
        """
        cache = getattr(self, "_catalog_cache", None)
        if cache is None:
            cache = CatalogCache()
            self._catalog_cache = cache
        return cache

    def invalidate_catalog(self) -> None:
        """Forget everything cached about this connection's catalog.

        Coarse on purpose: working out which entries one ALTER touched
        is guesswork (a rename shows up under two names, a CASCADE
        drop reaches objects the statement never named), and the cost
        of being wrong is a wrong answer. The cost of being coarse is
        one catalog query.
        """
        self.catalog_cache.clear()

    def _catalog_scope(self) -> tuple[str, str]:
        """(database, schema) this connection currently resolves names
        in, read from the adapter's own attributes rather than from the
        server — a cache key must not itself cost a round trip."""
        return (
            str(getattr(self, "database", "") or ""),
            str(getattr(self, "schema", "") or ""),
        )

    def _catalog_key(self, kind: str, name: str = "") -> tuple:
        database, schema = self._catalog_scope()
        return (kind, database, schema, name)

    def catalog_tables(self, *, reload: bool = False) -> list[TableInfo]:
        """list_tables(), cached for this connection."""
        key = self._catalog_key("tables")
        if reload:
            self.catalog_cache.drop(key)
        return list(self.catalog_cache.get(key, self.list_tables))

    def catalog_columns(
        self, table: str, *, reload: bool = False
    ) -> list[ColumnInfo]:
        """list_columns(table), cached for this connection."""
        key = self._catalog_key("columns", table)
        if reload:
            self.catalog_cache.drop(key)
        return list(
            self.catalog_cache.get(key, lambda: self.list_columns(table))
        )

    def catalog_relations(self, *, reload: bool = False) -> list[RelationInfo]:
        """list_relations(), cached for this connection."""
        key = self._catalog_key("relations")
        if reload:
            self.catalog_cache.drop(key)
        return list(self.catalog_cache.get(key, self.list_relations))

    def catalog_schemas(
        self, *, include_system: bool = False, reload: bool = False
    ) -> list[str]:
        """list_schemas(), cached for this connection. The system
        schemas are a different listing, so they are a different key
        rather than a filter over one."""
        key = self._catalog_key("schemas", "system" if include_system else "")
        if reload:
            self.catalog_cache.drop(key)
        return list(
            self.catalog_cache.get(
                key, lambda: self.list_schemas(include_system=include_system)
            )
        )

    def _note_statement(self, sql: str) -> None:
        """Drop the cache if `sql` could have changed the catalog.

        Called by every adapter's execute(). The rule is the cautious
        way round: a statement keeps the cache only when it is plainly
        a read, a row change or transaction control. Anything else —
        CREATE, ALTER, DROP, TRUNCATE, and the statements the
        classifier cannot name at all (COMMENT ON, RENAME TABLE, a
        procedure call that does DDL inside) — invalidates. Being
        wrong that way costs one catalog query; being wrong the other
        way puts a dropped table's name into a statement.
        """
        if sql_risk.changes_catalog(sql):
            self.invalidate_catalog()

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def list_tables(self) -> list[TableInfo]:
        """Tables and views in the connected database, sorted by name."""

    @abstractmethod
    def list_columns(self, table: str) -> list[ColumnInfo]: ...

    def list_databases(self, *, include_system: bool = False) -> list[str]:
        """Databases reachable through this connection, sorted by name,
        for the query console's database switcher.

        `include_system` adds the databases the server owns — on MySQL,
        where a schema *is* a database, that is `information_schema`,
        `mysql`, `performance_schema` and `sys`. The object tree asks
        for them and shows them dimmed (PG-03, MY-01); the switcher
        leaves them out, the same split `list_schemas` makes.

        Concrete default (not abstract): single-database connectors —
        SQLite, where one file is one database — need no override.
        """
        return []

    def list_tables_in(self, schema: str) -> list[TableInfo]:
        """Tables and views of one schema, sorted by name.

        Concrete default (not abstract): where a schema is not a level
        of its own — SQLite, MySQL, JDBC — there is only one list to
        give and the argument is redundant. Adapters with schemas
        override it, and the metadata provider (db/metadata.py) uses
        it to fill a schema node.
        """
        return self.list_tables()

    def list_schemas(self, *, include_system: bool = False) -> list[str]:
        """Schemas inside the connected database, sorted by name, for
        the console's schema switcher.

        `include_system` adds the schemas the server owns —
        `information_schema`, the engine's own catalog — which the
        object tree shows dimmed (PG-03) and the switcher leaves out.

        Concrete default (not abstract), empty for every adapter where
        a schema is not a level of its own: SQLite has none, and in
        MySQL a schema *is* a database, so its list_databases() is
        already the schema list and a second dropdown would only
        repeat it.
        """
        return []

    def current_schema(self) -> str:
        """The schema unqualified object names resolve to. Empty when
        the adapter has no schemas (see list_schemas)."""
        return ""

    def search_path(self) -> str:
        """The schemas an unqualified name is looked up in, in order,
        as one line for the console to show (PG-01).

        Defaults to `current_schema()` — an engine with one schema
        level and no search path of its own has a path of exactly one
        entry — and to nothing at all where there are no schemas.
        """
        return self.current_schema()

    def set_search_path(self, schema: str) -> None:
        """Make `schema` the one unqualified names resolve in, for the
        rest of this session.

        A no-op wherever schemas are not a level of their own: in
        MySQL the database switcher already did it, and SQLite has
        nothing to switch.
        """
        return None

    def list_functions(self) -> list[FunctionInfo]:
        """Stored functions in the connected database, sorted by name.

        Concrete default (not abstract) so adapters without a function
        catalog — SQLite, the unimplemented stubs — need no override.
        """
        return []

    def list_routines(self, kind: str = "") -> list[FunctionInfo]:
        """Stored routines of one kind — "function", "procedure" — or
        every routine for "".

        Concrete default (not abstract): an engine that does not tell
        functions from procedures answers with all of them either way,
        which is what `list_functions` already gives. Only an engine
        that has both *and* shows them as folders of their own (MySQL,
        MY-01) needs to override.
        """
        return self.list_functions()

    def list_indexes(self) -> list[IndexInfo]:
        """Indexes in the connected database, sorted by name, with
        their owning table when the dialect needs it to drop one.

        Concrete default (not abstract) so adapters without an index
        catalog need no override.
        """
        return []

    def list_triggers(self) -> list[TriggerInfo]:
        """Triggers in the connected database, sorted by name, with
        their owning table when the dialect needs it to drop one.

        Concrete default (not abstract) so adapters without a trigger
        catalog need no override.
        """
        return []

    def list_events(self) -> list[str]:
        """Scheduled event names (MySQL only), sorted by name.

        Concrete default (not abstract): only MySQL overrides.
        """
        return []

    def list_relations(self) -> list[RelationInfo]:
        """Foreign-key relations between the connected database's
        tables, for the relation graph.

        Concrete default (not abstract) so adapters without a
        foreign-key catalog need no override.
        """
        return []

    # Table properties (CORE-04). Everything here is optional: the
    # base implementations answer "nothing to report", and a provider
    # only offers the section to the UI when its engine's capability
    # flag says the engine has that concept at all (db/metadata.py).
    # That way a section is omitted where an engine has no such thing
    # and empty where it has none of them right now.

    def table_stats(self, table: str) -> TableStats:
        """Owner, size, row estimate and comment for one table."""
        return TableStats()

    def list_constraints(self, table: str) -> list[ConstraintInfo]:
        """The constraints declared on one table."""
        return []

    def list_references(self, table: str) -> list[RelationInfo]:
        """Foreign keys *pointing at* this table — the mirror of the
        table's own. Derived from list_relations() here, so every
        adapter with a foreign-key catalog gets it for free."""
        return [r for r in self.catalog_relations() if r.ref_table == table]

    def list_partitions(self, table: str) -> list[ObjectSummary]:
        """The partitions of a partitioned table."""
        return []

    def list_rules(self, table: str) -> list[ObjectSummary]:
        """Rewrite rules on the table (PostgreSQL)."""
        return []

    def list_policies(self, table: str) -> list[ObjectSummary]:
        """Row-level security policies on the table (PostgreSQL)."""
        return []

    def list_dependencies(self, table: str) -> list[ObjectSummary]:
        """Objects that depend on this table — views built on it and
        anything else the catalog records as depending on it."""
        return []

    def list_table_functions(self, table: str) -> list[ObjectSummary]:
        """Functions related to the table: the ones its triggers call,
        where the catalog can say."""
        return []

    def list_extensions(self) -> list[ExtensionState]:
        """Every extension the server has, installed or merely
        available (db/extensions.py's `ExtensionState`).

        One listing rather than two: an available extension and an
        installed one differ by a version string, and the Extensions
        folder wants to show both without asking twice.

        Concrete default: an engine with no extensions answers empty,
        which is also what the `extensions` capability says.
        """
        return []

    def can_manage_extensions(self) -> bool:
        """May the account this connection is on install, update or
        drop an extension? False everywhere by default — the actions
        are offered only where they would work (PG-05)."""
        return False

    def extension_owner(self, name: str, schema: str = "") -> str:
        """The extension that owns this object, "" for one nobody
        installed — so an extension's tables and functions can be
        attributed to it instead of appearing as mysterious user
        objects (PG-05)."""
        return ""

    def list_catalog(self, slug: str, schema: str = "") -> list[ObjectSummary]:
        """The rows of one of the looser catalog folders the object
        tree can show — sequences, extensions, tablespaces, server
        settings — named by the folder's slug (db/metadata.py's
        CATALOG_CATEGORIES).

        Deliberately one method rather than one per folder: what these
        listings have in common is a name and a line of explanation
        (`ObjectSummary`), and an engine that has no such folder never
        offers it, so the interface would otherwise grow a dozen
        methods that answer `[]` everywhere but PostgreSQL.

        Concrete default (not abstract): a slug this adapter has no
        catalog for answers empty, and the folder shows its empty
        state rather than an error.
        """
        return []

    # Accounts and privileges. Reading is a catalog query like any
    # other; changing anything returns SQL for the user to review and
    # run, the same contract the create/drop surface below keeps —
    # nothing here executes a GRANT behind anyone's back.

    #: Whether this adapter can list accounts at all (drives the
    #: sidebar's Users category and the users tab). False for the
    #: file-based and dialect-blind adapters: SQLite has no accounts,
    #: and JDBC has no portable catalog for them.
    supports_users = False

    def list_users(self) -> list[UserInfo]:
        """Accounts on the server this connection reaches, sorted by
        name. Server-wide rather than per-database: an account outlives
        any one database on the same server.

        Concrete default (not abstract) so adapters without accounts
        need no override.
        """
        return []

    def list_privileges(self, user: UserInfo) -> list[PrivilegeInfo]:
        """Everything `user` is allowed to do, as the catalog reports
        it — server-wide rights, per-database and per-table grants, and
        role memberships where the dialect has them."""
        return []

    def list_object_grants(self, kind: str, name: str) -> list[PrivilegeInfo]:
        """Every privilege recorded on one object, with the account it
        was granted to named in `scope` ("role analyst", "user
        'app'@'%'"). The mirror image of list_privileges(), which
        starts from an account instead.

        `name` comes from the catalog, but it goes into the query as a
        parameter all the same — a catalog is not a promise about what
        an object is called (see docs/architecture.md).

        Concrete default (not abstract): an adapter with no privilege
        system — SQLite — reports none and declares the `grants`
        capability off (db/metadata.py).
        """
        return []

    def grant_scopes(self) -> list[GrantScope]:
        """What the grant/revoke dialog can target on this server: the
        whole server plus each database (and, where they are a level of
        their own, each schema). Reads the catalog, so call it from a
        worker thread."""
        return []

    def privilege_names(self) -> tuple[str, ...]:
        """The privileges the grant/revoke dialog offers, in the order
        it shows them."""
        return ()

    def account_ident(self, user: UserInfo) -> str:
        """`user` as it is written inside a statement — MySQL's
        'name'@'host' form, a plain quoted identifier elsewhere."""
        return self.quote_ident(user.name)

    def create_user_sql(
        self, name: str, host: str = "", password: str = ""
    ) -> str:
        """Statement creating an account that can log in. An empty
        password means the dialect's own default (no password clause),
        not an empty one."""
        raise ConnectorError("This connection cannot manage accounts")

    def drop_user_sql(self, user: UserInfo) -> str:
        raise ConnectorError("This connection cannot manage accounts")

    def set_password_sql(self, user: UserInfo, password: str) -> str:
        raise ConnectorError("This connection cannot manage accounts")

    def grant_sql(
        self, user: UserInfo, privileges: list[str], target: str
    ) -> str:
        """GRANT statement for `privileges` on a GrantScope's target.

        Privileges are checked against privilege_names(): they land in
        the statement as text, and the catalog cannot vouch for them
        the way it vouches for a table name.
        """
        return (
            f"GRANT {self._privilege_list(privileges)} ON {target} "
            f"TO {self.account_ident(user)}"
        )

    def revoke_sql(
        self, user: UserInfo, privileges: list[str], target: str
    ) -> str:
        return (
            f"REVOKE {self._privilege_list(privileges)} ON {target} "
            f"FROM {self.account_ident(user)}"
        )

    def _privilege_list(self, privileges: list[str]) -> str:
        allowed = self.privilege_names()
        chosen = [p.strip().upper() for p in privileges if p.strip()]
        unknown = [p for p in chosen if p not in allowed]
        if unknown:
            raise ConnectorError(
                f"Unsupported privilege(s): {', '.join(unknown)}"
            )
        if not chosen:
            raise ConnectorError("No privileges selected")
        return ", ".join(chosen)

    def in_transaction(self) -> bool:
        """Whether an explicit transaction (user-issued BEGIN) is open
        on this connection. Drives the console's transaction badge and
        the close-time warnings.

        Concrete default (not abstract) for adapters that cannot tell.
        """
        return False

    def rollback(self) -> None:
        """Roll back the open transaction, if any. Used when the user
        force-closes a console or the window despite the warning."""
        self.execute("ROLLBACK")

    def get_ddl(self, name: str) -> str:
        """CREATE statement for a table or view, for the sidebar's
        hover preview. Empty string when unknown or unsupported.

        Concrete default (not abstract) so adapters without a DDL
        catalog need no override.
        """
        return ""

    def schema_ddl(self) -> list[str]:
        """Every CREATE statement needed to rebuild this database's
        structure, in an order that can be replayed top to bottom.

        Structure only — no INSERTs. This is what the sidebar's
        "Open Schema" captures (backend/schemas.py) and offers as a
        whole-database script.

        The generic implementation walks the catalog through get_ddl(),
        which covers tables, views and programmable objects. Adapters
        that can do better override it: some carry indexes inside their
        table DDL, and some can dump the lot in one query.
        """
        objects = self.list_tables()
        return self.ddl_for(
            [t.name for t in objects if t.kind != "view"]
            + [t.name for t in objects if t.kind == "view"]
            + [f.name for f in self.list_functions()]
        )

    def ddl_for(self, names: list[str]) -> list[str]:
        """get_ddl() over `names`, keeping the order given.

        An object whose DDL comes back empty becomes a comment saying
        so rather than nothing at all. A server can refuse to show a
        routine's body (MySQL returns NULL for one whose definer you
        are not, without erroring), and a schema script that quietly
        dropped an object would be rebuilt wrong by whoever ran it.
        """
        statements = []
        for name in names:
            if ddl := self.get_ddl(name).strip():
                statements.append(ddl)
            else:
                statements.append(
                    f"-- {name}: no CREATE statement available "
                    "(the server did not return one — insufficient "
                    "privileges on its definition?)"
                )
        return statements

    def explain_prefix(self) -> str:
        """Prefix that turns a statement into its plan query, for the
        console's Explain button (dialects override: SQLite uses
        EXPLAIN QUERY PLAN)."""
        return "EXPLAIN "

    def drop_function_sql(self, name: str) -> str:
        """Statement that removes the stored object `name` so its
        CREATE can be re-run when saving an edited definition. Empty
        string when the adapter doesn't support replacing functions.
        """
        return ""

    # Create/drop DDL. All of these return SQL for the user to review
    # (or a prefilled console) — nothing here executes anything.

    #: Whether drop_sql(cascade=True) means anything in this dialect
    #: (drives the drop dialog's CASCADE checkbox).
    supports_drop_cascade = False

    #: Whether the sidebar offers Drop… at all. JDBC turns this off:
    #: without reliable dialect knowledge it stays template-only.
    supports_drop = True

    def ddl_kinds(self) -> tuple[str, ...]:
        """Which of DDL_KINDS this adapter can create and drop (drives
        the sidebar's menu visibility). Safe to call before connect()."""
        return ("table", "view", "index")

    def drop_sql(
        self, kind: str, name: str, table: str = "", cascade: bool = False
    ) -> str:
        """Quoted, dialect-correct DROP statement for one object.

        `table` is the owning table for the kinds that need it (MySQL
        indexes, Postgres triggers); `cascade` is honored only where
        the dialect supports it (see supports_drop_cascade). May query
        the catalog (Postgres function signatures), so call it from a
        worker thread.
        """
        if kind not in DDL_KINDS:
            raise ConnectorError(f"Unknown object kind: {kind}")
        return f"DROP {kind.upper()} {self.quote_ident(name)}"

    def create_template(self, kind: str) -> str:
        """Commented, dialect-correct CREATE skeleton for a query
        console (the "New ▸" menu). Empty string when the adapter has
        no template for `kind`."""
        return _GENERIC_TEMPLATES.get(kind, "")

    def column_type_specs(self) -> list[TypeSpec]:
        """Every column type the table designer offers for this
        dialect, with the arguments each one takes (see TypeSpec).
        Free text stays allowed in the designer; this is a menu, not a
        whitelist. The generic list is SQL-92, for adapters with no
        dialect knowledge of their own (JDBC)."""
        return [
            TypeSpec("INTEGER"),
            TypeSpec("SMALLINT"),
            TypeSpec("BIGINT"),
            TypeSpec("DECIMAL", ("precision", "scale"), ("10", "2")),
            TypeSpec("REAL"),
            TypeSpec("DOUBLE PRECISION"),
            TypeSpec("CHAR", ("length",), ("1",)),
            TypeSpec("VARCHAR", ("length",), ("255",)),
            TypeSpec("TEXT"),
            TypeSpec("BOOLEAN"),
            TypeSpec("DATE"),
            TypeSpec("TIME"),
            TypeSpec("TIMESTAMP"),
            TypeSpec("BLOB"),
        ]

    def column_types(self) -> list[str]:
        """The type list as plain rendered strings (each type with its
        default arguments) — the shape callers wanted before types
        carried their arguments."""
        return [spec.render(spec.defaults) for spec in self.column_type_specs()]

    def create_table_sql(
        self,
        table: str,
        columns: list[ColumnInfo],
        defaults: dict[str, str] | None = None,
    ) -> str:
        """CREATE TABLE statement for the table designer: column names,
        types, DEFAULT expressions, NOT NULL and the primary key.
        Dialects only override quirks."""
        defaults = defaults or {}
        defs = []
        for column in columns:
            line = f"  {self.quote_ident(column.name)} {column.type}".rstrip()
            if defaults.get(column.name, "").strip():
                line += f" DEFAULT {defaults[column.name].strip()}"
            if not column.nullable:
                line += " NOT NULL"
            defs.append(line)
        pks = [c.name for c in columns if c.is_pk]
        if pks:
            defs.append(
                "  PRIMARY KEY ("
                + ", ".join(self.quote_ident(p) for p in pks)
                + ")"
            )
        return (
            f"CREATE TABLE {self.quote_ident(table)} (\n"
            + ",\n".join(defs)
            + "\n)"
        )

    # DDL editing (definition tab). All return SQL for the user to
    # review — nothing here executes anything.

    def rename_column_sql(self, table: str, old: str, new: str) -> str:
        """ALTER statement renaming one column (SQLite ≥3.25 and
        MySQL 8 share the syntax)."""
        return (
            f"ALTER TABLE {self.quote_ident(table)} "
            f"RENAME COLUMN {self.quote_ident(old)} "
            f"TO {self.quote_ident(new)}"
        )

    def modify_column_sql(self, table: str, column: ColumnInfo) -> str:
        """Statement changing a column's type/nullability in place.
        Empty string when the dialect has no in-place form (SQLite) —
        the caller falls back to a table rebuild."""
        return ""

    def add_column_sql(self, table: str, definition: str) -> str:
        """ALTER adding one column, `definition` being the column's
        entry exactly as it was written in the edited CREATE."""
        return (
            f"ALTER TABLE {self.quote_ident(table)} "
            f"ADD COLUMN {definition}"
        )

    def drop_column_sql(self, table: str, column: str) -> str:
        return (
            f"ALTER TABLE {self.quote_ident(table)} "
            f"DROP COLUMN {self.quote_ident(column)}"
        )

    #: Whether the rename-old / create-new / copy / drop-old rebuild is
    #: a safe way to apply an edited CREATE in this dialect. It is a
    #: workaround for SQLite's limited ALTER TABLE and nothing else:
    #: MySQL commits DDL implicitly, so no transaction can undo a
    #: rebuild that fails half way, and a Postgres RENAME carries the
    #: inbound foreign keys onto the backup, leaving the rebuilt table
    #: unreferenced even when every statement succeeds. Both have real
    #: ALTER TABLE and take that path instead.
    supports_table_rebuild = True

    #: Longest identifier the dialect accepts, used to keep generated
    #: backup names legal. 0 means no practical limit.
    identifier_max_length = 0

    #: Whether a table's CREATE statement spells out its secondary
    #: indexes inline (MySQL's SHOW CREATE TABLE does; the dialects that
    #: keep indexes as objects of their own do not). Decides whether a
    #: rebuild has to carry indexes across itself or would only be
    #: recreating what the new CREATE already declares.
    ddl_declares_indexes = False

    def rebuild_table_statements(
        self, table: str, new_ddl: str, copy_columns: list[tuple[str, str]]
    ) -> list[str]:
        """The rename-old / create-new / copy / drop-old sequence that
        applies an edited CREATE statement to an existing table.
        `copy_columns` maps each surviving column as (new name, old
        name) — identical when the column wasn't renamed.

        Indexes and triggers belong to the table rather than to its
        name: the rename carries them onto the backup, and dropping the
        backup takes them with it. Their DDL is captured here, while
        they still describe `table`, and replayed after the drop —
        after, because until then the old objects still hold the names.
        Reads the catalog to do it, so call this from a worker thread.
        """
        backup = self._backup_table_name(table)
        new_cols = ", ".join(self.quote_ident(n) for n, _o in copy_columns)
        old_cols = ", ".join(self.quote_ident(o) for _n, o in copy_columns)
        carried = self._carried_object_ddl(table)
        statements = [
            f"ALTER TABLE {self.quote_ident(table)} "
            f"RENAME TO {self.quote_ident(backup)}",
            new_ddl.rstrip().rstrip(";"),
        ]
        if copy_columns:
            statements.append(
                f"INSERT INTO {self.quote_ident(table)} ({new_cols}) "
                f"SELECT {old_cols} FROM {self.quote_ident(backup)}"
            )
        statements.append(f"DROP TABLE {self.quote_ident(backup)}")
        return statements + carried

    def wrap_rebuild(self, statements: list[str]) -> list[str]:
        """`statements` plus whatever makes the rebuild atomic in this
        dialect. The base class adds nothing: a wrapper is only worth
        having where the dialect can actually honor it, and claiming a
        transaction it would silently commit through is worse than
        claiming none.
        """
        return list(statements)

    def rebuild_check_failure(self, sql: str, result: Any) -> str:
        """Message describing a rebuild check that reported a problem
        instead of raising one, given the statement and what execute()
        returned for it. Empty string when the statement is not such a
        check, or when it passed."""
        return ""

    def _backup_table_name(self, table: str) -> str:
        """A free name for the rebuild's backup copy.

        The suffix is random rather than fixed: a `__old` left behind by
        an earlier failed rebuild would otherwise block every later one,
        and a real table of that name would collide with it. The catalog
        decides what is free, so call this from a worker thread.
        """
        taken = {t.name for t in self.list_tables()}
        limit = self.identifier_max_length
        while True:
            suffix = f"__old_{token_hex(4)}"
            stem = table
            if limit and len(stem) + len(suffix) > limit:
                stem = stem[: limit - len(suffix)]
            candidate = f"{stem}{suffix}"
            if candidate not in taken:
                return candidate

    def _carried_object_ddl(self, table: str) -> list[str]:
        """CREATE statements for the indexes and triggers attached to
        `table`, for a rebuild to replay once the new table is in place.

        An object whose adapter cannot produce a CREATE statement is
        skipped: there is nothing to replay, and a rebuild that carried
        a half-written one would fail rather than lose it quietly.
        """
        objects: list[IndexInfo | TriggerInfo] = list(self.list_triggers())
        if not self.ddl_declares_indexes:
            objects = list(self.list_indexes()) + objects
        return [
            obj.ddl.rstrip().rstrip(";")
            for obj in objects
            if obj.table == table and obj.ddl.strip()
        ]

    @abstractmethod
    def fetch_rows(
        self,
        table: str,
        offset: int = 0,
        limit: int = 500,
        filters: list[FilterCondition] | None = None,
        order_by: list[SortSpec] | None = None,
        cursor: PageCursor | None = None,
    ) -> ResultSet:
        """One page of `table`.

        With `cursor` set — the cursor a previous page came back with —
        the adapter carries on from where that page stopped and ignores
        `offset`; without one it starts at `offset`. Either way the
        result says which it did (ResultSet.cursor) and whether the
        order it used is one the engine guarantees (ResultSet.stable).
        """

    # Paging (CORE-40)

    def row_key_columns(self, table: str) -> list[str]:
        """Columns that identify a row of `table` uniquely, in the order
        a page should sort by them.

        The primary key by default, which is what every engine here can
        name from list_columns(). An adapter with something better for
        keyless relations (SQLite's rowid) overrides this; one with
        nothing to offer returns [] and the relation pages by offset in
        an order nobody guarantees, which paging_strategy() says out
        loud rather than hiding.
        """
        return [c.name for c in self.catalog_columns(table) if c.is_pk]

    def paging_strategy(
        self, table: str, order_by: list[SortSpec] | None = None
    ) -> PagePlan:
        """How this relation can be paged, given the user's sort.

        The one place the choice is made: the grid asks for a page and
        is told what it got, rather than deciding per engine itself.
        """
        user = list(order_by or [])
        try:
            key = self.row_key_columns(table)
            selectable = {c.name for c in self.catalog_columns(table)}
        except ConnectorError:
            key, selectable = [], set()
        if not key:
            return PagePlan(
                order_by=user,
                keyset=False,
                stable=False,
                note=(
                    "no primary key or row id, so the order of rows "
                    "between pages is not guaranteed"
                ),
            )
        used = {s.column for s in user}
        tail = [k for k in key if k not in used]
        # The tiebreaker takes the direction of the sort it closes, so a
        # descending sort stays uniform and keyset-able.
        direction = user[-1].descending if user else False
        effective = user + [SortSpec(k, direction) for k in tail]
        uniform = len({s.descending for s in effective}) <= 1
        # A key the projection does not carry (SQLite's rowid) can order
        # a page but cannot be carried forward as a cursor value.
        covered = all(s.column in selectable for s in effective)
        note = ""
        if not uniform:
            note = "sort directions are mixed, so pages use OFFSET"
        elif not covered:
            note = "the row key is not one of the columns, so pages use OFFSET"
        return PagePlan(
            order_by=effective,
            keyset=uniform and covered,
            stable=True,
            note=note,
        )

    def _page_query(
        self,
        table: str,
        offset: int,
        limit: int,
        filters: list[FilterCondition] | None,
        order_by: list[SortSpec] | None,
        cursor: PageCursor | None,
        placeholder: str = "?",
    ) -> PageQuery:
        """Build one page's statement, keyset where the plan allows it.

        Shared by every adapter whose dialect spells the tail
        `LIMIT n [OFFSET n]`, which is all three of them.
        """
        plan = self.paging_strategy(table, order_by)
        where, order, params = build_filter_clauses(
            filters, plan.order_by, self.quote_ident, placeholder
        )
        keyset, key_params = build_keyset_clause(
            cursor if plan.keyset else None,
            plan.order_by,
            self.quote_ident,
            placeholder,
        )
        if keyset:
            where = f"{where} AND {keyset}" if where else f" WHERE {keyset}"
            params = [*params, *key_params, max(limit, 0)]
            tail = f" LIMIT {placeholder}"
        else:
            params = [*params, max(limit, 0), max(offset, 0)]
            tail = f" LIMIT {placeholder} OFFSET {placeholder}"
        sql = f"SELECT * FROM {self.quote_ident(table)}{where}{order}{tail}"
        return PageQuery(
            sql=sql,
            params=params,
            plan=plan,
            display=inline_params(sql, params, placeholder),
        )

    def _page_result(
        self, columns: list[str], rows: list[tuple], query: PageQuery
    ) -> ResultSet:
        """Wrap a page's rows, working out the cursor the next page
        carries forward."""
        plan = query.plan
        cursor = None
        if plan.keyset and rows:
            names = [s.column for s in plan.order_by]
            try:
                values = tuple(rows[-1][columns.index(n)] for n in names)
            except ValueError:
                values = None
            # A NULL key value makes the row comparison return NULL and
            # silently drop rows, so the next page falls back to offset.
            if values is not None and all(v is not None for v in values):
                cursor = PageCursor(
                    columns=names,
                    values=values,
                    descending=bool(plan.order_by[0].descending),
                )
        return ResultSet(
            columns=columns,
            rows=rows,
            cursor=cursor,
            stable=plan.stable,
            order_note=plan.note if not plan.stable else "",
            statement=query.display,
        )

    def _assert_known_table(self, table: str) -> None:
        """Refuse a name the catalog does not list as a relation, so
        only identifiers the catalog vouches for reach the SQL text.

        Answered from the cache, and a miss is a reload rather than a
        rejection: a table another session created must be openable
        without reconnecting first. So the price of the check is one
        catalog query per connection, and a wrong answer still costs
        exactly what it did before — a round trip.
        """
        if table in {t.name for t in self.catalog_tables()}:
            return
        if table in {t.name for t in self.catalog_tables(reload=True)}:
            return
        raise ConnectorError(f"No such table or view: {table}")

    def _assert_known_columns(self, table: str, used: set[str]) -> None:
        """Refuse column names `table` does not have, on the same
        cache-then-reload rule as _assert_known_table: a column added
        by somebody else is picked up, not blocked."""
        if not used:
            return
        known = {c.name for c in self.catalog_columns(table)}
        if used <= known and known:
            return
        known = {c.name for c in self.catalog_columns(table, reload=True)}
        if not known:
            raise ConnectorError(f"No such table: {table}")
        if unknown := used - known:
            raise ConnectorError(
                f"Unknown column(s) for {table}: {', '.join(sorted(unknown))}"
            )

    def _assert_filter_columns(
        self,
        table: str,
        filters: list[FilterCondition] | None,
        order_by: list[SortSpec] | None,
    ) -> None:
        """Reject filter/sort column names the catalog doesn't vouch for,
        so they never reach the SQL text. Skipped when neither is set —
        the first page of a table needs no column listing at all."""
        if not filters and not order_by:
            return
        used = {f.column for f in filters or []} | {s.column for s in order_by or []}
        self._assert_known_columns(table, used)

    @abstractmethod
    def execute(self, sql: str, max_rows: int | None = None) -> ResultSet | int:
        """Run arbitrary SQL. Returns a ResultSet for row-returning
        statements, otherwise the affected row count.

        With max_rows set, at most that many rows are fetched and the
        ResultSet is flagged `truncated` if the statement had more. An
        unbounded fetch is how a SELECT over a large table takes the
        whole app down, so every caller that renders into a grid
        should pass a cap.
        """

    # Whether cancel() can actually reach the running statement. False
    # keeps the UI from offering a Cancel button that would do nothing.
    supports_cancel = False

    def cancel(self) -> None:
        """Ask the server to abort the statement running right now.

        Called from a *different* thread than the one blocked in
        execute(), so an implementation must not take the connector
        lock — it would deadlock behind the very statement it is
        trying to stop. Each backend has its own mechanism (a cancel
        request on the socket, KILL QUERY over a second connection,
        sqlite3's interrupt); the cancelled statement fails on its own
        thread with a driver error, which is the caller's signal that
        it stopped.
        """
        raise ConnectorError("This connection cannot cancel a running statement")

    @abstractmethod
    def update_cell(
        self,
        table: str,
        pk_values: dict[str, Any],
        column: str,
        value: Any,
    ) -> None:
        """UPDATE a single cell, addressing the row by its primary key."""

    # The parameter marker this driver binds with. Only the batch
    # helpers below build SQL from it; the hand-written statements in
    # each adapter spell their own.
    placeholder = "?"

    def apply_changes(
        self, table: str, operations: list[RowOperation]
    ) -> None:
        """Apply a Save's worth of row operations atomically.

        Everything in `operations` lands or nothing does: the batch runs
        inside one explicit transaction, and the first failure rolls the
        whole thing back and raises BatchError naming the operation (and
        carrying its index) that failed.

        If the user already has a transaction open on this connection —
        a BEGIN typed in the console — the batch joins it instead of
        nesting: a SAVEPOINT bounds the batch so a failure undoes only
        the batch, and nothing is committed here. Their Commit or
        Rollback still decides.

        Identifiers are validated against the catalog once for the whole
        batch rather than once per operation.
        """
        if not operations:
            return
        self._assert_batch_columns(table, operations)
        joined = self.in_transaction()
        savepoint = "sqlide_apply"
        self._run_statement(
            f"SAVEPOINT {savepoint}" if joined else "BEGIN"
        )
        try:
            for index, op in enumerate(operations):
                self._apply_operation(table, op, index)
        except Exception:
            try:
                self._run_statement(
                    f"ROLLBACK TO SAVEPOINT {savepoint}"
                    if joined
                    else "ROLLBACK"
                )
            except ConnectorError:
                # The driver may have unwound the transaction itself;
                # the batch's own failure is the one worth reporting.
                pass
            raise
        self._run_statement(
            f"RELEASE SAVEPOINT {savepoint}" if joined else "COMMIT"
        )

    def _assert_batch_columns(
        self, table: str, operations: list[RowOperation]
    ) -> None:
        """One catalog lookup for the batch: every column named by any
        operation has to be one the table actually has. Cached, so a
        second Save on the same table costs none at all."""
        used: set[str] = set()
        for op in operations:
            used |= set(op.pk_values)
            if op.column:
                used.add(op.column)
        if not self.catalog_columns(table) and not self.catalog_columns(
            table, reload=True
        ):
            raise ConnectorError(f"No such table: {table}")
        self._assert_known_columns(table, used)

    def _apply_operation(
        self, table: str, op: RowOperation, index: int
    ) -> None:
        if op.kind != "update":
            raise BatchError(f"Unsupported operation: {op.kind}", index)
        if not op.pk_values:
            raise BatchError(
                "Refusing to update without a primary-key filter", index
            )
        marker = self.placeholder
        where = " AND ".join(
            f"{self.quote_ident(name)} = {marker}" for name in op.pk_values
        )
        sql = (
            f"UPDATE {self.quote_ident(table)} "
            f"SET {self.quote_ident(op.column)} = {marker} WHERE {where}"
        )
        try:
            rowcount = self._run_operation(
                sql, (op.value, *op.pk_values.values())
            )
        except ConnectorError as exc:
            raise BatchError(
                f"{describe_operation(op)}: {exc}", index
            ) from exc
        if rowcount != 1:
            raise BatchError(
                f"{describe_operation(op)}: expected to modify 1 row, "
                f"matched {rowcount}",
                index,
            )

    def _run_operation(self, sql: str, params: tuple) -> int:
        """Run one statement of a batch and report its rowcount.

        The rowcount is checked by the caller, inside the batch's
        transaction — deliberately not through the adapters' own
        expect_rowcount, which rolls back on its own and would take the
        user's transaction with it.
        """
        run = getattr(self, "_run", None)
        if run is None:
            raise ConnectorError("This connection cannot apply batched edits")
        return run(sql, params)[2]

    def _run_statement(self, sql: str) -> None:
        """A transaction-control statement (BEGIN, COMMIT, SAVEPOINT …)."""
        self.execute(sql)

    @abstractmethod
    def quote_ident(self, name: str) -> str: ...
