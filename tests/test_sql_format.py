"""The SQL formatter: idempotence, meaning preservation, and the
statements it refuses to touch. Pure text — no database server."""

from __future__ import annotations

import pytest

from sqlide.backend.sql_format import (
    FormatOptions,
    format_sql,
    format_statement,
)
from sqlide.backend.sql_split import lex

# Every fixture is formatted, re-formatted and checked for meaning.
FIXTURES = (
    "select a, b from t",
    "select a.id, b.name, count(*) from users a "
    "left join orders b on a.id = b.user_id "
    "where a.age > 18 and b.total is not null "
    "group by a.id having count(*) > 2 order by 2 desc limit 10;",
    "select 'from where select' as s, \"order\" from t",
    "select `select` from `from`",
    "select case when a = 1 then 'one' when a = 2 then 'two' "
    "else 'many' end as label, b from t",
    "select * from t where x between 1 and 5 and y in (1, 2, 3)",
    "select * from t where id in (select max(id) from u where k = 'a')",
    "with recent as (select * from t) select * from recent",
    "insert into t (a, b) values (1, 'x'), (2, 'y');",
    "update t set a = 1, b = 2 where id = -3",
    "delete from t where id = 1",
    "-- leading note\nselect 1;\n-- tail note\n",
    "select /* block\ncomment */ a from t -- trailing\n",
    "select a::text, b -> 'k' ->> 'j' from t where id = :id and n = $1",
    "select e'it\\'s', 'a''b' from t",
    "SELECT 1 UNION ALL SELECT 2",
)


def meaning(sql: str) -> list[tuple[str, str]]:
    """What the statement says, independent of layout and keyword
    case: every lexical piece, in order, with a bare word uppercased.
    Two scripts with the same list mean the same thing."""
    return [
        (p.kind, p.text.upper() if p.kind == "word" else p.text)
        for p in lex(sql)
    ]


@pytest.mark.parametrize("sql", FIXTURES)
def test_formatting_is_idempotent(sql):
    once = format_sql(sql).text
    assert format_sql(once).text == once


@pytest.mark.parametrize("sql", FIXTURES)
def test_formatting_preserves_meaning(sql):
    assert meaning(format_sql(sql).text) == meaning(sql)


@pytest.mark.parametrize("sql", FIXTURES)
def test_options_do_not_change_meaning(sql):
    options = FormatOptions(
        keyword_case="lower", indent=4, comma_leading=True
    )
    formatted = format_sql(sql, options).text
    assert meaning(formatted) == meaning(sql)
    assert format_sql(formatted, options).text == formatted


def test_clause_per_line_and_indented_join():
    sql = (
        "select a.id, b.name from users a left join orders b "
        "on a.id = b.user_id where a.age > 18 and b.paid order by a.id"
    )
    assert format_sql(sql).text == (
        "SELECT a.id,\n"
        "  b.name\n"
        "FROM users a\n"
        "  LEFT JOIN orders b\n"
        "    ON a.id = b.user_id\n"
        "WHERE a.age > 18\n"
        "  AND b.paid\n"
        "ORDER BY a.id"
    )


def test_subquery_is_indented_and_calls_are_not():
    assert format_sql("select count(*) from t where id in (select id from u)").text == (
        "SELECT count(*)\n"
        "FROM t\n"
        "WHERE id IN (\n"
        "  SELECT id\n"
        "  FROM u\n"
        ")"
    )


def test_keyword_case_follows_the_option():
    sql = "Select A From T"
    assert format_sql(sql).text == "SELECT A\nFROM T"
    lower = FormatOptions(keyword_case="lower")
    assert format_sql(sql, lower).text == "select A\nfrom T"
    leave = FormatOptions(keyword_case="leave")
    assert format_sql(sql, leave).text == "Select A\nFrom T"


def test_quoted_identifier_keeps_its_case():
    assert format_sql('select "Select" from "From"').text == (
        'SELECT "Select"\nFROM "From"'
    )


def test_keyword_after_a_dot_is_a_column_name():
    assert format_sql("select t.key, t.order from t").text == (
        "SELECT t.key,\n  t.order\nFROM t"
    )


def test_leading_commas():
    options = FormatOptions(comma_leading=True)
    assert format_sql("select a, b, c from t", options).text == (
        "SELECT a\n  , b\n  , c\nFROM t"
    )


def test_indent_width():
    assert format_sql("select a, b from t", FormatOptions(indent=4)).text == (
        "SELECT a,\n    b\nFROM t"
    )


def test_comment_keeps_its_place():
    formatted = format_sql("-- why\nselect a, b from t -- and here\n").text
    assert formatted.startswith("-- why\nSELECT a,")
    assert formatted.rstrip().endswith("FROM t -- and here")


def test_string_containing_keywords_is_untouched():
    sql = "select 'select * from where' from t"
    assert "'select * from where'" in format_sql(sql).text


def test_dollar_quoted_body_is_untouched():
    sql = "select $tag$ select 1; from where $tag$ as body"
    assert "$tag$ select 1; from where $tag$" in format_sql(sql).text


def test_delimiter_script_is_left_alone():
    sql = (
        "DELIMITER $$\n"
        "CREATE TRIGGER t AFTER INSERT ON x FOR EACH ROW BEGIN\n"
        "  INSERT INTO log VALUES (1);\n"
        "END$$\n"
        "DELIMITER ;\n"
    )
    result = format_sql(sql)
    assert result.text == sql
    assert not result.changed
    assert "DELIMITER" in result.reason


def test_routine_body_is_left_alone():
    sql = "create function f() returns int as $$ select 1; $$ language sql;"
    result = format_sql(sql)
    assert result.text == sql
    assert result.reason == "routine body"


@pytest.mark.parametrize(
    "sql, reason",
    (
        ("select a from t where x = 'unterminated", "unterminated string"),
        ("select a /* never closed from t", "unterminated comment"),
        ("select (a from t", "unbalanced parentheses"),
        ("select a) from t", "unbalanced parentheses"),
    ),
)
def test_unformattable_statement_comes_back_whole(sql, reason):
    result = format_statement(sql)
    assert result.text == sql
    assert not result.changed
    assert result.reason == reason


def test_a_bad_statement_does_not_stop_the_good_ones():
    sql = "select a from t; select b from 'oops"
    result = format_sql(sql)
    assert result.text.startswith("SELECT a\nFROM t;")
    assert "select b from 'oops" in result.text
    assert result.reason == "unterminated string"


def test_empty_input_is_returned_as_is():
    assert format_sql("").text == ""
    assert format_sql("   \n").text == "   \n"


def test_options_from_settings_reads_the_shared_keyword_case(monkeypatch):
    """The formatter has no keyword-case setting of its own: it reads
    the one completion uses, and "follow" (no prefix to follow) means
    "leave the keywords alone"."""
    from sqlide.backend import settings as settings_module
    from sqlide.backend.sql_format import options_from_settings

    for stored, expected in (
        ("upper", "upper"), ("lower", "lower"), ("follow", "leave")
    ):
        monkeypatch.setattr(
            settings_module.store.settings, "sql_keyword_case", stored
        )
        assert options_from_settings().keyword_case == expected
    monkeypatch.setattr(settings_module.store.settings, "sql_format_indent", 4)
    monkeypatch.setattr(
        settings_module.store.settings, "sql_format_comma_leading", True
    )
    options = options_from_settings()
    assert options.indent == 4 and options.comma_leading
