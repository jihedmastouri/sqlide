"""Reading a CSV file into an existing table (CORE-37).

The other half of backend/export.py: that one turns rows into a file,
this one turns a file into rows and puts them in a table somebody
already has. No GTK and no driver knowledge — sniffing, mapping and
coercion are pure functions over text, which is why the dialog can
preview exactly what will be inserted before anything is sent.

Three ideas hold the module together:

- **Nothing is read twice and nothing is held.** `read_rows` is a
  generator over an open file, so a file larger than memory imports
  the same way a small one does. The preview takes a few rows off the
  front of the same generator and closes it.
- **A mapping is data.** `Mapping` says which source column feeds
  which target column, what text means NULL there, and which columns
  are ignored. It is a dataclass with no behaviour beyond building
  rows, so the dialog renders it and the tests assert on it.
- **A value that cannot be coerced is an error with a row number**,
  never a silent NULL. `coerce` is told the target column's *kind* —
  which the connector derives from its declared type, so no engine's
  type names are spelled here — and raises `RowError` naming the row,
  the column and the value it could not read.

Execution lives in `run`, which is a thin loop over
`Connector.insert_many`: one explicit transaction for the whole file,
executemany per batch, every value a bound parameter. A failure at row
N leaves the table exactly as it was.
"""

from __future__ import annotations

import csv
import io
import sys
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

# csv refuses fields larger than this by default; a single quoted cell
# holding a document is unusual but not an error, so the limit is
# raised once, here, rather than turning into a crash mid-file.
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

#: How many rows go to the server in one executemany by default.
DEFAULT_BATCH_SIZE = 500

#: Encodings offered by the dialog. "utf-8-sig" is what a file with a
#: byte-order mark needs; sniff() picks it on its own when it sees one.
ENCODINGS = ("utf-8", "utf-8-sig", "utf-16", "latin-1", "cp1252")

#: Text that means NULL when the user has not named one themselves.
#: An empty field is the ordinary case; the words are what
#: spreadsheets and dumps write.
DEFAULT_NULL_TOKENS = ("", "NULL", "\\N")

TRUE_WORDS = frozenset({"1", "t", "true", "y", "yes", "on"})
FALSE_WORDS = frozenset({"0", "f", "false", "n", "no", "off"})


class ImportFailed(Exception):
    """Base for everything this module refuses to do, in words.

    Spelled ImportFailed rather than ImportError so nothing here has
    to think about the builtin of that name.
    """


class RowError(ImportFailed):
    """One row of the file that could not become a row of the table.

    Carries where it was (`line`, the file's own 1-based line number,
    so it matches what an editor shows), which target column and the
    text that could not be read — an error message with no row number
    in it is an error message nobody can act on.
    """

    def __init__(
        self,
        line: int,
        reason: str,
        column: str = "",
        value: str = "",
    ) -> None:
        where = f"line {line}"
        if column:
            where += f", column {column}"
        detail = f"{where}: {reason}"
        if value:
            detail += f" ({value!r})"
        super().__init__(detail)
        self.line = line
        self.reason = reason
        self.column = column
        self.value = value


# Reading


@dataclass(frozen=True)
class Dialect:
    """How to read one file: the CSV knobs plus its encoding.

    Everything here is a guess `sniff()` made that the user can
    override in the dialog, which is why it is one small frozen bag
    rather than arguments threaded through every function.
    """

    delimiter: str = ","
    quotechar: str = '"'
    has_header: bool = True
    encoding: str = "utf-8"

    def reader_args(self) -> dict[str, str]:
        return {"delimiter": self.delimiter, "quotechar": self.quotechar}


#: The delimiters worth guessing between. Sniffer is told the list so
#: it cannot decide that a letter in the data is the separator.
CANDIDATE_DELIMITERS = ",;\t|"


def sniff_text(sample: str, encoding: str = "utf-8") -> Dialect:
    """A dialect guessed from the first lines of a file, as text.

    Pure, so it is the half the tests use. A sample csv cannot make
    sense of falls back to a comma with a header — the ordinary file —
    rather than raising: the user sees the guess in the preview and
    changes it if it is wrong.
    """
    if sample.startswith("﻿"):
        sample = sample[1:]
        encoding = "utf-8-sig"
    if not sample.strip():
        return Dialect(encoding=encoding)
    try:
        guess = csv.Sniffer().sniff(sample, delimiters=CANDIDATE_DELIMITERS)
        delimiter = guess.delimiter
        quotechar = guess.quotechar or '"'
    except csv.Error:
        delimiter, quotechar = ",", '"'
    try:
        header = csv.Sniffer().has_header(sample)
    except csv.Error:
        header = True
    return Dialect(
        delimiter=delimiter,
        quotechar=quotechar,
        has_header=header,
        encoding=encoding,
    )


