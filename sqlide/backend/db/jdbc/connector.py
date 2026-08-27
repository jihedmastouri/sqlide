"""Generic JDBC adapter, bridged through JayDeBeApi + JPype (needs a JVM).

Works with any database that ships a JDBC driver jar: the profile supplies
the JDBC URL, the driver class name, and the path to the jar. Catalog
information comes from java.sql.DatabaseMetaData, so no per-database SQL
is needed — that is what makes this adapter generic.

Status: experimental (milestone 7). Caveats:
- Pagination is emulated by skipping rows client-side because there is no
  portable LIMIT/OFFSET syntax; large offsets are slow.
- JPype can only start one JVM per process, and worker threads attach to
  it implicitly. Keep an eye on thread-attachment issues.
"""

from __future__ import annotations

import threading
from typing import Any

from sqlide.backend.db.base import (
    ColumnInfo,
    Connector,
    ConnectorError,
    FilterCondition,
    PageCursor,
    PageQuery,
    ResultSet,
    SortSpec,
    TableInfo,
    build_filter_clauses,
    build_keyset_clause,
    inline_params,
)


class JdbcConnector(Connector):
    def __init__(
        self,
        url: str,
        driver_class: str,
        jar_path: str = "",
        user: str = "",
        password: str = "",
    ) -> None:
        self.url = url
        self.driver_class = driver_class
        self.jar_path = jar_path
        self.user = user
        self.password = password
        self._conn = None
        self._quote = '"'
        self._lock = threading.Lock()
        # The cursor currently executing, for cancel(). Set and cleared
        # by execute() only; every other caller leaves it alone.
        self._active_cursor = None

    def connect(self) -> None:
        self.invalidate_catalog()
        try:
            import jaydebeapi
        except ImportError as exc:
            raise ConnectorError(
                "JDBC support needs the 'jaydebeapi' package (and a Java "
                "runtime). Install with: pip install sqlide[jdbc]"
            ) from exc
        try:
            credentials = [self.user, self.password] if self.user else None
            self._conn = jaydebeapi.connect(
                self.driver_class,
                self.url,
                credentials,
                self.jar_path or None,
            )
            quote = self._meta().getIdentifierQuoteString()
            self._quote = str(quote).strip() or '"'
        except Exception as exc:
            raise ConnectorError(str(exc)) from exc

    def close(self) -> None:
        self.invalidate_catalog()
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # No reliable dialect knowledge: "New ▸" offers only the generic
    # base templates and Drop… is hidden entirely.
    supports_drop = False

    def _meta(self):
        """java.sql.DatabaseMetaData of the underlying JDBC connection."""
        if self._conn is None:
            raise ConnectorError("Not connected")
        return self._conn.jconn.getMetaData()

    def list_tables(self) -> list[TableInfo]:
        try:
            with self._lock:
                rs = self._meta().getTables(None, None, "%", ["TABLE", "VIEW"])
                tables = []
                while rs.next():
                    name = str(rs.getString(3))
                    kind = "view" if str(rs.getString(4)).upper() == "VIEW" else "table"
                    tables.append(TableInfo(name=name, kind=kind))
                rs.close()
            return sorted(tables, key=lambda t: t.name)
        except ConnectorError:
            raise
        except Exception as exc:
            raise ConnectorError(str(exc)) from exc

    def list_columns(self, table: str) -> list[ColumnInfo]:
        try:
            with self._lock:
                meta = self._meta()
                rs = meta.getPrimaryKeys(None, None, table)
                pk_names = set()
                while rs.next():
                    pk_names.add(str(rs.getString(4)))
                rs.close()

                rs = meta.getColumns(None, None, table, "%")
                columns = []
                while rs.next():
                    name = str(rs.getString(4))
                    columns.append(
                        ColumnInfo(
                            name=name,
                            type=str(rs.getString(6) or ""),
                            is_pk=name in pk_names,
                            nullable=int(rs.getInt(11)) != 0,
                        )
                    )
                rs.close()
            return columns
        except ConnectorError:
            raise
        except Exception as exc:
            raise ConnectorError(str(exc)) from exc

    def fetch_rows(
        self,
        table: str,
        offset: int = 0,
        limit: int = 500,
        filters: list[FilterCondition] | None = None,
        order_by: list[SortSpec] | None = None,
        cursor: PageCursor | None = None,
    ) -> ResultSet:
        # No portable LIMIT/OFFSET across JDBC dialects: read and discard
        # `offset` rows, then keep `limit`. A key comparison is portable,
        # though, so a keyset page starts at the cursor and skips
        # nothing — which is also what keeps the order stable (CORE-40).
        self._assert_filter_columns(table, filters, order_by)
        plan = self.paging_strategy(table, order_by)
        where, order, params = build_filter_clauses(
            filters, plan.order_by, self.quote_ident
        )
        keyset, key_params = build_keyset_clause(
            cursor if plan.keyset else None,
            plan.order_by,
            self.quote_ident,
        )
        skip = offset
        if keyset:
            where = f"{where} AND {keyset}" if where else f" WHERE {keyset}"
            params = [*params, *key_params]
            skip = 0
        sql = f"SELECT * FROM {self.quote_ident(table)}{where}{order}"
        columns, rows = self._query(sql, params=params, skip=skip, limit=limit)
        query = PageQuery(
            sql=sql,
            params=params,
            plan=plan,
            display=inline_params(sql, params),
        )
        return self._page_result(columns, rows, query)

    def execute(self, sql: str, max_rows: int | None = None) -> ResultSet | int:
        if self._conn is None:
            raise ConnectorError("Not connected")
        # One row past the cap tells truncated from a result that
        # happens to be exactly max_rows long.
        limit = max_rows + 1 if max_rows else None
        try:
            # Whether it succeeded or not: a DDL statement that failed
            # half way may still have changed the catalog.
            self._note_statement(sql)
            with self._lock:
                cur = self._conn.cursor()
                self._active_cursor = cur
                try:
                    cur.execute(sql)
                    if cur.description is not None:
                        columns = [d[0] for d in cur.description]
                        raw = (
                            cur.fetchmany(limit)
                            if limit is not None
                            else cur.fetchall()
                        )
                        rows = [tuple(r) for r in raw]
                        truncated = (
                            max_rows is not None and len(rows) > max_rows
                        )
                        return ResultSet(
                            columns=columns,
                            rows=rows[:max_rows] if truncated else rows,
                            truncated=truncated,
                        )
                    return max(cur.rowcount, 0)
                finally:
                    self._active_cursor = None
                    cur.close()
        except ConnectorError:
            raise
        except Exception as exc:
            raise ConnectorError(str(exc)) from exc

    supports_cancel = True

    def cancel(self) -> None:
        """java.sql.Statement.cancel() on the statement in flight.

        jaydebeapi keeps the JDBC statement it prepared on the cursor as
        `_prep`; there is no public accessor, so this reaches for it and
        says so plainly when a driver or a jaydebeapi version doesn't
        provide one. Whether the cancel is honoured at all is up to the
        JDBC driver — many ignore it. Not under self._lock: the thread
        being cancelled holds it.
        """
        cursor = self._active_cursor
        statement = getattr(cursor, "_prep", None) if cursor else None
        if statement is None:
            raise ConnectorError(
                "This JDBC driver does not expose a cancellable statement"
            )
        try:
            statement.cancel()
        except Exception as exc:
            raise ConnectorError(str(exc)) from exc

    def _run_operation(self, sql: str, params: tuple) -> int:
        """One statement of an apply_changes() batch. The generic
        implementation reaches for a `_run` this adapter does not have,
        so the cursor work is spelled out here."""
        if self._conn is None:
            raise ConnectorError("Not connected")
        try:
            with self._lock:
                cur = self._conn.cursor()
                try:
                    cur.execute(sql, params)
                    return cur.rowcount
                finally:
                    cur.close()
        except Exception as exc:
            raise ConnectorError(str(exc)) from exc

    def update_cell(
        self, table: str, pk_values: dict[str, Any], column: str, value: Any
    ) -> None:
        if self._conn is None:
            raise ConnectorError("Not connected")
        where = " AND ".join(f"{self.quote_ident(k)} = ?" for k in pk_values)
        sql = (
            f"UPDATE {self.quote_ident(table)} "
            f"SET {self.quote_ident(column)} = ? WHERE {where}"
        )
        try:
            with self._lock:
                cur = self._conn.cursor()
                try:
                    cur.execute(sql, (value, *pk_values.values()))
                finally:
                    cur.close()
        except Exception as exc:
            raise ConnectorError(str(exc)) from exc

    def _query(
        self,
        sql: str,
        params: list | None = None,
        skip: int = 0,
        limit: int | None = None,
    ) -> tuple[list[str], list[tuple]]:
        if self._conn is None:
            raise ConnectorError("Not connected")
        try:
            with self._lock:
                cur = self._conn.cursor()
                try:
                    if params:
                        cur.execute(sql, params)
                    else:
                        cur.execute(sql)
                    columns = [d[0] for d in cur.description or []]
                    if skip:
                        cur.fetchmany(skip)
                    raw = cur.fetchmany(limit) if limit else cur.fetchall()
                    return columns, [tuple(r) for r in raw]
                finally:
                    cur.close()
        except ConnectorError:
            raise
        except Exception as exc:
            raise ConnectorError(str(exc)) from exc

    def quote_ident(self, name: str) -> str:
        q = self._quote
        return q + name.replace(q, q + q) + q
