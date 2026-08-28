"""Metadata providers: one interface, three engines behind it.

The point of backend/db/metadata.py is that a caller can walk any
engine's object tree without naming one, so the tests walk the tree
through the provider — connection, databases, categories, objects,
columns — and assert every node answered with children or a descriptor.
The same walk runs on SQLite here and against the Postgres and MySQL
fixtures, which is what catches an engine whose provider claims a level
it cannot fill.

Capabilities are asserted without a connection on purpose: the UI reads
them before it opens one.
"""

from __future__ import annotations

import sqlite3

import pytest

from sqlide.backend.db import objects, registry
from sqlide.backend.db.metadata import (
    CAPABILITY_FLAGS,
    Capabilities,
    MetadataProvider,
    NodeRef,
)
from sqlide.backend.db.sqlite.connector import SqliteConnector

# The flags the UI is entitled to ask about (CORE-02).
_REQUIRED_FLAGS = (
    "schemas", "materialized_views", "procedures", "events",
    "grants", "roles", "extensions", "partitions", "pragmas",
)


@pytest.fixture()
def sqlite_db(tmp_path):
    path = tmp_path / "metadata.db"
    sqlite3.connect(path).close()  # the adapter refuses missing files
    connector = SqliteConnector(str(path))
    connector.connect()
    connector.execute(
        "CREATE TABLE notes ("
        " id INTEGER PRIMARY KEY, body TEXT NOT NULL, tag TEXT)"
    )
    connector.execute("CREATE VIEW recent AS SELECT * FROM notes")
    connector.execute("CREATE INDEX notes_body ON notes (body)")
    yield connector
    connector.close()


# Capabilities and hierarchy: answerable with nothing connected.


def test_flags_cover_what_the_ui_asks_about() -> None:
    for flag in _REQUIRED_FLAGS:
        assert flag in CAPABILITY_FLAGS


def test_unknown_flag_is_no_not_an_error() -> None:
    assert Capabilities().supports("time_travel") is False


@pytest.mark.parametrize("kind", registry.KINDS)
def test_every_kind_declares_a_provider(kind: str) -> None:
    caps = registry.capabilities(kind)
    hierarchy = registry.hierarchy(kind)
    assert isinstance(caps, Capabilities)
    assert hierarchy[0] == "connection"
    assert hierarchy[-1] == "object"
    # Nothing may claim a level it did not declare a capability for.
    assert ("schema" in hierarchy) == caps.schemas
    assert ("database" in hierarchy) == caps.databases


def test_engine_shapes() -> None:
    assert registry.hierarchy("postgres") == (
        "connection", "database", "schema", "object",
    )
    assert registry.hierarchy("mysql") == ("connection", "database", "object")
    assert registry.hierarchy("sqlite") == ("connection", "object")


def test_engine_capabilities() -> None:
    postgres = registry.capabilities("postgres")
    mysql = registry.capabilities("mysql")
    sqlite = registry.capabilities("sqlite")
    assert postgres.schemas and postgres.extensions and postgres.partitions
    assert postgres.materialized_views and postgres.grants and postgres.roles
    assert not postgres.events and not postgres.pragmas
    assert mysql.events and mysql.procedures and mysql.account_hosts
    assert not mysql.schemas and not mysql.extensions
    # SQLite's permissions are the file's: no grants, no accounts.
    assert sqlite.pragmas
    assert not sqlite.grants and not sqlite.roles and not sqlite.databases


# The walk.


def _walk(provider: MetadataProvider, ref: NodeRef, depth: int = 0) -> int:
    """Describe `ref`, then everything under it. Returns how many nodes
    the walk reached, so a provider that quietly answers nothing at all
    is a failure rather than a pass."""
    info = provider.describe(ref)
    assert info.type_label, f"{ref.kind} {ref.name} has no type label"
    assert info.summary or info.tables or info.ddl or info.note, (
        f"{ref.kind} {ref.name} renders blank"
    )
    assert isinstance(provider.get_ddl(ref), str)
    assert isinstance(provider.list_grants(ref), list)
    seen = 1
    if depth >= 4:  # connection → database → schema → category → object
        return seen
    for child in provider.list_children(ref):
        assert child.kind, "a child node with no kind"
        seen += _walk(provider, child, depth + 1)
    return seen


