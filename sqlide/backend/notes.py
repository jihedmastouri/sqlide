"""Free-form notes, attached to a connection, a table, or nothing.

One global TOML file, ``notes.toml`` in the config directory
(backend/config.py resolves where that is), written through
tomlwrite.merge() like every other config file — so notes are
inspectable, diffable and committable to git alongside the rest of the
configuration. Each note is one ``[[note]]`` table:

    [[note]]
    id = "..."          # stable, generated once
    title = "Retention"
    body = "..."        # Markdown (see below)
    scope = "table"     # "global" | "connection" | "table"
    connection = "prod" # scope connection/table: the profile's name
    table = "orders"    # scope table: the table (or schema.table)
    created = "2026-08-26T10:00:00"
    updated = "2026-08-26T10:00:00"

**Markdown is the note format.** It is text, so it diffs and merges in
git the way the rest of the config does, and the editor only has to
insert a few markers rather than carry a document model: headings,
bold/italic, lists and fenced code blocks are what the toolbar writes.

Notes are never dropped because their target went away: a note whose
connection or table no longer exists is *orphaned*, which is a
question asked of the note (`is_orphaned`) at display time, not a
state stored in the file — reconnecting to a renamed-back connection
makes it a normal note again.

The module-level `store` is the single instance; panels subscribe to
follow changes made from any window and must unsubscribe on teardown.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from sqlide.backend import config, tomlwrite

GLOBAL = "global"
CONNECTION = "connection"
TABLE = "table"
SCOPES = (GLOBAL, CONNECTION, TABLE)

SCOPE_LABELS = {
    GLOBAL: "General",
    CONNECTION: "Connection",
    TABLE: "Table",
}


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


@dataclass
class Note:
    title: str = ""
    body: str = ""
    scope: str = GLOBAL
    connection: str = ""
    table: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created: str = field(default_factory=_now)
    updated: str = field(default_factory=_now)

    @classmethod
    def from_dict(cls, data: dict, path: Path | None = None) -> Note:
        """One note off disk. A bad scope is reported (naming the file
        and the key) and falls back to "global" rather than losing the
        note; everything else is coerced to text."""

        def text(key: str) -> str:
            got = data.get(key, "")
            return got if isinstance(got, str) else str(got)

        scope = text("scope") or GLOBAL
        if scope not in SCOPES:
            if path is not None:
                config.record_error(
                    path,
                    f"{scope!r} is not one of " + ", ".join(SCOPES),
                    key="note.scope",
                )
            scope = GLOBAL
        note = cls(
            title=text("title"),
            body=text("body"),
            scope=scope,
            connection=text("connection"),
            table=text("table"),
            id=text("id") or uuid.uuid4().hex,
        )
        note.created = text("created") or note.created
        note.updated = text("updated") or note.created
        return note

    @property
    def scope_label(self) -> str:
        """The badge shown next to the title."""
        if self.scope == TABLE:
            return self.table or "Table"
        if self.scope == CONNECTION:
            return self.connection or "Connection"
        return SCOPE_LABELS[GLOBAL]

    def matches_text(self, text: str) -> bool:
        """The free-text filter: title and body, case-insensitively."""
        needle = text.strip().lower()
        if not needle:
            return True
        return needle in self.title.lower() or needle in self.body.lower()

    def matches_scope(
        self, scope: str, connection: str = "", table: str = ""
    ) -> bool:
        """The All / This connection / This table filter. "This
        connection" includes the table notes of that connection: they
        are about it too."""
        if scope == GLOBAL:  # "All"
            return True
        if scope == CONNECTION:
            return self.connection == connection and bool(connection)
        return (
            self.scope == TABLE
            and self.connection == connection
            and self.table == table
            and bool(table)
        )

    def is_orphaned(
        self,
        connections: Iterable[str] | None,
        tables: Iterable[str] | None = None,
    ) -> bool:
        """True when the object this note is about is gone.

        `connections` is the connection names that exist and `tables`
        the tables known for this note's connection, both as the caller
        knows them right now; None means "not known yet" (nothing is
        connected, the tree has not been walked), and an unknown target
        is never called orphaned on a guess.
        """
        if self.scope == GLOBAL:
            return False
        if connections is None:
            return False
        if self.connection not in set(connections):
            return True
        if self.scope == TABLE and tables is not None:
            return self.table not in set(tables)
        return False


class NotesStore:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self.notes: list[Note] = []
        self._loaded = False
        self._listeners: list[Callable[[list[Note]], None]] = []

    @property
    def path(self) -> Path:
        # Resolved late: the config directory can be redirected (CLI
        # flag, tests) after this module is imported.
        return self._path or (config.config_dir() / "notes.toml")

    def load(self) -> list[Note]:
        """Read notes.toml once and start watching it, so a note added
        by hand (or by an agent) while the app runs shows up."""
        if not self._loaded:
            self._loaded = True
            self._read()
            config.watcher.watch(self.path, lambda _path: self.reload())
        return self.notes

    def reload(self) -> list[Note]:
        """Re-read the file and notify subscribers."""
        self._loaded = True
        self._read()
        self._notify()
        return self.notes

    def _read(self) -> None:
        path = self.path
        data = config.load_toml(path)
        entries = data.get("note", [])
        if not isinstance(entries, list):
            config.record_error(path, "note must be a list of [[note]] tables")
            entries = []
        self.notes = [
            Note.from_dict(entry, path=path)
            for entry in entries
            if isinstance(entry, dict)
        ]

    # Mutations. Each persists and notifies in one step, so no caller
    # ever sees the file and the UI disagree.

    def add(
        self,
        title: str,
        body: str,
        scope: str = GLOBAL,
        connection: str = "",
        table: str = "",
    ) -> Note:
        self.load()
        note = Note(
            title=title.strip() or "Untitled",
            body=body,
            scope=scope if scope in SCOPES else GLOBAL,
            connection=connection if scope in (CONNECTION, TABLE) else "",
            table=table if scope == TABLE else "",
        )
        self.notes.append(note)
        self._save()
        return note

    def update(self, note: Note, **changes) -> Note:
        """Apply field changes to a note and persist. `updated` is
        stamped here, so no caller has to remember to."""
        for name, value in changes.items():
            if not hasattr(note, name) or name in ("id", "created"):
                raise AttributeError(f"Unknown note field: {name}")
            setattr(note, name, value)
        if note.scope != TABLE:
            note.table = ""
        if note.scope == GLOBAL:
            note.connection = ""
        note.title = note.title.strip() or "Untitled"
        note.updated = _now()
        self._save()
        return note

    def remove(self, note: Note) -> None:
        self.load()
        for existing in list(self.notes):
            if existing.id == note.id:
                self.notes.remove(existing)
        self._save()

    def find(self, note_id: str) -> Note | None:
        return next((n for n in self.load() if n.id == note_id), None)

    def filter(
        self,
        scope: str = GLOBAL,
        connection: str = "",
        table: str = "",
        text: str = "",
    ) -> list[Note]:
        """The panel's list: scope filter and text filter together,
        newest change first."""
        matched = [
            note
            for note in self.load()
            if note.matches_scope(scope, connection, table)
            and note.matches_text(text)
        ]
        return sorted(matched, key=lambda n: n.updated, reverse=True)

    def subscribe(self, listener: Callable[[list[Note]], None]) -> None:
        self._listeners.append(listener)

    def unsubscribe(self, listener: Callable[[list[Note]], None]) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener(self.notes)

    def _save(self) -> None:
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError:
            existing = ""
        data = {"note": [asdict(note) for note in self.notes]}
        if not self.notes:
            # merge() has nothing to write an empty array of tables
            # over; keep the file's prose, drop the notes.
            text = tomlwrite.merge(existing, {}) if existing.strip() else ""
        else:
            text = tomlwrite.merge(existing, data)
        path.write_text(text, encoding="utf-8")
        # Our own write must not come back as an external edit.
        config.watcher.forget(path)
        self._notify()


store = NotesStore()
