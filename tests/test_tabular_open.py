"""Tabular objects open in a tab of their own (CORE-56).

Clicking Indexes under a table used to route into the properties page
of the right side panel. It now opens what it plainly is — a listing —
as a tab whose body is the shared result grid, the way the data tab
shows rows; an object that is a single record still opens the info
view, and the panel is left doing its own job (CORE-47).

The choice is the capability layer's: `objects.shape_of` answers from
the kind alone, and `objects.grid_listing` decides for a descriptor,
with a documented fallback for a kind that declares neither shape.
"""

from __future__ import annotations

import sqlite3

import pytest

from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.db import objects, registry
from sqlide.backend.db.metadata import NodeRef
from sqlide.backend.db.sqlite.connector import SqliteConnector


@pytest.fixture()
def sqlite_db(tmp_path):
    path = tmp_path / "shop.db"
    sqlite3.connect(path).close()
    connector = SqliteConnector(str(path))
    connector.connect()
    connector.execute(
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, sku TEXT NOT NULL)"
    )
    connector.execute("CREATE INDEX orders_sku ON orders (sku)")
    yield connector
    connector.close()


# The classification


def test_collections_declare_themselves_tabular() -> None:
    assert objects.shape_of("section") == "tabular"
    assert objects.shape_of("category") == "tabular"
    assert objects.shape_of("index") == "scalar"
    assert objects.shape_of("table") == "scalar"
    # A kind no layer has heard of declares neither.
    assert objects.shape_of("widget") == ""


def test_a_scalar_kind_never_opens_as_a_grid() -> None:
    info = objects.ObjectInfo(
        kind="index", name="orders_sku", type_label="Index",
        tables=[objects.DetailTable(
            title="Columns", columns=["Name"], rows=[("sku",)], tabular=True
        )],
    )
    assert objects.grid_listing("index", info) is None


def test_an_undeclared_kind_falls_back_to_its_descriptor() -> None:
    """The documented fallback: a kind that declares neither shape
    opens as a grid only where its whole body is one listing and there
    is nothing else to lose."""
    listing = objects.DetailTable(
        title="Rows", columns=["Name"], rows=[("a",), ("b",)], tabular=True
    )
    bare = objects.ObjectInfo(
        kind="widget", name="w", type_label="Widget", tables=[listing]
    )
    assert objects.grid_listing("widget", bare) is listing
    # A summary, a definition or a second section is content the grid
    # would drop, so the info view keeps it.
    assert objects.grid_listing(
        "widget", objects.ObjectInfo(
            kind="widget", name="w", type_label="Widget",
            summary=[("Kind", "widget")], tables=[listing],
        )
    ) is None
    assert objects.grid_listing(
        "widget", objects.ObjectInfo(
            kind="widget", name="w", type_label="Widget",
            tables=[listing], ddl="CREATE WIDGET w",
        )
    ) is None


# The descriptor a section opens with


def test_a_section_is_described_as_its_listing(sqlite_db) -> None:
    provider = registry.create_provider("sqlite", sqlite_db)
    info = provider.describe(NodeRef(
        kind="section", name="Indexes", table="orders", category="indexes"
    ))
    listing = objects.grid_listing("section", info)
    assert listing is not None
    assert listing.tabular and listing.slug == "indexes"
    assert [row[0] for row in listing.rows] == ["orders_sku"]
    # Every row still opens the object it stands for.
    assert listing.link(0) == objects.ObjectRef(
        kind="index", name="orders_sku", table="orders"
    )


def test_a_folder_opens_as_a_grid_of_its_children(sqlite_db) -> None:
    provider = registry.create_provider("sqlite", sqlite_db)
    info = provider.describe(NodeRef(kind="category", name="Indexes",
                                     category="indexes"))
    assert objects.grid_listing("category", info) is not None


