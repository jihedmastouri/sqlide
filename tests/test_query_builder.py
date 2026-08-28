"""The query builder's catalog, read through the MetadataProvider (CORE-18).

The builder used to call the connector's flat `list_tables()` /
`list_columns()`, which has no schemas, no views and no capability
flags. These tests pin what going through the provider bought: views
are selectable sources, a source is identified by a schema-qualified
key where the engine has schemas, and the SQL that comes out qualifies
its names — while the engines without schemas read exactly as they did.

`run_async` is collapsed onto this thread, so the catalog load is done
by the time the tab is constructed and no main loop is needed.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.db.base import FilterCondition, SortSpec
from sqlide.backend.db.query_model import render
from sqlide.backend.db.sqlite.connector import SqliteConnector
from sqlide.frontend import query_builder as builder_module
from sqlide.backend.workspaces import Workspace
from sqlide.frontend.query_builder import QueryBuilderTab


@pytest.fixture(autouse=True)
def inline_async(monkeypatch):
    def immediate(work, on_success, on_error):
        try:
            on_success(work())
        except Exception as exc:  # pragma: no cover - a failure is a failure
            on_error(exc)

    monkeypatch.setattr(builder_module, "run_async", immediate)


def _tab(profile: ConnectionProfile, connector, table: str = ""):
    return QueryBuilderTab(
        profile, lambda _p: connector, lambda message: None, table=table
    )


@pytest.fixture()
def sqlite_tab(tmp_path):
    path = tmp_path / "builder.db"
    sqlite3.connect(path).close()
    connector = SqliteConnector(str(path))
    connector.connect()
    connector.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
    connector.execute(
        "CREATE TABLE orders ("
        " id INTEGER PRIMARY KEY,"
        " user_id INTEGER REFERENCES users(id))"
    )
    connector.execute("CREATE VIEW recent AS SELECT * FROM orders")
    profile = ConnectionProfile(
        name="builder", kind="sqlite", file_path=str(path)
    )
    yield _tab(profile, connector, "orders"), connector
    connector.close()


def test_sqlite_picker_reads_as_it_always_did(sqlite_tab) -> None:
    tab, _connector = sqlite_tab
    keys = [source.key for source in tab._sources]
    # No schema level: bare names, and the base table the tab was
    # opened on is selected.
    assert "users" in keys and "orders" in keys
    assert tab._base_table() == "orders"
    assert render(tab.query_model(), dialect=tab._dialect()).sql.startswith(
        'SELECT *\nFROM "orders"'
    )


def test_views_are_selectable_sources(sqlite_tab) -> None:
    tab, _connector = sqlite_tab
    view = next(s for s in tab._sources if s.key == "recent")
    assert view.ref.kind == "view"
    # And the picker says what it is.
    assert "view" in view.label


def test_join_prefills_from_a_foreign_key(sqlite_tab) -> None:
    tab, _connector = sqlite_tab
    tab._add_join_row()
    row = tab._join_rows[0]
    row._table.set_selected(row._keys.index("users"))
    tab._sync_state()
    assert row.conditions() == [("orders.user_id", "=", "users.id")]
    sql = render(tab.query_model(), dialect=tab._dialect()).sql
    assert 'INNER JOIN "users"' in sql
    assert 'ON "orders"."user_id" = "users"."id"' in sql


def test_postgres_sources_and_sql_are_schema_qualified(postgres) -> None:
    _version, connector = postgres
    connector.execute("CREATE SCHEMA IF NOT EXISTS core18")
    connector.execute("DROP TABLE IF EXISTS core18.orders")
    connector.execute(
        "CREATE TABLE core18.orders ("
        " id integer PRIMARY KEY,"
        " user_id integer REFERENCES public.users(id))"
    )
    try:
        profile = ConnectionProfile(name="pg", kind="postgres")
        tab = _tab(profile, connector, "core18.orders")
        keys = [source.key for source in tab._sources]
        assert "core18.orders" in keys and "public.users" in keys
        assert tab._base_table() == "core18.orders"
        # A foreign key that leaves its own schema still prefills, and
        # both ends of the statement name their schema.
        tab._add_join_row()
        row = tab._join_rows[0]
        row._table.set_selected(row._keys.index("public.users"))
        tab._sync_state()
        # Columns are qualified by alias, which defaults to the bare
        # table name even where the source key carries a schema.
        assert row.conditions() == [("orders.user_id", "=", "users.id")]
        sql = render(tab.query_model(), dialect=tab._dialect()).sql
        assert 'FROM "core18"."orders"' in sql
        assert 'INNER JOIN "public"."users"' in sql
    finally:
        connector.execute("DROP SCHEMA core18 CASCADE")


# Persistence (CORE-19)


def _restored(tab, connector, state):
    """A fresh builder tab restored from `state`, as the window does."""
    return QueryBuilderTab(
        tab.profile,
        lambda _p: connector,
        lambda message: None,
        table=state.table,
        builder=state.builder,
    )


def _built_tab(sqlite_tab):
    """A builder with a join, checked columns, a filter and a sort."""
    tab, connector = sqlite_tab
    tab._add_join_row()
    row = tab._join_rows[0]
    row._table.set_selected(row._keys.index("users"))
    tab._sync_state()
    tab._checked = {"orders.id", "users.name"}
    tab._add_filter_row()
    tab._filter_rows[0].set_condition(
        FilterCondition(column="users.name", op="=", value="ada")
    )
    tab._add_sort_row()
    tab._sort_rows[0].set_spec(SortSpec(column="orders.id", descending=True))
    tab._distinct.set_active(True)
    tab._limit.set_value(42)
    tab._sync_state()
    return tab, connector


def test_tab_state_round_trips_the_whole_query(sqlite_tab) -> None:
    tab, connector = _built_tab(sqlite_tab)
    state = tab.tab_state()
    assert state.table == "orders"  # older builds still reopen the table

    back = _restored(tab, connector, state)
    assert back._base_table() == "orders"
    assert back._distinct.get_active() is True
    assert int(back._limit.get_value()) == 42
    assert back._checked == {"orders.id", "users.name"}
    assert [(r.kind(), r.table(), r.conditions()) for r in back._join_rows] == [
        ("INNER JOIN", "users", [("orders.user_id", "=", "users.id")])
    ]
    assert back._filter_rows[0].condition().column == "users.name"
    assert back._filter_rows[0].condition().value == "ada"
    assert back._sort_rows[0].spec() == SortSpec("orders.id", descending=True)
    # And the whole point: the same statement comes back out.
    assert render(back.query_model(), dialect=back._dialect()) == render(
        tab.query_model(), dialect=tab._dialect()
    )


def test_state_survives_the_workspace_file(sqlite_tab, tmp_path) -> None:
    tab, connector = _built_tab(sqlite_tab)
    workspace = Workspace(name="w")
    workspace.tabs = [tab.tab_state()]
    reread = Workspace.from_dict(json.loads(json.dumps(workspace.to_dict())))
    back = _restored(tab, connector, reread.tabs[0])
    assert render(back.query_model(), dialect=back._dialect()).sql == render(
        tab.query_model(), dialect=tab._dialect()
    ).sql


def test_older_workspace_without_a_model_opens_on_its_table(sqlite_tab) -> None:
    tab, connector = sqlite_tab
    # What a pre-CORE-19 build wrote: kind, connection and table only.
    old = Workspace.from_dict(
        {
            "name": "w",
            "tabs": [
                {
                    "kind": "querybuilder",
                    "connection": "builder",
                    "table": "orders",
                }
            ],
        }
    )
    state = old.tabs[0]
    assert state.builder == ""
    back = _restored(tab, connector, state)
    assert back._base_table() == "orders"
    assert back._join_rows == [] and back._filter_rows == []


def test_unknown_field_in_a_newer_workspace_is_ignored(sqlite_tab) -> None:
    workspace = Workspace.from_dict(
        {
            "name": "w",
            "tabs": [
                {
                    "kind": "querybuilder",
                    "connection": "builder",
                    "table": "orders",
                    "something_from_the_future": {"nested": 1},
                }
            ],
        }
    )
    assert workspace.tabs[0].table == "orders"


def test_dropped_table_and_column_are_left_out_with_an_explanation(
    sqlite_tab,
) -> None:
    tab, connector = _built_tab(sqlite_tab)
    state = tab.tab_state()
    connector.execute("DROP TABLE users")
    back = _restored(tab, connector, state)
    # The base table is still there, so the query opens — minus the
    # join, the checked column, the filter and the sort that named the
    # table that is gone.
    assert back._base_table() == "orders"
    assert back._join_rows == []
    assert back._checked == {"orders.id"}
    assert back._filter_rows == []
    assert "dropped" in back._status.get_text()
    assert render(back.query_model(), dialect=back._dialect()).sql.startswith(
        "SELECT DISTINCT"
    )


def test_a_gone_base_table_starts_fresh_rather_than_failing(
    sqlite_tab,
) -> None:
    tab, connector = _built_tab(sqlite_tab)
    state = tab.tab_state()
    connector.execute("DROP VIEW recent")
    connector.execute("DROP TABLE orders")
    back = _restored(tab, connector, state)
    assert back._join_rows == [] and back._checked == set()
    assert back._status.get_text()
