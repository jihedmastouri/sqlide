"""Finding a value across a database's tables (CORE-45).

Two halves, like CORE-42's and CORE-43's. The planning half is pure:
given a catalog and a term, which columns are searched, what each
statement looks like and why a table was left out — all asserted with
no database anywhere. The second half runs a plan against a real SQLite
database through `scan()`, which is where progress, cancellation and an
unreadable table are asserted.
"""

from __future__ import annotations

import sqlite3

import pytest

from sqlide.backend.db.base import ColumnInfo, ConnectorError
from sqlide.backend.db import search
from sqlide.backend.db.search import (
    Hit,
    SearchOptions,
    SearchTable,
    column_kind,
    hit_filters,
    plan,
    scan,
    term_kinds,
)
from sqlide.backend.db.sqlite.connector import SqliteConnector


def col(name, type_name, pk=False):
    return ColumnInfo(name, type_name, pk)


CUSTOMERS = SearchTable(
    "customers",
    (
        col("id", "integer", True),
        col("name", "varchar(80)"),
        col("balance", "numeric(10,2)"),
        col("signed_up", "timestamp with time zone"),
        col("avatar", "bytea"),
        col("active", "boolean"),
    ),
)
ORDERS = SearchTable(
    "orders",
    (col("id", "integer", True), col("note", "text")),
    schema="sales",
)
CATALOG = [CUSTOMERS, ORDERS]


class TestTypes:
    def test_declared_types_land_in_their_kind(self):
        assert column_kind("character varying(40)") == "text"
        assert column_kind("BIGINT") == "integer"
        assert column_kind("double precision") == "number"
        assert column_kind("timestamp with time zone") == "timestamp"
        assert column_kind("date") == "date"
        assert column_kind("bytea") == "binary"
        assert column_kind("geometry(Point,4326)") == "geometry"

    def test_an_unknown_type_is_never_searched(self):
        assert column_kind("hstore") == "other"
        assert column_kind("") == "other"


class TestTermKinds:
    def test_a_word_is_only_text(self):
        assert term_kinds("ada") == ("text",)

    def test_a_number_reaches_the_numeric_columns(self):
        kinds = term_kinds("4812")
        assert "integer" in kinds and "number" in kinds and "text" in kinds

    def test_a_decimal_is_not_an_integer(self):
        kinds = term_kinds("9.50")
        assert "number" in kinds and "integer" not in kinds

    def test_a_date_reaches_date_and_timestamp_columns(self):
        assert set(term_kinds("2026-08-28")) >= {"date", "timestamp"}
        assert "integer" not in term_kinds("2026-08-28")


class TestColumnChoice:
    def test_a_word_searches_only_text_columns(self):
        [query] = plan(CATALOG[:1], "ada").queries
        assert query.columns == ("name",)
        assert "balance" not in query.sql and "avatar" not in query.sql

    def test_no_column_is_cast_to_text_to_make_it_match(self):
        [query] = plan(CATALOG[:1], "ada").queries
        lowered = query.sql.lower()
        assert "cast" not in lowered and "::text" not in lowered

    def test_a_numeric_term_reaches_the_numeric_columns(self):
        [query] = plan(CATALOG[:1], "42").queries
        assert set(query.columns) == {"name", "balance", "id"}
        assert "active" not in query.columns  # 42 is not a boolean

    def test_binary_and_geometry_are_never_searched(self):
        table = SearchTable(
            "shapes",
            (col("blob", "bytea"), col("area", "geometry")),
        )
        [skip] = plan([table], "1").skipped
        assert skip.table == "shapes" and "no column" in skip.reason

    def test_the_primary_key_comes_back_with_the_match(self):
        [query] = plan(CATALOG[:1], "ada").queries
        assert query.key_columns == ("id",)
        assert query.selected[0] == "id"


