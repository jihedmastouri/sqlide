"""The designer's catalog, read through the MetadataProvider (CORE-24).

The designer used to call `connector.column_type_specs()` straight off
the connector, so no capability flag reached it and it had no idea
which schema the sidebar row it was launched from belonged to. These
tests pin what going through the provider bought: the type list and the
schema list come from the provider, the schema chooser appears only
where the `schemas` capability is on, the chosen schema qualifies the
name in the preview, and a type declaring three arguments gets three
entries rather than being pushed into "Custom…".

`run_async` is collapsed onto this thread, so the load is done by the
time the tab is constructed and no main loop is needed.
"""

from __future__ import annotations

import sqlite3

import pytest
from gi.repository import Gtk  # noqa: F401  (initialises GTK types)

from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.db.base import ColumnInfo, TypeSpec
from sqlide.backend.db.metadata import Capabilities, NodeRef
from sqlide.backend.db.sqlite.connector import SqliteConnector
from sqlide.frontend import table_designer as designer_module
from sqlide.frontend.table_designer import TableDesignerTab


@pytest.fixture(autouse=True)
def inline_async(monkeypatch):
    def immediate(work, on_success, on_error):
        try:
            on_success(work())
        except Exception as exc:  # pragma: no cover - a failure is a failure
            on_error(exc)

    monkeypatch.setattr(designer_module, "run_async", immediate)


class _FakeProvider:
    """A provider with schemas, so the schema path is testable without
    a PostgreSQL server."""

    def __init__(self, connector, *, schemas: bool, specs=()) -> None:
        self.connector = connector
        self._schemas = schemas
        self._specs = list(specs) or list(connector.column_type_specs())

    def column_type_specs(self):
        return list(self._specs)

    def capabilities(self):
        return Capabilities(schemas=self._schemas)

    def schemas(self, *, include_system: bool = False):
        return ["billing", "public"] if self._schemas else []

    def list_sources(self):
        schema = "billing" if self._schemas else ""
        return [
            NodeRef(kind="table", name="customers", schema=schema),
            NodeRef(kind="table", name="regions", schema=schema),
        ]

    def columns_of(self, ref):
        return [ColumnInfo(name="id", type="integer"),
            ColumnInfo(name="code", type="text"),]


@pytest.fixture()
def connector(tmp_path):
    path = tmp_path / "designer.db"
    sqlite3.connect(path).close()
    con = SqliteConnector(str(path))
    con.connect()
    yield con
    con.close()


def _tab(
    monkeypatch, connector, *, schemas: bool, specs=(), ref=None, engine=""
):
    if engine:
        # Rendering asks the connector which dialect it is; an engine
        # with schemas is the only one that qualifies a name.
        connector.engine = engine
    monkeypatch.setattr(
        designer_module.registry,
        "create_provider",
        lambda _kind, con: _FakeProvider(con, schemas=schemas, specs=specs),
    )
    profile = ConnectionProfile(name="designer", kind="sqlite")
    return TableDesignerTab(
        profile,
        lambda _p: connector,
        lambda _message: None,
        on_created=lambda _table, _schema: None,
        ref=ref,
    )


def _fill(tab, name: str = "invoices") -> None:
    tab._table_name.set_text(name)
    row = tab._rows[0]
    row.name.set_text("id")
    row._type.set_selected(0)


def test_no_schema_chooser_without_the_capability(monkeypatch, connector):
    tab = _tab(monkeypatch, connector, schemas=False)
    assert not tab._schema.get_visible()
    assert tab.schema() == ""
    _fill(tab)
    # The name goes in bare, exactly as it always did.
    assert '"invoices"' in tab._build_sql()
    assert "." not in tab._build_sql().splitlines()[0]


def test_chooser_appears_and_opens_on_the_launching_schema(
    monkeypatch, connector
):
    tab = _tab(
        monkeypatch,
        connector,
        schemas=True,
        engine="postgres",
        ref=NodeRef(kind="schema", name="billing"),
    )
    assert tab._schema.get_visible()
    assert tab.schema() == "billing"
    _fill(tab)
    assert tab.model().schema == "billing"
    assert '"billing"."invoices"' in tab._build_sql()


def test_chooser_lists_the_providers_schemas(monkeypatch, connector):
    tab = _tab(monkeypatch, connector, schemas=True)
    model = tab._schema.get_model()
    listed = [model.get_string(i) for i in range(model.get_n_items())]
    assert listed == ["billing", "public"]


