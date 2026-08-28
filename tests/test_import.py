"""Loading a CSV file into a table (CORE-37).

Two halves, matching the module's own split. Sniffing, mapping and
coercion are pure text work and are asserted with no database at all;
the execution half runs against SQLite, which needs no server, because
the promise worth testing there — a failure at row N leaves the table
exactly as it was — is a promise about a transaction.

The awkward files are the point of the first half: a byte-order mark,
CRLF endings, a quoted field with a newline in it, non-ASCII text, and
a row missing its last column. Each is either handled or refused by
name; none of them may turn into a silent NULL.
"""

from __future__ import annotations

import sqlite3

import pytest

from sqlide.backend import importer
from sqlide.backend.db.base import BatchError, BulkCancelled
from sqlide.backend.db.sqlite.connector import SqliteConnector


def write(tmp_path, name: str, text: str, encoding: str = "utf-8"):
    path = tmp_path / name
    path.write_bytes(text.encode(encoding))
    return path


# Sniffing


def test_sniff_finds_the_delimiter_and_the_header(tmp_path):
    path = write(tmp_path, "a.csv", "id;name\n1;ada\n2;brian\n")
    dialect = importer.sniff(path)
    assert dialect.delimiter == ";"
    assert dialect.has_header is True
    assert importer.header(path, dialect) == ["id", "name"]


def test_sniff_reads_a_tab_separated_file(tmp_path):
    path = write(tmp_path, "a.tsv", "id\tname\n1\tada\n2\tbrian\n")
    assert importer.sniff(path).delimiter == "\t"


def test_a_byte_order_mark_is_not_part_of_the_first_column_name(tmp_path):
    path = write(tmp_path, "bom.csv", "id,name\n1,ada\n", "utf-8-sig")
    dialect = importer.sniff(path)
    assert dialect.encoding == "utf-8-sig"
    # Read as plain UTF-8 the first name would be "﻿id", which
    # then matches no column in any table.
    assert importer.header(path, dialect) == ["id", "name"]


def test_crlf_endings_leave_no_carriage_return_in_the_last_column(tmp_path):
    path = write(tmp_path, "crlf.csv", "id,name\r\n1,ada\r\n2,brian\r\n")
    dialect = importer.sniff(path)
    assert importer.preview(path, dialect) == [["1", "ada"], ["2", "brian"]]


def test_a_quoted_field_may_hold_a_newline(tmp_path):
    path = write(
        tmp_path, "q.csv", 'id,note\n1,"first\nsecond"\n2,plain\n'
    )
    dialect = importer.Dialect()
    assert importer.preview(path, dialect) == [
        ["1", "first\nsecond"],
        ["2", "plain"],
    ]


def test_non_ascii_text_survives(tmp_path):
    path = write(tmp_path, "u.csv", "id,name\n1,Ångström\n")
    dialect = importer.sniff(path)
    assert importer.preview(path, dialect) == [["1", "Ångström"]]


def test_the_wrong_encoding_is_refused_by_name(tmp_path):
    path = write(tmp_path, "l.csv", "id,name\n1,café\n", "latin-1")
    dialect = importer.Dialect(encoding="utf-8")
    with pytest.raises(importer.ImportFailed) as caught:
        importer.preview(path, dialect)
    assert "utf-8" in str(caught.value)
    # Told the truth about the file, it reads.
    assert importer.preview(
        path, importer.Dialect(encoding="latin-1")
    ) == [["1", "café"]]


def test_an_unknown_encoding_is_named(tmp_path):
    path = write(tmp_path, "a.csv", "id\n1\n")
    with pytest.raises(importer.ImportFailed) as caught:
        importer.open_text(path, importer.Dialect(encoding="klingon-1"))
    assert "klingon-1" in str(caught.value)


def test_a_headerless_file_gets_positional_names(tmp_path):
    path = write(tmp_path, "n.csv", "1,ada\n2,brian\n")
    dialect = importer.Dialect(has_header=False)
    assert importer.header(path, dialect) == ["Column 1", "Column 2"]
    assert len(importer.preview(path, dialect)) == 2


# Mapping


def test_default_mapping_matches_names_ignoring_case():
    mapping = importer.default_mapping(
        ["ID", " Name ", "extra"], ["id", "name", "email"]
    )
    assert mapping.targets == ["id", "name"]
    # A name the table does not have starts skipped rather than being
    # pointed at whatever column sits in that position.
    assert mapping.columns[2].skip is True


