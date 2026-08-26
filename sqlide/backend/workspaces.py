"""Workspaces and their file-based store.

A workspace groups 0..n connection profiles and remembers the tabs
that were open in it (table tabs and query consoles, including the
console SQL text), so reopening a workspace restores it as it was
left. It also keeps a capped history of executed queries (successes
and failures).

Each workspace is a directory of its own, workspaces/<id>/ under the
config directory (backend/config.py), holding:

* `workspace.toml` — what the workspace *is*: id, name, identity
  colour. Small, stable, and the thing worth committing;
* `connections.toml` — the connection definitions, one `[[connection]]`
  table each, comments and key order preserved across saves. Passwords
  are never in here: they go through backend/secrets.py (system
  keyring when available, plain text only when it isn't — see
  docs/configuration.md on committing this file to git);
* `state.json` — open tabs, selected tab, query history, placeholder
  values and saved filters. Session state, not configuration: it
  changes on every keystroke-ish action, so it stays out of the TOML
  a person edits and out of a diff worth reading.

A workspace written by an earlier version as workspaces/<id>.json is
converted to that layout on the first load, the old file kept beside
it as <id>.json.bak.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from sqlide.backend import config, identity, secrets, tomlwrite
from sqlide.backend.connections import ConnectionProfile

MAX_HISTORY = 200


@dataclass
class TabState:
    kind: str  # "table" | "query" | "cli" | "definition" | "function" | "relations" | "querybuilder" | "indexes" | "users" | "object"
    connection: str  # ConnectionProfile.name within the workspace
    table: str = ""  # table/definition/function tabs: object name; querybuilder: base table
    sql: str = ""  # query tabs only
    # Object info tabs: which node of the tree the tab was opened on.
    # `table` carries the object's own name, so these three are the
    # rest of its identity (see frontend/object_info.py).
    object_kind: str = ""  # "index" | "column" | "category" | …
    object_owner: str = ""  # owning table, for the kinds that need one
    object_category: str = ""  # category nodes: "indexes", "triggers", …


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
    def from_dict(cls, data: dict, path: Path | None = None) -> Workspace:
        """A workspace from its parsed files. Anything the file gets
        wrong — an unknown key, an unknown colour — is reported through
        backend/config.py and replaced by the default, so a hand edit
        can never make a workspace unopenable."""
        workspace_id = str(data.get("id") or uuid.uuid4().hex)
        connections = []
        for c in data.get("connections", []):
            profile = ConnectionProfile(**_keep_known(ConnectionProfile, c, path))
            secrets.hydrate(workspace_id, profile)
            connections.append(profile)
        return cls(
            id=workspace_id,
            name=data["name"],
            color=identity.normalize_color(data.get("color")),
            connections=connections,
            tabs=[
                TabState(**_keep_known(TabState, t, path))
                for t in data.get("tabs", [])
            ],
            selected_tab=int(data.get("selected_tab", -1)),
            history=[
                HistoryEntry(**_keep_known(HistoryEntry, h, path))
                for h in data.get("history", [])
            ],
            placeholders=dict(data.get("placeholders") or {}),
            saved_filters=dict(data.get("saved_filters") or {}),
        )


def _keep_known(cls, data: dict, path: Path | None) -> dict:
    """`data` cut down to the fields `cls` actually has. A hand-edited
    file naming a key that doesn't exist would otherwise blow up the
    whole load with a TypeError; instead the key is reported and
    ignored, and the rest of the object still opens."""
    known = set(cls.__dataclass_fields__)
    if path is not None:
        for key in data:
            if key not in known:
                config.record_error(
                    path, f"unknown key for {cls.__name__}, ignored", key=key
                )
    return {k: v for k, v in data.items() if k in known}


class WorkspaceStore:
    """One directory per workspace under <config>/workspaces/ —
    workspace.toml, connections.toml and state.json each."""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or (config.config_dir() / "workspaces")
        self.workspaces: list[Workspace] = []

    def load(self) -> list[Workspace]:
        self._migrate_files()
        if not self.directory.exists():
            self.workspaces = self._migrate_legacy()
            return self.workspaces
        items = []
        for path in sorted(self.directory.iterdir()):
            if (path / "workspace.toml").exists():
                items.append(self._read(path))
        items.sort(key=lambda w: w.name.lower())
        self.workspaces = items
        return items

    def path_for(self, workspace_id: str) -> Path:
        return self.directory / workspace_id

    def _read(self, folder: Path) -> Workspace:
        meta = config.load_toml(folder / "workspace.toml")
        connections = config.load_toml(folder / "connections.toml")
        state = self._read_state(folder / "state.json")
        data = dict(meta)
        data["id"] = str(data.get("id") or folder.name)
        data["name"] = str(data.get("name") or folder.name)
        data["connections"] = [
            _keep_known(ConnectionProfile, dict(c), folder / "connections.toml")
            for c in connections.get("connection", [])
        ]
        data.update(state)
        return Workspace.from_dict(data, path=folder / "workspace.toml")

    @staticmethod
    def _read_state(path: Path) -> dict:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            config.record_error(path, f"is not valid JSON: {exc}")
            return {}

    def save(self, workspace: Workspace) -> None:
        """Write the workspace's three files. connections.toml is
        merged over what is there, so comments and key order in a
        hand-maintained file survive a change made in the UI."""
        folder = self.path_for(workspace.id)
        folder.mkdir(parents=True, exist_ok=True)
        data = workspace.to_dict()

        meta = folder / "workspace.toml"
        self._merge_write(
            meta,
            {
                "id": data["id"],
                "name": data["name"],
                "color": data["color"],
            },
        )
        self._merge_write(
            folder / "connections.toml",
            {"connection": data["connections"]},
        )
        (folder / "state.json").write_text(
            json.dumps(
                {
                    "tabs": data["tabs"],
                    "selected_tab": data["selected_tab"],
                    "history": data["history"],
                    "placeholders": data["placeholders"],
                    "saved_filters": data["saved_filters"],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _merge_write(path: Path, data: dict) -> None:
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError:
            existing = ""
        path.write_text(tomlwrite.merge(existing, data), encoding="utf-8")
        config.watcher.forget(path)

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

    def _migrate_files(self) -> None:
        """v2 kept one JSON file per workspace; convert each into the
        workspace directory layout, leaving the old file as .json.bak
        so a downgrade (or a bad conversion) still has the original."""
        if not self.directory.exists():
            return
        for path in sorted(self.directory.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                config.record_error(path, f"is not valid JSON: {exc}")
                continue
            workspace = Workspace.from_dict(data, path=path)
            self.save(workspace)
            path.rename(path.with_suffix(".json.bak"))

    def _migrate_legacy(self) -> list[Workspace]:
        """v1 kept a flat connections.json and treated each profile as a
        workspace; import each one as a single-connection workspace."""
        legacy = config.config_dir() / "connections.json"
        if not legacy.exists():
            return []
        try:
            profiles = [
                ConnectionProfile(**item)
                for item in json.loads(legacy.read_text(encoding="utf-8"))
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
