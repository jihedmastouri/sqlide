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

import sqlite3

import pytest

from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.db.query_model import render
from sqlide.backend.db.sqlite.connector import SqliteConnector
from sqlide.frontend import query_builder as builder_module
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
    assert (row.left(), row.right()) == ("orders.user_id", "users.id")
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
        assert (row.left(), row.right()) == (
            "core18.orders.user_id", "public.users.id",
        )
        sql = render(tab.query_model(), dialect=tab._dialect()).sql
        assert 'FROM "core18"."orders"' in sql
        assert 'INNER JOIN "public"."users"' in sql
    finally:
        connector.execute("DROP SCHEMA core18 CASCADE")
