"""Generic connector interface shared by all database adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from secrets import token_hex
from typing import Any


@dataclass(frozen=True)
class TableInfo:
    name: str
    kind: str  # "table" | "view"


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    type: str
    is_pk: bool = False
    nullable: bool = True


@dataclass(frozen=True)
class FunctionInfo:
    name: str


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
    """One foreign-key column: table.column references ref_table.ref_column."""

    table: str
    column: str
    ref_table: str
    ref_column: str


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

    def __len__(self) -> int:
        return len(self.rows)


class ConnectorError(Exception):
    """Raised by adapters for any database failure, wrapping the driver error."""


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

    def list_databases(self) -> list[str]:
        """Databases reachable through this connection, sorted by name,
        for the query console's database switcher.

        Concrete default (not abstract): single-database connectors —
        SQLite, where one file is one database — need no override.
        """
        return []

    def list_schemas(self) -> list[str]:
        """Schemas inside the connected database, sorted by name, for
        the console's schema switcher.

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

    def list_functions(self) -> list[FunctionInfo]:
        """Stored functions in the connected database, sorted by name.

        Concrete default (not abstract) so adapters without a function
        catalog — SQLite, the unimplemented stubs — need no override.
        """
        return []

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

        Structure only — no INSERTs. This is what "save this schema
        for later" captures (backend/schemas.py) and what the sidebar
        offers as a whole-database script.

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
    def execute(self, sql: str) -> ResultSet | int:
        """Run arbitrary SQL. Returns a ResultSet for row-returning
        statements, otherwise the affected row count."""

    @abstractmethod
    def update_cell(
        self,
        table: str,
        pk_values: dict[str, Any],
        column: str,
        value: Any,
    ) -> None:
        """UPDATE a single cell, addressing the row by its primary key."""

    @abstractmethod
    def quote_ident(self, name: str) -> str: ...
