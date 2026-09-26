"""The overview of a whole server or a whole database (CORE-59).

A connection and a database are the two nodes of the tree whose
properties nobody reads as a list of attributes. What is asked of them
is "how big is this, and is it healthy right now" — so their descriptor
carries, beyond the key/value summary every object has, two things the
info view draws as panels instead of rows:

* **counts** — the tiles across the top: how many tables, views,
  functions, indexes a database holds; how many databases, schemas,
  roles and extensions a server does. They come from the catalog the
  descriptor was reading anyway.
* **meters** — the LIVE row: a handful of numbers that move, each with
  the fraction of its ceiling that draws the bar under it. Size on
  disk, connections against `max_connections`, cache hit ratio, uptime.

Both are engine-specific and *optional*: every query here is wrapped,
and a meter the server will not answer (no `pg_stat_statements`, an
account without the PROCESS privilege) is simply not in the list rather
than being a panel showing "—". A connection whose engine has no live
numbers at all — SQLite is a file, not a server — gets an empty tuple
and the LIVE row is not drawn.

Nothing here is GTK-aware and everything queries, so it runs on the
worker thread that reads the descriptor (frontend/util.run_async).

This module deliberately knows no more about an engine than the SQL it
needs: `db/metrics.py` remains the monitoring dashboard's sampler, on
its own connection and its own timer. The two overlap in subject and
not in code — a dashboard polls every two seconds and keeps history,
an overview asks once when a tab is opened.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from sqlide.backend.db.base import Connector, ResultSet

#: Fraction a meter reports when it has no ceiling to be a fraction of
#: (uptime, a latency): the panel draws the value and no bar.
NO_BAR = -1.0


@dataclass(frozen=True)
class Count:
    """One tile: what there are, and how many of them."""

    label: str
    value: str
    #: The section slug this tile counts, where the same descriptor has
    #: one, so the tile can be the way into that listing. Empty for a
    #: count nothing is listed for.
    section: str = ""


@dataclass(frozen=True)
class Meter:
    """One live panel: a number now, and how full it is."""

    label: str
    #: The value as it is shown — already formatted, units and all,
    #: because only this module knows whether "3 / 100" is a ratio, a
    #: percentage or a duration.
    value: str
    #: The small caption to the right of the label, saying what the
    #: number is *of*: "pool", "since restart", "last 5 min".
    note: str = ""
    #: 0..1 for the bar under the value, or NO_BAR for a number with no
    #: ceiling.
    fraction: float = NO_BAR
    #: How to colour the bar: "" (accent), "good", "warn".
    tone: str = ""


@dataclass(frozen=True)
class Overview:
    """The panels above an object's properties, if it has any."""

    counts: tuple[Count, ...] = ()
    meters: tuple[Meter, ...] = ()
    #: Where this is, in the words a header shows under the name:
    #: "postgres 10 ▸ demo", "postgres@localhost:5432".
    endpoint: str = ""

    def __bool__(self) -> bool:
        return bool(self.counts or self.meters)


# Reading, defensively


def _safe(call, default=None):
    """One optional question. A server that refuses it costs that
    panel, never the descriptor."""
    try:
        return call()
    except Exception:
        return default


def _rows(connector: Connector, sql: str) -> list[tuple]:
    result = connector.execute(sql)
    if not isinstance(result, ResultSet):
        return []
    return [tuple(row) for row in result.rows]


def scalar(connector: Connector, sql: str):
    """One value out of a one-row query, or None. Public because the
    descriptor builders ask their own small catalog questions (a
    database's owner, its encoding) through the same plumbing."""
    rows = _rows(connector, sql)
    return rows[0][0] if rows and rows[0] else None


_scalar = scalar


