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
import re
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
    "Preflight",
    "MYSQL",
    "OPTION_KINDS",
    "OptionSpec",
    "POSTGRES",
    "SQLITE",
    "Statement",
    "TableModel",
    "dialect_for",
    "dump_state",
    "from_dict",
    "load_state",
    "option_on",
    "option_set",
    "option_value",
    "plan",
    "preflight",
    "prune_options",
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


#: The kinds of value an engine option can take. A kind the designer
#: has never heard of is simply not shown, so an engine may add one
#: here without the frontend growing a branch for it.
OPTION_KINDS = ("boolean", "choice", "text", "integer")


@dataclass(frozen=True)
class OptionSpec:
    """One option an engine offers on a table or a column.

    This is the whole of CORE-27's answer to "MySQL has a storage
    engine, PostgreSQL has a tablespace, SQLite has WITHOUT ROWID":
    every one of them is *described* here as data — a name, a label, a
    kind, the values it allows and how it is written — and both the
    designer and the renderer read the description. Neither has an `if
    engine == …` in it, and an engine that grows an option grows it in
    one place.

    - `name` is the key in `TableModel.options` / `ColumnModel.options`
      *and*, unless `template` says otherwise, the word that goes into
      the SQL.
    - `kind` is one of `OPTION_KINDS`; `choices` fills a chooser for a
      "choice" and `default` is what the designer starts it on.
    - `capability` gates the option on a `Capabilities` flag, so
      partitioning is offered only where the provider says the server
      partitions.
    - `field` maps the option onto a field of the model instead of the
      free options map — identity and generated columns are columns'
      options in the UI but have had model fields (and dialect-aware
      rendering) since CORE-23, and this is what keeps them one thing.
    - `placement` says where the rendered option goes: "prefix"
      (between CREATE and TABLE, PostgreSQL's UNLOGGED), "tail" (after
      the closing parenthesis, MySQL's ENGINE= and SQLite's WITHOUT
      ROWID), "with" (inside PostgreSQL's `WITH (…)` storage
      parameters) or "column" (inside a column's own definition).
    - `alterable` is whether an existing table's option can be changed
      by an ALTER rather than only set at creation.
    """

    name: str
    label: str = ""
    scope: str = "table"
    kind: str = "text"
    choices: tuple[str, ...] = ()
    default: str = ""
    note: str = ""
    capability: str = ""
    field: str = ""
    placement: str = "tail"
    template: str = ""
    alterable: bool = False

    @property
    def title(self) -> str:
        return self.label or self.name

    def render(self, value: str) -> str:
        """The option, as SQL, for a value that is present.

        A template wins; otherwise a boolean is its own name (`STRICT`)
        and everything else is `NAME=value`, spaced out inside a
        `WITH (…)` list the way PostgreSQL writes it.
        """
        if self.template:
            return self.template.format(value=value)
        if self.kind == "boolean":
            return self.name
        if self.placement == "with":
            return f"{self.name} = {value}"
        return f"{self.name}={value}"


def option_on(value: Any) -> bool:
    """Whether an option's stored value means "set".

    Values are strings — that is what survives JSON and a text entry —
    so a boolean is "true"/"" and anything non-blank is a value. A
    real `True` is accepted too, because a caller building a model in
    Python will write one.
    """
    if value is True:
        return True
    if value is False or value is None:
        return False
    return bool(str(value).strip())


def option_set(spec: OptionSpec, value: Any) -> bool:
    """Whether `value` means this option is on.

    Almost always "is there anything there", with one exception worth
    the line: a boolean stores "true"/"" and a stored "false" means
    off, while a *choice* whose values happen to be "true" and "false"
    (PostgreSQL's autovacuum_enabled) means exactly what it says.
    """
    if spec.kind == "boolean" and str(value).strip().lower() == "false":
        return False
    return option_on(value)


