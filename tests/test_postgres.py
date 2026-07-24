"""Integration tests for the PostgreSQL adapter, run against every
server version in docker-compose.yml (see the `postgres` fixture)."""

from __future__ import annotations

import pytest

from sqlide.backend.db.base import ConnectorError, FilterCondition, SortSpec


def test_server_version_matches_fixture(postgres):
    version, db = postgres
    result = db.execute("SHOW server_version")
    assert result.rows[0][0].startswith(f"{version}.")


def test_list_tables_and_views(postgres):
    _, db = postgres
    kinds = {t.name: t.kind for t in db.list_tables()}
    assert kinds["users"] == "table"
    assert kinds["orders"] == "table"
    assert kinds["big_orders"] == "view"


def test_list_columns(postgres):
    _, db = postgres
    columns = {c.name: c for c in db.list_columns("users")}
    assert columns["id"].is_pk and not columns["id"].nullable
    assert not columns["email"].is_pk and columns["email"].nullable
    assert columns["name"].type == "character varying(40)"


def test_list_relations(postgres):
    _, db = postgres
    assert any(
        r.table == "orders" and r.column == "user_id"
        and r.ref_table == "users" and r.ref_column == "id"
        for r in db.list_relations()
    )


def test_list_databases(postgres):
    _, db = postgres
    assert "sqlide" in db.list_databases()


def test_fetch_rows_filters_and_sort(postgres):
    _, db = postgres
    # Filter values arrive as strings from the UI; the adapter must
    # still match them against integer columns.
    result = db.fetch_rows(
        "orders",
        filters=[FilterCondition(column="amount", op=">", value="100")],
        order_by=[SortSpec(column="amount", descending=True)],
    )
    amounts = [row[result.columns.index("amount")] for row in result.rows]
    assert amounts == [200, 150]


def test_fetch_rows_on_view(postgres):
    _, db = postgres
    assert len(db.fetch_rows("big_orders")) == 2


def test_fetch_rows_rejects_unknown_column(postgres):
    _, db = postgres
    with pytest.raises(ConnectorError, match="Unknown column"):
        db.fetch_rows(
            "orders",
            filters=[FilterCondition(column="nope", op="=", value="1")],
        )


def test_execute_select_and_rowcount(postgres):
    _, db = postgres
    result = db.execute("SELECT count(*) FROM users")
    assert result.rows[0][0] == 3
    assert db.execute("UPDATE users SET email = email WHERE false") == 0


def test_update_cell_roundtrip(postgres):
    _, db = postgres
    db.update_cell("users", {"id": 3}, "email", "carol@new.example.com")
    result = db.execute("SELECT email FROM users WHERE id = 3")
    assert result.rows[0][0] == "carol@new.example.com"
    db.update_cell("users", {"id": 3}, "email", "carol@example.com")


def test_update_cell_rolls_back_on_rowcount_mismatch(postgres):
    _, db = postgres
    with pytest.raises(ConnectorError, match="rolled back"):
        db.update_cell("users", {"id": 999}, "email", "nobody@example.com")


def test_update_cell_rejects_unknown_column(postgres):
    _, db = postgres
    with pytest.raises(ConnectorError, match="Unknown column"):
        db.update_cell("users", {"id": 1}, "nope", "x")


def test_transaction_tracking(postgres):
    _, db = postgres
    assert not db.in_transaction()
    db.execute("BEGIN")
    try:
        assert db.in_transaction()
    finally:
        db.rollback()
    assert not db.in_transaction()


def test_get_ddl_table_and_view(postgres):
    _, db = postgres
    ddl = db.get_ddl("users")
    assert ddl.startswith('CREATE TABLE "users"')
    assert 'PRIMARY KEY ("id")' in ddl
    assert db.get_ddl("big_orders").startswith('CREATE VIEW "big_orders"')


def test_plpgsql_function_listed_with_ddl(postgres):
    _, db = postgres
    assert "add_amounts" in [f.name for f in db.list_functions()]
    assert "CREATE OR REPLACE FUNCTION" in db.get_ddl("add_amounts")


def test_procedures_from_pg11(postgres):
    version, db = postgres
    if int(version) < 11:
        pytest.skip("CREATE PROCEDURE arrived in PostgreSQL 11")
    db.execute(
        "CREATE OR REPLACE PROCEDURE touch_nothing()"
        " LANGUAGE plpgsql AS $$ BEGIN NULL; END $$"
    )
    assert "touch_nothing" in [f.name for f in db.list_functions()]


def test_quote_ident(postgres):
    _, db = postgres
    assert db.quote_ident('we"ird') == '"we""ird"'
    with pytest.raises(ConnectorError):
        db.quote_ident("")


def test_index_roundtrip(postgres):
    _, db = postgres
    db.execute("CREATE INDEX orders_amount ON orders (amount)")
    try:
        indexes = {i.name: i.table for i in db.list_indexes()}
        assert indexes["orders_amount"] == "orders"
    finally:
        db.execute(db.drop_sql("index", "orders_amount"))
    assert "orders_amount" not in [i.name for i in db.list_indexes()]


def test_trigger_roundtrip(postgres):
    _, db = postgres
    db.execute(
        "CREATE OR REPLACE FUNCTION touch_row() RETURNS trigger "
        "LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$"
    )
    db.execute(
        "CREATE TRIGGER users_touch BEFORE INSERT ON users "
        "FOR EACH ROW EXECUTE PROCEDURE touch_row()"
    )
    try:
        triggers = {t.name: t.table for t in db.list_triggers()}
        assert triggers["users_touch"] == "users"
        sql = db.drop_sql("trigger", "users_touch", table="users")
        assert sql == 'DROP TRIGGER "users_touch" ON "users"'
    finally:
        db.execute('DROP TRIGGER IF EXISTS "users_touch" ON "users"')
        db.execute(db.drop_sql("function", "touch_row"))
    assert "users_touch" not in [t.name for t in db.list_triggers()]


def test_drop_sql_uses_function_signature(postgres):
    _, db = postgres
    db.execute(
        "CREATE OR REPLACE FUNCTION drop_me(a integer, b text) "
        "RETURNS integer LANGUAGE sql AS $$ SELECT a $$"
    )
    sql = db.drop_sql("function", "drop_me")
    # regprocedure::text renders the signature without spaces after commas.
    assert sql == "DROP FUNCTION drop_me(integer,text)"
    db.execute(sql)
    assert "drop_me" not in [f.name for f in db.list_functions()]


def test_drop_sql_detects_procedures(postgres):
    version, db = postgres
    if int(version) < 11:
        pytest.skip("CREATE PROCEDURE arrived in PostgreSQL 11")
    db.execute(
        "CREATE OR REPLACE PROCEDURE drop_me_proc() "
        "LANGUAGE plpgsql AS $$ BEGIN NULL; END $$"
    )
    # The catalog decides the verb even when the caller says "function"
    # (the sidebar lists procedures under Functions).
    sql = db.drop_sql("function", "drop_me_proc")
    assert sql == "DROP PROCEDURE drop_me_proc()"
    db.execute(sql)


def test_drop_cascade(postgres):
    _, db = postgres
    db.execute("CREATE TABLE base_t (id integer)")
    db.execute("CREATE VIEW base_v AS SELECT * FROM base_t")
    db.execute(db.drop_sql("table", "base_t", cascade=True))
    names = {t.name for t in db.list_tables()}
    assert "base_t" not in names and "base_v" not in names
