"""Integration tests for the MySQL adapter, run against every server
version in docker-compose.yml (see the `mysql` fixture)."""

from __future__ import annotations

import pytest

from sqlide.backend.db.base import ConnectorError, FilterCondition, SortSpec


def test_server_version_matches_fixture(mysql):
    version, db = mysql
    result = db.execute("SELECT VERSION()")
    assert result.rows[0][0].startswith(version)


def test_list_tables_and_views(mysql):
    _, db = mysql
    kinds = {t.name: t.kind for t in db.list_tables()}
    assert kinds["users"] == "table"
    assert kinds["orders"] == "table"
    assert kinds["big_orders"] == "view"


def test_list_columns(mysql):
    _, db = mysql
    columns = {c.name: c for c in db.list_columns("users")}
    assert columns["id"].is_pk and not columns["id"].nullable
    assert not columns["email"].is_pk and columns["email"].nullable
    assert columns["name"].type == "varchar(40)"


def test_list_relations(mysql):
    _, db = mysql
    assert any(
        r.table == "orders" and r.column == "user_id"
        and r.ref_table == "users" and r.ref_column == "id"
        for r in db.list_relations()
    )


def test_list_databases_excludes_system_schemas(mysql):
    _, db = mysql
    databases = db.list_databases()
    assert "sqlide" in databases
    assert "information_schema" not in databases
    assert "mysql" not in databases


def test_fetch_rows_filters_and_sort(mysql):
    _, db = mysql
    # Filter values arrive as strings from the UI; the adapter must
    # still match them against integer columns.
    result = db.fetch_rows(
        "orders",
        filters=[FilterCondition(column="amount", op=">", value="100")],
        order_by=[SortSpec(column="amount", descending=True)],
    )
    amounts = [row[result.columns.index("amount")] for row in result.rows]
    assert amounts == [200, 150]


def test_fetch_rows_on_view(mysql):
    _, db = mysql
    assert len(db.fetch_rows("big_orders")) == 2


def test_fetch_rows_rejects_unknown_column(mysql):
    _, db = mysql
    with pytest.raises(ConnectorError, match="Unknown column"):
        db.fetch_rows(
            "orders",
            filters=[FilterCondition(column="nope", op="=", value="1")],
        )


def test_execute_select_and_rowcount(mysql):
    _, db = mysql
    result = db.execute("SELECT count(*) FROM users")
    assert result.rows[0][0] == 3
    assert db.execute("UPDATE users SET email = email WHERE false") == 0


def test_update_cell_roundtrip(mysql):
    _, db = mysql
    db.update_cell("users", {"id": 3}, "email", "carol@new.example.com")
    result = db.execute("SELECT email FROM users WHERE id = 3")
    assert result.rows[0][0] == "carol@new.example.com"
    db.update_cell("users", {"id": 3}, "email", "carol@example.com")


def test_update_cell_to_same_value(mysql):
    """CLIENT.FOUND_ROWS makes UPDATE rowcounts report matched rows,
    so setting a cell to its current value must not trip the
    expect_rowcount guard."""
    _, db = mysql
    db.update_cell("users", {"id": 1}, "email", "ada@example.com")


def test_update_cell_rolls_back_on_rowcount_mismatch(mysql):
    _, db = mysql
    with pytest.raises(ConnectorError, match="rolled back"):
        db.update_cell("users", {"id": 999}, "email", "nobody@example.com")


def test_transaction_tracking(mysql):
    _, db = mysql
    assert not db.in_transaction()
    db.execute("BEGIN")
    try:
        assert db.in_transaction()
    finally:
        db.rollback()
    assert not db.in_transaction()


def test_get_ddl_table_and_function(mysql):
    _, db = mysql
    assert db.get_ddl("users").startswith("CREATE TABLE `users`")
    assert "FUNCTION" in db.get_ddl("add_amounts")


def test_function_listed(mysql):
    _, db = mysql
    assert "add_amounts" in [f.name for f in db.list_functions()]


