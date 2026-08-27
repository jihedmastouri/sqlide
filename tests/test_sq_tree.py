"""The SQLite object tree, node type by node type (SQ-01).

The shape under a connection is connection → object: one file is one
database, so there is neither a database nor a schema level and the
folders PostgreSQL hangs off a schema hang straight off the connection
row. PG-02 and MY-01 did this job for their engines; this follows the
same machinery — the provider's `list_children` for the rows,
`describe` for the info view (CORE-01), `level_categories` for the
folders — rather than inventing a third one.

What is asserted beyond the shape is that the awkward folders are
honest: Sequences is `sqlite_sequence` and is empty where nothing
declared AUTOINCREMENT, Functions is read-only, Data Types is the
storage classes, and SQLite's own `sqlite_*` objects are listed but
marked so the tree can dim them (PG-03).

Everything here runs against a file in tmp_path: no server is
involved.
"""

from __future__ import annotations

import sqlite3

import pytest

from sqlide.backend.db import objects, registry
from sqlide.backend.db.metadata import NodeRef
from sqlide.backend.db.sqlite.connector import SqliteConnector
from sqlide.frontend.sidebar import _LAZY_CATEGORIES

# The tree the ticket draws: the connection's folders, in order.
_CONNECTION_FOLDERS = (
    "Tables", "Views", "Indexes", "Functions", "Sequences",
    "Table Triggers", "Data Types",
)
# The rows under a table: its Properties sections (CORE-05), which is
# the mechanism PG-02 and MY-01 already used for the same list.
_TABLE_SECTIONS = (
    "columns", "keys", "constraints", "foreign_keys", "references",
    "indexes", "triggers",
)

_SEED = (
    "CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT UNIQUE)",
    "CREATE TABLE books ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " title TEXT NOT NULL,"
    " author_id INTEGER REFERENCES authors(id))",
    "CREATE INDEX books_title ON books (title)",
    "CREATE VIEW recent AS SELECT * FROM books",
    "CREATE TRIGGER books_touch AFTER INSERT ON books"
    " BEGIN UPDATE books SET title = trim(NEW.title) WHERE id = NEW.id; END",
    "INSERT INTO books (title) VALUES ('one')",
)


@pytest.fixture()
def sq_tree(tmp_path):
    """The provider, on a file with one of every node type the tree
    can show."""
    path = tmp_path / "sq01.db"
    sqlite3.connect(path).close()  # the adapter refuses missing files
    connector = SqliteConnector(str(path))
    connector.connect()
    for statement in _SEED:
        connector.execute(statement)
    yield registry.create_provider("sqlite", connector), connector
    connector.close()


def _folders(refs) -> list[str]:
    return [ref.name for ref in refs if ref.kind == "category"]


def _folder(provider, label: str) -> NodeRef:
    return next(
        f for f in provider.list_children(provider.root("sqlite"))
        if f.name == label
    )


# The shape, without a connection


def test_the_connection_declares_its_folders() -> None:
    folders = registry.level_categories("sqlite", "connection")
    assert [label for _slug, label in folders] == list(_CONNECTION_FOLDERS)


def test_there_is_no_database_or_schema_level() -> None:
    """One file is one database, so both levels are absent rather than
    empty: the hierarchy says so and nothing hangs off either."""
    assert registry.hierarchy("sqlite") == ("connection", "object")
    assert registry.level_categories("sqlite", "database") == ()
    assert registry.level_categories("sqlite", "schema") == ()
    caps = registry.capabilities("sqlite")
    assert not caps.databases and not caps.schemas


def test_the_missing_features_are_capability_answers() -> None:
    """A file has no accounts, so the grant screens are off rather than
    empty; PRAGMAs are the settings surface instead (SQ-02)."""
    caps = registry.capabilities("sqlite")
    assert not caps.grants and not caps.roles and not caps.permission_editor
    assert not caps.procedures and not caps.events and not caps.extensions
    assert caps.pragmas and caps.constraints and caps.keys
    assert registry.administer_categories("sqlite") == ()


def test_a_table_shows_the_sections_the_ticket_draws() -> None:
    assert registry.property_sections("sqlite") == (
        ("general",) + _TABLE_SECTIONS + ("ddl",)
    )


def test_every_folder_but_the_relations_is_lazy() -> None:
    """No folder costs a query until it is expanded — except Tables and
    Views, which share the listing the connection row already made."""
    for slug, _label in registry.level_categories("sqlite", "connection"):
        if slug in objects.RELATION_FOLDERS:
            assert slug not in _LAZY_CATEGORIES
        else:
            assert slug in _LAZY_CATEGORIES


# The shape, against a file


def test_a_connection_shows_every_folder_and_no_level(sq_tree) -> None:
    provider, _connector = sq_tree
    children = provider.list_children(provider.root("sqlite"))
    assert _folders(children) == list(_CONNECTION_FOLDERS)
    assert not [c for c in children if c.kind in ("database", "schema")]


@pytest.mark.parametrize(
    ("folder", "kind", "member"),
    [
        ("Tables", "table", "authors"),
        ("Views", "view", "recent"),
        ("Indexes", "index", "books_title"),
        ("Functions", "function", "substr"),
        ("Sequences", "sequence", "books"),
        ("Table Triggers", "trigger", "books_touch"),
        ("Data Types", "data_type", "INTEGER"),
    ],
)
def test_a_folder_holds_its_kind_and_every_row_opens(
    sq_tree, folder, kind, member
) -> None:
    provider, _connector = sq_tree
    rows = provider.list_children(_folder(provider, folder))
    found = next(row for row in rows if row.name == member)
    assert found.kind == kind
    # Every node opens an info view, titled as the kind it is (CORE-01).
    info = provider.describe(found)
    assert info.kind == kind
    assert info.type_label == objects.TYPE_LABELS[kind]
    assert info.summary


