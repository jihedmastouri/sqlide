"""SQLite adapter (stdlib sqlite3)."""

from __future__ import annotations

import os
import sqlite3
import threading
import urllib.parse
from typing import Any

from sqlide.backend.db.base import (
    ColumnInfo,
    Connector,
    ConnectorError,
    ConstraintInfo,
    FilterCondition,
    FunctionInfo,
    IndexInfo,
    RelationInfo,
    ResultSet,
    SortSpec,
    TableInfo,
    TableStats,
    TriggerInfo,
    TypeSpec,
    build_filter_clauses,
)

_TEMPLATES = {
    "table": (
        "-- New table: adjust the name and columns, then Run.\n"
        "CREATE TABLE table_name (\n"
        "  id INTEGER PRIMARY KEY,\n"
        "  name TEXT NOT NULL,\n"
        "  created_at TEXT DEFAULT (datetime('now'))\n"
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
        "-- Timing: BEFORE | AFTER | INSTEAD OF, on INSERT | UPDATE |"
        " DELETE.\n"
        "CREATE TRIGGER trigger_name AFTER INSERT ON table_name\n"
        "FOR EACH ROW\n"
        "BEGIN\n"
        "  UPDATE table_name SET name = NEW.name WHERE id = NEW.id;\n"
        "END;\n"
    ),
}


def create_database_file(path: str) -> None:
    """Create an empty SQLite database at `path`.

    connect() refuses a missing file on purpose (a typo should not
    silently open an empty database), so the connection dialog's
    "New Database…" needs a way to say *yes, make this one*. Opening an
    existing file never truncates it, so this is safe to call on a path
    the user picked over an existing database — it just adopts it.
    """
    if not path.strip():
        raise ConnectorError("No file path given")
    directory = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(directory):
        raise ConnectorError(f"No such directory: {directory}")
    try:
        connection = sqlite3.connect(path)
        try:
            # sqlite3.connect() leaves a zero-length file until
            # something is written; this gives it a real header, so
            # other tools recognise it as a database straight away.
            connection.execute("PRAGMA user_version = 0")
            connection.commit()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise ConnectorError(str(exc)) from exc


def _decode_text(raw: bytes) -> str:
    """TEXT bytes as UTF-8, lossily rather than not at all."""
    return raw.decode("utf-8", "replace")


