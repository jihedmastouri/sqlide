"""Dashboards: several saved charts, refreshed together (CORE-35).

The backend half is pure — a TOML file, a layout that is arithmetic
over cell widths, and the binding of a cell to the saved query it
names — so most of this needs neither a display nor a server. The tab
tests skip without a display and drive the refresh directly, the way
tests/test_monitor_tab.py drives the monitoring one: the sweep runs on
a worker thread, so the thread is what is replaced, not the behaviour.
"""

from __future__ import annotations

import pytest

from sqlide.backend import charts, config, dashboards
from sqlide.backend.dashboards import Dashboard, DashboardCell, DashboardStore
from sqlide.backend.saved import SavedStore

SPEC = charts.dump_state(
    charts.ChartSpec(type="bar", x="city", series=("sales",))
)


@pytest.fixture()
def store(tmp_path):
    return DashboardStore(tmp_path / "dashboards")


@pytest.fixture()
def saved(tmp_path):
    store = SavedStore("saved_queries.json", tmp_path)
    store.add("Sales by city", "SELECT city, sales FROM t", SPEC)
    store.add("No chart", "SELECT 1")
    return store


# The file


def test_a_dashboard_is_a_readable_toml_file(store) -> None:
    board = store.create("Sales", "prod")
    board.add_cell("Sales by city")
    board.cells[0].width = 2
    path = store.save(board)

    text = path.read_text(encoding="utf-8")
    assert path.name == "sales.toml"
    assert 'name = "Sales"' in text
    assert 'connection = "prod"' in text
    assert "[[cell]]" in text
    assert 'query = "Sales by city"' in text
    assert "width = 2" in text


def test_a_hand_edited_file_is_what_loads(store) -> None:
    store.directory.mkdir(parents=True)
    (store.directory / "ops.toml").write_text(
        """
        # The team dashboard. Committed on purpose.
        id = "ops"
        name = "Ops"
        connection = "prod"
        columns = 3
        interval = 30

        [[cell]]
        query = "Sales by city"
        title = "Where the money is"
        width = 2
        height = 2
        """,
        encoding="utf-8",
    )
    board = store.load()[0]
    assert (board.name, board.connection, board.columns) == ("Ops", "prod", 3)
    assert board.interval == 30
    assert board.cells[0].label() == "Where the money is"
    assert (board.cells[0].width, board.cells[0].height) == (2, 2)


def test_comments_survive_a_change_made_in_the_app(store) -> None:
    board = store.create("Ops", "prod")
    path = store.save(board)
    path.write_text(
        "# hand-written\n" + path.read_text(encoding="utf-8"), encoding="utf-8"
    )
    board.name = "Ops"
    board.add_cell("Sales by city")
    store.save(board)
    assert "# hand-written" in path.read_text(encoding="utf-8")


def test_nonsense_in_the_file_is_reported_not_raised(store) -> None:
    config.clear_errors()
    store.directory.mkdir(parents=True)
    (store.directory / "bad.toml").write_text(
        'name = "Bad"\ncolumns = 99\ninterval = -4\nwibble = 1\n'
        '\n[[cell]]\nquery = "Sales by city"\nwidth = 40\n',
        encoding="utf-8",
    )
    board = store.load()[0]
    assert board.columns == dashboards.MAX_COLUMNS
    assert board.interval == dashboards.INTERVAL_OFF
    assert board.cells[0].width == board.columns
    assert any("wibble" in str(e) for e in config.errors())


def test_an_id_survives_the_round_trip_so_a_tab_reopens(store) -> None:
    board = store.create("Sales", "prod")
    again = DashboardStore(store.directory).load()[0]
    assert again.id == board.id
    assert DashboardStore(store.directory).get(board.id) is not None


def test_removing_a_dashboard_removes_its_file(store) -> None:
    board = store.create("Sales", "prod")
    path = store.path_for(board)
    store.remove(board)
    assert not path.exists()
    assert store.dashboards == []


