"""Stable, efficient grid paging (CORE-40).

The grid reads a table a page at a time and appends the pages as the
user scrolls, so a page boundary that moves is a correctness bug: rows
show up twice, or never. The adapter therefore always orders a page by
something total (the user's sort, closed by the row key) and, where
that order is unique-prefixed, walks it with a key comparison instead
of an OFFSET.

The rules are asserted here on SQLite, which needs no server;
tests/test_postgres.py and tests/test_mysql.py run walk_pages() against
the real engines.
"""

from __future__ import annotations

import sqlite3

import pytest

from sqlide.backend.db.base import (
    FilterCondition,
    PageCursor,
    SortSpec,
    build_keyset_clause,
)
from sqlide.backend.db.sqlite.connector import SqliteConnector

PAGE = 2


def walk_pages(db, table, page=PAGE, **kwargs):
    """Read `table` the way the grid does — page by page, carrying the
    cursor forward — and return the rows in the order they arrived."""
    rows: list[tuple] = []
    cursor = None
    offset = 0
    while True:
        result = db.fetch_rows(
            table, offset, page, cursor=cursor, **kwargs
        )
        rows.extend(result.rows)
        if len(result.rows) < page:
            return rows
        offset += len(result.rows)
        cursor = result.cursor
        assert len(rows) < 10_000, "paging never reached the end"


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "core40.db"
    sqlite3.connect(path).close()
    connector = SqliteConnector(str(path))
    connector.connect()
    connector.execute(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, team TEXT)"
    )
    connector.execute(
        "INSERT INTO users (id, name, team) VALUES "
        + ", ".join(
            f"({i}, 'name{i % 3}', 'team{i % 2}')" for i in range(1, 8)
        )
    )
    # No primary key: SQLite still has a rowid to order by.
    connector.execute("CREATE TABLE notes (body TEXT)")
    connector.execute(
        "INSERT INTO notes (body) VALUES ('a'), ('b'), ('c'), ('d'), ('e')"
    )
    connector.execute("CREATE VIEW names AS SELECT name FROM users")
    yield connector
    connector.close()


# The plan


def test_primary_key_closes_the_order(db):
    plan = db.paging_strategy("users")
    assert [s.column for s in plan.order_by] == ["id"]
    assert plan.keyset and plan.stable


def test_user_sort_leads_and_the_key_closes(db):
    plan = db.paging_strategy("users", [SortSpec("name")])
    assert [s.column for s in plan.order_by] == ["name", "id"]
    assert plan.keyset


def test_descending_sort_keeps_a_uniform_tiebreaker(db):
    plan = db.paging_strategy("users", [SortSpec("name", descending=True)])
    assert [(s.column, s.descending) for s in plan.order_by] == [
        ("name", True),
        ("id", True),
    ]
    assert plan.keyset


def test_mixed_directions_fall_back_to_offset(db):
    plan = db.paging_strategy(
        "users", [SortSpec("name"), SortSpec("team", descending=True)]
    )
    assert not plan.keyset
    assert plan.stable  # still ordered, just not by key comparison
    assert "mixed" in plan.note


def test_rowid_orders_a_table_with_no_primary_key(db):
    assert db.row_key_columns("notes") == ["rowid"]
    plan = db.paging_strategy("notes")
    assert [s.column for s in plan.order_by] == ["rowid"]
    # rowid is not one of the columns SELECT * returns, so it cannot be
    # carried forward as a cursor value — ordered, but by offset.
    assert plan.stable and not plan.keyset


def test_a_view_has_no_key_and_says_so(db):
    plan = db.paging_strategy("names")
    assert not plan.stable and not plan.keyset
    assert "not guaranteed" in plan.note
    assert db.fetch_rows("names").stable is False
    assert db.fetch_rows("names").order_note


# The SQL


