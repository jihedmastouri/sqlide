"""Explain plans parsed into the tree the graph view draws.

The parsing lives in frontend/plan_graph.py apart from the widget
precisely so the three shapes EXPLAIN answers in can be checked without
a display: SQLite's id/parent rows, PostgreSQL's (and MySQL's
FORMAT=TREE) indented text, and MySQL's classic row-per-table pipeline.
"""

from __future__ import annotations

import pytest

plan_graph = pytest.importorskip("sqlide.frontend.plan_graph")

parse_plan = plan_graph.parse_plan


def titles(nodes):
    return [node.title for node in nodes]


# SQLite: (id, parent, notused, detail)


SQLITE_COLUMNS = ["id", "parent", "notused", "detail"]


def test_sqlite_rows_nest_by_parent():
    roots = parse_plan(
        SQLITE_COLUMNS,
        [
            (2, 0, 0, "SCAN users"),
            (4, 0, 0, "SEARCH orders USING INDEX ix_user (user_id=?)"),
        ],
    )
    assert len(roots) == 2
    assert titles(roots) == ["SCAN users", "SEARCH orders"]
    # The USING qualifier becomes an attribute rather than a long title.
    assert roots[1].details == ["USING INDEX ix_user (user_id=?)"]


def test_sqlite_child_rows_hang_off_their_parent():
    roots = parse_plan(
        SQLITE_COLUMNS,
        [
            (1, 0, 0, "COMPOUND QUERY"),
            (2, 1, 0, "LEFT-MOST SUBQUERY"),
            (3, 1, 0, "UNION ALL"),
        ],
    )
    assert len(roots) == 1
    assert titles(roots[0].children) == ["LEFT-MOST SUBQUERY", "UNION ALL"]
    assert sum(1 for _ in roots[0].walk()) == 3


# PostgreSQL / MySQL FORMAT=TREE: one column of indented text


def test_indented_text_becomes_a_tree_with_attributes():
    lines = [
        "Hash Join  (cost=1.09..2.18 rows=4 width=100)",
        "  Hash Cond: (orders.user_id = users.id)",
        "  ->  Seq Scan on orders  (cost=0.00..1.04 rows=4 width=40)",
        "  ->  Hash  (cost=1.04..1.04 rows=4 width=68)",
        "        ->  Seq Scan on users  (cost=0.00..1.04 rows=4 width=68)",
    ]
    roots = parse_plan(["QUERY PLAN"], [(line,) for line in lines])
    assert len(roots) == 1
    root = roots[0]
    assert root.title == "Hash Join"
    assert root.details == [
        "(cost=1.09..2.18 rows=4 width=100)",
        "Hash Cond: (orders.user_id = users.id)",
    ]
    assert titles(root.children) == ["Seq Scan on orders", "Hash"]
    assert titles(root.children[1].children) == ["Seq Scan on users"]


def test_a_single_cell_holding_the_whole_plan_is_split_too():
    plan = "\n".join(
        [
            "-> Nested loop inner join",
            "    -> Table scan on users",
            "    -> Index lookup on orders using ix_user",
        ]
    )
    roots = parse_plan(["EXPLAIN"], [(plan,)])
    assert titles(roots) == ["Nested loop inner join"]
    assert titles(roots[0].children) == [
        "Table scan on users",
        "Index lookup on orders using ix_user",
    ]


# MySQL's classic EXPLAIN: a row per accessed table, in join order


def test_tabular_rows_chain_in_join_order():
    columns = ["id", "select_type", "table", "type", "key", "rows", "Extra"]
    roots = parse_plan(
        columns,
        [
            (1, "SIMPLE", "users", "ALL", None, 4, ""),
            (1, "SIMPLE", "orders", "ref", "ix_user", 2, "Using index"),
        ],
    )
    assert len(roots) == 1
    assert roots[0].title == "users"
    assert titles(roots[0].children) == ["orders"]
    # Empty and NULL columns are dropped, the rest read as attributes.
    assert "key: ix_user" in roots[0].children[0].details
    assert not any(d.startswith("Extra") for d in roots[0].details)


def test_unrecognised_columns_still_yield_one_node_per_step():
    roots = parse_plan(
        ["step", "note"], [("first", "a"), ("second", "b")]
    )
    assert len(roots) == 1
    assert roots[0].title == "step 1"
    assert titles(roots[0].children) == ["step 2"]


def test_no_rows_means_no_graph():
    assert parse_plan(["QUERY PLAN"], []) == []
    assert plan_graph.plan_graph(["QUERY PLAN"], []) is None


def test_full_text_joins_the_title_and_its_attributes():
    roots = parse_plan(SQLITE_COLUMNS, [(2, 0, 0, "SCAN users USING INDEX ix")])
    assert roots[0].full_text() == "SCAN users\nUSING INDEX ix"


# A plan the user asked for by typing EXPLAIN gets the same views


def test_hand_written_explain_is_recognised():
    console = pytest.importorskip("sqlide.frontend.query_console")
    assert console._is_explain("EXPLAIN SELECT 1")
    assert console._is_explain("  explain analyze select 1")
    assert console._is_explain("-- why is this slow?\nEXPLAIN SELECT 1")
    assert console._is_explain("/* plan */ explain select 1")
    assert console._is_explain("DESCRIBE users")
    assert not console._is_explain("SELECT explain FROM notes")
    assert not console._is_explain("")
