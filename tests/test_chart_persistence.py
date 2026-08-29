"""Persisted chart specs (CORE-33).

A chart comes back when its tab comes back, and a saved query can carry
the chart it is meant to be seen as. The round trips below go through
the real stores — the workspace layer for `TabState.chart`, `SavedStore`
for `SavedItem.chart` — and the widget tests check what a restored spec
does to a console's result: it is applied by column name, so a re-run
redraws it, and one that no longer fits is explained rather than raised
on.
"""

from __future__ import annotations

import json

import pytest

from sqlide.backend import charts, saved
from sqlide.backend.workspaces import TabState, Workspace, WorkspaceStore


@pytest.fixture()
def gtk():
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gtk

    if not Gtk.init_check():
        pytest.skip("no display for GTK")
    return Gtk


SPEC = charts.ChartSpec(
    type="bar", x="city", series=("sales",), split="kind", aggregation="sum"
)
COLUMNS = ["city", "kind", "sales"]
ROWS = [
    ("Paris", "web", 3),
    ("Paris", "shop", 4),
    ("Lyon", "web", 5),
    ("Lyon", "shop", 6),
]


# The workspace


def test_a_tab_s_chart_survives_a_restart(tmp_path):
    store = WorkspaceStore(tmp_path / "workspaces")
    workspace = Workspace(name="Work")
    workspace.tabs.append(
        TabState(
            kind="query",
            connection="prod",
            sql="SELECT city, kind, sum(sales) FROM s GROUP BY 1, 2",
            chart=charts.dump_state(SPEC),
        )
    )
    store.save(workspace)

    (loaded,) = WorkspaceStore(tmp_path / "workspaces").load()
    assert charts.load_state(loaded.tabs[0].chart) == SPEC


def test_the_chart_is_session_state_not_configuration(tmp_path):
    store = WorkspaceStore(tmp_path / "workspaces")
    workspace = Workspace(name="Work")
    workspace.tabs.append(
        TabState(kind="query", connection="prod", chart=charts.dump_state(SPEC))
    )
    store.save(workspace)
    folder = store.path_for(workspace.id)
    assert "chart" in json.loads((folder / "state.json").read_text())["tabs"][0]
    assert "sales" not in (folder / "workspace.toml").read_text()


def test_a_workspace_from_an_older_build_opens_with_no_chart(tmp_path):
    directory = tmp_path / "workspaces"
    (directory / "abc").mkdir(parents=True)
    (directory / "abc" / "workspace.toml").write_text(
        'id = "abc"\nname = "Old"\n'
    )
    (directory / "abc" / "state.json").write_text(
        json.dumps(
            {"tabs": [{"kind": "query", "connection": "prod", "sql": "SELECT 1"}]}
        )
    )
    (loaded,) = WorkspaceStore(directory).load()
    assert loaded.tabs[0].chart == ""


def test_a_spec_from_an_unknown_version_is_discarded_not_raised():
    text = json.dumps(
        {"version": charts.MODEL_VERSION + 1, "chart": charts.to_dict(SPEC)}
    )
    assert charts.load_state(text) is None


# Saved queries


def test_a_saved_query_carries_its_chart(tmp_path):
    store = saved.SavedStore("saved_queries.json", tmp_path)
    store.add("Weekly signups", "SELECT 1", charts.dump_state(SPEC))

    reopened = saved.SavedStore("saved_queries.json", tmp_path).load()
    assert reopened[0].name == "Weekly signups"
    assert charts.load_state(reopened[0].chart) == SPEC


def test_a_query_saved_without_a_chart_keeps_the_field_empty(tmp_path):
    store = saved.SavedStore("saved_queries.json", tmp_path)
    store.add("Plain", "SELECT 1")
    assert saved.SavedStore("saved_queries.json", tmp_path).load()[0].chart == ""


