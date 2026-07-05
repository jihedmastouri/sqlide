"""Workspaces and their file-based store.

A workspace groups 0..n connection profiles and remembers the tabs
that were open in it (table tabs and query consoles, including the
console SQL text), so reopening a workspace restores it as it was
left. It also keeps a capped history of executed queries (successes
and failures). Each workspace persists as its own JSON file in
$XDG_CONFIG_HOME/sqlide/workspaces/<id>.json.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from sqlide.backend.connections import ConnectionProfile


def _config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    return Path(base) / "sqlide"


MAX_HISTORY = 200


@dataclass
class TabState:
    kind: str  # "table" | "query" | "definition" | "function"
    connection: str  # ConnectionProfile.name within the workspace
    table: str = ""  # table/definition/function tabs: object name
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
    connections: list[ConnectionProfile] = field(default_factory=list)
    tabs: list[TabState] = field(default_factory=list)
    selected_tab: int = -1
    history: list[HistoryEntry] = field(default_factory=list)

    def add_history(self, entry: HistoryEntry) -> None:
        self.history.append(entry)
        del self.history[:-MAX_HISTORY]

    def add_connection(self, profile: ConnectionProfile) -> None:
        # Names key the open-connector cache and saved tab states; keep
        # them unique within the workspace.
        names = {p.name for p in self.connections}
        if profile.name in names:
            n = 2
            while f"{profile.name} ({n})" in names:
                n += 1
            profile.name = f"{profile.name} ({n})"
        self.connections.append(profile)

    def find_connection(self, name: str) -> ConnectionProfile | None:
        for profile in self.connections:
            if profile.name == name:
                return profile
        return None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "connections": [asdict(p) for p in self.connections],
            "tabs": [asdict(t) for t in self.tabs],
            "selected_tab": self.selected_tab,
            "history": [asdict(h) for h in self.history],
        }

    @classmethod
    def from_dict(cls, data: dict) -> Workspace:
        return cls(
            id=data["id"],
            name=data["name"],
            connections=[
                ConnectionProfile(**c) for c in data.get("connections", [])
            ],
            tabs=[TabState(**t) for t in data.get("tabs", [])],
            selected_tab=data.get("selected_tab", -1),
            history=[HistoryEntry(**h) for h in data.get("history", [])],
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

    def create(self, name: str) -> Workspace:
        workspace = Workspace(name=name)
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
