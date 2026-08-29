"""Create/drop DDL surface: the pure string builders per adapter (no
server needed) and a live SQLite round-trip on a temp-file database."""

from __future__ import annotations

import sqlite3

import pytest

from sqlide.backend.db.base import (
    ColumnInfo,
    Connector,
    ConnectorError,
    IndexInfo,
    TriggerInfo,
)
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


def test_sqlite_rebuild_keeps_indexes_and_triggers(sqlite_db: Connector) -> None:
    db = sqlite_db
    db.execute(
        'CREATE TABLE notes ("id" INTEGER NOT NULL, "body" TEXT, '
        'PRIMARY KEY ("id"))'
    )
    db.execute("CREATE INDEX notes_body ON notes (body)")
    db.execute(
        "CREATE TRIGGER notes_touch AFTER INSERT ON notes "
        "BEGIN UPDATE notes SET body = body WHERE id = NEW.id; END"
    )

    new_ddl = (
        'CREATE TABLE "notes" (\n'
        '  "id" INTEGER NOT NULL,\n'
        '  "body" TEXT,\n'
        '  "extra" TEXT,\n'
        '  PRIMARY KEY ("id")\n'
        ")"
    )
    statements = db.rebuild_table_statements(
        "notes", new_ddl, [("id", "id"), ("body", "body")]
    )
    for sql in statements:
        db.execute(sql)

    assert [c.name for c in db.list_columns("notes")] == [
        "id", "body", "extra",
    ]
    assert [i.name for i in db.list_indexes()] == ["notes_body"]
    assert [t.name for t in db.list_triggers()] == ["notes_touch"]


def test_rebuild_carries_indexes_and_triggers_for_every_dialect() -> None:
    """The carry lives on the base class, so an adapter gets it from
    the shape of its catalog rather than from being SQLite."""
    for db in (_sqlite(), _postgres()):
        db.list_tables = lambda: []  # type: ignore[method-assign]
        db.list_indexes = lambda: [  # type: ignore[method-assign]
            IndexInfo(name="i", table="notes", ddl="CREATE INDEX i ON notes (b);"),
            IndexInfo(name="other", table="elsewhere", ddl="CREATE INDEX other ON elsewhere (b)"),
        ]
        db.list_triggers = lambda: [  # type: ignore[method-assign]
            TriggerInfo(name="t", table="notes", ddl="CREATE TRIGGER t ..."),
        ]
        statements = db.rebuild_table_statements("notes", "CREATE TABLE notes (b)", [])
        # Replayed after the DROP: until then the old objects hold the names.
        carried = ["CREATE INDEX i ON notes (b)", "CREATE TRIGGER t ..."]
        at = statements.index(carried[0])
        assert statements[at : at + 2] == carried
        assert any(s.startswith("DROP TABLE") for s in statements[:at])


def test_rebuild_skips_indexes_when_the_ddl_declares_them() -> None:
    """MySQL's SHOW CREATE TABLE already writes every KEY inline;
    replaying them too would fail on the duplicate key name."""
    db = _mysql()
    db.list_tables = lambda: []  # type: ignore[method-assign]
    db.list_indexes = lambda: [  # type: ignore[method-assign]
        IndexInfo(name="i", table="notes", ddl="CREATE INDEX `i` ON `notes` (`b`)"),
    ]
    db.list_triggers = lambda: [  # type: ignore[method-assign]
        TriggerInfo(name="t", table="notes", ddl="CREATE TRIGGER `t` ..."),
    ]
    statements = db.rebuild_table_statements("notes", "CREATE TABLE notes (b)", [])
    assert statements[-1] == "CREATE TRIGGER `t` ..."  # nothing trails it on MySQL
    assert not any("CREATE INDEX" in s for s in statements)


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


# Rebuild safety: who may rebuild, the backup name, and the SQLite check


def test_only_sqlite_rebuilds_tables() -> None:
    """The rebuild is a workaround for SQLite's ALTER TABLE; the
    engines with a real one must not be on it."""
    assert _sqlite().supports_table_rebuild
    assert not _mysql().supports_table_rebuild
    assert not _postgres().supports_table_rebuild