#: PostgreSQL's table options. UNLOGGED and the storage parameters are
#: the ones with an everyday use; TABLESPACE and INHERITS are here
#: because the alternative is leaving the designer for a console.
POSTGRES_TABLE_OPTIONS = (
    OptionSpec(
        "UNLOGGED",
        label="Unlogged",
        kind="boolean",
        placement="prefix",
        note="Faster writes, but the table is empty after a crash and "
        "is not replicated.",
    ),
    OptionSpec(
        "INHERITS",
        label="Inherits",
        kind="text",
        placement="tail",
        template="INHERITS ({value})",
        note="Parent tables, comma separated.",
    ),
    OptionSpec(
        "PARTITION BY",
        label="Partition by",
        kind="text",
        placement="tail",
        template="PARTITION BY {value}",
        capability="partitions",
        note="A strategy and its columns, e.g. RANGE (created_at).",
    ),
    OptionSpec(
        "fillfactor",
        label="Fill factor",
        kind="integer",
        placement="with",
        default="100",
        alterable=True,
        note="Percentage of a page to fill; below 100 leaves room for "
        "updates in place.",
    ),
    OptionSpec(
        "autovacuum_enabled",
        label="Autovacuum",
        kind="choice",
        choices=("true", "false"),
        placement="with",
        alterable=True,
    ),
    OptionSpec(
        "TABLESPACE",
        label="Tablespace",
        kind="text",
        placement="tail",
        template="TABLESPACE {value}",
        note="Where the table's files live; blank means the default.",
    ),
)

POSTGRES_COLUMN_OPTIONS = (
    OptionSpec(
        "identity",
        label="Identity",
        scope="column",
        kind="boolean",
        field="identity",
        note="GENERATED BY DEFAULT AS IDENTITY — the standard spelling, "
        "preferred over the serial types for a new table.",
    ),
    OptionSpec(
        "generated",
        label="Generated as",
        scope="column",
        kind="text",
        field="generated",
        note="An expression computed from the other columns "
        "(PostgreSQL 12 and later).",
    ),
)

#: MySQL's table options, all of them trailing `NAME=value` pairs.
MYSQL_TABLE_OPTIONS = (
    OptionSpec(
        "ENGINE",
        label="Storage engine",
        kind="choice",
        choices=("InnoDB", "MyISAM", "MEMORY", "ARCHIVE", "CSV"),
        default="InnoDB",
        alterable=True,
        note="InnoDB is the only one with transactions and foreign keys.",
    ),
    OptionSpec(
        "DEFAULT CHARSET",
        label="Character set",
        kind="choice",
        choices=("utf8mb4", "utf8", "latin1", "binary"),
        alterable=True,
        template="DEFAULT CHARSET={value}",
        note="The default for the table's text columns.",
    ),
    OptionSpec(
        "COLLATE",
        label="Collation",
        kind="choice",
        choices=(
            "utf8mb4_general_ci",
            "utf8mb4_unicode_ci",
            "utf8mb4_bin",
            "latin1_swedish_ci",
        ),
        alterable=True,
        note="How text in this table compares and sorts.",
    ),
    OptionSpec(
        "AUTO_INCREMENT",
        label="Auto increment start",
        kind="integer",
        alterable=True,
        note="The next value the auto-numbered column takes.",
    ),
    OptionSpec(
        "ROW_FORMAT",
        label="Row format",
        kind="choice",
        choices=("DEFAULT", "DYNAMIC", "COMPACT", "REDUNDANT", "COMPRESSED"),
        alterable=True,
    ),
)

MYSQL_COLUMN_OPTIONS = (
    OptionSpec(
        "identity",
        label="Auto increment",
        scope="column",
        kind="boolean",
        field="identity",
        note="AUTO_INCREMENT: the server numbers this column. One per "
        "table, and it has to be a key.",
    ),
    OptionSpec(
        "generated",
        label="Generated as",
        scope="column",
        kind="text",
        field="generated",
        note="An expression computed from the other columns.",
    ),
)

#: SQLite's two table options, both bare words after the closing
#: parenthesis.
SQLITE_TABLE_OPTIONS = (
    OptionSpec(
        "WITHOUT ROWID",
        label="Without rowid",
        kind="boolean",
        note="Stores the rows in the primary key's own index. The table "
        "must have a primary key.",
    ),
    OptionSpec(
        "STRICT",
        label="Strict types",
        kind="boolean",
        note="Refuses a value that does not fit the column's declared "
        "type (SQLite 3.37 and later).",
    ),
)

