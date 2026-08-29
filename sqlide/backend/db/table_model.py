"""A serialisable table model, a DDL renderer, and a change planner.

The table designer used to build its CREATE statement out of GTK
state: `_build_sql()` read the widgets and handed names, `ColumnInfo`s
and a side dict of defaults to `Connector.create_table_sql()`. There
was no table to save, nothing to diff against a table that already
exists, and nothing to unit test. This module is the other half of
that split — the same shape `db/query_model.py` chose for the query
builder (CORE-17): the widget edits a `TableModel`, and the DDL is a
pure function of it.

The rules are query_model's rules:

- **No frontend, no connection.** Nothing here imports from
  `sqlide.frontend` and nothing here opens a cursor, so the renderer
  and the planner are testable without a database.
- **Identifiers are quoted.** Every schema, table, column, constraint
  and index name goes through the dialect's `quote` — in practice the
  connector's own `quote_ident`. The deliberate exceptions are the
  places that are *meant* to be SQL the caller vouches for: a CHECK
  expression, a generated expression, and a `ColumnDefault` tagged
  `expression`. A default tagged `literal` is a value, and is escaped
  as one.
- **Dialect differences live in `Dialect`**, never in a caller's `if
  engine == ...`. It carries the quoting function and the capability
  flags the renderer and the planner need: how the engine spells an
  auto-numbered key, whether a column comment goes inline or in a
  `COMMENT ON`, whether ALTER TABLE can change a column in place at
  all. `dialect_for(connector)` builds one from any connector.

`plan(current, target, dialect)` is the one entry point the designer
needs for both of its modes: `current is None` is a create and yields
one CREATE TABLE, and anything else is a migration. Every statement
comes back classified — `safe`, `rewrite`, `may_fail`, `destructive` —
so a confirmation dialog can lead with what is dangerous instead of
trusting the reader to spot it in the SQL.

`to_dict` / `from_dict` round-trip the model through plain JSON-able
data, and `dump_state` / `load_state` wrap that in a versioned
envelope, which is what lets a design survive a restart (CORE-28) and
seed a template or a copy of an existing table (CORE-29).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any, Callable

__all__ = [
    "CASCADE_ACTIONS",
    "CONSTRAINT_KINDS",
    "CLASSIFICATIONS",
    "ColumnDefault",
    "ColumnModel",
    "ConstraintModel",
    "Dialect",
    "GENERIC",
    "IndexModel",
    "MODEL_VERSION",
    "MYSQL",
    "POSTGRES",
    "SQLITE",
    "Statement",
    "TableModel",
    "dialect_for",
    "dump_state",
    "from_dict",
    "load_state",
    "plan",
    "render_create",
    "to_dict",
]


#: The constraint kinds the model can express. A dialect that lacks
#: one says so through `Dialect.constraint_kinds`.
CONSTRAINT_KINDS = ("PRIMARY KEY", "UNIQUE", "CHECK", "FOREIGN KEY")

#: What a foreign key may do to the referencing row.
CASCADE_ACTIONS = ("NO ACTION", "RESTRICT", "CASCADE", "SET NULL", "SET DEFAULT")

#: How dangerous a planned statement is, worst last. The designer's
#: confirmation dialog orders its warnings by this.
CLASSIFICATIONS = ("safe", "rewrite", "may_fail", "destructive")

#: Defaults that are keywords rather than values, so a model that
#: carries them as a `literal` still renders as SQL rather than as a
#: quoted string.
_BARE_DEFAULTS = ("NULL", "TRUE", "FALSE", "CURRENT_TIMESTAMP", "CURRENT_DATE")


class TableModelError(ValueError):
    """A model that cannot be rendered — an empty identifier, a
    constraint naming no column. Raised rather than rendered into
    broken SQL, so the caller reports it as a problem with the form."""


# Model


@dataclass(frozen=True)
class ColumnDefault:
    """A column's DEFAULT, tagged so the renderer knows what it is.

    `kind` is "none" (no default at all), "literal" (a value, escaped
    and quoted here) or "expression" (SQL the caller vouches for —
    `now()`, `nextval('s')` — pasted in verbatim). The tag is why the
    default can stop being free text the user has to get right by
    hand: a text column's literal `hello` becomes `'hello'` instead of
    an unquoted identifier the server rejects.
    """

    kind: str = "none"
    value: str = ""

    @property
    def present(self) -> bool:
        return self.kind in ("literal", "expression") and (
            self.kind == "literal" or bool(self.value.strip())
        )

    def render(self) -> str:
        """The default as it goes after DEFAULT, or "" when there is
        none."""
        if not self.present:
            return ""
        if self.kind == "expression":
            return self.value.strip()
        text = self.value
        if text.strip().upper() in _BARE_DEFAULTS:
            return text.strip().upper()
        if _is_number(text):
            return text.strip()
        return "'" + text.replace("'", "''") + "'"


def _is_number(text: str) -> bool:
    try:
        float(text.strip())
    except (TypeError, ValueError):
        return False
    return True


@dataclass(frozen=True)
class ColumnModel:
    """One column of the table.

    Everything `ColumnInfo` could not hold lives here: the default as
    a tagged value rather than a string in a side dict, a comment, a
    collation, an identity/auto-increment flag, a generated
    expression, and a free `options` map for whatever an engine has
    that the model has no field for (CORE-27).

    `primary_key` marks participation in the table's primary key; the
    key itself is rendered as one table-level clause, so a composite
    key is just two columns carrying the flag.
    """

    name: str
    type: str = ""
    nullable: bool = True
    primary_key: bool = False
    default: ColumnDefault = ColumnDefault()
    comment: str = ""
    collation: str = ""
    #: Auto-numbered key: PostgreSQL's GENERATED … AS IDENTITY,
    #: MySQL's AUTO_INCREMENT, SQLite's INTEGER PRIMARY KEY. How it is
    #: spelled is the dialect's business, not the caller's.
    identity: bool = False
    #: SQL for a generated (computed) column, empty for a stored one.
    generated: str = ""
    #: Whether a generated column is STORED rather than VIRTUAL.
    generated_stored: bool = True
    options: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ConstraintModel:
    """A primary key, unique, check or foreign-key constraint.

    `columns` are the constrained columns; for a foreign key,
    `ref_columns` are the columns of `ref_table` they point at, and
    `on_delete` / `on_update` are the referential actions. `expression`
    is the CHECK body — raw SQL, like a projection's expression in the
    query model. An unnamed constraint renders without a CONSTRAINT
    clause and lets the engine name it.
    """

    kind: str = "UNIQUE"
    name: str = ""
    columns: tuple[str, ...] = ()
    ref_schema: str = ""
    ref_table: str = ""
    ref_columns: tuple[str, ...] = ()
    on_delete: str = ""
    on_update: str = ""
    expression: str = ""

    @property
    def key(self) -> tuple:
        """What makes two constraints the same one across a diff: the
        name where there is one, the shape otherwise."""
        if self.name:
            return ("name", self.kind.upper(), self.name)
        return (
            "shape",
            self.kind.upper(),
            tuple(self.columns),
            self.ref_table,
            tuple(self.ref_columns),
            self.expression.strip(),
        )


@dataclass(frozen=True)
class IndexModel:
    """A secondary index on the table. `method` is the engine's access
    method (PostgreSQL's btree/gin/gist, MySQL's BTREE/HASH), empty for
    the engine's default; `where` is a partial-index predicate for the
    engines that have one."""

    name: str = ""
    columns: tuple[str, ...] = ()
    unique: bool = False
    method: str = ""
    where: str = ""

    @property
    def key(self) -> tuple:
        if self.name:
            return ("name", self.name)
        return ("shape", tuple(self.columns), self.unique, self.method)


@dataclass(frozen=True)
class TableModel:
    """A whole table: where it lives, its columns, its constraints, its
    indexes, and the table-level options its engine offers (MySQL's
    ENGINE and CHARSET, SQLite's WITHOUT ROWID and STRICT, PostgreSQL's
    UNLOGGED). `schema` is filled only where a schema is a level of its
    own; everywhere else the name renders unqualified, exactly as
    before."""

    name: str = ""
    schema: str = ""
    columns: tuple[ColumnModel, ...] = ()
    constraints: tuple[ConstraintModel, ...] = ()
    indexes: tuple[IndexModel, ...] = ()
    comment: str = ""
    options: dict[str, str] = field(default_factory=dict)

    def column(self, name: str) -> ColumnModel | None:
        for c in self.columns:
            if c.name.lower() == name.lower():
                return c
        return None

    @property
    def primary_key(self) -> tuple[str, ...]:
        """The columns of the primary key, in table order. Read from
        the columns' own flags and from an explicit PRIMARY KEY
        constraint, whichever the caller used."""
        flagged = tuple(c.name for c in self.columns if c.primary_key)
        if flagged:
            return flagged
        for con in self.constraints:
            if con.kind.upper() == "PRIMARY KEY":
                return tuple(con.columns)
        return ()


# Dialects


@dataclass(frozen=True)
class Dialect:
    """Everything the renderer and the planner need to know about one
    engine. Data, not subclasses: an adapter contributes a `Dialect`
    (see `dialect_for`) instead of this module growing a branch per
    engine."""

    name: str = "generic"
    quote: Callable[[str], str] = lambda n: '"' + n.replace('"', '""') + '"'
    #: Whether a table name may be qualified by a schema.
    schemas: bool = False
    #: How an auto-numbered key is spelled: "identity" (PostgreSQL 10+
    #: GENERATED BY DEFAULT AS IDENTITY), "auto_increment" (MySQL),
    #: "rowid" (SQLite, where INTEGER PRIMARY KEY already is one) or ""
    #: where the engine has none.
    identity_style: str = "identity"
    #: Whether a column comment is written inline (MySQL) rather than
    #: as a separate COMMENT ON statement (PostgreSQL) or not at all
    #: (SQLite).
    inline_comments: bool = False
    comment_statements: bool = False
    #: Whether the engine has COLLATE on a column.
    collations: bool = True
    #: Whether the engine has generated (computed) columns at the
    #: minimum version we support. PostgreSQL got them in 12, past our
    #: 10 floor; SQLite in 3.31, past our 3.25 floor.
    generated_columns: bool = False
    #: The constraint kinds the engine enforces. SQLite parses foreign
    #: keys but only honours them with a pragma; it still declares them.
    constraint_kinds: tuple[str, ...] = CONSTRAINT_KINDS
    #: Whether CREATE INDEX takes a USING/method clause and a WHERE.
    index_method: bool = False
    partial_indexes: bool = False
    #: What ALTER TABLE can do in place. SQLite can add and (since
    #: 3.35) drop a column but cannot change one, which is why the
    #: planner falls back to a rebuild there.
    can_add_column: bool = True
    can_drop_column: bool = True
    can_modify_column: bool = True
    can_alter_constraint: bool = True
    #: How a column's type/nullability is changed: "postgres" (ALTER
    #: COLUMN … TYPE / SET NOT NULL) or "mysql" (MODIFY COLUMN, which
    #: restates the whole definition).
    modify_style: str = "postgres"
    #: How table-level options are written: "mysql" (trailing
    #: `ENGINE=InnoDB`), "sqlite" (trailing `WITHOUT ROWID`) or
    #: "postgres" (`WITH (…)`).
    option_style: str = "postgres"

    def quoted(self, name: str) -> str:
        if not name:
            raise TableModelError("Empty identifier")
        if "\x00" in name:
            raise TableModelError("Identifier contains a NUL byte")
        return self.quote(name)

    def table_name(self, model: TableModel) -> str:
        """The table as it is written after CREATE TABLE — qualified
        by its schema where the engine has schemas, bare where it does
        not, so nothing changes for SQLite and MySQL."""
        name = self.quoted(model.name)
        if self.schemas and model.schema:
            return f"{self.quoted(model.schema)}.{name}"
        return name


def _ansi_quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _backtick_quote(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


GENERIC = Dialect()
POSTGRES = Dialect(
    name="postgres",
    quote=_ansi_quote,
    schemas=True,
    identity_style="identity",
    comment_statements=True,
    index_method=True,
    partial_indexes=True,
    modify_style="postgres",
    option_style="postgres",
)
MYSQL = Dialect(
    name="mysql",
    quote=_backtick_quote,
    identity_style="auto_increment",
    inline_comments=True,
    # 5.7 is the floor and has had generated columns since 5.7.6.
    generated_columns=True,
    modify_style="mysql",
    option_style="mysql",
)
SQLITE = Dialect(
    name="sqlite",
    quote=_ansi_quote,
    identity_style="rowid",
    collations=True,
    # 3.25 is the floor; generated columns arrived in 3.31 and
    # DROP COLUMN in 3.35.
    generated_columns=False,
    index_method=False,
    partial_indexes=True,
    can_drop_column=False,
    can_modify_column=False,
    can_alter_constraint=False,
    option_style="sqlite",
)

_BY_NAME = {d.name: d for d in (POSTGRES, MYSQL, SQLITE)}


def dialect_for(connector: Any) -> Dialect:
    """The dialect for a live connector.

    Quoting comes from the connector itself — it is the authority on
    it — and the capability flags from the preset for its engine,
    falling back to the permissive generic set for an adapter we know
    nothing about (JDBC). The same shape as `query_model.dialect_for`.
    """
    name = str(
        getattr(connector, "engine", "") or getattr(connector, "kind", "") or ""
    ).lower()
    if not name:
        name = type(connector).__name__.replace("Connector", "").lower()
    base = _BY_NAME.get(name, GENERIC)
    quote = getattr(connector, "quote_ident", None)
    if callable(quote):
        base = replace(base, quote=quote)
    return base


# Rendering


def _column_sql(column: ColumnModel, dialect: Dialect, pk: tuple[str, ...]) -> str:
    """One column's entry in a CREATE TABLE, without the leading
    indent."""
    parts = [dialect.quoted(column.name)]
    if column.type.strip():
        parts.append(column.type.strip())
    line = " ".join(parts)
    if column.generated.strip() and dialect.generated_columns:
        kind = "STORED" if column.generated_stored else "VIRTUAL"
        line += f" GENERATED ALWAYS AS ({column.generated.strip()}) {kind}"
        return line
    if column.collation.strip() and dialect.collations:
        line += f" COLLATE {column.collation.strip()}"
    default = column.default.render()
    if default:
        line += f" DEFAULT {default}"
    if not column.nullable:
        line += " NOT NULL"
    if column.identity:
        line += _identity_sql(column, dialect, pk)
    if column.comment.strip() and dialect.inline_comments:
        line += " COMMENT '" + column.comment.replace("'", "''") + "'"
    return line


def _identity_sql(
    column: ColumnModel, dialect: Dialect, pk: tuple[str, ...]
) -> str:
    """How this engine spells "the server numbers this column"."""
    if dialect.identity_style == "auto_increment":
        return " AUTO_INCREMENT"
    if dialect.identity_style == "identity":
        return " GENERATED BY DEFAULT AS IDENTITY"
    if dialect.identity_style == "rowid":
        # SQLite's only auto-numbered column is the rowid alias, which
        # has to be declared inline as the primary key — the
        # table-level PRIMARY KEY clause does not make one.
        if len(pk) == 1 and pk[0].lower() == column.name.lower():
            return " PRIMARY KEY AUTOINCREMENT"
        return ""
    return ""


def _constraint_sql(con: ConstraintModel, dialect: Dialect) -> str:
    kind = con.kind.upper()
    if kind not in dialect.constraint_kinds:
        return ""
    head = f"CONSTRAINT {dialect.quoted(con.name)} " if con.name else ""
    if kind == "CHECK":
        if not con.expression.strip():
            raise TableModelError("A CHECK constraint needs an expression")
        return f"{head}CHECK ({con.expression.strip()})"
    if not con.columns:
        raise TableModelError(f"A {kind} constraint needs columns")
    cols = ", ".join(dialect.quoted(c) for c in con.columns)
    if kind == "FOREIGN KEY":
        if not con.ref_table:
            raise TableModelError("A foreign key needs a referenced table")
        target = dialect.quoted(con.ref_table)
        if dialect.schemas and con.ref_schema:
            target = f"{dialect.quoted(con.ref_schema)}.{target}"
        line = f"{head}FOREIGN KEY ({cols}) REFERENCES {target}"
        if con.ref_columns:
            line += (
                " (" + ", ".join(dialect.quoted(c) for c in con.ref_columns) + ")"
            )
        if con.on_delete.strip():
            line += f" ON DELETE {con.on_delete.strip().upper()}"
        if con.on_update.strip():
            line += f" ON UPDATE {con.on_update.strip().upper()}"
        return line
    return f"{head}{kind} ({cols})"


def _options_sql(model: TableModel, dialect: Dialect) -> str:
    """The tail after the closing parenthesis, per engine."""
    entries = [
        (k, v) for k, v in sorted(model.options.items()) if str(v).strip() or v is True
    ]
    if not entries:
        return ""
    if dialect.option_style == "mysql":
        return " " + " ".join(f"{k}={v}" for k, v in entries)
    if dialect.option_style == "sqlite":
        # SQLite's table options are bare words (WITHOUT ROWID, STRICT).
        words = [k if str(v).strip() in ("", "true", "True") else f"{k}={v}"
                 for k, v in entries]
        return " " + ", ".join(words)
    return " WITH (" + ", ".join(f"{k} = {v}" for k, v in entries) + ")"


def render_create(model: TableModel, connector: Any) -> str:
    """The CREATE TABLE statement for `model`.

    `connector` may be a live connector or a `Dialect` — the designer
    has a connector, the tests have a dialect, and neither needs a
    database.
    """
    dialect = _as_dialect(connector)
    if not model.name.strip():
        raise TableModelError("The table has no name")
    columns = [c for c in model.columns if c.name.strip()]
    if not columns:
        raise TableModelError("The table has no columns")
    pk = model.primary_key
    inline_pk = _inline_pk_columns(columns, dialect, pk)
    defs = [f"  {_column_sql(c, dialect, pk)}" for c in columns]
    if pk and not inline_pk:
        defs.append(
            "  PRIMARY KEY (" + ", ".join(dialect.quoted(c) for c in pk) + ")"
        )
    for con in model.constraints:
        if con.kind.upper() == "PRIMARY KEY":
            continue  # already rendered from model.primary_key
        rendered = _constraint_sql(con, dialect)
        if rendered:
            defs.append("  " + rendered)
    sql = (
        f"CREATE TABLE {dialect.table_name(model)} (\n"
        + ",\n".join(defs)
        + "\n)"
        + _options_sql(model, dialect)
    )
    return sql


def _inline_pk_columns(
    columns: list[ColumnModel], dialect: Dialect, pk: tuple[str, ...]
) -> bool:
    """Whether the primary key was already written into a column's own
    definition, so the table-level clause would repeat it. Only SQLite's
    auto-numbered rowid alias does that."""
    if dialect.identity_style != "rowid" or len(pk) != 1:
        return False
    for column in columns:
        if column.identity and column.name.lower() == pk[0].lower():
            return True
    return False


def render_indexes(model: TableModel, connector: Any) -> list[str]:
    """CREATE INDEX statements for the model's indexes. Separate from
    `render_create` because every engine we speak to keeps indexes as
    objects of their own."""
    dialect = _as_dialect(connector)
    return [_index_sql(model, index, dialect) for index in model.indexes]


def _index_sql(model: TableModel, index: IndexModel, dialect: Dialect) -> str:
    if not index.columns:
        raise TableModelError("An index needs columns")
    unique = "UNIQUE " if index.unique else ""
    # An unnamed index lets the engine pick the name (MySQL does;
    # PostgreSQL and SQLite want one, and the caller supplies it).
    named = f"{dialect.quoted(index.name)} " if index.name else ""
    head = f"CREATE {unique}INDEX {named}ON {dialect.table_name(model)}"
    if index.method.strip() and dialect.index_method:
        head += f" USING {index.method.strip()}"
    head += " (" + ", ".join(dialect.quoted(c) for c in index.columns) + ")"
    if index.where.strip() and dialect.partial_indexes:
        head += f" WHERE {index.where.strip()}"
    return head


def render_comments(model: TableModel, connector: Any) -> list[str]:
    """COMMENT ON statements, for the engines that keep comments out of
    the CREATE (PostgreSQL). Empty everywhere else — MySQL writes them
    inline and SQLite has nowhere to put them."""
    dialect = _as_dialect(connector)
    if not dialect.comment_statements:
        return []
    out: list[str] = []
    table = dialect.table_name(model)
    if model.comment.strip():
        out.append(
            f"COMMENT ON TABLE {table} IS "
            + "'" + model.comment.replace("'", "''") + "'"
        )
    for column in model.columns:
        if column.comment.strip():
            out.append(
                f"COMMENT ON COLUMN {table}.{dialect.quoted(column.name)} IS "
                + "'" + column.comment.replace("'", "''") + "'"
            )
    return out


def _as_dialect(connector: Any) -> Dialect:
    return connector if isinstance(connector, Dialect) else dialect_for(connector)


# Planning


@dataclass(frozen=True)
class Statement:
    """One statement of a plan, with what it will do to the table.

    `classification` is one of `CLASSIFICATIONS`:

    - **safe** — nothing existing can be lost or refused (a CREATE, an
      added nullable column, a dropped constraint).
    - **rewrite** — the engine rewrites the table to apply it, which
      can take a while and hold a lock (a type change, a SQLite
      rebuild).
    - **may_fail** — correct SQL that existing rows can refuse: a NOT
      NULL added to a column with nulls in it, a UNIQUE over duplicates,
      a foreign key over orphans.
    - **destructive** — data goes away (a dropped column, a dropped
      table).

    `note` is the one-line explanation the confirmation dialog shows
    beside the SQL. It is deliberately untranslated here: this is the
    backend, and the frontend puts it through gettext.
    """

    sql: str
    classification: str = "safe"
    note: str = ""


def plan(current: TableModel | None, target: TableModel, connector: Any) -> list[Statement]:
    """The statements that turn `current` into `target`, classified.

    `current is None` is a create: one CREATE TABLE plus whatever the
    engine needs beside it (indexes, comments). Anything else is a
    migration, and the designer's create and alter modes become the
    same code path over the same model — which is the whole point of
    the model existing.
    """
    dialect = _as_dialect(connector)
    if current is None:
        out = [Statement(render_create(target, dialect), "safe", "Creates the table")]
        out += [
            Statement(sql, "safe", "Creates an index")
            for sql in render_indexes(target, dialect)
        ]
        out += [
            Statement(sql, "safe", "Sets a comment")
            for sql in render_comments(target, dialect)
        ]
        return out
    if _needs_rebuild(current, target, dialect):
        return _rebuild_plan(current, target, dialect)
    return _alter_plan(current, target, dialect)


def _needs_rebuild(
    current: TableModel, target: TableModel, dialect: Dialect
) -> bool:
    """Whether the change asks for something this engine's ALTER TABLE
    cannot do in place, so the table has to be rebuilt (SQLite)."""
    names = {c.name.lower() for c in current.columns}
    target_names = {c.name.lower() for c in target.columns}
    if not dialect.can_drop_column and names - target_names:
        return True
    if not dialect.can_modify_column:
        for column in target.columns:
            old = current.column(column.name)
            if old is not None and _column_changed(old, column):
                return True
    if not dialect.can_alter_constraint:
        if _keys(current.constraints) != _keys(target.constraints):
            return True
        if current.primary_key != target.primary_key:
            return True
    return False


def _keys(items) -> set:
    return {i.key for i in items}


def _column_changed(old: ColumnModel, new: ColumnModel) -> bool:
    return (
        old.type.strip() != new.type.strip()
        or old.nullable != new.nullable
        or old.default != new.default
        or old.collation.strip() != new.collation.strip()
    )


def _rebuild_plan(
    current: TableModel, target: TableModel, dialect: Dialect
) -> list[Statement]:
    """The rename / create / copy / drop sequence, for an engine whose
    ALTER TABLE cannot express the change (SQLite)."""
    table = dialect.table_name(current)
    backup = dialect.quoted(current.name + "__sqlide_old")
    carried = [
        c.name
        for c in target.columns
        if current.column(c.name) is not None
    ]
    dropped = [
        c.name for c in current.columns if target.column(c.name) is None
    ]
    cols = ", ".join(dialect.quoted(c) for c in carried)
    out = [
        Statement(
            f"ALTER TABLE {table} RENAME TO {backup}",
            "rewrite",
            "Rebuilds the table: this engine cannot alter it in place",
        ),
        Statement(render_create(target, dialect), "rewrite", "Creates the new table"),
    ]
    if carried:
        out.append(
            Statement(
                f"INSERT INTO {dialect.table_name(target)} ({cols}) "
                f"SELECT {cols} FROM {backup}",
                "rewrite",
                "Copies the existing rows across",
            )
        )
    out.append(
        Statement(
            f"DROP TABLE {backup}",
            "destructive" if dropped else "rewrite",
            (
                "Drops the old table, losing "
                + ", ".join(dropped)
                if dropped
                else "Drops the old table once the rows are copied"
            ),
        )
    )
    out += [
        Statement(sql, "safe", "Recreates an index")
        for sql in render_indexes(target, dialect)
    ]
    return out


def _alter_plan(
    current: TableModel, target: TableModel, dialect: Dialect
) -> list[Statement]:
    table = dialect.table_name(target)
    out: list[Statement] = []
    if current.name != target.name or (
        dialect.schemas and current.schema != target.schema
    ):
        out.append(
            Statement(
                f"ALTER TABLE {dialect.table_name(current)} "
                f"RENAME TO {dialect.quoted(target.name)}",
                "safe",
                "Renames the table",
            )
        )
    for column in target.columns:
        old = current.column(column.name)
        if old is None:
            out.append(_add_column(table, column, dialect, target))
        elif _column_changed(old, column):
            out.extend(_modify_column(table, old, column, dialect, target))
    for column in current.columns:
        if target.column(column.name) is None:
            out.append(
                Statement(
                    f"ALTER TABLE {table} DROP COLUMN "
                    f"{dialect.quoted(column.name)}",
                    "destructive",
                    f"Drops column {column.name} and everything in it",
                )
            )
    out.extend(_constraint_statements(current, target, dialect, table))
    out.extend(_index_statements(current, target, dialect))
    return out


def _add_column(
    table: str, column: ColumnModel, dialect: Dialect, model: TableModel
) -> Statement:
    definition = _column_sql(column, dialect, model.primary_key)
    sql = f"ALTER TABLE {table} ADD COLUMN {definition}"
    if not column.nullable and not column.default.present:
        return Statement(
            sql,
            "may_fail",
            f"Adds NOT NULL column {column.name} with no default: this fails "
            "if the table has rows",
        )
    return Statement(sql, "safe", f"Adds column {column.name}")


def _modify_column(
    table: str,
    old: ColumnModel,
    new: ColumnModel,
    dialect: Dialect,
    model: TableModel,
) -> list[Statement]:
    name = dialect.quoted(new.name)
    tightened = old.nullable and not new.nullable
    if dialect.modify_style == "mysql":
        definition = _column_sql(new, dialect, model.primary_key)
        return [
            Statement(
                f"ALTER TABLE {table} MODIFY COLUMN {definition}",
                "may_fail" if tightened else "rewrite",
                (
                    f"Adds NOT NULL to {new.name}: this fails if any row has "
                    "a null there"
                    if tightened
                    else f"Rewrites column {new.name}"
                ),
            )
        ]
    out: list[Statement] = []
    if old.type.strip() != new.type.strip():
        out.append(
            Statement(
                f"ALTER TABLE {table} ALTER COLUMN {name} TYPE "
                f"{new.type.strip()}",
                "rewrite",
                f"Changes the type of {new.name}, rewriting the table",
            )
        )
    if old.nullable != new.nullable:
        if tightened:
            out.append(
                Statement(
                    f"ALTER TABLE {table} ALTER COLUMN {name} SET NOT NULL",
                    "may_fail",
                    f"Adds NOT NULL to {new.name}: this fails if any row has "
                    "a null there",
                )
            )
        else:
            out.append(
                Statement(
                    f"ALTER TABLE {table} ALTER COLUMN {name} DROP NOT NULL",
                    "safe",
                    f"Lets {new.name} be null",
                )
            )
    if old.default != new.default:
        rendered = new.default.render()
        if rendered:
            out.append(
                Statement(
                    f"ALTER TABLE {table} ALTER COLUMN {name} SET DEFAULT "
                    f"{rendered}",
                    "safe",
                    f"Sets the default for {new.name}",
                )
            )
        else:
            out.append(
                Statement(
                    f"ALTER TABLE {table} ALTER COLUMN {name} DROP DEFAULT",
                    "safe",
                    f"Removes the default for {new.name}",
                )
            )
    return out


def _constraint_statements(
    current: TableModel, target: TableModel, dialect: Dialect, table: str
) -> list[Statement]:
    out: list[Statement] = []
    before = {c.key: c for c in current.constraints}
    after = {c.key: c for c in target.constraints}
    for key, con in before.items():
        if key in after:
            continue
        if not con.name:
            continue  # unnamed: nothing to drop it by
        out.append(
            Statement(
                f"ALTER TABLE {table} DROP CONSTRAINT "
                f"{dialect.quoted(con.name)}",
                "safe",
                f"Drops constraint {con.name}",
            )
        )
    for key, con in after.items():
        if key in before:
            continue
        rendered = _constraint_sql(con, dialect)
        if not rendered:
            continue
        out.append(
            Statement(
                f"ALTER TABLE {table} ADD {rendered}",
                "may_fail",
                f"Adds a {con.kind.upper()} constraint: this fails if any "
                "existing row breaks it",
            )
        )
    return out


def _index_statements(
    current: TableModel, target: TableModel, dialect: Dialect
) -> list[Statement]:
    out: list[Statement] = []
    before = {i.key: i for i in current.indexes}
    after = {i.key: i for i in target.indexes}
    for key, index in before.items():
        if key not in after and index.name:
            out.append(
                Statement(
                    f"DROP INDEX {dialect.quoted(index.name)}",
                    "safe",
                    f"Drops index {index.name}",
                )
            )
    for key, index in after.items():
        if key not in before:
            out.append(
                Statement(
                    _index_sql(target, index, dialect),
                    "may_fail" if index.unique else "safe",
                    (
                        "Adds a unique index: this fails if the existing rows "
                        "have duplicates"
                        if index.unique
                        else "Creates an index"
                    ),
                )
            )
    return out


def worst(statements: list[Statement]) -> str:
    """The most dangerous classification in a plan, "" for an empty
    one — what a confirmation dialog leads with."""
    worst_index = -1
    for statement in statements:
        if statement.classification in CLASSIFICATIONS:
            worst_index = max(
                worst_index, CLASSIFICATIONS.index(statement.classification)
            )
    return CLASSIFICATIONS[worst_index] if worst_index >= 0 else ""


# Serialisation


def _default_dict(default: ColumnDefault) -> dict:
    return {"kind": default.kind, "value": default.value}


def _default_from(data: Any) -> ColumnDefault:
    if not isinstance(data, dict):
        return ColumnDefault()
    return ColumnDefault(
        kind=str(data.get("kind", "none")), value=str(data.get("value", ""))
    )


def to_dict(model: TableModel) -> dict:
    """`model` as plain JSON-able data."""
    return {
        "name": model.name,
        "schema": model.schema,
        "comment": model.comment,
        "options": dict(model.options),
        "columns": [
            {
                "name": c.name,
                "type": c.type,
                "nullable": c.nullable,
                "primary_key": c.primary_key,
                "default": _default_dict(c.default),
                "comment": c.comment,
                "collation": c.collation,
                "identity": c.identity,
                "generated": c.generated,
                "generated_stored": c.generated_stored,
                "options": dict(c.options),
            }
            for c in model.columns
        ],
        "constraints": [
            {
                "kind": c.kind,
                "name": c.name,
                "columns": list(c.columns),
                "ref_schema": c.ref_schema,
                "ref_table": c.ref_table,
                "ref_columns": list(c.ref_columns),
                "on_delete": c.on_delete,
                "on_update": c.on_update,
                "expression": c.expression,
            }
            for c in model.constraints
        ],
        "indexes": [
            {
                "name": i.name,
                "columns": list(i.columns),
                "unique": i.unique,
                "method": i.method,
                "where": i.where,
            }
            for i in model.indexes
        ],
    }


def from_dict(data: dict) -> TableModel:
    """The inverse of `to_dict`, tolerant of missing keys so an older
    saved workspace still opens."""
    data = data or {}
    return TableModel(
        name=str(data.get("name", "")),
        schema=str(data.get("schema", "")),
        comment=str(data.get("comment", "")),
        options=dict(data.get("options") or {}),
        columns=tuple(
            ColumnModel(
                name=str(c.get("name", "")),
                type=str(c.get("type", "")),
                nullable=bool(c.get("nullable", True)),
                primary_key=bool(c.get("primary_key", False)),
                default=_default_from(c.get("default")),
                comment=str(c.get("comment", "")),
                collation=str(c.get("collation", "")),
                identity=bool(c.get("identity", False)),
                generated=str(c.get("generated", "")),
                generated_stored=bool(c.get("generated_stored", True)),
                options=dict(c.get("options") or {}),
            )
            for c in data.get("columns", [])
        ),
        constraints=tuple(
            ConstraintModel(
                kind=str(c.get("kind", "UNIQUE")),
                name=str(c.get("name", "")),
                columns=tuple(c.get("columns", ())),
                ref_schema=str(c.get("ref_schema", "")),
                ref_table=str(c.get("ref_table", "")),
                ref_columns=tuple(c.get("ref_columns", ())),
                on_delete=str(c.get("on_delete", "")),
                on_update=str(c.get("on_update", "")),
                expression=str(c.get("expression", "")),
            )
            for c in data.get("constraints", [])
        ),
        indexes=tuple(
            IndexModel(
                name=str(i.get("name", "")),
                columns=tuple(i.get("columns", ())),
                unique=bool(i.get("unique", False)),
                method=str(i.get("method", "")),
                where=str(i.get("where", "")),
            )
            for i in data.get("indexes", [])
        ),
    )


#: Bumped whenever `to_dict`'s shape changes incompatibly. A saved
#: model from a *newer* version is discarded rather than guessed at.
MODEL_VERSION = 1


def dump_state(model: TableModel) -> str:
    """`model` as the JSON text a TabState carries (CORE-28)."""
    return json.dumps({"version": MODEL_VERSION, "model": to_dict(model)})


def load_state(text: str) -> TableModel | None:
    """The inverse of `dump_state`, and deliberately unexcitable:
    anything it cannot make sense of — empty text, malformed JSON, a
    version from the future, a payload that is not a mapping — comes
    back as None, which callers read as "no saved model", never as an
    error. A workspace must always open."""
    if not text:
        return None
    try:
        envelope = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(envelope, dict):
        return None
    try:
        version = int(envelope.get("version", 0))
    except (TypeError, ValueError):
        return None
    if version > MODEL_VERSION or version < 1:
        return None
    data = envelope.get("model")
    if not isinstance(data, dict):
        return None
    try:
        return from_dict(data)
    except (AttributeError, TypeError, ValueError):
        return None
