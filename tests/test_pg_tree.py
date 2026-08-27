"""The PostgreSQL object tree, node type by node type (PG-02).

The shape under a connection is server → databases → schemas → objects,
with folders hanging off each level: a schema holds its relations,
routines and declarations; a database holds what belongs to it rather
than to any schema in it; the connection holds Administer and System
Info. What is asserted here is that the tree has exactly those folders,
that every one of them resolves through the shared machinery — the
provider's `list_children` for the rows, `describe` for the info view
(CORE-01) — and that a special case of a kind says which it is instead
of hiding among the plain ones.

The structural half needs no server: which folders a level shows is a
capability answer (registry.level_categories). The half that needs one
runs against the `postgres` fixture, on a schema seeded here with one
of every node type, and drops it again.
"""

from __future__ import annotations

import pytest

from sqlide.backend.db import objects, registry
from sqlide.backend.db.metadata import NodeRef
from sqlide.frontend.sidebar import (
    _LAZY_CATEGORIES,
    Node,
    _category_rows,
    _relation_kind,
)

# The tree the ticket draws, level by level.
_CONNECTION_FOLDERS = ("Administer", "System Info")
_DATABASE_FOLDERS = (
    # Available Extensions is the other half of Extensions: what the
    # server could install and has not (PG-05).
    "Event Triggers", "Extensions", "Available Extensions",
    "Storage", "System Info", "Roles",
)
_SCHEMA_FOLDERS = (
    "Tables", "Foreign Tables", "Views", "Materialized Views", "Indexes",
    "Functions", "Sequences", "Data Types", "Aggregate Functions",
)

# One schema with a row for every folder the tree can show.
_SCHEMA = "pg02"
_SEED = (
    f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE",
    f"CREATE SCHEMA {_SCHEMA}",
    f"CREATE TABLE {_SCHEMA}.plain (id integer PRIMARY KEY)",
    f"CREATE TABLE {_SCHEMA}.events (id integer, at date)"
    " PARTITION BY RANGE (at)",
    f"CREATE TABLE {_SCHEMA}.events_2024 PARTITION OF {_SCHEMA}.events"
    " FOR VALUES FROM ('2024-01-01') TO ('2025-01-01')",
    f"CREATE VIEW {_SCHEMA}.plain_view AS SELECT * FROM {_SCHEMA}.plain",
    f"CREATE MATERIALIZED VIEW {_SCHEMA}.snapshot AS"
    f" SELECT * FROM {_SCHEMA}.plain",
    f"CREATE SEQUENCE {_SCHEMA}.counter",
    f"CREATE TYPE {_SCHEMA}.mood AS ENUM ('ok', 'bad')",
    f"CREATE AGGREGATE {_SCHEMA}.total (integer)"
    " (sfunc = int4pl, stype = int4, initcond = '0')",
)


@pytest.fixture()
def pg_tree(postgres):
    """The provider, on a connection pinned to a schema seeded with one
    of every node type."""
    _version, connector = postgres
    for statement in _SEED:
        connector.execute(statement)
    connector.set_search_path(_SCHEMA)
    provider = registry.create_provider("postgres", connector)
    yield provider
    connector.set_search_path("public")
    connector.execute(f"DROP SCHEMA {_SCHEMA} CASCADE")


def _folders(refs) -> list[str]:
    return [ref.name for ref in refs if ref.kind == "category"]


def _folder(provider, ref: NodeRef, label: str) -> NodeRef:
    return next(f for f in provider.list_children(ref) if f.name == label)


# The shape, without a server


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        ("connection", _CONNECTION_FOLDERS),
        ("database", _DATABASE_FOLDERS),
        ("schema", _SCHEMA_FOLDERS),
    ],
)
def test_each_level_declares_its_folders(level, expected) -> None:
    folders = registry.level_categories("postgres", level)
    assert [label for _slug, label in folders] == list(expected)


def test_administer_holds_the_server_wide_listings() -> None:
    assert [
        label for _slug, label in registry.administer_categories("postgres")
    ] == ["Roles", "Storage", "System Info"]


@pytest.mark.parametrize("kind", ["jdbc"])
def test_an_engine_without_these_folders_declares_none(kind) -> None:
    # The generic Tables/Views/Functions set is not a declaration: an
    # engine that has nothing extra — JDBC, which falls back to the
    # generic provider — keeps what the tree always did. (SQLite
    # declares its own folders since SQ-01.)
    assert registry.level_categories(kind, "schema") == ()
    assert registry.level_categories(kind, "connection") == ()


def test_an_engine_without_schemas_declares_nothing_at_that_level() -> None:
    """MySQL declares folders of its own (MY-01), but not at a level it
    does not have: a schema is a database there."""
    assert registry.level_categories("mysql", "schema") == ()


def test_every_catalog_folder_is_lazy() -> None:
    """No folder costs a query until it is expanded — except the
    relation folders, which share the listing the row above made, and
    Administer, which holds folders rather than objects."""
    for slug, _label in registry.level_categories("postgres", "schema") + (
        registry.level_categories("postgres", "database")
    ):
        if slug in objects.RELATION_FOLDERS or slug == "administer":
            assert slug not in _LAZY_CATEGORIES
        else:
            assert slug in _LAZY_CATEGORIES


