"""Find a value across the tables of a database (CORE-45).

The question is "which table has this order id in it", and the honest
answer costs one SELECT per candidate table. What decides whether that
is a useful feature or a way to flatten a server is *which* columns
each of those statements touches, so that decision lives here, on its
own, over plain data:

* `plan()` takes the catalog — tables and their columns, as the
  metadata provider already hands them over — plus the term and the
  options, and returns one bounded parameterised statement per table,
  along with a reason for every table it left out.
* `scan()` runs a plan against a callable that executes one statement,
  reporting progress per table, stopping when asked to, and turning a
  table the account cannot read into a skipped row with the server's
  reason on it rather than a failed search.

Neither function knows an engine. Quoting comes in as the connector's
`quote_ident`, the parameter marker as its `placeholder`, and the
column types are read the way the rest of the app reads them: by their
declared names, matched longest needle first (`column_kind`).

The column selection is the point. A search that casts every column to
text matches everything and scans everything; this one is type-driven
and conservative:

* text columns take the term as a substring (or whole, in exact mode);
* numeric, boolean and date/time columns are searched only when the
  term parses as one — `4812` looks at integer columns, `ada` does not;
* binary, geometry and any type this module does not recognise are
  never searched, because a match there is not something a person
  typed into a search box.

Every statement is parameter-bound (no value is ever formatted into
SQL), names every identifier from the catalog it was given, and carries
its own row cap; the plan carries an overall hit cap on top of that.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from sqlide.backend.db.base import (
    ColumnInfo,
    ConnectorError,
    FilterCondition,
    inline_params,
)

#: The kinds a declared type can fall into, and what each one means for
#: a search. Anything not listed here is "other" and is never searched.
SEARCHABLE_KINDS = (
    "text", "integer", "number", "boolean", "date", "time", "timestamp",
)
SKIPPED_KINDS = ("binary", "geometry", "other")

#: Declared type names, lowercased, mapped to a kind. Matched as
#: substrings against the declared type, longest needle first, so
#: "timestamp with time zone" lands on timestamp rather than time and
#: "varbinary" on binary rather than nothing.
TYPE_KINDS: tuple[tuple[str, str], ...] = (
    ("geometry", "geometry"),
    ("geography", "geometry"),
    ("point", "geometry"),
    ("polygon", "geometry"),
    ("linestring", "geometry"),
    ("raster", "geometry"),
    ("blob", "binary"),
    ("bytea", "binary"),
    ("binary", "binary"),
    ("image", "binary"),
    ("bool", "boolean"),
    ("bit", "boolean"),
    ("timestamp", "timestamp"),
    ("datetime", "timestamp"),
    ("date", "date"),
    ("time", "time"),
    ("serial", "integer"),
    ("int", "integer"),
    ("decimal", "number"),
    ("numeric", "number"),
    ("money", "number"),
    ("real", "number"),
    ("double", "number"),
    ("float", "number"),
    ("char", "text"),
    ("text", "text"),
    ("string", "text"),
    ("clob", "text"),
    ("name", "text"),
    ("uuid", "text"),
    ("json", "text"),
    ("xml", "text"),
    ("enum", "text"),
)

_BY_LENGTH = tuple(sorted(TYPE_KINDS, key=lambda pair: -len(pair[0])))

_INTEGER = re.compile(r"^[+-]?\d+$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME = re.compile(r"^\d{1,2}:\d{2}(:\d{2}(\.\d+)?)?$")
_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}[ T]\d{1,2}:\d{2}(:\d{2}(\.\d+)?)?$"
)
_TRUE = ("true", "t", "yes", "y", "1")
_FALSE = ("false", "f", "no", "n", "0")

#: What a LIKE pattern has to escape, and with what. Spelled out in the
#: statement (ESCAPE '\') because the engines disagree about whether
#: there is a default escape character at all.
LIKE_ESCAPE = "\\"


class SearchError(ConnectorError):
    """A plan that cannot be built: an empty term, a table asked for
    that the catalog handed over does not contain, a bad cap."""


def column_kind(type_name: str) -> str:
    """Which kind of value a column of this declared type holds, for
    the purpose of deciding whether a term could be in it.

    Unrecognised is "other", and "other" is never searched — the
    conservative direction. `Connector.value_kind` answers a narrower
    question (what an import may coerce to) and deliberately calls
    everything it does not know "text"; that default is wrong here,
    where it would put every dialect's own type into every scan.
    """
    name = (type_name or "").strip().lower()
    for needle, kind in _BY_LENGTH:
        if needle in name:
            return kind
    return "other"


@dataclass(frozen=True)
class SearchTable:
    """One candidate relation, as the catalog describes it."""

    name: str
    columns: tuple[ColumnInfo, ...] = ()
    schema: str = ""
    kind: str = "table"  # "table" | "view"
    system: bool = False  # a schema the server owns (PG-03)

    @property
    def label(self) -> str:
        return f"{self.schema}.{self.name}" if self.schema else self.name

    @property
    def key_columns(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns if c.is_pk)


@dataclass(frozen=True)
class SearchOptions:
    """The small set of choices the tab offers.

    `max_rows` caps each table's statement, `max_hits` the whole scan;
    both are hard, and both are why this feature cannot turn into an
    unbounded export of somebody's database.
    """

    exact: bool = False  # whole value rather than substring
    case_sensitive: bool = False
    schemas: tuple[str, ...] = ()  # empty means every non-system schema
    tables: tuple[str, ...] = ()  # empty means every table
    include_views: bool = False
    include_system: bool = False
    max_rows: int = 100
    max_hits: int = 1000


@dataclass(frozen=True)
class TableQuery:
    """One table's statement, ready to bind."""

    table: str
    schema: str
    sql: str
    params: tuple[Any, ...]
    #: The columns this statement tests, in the order it tests them —
    #: what a hit's "which column matched" is decided against.
    columns: tuple[str, ...]
    #: The term the statement was built for, kept so a returned row can
    #: be re-tested column by column without re-deriving it from the
    #: bound parameters.
    term: str = ""
    #: The primary key columns selected alongside, so a hit can be
    #: reopened as a filter on the row rather than on the value.
    key_columns: tuple[str, ...] = ()
    #: Everything the SELECT list names: keys first, then matches.
    selected: tuple[str, ...] = ()
    max_rows: int = 0

    @property
    def label(self) -> str:
        return f"{self.schema}.{self.table}" if self.schema else self.table

    #: The engine's parameter marker, for rendering `display`.
    marker: str = "?"

    @property
    def display(self) -> str:
        """The statement with its values written in, for showing the
        user what ran. Never sent to a server."""
        return inline_params(self.sql, list(self.params), self.marker)