class TestStatements:
    def test_every_value_is_bound_never_written_into_the_sql(self):
        [query] = plan(CATALOG[:1], "o'brien").queries
        assert "o'brien" not in query.sql
        assert query.params == ("%o'brien%",)

    def test_every_statement_is_row_capped(self):
        options = SearchOptions(max_rows=25)
        for query in plan(CATALOG, "1", options).queries:
            assert query.sql.endswith("LIMIT 25")
            assert query.max_rows == 25

    def test_a_cap_below_one_is_refused(self):
        with pytest.raises(ConnectorError):
            plan(CATALOG, "x", SearchOptions(max_rows=0))

    def test_an_empty_term_is_refused(self):
        with pytest.raises(ConnectorError):
            plan(CATALOG, "   ")

    def test_identifiers_are_quoted_by_the_engine_not_interpolated(self):
        table = SearchTable("odd name", (col('we"ird', "text"),))
        [query] = plan([table], "x").queries
        assert '"odd name"' in query.sql and '"we""ird"' in query.sql

    def test_a_schema_qualifies_the_table(self):
        [query] = plan([ORDERS], "note").queries
        assert 'FROM "sales"."orders"' in query.sql
        assert query.label == "sales.orders"

    def test_contains_is_a_like_with_wildcards_escaped(self):
        [query] = plan(CATALOG[:1], "50%").queries
        assert "LIKE" in query.sql and "ESCAPE" in query.sql
        assert query.params == ("%50\\%%",)

    def test_exact_compares_the_whole_value(self):
        [query] = plan(CATALOG[:1], "ada", SearchOptions(exact=True)).queries
        assert "LIKE" not in query.sql
        assert query.params == ("ada",)

    def test_case_insensitive_lowers_the_column_not_casts_it(self):
        [query] = plan(CATALOG[:1], "Ada").queries
        assert "LOWER(" in query.sql
        assert query.params == ("%ada%",)

    def test_case_sensitive_leaves_the_column_alone(self):
        [query] = plan(
            CATALOG[:1], "Ada", SearchOptions(case_sensitive=True)
        ).queries
        assert "LOWER(" not in query.sql
        assert query.params == ("%Ada%",)

    def test_a_numeric_column_is_compared_as_a_number(self):
        [query] = plan(CATALOG[:1], "42").queries
        assert 42 in query.params and 42.0 in query.params
        assert query.display.count("42") >= 2

    def test_the_marker_is_the_engines_own(self):
        [query] = plan(CATALOG[:1], "ada", placeholder="%s").queries
        assert "%s" in query.sql


class TestScope:
    def test_system_schemas_are_left_out_with_a_reason(self):
        table = SearchTable(
            "pg_class", (col("relname", "name"),), schema="pg_catalog",
            system=True,
        )
        result = plan([table], "x")
        assert result.queries == ()
        assert result.skipped[0].reason == "system schema"

    def test_views_are_left_out_unless_asked_for(self):
        view = SearchTable("v_orders", (col("note", "text"),), kind="view")
        assert plan([view], "x").queries == ()
        assert plan([view], "x", SearchOptions(include_views=True)).queries

    def test_a_schema_selection_narrows_the_scan(self):
        result = plan(CATALOG, "1", SearchOptions(schemas=("sales",)))
        assert [q.table for q in result.queries] == ["orders"]
        assert result.skipped[0].reason == "schema not selected"

    def test_a_table_selection_narrows_the_scan(self):
        result = plan(CATALOG, "1", SearchOptions(tables=("customers",)))
        assert [q.table for q in result.queries] == ["customers"]

    def test_the_plan_states_how_many_tables_it_will_read(self):
        result = plan(CATALOG, "1")
        assert result.table_count == 2
        assert "2 tables will be scanned" in search.summary(result)