def test_rename_column_dialect(mysql):
    """ALTER TABLE ... RENAME COLUMN is MySQL 8 syntax; 5.7 rejects it."""
    version, db = mysql
    sql = db.rename_column_sql("users", "email", "mail")
    if version.startswith("5"):
        with pytest.raises(ConnectorError):
            db.execute(sql)
        return
    db.execute(sql)
    try:
        assert "mail" in [c.name for c in db.list_columns("users")]
    finally:
        db.execute(db.rename_column_sql("users", "mail", "email"))


def test_quote_ident(mysql):
    _, db = mysql
    assert db.quote_ident("we`ird") == "`we``ird`"
    with pytest.raises(ConnectorError):
        db.quote_ident("")


def test_index_roundtrip(mysql):
    _, db = mysql
    db.execute("CREATE INDEX orders_amount ON orders (amount)")
    try:
        indexes = {i.name: i.table for i in db.list_indexes()}
        assert indexes["orders_amount"] == "orders"
        assert "PRIMARY" not in indexes
    finally:
        db.execute(db.drop_sql("index", "orders_amount", table="orders"))
    assert "orders_amount" not in [i.name for i in db.list_indexes()]


def test_trigger_roundtrip(mysql):
    _, db = mysql
    db.execute(
        "CREATE TRIGGER users_touch BEFORE INSERT ON users "
        "FOR EACH ROW SET NEW.name = TRIM(NEW.name)"
    )
    try:
        triggers = {t.name: t.table for t in db.list_triggers()}
        assert triggers["users_touch"] == "users"
    finally:
        db.execute(db.drop_sql("trigger", "users_touch"))
    assert "users_touch" not in [t.name for t in db.list_triggers()]


def test_procedure_roundtrip(mysql):
    _, db = mysql
    db.execute("DROP PROCEDURE IF EXISTS count_users")
    db.execute(
        "CREATE PROCEDURE count_users() SELECT count(*) FROM users"
    )
    assert "count_users" in [f.name for f in db.list_functions()]
    db.execute(db.drop_sql("procedure", "count_users"))
    assert "count_users" not in [f.name for f in db.list_functions()]


def test_event_roundtrip(mysql):
    _, db = mysql
    try:
        db.execute(
            "CREATE EVENT sqlide_evt ON SCHEDULE EVERY 1 DAY "
            "DO DELETE FROM orders WHERE amount < 0"
        )
    except ConnectorError as exc:
        pytest.skip(f"cannot create events on this server: {exc}")
    try:
        assert "sqlide_evt" in db.list_events()
    finally:
        db.execute(db.drop_sql("event", "sqlide_evt"))
    assert "sqlide_evt" not in db.list_events()


def test_build_demo_database(mysql):
    """The whole two-phase build, against a real server.

    Built as root: on MySQL, creating a database is an administrator's
    act, and the compose file's `sqlide` user only owns the `sqlide`
    database (it may create another one but cannot then use it). That
    is the same reason scripts/init_databases.py seeds MySQL as root.
    """
    from sqlide.backend import demo
    from sqlide.backend.db.mysql.connector import MysqlConnector

    _, db = mysql
    name = "demo_test_my"

    def connect_to(database: str):
        connector = MysqlConnector(
            host=db.host, port=db.port, user="root",
            password=db.password, database=database,
        )
        connector.connect()
        return connector

    admin = connect_to(db.database)
    admin.execute(f"DROP DATABASE IF EXISTS {admin.quote_ident(name)}")
    try:
        assert demo.create(
            "mysql", server=admin, connect=connect_to, database=name
        ) == name
        built = connect_to(name)
        try:
            kinds = {t.name: t.kind for t in built.list_tables()}
            assert kinds["customers"] == "table"
            assert kinds["order_totals"] == "view"
            assert not any(c.is_pk for c in built.list_columns("log"))
            assert built.execute("SELECT count(*) FROM orders").rows == [(5,)]
            assert "customer_total" in [f.name for f in built.list_functions()]
            assert any(
                r.table == "orders" and r.ref_table == "customers"
                for r in built.list_relations()
            )
        finally:
            built.close()
    finally:
        admin.execute(f"DROP DATABASE IF EXISTS {admin.quote_ident(name)}")
        admin.close()


