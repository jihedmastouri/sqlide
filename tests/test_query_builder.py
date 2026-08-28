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


# Joins: aliases, self-joins, multi-condition ON, all kinds (CORE-20)


@pytest.fixture()
def joins_tab(tmp_path):
    """A database with a self-referencing key and a composite one."""
    path = tmp_path / "joins.db"
    sqlite3.connect(path).close()
    connector = SqliteConnector(str(path))
    connector.connect()
    connector.execute(
        "CREATE TABLE staff ("
        " id INTEGER PRIMARY KEY,"
        " name TEXT,"
        " manager_id INTEGER REFERENCES staff(id))"
    )
    connector.execute(
        "CREATE TABLE parts ("
        " tenant TEXT, code TEXT, label TEXT,"
        " PRIMARY KEY (tenant, code))"
    )
    connector.execute(
        "CREATE TABLE lines ("
        " id INTEGER PRIMARY KEY, tenant TEXT, code TEXT,"
        " FOREIGN KEY (tenant, code) REFERENCES parts(tenant, code))"
    )
    profile = ConnectionProfile(
        name="joins", kind="sqlite", file_path=str(path)
    )
    yield _tab(profile, connector, "staff"), connector
    connector.close()


def _flat(sql: str) -> str:
    """The rendered statement on one line — the formatter's wrapping is
    not what these tests are about."""
    return " ".join(sql.split())


def _join_to(tab, key):
    """Add a join line on `key` and let the builder settle."""
    tab._add_join_row()
    row = tab._join_rows[-1]
    row._table.set_selected(row._keys.index(key))
    tab._sync_state()
    return row


def test_a_table_can_be_joined_to_itself(joins_tab) -> None:
    tab, _connector = joins_tab
    row = _join_to(tab, "staff")
    # The two sides get distinct aliases without anyone typing one.
    assert [i.alias for i in tab._instances()] == ["staff", "staff_2"]
    assert row.conditions() == [("staff.id", "=", "staff_2.manager_id")]
    sql = render(tab.query_model(), dialect=tab._dialect()).sql
    assert 'FROM "staff"' in sql
    assert 'INNER JOIN "staff" AS "staff_2"' in sql
    assert 'ON "staff"."id" = "staff_2"."manager_id"' in sql
    # And it is a statement the engine actually accepts.
    _connector.execute(sql)


def test_a_composite_key_prefills_every_condition(joins_tab) -> None:
    tab, _connector = joins_tab
    tab._table_dropdown.set_selected(
        [s.key for s in tab._sources].index("lines")
    )
    row = _join_to(tab, "parts")
    assert row.conditions() == [
        ("lines.tenant", "=", "parts.tenant"),
        ("lines.code", "=", "parts.code"),
    ]
    sql = render(tab.query_model(), dialect=tab._dialect()).sql
    assert (
        'ON "lines"."tenant" = "parts"."tenant"'
        ' AND "lines"."code" = "parts"."code"'
    ) in _flat(sql)
    _connector.execute(sql)


def test_a_condition_can_be_added_and_removed_by_hand(joins_tab) -> None:
    tab, _connector = joins_tab
    row = _join_to(tab, "staff")
    row._add_on_row(notify=True)
    tab._sync_state()
    row._on_rows[1].set_condition("staff.name", "staff_2.name", "<>")
    assert row.conditions() == [
        ("staff.id", "=", "staff_2.manager_id"),
        ("staff.name", "<>", "staff_2.name"),
    ]
    assert '<> "staff_2"."name"' in render(
        tab.query_model(), dialect=tab._dialect()
    ).sql
    # The last condition of a join cannot be removed — a join needs one.
    row._remove_on_row(row._on_rows[1])
    row._remove_on_row(row._on_rows[0])
    assert len(row._on_rows) == 1


def test_only_the_join_kinds_the_engine_has_are_offered(joins_tab) -> None:
    tab, _connector = joins_tab
    from dataclasses import replace as _replace

    tab._sql_dialect = _replace(
        tab._sql_dialect,
        join_kinds=("INNER JOIN", "LEFT JOIN", "CROSS JOIN"),
    )
    assert tab._join_kinds() == ["INNER JOIN", "LEFT JOIN", "CROSS JOIN"]
    tab._sql_dialect = _replace(
        tab._sql_dialect,
        join_kinds=(
            "INNER JOIN", "LEFT JOIN", "RIGHT JOIN",
            "FULL JOIN", "CROSS JOIN",
        ),
    )
    assert "FULL JOIN" in tab._join_kinds()
    assert "RIGHT JOIN" in tab._join_kinds()


