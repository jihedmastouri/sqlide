#!/usr/bin/env python3
"""Create the SQLite demo database for trying sqlide.

Usage: python3 scripts/make_demo_db.py [path]   (default: demo.db)

The schema comes from sqlide/backend/demo/sqlite.sql — the same demo
the app builds from its connection dialog, and the same shape the
PostgreSQL and MySQL files in that directory build. This script is
just the command-line way in.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlide.backend import demo  # noqa: E402

path = Path(sys.argv[1] if len(sys.argv) > 1 else "demo.db")

# The demo refuses to overwrite; re-running the script to rebuild it
# is normal, so this one removes the old file first (the app's own
# path has no such licence — there the file is one the user named).
if path.exists():
    path.unlink()

try:
    created = demo.create("sqlite", file_path=str(path))
except demo.DemoError as exc:
    sys.exit(f"{exc}")
print(f"Created {created}")
