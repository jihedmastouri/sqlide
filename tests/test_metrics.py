"""Sampling a server for the monitoring dashboard (CORE-15).

Two halves. The first needs no server: the maths the dashboard runs on
every sample — first differences, the restart when a counter goes
backwards, the ratio over deltas rather than over lifetime totals — and
the guards around a session id that goes into SQL unquoted.

The second aims the sampler at the live matrix and asserts the claims
docs/monitoring-spike.md makes: the always-drawn panels answer on an
ordinary account, storage answers separately, and both engines report a
session that has already gone as a lost race rather than an error.
"""

from __future__ import annotations

import pytest

from sqlide import i18n
from sqlide.backend.db import metrics
from sqlide.backend.db.base import ConnectorError, ResultSet


def _sample(at: float, **counters) -> metrics.Sample:
    return metrics.Sample(at=at, counters=dict(counters))


# The engine list


def test_sqlite_has_nothing_to_chart() -> None:
    assert metrics.charts("sqlite") == ()
    assert not metrics.supported("sqlite")
    assert metrics.storage("sqlite", None) == metrics.Storage()


def test_engine_aliases_share_their_charts() -> None:
    assert metrics.charts("postgresql") == metrics.charts("postgres")
    assert metrics.charts("mariadb") == metrics.charts("mysql")


def test_every_chart_names_the_keys_it_needs() -> None:
    for kind in ("postgres", "mysql"):
        for chart in metrics.charts(kind):
            assert chart.kind in ("rate", "gauge", "percent")
            assert chart.keys
            if chart.kind == "percent":
                assert len(chart.keys) == 2


def test_interval_is_clamped_to_its_range() -> None:
    assert metrics.clamp_interval(0) == metrics.MIN_INTERVAL
    assert metrics.clamp_interval(3600) == metrics.MAX_INTERVAL
    assert metrics.clamp_interval(5) == 5


# Series: counters in, rates out


def test_the_first_sample_has_no_rate_yet() -> None:
    series = metrics.Series("postgres")
    series.add(_sample(0.0, xact_commit=100))
    assert series.points("commits") == []
    assert series.latest("commits") is None


def test_a_rate_is_the_difference_over_the_interval() -> None:
    series = metrics.Series("postgres")
    series.add(_sample(0.0, xact_commit=100))
    series.add(_sample(2.0, xact_commit=140))
    assert series.latest("commits") == pytest.approx(20.0)


def test_a_ratio_is_taken_over_deltas_not_lifetime_totals() -> None:
    # A server up for a month with a 99% lifetime ratio, having a bad
    # minute: the chart must show the bad minute.
    series = metrics.Series("postgres")
    series.add(_sample(0.0, blks_hit=990_000, blks_read=10_000))
    series.add(_sample(1.0, blks_hit=990_050, blks_read=10_050))
    assert series.latest("cache") == pytest.approx(50.0)


def test_an_idle_interval_holds_the_last_ratio() -> None:
    series = metrics.Series("postgres")
    series.add(_sample(0.0, blks_hit=100, blks_read=0))
    series.add(_sample(1.0, blks_hit=200, blks_read=0))
    series.add(_sample(2.0, blks_hit=200, blks_read=0))  # nothing happened
    assert series.latest("cache") == pytest.approx(100.0)


def test_a_counter_going_backwards_restarts_the_series() -> None:
    """A restart or pg_stat_reset() must not draw a negative spike."""
    series = metrics.Series("postgres")
    series.add(_sample(0.0, xact_commit=100))
    series.add(_sample(2.0, xact_commit=140))
    assert series.points("commits")

    series.add(_sample(4.0, xact_commit=3))  # the server came back up
    assert series.restarted
    assert series.points("commits") == []

    series.add(_sample(6.0, xact_commit=9))
    assert not series.restarted
    assert series.latest("commits") == pytest.approx(3.0)


