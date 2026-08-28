"""Writing result rows to a file (CORE-36).

The one implementation of every row format in sqlide. The grid's
"Copy As" and the export dialog both come here, so what lands on the
clipboard and what lands in a file can never drift apart.

No GTK and no driver: a format is a pure function of columns, rows and
options, and a row source is any iterable of sequences — a page of a
grid, a selection, or a generator walking a table one page at a time.
That last one is why every writer is incremental (`iter_chunks` yields
text as it goes, `write_rows` pushes it straight at a stream): a table
larger than memory has to be exportable, so no writer may hold the
whole result.

`export_to_path` is the file half: it writes through a temporary file
beside the destination and renames it into place at the end, so a
cancelled or failed export leaves no half-written file where the user
asked for a whole one, and it turns the OS's errors into sentences.
"""

from __future__ import annotations

import csv
import io
import json
import os
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

Row = Sequence[Any]
Rows = Iterable[Row]


class Format(Enum):
    """A row format, with the file extension it belongs in."""

    CSV = "csv"
    JSON = "json"
    INSERT = "insert"
    MARKDOWN = "markdown"
    # Clipboard-only shapes: neither is a file format anyone asks for,
    # but both are formats, so they live with the others.
    PRETTY = "pretty"
    TSV = "default"

    @property
    def suffix(self) -> str:
        return {
            Format.CSV: ".csv",
            Format.JSON: ".json",
            Format.INSERT: ".sql",
            Format.MARKDOWN: ".md",
            Format.PRETTY: ".txt",
            Format.TSV: ".tsv",
        }[self]


@dataclass(frozen=True)
class Options:
    """Everything the writers can be told, in one bag.

    The CSV knobs are the ones that decide whether another program can
    read the file at all — delimiter, quoting, header row, what a NULL
    looks like — plus the encoding, which is recorded here rather than
    assumed so nothing is silently lossy.
    """

    delimiter: str = ","
    quote_all: bool = False
    header: bool = True
    null_text: str = ""
    encoding: str = "utf-8"
    # Table named by the INSERT format; the grid passes the open table.
    table_name: str = "table_name"


DEFAULTS = Options()

# Binary columns (BLOB, bytea, MySQL binary collations) arrive as
# bytes/memoryview. str() on those gives a Python repr — b'\x89PNG' —
# which is neither readable nor valid SQL, so every rendering path goes
# through these helpers instead.
_BINARY_TYPES = (bytes, bytearray, memoryview)


def is_binary(value: Any) -> bool:
    return isinstance(value, _BINARY_TYPES)


def hex_text(value: Any) -> str:
    return bytes(value).hex().upper()


def cell_text(value: Any, null_text: str = "NULL") -> str:
    """A cell's value as text for the row formats. Binary keeps its
    full hex: an export is not a preview, so it must not lose bytes."""
    if value is None:
        return null_text
    return "0x" + hex_text(value) if is_binary(value) else str(value)


def sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    if is_binary(value):
        # X'..' is SQLite's and MySQL's blob literal; PostgreSQL reads
        # it as a bit string, so a bytea column needs the pasted
        # literal adjusted to '\x..'::bytea by hand.
        return "X'" + hex_text(value) + "'"
    return "'" + str(value).replace("'", "''") + "'"


def _json_safe(value: Any) -> bool:
    return value is None or isinstance(value, (bool, int, float, str))


def _json_value(value: Any) -> Any:
    if _json_safe(value):
        return value
    return "0x" + hex_text(value) if is_binary(value) else str(value)


# Writers. Each is a generator over text chunks so a caller can stream
# a table it could never hold.


def _csv_chunks(
    columns: Sequence[str], rows: Rows, opts: Options
) -> Iterator[str]:
    buffer = io.StringIO()
    writer = csv.writer(
        buffer,
        delimiter=opts.delimiter,
        quoting=csv.QUOTE_ALL if opts.quote_all else csv.QUOTE_MINIMAL,
        lineterminator="\n",
    )

    def flush() -> str:
        text = buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)
        return text

    if opts.header:
        writer.writerow(columns)
        yield flush()
    for row in rows:
        writer.writerow(
            opts.null_text
            if v is None
            else ("0x" + hex_text(v) if is_binary(v) else v)
            for v in row
        )
        yield flush()


def _json_chunks(
    columns: Sequence[str], rows: Rows, opts: Options
) -> Iterator[str]:
    yield "[\n"
    first = True
    for row in rows:
        text = json.dumps(
            {h: _json_value(v) for h, v in zip(columns, row)},
            indent=2,
            ensure_ascii=False,
        )
        indented = "\n".join("  " + line for line in text.splitlines())
        yield ("" if first else ",\n") + indented
        first = False
    yield "\n]" if not first else "]"


def _insert_chunks(
    columns: Sequence[str], rows: Rows, opts: Options
) -> Iterator[str]:
    table = opts.table_name or "table_name"
    names = ", ".join(columns)
    for row in rows:
        values = ", ".join(sql_literal(v) for v in row)
        yield f"INSERT INTO {table} ({names}) VALUES ({values});\n"


def _markdown_line(values: Iterable[str]) -> str:
    return "| " + " | ".join(v.replace("|", "\\|") for v in values) + " |\n"


def _markdown_chunks(
    columns: Sequence[str], rows: Rows, opts: Options
) -> Iterator[str]:
    yield _markdown_line(columns)
    yield "| " + " | ".join("---" for _ in columns) + " |\n"
    for row in rows:
        yield _markdown_line(cell_text(v) for v in row)


