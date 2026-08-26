"""Disconnecting a connection from the sidebar (CORE-06).

Disconnect is a connection-row menu item that is only live while the
connection is open; taking it closes the session, folds the tree row
back up and leaves the tabs standing with a banner offering the way
back. The pieces are testable apart from the window: the menu the
sidebar builds, the action's enabled state, what collapsing does to a
cached node, the banner helper every tab gets, and the console's
"is a run in flight" answer.
"""

from __future__ import annotations

import sqlite3

import pytest

from sqlide.backend.db.sqlite.connector import SqliteConnector


@pytest.fixture()
def sqlite_db(tmp_path):
    path = tmp_path / "disconnect.db"
    sqlite3.connect(path).close()
    connector = SqliteConnector(str(path))
    connector.connect()
    connector.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY)")
    yield connector
    connector.close()


@pytest.fixture()
def gtk():
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gtk

    if not Gtk.init_check():
        pytest.skip("no display for GTK")
    return Gtk


@pytest.fixture()
def sidebar(gtk, sqlite_db):
    from sqlide.backend.connections import ConnectionProfile
    from sqlide.frontend.sidebar import Sidebar

    def unused(*_args, **_kwargs):
        raise AssertionError("callback should not fire")

    disconnected: list[str] = []
    profile = ConnectionProfile("shop", "sqlite", file_path=":memory:")
    bar = Sidebar(
        ensure_connector=lambda _profile: sqlite_db,
        on_open_table=unused,
        on_open_object=unused,
        on_open_section=unused,
        on_new_query=unused,
        on_open_cli=unused,
        on_open_definition=unused,
        on_open_function=unused,
        on_relation_graph=unused,
        on_view_indexes=unused,
        on_query_builder=unused,
        on_drop_object=unused,
        on_new_object=unused,
        on_mcp_server=unused,
        on_manage_users=unused,
        on_open_schema=unused,
        on_edit_connection=unused,
        on_disconnect=lambda p: disconnected.append(p.name),
        on_remove_connection=unused,
        on_add_connection=unused,
        show_error=unused,
    )
    bar.add_profile(profile)
    return bar, profile, disconnected


def _labels(menu) -> list[str]:
    out = []
    for i in range(menu.get_n_items()):
        value = menu.get_item_attribute_value(i, "label", None)
        if value is not None:
            out.append(value.get_string())
    return out


def test_a_connection_row_offers_disconnect(sidebar) -> None:
    bar, _profile, _seen = sidebar
    node = bar._roots.get_item(0)
    assert "Disconnect" in _labels(bar._menu_for(node))


def test_only_a_connection_row_offers_disconnect(sidebar) -> None:
    from sqlide.frontend.sidebar import Node

    bar, profile, _seen = sidebar
    table = Node("table", "orders", profile=profile)
    assert "Disconnect" not in _labels(bar._menu_for(table))


def test_disconnect_is_live_only_while_connected(sidebar) -> None:
    bar, profile, _seen = sidebar
    node = bar._roots.get_item(0)
    action = bar._actions.lookup_action("disconnect")

    bar.set_menu_node(node)
    assert not action.get_enabled()

    bar.set_connected(profile.name, True)
    bar.set_menu_node(node)
    assert action.get_enabled()

    bar.set_connected(profile.name, False)
    bar.set_menu_node(node)
    assert not action.get_enabled()


def test_taking_disconnect_asks_the_window(sidebar) -> None:
    bar, profile, seen = sidebar
    node = bar._roots.get_item(0)
    bar.set_connected(profile.name, True)
    bar.set_menu_node(node)
    bar._menu_disconnect()
    assert seen == [profile.name]


def test_collapsing_forgets_the_schema_it_cached(sidebar) -> None:
    from gi.repository import Gio

    from sqlide.frontend.sidebar import Node

    bar, profile, _seen = sidebar
    node = bar._roots.get_item(0)
    node.store = Gio.ListStore(item_type=Node)
    node.store.append(Node("category", "Tables", profile=profile))
    node.loaded = True
    node.ddl_kinds = ("table",)

    bar.collapse_connection(profile.name)

    assert node.store.get_n_items() == 0
    assert not node.loaded
    assert node.ddl_kinds == ()
    # Expanding again is a fresh load against a fresh session.
    assert bar._create_children(node) is node.store


# The tab side: a banner, not an exception


def test_a_tab_gets_a_reconnect_banner_and_loses_it_again(gtk) -> None:
    from sqlide.frontend import feedback

    tab = gtk.Box(orientation=gtk.Orientation.VERTICAL)
    tab.append(gtk.Label(label="rows"))
    banners: dict = {}
    clicked: list[bool] = []

    feedback.set_disconnected(
        tab, banners, "Disconnected from “shop”.",
        lambda: clicked.append(True),
    )
    banner = banners[tab]
    assert banner.get_revealed()
    assert banner.get_button_label() == "Reconnect"
    assert tab.get_first_child() is banner
    # The tab's own content is untouched.
    assert isinstance(banner.get_next_sibling(), gtk.Label)

    banner.emit("button-clicked")
    assert clicked == [True]

    feedback.set_disconnected(tab, banners, "", lambda: None)
    assert tab not in banners
    assert isinstance(tab.get_first_child(), gtk.Label)


def test_clearing_a_banner_a_tab_never_had_is_a_no_op(gtk) -> None:
    from sqlide.frontend import feedback

    tab = gtk.Box(orientation=gtk.Orientation.VERTICAL)
    feedback.set_disconnected(tab, {}, "", lambda: None)
    assert tab.get_first_child() is None


# The console side: is a run in flight?


def test_a_console_reports_a_run_in_flight(gtk) -> None:
    from sqlide.frontend.query_console import QueryConsole

    console = QueryConsole(
        connection_names=gtk.StringList.new(["shop"]),
        find_connection=lambda _name: None,
        ensure_connector=lambda _profile: None,
    )
    assert not console.is_running

    console._enter_running()
    assert console.is_running

    # Cancelling is still in flight until the result lands.
    console.cancel_run()
    assert console.is_running

    console._finish_job(console._job_id)
    assert not console.is_running
