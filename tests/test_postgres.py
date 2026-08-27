"""Integration tests for the PostgreSQL adapter, run against every
server version in docker-compose.yml (see the `postgres` fixture)."""

from __future__ import annotations

import pytest

from sqlide.backend.db.base import (
    BatchError,
    ConnectorError,
    FilterCondition,
    RowOperation,
    SortSpec,
    UserInfo,
)


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


def test_apply_changes_is_atomic(postgres):
    _, db = postgres
    before = db.execute("SELECT id, email FROM users ORDER BY id").rows
    ops = [
        RowOperation(pk_values={"id": 1}, column="email", value="a@x"),
        RowOperation(pk_values={"id": 2}, column="email", value="b@x"),
        RowOperation(pk_values={"id": 999}, column="email", value="nobody@x"),
    ]
    with pytest.raises(BatchError) as caught:
        db.apply_changes("users", ops)
    assert caught.value.index == 2
    assert "row (id=999) column email" in str(caught.value)
    assert db.execute("SELECT id, email FROM users ORDER BY id").rows == before
    assert not db.in_transaction()

    db.apply_changes("users", ops[:2])
    assert db.execute("SELECT email FROM users ORDER BY id").rows[:2] == [
        ("a@x",),
        ("b@x",),
    ]
    db.execute("UPDATE users SET email = 'ada@example.com' WHERE id = 1")
    db.execute("UPDATE users SET email = NULL WHERE id = 2")


def test_apply_changes_joins_an_open_transaction(postgres):
    _, db = postgres
    db.execute("BEGIN")
    try:
        db.apply_changes(
            "users",
            [RowOperation(pk_values={"id": 1}, column="email", value="a@x")],
        )
        assert db.in_transaction()
    finally:
        db.rollback()
    assert db.execute("SELECT email FROM users WHERE id = 1").rows[0][0] == (
        "ada@example.com"
    )


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
    # Constraints come from pg_get_constraintdef, so they are spelled
    # the way the server spells them — which quotes an identifier only
    # where it has to.
    assert "PRIMARY KEY (id)" in ddl
    assert db.get_ddl("big_orders").startswith('CREATE VIEW "big_orders"')


def test_get_ddl_keeps_foreign_keys(postgres):
    """A table's shape includes what it references: DDL that dropped
    the foreign key would rebuild a different table."""
    _, db = postgres
    ddl = db.get_ddl("orders")
    assert "FOREIGN KEY (user_id) REFERENCES users(id)" in ddl


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


# Schemas. A database holds many, and two of them may hold a table of
# the same name — the case that used to collapse into one bad answer.


@pytest.fixture
def two_schemas(postgres):
    """`shipping` and `billing`, each with an `invoices` table of its
    own shape, both on the search_path ahead of public."""
    _, db = postgres
    for schema in ("shipping", "billing"):
        db.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        db.execute(f"CREATE SCHEMA {schema}")
    db.execute("CREATE TABLE shipping.invoices (id integer PRIMARY KEY, carrier text)")
    db.execute("CREATE TABLE billing.invoices (id integer PRIMARY KEY, total integer)")
    db.execute("SET search_path TO shipping, billing, public")
    yield db
    db.execute("SET search_path TO public")
    for schema in ("shipping", "billing"):
        db.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


def test_list_schemas(postgres):
    _, db = postgres
    schemas = db.list_schemas()
    assert "public" in schemas
    # System and per-session catalogs are not places to work in.
    assert not [s for s in schemas if s.startswith(("pg_toast", "pg_temp"))]
    assert "information_schema" not in schemas and "pg_catalog" not in schemas


def test_same_name_in_two_schemas_lists_once(two_schemas):
    """The tree shows the table a bare name resolves to, not one row
    per schema that happens to hold the name."""
    names = [t.name for t in two_schemas.list_tables()]
    assert names.count("invoices") == 1