def test_a_type_with_three_parameters_gets_three_entries(
    monkeypatch, connector
):
    spec = TypeSpec(
        name="numeric_range",
        params=("precision", "scale", "bounds"),
        defaults=("10", "2", "[)"),
    )
    tab = _tab(monkeypatch, connector, schemas=False, specs=[spec])
    row = tab._rows[0]
    row._type.set_selected(0)
    # No two-argument cap: the row grows exactly the entries the spec
    # declares, prefilled from its defaults.
    assert len(row._params) == 3
    assert [e.get_text() for e in row._params] == ["10", "2", "[)"]
    assert row.type_text() == "numeric_range(10, 2, [))"
    _fill(tab)
    assert "numeric_range(10, 2, [))" in tab._build_sql()


def test_designer_makes_no_direct_connector_catalog_calls(connector):
    source = (
        designer_module.__file__.replace(".pyc", ".py")
    )
    text = open(source).read()
    assert "connector.column_type_specs" not in text
    assert ".column_type_specs()" in text  # via the provider


# Constraints and indexes (CORE-25)


def test_a_table_with_every_constraint_kind_is_designed_in_the_tab(
    monkeypatch, connector
):
    tab = _tab(monkeypatch, connector, schemas=False)
    tab._table_name.set_text("orders")
    for index, (name, pk) in enumerate(
        [("id", True), ("code", True), ("customer", False)]
    ):
        if index:
            tab._add_row()
        row = tab._rows[index]
        row.name.set_text(name)
        row._type.set_selected(0)
        row.pk.set_active(pk)

    unique = tab._add_constraint()
    unique.set_kind("UNIQUE")
    unique.name.set_text("orders_code")
    unique.columns.set_columns(["code"])

    check = tab._add_constraint()
    check.set_kind("CHECK")
    check._expression.set_text("id > 0")

    fk = tab._add_constraint()
    fk.set_kind("FOREIGN KEY")
    fk.columns.set_columns(["customer"])
    fk._ref_table.set_selected(0)  # customers
    fk._ref_cols.set_columns(["id"])
    fk._on_delete.set_selected(3)  # CASCADE

    model = tab.model()
    assert model.primary_key == ("id", "code")
    kinds = [c.kind for c in model.constraints]
    assert set(kinds) == {"PRIMARY KEY", "UNIQUE", "CHECK", "FOREIGN KEY"}
    sql = tab._build_sql()
    assert 'PRIMARY KEY ("id", "code")' in sql
    assert 'CONSTRAINT "orders_code" UNIQUE ("code")' in sql
    assert "CHECK (id > 0)" in sql
    assert 'FOREIGN KEY ("customer") REFERENCES "customers" ("id")' in sql
    assert "ON DELETE CASCADE" in sql


def test_indexes_are_part_of_the_script_the_dialog_lists(
    monkeypatch, connector
):
    tab = _tab(monkeypatch, connector, schemas=False)
    _fill(tab)
    index = tab._add_index()
    index.name.set_text("invoices_id")
    index.columns.set_columns(["id"])
    index.unique.set_active(True)
    statements = tab._statements()
    assert len(statements) == 2
    assert statements[0].startswith("CREATE TABLE")
    assert statements[1] == 'CREATE UNIQUE INDEX "invoices_id" ON "invoices" ("id")'
    # And the preview shows the whole script, not only the CREATE TABLE.
    assert "CREATE UNIQUE INDEX" in tab._preview.get_text()


def test_the_pk_checkbox_and_the_constraints_view_mirror_each_other(
    monkeypatch, connector
):
    tab = _tab(monkeypatch, connector, schemas=False)
    _fill(tab)
    # Ticking the checkbox creates the PRIMARY KEY row.
    tab._rows[0].pk.set_active(True)
    row = tab._pk_row()
    assert row is not None and row.columns.columns() == ("id",)
    # Clearing it takes the row away again.
    tab._rows[0].pk.set_active(False)
    assert tab._pk_row() is None
    # And editing the constraints view ticks the checkbox back.
    row = tab._add_constraint()
    row.set_kind("PRIMARY KEY")
    row.columns.set_columns(["id"])
    tab._constraint_changed()
    assert tab._rows[0].pk.get_active()


def test_unfinished_constraint_rows_block_create_with_a_reason(
    monkeypatch, connector
):
    tab = _tab(monkeypatch, connector, schemas=False)
    _fill(tab)
    assert tab._problem() == ""
    row = tab._add_constraint()
    row.set_kind("CHECK")
    row.name.set_text("positive")
    assert "CHECK" in tab._problem()
    assert not tab._create.get_sensitive()
    row._expression.set_text("id > 0")
    tab._refresh()
    assert tab._problem() == ""


def test_index_fields_follow_the_dialects_flags(monkeypatch, connector):
    # SQLite has partial indexes and no access method; the row shows
    # exactly that, and nothing in the tab names an engine.
    tab = _tab(monkeypatch, connector, schemas=False)
    row = tab._add_index()
    assert not row._method.get_visible()
    assert row._where.get_visible()
