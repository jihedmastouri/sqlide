"""A serialisable SELECT model and the renderer that turns it into SQL.

The visual query builder used to write SQL straight out of its GTK
widgets, so there was no query to save, nothing to unit test, and no
way for anything but that one widget to produce the same statement.
This module is the other half of that split: the widget edits a
`QueryModel`, and the SQL is a pure function of it.

Three rules keep the model honest:

- **No frontend, no connection.** Nothing here imports from
  `sqlide.frontend` and nothing here talks to a server, so the whole
  renderer is testable without a database.
- **Identifiers are quoted, values are bound.** Every table, column
  and alias goes through the dialect's `quote` (which in practice is
  the connector's own `quote_ident`); every filter value becomes a
  placeholder and travels in the parameter list. The one deliberate
  exception is `Projection.expression` / `Order.expression`, which is
  raw SQL the caller vouches for — that is what an aggregate or a
  computed column needs (CORE-21), and it is never fed from a value.
- **What is shown is what runs.** `render_display` renders the *same*
  model and then writes the bound values in, so the read-only preview
  can never drift from the executed form. Display SQL is never sent to
  a server.

Dialect differences live in `Dialect`, never in a caller's `if engine
== ...`: a `Dialect` carries the quoting function, the driver's
parameter marker, and the capability flags the renderer needs (which
join kinds the engine has, whether it can express OFFSET without a
LIMIT). `dialect_for(connector)` builds one from any connector.

`to_dict` / `from_dict` round-trip the model through plain JSON-able
data, which is what lets a builder tab persist itself (CORE-19).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Sequence

from sqlide.backend.db.base import (
    CONJUNCTIONS,
    FILTER_OPERATORS,
    NO_VALUE_OPERATORS,
    ConnectorError,
    inline_params,
)
from sqlide.backend.sql_format import format_sql, options_from_settings

__all__ = [
    "Column",
    "Condition",
    "Dialect",
    "FilterGroup",
    "GENERIC",
    "JOIN_KINDS",
    "Join",
    "MYSQL",
    "On",
    "Order",
    "POSTGRES",
    "Projection",
    "QueryModel",
    "Rendered",
    "SQLITE",
    "TableRef",
    "dialect_for",
    "render",
    "render_display",
]


# Every join kind the model can express. A dialect that lacks one says
# so through Dialect.join_kinds rather than the renderer guessing.
JOIN_KINDS = (
    "INNER JOIN",
    "LEFT JOIN",
    "RIGHT JOIN",
    "FULL JOIN",
    "CROSS JOIN",
)
# The marker the renderer writes while it works; render() swaps in the
# driver's own at the end (see _substitute).
MARKER = "?"
# CROSS JOIN carries no ON clause; the rest require one.
JOINS_WITHOUT_ON = ("CROSS JOIN",)


# Model


@dataclass(frozen=True)
class TableRef:
    """One relation in the FROM/JOIN list.

    `alias` is what the rest of the model refers to it by, so the same
    table can appear twice (a self-join) and stay distinguishable —
    identity is the alias when there is one, the name otherwise.
    """

    name: str
    schema: str = ""
    alias: str = ""

    @property
    def key(self) -> str:
        """How columns of this source are qualified."""
        return self.alias or self.name


@dataclass(frozen=True)
class Column:
    """A column reference. `source` is a TableRef.key, or "" for a
    name the renderer leaves unqualified (a single-source query)."""

    name: str
    source: str = ""


@dataclass(frozen=True)
class Projection:
    """One item of the select list.

    Either a `column` or a raw `expression` (an aggregate, a
    computation) — never both. An empty projection list means `*`.
    """

    column: Column | None = None
    expression: str = ""
    alias: str = ""


@dataclass(frozen=True)
class On:
    """One equality (or other comparison) of a join's ON clause. All
    of a join's conditions are ANDed, which covers composite keys."""

    left: Column
    right: Column
    op: str = "="


@dataclass(frozen=True)
class Join:
    kind: str  # one of JOIN_KINDS
    source: TableRef
    on: tuple[On, ...] = ()


@dataclass(frozen=True)
class Condition:
    """A leaf of the filter tree: `column op value`.

    `op` is one of FILTER_OPERATORS; the value is ignored for the
    operators that take none (IS NULL / IS NOT NULL) and is otherwise
    bound as a parameter, never written into the text.
    """

    column: Column
    op: str = "="
    value: Any = None


