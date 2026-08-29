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
from sqlide.backend.db.base import TypeSpec
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
