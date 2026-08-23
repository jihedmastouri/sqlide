"""Statement splitter: baseline behavior plus the routine-body and
dollar-quote contexts that keep trigger/function bodies intact."""

from __future__ import annotations

from sqlide.backend.sql_split import split_statements, statement_at, tokens


def texts(sql: str, dialect: str = "") -> list[str]:
    return [s.text for s in split_statements(sql, dialect)]


def test_plain_statements_and_comments():
    sql = "SELECT 1;\n-- comment\nSELECT 2;\n/* block */\n"
    assert texts(sql) == ["SELECT 1", "-- comment\nSELECT 2"]


def test_semicolons_in_strings_and_identifiers():
    sql = "SELECT ';', \"a;b\", `c;d`; SELECT 2"
    assert texts(sql) == ["SELECT ';', \"a;b\", `c;d`", "SELECT 2"]


def test_transactions_are_not_blocks():
    sql = "BEGIN; UPDATE t SET x = 1; COMMIT;"
    assert texts(sql) == ["BEGIN", "UPDATE t SET x = 1", "COMMIT"]


def test_sqlite_trigger_body_stays_one_statement():
    sql = (
        "CREATE TRIGGER touch AFTER INSERT ON t\n"
        "FOR EACH ROW\n"
        "BEGIN\n"
        "  UPDATE t SET x = 1 WHERE id = NEW.id;\n"
        "  DELETE FROM log WHERE id = OLD.id;\n"
        "END;\n"
        "SELECT 1;"
    )
    assert texts(sql) == [sql.rsplit(";", 2)[0].strip(), "SELECT 1"]


def test_mysql_routine_with_end_if_and_case():
    sql = (
        "CREATE PROCEDURE p(IN a INT)\n"
        "BEGIN\n"
        "  IF a > 0 THEN\n"
        "    SELECT a;\n"
        "  END IF;\n"
        "  SELECT CASE WHEN a = 1 THEN 'one' ELSE 'other' END;\n"
        "  CASE a WHEN 1 THEN SELECT 1; ELSE SELECT 2; END CASE;\n"
        "END;\n"
        "CALL p(1);"
    )
    result = texts(sql)
    assert len(result) == 2
    assert result[0].startswith("CREATE PROCEDURE")
    assert result[0].endswith("END")
    assert result[1] == "CALL p(1)"


def test_single_statement_mysql_function():
    sql = (
        "CREATE FUNCTION f(a INT) RETURNS INT DETERMINISTIC "
        "RETURN a + 1; SELECT f(1);"
    )
    assert len(texts(sql)) == 2


def test_dollar_quoted_body():
    sql = (
        "CREATE OR REPLACE FUNCTION add_one(a integer)\n"
        "RETURNS integer LANGUAGE plpgsql AS $$\n"
        "BEGIN\n"
        "  RETURN a + 1;\n"
        "END;\n"
        "$$;\n"
        "SELECT add_one(1);"
    )
    result = texts(sql)
    assert len(result) == 2
    assert result[0].endswith("$$")


def test_tagged_dollar_quote_and_placeholder():
    sql = "SELECT $tag$ a; b $tag$, $1; SELECT 2"
    assert texts(sql) == ["SELECT $tag$ a; b $tag$, $1", "SELECT 2"]


def test_create_table_named_after_keywords():
    # BEGIN/END only count inside CREATE TRIGGER/FUNCTION/PROCEDURE;
    # a table using such words in identifiers must split normally.
    sql = 'CREATE TABLE "begin" (id INT); SELECT 1;'
    assert len(texts(sql)) == 2


def test_statement_at_maps_offsets():
    statements = split_statements("SELECT 1;\nSELECT 2;")
    assert statement_at(statements, 0).text == "SELECT 1"
    assert statement_at(statements, 9).text == "SELECT 1"
    assert statement_at(statements, 12).text == "SELECT 2"
    assert statement_at(statements, 99).text == "SELECT 2"
    assert statement_at([], 0) is None


