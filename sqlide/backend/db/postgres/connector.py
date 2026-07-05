"""PostgreSQL adapter (psycopg v3).

Catalog queries go through pg_catalog / information_schema, scoped to
the schemas on the connection's search_path (excluding pg_catalog and
information_schema) so bare table names resolve the same way a plain
SELECT would. Identifiers are double-quoted; pagination is LIMIT/OFFSET.

One connection is shared by all of the app's worker threads, so every
statement is serialized behind a lock (like the SQLite and MySQL
adapters). autocommit is on: statements commit unless the user runs an
explicit BEGIN — matching the other adapters' isolation setup.

Programmable objects (the sidebar's Functions category and the
editable definition tab) cover all of PostgreSQL's: PL/pgSQL (and any
other language) functions, procedures and triggers. list_functions()
returns their names; get_ddl() reconstructs a runnable CREATE via
pg_get_functiondef / pg_get_triggerdef, so editing and re-saving a
PL/pgSQL body round-trips through the definition tab.
"""

from __future__ import annotations

import threading
from typing import Any

import psycopg
from psycopg.pq import TransactionStatus

from sqlide.backend.db.base import (
    ColumnInfo,
    Connector,
    ConnectorError,
    FilterCondition,
    FunctionInfo,
    RelationInfo,
    ResultSet,
    SortSpec,
    TableInfo,
    build_filter_clauses,
)

# Schemas visible on the search_path but not user objects.
_SYSTEM_SCHEMAS = ("pg_catalog", "information_schema")

# The subset of the search_path we treat as "the user's schemas": the
# real (non-system) entries, e.g. public. Bare names in the app resolve
# against exactly these.
_USER_SCHEMAS = (
    "SELECT nspname FROM unnest(current_schemas(false)) AS nspname "
    "WHERE nspname NOT IN ('pg_catalog', 'information_schema')"
)


