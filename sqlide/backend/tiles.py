"""Slippy-map tiles: the maths, the cache, and the policy around them.

The geo viewer (PG-04) draws OpenStreetMap tiles under the geometries
it renders. That means talking to somebody else's server, so the rules
this module enforces are as much of the point as the arithmetic:

* **Attribution is not optional.** `TileSource.attribution` travels
  with the URL template and the map always draws it; a source
  configured without one is refused rather than silently unattributed.
* **Caching.** OSM's tile usage policy asks that clients cache. Tiles
  land in the config directory's `tiles/` folder, keyed by source, and
  are re-used until `MAX_AGE_DAYS`. A cached tile is served without a
  request even when the network is up, and *especially* when it is not.
* **A real User-Agent.** The policy requires identifying the app.
* **One request at a time, and never from the GTK loop.** `fetch` is
  called on a worker thread; nothing here touches the UI.
* **Offline is a state, not a timeout.** `TileSource.enabled` and the
  injectable `online` probe decide *before* a request whether the map
  draws tiles at all, so a disconnected laptop gets a plain background
  and a one-line notice instead of a screen that hangs for 30 seconds.
* **The URL is configurable** (`settings.map_tile_url`), so anyone with
  their own tile server — or a licence for someone else's — points at
  it and never touches openstreetmap.org.

Nothing in the test suite reaches the network: `fetch` takes the
transport as an argument, and the tests pass a fake.
"""

from __future__ import annotations

import hashlib
import math
import re
import time
from dataclasses import dataclass
from pathlib import Path

from sqlide.backend import config

DEFAULT_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
DEFAULT_ATTRIBUTION = "© OpenStreetMap contributors"
#: Sent on every tile request; OSM's policy requires an identifying
#: agent, and an app that hides behind a browser string gets blocked.
USER_AGENT = "sqlide/1.0 (https://github.com/jihedmastouri/sqlide)"
TILE_SIZE = 256
MIN_ZOOM = 0
#: OSM serves to 19; asking for more is a 404 per tile.
MAX_ZOOM = 19
#: How long a cached tile is used before it is fetched again.
MAX_AGE_DAYS = 30
#: Per-request budget. A tile server that is slow is, for the map's
#: purposes, a tile server that is down.
TIMEOUT_SECONDS = 5.0

_PLACEHOLDERS = ("{z}", "{x}", "{y}")


class TileError(RuntimeError):
    """A tile that could not be had — offline, refused or malformed."""


@dataclass(frozen=True)
class TileSource:
    """Where tiles come from and what must be printed under them."""

    url_template: str = DEFAULT_TILE_URL
    attribution: str = DEFAULT_ATTRIBUTION
    #: Master switch. Off means the map draws geometries on a plain
    #: background — which is also what a machine with no network gets.
    enabled: bool = True
    max_zoom: int = MAX_ZOOM

    def validate(self) -> str:
        """"" when the source is usable, else why it is not."""
        template = self.url_template.strip()
        if not template:
            return "no tile URL is set"
        if not template.startswith(("http://", "https://")):
            return "the tile URL must be an http(s) URL"
        missing = [p for p in _PLACEHOLDERS if p not in template]
        if missing:
            return f"the tile URL is missing {', '.join(missing)}"
        if not self.attribution.strip():
            return "a tile source must carry an attribution line"
        return ""

    @property
    def key(self) -> str:
        """A short stable id for this source, used as its cache folder
        so switching servers never serves the other one's tiles."""
        digest = hashlib.sha256(self.url_template.encode()).hexdigest()
        host = re.sub(r"[^a-z0-9]+", "-", self.url_template.lower())
        return f"{host[8:40].strip('-') or 'tiles'}-{digest[:8]}"

    def url(self, z: int, x: int, y: int) -> str:
        return (
            self.url_template
            .replace("{z}", str(z))
            .replace("{x}", str(x))
            .replace("{y}", str(y))
        )


def clamp_zoom(zoom: int, source: TileSource | None = None) -> int:
    top = source.max_zoom if source else MAX_ZOOM
    return max(MIN_ZOOM, min(top, int(zoom)))


