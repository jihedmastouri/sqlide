"""The value-search tab (CORE-45).

The planning is asserted in tests/test_search.py, with no widgets. What
is left for here is what the tab promises around it: the table count
stated before anything runs, the production connection that has to be
answered first, the hits landing in the grid and opening the row they
came from, and Stop reaching the worker.

The tab's work runs through `run_async`, so the worker is collapsed
onto the calling thread here — the callbacks are the same ones the main
loop would deliver, in the same order.
"""

from __future__ import annotations

import sqlite3

import pytest

from sqlide.backend.connections import ConnectionProfile
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
def db(tmp_path):
    path = tmp_path / "search.db"
    sqlite3.connect(path).close()
    connector = SqliteConnector(str(path))
    connector.connect()
    connector.execute(
        "CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT)"
    )
    connector.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
    connector.execute("INSERT INTO customers VALUES (1, 'ada lovelace')")
    connector.execute("INSERT INTO notes VALUES (1, 'ask ada')")
    yield connector
    connector.close()


@pytest.fixture()
def sync(monkeypatch):
    """run_async, on this thread: the tab's flow without a main loop."""
    from sqlide.frontend import data_search

    def immediate(work, on_success, on_error):
        try:
            value = work()
        except Exception as exc:  # delivered exactly as the real one does
            on_error(exc)
        else:
            on_success(value)

    monkeypatch.setattr(data_search, "run_async", immediate)


def _tab(gtk, db, environment="development", opened=None):
    opened = [] if opened is None else opened
    from sqlide.frontend.data_search import DataSearchTab

    profile = ConnectionProfile(
        "shop", "sqlite", file_path=":memory:", environment=environment
    )
    errors: list[str] = []
    tab = DataSearchTab(
        profile,
        lambda _profile: db,
        errors.append,
        lambda profile, target: opened.append((profile, target)),
    )
    return tab, errors


class TestDeclaringTheScan:
    def test_the_table_count_is_stated_before_anything_runs(
        self, gtk, db, sync
    ):
        tab, _errors = _tab(gtk, db)
        assert "2 tables" in tab._status.get_label()
        assert "100 rows" in tab._status.get_label()

    def test_a_development_connection_raises_no_banner(self, gtk, db, sync):
        tab, _errors = _tab(gtk, db)
        assert tab._banner.get_revealed() is False

    def test_a_production_connection_is_warned_about_up_front(
        self, gtk, db, sync
    ):
        tab, _errors = _tab(gtk, db, environment="production")
        assert tab._banner.get_revealed() is True
        assert "Production" in tab._banner.get_title()


class TestRunning:
    def test_a_search_fills_the_grid_with_table_column_and_value(
        self, gtk, db, sync
    ):
        tab, errors = _tab(gtk, db)
        tab._entry.set_text("ada")
        tab.start()
        assert errors == []
        assert [(h.table, h.column) for h in tab._hits] == [
            ("customers", "name"),
            ("notes", "body"),
        ]
        assert tab._grid._store.get_n_items() == 2

    def test_the_status_reports_what_the_scan_did(self, gtk, db, sync):
        tab, _errors = _tab(gtk, db)
        tab._entry.set_text("ada")
        tab.start()
        assert "2 hits in 2 tables" in tab._status.get_label()

    def test_a_term_no_column_could_hold_says_so_and_runs_nothing(
        self, gtk, db, sync
    ):
        db.execute("DROP TABLE customers")
        db.execute("DROP TABLE notes")
        db.execute("CREATE TABLE files (id INTEGER PRIMARY KEY, data BLOB)")
        tab, _errors = _tab(gtk, db)
        tab._entry.set_text("ada")  # no text column left to hold it
        tab.start()
        assert tab._hits == []
        assert "No table" in tab._status.get_label()
        assert tab._skipped_row.get_visible() is True

    def test_an_empty_term_does_nothing(self, gtk, db, sync):
        tab, _errors = _tab(gtk, db)
        tab.start()
        assert tab._plan is None

    def test_stop_asks_the_worker_to_stop(self, gtk, db, sync):
        tab, _errors = _tab(gtk, db)
        tab.stop()
        assert tab._cancelled is True

    def test_a_table_that_cannot_be_read_becomes_a_reported_skip(
        self, gtk, db, sync, monkeypatch
    ):
        original = SqliteConnector.run_bound

        def refuse(self, sql, params=()):
            if "notes" in sql:
                raise RuntimeError("permission denied for table notes")
            return original(self, sql, params)

        monkeypatch.setattr(SqliteConnector, "run_bound", refuse)
        tab, errors = _tab(gtk, db)
        tab._entry.set_text("ada")
        tab.start()
        assert errors == []
        assert [h.table for h in tab._hits] == ["customers"]
        assert "permission denied" in tab._skipped_label.get_label()
        assert tab._skipped_row.get_visible() is True


