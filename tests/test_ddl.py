"""Create/drop DDL surface: the pure string builders per adapter (no
server needed) and a live SQLite round-trip on a temp-file database."""

from __future__ import annotations

import sqlite3

import pytest

from sqlide.backend.db.base import ColumnInfo, Connector, ConnectorError
from sqlide.backend.db.mysql.connector import MysqlConnector
from sqlide.backend.db.postgres.connector import PostgresConnector
from sqlide.backend.db.sqlite.connector import SqliteConnector

_SERVER = {"host": "", "port": 0, "user": "", "password": "", "database": ""}


def _sqlite() -> SqliteConnector:
    return SqliteConnector("unused.db")


def _mysql() -> MysqlConnector:
    return MysqlConnector(**_SERVER)


def _postgres() -> PostgresConnector:
    return PostgresConnector(**_SERVER)


# drop_sql (pure paths; the Postgres trigger/function paths query the
# catalog and are covered by the live tests in test_postgres.py)


def test_drop_sql_defaults():
    db = _sqlite()
    assert db.drop_sql("table", "users") == 'DROP TABLE "users"'
    assert db.drop_sql("view", "v") == 'DROP VIEW "v"'
    assert db.drop_sql("index", "i") == 'DROP INDEX "i"'
    assert db.drop_sql("trigger", "t") == 'DROP TRIGGER "t"'


def test_drop_sql_rejects_unknown_kind():
    with pytest.raises(ConnectorError, match="Unknown object kind"):
        _sqlite().drop_sql("nope", "x")


def test_mysql_drop_index_needs_owning_table():
    db = _mysql()
    assert (
        db.drop_sql("index", "idx", table="users")
        == "DROP INDEX `idx` ON `users`"
    )
    with pytest.raises(ConnectorError, match="owning table"):
        db.drop_sql("index", "idx")


def test_mysql_drop_routines_and_events():
    db = _mysql()
    assert db.drop_sql("function", "f") == "DROP FUNCTION `f`"
    assert db.drop_sql("procedure", "p") == "DROP PROCEDURE `p`"
    assert db.drop_sql("event", "e") == "DROP EVENT `e`"


def test_postgres_cascade():
    db = _postgres()
    assert db.supports_drop_cascade
    assert (
        db.drop_sql("table", "users", cascade=True)
        == 'DROP TABLE "users" CASCADE'
    )
    assert db.drop_sql("table", "users") == 'DROP TABLE "users"'
    assert not _sqlite().supports_drop_cascade
    assert not _mysql().supports_drop_cascade


# ddl_kinds / templates / column types


def test_ddl_kinds_per_dialect():
    assert _sqlite().ddl_kinds() == ("table", "view", "index", "trigger")
    assert "event" in _mysql().ddl_kinds()
    kinds = _postgres().ddl_kinds()
    assert "function" in kinds and "event" not in kinds


def test_templates_cover_advertised_kinds():
    for db in (_sqlite(), _mysql(), _postgres()):
        for kind in db.ddl_kinds():
            template = db.create_template(kind)
            assert "CREATE" in template, (db, kind)
            assert template.lstrip().startswith("--"), (db, kind)


def test_column_types_are_nonempty():
    for db in (_sqlite(), _mysql(), _postgres()):
        assert db.column_types()


# create_table_sql


def test_create_table_sql_shared_builder():
    sql = _sqlite().create_table_sql(
        "people",
        [
            ColumnInfo(name="id", type="INTEGER", is_pk=True, nullable=False),
            ColumnInfo(name="name", type="TEXT", nullable=False),
            ColumnInfo(name="bio", type="TEXT"),
        ],
        defaults={"bio": "''"},
    )
    assert sql == (
        'CREATE TABLE "people" (\n'
        '  "id" INTEGER NOT NULL,\n'
        '  "name" TEXT NOT NULL,\n'
        "  \"bio\" TEXT DEFAULT '',\n"
        '  PRIMARY KEY ("id")\n'
        ")"
    )


def test_create_table_sql_quotes_dialect():
    sql = _mysql().create_table_sql(
        "t", [ColumnInfo(name="id", type="INT", is_pk=True, nullable=False)]
    )
    assert "`t`" in sql and "`id` INT NOT NULL" in sql


# Live SQLite round-trip: create, list, drop.


@pytest.fixture()
def sqlite_db(tmp_path):
    path = tmp_path / "ddl.db"
    sqlite3.connect(path).close()  # the adapter refuses missing files
    connector = SqliteConnector(str(path))
    connector.connect()
    yield connector
    connector.close()


def test_sqlite_ddl_roundtrip(sqlite_db: Connector) -> None:
    db = sqlite_db
    db.execute(db.create_table_sql(
        "notes",
        [
            ColumnInfo(name="id", type="INTEGER", is_pk=True, nullable=False),
            ColumnInfo(name="body", type="TEXT"),
        ],
    ))
    db.execute("CREATE VIEW recent AS SELECT * FROM notes")
    db.execute("CREATE INDEX notes_body ON notes (body)")
    db.execute(
        "CREATE TRIGGER notes_touch AFTER INSERT ON notes "
        "BEGIN UPDATE notes SET body = body WHERE id = NEW.id; END"
    )

    assert {t.name for t in db.list_tables()} == {"notes", "recent"}
    assert [i.name for i in db.list_indexes()] == ["notes_body"]
    indexes = db.list_indexes()
    assert indexes[0].table == "notes"
    triggers = db.list_triggers()
    assert [t.name for t in triggers] == ["notes_touch"]
    assert triggers[0].table == "notes"

    db.execute(db.drop_sql("trigger", "notes_touch"))
    db.execute(db.drop_sql("index", "notes_body"))
    db.execute(db.drop_sql("view", "recent"))
    db.execute(db.drop_sql("table", "notes"))
    assert db.list_tables() == []
    assert db.list_indexes() == []
    assert db.list_triggers() == []


def test_sqlite_template_runs(sqlite_db: Connector) -> None:
    """The trigger template must survive the console's statement
    splitter and actually execute (body semicolons included)."""
    from sqlide.backend.sql_split import split_statements

    db = sqlite_db
    db.execute(
        "CREATE TABLE table_name (id INTEGER PRIMARY KEY, name TEXT, "
        "created_at TEXT, column_a TEXT, column_b TEXT)"
    )
    for kind in ("view", "index", "trigger"):
        template = db.create_template(kind)
        statements = split_statements(template)
        assert len(statements) == 1, kind
        db.execute(statements[0].text)