def test_two_dashboards_of_the_same_name_get_their_own_file(store) -> None:
    first = store.create("Sales", "prod")
    second = store.create("Sales", "prod")
    assert second.name == "Sales (2)"
    assert store.path_for(first) != store.path_for(second)


# The layout


def test_cells_pack_left_to_right_and_wrap(store) -> None:
    board = Dashboard(name="Ops", columns=2)
    board.cells = [
        DashboardCell(query="a"),
        DashboardCell(query="b"),
        DashboardCell(query="c", width=2),
    ]
    placements = dashboards.layout(board)
    assert [(p.column, p.row, p.width) for p in placements] == [
        (0, 0, 1),
        (1, 0, 1),
        (0, 1, 2),
    ]


def test_a_cell_too_wide_for_the_grid_is_narrowed_not_lost(store) -> None:
    board = Dashboard(name="Ops", columns=2)
    board.cells = [DashboardCell(query="a", width=4)]
    board.normalise()
    assert board.cells[0].width == 2
    assert len(dashboards.layout(board)) == 1


def test_a_tall_cell_pushes_the_next_row_past_it(store) -> None:
    board = Dashboard(name="Ops", columns=2)
    board.cells = [
        DashboardCell(query="a", height=2),
        DashboardCell(query="b"),
        DashboardCell(query="c"),
    ]
    rows = [p.row for p in dashboards.layout(board)]
    assert rows == [0, 0, 2]


def test_reordering_is_the_list_order_and_persists(store) -> None:
    board = store.create("Ops", "prod")
    board.add_cell("a")
    board.add_cell("b")
    assert board.move(1, -1)
    store.save(board)
    again = DashboardStore(store.directory).load()[0]
    assert [c.query for c in again.cells] == ["b", "a"]


def test_moving_off_the_end_does_nothing(store) -> None:
    board = Dashboard(name="Ops")
    board.add_cell("a")
    assert not board.move(0, -1)
    assert not board.move(0, 3)


# Cells and their saved queries


def test_a_cell_binds_to_its_saved_query(saved) -> None:
    board = Dashboard(name="Ops")
    board.add_cell("Sales by city")
    bound = dashboards.bind(board, saved.load())
    assert bound[0].item is not None
    assert bound[0].chart == SPEC
    assert bound[0].problem == ""


def test_a_cell_whose_query_was_deleted_says_so(saved) -> None:
    board = Dashboard(name="Ops")
    board.add_cell("Sales by city")
    board.add_cell("Gone")
    saved.remove(saved.load()[0])
    bound = dashboards.bind(board, saved.load())
    # Both cells are still there; each explains itself.
    assert len(bound) == 2
    assert "no longer exists" in bound[0].problem
    assert "Gone" in bound[1].problem


def test_only_queries_with_a_chart_are_offered_as_cells(saved) -> None:
    assert [i.name for i in dashboards.chartable(saved.load())] == [
        "Sales by city"
    ]


def test_the_interval_is_clamped_the_way_monitoring_clamps_it() -> None:
    from sqlide.backend.db import metrics

    assert dashboards.clamp_interval(0) == 0
    assert dashboards.clamp_interval(-5) == 0
    assert dashboards.clamp_interval("x") == 0
    assert dashboards.clamp_interval(10_000) == metrics.MAX_INTERVAL
    assert dashboards.clamp_interval(30) == 30


# The tab


@pytest.fixture()
def gtk():
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gtk

    if not Gtk.init_check():
        pytest.skip("no display for GTK")
    return Gtk


class _Sync:
    """GLib, minus the main loop: idle work runs where it is posted."""

    SOURCE_CONTINUE = True
    SOURCE_REMOVE = False

    def __init__(self) -> None:
        self.sources = 0

    def idle_add(self, fn, *args) -> int:
        fn(*args)
        return 1

    def timeout_add_seconds(self, _seconds, _fn) -> int:
        self.sources += 1
        return self.sources

    def source_remove(self, _source) -> None:
        pass


