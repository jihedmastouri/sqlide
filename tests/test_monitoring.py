"""The monitoring probe (CORE-14): what a connection can actually read.

These tests are the spike's evidence, kept runnable — docs/monitoring-spike.md
states which sources answer on which server, and the assertions here are the
same claims aimed at the live matrix. The interesting ones are negative: the
sources that must stay unavailable (pg_stat_io before PostgreSQL 16,
pg_stat_statements without the extension) have to fail with a reason a panel
can show, not with an exception escaping the probe.
"""

from __future__ import annotations

import sqlite3

import pytest

from sqlide.backend.db import monitoring
from sqlide.backend.db.sqlite.connector import SqliteConnector


def _by_name(statuses):
    return {status.name: status for status in statuses}


# Answerable with nothing connected.


def test_sqlite_has_nothing_to_monitor() -> None:
    assert monitoring.sources("sqlite") == ()


def test_every_source_declares_a_probe_and_a_requirement() -> None:
    for kind in ("postgres", "mysql"):
        for source in monitoring.sources(kind):
            assert source.probe_sql.strip()
            assert source.requires.strip()
            assert source.title.strip()


def test_engine_aliases_share_a_source_list() -> None:
    assert monitoring.sources("postgresql") == monitoring.sources("postgres")
    assert monitoring.sources("mariadb") == monitoring.sources("mysql")


def test_masked_sessions_spots_postgres_blanking() -> None:
    assert monitoring.masked_sessions([("<insufficient privilege>",)])
    assert not monitoring.masked_sessions([("SELECT 1",), (None,)])
    assert not monitoring.masked_sessions([()])


def test_probe_of_an_engine_without_sources_is_empty(tmp_path) -> None:
    path = tmp_path / "monitor.db"
    sqlite3.connect(path).close()
    connector = SqliteConnector(str(path))
    connector.connect()
    try:
        assert monitoring.probe("sqlite", connector) == []
    finally:
        connector.close()


# Against the live servers.


def test_postgres_core_sources_answer(postgres) -> None:
    _version, connector = postgres
    statuses = _by_name(monitoring.probe("postgres", connector))
    for name in ("activity", "database", "bgwriter", "locks", "sizes",
                 "replication"):
        assert statuses[name].available, statuses[name].detail


def test_postgres_stat_io_follows_the_server_version(postgres) -> None:
    version, connector = postgres
    status = _by_name(monitoring.probe("postgres", connector))["io"]
    assert status.available is (int(version) >= 16)
    if not status.available:
        assert "16" in status.detail


def test_postgres_statements_is_opt_in(postgres) -> None:
    _version, connector = postgres
    status = _by_name(monitoring.probe("postgres", connector))["statements"]
    # The test servers load no shared_preload_libraries, so the extension
    # cannot be there; the point is that its absence is explained.
    assert not status.available
    assert "pg_stat_statements" in status.detail


def test_mysql_open_sources_answer(mysql) -> None:
    _version, connector = mysql
    statuses = _by_name(monitoring.probe("mysql", connector))
    for name in ("status", "processlist", "sizes"):
        assert statuses[name].available, statuses[name].detail


def test_mysql_hides_the_instrumentation_from_a_plain_account(mysql) -> None:
    _version, connector = mysql
    statuses = _by_name(monitoring.probe("mysql", connector))
    # The fixture account holds no PROCESS and no SELECT on the
    # instrumentation schemas — the shape most users connect with.
    for name in ("performance_schema", "sys", "replication"):
        assert not statuses[name].available
        assert statuses[name].detail

    assert statuses["processlist"].available


@pytest.mark.parametrize("kind", ["postgres", "mysql"])
def test_probe_reports_rather_than_raises(kind) -> None:
    class Broken:
        def execute(self, sql):
            raise RuntimeError("connection is closed")

    statuses = monitoring.probe(kind, Broken())
    assert statuses
    assert all(not status.available and status.detail for status in statuses)