def _number(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _engine(kind: str) -> str:
    engine = (kind or "").lower()
    if engine in ("postgresql", "postgres"):
        return "postgres"
    if engine in ("mysql", "mariadb"):
        return "mysql"
    return engine


def engine_of(connector: Connector) -> str:
    """Which engine this connector speaks, from where its class lives.

    The descriptor builders are handed a connector and not the profile
    that made it, and every question in this module is engine-shaped;
    the adapter package name is the one answer that needs no new method
    on `Connector` and no plumbing through `describe`.
    """
    module = type(connector).__module__ or ""
    for part in module.split("."):
        if part in ("postgres", "mysql", "sqlite", "jdbc"):
            return part
    return _engine(getattr(connector, "kind", ""))


# Facts about the connection itself, which the profile also knows but
# the descriptor is not given: read off the connector, which was built
# from that profile, so an adapter that has no host (SQLite, JDBC)
# simply answers with what it does have.


def endpoint(connector: Connector) -> str:
    """"host:port", or the file a file-backed engine opened."""
    path = getattr(connector, "path", "") or getattr(
        connector, "file_path", ""
    )
    if path:
        return str(path)
    url = getattr(connector, "url", "")
    if url:
        return str(url)
    host = getattr(connector, "host", "")
    if not host:
        return ""
    port = getattr(connector, "port", 0)
    return f"{host}:{port}" if port else str(host)


def account(connector: Connector) -> str:
    return str(getattr(connector, "user", "") or "")


def ssl_state(connector: Connector) -> str:
    """"disabled", or the SSL mode the connection was made with."""
    ssl = getattr(connector, "ssl", None)
    if not ssl:
        return "disabled"
    mode = ssl.get("mode") if isinstance(ssl, dict) else ""
    return str(mode or "enabled")


def server_version(kind: str, connector: Connector) -> str:
    """The server's own version string, short enough for a row."""
    engine = _engine(kind)
    if engine == "postgres":
        # "10.21 (Debian 10.21-1.pgdg90+1)" is the packaging, not the
        # version: a properties row wants the number.
        version = str(_safe(
            lambda: _scalar(connector, "SHOW server_version"), ""
        ) or "")
        return version.split()[0] if version else ""
    if engine == "mysql":
        return str(_safe(
            lambda: _scalar(connector, "SELECT version()"), ""
        ) or "")
    if engine == "sqlite":
        return str(_safe(
            lambda: _scalar(connector, "SELECT sqlite_version()"), ""
        ) or "")
    return ""


# The live row


def connection_meters(kind: str, connector: Connector) -> tuple[Meter, ...]:
    """What a whole server says about itself right now."""
    engine = _engine(kind)
    meters: list[Meter] = []
    if engine == "postgres":
        meters += _pg_connection_meters(connector)
    elif engine == "mysql":
        meters += _mysql_connection_meters(connector)
    else:
        # A file-backed engine has no round trip to measure and no
        # server to be up: whatever it has to say is said per database.
        return ()
    latency = _ping(connector)
    if latency is not None:
        meters.insert(
            min(1, len(meters)),
            Meter(
                label="Round-trip latency",
                value=format_ms(latency),
                note="last ping",
                # A round trip has no ceiling; the bar reads as "how
                # far into a tenth of a second", which is the range a
                # local server and a distant one differ across.
                fraction=min(1.0, latency / 100.0),
                tone="good" if latency < 25 else "warn",
            ),
        )
    return tuple(meters)


def database_meters(
    kind: str, connector: Connector, database: str
) -> tuple[Meter, ...]:
    """What one database says about itself right now."""
    engine = _engine(kind)
    if engine == "postgres":
        return tuple(_pg_database_meters(connector, database))
    if engine == "mysql":
        return tuple(_mysql_database_meters(connector, database))
    if engine == "sqlite":
        return tuple(_sqlite_database_meters(connector))
    return ()


def _ping(connector: Connector) -> float | None:
    """One round trip, in milliseconds — the only number here the
    client measures rather than the server reporting."""
    started = time.monotonic()
    if _safe(lambda: _scalar(connector, "SELECT 1"), None) is None:
        return None
    return (time.monotonic() - started) * 1000.0


_PG_CONNECTIONS = """
SELECT (SELECT count(*) FROM pg_stat_activity),
       (SELECT setting::bigint FROM pg_settings
         WHERE name = 'max_connections')
"""

_PG_CACHE = """
SELECT coalesce(sum(blks_hit), 0), coalesce(sum(blks_read), 0)
FROM pg_stat_database
"""


def _pg_connection_meters(connector: Connector) -> list[Meter]:
    meters: list[Meter] = []
    rows = _safe(lambda: _rows(connector, _PG_CONNECTIONS), [])
    if rows:
        meters.append(_pool_meter(_number(rows[0][0]), _number(rows[0][1])))
    rows = _safe(lambda: _rows(
        connector,
        "SELECT EXTRACT(EPOCH FROM (now() - pg_postmaster_start_time())),"
        " to_char(pg_postmaster_start_time(), 'DD Mon')",
    ), [])
    if rows and _number(rows[0][0]) is not None:
        meters.append(_uptime_meter(_number(rows[0][0]), str(rows[0][1])))
    rows = _safe(lambda: _rows(connector, _PG_CACHE), [])
    if rows:
        meters.append(_cache_meter(
            _number(rows[0][0]), _number(rows[0][1]), "cluster"
        ))
    return [m for m in meters if m is not None]


def _mysql_connection_meters(connector: Connector) -> list[Meter]:
    status = _safe(lambda: {
        str(row[0]): row[-1]
        for row in _rows(connector, "SHOW GLOBAL STATUS")
        if row
    }, {}) or {}
    ceiling = None
    for row in _safe(lambda: _rows(
        connector, "SHOW GLOBAL VARIABLES LIKE 'max_connections'"
    ), []) or []:
        ceiling = _number(row[-1])
    meters: list[Meter] = []
    in_use = _number(status.get("Threads_connected"))
    if in_use is not None:
        meters.append(_pool_meter(in_use, ceiling))
    uptime = _number(status.get("Uptime"))
    if uptime is not None:
        meters.append(_uptime_meter(uptime, ""))
    hits = _number(status.get("Innodb_buffer_pool_read_requests"))
    misses = _number(status.get("Innodb_buffer_pool_reads"))
    if hits is not None and misses is not None:
        meters.append(_cache_meter(hits, misses, "buffer pool"))
    return [m for m in meters if m is not None]


_PG_DB_SIZE = "SELECT pg_database_size(current_database())"

_PG_DB_CONNECTIONS = """
SELECT (SELECT count(*) FROM pg_stat_activity
         WHERE datname = current_database()),
       (SELECT setting::bigint FROM pg_settings
         WHERE name = 'max_connections')
"""

_PG_DB_CACHE = """
SELECT coalesce(blks_hit, 0), coalesce(blks_read, 0)
FROM pg_stat_database WHERE datname = current_database()
"""

#: Average statement time, where the server has pg_stat_statements. It
#: cannot be installed from a client (it needs a restart with
#: shared_preload_libraries), so the panel is absent rather than empty
#: on the servers that do not have it.
_PG_DB_QUERY_TIME = """
SELECT sum(total_exec_time) / nullif(sum(calls), 0)
FROM pg_stat_statements s
JOIN pg_database d ON d.oid = s.dbid
WHERE d.datname = current_database()
"""


def _pg_database_meters(connector: Connector, database: str) -> list[Meter]:
    meters: list[Meter] = []
    size = _number(_safe(lambda: _scalar(connector, _PG_DB_SIZE), None))
    if size is not None:
        meters.append(_size_meter(size, database))
    rows = _safe(lambda: _rows(connector, _PG_DB_CONNECTIONS), [])
    if rows:
        meters.append(_pool_meter(
            _number(rows[0][0]), _number(rows[0][1]),
            label="Active connections",
        ))
    rows = _safe(lambda: _rows(connector, _PG_DB_CACHE), [])
    if rows:
        meters.append(_cache_meter(
            _number(rows[0][0]), _number(rows[0][1]), "since restart"
        ))
    average = _number(_safe(
        lambda: _scalar(connector, _PG_DB_QUERY_TIME), None
    ))
    if average is not None:
        meters.append(Meter(
            label="Avg query time",
            value=format_ms(average),
            note="all statements",
            fraction=min(1.0, average / 100.0),
            tone="good" if average < 20 else "warn",
        ))
    return [m for m in meters if m is not None]


_MYSQL_DB_SIZE = """
SELECT coalesce(SUM(data_length + index_length), 0)
FROM information_schema.tables WHERE table_schema = database()
"""


def _mysql_database_meters(
    connector: Connector, database: str
) -> list[Meter]:
    meters: list[Meter] = []
    size = _number(_safe(lambda: _scalar(connector, _MYSQL_DB_SIZE), None))
    if size is not None:
        meters.append(_size_meter(size, database))
    rows = _safe(lambda: _rows(
        connector,
        "SELECT count(*) FROM information_schema.processlist "
        "WHERE db = database()",
    ), [])
    ceiling = None
    for row in _safe(lambda: _rows(
        connector, "SHOW GLOBAL VARIABLES LIKE 'max_connections'"
    ), []) or []:
        ceiling = _number(row[-1])
    if rows:
        # Against the server's whole pool, as PostgreSQL's is: the
        # ceiling belongs to the server either way, and a count with no
        # ceiling says nothing about how close to full it is.
        meters.append(_pool_meter(
            _number(rows[0][0]), ceiling, label="Active connections"
        ))
    # The buffer pool is the server's, but it is the number that says
    # whether reads here are hitting memory, so it ends the row.
    return [m for m in meters if m is not None] + [
        m for m in _mysql_connection_meters(connector)
        if m.label == "Cache hit ratio"
    ]


def _sqlite_database_meters(connector: Connector) -> list[Meter]:
    """A file, not a server: its size is the one live number there is,
    and it is the honest one."""
    rows = _safe(lambda: _rows(
        connector, "PRAGMA page_count"
    ), []) or []
    pages = _number(rows[0][0]) if rows else None
    rows = _safe(lambda: _rows(connector, "PRAGMA page_size"), []) or []
    page_size = _number(rows[0][0]) if rows else None
    if pages is None or page_size is None:
        return []
    return [_size_meter(pages * page_size, "file")]


# The shapes a meter comes in, so an engine only supplies numbers


def _pool_meter(
    in_use: float | None,
    ceiling: float | None,
    label: str = "Connections in use",
) -> Meter | None:
    if in_use is None:
        return None
    if not ceiling:
        return Meter(label=label, value=f"{int(in_use)}", note="")
    fraction = min(1.0, in_use / ceiling)
    return Meter(
        label=label,
        value=f"{int(in_use)} / {int(ceiling)}",
        note="pool",
        fraction=fraction,
        tone="warn" if fraction > 0.8 else "",
    )


def _uptime_meter(seconds: float | None, since: str) -> Meter | None:
    if seconds is None:
        return None
    return Meter(
        label="Server uptime",
        value=format_uptime(seconds),
        note=f"since {since}" if since else "",
        # A week reads as "settled"; the bar fills across the first one
        # so a server that restarted an hour ago is visibly different.
        fraction=min(1.0, seconds / (7 * 86400.0)),
    )


def _cache_meter(
    hits: float | None, misses: float | None, note: str
) -> Meter | None:
    if hits is None or misses is None or (hits + misses) <= 0:
        return None
    ratio = hits / (hits + misses)
    return Meter(
        label="Cache hit ratio",
        value=f"{ratio * 100:.1f} %",
        note=note,
        fraction=ratio,
        tone="good" if ratio >= 0.95 else "warn",
    )


def _size_meter(size: float, where: str) -> Meter:
    return Meter(
        label="Size on disk",
        value=format_size(size),
        note=where,
        # Sizes have no ceiling either, but a bar that grows through
        # the gigabyte range says more at a glance than none at all.
        fraction=min(1.0, size / float(10 * 1024 ** 3)),
    )


# Formatting — shared, so a size reads the same in a meter and a row


def format_size(size: float | None) -> str:
    if size is None:
        return "—"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if value >= 10 or unit == "B" \
                else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.0f} TB"