@dataclass(frozen=True)
class SkippedTable:
    """A table the scan did not search, and why. Always reported: a
    table silently missing from the results reads as "the value is not
    in there", which is the one answer this must never fake."""

    table: str
    schema: str
    reason: str

    @property
    def label(self) -> str:
        return f"{self.schema}.{self.table}" if self.schema else self.table


@dataclass(frozen=True)
class SearchPlan:
    term: str
    options: SearchOptions
    queries: tuple[TableQuery, ...] = ()
    skipped: tuple[SkippedTable, ...] = ()

    @property
    def table_count(self) -> int:
        """How many tables this scan will actually read — the number
        the tab states before anything runs."""
        return len(self.queries)


@dataclass(frozen=True)
class Hit:
    """One matched value: which table, which column, what was in it."""

    table: str
    schema: str
    column: str
    value: Any
    row: dict[str, Any] = field(default_factory=dict)
    key_columns: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        return f"{self.schema}.{self.table}" if self.schema else self.table


@dataclass
class SearchReport:
    hits: list[Hit] = field(default_factory=list)
    skipped: list[SkippedTable] = field(default_factory=list)
    scanned: int = 0  # tables actually read
    truncated: bool = False  # stopped at max_hits
    cancelled: bool = False


def term_kinds(term: str) -> tuple[str, ...]:
    """The column kinds `term` could possibly be found in.

    Text always: any term is a string somewhere. The rest only when the
    term parses as that kind, which is what keeps `ada` out of every
    numeric column of the database and keeps the search from casting
    those columns to text to make it fit.
    """
    text = (term or "").strip()
    kinds = ["text"]
    if _INTEGER.match(text):
        kinds.append("integer")
    if _is_number(text):
        kinds.append("number")
    if text.lower() in _TRUE + _FALSE:
        kinds.append("boolean")
    if _TIMESTAMP.match(text):
        kinds.append("timestamp")
    elif _DATE.match(text):
        kinds.extend(("date", "timestamp"))
    elif _TIME.match(text):
        kinds.append("time")
    return tuple(kinds)


def _is_number(text: str) -> bool:
    try:
        float(text)
    except ValueError:
        return False
    return text.strip().lower() not in ("nan", "inf", "-inf", "+inf",
                                        "infinity", "-infinity")


def bound_value(term: str, kind: str) -> Any:
    """The parameter a column of `kind` is compared against — typed, so
    the server compares like with like instead of coercing a column."""
    text = term.strip()
    if kind == "integer":
        return int(text)
    if kind == "number":
        return float(text)
    if kind == "boolean":
        return text.lower() in _TRUE
    return text


