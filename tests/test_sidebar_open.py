"""Sidebar mouse behaviour: expand, open, and the Open menu (CORE-52).

A single click selects and expands and opens nothing; a double click
(or Enter) opens, focusing an already-open tab rather than stacking a
second copy (CORE-01); and every row that opens something offers Open
and Open (Window) at the top of its context menu. Open (Window) goes
through the window's own tear-out path, so there is one way a tab ends
up popped out.
"""

from __future__ import annotations

import sqlite3

import pytest

from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.db.sqlite.connector import SqliteConnector


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
def connector(tmp_path):
    path = tmp_path / "shop.db"
    sqlite3.connect(path).close()
    conn = SqliteConnector(str(path))
    conn.connect()
    conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY)")
    yield conn
    conn.close()


@pytest.fixture()
def sidebar(gtk, connector):
    """A sidebar on one SQLite connection, with every open callback
    recording instead of opening."""
    from sqlide.frontend.sidebar import Sidebar

    opened: list[tuple] = []
    windowed: list = []

    def unused(*_args, **_kwargs):
        raise AssertionError("callback should not fire")

    def record(name):
        def fire(*args, **_kwargs):
            opened.append((name, *args))

        return fire

    profile = ConnectionProfile("shop", "sqlite", file_path=":memory:")
    bar = Sidebar(
        ensure_connector=lambda _profile: connector,
        on_open_table=record("table"),
        on_open_object=record("object"),
        on_open_section=record("section"),
        on_new_query=unused,
        on_open_cli=unused,
        on_open_definition=unused,
        on_open_function=record("function"),
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
        on_open_window=lambda opener: (windowed.append(opener), opener())[0],
    )
    bar.add_profile(profile)
    return bar, profile, opened, windowed


class _ListItem:
    """The bit of Gtk.ListItem the row gestures use."""

    def __init__(self, row) -> None:
        self._row = row

    def get_item(self):
        return self._row


def _labels(menu) -> list[str]:
    out = []
    for index in range(menu.get_n_items()):
        value = menu.get_item_attribute_value(index, "label", None)
        if value is not None:
            out.append(value.get_string())
    return out


def _node(bar, label: str = "", kind: str = "", **kwargs):
    from sqlide.frontend.sidebar import Node

    return Node(kind, label, **kwargs)


# Clicking


def test_a_single_click_expands_without_opening(sidebar) -> None:
    bar, _profile, opened, _windowed = sidebar
    row = bar._tree.get_item(0)
    assert not row.get_expanded()

    bar._row_pressed(None, 1, 0.0, 0.0, _ListItem(row))

    assert row.get_expanded()
    assert opened == []


def test_a_second_single_click_collapses_it_again(sidebar) -> None:
    bar, _profile, opened, _windowed = sidebar
    row = bar._tree.get_item(0)
    item = _ListItem(row)
    bar._row_pressed(None, 1, 0.0, 0.0, item)
    bar._row_pressed(None, 1, 0.0, 0.0, item)
    assert not row.get_expanded()
    assert opened == []


def test_the_second_press_of_a_double_click_is_left_alone(sidebar) -> None:
    """That one is row activation; toggling again would undo the
    expansion the first press just made."""
    bar, _profile, _opened, _windowed = sidebar
    row = bar._tree.get_item(0)
    bar._row_pressed(None, 1, 0.0, 0.0, _ListItem(row))
    bar._row_pressed(None, 2, 0.0, 0.0, _ListItem(row))
    assert row.get_expanded()


def test_a_double_click_opens_the_row(sidebar) -> None:
    bar, profile, opened, _windowed = sidebar
    bar._on_activate(bar._view, 0)  # the connection row
    assert opened and opened[0][0] == "object"
    assert opened[0][1] is profile


def test_a_double_click_on_a_table_opens_its_data(sidebar) -> None:
    bar, profile, opened, _windowed = sidebar
    bar.open_node(_node(bar, "orders", "table", profile=profile))
    assert opened == [("table", profile, "orders")]


def test_a_placeholder_row_opens_nothing(sidebar) -> None:
    bar, profile, opened, _windowed = sidebar
    note = _node(bar, "Loading…", "note", profile=profile)
    assert not bar.is_openable(note)
    bar.open_node(note)
    assert opened == []


# The menu


def test_every_openable_row_leads_with_open(sidebar) -> None:
    bar, profile, _opened, _windowed = sidebar
    rows = [
        bar._roots.get_item(0),  # the connection
        _node(bar, "orders", "table", profile=profile),
        _node(bar, "Tables", "category", profile=profile, category="tables"),
        _node(bar, "id", "column", profile=profile, table="orders"),
    ]
    for node in rows:
        labels = _labels(bar._menu_for(node))
        assert labels[:2] == ["Open", "Open (Window)"], node.kind