def test_every_folder_row_opens_too(sq_tree) -> None:
    provider, _connector = sq_tree
    for folder in _folders(provider.list_children(provider.root("sqlite"))):
        info = provider.describe(_folder(provider, folder))
        assert info.type_label == objects.TYPE_LABELS["category"]
        assert info.tables


def test_the_top_level_indexes_are_the_tables_indexes(sq_tree) -> None:
    """Both ways in read the one listing: the folder under a table is
    that table's rows out of the same `list_indexes`."""
    provider, _connector = sq_tree
    everywhere = provider.list_children(_folder(provider, "Indexes"))
    assert "books_title" in [row.name for row in everywhere]
    section = provider.table_properties(NodeRef("table", "books"))
    indexes = next(t for t in section.tables if t.slug == "indexes")
    assert [row[0] for row in indexes.rows] == ["books_title"]


def test_keys_are_the_key_constraints_and_nothing_else(sq_tree) -> None:
    """Keys is a subset of Constraints rather than a second query: the
    primary key and the unique ones, with the foreign keys left to the
    section that is about references."""
    provider, _connector = sq_tree
    info = provider.table_properties(NodeRef("table", "books"))
    keys = next(t for t in info.tables if t.slug == "keys")
    constraints = next(t for t in info.tables if t.slug == "constraints")
    assert {row[1] for row in keys.rows} == {"PRIMARY KEY"}
    assert "FOREIGN KEY" in {row[1] for row in constraints.rows}
    authors = provider.table_properties(NodeRef("table", "authors"))
    unique = next(t for t in authors.tables if t.slug == "keys")
    assert {row[1] for row in unique.rows} == {"PRIMARY KEY", "UNIQUE"}


def test_sequences_are_sqlite_sequence(sq_tree, tmp_path) -> None:
    """"Sequence" here means the AUTOINCREMENT bookkeeping and says so;
    a file with no AUTOINCREMENT column has no such table, and the
    folder is empty rather than an error."""
    provider, _connector = sq_tree
    rows = provider.list_children(_folder(provider, "Sequences"))
    assert [row.name for row in rows] == ["books"]
    assert "AUTOINCREMENT" in rows[0].detail

    path = tmp_path / "plain.db"
    sqlite3.connect(path).close()
    other = SqliteConnector(str(path))
    other.connect()
    other.execute("CREATE TABLE plain (id INTEGER PRIMARY KEY)")
    empty = registry.create_provider("sqlite", other)
    assert empty.list_children(_folder(empty, "Sequences")) == []
    other.close()


def test_functions_are_read_only(sq_tree) -> None:
    """SQLite has no stored functions: the folder lists what the
    library and the process brought, and every row says it cannot be
    edited from here."""
    provider, connector = sq_tree
    rows = provider.list_children(_folder(provider, "Functions"))
    names = [row.name for row in rows]
    assert "substr" in names and "count" in names
    assert all("read-only" in row.detail for row in rows)
    # The sidebar's Edit Definition and Drop… hang off the same answer:
    # a kind this adapter cannot create is a kind it does not offer to.
    assert "function" not in connector.ddl_kinds()


def test_data_types_are_storage_classes_not_a_catalog(sq_tree) -> None:
    """There is no user-defined type catalog to list, so Data Types is
    what SQLite has instead: the storage classes and the affinity the
    familiar spellings map onto."""
    provider, _connector = sq_tree
    rows = provider.list_children(_folder(provider, "Data Types"))
    by_name = {row.name: row.detail for row in rows}
    for storage_class in ("INTEGER", "TEXT", "REAL", "BLOB", "NUMERIC"):
        assert by_name[storage_class] == "storage class"
    assert "affinity" in by_name["VARCHAR"]


def test_sqlite_s_own_objects_are_listed_and_marked(sq_tree) -> None:
    """They are in the one namespace there is, so they are shown rather
    than hidden — dimmed and last, the treatment a system schema gets
    on the engines that have schemas (PG-03)."""
    provider, _connector = sq_tree
    tables = provider.list_children(_folder(provider, "Tables"))
    names = [row.name for row in tables]
    assert "sqlite_sequence" in names
    system = [row.name for row in tables if row.system]
    assert system == ["sqlite_sequence"]
    assert names[-1] == "sqlite_sequence"  # the user's own first
    assert registry.is_system_object("sqlite", "sqlite_stat1")
    assert not registry.is_system_object("sqlite", "books")
    # Dimmed is not disabled: the row still opens like any other.
    assert provider.describe(tables[-1]).summary


def test_an_empty_folder_is_empty_and_not_an_error(tmp_path) -> None:
    """A file with nothing in it shows the same tree with nothing in
    the folders — the shape is a declaration, not a query."""
    path = tmp_path / "empty.db"
    sqlite3.connect(path).close()
    connector = SqliteConnector(str(path))
    connector.connect()
    provider = registry.create_provider("sqlite", connector)
    assert _folders(
        provider.list_children(provider.root("sqlite"))
    ) == list(_CONNECTION_FOLDERS)
    for folder in ("Tables", "Views", "Indexes", "Sequences",
                   "Table Triggers"):
        assert provider.list_children(_folder(provider, folder)) == []
    connector.close()