class SqliteConnector(Connector):
    """Catalog via sqlite_master and PRAGMA table_info().

    One connection is shared by all of the app's worker threads, so every
    statement is serialized behind a lock (hence check_same_thread=False).
    """

    def __init__(self, file_path: str, read_only: bool = False) -> None:
        self.file_path = file_path
        self.read_only = read_only  # MCP instances: open with mode=ro
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def connect(self) -> None:
        # sqlite3.connect() silently creates missing files; a typo'd path
        # should fail instead of opening an empty database.
        if not os.path.isfile(self.file_path):
            raise ConnectorError(f"No such database file: {self.file_path}")
        try:
            # isolation_level=None: the module never opens implicit
            # transactions, so statements autocommit unless the user
            # runs an explicit BEGIN (the console's transaction
            # buttons) — which then stays open across statements.
            target = self.file_path
            if self.read_only:
                target = (
                    "file:"
                    + urllib.parse.quote(os.path.abspath(self.file_path))
                    + "?mode=ro"
                )
            self._conn = sqlite3.connect(
                target,
                uri=self.read_only,
                check_same_thread=False,
                isolation_level=None,
            )
            # Legacy files sometimes hold TEXT that is not valid UTF-8
            # (latin1 written by an older tool). The default text
            # factory decodes strictly, so one bad byte raises out of
            # fetchall() and loses the whole result set; replacing the
            # undecodable bytes keeps the rest of the row readable.
            self._conn.text_factory = _decode_text
        except sqlite3.Error as exc:
            raise ConnectorError(str(exc)) from exc

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _run(
        self,
        sql: str,
        params: tuple = (),
        expect_rowcount: int | None = None,
        fetch_limit: int | None = None,
    ) -> tuple[list[str], list[tuple], int]:
        """Execute one statement; returns (columns, rows, rowcount).

        The connection autocommits (isolation_level=None), so nothing
        is committed here; a user-issued BEGIN keeps its transaction
        open across calls. With expect_rowcount set, the statement is
        wrapped in its own transaction (unless one is already open) so
        a mismatch rolls back before the change is durable.
        """
        if self._conn is None:
            raise ConnectorError("Not connected")
        try:
            with self._lock:
                own_tx = (
                    expect_rowcount is not None
                    and not self._conn.in_transaction
                )
                if own_tx:
                    self._conn.execute("BEGIN")
                try:
                    cur = self._conn.execute(sql, params)
                    if cur.description is not None:
                        columns = [d[0] for d in cur.description]
                        rows = (
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
                            f"Expected to modify {expect_rowcount} row(s), "
                            f"matched {cur.rowcount}; rolled back"
                        )
                except sqlite3.Error:
                    if own_tx and self._conn.in_transaction:
                        self._conn.rollback()
                    raise
                if own_tx and self._conn.in_transaction:
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

    # Table properties (CORE-04). SQLite keeps no constraint catalog,
    # so the constraints are read back off the PRAGMAs: the primary key
    # from the column list, unique constraints from the indexes SQLite
    # created for them, foreign keys from foreign_key_list. CHECK
    # constraints live only in the CREATE text, which the properties
    # view shows in full anyway.

    def table_stats(self, table: str) -> TableStats:
        _, rows, _ = self._run(
            "SELECT type FROM sqlite_master WHERE name = ?", (table,)
        )
        kind = rows[0][0] if rows else ""
        stats = TableStats(kind=kind)
        if kind != "table":
            return stats
        _, counted, _ = self._run(
            f"SELECT COUNT(*) FROM {self.quote_ident(table)}"
        )
        return TableStats(
            kind=kind, rows=str(counted[0][0]) if counted else ""
        )

    def list_constraints(self, table: str) -> list[ConstraintInfo]:
        quoted = self.quote_ident(table)
        found = []
        keys = [c.name for c in self.list_columns(table) if c.is_pk]
        if keys:
            found.append(ConstraintInfo(
                name="(primary key)", kind="PRIMARY KEY", table=table,
                columns=", ".join(keys),
            ))
        _, indexes, _ = self._run(f"PRAGMA index_list({quoted})")
        for _seq, name, unique, origin, *_rest in indexes:
            if not unique or origin == "pk":
                continue
            _, columns, _ = self._run(
                f"PRAGMA index_info({self.quote_ident(name)})"
            )
            found.append(ConstraintInfo(
                name=name, kind="UNIQUE", table=table,
                # PRAGMA index_info rows are (seqno, cid, name).
                columns=", ".join(str(c[2]) for c in columns),
            ))
        _, keys_out, _ = self._run(f"PRAGMA foreign_key_list({quoted})")
        for _id, _seq, ref_table, column, ref_column, *_rest in keys_out:
            found.append(ConstraintInfo(
                name=f"{column} → {ref_table}", kind="FOREIGN KEY",
                table=table, columns=column,
                definition=(
                    f"REFERENCES {ref_table}"
                    f"({ref_column or 'rowid'})"
                ),
            ))
        return found

    def list_relations(self) -> list[RelationInfo]:
        relations = []
        for table in self.list_tables():
            if table.kind == "view":
                continue
            _, rows, _ = self._run(
                f"PRAGMA foreign_key_list({self.quote_ident(table.name)})"
            )
            for _id, _seq, ref_table, column, ref_column, *_rest in rows:
                relations.append(RelationInfo(
                    table=table.name,
                    column=column,
                    # ref_column is NULL when the FK targets the
                    # referenced table's primary key implicitly.
                    ref_table=ref_table,
                    ref_column=ref_column or "",
                ))
        return relations

    def in_transaction(self) -> bool:
        return self._conn is not None and self._conn.in_transaction

    def rollback(self) -> None:
        if self._conn is None:
            return
        try:
            with self._lock:
                self._conn.rollback()
        except sqlite3.Error as exc:
            raise ConnectorError(str(exc)) from exc

    def get_ddl(self, name: str) -> str:
        # sqlite_master.sql is NULL for some internal objects.
        _, rows, _ = self._run(
            "SELECT sql FROM sqlite_master WHERE name = ?", (name,)
        )
        return (rows[0][0] or "") if rows else ""

    def schema_ddl(self) -> list[str]:
        """SQLite keeps the exact CREATE text of every object it was
        given, so the whole schema is one query — and it round-trips
        byte for byte instead of being reconstructed.

        Ordered tables, then indexes, then views and triggers, so the
        script replays without forward references. Autoindexes
        (sqlite_autoindex_*, behind PRIMARY KEY/UNIQUE) have a NULL
        sql and drop out on their own.
        """
        _, rows, _ = self._run(
            "SELECT sql FROM sqlite_master "
            "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
            "ORDER BY CASE type "
            "WHEN 'table' THEN 1 WHEN 'index' THEN 2 "
            "WHEN 'view' THEN 3 ELSE 4 END, name"
        )
        return [sql.strip() for (sql,) in rows if sql and sql.strip()]

    def list_functions(self) -> list[FunctionInfo]:
        # SQLite has no stored functions or procedures; triggers are
        # its programmable objects, so they populate Functions.
        _, rows, _ = self._run(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' "
            "ORDER BY name"
        )
        return [FunctionInfo(name=name) for (name,) in rows]

    def list_indexes(self) -> list[IndexInfo]:
        # Autoindexes (sqlite_autoindex_*) back PRIMARY KEY/UNIQUE
        # constraints and cannot be dropped.
        _, rows, _ = self._run(
            "SELECT name, tbl_name, sql FROM sqlite_master "
            "WHERE type = 'index' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        )
        return [
            IndexInfo(name=name, table=table, ddl=sql or "")
            for name, table, sql in rows
        ]

    def list_triggers(self) -> list[TriggerInfo]:
        _, rows, _ = self._run(
            "SELECT name, tbl_name, sql FROM sqlite_master "
            "WHERE type = 'trigger' ORDER BY name"
        )
        return [
            TriggerInfo(name=name, table=table, ddl=sql or "")
            for name, table, sql in rows
        ]

    def ddl_kinds(self) -> tuple[str, ...]:
        return ("table", "view", "index", "trigger")

    def create_template(self, kind: str) -> str:
        return _TEMPLATES.get(kind, "")

    def column_type_specs(self) -> list[TypeSpec]:
        # The five storage classes first, because they are what SQLite
        # actually has; the aliases below them are accepted and mapped
        # onto those affinities, and people coming from other engines
        # look for them.
        return [
            TypeSpec("INTEGER", note="storage class"),
            TypeSpec("TEXT", note="storage class"),
            TypeSpec("REAL", note="storage class"),
            TypeSpec("BLOB", note="storage class"),
            TypeSpec("NUMERIC", note="storage class"),
            TypeSpec("VARCHAR", ("length",), ("255",), "TEXT affinity"),
            TypeSpec("CHAR", ("length",), ("1",), "TEXT affinity"),
            TypeSpec(
                "DECIMAL", ("precision", "scale"), ("10", "2"),
                "NUMERIC affinity",
            ),
            TypeSpec("BOOLEAN", note="NUMERIC affinity (0/1)"),
            TypeSpec("DATE", note="NUMERIC affinity (no date type)"),
            TypeSpec("DATETIME", note="NUMERIC affinity (no date type)"),
        ]

    def explain_prefix(self) -> str:
        # Plain EXPLAIN dumps VDBE opcodes; the query plan is the
        # readable form.
        return "EXPLAIN QUERY PLAN "

    def drop_function_sql(self, name: str) -> str:
        return f"DROP TRIGGER IF EXISTS {self.quote_ident(name)}"

    def rebuild_table_statements(
        self, table: str, new_ddl: str, copy_columns: list[tuple[str, str]]
    ) -> list[str]:
        # Since 3.25, RENAME rewrites REFERENCES clauses in *other*
        # tables to follow the renamed table — they would end up
        # pointing at the dropped backup. legacy_alter_table keeps the
        # rename local while the backup name exists.
        statements = super().rebuild_table_statements(
            table, new_ddl, copy_columns
        )
        return [
            "PRAGMA legacy_alter_table = ON",
            statements[0],  # the RENAME
            "PRAGMA legacy_alter_table = OFF",
            *statements[1:],
            # Last, so it sees the finished table: the copy can leave a
            # row pointing at a parent the new definition no longer
            # accepts. It reports violations as rows rather than
            # raising, so the caller has to read them back — see
            # rebuild_check_failure().
            "PRAGMA foreign_key_check",
        ]

    def wrap_rebuild(self, statements: list[str]) -> list[str]:
        # SQLite's DDL is transactional, so the rebuild really is all
        # or nothing. Enforcement goes off around it (the pragma is a
        # no-op inside a transaction, hence outside the BEGIN): while
        # the backup exists the two tables cannot both satisfy the
        # foreign keys, and foreign_key_check above is what replaces
        # the enforcement we switched off.
        return [
            "PRAGMA foreign_keys = OFF",
            "BEGIN",
            *statements,
            "COMMIT",
            "PRAGMA foreign_keys = ON",
        ]

    def rebuild_check_failure(self, sql: str, result: Any) -> str:
        if "foreign_key_check" not in sql.lower():
            return ""
        rows = getattr(result, "rows", None) or []
        if not rows:
            return ""
        # (table, rowid, parent, fkid) per violating row.
        detail = ", ".join(
            f"{row[0]}(rowid {row[1]}) -> {row[2]}" for row in rows[:5]
        )
        more = f" and {len(rows) - 5} more" if len(rows) > 5 else ""
        return f"Foreign-key violations after the rebuild: {detail}{more}"

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
            filters, order_by, self.quote_ident
        )
        columns, rows, _ = self._run(
            f"SELECT * FROM {self.quote_ident(table)}{where}{order} "
            "LIMIT ? OFFSET ?",
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
        # interrupt() is the one sqlite3 call meant to be made from
        # another thread while a statement runs; the blocked execute
        # then raises OperationalError("interrupted").
        conn = self._conn
        if conn is None:
            return
        conn.interrupt()

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