class _Connector:
    """One connection's worth of answers, keyed by SQL."""

    def __init__(self, answers) -> None:
        self.answers = answers
        self.closed = False
        self.ran: list[str] = []

    def execute(self, sql, max_rows=None):
        self.ran.append(sql)
        answer = self.answers[sql]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def close(self) -> None:
        self.closed = True


def _tab(gtk, monkeypatch, store, saved, board, answers):
    """A DashboardTab that opens no connection of its own: the open and
    the sweep both run on a worker thread, so both are driven here."""
    from sqlide.frontend import dashboard_tab as module

    monkeypatch.setattr(module.DashboardTab, "_open", lambda self: None)
    monkeypatch.setattr(module, "queries_store", saved)
    monkeypatch.setattr(module, "GLib", _Sync())
    monkeypatch.setattr(
        module,
        "run_async",
        lambda work, done, failed=None: done(work()),
    )
    from sqlide.backend.connections import ConnectionProfile

    errors: list[str] = []
    tab = module.DashboardTab(
        board,
        ConnectionProfile("prod", "sqlite", file_path=":memory:"),
        errors.append,
        lambda *_a: None,
        store=store,
    )
    tab._connector = _Connector(answers)
    return tab, errors


def _result(columns, rows):
    from sqlide.backend.db.base import ResultSet

    return ResultSet(columns=list(columns), rows=[tuple(r) for r in rows])


def test_a_manual_refresh_runs_every_cell(gtk, monkeypatch, store, saved):
    board = store.create("Ops", "prod")
    saved.add("Second", "SELECT city, sales FROM u", SPEC)
    board.add_cell("Sales by city")
    board.add_cell("Second")
    store.save(board)
    answers = {
        "SELECT city, sales FROM t": _result(
            ["city", "sales"], [("Paris", 3), ("Lyon", 5)]
        ),
        "SELECT city, sales FROM u": _result(["city", "sales"], [("Nice", 1)]),
    }
    tab, _errors = _tab(gtk, monkeypatch, store, saved, board, answers)

    tab.refresh_now()
    assert tab._connector.ran == list(answers)
    assert [len(c._data.series) for c in tab._cards] == [1, 1]
    assert tab._cards[0]._data.rows == 2


def test_a_failing_cell_reports_in_place_and_the_others_still_draw(
    gtk, monkeypatch, store, saved
):
    board = store.create("Ops", "prod")
    saved.add("Second", "SELECT city, sales FROM u", SPEC)
    board.add_cell("Sales by city")
    board.add_cell("Second")
    store.save(board)
    answers = {
        "SELECT city, sales FROM t": RuntimeError("relation t does not exist"),
        "SELECT city, sales FROM u": _result(["city", "sales"], [("Nice", 1)]),
    }
    tab, _errors = _tab(gtk, monkeypatch, store, saved, board, answers)

    tab.refresh_now()
    assert "does not exist" in tab._cards[0]._data.reason
    assert tab._cards[1]._data.series  # the sweep carried on


def test_a_cell_whose_saved_query_is_gone_says_so_and_stays(
    gtk, monkeypatch, store, saved
):
    board = store.create("Ops", "prod")
    board.add_cell("Sales by city")
    board.add_cell("Deleted")
    store.save(board)
    tab, _errors = _tab(
        gtk, monkeypatch, store, saved, board,
        {"SELECT city, sales FROM t": _result(["city", "sales"], [("Nice", 1)])},
    )

    tab.refresh_now()
    assert len(tab._cards) == 2
    assert "no longer exists" in tab._cards[1]._data.reason
    assert tab._cards[0]._data.series


def test_a_query_with_placeholders_is_refused_rather_than_run(
    gtk, monkeypatch, store, saved
):
    saved.add("Parametrised", "SELECT city, sales FROM t WHERE id = :id", SPEC)
    board = store.create("Ops", "prod")
    board.add_cell("Parametrised")
    store.save(board)
    tab, _errors = _tab(gtk, monkeypatch, store, saved, board, {})

    tab.refresh_now()
    assert tab._connector.ran == []
    assert "placeholders" in tab._cards[0]._data.reason


