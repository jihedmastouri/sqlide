"""SQLite adapter (stdlib sqlite3)."""

from __future__ import annotations

import os
import sqlite3
import threading
from typing import Any

from sqlide.backend.db.base import (
    ColumnInfo,
    Connector,
    ConnectorError,
    ResultSet,
    TableInfo,
)


class SqliteConnector(Connector):
    """Catalog via sqlite_master and PRAGMA table_info().

    One connection is shared by all of the app's worker threads, so every
    statement is serialized behind a lock (hence check_same_thread=False).
    """

    def __init__(self, file_path: str) -> None:
        self.file_path = file_path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def connect(self) -> None:
        # sqlite3.connect() silently creates missing files; a typo'd path
        # should fail instead of opening an empty database.
        if not os.path.isfile(self.file_path):
            raise ConnectorError(f"No such database file: {self.file_path}")
        try:
            self._conn = sqlite3.connect(self.file_path, check_same_thread=False)
        except sqlite3.Error as exc:
            raise ConnectorError(str(exc)) from exc

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _run(
        self, sql: str, params: tuple = (), expect_rowcount: int | None = None
    ) -> tuple[list[str], list[tuple], int]:
        """Execute one statement; returns (columns, rows, rowcount).

        With expect_rowcount set, a mismatch rolls the statement back
        instead of committing it — the check must happen before commit
        or an over-broad UPDATE is already durable when it fails.
        """
        if self._conn is None:
            raise ConnectorError("Not connected")
        try:
            with self._lock:
                cur = self._conn.execute(sql, params)
                if cur.description is not None:
                    columns = [d[0] for d in cur.description]
                    rows = cur.fetchall()
                else:
                    columns, rows = [], []
                if expect_rowcount is not None and cur.rowcount != expect_rowcount:
                    self._conn.rollback()
                    raise ConnectorError(
                        f"Expected to modify {expect_rowcount} row(s), "
                        f"matched {cur.rowcount}; rolled back"
                    )
                self._conn.commit()
                return columns, rows, cur.rowcount
        except sqlite3.Error as exc:
            raise ConnectorError(str(exc)) from exc

    def list_tables(self) -> list[TableInfo]:
        _, rows, _ = self._run(
            "SELECT name, type FROM sqlite_master "
            "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        )
        return [TableInfo(name=name, kind=kind) for name, kind in rows]

    def list_columns(self, table: str) -> list[ColumnInfo]:
        _, rows, _ = self._run(f"PRAGMA table_info({self.quote_ident(table)})")
        return [
            ColumnInfo(
                name=name,
                type=ctype or "",
                is_pk=pk > 0,
                nullable=not notnull,
            )
            for _cid, name, ctype, notnull, _default, pk in rows
        ]

    def fetch_rows(self, table: str, offset: int = 0, limit: int = 500) -> ResultSet:
        self._assert_known_table(table)
        columns, rows, _ = self._run(
            f"SELECT * FROM {self.quote_ident(table)} LIMIT ? OFFSET ?",
            (max(limit, 0), max(offset, 0)),
        )
        return ResultSet(columns=columns, rows=rows)

    def execute(self, sql: str) -> ResultSet | int:
        columns, rows, rowcount = self._run(sql)
        if columns:
            return ResultSet(columns=columns, rows=rows)
        return max(rowcount, 0)

    def update_cell(
        self, table: str, pk_values: dict[str, Any], column: str, value: Any
    ) -> None:
        if not pk_values:
            raise ConnectorError("Refusing to update without a primary-key filter")
        # Only identifiers the catalog vouches for reach the SQL text.
        known = {c.name for c in self.list_columns(table)}
        if not known:
            raise ConnectorError(f"No such table: {table}")
        unknown = ({column} | set(pk_values)) - known
        if unknown:
            raise ConnectorError(
                f"Unknown column(s) for {table}: {', '.join(sorted(unknown))}"
            )
        where = " AND ".join(f"{self.quote_ident(k)} = ?" for k in pk_values)
        sql = (
            f"UPDATE {self.quote_ident(table)} "
            f"SET {self.quote_ident(column)} = ? WHERE {where}"
        )
        self._run(sql, (value, *pk_values.values()), expect_rowcount=1)

    def _assert_known_table(self, table: str) -> None:
        if table not in {t.name for t in self.list_tables()}:
            raise ConnectorError(f"No such table or view: {table}")

    def quote_ident(self, name: str) -> str:
        if not name:
            raise ConnectorError("Empty identifier")
        if "\x00" in name:
            raise ConnectorError("Identifier contains a NUL byte")
        return '"' + name.replace('"', '""') + '"'
