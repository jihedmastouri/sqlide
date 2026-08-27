"""Sampling the server: what the monitoring dashboard draws (CORE-15).

`monitoring.py` (CORE-14) answers *what this connection may read*; this
module answers *what it says right now*. The split is deliberate — the
probe runs once when the dashboard opens and decides which panels exist,
and this module is then called on a timer for as long as it stays open.

The shape follows docs/monitoring-spike.md, and three of its findings
are load-bearing here:

1. **Everything is a cumulative counter.** `xact_commit`, `Questions`,
   `blks_hit` and friends count since the server started or since a
   stats reset, so the useful number is the first difference over the
   poll interval. `Series` does that, and restarts the line rather than
   drawing a negative spike when a counter goes backwards (a restart,
   or `pg_stat_reset()`).
2. **Storage is the expensive question and it barely moves.** It is a
   separate call (`storage()`) for a separate, much slower timer; a poll
   of the live panels (`sample()`) is three small system-view queries.
3. **Silence is the dangerous failure.** A sample carries `masked`,
   which is true when PostgreSQL blanked other sessions' SQL or MySQL
   listed only this account's threads. The dashboard must say so; a
   nearly empty session list is otherwise indistinguishable from an
   idle server.

Nothing here is GTK-aware and every function issues queries, so all of
it belongs on a worker thread — and on a connection of its own, never
the one the user's tabs are running statements through.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from sqlide.backend.db.base import Connector, ConnectorError, ResultSet
from sqlide.backend.db.monitoring import masked_sessions

#: How much history a dashboard keeps, in seconds. Nothing is persisted:
#: closing the view forgets it.
WINDOW_SECONDS = 300.0

#: Poll interval bounds for the live panels, and the default. The spike
#: measured a full sample at 1–2 ms of server work, which makes 2 s
#: about 0.1% of one core — safe on production.
MIN_INTERVAL = 1
MAX_INTERVAL = 60
DEFAULT_INTERVAL = 2

def clamp_interval(seconds: int) -> int:
    """A poll interval held to its allowed range — the one place the
    limits are applied, whether the number came from the dashboard's
    spin control or from a hand-edited settings.toml."""
    return max(MIN_INTERVAL, min(MAX_INTERVAL, int(seconds)))


#: The storage panel's own, much slower timer.
STORAGE_INTERVAL = 60

#: Said once, in words, in the dashboard footer. The host's CPU, RAM and
#: disk are not exposed over SQL by either engine, and a proxy for them
#: would be a lie (see the spike's "hard limit").
HOST_METRICS_NOTE = (
    "sqlide reads only what the server reports over SQL. Host CPU, "
    "memory and disk space need an agent, SSH or a metrics endpoint, so "
    "they are not shown rather than guessed at."
)


@dataclass(frozen=True)
class Session:
    """One backend/thread, in the columns both engines can answer."""

    id: str
    user: str
    database: str
    state: str
    seconds: float | None
    wait: str
    query: str
    #: True for the monitoring connection itself: killing it would kill
    #: the dashboard, so the UI greys the button rather than letting it.
    is_self: bool = False

    @property
    def cells(self) -> tuple[str, ...]:
        return (
            self.id,
            self.user,
            self.database,
            self.state,
            format_duration(self.seconds),
            self.wait,
            " ".join(self.query.split()),
        )


SESSION_COLUMNS: tuple[str, ...] = (
    "ID", "User", "Database", "State", "Duration", "Waiting on", "Query",
)


@dataclass(frozen=True)
class Blocked:
    """A session waiting on another one's lock (PostgreSQL). MySQL
    reports lock waiting as counters, not as a pairing a client can
    read without performance_schema, so the list stays empty there."""

    id: str
    blocked_by: str
    query: str


@dataclass(frozen=True)
class Sample:
    """One poll of the live panels."""

    at: float
    #: Cumulative counters, charted as first differences.
    counters: dict[str, float] = field(default_factory=dict)
    #: Values that are already a level, charted as they are.
    gauges: dict[str, float] = field(default_factory=dict)
    sessions: tuple[Session, ...] = ()
    blocked: tuple[Blocked, ...] = ()
    #: The server showed less than the whole truth (see the module
    #: docstring); the text says which case.
    masked: str = ""


@dataclass(frozen=True)
class Chart:
    """One line on the dashboard.

    `kind` says how the counters underneath become a number:

    * ``rate`` — first difference of one counter, per second;
    * ``gauge`` — a level, plotted as read (optionally against a
      ceiling gauge, e.g. sessions against ``max_connections``);
    * ``percent`` — a ratio of two counters' *deltas*, in percent. Cache
      hit ratio is this and not the lifetime total: a server that has
      been up for a month averages away the minute you are watching.
    """

    name: str
    title: str
    kind: str
    keys: tuple[str, ...]
    unit: str = ""
    ceiling: str = ""


_PG_CHARTS: tuple[Chart, ...] = (
    Chart("sessions", "Sessions", "gauge", ("backends",),
          ceiling="max_connections"),
    Chart("commits", "Transactions", "rate", ("xact_commit",), unit="/s"),
    Chart("rollbacks", "Rollbacks", "rate", ("xact_rollback",), unit="/s"),
    Chart("cache", "Cache hit ratio", "percent", ("blks_hit", "blks_read"),
          unit="%"),
    Chart("locks", "Locks waiting", "gauge", ("locks_waiting",)),
    Chart("deadlocks", "Deadlocks", "rate", ("deadlocks",), unit="/s"),
)

_MYSQL_CHARTS: tuple[Chart, ...] = (
    Chart("sessions", "Sessions", "gauge", ("Threads_connected",),
          ceiling="max_connections"),
    Chart("queries", "Queries", "rate", ("Questions",), unit="/s"),
    Chart("commits", "Commits", "rate", ("Com_commit",), unit="/s"),
    Chart(
        "cache", "Buffer pool hit ratio", "percent",
        ("Innodb_buffer_pool_read_requests", "Innodb_buffer_pool_reads"),
        unit="%",
    ),
    Chart("locks", "Lock waits", "rate",
          ("Innodb_row_lock_waits",), unit="/s"),
    Chart("table_locks", "Table lock waits", "rate",
          ("Table_locks_waited",), unit="/s"),
)

_CHARTS: dict[str, tuple[Chart, ...]] = {
    "postgres": _PG_CHARTS,
    "postgresql": _PG_CHARTS,
    "mysql": _MYSQL_CHARTS,
    "mariadb": _MYSQL_CHARTS,
}


def charts(kind: str) -> tuple[Chart, ...]:
    """The charts this engine can fill. Empty for an engine with no
    server to watch, which is how the caller knows to offer nothing."""
    return _CHARTS.get(kind.lower(), ())


def supported(kind: str) -> bool:
    return bool(charts(kind))


# Sampling


def sample(kind: str, connector: Connector) -> Sample:
    """One poll of the live panels: sessions, counters and locks.

    Raises ConnectorError only if the connection itself is gone —
    every panel that may be refused for want of a privilege is caught
    here and left empty, because the probe has already told the UI why.
    """
    engine = kind.lower()
    if engine not in _CHARTS:
        return Sample(at=time.monotonic())
    # The one query allowed to raise: if the monitoring connection has
    # dropped, the dashboard must say so rather than draw flat lines
    # forever off panels that all swallow their own failures.
    _scalar(connector, "SELECT 1")
    if engine in ("mysql", "mariadb"):
        return _sample_mysql(connector)
    return _sample_postgres(connector)


_PG_SESSIONS = """
SELECT pid,
       coalesce(usename, ''),
       coalesce(datname, ''),
       coalesce(state, ''),
       EXTRACT(EPOCH FROM (now() - coalesce(query_start, xact_start,
                                            backend_start))),
       coalesce(wait_event_type, ''),
       coalesce(query, ''),
       pid = pg_backend_pid()
