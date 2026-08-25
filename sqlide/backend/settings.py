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
- max_result_rows: how many rows a console/preview statement may fetch
  into a grid. An unbounded SELECT over a big table otherwise pulls
  the whole result into memory and freezes the app; past the cap the
  result is marked truncated and the UI says so. 0 means no cap.
- time_zone: which time zone a database session reports timestamps
  in — "local" (the default: the machine's own zone, so every server
  agrees with the clock on screen), "utc", or "server" (ask for
  nothing and take whatever the server is configured for, which is
  what a bare psql/mysql session gets). See session_time_zone().
- lsp_enabled: master switch for completion language servers
- lsp_defaults: connection kind -> what an "auto" console LSP choice
  resolves to ("auto" keeps the built-in resolution in
  sqlide.lsp.servers, "none" turns completion off for that kind, any
  other value names a server from available_servers()).
- mcp_defaults: last-used values of the MCP server tab's form
  (bind_host, row_limit, allow_query, auth_mode — "none" or "token").
  The token itself is never persisted: regenerated per instance, or
  re-typed, like every other credential in this app.
- last_workspace: id of the workspace the app reopens on startup, so a
  launch lands in the workspace you were last in rather than on a
  picker. Empty (or naming a workspace that no longer exists) falls
  back to the first workspace on file.
- keymap: user-edited keyboard shortcuts, action id -> accelerator
  string (Gtk.accelerator_parse() syntax; "" means "no binding"). Only
  actions the user rebound appear here — everything else falls back to
  its built-in default. See frontend/keymap.py for the registry of
  actions and defaults, and the reserved keys that can never be
  assigned (the text editor's own bindings).
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from sqlide.backend.sql_risk import CONFIRM_MODES, DEFAULT_CONFIRM_MODE

THEMES = ("system", "light", "dark")
TIME_ZONES = ("local", "utc", "server")
DEFAULT_TIME_ZONE = "local"
DEFAULT_FONT_SIZE = 11
DEFAULT_MAX_RESULT_ROWS = 5000


def _config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    return Path(base) / "sqlide"


@dataclass
class Settings:
    theme: str = "system"
    editor_font_size: int = DEFAULT_FONT_SIZE
    vim_mode: bool = False
    confirm_destructive: str = DEFAULT_CONFIRM_MODE
    max_result_rows: int = DEFAULT_MAX_RESULT_ROWS
    time_zone: str = DEFAULT_TIME_ZONE
    lsp_enabled: bool = True
    lsp_defaults: dict[str, str] = field(default_factory=dict)
    mcp_defaults: dict[str, str] = field(default_factory=dict)
    last_workspace: str = ""
    keymap: dict[str, str] = field(default_factory=dict)

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
            max_result_rows=max(
                0, int(data.get("max_result_rows", DEFAULT_MAX_RESULT_ROWS))
            ),
            time_zone=(
                tz
                if (tz := data.get("time_zone")) in TIME_ZONES
                else DEFAULT_TIME_ZONE
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
            last_workspace=str(data.get("last_workspace") or ""),
            keymap={
                str(k): str(v)
                for k, v in (data.get("keymap") or {}).items()
            },
        )


def session_time_zone() -> str | None:
    """The zone name a new database session should be pinned to, or
    None to leave the server's own setting alone.

    Returned as an IANA name ("Europe/Paris") where the machine's zone
    can be identified, since a name survives a DST change that a fixed
    "+02:00" offset would not; the offset is only a fallback for hosts
    that expose no name.
    """
    mode = store.settings.time_zone
    if mode == "server":
        return None
    if mode == "utc":
        return "UTC"
    return _local_time_zone()


def _local_time_zone() -> str:
    """The machine's IANA zone name, falling back to its current UTC
    offset when nothing on the host names one."""
    name = os.environ.get("TZ", "").strip()
    if name and "/" in name:
        return name
    localtime = Path("/etc/localtime")
    if localtime.is_symlink():
        target = os.readlink(localtime)
        # .../zoneinfo/Europe/Paris -> Europe/Paris
        _, _, tail = target.partition("zoneinfo/")
        if tail:
            return tail
    offset = datetime.now().astimezone().utcoffset() or timedelta(0)
    total = int(offset.total_seconds())
    sign = "-" if total < 0 else "+"
    hours, minutes = divmod(abs(total) // 60, 60)
    return f"{sign}{hours:02d}:{minutes:02d}"


def result_row_cap() -> int | None:
    """The row cap to pass as Connector.execute(max_rows=…), or None
    when the user turned capping off."""
    return store.settings.max_result_rows or None


class SettingsStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (_config_dir() / "settings.json")
        self.settings = Settings()
        self._listeners: list[Callable[[Settings], None]] = []

    def load(self) -> Settings:
        if self.path.exists():
            self.settings = Settings.from_dict(
                json.loads(self.path.read_text(encoding="utf-8"))
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
            json.dumps(asdict(self.settings), indent=2) + "\n",
            encoding="utf-8",
        )
        for listener in self._listeners:
            listener(self.settings)


store = SettingsStore()