class TestOptions:
    def test_whole_value_and_case_reach_the_plan(self, gtk, db, sync):
        tab, _errors = _tab(gtk, db)
        tab._exact.set_active(True)
        tab._case.set_active(True)
        tab._rows.set_value(5)
        options = tab.options()
        assert options.exact and options.case_sensitive
        assert options.max_rows == 5

    def test_whole_value_finds_only_the_whole_value(self, gtk, db, sync):
        tab, _errors = _tab(gtk, db)
        tab._exact.set_active(True)
        tab._entry.set_text("ada")
        tab.start()
        assert tab._hits == []
        tab._entry.set_text("ada lovelace")
        tab.start()
        assert [h.table for h in tab._hits] == ["customers"]


class TestOpeningAHit:
    def test_a_hit_opens_its_table_filtered_to_the_row(self, gtk, db, sync):
        opened: list = []
        tab, _errors = _tab(gtk, db, opened=opened)
        tab._entry.set_text("lovelace")
        tab.start()
        tab._open_hit(0)
        [(profile, target)] = opened
        assert profile.name == "shop"
        assert target.table == "customers"
        assert [(f.column, f.op, f.value) for f in target.filters] == [
            ("id", "=", "1")
        ]

    def test_the_filter_selects_that_row_in_the_table(self, gtk, db, sync):
        opened: list = []
        tab, _errors = _tab(gtk, db, opened=opened)
        tab._entry.set_text("lovelace")
        tab.start()
        tab._open_hit(0)
        [(_profile, target)] = opened
        rows = db.fetch_rows("customers", filters=target.filters).rows
        assert [row[1] for row in rows] == ["ada lovelace"]


class TestProductionGate:
    def test_production_asks_before_it_scans(
        self, gtk, db, sync, monkeypatch
    ):
        from sqlide.frontend import data_search

        asked: list[dict] = []
        monkeypatch.setattr(
            data_search.confirm,
            "present",
            lambda parent, **kwargs: asked.append(kwargs),
        )
        tab, _errors = _tab(gtk, db, environment="production")
        tab._entry.set_text("ada")
        tab.start()
        assert tab._hits == []  # nothing ran yet
        assert "2 tables" in asked[0]["heading"]
        asked[0]["on_confirm"]()
        assert [h.table for h in tab._hits] == ["customers", "notes"]

    def test_a_development_connection_is_not_asked(self, gtk, db, sync,
                                                   monkeypatch):
        from sqlide.frontend import data_search

        asked: list = []
        monkeypatch.setattr(
            data_search.confirm,
            "present",
            lambda parent, **kwargs: asked.append(kwargs),
        )
        tab, _errors = _tab(gtk, db)
        tab._entry.set_text("ada")
        tab.start()
        assert asked == [] and tab._hits


class TestSidebar:
    def test_the_search_is_offered_on_a_connection_row(self, gtk, db):
        from sqlide.frontend.sidebar import Sidebar

        def unused(*_args, **_kwargs):
            raise AssertionError("callback should not fire")

        found: list = []
        bar = Sidebar(
            ensure_connector=lambda _profile: db,
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
            on_data_search=found.append,
        )
        profile = ConnectionProfile("shop", "sqlite", file_path=":memory:")
        bar.add_profile(profile)
        node = bar._roots.get_item(0)
        labels = []
        menu = bar._menu_for(node)
        for index in range(menu.get_n_items()):
            value = menu.get_item_attribute_value(index, "label", None)
            if value is not None:
                labels.append(value.get_string())
        assert "Find Data…" in labels
        bar.set_menu_node(node)
        bar._menu_data_search()
        assert [p.name for p in found] == ["shop"]