@dataclass(frozen=True)
class FilterGroup:
    """`items` joined by one conjunction, parenthesised when nested.

    This is what makes `a AND (b OR c)` expressible: the old builder
    folded its lines strictly left-associatively and could not.
    """

    items: tuple["Condition | FilterGroup", ...] = ()
    conjunction: str = "AND"  # one of CONJUNCTIONS
    negated: bool = False


@dataclass(frozen=True)
class Order:
    column: Column | None = None
    expression: str = ""
    descending: bool = False


@dataclass(frozen=True)
class QueryModel:
    """A whole SELECT, as data.

    Frozen throughout, so a tab can hold one and hand it around
    without anyone editing it from under it; `replace()` from
    dataclasses is how you make an edited copy.
    """

    source: TableRef | None = None
    joins: tuple[Join, ...] = ()
    projections: tuple[Projection, ...] = ()
    distinct: bool = False
    where: FilterGroup | None = None
    group_by: tuple[Column | str, ...] = ()
    having: FilterGroup | None = None
    order_by: tuple[Order, ...] = ()
    limit: int | None = None
    offset: int | None = None

    @property
    def sources(self) -> tuple[TableRef, ...]:
        """Base table plus joined tables, in query order."""
        base = (self.source,) if self.source is not None else ()
        return (*base, *(j.source for j in self.joins))

    @property
    def qualified(self) -> bool:
        """Whether columns need a table prefix — true as soon as more
        than one source is in play."""
        return len(self.sources) > 1


# Dialects


@dataclass(frozen=True)
class Dialect:
    """Everything the renderer needs to know about one engine.

    Deliberately data, not subclasses: an adapter contributes a
    `Dialect` (see `dialect_for`) instead of the renderer growing a
    branch per engine.
    """

    name: str = "generic"
    quote: Callable[[str], str] = lambda n: '"' + n.replace('"', '""') + '"'
    placeholder: str = "?"
    #: Join kinds the engine actually has. MySQL 5.7 has no FULL JOIN;
    #: SQLite gained RIGHT and FULL only in 3.39, past our 3.25 floor.
    join_kinds: tuple[str, ...] = JOIN_KINDS
    #: Whether OFFSET can be written without a LIMIT. MySQL cannot, so
    #: the renderer supplies a very large LIMIT there instead.
    offset_without_limit: bool = True
    #: What that stand-in LIMIT is, where one is needed.
    offset_limit_stand_in: int = 18446744073709551615

    def quoted(self, name: str) -> str:
        if not name:
            raise ConnectorError("Empty identifier")
        if "\x00" in name:
            raise ConnectorError("Identifier contains a NUL byte")
        return self.quote(name)


