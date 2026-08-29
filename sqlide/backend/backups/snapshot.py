"""A one-off backup taken through the connection sqlide already has.

`dump.py` drives the vendor's own tool, which is the right way to back
up a database — and is also why it cannot back up every connection: a
JDBC bridge has no `pg_dump` equivalent to drive, and an SSH-tunnelled
connection reaches its server through a forward that only exists
inside this process.

This module is the portable path for exactly those cases. It reads the
database through an open `Connector` — the same one the grid and the
console use, tunnel and JDBC driver included — and writes an ordinary
SQL script: the CREATE statements the catalog reports, then the rows
as batched INSERTs.

What that buys, and what it costs, stated plainly because the UI
repeats it to the user:

- It works for every connection kind, and needs nothing installed.
- It is *logical*, not physical: no triggers-with-definers, no storage
  parameters, no grants — whatever `get_ddl()` does not report is not
  in the file. Where an adapter reports no DDL at all (JDBC), the
  CREATE TABLE is reconstructed from the column catalog and marked as
  such in the script.
- It reads rows through the app's own connection, in pages. A table
  with a primary key is paged in key order, which is stable; one
  without is paged by offset, which is not stable under concurrent
  writes. Both are fine for a snapshot of a quiet database and neither
  is a substitute for `pg_dump --single-transaction` on a busy one.

`apply_script()` is the other direction, for the same reason: a JDBC
or tunnelled connection has no `psql` to pipe a restore through, so
the statements are executed over the connector instead.
"""

from __future__ import annotations

import gzip
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

from sqlide.backend.backups.jobs import CONTENT_DATA, CONTENT_SCHEMA
from sqlide.backend.db.base import ColumnInfo, Connector, ConnectorError, ResultSet
from sqlide.backend.sql_split import split_statements

# Rows read (and INSERTed) per batch. Small enough that a wide table
# does not build a huge statement, big enough that a million-row table
# is not a million round trips.
PAGE = 500


class SnapshotError(Exception):
    pass


@dataclass
class SnapshotSpec:
    """What a one-off backup covers. Mirrors the fields of a Job that
    still mean something without a schedule or a destination."""

    tables: list[str] = field(default_factory=list)  # empty -> every table
    content: str = "both"
    compression: str = "gzip"


def write_snapshot(
    connector: Connector,
    kind: str,
    spec: SnapshotSpec,
    dest: Path,
    *,
    on_progress: Callable[[str], None] | None = None,
) -> int:
    """Write a SQL script for `spec` to `dest`. Returns bytes written.

    A failure deletes the partial file: a truncated script that looks
    like a backup is worse than no backup.
    """
    say = on_progress or (lambda _text: None)
    opener = gzip.open if spec.compression == "gzip" else open
    known = [t.name for t in connector.list_tables() if t.kind == "table"]
    tables = spec.tables or known
    if not tables:
        raise SnapshotError("This connection reports no tables to back up.")
    # A name the catalog does not know produces a script of apologetic
    # comments rather than an error, which is the shape a backup takes
    # when it silently covers nothing. Refuse it here instead.
    if missing := _unknown(tables, known):
        raise SnapshotError(
            "This connection has no table called "
            + ", ".join(sorted(missing))
        )
    try:
        with opener(dest, "wt", encoding="utf-8") as out:
            out.write(_header(kind, spec, tables))
            if spec.content != CONTENT_DATA:
                for table in tables:
                    say(f"Schema of {table}…")
                    out.write(_schema_for(connector, table))
            if spec.content != CONTENT_SCHEMA:
                out.write("\nBEGIN;\n")
                for table in tables:
                    rows = _write_rows(connector, kind, table, out, say)
                    say(f"{table}: {rows} row(s)")
                out.write("COMMIT;\n")
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    return dest.stat().st_size


def _unknown(wanted: list[str], known: list[str]) -> set[str]:
    """Requested names the catalog does not report, comparing the bare
    name too so a schema-qualified pick still matches."""
    bare = {name.rsplit(".", 1)[-1] for name in known}
    return {
        name
        for name in wanted
        if name not in known and name.rsplit(".", 1)[-1] not in bare
    }


def _header(kind: str, spec: SnapshotSpec, tables: list[str]) -> str:
    what = {
        CONTENT_SCHEMA: "structure only",
        CONTENT_DATA: "data only",
    }.get(spec.content, "structure and data")
    return (
        f"-- sqlide snapshot ({kind}), {what}\n"
        f"-- taken {datetime.now().isoformat(timespec='seconds')}\n"
        f"-- {len(tables)} table(s): {', '.join(tables)}\n"
        "-- Read through the application's own connection, so this is a\n"
        "-- logical copy: no grants, no storage parameters, and no\n"
        "-- guarantee of a single consistent instant across tables.\n"
    )


def _schema_for(connector: Connector, table: str) -> str:
    try:
        ddl = connector.get_ddl(table).strip()
    except ConnectorError:
        ddl = ""
    if ddl:
        return f"\n{ddl.rstrip(';')};\n"
    columns = connector.list_columns(table)
    if not columns:
        return f"\n-- {table}: no definition available\n"
    return (
        f"\n-- {table}: reconstructed from the column catalog "
        "(this adapter reports no CREATE statement)\n"
        + _create_table(connector, table, columns)
    )