def searchable_columns(
    table: SearchTable, term: str
) -> tuple[list[ColumnInfo], list[str]]:
    """The columns of `table` worth testing for `term`, and the names
    of the ones deliberately left out (for the "why" of a skip)."""
    kinds = set(term_kinds(term))
    wanted, ignored = [], []
    for column in table.columns:
        if column_kind(column.type) in kinds:
            wanted.append(column)
        else:
            ignored.append(column.name)
    return wanted, ignored


def like_pattern(term: str, exact: bool) -> str:
    """`term` as a LIKE pattern, its own wildcards escaped so a value
    containing `%` is searched for rather than matching everything."""
    escaped = (
        term.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
        .replace("%", LIKE_ESCAPE + "%")
        .replace("_", LIKE_ESCAPE + "_")
    )
    return escaped if exact else f"%{escaped}%"


def _condition(
    column: ColumnInfo,
    term: str,
    options: SearchOptions,
    quote: Callable[[str], str],
    marker: str,
) -> tuple[str, Any]:
    """One column's test, as SQL and the value bound into it."""
    kind = column_kind(column.type)
    name = quote(column.name)
    if kind != "text":
        # Typed comparison: the column stays a number, a boolean or a
        # date, and the parameter is converted to meet it.
        return f"{name} = {marker}", bound_value(term, kind)
    pattern = like_pattern(term, options.exact)
    if not options.case_sensitive:
        name = f"LOWER({name})"
        pattern = pattern.lower()
    if options.exact:
        return f"{name} = {marker}", pattern.replace(LIKE_ESCAPE, "")
    return f"{name} LIKE {marker} ESCAPE '{LIKE_ESCAPE}'", pattern


def default_quote(name: str) -> str:
    """SQL-standard quoting, for tests and for callers with no
    connector to hand. A real scan passes `connector.quote_ident`."""
    if not name:
        raise SearchError("Empty identifier")
    if "\x00" in name:
        raise SearchError("Identifier contains a NUL byte")
    return '"' + name.replace('"', '""') + '"'


def _wanted(table: SearchTable, options: SearchOptions) -> str:
    """Why `table` is not in the scan, or "" when it is. The order is
    cheapest-to-explain first, so the reported reason is the one a
    person would give."""
    if table.system and not options.include_system:
        return "system schema"
    if options.schemas and table.schema not in options.schemas:
        return "schema not selected"
    if options.tables and table.name not in options.tables:
        return "table not selected"
    if table.kind != "table" and not options.include_views:
        return f"{table.kind or 'relation'}, not a table"
    if not table.columns:
        return "no columns in the catalog"
    return ""


def plan(
    tables: Sequence[SearchTable],
    term: str,
    options: SearchOptions | None = None,
    *,
    quote: Callable[[str], str] = default_quote,
    placeholder: str = "?",
) -> SearchPlan:
    """One bounded statement per searchable table, plus a reason for
    every table left out.

    Pure: no connection, no side effect, one answer per input. The
    identifiers come from `tables` — the catalog the caller read — and
    go through `quote`, so nothing a user typed ever reaches the SQL
    text; the term is bound.
    """
    options = options or SearchOptions()
    text = (term or "").strip()
    if not text:
        raise SearchError("Nothing to search for")
    max_rows = int(options.max_rows)
    if max_rows < 1:
        raise SearchError("The per-table row cap must be at least 1")
    queries: list[TableQuery] = []
    skipped: list[SkippedTable] = []
    for table in tables:
        reason = _wanted(table, options)
        if reason:
            skipped.append(SkippedTable(table.name, table.schema, reason))
            continue
        columns, _ignored = searchable_columns(table, text)
        if not columns:
            skipped.append(
                SkippedTable(
                    table.name,
                    table.schema,
                    f"no column of a type {text!r} could be in",
                )
            )
            continue
        queries.append(
            _statement(table, columns, text, options, quote, placeholder,
                       max_rows)
        )
    return SearchPlan(text, options, tuple(queries), tuple(skipped))