def test_mysql_backslash_escaped_quote_does_not_end_the_string():
    sql = r"INSERT INTO t VALUES ('it\'s'); SELECT 1;"
    assert texts(sql, "mysql") == [
        r"INSERT INTO t VALUES ('it\'s')",
        "SELECT 1",
    ]


def test_doubled_quotes_still_work_with_backslash_escapes_on():
    sql = "INSERT INTO t VALUES ('it''s'); SELECT 1;"
    assert texts(sql, "mysql") == [
        "INSERT INTO t VALUES ('it''s')",
        "SELECT 1",
    ]


def test_trailing_backslash_ends_a_sqlite_string():
    # SQLite has no backslash escape: the quote closes the string.
    sql = r"INSERT INTO t VALUES ('a\'); SELECT 1;"
    assert texts(sql, "sqlite") == [
        r"INSERT INTO t VALUES ('a\')",
        "SELECT 1",
    ]


def test_postgres_escapes_only_inside_e_strings():
    escaped = r"SELECT E'it\'s'; SELECT 1;"
    assert texts(escaped, "postgres") == [r"SELECT E'it\'s'", "SELECT 1"]
    plain = r"SELECT 'a\', ';'; SELECT 1;"
    assert texts(plain, "postgres") == [r"SELECT 'a\', ';'", "SELECT 1"]


def test_e_must_not_be_the_tail_of_a_word():
    # `value'…'` is not an escape string, so the backslash is literal.
    sql = r"SELECT value'a\', ';'; SELECT 1;"
    assert texts(sql, "postgres") == [r"SELECT value'a\', ';'", "SELECT 1"]


def test_unknown_dialect_honours_e_strings_but_not_plain_ones():
    assert texts(r"SELECT E'it\'s'; SELECT 1;") == [
        r"SELECT E'it\'s'",
        "SELECT 1",
    ]
    assert texts(r"SELECT 'a\'; SELECT 1;") == [r"SELECT 'a\'", "SELECT 1"]


def test_backslash_escapes_apply_to_tokens_too():
    # Without the escape the string closes early and `DROP` inside it
    # reads as a keyword — exactly what the read-only guard must not do.
    sql = r"SELECT 'it\' DROP \'' AS x FROM t"
    assert [t.word for t in tokens(sql, "mysql")] == [
        "SELECT",
        "AS",
        "X",
        "FROM",
        "T",
    ]
    assert "DROP" in [t.word for t in tokens(sql, "sqlite")]


def test_mysql_delimiter_lines_are_skipped_and_honoured():
    sql = (
        "DELIMITER $$\n"
        "CREATE TRIGGER touch AFTER INSERT ON t FOR EACH ROW\n"
        "BEGIN\n"
        "  UPDATE t SET x = 1;\n"
        "END$$\n"
        "DELIMITER ;\n"
        "SELECT 1;\n"
    )
    statements = texts(sql, "mysql")
    assert len(statements) == 2
    assert statements[0].startswith("CREATE TRIGGER touch")
    assert statements[0].endswith("END")
    assert statements[1] == "SELECT 1"


def test_custom_delimiter_ends_plain_statements():
    sql = "DELIMITER //\nSELECT 1//\nSELECT ';'//\nDELIMITER ;\nSELECT 3;"
    assert texts(sql) == ["SELECT 1", "SELECT ';'", "SELECT 3"]


def test_semicolons_are_not_terminators_under_a_custom_delimiter():
    sql = "DELIMITER $$\nSELECT 1; SELECT 2$$\n"
    assert texts(sql) == ["SELECT 1; SELECT 2"]


def test_dollar_quoting_still_works():
    sql = "CREATE FUNCTION f() RETURNS int AS $$ SELECT 1; $$ LANGUAGE sql;\nSELECT 2;"
    assert texts(sql, "postgres") == [
        "CREATE FUNCTION f() RETURNS int AS $$ SELECT 1; $$ LANGUAGE sql",
        "SELECT 2",
    ]