def test_a_mapping_with_nothing_in_it_says_so():
    mapping = importer.default_mapping(["a"], ["b"])
    assert "at least one" in mapping.problem()


def test_two_sources_may_not_fill_one_column():
    mapping = importer.Mapping(
        (
            importer.ColumnMap(0, "name"),
            importer.ColumnMap(1, "name"),
        )
    )
    assert "name" in mapping.problem()


# Coercion


@pytest.mark.parametrize(
    "text,kind,expected",
    [
        ("007", "integer", 7),
        ("42.0", "integer", 42),
        (" 3 ", "integer", 3),
        ("2.5", "number", 2.5),
        ("yes", "boolean", True),
        ("0", "boolean", False),
        ("0x4142", "binary", b"AB"),
        (" keep me ", "text", " keep me "),
        ("2024-01-02", "text", "2024-01-02"),
    ],
)
def test_coercion(text, kind, expected):
    assert importer.coerce(text, kind) == expected


@pytest.mark.parametrize(
    "text,kind",
    [("seven", "integer"), ("42.5", "integer"), ("maybe", "boolean")],
)
def test_a_value_that_cannot_be_coerced_is_refused(text, kind):
    with pytest.raises(ValueError):
        importer.coerce(text, kind)


def test_a_bad_value_names_its_line_and_column():
    mapping = importer.default_mapping(["id"], ["id"])
    with pytest.raises(importer.RowError) as caught:
        importer.build_row(7, ["seven"], mapping, {"id": "integer"})
    error = caught.value
    assert error.line == 7 and error.column == "id"
    assert "'seven'" in str(error)


def test_a_missing_trailing_column_is_refused_not_nulled():
    mapping = importer.default_mapping(["id", "name"], ["id", "name"])
    with pytest.raises(importer.RowError) as caught:
        importer.build_row(3, ["1"], mapping)
    assert "row has 1 values" in str(caught.value)


def test_the_null_token_is_the_only_thing_that_becomes_null():
    mapping = importer.Mapping(
        (importer.ColumnMap(0, "name", null_token="\\N"),)
    )
    assert importer.build_row(1, ["\\N"], mapping) == (None,)
    assert importer.build_row(1, [""], mapping) == ("",)


def test_blank_rows_are_skipped_and_counted():
    mapping = importer.default_mapping(["id"], ["id"])
    skipped = []
    rows = list(
        importer.build_rows(
            [(1, ["1"]), (2, ["  "]), (3, ["2"])],
            mapping,
            {"id": "integer"},
            on_skip=skipped.append,
        )
    )
    assert rows == [(1,), (2,)]
    assert skipped == [2]


# Execution, against SQLite


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "core37.db"
    sqlite3.connect(path).close()
    connector = SqliteConnector(str(path))
    connector.connect()
    connector.execute(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL, "
        "score INTEGER)"
    )
    yield connector
    connector.close()


def rows_of(db, sql="SELECT id, name, score FROM users ORDER BY id"):
    return db.execute(sql).rows


def job_for(path, **changes) -> importer.Job:
    job = importer.Job(
        path=str(path),
        table="users",
        dialect=importer.sniff(path),
        mapping=importer.default_mapping(
            importer.header(path, importer.sniff(path)),
            ["id", "name", "score"],
        ),
        kinds={"id": "integer", "name": "text", "score": "integer"},
    )
    for key, value in changes.items():
        setattr(job, key, value)
    return job


def test_an_import_inserts_every_row(db, tmp_path):
    path = write(
        tmp_path, "users.csv", "id,name,score\n1,ada,10\n2,brian,20\n"
    )
    report = importer.run(db, job_for(path))
    assert report.inserted == 2
    assert rows_of(db) == [(1, "ada", 10), (2, "brian", 20)]


def test_values_are_bound_not_interpolated(db, tmp_path):
    # A name that would end the statement if it were pasted into it.
    path = write(
        tmp_path,
        "users.csv",
        'id,name,score\n1,"O\'Hara\'); DROP TABLE users; --",5\n',
    )
    importer.run(db, job_for(path))
    assert rows_of(db, "SELECT name FROM users") == [
        ("O'Hara'); DROP TABLE users; --",)
    ]


