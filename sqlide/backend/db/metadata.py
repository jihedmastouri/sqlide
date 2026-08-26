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

from dataclasses import dataclass, fields

from sqlide.backend.db import objects
from sqlide.backend.db.base import (
    Connector,
    ConnectorError,
    PrivilegeInfo,
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
    partitions: bool = False
    pragmas: bool = False  # SQLite's PRAGMA settings
    constraints: bool = False  # a constraint catalog of its own
    rules: bool = False  # rewrite rules (PostgreSQL)
    policies: bool = False  # row-level security policies (PostgreSQL)
    dependencies: bool = False  # what depends on an object is readable
    related_functions: bool = False  # functions a table's triggers call
    account_hosts: bool = False  # an account is 'name'@'host'

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

    def child(self, kind: str, name: str, **extra) -> NodeRef:
        """A child of this node, inheriting its database and schema."""
        return NodeRef(
            kind=kind,
            name=name,
            database=extra.pop("database", self.database),
            schema=extra.pop("schema", self.schema),
            **extra,
        )


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

    def __init__(self, connector: Connector) -> None:
        self.connector = connector

    # The interface

    def hierarchy(self) -> tuple[str, ...]:
        return self.HIERARCHY

    def capabilities(self) -> Capabilities:
        return self.CAPABILITIES

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

    def describe(self, ref: NodeRef) -> objects.ObjectInfo:
        """The read-only descriptor the info view renders (CORE-01)."""
        return objects.describe(
            self.connector,
            ref.kind,
            ref.name,
            table=ref.table,
            category=ref.category,
            detail=ref.detail,
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
            "constraints": caps.constraints,
            "partitions": caps.partitions,
            "rules": caps.rules,
            "policies": caps.policies,
            "dependencies": caps.dependencies,
            "functions": caps.related_functions,
        }
        return tuple(
            slug
            for slug, _label in objects.PROPERTY_SECTIONS
            if gated.get(slug, True)
        )

    def table_properties(self, ref: NodeRef) -> objects.ObjectInfo:
        """The descriptor behind a table tab's Properties side: the
        sections this engine supports, filled for `ref`."""
        return objects.table_properties(
            self.connector,
            ref.name,
            self.property_sections(),
            kind=ref.kind or "table",
        )

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

    def list_grants(self, ref: NodeRef) -> list[PrivilegeInfo]:
        """Who may do what to `ref`.

        A principal node answers with everything that account may do;
        any other node with the grants recorded on that object. Empty
        wherever the `grants` capability is off — SQLite has no
        privilege system to report on.
        """
        if not self.CAPABILITIES.grants:
            return []
        if ref.kind in ("user", "role", "principal"):
            user = self._principal(ref.name)
            if user is None:
                return []
            return _safe(lambda: self.connector.list_privileges(user), [])
        if not ref.name:
            return []
        return _safe(
            lambda: self.connector.list_object_grants(ref.kind, ref.name), []
        )

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
            names = _safe(self.connector.list_databases, [])
            return [
                NodeRef(
                    "database", name, database=name,
                    detail="current" if name == current else "",
                )
                for name in sorted(names, key=lambda n: n != current)
            ]
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

    def _category_children(self, ref: NodeRef) -> list[NodeRef]:
        slug = ref.category or ref.name.lower()
        if slug in ("tables", "views"):
            want_view = slug == "views"
            return [
                ref.child(
                    "view" if want_view else "table", info.name,
                    detail=self.object_detail(info),
                )
                for info in self._objects(ref)
                if (info.kind == "view") == want_view
            ]
        if slug in ("functions", "procedures"):
            return [
                ref.child("function" if slug == "functions" else "procedure",
                          function.name)
                for function in _safe(self.connector.list_functions, [])
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
        return []

    def _objects(self, ref: NodeRef):
        """The tables and views of the node being listed. `ref` is the
        category folder, so an engine with schemas can read the one it
        was opened under (see postgres/metadata.py)."""
        return _safe(self.connector.list_tables, [])

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
            for column in _safe(
                lambda: self.connector.list_columns(ref.name), []
            )
        ]