def test_a_row_that_opens_nothing_has_neither(sidebar) -> None:
    bar, profile, _opened, _windowed = sidebar
    note = _node(bar, "(none)", "note", profile=profile)
    assert bar._menu_for(note) is None
    orphan = _node(bar, "orders", "table")  # no connection behind it
    assert "Open" not in _labels(bar._menu_for(orphan))


def test_open_on_the_menu_opens_the_row(sidebar) -> None:
    bar, profile, opened, _windowed = sidebar
    bar.set_menu_node(_node(bar, "orders", "table", profile=profile))
    bar._menu_open()
    assert opened == [("table", profile, "orders")]


def test_open_window_goes_through_the_windows_tear_out(sidebar) -> None:
    bar, profile, opened, windowed = sidebar
    bar.set_menu_node(_node(bar, "orders", "table", profile=profile))
    bar._menu_open_window()
    # One opener, handed to the window rather than opened a second way.
    assert len(windowed) == 1
    assert opened == [("table", profile, "orders")]


def test_without_a_window_open_window_still_opens(gtk, connector) -> None:
    """A harness that only walks the tree passes no on_open_window; the
    item then opens as a tab instead of doing nothing."""
    from sqlide.frontend.sidebar import Node, Sidebar

    opened: list[tuple] = []
    profile = ConnectionProfile("shop", "sqlite", file_path=":memory:")
    bar = Sidebar(
        ensure_connector=lambda _profile: connector,
        on_open_table=lambda *args: opened.append(args),
        on_open_object=lambda *args, **kw: None,
        on_open_section=lambda *args: None,
        on_new_query=lambda *args: None,
        on_open_cli=lambda *args: None,
        on_open_definition=lambda *args: None,
        on_open_function=lambda *args: None,
        on_relation_graph=lambda *args: None,
        on_view_indexes=lambda *args: None,
        on_query_builder=lambda *args, **kw: None,
        on_drop_object=lambda *args: None,
        on_new_object=lambda *args: None,
        on_mcp_server=lambda *args: None,
        on_manage_users=lambda *args: None,
        on_monitor=lambda *args: None,
        on_open_schema=lambda *args: None,
        on_edit_connection=lambda *args: None,
        on_disconnect=lambda *args: None,
        on_close_tabs=lambda *args: None,
        count_tabs=lambda _name: 0,
        on_remove_connection=lambda *args: None,
        on_add_connection=lambda: None,
        show_error=lambda _message: None,
    )
    bar.add_profile(profile)
    bar.open_node_in_window(Node("table", "orders", profile=profile))
    assert opened == [(profile, "orders")]


# The window's side of Open (Window)


@pytest.fixture()
def window(gtk, tmp_path, monkeypatch):
    from sqlide.backend import settings as settings_backend
    from sqlide.backend.settings import SettingsStore
    from sqlide.backend.workspaces import Workspace

    store = SettingsStore(tmp_path / "settings.toml")
    store.load()
    monkeypatch.setattr(settings_backend, "store", store)
    from sqlide.frontend.application import SqlideApplication
    from sqlide.frontend.window import MainWindow

    app = SqlideApplication()
    app.workspace_store.directory = tmp_path / "workspaces"
    win = MainWindow(Workspace(name="test"), application=app)
    yield win
    win.destroy()


def test_a_new_tab_opened_in_a_window_lands_in_a_popout(window, gtk) -> None:
    from gi.repository import Gtk

    win = window
    key = ("table", "shop", "orders")
    win.open_in_window(
        lambda: win._append_tab(Gtk.Label(label="orders"), key, "orders", "")
    )
    assert win._panes[0].view.get_n_pages() == 0
    assert len(win._popouts) == 1
    assert win._popouts[0].pane.view.get_n_pages() == 1


def test_an_open_tab_is_moved_rather_than_copied(window, gtk) -> None:
    from gi.repository import Gtk

    win = window
    key = ("table", "shop", "orders")
    win._append_tab(Gtk.Label(label="orders"), key, "orders", "")
    assert win._panes[0].view.get_n_pages() == 1

    # What opening an already-open object does (CORE-01): focus it.
    win.open_in_window(lambda: win._focus_tab(key))

    assert win._panes[0].view.get_n_pages() == 0
    assert len(win._popouts) == 1
    assert win._popouts[0].pane.view.get_n_pages() == 1


def test_opening_normally_still_makes_a_tab(window, gtk) -> None:
    from gi.repository import Gtk

    win = window
    win._append_tab(Gtk.Label(label="orders"), ("t", "shop", "o"), "o", "")
    assert win._panes[0].view.get_n_pages() == 1
    assert win._popouts == []