def test_a_relation_folder_takes_only_its_own_relations() -> None:
    """One listing feeds four folders, so each takes the rows whose
    note puts them in it — and a partition is in none of them: it
    belongs under the table it is part of."""
    from sqlide.backend.db.base import TableInfo

    listing = [
        TableInfo("plain", "table"),
        TableInfo("events", "table", detail="partitioned"),
        TableInfo("events_2024", "table", detail="partition"),
        TableInfo("remote", "table", detail="foreign"),
        TableInfo("plain_view", "view"),
        TableInfo("snapshot", "view", detail="materialized"),
    ]
    found = {
        slug: [
            info.name
            for info in _category_rows(
                Node("category", slug, category=slug, payload=listing)
            )
        ]
        for slug in objects.RELATION_FOLDERS
    }
    assert found == {
        "tables": ["plain", "events"],
        "foreign_tables": ["remote"],
        "views": ["plain_view"],
        "materialized_views": ["snapshot"],
    }
    assert _relation_kind("materialized_views") == "view"
    assert _relation_kind("foreign_tables") == "table"


# The shape, against a server


def test_a_schema_shows_every_folder(pg_tree) -> None:
    schema = NodeRef("schema", _SCHEMA, schema=_SCHEMA)
    assert _folders(pg_tree.list_children(schema)) == list(_SCHEMA_FOLDERS)


def test_a_database_shows_its_schemas_and_then_its_own_folders(
    pg_tree,
) -> None:
    children = pg_tree.list_children(NodeRef("database", "sqlide"))
    assert _SCHEMA in [c.name for c in children if c.kind == "schema"]
    assert _folders(children) == list(_DATABASE_FOLDERS)


def test_a_connection_shows_its_databases_and_then_the_server(
    pg_tree,
) -> None:
    children = pg_tree.list_children(NodeRef("connection", "pg"))
    assert [c.name for c in children if c.kind == "database"]
    assert _folders(children) == list(_CONNECTION_FOLDERS)
    administer = _folder(pg_tree, NodeRef("connection", "pg"), "Administer")
    assert _folders(pg_tree.list_children(administer)) == [
        "Roles", "Storage", "System Info",
    ]


@pytest.mark.parametrize(
    ("folder", "kind", "member"),
    [
        ("Tables", "table", "plain"),
        ("Views", "view", "plain_view"),
        ("Materialized Views", "view", "snapshot"),
        ("Sequences", "sequence", "counter"),
        ("Data Types", "data_type", "mood"),
        ("Aggregate Functions", "aggregate", "total"),
    ],
)
def test_a_schema_folder_holds_its_kind(pg_tree, folder, kind, member) -> None:
    schema = NodeRef("schema", _SCHEMA, schema=_SCHEMA)
    rows = pg_tree.list_children(_folder(pg_tree, schema, folder))
    found = next(row for row in rows if row.name == member)
    assert found.kind == kind
    # Every node opens an info view, titled as the kind it is (CORE-01).
    info = pg_tree.describe(found)
    assert info.kind == kind
    assert info.type_label == objects.TYPE_LABELS[kind]
    assert info.summary


@pytest.mark.parametrize(
    ("folder", "kind"),
    [
        ("Extensions", "extension"),
        ("Storage", "tablespace"),
        ("System Info", "setting"),
        ("Roles", "principal"),
    ],
)
def test_a_database_folder_holds_its_kind(pg_tree, folder, kind) -> None:
    database = NodeRef("database", "sqlide")
    rows = pg_tree.list_children(_folder(pg_tree, database, folder))
    assert rows, f"{folder} is empty on a live server"
    assert {row.kind for row in rows} == {kind}
    assert pg_tree.describe(rows[0]).summary


def test_an_empty_folder_is_empty_and_not_an_error(pg_tree) -> None:
    """Event triggers need a superuser to create and the seed has
    none, so the folder is the empty case: no rows, no exception, and
    a folder view that says so rather than spinning."""
    database = NodeRef("database", "sqlide")
    folder = _folder(pg_tree, database, "Event Triggers")
    assert pg_tree.list_children(folder) == []
    info = pg_tree.describe(folder)
    assert info.tables and info.tables[0].rows == []
    assert info.tables[0].empty_note


def test_a_partitioned_table_says_so_and_nests_its_partitions(
    pg_tree,
) -> None:
    schema = NodeRef("schema", _SCHEMA, schema=_SCHEMA)
    tables = pg_tree.list_children(_folder(pg_tree, schema, "Tables"))
    names = {row.name: row for row in tables}
    # The partitioned table is in Tables, marked; its partition is not
    # beside it — it hangs under the table it is part of.
    assert names["events"].detail == "partitioned"
    assert names["plain"].detail == ""
    assert "events_2024" not in names
    children = pg_tree.list_children(names["events"])
    partitions = [c for c in children if c.kind == "table"]
    assert [p.name for p in partitions] == ["events_2024"]
    # Columns still come first: a partitioned table is a table.
    assert [c.name for c in children if c.kind == "column"] == ["id", "at"]
    assert pg_tree.describe(partitions[0]).type_label == "Table"


def test_a_table_still_opens_every_properties_section(pg_tree) -> None:
    """The rows under a table are its Properties sections (CORE-05),
    and PostgreSQL has all of the ones the ticket draws."""
    sections = registry.property_sections("postgres")
    for slug in (
        "columns", "constraints", "foreign_keys", "references", "indexes",
        "triggers", "partitions", "rules", "policies", "dependencies",
    ):
        assert slug in sections


def test_names_are_schema_qualified_wherever_they_are_read_back(
    pg_tree,
) -> None:
    schema = NodeRef("schema", _SCHEMA, schema=_SCHEMA)
    rows = pg_tree.list_children(_folder(pg_tree, schema, "Sequences"))
    assert pg_tree.describe(rows[0]).name == f"{_SCHEMA}.counter"