def _assert_tree_walks(kind: str, connector, name: str) -> None:
    provider = registry.create_provider(kind, connector)
    root = provider.root(name)
    assert _walk(provider, root) > 1, "the connection had no children"
    assert isinstance(provider.list_principals(), list)


def test_sqlite_tree_walks(sqlite_db) -> None:
    _assert_tree_walks("sqlite", sqlite_db, "metadata.db")


def test_sqlite_has_no_accounts_or_grants(sqlite_db) -> None:
    provider = registry.create_provider("sqlite", sqlite_db)
    assert provider.list_principals() == []
    assert provider.list_grants(NodeRef("table", "notes")) == []


def test_sqlite_categories_skip_what_it_cannot_do(sqlite_db) -> None:
    provider = registry.create_provider("sqlite", sqlite_db)
    slugs = [
        child.category
        for child in provider.list_children(provider.root("metadata.db"))
    ]
    assert "tables" in slugs and "indexes" in slugs
    # No scheduled events and no stored procedures in SQLite.
    assert "events" not in slugs and "procedures" not in slugs


def test_sqlite_columns_are_reached_through_the_tree(sqlite_db) -> None:
    provider = registry.create_provider("sqlite", sqlite_db)
    tables = provider.list_children(
        NodeRef("category", "Tables", category="tables")
    )
    assert [t.name for t in tables] == ["notes"]
    columns = provider.list_children(tables[0])
    assert [c.name for c in columns] == ["id", "body", "tag"]
    assert all(c.table == "notes" for c in columns)


def test_names_from_the_catalog_are_not_concatenated(sqlite_db) -> None:
    """A table whose name carries a quote still resolves — the only way
    it can is if the name travelled as a parameter or a quoted
    identifier rather than as SQL text."""
    sqlite_db.execute('CREATE TABLE "od""d" (id INTEGER PRIMARY KEY)')
    provider = registry.create_provider("sqlite", sqlite_db)
    odd = NodeRef("table", 'od"d')
    assert [c.name for c in provider.list_children(odd)] == ["id"]
    assert provider.describe(odd).summary


def test_postgres_tree_walks(postgres) -> None:
    _version, connector = postgres
    _assert_tree_walks("postgres", connector, "sqlide")


def test_postgres_lists_schema_objects_without_the_search_path(postgres) -> None:
    _version, connector = postgres
    provider = registry.create_provider("postgres", connector)
    children = provider.list_children(NodeRef("database", "sqlide"))
    schemas = [c for c in children if c.kind == "schema"]
    # The schemas come first; the database's own folders — what belongs
    # to it rather than to any schema in it — follow (PG-02).
    assert [c.kind for c in children] == (
        ["schema"] * len(schemas) + ["category"] * (len(children) - len(schemas))
    )
    assert schemas
    public = next(s for s in schemas if s.name == "public")
    tables = provider.list_children(
        public.child("category", "Tables", category="tables")
    )
    assert "users" in [t.name for t in tables]


def test_sqlite_sources_include_views_and_are_unqualified(sqlite_db) -> None:
    provider = registry.create_provider("sqlite", sqlite_db)
    sources = provider.list_sources()
    kinds = {ref.name: ref.kind for ref in sources}
    assert kinds["notes"] == "table"
    # A view is a SELECT source, so the builder gets offered it.
    assert kinds["recent"] == "view"
    # No schema level here: nothing is qualified, and the columns read
    # off the bare name exactly as they always did.
    assert all(not ref.schema for ref in sources)
    columns = provider.columns_of(NodeRef("table", "notes"))
    assert [c.name for c in columns] == ["id", "body", "tag"]