def lonlat_to_pixel(lon: float, lat: float, zoom: int) -> tuple[float, float]:
    """World pixel coordinates at `zoom` (origin: top-left, 0/0 tile)."""
    lat = max(-85.05112878, min(85.05112878, lat))
    scale = TILE_SIZE * 2 ** zoom
    x = (lon + 180.0) / 360.0 * scale
    sin = math.sin(math.radians(lat))
    y = (0.5 - math.log((1 + sin) / (1 - sin)) / (4 * math.pi)) * scale
    return (x, y)


def pixel_to_lonlat(x: float, y: float, zoom: int) -> tuple[float, float]:
    scale = TILE_SIZE * 2 ** zoom
    lon = x / scale * 360.0 - 180.0
    lat = math.degrees(
        math.atan(math.sinh(math.pi * (1 - 2 * y / scale)))
    )
    return (lon, lat)


def fit_bounds(
    bounds: tuple[float, float, float, float],
    width: int,
    height: int,
    source: TileSource | None = None,
    padding: int = 24,
) -> tuple[float, float, int]:
    """(center lon, center lat, zoom) that fits `bounds` in a viewport.

    A single point has no extent, so it comes back at a readable street
    zoom rather than at "the whole planet" or "one building".
    """
    west, south, east, north = bounds
    center_lon = (west + east) / 2
    center_lat = (south + north) / 2
    if east <= west and north <= south:
        return (center_lon, center_lat, clamp_zoom(14, source))
    usable_w = max(32, width - 2 * padding)
    usable_h = max(32, height - 2 * padding)
    for zoom in range(clamp_zoom(MAX_ZOOM, source), MIN_ZOOM - 1, -1):
        x0, y0 = lonlat_to_pixel(west, north, zoom)
        x1, y1 = lonlat_to_pixel(east, south, zoom)
        if abs(x1 - x0) <= usable_w and abs(y1 - y0) <= usable_h:
            return (center_lon, center_lat, zoom)
    return (center_lon, center_lat, MIN_ZOOM)


def visible_tiles(
    center: tuple[float, float], zoom: int, width: int, height: int
) -> list[tuple[int, int, int, float, float]]:
    """The tiles covering a viewport: (z, x, y, screen x, screen y).

    Tiles off the top or bottom of the world are dropped; the x axis
    wraps, so panning past the date line keeps drawing.
    """
    cx, cy = lonlat_to_pixel(center[0], center[1], zoom)
    left = cx - width / 2
    top = cy - height / 2
    span = 2 ** zoom
    first_x = math.floor(left / TILE_SIZE)
    first_y = math.floor(top / TILE_SIZE)
    last_x = math.floor((left + width) / TILE_SIZE)
    last_y = math.floor((top + height) / TILE_SIZE)
    tiles = []
    for ty in range(first_y, last_y + 1):
        if ty < 0 or ty >= span:
            continue
        for tx in range(first_x, last_x + 1):
            tiles.append((
                zoom,
                tx % span,
                ty,
                tx * TILE_SIZE - left,
                ty * TILE_SIZE - top,
            ))
    return tiles


class TileCache:
    """Tiles on disk, with a small in-memory layer over it.

    The disk copy is what makes the app usable offline after it has
    been used online once, and what keeps a pan from re-requesting
    every tile it already drew.
    """

    def __init__(
        self, source: TileSource, directory: Path | None = None
    ) -> None:
        self.source = source
        base = directory or (config.config_dir() / "tiles")
        self.directory = Path(base) / source.key

    def path(self, z: int, x: int, y: int) -> Path:
        return self.directory / str(z) / str(x) / f"{y}.tile"

    def read(self, z: int, x: int, y: int, max_age_days: int = MAX_AGE_DAYS):
        """The cached tile, or None when it is missing or too old.

        `stale` tiles are still returned by `read_any`, which is what
        an offline map falls back to: an old tile is a better map than
        no map.
        """
        path = self.path(z, x, y)
        try:
            age = time.time() - path.stat().st_mtime
            if age > max_age_days * 86400:
                return None
            return path.read_bytes()
        except OSError:
            return None

    def read_any(self, z: int, x: int, y: int):
        try:
            return self.path(z, x, y).read_bytes()
        except OSError:
            return None

    def write(self, z: int, x: int, y: int, data: bytes) -> None:
        path = self.path(z, x, y)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except OSError:
            # A cache that cannot be written is a slow map, not a
            # broken one.
            pass

    def clear(self) -> None:
        for path in sorted(
            self.directory.rglob("*"), key=lambda p: -len(p.parts)
        ):
            try:
                path.unlink() if path.is_file() else path.rmdir()
            except OSError:
                pass


