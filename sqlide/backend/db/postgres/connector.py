"""PostgreSQL adapter (psycopg v3) — milestone 7."""

from __future__ import annotations

from typing import Any

from sqlide.backend.db.base import ColumnInfo, Connector, ResultSet, TableInfo


class PostgresConnector(Connector):
    """Catalog queries via information_schema / pg_catalog (public schema
    in v1). Double-quoted identifiers. LIMIT/OFFSET pagination."""

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        database: str,
    ) -> None:
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.database = database
        self._conn = None

    def connect(self) -> None:
        raise NotImplementedError  # TODO: milestone 7

    def close(self) -> None:
        raise NotImplementedError

    def list_databases(self) -> list[str]:
        raise NotImplementedError  # TODO: pg_database, skip templates

    def list_tables(self) -> list[TableInfo]:
        raise NotImplementedError

    def list_columns(self, table: str) -> list[ColumnInfo]:
        raise NotImplementedError

    def fetch_rows(self, table: str, offset: int = 0, limit: int = 500) -> ResultSet:
        raise NotImplementedError

    def execute(self, sql: str) -> ResultSet | int:
        raise NotImplementedError

    def update_cell(
        self, table: str, pk_values: dict[str, Any], column: str, value: Any
    ) -> None:
        raise NotImplementedError

    def quote_ident(self, name: str) -> str:
        return '"' + name.replace('"', '""') + '"'
