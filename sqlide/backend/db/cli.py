"""A tiny psql/mysql/sqlite-style command interpreter.

The CLI client console (frontend/cli_console.py) sends every line the
user types here. A line is either a *meta-command* — psql's backslash
commands (``\\dt``, ``\\d table``, ``\\l`` …) or sqlite's dot commands
(``.tables``, ``.schema`` …) — or plain SQL.

Meta-commands are answered from the connector's catalog methods, so
the same small set works across SQLite, MySQL and PostgreSQL; both the
backslash and dot spellings are accepted regardless of the connection
kind (a forgiving superset of the three real clients). Plain SQL is run
through ``Connector.execute`` and rendered as an aligned text table,
the way a terminal client prints a result set.

This module is pure backend (no GTK): it only produces text. The
frontend owns the prompt, the scrollback and threading.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlide.backend.db.base import Connector, ConnectorError, ResultSet

# Rows past this are summarised rather than printed, so a `SELECT *`
# against a big table doesn't flood the scrollback.
_MAX_ROWS = 1000


@dataclass
class CliOutput:
    text: str
    ok: bool = True


def prompt_for(kind: str, database: str) -> str:
    """The prompt string a client of this kind would show."""
    if kind == "sqlite":
        return "sqlite> "
    if kind == "postgres":
        return f"{database or 'postgres'}=# "
    if kind == "mysql":
        return "mysql> "
    return f"{kind}> "


def run_command(connector: Connector, kind: str, line: str) -> CliOutput:
    """Interpret one input line against an open connector.

    Meta-commands never raise; SQL errors are caught and returned as
    failed output so the console prints them inline like a real client.
    """
    line = line.strip()
    if not line:
        return CliOutput("")
    if line[0] in "\\.":
        return _meta(connector, kind, line)
    return _sql(connector, line)


def _sql(connector: Connector, sql: str) -> CliOutput:
    try:
        result = connector.execute(sql)
    except ConnectorError as exc:
        return CliOutput(f"ERROR:  {exc}", ok=False)
    except Exception as exc:  # a driver error that escaped wrapping
        return CliOutput(f"ERROR:  {exc}", ok=False)
    if isinstance(result, ResultSet):
        return CliOutput(_render_result(result))
    verb = "row" if result == 1 else "rows"
    return CliOutput(f"OK, {result} {verb} affected")


def _meta(connector: Connector, kind: str, line: str) -> CliOutput:
    parts = line[1:].split()
    if not parts:
        return CliOutput(_help(kind), ok=False)
    cmd, args = parts[0].lower(), parts[1:]
    arg = args[0] if args else ""

    try:
        if cmd in ("?", "h", "help"):
            return CliOutput(_help(kind))
        if cmd in ("dt", "tables"):
            return _list_objects(connector, "table")
        if cmd in ("dv", "views"):
            return _list_objects(connector, "view")
        if cmd in ("l", "list", "databases"):
            return _list_databases(connector)
        if cmd in ("df", "functions", "sf", "dp"):
            return _list_functions(connector)
        if cmd in ("d", "desc", "describe"):
            if not arg:
                return _list_objects(connector, None)
            return _describe(connector, arg)
        if cmd in ("schema",):
            return _schema(connector, arg)
        if cmd in ("q", "quit", "exit"):
            return CliOutput("Close the tab to end the session.")
        return CliOutput(
            f"Unknown command: {line.split()[0]!r}. Type "
            f"{_help_hint(kind)} for help.",
            ok=False,
        )
    except ConnectorError as exc:
        return CliOutput(f"ERROR:  {exc}", ok=False)


def _list_objects(connector: Connector, kind: str | None) -> CliOutput:
    objects = connector.list_tables()
    if kind is not None:
        objects = [o for o in objects if o.kind == kind]
    if not objects:
        what = {"table": "tables", "view": "views", None: "relations"}[kind]
        return CliOutput(f"No {what} found.")
    return CliOutput(
        _render_rows(["Name", "Type"], [(o.name, o.kind) for o in objects])
    )


def _list_databases(connector: Connector) -> CliOutput:
    names = connector.list_databases()
    if not names:
        return CliOutput("No databases listed for this connection.")
    return CliOutput(_render_rows(["Database"], [(n,) for n in names]))


def _list_functions(connector: Connector) -> CliOutput:
    functions = connector.list_functions()
    if not functions:
        return CliOutput("No functions found.")
    return CliOutput(
        _render_rows(["Name"], [(f.name,) for f in functions])
    )


def _describe(connector: Connector, table: str) -> CliOutput:
    columns = connector.list_columns(table)
    if not columns:
        return CliOutput(f"No such table or view: {table}", ok=False)
    rows = [
        (
            c.name,
            c.type,
            "" if c.nullable else "not null",
            "PK" if c.is_pk else "",
        )
        for c in columns
    ]
    header = f'Table "{table}"'
    body = _render_rows(["Column", "Type", "Nullable", "Key"], rows)
    return CliOutput(f"{header}\n{body}")


def _schema(connector: Connector, arg: str) -> CliOutput:
    if arg:
        ddl = connector.get_ddl(arg)
        if not ddl:
            return CliOutput(f"No definition available for {arg}.", ok=False)
        return CliOutput(ddl.rstrip() + ";")
    pieces = []
    for obj in connector.list_tables():
        ddl = connector.get_ddl(obj.name)
        if ddl:
            pieces.append(ddl.rstrip() + ";")
    if not pieces:
        return CliOutput("Nothing to show.")
    return CliOutput("\n\n".join(pieces))


# Rendering


def _render_result(result: ResultSet) -> str:
    total = len(result.rows)
    rows = result.rows[:_MAX_ROWS]
    table = _render_rows(result.columns, rows)
    verb = "row" if total == 1 else "rows"
    footer = f"({total} {verb})"
    if total > len(rows):
        footer = f"(showing {len(rows)} of {total} {verb})"
    return f"{table}\n{footer}" if result.columns else footer


def _render_rows(headers: list[str], rows) -> str:
    """psql-style aligned table: centred headers, a rule, left-aligned
    cells. Empty result still shows the header and a (0 rows) line via
    the caller."""
    cells = [[_cell(v) for v in row] for row in rows]
    widths = [len(h) for h in headers]
    for row in cells:
        for i, text in enumerate(row):
            if text.count("\n"):
                text = text.splitlines()[0]
            widths[i] = max(widths[i], len(text))

    def line(values: list[str], pad) -> str:
        return " " + " | ".join(pad(v, w) for v, w in zip(values, widths)) + " "

    header_line = line(headers, str.center)
    rule = "-+-".join("-" * w for w in widths)
    rule = "-" + rule + "-"
    body = [line(row, str.ljust) for row in cells]
    return "\n".join([header_line, rule, *body])


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.hex()
    text = str(value)
    # Keep multi-line values (e.g. a function body in a result) from
    # breaking column alignment.
    return text.replace("\n", " ").replace("\t", " ")


def _help_hint(kind: str) -> str:
    return "\\?" if kind != "sqlite" else ".help"


def _help(kind: str) -> str:
    dot = kind == "sqlite"
    p = "." if dot else "\\"
    lines = [
        "Commands (backslash and dot forms both work):",
        f"  {p}dt / {p}tables         list tables",
        f"  {p}dv / {p}views          list views",
        f"  {p}df / {p}functions      list functions, procedures, triggers",
        f"  {p}l  / {p}databases      list databases",
        f"  {p}d                     list tables and views",
        f"  {p}d NAME                describe a table's columns",
        f"  {p}schema [NAME]         show CREATE statement(s)",
        f"  {p}? / {p}help            this help",
        "",
        "Anything else is run as SQL. End statements with ; is optional.",
    ]
    return "\n".join(lines)