def test_a_single_object_opens_the_info_view(sqlite_db) -> None:
    provider = registry.create_provider("sqlite", sqlite_db)
    for ref in (
        NodeRef(kind="index", name="orders_sku", table="orders"),
        NodeRef(kind="table", name="orders"),
        NodeRef(kind="column", name="sku", table="orders"),
    ):
        info = provider.describe(ref)
        assert objects.grid_listing(ref.kind, info) is None, ref.kind


# The sidebar routes a section to a tab, not to the panel


@pytest.fixture()
def sidebar(sqlite_db):
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gtk

    if not Gtk.init_check():
        pytest.skip("no display for GTK")
    from sqlide.frontend.sidebar import Sidebar

    def unused(*_args, **_kwargs):
        raise AssertionError("callback should not fire")

    opened: list[tuple] = []
    profile = ConnectionProfile("shop", "sqlite", file_path=":memory:")
    bar = Sidebar(
        ensure_connector=lambda _profile: sqlite_db,
        on_open_table=unused,
        on_open_object=lambda p, ref, path="": opened.append(("object", ref)),
        on_open_section=unused,
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
    return bar, profile, opened


def _section_node(bar, profile, slug: str):
    from sqlide.frontend.sidebar import Node

    store = bar._create_children(Node("table", "orders", profile=profile))
    for index in range(store.get_n_items()):
        node = store.get_item(index)
        if node.category == slug:
            return node
    raise AssertionError(f"no {slug} row under the table")


def test_opening_a_section_opens_an_object_tab(sidebar) -> None:
    bar, profile, opened = sidebar
    bar.open_node(_section_node(bar, profile, "indexes"))
    assert opened == [("object", objects.ObjectRef(
        kind="section", name="Indexes", table="orders", category="indexes"
    ))]


def test_the_panel_route_is_still_on_the_menu(sidebar) -> None:
    """Nothing about opening the listing forces the panel; asking for
    the panel explicitly is what the row's own menu item is for."""
    bar, profile, _opened = sidebar
    node = _section_node(bar, profile, "indexes")
    labels = []
    menu = bar._menu_for(node)
    for index in range(menu.get_n_items()):
        value = menu.get_item_attribute_value(index, "label", None)
        if value is not None:
            labels.append(value.get_string())
    assert labels[:2] == ["Open", "Open (Window)"]
    assert "Open in Properties" in labels


# The tab itself


def test_a_listing_tab_shows_the_grid_and_an_object_tab_does_not(
    sidebar, sqlite_db
) -> None:
    bar, profile, _opened = sidebar
    from sqlide.frontend.object_info import ObjectInfoTab

    provider = registry.create_provider("sqlite", sqlite_db)

    def tab(ref: objects.ObjectRef) -> ObjectInfoTab:
        return ObjectInfoTab(
            profile, ref, lambda _p: sqlite_db, lambda _m: None,
            lambda *_args: None,
        )

    ref = objects.ObjectRef(
        kind="section", name="Indexes", table="orders", category="indexes"
    )
    listing = tab(ref)
    listing._render(provider.describe(NodeRef(
        kind="section", name="Indexes", table="orders", category="indexes"
    )))
    assert listing._stack.get_visible_child_name() == "grid"
    assert listing._grid is not None
    # Titled for the object and its parent.
    assert listing._title.get_label() == "orders · indexes"
    # A row opens the index it names.
    followed: list = []
    listing._grid._on_open_link = followed.append
    listing._grid._activate(0)
    assert followed == [objects.ObjectRef("index", "orders_sku", "orders")]
    # Sorting is the grid's own, on the rows it already holds.
    listing._grid._sort([("Name", True)])
    assert listing._grid._rows

    single = tab(objects.ObjectRef(kind="index", name="orders_sku",
                                   table="orders"))
    single._render(provider.describe(
        NodeRef(kind="index", name="orders_sku", table="orders")
    ))
    assert single._stack.get_visible_child_name() == "info"