def _tsv_chunks(
    columns: Sequence[str], rows: Rows, opts: Options
) -> Iterator[str]:
    if opts.header:
        yield "\t".join(columns) + "\n"
    for row in rows:
        yield "\t".join(cell_text(v) for v in row) + "\n"


def _pretty_chunks(
    columns: Sequence[str], rows: Rows, opts: Options
) -> Iterator[str]:
    """An ASCII table. The one format that cannot stream — a column's
    width is not known until the last row — so it materialises, and is
    offered for a selection on the clipboard rather than for a table."""
    cells = [list(columns)] + [[cell_text(v) for v in row] for row in rows]
    widths = [max(len(line[i]) for line in cells) for i in range(len(columns))]
    rule = "+" + "+".join("-" * (w + 2) for w in widths) + "+\n"

    def line(values: list[str]) -> str:
        body = " | ".join(v.ljust(w) for v, w in zip(values, widths))
        return "| " + body + " |\n"

    yield rule
    yield line(cells[0])
    yield rule
    for values in cells[1:]:
        yield line(values)
    yield rule


_WRITERS: dict[Format, Callable[..., Iterator[str]]] = {
    Format.CSV: _csv_chunks,
    Format.JSON: _json_chunks,
    Format.INSERT: _insert_chunks,
    Format.MARKDOWN: _markdown_chunks,
    Format.PRETTY: _pretty_chunks,
    Format.TSV: _tsv_chunks,
}


def iter_chunks(
    fmt: Format,
    columns: Sequence[str],
    rows: Rows,
    options: Options | None = None,
) -> Iterator[str]:
    """`rows` formatted as `fmt`, a piece of text at a time."""
    return _WRITERS[fmt](columns, rows, options or DEFAULTS)


def format_rows(
    fmt: Format,
    columns: Sequence[str],
    rows: Rows,
    options: Options | None = None,
) -> str:
    """The whole thing as one string — what the clipboard wants, and
    what the export dialog's preview shows."""
    return "".join(iter_chunks(fmt, columns, rows, options)).rstrip("\n")


def write_rows(
    sink,
    fmt: Format,
    columns: Sequence[str],
    rows: Rows,
    options: Options | None = None,
    *,
    on_row: Callable[[int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> int:
    """Stream `rows` into an open text `sink`; return the rows written.

    `on_row` is called with the running count so a UI can show
    progress, and `cancelled` is polled between rows so a long export
    can be stopped without waiting for the table to end.
    """
    written = 0

    def counted() -> Iterator[Row]:
        nonlocal written
        for row in rows:
            if cancelled is not None and cancelled():
                raise ExportCancelled()
            yield row
            written += 1
            if on_row is not None:
                on_row(written)

    for chunk in iter_chunks(fmt, columns, counted(), options):
        sink.write(chunk)
    return written


class ExportError(Exception):
    """A destination that could not be written, in words."""


class ExportCancelled(Exception):
    """The user stopped the export; nothing is left behind."""


def export_to_path(
    path: str | os.PathLike[str],
    fmt: Format,
    columns: Sequence[str],
    rows: Rows,
    options: Options | None = None,
    *,
    on_row: Callable[[int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> int:
    """Write `rows` to `path`, all of it or none of it.

    The bytes go to a temporary file beside the destination and are
    renamed over it only once the last row is written, so a cancel, a
    dead connection or a full disk leaves the destination as it was
    rather than as a truncated file that looks like an export.
    """
    opts = options or DEFAULTS
    target = Path(path)
    try:
        temp = target.with_name(target.name + ".part")
        handle = open(
            temp, "w", encoding=opts.encoding, newline="", errors="strict"
        )
    except LookupError:
        raise ExportError(f"Unknown encoding: {opts.encoding}") from None
    except OSError as exc:
        raise ExportError(_reason(target, exc)) from exc
    try:
        with handle:
            count = write_rows(
                handle,
                fmt,
                columns,
                rows,
                opts,
                on_row=on_row,
                cancelled=cancelled,
            )
        os.replace(temp, target)
    except ExportCancelled:
        _discard(temp)
        raise
    except UnicodeEncodeError as exc:
        _discard(temp)
        raise ExportError(
            f"{opts.encoding} cannot hold every character in these rows "
            f"({exc.reason}). Export as UTF-8 instead."
        ) from exc
    except OSError as exc:
        _discard(temp)
        raise ExportError(_reason(target, exc)) from exc
    except Exception:
        _discard(temp)
        raise
    return count


def _discard(temp: Path) -> None:
    try:
        temp.unlink()
    except OSError:
        pass


def _reason(target: Path, exc: OSError) -> str:
    detail = exc.strerror or str(exc)
    return f"Could not write {target}: {detail}"


# Row sources


def iter_pages(
    connector,
    table: str,
    *,
    filters=None,
    order_by=None,
    page_size: int = 500,
    cancelled: Callable[[], bool] | None = None,
) -> Iterator[Row]:
    """Every row of `table`, one page at a time.

    The generator holds a single page: the rows of page N are yielded
    and dropped before page N+1 is asked for, which is what makes
    "export the whole table" independent of how big the table is. The
    connector's own cursor is carried forward when it offers one, so
    the pages follow the same total order the grid pages in (CORE-40)
    and the export honours the tab's filters and sort.
    """
    offset = 0
    cursor = None
    while True:
        if cancelled is not None and cancelled():
            raise ExportCancelled()
        result = connector.fetch_rows(
            table,
            offset,
            page_size,
            filters=filters,
            order_by=order_by,
            cursor=cursor,
        )
        rows = result.rows
        for row in rows:
            yield row
        if len(rows) < page_size:
            return
        offset += len(rows)
        cursor = result.cursor
