"""Saved SQL — code snippets and whole queries.

Both are global (cross-workspace) named lists of SQL text, each in
its own JSON file under $XDG_CONFIG_HOME/sqlide/: snippets are
fragments meant to be inserted into the editor at the cursor, saved
queries are complete statements meant to open in a console. The two
module-level stores are the single instances; panels subscribe to
show changes made from any window and must unsubscribe on teardown.

Saved table filters are not here: they are keyed by a workspace's
connection names, so they live in the workspace file
(Workspace.saved_filters).
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable


def _config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    return Path(base) / "sqlide"


@dataclass
class SavedItem:
    name: str
    sql: str


class SavedStore:
    def __init__(self, filename: str, directory: Path | None = None) -> None:
        self.path = (directory or _config_dir()) / filename
        self.items: list[SavedItem] = []
        self._loaded = False
        self._listeners: list[Callable[[list[SavedItem]], None]] = []

    def load(self) -> list[SavedItem]:
        """Read the file once; later calls return the live list."""
        if not self._loaded:
            self._loaded = True
            if self.path.exists():
                try:
                    self.items = [
                        SavedItem(**item)
                        for item in json.loads(self.path.read_text())
                    ]
                except (ValueError, TypeError):
                    self.items = []  # unreadable file: start empty, keep it
        return self.items

    def add(self, name: str, sql: str) -> SavedItem:
        """Save under a unique name (an existing name gets " (2)" …)."""
        self.load()
        names = {item.name for item in self.items}
        if name in names:
            n = 2
            while f"{name} ({n})" in names:
                n += 1
            name = f"{name} ({n})"
        item = SavedItem(name=name, sql=sql)
        self.items.append(item)
        self._save()
        return item

    def remove(self, item: SavedItem) -> None:
        if item in self.items:
            self.items.remove(item)
            self._save()

    def subscribe(
        self, listener: Callable[[list[SavedItem]], None]
    ) -> None:
        self._listeners.append(listener)

    def unsubscribe(
        self, listener: Callable[[list[SavedItem]], None]
    ) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps([asdict(i) for i in self.items], indent=2) + "\n"
        )
        for listener in self._listeners:
            listener(self.items)


snippets = SavedStore("snippets.json")
queries = SavedStore("saved_queries.json")
