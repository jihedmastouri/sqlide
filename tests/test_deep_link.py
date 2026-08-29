"""Sidebar deep links into a table's properties (CORE-05).

Opening *Tables → orders → Indexes* has to land on the orders table's
properties with the Indexes section selected, reusing the table tab if
it is already open. Since CORE-47 that surface is the right side panel
(or a detached properties window), not a mode of the tab — the wiring
end of that is tested in test_properties_panel.py. The pieces that make that possible are testable
apart from the display — the section slugs the descriptor carries and
the rows the sidebar grows under a table — and the wiring itself is
checked against a real Sidebar where GTK has a display to open.
"""

from __future__ import annotations

import sqlite3

import pytest

from sqlide.backend.db import objects, registry
from sqlide.backend.db.base import Connector
from sqlide.backend.db.sqlite.connector import SqliteConnector


@pytest.fixture()
def sqlite_db(tmp_path):
    path = tmp_path / "deeplink.db"
    sqlite3.connect(path).close()
    connector = SqliteConnector(str(path))
    connector.connect()
    connector.execute(
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, sku TEXT NOT NULL)"
    )
    connector.execute("CREATE INDEX orders_sku ON orders (sku)")
    yield connector
    connector.close()


# The descriptor side: sections are addressable


def test_every_property_section_carries_its_slug(
    sqlite_db: Connector,
) -> None:
    sections = registry.property_sections("sqlite")
    info = objects.table_properties(sqlite_db, "orders", sections)
    slugs = [table.slug for table in info.tables]
    assert slugs and all(slug in dict(objects.PROPERTY_SECTIONS)
                         for slug in slugs)
    # Every list section the engine offers is drawn and addressable.
    assert slugs == [
        slug for slug in sections if slug not in ("general", "ddl")
    ]


def test_section_child_kinds_are_real_sections() -> None:
    known = dict(objects.PROPERTY_SECTIONS)
    assert set(objects.SECTION_CHILD_KINDS) <= set(known)
    assert "general" not in objects.SECTION_CHILD_KINDS
    assert "ddl" not in objects.SECTION_CHILD_KINDS


# The sidebar side: a table's children are its sections


@pytest.fixture()
def sidebar(sqlite_db, tmp_path):
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    if not Gtk.init_check():
        pytest.skip("no display for GTK")
    from sqlide.backend.connections import ConnectionProfile
    from sqlide.frontend.sidebar import Node, Sidebar

    def unused(*_args, **_kwargs):
        raise AssertionError("callback should not fire")

    opened: list[tuple] = []
    profile = ConnectionProfile("shop", "sqlite", file_path=":memory:")
    bar = Sidebar(
        ensure_connector=lambda _profile: sqlite_db,
        on_open_table=lambda p, table: opened.append(("table", table)),
        on_open_object=unused,
        on_open_section=lambda p, table, slug: opened.append(
            ("section", table, slug)
        ),
        on_new_query=unused,
        on_open_cli=unused,
        on_open_definition=unused,
        on_edit_table=unused,
        on_open_function=unused,
        on_relation_graph=unused,
        on_view_indexes=unused,
        on_query_builder=unused,
        on_drop_object=unused,
        on_new_object=unused,
        on_mcp_server=unused,
        on_manage_users=unused,
        on_monitor=unused,
        on_open_schema=unused,
        on_edit_connection=unused,
        on_disconnect=unused,
        on_close_tabs=unused,
        count_tabs=lambda _name: 0,
        on_remove_connection=unused,
        on_add_connection=unused,
        show_error=unused,
    )
    bar.add_profile(profile)
    return bar, Node("table", "orders", profile=profile), opened, Node


def test_a_table_grows_one_row_per_property_section(sidebar) -> None:
    bar, table, _opened, _Node = sidebar
    store = bar._create_children(table)
    slugs = [store.get_item(i).category for i in range(store.get_n_items())]
    sections = registry.property_sections("sqlite")
    assert slugs == [
        slug for slug in sections if slug not in ("general", "ddl")
    ]
    # Every child maps to a section, and none is a dead end.
    for i in range(store.get_n_items()):
        node = store.get_item(i)
        assert node.kind == "section"
        assert node.table == "orders"
        assert node.label == dict(objects.PROPERTY_SECTIONS)[node.category]


def test_activating_a_section_deep_links_into_properties(sidebar) -> None:
    bar, table, opened, _Node = sidebar
    store = bar._create_children(table)
    indexes = [
        store.get_item(i) for i in range(store.get_n_items())
        if store.get_item(i).category == "indexes"
    ][0]
    bar._menu_node = indexes
    bar._menu_view_section()
    assert opened == [("section", "orders", "indexes")]


def test_object_sections_expand_into_their_children(sidebar) -> None:
    bar, table, _opened, _Node = sidebar
    store = bar._create_children(table)
    by_slug = {
        store.get_item(i).category: store.get_item(i)
        for i in range(store.get_n_items())
    }
    # Columns, Indexes and Triggers hold objects of their own…
    assert bar._create_children(by_slug["columns"]) is not None
    assert bar._create_children(by_slug["indexes"]) is not None
    # …a section that is only a heading in the Properties view does not.
    assert bar._create_children(by_slug["constraints"]) is None


def test_a_named_section_is_scrolled_to_and_marked(sidebar) -> None:
    gi = pytest.importorskip("gi")
    gi.require_version("Adw", "1")
    from sqlide.frontend.object_info import InfoBody

    body = InfoBody(lambda _ref: None, summary_title="General")
    # The link arrives before the catalog read finishes…
    body.select_section("indexes")
    body.render(objects.ObjectInfo(
        kind="table", name="orders", type_label="Table", path="",
        summary=[("Columns", "2")],
        tables=[objects.DetailTable(
            title="Indexes", columns=["Name"], rows=[("orders_sku",)],
            slug="indexes",
        )],
    ))
    # …and is applied to the section once it is on screen.
    group = body._sections["indexes"]
    assert group.has_css_class("section-target")
    body.select_section("general")
    assert not group.has_css_class("section-target")
    assert body._sections["general"].has_css_class("section-target")
