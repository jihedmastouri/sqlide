"""Exporting rows to a file (CORE-36).

No server and no GTK: backend/export.py is a pure function of columns,
rows and options, which is what makes the formats testable at all. The
fixture is deliberately nasty — NULLs, the delimiter inside a value,
quotes, a newline, non-ASCII text and a blob — because those are the
values a naive formatter loses.
"""

from __future__ import annotations

import csv
import io
import json
import re

import pytest

from sqlide.backend import export
from sqlide.backend.export import (
    ExportCancelled,
    ExportError,
    Format,
    Options,
)

COLUMNS = ["id", "name", "note", "blob"]
ROWS = [
    [1, "ada", None, b"\x00\xff"],
    [2, "a,b", 'say "hi"', b""],
    [3, "line\nbreak", "Ünïcødé 🎉", None],
    [4, "o'brien", "tab\there", b"\xde\xad\xbe\xef"],
]


def written(tmp_path, fmt, rows=None, options=None):
    path = tmp_path / "out.txt"
    export.export_to_path(path, fmt, COLUMNS, rows or ROWS, options)
    encoding = (options or export.DEFAULTS).encoding
    return path.read_text(encoding=encoding)


# Round trips


def test_csv_round_trips_every_awkward_value(tmp_path):
    text = written(tmp_path, Format.CSV)
    back = list(csv.reader(io.StringIO(text)))
    assert back[0] == COLUMNS
    assert [r[1] for r in back[1:]] == [
        "ada", "a,b", "line\nbreak", "o'brien"
    ]
    assert back[1][2] == ""  # NULL
    assert back[2][2] == 'say "hi"'
    assert back[3][2] == "Ünïcødé 🎉"
    assert back[1][3] == "0x00FF"  # the blob keeps all its bytes
    assert back[4][3] == "0xDEADBEEF"


def test_csv_options_are_honoured(tmp_path):
    opts = Options(
        delimiter=";", header=False, null_text="\\N", quote_all=True
    )
    text = written(tmp_path, Format.CSV, options=opts)
    assert not text.startswith("id")
    assert text.startswith('"1";"ada";"\\N"')


def test_csv_encoding_is_the_one_that_was_asked_for(tmp_path):
    path = tmp_path / "latin.csv"
    export.export_to_path(
        path, Format.CSV, ["name"], [["café"]], Options(encoding="latin-1")
    )
    assert path.read_bytes().endswith(b"caf\xe9\n")


def test_an_encoding_that_cannot_hold_the_rows_says_so(tmp_path):
    path = tmp_path / "latin.csv"
    with pytest.raises(ExportError) as err:
        export.export_to_path(
            path, Format.CSV, ["name"], [["🎉"]], Options(encoding="latin-1")
        )
    assert "latin-1" in str(err.value)
    assert not path.exists()


def test_json_round_trips(tmp_path):
    back = json.loads(written(tmp_path, Format.JSON))
    assert [row["name"] for row in back] == [
        "ada", "a,b", "line\nbreak", "o'brien"
    ]
    assert back[0]["note"] is None
    assert back[0]["blob"] == "0x00FF"
    assert back[2]["note"] == "Ünïcødé 🎉"
    assert back[3]["blob"] == "0xDEADBEEF"


def test_insert_statements_quote_and_hex(tmp_path):
    text = written(tmp_path, Format.INSERT, options=Options(table_name="t"))
    # A value with a newline in it splits a line but not a statement.
    assert text.count("INSERT INTO t") == 4
    lines = text.strip().split(";\n")
    assert lines[0] + ";" == (
        "INSERT INTO t (id, name, note, blob) "
        "VALUES (1, 'ada', NULL, X'00FF');"
    )
    assert "'o''brien'" in text  # the quote is doubled, not dropped
    assert "X'DEADBEEF'" in text


def test_markdown_escapes_pipes_and_keeps_the_bytes(tmp_path):
    text = written(
        tmp_path, Format.MARKDOWN, rows=[[1, "a|b", None, b"\x01"]]
    )
    lines = text.strip().splitlines()
    assert lines[0].startswith("| id | name |")
    assert "a\\|b" in lines[2]
    assert "NULL" in lines[2]
    assert "0x01" in lines[2]