def test_gauges_are_plotted_as_read() -> None:
    series = metrics.Series("mysql")
    series.add(
        metrics.Sample(
            at=0.0, gauges={"Threads_connected": 7, "max_connections": 151}
        )
    )
    assert series.latest("sessions") == 7
    chart = {c.name: c for c in metrics.charts("mysql")}["sessions"]
    assert series.ceiling(chart) == 151


def test_the_window_drops_samples_that_fell_out_of_it() -> None:
    series = metrics.Series("postgres", window=10.0)
    for step in range(20):
        series.add(_sample(float(step), xact_commit=step * 10))
    times = [at for at, _value in series.points("commits")]
    assert times and max(times) - min(times) <= 10.0


# Formatting


def test_sizes_and_durations_read_as_words() -> None:
    assert i18n.format_size(None) == "—"
    assert i18n.format_size(512) == "512 B"
    assert i18n.format_size(1536) == "1.5 kB"
    assert metrics.format_duration(None) == ""
    assert metrics.format_duration(0.05) == "50 ms"
    assert metrics.format_duration(90) == "1m 30s"
    assert metrics.format_duration(3900) == "1h 05m"


def test_a_missing_value_is_a_dash_never_a_zero() -> None:
    chart = metrics.charts("postgres")[0]
    assert metrics.format_value(chart, None) == "—"


# Signalling a session


class _Fake:
    """A connector that answers a scripted map of SQL -> result."""

    def __init__(self, answers: dict[str, object]) -> None:
        self.answers = answers
        self.ran: list[str] = []

    def execute(self, sql: str):
        self.ran.append(sql)
        for key, value in self.answers.items():
            if key in sql:
                if isinstance(value, Exception):
                    raise value
                return value
        return ResultSet(columns=["?"], rows=[])


def test_a_session_id_that_is_not_a_number_is_refused() -> None:
    # It is interpolated into SQL unquoted, so nothing else may pass.
    connector = _Fake({})
    with pytest.raises(ConnectorError):
        metrics.terminate_session("postgres", connector, "1; DROP TABLE t")
    assert not connector.ran


def test_postgres_false_means_the_session_had_already_gone() -> None:
    connector = _Fake(
        {"pg_cancel_backend": ResultSet(columns=["?"], rows=[(False,)])}
    )
    assert metrics.cancel_session("postgres", connector, "42") == metrics.GONE


def test_mysql_unknown_thread_id_means_the_same() -> None:
    connector = _Fake(
        {"KILL": ConnectorError("Unknown thread id: 42")}
    )
    assert metrics.terminate_session("mysql", connector, "42") == metrics.GONE


def test_a_real_kill_failure_still_raises() -> None:
    connector = _Fake({"KILL": ConnectorError("Access denied")})
    with pytest.raises(ConnectorError):
        metrics.terminate_session("mysql", connector, "42")


def test_mysql_says_so_when_it_listed_fewer_threads_than_exist() -> None:
    """The silent degradation of the whole feature: without PROCESS,
    SHOW PROCESSLIST lists this account's threads and no error, which
    reads as an idle server."""
    connector = _Fake({
        "SHOW GLOBAL STATUS": ResultSet(
            columns=["Variable_name", "Value"],
            rows=[("Threads_connected", "9"), ("Questions", "10")],
        ),
        "SHOW GLOBAL VARIABLES": ResultSet(
            columns=["Variable_name", "Value"],
            rows=[("max_connections", "151")],
        ),
        "SHOW FULL PROCESSLIST": ResultSet(
            columns=list(range(8)),
            rows=[(7, "app", "localhost", "sqlide", "Query", 0, "", "SELECT 1")],
        ),
        "SELECT CONNECTION_ID()": ResultSet(columns=["id"], rows=[(7,)]),
        "SELECT 1": ResultSet(columns=["?"], rows=[(1,)]),
    })
    sample = metrics.sample("mysql", connector)
    assert "PROCESS" in sample.masked
    assert "9" in sample.masked and "1" in sample.masked


