"""Application-wide settings and their file-based store.

Unlike workspaces, settings are global: one JSON file at
$XDG_CONFIG_HOME/sqlide/settings.json. The module-level `store` is the
single instance; the application loads it at startup and widgets that
render a setting subscribe to be re-applied on change. update() is the
only mutator — it persists and notifies in one step, so callers never
see a saved file and a live UI disagree.

Settings:
- theme: "system" | "light" | "dark" (Adw color scheme override)
- editor_font_size: SQL editor font size in points
- vim_mode: modal Vim editing in SQL editors (GtkSourceView only)
- confirm_destructive: when the destructive-action ladder engages —
  "always", "non-dev" (the default: development connections run
  without a prompt) or "never". See backend/sql_risk.py.
- lsp_enabled: master switch for completion language servers
- lsp_defaults: connection kind -> what an "auto" console LSP choice
  resolves to ("auto" keeps the built-in resolution in
  sqlide.lsp.servers, "none" turns completion off for that kind, any
  other value names a server from available_servers()).
- mcp_defaults: last-used values of the MCP server tab's form
  (bind_host, row_limit, allow_query, auth_mode — "none" or "token").
  The token itself is never persisted: regenerated per instance, or
  re-typed, like every other credential in this app.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from sqlide.backend.sql_risk import CONFIRM_MODES, DEFAULT_CONFIRM_MODE

THEMES = ("system", "light", "dark")
DEFAULT_FONT_SIZE = 11


def _config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    return Path(base) / "sqlide"


@dataclass
class Settings:
    theme: str = "system"
    editor_font_size: int = DEFAULT_FONT_SIZE
    vim_mode: bool = False
    confirm_destructive: str = DEFAULT_CONFIRM_MODE
    lsp_enabled: bool = True
    lsp_defaults: dict[str, str] = field(default_factory=dict)
    mcp_defaults: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> Settings:
        theme = data.get("theme", "system")
        return cls(
            theme=theme if theme in THEMES else "system",
            editor_font_size=int(
                data.get("editor_font_size", DEFAULT_FONT_SIZE)
            ),
            vim_mode=bool(data.get("vim_mode", False)),
            confirm_destructive=(
                mode
                if (mode := data.get("confirm_destructive")) in CONFIRM_MODES
                else DEFAULT_CONFIRM_MODE
            ),
            lsp_enabled=bool(data.get("lsp_enabled", True)),
            lsp_defaults={
                str(k): str(v)
                for k, v in (data.get("lsp_defaults") or {}).items()
            },
            mcp_defaults={
                str(k): str(v)
                for k, v in (data.get("mcp_defaults") or {}).items()
            },
        )


class SettingsStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (_config_dir() / "settings.json")
        self.settings = Settings()
        self._listeners: list[Callable[[Settings], None]] = []

    def load(self) -> Settings:
        if self.path.exists():
            self.settings = Settings.from_dict(
                json.loads(self.path.read_text())
            )
        return self.settings

    def subscribe(self, listener: Callable[[Settings], None]) -> None:
        self._listeners.append(listener)

    def unsubscribe(self, listener: Callable[[Settings], None]) -> None:
        """Short-lived subscribers (per-tab widgets) must drop their
        listener on teardown or the store keeps them alive forever."""
        if listener in self._listeners:
            self._listeners.remove(listener)

    def update(self, **changes) -> None:
        """Apply field changes, persist, and notify subscribers."""
        for name, value in changes.items():
            if not hasattr(self.settings, name):
                raise AttributeError(f"Unknown setting: {name}")
            setattr(self.settings, name, value)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(asdict(self.settings), indent=2) + "\n"
        )
        for listener in self._listeners:
            listener(self.settings)


store = SettingsStore()