def test_same_name_in_two_schemas_keeps_columns_apart(two_schemas):
    """The bug this guards: matching on relname alone merged every
    same-named table's columns into one table that does not exist."""
    columns = {c.name for c in two_schemas.list_columns("invoices")}
    # shipping is first on the search_path, so `invoices` is its one.
    assert columns == {"id", "carrier"}
    assert "total" not in columns


def test_same_name_in_two_schemas_keeps_ddl_apart(two_schemas):
    ddl = two_schemas.get_ddl("invoices")
    assert "carrier" in ddl and "total" not in ddl


def test_schema_parameter_pins_the_search_path(postgres):
    """A profile's schema decides what the whole connection sees."""
    from sqlide.backend.db.postgres.connector import PostgresConnector

    _, db = postgres
    db.execute("DROP SCHEMA IF EXISTS reporting CASCADE")
    db.execute("CREATE SCHEMA reporting")
    db.execute("CREATE TABLE reporting.summary (id integer PRIMARY KEY)")
    scoped = PostgresConnector(
        host=db.host, port=db.port, user=db.user,
        password=db.password, database=db.database, schema="reporting",
    )
    scoped.connect()
    try:
        assert scoped.current_schema() == "reporting"
        # Only this schema's objects, and `users` from public is gone.
        assert [t.name for t in scoped.list_tables()] == ["summary"]
        # An unqualified CREATE lands here too.
        scoped.execute("CREATE TABLE landed_here (id integer)")
        assert "landed_here" in [t.name for t in scoped.list_tables()]
    finally:
        scoped.close()
        db.execute("DROP SCHEMA IF EXISTS reporting CASCADE")


def test_unknown_schema_is_reported_on_connect(postgres):
    """SET search_path accepts a name that does not exist, so an empty
    sidebar would be the only clue. Say it instead."""
    from sqlide.backend.db.postgres.connector import PostgresConnector

    _, db = postgres
    connector = PostgresConnector(
        host=db.host, port=db.port, user=db.user,
        password=db.password, database=db.database, schema="no_such_schema",
    )
    with pytest.raises(ConnectorError, match="No schema named"):
        connector.connect()