def _ansi_quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _backtick_quote(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


GENERIC = Dialect()
POSTGRES = Dialect(name="postgres", quote=_ansi_quote, placeholder="%s")
MYSQL = Dialect(
    name="mysql",
    quote=_backtick_quote,
    placeholder="%s",
    # 5.7 is the floor and has no FULL JOIN.
    join_kinds=("INNER JOIN", "LEFT JOIN", "RIGHT JOIN", "CROSS JOIN"),
    offset_without_limit=False,
)
SQLITE = Dialect(
    name="sqlite",
    quote=_ansi_quote,
    placeholder="?",
    # RIGHT/FULL arrived in SQLite 3.39; the floor is 3.25.
    join_kinds=("INNER JOIN", "LEFT JOIN", "CROSS JOIN"),
)

_BY_NAME = {
    "postgres": POSTGRES,
    "postgresql": POSTGRES,
    "mysql": MYSQL,
    "mariadb": MYSQL,
    "sqlite": SQLITE,
    "sqlite3": SQLITE,
}


def dialect_for(connector: Any) -> Dialect:
    """The dialect for a live connector.

    Quoting and the parameter marker come from the connector itself —
    it is the authority on both — and the capability flags from the
    preset for its engine, falling back to the permissive generic set
    for an adapter we know nothing about (JDBC).
    """
    name = str(
        getattr(connector, "engine", "") or getattr(connector, "kind", "") or ""
    ).lower()
    if not name:
        name = type(connector).__name__.replace("Connector", "").lower()
    base = _BY_NAME.get(name, GENERIC)
    quote = getattr(connector, "quote_ident", None)
    placeholder = getattr(connector, "placeholder", base.placeholder)
    return replace(
        base,
        name=name or base.name,
        quote=quote if callable(quote) else base.quote,
        placeholder=placeholder,
    )


# Rendering


@dataclass(frozen=True)
class Rendered:
    """A rendered statement: the text, its parameters, and the same
    statement with the values written in for showing the user."""

    sql: str
    params: list[Any] = field(default_factory=list)


class _Renderer:
    def __init__(self, model: QueryModel, dialect: Dialect) -> None:
        self.model = model
        self.d = dialect
        self.params: list[Any] = []
        self.qualified = model.qualified

    # Pieces

    def ident(self, name: str) -> str:
        return self.d.quoted(name)

    def table(self, ref: TableRef) -> str:
        text = self.ident(ref.name)
        if ref.schema:
            text = f"{self.ident(ref.schema)}.{text}"
        if ref.alias:
            text += f" AS {self.ident(ref.alias)}"
        return text

    def column(self, col: Column, *, force_qualified: bool = False) -> str:
        if col.source and (self.qualified or force_qualified):
            return f"{self.ident(col.source)}.{self.ident(col.name)}"
        return self.ident(col.name)

    def select_list(self) -> str:
        if not self.model.projections:
            return "*"
        parts = []
        for proj in self.model.projections:
            if proj.expression:
                text = proj.expression
            elif proj.column is not None:
                text = self.column(proj.column)
            else:
                raise ConnectorError("Projection has neither column nor expression")
            if proj.alias:
                text += f" AS {self.ident(proj.alias)}"
            parts.append(text)
        return ", ".join(parts)

    def joins(self) -> list[str]:
        lines = []
        for join in self.model.joins:
            kind = join.kind.upper()
            if kind not in JOIN_KINDS:
                raise ConnectorError(f"Unsupported join kind: {join.kind}")
            if kind not in self.d.join_kinds:
                raise ConnectorError(
                    f"{self.d.name} does not support {kind}"
                )
            line = f"{kind} {self.table(join.source)}"
            if kind in JOINS_WITHOUT_ON:
                if join.on:
                    raise ConnectorError(f"{kind} takes no ON clause")
            else:
                if not join.on:
                    raise ConnectorError(f"{kind} needs an ON condition")
                # A join's ON is always qualified: an unqualified name
                # there is ambiguous by construction.
                conditions = " AND ".join(
                    f"{self.column(on.left, force_qualified=True)} {on.op} "
                    f"{self.column(on.right, force_qualified=True)}"
                    for on in join.on
                )
                line += f" ON {conditions}"
            lines.append(line)
        return lines

    def condition(self, cond: Condition) -> str:
        op = cond.op.upper()
        if op not in FILTER_OPERATORS:
            raise ConnectorError(f"Unsupported filter operator: {cond.op}")
        text = f"{self.column(cond.column)} {op}"
        if op not in NO_VALUE_OPERATORS:
            # Always "?" here, whatever the driver binds with: the
            # formatter reads "%s" as two tokens and would space it out
            # into "% s". The real marker goes in after formatting.
            text += f" {MARKER}"
            self.params.append(cond.value)
        return text

    def group(self, group: FilterGroup, *, top: bool = False) -> str:
        conj = group.conjunction.upper()
        if conj not in CONJUNCTIONS:
            raise ConnectorError(f"Unsupported conjunction: {group.conjunction}")
        parts = []
        for item in group.items:
            if isinstance(item, FilterGroup):
                rendered = self.group(item)
                if not rendered:
                    continue
                parts.append(rendered)
            else:
                parts.append(self.condition(item))
        if not parts:
            return ""
        text = f" {conj} ".join(parts)
        # A single unnegated item at the top needs no parentheses; a
        # nested group always gets them, so precedence is explicit and
        # never depends on the reader knowing AND binds tighter.
        if group.negated:
            return f"NOT ({text})"
        if top and len(parts) == 1:
            return text
        if top:
            return text
        if len(parts) == 1:
            return text
        return f"({text})"

    def order(self) -> str:
        parts = []
        for item in self.model.order_by:
            if item.expression:
                text = item.expression
            elif item.column is not None:
                text = self.column(item.column)
            else:
                continue
            parts.append(f"{text} {'DESC' if item.descending else 'ASC'}")
        return ", ".join(parts)

    def grouping(self) -> str:
        parts = []
        for item in self.model.group_by:
            parts.append(item if isinstance(item, str) else self.column(item))
        return ", ".join(parts)

    def build(self) -> tuple[str, list[Any]]:
        model = self.model
        if model.source is None or not model.source.name:
            return "", []
        lines = [
            "SELECT " + ("DISTINCT " if model.distinct else "") + self.select_list(),
            f"FROM {self.table(model.source)}",
        ]
        lines.extend(self.joins())
        if model.where is not None:
            where = self.group(model.where, top=True)
            if where:
                lines.append(f"WHERE {where}")
        grouping = self.grouping()
        if grouping:
            lines.append(f"GROUP BY {grouping}")
        if model.having is not None:
            having = self.group(model.having, top=True)
            if having:
                lines.append(f"HAVING {having}")
        order = self.order()
        if order:
            lines.append(f"ORDER BY {order}")
        limit, offset = model.limit, model.offset
        if limit is None and offset and not self.d.offset_without_limit:
            limit = self.d.offset_limit_stand_in
        if limit is not None:
            lines.append(f"LIMIT {int(limit)}")
        if offset:
            lines.append(f"OFFSET {int(offset)}")
        return "\n".join(lines) + ";", self.params


def _substitute(sql: str, marker: str, count: int) -> str:
    """Swap the first `count` "?" markers for the driver's own.

    Done after formatting, never before: the formatter lexes "%s" as a
    modulo and an alias and would respell it "% s".
    """
    if marker == MARKER or not count:
        return sql
    parts = sql.split(MARKER)
    out = parts[0]
    for index, rest in enumerate(parts[1:]):
        out += (marker if index < count else MARKER) + rest
    return out


def _rendered(
    model: QueryModel, dialect: Dialect, formatted: bool
) -> tuple[str, list[Any]]:
    """The statement with "?" markers still in it, plus its values."""
    sql, params = _Renderer(model, dialect).build()
    if sql and formatted:
        sql = format_sql(sql, options_from_settings()).text
    return sql, params


def render(
    model: QueryModel,
    *,
    quote: Callable[[str], str] | None = None,
    dialect: Dialect = GENERIC,
    formatted: bool = True,
) -> Rendered:
    """`model` as an executable statement plus its parameters.

    `quote` overrides the dialect's quoting — pass the connector's own
    `quote_ident` when you have one and no full `Dialect`. LIMIT and
    OFFSET are written in as integers (the model owns them, they are
    never user text); every filter value is a placeholder.

    An empty model — no source — renders to the empty string, which is
    what a builder with nothing picked yet should show.
    """
    if quote is not None:
        dialect = replace(dialect, quote=quote)
    sql, params = _rendered(model, dialect, formatted)
    return Rendered(
        sql=_substitute(sql, dialect.placeholder, len(params)), params=params
    )


def render_display(
    model: QueryModel,
    *,
    quote: Callable[[str], str] | None = None,
    dialect: Dialect = GENERIC,
    formatted: bool = True,
) -> str:
    """The same statement with its values written in, for the read-only
    preview and the history entry.

    Never send this to a server: the executed form is `render()`, whose
    values are bound. Rendering both from one model is what keeps the
    preview honest — it cannot describe a query other than the one that
    would run.
    """
    if quote is not None:
        dialect = replace(dialect, quote=quote)
    sql, params = _rendered(model, dialect, formatted)
    if not sql:
        return ""
    return inline_params(sql, params, MARKER)


# Serialisation (CORE-19 persists these dicts into TabState)


def _column_dict(col: Column | None) -> dict | None:
    return None if col is None else {"name": col.name, "source": col.source}


def _column_from(data: Any) -> Column | None:
    if not data:
        return None
    return Column(name=data.get("name", ""), source=data.get("source", ""))


def _filter_dict(node: "Condition | FilterGroup") -> dict:
    if isinstance(node, FilterGroup):
        return {
            "kind": "group",
            "conjunction": node.conjunction,
            "negated": node.negated,
            "items": [_filter_dict(i) for i in node.items],
        }
    return {
        "kind": "condition",
        "column": _column_dict(node.column),
        "op": node.op,
        "value": node.value,
    }


def _filter_from(data: Any) -> "Condition | FilterGroup | None":
    if not data:
        return None
    if data.get("kind") == "group":
        items = [_filter_from(i) for i in data.get("items", [])]
        return FilterGroup(
            items=tuple(i for i in items if i is not None),
            conjunction=data.get("conjunction", "AND"),
            negated=bool(data.get("negated", False)),
        )
    column = _column_from(data.get("column"))
    return Condition(
        column=column or Column(""),
        op=data.get("op", "="),
        value=data.get("value"),
    )


def _table_dict(ref: TableRef | None) -> dict | None:
    if ref is None:
        return None
    return {"name": ref.name, "schema": ref.schema, "alias": ref.alias}


def _table_from(data: Any) -> TableRef | None:
    if not data:
        return None
    return TableRef(
        name=data.get("name", ""),
        schema=data.get("schema", ""),
        alias=data.get("alias", ""),
    )


def to_dict(model: QueryModel) -> dict:
    """`model` as plain JSON-able data."""
    return {
        "source": _table_dict(model.source),
        "joins": [
            {
                "kind": j.kind,
                "source": _table_dict(j.source),
                "on": [
                    {
                        "left": _column_dict(o.left),
                        "right": _column_dict(o.right),
                        "op": o.op,
                    }
                    for o in j.on
                ],
            }
            for j in model.joins
        ],
        "projections": [
            {
                "column": _column_dict(p.column),
                "expression": p.expression,
                "alias": p.alias,
            }
            for p in model.projections
        ],
        "distinct": model.distinct,
        "where": _filter_dict(model.where) if model.where else None,
        "group_by": [
            g if isinstance(g, str) else _column_dict(g) for g in model.group_by
        ],
        "having": _filter_dict(model.having) if model.having else None,
        "order_by": [
            {
                "column": _column_dict(o.column),
                "expression": o.expression,
                "descending": o.descending,
            }
            for o in model.order_by
        ],
        "limit": model.limit,
        "offset": model.offset,
    }


def from_dict(data: dict) -> QueryModel:
    """The inverse of `to_dict`, tolerant of missing keys so an older
    saved workspace still opens."""
    data = data or {}
    where = _filter_from(data.get("where"))
    having = _filter_from(data.get("having"))
    return QueryModel(
        source=_table_from(data.get("source")),
        joins=tuple(
            Join(
                kind=j.get("kind", "INNER JOIN"),
                source=_table_from(j.get("source")) or TableRef(""),
                on=tuple(
                    On(
                        left=_column_from(o.get("left")) or Column(""),
                        right=_column_from(o.get("right")) or Column(""),
                        op=o.get("op", "="),
                    )
                    for o in j.get("on", [])
                ),
            )
            for j in data.get("joins", [])
        ),
        projections=tuple(
            Projection(
                column=_column_from(p.get("column")),
                expression=p.get("expression", ""),
                alias=p.get("alias", ""),
            )
            for p in data.get("projections", [])
        ),
        distinct=bool(data.get("distinct", False)),
        where=where if isinstance(where, FilterGroup) else None,
        group_by=tuple(
            g if isinstance(g, str) else (_column_from(g) or Column(""))
            for g in data.get("group_by", [])
        ),
        having=having if isinstance(having, FilterGroup) else None,
        order_by=tuple(
            Order(
                column=_column_from(o.get("column")),
                expression=o.get("expression", ""),
                descending=bool(o.get("descending", False)),
            )
            for o in data.get("order_by", [])
        ),
        limit=data.get("limit"),
        offset=data.get("offset"),
    )


# Convenience for callers assembling a model from flat lines


def and_group(items: Iterable["Condition | FilterGroup"]) -> FilterGroup:
    return FilterGroup(items=tuple(items), conjunction="AND")


def folded_group(
    lines: Sequence[tuple[str, "Condition | FilterGroup"]],
) -> FilterGroup | None:
    """Fold `(conjunction, node)` lines the way the flat filter panel
    reads them: strictly left to right, `((a AND b) OR c)`.

    This is the shape the existing one-line-per-filter UI implies; a
    UI that lets the user group explicitly (CORE-22) builds nested
    `FilterGroup`s directly instead.
    """
    group: FilterGroup | None = None
    for conjunction, node in lines:
        if group is None:
            group = FilterGroup(items=(node,), conjunction="AND")
        else:
            group = FilterGroup(items=(group, node), conjunction=conjunction)
    return group