FROM pg_stat_activity
ORDER BY 5 DESC NULLS LAST
"""

_PG_COUNTERS = """
SELECT coalesce(sum(xact_commit), 0),
       coalesce(sum(xact_rollback), 0),
       coalesce(sum(blks_hit), 0),
       coalesce(sum(blks_read), 0),
       coalesce(sum(tup_returned), 0),
       coalesce(sum(deadlocks), 0),
       coalesce(sum(temp_bytes), 0),
       coalesce(sum(numbackends), 0),
       (SELECT setting::bigint FROM pg_settings
         WHERE name = 'max_connections')
FROM pg_stat_database
"""

_PG_LOCKS = """
SELECT count(*) FILTER (WHERE granted),
       count(*) FILTER (WHERE NOT granted)
FROM pg_locks
"""

_PG_BLOCKED = """
SELECT pid, pg_blocking_pids(pid), coalesce(query, '')
FROM pg_stat_activity
WHERE cardinality(pg_blocking_pids(pid)) > 0
"""


def _sample_postgres(connector: Connector) -> Sample:
    rows = _rows(connector, _PG_SESSIONS)
    sessions = tuple(
        Session(
            id=_text(row[0]),
            user=_text(row[1]),
            database=_text(row[2]),
            state=_text(row[3]),
            seconds=_number(row[4]),
            wait=_text(row[5]),
            query=_text(row[6]),
            is_self=bool(row[7]),
        )
        for row in rows
        if len(row) >= 8
    )
    masked = ""
    if masked_sessions([(session.query,) for session in sessions]):
        masked = (
            "Other sessions' state and SQL are hidden: this account holds "
            "neither pg_read_all_stats nor pg_monitor. GRANT pg_monitor "
            "TO the role to see the whole server."
        )

    counters: dict[str, float] = {}
    gauges: dict[str, float] = {}
    totals = _rows(connector, _PG_COUNTERS)
    if totals:
        names = (
            "xact_commit", "xact_rollback", "blks_hit", "blks_read",
            "tup_returned", "deadlocks", "temp_bytes",
        )
        for index, name in enumerate(names):
            value = _number(totals[0][index])
            if value is not None:
                counters[name] = value
        backends = _number(totals[0][7])
        gauges["backends"] = float(len(sessions) or (backends or 0))
        ceiling = _number(totals[0][8])
        if ceiling:
            gauges["max_connections"] = ceiling

    locks = _rows(connector, _PG_LOCKS)
    if locks:
        gauges["locks_held"] = _number(locks[0][0]) or 0.0
        gauges["locks_waiting"] = _number(locks[0][1]) or 0.0

    blocked = tuple(
        Blocked(
            id=_text(row[0]),
            blocked_by=", ".join(str(pid) for pid in (row[1] or [])),
            query=_text(row[2]),
        )
        for row in _rows(connector, _PG_BLOCKED)
        if len(row) >= 3
    )
    return Sample(
        at=time.monotonic(),
        counters=counters,
        gauges=gauges,
        sessions=sessions,
        blocked=blocked,
        masked=masked,
    )


#: The counters the MySQL charts read out of SHOW GLOBAL STATUS. The
#: whole status set is ~500 rows and one round trip either way, so it is
#: read in one call and picked over here.
_MYSQL_COUNTERS: tuple[str, ...] = (
    "Questions",
    "Com_commit",
    "Com_rollback",
    "Innodb_buffer_pool_read_requests",
    "Innodb_buffer_pool_reads",
    "Innodb_row_lock_waits",
    "Table_locks_waited",
    "Slow_queries",
    "Aborted_connects",
    "Created_tmp_disk_tables",
)

_MYSQL_GAUGES: tuple[str, ...] = ("Threads_connected", "Threads_running")


def _sample_mysql(connector: Connector) -> Sample:
    status = {
        _text(row[0]): row[-1]
        for row in _rows(connector, "SHOW GLOBAL STATUS")
        if row
    }
    counters = {
        name: value
        for name in _MYSQL_COUNTERS
        if (value := _number(status.get(name))) is not None
    }
    gauges = {
        name: value
        for name in _MYSQL_GAUGES
        if (value := _number(status.get(name))) is not None
    }
    for row in _rows(connector, "SHOW GLOBAL VARIABLES LIKE 'max_connections'"):
        ceiling = _number(row[-1])
        if ceiling:
            gauges["max_connections"] = ceiling

    rows = _rows(connector, "SHOW FULL PROCESSLIST")
    own = _scalar(connector, "SELECT CONNECTION_ID()")
    sessions = tuple(
        Session(
            id=_text(row[0]),
            user=_text(row[1]),
            database=_text(row[3]),
            state=" · ".join(
                part for part in (_text(row[4]), _text(row[6])) if part
            ),
            seconds=_number(row[5]),
            wait=_text(row[6]),
            query=_text(row[7]) if len(row) > 7 else "",
            is_self=_text(row[0]) == _text(own),
        )
        for row in rows
        if len(row) >= 7
    )
    masked = ""
    connected = gauges.get("Threads_connected")
    if connected is not None and connected > len(sessions):
        masked = (
            f"Only this account's threads are listed: the server reports "
            f"{int(connected)} connections and SHOW PROCESSLIST returned "
            f"{len(sessions)}. Seeing the rest needs the PROCESS privilege."
        )
    return Sample(
        at=time.monotonic(),
        counters=counters,
        gauges=gauges,
        sessions=sessions,
        masked=masked,
    )


# Storage — its own, much slower timer (see the module docstring).


@dataclass(frozen=True)
class Storage:
    databases: tuple[tuple[str, int | None], ...] = ()
    tables: tuple[tuple[str, int | None], ...] = ()
    #: Why part of it is missing, when part of it is.
    detail: str = ""


#: How many of the largest tables the storage panel lists. Long enough
#: to find the table that grew, short enough that the query stays the
#: cheap end of the spike's measurements.
TOP_TABLES = 20

_PG_DATABASE_SIZES = """
SELECT datname,
       CASE WHEN has_database_privilege(datname, 'CONNECT')
            THEN pg_database_size(datname) END