def test_backup_name_is_unique_per_rebuild(sqlite_db: Connector) -> None:
    db = sqlite_db
    db.execute("CREATE TABLE notes (b TEXT)")
    # A leftover from an earlier failed rebuild must not block this one.
    db.execute("CREATE TABLE notes__old (b TEXT)")

    def backup_of(statements: list[str]) -> str:
        rename = next(s for s in statements if "RENAME TO" in s)
        return rename.split("RENAME TO")[1].strip().strip('"')

    first = backup_of(db.rebuild_table_statements("notes", "CREATE TABLE notes (b)", []))
    second = backup_of(db.rebuild_table_statements("notes", "CREATE TABLE notes (b)", []))
    assert first != second
    assert first != "notes__old" and second != "notes__old"
    for name in (first, second):
        assert name.startswith("notes__old_")


def test_backup_name_respects_the_identifier_limit() -> None:
    db = _postgres()
    db.list_tables = lambda: []  # type: ignore[method-assign]
    name = db._backup_table_name("t" * 70)
    assert len(name) == db.identifier_max_length


def test_sqlite_rebuild_checks_foreign_keys_inside_the_transaction() -> None:
    db = _sqlite()
    db.list_tables = lambda: []  # type: ignore[method-assign]
    db.list_indexes = lambda: []  # type: ignore[method-assign]
    db.list_triggers = lambda: []  # type: ignore[method-assign]
    statements = db.wrap_rebuild(
        db.rebuild_table_statements("notes", "CREATE TABLE notes (b)", [])
    )
    assert statements[0] == "PRAGMA foreign_keys = OFF"
    assert statements[1] == "BEGIN"
    assert statements[-1] == "PRAGMA foreign_keys = ON"
    assert statements[-2] == "COMMIT"
    # Inside the transaction, and last: it must see the finished table.
    assert statements[-3] == "PRAGMA foreign_key_check"


def test_foreign_key_check_reports_rather_than_raises(sqlite_db: Connector) -> None:
    """The pragma returns violations as rows, so a caller that only
    watches for exceptions would commit a broken rebuild."""
    db = sqlite_db
    db.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
    db.execute(
        "CREATE TABLE child (id INTEGER PRIMARY KEY, "
        "parent_id INTEGER REFERENCES parent(id))"
    )
    db.execute("INSERT INTO parent VALUES (1)")
    db.execute("INSERT INTO child VALUES (1, 1)")
    db.execute("PRAGMA foreign_keys = OFF")
    db.execute("DELETE FROM parent")

    result = db.execute("PRAGMA foreign_key_check")
    assert result.rows  # no exception was raised
    message = db.rebuild_check_failure("PRAGMA foreign_key_check", result)
    assert "child" in message

    db.execute("INSERT INTO parent VALUES (1)")
    clean = db.execute("PRAGMA foreign_key_check")
    assert not db.rebuild_check_failure("PRAGMA foreign_key_check", clean)


def test_only_the_check_statement_is_inspected() -> None:
    db = _sqlite()
    assert db.rebuild_check_failure("DROP TABLE notes__old_abcd1234", 1) == ""


# The ALTER path the servers take instead of a rebuild


def test_alter_path_adds_and_drops_the_columns_the_edit_changed() -> None:
    from sqlide.frontend.definition_tab import _alter_statements

    db = _postgres()
    statements, caption = _alter_statements(
        db,
        "notes",
        ["id", "body", "gone"],
        'CREATE TABLE "notes" ("id" integer, "body" text, '
        '"extra" text NOT NULL DEFAULT \'x\')',
    )
    # The entry goes through verbatim: re-rendering it would drop the
    # DEFAULT we didn't parse.
    assert statements == [
        'ALTER TABLE "notes" ADD COLUMN "extra" text NOT NULL DEFAULT \'x\'',
        'ALTER TABLE "notes" DROP COLUMN "gone"',
    ]
    assert "table designer" in caption


def test_alter_path_refuses_an_edit_it_cannot_express() -> None:
    """A type change on a surviving column can't be diffed from the
    text honestly, so it is refused rather than silently dropped."""
    from sqlide.frontend.definition_tab import _alter_statements

    with pytest.raises(ConnectorError) as excinfo:
        _alter_statements(
            _mysql(), "notes", ["id"], "CREATE TABLE `notes` (`id` bigint)"
        )
    assert "table designer" in str(excinfo.value)


def test_alter_path_ignores_table_constraints() -> None:
    from sqlide.frontend.definition_tab import _alter_statements

    statements, _caption = _alter_statements(
        _postgres(),
        "notes",
        ["id"],
        'CREATE TABLE "notes" ("id" integer, "b" text, PRIMARY KEY ("id"))',
    )
    assert statements == ['ALTER TABLE "notes" ADD COLUMN "b" text']