SQLITE_COLUMN_OPTIONS = (
    OptionSpec(
        "identity",
        label="Auto increment",
        scope="column",
        kind="boolean",
        field="identity",
        note="AUTOINCREMENT, which SQLite only has on a single-column "
        "INTEGER primary key.",
    ),
)


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
    #: The name this column had when the model was loaded from the
    #: catalog, where the designer has since renamed it. Empty for a
    #: column that was not renamed, which is every column of a table
    #: being created. It is what lets the planner tell a rename from a
    #: drop plus an add: without it, `id` -> `person_id` reads as
    #: losing a column and gaining an empty one (CORE-26).
    renamed_from: str = ""
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
    #: Per-column sort direction, positionally matched to `columns`:
    #: "DESC" for a descending entry, "" (or "ASC") for the engine's
    #: default. Shorter than `columns` is allowed and means "default
    #: for the rest", so an index that never mentions direction keeps
    #: rendering exactly as it did.
    directions: tuple[str, ...] = ()

    def direction(self, position: int) -> str:
        """The direction of the column at `position`, normalised to
        "DESC" or ""."""
        if position < len(self.directions):
            if self.directions[position].strip().upper() == "DESC":
                return "DESC"
        return ""

    @property
    def key(self) -> tuple:
        if self.name:
            return ("name", self.name)
        return (
            "shape",
            tuple(self.columns),
            tuple(
                self.direction(i) for i in range(len(self.columns))
            ),
            self.unique,
            self.method,
        )


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


    @classmethod
    def from_provider(cls, provider: Any, ref: Any) -> "TableModel":
        """The table `ref` names, as a model, read through the
        MetadataProvider (CORE-26).

        Everything comes from the provider — its columns, its indexes,
        its foreign keys and the schema it should be qualified by — so
        the designer opens an existing table on the same catalog the
        sidebar reads, with the same capability flags, and no query of
        its own. What the catalog cannot tell us (a column's default
        expression, its comment) stays unset rather than guessed: an
        unset field is one the diff will not touch, so loading a table
        and applying it back unchanged plans nothing.
        """
        columns = tuple(
            ColumnModel(
                name=info.name,
                type=info.type,
                nullable=bool(info.nullable),
                primary_key=bool(info.is_pk),
            )
            for info in provider.columns_of(ref)
        )
        indexes = tuple(
            index
            for index in (
                _index_from_ddl(info)
                for info in _quiet(lambda: provider.indexes_of(ref), [])
            )
            if index is not None
        )
        return cls(
            name=ref.name,
            schema=_quiet(lambda: provider.schema_of(ref), "") or "",
            columns=columns,
            constraints=_foreign_keys(provider, ref),
            indexes=indexes,
        )


def _quiet(call: Callable[[], Any], default: Any) -> Any:
    """`call()`, or `default` when the adapter has nothing to say.

    Loading a table for editing must not fail because one optional
    catalog listing is missing — a model without its indexes is still
    a model, and the diff simply will not mention them.
    """
    try:
        return call()
    except Exception:
        return default


def _foreign_keys(provider: Any, ref: Any) -> tuple[ConstraintModel, ...]:
    """The table's foreign keys, from the provider's relation listing.

    The listing is one entry per *column pair*, with no constraint
    name, so pairs pointing at the same table are collected into one
    composite key in the order the catalog reported them — which is
    what a two-column key looks like in the designer.
    """
    schema = (getattr(ref, "schema", "") or "").lower()
    name = ref.name.lower()
    grouped: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for rel in _quiet(provider.relations, []):
        if rel.table.lower() != name:
            continue
        if schema and rel.schema and rel.schema.lower() != schema:
            continue
        grouped.setdefault(
            (rel.ref_schema or "", rel.ref_table), []
        ).append((rel.column, rel.ref_column))
    return tuple(
        ConstraintModel(
            kind="FOREIGN KEY",
            columns=tuple(c for c, _r in pairs),
            ref_schema=ref_schema,
            ref_table=ref_table,
            ref_columns=tuple(r for _c, r in pairs),
        )
        for (ref_schema, ref_table), pairs in grouped.items()
    )


