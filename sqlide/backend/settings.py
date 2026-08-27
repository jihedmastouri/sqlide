"""Application-wide settings and their file-based store.

Unlike workspaces, settings are global: one TOML file, settings.toml
in the config directory (backend/config.py resolves where that is).
The module-level `store` is the single instance; the application loads
it at startup and widgets that render a setting subscribe to be
re-applied on change. update() is the only mutator — it persists and
notifies in one step, so callers never see a saved file and a live UI
disagree — and it rewrites the file through tomlwrite.merge(), so
comments and key order a person put there survive a change made in the
UI.

The file is meant to be edited by hand (or by an agent) while the app
runs: reload() re-reads it and notifies the same subscribers, and the
application ticks config.watcher so an external edit is picked up
without a restart. A settings.json from an earlier version is imported
once, on the first load that finds no settings.toml.

An unreadable file, or a key with a value outside its allowed set, is
reported through backend/config.py — naming file, line and key — and
falls back to the default for that key; the rest of the file still
applies.

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
- monitor_interval: how often the monitoring dashboard (CORE-15) polls
  a server's live panels, in seconds, clamped to MIN_INTERVAL ..
  MAX_INTERVAL. The dashboard's own spin control writes here, so the
  interval you settle on is the one every later dashboard opens with;
  storage keeps its own much slower timer. See backend/db/metrics.py.
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
- sidebar_width: width in pixels of the window's connections sidebar,
  so a drag survives a restart. Clamped to SIDEBAR_MIN_WIDTH ..
  SIDEBAR_MAX_WIDTH on the way in and out, and double-clicking the
  drag handle puts DEFAULT_SIDEBAR_WIDTH back.
- map_tiles_enabled: whether the geo viewer (PG-04) draws map tiles at
  all. Off gives geometries on a plain background and never makes a
  network request — the setting for an air-gapped machine, and what
  the viewer falls back to when the network is down.
- map_tile_url: the slippy-map tile template the geo viewer fetches,
  with {z}/{x}/{y} placeholders. Defaults to OpenStreetMap's own
  server; point it at your own tile server (or a paid one) and nothing
  in the app talks to openstreetmap.org again. See backend/tiles.py
  for the caching and attribution the viewer holds itself to.
- map_attribution: the credit line drawn over the map. It travels with
  the URL because a tile server's terms come with it; blanking it
  disables tiles rather than dropping the credit.
- map_max_features: how many geometries one map draws before it stops
  and says "showing N of M".
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

from sqlide.backend import config, tiles, tomlwrite
from sqlide.backend.db import metrics
from sqlide.backend.sql_risk import CONFIRM_MODES, DEFAULT_CONFIRM_MODE

THEMES = ("system", "light", "dark")
TIME_ZONES = ("local", "utc", "server")
DEFAULT_TIME_ZONE = "local"
DEFAULT_FONT_SIZE = 11
DEFAULT_MAX_RESULT_ROWS = 5000
# The connections sidebar: how wide it starts, and how far a drag of
# its handle may take it either way.
DEFAULT_SIDEBAR_WIDTH = 280
SIDEBAR_MIN_WIDTH = 180
SIDEBAR_MAX_WIDTH = 600
# The geo viewer's tile source and its drawing cap (PG-04). The
# defaults are OpenStreetMap's public server, used under its tile
# policy — see backend/tiles.py, which does the caching and sends the
# identifying User-Agent that policy asks for.
DEFAULT_TILE_URL = tiles.DEFAULT_TILE_URL
DEFAULT_TILE_ATTRIBUTION = tiles.DEFAULT_ATTRIBUTION
DEFAULT_MAX_FEATURES = 2000


def clamp_sidebar_width(width: int) -> int:
    """A sidebar width held to its allowed range — the one place the
    limits are applied, whether the number came from a drag or from a
    hand-edited settings.toml."""
    return max(SIDEBAR_MIN_WIDTH, min(SIDEBAR_MAX_WIDTH, int(width)))


@dataclass
class Settings:
    theme: str = "system"
    editor_font_size: int = DEFAULT_FONT_SIZE
    vim_mode: bool = False
    confirm_destructive: str = DEFAULT_CONFIRM_MODE
    max_result_rows: int = DEFAULT_MAX_RESULT_ROWS
    time_zone: str = DEFAULT_TIME_ZONE
    monitor_interval: int = metrics.DEFAULT_INTERVAL
    lsp_enabled: bool = True
    #: Show the server's own schemas (information_schema, pg_catalog)
    #: in the object tree. Shown by default, dimmed rather than
    #: hidden: they are worth reading, just never the thing you came
    #: for (PG-03).
    show_system_schemas: bool = True
    map_tiles_enabled: bool = True
    map_tile_url: str = DEFAULT_TILE_URL
    map_attribution: str = DEFAULT_TILE_ATTRIBUTION
    map_max_features: int = DEFAULT_MAX_FEATURES
    lsp_defaults: dict[str, str] = field(default_factory=dict)
    mcp_defaults: dict[str, str] = field(default_factory=dict)
    sidebar_width: int = DEFAULT_SIDEBAR_WIDTH
    last_workspace: str = ""
    keymap: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict, path: Path | None = None) -> Settings:
        """Settings from a parsed file. Every value is validated here;
        a bad one is reported (naming the file and the key) and
        replaced by its default rather than failing the whole load."""

        def bad(key: str, got, expected: str, default):
            if path is not None:
                config.record_error(
                    path,
                    f"{got!r} is not {expected}; using {default!r}",
                    line=_line_of(path, key),
                    key=key,
                )
            return default

        def choice(key: str, allowed, default: str) -> str:
            got = data.get(key, default)
            if got in allowed:
                return got
            return bad(key, got, "one of " + ", ".join(allowed), default)

        def flag(key: str, default: bool) -> bool:
            got = data.get(key, default)
            if isinstance(got, bool):
                return got
            return bad(key, got, "true or false", default)

        def number(key: str, default: int, minimum: int = 0) -> int:
            got = data.get(key, default)
            if isinstance(got, bool) or not isinstance(got, (int, float)):
                return bad(key, got, "a number", default)
            return max(minimum, int(got))

        def table(key: str) -> dict[str, str]:
            got = data.get(key) or {}
            if not isinstance(got, dict):
                return bad(key, got, "a table of strings", {})
            return {str(k): str(v) for k, v in got.items()}

        def text(key: str, default: str = "") -> str:
            got = data.get(key, default)
            if isinstance(got, str):
                return got
            return bad(key, got, "a string", default)

        return cls(
            theme=choice("theme", THEMES, "system"),
            editor_font_size=number(
                "editor_font_size", DEFAULT_FONT_SIZE, minimum=1
            ),
            vim_mode=flag("vim_mode", False),
            confirm_destructive=choice(
                "confirm_destructive", CONFIRM_MODES, DEFAULT_CONFIRM_MODE
            ),
            max_result_rows=number(
                "max_result_rows", DEFAULT_MAX_RESULT_ROWS
            ),
            time_zone=choice("time_zone", TIME_ZONES, DEFAULT_TIME_ZONE),
            monitor_interval=metrics.clamp_interval(
                number("monitor_interval", metrics.DEFAULT_INTERVAL)
            ),
            lsp_enabled=flag("lsp_enabled", True),
            show_system_schemas=flag("show_system_schemas", True),
            map_tiles_enabled=flag("map_tiles_enabled", True),
            map_tile_url=text("map_tile_url", DEFAULT_TILE_URL),
            map_attribution=text(
                "map_attribution", DEFAULT_TILE_ATTRIBUTION
            ),
            map_max_features=number(
                "map_max_features", DEFAULT_MAX_FEATURES, minimum=1
            ),
            lsp_defaults=table("lsp_defaults"),
            mcp_defaults=table("mcp_defaults"),
            sidebar_width=clamp_sidebar_width(
                number("sidebar_width", DEFAULT_SIDEBAR_WIDTH)
            ),
            last_workspace=text("last_workspace"),
            keymap=table("keymap"),
        )


def _line_of(path: Path, key: str) -> int:
    """Which line of `path` sets `key` — so an error message can point
    at it. 0 when the file doesn't mention the key at all."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    for number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        if stripped.split("=", 1)[0].strip().strip('"') == key:
            return number
    return 0