def test_postgres_sources_are_schema_qualified(postgres) -> None:
    _version, connector = postgres
    provider = registry.create_provider("postgres", connector)
    sources = provider.list_sources()
    assert sources
    # Every source names the schema it lives in, and none of them is a
    # schema the server owns.
    assert all(ref.schema for ref in sources)
    assert not any(
        provider.is_system_schema(ref.schema) for ref in sources
    )
    users = next(
        ref for ref in sources if ref.name == "users" and ref.schema == "public"
    )
    assert [c.name for c in provider.columns_of(users)]


def test_postgres_columns_read_the_named_schema(postgres) -> None:
    """Two same-named tables in different schemas answer with their own
    columns, whatever the search path resolves the bare name to."""
    _version, connector = postgres
    provider = registry.create_provider("postgres", connector)
    connector.execute("CREATE SCHEMA IF NOT EXISTS core18")
    connector.execute(
        "CREATE TABLE IF NOT EXISTS core18.users (only_here int)"
    )
    try:
        theirs = provider.columns_of(
            NodeRef("table", "users", schema="core18")
        )
        assert [c.name for c in theirs] == ["only_here"]
        ours = provider.columns_of(NodeRef("table", "users", schema="public"))
        assert [c.name for c in ours] != ["only_here"]
    finally:
        connector.execute("DROP SCHEMA core18 CASCADE")


def test_postgres_reads_principals_and_object_grants(postgres) -> None:
    _version, connector = postgres
    provider = registry.create_provider("postgres", connector)
    assert [u.name for u in provider.list_principals()]
    grants = provider.list_grants(NodeRef("table", "users"))
    assert any(g.privilege == "SELECT" for g in grants)
    # The inverse view: the same grants, keyed by who holds them.
    holders = provider.object_grants(NodeRef("table", "users"))
    assert any(g.privilege == "SELECT" and g.principal for g in holders)
    assert all(g.source for g in holders)
    # An index has no ACL of its own; asking is not an error.
    assert provider.list_grants(NodeRef("index", "users_pkey")) == []
    # The overview (CORE-12): one cell per declared column, per account.
    columns, rows = provider.principal_table()
    assert "Superuser" in columns and rows
    assert all(len(cells) == len(columns) for _user, cells in rows)
    by_name = {user.name: dict(zip(columns, cells)) for user, cells in rows}
    assert by_name["sqlide"]["Login"] == "yes"


def test_mysql_tree_walks(mysql) -> None:
    _version, connector = mysql
    _assert_tree_walks("mysql", connector, "sqlide")


def test_mysql_lists_databases_then_objects(mysql) -> None:
    _version, connector = mysql
    provider = registry.create_provider("mysql", connector)
    children = provider.list_children(provider.root("sqlide"))
    databases = [c for c in children if c.kind == "database"]
    # The databases first, then the folders the connection itself
    # holds — the accounts, Administer, System Info (MY-01).
    assert [c.kind for c in children] == (
        ["database"] * len(databases)
        + ["category"] * (len(children) - len(databases))
    )
    assert databases[0].name == connector.database  # the current one first
    tables = provider.list_children(
        databases[0].child("category", "Tables", category="tables")
    )
    assert "users" in [t.name for t in tables]


def test_mysql_reads_principals_and_object_grants(mysql) -> None:
    _version, connector = mysql
    provider = registry.create_provider("mysql", connector)
    assert [u.name for u in provider.list_principals()]
    grants = provider.list_grants(NodeRef("table", "users"))
    assert isinstance(grants, list)  # the demo account may hold none
    assert provider.list_grants(NodeRef("trigger", "whatever")) == []
    columns, rows = provider.principal_table()
    assert "Host" in columns and rows
    assert all(len(cells) == len(columns) for _user, cells in rows)
    # Every account mysql.user reports has a host and an auth plugin.
    hosts = [dict(zip(columns, cells))["Host"] for _user, cells in rows]
    assert all(hosts)