def test_the_grid_copy_uses_the_same_implementation():
    """The clipboard and the file cannot drift: one implementation."""
    from sqlide.frontend import data_grid

    assert data_grid._sql_literal is export.sql_literal
    assert data_grid._cell_text is export.cell_text
    assert data_grid._format_csv(COLUMNS, ROWS) == export.format_rows(
        Format.CSV, COLUMNS, ROWS
    )


# Streaming


class FakeResult:
    def __init__(self, rows):
        self.rows = rows
        self.cursor = None


class FakeConnector:
    """Counts its pages and refuses to hand out more than one at a
    time, so a caller that materialised the table would be caught."""

    def __init__(self, total, page_size):
        self.total = total
        self.page_size = page_size
        self.calls = 0
        self.seen_filters = None
        self.seen_order = None
        self.live: list[list] | None = None

    def fetch_rows(
        self, table, offset=0, limit=500, filters=None, order_by=None,
        cursor=None,
    ):
        self.calls += 1
        self.seen_filters = filters
        self.seen_order = order_by
        rows = [
            [i, f"name{i}"] for i in range(offset, min(offset + limit, self.total))
        ]
        self.live = rows
        return FakeResult(rows)


def test_whole_query_export_never_holds_more_than_a_page(tmp_path):
    page = 10
    db = FakeConnector(total=95, page_size=page)
    path = tmp_path / "all.csv"
    count = export.export_to_path(
        path,
        Format.CSV,
        ["id", "name"],
        export.iter_pages(db, "users", page_size=page),
    )
    assert count == 95
    # Ten pages of ten (the last one short) and no eleventh: the
    # generator stops as soon as a page comes back short.
    assert db.calls == 10
    assert len(path.read_text().strip().splitlines()) == 96
    # Whatever the connector last handed over is one page, never 95.
    assert len(db.live) <= page


def test_streaming_export_passes_the_filters_and_sort(tmp_path):
    from sqlide.backend.db.base import FilterCondition, SortSpec

    db = FakeConnector(total=3, page_size=10)
    filters = [FilterCondition("name", "=", "ada")]
    order = [SortSpec("id", descending=True)]
    export.export_to_path(
        tmp_path / "f.csv",
        Format.CSV,
        ["id", "name"],
        export.iter_pages(
            db, "users", filters=filters, order_by=order, page_size=10
        ),
    )
    assert db.seen_filters == filters
    assert db.seen_order == order


def test_a_cancelled_export_leaves_nothing_behind(tmp_path):
    path = tmp_path / "half.csv"
    stop = {"now": False}

    def rows():
        for i in range(1000):
            if i == 5:
                stop["now"] = True
            yield [i, "x"]

    with pytest.raises(ExportCancelled):
        export.export_to_path(
            path,
            Format.CSV,
            ["id", "name"],
            rows(),
            cancelled=lambda: stop["now"],
        )
    assert not path.exists()
    assert not list(tmp_path.iterdir())  # not even the temporary


def test_progress_counts_rows(tmp_path):
    seen = []
    export.export_to_path(
        tmp_path / "p.csv",
        Format.CSV,
        COLUMNS,
        ROWS,
        on_row=seen.append,
    )
    assert seen == [1, 2, 3, 4]


# Failures


def test_an_unwritable_destination_reads_as_a_sentence(tmp_path):
    missing = tmp_path / "nope" / "out.csv"
    with pytest.raises(ExportError) as err:
        export.export_to_path(missing, Format.CSV, COLUMNS, ROWS)
    message = str(err.value)
    assert str(missing) in message
    assert not re.search(r"Errno|Traceback", message)


def test_a_directory_where_a_file_was_asked_for_is_an_error(tmp_path):
    target = tmp_path / "adir"
    target.mkdir()
    with pytest.raises(ExportError):
        export.export_to_path(target, Format.CSV, COLUMNS, ROWS)


def test_an_existing_file_survives_a_failed_export(tmp_path):
    path = tmp_path / "keep.csv"
    path.write_text("the old export\n")

    def rows():
        yield [1, "ok", None, None]
        raise RuntimeError("connection dropped")

    with pytest.raises(RuntimeError):
        export.export_to_path(path, Format.CSV, COLUMNS, rows())
    assert path.read_text() == "the old export\n"