FROM pg_database
WHERE datallowconn
ORDER BY 2 DESC NULLS LAST
"""

_PG_TABLE_SIZES = f"""
SELECT schemaname || '.' || relname, pg_total_relation_size(relid)
FROM pg_stat_user_tables
ORDER BY 2 DESC NULLS LAST
LIMIT {TOP_TABLES}
"""

_MYSQL_DATABASE_SIZES = """
SELECT table_schema, SUM(data_length + index_length)
FROM information_schema.tables
GROUP BY table_schema
ORDER BY 2 DESC
"""

_MYSQL_TABLE_SIZES = f"""
SELECT CONCAT(table_schema, '.', table_name), data_length + index_length
FROM information_schema.tables
WHERE table_type = 'BASE TABLE'
ORDER BY 2 DESC
LIMIT {TOP_TABLES}
"""


def storage(kind: str, connector: Connector) -> Storage:
    """Database sizes and the largest tables.

    The expensive query of the set — it scales with the number of
    relations — and the one that changes slowest, so it gets its own
    timer. A database sqlide may not connect to reports no size rather
    than an error, and the panel says so.
    """
    engine = kind.lower()
    if engine in ("postgres", "postgresql"):
        databases = _size_rows(connector, _PG_DATABASE_SIZES)
        tables = _size_rows(connector, _PG_TABLE_SIZES)
        detail = ""
        if any(size is None for _name, size in databases):
            detail = (
                "Sizes are blank for the databases this account may not "
                "connect to: PostgreSQL refuses pg_database_size() there."
            )
        return Storage(databases, tables, detail)
    if engine in ("mysql", "mariadb"):
        return Storage(
            _size_rows(connector, _MYSQL_DATABASE_SIZES),
            _size_rows(connector, _MYSQL_TABLE_SIZES),
            "information_schema lists only the schemas this account may "
            "see, so a bigger database elsewhere on the server is not "
            "counted here.",
        )
    return Storage()


def _size_rows(
    connector: Connector, sql: str
) -> tuple[tuple[str, int | None], ...]:
    out: list[tuple[str, int | None]] = []
    for row in _rows(connector, sql):
        if len(row) < 2:
            continue
        size = _number(row[1])
        out.append((_text(row[0]), None if size is None else int(size)))
    return tuple(out)


# Cancelling and killing a session


@dataclass(frozen=True)
class SignalRights:
    """Whether this connection may stop other people's sessions, and
    why not when it may not. Checked before the buttons are offered:
    a Kill that always fails is worse than no Kill."""

    others: bool
    detail: str = ""


_PG_RIGHTS = """
SELECT current_setting('is_superuser') = 'on'
    OR EXISTS (SELECT 1 FROM pg_roles
                WHERE rolname = 'pg_signal_backend'
                  AND pg_has_role(current_user, oid, 'member'))
