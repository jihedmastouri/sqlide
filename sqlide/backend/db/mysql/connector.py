"""MySQL adapter (PyMySQL).

In MySQL a "database" and a "schema" are the same object, so the
query console's database dropdown doubles as the schema switcher;
every catalog query below scopes itself to DATABASE().
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pymysql
from pymysql.constants import CLIENT, SERVER_STATUS

from sqlide.backend.db.base import (
    ColumnInfo,
    Connector,
    ConnectorError,
    ConstraintInfo,
    FilterCondition,
    FunctionInfo,
    GrantScope,
    IndexInfo,
    ObjectSummary,
    PrivilegeInfo,
    RelationInfo,
    ResultSet,
    SortSpec,
    TableInfo,
    TableStats,
    TriggerInfo,
    TypeSpec,
    UserInfo,
    build_filter_clauses,
)
from sqlide.backend.settings import session_time_zone

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
        # Set at connect(): what a second, short-lived connection needs
        # to reach the same server, and the id of the session whose
        # statement cancel() has to KILL.
        self._connect_kwargs: dict = {}
        self._thread_id: int | None = None

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
        kwargs = dict(
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
        try:
            self._conn = pymysql.connect(**kwargs)
            self._connect_kwargs = kwargs
            self._thread_id = self._conn.thread_id()
        except pymysql.Error as exc:
            self._stop_tunnel()
            raise ConnectorError(_message(exc)) from exc
        self._apply_time_zone()

    def _apply_time_zone(self) -> None:
        """Pin the session's zone, so a TIMESTAMP reads the same
        against every server instead of following whichever time_zone
        the server happens to be configured with.

        Named zones only work where the server's mysql.time_zone
        tables are populated, which many installations skip; the
        fallback is the zone's current UTC offset, which MySQL always
        accepts. An offset does not follow a DST change, so a session
        left open across one shows timestamps an hour out until it is
        reconnected — still better than not knowing the zone at all.

        Best-effort: a server that rejects both keeps its own setting
        rather than failing the whole connection.
        """
        zone = session_time_zone()
        if zone is None or self._conn is None:
            return
        for value in (zone, _utc_offset(zone)):
            if not value:
                continue
            try:
                with self._conn.cursor() as cursor:
                    cursor.execute("SET time_zone = %s", (value,))
                return
            except pymysql.Error:
                continue

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
        self,
        sql: str,
        params: tuple = (),
        expect_rowcount: int | None = None,
        fetch_limit: int | None = None,
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
                            rows = list(
                                cur.fetchmany(fetch_limit)
                                if fetch_limit is not None
                                else cur.fetchall()
                            )
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

    # Accounts and privileges

    supports_users = True

    def list_users(self) -> list[UserInfo]:
        # mysql.user is readable by administrators only; a connection
        # that cannot read it still knows one account — its own — and
        # showing that beats an empty list that reads as "no accounts".
        try:
            _, rows, _ = self._run(
                "SELECT user, host, account_locked, plugin, "
                "password_expired, password_last_changed "
                "FROM mysql.user ORDER BY user, host"
            )
        except ConnectorError:
            try:
                _, rows, _ = self._run(
                    "SELECT user, host, 'N', '', 'N', NULL "
                    "FROM mysql.user ORDER BY user, host"
                )
            except ConnectorError:
                _, rows, _ = self._run(
                    "SELECT SUBSTRING_INDEX(CURRENT_USER(), '@', 1), "
                    "SUBSTRING_INDEX(CURRENT_USER(), '@', -1), "
                    "'N', '', 'N', NULL"
                )
        roles, memberships = self._role_edges()
        users = []
        for name, host, locked, plugin, expired, changed in rows:
            is_locked = (locked or "N") == "Y"
            is_expired = (expired or "N") == "Y"
            account = f"{name}@{host}"
            users.append(
                UserInfo(
                    name=name,
                    host=host,
                    detail="locked" if is_locked else "",
                    # MySQL 8 keeps roles in mysql.user like any other
                    # account; what makes one a role is that something
                    # was granted it (mysql.role_edges).
                    kind="role" if account in roles else "user",
                    can_login=not is_locked,
                    locked=is_locked,
                    plugin=plugin or "",
                    password_expiry=(
                        "expired"
                        if is_expired
                        else ("" if changed is None else str(changed)[:19])
                    ),
                    member_of=tuple(memberships.get(account, ())),
                )
            )
        return users

    def _role_edges(self) -> tuple[set[str], dict[str, list[str]]]:
        """The accounts that are used as roles, and which roles each
        account holds. mysql.role_edges arrived in 8.0 and is
        administrator-readable, so a server or a login without it
        costs these two columns and nothing else."""
        try:
            _, rows, _ = self._run(
                "SELECT from_user, from_host, to_user, to_host "
                "FROM mysql.role_edges"
            )
        except ConnectorError:
            return set(), {}
        roles: set[str] = set()
        memberships: dict[str, list[str]] = {}
        for from_user, from_host, to_user, to_host in rows:
            role = f"{from_user}@{from_host}"
            roles.add(role)
            memberships.setdefault(f"{to_user}@{to_host}", []).append(role)
        return roles, memberships

    def list_privileges(self, user: UserInfo) -> list[PrivilegeInfo]:
        # The information_schema privilege views name their grantee in
        # the same 'user'@'host' spelling account_ident() builds, so one
        # parameter matches all three levels.
        grantee = self.account_ident(user)
        privileges = []
        for sql, scope in (
            (
                "SELECT privilege_type, is_grantable "
                "FROM information_schema.user_privileges "
                "WHERE grantee = %s ORDER BY privilege_type",
                lambda row: "server",
            ),
            (
                "SELECT privilege_type, is_grantable, table_schema "
                "FROM information_schema.schema_privileges "
                "WHERE grantee = %s ORDER BY table_schema, privilege_type",
                lambda row: f"database {row[2]}",
            ),
            (
                "SELECT privilege_type, is_grantable, table_schema, table_name "
                "FROM information_schema.table_privileges "
                "WHERE grantee = %s "
                "ORDER BY table_schema, table_name, privilege_type",
                lambda row: f"table {row[2]}.{row[3]}",
            ),
            (
                # Column grants are their own rows here (mysql.columns_priv),
                # not the columns a table grant happens to cover, so the
                # permission editor can show them as the grants they are.
                "SELECT privilege_type, is_grantable, table_schema, "
                "table_name, column_name "
                "FROM information_schema.column_privileges "
                "WHERE grantee = %s "
                "ORDER BY table_schema, table_name, column_name, "
                "privilege_type",
                lambda row: f"column {row[2]}.{row[3]}.{row[4]}",
            ),
        ):
            _, rows, _ = self._run(sql, (grantee,))
            privileges += [
                PrivilegeInfo(
                    scope=scope(row),
                    privilege=row[0],
                    grantable=row[1] == "YES",
                )
                for row in rows
            ]
        return privileges

    def list_object_grants(self, kind: str, name: str) -> list[PrivilegeInfo]:
        """The table grants recorded on one object, scoped to the
        connected database. Only tables and views carry per-object
        grants here — a MySQL index or trigger is the table's."""
        if kind not in ("table", "view"):
            return []
        _, rows, _ = self._run(
            "SELECT grantee, privilege_type, is_grantable "
            "FROM information_schema.table_privileges "
            "WHERE table_schema = DATABASE() AND table_name = %s "
            "ORDER BY grantee, privilege_type",
            (name,),
        )
        return [
            PrivilegeInfo(
                scope=f"user {grantee}",
                privilege=privilege,
                grantable=grantable == "YES",
            )
            for grantee, privilege, grantable in rows
        ]

    def grant_scopes(self) -> list[GrantScope]:
        scopes = [GrantScope("Whole server", "*.*")]
        for database in self.list_databases():
            quoted = self.quote_ident(database)
            scopes.append(
                GrantScope(f"Database: {database}", f"{quoted}.*")
            )
        return scopes

    def privilege_names(self) -> tuple[str, ...]:
        return (
            "ALL PRIVILEGES", "SELECT", "INSERT", "UPDATE", "DELETE",
            "CREATE", "DROP", "ALTER", "INDEX", "REFERENCES",
            "CREATE VIEW", "SHOW VIEW", "CREATE ROUTINE", "ALTER ROUTINE",
            "EXECUTE", "TRIGGER", "EVENT", "LOCK TABLES",
            "CREATE TEMPORARY TABLES", "RELOAD", "PROCESS", "GRANT OPTION",
        )

    def account_ident(self, user: UserInfo) -> str:
        return f"{_quote_str(user.name)}@{_quote_str(user.host or '%')}"

    def create_user_sql(
        self, name: str, host: str = "", password: str = ""
    ) -> str:
        account = self.account_ident(UserInfo(name=name, host=host))
        sql = f"CREATE USER {account}"
        if password:
            sql += f" IDENTIFIED BY {_quote_str(password)}"
        return sql

    def drop_user_sql(self, user: UserInfo) -> str:
        return f"DROP USER {self.account_ident(user)}"

    def set_password_sql(self, user: UserInfo, password: str) -> str:
        return (
            f"ALTER USER {self.account_ident(user)} "
            f"IDENTIFIED BY {_quote_str(password)}"
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

    # Table properties (CORE-04). information_schema answers all of
    # these, and the table name always travels as a parameter.

    def table_stats(self, table: str) -> TableStats:
        _, rows, _ = self._run(
            "SELECT table_type, engine, "
            "data_length + index_length, table_rows, table_comment "
            "FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = %s",
            (table,),
        )
        if not rows:
            return TableStats()
        table_type, engine, size, estimate, comment = rows[0]
        return TableStats(
            kind="view" if table_type == "VIEW" else "table",
            engine=engine or "",
            size=_pretty_size(size),
            # A view has no rows of its own to estimate.
            rows="" if estimate is None or table_type == "VIEW"
                 else str(estimate),
            comment=comment or "",
        )

    def list_constraints(self, table: str) -> list[ConstraintInfo]:
        _, rows, _ = self._run(
            "SELECT tc.constraint_name, tc.constraint_type, "
            "GROUP_CONCAT(kcu.column_name "
            " ORDER BY kcu.ordinal_position) "
            "FROM information_schema.table_constraints tc "
            "LEFT JOIN information_schema.key_column_usage kcu "
            "ON kcu.constraint_schema = tc.constraint_schema "
            "AND kcu.constraint_name = tc.constraint_name "
            "AND kcu.table_name = tc.table_name "
            "WHERE tc.table_schema = DATABASE() AND tc.table_name = %s "
            "GROUP BY tc.constraint_name, tc.constraint_type "
            "ORDER BY tc.constraint_type, tc.constraint_name",
            (table,),
        )
        return [
            ConstraintInfo(
                name=name, kind=kind or "", table=table,
                columns=columns or "",
            )
            for name, kind, columns in rows
        ]

    def list_partitions(self, table: str) -> list[ObjectSummary]:
        _, rows, _ = self._run(
            "SELECT partition_name, partition_method, "
            "partition_expression, table_rows "
            "FROM information_schema.partitions "
            "WHERE table_schema = DATABASE() AND table_name = %s "
            "AND partition_name IS NOT NULL "
            "ORDER BY partition_ordinal_position",
            (table,),
        )
        return [
            ObjectSummary(
                name=name,
                detail=f"{method or ''} · {rows_estimate} rows".strip(" ·"),
                definition=expression or "",
            )
            for name, method, expression, rows_estimate in rows
        ]

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

    def execute(self, sql: str, max_rows: int | None = None) -> ResultSet | int:
        # One row past the cap: the extra is what tells truncated from
        # a result that happens to be exactly max_rows long.
        limit = max_rows + 1 if max_rows else None
        columns, rows, rowcount = self._run(sql, fetch_limit=limit)
        if columns:
            truncated = max_rows is not None and len(rows) > max_rows
            return ResultSet(
                columns=columns,
                rows=rows[:max_rows] if truncated else rows,
                truncated=truncated,
            )
        return max(rowcount, 0)

    supports_cancel = True

    def cancel(self) -> None:
        """KILL QUERY our own session from a second connection.

        MySQL has no out-of-band cancel: the only way to stop a running
        statement is another session telling the server to kill it. So
        this opens a throwaway connection (same credentials, through
        the same SSH tunnel if one is up — _connect_kwargs already
        points at the local end) and kills by session id. It must not
        take self._lock, which the statement being killed is holding.
        """
        thread_id = self._thread_id
        if self._conn is None or thread_id is None:
            return
        try:
            killer = pymysql.connect(**self._connect_kwargs)
        except pymysql.Error as exc:
            raise ConnectorError(
                f"Could not open a second connection to cancel: "
                f"{_message(exc)}"
            ) from exc
        try:
            with killer.cursor() as cur:
                cur.execute("KILL QUERY %s", (thread_id,))
        except pymysql.Error as exc:
            raise ConnectorError(_message(exc)) from exc
        finally:
            try:
                killer.close()
            except pymysql.Error:
                pass

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


def _quote_str(value: str) -> str:
    """A string literal for a statement the user is about to review —
    account names and passwords cannot be bound as parameters inside
    CREATE USER / GRANT. Backslash escapes as well as quotes: MySQL
    honors backslash escapes in literals by default."""
    if "\x00" in value:
        raise ConnectorError("Value contains a NUL byte")
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def _utc_offset(zone: str) -> str:
    """A zone name as the "+HH:MM" offset it is on right now, for a
    server without the named-zone tables. Already-an-offset input is
    handed back unchanged; an unknown name gives "" (nothing to try)."""
    if zone.startswith(("+", "-")):
        return zone
    try:
        delta = datetime.now(ZoneInfo(zone)).utcoffset()
    except (ZoneInfoNotFoundError, ValueError):
        return ""
    total = int((delta or timedelta(0)).total_seconds()) // 60
    sign = "-" if total < 0 else "+"
    hours, minutes = divmod(abs(total), 60)
    return f"{sign}{hours:02d}:{minutes:02d}"


def _message(exc: pymysql.Error) -> str:
    # pymysql errors carry (errno, message) args; the message alone
    # reads better in a toast.
    if len(exc.args) == 2 and isinstance(exc.args[1], str):
        return exc.args[1]
    return str(exc)


def _pretty_size(size) -> str:
    """Bytes as the catalog reports them, in the units a person reads."""
    if size is None:
        return ""
    value = float(size)
    for unit in ("bytes", "kB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "bytes" \
                else f"{value:.1f} {unit}"
        value /= 1024
    return ""