def http_get(url: str, timeout: float = TIMEOUT_SECONDS) -> bytes:
    """One tile over HTTP. The only place in the app that fetches a
    tile, and the only thing the tests replace."""
    import urllib.request

    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def probe_online(host: str = "tile.openstreetmap.org", port: int = 443) -> bool:
    """Whether a tile server can be reached at all, in one short
    connect. Replaced by the frontend with Gio.NetworkMonitor where
    that is available, and by the tests with a constant."""
    import socket

    try:
        socket.create_connection((host, port), timeout=1.5).close()
        return True
    except OSError:
        return False


class TileLoader:
    """Cache first, network second, never both if the first answered.

    `transport` and `online` are arguments rather than imports so the
    test suite can exercise every path — hit, miss, refusal, offline —
    without a socket. A source that fails `validate()`, is disabled, or
    reports offline serves whatever the cache already holds and raises
    `TileError` for anything else; the map turns that into a notice.
    """

    def __init__(
        self,
        source: TileSource,
        directory: Path | None = None,
        transport=http_get,
        online=probe_online,
        memory_tiles: int = 256,
    ) -> None:
        self.source = source
        self.cache = TileCache(source, directory)
        self._transport = transport
        self._online = online
        self._memory: dict[tuple[int, int, int], bytes] = {}
        self._memory_tiles = memory_tiles
        #: Set once a fetch fails, so a map on a dead network stops
        #: re-trying every tile of every pan.
        self.offline_reason = ""

    def available(self) -> str:
        """"" when tiles can be fetched, else why they cannot."""
        if not self.source.enabled:
            return "map tiles are turned off in Preferences"
        problem = self.source.validate()
        if problem:
            return problem
        if self.offline_reason:
            return self.offline_reason
        if not self._online():
            self.offline_reason = "no network connection — showing cached tiles"
            return self.offline_reason
        return ""

    def cached(self, z: int, x: int, y: int):
        """A tile already on hand, without any request. The map draws
        these synchronously; everything else is queued to a worker."""
        key = (z, x, y)
        if key in self._memory:
            return self._memory[key]
        data = self.cache.read(z, x, y)
        if data is None:
            return None
        self._remember(key, data)
        return data

    def fetch(self, z: int, x: int, y: int) -> bytes:
        """One tile, from the cache or the network. Worker thread only."""
        key = (z, x, y)
        data = self.cached(z, x, y)
        if data is not None:
            return data
        problem = self.available()
        if problem:
            stale = self.cache.read_any(z, x, y)
            if stale is not None:
                self._remember(key, stale)
                return stale
            raise TileError(problem)
        try:
            data = self._transport(self.source.url(z, x, y))
        except Exception as exc:  # any transport failure is "no tile"
            stale = self.cache.read_any(z, x, y)
            if stale is not None:
                self._remember(key, stale)
                return stale
            self.offline_reason = f"tile server unreachable: {exc}"
            raise TileError(self.offline_reason) from exc
        if not data:
            raise TileError("empty tile")
        self.cache.write(z, x, y, data)
        self._remember(key, data)
        return data

    def reset(self) -> None:
        """Try the network again — after the user reconnects, or
        changes the tile URL."""
        self.offline_reason = ""

    def _remember(self, key, data: bytes) -> None:
        if len(self._memory) >= self._memory_tiles:
            # Cheap eviction: the disk cache is the real one.
            self._memory.clear()
        self._memory[key] = data