"""


def signal_rights(kind: str, connector: Connector) -> SignalRights:
    engine = kind.lower()
    if engine in ("postgres", "postgresql"):
        try:
            allowed = bool(_scalar(connector, _PG_RIGHTS))
        except ConnectorError as exc:
            return SignalRights(False, str(exc))
        if allowed:
            return SignalRights(True)
        return SignalRights(
            False,
            "Cancelling or ending another role's session needs membership "
            "of pg_signal_backend (or superuser). Sessions belonging to "
            "roles this account is a member of can still be stopped, and "
            "PostgreSQL refuses the rest.",
        )
    if engine in ("mysql", "mariadb"):
        try:
            grants = _rows(connector, "SHOW GRANTS")
        except ConnectorError as exc:
            return SignalRights(False, str(exc))
        text = " ".join(_text(row[0]).upper() for row in grants if row)
        if any(
            right in text
            for right in ("SUPER", "CONNECTION_ADMIN", "ALL PRIVILEGES ON *.*")
        ):
            return SignalRights(True)
        return SignalRights(
            False,
            "Killing another account's thread needs CONNECTION_ADMIN "
            "(MySQL 8.0) or SUPER (5.7). This account may still kill its "
            "own threads.",
        )
    return SignalRights(False, "This engine has no sessions to stop.")


#: What came back when the session had already gone. Both engines say so
#: rather than failing, and it is a lost race, not an error: by the time
#: the click arrived the query had finished on its own.
GONE = "That session had already finished — nothing to stop."


def cancel_session(kind: str, connector: Connector, session_id: str) -> str:
    """Stop the running statement, leaving the connection open."""
    return _signal(kind, connector, session_id, terminate=False)


def terminate_session(kind: str, connector: Connector, session_id: str) -> str:
    """Disconnect the session, statement and all."""
    return _signal(kind, connector, session_id, terminate=True)


def _signal(
    kind: str, connector: Connector, session_id: str, *, terminate: bool
) -> str:
    engine = kind.lower()
    ident = _session_id(session_id)
    if engine in ("postgres", "postgresql"):
        function = "pg_terminate_backend" if terminate else "pg_cancel_backend"
        answered = _scalar(connector, f"SELECT {function}({ident})")
        if answered is False or answered in (0, "f", "false"):
            return GONE
        return (
            f"Session {ident} ended." if terminate
            else f"Cancelled the statement running in session {ident}."
        )
    if engine in ("mysql", "mariadb"):
        verb = "CONNECTION" if terminate else "QUERY"
        try:
            connector.execute(f"KILL {verb} {ident}")
        except ConnectorError as exc:
            if "unknown thread id" in str(exc).lower():
                return GONE
            raise
        return (
            f"Thread {ident} killed." if terminate
            else f"Cancelled the query running in thread {ident}."
        )
    raise ConnectorError("This engine has no sessions to stop.")


def _session_id(session_id: str) -> int:
    """A session id as an integer, because it goes into SQL unquoted.
    Both engines number their backends/threads, so anything else is a
    bug or a tampered row, not a session."""
    try:
        return int(str(session_id).strip())
    except (TypeError, ValueError):
        raise ConnectorError(f"Not a session id: {session_id!r}") from None


# The rolling series behind the charts


class Series:
    """The last `window` seconds of one dashboard, chart by chart.

    Fed one `Sample` per poll. Counters arrive cumulative and leave as
    first differences per second; a counter that goes *backwards* means
    the server restarted or the statistics were reset, so the line is
    started again from there instead of dipping through a negative
    spike that never happened.
    """

    def __init__(
        self, kind: str, window: float = WINDOW_SECONDS
    ) -> None:
        self.charts = charts(kind)
        self.window = window
        self._points: dict[str, list[tuple[float, float]]] = {
            chart.name: [] for chart in self.charts
        }
        self._previous: Sample | None = None
        #: Set for one add() when a counter went backwards, so the UI
        #: can say why the lines restarted.
        self.restarted = False

    def add(self, sample: Sample) -> None:
        previous, self._previous = self._previous, sample
        self.restarted = previous is not None and _went_backwards(
            previous, sample
        )
        if self.restarted:
            for points in self._points.values():
                points.clear()
            previous = None
        elapsed = 0.0 if previous is None else sample.at - previous.at
        for chart in self.charts:
            value = self._value(chart, sample, previous, elapsed)
            if value is None:
                continue
            points = self._points[chart.name]
            points.append((sample.at, value))
            cutoff = sample.at - self.window
            while points and points[0][0] < cutoff:
                points.pop(0)

    def _value(
        self,
        chart: Chart,
        sample: Sample,
        previous: Sample | None,
        elapsed: float,
    ) -> float | None:
        if chart.kind == "gauge":
            return sample.gauges.get(chart.keys[0])
        if previous is None or elapsed <= 0:
            return None  # the first sample of a series has no delta yet
        if chart.kind == "rate":
            delta = _delta(previous, sample, chart.keys[0])
            return None if delta is None else delta / elapsed
        if chart.kind == "percent":
            hits = _delta(previous, sample, chart.keys[0])
            misses = _delta(previous, sample, chart.keys[1])
            if hits is None or misses is None:
                return None
            total = hits + misses
            # An idle interval read nothing at all; carrying the last
            # ratio forward is truer than plotting 0% or a gap.
            if total <= 0:
                last = self._points[chart.name]
                return last[-1][1] if last else None
            return 100.0 * hits / total
        return None

    def points(self, name: str) -> list[tuple[float, float]]:
        return list(self._points.get(name, ()))

    def latest(self, name: str) -> float | None:
        points = self._points.get(name) or []
        return points[-1][1] if points else None

    def ceiling(self, chart: Chart) -> float | None:
        if not chart.ceiling or self._previous is None:
            return None
        return self._previous.gauges.get(chart.ceiling)


def _delta(previous: Sample, sample: Sample, key: str) -> float | None:
    before, after = previous.counters.get(key), sample.counters.get(key)
    if before is None or after is None:
        return None
    return max(0.0, after - before)


def _went_backwards(previous: Sample, sample: Sample) -> bool:
    return any(
        key in sample.counters and sample.counters[key] < value
        for key, value in previous.counters.items()
    )


# Formatting, shared by the panels


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return ""
    if seconds < 1:
        return f"{seconds * 1000:.0f} ms"
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes, rest = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {rest:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def format_value(chart: Chart, value: float | None) -> str:
    if value is None:
        return "—"
    if chart.kind == "percent":
        return f"{value:.1f}%"
    if chart.kind == "gauge":
        return f"{value:.0f}{chart.unit}"
    if value >= 100:
        return f"{value:.0f}{chart.unit}"
    return f"{value:.1f}{chart.unit}"


# Small helpers over the connector, all of them tolerant: a panel the
# account may not read is an empty panel with a reason from the probe,
# never an exception out of a poll.


def _rows(connector: Connector, sql: str) -> list[tuple]:
    try:
        result = connector.execute(sql)
    except Exception:
        return []
    return list(result.rows) if isinstance(result, ResultSet) else []


def _scalar(connector: Connector, sql: str):
    result = connector.execute(sql)
    if isinstance(result, ResultSet) and result.rows and result.rows[0]:
        return result.rows[0][0]
    return None


def _text(value) -> str:
    return "" if value is None else str(value)


def _number(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
