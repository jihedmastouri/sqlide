"""The monitoring dashboard tab (CORE-15).

The interesting behaviour is what happens around the data rather than
the drawing: the sidebar offers the screen only for engines that have a
server to watch, the probe's unavailable sources become explaining rows
instead of blank charts, restricted ones raise the banner, and closing
the tab stops both timers and hands its connection back.
"""

from __future__ import annotations

import sqlite3

import pytest

from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.db import metrics, monitoring
from sqlide.backend.db.sqlite.connector import SqliteConnector


@pytest.fixture()
def gtk():
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gtk

    if not Gtk.init_check():
        pytest.skip("no display for GTK")
    return Gtk


@pytest.fixture()
def sqlite_db(tmp_path):
    path = tmp_path / "monitor.db"
    sqlite3.connect(path).close()
    connector = SqliteConnector(str(path))
    connector.connect()
    yield connector
    connector.close()


def _sidebar(gtk, sqlite_db, profile):
    from sqlide.frontend.sidebar import Sidebar

    def unused(*_args, **_kwargs):
        raise AssertionError("callback should not fire")

    bar = Sidebar(
        ensure_connector=lambda _profile: sqlite_db,
        on_open_table=unused,
        on_open_object=unused,
        on_open_section=unused,
        on_new_query=unused,
        on_open_cli=unused,
        on_open_definition=unused,
        on_edit_table=unused,
        on_open_function=unused,
        on_relation_graph=unused,
        on_view_indexes=unused,
        on_query_builder=unused,
        on_drop_object=unused,
        on_new_object=unused,
        on_mcp_server=unused,
        on_manage_users=unused,
        on_monitor=unused,
        on_open_schema=unused,
        on_edit_connection=unused,
        on_disconnect=unused,
        on_close_tabs=unused,
        count_tabs=lambda _name: 0,
        on_remove_connection=unused,
        on_add_connection=unused,
        show_error=unused,
    )
    bar.add_profile(profile)
    return bar


def _labels(menu) -> list[str]:
    out = []
    for index in range(menu.get_n_items()):
        value = menu.get_item_attribute_value(index, "label", None)
        if value is not None:
            out.append(value.get_string())
    return out


def test_a_file_database_is_offered_no_dashboard(gtk, sqlite_db) -> None:
    profile = ConnectionProfile("shop", "sqlite", file_path=":memory:")
    bar = _sidebar(gtk, sqlite_db, profile)
    assert "Monitoring…" not in _labels(bar._menu_for(bar._roots.get_item(0)))


def test_a_server_connection_is_offered_one(gtk, sqlite_db) -> None:
    profile = ConnectionProfile("reports", "postgres", host="localhost")
    bar = _sidebar(gtk, sqlite_db, profile)
    assert "Monitoring…" in _labels(bar._menu_for(bar._roots.get_item(0)))


def _tab(gtk, monkeypatch, kind="postgres", statuses=None, sample=None):
    """A MonitorTab that never opens a connection: the open runs on a
    worker thread, so the pieces it feeds are driven directly here."""
    from sqlide.frontend.monitor_tab import MonitorTab

    monkeypatch.setattr(MonitorTab, "_open", lambda self: None)
    errors: list[str] = []
    tab = MonitorTab(ConnectionProfile("db", kind, host="localhost"),
                     errors.append)
    return tab, errors


def test_an_unreadable_source_becomes_a_reason_not_a_blank_chart(
    gtk, monkeypatch
) -> None:
    tab, _errors = _tab(gtk, monkeypatch)
    tab._show_unavailable([
        monitoring.SourceStatus("database", "Throughput", True),
        monitoring.SourceStatus(
            "statements", "Top statements", False,
            detail="needs the pg_stat_statements extension",
        ),
    ])
    assert tab._unavailable.get_visible()


def test_a_connection_with_nothing_to_explain_hides_the_group(
    gtk, monkeypatch
) -> None:
    tab, _errors = _tab(gtk, monkeypatch)
    tab._show_unavailable(
        [monitoring.SourceStatus("database", "Throughput", True)]
    )
    assert not tab._unavailable.get_visible()