def test_a_failure_leaves_the_table_exactly_as_it_was(db, tmp_path):
    db.execute("INSERT INTO users VALUES (1, 'ada', 10)")
    # Row 3 repeats the primary key of row 1.
    path = write(
        tmp_path,
        "users.csv",
        "id,name,score\n2,brian,20\n3,carol,30\n1,dana,40\n",
    )
    with pytest.raises(BatchError) as caught:
        importer.run(db, job_for(path, batch_size=1))
    assert caught.value.index == 2  # the third data row
    assert rows_of(db) == [(1, "ada", 10)]


def test_a_failure_inside_one_batch_still_names_the_row(db, tmp_path):
    db.execute("INSERT INTO users VALUES (1, 'ada', 10)")
    path = write(
        tmp_path,
        "users.csv",
        "id,name,score\n2,brian,20\n1,carol,30\n4,dana,40\n",
    )
    # One executemany for all three rows: the offending one is found by
    # replaying the batch, not by the driver's single error.
    with pytest.raises(BatchError) as caught:
        importer.run(db, job_for(path, batch_size=100))
    assert caught.value.index == 1
    assert rows_of(db) == [(1, "ada", 10)]


def test_a_row_error_stops_the_import_before_anything_lands(db, tmp_path):
    path = write(
        tmp_path, "users.csv", "id,name,score\n1,ada,10\n2,brian,lots\n"
    )
    with pytest.raises(importer.RowError) as caught:
        importer.run(db, job_for(path))
    assert caught.value.line == 3
    assert rows_of(db) == []


def test_replace_empties_the_table_in_the_same_transaction(db, tmp_path):
    db.execute("INSERT INTO users VALUES (9, 'old', 1)")
    path = write(tmp_path, "users.csv", "id,name,score\n1,ada,10\n")
    report = importer.run(db, job_for(path, mode="replace"))
    assert report.inserted == 1
    assert rows_of(db) == [(1, "ada", 10)]


def test_a_failed_replace_keeps_the_rows_it_was_going_to_delete(db, tmp_path):
    db.execute("INSERT INTO users VALUES (9, 'old', 1)")
    path = write(
        tmp_path, "users.csv", "id,name,score\n1,ada,10\n1,dup,20\n"
    )
    with pytest.raises(BatchError):
        importer.run(db, job_for(path, mode="replace", batch_size=1))
    assert rows_of(db) == [(9, "old", 1)]


def test_the_truncate_statement_is_the_dialects_own(db):
    statement = importer.truncate_statement(db, "users")
    # SQLite has no TRUNCATE; whatever the adapter says, sql_risk has
    # to see a destructive statement so the ladder can catch it.
    from sqlide.backend import sql_risk

    risk = sql_risk.classify(statement)
    assert risk.destructive
    assert risk.severe  # every row, one statement
    assert sql_risk.confirmation_level(risk, "production") == "type"


def test_a_cancelled_import_writes_nothing(db, tmp_path):
    path = write(
        tmp_path, "users.csv", "id,name,score\n1,ada,10\n2,brian,20\n"
    )
    with pytest.raises(BulkCancelled):
        importer.run(
            db, job_for(path, batch_size=1), cancelled=lambda: True
        )
    assert rows_of(db) == []


def test_unmapped_columns_are_left_out_of_the_statement(db, tmp_path):
    path = write(
        tmp_path, "users.csv", "id,name,score,note\n1,ada,10,ignored\n"
    )
    dialect = importer.sniff(path)
    job = importer.Job(
        path=str(path),
        table="users",
        dialect=dialect,
        mapping=importer.default_mapping(
            importer.header(path, dialect), ["id", "name", "score"]
        ),
        kinds=db.column_kinds("users"),
    )
    assert "note" not in importer.preview_statement(db, job)
    importer.run(db, job)
    assert rows_of(db) == [(1, "ada", 10)]


def test_column_kinds_come_from_the_declared_types(db):
    assert db.column_kinds("users") == {
        "id": "integer",
        "name": "text",
        "score": "integer",
    }


def test_an_unknown_column_never_reaches_the_sql(db, tmp_path):
    path = write(tmp_path, "users.csv", "id\n1\n")
    job = job_for(path)
    job.mapping = importer.Mapping((importer.ColumnMap(0, "nope"),))
    with pytest.raises(Exception) as caught:
        importer.run(db, job)
    assert "nope" in str(caught.value)
