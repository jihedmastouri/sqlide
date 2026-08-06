#!/usr/bin/env python3
"""Put the demo database on the dev servers from docker-compose.yml.

The compose file already seeds a *fresh* container through its
entrypoint, but only on first start: a server whose volume already
exists never runs those files again. This replays the same SQL against
servers that are already up, so re-seeding does not mean wiping data
directories.

    python3 scripts/init_databases.py                # every server it can reach
    python3 scripts/init_databases.py postgres16 mysql8
    python3 scripts/init_databases.py --list
    python3 scripts/init_databases.py --drop         # start the demo over

Servers that are not running (or whose driver is not installed) are
reported and skipped, exactly like the test fixtures do, so running
this with only two containers up is normal.

The SQL comes from scripts/init/<engine>.sql — the same files the
containers use. Two of their statements are directives rather than
plain SQL: CREATE DATABASE is applied only when the database is
missing, and \\connect / USE switch the connection to another database
(psql spells it one way, MySQL the other).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlide.backend.db import registry  # noqa: E402
from sqlide.backend.sql_split import split_statements  # noqa: E402

# Creating a database and granting access to it are admin jobs. The
# compose file's PostgreSQL superuser *is* `sqlide`; its MySQL `sqlide`
# user is an ordinary one, so MySQL is seeded as root (same password —
# these are throwaway containers).
ADMIN = {
    "postgres": {"user": "sqlide", "password": "sqlide"},
    "mysql": {"user": "root", "password": "sqlide"},
}

# name -> (kind, port, admin database). Must match docker-compose.yml;
# tests/conftest.py carries the same table for the same reason.
SERVERS: dict[str, tuple[str, int, str]] = {
    "postgres10": ("postgres", 54310, "sqlide"),
    "postgres11": ("postgres", 54311, "sqlide"),
    "postgres12": ("postgres", 54312, "sqlide"),
    "postgres13": ("postgres", 54313, "sqlide"),
    "postgres14": ("postgres", 54314, "sqlide"),
    "postgres15": ("postgres", 54315, "sqlide"),
    "postgres16": ("postgres", 54316, "sqlide"),
    "mysql5": ("mysql", 33057, "sqlide"),
    "mysql8": ("mysql", 33080, "sqlide"),
}

DEMO_DATABASE = "demo"

_CREATE_DATABASE = re.compile(
    r"^CREATE\s+DATABASE\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w$]+)", re.IGNORECASE
)
_USE = re.compile(r"^USE\s+([\w$]+)\s*$", re.IGNORECASE)


def statements(kind: str) -> list[str]:
    """The engine's seed statements, with psql's \\connect rewritten to
    the USE form so one file serves both the container and this
    script."""
    path = ROOT / "scripts" / "init" / f"{kind}.sql"
    text = "\n".join(
        f"USE {line.split()[1]};" if line.startswith("\\connect ") else line
        for line in path.read_text().splitlines()
    )
    return [s.text for s in split_statements(text)]


def connect(kind: str, port: int, database: str):
    connector = registry.create_connector(
        kind, host="127.0.0.1", port=port, database=database, **ADMIN[kind]
    )
    connector.connect()
    return connector


def bare(sql: str) -> str:
    """A statement without its leading comment lines — the file's
    header comment belongs to its first statement, and the directives
    are recognised by what the statement actually says."""
    return "\n".join(
        line
        for line in sql.splitlines()
        if line.strip() and not line.strip().startswith("--")
    ).strip()


def seed(name: str, drop: bool) -> str:
    """Apply the demo schema to one server; returns a status line."""
    kind, port, admin_database = SERVERS[name]
    if not registry.driver_available(kind):
        return f"{name}: skipped, no {kind} driver installed"
    try:
        admin = connect(kind, port, admin_database)
    except Exception as exc:
        return (
            f"{name}: skipped, not reachable ({exc}); "
            f"start it with: docker compose up -d {name}"
        )
    connector = admin
    try:
        if DEMO_DATABASE in set(admin.list_databases()):
            if not drop:
                return (
                    f"{name}: demo database already there "
                    "(--drop rebuilds it from scratch)"
                )
            admin.execute(f"DROP DATABASE {admin.quote_ident(DEMO_DATABASE)}")
        ran = 0
        for sql in statements(kind):
            statement = bare(sql)
            database = _CREATE_DATABASE.match(statement)
            switch = _USE.match(statement)
            if database:
                admin.execute(
                    f"CREATE DATABASE {admin.quote_ident(database.group(1))}"
                )
            elif switch:
                if connector is not admin:
                    connector.close()
                connector = connect(kind, port, switch.group(1))
            else:
                connector.execute(statement)
                ran += 1
        return f"{name}: demo database created ({ran} statement(s))"
    except Exception as exc:
        return f"{name}: FAILED — {exc}"
    finally:
        if connector is not admin:
            connector.close()
        admin.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Seed the demo database on the dev servers."
    )
    parser.add_argument(
        "servers",
        nargs="*",
        metavar="SERVER",
        help="compose service names (default: all of them)",
    )
    parser.add_argument(
        "--list", action="store_true", help="list the known servers and exit"
    )
    parser.add_argument(
        "--drop",
        action="store_true",
        help="drop the demo database first, so it is rebuilt from scratch",
    )
    args = parser.parse_args()

    if args.list:
        for name, (kind, port, _admin) in SERVERS.items():
            print(f"{name:<12} {kind:<9} 127.0.0.1:{port}")
        return 0

    unknown = [name for name in args.servers if name not in SERVERS]
    if unknown:
        parser.error(
            f"unknown server(s): {', '.join(unknown)} "
            f"(known: {', '.join(SERVERS)})"
        )

    failed = False
    for name in args.servers or list(SERVERS):
        line = seed(name, drop=args.drop)
        failed = failed or "FAILED" in line
        print(line, flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
