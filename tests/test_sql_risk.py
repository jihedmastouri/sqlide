"""The destructive-statement classifier and the confirmation ladder.

The classifier decides whether the user gets a dialog before their
statement runs, so its blind spots are the interesting cases: keywords
hiding in strings, comments and quoted identifiers, and a WHERE clause
that isn't there.
"""

from __future__ import annotations

import pytest

from sqlide.backend import sql_risk


def test_drop_names_its_target():
    risk = sql_risk.classify("DROP TABLE IF EXISTS orders")
    assert risk.action == "drop"
    assert risk.target == "orders"
    assert risk.destructive and risk.severe


def test_truncate_and_delete_and_update():
    assert sql_risk.classify("TRUNCATE TABLE logs").target == "logs"
    delete = sql_risk.classify("DELETE FROM users WHERE id = 1")
    assert delete.action == "delete"
    assert delete.target == "users"
    assert not delete.unfiltered and not delete.severe
    update = sql_risk.classify("UPDATE users SET active = 0")
    assert update.action == "update"
    assert update.target == "users"
    assert update.unfiltered and update.severe


def test_reads_and_unknown_statements_are_not_destructive():
    for sql in (
        "SELECT * FROM users",
        "WITH x AS (SELECT 1) SELECT * FROM x",
        "EXPLAIN SELECT 1",
        "VACUUM",
        "BEGIN",
        "",
    ):
        assert not sql_risk.classify(sql).destructive, sql


def test_creates_are_not_destructive():
    risk = sql_risk.classify("CREATE TABLE orders (id INT)")
    assert risk.action == "create"
    assert not risk.destructive


def test_keywords_inside_strings_and_comments_are_ignored():
    risk = sql_risk.classify(
        "SELECT 'DROP TABLE orders' -- DELETE FROM users\n FROM t"
    )
    assert risk.action == "read"
    # A WHERE inside a string must not make an unfiltered DELETE look
    # filtered.
    delete = sql_risk.classify("DELETE FROM t /* WHERE id = 1 */")
    assert delete.unfiltered


def test_quoted_identifiers_are_names_not_keywords():
    risk = sql_risk.classify('DROP TABLE "select"')
    assert risk.action == "drop"
    assert risk.target == "select"
    assert sql_risk.classify("DROP TABLE `from`").target == "from"


def test_worst_picks_the_riskiest_statement_of_a_script():
    script = ["SELECT 1", "UPDATE t SET a = 1 WHERE id = 2", "DROP TABLE t"]
    assert sql_risk.worst(script).action == "drop"
    assert sql_risk.worst(["SELECT 1", "SELECT 2"]).destructive is False
    assert sql_risk.worst([]).action == "other"


@pytest.mark.parametrize(
    "environment,sql,expected",
    [
        # Development stays out of the way.
        ("development", "DROP TABLE t", "none"),
        ("development", "DELETE FROM t", "none"),
        # Staging asks, once.
        ("staging", "DROP TABLE t", "confirm"),
        ("staging", "DELETE FROM t WHERE id = 1", "confirm"),
        # Production asks for the object's name on the severe ones.
        ("production", "DROP TABLE t", "type"),
        ("production", "TRUNCATE TABLE t", "type"),
        ("production", "DELETE FROM t", "type"),
        ("production", "DELETE FROM t WHERE id = 1", "confirm"),
        ("production", "SELECT * FROM t", "none"),
        # An unclassified connection still catches the severe ones.
        ("unset", "DROP TABLE t", "confirm"),
        ("unset", "DELETE FROM t WHERE id = 1", "none"),
        ("unset", "DELETE FROM t", "confirm"),
    ],
)
def test_confirmation_level(environment, sql, expected):
    assert (
        sql_risk.confirmation_level(sql_risk.classify(sql), environment)
        == expected
    )


def test_confirm_mode_overrides():
    risk = sql_risk.classify("DROP TABLE t")
    assert sql_risk.confirmation_level(risk, "production", "never") == "none"
    assert sql_risk.confirmation_level(risk, "development", "never") == "none"
    assert (
        sql_risk.confirmation_level(risk, "development", "always") == "confirm"
    )
    # An unrecognised mode falls back to the default rather than
    # turning confirmations off.
    assert sql_risk.confirmation_level(risk, "production", "yes") == "type"


def test_describe_spells_out_the_missing_where():
    text = sql_risk.classify("DELETE FROM orders").describe()
    assert "DELETE orders" in text
    assert "no WHERE clause" in text
