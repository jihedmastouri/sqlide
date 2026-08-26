"""Object descriptors: every sidebar node resolves to a view.

The point of backend/db/objects.py is that nothing in the tree is a
dead end, so the tests walk the whole tree — connection, database,
every category, every object in it and every column under a table —
and assert each node came back with something to render. The walk runs
on a live SQLite database here and against the Postgres and MySQL
fixtures in the same shape, so an adapter that cannot answer one of the
optional catalog calls is caught rather than silently rendering blanks.
"""

from __future__ import annotations

import sqlite3

import pytest

from sqlide.backend.db import objects
from sqlide.backend.db.base import Connector, ConnectorError
from sqlide.backend.db.sqlite.connector import SqliteConnector

_CATEGORIES = ("Tables", "Views", "Functions", "Indexes", "Triggers", "Events")


@pytest.fixture()
def sqlite_db(tmp_path):
    path = tmp_path / "objects.db"
    sqlite3.connect(path).close()  # the adapter refuses missing files
    connector = SqliteConnector(str(path))
    connector.connect()
    connector.execute(
        "CREATE TABLE notes ("
        " id INTEGER PRIMARY KEY, body TEXT NOT NULL, tag TEXT)"
    )
    connector.execute("CREATE VIEW recent AS SELECT * FROM notes")
    connector.execute("CREATE INDEX notes_body ON notes (body)")
    connector.execute(
        "CREATE TRIGGER notes_touch AFTER INSERT ON notes "
        "BEGIN SELECT 1; END"
    )
    yield connector
    connector.close()


def _walk(connector: Connector, name: str) -> list[objects.ObjectInfo]:
    """Every node of the tree this connection would show, described.

    Mirrors the sidebar's own shape (frontend/sidebar.py): connection →
    databases → categories → objects → columns.
    """
    seen = [objects.describe(connector, "connection", name)]
    for database in connector.list_databases() or [name]:
        seen.append(objects.describe(connector, "database", database))
    for category in _CATEGORIES:
        info = objects.describe(
            connector, "category", category, category=category.lower()
        )
        seen.append(info)
        detail = info.tables[0]
        for index in range(len(detail.rows)):
            link = detail.link(index)
            assert link is not None, f"{category} row {index} opens nothing"
            child = objects.describe(
                connector, link.kind, link.name, table=link.table
            )
            seen.append(child)
            if link.kind in ("table", "view"):
                for column in connector.list_columns(link.name):
                    seen.append(objects.describe(
                        connector, "column", column.name, table=link.name
                    ))
    return seen


def _assert_every_node_renders(connector: Connector, name: str) -> None:
    for info in _walk(connector, name):
        assert info.name
        assert info.type_label
        # Something to show: a summary, a detail table, DDL or a note.
        assert info.summary or info.tables or info.ddl or info.note


def test_sqlite_tree_resolves(sqlite_db: Connector) -> None:
    _assert_every_node_renders(sqlite_db, "notes.db")


def test_postgres_tree_resolves(postgres) -> None:
    _version, connector = postgres
    _assert_every_node_renders(connector, "sqlide")


def test_mysql_tree_resolves(mysql) -> None:
    _version, connector = mysql
    _assert_every_node_renders(connector, "sqlide")


def test_table_lists_its_columns_indexes_and_ddl(sqlite_db: Connector) -> None:
    info = objects.describe(sqlite_db, "table", "notes")
    assert info.type_label == "Table"
    assert dict(info.summary)["Primary key"] == "id"
    columns = info.tables[0]
    assert columns.title == "Columns"
    assert [row[0] for row in columns.rows] == ["id", "body", "tag"]
    assert ("body", "TEXT", "no", "") in columns.rows
    indexes = [t for t in info.tables if t.title == "Indexes"]
    assert [row[0] for row in indexes[0].rows] == ["notes_body"]
    assert "CREATE TABLE" in info.ddl.upper()


def test_table_ddl_matches_the_server(sqlite_db: Connector) -> None:
    info = objects.describe(sqlite_db, "table", "notes")
    assert info.ddl == sqlite_db.get_ddl("notes")


def test_column_reads_its_own_row(sqlite_db: Connector) -> None:
    info = objects.describe(sqlite_db, "column", "body", table="notes")
    summary = dict(info.summary)
    assert summary["Table"] == "notes"
    assert summary["Nullable"] == "no"
    assert summary["Primary key"] == "no"


def test_index_reports_its_table_and_definition(sqlite_db: Connector) -> None:
    info = objects.describe(sqlite_db, "index", "notes_body", table="notes")
    assert dict(info.summary)["Table"] == "notes"
    assert "notes_body" in info.ddl
    assert not info.note


def test_trigger_reports_timing_and_event(sqlite_db: Connector) -> None:
    info = objects.describe(sqlite_db, "trigger", "notes_touch")
    summary = dict(info.summary)
    assert summary["Table"] == "notes"
    assert summary["Timing"] == "AFTER"
    assert summary["Event"] == "INSERT"


def test_category_rows_link_to_their_children(sqlite_db: Connector) -> None:
    info = objects.describe(
        sqlite_db, "category", "Indexes", category="indexes"
    )
    detail = info.tables[0]
    assert detail.columns == ["Name", "Table", "Definition"]
    assert detail.rows[0][0] == "notes_body"
    assert detail.link(0) == objects.ObjectRef("index", "notes_body", "notes")


def test_empty_category_still_renders(sqlite_db: Connector) -> None:
    info = objects.describe(
        sqlite_db, "category", "Events", category="events"
    )
    assert info.tables[0].rows == []
    assert dict(info.summary)["Count"] == "0"


def test_unknown_kind_falls_back_to_the_generic_view(
    sqlite_db: Connector,
) -> None:
    info = objects.describe(sqlite_db, "sequence", "notes", path="db ▸ notes")
    assert info.kind == "sequence"
    assert info.type_label == "Sequence"
    assert info.path == "db ▸ notes"
    assert dict(info.summary)["Kind"] == "sequence"
    assert info.note  # says why it is generic
    assert "CREATE TABLE" in info.ddl.upper()  # whatever the catalog knew


def test_missing_object_is_reported_not_raised(sqlite_db: Connector) -> None:
    info = objects.describe(sqlite_db, "index", "nope")
    assert info.note
    assert info.summary


def test_a_failing_catalog_costs_only_its_section() -> None:
    class Broken(SqliteConnector):
        def list_indexes(self):
            raise ConnectorError("no index catalog here")

    described = objects.describe(
        Broken("unused.db"), "category", "Indexes", category="indexes"
    )
    assert described.tables[0].rows == []


def test_path_is_carried_through(sqlite_db: Connector) -> None:
    info = objects.describe(
        sqlite_db, "table", "notes", path="local ▸ Tables ▸ notes"
    )
    assert info.path == "local ▸ Tables ▸ notes"