# Table properties (CORE-04): the sections an engine offers, and what
# they are filled with. The point of the section list is that it is
# capability-driven — a heading an engine has no concept of is never
# offered, so the view omits it instead of drawing it empty.


def _sections(kind: str) -> tuple[str, ...]:
    """The sections `kind` offers, read with nothing connected — the
    list is capability-driven, so it is answerable before a connection
    the same way the flags are."""
    return registry.property_sections(kind)


def test_property_sections_follow_capabilities() -> None:
    postgres = _sections("postgres")
    mysql = _sections("mysql")
    sqlite = _sections("sqlite")
    # Everywhere: the general block, the columns, the keys, the DDL.
    for sections in (postgres, mysql, sqlite):
        for slug in ("general", "columns", "foreign_keys", "references",
                     "indexes", "triggers", "ddl"):
            assert slug in sections
    assert "policies" in postgres and "rules" in postgres
    assert "dependencies" in postgres and "partitions" in postgres
    # MySQL partitions tables but has no policies or rewrite rules.
    assert "partitions" in mysql
    assert "policies" not in mysql and "rules" not in mysql
    # Keys are a section of their own only where the engine asks for
    # one: SQLite does (SQ-01), the server engines list them among the
    # constraints.
    assert "keys" in sqlite
    assert "keys" not in postgres and "keys" not in mysql
    # SQLite has none of the three.
    for slug in ("partitions", "policies", "rules", "dependencies"):
        assert slug not in sqlite
    # Permissions follow the grant model (CORE-11): a file has none.
    assert "permissions" in postgres and "permissions" in mysql
    assert "permissions" not in sqlite
    # Order is the display order, whatever the subset.
    order = [slug for slug, _label in objects.PROPERTY_SECTIONS]
    for sections in (postgres, mysql, sqlite):
        assert list(sections) == [s for s in order if s in sections]


def test_sqlite_table_properties(sqlite_db) -> None:
    sqlite_db.execute(
        "CREATE TABLE tags ("
        " id INTEGER PRIMARY KEY,"
        " label TEXT UNIQUE,"
        " note_id INTEGER REFERENCES notes(id))"
    )
    provider = registry.create_provider("sqlite", sqlite_db)
    info = provider.table_properties(NodeRef("table", "tags"))
    titles = [t.title for t in info.tables]
    assert titles == [
        "Columns", "Keys", "Constraints", "Foreign keys", "References",
        "Indexes", "Triggers",
    ]
    assert ("Primary key", "id") in info.summary
    constraints = next(t for t in info.tables if t.title == "Constraints")
    kinds = {row[1] for row in constraints.rows}
    assert {"PRIMARY KEY", "UNIQUE", "FOREIGN KEY"} <= kinds
    assert "CREATE TABLE" in info.ddl.upper()


def test_properties_rows_link_to_the_child_object(sqlite_db) -> None:
    provider = registry.create_provider("sqlite", sqlite_db)
    info = provider.table_properties(NodeRef("table", "notes"))
    columns = next(t for t in info.tables if t.title == "Columns")
    link = columns.link(0)
    assert link is not None and link.kind == "column"
    assert link.table == "notes"
    # And that link opens: the info view is the same one CORE-01 built.
    assert provider.describe(
        NodeRef("column", link.name, table=link.table)
    ).summary


def test_supported_but_empty_sections_still_render(sqlite_db) -> None:
    """"This engine has no policies" and "this table has none yet" are
    different answers: the second keeps its heading and says so."""
    provider = registry.create_provider("sqlite", sqlite_db)
    info = provider.table_properties(NodeRef("table", "notes"))
    triggers = next(t for t in info.tables if t.title == "Triggers")
    assert triggers.rows == [] and triggers.empty_note