def sniff(path: str | Path, encoding: str = "", sample_bytes: int = 65536):
    """A dialect guessed from the file at `path`.

    The byte-order mark decides the encoding when there is one (a BOM
    read as plain UTF-8 turns the first column name into garbage), and
    an explicit `encoding` always wins. A file that cannot be decoded
    is an ImportFailed naming the encoding, not a traceback.
    """
    head = _read_head(Path(path), sample_bytes)
    chosen = encoding or _encoding_of(head)
    sample = decode(head, chosen, whole=False)
    guessed = sniff_text(sample, chosen)
    return replace(guessed, encoding=chosen) if encoding else guessed


def _read_head(path: Path, size: int) -> bytes:
    try:
        with open(path, "rb") as handle:
            return handle.read(size)
    except OSError as exc:
        raise ImportFailed(
            f"Could not read {path}: {exc.strerror or exc}"
        ) from exc


def _encoding_of(head: bytes) -> str:
    if head.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    return "utf-8"


def decode(data: bytes, encoding: str, whole: bool = True) -> str:
    """`data` as text, or a sentence saying why it is not.

    A sample is decoded leniently at its tail (`whole=False`): the
    read almost certainly cut a multi-byte character in half, and that
    is not the file being broken.
    """
    try:
        return data.decode(encoding, "strict" if whole else "ignore")
    except LookupError:
        raise ImportFailed(f"Unknown encoding: {encoding}") from None
    except UnicodeDecodeError as exc:
        raise ImportFailed(
            f"This file is not {encoding} text (byte {exc.start}). "
            "Choose the encoding it was written in."
        ) from exc


def open_text(path: str | Path, dialect: Dialect):
    """The file open for csv: text mode, no newline translation.

    `newline=""` is not decoration — it is what lets a quoted field
    hold a line break of its own, and what keeps CRLF files from
    growing a stray carriage return at the end of every last column.
    """
    try:
        return open(
            path, "r", encoding=dialect.encoding, newline="", errors="strict"
        )
    except LookupError:
        raise ImportFailed(f"Unknown encoding: {dialect.encoding}") from None
    except OSError as exc:
        raise ImportFailed(
            f"Could not read {path}: {exc.strerror or exc}"
        ) from exc


def read_rows(handle, dialect: Dialect) -> Iterator[tuple[int, list[str]]]:
    """(line number, fields) for every row of an open file.

    The line number is the reader's own, so it counts a quoted field's
    embedded newlines the way the file does and an error can be looked
    up in an editor. The header row, when the dialect says there is
    one, is consumed here and reported by `header()` instead.
    """
    reader = csv.reader(handle, **dialect.reader_args())
    try:
        if dialect.has_header:
            next(reader, None)
        for fields in reader:
            yield reader.line_num, fields
    except csv.Error as exc:
        raise ImportFailed(f"This file is not readable as CSV: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ImportFailed(
            f"This file is not {dialect.encoding} text. "
            "Choose the encoding it was written in."
        ) from exc


def header(path: str | Path, dialect: Dialect) -> list[str]:
    """The source column names: the header row, or "Column 1", "Column
    2"… for a file that has none, so a mapping can still be built."""
    with open_text(path, dialect) as handle:
        reader = csv.reader(handle, **dialect.reader_args())
        first = next(reader, [])
    if dialect.has_header:
        return [name.strip() for name in first]
    return [f"Column {index + 1}" for index in range(len(first))]


def preview(
    path: str | Path, dialect: Dialect, limit: int = 20
) -> list[list[str]]:
    """The first `limit` data rows as parsed — what the dialog shows so
    the delimiter and header guesses can be seen before anything runs."""
    rows: list[list[str]] = []
    with open_text(path, dialect) as handle:
        for _line, fields in read_rows(handle, dialect):
            rows.append(fields)
            if len(rows) >= limit:
                break
    return rows


# Mapping


@dataclass(frozen=True)
class ColumnMap:
    """One source column pointed at one target column.

    `skip` keeps a column in the list while leaving it out of the
    INSERT: a file's bookkeeping columns should be visible and
    ignored, not silently dropped. `null_token` is per column because
    only the person importing knows whether "NULL" is a missing value
    or somebody's surname.
    """

    source: int
    target: str = ""
    null_token: str = ""
    skip: bool = False

    @property
    def active(self) -> bool:
        return bool(self.target) and not self.skip


