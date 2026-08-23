"""MySQL adapter (PyMySQL).

In MySQL a "database" and a "schema" are the same object, so the
query console's database dropdown doubles as the schema switcher;
every catalog query below scopes itself to DATABASE().
"""

from __future__ import annotations

import threading
from typing import Any

import pymysql
from pymysql.constants import CLIENT, SERVER_STATUS

from sqlide.backend.db.base import (
    ColumnInfo,
    Connector,
    ConnectorError,
    FilterCondition,
    FunctionInfo,
    IndexInfo,
    RelationInfo,
    ResultSet,
    SortSpec,
    TableInfo,
    TriggerInfo,
    TypeSpec,
    build_filter_clauses,
)

# Server-side catalogs that are not user databases.
_SYSTEM_SCHEMAS = ("information_schema", "performance_schema", "mysql", "sys")

_TEMPLATES = {
    "table": (
        "-- New table: adjust the name and columns, then Run.\n"
        "CREATE TABLE table_name (\n"
        "  id INT AUTO_INCREMENT PRIMARY KEY,\n"
        "  name VARCHAR(255) NOT NULL,\n"
        "  created_at DATETIME DEFAULT CURRENT_TIMESTAMP\n"
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
        "-- Add UNIQUE after CREATE for a unique index.\n"
        "CREATE INDEX index_name\n"
        "ON table_name (column_a);\n"
    ),
    "trigger": (
        "-- New trigger: adjust the name, timing and body, then Run.\n"
        "-- Timing: BEFORE | AFTER, on INSERT | UPDATE | DELETE.\n"
        "CREATE TRIGGER trigger_name BEFORE INSERT ON table_name\n"
        "FOR EACH ROW\n"
        "BEGIN\n"
        "  SET NEW.created_at = NOW();\n"
        "END;\n"
    ),
    "function": (
        "-- New function: adjust the name, arguments and body, then"
        " Run.\n"
        "-- DETERMINISTIC (or NO SQL / READS SQL DATA) is required\n"
        "-- when binary logging is enabled.\n"
        "CREATE FUNCTION function_name(a INT, b INT)\n"
        "RETURNS INT\n"
        "DETERMINISTIC\n"
        "RETURN a + b;\n"
    ),
    "procedure": (
        "-- New procedure: adjust the name, arguments and body, then"
        " Run.\n"
        "CREATE PROCEDURE procedure_name(IN a INT)\n"
        "BEGIN\n"
        "  SELECT a;\n"
        "END;\n"
    ),
    "event": (
        "-- New scheduled event: adjust the name, schedule and body,\n"
        "-- then Run. Needs the event scheduler"
        " (SET GLOBAL event_scheduler = ON).\n"
        "CREATE EVENT event_name\n"
        "ON SCHEDULE EVERY 1 DAY\n"
        "DO\n"
        "  DELETE FROM table_name WHERE created_at < NOW() - INTERVAL 30"
        " DAY;\n"
    ),
}