def test_postgres_says_so_when_other_sessions_sql_is_blanked() -> None:
    connector = _Fake({
        "FROM pg_stat_activity\nORDER BY": ResultSet(
            columns=list(range(8)),
            rows=[
                (1, "app", "sqlide", "active", 1.0, "", "SELECT 1", True),
                (2, "other", "sqlide", "", None, "",
                 "<insufficient privilege>", False),
            ],
        ),
        "SELECT 1": ResultSet(columns=["?"], rows=[(1,)]),
    })
    sample = metrics.sample("postgres", connector)
    assert "pg_monitor" in sample.masked


def test_signal_rights_explain_themselves_when_refused() -> None:
    postgres = metrics.signal_rights(
        "postgres",
        _Fake({"pg_signal_backend": ResultSet(columns=["?"], rows=[(False,)])}),
    )
    assert not postgres.others and "pg_signal_backend" in postgres.detail

    mysql = metrics.signal_rights(
        "mysql",
        _Fake({"SHOW GRANTS": ResultSet(
            columns=["g"], rows=[("GRANT USAGE ON *.* TO 'app'@'%'",)]
        )}),
    )
    assert not mysql.others and "CONNECTION_ADMIN" in mysql.detail

    allowed = metrics.signal_rights(
        "mysql",
        _Fake({"SHOW GRANTS": ResultSet(
            columns=["g"], rows=[("GRANT SUPER ON *.* TO 'root'@'%'",)]
        )}),
    )
    assert allowed.others and not allowed.detail


# Against the live servers.


def test_postgres_sample_fills_the_always_drawn_panels(postgres) -> None:
    _version, connector = postgres
    sample = metrics.sample("postgres", connector)
    assert sample.sessions
    assert any(session.is_self for session in sample.sessions)
    for key in ("xact_commit", "blks_hit", "blks_read"):
        assert key in sample.counters
    assert sample.gauges["max_connections"] > 0
    assert "locks_waiting" in sample.gauges


def test_postgres_storage_answers_on_its_own(postgres) -> None:
    _version, connector = postgres
    storage = metrics.storage("postgres", connector)
    assert any(name == "sqlide" for name, _size in storage.databases)
    assert any(name.endswith(".users") for name, _size in storage.tables)


def test_postgres_cancel_of_a_finished_session_is_a_lost_race(
    postgres,
) -> None:
    _version, connector = postgres
    # A backend id no server will have: the engine says so with `false`,
    # which is not a failure the UI should report as one.
    assert metrics.cancel_session("postgres", connector, "2147483647") == (
        metrics.GONE
    )


def test_mysql_sample_fills_the_always_drawn_panels(mysql) -> None:
    _version, connector = mysql
    sample = metrics.sample("mysql", connector)
    assert sample.sessions
    for key in ("Questions", "Innodb_buffer_pool_read_requests"):
        assert key in sample.counters
    assert sample.gauges["max_connections"] > 0
    assert any(session.is_self for session in sample.sessions)
    # Hidden threads are only detectable when there are some: with the
    # fixture account alone on the server the counts agree. What must
    # never happen is a shortfall that goes unsaid.
    if sample.gauges["Threads_connected"] > len(sample.sessions):
        assert "PROCESS" in sample.masked


def test_mysql_storage_answers_on_its_own(mysql) -> None:
    _version, connector = mysql
    storage = metrics.storage("mysql", connector)
    assert any(name == "sqlide" for name, _size in storage.databases)
    assert storage.detail  # information_schema only shows what it sees


def test_mysql_kill_of_a_finished_thread_is_a_lost_race(mysql) -> None:
    _version, connector = mysql
    assert metrics.cancel_session("mysql", connector, "2147483647") == (
        metrics.GONE
    )


@pytest.mark.parametrize("kind", ["postgres", "mysql"])
def test_a_dead_connection_is_the_one_thing_a_sample_raises(kind) -> None:
    class Dead:
        def execute(self, sql):
            raise ConnectorError("connection is closed")

    with pytest.raises(ConnectorError):
        metrics.sample(kind, Dead())