def test_masked_sessions_raise_the_banner(gtk, monkeypatch) -> None:
    tab, _errors = _tab(gtk, monkeypatch)
    tab._apply(metrics.Sample(at=0.0, masked="Other sessions are hidden."))
    assert tab._banner.get_revealed()
    assert "hidden" in tab._banner.get_title()

    tab._apply(metrics.Sample(at=2.0))
    assert not tab._banner.get_revealed()


def test_a_stats_reset_says_why_the_lines_restarted(gtk, monkeypatch) -> None:
    tab, _errors = _tab(gtk, monkeypatch)
    tab._apply(metrics.Sample(at=0.0, counters={"xact_commit": 100}))
    tab._apply(metrics.Sample(at=2.0, counters={"xact_commit": 1}))
    assert tab._banner.get_revealed()
    assert "statistics were reset" in tab._banner.get_title()


def test_sessions_are_listed_and_the_dashboards_own_cannot_be_killed(
    gtk, monkeypatch
) -> None:
    tab, _errors = _tab(gtk, monkeypatch)
    mine = metrics.Session("7", "app", "shop", "active", 1.0, "", "SELECT 1",
                           is_self=True)
    theirs = metrics.Session("8", "bot", "shop", "active", 90.0, "Lock",
                             "UPDATE t SET x = 1")
    tab._apply(metrics.Sample(at=0.0, sessions=(mine, theirs)))
    assert tab._sessions.get_n_items() == 2

    tab._selection.set_selected(0)
    assert not tab._kill_button.get_sensitive()  # that is our own poller
    tab._selection.set_selected(1)
    assert tab._kill_button.get_sensitive()


def test_the_selected_session_survives_a_refresh(gtk, monkeypatch) -> None:
    """The list reshuffles every couple of seconds; a Kill button on a
    row that moved out from under the cursor would be unusable."""
    tab, _errors = _tab(gtk, monkeypatch)
    first = metrics.Session("7", "app", "shop", "active", 1.0, "", "SELECT 1")
    second = metrics.Session("8", "bot", "shop", "idle", 2.0, "", "SELECT 2")
    tab._apply(metrics.Sample(at=0.0, sessions=(first, second)))
    tab._selection.set_selected(1)

    tab._apply(metrics.Sample(at=2.0, sessions=(second, first)))
    assert tab._selected_session().id == "8"


def test_pausing_stops_the_timers_and_resuming_starts_them(
    gtk, monkeypatch
) -> None:
    tab, _errors = _tab(gtk, monkeypatch)
    tab._start_timers()
    assert tab._live_source and tab._storage_source

    tab._pause.set_active(True)
    assert not tab._live_source and not tab._storage_source

    tab._pause.set_active(False)
    assert tab._live_source


def test_closing_the_tab_stops_polling_and_returns_the_connection(
    gtk, monkeypatch
) -> None:
    class Fake:
        closed = False

        def close(self) -> None:
            type(self).closed = True

    tab, _errors = _tab(gtk, monkeypatch)
    tab._connector = Fake()
    tab._start_timers()
    tab.shutdown()
    assert not tab._live_source and not tab._storage_source
    assert tab._connector is None
    # refresh_now after a close must be a no-op, not a query on a
    # connection that is going away.
    tab.refresh_now()
    assert not tab._sampling


def test_the_interval_is_remembered_globally(gtk, monkeypatch) -> None:
    from sqlide.backend.settings import store

    tab, _errors = _tab(gtk, monkeypatch)
    written: list[int] = []
    monkeypatch.setattr(
        store, "update", lambda **kw: written.append(kw["monitor_interval"])
    )
    tab._spin.set_value(9)
    assert written == [9]
    assert tab._interval == 9


def test_the_footer_never_promises_host_metrics() -> None:
    assert "agent" in metrics.HOST_METRICS_NOTE
    assert "CPU" in metrics.HOST_METRICS_NOTE
