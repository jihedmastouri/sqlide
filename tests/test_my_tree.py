"""The MySQL object tree, node type by node type (MY-01).

The shape under a connection is server → databases → objects: a schema
*is* a database here, so there is no schema level and the folders that
PostgreSQL hangs off a schema hang off a database instead (PG-02 did
the same job for that engine; this ticket follows it rather than
inventing a second mechanism). What is asserted here is that the tree
has exactly those folders, that every one of them resolves through the
shared machinery — the provider's `list_children` for the rows,
`describe` for the info view (CORE-01) — and that the server's own
databases are present, named as the server's, and sorted last (PG-03).

The structural half needs no server: which folders a level shows is a
capability answer (registry.level_categories). The half that needs one
runs against the `mysql` fixture, on a database seeded here with one of
every node type, and drops what it made again. Differences between 5.7
and 8.x are meant to cost a row rather than raise, so nothing here
branches on the version.
"""

from __future__ import annotations

import pytest

from sqlide.backend.db import objects, registry
from sqlide.backend.db.base import ConnectorError
from sqlide.backend.db.metadata import NodeRef
from sqlide.frontend.sidebar import _LAZY_CATEGORIES

# The tree the ticket draws, level by level.
_CONNECTION_FOLDERS = ("Users", "Administer", "System Info")
_DATABASE_FOLDERS = (
    # Functions sit beside Procedures: MySQL has both, and the ticket's
    # sketch names only one of them.
    "Tables", "Views", "Indexes", "Procedures", "Functions",
    "Triggers", "Events",
)
# The rows under a table: its Properties sections (CORE-05), which is
# the mechanism PG-02 already used for the same list.
_TABLE_SECTIONS = (
    "columns", "constraints", "foreign_keys", "references",
    "indexes", "triggers", "partitions",
)

_SEED = (
    "DROP PROCEDURE IF EXISTS my01_count",
    "DROP TRIGGER IF EXISTS my01_touch",
    "DROP TABLE IF EXISTS my01_parts",
    "DROP TABLE IF EXISTS my01_plain",
    "CREATE TABLE my01_plain (id integer PRIMARY KEY, name varchar(40))",
    "CREATE INDEX my01_name ON my01_plain (name)",
    "CREATE TABLE my01_parts (id integer, at date, PRIMARY KEY (id, at))"
    " PARTITION BY RANGE (YEAR(at)) ("
    " PARTITION my01_p2024 VALUES LESS THAN (2025),"
    " PARTITION my01_rest VALUES LESS THAN MAXVALUE)",
    "CREATE PROCEDURE my01_count() SELECT count(*) FROM my01_plain",
    "CREATE TRIGGER my01_touch BEFORE INSERT ON my01_plain"
    " FOR EACH ROW SET NEW.name = TRIM(NEW.name)",
)
_DROP = (
    "DROP TRIGGER IF EXISTS my01_touch",
    "DROP PROCEDURE IF EXISTS my01_count",
    "DROP TABLE IF EXISTS my01_parts",
    "DROP TABLE IF EXISTS my01_plain",
)


@pytest.fixture()
def my_tree(mysql):
    """The provider, on a connection whose database has one of every
    node type the tree can show."""
    _version, connector = mysql
    for statement in _SEED:
        connector.execute(statement)
    provider = registry.create_provider("mysql", connector)
    yield provider
    for statement in _DROP:
        connector.execute(statement)


def _folders(refs) -> list[str]:
    return [ref.name for ref in refs if ref.kind == "category"]


def _folder(provider, ref: NodeRef, label: str) -> NodeRef:
    return next(f for f in provider.list_children(ref) if f.name == label)


def _database(connector) -> NodeRef:
    return NodeRef("database", connector.database, database=connector.database)


# The shape, without a server


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        ("connection", _CONNECTION_FOLDERS),
        ("database", _DATABASE_FOLDERS),
    ],
)
def test_each_level_declares_its_folders(level, expected) -> None:
    folders = registry.level_categories("mysql", level)
    assert [label for _slug, label in folders] == list(expected)


def test_there_is_no_schema_level() -> None:
    """A schema is a database here, so the level is absent rather than
    empty: the hierarchy says so and nothing hangs off it."""
    assert registry.hierarchy("mysql") == ("connection", "database", "object")
    assert registry.level_categories("mysql", "schema") == ()


def test_administer_holds_the_server_wide_listings() -> None:
    assert [
        label for _slug, label in registry.administer_categories("mysql")
    ] == ["Users", "System Info"]


def test_every_folder_the_tree_shows_is_lazy() -> None:
    """No folder costs a query until it is expanded — except the
    relation folders, which share the listing the row above made, and
    Administer, which holds folders rather than objects."""
    declared = registry.level_categories(
        "mysql", "connection"
    ) + registry.level_categories("mysql", "database")
    for slug, _label in declared:
        if slug in objects.RELATION_FOLDERS or slug == "administer":
            assert slug not in _LAZY_CATEGORIES
        else:
            assert slug in _LAZY_CATEGORIES


def test_a_five_seven_server_loses_a_section_and_not_the_tree() -> None:
    """Roles arrived in 8.0 and the tree does not ask for a role
    catalog: the folders are declared from capability flags, so a 5.7
    server shows the same tree with shorter listings in it."""
    caps = registry.capabilities("mysql")
    assert caps.databases and caps.events and caps.procedures
    assert not caps.schemas  # a schema is a database
    assert not caps.extensions and not caps.materialized_views


# The shape, against a server