class PostgresConnector(Connector):
    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        database: str,
        ssl: dict | None = None,
        ssh: dict | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.database = database
        self.ssl = ssl
        self.ssh = ssh
        self._conn: psycopg.Connection | None = None
        self._tunnel = None  # sqlide.backend.ssh.SshTunnel when active
        self._lock = threading.Lock()

    def _ssl_kwargs(self) -> dict:
        """psycopg connect kwargs for the profile's SSL settings."""
        if not self.ssl:
            return {}
        kwargs: dict = {}
        mode = self.ssl.get("mode", "")
        if mode:
            kwargs["sslmode"] = mode
        if self.ssl.get("ca"):
            kwargs["sslrootcert"] = self.ssl["ca"]
        if self.ssl.get("cert"):
            kwargs["sslcert"] = self.ssl["cert"]
        if self.ssl.get("key"):
            kwargs["sslkey"] = self.ssl["key"]
        return kwargs

    def connect(self) -> None:
        host, port = self.host, self.port
        if self.ssh:
            from sqlide.backend.ssh import SshTunnel

            self._tunnel = SshTunnel(
                host=self.ssh.get("host", ""),
                port=self.ssh.get("port", 22),
                user=self.ssh.get("user", ""),
                password=self.ssh.get("password", ""),
                key_path=self.ssh.get("key_path", ""),
                remote_host=self.host,
                remote_port=self.port,
            )
            try:
                port = self._tunnel.start()
            except Exception:
                self._tunnel = None
                raise
            host = "127.0.0.1"
        try:
            self._conn = psycopg.connect(
                host=host,
                port=port,
                user=self.user,
                password=self.password,
                dbname=self.database or None,
                autocommit=True,
                connect_timeout=10,
                **self._ssl_kwargs(),
            )
        except psycopg.Error as exc:
            self._stop_tunnel()
            raise ConnectorError(_message(exc)) from exc

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except psycopg.Error:
                pass
            self._conn = None
        self._stop_tunnel()

    def _stop_tunnel(self) -> None:
        if self._tunnel is not None:
            try:
                self._tunnel.stop()
            except Exception:
                pass
            self._tunnel = None

    def _run(
        self, sql: str, params: tuple = (), expect_rowcount: int | None = None
    ) -> tuple[list[str], list[tuple], int]:
        """Execute one statement; returns (columns, rows, rowcount).

        With expect_rowcount set, the statement runs in its own
        transaction (unless the user already opened one) so a mismatch
        rolls back before the change is durable.
        """
        if self._conn is None:
            raise ConnectorError("Not connected")
        try:
            with self._lock:
                own_tx = expect_rowcount is not None and not self._in_tx()
                if own_tx:
                    self._conn.execute("BEGIN")
                try:
                    with self._conn.cursor() as cur:
                        cur.execute(sql, params or None)
                        if cur.description is not None:
                            columns = [d.name for d in cur.description]
                            rows = list(cur.fetchall())
                        else:
                            columns, rows = [], []
                        if (
                            expect_rowcount is not None
                            and cur.rowcount != expect_rowcount
                        ):
                            self._conn.execute("ROLLBACK")
                            raise ConnectorError(
                                f"Expected to modify {expect_rowcount} "
                                f"row(s), matched {cur.rowcount}; rolled back"
                            )
                        rowcount = cur.rowcount
                except psycopg.Error:
                    if own_tx and self._in_tx():
                        self._conn.execute("ROLLBACK")
                    raise
                if own_tx and self._in_tx():
                    self._conn.execute("COMMIT")
                return columns, rows, rowcount
        except psycopg.Error as exc:
            raise ConnectorError(_message(exc)) from exc

    def _in_tx(self) -> bool:
        # INTRANS: an explicit BEGIN block is open. INERROR: a block is
        # open but a statement in it failed (still needs ROLLBACK).
        return self._conn.info.transaction_status in (
            TransactionStatus.INTRANS,
            TransactionStatus.INERROR,
        )

    def in_transaction(self) -> bool:
        return self._conn is not None and self._in_tx()

    def rollback(self) -> None:
        if self._conn is None:
            return
        try:
            with self._lock:
                self._conn.execute("ROLLBACK")
        except psycopg.Error as exc:
            raise ConnectorError(_message(exc)) from exc

    def list_databases(self) -> list[str]:
        _, rows, _ = self._run(
            "SELECT datname FROM pg_database "
            "WHERE datistemplate = false AND datallowconn "
            "ORDER BY datname"
        )
        return [name for (name,) in rows]

    def list_tables(self) -> list[TableInfo]:
        # relkind: r ordinary table, p partitioned table, v view,
        # m materialized view, f foreign table.
        _, rows, _ = self._run(
            "SELECT c.relname, c.relkind "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f') "
            f"AND n.nspname IN ({_USER_SCHEMAS}) "
            "ORDER BY c.relname"
        )
        return [
            TableInfo(
                name=name,
                kind="view" if relkind in ("v", "m") else "table",
            )
            for name, relkind in rows
        ]

    def list_columns(self, table: str) -> list[ColumnInfo]:
        _, rows, _ = self._run(
            "SELECT a.attname, format_type(a.atttypid, a.atttypmod), "
            "a.attnotnull, "
            "COALESCE(bool_or(ct.contype = 'p'), false) AS is_pk "
            "FROM pg_attribute a "
            "JOIN pg_class c ON c.oid = a.attrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "LEFT JOIN pg_constraint ct ON ct.conrelid = c.oid "
            "AND ct.contype = 'p' AND a.attnum = ANY(ct.conkey) "
            "WHERE c.relname = %s AND a.attnum > 0 AND NOT a.attisdropped "
            f"AND n.nspname IN ({_USER_SCHEMAS}) "
            "GROUP BY a.attname, a.atttypid, a.atttypmod, a.attnotnull, "
            "a.attnum "
            "ORDER BY a.attnum",
            (table,),
        )
        return [
            ColumnInfo(
                name=name,
                type=ctype or "",
                is_pk=is_pk,
                nullable=not notnull,
            )
            for name, ctype, notnull, is_pk in rows
        ]

    def list_functions(self) -> list[FunctionInfo]:
        """Functions and procedures (any language, PL/pgSQL included)
        plus triggers — everything the definition tab can edit."""
        _, rows, _ = self._run(
            "SELECT p.proname AS name "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            f"WHERE n.nspname IN ({_USER_SCHEMAS}) "
            "UNION "
            "SELECT tgname AS name FROM pg_trigger "
            "WHERE NOT tgisinternal "
            "ORDER BY name"
        )
        return [FunctionInfo(name=name) for (name,) in rows]

    def list_relations(self) -> list[RelationInfo]:
        _, rows, _ = self._run(
            "SELECT tc.table_name, kcu.column_name, "
            "ccu.table_name AS ref_table, ccu.column_name AS ref_column "
            "FROM information_schema.table_constraints tc "
            "JOIN information_schema.key_column_usage kcu "
            "ON kcu.constraint_name = tc.constraint_name "
            "AND kcu.constraint_schema = tc.constraint_schema "
            "JOIN information_schema.constraint_column_usage ccu "
            "ON ccu.constraint_name = tc.constraint_name "
            "AND ccu.constraint_schema = tc.constraint_schema "
            "WHERE tc.constraint_type = 'FOREIGN KEY' "
            f"AND tc.table_schema IN ({_USER_SCHEMAS}) "
            "ORDER BY tc.table_name, kcu.column_name"
        )
        return [
            RelationInfo(
                table=table, column=column,
                ref_table=ref_table, ref_column=ref_column or "",
            )
            for table, column, ref_table, ref_column in rows
        ]

    def get_ddl(self, name: str) -> str:
        """CREATE statement for a table, view, function/procedure or
        trigger named `name` (the sidebar preview and definition tab)."""
        # Relations first: to_regclass resolves against the search_path
        # and returns NULL rather than erroring on a non-relation name.
        _, rows, _ = self._run(
            "SELECT c.relkind, c.oid FROM pg_class c "
            "WHERE c.oid = to_regclass(%s)",
            (name,),
        )
        if rows:
            relkind, oid = rows[0]
            if relkind in ("v", "m"):
                _, vrows, _ = self._run(
                    "SELECT pg_get_viewdef(%s, true)", (oid,)
                )
                body = (vrows[0][0] or "").rstrip() if vrows else ""
                verb = "MATERIALIZED VIEW" if relkind == "m" else "VIEW"
                return (
                    f"CREATE {verb} {self.quote_ident(name)} AS\n{body}"
                    if body else ""
                )
            return self._table_ddl(name)

        # Function or procedure (pg_get_functiondef emits CREATE OR
        # REPLACE ... with the full body, language and all).
        _, frows, _ = self._run(
            "SELECT pg_get_functiondef(p.oid) "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            f"WHERE p.proname = %s AND n.nspname IN ({_USER_SCHEMAS}) "
            "LIMIT 1",
            (name,),
        )
        if frows and frows[0][0]:
            return frows[0][0]

        # Trigger.
        _, trows, _ = self._run(
            "SELECT pg_get_triggerdef(oid, true) FROM pg_trigger "
            "WHERE tgname = %s AND NOT tgisinternal LIMIT 1",
            (name,),
        )
        if trows and trows[0][0]:
            return trows[0][0]
        return ""

    def _table_ddl(self, table: str) -> str:
        """Synthesize a CREATE TABLE from the catalog (Postgres has no
        pg_get_tabledef): columns with types, NOT NULL, defaults, and
        the primary key."""
        _, rows, _ = self._run(
            "SELECT a.attname, format_type(a.atttypid, a.atttypmod), "
            "a.attnotnull, pg_get_expr(ad.adbin, ad.adrelid) AS default "
            "FROM pg_attribute a "
            "JOIN pg_class c ON c.oid = a.attrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "LEFT JOIN pg_attrdef ad ON ad.adrelid = a.attrelid "
            "AND ad.adnum = a.attnum "
            "WHERE c.relname = %s AND a.attnum > 0 AND NOT a.attisdropped "
            f"AND n.nspname IN ({_USER_SCHEMAS}) "
            "ORDER BY a.attnum",
            (table,),
        )
        if not rows:
            return ""
        defs = []
        for attname, ctype, notnull, default in rows:
            line = f"  {self.quote_ident(attname)} {ctype}"
            if default is not None:
                line += f" DEFAULT {default}"
            if notnull:
                line += " NOT NULL"
            defs.append(line)
        pks = [c.name for c in self.list_columns(table) if c.is_pk]
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

    def drop_function_sql(self, name: str) -> str:
        """DROP for a stored object so an edited CREATE can be re-run.

        Functions and procedures need none — pg_get_functiondef emits
        CREATE OR REPLACE, which replaces in place. Triggers have no
        OR REPLACE before PG 14, so they get an explicit DROP … ON
        table (the table is looked up from the catalog)."""
        _, rows, _ = self._run(
            "SELECT c.relname FROM pg_trigger t "
            "JOIN pg_class c ON c.oid = t.tgrelid "
            "WHERE t.tgname = %s AND NOT t.tgisinternal LIMIT 1",
            (name,),
        )
        if rows:
            table = rows[0][0]
            return (
                f"DROP TRIGGER IF EXISTS {self.quote_ident(name)} "
                f"ON {self.quote_ident(table)}"
            )
        return ""

    def modify_column_sql(self, table: str, column: ColumnInfo) -> str:
        # Postgres splits type and nullability into separate ALTER
        # COLUMN actions; both ride on one ALTER TABLE.
        null_action = (
            "DROP NOT NULL" if column.nullable else "SET NOT NULL"
        )
        return (
            f"ALTER TABLE {self.quote_ident(table)} "
            f"ALTER COLUMN {self.quote_ident(column.name)} "
            f"TYPE {column.type}, "
            f"ALTER COLUMN {self.quote_ident(column.name)} {null_action}"
        )

    def fetch_rows(
        self,
        table: str,
        offset: int = 0,
        limit: int = 500,
        filters: list[FilterCondition] | None = None,
        order_by: list[SortSpec] | None = None,
    ) -> ResultSet:
        self._assert_known_table(table)
        self._assert_filter_columns(table, filters, order_by)
        where, order, params = build_filter_clauses(
            filters, order_by, self.quote_ident, placeholder="%s"
        )
        columns, rows, _ = self._run(
            f"SELECT * FROM {self.quote_ident(table)}{where}{order} "
            "LIMIT %s OFFSET %s",
            (*params, max(limit, 0), max(offset, 0)),
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
            raise ConnectorError(
                "Refusing to update without a primary-key filter"
            )
        # Only identifiers the catalog vouches for reach the SQL text.
        known = {c.name for c in self.list_columns(table)}
        if not known:
            raise ConnectorError(f"No such table: {table}")
        unknown = ({column} | set(pk_values)) - known
        if unknown:
            raise ConnectorError(
                f"Unknown column(s) for {table}: {', '.join(sorted(unknown))}"
            )
        where = " AND ".join(f"{self.quote_ident(k)} = %s" for k in pk_values)
        sql = (
            f"UPDATE {self.quote_ident(table)} "
            f"SET {self.quote_ident(column)} = %s WHERE {where}"
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


def _message(exc: psycopg.Error) -> str:
    # psycopg wraps the server's primary message on the diag object;
    # it reads better in a toast than the full exception repr.
    diag = getattr(exc, "diag", None)
    primary = getattr(diag, "message_primary", None) if diag else None
    return primary or str(exc).strip()