def test_build_demo_database(postgres):
    """The whole two-phase build, against a real server."""
    from sqlide.backend import demo
    from sqlide.backend.db.postgres.connector import PostgresConnector

    _, db = postgres
    name = "demo_test_pg"

    def connect_to(database: str):
        connector = PostgresConnector(
            host=db.host, port=db.port, user=db.user,
            password=db.password, database=database,
        )
        connector.connect()
        return connector

    if name in db.list_databases():
        db.execute(f"DROP DATABASE {db.quote_ident(name)}")
    try:
        assert demo.create(
            "postgres", server=db, connect=connect_to, database=name
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
        db.execute(f"DROP DATABASE IF EXISTS {db.quote_ident(name)}")


def test_schema_ddl_replays_into_an_empty_database(postgres):
    """The saved-schema round trip: capture this database's structure,
    run the script into a fresh one, and get the same shape back."""
    from sqlide.backend import schemas
    from sqlide.backend.db.postgres.connector import PostgresConnector
    from sqlide.backend.sql_split import split_statements

    _, db = postgres
    target = "schema_replay_test"

    def connect_to(database: str):
        connector = PostgresConnector(
            host=db.host, port=db.port, user=db.user,
            password=db.password, database=database,
        )
        connector.connect()
        return connector

    script = schemas.capture(db, kind="postgres", source="sqlide")
    db.execute(f"DROP DATABASE IF EXISTS {db.quote_ident(target)}")
    db.execute(f"CREATE DATABASE {db.quote_ident(target)}")
    try:
        replica = connect_to(target)
        try:
            for statement in split_statements(script):
                if sql := _sql_only(statement.text):
                    replica.execute(sql)
            assert {t.name for t in replica.list_tables()} >= {
                "users", "orders", "big_orders"
            }
            assert [c.name for c in replica.list_columns("users")] == [
                c.name for c in db.list_columns("users")
            ]
            assert any(
                r.table == "orders" and r.ref_table == "users"
                for r in replica.list_relations()
            )
            assert "add_amounts" in [f.name for f in replica.list_functions()]
        finally:
            replica.close()
    finally:
        db.execute(f"DROP DATABASE IF EXISTS {db.quote_ident(target)}")


def test_schema_ddl_survives_circular_foreign_keys(postgres):
    """Two tables that reference each other cannot both come second,
    so the foreign keys are added after every table exists."""
    _, db = postgres
    db.execute("DROP TABLE IF EXISTS ring_b CASCADE")
    db.execute("DROP TABLE IF EXISTS ring_a CASCADE")
    db.execute("CREATE TABLE ring_a (id integer PRIMARY KEY, b_id integer)")
    db.execute(
        "CREATE TABLE ring_b (id integer PRIMARY KEY, "
        "a_id integer REFERENCES ring_a(id))"
    )
    db.execute(
        "ALTER TABLE ring_a ADD CONSTRAINT ring_a_b_fk "
        "FOREIGN KEY (b_id) REFERENCES ring_b(id)"
    )
    try:
        statements = db.schema_ddl()
        creates = [s for s in statements if s.startswith("CREATE TABLE")]
        alters = [s for s in statements if s.startswith("ALTER TABLE")]
        # No CREATE TABLE carries a REFERENCES clause…
        assert not any("REFERENCES" in s for s in creates)
        # …and every table is created before the first ALTER runs.
        assert statements.index(creates[-1]) < statements.index(alters[0])
        assert any("ring_a_b_fk" in s for s in alters)
    finally:
        db.execute("DROP TABLE IF EXISTS ring_b CASCADE")
        db.execute("DROP TABLE IF EXISTS ring_a CASCADE")


def _sql_only(text: str) -> str:
    """A statement without its comment lines — the capture's header
    comment attaches to the statement that follows it."""
    return "\n".join(
        line
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("--")
    ).strip()


# Rebuild safety (milestone 12): this engine edits tables in place


def test_postgres_does_not_rebuild_tables(postgres):
    """A rebuild renames the table out of the way, and in Postgres the inbound foreign keys follow the
    backup rather than the name.
    Editing a referenced table's definition must not go near it."""
    _, db = postgres
    assert not db.supports_table_rebuild
    assert any(r.ref_table == "users" for r in db.list_relations())


def test_postgres_alter_path_applies_a_definition_edit(postgres):
    """The ALTER statements the definition tab generates for a text
    edit must really run on the server."""
    from sqlide.frontend.definition_tab import _alter_statements

    _, db = postgres
    db.execute("DROP TABLE IF EXISTS rebuild_probe")
    db.execute("CREATE TABLE rebuild_probe (id integer PRIMARY KEY, gone text)")
    db.execute("INSERT INTO rebuild_probe VALUES (1, 'keep me')")
    try:
        old_names = [c.name for c in db.list_columns("rebuild_probe")]
        statements, caption = _alter_statements(
            db,
            "rebuild_probe",
            old_names,
            "CREATE TABLE rebuild_probe (id integer, extra text)",
        )
        for sql in statements:
            db.execute(sql)

        assert [c.name for c in db.list_columns("rebuild_probe")] == ["id", "extra"]
        # The rows stay put: nothing was copied through a backup table.
        assert db.execute("SELECT COUNT(*) FROM rebuild_probe").rows[0][0] == 1
        assert "Table mode" in caption
    finally:
        db.execute("DROP TABLE IF EXISTS rebuild_probe")


def test_postgres_alter_path_refuses_what_it_cannot_express(postgres):
    from sqlide.frontend.definition_tab import _alter_statements

    _, db = postgres
    with pytest.raises(ConnectorError):
        _alter_statements(
            db, "users", ["id", "name", "email"],
            "CREATE TABLE users (id integer, name text, email text)",
        )


def test_postgres_row_cap_reports_truncation(postgres):
    _, db = postgres
    result = db.execute("SELECT * FROM generate_series(1, 100)", max_rows=10)
    assert len(result) == 10
    assert result.truncated
    # Exactly at the cap is the whole answer, not a clipped one.
    exact = db.execute("SELECT * FROM generate_series(1, 10)", max_rows=10)
    assert len(exact) == 10 and not exact.truncated


def test_postgres_cancel_stops_a_running_statement(postgres):
    import threading
    import time

    _, db = postgres
    error: list[Exception] = []
    started = threading.Event()

    def run() -> None:
        started.set()
        try:
            db.execute("SELECT pg_sleep(30)")
        except Exception as exc:
            error.append(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    started.wait(5)
    time.sleep(0.3)  # let the statement reach the server
    db.cancel()
    worker.join(15)

    assert not worker.is_alive(), "cancel did not unblock the statement"
    assert isinstance(error[0], ConnectorError)
    # The connection is still usable afterwards.
    assert db.execute("SELECT 1").rows[0][0] == 1


def test_unicode_round_trips(postgres):
    _, db = postgres
    db.execute("DROP TABLE IF EXISTS unicode_probe")
    db.execute("CREATE TABLE unicode_probe (id integer PRIMARY KEY, s text)")
    try:
        db.execute("INSERT INTO unicode_probe VALUES (1, 'Ünïcødé 🎉')")
        assert db.execute(
            "SELECT s FROM unicode_probe"
        ).rows[0][0] == "Ünïcødé 🎉"
    finally:
        db.execute("DROP TABLE unicode_probe")


def test_client_encoding_is_utf8(postgres):
    _, db = postgres
    assert db.execute("SHOW client_encoding").rows[0][0].upper() == "UTF8"


def test_session_time_zone_is_pinned(postgres):
    """connect() sets TimeZone, so a timestamptz reads the same here as
    it would against any other server."""
    from sqlide.backend.settings import session_time_zone

    _, db = postgres
    expected = session_time_zone()
    if expected is None:
        pytest.skip("settings ask for the server's own zone")
    assert db.execute("SHOW TimeZone").rows[0][0] == expected


def test_list_users_reports_role_attributes(postgres):
    _, db = postgres
    users = {u.name: u for u in db.list_users()}
    assert "sqlide" in users
    assert users["sqlide"].can_login
    # The cluster's built-in pg_* roles are the server's own business.
    assert not any(name.startswith("pg_") for name in users)


def test_list_privileges_reports_role_attributes_and_grants(postgres):
    _, db = postgres
    user = next(u for u in db.list_users() if u.name == "sqlide")
    privileges = db.list_privileges(user)
    assert any(
        p.scope == "server" and p.privilege == "LOGIN" for p in privileges
    )


def test_grant_scopes_cover_databases_and_schemas(postgres):
    _, db = postgres
    targets = {s.target for s in db.grant_scopes()}
    assert 'DATABASE "sqlide"' in targets
    assert 'SCHEMA "public"' in targets
    assert 'ALL TABLES IN SCHEMA "public"' in targets


def test_account_statements_quote_role_and_password(postgres):
    _, db = postgres
    user = UserInfo(name="app")
    assert db.create_user_sql("app", "", "pa'ss") == (
        'CREATE ROLE "app" LOGIN PASSWORD \'pa\'\'ss\''
    )
    assert db.set_password_sql(user, "x") == 'ALTER ROLE "app" PASSWORD \'x\''
    assert db.drop_user_sql(user) == 'DROP ROLE "app"'
    assert db.grant_sql(user, ["select"], 'SCHEMA "public"') == (
        'GRANT SELECT ON SCHEMA "public" TO "app"'
    )
    assert db.revoke_sql(user, ["USAGE"], 'SCHEMA "public"') == (
        'REVOKE USAGE ON SCHEMA "public" FROM "app"'
    )


def test_grant_rejects_an_unknown_privilege(postgres):
    _, db = postgres
    with pytest.raises(ConnectorError):
        db.grant_sql(UserInfo(name="app"), ["SUPERUSER"], 'DATABASE "sqlide"')


def test_permission_editor_round_trip(postgres):
    """The permission editor's loop, end to end on a real server: read
    what a role holds, tick a box, run the statement, read it back —
    and the same again to take it away (CORE-10)."""
    from sqlide.backend.db.metadata import NodeRef
    from sqlide.backend.db.postgres.metadata import PostgresMetadata

    _, db = postgres
    provider = PostgresMetadata(db)
    db.execute('DROP ROLE IF EXISTS "sqlide_perm_test"')
    db.execute('CREATE ROLE "sqlide_perm_test"')
    user = UserInfo(name="sqlide_perm_test")
    ref = NodeRef("table", "orders", schema="public")
    try:
        before = provider.permission_set(user, ref)
        assert before.target == 'TABLE "public"."orders"'
        assert not before.state("SELECT").granted

        statements = provider.permission_statements(
            user, before, {"SELECT": (True, True)}
        )
        assert statements == [
            'GRANT SELECT ON TABLE "public"."orders" '
            'TO "sqlide_perm_test" WITH GRANT OPTION'
        ]
        provider.apply_permissions(statements)

        after = provider.permission_set(user, ref)
        assert after.state("SELECT").granted
        assert after.state("SELECT").grantable
        assert after.state("SELECT").editable

        provider.apply_permissions(
            provider.permission_statements(
                user, after, {"SELECT": (False, False)}
            )
        )
        assert not provider.permission_set(user, ref).state("SELECT").granted
    finally:
        db.execute(
            'REVOKE ALL ON TABLE "public"."orders" FROM "sqlide_perm_test"'
        )
        db.execute('DROP ROLE IF EXISTS "sqlide_perm_test"')


def test_a_failing_permission_batch_applies_nothing(postgres):
    """The transaction the editor leans on: a batch whose last
    statement is bad leaves the good ones unapplied too."""
    from sqlide.backend.db.base import ConnectorError as _Error
    from sqlide.backend.db.metadata import NodeRef
    from sqlide.backend.db.postgres.metadata import PostgresMetadata

    _, db = postgres
    provider = PostgresMetadata(db)
    db.execute('DROP ROLE IF EXISTS "sqlide_perm_test"')
    db.execute('CREATE ROLE "sqlide_perm_test"')
    user = UserInfo(name="sqlide_perm_test")
    ref = NodeRef("table", "orders", schema="public")
    try:
        with pytest.raises(_Error) as failure:
            provider.apply_permissions([
                'GRANT SELECT ON TABLE "public"."orders" '
                'TO "sqlide_perm_test"',
                'GRANT SELECT ON TABLE "public"."no_such_table" '
                'TO "sqlide_perm_test"',
            ])
        assert "no_such_table" in str(failure.value)
        assert not provider.permission_set(user, ref).state("SELECT").granted
    finally:
        db.execute('DROP ROLE IF EXISTS "sqlide_perm_test"')


# Schema as a level of its own (PG-01).


@pytest.fixture()
def crm_schema(postgres):
    """A second schema with a table that `public.orders` points at, so
    the cross-schema cases have something real to cross into."""
    _, db = postgres
    db.execute('DROP SCHEMA IF EXISTS "sqlide_crm" CASCADE')
    db.execute('CREATE SCHEMA "sqlide_crm"')
    db.execute(
        'CREATE TABLE "sqlide_crm"."accounts" ('
        " id integer PRIMARY KEY, label text)"
    )
    db.execute(
        'ALTER TABLE "public"."orders" ADD COLUMN account_id integer '
        'REFERENCES "sqlide_crm"."accounts" (id)'
    )
    yield db
    db.execute('ALTER TABLE "public"."orders" DROP COLUMN account_id')
    db.execute('DROP SCHEMA IF EXISTS "sqlide_crm" CASCADE')


def test_search_path_is_readable_and_switchable(postgres):
    _, db = postgres
    before = db.search_path()
    assert "public" in before
    db.execute('DROP SCHEMA IF EXISTS "sqlide_path" CASCADE')
    db.execute('CREATE SCHEMA "sqlide_path"')
    try:
        db.set_search_path("sqlide_path")
        path = db.search_path()
        # The *effective* path, so the implicit pg_catalog is in it and
        # the chosen schema is the only user schema left on it.
        assert path.split(", ") == ["pg_catalog", "sqlide_path"]
        assert db.current_schema() == "sqlide_path"
    finally:
        db.set_search_path("")
        db.execute('DROP SCHEMA IF EXISTS "sqlide_path" CASCADE')
    assert db.current_schema() == "public"


def test_set_search_path_refuses_a_schema_that_is_not_there(postgres):
    _, db = postgres
    with pytest.raises(ConnectorError):
        db.set_search_path("no_such_schema_here")


def test_relations_carry_both_schemas(crm_schema):
    db = crm_schema
    crossing = [
        r for r in db.list_relations()
        if r.table == "orders" and r.column == "account_id"
    ]
    assert crossing, "the cross-schema foreign key was not reported"
    relation = crossing[0]
    assert relation.schema == "public"
    assert relation.ref_schema == "sqlide_crm"
    assert relation.cross_schema
    # Qualified only where it crosses: a key inside the schema reads as
    # it always did.
    assert relation.target == "sqlide_crm.accounts"
    same = next(
        r for r in db.list_relations()
        if r.table == "orders" and r.column == "user_id"
    )
    assert not same.cross_schema and same.target == "users"


def test_inbound_references_are_found_across_schemas(crm_schema):
    db = crm_schema
    try:
        # The referring table lives in a schema that is not on the path
        # at all, and is still reported.
        db.set_search_path("sqlide_crm")
        inbound = db.list_references("accounts")
    finally:
        db.set_search_path("")
    assert [(r.schema, r.table, r.column) for r in inbound] == [
        ("public", "orders", "account_id")
    ]
    assert inbound[0].source == "public.orders"


def test_foreign_key_section_qualifies_only_the_crossing_row(crm_schema):
    from sqlide.backend.db import objects

    db = crm_schema
    info = objects.table_properties(db, "orders", ("general", "foreign_keys"))
    section = next(t for t in info.tables if t.slug == "foreign_keys")
    references = {row[0]: row[1] for row in section.rows}
    assert references["account_id"] == "sqlide_crm.accounts.id"
    assert references["user_id"] == "users.id"
    # The crossing row links with the schema on it, so following it
    # opens that `accounts` and not another.
    crossing = section.rows.index(("account_id", "sqlide_crm.accounts.id"))
    assert section.link(crossing).schema == "sqlide_crm"
    same = section.rows.index(("user_id", "users.id"))
    assert section.link(same).schema == ""


def test_provider_qualifies_and_quotes_by_schema(crm_schema):
    from sqlide.backend.db.metadata import NodeRef
    from sqlide.backend.db.postgres.metadata import PostgresMetadata

    provider = PostgresMetadata(crm_schema)
    ref = NodeRef("table", "accounts", database="sqlide", schema="sqlide_crm")
    assert provider.qualified_name(ref) == "sqlide_crm.accounts"
    assert provider.quoted_name(ref) == '"sqlide_crm"."accounts"'
    assert provider.grant_target(ref) == 'TABLE "sqlide_crm"."accounts"'
    assert ref.path == ("sqlide", "sqlide_crm", "accounts")
    # A schema names itself; it is not qualified by itself.
    schema = NodeRef("schema", "sqlide_crm", database="sqlide",
                     schema="sqlide_crm")
    assert provider.qualified_name(schema) == "sqlide_crm"
    assert schema.path == ("sqlide", "sqlide_crm")


def test_reserved_and_capitalised_names_stay_one_identifier(postgres):
    from sqlide.backend.db.metadata import NodeRef
    from sqlide.backend.db.postgres.metadata import PostgresMetadata

    _, db = postgres
    provider = PostgresMetadata(db)
    ref = NodeRef("table", "Order.2024", schema="select")
    assert provider.quoted_name(ref) == '"select"."Order.2024"'
    assert provider.grant_target(ref) == 'TABLE "select"."Order.2024"'


def test_schema_node_lists_its_own_tables(crm_schema):
    from sqlide.backend.db.metadata import NodeRef
    from sqlide.backend.db.postgres.metadata import PostgresMetadata

    provider = PostgresMetadata(crm_schema)
    schemas = provider.list_children(NodeRef("database", "sqlide"))
    crm = next(s for s in schemas if s.name == "sqlide_crm")
    assert crm.kind == "schema" and crm.schema == "sqlide_crm"
    tables = provider.list_children(
        crm.child("category", "Tables", category="tables")
    )
    assert [t.name for t in tables] == ["accounts"]
    # The schema travels down to the object, so the row can name itself
    # in full.
    assert tables[0].schema == "sqlide_crm"
    assert provider.qualified_name(tables[0]) == "sqlide_crm.accounts"
    assert provider.describe(tables[0]).name == "sqlide_crm.accounts"


def test_list_schemas_can_include_the_system_ones(postgres):
    """The object tree shows them (dimmed, PG-03), so the adapter can
    be asked for them — the per-session and per-table catalogs stay
    out either way, being bookkeeping rather than places to browse."""
    _, db = postgres
    schemas = db.list_schemas(include_system=True)
    assert "information_schema" in schemas and "pg_catalog" in schemas
    assert "public" in schemas
    assert not [s for s in schemas if s.startswith(("pg_toast", "pg_temp"))]


def test_the_tree_sorts_system_schemas_last(postgres):
    """A database's schema rows: the user's first, the server's own
    after them and marked as such (PG-03)."""
    from sqlide.backend.db import registry
    from sqlide.backend.db.metadata import NodeRef

    _, db = postgres
    provider = registry.create_provider("postgres", db)
    rows = [
        ref
        for ref in provider.list_children(NodeRef("database", db.database))
        if ref.kind == "schema"
    ]
    names = [ref.name for ref in rows]
    assert "public" in names and "information_schema" in names
    flags = [ref.system for ref in rows]
    assert flags == sorted(flags)  # every user schema before every system one
    assert dict(zip(names, flags))["information_schema"] is True
    assert dict(zip(names, flags))["public"] is False


def test_postgis_detection_survives_its_absence(postgres):
    """The geo viewer's gate (PG-04).

    Whether the server has PostGIS is a catalog question, and the
    answer on a plain image is "no" — which has to come back as an
    empty string, not an error, or every table tab on a PostgreSQL
    connection without PostGIS breaks on load.
    """
    from sqlide.backend.db import registry

    _, db = postgres
    provider = registry.create_provider("postgres", db)
    assert provider.capabilities().geometry  # the engine could have it
    found = provider.spatial_extension()  # this server may not
    assert found == "" or found.startswith("postgis ")


def test_geometry_values_read_without_postgis_installed(postgres):
    """The WKB the grid summarises is parsed here, not by the server:
    a hex EWKB literal round-trips through a query and comes back as a
    readable geometry even where PostGIS is not installed."""
    from sqlide.backend.db import geo

    _, db = postgres
    # A Point(2.35 48.85) with SRID 4326, as PostGIS would return it.
    wkb = "0101000020E6100000CDCCCCCCCCCC0240CDCCCCCCCC6C4840"
    result = db.execute(f"SELECT 1 AS id, '{wkb}' AS geom")
    built = geo.build_features(result.columns, result.rows)
    assert len(built.features) == 1
    assert built.features[0].geometry.kind == "Point"
    assert built.features[0].geometry.srid == 4326
    lon, lat = built.features[0].geometry.shapes[0].coords[0]
    assert round(lon, 2) == 2.35 and round(lat, 2) == 48.85