def test_editing_the_layout_persists_immediately(
    gtk, monkeypatch, store, saved
):
    board = store.create("Ops", "prod")
    board.add_cell("Sales by city")
    store.save(board)
    tab, _errors = _tab(
        gtk, monkeypatch, store, saved, board,
        {"SELECT city, sales FROM t": _result(["city", "sales"], [("Nice", 1)])},
    )

    tab._resize(0, 1, 1)
    on_disk = DashboardStore(store.directory).load()[0]
    assert (on_disk.cells[0].width, on_disk.cells[0].height) == (2, 2)

    tab._remove(0)
    assert DashboardStore(store.directory).load()[0].cells == []


def test_the_interval_is_written_to_the_file(gtk, monkeypatch, store, saved):
    board = store.create("Ops", "prod")
    store.save(board)
    tab, _errors = _tab(gtk, monkeypatch, store, saved, board, {})
    tab._spin.set_value(15)
    assert DashboardStore(store.directory).load()[0].interval == 15


def test_pausing_stops_the_timer_and_resuming_starts_it(
    gtk, monkeypatch, store, saved
):
    board = store.create("Ops", "prod")
    board.interval = 10
    store.save(board)
    tab, _errors = _tab(gtk, monkeypatch, store, saved, board, {})
    tab._start_timer()
    assert tab._source

    tab._pause.set_active(True)
    assert not tab._source

    tab._pause.set_active(False)
    assert tab._source


def test_closing_the_tab_stops_refreshing_and_returns_the_connection(
    gtk, monkeypatch, store, saved
):
    board = store.create("Ops", "prod")
    board.interval = 10
    board.add_cell("Sales by city")
    store.save(board)
    tab, _errors = _tab(
        gtk, monkeypatch, store, saved, board,
        {"SELECT city, sales FROM t": _result(["city", "sales"], [("Nice", 1)])},
    )
    connector = tab._connector
    tab._start_timer()
    tab.shutdown()

    assert not tab._source
    assert tab._connector is None
    assert connector.closed
    # A refresh after the close must be a no-op, not a query on a
    # connection that is going away.
    tab.refresh_now()
    assert connector.ran == []


def test_a_chart_that_no_longer_fits_its_columns_explains_itself(
    gtk, monkeypatch, store, saved
):
    board = store.create("Ops", "prod")
    board.add_cell("Sales by city")
    store.save(board)
    tab, _errors = _tab(
        gtk, monkeypatch, store, saved, board,
        # "sales" was renamed; the saved spec names a column that is gone.
        {"SELECT city, sales FROM t": _result(["city", "revenue"], [("Nice", 1)])},
    )

    tab.refresh_now()
    card = tab._cards[0]
    assert card._notice.get_visible()
    assert "sales" in card._notice.get_text()
    # …and it still drew something rather than blanking.
    assert card._data.series


def test_the_tab_is_only_a_reference_to_the_file(gtk, monkeypatch, store, saved):
    """The dashboard lives in its TOML file; the workspace remembers
    nothing but its id, so a hand edit is what reopens."""
    from sqlide.backend.workspaces import TabState, Workspace

    board = store.create("Ops", "prod")
    board.add_cell("Sales by city")
    store.save(board)
    tab, _errors = _tab(gtk, monkeypatch, store, saved, board, {})

    state = tab.tab_state()
    assert (state.kind, state.dashboard) == ("dashboard", board.id)
    again = Workspace.from_dict(
        {"name": "w", "tabs": [state.__dict__]}
    )
    assert again.tabs[0].dashboard == board.id
    assert DashboardStore(store.directory).get(again.tabs[0].dashboard) is not None
    assert TabState(kind="query", connection="prod").dashboard == ""