def test_postgres_table_properties(postgres) -> None:
    _version, connector = postgres
    provider = registry.create_provider("postgres", connector)
    info = provider.table_properties(NodeRef("table", "orders"))
    summary = dict(info.summary)
    assert summary["Owner"] and summary["Size"]
    constraints = next(t for t in info.tables if t.title == "Constraints")
    assert {row[1] for row in constraints.rows} >= {
        "PRIMARY KEY", "FOREIGN KEY"
    }
    keys = next(t for t in info.tables if t.title == "Foreign keys")
    assert ("user_id", "users.id") in keys.rows
    # The view built on orders is what depends on it.
    dependencies = next(t for t in info.tables if t.title == "Dependencies")
    assert "big_orders" in [row[0] for row in dependencies.rows]
    # Postgres-only sections are offered even when the table has none.
    assert [t.title for t in info.tables].count("Policies") == 1


def test_postgres_references_are_the_other_direction(postgres) -> None:
    _version, connector = postgres
    provider = registry.create_provider("postgres", connector)
    info = provider.table_properties(NodeRef("table", "users"))
    references = next(t for t in info.tables if t.title == "References")
    assert ("orders", "user_id", "users.id") in references.rows


def test_mysql_table_properties(mysql) -> None:
    _version, connector = mysql
    provider = registry.create_provider("mysql", connector)
    info = provider.table_properties(NodeRef("table", "orders"))
    summary = dict(info.summary)
    assert summary["Storage engine"] and summary["Size"]
    constraints = next(t for t in info.tables if t.title == "Constraints")
    assert {row[1] for row in constraints.rows} >= {
        "PRIMARY KEY", "FOREIGN KEY"
    }
    titles = [t.title for t in info.tables]
    assert "Partitions" in titles
    # MySQL has neither, so neither heading is drawn.
    assert "Policies" not in titles and "Rules" not in titles


# Naming: how much of an object's address is worth showing (PG-01).


def test_node_ref_path_is_the_engine_shape() -> None:
    """A node's address has exactly the levels its engine has: no
    empty step stands in for a level the engine does not have."""
    postgres = NodeRef("table", "orders", database="sales", schema="staging")
    assert postgres.path == ("sales", "staging", "orders")
    mysql = NodeRef("table", "orders", database="sales")
    assert mysql.path == ("sales", "orders")
    assert NodeRef("table", "notes").path == ("notes",)
    assert NodeRef("database", "sales", database="sales").path == ("sales",)


def test_sqlite_names_are_never_qualified(sqlite_db) -> None:
    """No phantom schema level: SQLite has none, so a name the caller
    hands over comes back exactly as it went in — even if the ref
    somehow carries a schema."""
    provider = registry.create_provider("sqlite", sqlite_db)
    ref = NodeRef("table", "notes", schema="main")
    assert provider.qualified_name(ref) == "notes"
    assert provider.quoted_name(ref) == '"notes"'
    assert provider.schema_of(ref) == ""
    assert provider.describe(ref).name == "notes"


def test_qualification_stops_where_the_object_is_not_named_by_a_schema(
) -> None:
    """Only the kinds addressed *through* a schema get one in front of
    them: a schema names itself, and a column is named through the
    table above it."""
    from sqlide.backend.db.postgres.metadata import PostgresMetadata

    provider = PostgresMetadata(connector=None)
    assert provider.qualified_name(
        NodeRef("table", "orders", schema="staging")
    ) == "staging.orders"
    assert provider.qualified_name(
        NodeRef("view", "recent", schema="staging")
    ) == "staging.recent"
    for kind in ("database", "schema", "category", "column", "index"):
        ref = NodeRef(kind, "thing", schema="staging", table="orders")
        assert provider.schema_of(ref) == "", kind
        assert provider.qualified_name(ref) == "thing"


def test_mysql_qualifies_nothing_by_schema() -> None:
    """In MySQL a schema *is* a database, so the schema level is off
    and a name is never prefixed by one."""
    from sqlide.backend.db.mysql.metadata import MysqlMetadata

    provider = MysqlMetadata(connector=None)
    ref = NodeRef("table", "orders", database="sales", schema="sales")
    assert provider.qualified_name(ref) == "orders"
    assert provider.schema_of(ref) == ""
