"""Workspaces and their file-based store.

A workspace groups 0..n connection profiles and remembers the tabs
that were open in it (table tabs and query consoles, including the
console SQL text), so reopening a workspace restores it as it was
left. It also keeps a capped history of executed queries (successes
and failures). Each workspace persists as its own JSON file in
$XDG_CONFIG_HOME/sqlide/workspaces/<id>.json — connection passwords
excepted, which go through backend/secrets.py (system keyring when
available, otherwise plain text in this same file, as before).
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from sqlide.backend import identity, secrets
from sqlide.backend.connections import ConnectionProfile


def _config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    return Path(base) / "sqlide"


MAX_HISTORY = 200


@dataclass
class TabState:
    kind: str  # "table" | "query" | "cli" | "definition" | "function" | "relations" | "querybuilder"
    connection: str  # ConnectionProfile.name within the workspace
    table: str = ""  # table/definition/function tabs: object name; querybuilder: base table
    sql: str = ""  # query tabs only


@dataclass
class HistoryEntry:
    sql: str
    connection: str  # ConnectionProfile.name at run time
    timestamp: str  # local time, ISO format
    ok: bool = True  # failed runs are recorded too
    panel: str = ""  # tab title that ran it ("" in pre-panel history)
    # Set when the tab that ran the query is closed: the side panel's
    # per-panel history drops the entry, the workspace-wide History
    # tab keeps it.
    panel_closed: bool = False


@dataclass
class Workspace:
    name: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    # Identity colour (backend/identity.py): the window stripe and the
    # launcher dot. "none" until the user picks one.
    color: str = identity.NONE
    connections: list[ConnectionProfile] = field(default_factory=list)
    tabs: list[TabState] = field(default_factory=list)
    selected_tab: int = -1
    history: list[HistoryEntry] = field(default_factory=list)
    # Last value entered for each query placeholder (":name" -> value,
    # "?1" -> value), prefilling the console's placeholder prompt.
    placeholders: dict[str, str] = field(default_factory=dict)
    # Saved table filters, keyed "connection.database.table"; each is
    # {"name": …, "filters": [FilterCondition dicts]}.
    saved_filters: dict[str, list[dict]] = field(default_factory=dict)

    def add_history(self, entry: HistoryEntry) -> None:
        self.history.append(entry)
        del self.history[:-MAX_HISTORY]

    def unique_connection_name(
        self, name: str, *, exclude: ConnectionProfile | None = None
    ) -> str:
        """A variant of `name` guaranteed not to collide with another
        connection in this workspace (skipping `exclude`, e.g. the
        profile being renamed). Names key the open-connector cache and
        saved tab states, so they must stay unique within a workspace."""
        names = {p.name for p in self.connections if p is not exclude}
        if name not in names:
            return name
        n = 2
        while f"{name} ({n})" in names:
            n += 1
        return f"{name} ({n})"

    def add_connection(self, profile: ConnectionProfile) -> None:
        profile.name = self.unique_connection_name(profile.name)
        self.connections.append(profile)
        secrets.store_profile_secrets(self.id, profile)

    def remove_connection(self, name: str) -> None:
        self.connections = [p for p in self.connections if p.name != name]
        secrets.drop_profile_secrets(self.id, name)

    def sync_renamed_connection_secrets(
        self, old_name: str, profile: ConnectionProfile
    ) -> None:
        """Called after an edit mutates `profile` in place (window.py's
        _connection_edited): push the current secrets under its
        (possibly new) name, then drop the old name's entries so a
        rename doesn't leave the keyring holding a stale password."""
        secrets.store_profile_secrets(self.id, profile)
        if profile.name != old_name:
            secrets.drop_profile_secrets(self.id, old_name)

    def find_connection(self, name: str) -> ConnectionProfile | None:
        for profile in self.connections:
            if profile.name == name:
                return profile
        return None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "color": self.color,
            "connections": [secrets.redact(asdict(p)) for p in self.connections],
            "tabs": [asdict(t) for t in self.tabs],
            "selected_tab": self.selected_tab,
            "history": [asdict(h) for h in self.history],
            "placeholders": self.placeholders,
            "saved_filters": self.saved_filters,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Workspace:
        workspace_id = data["id"]
        connections = []
        for c in data.get("connections", []):
            profile = ConnectionProfile(**c)
            secrets.hydrate(workspace_id, profile)
            connections.append(profile)
        return cls(
            id=workspace_id,
            name=data["name"],
            color=identity.normalize_color(data.get("color")),
            connections=connections,
            tabs=[TabState(**t) for t in data.get("tabs", [])],
            selected_tab=data.get("selected_tab", -1),
            history=[HistoryEntry(**h) for h in data.get("history", [])],
            placeholders=dict(data.get("placeholders") or {}),
            saved_filters=dict(data.get("saved_filters") or {}),
        )


class WorkspaceStore:
    """One JSON file per workspace under <config>/sqlide/workspaces/."""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or (_config_dir() / "workspaces")
        self.workspaces: list[Workspace] = []

    def load(self) -> list[Workspace]:
        if not self.directory.exists():
            self.workspaces = self._migrate_legacy()
            return self.workspaces
        items = [
            Workspace.from_dict(json.loads(path.read_text()))
            for path in sorted(self.directory.glob("*.json"))
        ]
        items.sort(key=lambda w: w.name.lower())
        self.workspaces = items
        return items

    def save(self, workspace: Workspace) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{workspace.id}.json"
        path.write_text(json.dumps(workspace.to_dict(), indent=2) + "\n")

    def default_name(self) -> str:
        """Default Workspace for the very first one; after that
        Workspace 2, Workspace 3, etc., skipping names already taken
        so it never collides with an import or a rename."""
        if not self.workspaces:
            return "Default Workspace"
        taken = {w.name for w in self.workspaces}
        n = 2
        while f"Workspace {n}" in taken:
            n += 1
        return f"Workspace {n}"

    def create(self, name: str, color: str = identity.NONE) -> Workspace:
        workspace = Workspace(name=name, color=identity.normalize_color(color))
        self.workspaces.append(workspace)
        self.save(workspace)
        return workspace

    def _migrate_legacy(self) -> list[Workspace]:
        """v1 kept a flat connections.json and treated each profile as a
        workspace; import each one as a single-connection workspace."""
        legacy = _config_dir() / "connections.json"
        if not legacy.exists():
            return []
        try:
            profiles = [
                ConnectionProfile(**item)
                for item in json.loads(legacy.read_text())
            ]
        except Exception:
            return []
        workspaces = []
        for profile in profiles:
            workspace = Workspace(name=profile.name)
            workspace.add_connection(profile)
            self.save(workspace)
            workspaces.append(workspace)
        return workspaces
