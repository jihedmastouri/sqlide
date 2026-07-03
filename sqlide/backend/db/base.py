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


@dataclass
class ResultSet:
    columns: list[str]
    rows: list[tuple[Any, ...]] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.rows)


class ConnectorError(Exception):
    """Raised by adapters for any database failure, wrapping the driver error."""


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

    def list_functions(self) -> list[FunctionInfo]:
        """Stored functions in the connected database, sorted by name.

        Concrete default (not abstract) so adapters without a function
        catalog — SQLite, the unimplemented stubs — need no override.
        """
        return []

    @abstractmethod
    def fetch_rows(self, table: str, offset: int = 0, limit: int = 500) -> ResultSet: ...

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