#: CREATE [UNIQUE] INDEX name ON table [USING method] (columns) [WHERE …]
_INDEX_RE = re.compile(
    r"create\s+(?P<unique>unique\s+)?index\s+(?:if\s+not\s+exists\s+)?"
    r"(?P<name>[\"`\[]?[^\s\"`\[\]()]+[\"`\]]?)\s+on\s+"
    r"(?P<table>[^\s(]+)\s*(?:using\s+(?P<method>\w+)\s*)?"
    r"\((?P<columns>.+?)\)\s*(?:where\s+(?P<where>.+))?$",
    re.IGNORECASE | re.DOTALL,
)


def _index_from_ddl(info: Any) -> "IndexModel | None":
    """One catalog index as an `IndexModel`.

    The adapters hand back a name and, where the engine records one,
    the CREATE INDEX text; the columns are only in the text, so it is
    parsed. A definition that will not parse still yields an index
    carrying its name — enough for the diff to leave it alone, which is
    the one thing that must not go wrong — and it is simply not
    rendered again.
    """
    name = getattr(info, "name", "") or ""
    ddl = (getattr(info, "ddl", "") or "").strip().rstrip(";")
    match = _INDEX_RE.search(ddl) if ddl else None
    if match is None:
        return IndexModel(name=name) if name else None
    columns: list[str] = []
    directions: list[str] = []
    for entry in match.group("columns").split(","):
        entry = entry.strip()
        if not entry:
            continue
        direction = ""
        if entry.lower().endswith(" desc"):
            direction = "DESC"
            entry = entry[: -len(" desc")].strip()
        elif entry.lower().endswith(" asc"):
            entry = entry[: -len(" asc")].strip()
        columns.append(_unquote(entry))
        directions.append(direction)
    return IndexModel(
        name=name or _unquote(match.group("name")),
        columns=tuple(columns),
        unique=bool(match.group("unique")),
        method=(match.group("method") or "").strip(),
        where=(match.group("where") or "").strip(),
        directions=tuple(directions),
    )