class MysqlConnector(Connector):
    """Catalog queries via information_schema. Identifiers quoted with
    backticks. LIMIT/OFFSET pagination.

    One connection is shared by all of the app's worker threads, so
    every statement is serialized behind a lock (like the SQLite
    adapter). autocommit is on: statements commit unless the user runs
    an explicit BEGIN — matching SQLite's isolation_level=None setup.
    """

    # SHOW CREATE TABLE — which is what get_ddl() hands the definition
    # tab — writes every KEY inline, so a rebuild's new CREATE already
    # declares the indexes and must not also replay them.
    ddl_declares_indexes = True

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
        self._conn: pymysql.connections.Connection | None = None
        self._tunnel = None  # sqlide.backend.ssh.SshTunnel when active
        self._lock = threading.Lock()

    def _ssl_kwargs(self) -> dict:
        """pymysql keyword args for the profile's SSL settings."""
        if not self.ssl:
            return {}
        mode = self.ssl.get("mode", "")
        if mode == "disable":
            return {"ssl_disabled": True}
        kwargs: dict = {}
        if self.ssl.get("ca"):
            kwargs["ssl_ca"] = self.ssl["ca"]
        if self.ssl.get("cert"):
            kwargs["ssl_cert"] = self.ssl["cert"]
        if self.ssl.get("key"):
            kwargs["ssl_key"] = self.ssl["key"]
        if mode in ("verify-ca", "verify-full"):
            kwargs["ssl_verify_cert"] = True
        if mode == "verify-full":
            kwargs["ssl_verify_identity"] = True
        if mode == "require" and not kwargs:
            # Force TLS on without certificate checks: a truthy ssl
            # dict makes pymysql build a default (non-verifying) context.
            kwargs["ssl"] = {"ca": None}
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
            self._conn = pymysql.connect(
                host=host,
                port=port,
                user=self.user,
                password=self.password,
                database=self.database or None,
                autocommit=True,
                # FOUND_ROWS: UPDATE rowcounts report matched rows, not
                # changed rows — otherwise update_cell's expect_rowcount
                # check fails when a cell is set to its current value.
                client_flag=CLIENT.FOUND_ROWS,
                **self._ssl_kwargs(),
            )
        except pymysql.Error as exc:
            self._stop_tunnel()
            raise ConnectorError(_message(exc)) from exc

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except pymysql.Error:
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
                    self._conn.begin()
                try:
                    with self._conn.cursor() as cur:
                        cur.execute(sql, params or None)
                        if cur.description is not None:
                            columns = [d[0] for d in cur.description]
                            rows = list(cur.fetchall())
                        else:
                            columns, rows = [], []
                        if (
                            expect_rowcount is not None
                            and cur.rowcount != expect_rowcount
                        ):
                            self._conn.rollback()
                            raise ConnectorError(
                                f"Expected to modify {expect_rowcount} "
                                f"row(s), matched {cur.rowcount}; rolled back"
                            )
                        rowcount = cur.rowcount
                except pymysql.Error:
                    if own_tx:
                        self._conn.rollback()
                    raise
                if own_tx and self._in_tx():
                    self._conn.commit()
                return columns, rows, rowcount
        except pymysql.Error as exc:
            raise ConnectorError(_message(exc)) from exc

    def _in_tx(self) -> bool:
        return bool(
            self._conn.server_status & SERVER_STATUS.SERVER_STATUS_IN_TRANS
        )

    def in_transaction(self) -> bool:
        return self._conn is not None and self._in_tx()

    def rollback(self) -> None:
        if self._conn is None:
            return
        try:
            with self._lock:
                self._conn.rollback()
        except pymysql.Error as exc:
            raise ConnectorError(_message(exc)) from exc

    def list_databases(self) -> list[str]:
        _, rows, _ = self._run("SHOW DATABASES")
        return sorted(
            name for (name,) in rows if name not in _SYSTEM_SCHEMAS
        )

    def list_tables(self) -> list[TableInfo]:
        _, rows, _ = self._run(
            "SELECT table_name, table_type FROM information_schema.tables "
            "WHERE table_schema = DATABASE() ORDER BY table_name"
        )
        return [
            TableInfo(
                name=name,
                kind="view" if table_type == "VIEW" else "table",
            )
            for name, table_type in rows
        ]

    def list_columns(self, table: str) -> list[ColumnInfo]:
        _, rows, _ = self._run(
            "SELECT column_name, column_type, is_nullable, column_key "
            "FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = %s "
            "ORDER BY ordinal_position",
            (table,),
        )
        return [
            ColumnInfo(
                name=name,
                type=ctype or "",
                is_pk=key == "PRI",
                nullable=nullable == "YES",
            )
            for name, ctype, nullable, key in rows
        ]

    def list_functions(self) -> list[FunctionInfo]:
        _, rows, _ = self._run(
            "SELECT routine_name FROM information_schema.routines "
            "WHERE routine_schema = DATABASE() ORDER BY routine_name"
        )
        return [FunctionInfo(name=name) for (name,) in rows]

    def list_relations(self) -> list[RelationInfo]:
        _, rows, _ = self._run(
            "SELECT table_name, column_name, "
            "referenced_table_name, referenced_column_name "
            "FROM information_schema.key_column_usage "
            "WHERE table_schema = DATABASE() "
            "AND referenced_table_name IS NOT NULL "
            "ORDER BY table_name, column_name"
        )
        return [
            RelationInfo(
                table=table, column=column,
                ref_table=ref_table, ref_column=ref_column or "",
            )
            for table, column, ref_table, ref_column in rows
        ]

    def list_indexes(self) -> list[IndexInfo]:
        # MySQL has no SHOW CREATE INDEX, so the DDL is synthesized from
        # statistics: one row per index once GROUP_CONCAT folds its
        # columns back together in definition order. PRIMARY is dropped
        # through ALTER TABLE, not DROP INDEX, so it stays out.
        _, rows, _ = self._run(
            "SELECT index_name, table_name, MIN(non_unique), "
            "GROUP_CONCAT(column_name ORDER BY seq_in_index) "
            "FROM information_schema.statistics "
            "WHERE table_schema = DATABASE() AND index_name <> 'PRIMARY' "
            "GROUP BY index_name, table_name "
            "ORDER BY index_name"
        )
        return [
            IndexInfo(
                name=name,
                table=table,
                ddl=(
                    f"CREATE {'' if non_unique else 'UNIQUE '}INDEX "
                    f"{self.quote_ident(name)} ON {self.quote_ident(table)} "
                    f"({columns})"
                ),
            )
            for name, table, non_unique, columns in rows
        ]

    def list_triggers(self) -> list[TriggerInfo]:
        # The CREATE is assembled from the catalog rather than taken
        # from SHOW CREATE TRIGGER: that form carries a DEFINER clause,
        # which only a user holding SET_USER_ID (or SUPER) can replay.
        # MySQL has row triggers and no others, so FOR EACH ROW is not
        # a guess.
        _, rows, _ = self._run(
            "SELECT trigger_name, event_object_table, action_timing, "
            "event_manipulation, action_statement "
            "FROM information_schema.triggers "
            "WHERE trigger_schema = DATABASE() ORDER BY trigger_name"
        )
        return [
            TriggerInfo(
                name=name,
                table=table,
                ddl=(
                    f"CREATE TRIGGER {self.quote_ident(name)} "
                    f"{timing} {event} ON {self.quote_ident(table)} "
                    f"FOR EACH ROW {body}"
                ),
            )
            for name, table, timing, event, body in rows
        ]

    def list_events(self) -> list[str]:
        _, rows, _ = self._run(
            "SELECT event_name FROM information_schema.events "
            "WHERE event_schema = DATABASE() ORDER BY event_name"
        )
        return [name for (name,) in rows]

    def ddl_kinds(self) -> tuple[str, ...]:
        return (
            "table", "view", "index", "trigger", "function", "procedure",
            "event",
        )

    def drop_sql(
        self, kind: str, name: str, table: str = "", cascade: bool = False
    ) -> str:
        if kind == "index":
            # MySQL has no bare DROP INDEX; it needs the owning table.
            if not table:
                raise ConnectorError(
                    f"Cannot drop index {name}: owning table unknown"
                )
            return (
                f"DROP INDEX {self.quote_ident(name)} "
                f"ON {self.quote_ident(table)}"
            )
        return super().drop_sql(kind, name, table=table, cascade=cascade)

    def create_template(self, kind: str) -> str:
        return _TEMPLATES.get(kind, "")

    def column_type_specs(self) -> list[TypeSpec]:
        return [
            TypeSpec("INT"),
            TypeSpec("BIGINT"),
            TypeSpec("SMALLINT"),
            TypeSpec("MEDIUMINT"),
            TypeSpec("TINYINT", ("display width",)),
            TypeSpec("BOOLEAN", note="synonym for TINYINT(1)"),
            TypeSpec("DECIMAL", ("precision", "scale"), ("10", "2")),
            TypeSpec("FLOAT"),
            TypeSpec("DOUBLE"),
            TypeSpec("BIT", ("bits",), ("1",)),
            TypeSpec("VARCHAR", ("length",), ("255",), "length required"),
            TypeSpec("CHAR", ("length",), ("1",)),
            TypeSpec("TEXT", note="up to 64 KiB"),
            TypeSpec("MEDIUMTEXT", note="up to 16 MiB"),
            TypeSpec("LONGTEXT", note="up to 4 GiB"),
            TypeSpec("TINYTEXT", note="up to 255 bytes"),
            TypeSpec("JSON"),
            TypeSpec(
                "ENUM", ("values",), ("'a', 'b'",), "quoted, comma separated",
            ),
            TypeSpec(
                "SET", ("values",), ("'a', 'b'",), "quoted, comma separated",
            ),
            TypeSpec("DATE"),
            TypeSpec("DATETIME", ("fractional digits",)),
            TypeSpec("TIMESTAMP", ("fractional digits",)),
            TypeSpec("TIME", ("fractional digits",)),
            TypeSpec("YEAR"),
            TypeSpec("BINARY", ("length",), ("16",)),
            TypeSpec("VARBINARY", ("length",), ("255",), "length required"),
            TypeSpec("BLOB"),
            TypeSpec("MEDIUMBLOB"),
            TypeSpec("LONGBLOB"),
        ]

    def get_ddl(self, name: str) -> str:
        # SHOW CREATE TABLE covers views too (the DDL is always the
        # second column); stored routines and triggers need their own
        # SHOW forms, which all put the statement in the third.
        for shape in ("TABLE", "FUNCTION", "PROCEDURE", "TRIGGER"):
            try:
                _, rows, _ = self._run(
                    f"SHOW CREATE {shape} {self.quote_ident(name)}"
                )
            except ConnectorError:
                continue
            if rows and len(rows[0]) > 1:
                ddl = rows[0][2] if shape != "TABLE" else rows[0][1]
                return ddl or ""
        return ""

    def schema_ddl(self) -> list[str]:
        """The generic walk, plus triggers, between two
        FOREIGN_KEY_CHECKS statements.

        MySQL needs no separate CREATE INDEX pass — SHOW CREATE TABLE
        writes a table's keys and foreign keys inside the statement —
        but that is also why the script has to turn the checks off
        first: the references are baked into each CREATE TABLE, and no
        ordering of the tables satisfies a pair that reference each
        other. (PostgreSQL's adapter solves the same problem the other
        way, by adding its foreign keys afterwards; MySQL cannot,
        because it does not hand them over separately.) This is what
        mysqldump writes at the top of its files, for the same reason.

        Its triggers also live outside list_functions() — that is
        information_schema.routines, so stored routines only — and a
        schema without them is not the schema.
        """
        statements = super().schema_ddl() + self.ddl_for(
            [t.name for t in self.list_triggers()]
        )
        if not statements:
            return []
        return [
            "SET FOREIGN_KEY_CHECKS = 0",
            *statements,
            "SET FOREIGN_KEY_CHECKS = 1",
        ]

    # DDL commits implicitly here, so a rebuild that fails half way
    # cannot be rolled back: it would leave the table renamed to its
    # backup with the rows half copied. MODIFY/ADD/DROP COLUMN do the
    # job in place instead.
    supports_table_rebuild = False
    identifier_max_length = 64

    def modify_column_sql(self, table: str, column: ColumnInfo) -> str:
        null = "NULL" if column.nullable else "NOT NULL"
        return (
            f"ALTER TABLE {self.quote_ident(table)} "
            f"MODIFY COLUMN {self.quote_ident(column.name)} "
            f"{column.type} {null}"
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
        return "`" + name.replace("`", "``") + "`"


def _message(exc: pymysql.Error) -> str:
    # pymysql errors carry (errno, message) args; the message alone
    # reads better in a toast.
    if len(exc.args) == 2 and isinstance(exc.args[1], str):
        return exc.args[1]
    return str(exc)
