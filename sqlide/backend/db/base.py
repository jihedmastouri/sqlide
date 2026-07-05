"""Generic connector interface shared by all database adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
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

    def list_functions(self) -> list[FunctionInfo]:
        """Stored functions in the connected database, sorted by name.

        Concrete default (not abstract) so adapters without a function
        catalog — SQLite, the unimplemented stubs — need no override.
        """
        return []

    def get_ddl(self, name: str) -> str:
        """CREATE statement for a table or view, for the sidebar's
        hover preview. Empty string when unknown or unsupported.

        Concrete default (not abstract) so adapters without a DDL
        catalog need no override.
        """
        return ""

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