def test_a_saved_file_from_an_older_build_still_opens(tmp_path):
    path = tmp_path / "saved_queries.json"
    path.write_text(json.dumps([{"name": "Old", "sql": "SELECT 1"}]))
    (item,) = saved.SavedStore("saved_queries.json", tmp_path).load()
    assert (item.name, item.sql, item.chart) == ("Old", "SELECT 1", "")


def test_a_saved_file_from_a_newer_build_drops_what_it_cannot_read(tmp_path):
    path = tmp_path / "saved_queries.json"
    path.write_text(
        json.dumps([{"name": "New", "sql": "SELECT 1", "dashboard": "x"}])
    )
    (item,) = saved.SavedStore("saved_queries.json", tmp_path).load()
    assert (item.name, item.chart) == ("New", "")


# The console


def _console(gtk, chart: str = ""):
    from sqlide.frontend.query_console import QueryConsole

    return QueryConsole(
        connection_names=gtk.StringList.new(["shop"]),
        find_connection=lambda _name: None,
        ensure_connector=lambda _profile: None,
        chart=chart,
    )


def _pane(gtk, columns=COLUMNS, rows=ROWS):
    from sqlide.frontend.chart_view import ChartPane
    from sqlide.frontend.data_grid import ResultGrid

    grid = ResultGrid()
    grid.set_result(columns, rows)
    pane = ChartPane(grid)
    pane.set_result(columns, rows)
    return pane


def test_a_restored_console_draws_its_chart_and_shows_it(gtk):
    console = _console(gtk, charts.dump_state(SPEC))
    pane = _pane(gtk)
    console.apply_chart(pane)
    assert pane.chart.spec == SPEC
    assert pane._stack.get_visible_child_name() == "chart"


def test_a_re_run_redraws_the_same_chart_from_the_new_rows(gtk):
    console = _console(gtk, charts.dump_state(SPEC))
    first = _pane(gtk)
    console.apply_chart(first)
    # A second run: new pane, same columns, more rows.
    second = _pane(gtk, COLUMNS, ROWS + [("Nice", "web", 7)])
    console._chart_spec = console.chart_spec()
    console._chart_panes = []
    console.apply_chart(second)
    assert second.chart.spec == SPEC
    assert second.chart._data.series
    # The user is left where they were on a re-run.
    assert second._stack.get_visible_child_name() == "data"


def test_a_console_hands_its_chart_back_to_the_workspace(gtk):
    console = _console(gtk, charts.dump_state(SPEC))
    console.apply_chart(_pane(gtk))
    state = console.tab_state()
    assert state.kind == "query"
    assert charts.load_state(state.chart) == SPEC


def test_an_edited_mapping_is_what_gets_saved(gtk):
    console = _console(gtk)
    pane = _pane(gtk)
    console.apply_chart(pane)
    pane.chart.set_spec(charts.ChartSpec(type="scatter", x="city", series=("sales",)))
    assert charts.load_state(console.chart_state()).type == "scatter"


def test_a_spec_whose_columns_are_gone_loses_those_parts_with_a_notice(gtk):
    console = _console(gtk, charts.dump_state(SPEC))
    pane = _pane(gtk, ["city", "orders"], [("Paris", 2), ("Lyon", 3)])
    console.apply_chart(pane)
    notice = pane.chart._notice.get_text()
    assert "sales" in notice
    assert pane.chart.spec is None or "sales" not in pane.chart.spec.columns()


def test_a_chart_from_an_unknown_version_is_reported_not_raised(gtk):
    text = json.dumps(
        {"version": charts.MODEL_VERSION + 1, "chart": charts.to_dict(SPEC)}
    )
    console = _console(gtk, text)
    pane = _pane(gtk)
    console.apply_chart(pane)
    assert "could not be restored" in pane.chart._notice.get_text()
    # And the result still draws, inferred.
    assert pane.chart.spec is not None


def test_a_console_with_no_chart_saves_nothing(gtk):
    console = _console(gtk)
    assert console.chart_state() == "" or console.tab_state().chart == ""
