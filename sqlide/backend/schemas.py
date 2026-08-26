"""A database's whole structure, as one runnable script.

`capture` walks a live connection's catalog and returns the CREATE
script for everything in it (structure only, never rows). It is the
answer to "set the next project up like this one", and to "keep what
this looked like before I changed it".

Nothing here is stored: the script opens in a query console like any
other SQL, and the console's Save keeps it among the saved queries if
it is worth keeping — one place for kept SQL rather than two.

`Connector.execute` is never called from here either. A captured
schema opens for the user to read and run, the same way every other
create/drop path in this app works.
"""

from __future__ import annotations

from datetime import datetime

from sqlide.backend.db.base import Connector

#: Written at the top of a captured script, so a file that ends up in
#: an editor months later still says where it came from.
HEADER = "-- sqlide schema"


def capture(connector: Connector, kind: str = "", source: str = "") -> str:
    """The connected database's structure as one runnable script.

    Statements come from Connector.schema_ddl(), which each adapter
    orders so the script replays top to bottom. They are joined with
    semicolons — objects with bodies (PL/pgSQL, MySQL triggers) carry
    their own internal semicolons, which is exactly why the console
    splits statements with backend/sql_split.py rather than on every
    ";" it sees.
    """
    statements = connector.schema_ddl()
    header = [HEADER]
    if source:
        header.append(f"-- from: {source}" + (f" ({kind})" if kind else ""))
    header.append(f"-- captured: {datetime.now().isoformat(timespec='seconds')}")
    header.append("-- structure only: no rows are included")
    body = "\n\n".join(f"{s.rstrip().rstrip(';')};" for s in statements)
    return "\n".join(header) + "\n\n" + body + ("\n" if body else "")