def test_sqlite_declares_its_own_outer_joins(joins_tab) -> None:
    _tab_, connector = joins_tab
    modern = sqlite3.sqlite_version_info >= (3, 39)
    assert ("RIGHT JOIN" in connector.join_kinds) is modern
    assert ("FULL JOIN" in connector.join_kinds) is modern
    # Whatever the library has, the dialect says the same thing.
    from sqlide.backend.db.query_model import dialect_for

    assert set(dialect_for(connector).join_kinds) == set(connector.join_kinds)


def test_a_cross_join_takes_no_on_clause(joins_tab) -> None:
    tab, _connector = joins_tab
    row = _join_to(tab, "parts")
    kinds = tab._join_kinds()
    row._kind.set_selected(kinds.index("CROSS JOIN"))
    tab._sync_state()
    assert row.conditions() == []
    sql = render(tab.query_model(), dialect=tab._dialect()).sql
    assert 'CROSS JOIN "parts"' in sql and " ON " not in sql
    _connector.execute(sql)


def test_columns_filters_and_sorts_address_a_self_join_by_alias(
    joins_tab,
) -> None:
    tab, _connector = joins_tab
    _join_to(tab, "staff")
    names = tab._display_columns(tab._instances())
    # Both sides are in the checklist, and they do not merge.
    assert "staff.name" in names and "staff_2.name" in names
    tab._checked = {"staff.name", "staff_2.name"}
    tab._add_filter_row()
    tab._filter_rows[0].set_condition(
        FilterCondition(column="staff_2.name", op="=", value="ada")
    )
    tab._add_sort_row()
    tab._sort_rows[0].set_spec(SortSpec(column="staff.name"))
    tab._sync_state()
    query = render(tab.query_model(), dialect=tab._dialect())
    assert 'SELECT "staff"."name", "staff_2"."name"' in _flat(query.sql)
    assert 'WHERE "staff_2"."name" =' in query.sql
    assert 'ORDER BY "staff"."name" ASC' in query.sql
    assert query.params == ["ada"]


def test_renaming_an_alias_carries_its_columns_along(joins_tab) -> None:
    tab, _connector = joins_tab
    row = _join_to(tab, "staff")
    tab._checked = {"staff_2.name"}
    tab._add_filter_row()
    tab._filter_rows[0].set_condition(
        FilterCondition(column="staff_2.name", op="=", value="ada")
    )
    row._alias.set_text("boss")
    assert [i.alias for i in tab._instances()] == ["staff", "boss"]
    assert tab._checked == {"boss.name"}
    assert tab._filter_rows[0].condition().column == "boss.name"
    assert row.conditions() == [("staff.id", "=", "boss.manager_id")]
    assert 'INNER JOIN "staff" AS "boss"' in render(
        tab.query_model(), dialect=tab._dialect()
    ).sql


def test_a_self_join_survives_the_workspace(joins_tab) -> None:
    tab, connector = joins_tab
    row = _join_to(tab, "staff")
    row._alias.set_text("boss")
    tab._checked = {"staff.name", "boss.name"}
    tab._add_sort_row()
    tab._sort_rows[0].set_spec(SortSpec(column="boss.name", descending=True))
    tab._sync_state()
    back = _restored(tab, connector, tab.tab_state())
    assert [i.alias for i in back._instances()] == ["staff", "boss"]
    assert back._checked == {"staff.name", "boss.name"}
    assert back._sort_rows[0].spec() == SortSpec("boss.name", descending=True)
    assert render(back.query_model(), dialect=back._dialect()).sql == render(
        tab.query_model(), dialect=tab._dialect()
    ).sql


# Aggregates, grouping and having in the tab (CORE-21)


def _flat_sql(tab) -> str:
    return _flat(render(tab.query_model(), dialect=tab._dialect()).sql)


