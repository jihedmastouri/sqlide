"""The demo database: one small schema, per dialect, and the code
that builds it.

There is a demo because an empty SQL client cannot be tried. The
schema is deliberately the smallest one that exercises what sqlide
has to handle — a table with a primary key (editable rows), one
without (read-only), a view, an index, a trigger, a stored function
where the engine has them, and a foreign key (the relation graph) —
and it is the same shape on every engine, so a screenshot taken
against SQLite matches what PostgreSQL shows.

The SQL lives next to this module as <kind>.sql, one file per
dialect, and those files are the single source: the app reads them
through here, docker-compose.yml mounts them into fresh containers,
and scripts/init_databases.py replays them against servers that are
already running.

Two lines in those files are directives rather than statements, and
`load()` turns them into the fields of a DemoScript instead of SQL to
run:

    CREATE DATABASE demo   -> DemoScript.database  ("" on SQLite)
    \\connect demo / USE demo -> the boundary: everything after it is
                              the body, and runs *inside* that database

Building the demo therefore always has the same two phases — make the
database, then fill it — which is what `create()` does. SQLite has no
first phase: one file is one database, so the file *is* the demo. It
also needs no name from the caller — `default_sqlite_path()` picks an
unused one under $XDG_DATA_HOME/sqlide, so "give me a demo" is one
click on every engine rather than a file dialog on one of them.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from sqlide.backend.db import registry
from sqlide.backend.db.base import Connector, ConnectorError
from sqlide.backend.db.sqlite.connector import create_database_file
from sqlide.backend.sql_split import split_statements

#: Engines the demo has a dialect file for. JDBC is missing on
#: purpose: without knowing the driver's dialect there is no way to
#: write DDL it will accept.
KINDS = ("sqlite", "postgres", "mysql")

#: The database the server dialects build, when the caller names none.
DEFAULT_DATABASE = "demo"

_CREATE_DATABASE = re.compile(
    r"^CREATE\s+DATABASE\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w$]+)", re.IGNORECASE
)
_USE = re.compile(r"^USE\s+([\w$]+)\s*$", re.IGNORECASE)
_GRANT = re.compile(r"^GRANT\s", re.IGNORECASE)


class DemoError(Exception):
    """The demo could not be built (bad kind, or the server said no)."""


@dataclass(frozen=True)
class DemoScript:
    """One dialect's demo, split along the directive boundary."""

    kind: str
    #: The database the file wants to build. Empty for SQLite, where
    #: the caller's file path is the database.
    database: str = ""
    #: Statements that must run on the *server* before the database
    #: exists — in practice, CREATE DATABASE.
    setup: list[str] = field(default_factory=list)
    #: GRANTs, kept apart from `setup` because they only make sense
    #: for a seeder running as an administrator on behalf of somebody
    #: else: the dev containers are seeded as root and have to hand
    #: the demo to the `sqlide` user afterwards. Someone building the
    #: demo from inside the app is already the user who created it,
    #: and naming another one would only fail on their server.
    grants: list[str] = field(default_factory=list)
    #: Statements that run inside the database: the whole schema.
    body: list[str] = field(default_factory=list)


def sql_path(kind: str) -> Path:
    if kind not in KINDS:
        raise DemoError(
            f"No demo database for {kind!r} "
            f"(have: {', '.join(KINDS)})"
        )
    return Path(__file__).with_name(f"{kind}.sql")


def load(kind: str, database: str = "") -> DemoScript:
    """Parse a dialect's file into the two phases described above.

    `database` renames the database the demo builds; the default is
    whatever the file's own CREATE DATABASE says.
    """
    text = sql_path(kind).read_text()
    # psql spells the switch \connect, MySQL spells it USE. Rewriting
    # to one form here means the parse loop below has a single case.
    text = "\n".join(
        f"USE {line.split()[1]};" if line.startswith("\\connect ") else line
        for line in text.splitlines()
    )

    wanted = ""
    setup: list[str] = []
    grants: list[str] = []
    body: list[str] = []
    in_body = False
    for statement in split_statements(text):
        sql = _bare(statement.text)
        if not sql:
            continue
        if in_body:
            body.append(sql)
            continue
        if match := _CREATE_DATABASE.match(sql):
            wanted = match.group(1)
            setup.append(sql)
        elif _USE.match(sql):
            in_body = True  # everything after the switch is the schema
        elif _GRANT.match(sql):
            grants.append(sql)
        else:
            setup.append(sql)
    if not wanted:
        # SQLite: no CREATE DATABASE, so nothing was ever "setup" —
        # every statement in the file is the schema.
        return DemoScript(kind=kind, body=setup + body)

    name = database.strip() or wanted
    if name != wanted:
        setup = [_rename(sql, wanted, name) for sql in setup]
        grants = [_rename(sql, wanted, name) for sql in grants]
    return DemoScript(
        kind=kind, database=name, setup=setup, grants=grants, body=body
    )