def _statement(
    table: SearchTable,
    columns: list[ColumnInfo],
    term: str,
    options: SearchOptions,
    quote: Callable[[str], str],
    marker: str,
    max_rows: int,
) -> TableQuery:
    tests, params = [], []
    for column in columns:
        sql, value = _condition(column, term, options, quote, marker)
        tests.append(sql)
        params.append(value)
    keys = table.key_columns
    names = list(dict.fromkeys([*keys, *(c.name for c in columns)]))
    select = ", ".join(quote(name) for name in names)
    source = quote(table.name)
    if table.schema:
        source = f"{quote(table.schema)}.{source}"
    where = " OR ".join(tests)
    sql = (
        f"SELECT {select} FROM {source} WHERE {where} "
        f"LIMIT {max_rows}"  # an int this function validated, never a term
    )
    return TableQuery(
        table=table.name,
        schema=table.schema,
        sql=sql,
        params=tuple(params),
        columns=tuple(c.name for c in columns),
        key_columns=keys,
        selected=tuple(names),
        max_rows=max_rows,
        term=term,
        marker=marker,
    )


def matches(value: Any, term: str, options: SearchOptions) -> bool:
    """Whether `value` is what the search asked for.

    The statement says a row matched, not which of its columns did, and
    a second round trip to find out would double the cost of the scan.
    The same comparison is therefore made here over the values already
    fetched — the identical rule the SQL applies, so the column named
    in a hit is a column the server would have matched.
    """
    if value is None:
        return False
    text = str(value)
    needle = term.strip()
    if isinstance(value, bool):
        return needle.lower() in (_TRUE if value else _FALSE)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return float(needle) == float(value)
        except ValueError:
            return False
    if not options.case_sensitive:
        text, needle = text.lower(), needle.lower()
    return text == needle if options.exact else needle in text


def hits_in_row(
    query: TableQuery,
    row: dict[str, Any],
    options: SearchOptions,
) -> list[Hit]:
    """The hits one returned row carries — one per column that actually
    holds the term, in the order the statement tested them."""
    found = []
    for column in query.columns:
        if column in row and matches(row[column], query.term, options):
            found.append(
                Hit(
                    table=query.table,
                    schema=query.schema,
                    column=column,
                    value=row[column],
                    row=dict(row),
                    key_columns=query.key_columns,
                )
            )
    return found



def hit_filters(hit: Hit) -> list[FilterCondition]:
    """The filter that opens the hit's row in a table tab (CORE-43's
    "open filtered" path).

    The primary key where there is one and it came back whole — that
    selects the row itself. Otherwise the matched column equals the
    matched value, which selects at least the row and never pretends to
    a precision the table does not have.
    """
    keys = [k for k in hit.key_columns if hit.row.get(k) is not None]
    if keys and len(keys) == len(hit.key_columns):
        return [
            FilterCondition(key, "=", str(hit.row[key])) for key in keys
        ]
    if hit.value is None:
        return []
    return [FilterCondition(hit.column, "=", str(hit.value))]


def scan(
    search_plan: SearchPlan,
    execute: Callable[[TableQuery], tuple[Sequence[str], Iterable[Sequence]]],
    *,
    on_progress: Callable[[int, TableQuery], None] | None = None,
    on_hit: Callable[[Hit], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> SearchReport:
    """Run a plan, one table at a time.

    `execute` runs a single statement and hands back `(columns, rows)`;
    anything it raises is that table's reported reason rather than the
    end of the search — a scan across a schema will meet a table this
    account cannot read, and that is a line in the results, not a
    failure. Cancellation is checked between tables, so stopping costs
    at most the statement already in flight.
    """
    report = SearchReport(skipped=list(search_plan.skipped))
    options = search_plan.options
    for index, query in enumerate(search_plan.queries):
        if should_cancel is not None and should_cancel():
            report.cancelled = True
            return report
        if on_progress is not None:
            on_progress(index, query)
        try:
            columns, rows = execute(query)
        except Exception as exc:  # a per-table answer, not a failed scan
            report.skipped.append(
                SkippedTable(query.table, query.schema, _reason(exc))
            )
            continue
        report.scanned += 1
        names = list(columns)
        for values in rows:
            row = dict(zip(names, values))
            for hit in hits_in_row(query, row, options):
                report.hits.append(hit)
                if on_hit is not None:
                    on_hit(hit)
                if len(report.hits) >= options.max_hits:
                    report.truncated = True
                    return report
    return report


def _reason(exc: Exception) -> str:
    text = str(exc).strip().splitlines()
    return text[0] if text else exc.__class__.__name__


def statements(search_plan: SearchPlan) -> list[str]:
    """Every statement of a plan with its values inlined — what the tab
    shows when somebody asks what it is about to run."""
    return [query.display for query in search_plan.queries]


def summary(search_plan: SearchPlan) -> str:
    """The one-line declaration the tab makes before it starts."""
    count = search_plan.table_count
    tables = "table" if count == 1 else "tables"
    return (
        f"{count} {tables} will be scanned, "
        f"at most {search_plan.options.max_rows} rows from each"
    )