def test_schema_ddl_survives_circular_foreign_keys(mysql):
    """MySQL bakes references into each CREATE TABLE, so no ordering
    of the tables satisfies a pair that reference each other — the
    script turns the checks off instead, as mysqldump does."""
    _, db = mysql
    db.execute("SET FOREIGN_KEY_CHECKS = 0")
    db.execute("DROP TABLE IF EXISTS ring_b")
    db.execute("DROP TABLE IF EXISTS ring_a")
    db.execute("SET FOREIGN_KEY_CHECKS = 1")
    db.execute("CREATE TABLE ring_a (id int PRIMARY KEY, b_id int)")
    db.execute(
        "CREATE TABLE ring_b (id int PRIMARY KEY, a_id int, "
        "FOREIGN KEY (a_id) REFERENCES ring_a(id))"
    )
    db.execute(
        "ALTER TABLE ring_a ADD CONSTRAINT ring_a_b_fk "
        "FOREIGN KEY (b_id) REFERENCES ring_b(id)"
    )
    try:
        statements = db.schema_ddl()
        assert statements[0] == "SET FOREIGN_KEY_CHECKS = 0"
        assert statements[-1] == "SET FOREIGN_KEY_CHECKS = 1"
        assert any("ring_a_b_fk" in s for s in statements)
    finally:
        db.execute("SET FOREIGN_KEY_CHECKS = 0")
        db.execute("DROP TABLE IF EXISTS ring_b")
        db.execute("DROP TABLE IF EXISTS ring_a")
        db.execute("SET FOREIGN_KEY_CHECKS = 1")


def test_schema_ddl_includes_triggers(mysql):
    """Triggers are not in information_schema.routines, so the generic
    walk misses them."""
    _, db = mysql
    db.execute("DROP TRIGGER IF EXISTS users_touch")
    db.execute(
        "CREATE TRIGGER users_touch BEFORE INSERT ON users "
        "FOR EACH ROW SET NEW.name = NEW.name"
    )
    try:
        assert any("users_touch" in s for s in db.schema_ddl())
    finally:
        db.execute("DROP TRIGGER IF EXISTS users_touch")


# Rebuild safety (milestone 12): this engine edits tables in place


def test_mysql_does_not_rebuild_tables(mysql):
    """A rebuild renames the table out of the way, and MySQL commits DDL implicitly, so a failure part way
    cannot be undone.
    Editing a referenced table's definition must not go near it."""
    _, db = mysql
    assert not db.supports_table_rebuild
    assert any(r.ref_table == "users" for r in db.list_relations())


def test_mysql_alter_path_applies_a_definition_edit(mysql):
    """The ALTER statements the definition tab generates for a text
    edit must really run on the server."""
    from sqlide.frontend.definition_tab import _alter_statements

    _, db = mysql
    db.execute("DROP TABLE IF EXISTS rebuild_probe")
    db.execute("CREATE TABLE rebuild_probe (id int PRIMARY KEY, gone varchar(40))")
    db.execute("INSERT INTO rebuild_probe VALUES (1, 'keep me')")
    try:
        old_names = [c.name for c in db.list_columns("rebuild_probe")]
        statements, caption = _alter_statements(
            db,
            "rebuild_probe",
            old_names,
            "CREATE TABLE rebuild_probe (id int, extra varchar(40))",
        )
        for sql in statements:
            db.execute(sql)

        assert [c.name for c in db.list_columns("rebuild_probe")] == ["id", "extra"]
        # The rows stay put: nothing was copied through a backup table.
        assert db.execute("SELECT COUNT(*) FROM rebuild_probe").rows[0][0] == 1
        assert "Table mode" in caption
    finally:
        db.execute("DROP TABLE IF EXISTS rebuild_probe")


def test_mysql_alter_path_refuses_what_it_cannot_express(mysql):
    from sqlide.frontend.definition_tab import _alter_statements

    _, db = mysql
    with pytest.raises(ConnectorError):
        _alter_statements(
            db, "users", ["id", "name", "email"],
            "CREATE TABLE users (id int, name varchar(40), email varchar(40))",
        )