class TestHits:
    def test_a_key_hit_opens_the_row_itself(self):
        hit = Hit("customers", "", "name", "ada", {"id": 7, "name": "ada"},
                  ("id",))
        assert [(f.column, f.op, f.value) for f in hit_filters(hit)] == [
            ("id", "=", "7")
        ]

    def test_a_keyless_hit_falls_back_to_the_matched_value(self):
        hit = Hit("log", "", "body", "ada", {"body": "ada"}, ())
        assert [(f.column, f.value) for f in hit_filters(hit)] == [
            ("body", "ada")
        ]

    def test_only_the_column_that_matched_is_reported(self):
        [query] = plan(CATALOG[:1], "ada").queries
        hits = search.hits_in_row(query, {"id": 1, "name": "ada"},
                                  SearchOptions())
        assert [h.column for h in hits] == ["name"]


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "core45.db"
    sqlite3.connect(path).close()
    connector = SqliteConnector(str(path))
    connector.connect()
    connector.execute(
        "CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, "
        "balance REAL)"
    )
    connector.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
    connector.execute(
        "INSERT INTO customers VALUES (1, 'ada lovelace', 9.5), "
        "(2, 'grace hopper', 42.0)"
    )
    connector.execute("INSERT INTO notes VALUES (1, 'ask ada')")
    yield connector
    connector.close()


def catalog(connector) -> list[SearchTable]:
    return [
        SearchTable(
            table.name,
            tuple(connector.list_columns(table.name)),
            kind=table.kind,
        )
        for table in connector.list_tables()
    ]


def runner(connector):
    def execute(query):
        result = connector.run_bound(query.sql, query.params)
        return result.columns, result.rows

    return execute


class TestOverARealDatabase:
    def test_the_planned_statements_run_and_find_the_value(self, db):
        report = scan(
            plan(catalog(db), "ada", quote=db.quote_ident,
                 placeholder=db.placeholder),
            runner(db),
        )
        assert {(h.table, h.column) for h in report.hits} == {
            ("customers", "name"),
            ("notes", "body"),
        }
        assert report.scanned == 2 and not report.skipped

    def test_a_numeric_term_matches_the_number_not_its_text(self, db):
        report = scan(
            plan(catalog(db), "42", quote=db.quote_ident,
                 placeholder=db.placeholder),
            runner(db),
        )
        assert [(h.table, h.column, h.value) for h in report.hits] == [
            ("customers", "balance", 42.0)
        ]

    def test_a_hit_reopens_the_row_it_came_from(self, db):
        report = scan(
            plan(catalog(db), "grace", quote=db.quote_ident,
                 placeholder=db.placeholder),
            runner(db),
        )
        [hit] = report.hits
        rows = db.fetch_rows("customers", filters=hit_filters(hit)).rows
        assert [row[1] for row in rows] == ["grace hopper"]

    def test_progress_names_each_table_as_it_starts(self, db):
        seen = []
        scan(
            plan(catalog(db), "ada", quote=db.quote_ident),
            runner(db),
            on_progress=lambda index, query: seen.append(query.table),
        )
        assert seen == ["customers", "notes"]

    def test_a_cancelled_scan_stops_between_tables(self, db):
        seen = []
        report = scan(
            plan(catalog(db), "ada", quote=db.quote_ident),
            runner(db),
            on_progress=lambda index, query: seen.append(query.table),
            should_cancel=lambda: len(seen) >= 1,
        )
        assert report.cancelled and seen == ["customers"]
        assert report.scanned == 1

    def test_an_unreadable_table_is_skipped_with_its_reason(self, db):
        def execute(query):
            if query.table == "notes":
                raise ConnectorError("permission denied for table notes")
            return runner(db)(query)

        report = scan(plan(catalog(db), "ada", quote=db.quote_ident), execute)
        assert report.scanned == 1
        assert [(s.table, s.reason) for s in report.skipped] == [
            ("notes", "permission denied for table notes")
        ]
        assert [h.table for h in report.hits] == ["customers"]

    def test_the_overall_hit_cap_truncates_rather_than_runs_on(self, db):
        report = scan(
            plan(catalog(db), "a", SearchOptions(max_hits=1),
                 quote=db.quote_ident),
            runner(db),
        )
        assert report.truncated and len(report.hits) == 1

    def test_hits_arrive_one_by_one_while_the_scan_runs(self, db):
        streamed = []
        scan(
            plan(catalog(db), "ada", quote=db.quote_ident),
            runner(db),
            on_hit=streamed.append,
        )
        assert [h.table for h in streamed] == ["customers", "notes"]
