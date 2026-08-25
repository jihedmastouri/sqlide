"""Saved schemas — a database's structure, kept to build again later.

A saved schema is the CREATE script for one database (structure only,
never rows), captured from a live connection and stored under a name.
It is the answer to "set the next project up like this one", and to
"keep what this looked like before I changed it".

Like snippets and saved queries (backend/saved.py) these are global
rather than per-workspace — a schema worth keeping is worth having in
every workspace — and live in their own JSON file under
$XDG_CONFIG_HOME/sqlide/. The module-level `store` is the single
instance; panels subscribe to it and must unsubscribe on teardown.

Each entry remembers the engine it came from, because the SQL is
dialect-specific: a MySQL capture will not replay on PostgreSQL, and
the UI says so rather than letting the server produce a syntax error
twenty statements in.

Applying one is deliberately not automatic. `Connector.execute` is
never called from here: a saved schema opens in a query console for
the user to read and run, the same way every other create/drop path
in this app works.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from sqlide.backend.db.base import Connector

#: Written at the top of a captured script, so a file that ends up in
#: an editor months later still says where it came from.
HEADER = "-- sqlide saved schema"


def _config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    return Path(base) / "sqlide"


@dataclass
class SavedSchema:
    name: str
    #: Connection kind it was captured from ("sqlite" | "postgres" |
    #: "mysql" | "jdbc"), so applying it elsewhere can warn first.
    kind: str = ""
    sql: str = ""
    #: Local time of the capture, ISO format, and the connection it
    #: came from — both only ever shown, never matched on.
    captured: str = ""
    source: str = ""

    def summary(self) -> str:
        """One line for the panel's subtitle."""
        parts = [p for p in (self.kind, self.source) if p]
        if self.captured:
            parts.append(self.captured.split("T")[0])
        return " · ".join(parts)


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


class SchemaStore:
    """The saved schemas on disk, with change notification.

    Deliberately the same shape as backend/saved.py's SavedStore —
    load once, mutate, write the whole file — because the list is
    small and two stores that behave differently would be two things
    to remember.
    """

    def __init__(
        self, filename: str = "schemas.json", directory: Path | None = None
    ) -> None:
        self.path = (directory or _config_dir()) / filename
        self.items: list[SavedSchema] = []
        self._loaded = False
        self._listeners: list[Callable[[list[SavedSchema]], None]] = []

    def load(self) -> list[SavedSchema]:
        """Read the file once; later calls return the live list."""
        if not self._loaded:
            self._loaded = True
            if self.path.exists():
                try:
                    self.items = [
                        SavedSchema(
                            **{
                                # Fields a newer version added are
                                # skipped rather than fatal, the same
                                # rule the XML exchange format follows.
                                k: v
                                for k, v in item.items()
                                if k in SavedSchema.__dataclass_fields__
                            }
                        )
                        for item in json.loads(self.path.read_text(encoding="utf-8"))
                    ]
                except (ValueError, TypeError, AttributeError):
                    self.items = []  # unreadable file: start empty, keep it
        return self.items

    def add(
        self, name: str, sql: str, kind: str = "", source: str = ""
    ) -> SavedSchema:
        """Save under a unique name (an existing name gets " (2)" …)."""
        self.load()
        names = {item.name for item in self.items}
        if name in names:
            n = 2
            while f"{name} ({n})" in names:
                n += 1
            name = f"{name} ({n})"
        item = SavedSchema(
            name=name,
            kind=kind,
            sql=sql,
            captured=datetime.now().isoformat(timespec="seconds"),
            source=source,
        )
        self.items.append(item)
        self._save()
        return item

    def remove(self, item: SavedSchema) -> None:
        if item in self.items:
            self.items.remove(item)
            self._save()

    def subscribe(
        self, listener: Callable[[list[SavedSchema]], None]
    ) -> None:
        self._listeners.append(listener)

    def unsubscribe(
        self, listener: Callable[[list[SavedSchema]], None]
    ) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps([asdict(i) for i in self.items], indent=2) + "\n",
            encoding="utf-8",
        )
        for listener in self._listeners:
            listener(self.items)


store = SchemaStore()
