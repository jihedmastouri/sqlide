"""Which monitoring sources a connection can actually read (CORE-14).

The spike behind this module is written up in docs/monitoring-spike.md.
Its finding, in one line: every useful server metric for PostgreSQL and
MySQL is reachable over a plain client connection, but *which* of them
answer depends on the server version, on one PostgreSQL extension, and
above all on the connected account's privileges — and the failure mode
is not an error, it is a view that quietly returns only your own rows.

So a monitoring screen cannot ask "which engine is this" and draw a
fixed set of panels. It asks here first, once, when the screen opens:

    for status in probe(profile.kind, connector):
        if not status.available:
            panel.explain(status.detail)   # never a blank chart

Each `Source` is one panel's worth of data with a probe cheap enough to
run on connect (a single row from a system view). `probe()` runs them
and reports, per source, whether it answered, and whether it answered
*fully* — `restricted` is the case that matters: PostgreSQL blanks other
sessions' query text for a non-superuser instead of refusing, and MySQL's
SHOW PROCESSLIST silently lists only the current account's threads
without the PROCESS privilege. Both look like a working, nearly empty
dashboard unless the UI says why.

This is a read-only probe: nothing here creates an extension, changes a
setting or kills a session. Every method queries the server, so call it
from a worker thread (frontend/util.run_async), never the GTK main loop.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlide.backend.db.base import Connector, ConnectorError, ResultSet


@dataclass(frozen=True)
class Source:
    """One panel's data source, and the cheapest question that proves
    the connection can read it."""

    name: str
    title: str
    #: A one-row query. It must be cheap: probes run together on connect.
    probe_sql: str
    #: What the user has to be granted when the probe fails, in words
    #: the "not available because X" panel can show as-is.
    requires: str
    #: True when the source answers for an unprivileged account too, but
    #: with other sessions' rows hidden or masked rather than refused.
    partial_without_privilege: bool = False


@dataclass(frozen=True)
class SourceStatus:
    """The answer for one source on one connection."""

    name: str
    title: str
    available: bool
    #: Available, but showing less than the whole server (see the module
    #: docstring). The panel should draw, with the caveat spelled out.
    restricted: bool = False
    #: Why it is unavailable or restricted; empty when it is neither.
    detail: str = ""


#: PostgreSQL. Version-gated sources probe the view directly rather than
#: comparing server_version_num: an ancient view that was renamed and a
#: view an extension has not created fail identically, and the UI wants
#: the reason, not the version arithmetic.
_POSTGRES: tuple[Source, ...] = (
    Source(
        "activity", "Sessions",
        "SELECT query FROM pg_stat_activity",
        "membership of pg_read_all_stats or pg_monitor to see other "
        "sessions' state and SQL",
        partial_without_privilege=True,
    ),
    Source(
        "database", "Throughput and cache hits",
        "SELECT xact_commit FROM pg_stat_database LIMIT 1",
        "nothing beyond CONNECT — pg_stat_database is world-readable",
    ),
    Source(
        "bgwriter", "Checkpoints and buffers",
        "SELECT checkpoints_timed FROM pg_stat_bgwriter",
        "nothing beyond CONNECT",
    ),
    Source(
        "io", "I/O by backend type",
        "SELECT backend_type FROM pg_stat_io LIMIT 1",
        "PostgreSQL 16 or newer (pg_stat_io does not exist before it)",
    ),
    Source(
        "statements", "Top statements",
        "SELECT calls FROM pg_stat_statements LIMIT 1",
        "the pg_stat_statements extension, which needs a server restart "
        "with shared_preload_libraries set — a client cannot enable it",
        partial_without_privilege=True,
    ),
    Source(
        "locks", "Locks and waits",
        "SELECT locktype FROM pg_locks LIMIT 1",
        "nothing beyond CONNECT",
    ),
    Source(
        "sizes", "Storage",
        "SELECT pg_database_size(current_database())",
        "CONNECT on the databases whose size is shown",
    ),
    Source(
        "replication", "Replication",
        "SELECT pg_is_in_recovery()",
        "membership of pg_monitor for the standby detail in "
        "pg_stat_replication",
        partial_without_privilege=True,
    ),
)

#: MySQL. performance_schema and sys are ordinary tables as far as
#: privileges go: without SELECT on them the server refuses outright,
#: which is the friendly case. SHOW PROCESSLIST is the unfriendly one.
_MYSQL: tuple[Source, ...] = (
    Source(
        "status", "Throughput and buffer pool",
        "SHOW GLOBAL STATUS LIKE 'Questions'",
        "nothing — SHOW GLOBAL STATUS is open to every account",
    ),
    Source(
        "processlist", "Sessions",
        "SHOW PROCESSLIST",
        "the PROCESS privilege to see other accounts' threads",
        partial_without_privilege=True,
    ),
    Source(
        "performance_schema", "Waits and statement digests",
        "SELECT COUNT(*) FROM performance_schema.threads",
        "SELECT on performance_schema.*, and the instrumentation left "
        "enabled on the server",
    ),
    Source(
        "sys", "Statement analysis",
        "SELECT COUNT(*) FROM sys.schema_table_statistics",
        "SELECT on the sys schema (which reads performance_schema)",
    ),
    Source(
        "innodb", "InnoDB engine status",
        "SHOW ENGINE INNODB STATUS",
        "the PROCESS privilege",
    ),
    Source(
        "sizes", "Storage",
        "SELECT SUM(data_length + index_length) "
        "FROM information_schema.tables",
        "nothing — but information_schema shows only the schemas the "
        "account may see",
        partial_without_privilege=True,
    ),
    Source(
        "replication", "Replication",
        "SHOW SLAVE STATUS",
        "the REPLICATION CLIENT privilege",
    ),
)

_SOURCES: dict[str, tuple[Source, ...]] = {
    "postgres": _POSTGRES,
    "postgresql": _POSTGRES,
    "mysql": _MYSQL,
    "mariadb": _MYSQL,
}


def sources(kind: str) -> tuple[Source, ...]:
    """The sources this engine has. Empty for an engine with no server
    to monitor (SQLite is a file: no sessions, no throughput, no locks
    a client can read), which is how a caller knows to offer no
    monitoring at all rather than an empty dashboard."""
    return _SOURCES.get(kind.lower(), ())


def probe(kind: str, connector: Connector) -> list[SourceStatus]:
    """Run every source's probe and report what this connection can
    read. Never raises: a source that fails is a status, not an error,
    because the whole point is to describe a partial server."""
    return [_probe_one(source, connector) for source in sources(kind)]


def _probe_one(source: Source, connector: Connector) -> SourceStatus:
    try:
        result = connector.execute(source.probe_sql)
    except ConnectorError as exc:
        return SourceStatus(
            source.name, source.title, False, detail=_because(source, exc)
        )
    except Exception as exc:  # a driver that raises its own type
        return SourceStatus(
            source.name, source.title, False, detail=_because(source, exc)
        )
    restricted, detail = _restriction(source, result, connector)
    return SourceStatus(source.name, source.title, True, restricted, detail)


def _because(source: Source, exc: Exception) -> str:
    message = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    text = f"{source.title} needs {source.requires}."
    return f"{text} The server said: {message}" if message else text


def _restriction(
    source: Source, result: ResultSet | int, connector: Connector
) -> tuple[bool, str]:
    """Spot a source that answered without refusing and still showed
    less than the server. Only two do, and each is caught from the probe
    result it already has (see the module docstring)."""
    if not source.partial_without_privilege or not isinstance(result, ResultSet):
        return False, ""
    if source.name == "activity" and masked_sessions(result.rows, 0):
        return True, (
            "Other sessions' SQL is hidden: this account is not a "
            "superuser and holds neither pg_read_all_stats nor pg_monitor."
        )
    if source.name == "processlist" and _only_own_threads(result, connector):
        return True, (
            "Only this account's threads are listed: seeing the rest "
            "needs the PROCESS privilege."
        )
    return False, ""


def _only_own_threads(result: ResultSet, connector: Connector) -> bool:
    """MySQL hides other accounts' threads from SHOW PROCESSLIST rather
    than refusing it, so compare what it listed with what the server
    says is connected."""
    try:
        status = connector.execute("SHOW GLOBAL STATUS LIKE 'Threads_connected'")
    except Exception:
        return False
    if not isinstance(status, ResultSet) or not status.rows:
        return False
    try:
        connected = int(status.rows[0][-1])
    except (TypeError, ValueError):
        return False
    return connected > len(result.rows)


def masked_sessions(rows: list[tuple[object, ...]], column: int = 0) -> bool:
    """True when PostgreSQL blanked other sessions' query text — that is
    what a non-superuser without pg_read_all_stats sees, and it reads as
    an idle server unless the UI says otherwise."""
    return any(
        isinstance(row[column], str)
        and row[column].strip() == "<insufficient privilege>"
        for row in rows
        if len(row) > column
    )