def _unquote(name: str) -> str:
    name = name.strip()
    for opener, closer in (('"', '"'), ("`", "`"), ("[", "]")):
        if name.startswith(opener) and name.endswith(closer) and len(name) > 1:
            return name[1:-1].replace(opener * 2, opener)
    return name


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
    #: How a column is renamed: "rename" (ALTER TABLE … RENAME COLUMN
    #: a TO b, which PostgreSQL and SQLite 3.25+ have) or "change"
    #: (MySQL's CHANGE COLUMN, which restates the whole definition —
    #: 5.7 is our floor and RENAME COLUMN only arrived in 8.0).
    rename_column_style: str = "rename"
    #: What a table rebuild has to be wrapped in for this engine to
    #: apply it atomically — SQLite's PRAGMA/BEGIN … COMMIT/PRAGMA
    #: sandwich (`Connector.wrap_rebuild`). Empty where a rebuild is
    #: never planned, so nothing else grows a wrapper it does not need.
    rebuild_prologue: tuple[str, ...] = ()
    rebuild_epilogue: tuple[str, ...] = ()
    #: How table-level options are written: "mysql" (trailing
    #: `ENGINE=InnoDB`), "sqlite" (trailing `WITHOUT ROWID`) or
    #: "postgres" (`WITH (…)`). It decides the punctuation between
    #: trailing options and how an existing table's option is altered;
    #: *which* options exist is `table_options` below.
    option_style: str = "postgres"
    #: The engine's own table and column options, as specs (CORE-27).
    #: The renderer emits exactly these and ignores anything else a
    #: model carries, which is what lets a model designed against one
    #: engine open against another and simply lose what does not apply.
    table_options: tuple[OptionSpec, ...] = ()
    column_options: tuple[OptionSpec, ...] = ()

    def option_spec(self, name: str, scope: str = "table") -> "OptionSpec | None":
        specs = self.table_options if scope == "table" else self.column_options
        for spec in specs:
            if spec.name.lower() == name.lower():
                return spec
        return None

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
    table_options=POSTGRES_TABLE_OPTIONS,
    column_options=POSTGRES_COLUMN_OPTIONS,
    # Generated columns arrived in 12, above our 10 floor; the option's
    # own note says so rather than the engine hiding the field.
    generated_columns=True,
)
MYSQL = Dialect(
    name="mysql",
    quote=_backtick_quote,
    identity_style="auto_increment",
    inline_comments=True,
    rename_column_style="change",
    # 5.7 is the floor and has had generated columns since 5.7.6.
    generated_columns=True,
    modify_style="mysql",
    option_style="mysql",
    table_options=MYSQL_TABLE_OPTIONS,
    column_options=MYSQL_COLUMN_OPTIONS,
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
    table_options=SQLITE_TABLE_OPTIONS,
    column_options=SQLITE_COLUMN_OPTIONS,
    # Mirrors SqliteConnector.wrap_rebuild; `dialect_for` re-reads it
    # off the live connector so the two cannot drift apart.
    rebuild_prologue=("PRAGMA foreign_keys = OFF", "BEGIN"),
    rebuild_epilogue=("COMMIT", "PRAGMA foreign_keys = ON"),
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
    return replace(base, **_rebuild_wrapper(connector, base))


#: Stands in for the rebuild body while `wrap_rebuild` is asked what it
#: puts around one. Never rendered: it is split back out immediately.
_BODY = "\x00sqlide-rebuild-body\x00"


def _rebuild_wrapper(connector: Any, base: Dialect) -> dict:
    """What this connector wraps a rebuild in, asked of the connector
    rather than restated here.

    `wrap_rebuild` is the authority (`Connector.wrap_rebuild`, SQLite's
    override), so the planner emits exactly the sandwich the rebuild
    path has always used instead of a copy that can drift. An adapter
    that wraps nothing — every engine with a real ALTER TABLE — leaves
    the dialect's own (empty) wrapper alone.
    """
    wrap = getattr(connector, "wrap_rebuild", None)
    if not callable(wrap):
        return {}
    try:
        wrapped = list(wrap([_BODY]))
        position = wrapped.index(_BODY)
    except Exception:
        return {}
    return {
        "rebuild_prologue": tuple(wrapped[:position]),
        "rebuild_epilogue": tuple(wrapped[position + 1:]),
    }


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
    line += _column_options_sql(column, dialect)
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


def option_value(holder: Any, spec: OptionSpec) -> str:
    """What `holder` (a table or a column) has set for `spec`.

    A spec that names a model field reads the field; everything else
    reads the free options map. Booleans come back as "true"/"" so one
    kind of value travels through JSON, a text entry and a check box
    alike.
    """
    if spec.field:
        value = getattr(holder, spec.field, "")
    else:
        value = holder.options.get(spec.name, "")
    if isinstance(value, bool):
        return "true" if value else ""
    return str(value or "")


def _present_options(
    holder: Any, specs: tuple[OptionSpec, ...]
) -> list[tuple[OptionSpec, str]]:
    """The specs this holder has a value for, in the order the engine
    declared them — which is the order the engine's grammar wants."""
    out = []
    for spec in specs:
        if spec.field:
            continue  # rendered by the field's own code, not as an option
        value = option_value(holder, spec)
        if option_set(spec, value):
            out.append((spec, value.strip()))
    return out


#: Stands in for the `WITH (…)` group while the trailing options are
#: assembled, so the group lands where the engine declared its first
#: storage parameter rather than always last.
_WITH_SLOT = "\x00sqlide-with\x00"


def _options_sql(model: TableModel, dialect: Dialect) -> str:
    """The tail after the closing parenthesis, from the dialect's own
    option specs — never from a branch on the engine's name."""
    entries = _present_options(model, dialect.table_options)
    tail: list[str] = []
    with_group: list[str] = []
    for spec, value in entries:
        if spec.placement == "prefix":
            continue  # already written before TABLE
        if spec.placement == "with":
            if not with_group:
                tail.append(_WITH_SLOT)
            with_group.append(spec.render(value))
            continue
        tail.append(spec.render(value))
    if not tail:
        return ""
    separator = ", " if dialect.option_style == "sqlite" else " "
    rendered = separator.join(tail)
    if with_group:
        rendered = rendered.replace(
            _WITH_SLOT, "WITH (" + ", ".join(with_group) + ")"
        )
    return " " + rendered


def _create_prefix(model: TableModel, dialect: Dialect) -> str:
    """The words between CREATE and TABLE — PostgreSQL's UNLOGGED, and
    whatever else an engine puts there."""
    words = [
        spec.render(value)
        for spec, value in _present_options(model, dialect.table_options)
        if spec.placement == "prefix"
    ]
    return (" ".join(words) + " ") if words else ""


def _column_options_sql(column: ColumnModel, dialect: Dialect) -> str:
    """A column's engine options, for the ones written into the column
    definition itself. Identity and generated columns are options in
    the designer but fields in the model, and they are rendered by the
    code that has always rendered them."""
    parts = [
        spec.render(value)
        for spec, value in _present_options(column, dialect.column_options)
        if spec.placement in ("column", "tail")
    ]
    return (" " + " ".join(parts)) if parts else ""


def prune_options(model: TableModel, connector: Any) -> TableModel:
    """`model` with every option this engine does not offer dropped.

    A design saved against MySQL and opened against SQLite keeps its
    columns and loses ENGINE=InnoDB, rather than rendering SQL the
    server will refuse. The renderer ignores unknown options anyway;
    this is for the paths that compare two models — the diff, and a
    designer showing what it is about to apply.
    """
    dialect = _as_dialect(connector)
    known = {spec.name.lower() for spec in dialect.table_options}
    column_known = {spec.name.lower() for spec in dialect.column_options}
    return replace(
        model,
        options={
            k: v for k, v in model.options.items() if k.lower() in known
        },
        columns=tuple(
            replace(
                column,
                options={
                    k: v
                    for k, v in column.options.items()
                    if k.lower() in column_known
                },
            )
            for column in model.columns
        ),
    )


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
        # A named PRIMARY KEY constraint keeps its name; the key
        # itself still comes from `model.primary_key`, so the column
        # flags and the constraint cannot disagree.
        named = next(
            (
                con.name
                for con in model.constraints
                if con.kind.upper() == "PRIMARY KEY" and con.name
            ),
            "",
        )
        head = f"CONSTRAINT {dialect.quoted(named)} " if named else ""
        defs.append(
            f"  {head}PRIMARY KEY ("
            + ", ".join(dialect.quoted(c) for c in pk)
            + ")"
        )
    for con in model.constraints:
        if con.kind.upper() == "PRIMARY KEY":
            continue  # already rendered from model.primary_key
        rendered = _constraint_sql(con, dialect)
        if rendered:
            defs.append("  " + rendered)
    sql = (
        f"CREATE {_create_prefix(model, dialect)}TABLE "
        f"{dialect.table_name(model)} (\n"
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
    # An index loaded from a catalog that would not give up its columns
    # (`from_provider`) cannot be written out again; it is skipped
    # rather than turned into broken SQL. Everything the designer
    # builds has columns.
    return [
        _index_sql(model, index, dialect)
        for index in model.indexes
        if index.columns
    ]


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
    entries = []
    for position, column in enumerate(index.columns):
        entry = dialect.quoted(column)
        direction = index.direction(position)
        if direction:
            entry += f" {direction}"
        entries.append(entry)
    head += " (" + ", ".join(entries) + ")"
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
    # Options this engine does not have are dropped before anything is
    # compared, so a model carried over from another engine plans the
    # table it can actually create (CORE-27).
    target = prune_options(target, dialect)
    if current is not None:
        current = prune_options(current, dialect)
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
        return _wrapped(_rebuild_plan(current, target, dialect), dialect)
    return _alter_plan(current, target, dialect)


def _wrapped(statements: list[Statement], dialect: Dialect) -> list[Statement]:
    """A rebuild inside whatever makes it atomic here (SQLite's
    PRAGMA/BEGIN … COMMIT/PRAGMA). The wrapper's own statements are
    `safe`: they are bookkeeping, and classifying them would put a
    PRAGMA in a dialog's list of dangerous changes."""
    if not statements or not (
        dialect.rebuild_prologue or dialect.rebuild_epilogue
    ):
        return statements
    return [
        *(Statement(sql, "safe", "Makes the rebuild atomic")
          for sql in dialect.rebuild_prologue),
        *statements,
        *(Statement(sql, "safe", "Closes the rebuild")
          for sql in dialect.rebuild_epilogue),
    ]


def _matched(current: TableModel, column: ColumnModel) -> ColumnModel | None:
    """The column of `current` that `column` is a changed version of.

    Normally that is the column of the same name; a column the designer
    renamed carries the name it was loaded under in `renamed_from`,
    which is what keeps a rename from reading as a drop plus an add.
    """
    if column.renamed_from:
        old = current.column(column.renamed_from)
        if old is not None:
            return old
    return current.column(column.name)


def _kept(current: TableModel, target: TableModel) -> set[str]:
    """The lower-cased names, in `current`, of the columns that survive
    into `target` — under that name or a new one."""
    kept = set()
    for column in target.columns:
        old = _matched(current, column)
        if old is not None:
            kept.add(old.name.lower())
    return kept


def _needs_rebuild(
    current: TableModel, target: TableModel, dialect: Dialect
) -> bool:
    """Whether the change asks for something this engine's ALTER TABLE
    cannot do in place, so the table has to be rebuilt (SQLite)."""
    names = {c.name.lower() for c in current.columns}
    if not dialect.can_drop_column and names - _kept(current, target):
        return True
    if not dialect.can_modify_column:
        for column in target.columns:
            old = _matched(current, column)
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
    # (new name, old name) for every column that survives: a renamed
    # column is copied out of the column it used to be, so the rebuild
    # carries its rows instead of leaving it empty.
    carried = [
        (c.name, _matched(current, c).name)
        for c in target.columns
        if _matched(current, c) is not None
    ]
    kept = _kept(current, target)
    dropped = [
        c.name for c in current.columns if c.name.lower() not in kept
    ]
    cols = ", ".join(dialect.quoted(new) for new, _old in carried)
    old_cols = ", ".join(dialect.quoted(old) for _new, old in carried)
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
                f"SELECT {old_cols} FROM {backup}",
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
        old = _matched(current, column)
        if old is None:
            out.append(_add_column(table, column, dialect, target))
            continue
        renamed = old.name.lower() != column.name.lower()
        if renamed and dialect.rename_column_style != "change":
            out.append(
                Statement(
                    f"ALTER TABLE {table} RENAME COLUMN "
                    f"{dialect.quoted(old.name)} TO "
                    f"{dialect.quoted(column.name)}",
                    "safe",
                    f"Renames {old.name} to {column.name}",
                )
            )
            renamed = False
        if renamed or _column_changed(old, column):
            # MySQL has no RENAME COLUMN at our floor: CHANGE COLUMN
            # restates the definition and does both at once.
            out.extend(
                _modify_column(table, old, column, dialect, target)
            )
    kept = _kept(current, target)
    for column in current.columns:
        if column.name.lower() not in kept:
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
    out.extend(_option_statements(current, target, dialect, table))
    return out


def _option_statements(
    current: TableModel, target: TableModel, dialect: Dialect, table: str
) -> list[Statement]:
    """The ALTERs for the engine options that changed.

    Only the options the engine's spec marks `alterable` — a SQLite
    table cannot stop being WITHOUT ROWID, and saying so by leaving the
    flag off beats a branch here.
    """
    out: list[Statement] = []
    for spec in dialect.table_options:
        if not spec.alterable or spec.field:
            continue
        before = option_value(current, spec).strip()
        after = option_value(target, spec).strip()
        if before == after:
            continue
        if spec.placement == "with":
            sql = (
                f"ALTER TABLE {table} SET ({spec.render(after)})"
                if option_on(after)
                else f"ALTER TABLE {table} RESET ({spec.name})"
            )
            out.append(
                Statement(sql, "safe", f"Sets {spec.title.lower()}")
            )
            continue
        if not option_on(after):
            # Nothing to set it back to: an engine or a charset has no
            # "unset", and guessing the server's default would be worse
            # than leaving it alone.
            continue
        out.append(
            Statement(
                f"ALTER TABLE {table} {spec.render(after)}",
                "rewrite",
                f"Sets {spec.title.lower()}, which rewrites the table",
            )
        )
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
        verb = "MODIFY COLUMN"
        if old.name.lower() != new.name.lower():
            # CHANGE COLUMN takes the old name and the whole new
            # definition; MODIFY cannot rename.
            verb = f"CHANGE COLUMN {dialect.quoted(old.name)}"
        return [
            Statement(
                f"ALTER TABLE {table} {verb} {definition}",
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
                "destructive",
                f"Drops constraint {con.name}: what it guaranteed stops "
                "being guaranteed",
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


@dataclass(frozen=True)
class Preflight:
    """A cheap question to ask the server before running a plan.

    A `may_fail` statement fails on the *data*, not on the SQL, and the
    server's answer is one line with no row count in it. These are the
    checks that turn "ERROR: column contains null values" into "412
    rows have no email" *before* anything runs: `sql` counts the rows
    that would block the change, `blocking` is the count above which it
    will, and `label` is the untranslated line the dialog shows (the
    frontend puts it through gettext, like `Statement.note`).
    """

    sql: str
    label: str
    #: The statement this check guards, so the dialog can put the
    #: answer beside it.
    statement: str = ""


def preflight(
    current: TableModel | None, target: TableModel, connector: Any
) -> list[Preflight]:
    """The pre-flight checks for `plan(current, target, …)`.

    Only the cheap ones, and only where the answer is actionable: a
    NOT NULL added to a column that has nulls, and a UNIQUE constraint
    or index added over columns that have duplicates. A create has
    none — there are no rows yet — and neither has anything the
    planner classified as safe.
    """
    if current is None:
        return []
    dialect = _as_dialect(connector)
    table = dialect.table_name(current)
    out: list[Preflight] = []
    for column in target.columns:
        old = _matched(current, column)
        tightened = (
            old is not None
            and old.nullable
            and not column.nullable
            and not column.default.present
        )
        if not tightened or old is None:
            continue
        name = dialect.quoted(old.name)
        out.append(
            Preflight(
                f"SELECT count(*) FROM {table} WHERE {name} IS NULL",
                f"rows where {old.name} is null, which NOT NULL would "
                "reject",
                statement=column.name,
            )
        )
    before_constraints = _keys(current.constraints)
    for con in target.constraints:
        if con.key in before_constraints:
            continue
        if con.kind.upper() not in ("UNIQUE", "PRIMARY KEY") or not con.columns:
            continue
        out.append(_duplicate_check(current, con.columns, dialect, table))
    before_indexes = _keys(current.indexes)
    for index in target.indexes:
        if index.key in before_indexes or not index.unique or not index.columns:
            continue
        out.append(_duplicate_check(current, index.columns, dialect, table))
    return out


def _duplicate_check(
    current: TableModel,
    columns: tuple[str, ...],
    dialect: Dialect,
    table: str,
) -> Preflight:
    """How many rows sit in a duplicated group over `columns` — the
    rows a UNIQUE would refuse, not the number of duplicate groups,
    because the first is the number the user has to go and fix."""
    listed = ", ".join(dialect.quoted(c) for c in columns)
    return Preflight(
        f"SELECT count(*) FROM (SELECT {listed} FROM {table} "
        f"GROUP BY {listed} HAVING count(*) > 1) AS d",
        "duplicate groups over "
        + ", ".join(columns)
        + ", which UNIQUE would reject",
        statement=", ".join(columns),
    )


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
                "renamed_from": c.renamed_from,
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
                "directions": list(i.directions),
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
                renamed_from=str(c.get("renamed_from", "")),
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
                directions=tuple(i.get("directions", ())),
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
