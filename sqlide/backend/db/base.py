"""Generic connector interface shared by all database adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from secrets import token_hex
from typing import Any

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


@dataclass
class ResultSet:
    columns: list[str]
    rows: list[tuple[Any, ...]] = field(default_factory=list)
    # True when the adapter stopped fetching at the caller's max_rows
    # and the statement had more rows to give. The UI must say so:
    # a silently short result reads as the whole answer.
    truncated: bool = False

    def __len__(self) -> int:
        return len(self.rows)


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


class Connector(ABC):
    """Everything the UI knows about a database goes through this interface.

    Adapters own all dialect differences: identifier quoting, catalog
    queries, pagination syntax. Driver exceptions must be re-raised as
    ConnectorError with a readable message.
    """

    @abstractmethod
    def connect(self) -> None: ...

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
        return [r for r in self.list_relations() if r.ref_table == table]

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
    ) -> ResultSet: ...

    def _assert_filter_columns(
        self,
        table: str,
        filters: list[FilterCondition] | None,
        order_by: list[SortSpec] | None,
    ) -> None:
        """Reject filter/sort column names the catalog doesn't vouch for,
        so they never reach the SQL text. Skipped when neither is set —
        list_columns() can cost a catalog round trip."""
        if not filters and not order_by:
            return
        used = {f.column for f in filters or []} | {s.column for s in order_by or []}
        unknown = used - {c.name for c in self.list_columns(table)}
        if unknown:
            raise ConnectorError(
                f"Unknown column(s) for {table}: {', '.join(sorted(unknown))}"
            )

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
        operation has to be one the table actually has."""
        known = {c.name for c in self.list_columns(table)}
        if not known:
            raise ConnectorError(f"No such table: {table}")
        used: set[str] = set()
        for op in operations:
            used |= set(op.pk_values)
            if op.column:
                used.add(op.column)
        unknown = used - known
        if unknown:
            raise ConnectorError(
                f"Unknown column(s) for {table}: {', '.join(sorted(unknown))}"
            )

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