def _create_table(
    connector: Connector, table: str, columns: list[ColumnInfo]
) -> str:
    lines = []
    for column in columns:
        piece = f"  {connector.quote_ident(column.name)} {column.type or 'TEXT'}"
        if not column.nullable:
            piece += " NOT NULL"
        lines.append(piece)
    keys = [c.name for c in columns if c.is_pk]
    if keys:
        quoted = ", ".join(connector.quote_ident(k) for k in keys)
        lines.append(f"  PRIMARY KEY ({quoted})")
    body = ",\n".join(lines)
    return f"CREATE TABLE {_quote_table(connector, table)} (\n{body}\n);\n"


def _quote_table(connector: Connector, table: str) -> str:
    """Quote a possibly schema-qualified name, part by part."""
    return ".".join(connector.quote_ident(part) for part in table.split("."))


def _write_rows(
    connector: Connector,
    kind: str,
    table: str,
    out,
    say: Callable[[str], None],
) -> int:
    columns = connector.list_columns(table)
    if not columns:
        out.write(f"\n-- {table}: no columns reported; rows skipped\n")
        return 0
    names = [c.name for c in columns]
    keys = [c.name for c in columns if c.is_pk]
    quoted_columns = ", ".join(connector.quote_ident(n) for n in names)
    target = _quote_table(connector, table)
    order = (
        " ORDER BY " + ", ".join(connector.quote_ident(k) for k in keys)
        if keys
        else ""
    )
    if not order:
        out.write(
            f"\n-- {table}: no primary key, so rows are paged by offset — "
            "a concurrent write during this backup can duplicate or skip "
            "one.\n"
        )
    else:
        out.write(f"\n-- {table}\n")

    total, offset = 0, 0
    while True:
        sql = (
            f"SELECT {quoted_columns} FROM {target}{order} "
            f"LIMIT {PAGE} OFFSET {offset}"
        )
        result = connector.execute(sql)
        if not isinstance(result, ResultSet) or not result.rows:
            break
        out.write(_insert(connector, kind, target, quoted_columns, result.rows))
        total += len(result.rows)
        offset += len(result.rows)
        say(f"{table}: {total} row(s)…")
        if len(result.rows) < PAGE:
            break
    return total


def _insert(
    connector: Connector, kind: str, target: str, columns: str, rows
) -> str:
    values = ",\n  ".join(
        "(" + ", ".join(literal(v, kind) for v in row) + ")" for row in rows
    )
    return f"INSERT INTO {target} ({columns}) VALUES\n  {values};\n"


def literal(value, kind: str) -> str:
    """One Python value as a SQL literal for `kind`.

    Deliberately conservative: anything this does not recognise is
    written as a quoted string, which is what the drivers hand back for
    the exotic types anyway (JSON, arrays, UUIDs, enums) and what a
    target column will parse back.
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        # MySQL has no boolean type of its own; 1/0 restores into
        # TINYINT(1) and into a real BOOLEAN alike.
        return ("1" if value else "0") if kind == "mysql" else (
            "TRUE" if value else "FALSE"
        )
    if isinstance(value, (int, float, Decimal)):
        text = repr(value) if isinstance(value, float) else str(value)
        # NaN and the infinities have no portable literal; a NULL is
        # honest about what could not be carried across.
        return "NULL" if text in ("nan", "inf", "-inf") else text
    if isinstance(value, (bytes, bytearray, memoryview)):
        return _blob(bytes(value), kind)
    if isinstance(value, (datetime, date, time)):
        return _quote(value.isoformat(sep=" ") if isinstance(value, datetime)
                      else value.isoformat(), kind)
    return _quote(str(value), kind)


def _blob(data: bytes, kind: str) -> str:
    if kind == "postgres":
        return f"'\\x{data.hex()}'::bytea"
    return f"X'{data.hex()}'"


def _quote(text: str, kind: str) -> str:
    text = text.replace("'", "''")
    if kind in ("mysql", "jdbc"):
        # MySQL treats backslash as an escape inside string literals
        # unless NO_BACKSLASH_ESCAPES is set; doubling it survives both
        # settings. JDBC gets the same treatment because the bridge may
        # be sitting in front of MySQL.
        text = text.replace("\\", "\\\\")
    if "\x00" in text:
        text = text.replace("\x00", "")
    return f"'{text}'"


def apply_script(
    connector: Connector,
    script: Path,
    kind: str = "",
    *,
    on_progress: Callable[[str], None] | None = None,
) -> int:
    """Execute a SQL script over the connector, statement by statement.

    The restore path for connections with no vendor client to pipe
    into. It stops at the first failure and says which statement broke,
    rather than carrying on and leaving a half-restored database that
    looks finished — the same bargain `psql --set ON_ERROR_STOP=1`
    makes.
    """
    say = on_progress or (lambda _text: None)
    opener = gzip.open if script.suffix == ".gz" else open
    with opener(script, "rt", encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    statements = [s for s in split_statements(text, kind) if s.text.strip()]
    if not statements:
        raise SnapshotError("There is nothing to run in that file.")
    for index, statement in enumerate(statements, start=1):
        try:
            connector.execute(statement.text)
        except ConnectorError as exc:
            raise SnapshotError(
                f"Statement {index} of {len(statements)} failed: {exc}\n\n"
                + statement.text.strip()[:400]
            ) from exc
        if index % 20 == 0 or index == len(statements):
            say(f"{index} of {len(statements)} statements…")
    return len(statements)
