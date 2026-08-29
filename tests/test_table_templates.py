"""Copying a table's structure, and saved templates (CORE-29).

Two ways to start a table from something that already exists: the
structure of a table on the connection, and a shape somebody saved
under a name. Both are the same `TableModel` the designer already
edits, so what these tests pin is the part that is new — that a copy
renders the source's own DDL under a new name, that a template is a
plain TOML file in the config directory, and that a template opened on
another engine opens (pruned and marked) rather than failing.

`run_async` is collapsed onto this thread, so the designer's catalog
load is done by the time the tab is constructed.
"""

from __future__ import annotations

import sqlite3

import pytest
from gi.repository import Gtk  # noqa: F401  (initialises GTK types)

from sqlide.backend import config, table_templates
from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.db.sqlite.connector import SqliteConnector
from sqlide.backend.db.table_model import (
    SQLITE,
    ColumnModel,
    ConstraintModel,
    IndexModel,
    TableModel,
    copy_structure,
    dump_state,
    render_create,
    render_indexes,
)
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


@pytest.fixture(autouse=True)
def clean_config(monkeypatch, tmp_path):
    """Point every store at a config directory of this test's own."""
    config.set_config_dir(tmp_path / "config")
    yield
    config.set_config_dir(None)


@pytest.fixture()
def connector(tmp_path):
    path = tmp_path / "templates.db"
    sqlite3.connect(path).close()
    con = SqliteConnector(str(path))
    con.connect()
    yield con
    con.close()


def _designer(
    connector,
    *,
    designer: str = "",
    designer_engine: str = "",
    designer_source: str = "",
):
    return TableDesignerTab(
        ConnectionProfile(name="designer", kind="sqlite"),
        lambda _p: connector,
        lambda _message: None,
        on_created=lambda _table, _schema: None,
        designer=designer,
        designer_engine=designer_engine,
        designer_source=designer_source,
    )


def _source() -> TableModel:
    return TableModel(
        name="orders",
        columns=(
            ColumnModel(
                name="id", type="INTEGER", nullable=False, primary_key=True
            ),
            ColumnModel(name="code", type="TEXT"),
        ),
        constraints=(
            ConstraintModel(kind="UNIQUE", name="orders_code", columns=("code",)),
        ),
        indexes=(IndexModel(name="orders_code_idx", columns=("code",)),),
    )


# Copying a table's structure


def test_a_copy_is_the_source_statement_under_a_new_name():
    source = _source()
    copy = copy_structure(source, "orders_copy")
    assert render_create(copy, SQLITE) == render_create(source, SQLITE).replace(
        "orders", "orders_copy"
    )


def test_a_copy_renames_the_names_that_belong_to_the_schema():
    copy = copy_structure(_source(), "orders_copy")
    # Two indexes cannot share a name, so the copy's is derived from
    # the new table rather than clashing with the one it came from.
    assert [i.name for i in copy.indexes] == ["orders_copy_code_idx"]
    assert [c.name for c in copy.constraints] == ["orders_copy_code"]
    assert render_indexes(copy, SQLITE)


def test_indexes_and_keys_are_carried_only_when_asked():
    bare = copy_structure(_source(), "orders_copy", indexes=False)
    assert bare.indexes == ()
    assert [c.name for c in bare.constraints] == ["orders_copy_code"]
    keyed = TableModel(
        name="lines",
        columns=(ColumnModel(name="order_id", type="INTEGER"),),
        constraints=(
            ConstraintModel(
                kind="FOREIGN KEY",
                name="lines_order",
                columns=("order_id",),
                ref_table="orders",
                ref_columns=("id",),
            ),
        ),
    )
    assert copy_structure(keyed, "lines_copy", foreign_keys=False).constraints == ()


def test_a_designer_opened_on_a_copy_says_no_rows_come_with_it(connector):
    tab = _designer(
        connector,
        designer=dump_state(copy_structure(_source(), "orders_copy")),
        designer_engine="sqlite",
        designer_source="copy",
    )
    assert tab.model().name == "orders_copy"
    assert [c.name for c in tab.model().columns] == ["id", "code"]
    assert "no rows" in tab._status.get_text()