def tile_source() -> tiles.TileSource:
    """The geo viewer's tile source as the settings describe it.

    One place builds it, so the viewer, the preferences page and the
    tests all agree on what "the current tile server" means — and a
    blank attribution disables tiles rather than drawing somebody's
    tiles uncredited (see backend/tiles.py).
    """
    current = store.settings
    return tiles.TileSource(
        url_template=current.map_tile_url or DEFAULT_TILE_URL,
        attribution=current.map_attribution,
        enabled=current.map_tiles_enabled,
    )


def max_map_features() -> int:
    """The cap on features one map draws (PG-04)."""
    return max(1, store.settings.map_max_features)


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
        self.path = path or (config.config_dir() / "settings.toml")
        self.settings = Settings()
        self._listeners: list[Callable[[Settings], None]] = []

    def load(self) -> Settings:
        """Read settings.toml, importing a pre-TOML settings.json once
        if that is all there is, and start watching the file so a hand
        edit while the app runs is picked up."""
        if not self.path.exists():
            self._import_json()
        self.settings = Settings.from_dict(
            config.load_toml(self.path), path=self.path
        )
        config.watcher.watch(self.path, lambda _path: self.reload())
        return self.settings

    def reload(self) -> Settings:
        """Re-read the file and notify subscribers — what an external
        edit runs through. Every setting the UI renders (theme, font,
        row cap, keymap…) is applied from a listener, so a reload is
        enough; the ones read per action read the store directly.
        Nothing here needs a restart."""
        self.settings = Settings.from_dict(
            config.load_toml(self.path), path=self.path
        )
        for listener in self._listeners:
            listener(self.settings)
        return self.settings

    def _import_json(self) -> None:
        legacy = self.path.with_name("settings.json")
        if not legacy.exists():
            return
        try:
            data = json.loads(legacy.read_text(encoding="utf-8"))
        except ValueError as exc:
            config.record_error(legacy, f"is not valid JSON: {exc}")
            return
        self.settings = Settings.from_dict(data, path=legacy)
        self._write()

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
        self._write()
        for listener in self._listeners:
            listener(self.settings)

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            existing = self.path.read_text(encoding="utf-8")
        except OSError:
            existing = ""
        self.path.write_text(
            tomlwrite.merge(existing, asdict(self.settings)),
            encoding="utf-8",
        )
        # Our own write must not come back through the watcher as an
        # external edit (it would reload and re-notify for nothing).
        config.watcher.forget(self.path)


store = SettingsStore()
