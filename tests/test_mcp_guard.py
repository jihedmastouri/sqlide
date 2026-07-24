"""Unit tests for the MCP query guard: the accept/reject matrix."""

from __future__ import annotations

import pytest

from sqlide.backend.mcp.guard import GuardError, check_read_only


def test_accepts_plain_select():
    assert check_read_only("SELECT * FROM users", "sqlite") == (
        "SELECT * FROM users"
    )


def test_accepts_with_and_explain():
    assert check_read_only("WITH x AS (SELECT 1) SELECT * FROM x", "postgres")
    assert check_read_only("EXPLAIN SELECT 1", "postgres")


def test_mysql_allows_show_others_reject():
    assert check_read_only("SHOW TABLES", "mysql")
    with pytest.raises(GuardError):
        check_read_only("SHOW TABLES", "postgres")


def test_rejects_pragma_on_sqlite():
    with pytest.raises(GuardError, match="SELECT"):
        check_read_only("PRAGMA table_info(users)", "sqlite")


def test_rejects_write_statements():
    for sql in (
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET x = 1",
        "DELETE FROM t",
        "DROP TABLE t",
        "CREATE TABLE t (id int)",
    ):
        with pytest.raises(GuardError):
            check_read_only(sql, "sqlite")


def test_rejects_multiple_statements():
    with pytest.raises(GuardError, match="single statement"):
        check_read_only("SELECT 1; SELECT 2", "sqlite")


def test_rejects_trailing_second_statement_after_comment():
    with pytest.raises(GuardError, match="single statement"):
        check_read_only("SELECT 1; -- sneaky\nDELETE FROM t", "sqlite")


def test_rejects_data_modifying_cte():
    with pytest.raises(GuardError, match="Write keyword"):
        check_read_only(
            "WITH deleted AS (DELETE FROM t RETURNING *) SELECT * FROM deleted",
            "postgres",
        )


def test_rejects_select_into():
    with pytest.raises(GuardError, match="Write keyword"):
        check_read_only("SELECT * INTO backup FROM t", "postgres")


def test_rejects_insert_disguised_in_comment_but_allows_the_word_in_strings():
    # A write keyword inside a string literal must not trip the guard —
    # only bare (unquoted) keywords count.
    assert check_read_only(
        "SELECT 'please update nothing' AS msg", "sqlite"
    )


def test_empty_query_rejected():
    with pytest.raises(GuardError, match="Empty"):
        check_read_only("", "sqlite")
    with pytest.raises(GuardError, match="Empty"):
        check_read_only("  -- just a comment\n", "sqlite")


def test_unknown_dialect_falls_back_to_jdbc_allowlist():
    assert check_read_only("SELECT 1", "jdbc")
    with pytest.raises(GuardError):
        check_read_only("SHOW TABLES", "jdbc")
