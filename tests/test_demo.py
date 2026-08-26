"""The demo database: parsing the dialect files, and building the
SQLite one (the two server engines are covered by test_postgres.py
and test_mysql.py, which have a server to build on)."""

from __future__ import annotations

import os

import pytest

from sqlide.backend import demo


def test_every_kind_has_a_dialect_file():
    for kind in demo.KINDS:
        assert demo.sql_path(kind).exists()


def test_unknown_kind_is_refused():
    with pytest.raises(demo.DemoError, match="No demo database for"):
        demo.load("jdbc")


def test_sqlite_is_all_body():
    """One file is one database, so there is nothing to create first."""
    script = demo.load("sqlite")
    assert script.database == ""
    assert script.setup == [] and script.grants == []
    assert any(s.startswith("CREATE TABLE") for s in script.body)


@pytest.mark.parametrize("kind", ["postgres", "mysql"])
def test_server_kinds_split_at_the_database_switch(kind):
    script = demo.load(kind)
    assert script.database == demo.DEFAULT_DATABASE
    # The header comment sits on the first statement; the directive
    # behind it still has to be recognised.
    assert script.setup and script.setup[0].upper().startswith("CREATE DATABASE")
    # The switch itself is consumed, never handed back as SQL to run.
    assert not any(s.upper().startswith(("USE ", "\\CONNECT")) for s in script.body)
    assert any(s.startswith("CREATE TABLE") for s in script.body)


@pytest.mark.parametrize("kind", ["postgres", "mysql"])
def test_database_can_be_renamed(kind):
    script = demo.load(kind, database="playground")
    assert script.database == "playground"
    assert "playground" in script.setup[0]
    assert demo.DEFAULT_DATABASE not in script.setup[0]


def test_grants_are_kept_out_of_setup():
    """MySQL's file hands the demo to the dev container's `sqlide`
    user. That is a seeding concern, and running it against somebody
    else's server would just fail."""
    script = demo.load("mysql")
    assert script.grants and all(
        s.upper().startswith("GRANT") for s in script.grants
    )
    assert not any(s.upper().startswith("GRANT") for s in script.setup)
    # The seeding script wants them, so `statements()` puts them back.
    assert any(
        s.upper().startswith("GRANT") for s in demo.statements("mysql")
    )


def test_build_sqlite_demo(tmp_path):
    path = tmp_path / "demo.db"
    assert demo.create("sqlite", file_path=str(path)) == str(path)

    from sqlide.backend.db import registry

    connector = registry.create_connector("sqlite", file_path=str(path))
    connector.connect()
    try:
        kinds = {t.name: t.kind for t in connector.list_tables()}
        # The shapes the demo exists to show.
        assert kinds["customers"] == "table"
        assert kinds["order_totals"] == "view"
        assert [c.name for c in connector.list_columns("customers")] == [
            "id", "name", "email", "city"
        ]
        # A table with no primary key: the grid shows it read-only.
        assert not any(c.is_pk for c in connector.list_columns("log"))
        assert [i.name for i in connector.list_indexes()] == ["ix_orders_customer"]
        assert any(t.name == "orders_logged" for t in connector.list_triggers())
        assert any(
            r.table == "orders" and r.ref_table == "customers"
            for r in connector.list_relations()
        )
        assert connector.execute("SELECT count(*) FROM customers").rows == [(4,)]
        assert connector.execute("SELECT count(*) FROM orders").rows == [(5,)]
        # The trigger ran while the orders were inserted.
        assert connector.execute("SELECT count(*) FROM log").rows == [(5,)]
    finally:
        connector.close()


def test_sqlite_demo_picks_its_own_path(tmp_path, monkeypatch):
    """No file to name: "give me a demo" is one press on SQLite too,
    not a file dialog on the one engine where the database is a file."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    created = demo.create("sqlite")
    assert created == str(tmp_path / "sqlide" / "demo.db")

    from sqlide.backend.db import registry

    connector = registry.create_connector("sqlite", file_path=created)
    connector.connect()
    try:
        assert "customers" in {t.name for t in connector.list_tables()}
    finally:
        connector.close()


def test_repeated_demos_never_overwrite_the_last(tmp_path, monkeypatch):
    """A second press means a second demo. The first may have been
    worked in, so it is numbered around, not built over."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    names = [os.path.basename(demo.create("sqlite")) for _ in range(3)]
    assert names == ["demo.db", "demo-2.db", "demo-3.db"]


def test_sqlite_demo_refuses_to_overwrite(tmp_path):
    """The file is one the user named; the demo does not get to
    replace whatever is already there."""
    path = tmp_path / "demo.db"
    path.write_text("not a database, but not ours to delete either")
    with pytest.raises(demo.DemoError, match="already exists"):
        demo.create("sqlite", file_path=str(path))
    assert path.read_text().startswith("not a database")


def test_server_demo_needs_a_server():
    with pytest.raises(demo.DemoError, match="needs a connection"):
        demo.create("postgres")


# Schema capture (backend/schemas.py).


def test_capture_writes_a_header_and_statements():
    from sqlide.backend import schemas
    from sqlide.backend.db.base import Connector

    class Fake(Connector):
        def connect(self): ...
        def close(self): ...
        def list_tables(self): return []
        def list_columns(self, table): return []
        def fetch_rows(self, table, **kw): ...
        def execute(self, sql): ...
        def update_cell(self, table, pk_values, column, value): ...
        def quote_ident(self, name): return name
        def schema_ddl(self):
            return ["CREATE TABLE a (id int)", "CREATE VIEW v AS SELECT 1"]

    script = schemas.capture(Fake(), kind="sqlite", source="demo.db")
    assert script.startswith(schemas.HEADER)
    assert "-- from: demo.db (sqlite)" in script
    assert "structure only" in script
    # Every statement ends with exactly one semicolon, so the script runs.
    assert "CREATE TABLE a (id int);" in script
    assert "CREATE VIEW v AS SELECT 1;" in script
    assert ";;" not in script


def test_capture_marks_objects_it_could_not_read():
    """A server can refuse to hand over a routine's body. Dropping the
    object silently would produce a schema that rebuilds wrong."""
    from sqlide.backend import schemas
    from sqlide.backend.db.base import Connector, FunctionInfo, TableInfo

    class Fake(Connector):
        def connect(self): ...
        def close(self): ...
        def list_tables(self): return [TableInfo(name="a", kind="table")]
        def list_columns(self, table): return []
        def list_functions(self): return [FunctionInfo(name="secret_fn")]
        def fetch_rows(self, table, **kw): ...
        def execute(self, sql): ...
        def update_cell(self, table, pk_values, column, value): ...
        def quote_ident(self, name): return name
        def get_ddl(self, name):
            return "CREATE TABLE a (id int)" if name == "a" else ""

    script = schemas.capture(Fake())
    assert "CREATE TABLE a (id int);" in script
    assert "secret_fn: no CREATE statement available" in script


def test_sqlite_schema_ddl_orders_tables_before_views(tmp_path):
    from sqlide.backend import demo
    from sqlide.backend.db import registry

    path = tmp_path / "demo.db"
    demo.create("sqlite", file_path=str(path))
    connector = registry.create_connector("sqlite", file_path=str(path))
    connector.connect()
    try:
        statements = connector.schema_ddl()
        kinds = [s.split()[1].upper() for s in statements]
        # Tables, then the index, then view/trigger: replayable top down.
        assert kinds.index("VIEW") > kinds.index("INDEX") > kinds.index("TABLE")
        assert "TRIGGER" in kinds
    finally:
        connector.close()