@dataclass(frozen=True)
class Mapping:
    """Every source column, in file order, with where it goes."""

    columns: tuple[ColumnMap, ...] = ()

    @property
    def active(self) -> tuple[ColumnMap, ...]:
        return tuple(c for c in self.columns if c.active)

    @property
    def targets(self) -> list[str]:
        return [c.target for c in self.active]

    def problem(self) -> str:
        """Why this mapping cannot be run, or "" when it can."""
        active = self.active
        if not active:
            return "Map at least one column onto a table column."
        seen: set[str] = set()
        for column in active:
            if column.target in seen:
                return f"Two source columns both map to {column.target}."
            seen.add(column.target)
        return ""


def default_mapping(
    source_names: Sequence[str],
    target_columns: Sequence[str],
    null_token: str = "",
) -> Mapping:
    """Source columns matched onto target columns by name, ignoring
    case and surrounding space — which is right for a file exported
    from the same table, and a starting point for everything else. A
    name with no match starts skipped rather than guessed at by
    position: a wrong column silently filled is worse than a blank
    one the user has to fill in.
    """
    by_name = {name.strip().casefold(): name for name in target_columns}
    columns = []
    for index, name in enumerate(source_names):
        target = by_name.get(name.strip().casefold(), "")
        columns.append(
            ColumnMap(
                source=index,
                target=target,
                null_token=null_token,
                skip=not target,
            )
        )
    return Mapping(tuple(columns))


# Coercion. The *kind* of a target column comes from the connector
# (Connector.value_kind), so the type names of any one engine are not
# spelled here.


def coerce(text: str, kind: str) -> Any:
    """`text` as a value of `kind`, or ValueError saying why not.

    Never guesses from the text: an integer column reads "007" as 7
    and refuses "seven", where a text column keeps both exactly as
    written. Whitespace is stripped for the numeric kinds only —
    trailing space in a text field is data.
    """
    if kind == "integer":
        stripped = text.strip()
        try:
            return int(stripped, 10)
        except ValueError:
            # "42.0" from a spreadsheet is an integer; "42.5" is not.
            number = float(stripped)
            if number.is_integer():
                return int(number)
            raise ValueError("not a whole number") from None
    if kind == "number":
        return float(text.strip())
    if kind == "boolean":
        word = text.strip().casefold()
        if word in TRUE_WORDS:
            return True
        if word in FALSE_WORDS:
            return False
        raise ValueError("not true or false")
    if kind == "binary":
        stripped = text.strip()
        if stripped[:2].lower() == "0x":
            stripped = stripped[2:]
        try:
            return bytes.fromhex(stripped)
        except ValueError:
            raise ValueError("not hexadecimal bytes") from None
    # "text" and everything an adapter has no better word for: the
    # driver and the server decide, which is what makes dates, JSON
    # and a dialect's own types work without being listed here.
    return text


def build_row(
    line: int,
    fields: Sequence[str],
    mapping: Mapping,
    kinds: dict[str, str] | None = None,
) -> tuple:
    """One file row as the tuple of bound parameters for its INSERT.

    A row with fewer fields than the mapping reaches is rejected by
    name — a missing trailing column is the commonest broken file, and
    silently inserting NULL there is how a column of empties gets into
    a table nobody notices for a month.
    """
    kinds = kinds or {}
    values = []
    for column in mapping.active:
        if column.source >= len(fields):
            raise RowError(
                line,
                f"row has {len(fields)} values, but column "
                f"{column.source + 1} is mapped onto {column.target}",
                column.target,
            )
        text = fields[column.source]
        if text == column.null_token:
            values.append(None)
            continue
        try:
            values.append(coerce(text, kinds.get(column.target, "text")))
        except ValueError as exc:
            raise RowError(line, str(exc), column.target, text) from exc
    return tuple(values)


def is_blank(fields: Sequence[str]) -> bool:
    """A row with nothing in it — the trailing newline of a file, or a
    spacer line. Skipped and counted rather than being an error."""
    return all(not text.strip() for text in fields)


def build_rows(
    rows: Iterable[tuple[int, list[str]]],
    mapping: Mapping,
    kinds: dict[str, str] | None = None,
    *,
    on_skip: Callable[[int], None] | None = None,
) -> Iterator[tuple]:
    """Every file row as bound parameters, lazily. Blank rows are
    skipped (and counted through `on_skip`); anything else that cannot
    be read raises RowError, which stops the import."""
    for line, fields in rows:
        if is_blank(fields):
            if on_skip is not None:
                on_skip(line)
            continue
        yield build_row(line, fields, mapping, kinds)


