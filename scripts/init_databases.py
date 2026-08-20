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

The SQL comes from sqlide/backend/demo/<engine>.sql — the same files
the containers mount and the app itself builds its demo from.
backend/demo parses them (see its docstring for the two directives
they carry); this script only has to run the pieces in order, against
an admin connection first and the new database after.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlide.backend import demo  # noqa: E402
from sqlide.backend.db import registry  # noqa: E402

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

DEMO_DATABASE = demo.DEFAULT_DATABASE


def connect(kind: str, port: int, database: str):
    connector = registry.create_connector(
        kind, host="127.0.0.1", port=port, database=database, **ADMIN[kind]
    )
    connector.connect()
    return connector


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
    script = demo.load(kind)
    connector = None
    try:
        if script.database in set(admin.list_databases()):
            if not drop:
                return (
                    f"{name}: demo database already there "
                    "(--drop rebuilds it from scratch)"
                )
            admin.execute(f"DROP DATABASE {admin.quote_ident(script.database)}")
        # Seeded as an administrator on the `sqlide` user's behalf, so
        # unlike the app's own path this one does run the GRANTs.
        for sql in script.setup + script.grants:
            admin.execute(sql)
        connector = connect(kind, port, script.database)
        for sql in script.body:
            connector.execute(sql)
        return f"{name}: demo database created ({len(script.body)} statement(s))"
    except Exception as exc:
        return f"{name}: FAILED — {exc}"
    finally:
        if connector is not None:
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