def test_keyset_page_has_a_row_comparison_and_no_offset(db):
    first = db.fetch_rows("users", 0, PAGE)
    assert "OFFSET" in first.statement
    second = db.fetch_rows("users", PAGE, PAGE, cursor=first.cursor)
    assert '("id") > (2)' in second.statement
    assert "OFFSET" not in second.statement
    # Deep pages have the same shape as shallow ones.
    third = db.fetch_rows("users", 4, PAGE, cursor=second.cursor)
    assert third.statement.replace("(4)", "(2)") == second.statement


def test_descending_page_compares_the_other_way(db):
    first = db.fetch_rows(
        "users", 0, PAGE, order_by=[SortSpec("id", descending=True)]
    )
    second = db.fetch_rows(
        "users",
        PAGE,
        PAGE,
        order_by=[SortSpec("id", descending=True)],
        cursor=first.cursor,
    )
    assert '("id") < (6)' in second.statement


def test_offset_page_is_still_ordered(db):
    result = db.fetch_rows("notes", 0, PAGE)
    assert 'ORDER BY "rowid" ASC' in result.statement
    assert "OFFSET" in result.statement


def test_the_statement_shown_is_the_statement_run(db):
    first = db.fetch_rows("users", 0, PAGE, order_by=[SortSpec("name")])
    second = db.fetch_rows("users", PAGE, PAGE, cursor=first.cursor)
    for result in (first, second):
        assert db.execute(result.statement).rows == result.rows


def test_a_stale_cursor_is_ignored_rather_than_mispredicated(db):
    first = db.fetch_rows("users", 0, PAGE)
    # The sort changed under the cursor: its columns no longer match
    # the order, so the page falls back to offset.
    after = db.fetch_rows(
        "users", PAGE, PAGE, order_by=[SortSpec("name")], cursor=first.cursor
    )
    assert "OFFSET" in after.statement
    assert ">" not in after.statement


def test_build_keyset_clause_refuses_what_it_cannot_answer():
    quote = lambda name: f'"{name}"'  # noqa: E731
    order = [SortSpec("a"), SortSpec("b")]
    good = PageCursor(columns=["a", "b"], values=(1, 2))
    assert build_keyset_clause(good, order, quote) == (
        '("a", "b") > (?, ?)',
        [1, 2],
    )
    # A NULL makes the row comparison NULL, which drops rows silently.
    null = PageCursor(columns=["a", "b"], values=(1, None))
    assert build_keyset_clause(null, order, quote) == ("", [])
    # A cursor from a different order, or the other direction.
    other = PageCursor(columns=["a"], values=(1,))
    assert build_keyset_clause(other, order, quote) == ("", [])
    flipped = PageCursor(columns=["a", "b"], values=(1, 2), descending=True)
    assert build_keyset_clause(flipped, order, quote) == ("", [])
    assert build_keyset_clause(None, order, quote) == ("", [])


# Walking a whole table


def test_every_row_exactly_once(db):
    ids = [row[0] for row in walk_pages(db, "users")]
    assert ids == list(range(1, 8))


def test_every_row_exactly_once_with_a_non_unique_sort(db):
    rows = walk_pages(db, "users", order_by=[SortSpec("name")])
    ids = sorted(row[0] for row in rows)
    assert ids == list(range(1, 8))
    assert [row[1] for row in rows] == sorted(row[1] for row in rows)


def test_every_row_exactly_once_with_a_filter(db):
    rows = walk_pages(
        db,
        "users",
        filters=[FilterCondition(column="team", op="=", value="team1")],
        order_by=[SortSpec("name")],
    )
    ids = sorted(row[0] for row in rows)
    assert ids == [1, 3, 5, 7]


def test_every_row_exactly_once_descending(db):
    ids = [row[0] for row in walk_pages(
        db, "users", order_by=[SortSpec("id", descending=True)]
    )]
    assert ids == list(range(7, 0, -1))


def test_every_row_exactly_once_without_a_primary_key(db):
    bodies = [row[0] for row in walk_pages(db, "notes")]
    assert bodies == ["a", "b", "c", "d", "e"]