# Templates on disk


def test_a_template_is_a_toml_file_in_the_config_directory():
    store = table_templates.TemplateStore()
    template = store.save("Audit columns", _source(), engine="sqlite")
    assert template.path.parent == config.config_dir() / "table_templates"
    assert template.path.suffix == ".toml"
    text = template.path.read_text(encoding="utf-8")
    assert 'name = "Audit columns"' in text and "[[column]]" in text
    # And it reads back as the model it was saved from.
    (back,) = store.templates()
    assert back.model == _source() and back.engine == "sqlite"


def test_a_second_template_of_the_same_name_gets_its_own_file():
    store = table_templates.TemplateStore()
    first = store.save("Shape", _source(), engine="sqlite")
    second = store.save("Shape", _source(), engine="sqlite")
    assert second.name == "Shape (2)"
    assert first.path != second.path
    assert [t.name for t in store.templates()] == ["Shape", "Shape (2)"]


def test_a_broken_template_costs_only_itself():
    store = table_templates.TemplateStore()
    store.save("Good", _source(), engine="sqlite")
    store.directory.joinpath("broken.toml").write_text(
        "this is not = toml [", encoding="utf-8"
    )
    store.directory.joinpath("future.toml").write_text(
        'version = 99\nname = "Later"\n', encoding="utf-8"
    )
    assert [t.name for t in store.templates()] == ["Good"]


def test_a_template_can_be_written_by_hand():
    store = table_templates.TemplateStore()
    store.directory.mkdir(parents=True, exist_ok=True)
    store.directory.joinpath("audit.toml").write_text(
        "\n".join(
            [
                "version = 1",
                'name = "Audit"',
                'engine = "postgres"',
                "",
                "[[column]]",
                'name = "created_at"',
                'type = "timestamptz"',
            ]
        ),
        encoding="utf-8",
    )
    (template,) = store.templates()
    assert [c.name for c in template.model.columns] == ["created_at"]


def test_the_designer_saves_the_design_as_a_template(monkeypatch, connector):
    tab = _designer(
        connector,
        designer=dump_state(_source()),
    )
    monkeypatch.setattr(
        designer_module,
        "ask_name",
        lambda _parent, _heading, initial, on_done: on_done(initial),
    )
    tab._on_save_template()
    (template,) = table_templates.TemplateStore().templates()
    assert template.name == "orders" and template.engine == "sqlite"
    assert [c.name for c in template.model.columns] == ["id", "code"]
    assert "orders" in tab._status.get_text()


# A template opened on another engine


def _postgres_template() -> TableModel:
    return TableModel(
        name="events",
        columns=(
            ColumnModel(name="id", type="bigint", primary_key=True),
            ColumnModel(name="seen_at", type="timestamptz"),
        ),
        options={"UNLOGGED": "on"},
    )


def test_a_postgres_template_opens_on_sqlite_marked_not_broken(connector):
    tab = _designer(
        connector,
        designer=dump_state(_postgres_template()),
        designer_engine="postgres",
        designer_source="template",
    )
    model = tab.model()
    # It opened: the columns are all there, and the type it could not
    # translate is kept verbatim rather than guessed at.
    assert [c.name for c in model.columns] == ["id", "seen_at"]
    assert model.column("seen_at").type == "timestamptz"
    # The option this engine does not offer is gone, so the preview is
    # SQL SQLite would accept.
    assert model.options == {}
    assert "UNLOGGED" not in tab._preview.get_text()
    status = tab._status.get_text()
    assert "translated" in status and "seen_at" in status
    assert "option" in status
    assert tab._rows[1].untranslated()


def test_a_type_the_engine_knows_is_not_marked(connector):
    tab = _designer(
        connector,
        designer=dump_state(
            TableModel(
                name="events",
                columns=(ColumnModel(name="id", type="INTEGER"),),
            )
        ),
        designer_engine="postgres",
        designer_source="template",
    )
    assert not tab._rows[0].untranslated()
    assert "translated" not in tab._status.get_text()