# Running it


@dataclass
class Report:
    """What an import did, for the dialog to put on screen."""

    inserted: int = 0
    skipped: int = 0
    #: The first error, and the line it was on. An import stops at the
    #: first one — the transaction is gone, so there is no second.
    error: str = ""
    error_line: int = 0
    truncated: bool = False

    def describe(self) -> str:
        parts = [f"{self.inserted} rows inserted"]
        if self.skipped:
            parts.append(f"{self.skipped} blank rows skipped")
        if self.error:
            parts.append(self.error)
        return ", ".join(parts)


@dataclass
class Job:
    """Everything one import run needs, in one bag the dialog fills.

    A dataclass rather than eight arguments because the dialog builds
    it once and the tests build it in three lines.
    """

    path: str = ""
    table: str = ""
    dialect: Dialect = field(default_factory=Dialect)
    mapping: Mapping = field(default_factory=Mapping)
    kinds: dict[str, str] = field(default_factory=dict)
    #: "append" or "replace" — replace empties the table first, behind
    #: the confirmation ladder (see truncate_statement).
    mode: str = "append"
    batch_size: int = DEFAULT_BATCH_SIZE


MODES = ("append", "replace")


def truncate_statement(connector, table: str) -> str:
    """The statement that empties `table` before a replace, spelled by
    the adapter — the dialect question of whether that is TRUNCATE or
    DELETE belongs to the connector, and the statement is shown to the
    user through backend/sql_risk.py before it runs."""
    return connector.truncate_sql(table)


def run(
    connector,
    job: Job,
    *,
    on_progress: Callable[[int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> Report:
    """Import the file `job` describes into its table, all or nothing.

    The whole file is one transaction: the rows go out in batches of
    `job.batch_size` through the driver's executemany, every value
    bound, and the first failure rolls back everything — including the
    truncate of a replace, which is why that statement runs inside the
    same transaction rather than before it.
    """
    problem = job.mapping.problem()
    if problem:
        raise ImportFailed(problem)
    if job.mode not in MODES:
        raise ImportFailed(f"Unknown import mode: {job.mode}")
    report = Report()

    def skipped(_line: int) -> None:
        report.skipped += 1

    handle = open_text(job.path, job.dialect)
    try:
        rows = build_rows(
            read_rows(handle, job.dialect),
            job.mapping,
            job.kinds,
            on_skip=skipped,
        )
        report.inserted = connector.insert_many(
            job.table,
            job.mapping.targets,
            rows,
            batch_size=job.batch_size,
            before=(
                truncate_statement(connector, job.table)
                if job.mode == "replace"
                else ""
            ),
            on_progress=on_progress,
            cancelled=cancelled,
        )
        report.truncated = job.mode == "replace"
    finally:
        handle.close()
    return report


def preview_statement(connector, job: Job) -> str:
    """The shape of the statement that will run — placeholders, not
    values, because no row content is ever interpolated into SQL."""
    targets = job.mapping.targets
    if not targets:
        return ""
    columns = ", ".join(connector.quote_ident(name) for name in targets)
    marks = ", ".join([connector.placeholder] * len(targets))
    return (
        f"INSERT INTO {connector.quote_ident(job.table)} ({columns})\n"
        f"VALUES ({marks})"
    )


def preview_values(
    job: Job, rows: Iterable[tuple[int, list[str]]], limit: int = 10
) -> tuple[list[list[str]], str]:
    """The first rows as they will be bound, rendered for the dialog,
    plus the first row error found (empty when there is none).

    Errors are reported rather than raised here: a preview whose job
    is to show what is wrong must not stop at the first thing that
    is."""
    out: list[list[str]] = []
    for line, fields in rows:
        if is_blank(fields):
            continue
        try:
            values = build_row(line, fields, job.mapping, job.kinds)
        except RowError as exc:
            return out, str(exc)
        out.append(["NULL" if v is None else str(v) for v in values])
        if len(out) >= limit:
            break
    return out, ""


def sample_text(path: str | Path, dialect: Dialect, limit: int = 20) -> str:
    """The first parsed rows as a small table of text — the preview
    grid, rendered without a widget so it can be tested."""
    rows = preview(path, dialect, limit)
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter="\t", lineterminator="\n")
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()
