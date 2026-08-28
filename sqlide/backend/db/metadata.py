"""Metadata providers: one interface the UI walks, one per engine.

The engines disagree about what a database *is*. PostgreSQL nests
schemas inside databases, MySQL calls a database a schema and has
neither level twice, and a SQLite connection is one file — one
database, no accounts, no grants. Left alone, every feature grows its
own `if kind == "postgres"`, and the branching spreads across the UI.

So the UI asks a provider instead:

    provider = registry.create_provider(profile.kind, connector)
    for child in provider.list_children(provider.root(profile.name)):
        ...

* `hierarchy()` — the levels this engine actually has, outermost
  first, so a caller can tell "connection → database → schema →
  object" from "connection → object" without naming an engine.
* `list_children(ref)` — the children of any node, typed as `NodeRef`.
* `describe(ref)` — the `ObjectInfo` the info view renders (db/objects.py).
* `property_sections()` / `table_properties(ref)` — the sections a
  table's Properties view can show on this engine, and that view's
  descriptor (CORE-04); a section the engine has no concept of is
  never offered, so it is omitted rather than drawn empty.
* `get_ddl(ref)` — the object's CREATE statement, where there is one.
* `list_grants(ref)` / `list_principals()` — accounts and what they may
  do; empty everywhere the `grants`/`roles` capabilities are off.
* `principal_columns()` / `principal_table()` — the account overview
  (CORE-12): the columns this engine has attributes for, and the rows
  filled in for them.
* `principal_properties(ref)` — a user or a role as the properties
  panel shows it (CORE-53): the account's own attributes and a link to
  the permission editor, never its grants.
* `object_grants(ref)` — the inverse: who may do what to one object,
  direct and inherited, for the Permissions section of its properties
  (CORE-11).
* `permission_set(user, ref)` / `permission_statements(...)` /
  `apply_permissions(...)` — the permission editor (CORE-10): what one
  principal holds on one object, the GRANT/REVOKE that would change it,
  and running those. Off wherever `permission_editor` is off (SQLite).
* `capabilities()` — the feature flags, so a screen an engine cannot
  fill is hidden rather than shown broken.

Capabilities are declared per engine *without* importing its driver
(each engine's `metadata.py` imports only from `db.base`), because the
UI needs the flags before it opens a connection — that is how the
console knows not to offer a database switcher for a local file.

Every method here queries the catalog, so call a provider from a
worker thread (frontend/util.run_async), never from the GTK main loop.

A provider is bound to one connector, and a connector is attached to
one database: `list_children` on a database other than the connected
one returns nothing rather than guessing. The sidebar already opens a
database through a connection of its own (see frontend/sidebar.py).

Minimum server versions: PostgreSQL 10 and MySQL 5.7 — the oldest the
test matrix in docker-compose.yml starts. Below those the catalog
queries here are not guaranteed. Version differences above them
degrade rather than fail: a capability that a specific server does not
have (MySQL 5.7 has no roles) costs its own section, because every
catalog call goes through `_safe` and answers with an empty list.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, fields, replace

from sqlide.backend.db import extensions as ext, objects
from sqlide.backend.db.base import (
    ColumnInfo,
    Connector,
    ConnectorError,
    PrivilegeInfo,
    RelationInfo,
    ResultSet,
    UserInfo,
)

#: The levels an engine can nest, outermost first. A hierarchy is
#: always a prefix-and-suffix slice of this: every engine starts at a
#: connection and ends at an object.
LEVELS = ("connection", "database", "schema", "object")


@dataclass(frozen=True)
class Capabilities:
    """What an engine can do, as flags the UI can read before it has a
    connection (see the module docstring).

    Everything is off by default: a provider that forgets to declare a
    feature hides it, which is the failure this whole layer exists to
    prevent — the opposite default would show a screen the engine
    cannot fill.
    """

    databases: bool = False  # one server, several databases
    schemas: bool = False  # schemas are a level of their own
    materialized_views: bool = False
    procedures: bool = False
    events: bool = False  # scheduled events (MySQL)
    grants: bool = False  # per-object privileges are readable
    roles: bool = False  # accounts/roles are readable
    extensions: bool = False
    #: The engine has spatial types the geo viewer could map (PG-04).
    #: A flag, not a promise: whether *this* server actually has
    #: PostGIS installed is a catalog question, asked at runtime
    #: through `MetadataProvider.spatial_extension()`.
    geometry: bool = False
    partitions: bool = False
    pragmas: bool = False  # SQLite's PRAGMA settings
    constraints: bool = False  # a constraint catalog of its own
    #: The key constraints are worth a section (and a folder) of their
    #: own, beside the full constraint list — true where the primary
    #: key and the unique constraints are what a person looks for and
    #: the rest is CHECK text (SQ-01).
    keys: bool = False
    rules: bool = False  # rewrite rules (PostgreSQL)
    policies: bool = False  # row-level security policies (PostgreSQL)
    dependencies: bool = False  # what depends on an object is readable
    related_functions: bool = False  # functions a table's triggers call
    account_hosts: bool = False  # an account is 'name'@'host'
    permission_editor: bool = False  # grants are editable object by object
    transactional_grants: bool = False  # GRANT/REVOKE roll back together

    def supports(self, name: str) -> bool:
        """One flag by name, False for a flag this version has never
        heard of — a caller asking about a feature that does not exist
        wants "no", not an AttributeError."""
        return bool(getattr(self, name, False))

    def as_dict(self) -> dict[str, bool]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


#: Every flag name, for tests and for UI that renders the whole set.
CAPABILITY_FLAGS = tuple(f.name for f in fields(Capabilities))


@dataclass(frozen=True)
class NodeRef:
    """One node of the object tree, and the path that reaches it.

    `kind` is the node's own type — a level name ("connection",
    "database", "schema"), "category" for a folder, or an object kind
    ("table", "view", "column", …), the same vocabulary
    db/objects.py builds descriptors for. The rest carries the context
    a child needs to be resolved later: which database and schema it
    sits in, and the table an index or column belongs to.
    """

    kind: str
    name: str = ""
    database: str = ""
    schema: str = ""
    table: str = ""
    category: str = ""
    detail: str = ""  # one-line note for the row (a type, a table)
    #: A schema the server owns rather than the user — `pg_catalog`,
    #: `information_schema`. Browsable like any other, but shown
    #: dimmed, sorted last, and skipped by search unless it is asked
    #: for (PG-03). Whether a name is one is the provider's call
    #: (`is_system_schema`), never the UI's.
    system: bool = False

    #: The kinds that live *inside* a schema, and so are qualified by
    #: one. A database or a schema names itself; a column and an index
    #: are named through the table above them; a tablespace, an
    #: extension and a server setting belong to the cluster and to no
    #: schema at all (PG-02).
    QUALIFIED_KINDS = (
        "table", "view", "function", "procedure",
        "sequence", "data_type", "aggregate",
    )

    @property
    def path(self) -> tuple[str, ...]:
        """The node's address, outermost first — the parts that are
        actually filled. `database.schema.object` on PostgreSQL,
        `database.object` on MySQL, `object` on SQLite (PG-01)."""
        parts = [p for p in (self.database, self.schema) if p]
        if self.kind == "database":
            return (self.name,)
        if self.kind == "schema":
            return tuple(dict.fromkeys([*parts, self.name]))
        if self.name:
            parts.append(self.name)
        return tuple(parts)

    def child(self, kind: str, name: str, **extra) -> NodeRef:
        """A child of this node, inheriting its database and schema."""
        return NodeRef(
            kind=kind,
            name=name,
            database=extra.pop("database", self.database),
            schema=extra.pop("schema", self.schema),
            **extra,
        )


@dataclass(frozen=True)
class GrantEntry:
    """One line of an object's Permissions section (CORE-11).

    The inverse of `PrivilegeInfo`: that one starts from an account and
    says where its rights apply, this one starts from an object and
    says who holds rights on it.

    `via` is the role the grant arrives through, empty for a grant made
    to `principal` itself — the direct/inherited distinction, kept as
    the role's name so the view can say which role it came from rather
    than only that it was not direct. `public` marks the grant every
    account holds (PostgreSQL's PUBLIC), which is shown as its own line
    rather than expanded over every account on the server.
    """

    principal: str
    privilege: str
    via: str = ""
    grantor: str = ""
    grantable: bool = False
    public: bool = False

    @property
    def source(self) -> str:
        """"Direct" or the role it is inherited through, as the
        Permissions section prints it."""
        if self.public:
            return "everyone"
        return f"via {self.via}" if self.via else "direct"


#: Category folder -> (label, the object kind its rows hold). The
#: providers pick their subset; the order here is the order shown.
CATEGORIES = (
    ("tables", "Tables", "table"),
    ("views", "Views", "view"),
    ("functions", "Functions", "function"),
    ("procedures", "Procedures", "procedure"),
    ("indexes", "Indexes", "index"),
    ("triggers", "Triggers", "trigger"),
    ("events", "Events", "event"),
)


#: The catalog folders a provider can hang off a level of the tree,
#: declared in db/objects.py (which this module imports) and re-exported
#: here so the providers have one place to read the vocabulary from.
CATALOG_CATEGORIES = objects.CATALOG_CATEGORIES

#: The folders filled from the relation listing (objects.py), likewise
#: re-exported for the providers.
RELATION_FOLDERS = objects.RELATION_FOLDERS

#: Every folder label the tree can show, catalog folders included, so
#: a caller that has only a slug can name it.
CATEGORY_LABELS = {slug: label for slug, label, _kind in CATEGORIES} | {
    slug: label for slug, (label, _kind) in CATALOG_CATEGORIES.items()
}


@dataclass(frozen=True)
class PrivilegeState:
    """One checkbox of the permission editor.

    `granted` is whether the principal holds the privilege at all,
    `grantable` whether it may pass it on (WITH GRANT OPTION), and
    `inherited_from` names the role it comes through — empty for a
    direct grant, which is the only kind the editor lets you change.
    """

    privilege: str
    granted: bool = False
    grantable: bool = False
    inherited_from: str = ""

    @property
    def editable(self) -> bool:
        return not self.inherited_from


@dataclass(frozen=True)
class PermissionSet:
    """What one principal holds on one object: the node, the dialect's
    own text for it after ON, and a state per privilege the engine
    allows there. An object that carries no grants answers with an
    empty target and no entries."""

    ref: NodeRef
    target: str
    entries: tuple[PrivilegeState, ...] = ()

    def state(self, privilege: str) -> PrivilegeState | None:
        for entry in self.entries:
            if entry.privilege == privilege:
                return entry
        return None


#: How the adapters spell a grantee in `PrivilegeInfo.scope`: the level
#: word, then the account. Stripped back to the account here so a row
#: can name the principal on its own.
_PRINCIPAL_PREFIXES = ("role ", "user ", "grantee ")


def _principal_of(scope: str) -> str:
    """The account named by an object grant's scope ("role analyst" ->
    "analyst"). A scope that names no account answers empty."""
    text = (scope or "").strip()
    for prefix in _PRINCIPAL_PREFIXES:
        if text.lower().startswith(prefix):
            return text[len(prefix):].strip()
    return text


def _grant_table(
    entries: list[GrantEntry], ref: NodeRef
) -> objects.DetailTable:
    """The Permissions section: one row per principal and privilege,
    each linking through to that principal in the permission editor,
    already scoped to this object (CORE-11).

    A grant to PUBLIC links nowhere — it belongs to no account, so
    there is no principal for the editor to open on.
    """
    rows = []
    links: list[objects.ObjectRef | None] = []
    for entry in entries:
        rows.append((
            "PUBLIC" if entry.public else entry.principal,
            entry.privilege,
            entry.source,
            entry.grantor or "—",
            "yes" if entry.grantable else "",
        ))
        links.append(
            None
            if entry.public
            else objects.ObjectRef(
                kind="principal",
                name=entry.principal,
                table=ref.name,
                category=ref.kind,
            )
        )
    return objects.DetailTable(
        title=objects.PROPERTY_SECTION_LABELS["permissions"],
        columns=["Principal", "Privilege", "Source", "Grantor", "Grant option"],
        tabular=True,
        rows=rows,
        links=links,
        empty_note="(nobody holds a privilege here)",
        slug="permissions",
    )


#: How one account attribute is rendered as a table cell, by column
#: name. A provider names the columns its engine fills
#: (`PRINCIPAL_COLUMNS`); this is the one place that turns a
#: `UserInfo` field into text, so every engine spells "yes", a role
#: list or an unlimited connection count the same way.
PRINCIPAL_FIELDS: dict[str, Callable[[UserInfo], str]] = {
    "Name": lambda u: u.name,
    "Host": lambda u: u.host,
    "Type": lambda u: u.kind,
    "Login": lambda u: _yes(u.can_login),
    "Superuser": lambda u: _yes(u.superuser),
    "Create DB": lambda u: _yes(u.create_db),
    "Create role": lambda u: _yes(u.create_role),
    "Member of": lambda u: ", ".join(u.member_of),
    "Valid until": lambda u: u.valid_until,
    "Connection limit": lambda u: (
        "unlimited"
        if u.connection_limit.lstrip("-").isdigit()
        and int(u.connection_limit) < 0
        else u.connection_limit
    ),
    "Plugin": lambda u: u.plugin,
    "Locked": lambda u: _yes(u.locked),
    "Password expiry": lambda u: u.password_expiry,
}

#: Every column an engine could name, for tests and for UI that has to
#: know the vocabulary before a provider is chosen.
PRINCIPAL_COLUMN_NAMES = tuple(PRINCIPAL_FIELDS)

#: The columns of that set that are yes/no answers rather than text.
PRINCIPAL_FLAGS = frozenset(
    ("Login", "Superuser", "Create DB", "Create role", "Locked")
)


def _permission_editor_link(user: UserInfo) -> objects.DetailTable:
    """The one row a principal's properties show instead of its grants:
    a link into the permission editor for that account (CORE-53).

    A link rather than a listing — the editor is the screen that shows
    what the account holds and the only one that can change it, so the
    panel points at it rather than fetching the same thing again.
    """
    return objects.DetailTable(
        title="Permissions",
        columns=["Privileges"],
        rows=[("Open the permission editor…",)],
        links=[objects.ObjectRef("principal", user.name)],
    )


def _yes(flag: bool) -> str:
    """A boolean cell: "yes" or nothing. An empty cell reads as "no"
    at a glance in a wide table, where a column of "no" reads as noise.
    """
    return "yes" if flag else ""


def _safe(call, default):
    """A catalog call that is allowed to be unsupported.

    An engine version that lacks a catalog (MySQL 5.7 and roles), or a
    server that refuses one, should cost that list and nothing else —
    the same contract db/objects.py keeps for its sections.
    """
    try:
        return call()
    except ConnectorError:
        return default
    except Exception:  # a driver that escaped its wrapper
        return default


class MetadataProvider:
    """The interface, plus the implementation every engine shares.

    Subclasses declare `HIERARCHY` and `CAPABILITIES` and override only
    what their engine does differently. Nothing here is dialect-aware:
    the catalog SQL stays inside each adapter (see docs/architecture.md),
    and this layer only decides what to ask for and in what shape.
    """

    #: The levels this engine has, a slice of LEVELS.
    HIERARCHY: tuple[str, ...] = ("connection", "object")
    CAPABILITIES = Capabilities()
    #: The accounts-overview columns this engine can fill (CORE-12),
    #: keys of PRINCIPAL_FIELDS. The generic provider knows only what
    #: every account has.
    PRINCIPAL_COLUMNS: tuple[str, ...] = ("Name", "Type", "Login")
    #: The node kinds that are an account rather than an object. They
    #: are described from their own attributes and never carry a
    #: grants listing (CORE-53); the tree spells one "principal", a
    #: grant row or a link may spell it "user" or "role".
    PRINCIPAL_KINDS: tuple[str, ...] = ("user", "role", "principal")
    #: The schemas the server owns: exact names, and the prefixes a
    #: whole family of them shares (PostgreSQL's `pg_*`). Declared per
    #: engine so the sidebar can dim and sort them without knowing
    #: which engine it is looking at (PG-03); empty for the engines
    #: with no schema level at all.
    SYSTEM_SCHEMAS: tuple[str, ...] = ()
    SYSTEM_SCHEMA_PREFIXES: tuple[str, ...] = ()

    def __init__(self, connector: Connector) -> None:
        self.connector = connector

    # The interface

    def hierarchy(self) -> tuple[str, ...]:
        return self.HIERARCHY

    @classmethod
    def is_system_schema(cls, name: str) -> bool:
        """Is `name` a schema the server owns rather than the user?
        Answerable without a connection, so a row can be styled as it
        is built (PG-03)."""
        lowered = name.lower()
        if lowered in cls.SYSTEM_SCHEMAS:
            return True
        return bool(cls.SYSTEM_SCHEMA_PREFIXES) and lowered.startswith(
            cls.SYSTEM_SCHEMA_PREFIXES
        )

    @classmethod
    def is_system_database(cls, name: str) -> bool:
        """Is `name` a database the server owns rather than the user?

        False everywhere a database and a schema are different things:
        PostgreSQL keeps its catalogs in schemas, so no database of its
        own is the server's. In MySQL a schema *is* a database, so
        there this is the same question as `is_system_schema` and the
        provider says so (MY-01).
        """
        return False

    @classmethod
    def is_system_object(cls, name: str) -> bool:
        """Is `name` an object the engine owns rather than the user?

        The per-object twin of `is_system_schema`, for the engines
        whose internals live beside the user's objects instead of in a
        schema of their own: SQLite's `sqlite_*` tables are in the one
        namespace there is (SQ-01). False by default — an engine that
        keeps its catalog somewhere else has nothing to mark here.

        Answerable without a connection, so a row can be styled as it
        is built, and dimming is all it means: the row expands, opens
        and refreshes like any other.
        """
        return False

    def capabilities(self) -> Capabilities:
        return self.CAPABILITIES

    # Column types, for the one caller that has to turn text into
    # values: the CSV import (CORE-37).

    def column_kinds(self, table: str) -> dict[str, str]:
        """Every column of `table` mapped to the kind of value it
        takes — "integer", "number", "boolean", "binary" or "text".

        The declared type comes from the catalog and the reading of it
        from the adapter (Connector.value_kind), which is why an
        importer can coerce a field without knowing which engine it is
        talking to. A type this provider's adapter has never heard of
        is "text": the driver and the server then decide, rather than
        this layer guessing from the field's own text.
        """
        return self.connector.column_kinds(table)

    def column_type_names(self) -> list[str]:
        """The types this dialect offers, rendered — the designer's
        menu (see Connector.column_type_specs), and the vocabulary
        column_kinds() classifies against."""
        return self.connector.column_types()

    # Extensions (PG-05). One listing, read through the registry in
    # db/extensions.py, so every question above this layer is asked
    # about a *feature* and never about an extension's name.

    def extensions(self) -> list[ext.ExtensionState]:
        """Every extension this server has, installed or available.

        The generic implementation asks the connector, and falls back
        to the Extensions catalog folder for an adapter that only has
        that — one listing either way, empty for an engine with no
        extensions at all.
        """
        if not self.capabilities().extensions:
            return []
        states = _safe(lambda: self.connector.list_extensions(), [])
        if states:
            return list(states)
        return [
            ext.ExtensionState(
                name=row.name,
                version=(row.detail or "").split(" in ")[0].strip(),
                schema=(
                    (row.detail or "").split(" in ", 1)[1].split(" ·")[0]
                    if " in " in (row.detail or "")
                    else ""
                ),
                comment=getattr(row, "definition", "") or "",
            )
            for row in _safe(
                lambda: self.connector.list_catalog("extensions"), []
            )
        ]

    def installed_extensions(self) -> list[ext.ExtensionState]:
        return [state for state in self.extensions() if state.installed]

    def extension_features(self) -> set[str]:
        """The features this connection's installed extensions unlock
        — the one question the UI should ask, since "does this server
        do X" outlives which extension provides X."""
        return ext.features(self.extensions())

    def has_extension_feature(self, feature: str) -> bool:
        return feature in self.extension_features()

    def can_manage_extensions(self) -> bool:
        """May this account install, update or drop one? The actions
        are gated on it, so a role that could not run them is not
        offered them (PG-05)."""
        if not self.capabilities().extensions:
            return False
        return bool(_safe(lambda: self.connector.can_manage_extensions(), False))

    def extension_statements(
        self,
        action: str,
        name: str,
        *,
        schema: str = "",
        version: str = "",
        cascade: bool = False,
    ) -> list[str]:
        """The SQL for one extension action, for the confirmation to
        show and the caller to run. Nothing here executes."""
        quote = self.connector.quote_ident
        if action == "install":
            return [ext.install_sql(name, schema=schema, quote=quote)]
        if action == "update":
            return [ext.update_sql(name, version=version, quote=quote)]
        if action == "drop":
            return [ext.drop_sql(name, cascade=cascade, quote=quote)]
        return []

    def object_extension(self, ref: NodeRef) -> str:
        """The extension `ref` belongs to, "" for a user object — what
        stops an extension's tables and functions reading as mysterious
        objects somebody left behind (PG-05)."""
        if not self.capabilities().extensions or not ref.name:
            return ""
        return _safe(
            lambda: self.connector.extension_owner(ref.name, ref.schema), ""
        )

    def spatial_extension(self) -> str:
        """The spatial extension this connection has, "" for none.

        The geo viewer (PG-04) is offered only where this answers: the
        capability flag says the *engine* could have geometry types,
        this says the *server* does. A PostgreSQL database without
        PostGIS therefore never grows a Map view it could not fill,
        and no engine has to be named in the UI to arrange that.

        Since PG-05 this is the registry's "spatial" feature rather
        than a PostGIS lookup of its own: one mechanism, so a second
        spatial extension would need a registry entry and nothing
        else.
        """
        for state in self.installed_extensions():
            if state.trait.has("spatial"):
                return f"{state.name} {state.version}".strip()
        return ""

    # PRAGMAs (SQ-02). SQLite's settings surface: a catalog of
    # declarations (db/sqlite/pragmas.py) read through the connector.
    # Every engine answers, and every engine but SQLite answers with
    # nothing — the `pragmas` capability says which is which, so the
    # UI asks the provider rather than the engine's name.

    def list_pragmas(self, advanced: bool = False) -> list:
        """The connection's pragmas, as `PragmaState` rows: name,
        current value, documented default and description.

        The expensive checks (`integrity_check` and friends) are listed
        with an empty value — they are run on request, through
        `run_pragma_check`, never as part of drawing the list.
        """
        return []

    def set_pragma(self, name: str, value) -> object:
        """Apply one pragma and return its state re-read from the
        database. Never trusts the write: see `PragmaState`."""
        raise ConnectorError("This connection has no PRAGMA settings")

    def run_pragma_check(self, name: str) -> ResultSet:
        """Run one of the informational pragmas and return its rows —
        `integrity_check`, `compile_options`, `database_list`."""
        raise ConnectorError("This connection has no PRAGMA settings")

    def root(self, name: str = "") -> NodeRef:
        """The connection node the tree starts at."""
        return NodeRef("connection", name)

    def list_children(self, ref: NodeRef) -> list[NodeRef]:
        """The children of `ref`, in display order. A leaf — and any
        kind this provider does not know — answers with an empty list
        rather than raising: the tree asks about every row."""
        if ref.kind == "connection":
            return self._connection_children(ref)
        if ref.kind == "database":
            return self._database_children(ref)
        if ref.kind == "schema":
            return self.categories(ref)
        if ref.kind == "category":
            return self._category_children(ref)
        if ref.kind in ("table", "view"):
            return self._columns(ref)
        return []

    def list_sources(self) -> list[NodeRef]:
        """Every relation a SELECT can read from, schema-qualified
        where the engine has schemas (CORE-18).

        This is what the query builder picks its tables from, so it is
        deliberately wider than the tree's Tables folder: views are
        sources too, and materialized views are where the engine has
        them. What is left out is what cannot be selected sensibly —
        the server's own schemas and objects, and a partition, which
        is read through the table it belongs to.

        Engine-agnostic by construction: the schema level comes from
        the capability flag, never from a name check.
        """
        caps = self.capabilities()
        refs: list[NodeRef] = []
        if caps.schemas:
            schemas = [
                name
                for name in _safe(self.connector.catalog_schemas, [])
                if not self.is_system_schema(name)
            ]
            listings = [
                (
                    schema,
                    _safe(
                        lambda s=schema: self.connector.catalog_tables_in(s),
                        [],
                    ),
                )
                for schema in schemas
            ]
        else:
            listings = [("", _safe(self.connector.catalog_tables, []))]
        for schema, infos in listings:
            for info in infos:
                detail = self.object_detail(info)
                if detail == "partition":
                    continue
                if detail == "materialized" and not caps.materialized_views:
                    continue
                if self.is_system_object(info.name):
                    continue
                refs.append(
                    NodeRef(
                        kind=info.kind,
                        name=info.name,
                        schema=schema,
                        detail=detail,
                    )
                )
        return refs

    def columns_of(self, ref: NodeRef) -> list[ColumnInfo]:
        """The columns of one relation, read through its schema where
        it has one — so a table off the search path answers with its
        own columns rather than a same-named table's (CORE-18)."""
        schema = self.schema_of(ref)
        if schema:
            return _safe(
                lambda: self.connector.catalog_columns_in(schema, ref.name), []
            )
        return _safe(lambda: self.connector.catalog_columns(ref.name), [])

    def relations(self) -> list[RelationInfo]:
        """The database's foreign keys, for anything that infers a
        join from the schema. Schema-qualified on the engines that
        fill `RelationInfo.schema` / `ref_schema`."""
        return _safe(self.connector.catalog_relations, [])

    def describe(self, ref: NodeRef) -> objects.ObjectInfo:
        """The read-only descriptor the info view renders (CORE-01),
        with the Permissions section appended where this engine and
        this kind of object have one (CORE-11)."""
        if ref.kind in self.PRINCIPAL_KINDS:
            return self.principal_properties(ref)
        if ref.kind == "section":
            return self.section_listing(ref)
        info = objects.describe(
            self.connector,
            ref.kind,
            ref.name,
            table=ref.table,
            category=ref.category,
            detail=ref.detail,
            schema=ref.schema,
            administer=self.ADMINISTER_CATEGORIES,
        )
        qualified = self.qualified_name(ref)
        if qualified != info.name:
            # The heading names the object the way the rest of the app
            # does: `crm.customers`, not whichever `customers` the
            # search path happened to find (PG-01).
            info = replace(info, name=qualified)
        return self._with_permissions(info, ref)

    def section_listing(self, ref: NodeRef) -> objects.ObjectInfo:
        """One properties section of a table, on its own (CORE-56).

        Clicking Indexes (or Columns, or Permissions) under a table
        opens that listing as a tab rather than a page of the side
        panel, so the section is described by itself. Most sections
        the plain connector can fill; the provider-only ones
        (`PROVIDER_SECTIONS` — who holds a grant is a question about
        accounts, not about the table) are assembled here, the same
        way `_with_permissions` assembles them into a full descriptor.
        """
        slug = ref.category or ref.name.lower()
        label = objects.PROPERTY_SECTION_LABELS.get(slug, ref.name)
        if slug in objects.PROVIDER_SECTIONS and ref.table:
            owner = NodeRef(kind="table", name=ref.table, schema=ref.schema)
            table = replace(
                _grant_table(self.object_grants(owner), owner),
                tabular=True,
                slug=slug,
            )
            return objects.ObjectInfo(
                kind="section",
                name=label,
                type_label=objects.TYPE_LABELS["section"],
                tables=[table],
            )
        return objects.describe(
            self.connector,
            "section",
            ref.name,
            table=ref.table,
            category=slug,
            schema=ref.schema,
        )

    @classmethod
    def property_sections(cls) -> tuple[str, ...]:
        """The sections a table's Properties view can show on this
        engine, in display order (CORE-04).

        Only capability-gated ones are decided here: the rest are
        assembled from the plain Connector interface, so every adapter
        has them. A section this engine has no concept of is left out
        of the list and the view never draws the heading.

        A classmethod like `capabilities()`, because it is decided by
        the flags alone: the UI can lay the toggle out before it has a
        connection.
        """
        caps = cls.CAPABILITIES
        gated = {
            "keys": caps.keys,
            "constraints": caps.constraints,
            "partitions": caps.partitions,
            "rules": caps.rules,
            "policies": caps.policies,
            "dependencies": caps.dependencies,
            "functions": caps.related_functions,
            "permissions": caps.grants,
        }
        return tuple(
            slug
            for slug, _label in objects.PROPERTY_SECTIONS
            if gated.get(slug, True)
        )

    @classmethod
    def sections_for(cls, kind: str) -> tuple[str, ...]:
        """`property_sections()` as it applies to one kind of node.

        An account is not an object with an ACL of its own: what a user
        or a role may do is a long, slow listing and the permission
        editor is the screen built for it, so the Permissions section
        is dropped from a principal's descriptor rather than inlined
        beside its attributes (CORE-53). Every other kind gets the
        engine's full section list.
        """
        sections = cls.property_sections()
        if kind in cls.PRINCIPAL_KINDS:
            return tuple(slug for slug in sections if slug != "permissions")
        return sections

    def table_properties(self, ref: NodeRef) -> objects.ObjectInfo:
        """The descriptor behind a table tab's Properties side: the
        sections this engine supports, filled for `ref`."""
        info = objects.table_properties(
            self.connector,
            ref.name,
            self.property_sections(),
            kind=ref.kind or "table",
        )
        return self._with_permissions(info, ref)

    # Naming (PG-01)

    def qualified_name(self, ref: NodeRef) -> str:
        """`ref` as a person should see it written: `schema.object` on
        an engine where a schema is a level of its own, the bare name
        everywhere else.

        This is the one place that decides how much of an object's
        address is worth showing, so a tab title, a breadcrumb and a
        generated statement all agree. On MySQL and SQLite it answers
        the plain name and no phantom level appears.
        """
        schema = self.schema_of(ref)
        if not schema:
            return ref.name
        return f"{schema}.{ref.name}"

    def quoted_name(self, ref: NodeRef) -> str:
        """The same address, quoted for this dialect — what belongs in
        generated SQL and DDL.

        Each part is quoted separately, so a schema or an object whose
        name is a reserved word, has capitals or contains a dot stays
        one identifier per part instead of being re-read as a
        qualification the caller never wrote.
        """
        quote = self.connector.quote_ident
        if not ref.name:
            return ""
        schema = self.schema_of(ref)
        if not schema:
            return quote(ref.name)
        return f"{quote(schema)}.{quote(ref.name)}"

    def schema_of(self, ref: NodeRef) -> str:
        """The schema `ref` should be qualified by, empty where it
        should not be qualified at all: an engine without schemas, a
        node that names no schema, and the kinds that are not addressed
        through one (a database, a schema itself, a category folder, a
        column named through its table)."""
        if not self.CAPABILITIES.schemas or not ref.schema:
            return ""
        if ref.kind not in NodeRef.QUALIFIED_KINDS:
            return ""
        return ref.schema

    def get_ddl(self, ref: NodeRef) -> str:
        """The object's CREATE statement, empty where the engine has
        none to give (a column, a category, a plain SQLite index the
        server did not record)."""
        if not ref.name or ref.kind in ("connection", "category", "column"):
            return ""
        return _safe(lambda: self.connector.get_ddl(ref.name), "") or ""

    def list_principals(self) -> list[UserInfo]:
        """The accounts/roles on the server, empty where the engine has
        none (SQLite) or cannot list them portably (JDBC)."""
        if not self.CAPABILITIES.roles:
            return []
        return _safe(self.connector.list_users, [])

    @classmethod
    def principal_columns(cls) -> tuple[str, ...]:
        """The columns the accounts overview shows on this engine, in
        display order — answerable without a connection, like every
        other capability question here.

        An engine only lists a column it has an attribute behind: a
        PostgreSQL account has no host and a MySQL one has no
        "can create db", and a column that would be blank in every row
        is a column that teaches nothing.
        """
        return cls.PRINCIPAL_COLUMNS if cls.CAPABILITIES.roles else ()

    def principal_table(
        self,
    ) -> tuple[tuple[str, ...], list[tuple[UserInfo, tuple[str, ...]]]]:
        """The overview as (columns, rows), each row the account itself
        paired with its already-rendered cells — the account travels
        with the row so activating one opens that principal without the
        UI parsing its own table back into a name."""
        columns = self.principal_columns()
        return columns, [
            (user, tuple(PRINCIPAL_FIELDS[name](user) for name in columns))
            for user in self.list_principals()
        ]

    def principal_properties(self, ref: NodeRef) -> objects.ObjectInfo:
        """A user or a role as the properties panel shows it: its own
        attributes, and a way through to the permission editor
        (CORE-53).

        The panel is a summary. What an account may do is a listing of
        its own — long to read and slow to fetch — and the permission
        editor is where it is both shown and changed, so selecting a
        principal asks the catalog for the account list and nothing
        else: no grant query is issued here.
        """
        label = objects.TYPE_LABELS.get(ref.kind, "Account")
        user = self._principal(ref.name)
        if user is None:
            return objects.ObjectInfo(
                kind=ref.kind or "principal",
                name=ref.name,
                type_label=label,
                summary=[("Name", ref.name)],
                note="No account by this name on the server any more.",
            )
        return objects.ObjectInfo(
            kind=ref.kind or "principal",
            name=ref.name,
            type_label=label,
            summary=self.principal_summary(user),
            tables=[_permission_editor_link(user)],
        )

    def principal_summary(self, user: UserInfo) -> list[tuple[str, str]]:
        """One account's own attributes, as the General block shows
        them: the columns this engine has something behind
        (`principal_columns`), spelled the way the overview spells
        them.

        A flag reads "yes"/"no" here rather than "yes"/blank: a column
        of blanks reads as "no" in a wide table, but a single line
        saying nothing at all reads as a missing answer.
        """
        summary = []
        for column in self.principal_columns():
            value = PRINCIPAL_FIELDS[column](user)
            if column in PRINCIPAL_FLAGS:
                value = value or "no"
            elif not value:
                continue
            summary.append((column, value))
        return summary

    def list_grants(self, ref: NodeRef) -> list[PrivilegeInfo]:
        """Who may do what to `ref`.

        A principal node answers with everything that account may do;
        any other node with the grants recorded on that object. Empty
        wherever the `grants` capability is off — SQLite has no
        privilege system to report on.
        """
        if not self.CAPABILITIES.grants:
            return []
        if ref.kind in self.PRINCIPAL_KINDS:
            user = self._principal(ref.name)
            if user is None:
                return []
            return _safe(lambda: self.connector.list_privileges(user), [])
        if not ref.name:
            return []
        return _safe(
            lambda: self.connector.list_object_grants(ref.kind, ref.name), []
        )

    # Who may do what to one object (CORE-11)

    #: The kinds that carry grants of their own, and so get a
    #: Permissions section. An index belongs to its table and a trigger
    #: to the table it fires on: neither has an ACL to show.
    GRANTABLE_KINDS = ("table", "view", "function", "procedure")

    def object_grants(self, ref: NodeRef) -> list[GrantEntry]:
        """Every principal that holds a privilege on `ref` — the
        inverse of `list_grants` on an account.

        Grants recorded against a role are also held by everyone who is
        a member of it, so those are reported a second time against the
        member, naming the role they arrive through; the checkbox that
        edits them is the role's, not the member's (CORE-10). A grant
        to PUBLIC is one line saying so rather than one line per
        account.

        Empty wherever the `grants` capability is off, and for the
        kinds that carry no ACL of their own.
        """
        if not self.CAPABILITIES.grants or not ref.name:
            return []
        if ref.kind not in self.GRANTABLE_KINDS:
            return []
        recorded = _safe(
            lambda: self.connector.list_object_grants(ref.kind, ref.name), []
        )
        entries: list[GrantEntry] = []
        holders: dict[str, list[PrivilegeInfo]] = {}
        for privilege in recorded:
            name = _principal_of(privilege.scope)
            if not name:
                continue
            public = name.upper() == "PUBLIC"
            entries.append(
                GrantEntry(
                    principal=name,
                    privilege=privilege.privilege,
                    grantor=privilege.grantor,
                    grantable=privilege.grantable,
                    public=public,
                )
            )
            if not public:
                holders.setdefault(name, []).append(privilege)
        entries += self._inherited_grants(holders)
        return sorted(
            entries,
            key=lambda e: (
                e.public, e.principal.lower(), e.via, e.privilege
            ),
        )

    def _inherited_grants(
        self, holders: dict[str, list[PrivilegeInfo]]
    ) -> list[GrantEntry]:
        """The same grants again, once per account that inherits them
        through one of the roles they were made to."""
        if not holders or not self.CAPABILITIES.roles:
            return []
        entries = []
        for account in self.list_principals():
            label = account.name
            for role in self.role_memberships(account):
                if role == label or role not in holders:
                    continue
                for privilege in holders[role]:
                    entries.append(
                        GrantEntry(
                            principal=label,
                            privilege=privilege.privilege,
                            via=role,
                            grantor=privilege.grantor,
                            grantable=privilege.grantable,
                        )
                    )
        return entries

    def _with_permissions(
        self, info: objects.ObjectInfo, ref: NodeRef
    ) -> objects.ObjectInfo:
        """`info` plus its Permissions section, where the engine has a
        grant model and this object kind carries grants. Everywhere
        else the descriptor is returned untouched — a section this
        engine cannot fill is never drawn (CORE-04)."""
        if "permissions" not in self.sections_for(ref.kind):
            return info
        if ref.kind not in self.GRANTABLE_KINDS:
            return info
        table = _grant_table(self.object_grants(ref), ref)
        return replace(info, tables=[*info.tables, table])

    # The permission editor (CORE-10)

    #: Object kind -> the privileges that kind can carry on this
    #: engine, in the order the editor shows them. A kind that is
    #: missing has no grantable privileges here and the editor draws
    #: no checkboxes for it.
    OBJECT_PRIVILEGES: dict[str, tuple[str, ...]] = {}

    def privileges_for(self, ref: NodeRef) -> tuple[str, ...]:
        """The privilege list the editor offers on `ref` — engine- and
        kind-correct, empty where this node carries no grants of its
        own (a category folder, an index, a trigger)."""
        if not self.CAPABILITIES.permission_editor:
            return ()
        if not self.grant_target(ref):
            return ()
        return self.OBJECT_PRIVILEGES.get(ref.kind, ())

    def grant_target(self, ref: NodeRef) -> str:
        """`ref` as the dialect writes it after ON, empty for a node
        GRANT cannot name. Dialect text, so each engine overrides."""
        return ""

    #: The levels of the tree the permission editor always walks
    #: through: a connection, a database or a schema is a container the
    #: grantable objects hang under, and it is usually grantable in its
    #: own right as well.
    GRANT_LEVEL_KINDS = ("connection", "database", "schema")

    @classmethod
    def grantable_kinds(cls) -> frozenset[str]:
        """The object kinds that can carry a grant on this engine.

        Empty where the engine has no privilege system at all, which is
        how the editor knows to explain itself instead of drawing a
        tree with nothing in it (CORE-54).
        """
        if not cls.CAPABILITIES.permission_editor:
            return frozenset()
        return frozenset(
            kind
            for kind, privileges in cls.OBJECT_PRIVILEGES.items()
            if privileges
        )

    @classmethod
    def category_kind(cls, slug: str) -> str:
        """The object kind one folder's rows are, empty for a folder
        whose rows are not one kind (Administer holds more folders)."""
        for name, _label, kind in CATEGORIES:
            if name == slug:
                return kind
        label_kind = CATALOG_CATEGORIES.get(slug)
        return label_kind[1] if label_kind else ""

    def grantable_subtree(self, ref: NodeRef) -> bool:
        """Whether `ref` — or anything under it — can carry a grant for
        the engine's privilege model.

        The permission editor's tree filter (CORE-54): a node that
        answers False is left out of the tree entirely, so a folder of
        indexes or a leaf nothing can be granted on never shows up, and
        a folder whose whole subtree is ungrantable is hidden rather
        than shown empty.
        """
        kinds = self.grantable_kinds()
        if not kinds:
            return False
        if ref.kind in self.GRANT_LEVEL_KINDS:
            # A level is a route to the objects under it even where the
            # level itself takes no grant (a PostgreSQL connection).
            return True
        if ref.kind == "category":
            kind = self.category_kind(ref.category or ref.name.lower())
            if not kind:
                return False
            return kind in kinds or (
                kind in ("table", "view") and "column" in kinds
            )
        if ref.kind in ("table", "view"):
            return ref.kind in kinds or "column" in kinds
        return ref.kind in kinds

    def grantable_children(self, ref: NodeRef) -> list[NodeRef]:
        """`list_children`, with everything ungrantable filtered out."""
        return [
            child
            for child in self.list_children(ref)
            if self.grantable_subtree(child)
        ]

    def privilege_suffix(self, ref: NodeRef) -> str:
        """What each privilege carries in the statement — the column
        parenthetical of a column-level grant, empty everywhere else."""
        return ""

    def object_scope(self, ref: NodeRef) -> str:
        """The `PrivilegeInfo.scope` a grant on `ref` is reported
        under, so a principal's privilege list can be indexed by
        object. The adapters agree on this spelling ("server",
        "database sales", "schema public", "table public.orders",
        "column public.orders.total"); the level names differ per
        engine, which is why the engines can override.
        """
        if ref.kind == "connection":
            return "server"
        if ref.kind == "database":
            return f"database {ref.name}"
        if ref.kind == "schema":
            return f"schema {ref.name}"
        container = ref.schema or ref.database
        if ref.kind in ("table", "view"):
            return f"table {container}.{ref.name}"
        if ref.kind == "column":
            return f"column {container}.{ref.table}.{ref.name}"
        if ref.kind in ("function", "procedure"):
            return f"function {container}.{ref.name}"
        return ""

    def role_memberships(self, user: UserInfo) -> list[str]:
        """The roles `user` is a member of — the ones whose grants it
        also holds. Empty where the engine has no role inheritance."""
        if not self.CAPABILITIES.roles:
            return []
        names = []
        for privilege in _safe(
            lambda: self.connector.list_privileges(user), []
        ):
            if privilege.scope == "role membership":
                names.append(privilege.privilege.removeprefix("member of "))
        return names

    def permission_set(
        self, user: UserInfo, ref: NodeRef
    ) -> PermissionSet:
        """What `user` holds on `ref`, one entry per privilege the
        engine allows there.

        A privilege the account was granted directly is editable; one
        it only has through a role it belongs to is reported with the
        role that carries it and left alone — revoking it here would
        either fail or take it from everyone else in that role, and
        neither is what the checkbox appears to promise.
        """
        names = self.privileges_for(ref)
        target = self.grant_target(ref)
        if not names or not target:
            return PermissionSet(ref=ref, target="", entries=())
        scope = self.object_scope(ref)
        direct = self._grants_at(user, scope)
        inherited: dict[str, tuple[PrivilegeInfo, str]] = {}
        for role in self.role_memberships(user):
            for name, privilege in self._grants_at(
                UserInfo(name=role), scope
            ).items():
                inherited.setdefault(name, (privilege, role))
        entries = []
        for name in names:
            held = direct.get(name)
            if held is not None:
                entries.append(
                    PrivilegeState(
                        privilege=name,
                        granted=True,
                        grantable=held.grantable,
                    )
                )
                continue
            via = inherited.get(name)
            entries.append(
                PrivilegeState(
                    privilege=name,
                    granted=via is not None,
                    grantable=bool(via and via[0].grantable),
                    inherited_from=via[1] if via else "",
                )
            )
        return PermissionSet(ref=ref, target=target, entries=tuple(entries))

    def permission_statements(
        self,
        user: UserInfo,
        current: PermissionSet,
        desired: dict[str, tuple[bool, bool]],
    ) -> list[str]:
        """The GRANT/REVOKE turning `current` into `desired`.

        `desired` maps a privilege to (granted, grantable); a privilege
        it does not mention is left as it is, and so is one that is
        only inherited — the editor never offers to edit those, and a
        caller that asks anyway is ignored rather than obeyed.

        Privileges are checked against the list this engine allows on
        that object, because they land in the statement as text.
        """
        if not current.target:
            return []
        allowed = self.privileges_for(current.ref)
        account = self.connector.account_ident(user)
        suffix = self.privilege_suffix(current.ref)
        add: list[str] = []
        add_grantable: list[str] = []
        drop_option: list[str] = []
        remove: list[str] = []
        for entry in current.entries:
            want = desired.get(entry.privilege)
            if want is None or entry.inherited_from:
                continue
            granted, grantable = bool(want[0]), bool(want[1])
            if granted == entry.granted and grantable == entry.grantable:
                continue
            if entry.privilege not in allowed:
                raise ConnectorError(
                    f"{entry.privilege} cannot be granted on "
                    f"{current.target}"
                )
            if not granted:
                remove.append(entry.privilege)
            elif grantable:
                add_grantable.append(entry.privilege)
            elif entry.granted and entry.grantable:
                drop_option.append(entry.privilege)
            else:
                add.append(entry.privilege)

        def listed(privileges: list[str]) -> str:
            return ", ".join(p + suffix for p in privileges)

        statements = []
        for privileges, option in ((add, False), (add_grantable, True)):
            if privileges:
                statements.append(
                    f"GRANT {listed(privileges)} ON {current.target} "
                    f"TO {account}"
                    + (" WITH GRANT OPTION" if option else "")
                )
        if drop_option:
            statements.append(
                f"REVOKE GRANT OPTION FOR {listed(drop_option)} "
                f"ON {current.target} FROM {account}"
            )
        if remove:
            statements.append(
                f"REVOKE {listed(remove)} ON {current.target} "
                f"FROM {account}"
            )
        return statements

    def apply_permissions(self, statements: list[str]) -> None:
        """Run the editor's statements.

        In one transaction where the engine keeps DDL transactional
        (PostgreSQL): a half-applied permission change is a security
        state nobody chose. MySQL commits each GRANT as it runs, so
        there the flag is off and the error says which statement was
        the last to succeed by naming the one that failed.
        """
        if not statements:
            return
        transactional = self.CAPABILITIES.transactional_grants
        if transactional:
            self.connector.execute("BEGIN")
        for sql in statements:
            try:
                self.connector.execute(sql)
            except ConnectorError as exc:
                if transactional:
                    _safe(lambda: self.connector.execute("ROLLBACK"), None)
                raise ConnectorError(f"{exc}\n\nFailed on: {sql}") from exc
        if transactional:
            self.connector.execute("COMMIT")

    def _grants_at(
        self, user: UserInfo, scope: str
    ) -> dict[str, PrivilegeInfo]:
        """One account's grants at one scope, by privilege name. The
        strongest entry wins: two rows for the same privilege differ
        only in whether it may be passed on."""
        found: dict[str, PrivilegeInfo] = {}
        for privilege in _safe(
            lambda: self.connector.list_privileges(user), []
        ):
            if privilege.scope != scope:
                continue
            name = privilege.privilege.upper()
            if name not in found or privilege.grantable:
                found[name] = privilege
        return found

    # Shared implementation

    def _principal(self, name: str) -> UserInfo | None:
        for user in self.list_principals():
            if user.name == name or self.connector.account_ident(user) == name:
                return user
        return None

    def _connection_children(self, ref: NodeRef) -> list[NodeRef]:
        """Databases where the server hosts several, otherwise the
        object categories straight off the connection — one file is
        one database and a level of its own would be an empty step."""
        if self.CAPABILITIES.databases:
            current = self._current_database()
            names = _safe(
                lambda: self.connector.list_databases(include_system=True), []
            )
            return [
                NodeRef(
                    "database", name, database=name,
                    detail="current" if name == current else "",
                    system=self.is_system_database(name),
                )
                # The server's own databases come last: they are there
                # to be looked into, not worked in (PG-03, MY-01). On an
                # engine that has none this is the plain "current
                # first" order it always was.
                for name in sorted(
                    names,
                    key=lambda n: (
                        self.is_system_database(n), n != current, n
                    ),
                )
            ] + self.tree_categories(ref)
        return self.categories(ref)

    def _database_children(self, ref: NodeRef) -> list[NodeRef]:
        return self.categories(ref)

    def _current_database(self) -> str:
        return ""

    def categories(self, ref: NodeRef) -> list[NodeRef]:
        """The folders under a database (or, for a single-database
        engine, under the connection): the ones the adapter says it
        knows the kind of, in CATEGORIES order."""
        kinds = set(_safe(self.connector.ddl_kinds, ()))
        caps = self.CAPABILITIES
        children = []
        for slug, label, kind in CATEGORIES:
            if slug in ("tables", "views", "functions"):
                available = True
            elif slug == "procedures":
                available = caps.procedures
            elif slug == "events":
                available = caps.events
            else:  # indexes, triggers — browse-to-drop folders
                available = kind in kinds and self.connector.supports_drop
            if available:
                children.append(ref.child("category", label, category=slug))
        return children

    #: The sub-folders an "Administer" folder holds, by slug, for the
    #: engines that have one. Empty here: a generic engine has no
    #: server-wide administration the tree can browse.
    ADMINISTER_CATEGORIES: tuple[str, ...] = ()

    #: The folders each *level* of this engine's tree shows alongside
    #: the level rows under it: level name -> folder slugs, in display
    #: order. Empty here — a generic engine hangs its categories off
    #: the innermost level and shows nothing else (see
    #: postgres/metadata.py, PG-02).
    LEVEL_CATEGORIES: dict[str, tuple[str, ...]] = {}

    #: Folders this engine spells differently from the shared
    #: vocabulary: slug -> label. SQLite's only triggers are a table's,
    #: and its tree says so ("Table Triggers", SQ-01) rather than
    #: repeating the word the section under every table already uses.
    CATEGORY_LABEL_OVERRIDES: dict[str, str] = {}

    @classmethod
    def category_label(cls, slug: str) -> str:
        """What this engine calls one folder — its own word for it
        where it has one, the shared label otherwise."""
        return cls.CATEGORY_LABEL_OVERRIDES.get(slug) or CATEGORY_LABELS[slug]

    @classmethod
    def level_categories(cls, level: str) -> tuple[tuple[str, str], ...]:
        """(slug, label) for the folders `level` shows, decided by the
        declaration alone so the sidebar can lay a level out before it
        has asked the server anything (CORE-02)."""
        return tuple(
            (slug, cls.category_label(slug))
            for slug in cls.LEVEL_CATEGORIES.get(level, ())
        )

    def tree_categories(self, ref: NodeRef) -> list[NodeRef]:
        """The folders that hang off one *level* row — a connection, a
        database, a schema — alongside the level rows under it.

        The default is the shape every engine had before there were
        catalog folders: the object categories hang off the innermost
        level this engine has, and the levels above it list only their
        children. An engine with more to show at a level says so by
        overriding this (see postgres/metadata.py), and the sidebar
        renders whatever it answers rather than deciding per engine.
        """
        if ref.kind == "connection":
            return [] if self.CAPABILITIES.databases else self.categories(ref)
        if ref.kind == "database":
            return [] if self.CAPABILITIES.schemas else self.categories(ref)
        if ref.kind == "schema":
            return self.categories(ref)
        return []

    def catalog_category(self, ref: NodeRef, slug: str) -> NodeRef:
        """One catalog folder under `ref`, labelled the one way
        CATALOG_CATEGORIES spells it."""
        return ref.child(
            "category", self.category_label(slug), category=slug
        )

    def _catalog_children(self, ref: NodeRef, slug: str) -> list[NodeRef]:
        """The rows of a catalog folder: the accounts, the sub-folders
        of Administer, or whatever the adapter's `list_catalog` gives
        for this slug."""
        if slug in ("roles", "users"):
            return [
                ref.child("principal", user.name, detail=user.detail)
                for user in self.list_principals()
            ]
        if slug == "administer":
            return [
                self.catalog_category(ref, name)
                for name in self.ADMINISTER_CATEGORIES
            ]
        kind = CATALOG_CATEGORIES[slug][1]
        rows = _safe(
            lambda: self.connector.list_catalog(slug, ref.schema), []
        )
        return [
            ref.child(
                row.kind or kind, row.name,
                category=slug, detail=row.detail,
            )
            for row in rows
        ]

    def _category_children(self, ref: NodeRef) -> list[NodeRef]:
        slug = ref.category or ref.name.lower()
        if slug in ("tables", "views"):
            want_view = slug == "views"
            return [
                ref.child(
                    "view" if want_view else "table", info.name,
                    detail=self.object_detail(info),
                    system=ref.system or self.is_system_object(info.name),
                )
                for info in self._objects(ref)
                if (info.kind == "view") == want_view
            ]
        if slug in ("functions", "procedures"):
            kind = "function" if slug == "functions" else "procedure"
            return [
                ref.child(
                    kind, function.name, category=slug,
                    detail=function.detail,
                )
                for function in _safe(
                    lambda: self.connector.list_routines(kind), []
                )
            ]
        if slug == "indexes":
            return [
                ref.child("index", index.name, table=index.table,
                          detail=index.table)
                for index in _safe(self.connector.list_indexes, [])
            ]
        if slug == "triggers":
            return [
                ref.child("trigger", trigger.name, table=trigger.table,
                          detail=trigger.table)
                for trigger in _safe(self.connector.list_triggers, [])
            ]
        if slug == "events":
            return [
                ref.child("event", name)
                for name in _safe(self.connector.list_events, [])
            ]
        if slug in CATALOG_CATEGORIES:
            return self._catalog_children(ref, slug)
        return []

    def _objects(self, ref: NodeRef):
        """The tables and views of the node being listed. `ref` is the
        category folder, so an engine with schemas can read the one it
        was opened under (see postgres/metadata.py)."""
        return _safe(self.connector.catalog_tables, [])

    def object_detail(self, info) -> str:
        """The one-line note next to a table or view row. Empty here;
        the engines that mark a kind out — a materialized view, a
        partitioned table — say so in their own override."""
        return ""

    def _columns(self, ref: NodeRef) -> list[NodeRef]:
        return [
            ref.child(
                "column", column.name, table=ref.name,
                detail=column.type + (" · PK" if column.is_pk else ""),
            )
            for column in self.columns_of(ref)
        ]