def _bare(sql: str) -> str:
    """A statement without its comment lines.

    The file's header comment belongs to whatever statement follows
    it, and the directives are recognised by what the statement
    actually says — a CREATE DATABASE behind twenty lines of comment
    is still a CREATE DATABASE.
    """
    return "\n".join(
        line
        for line in sql.splitlines()
        if line.strip() and not line.strip().startswith("--")
    ).strip()


def _rename(sql: str, old: str, new: str) -> str:
    """The demo's own database name swapped for the caller's, in the
    setup statements that spell it out (CREATE DATABASE, and MySQL's
    GRANT ON <db>.*). Only whole words match, so a table whose name
    contains "demo" is left alone."""
    return re.sub(rf"\b{re.escape(old)}\b", new, sql)


def statements(kind: str, database: str = "") -> list[str]:
    """The whole demo as one flat statement list, GRANTs included —
    for a caller connected as an administrator seeding a server for
    somebody else (scripts/init_databases.py). Note that the caller
    still has to switch connection to `DemoScript.database` between
    the setup statements and the body."""
    script = load(kind, database)
    return script.setup + script.grants + script.body


def create(
    kind: str,
    *,
    file_path: str = "",
    server: Connector | None = None,
    connect: Callable[[str], Connector] | None = None,
    database: str = "",
) -> str:
    """Build the demo and return the name of what it built — the file
    path on SQLite, the database name on a server.

    Two shapes, because the engines genuinely differ:

    * SQLite — `file_path` is the database, and must not already
      exist: overwriting a file the caller named is not this
      function's call to make. Left empty, a fresh unused path is
      picked (see default_sqlite_path) — the equivalent of the server
      kinds building a `demo` database without being told where.
    * PostgreSQL / MySQL — `server` is an open connection to the
      server (any database on it), which runs CREATE DATABASE.
      `connect` is then called with the new database's name and must
      return an open connector *to it*, because neither engine can
      switch database mid-session. The demo's schema is created
      through that second connection, and it is closed again here.

    GRANTs in the dialect file are deliberately skipped: see
    DemoScript.grants.
    """
    if kind == "sqlite":
        return _create_sqlite(file_path)
    script = load(kind, database)
    if server is None or connect is None:
        raise DemoError(
            f"Building the {kind} demo needs a connection to the server"
        )
    try:
        for sql in script.setup:
            server.execute(sql)
    except ConnectorError as exc:
        raise DemoError(f"Could not create {script.database}: {exc}") from exc

    body = connect(script.database)
    try:
        for sql in script.body:
            body.execute(sql)
    except ConnectorError as exc:
        raise DemoError(
            f"Created {script.database}, but its schema failed: {exc}"
        ) from exc
    finally:
        body.close()
    return script.database


def data_dir() -> Path:
    """Where a demo database the app made for itself belongs.

    $XDG_DATA_HOME/sqlide, not the config directory: this is a
    database, not settings, and the user is free to move or delete it.
    """
    base = os.environ.get(
        "XDG_DATA_HOME", str(Path.home() / ".local" / "share")
    )
    return Path(base) / "sqlide"


def default_sqlite_path(directory: Path | None = None) -> Path:
    """A path for a demo database nobody has to name.

    Unused, and numbered when it has to be: pressing the button twice
    means wanting two demos, not an error about the first one — and
    never an overwrite of a database that may have been worked in.
    """
    directory = directory or data_dir()
    path = directory / "demo.db"
    n = 2
    while path.exists():
        path = directory / f"demo-{n}.db"
        n += 1
    return path


def _create_sqlite(file_path: str) -> str:
    path = (
        Path(file_path).expanduser() if file_path.strip()
        else default_sqlite_path()
    )
    if path.exists():
        raise DemoError(
            f"{path.name} already exists — pick a name that does not, "
            "so nothing is overwritten"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    # connect() refuses a file that is not there (a typo must not open
    # an empty database silently), so the file is made explicitly
    # first — the same way the connection dialog's "New database
    # file…" does it.
    create_database_file(str(path))
    connector = registry.create_connector("sqlite", file_path=str(path))
    connector.connect()
    try:
        for sql in load("sqlite").body:
            connector.execute(sql)
    except ConnectorError as exc:
        connector.close()
        path.unlink(missing_ok=True)  # a half-built demo helps nobody
        raise DemoError(f"Could not build the demo: {exc}") from exc
    else:
        connector.close()
    return str(path)