def test_a_database_shows_every_folder(my_tree, mysql) -> None:
    _version, connector = mysql
    children = my_tree.list_children(_database(connector))
    assert _folders(children) == list(_DATABASE_FOLDERS)


def test_a_connection_shows_its_databases_and_then_the_server(
    my_tree, mysql
) -> None:
    _version, connector = mysql
    children = my_tree.list_children(my_tree.root("mysql"))
    assert connector.database in [
        c.name for c in children if c.kind == "database"
    ]
    assert _folders(children) == list(_CONNECTION_FOLDERS)
    administer = _folder(my_tree, my_tree.root("mysql"), "Administer")
    assert _folders(my_tree.list_children(administer)) == [
        "Users", "System Info",
    ]


def test_the_servers_own_databases_are_present_dimmed_and_last(
    my_tree, mysql
) -> None:
    """They belong in the tree — they are worth reading — but they are
    never what someone came for (PG-03)."""
    _version, connector = mysql
    databases = [
        c for c in my_tree.list_children(my_tree.root("mysql"))
        if c.kind == "database"
    ]
    names = [d.name for d in databases]
    assert "information_schema" in names
    assert names[0] == connector.database  # the current one first
    # Which of the four the account can see is its own business —
    # `mysql` needs a privilege the demo user does not hold.
    system = [d.name for d in databases if d.system]
    assert "information_schema" in system
    assert names[-len(system):] == system  # and the server's own last
    # The switcher still leaves them out: dimming is the tree's job.
    assert "information_schema" not in connector.list_databases()


@pytest.mark.parametrize(
    ("folder", "kind", "member"),
    [
        ("Tables", "table", "my01_plain"),
        ("Views", "view", "big_orders"),
        ("Indexes", "index", "my01_name"),
        ("Procedures", "procedure", "my01_count"),
        ("Functions", "function", "add_amounts"),
        ("Triggers", "trigger", "my01_touch"),
    ],
)
def test_a_database_folder_holds_its_kind(
    my_tree, mysql, folder, kind, member
) -> None:
    _version, connector = mysql
    rows = my_tree.list_children(
        _folder(my_tree, _database(connector), folder)
    )
    found = next(row for row in rows if row.name == member)
    assert found.kind == kind
    # Every node opens an info view, titled as the kind it is (CORE-01).
    info = my_tree.describe(found)
    assert info.kind == kind
    assert info.type_label == objects.TYPE_LABELS[kind]
    assert info.summary


def test_procedures_and_functions_are_two_folders(my_tree, mysql) -> None:
    """MySQL has both, and a routine belongs to one folder only."""
    _version, connector = mysql
    database = _database(connector)
    procedures = [
        r.name
        for r in my_tree.list_children(_folder(my_tree, database, "Procedures"))
    ]
    functions = [
        r.name
        for r in my_tree.list_children(_folder(my_tree, database, "Functions"))
    ]
    assert "my01_count" in procedures and "my01_count" not in functions
    assert "add_amounts" in functions and "add_amounts" not in procedures


@pytest.mark.parametrize(
    ("folder", "kind"),
    [
        ("Users", "principal"),
        ("System Info", "setting"),
    ],
)
def test_a_connection_folder_holds_its_kind(my_tree, folder, kind) -> None:
    rows = my_tree.list_children(
        _folder(my_tree, my_tree.root("mysql"), folder)
    )
    assert rows, f"{folder} is empty on a live server"
    assert {row.kind for row in rows} == {kind}
    assert my_tree.describe(rows[0]).summary


def test_an_empty_folder_is_empty_and_not_an_error(my_tree, mysql) -> None:
    """Creating an event needs the EVENT privilege and the seed has
    none, so Events is the empty case: no rows, no exception, and a
    folder view that says so rather than spinning."""
    _version, connector = mysql
    folder = _folder(my_tree, _database(connector), "Events")
    try:
        connector.execute("DROP EVENT IF EXISTS my01_evt")
    except ConnectorError:
        pass  # no EVENT privilege: the folder is empty either way
    assert my_tree.list_children(folder) == []
    info = my_tree.describe(folder)
    assert info.tables and info.tables[0].rows == []
    assert info.tables[0].empty_note


def test_a_table_opens_every_section_the_ticket_draws(my_tree) -> None:
    """The rows under a table are its Properties sections (CORE-05),
    and MySQL has all of the ones the ticket draws — the ones it does
    not have (rules, policies) are left out rather than drawn empty."""
    sections = registry.property_sections("mysql")
    for slug in _TABLE_SECTIONS:
        assert slug in sections
    for slug in ("rules", "policies", "dependencies"):
        assert slug not in sections


def test_a_partitioned_table_lists_its_partitions(my_tree, mysql) -> None:
    _version, connector = mysql
    rows = my_tree.list_children(
        _folder(my_tree, _database(connector), "Tables")
    )
    assert "my01_parts" in [r.name for r in rows]
    # A partition is not a table of its own here — MySQL keeps them
    # inside the table — so they arrive through the Partitions section.
    partitions = connector.list_partitions("my01_parts")
    assert [p.name for p in partitions] == ["my01_p2024", "my01_rest"]
    assert connector.list_partitions("my01_plain") == []


def test_every_node_resolves_to_an_info_view(my_tree, mysql) -> None:
    """The contract the whole tree rests on: a node never opens a blank
    screen, whatever kind it is (CORE-01)."""
    _version, connector = mysql
    walked = 0
    for level in (my_tree.root("mysql"), _database(connector)):
        for folder in my_tree.list_children(level):
            info = my_tree.describe(folder)
            assert info.type_label
            assert info.summary or info.tables
            walked += 1
    assert walked > len(_DATABASE_FOLDERS)