def test_an_aggregate_with_an_alias_renders_and_runs(sqlite_tab) -> None:
    tab, connector = sqlite_tab
    tab._checked = {"orders.user_id"}
    row = tab._add_aggregate_row()
    row.restore("COUNT", "", False, "orders_count")
    sql = _flat_sql(tab)
    assert 'SELECT "user_id", COUNT(*) AS "orders_count"' in sql
    # And the statement the engine is actually given comes back.
    result = connector.execute(render(tab.query_model()).sql.rstrip(";"))
    assert "orders_count" in result.columns


def test_an_aggregate_beside_a_column_groups_rather_than_failing(
    sqlite_tab,
) -> None:
    tab, _connector = sqlite_tab
    tab._checked = {"orders.user_id"}
    tab._add_aggregate_row().restore("COUNT", "", False, "n")
    assert tab._grouping_columns() == ["user_id"]
    assert 'GROUP BY "user_id"' in _flat_sql(tab)
    assert "user_id" in tab._group_note.get_text()
    # And the derived grouping is the user's to refuse.
    tab._auto_group.set_active(False)
    assert "GROUP BY" not in _flat_sql(tab)


def test_grouping_and_having_render_around_where_and_order(
    sqlite_tab,
) -> None:
    tab, _connector = sqlite_tab
    tab._checked = {"orders.user_id"}
    tab._add_aggregate_row().restore("COUNT", "id", True, "n")
    tab._add_filter_row()
    tab._filter_rows[0].set_condition(
        FilterCondition(column="user_id", op=">", value="1")
    )
    tab._add_having_row().set_condition(
        FilterCondition(column="n", op=">", value="2")
    )
    tab._add_sort_row()
    tab._sort_rows[0].set_spec(SortSpec(column="n", descending=True))
    sql = _flat_sql(tab)
    order = [
        sql.index(word)
        for word in ("WHERE", "GROUP BY", "HAVING", "ORDER BY")
    ]
    assert order == sorted(order)
    assert 'COUNT(DISTINCT "id") AS "n"' in sql
    assert 'HAVING COUNT(DISTINCT "id") > ?' in sql
    assert 'ORDER BY "n" DESC' in sql
    assert render(tab.query_model(), dialect=tab._dialect()).params == [
        "1",
        "2",
    ]


def test_an_unaliased_aggregate_sorts_by_its_expression(sqlite_tab) -> None:
    tab, _connector = sqlite_tab
    tab._add_aggregate_row().restore("MAX", "id", False, "")
    assert tab._aggregate_labels() == ["MAX(id)"]
    tab._add_sort_row()
    tab._sort_rows[0].set_spec(SortSpec(column="MAX(id)", descending=True))
    assert 'ORDER BY MAX("id") DESC' in _flat_sql(tab)


def test_a_free_text_expression_is_passed_through(sqlite_tab) -> None:
    tab, _connector = sqlite_tab
    tab._add_expression_row().restore("id * 2", "doubled")
    assert 'id * 2 AS "doubled"' in _flat_sql(tab)


def test_only_the_aggregates_the_engine_has_are_offered(sqlite_tab) -> None:
    import dataclasses

    tab, _connector = sqlite_tab
    assert tab._aggregate_functions() == ["COUNT", "SUM", "AVG", "MIN", "MAX"]
    tab._sql_dialect = dataclasses.replace(
        tab._sql_dialect, aggregates=("COUNT", "MAX")
    )
    assert tab._aggregate_functions() == ["COUNT", "MAX"]


def test_the_whole_summarised_query_survives_the_workspace(
    sqlite_tab,
) -> None:
    tab, connector = sqlite_tab
    tab._checked = {"orders.user_id"}
    tab._add_aggregate_row().restore("COUNT", "id", True, "n")
    tab._add_expression_row().restore("id * 2", "doubled")
    tab._add_having_row().set_condition(
        FilterCondition(column="n", op=">", value="2")
    )
    tab._add_sort_row()
    tab._sort_rows[0].set_spec(SortSpec(column="n", descending=True))
    back = _restored(tab, connector, tab.tab_state())
    assert back._checked == {"orders.user_id"}
    assert back._aggregate_labels() == ["n", "doubled"]
    assert back._having_rows[0].condition().column == "n"
    assert back._sort_rows[0].spec() == SortSpec("n", descending=True)
    assert _flat_sql(back) == _flat_sql(tab)