def format_ms(milliseconds: float | None) -> str:
    if milliseconds is None:
        return "—"
    if milliseconds < 10:
        return f"{milliseconds:.1f} ms"
    if milliseconds < 1000:
        return f"{milliseconds:.0f} ms"
    return f"{milliseconds / 1000:.1f} s"


def format_uptime(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def format_count(value: float | None) -> str:
    """A row count with thousands separated, or an em dash."""
    if value is None:
        return "—"
    return f"{int(value):,}"


# The listing beside the overview: what each table weighs


_PG_OBJECT_SIZES = """
SELECT c.relname,
       CASE WHEN c.reltuples < 0 THEN NULL ELSE c.reltuples END,
       pg_total_relation_size(c.oid)
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p', 'm')
  AND n.nspname = current_schema()
"""

_MYSQL_OBJECT_SIZES = """
SELECT table_name, table_rows, data_length + index_length
FROM information_schema.tables
WHERE table_schema = database()
"""


def object_sizes(
    kind: str, connector: Connector
) -> dict[str, tuple[float | None, float | None]]:
    """Row estimate and bytes on disk per table, in one query.

    Estimates, deliberately: the catalog's own numbers (`reltuples`,
    `information_schema.table_rows`) cost nothing, where counting the
    rows of every table in a database to fill a listing would cost more
    than the listing is worth. A view — which has neither — is simply
    not in the mapping, and the listing shows an em dash for it.
    """
    engine = _engine(kind)
    if engine == "postgres":
        rows = _safe(lambda: _rows(connector, _PG_OBJECT_SIZES), []) or []
    elif engine == "mysql":
        rows = _safe(lambda: _rows(connector, _MYSQL_OBJECT_SIZES), []) or []
    else:
        return {}
    return {
        str(row[0]): (_number(row[1]), _number(row[2]))
        for row in rows
        if len(row) >= 3
    }
