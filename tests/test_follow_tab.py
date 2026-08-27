"""The sidebar follows the active tab (CORE-55).

Switching tabs highlights the object that tab is showing: the rows on
the way to it are expanded, the row is selected, and a tab with no
object behind it clears the highlight instead of leaving a stale one.
What is asserted beyond that is the two things the ticket asks the
feature not to do — walk sideways into folders nobody asked about, and
move a scroll position that is already showing the row — plus the
switch that turns the whole thing off.

Everything runs on a SQLite file in tmp_path, with the sidebar's
worker threads made synchronous so a walk finishes inside the test.
"""

from __future__ import annotations

import sqlite3

import pytest

from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.db.sqlite.connector import SqliteConnector
from sqlide.backend.settings import Settings


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
    conn.execute("CREATE TABLE customers (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE VIEW recent AS SELECT * FROM orders")
    yield conn
    conn.close()


@pytest.fixture()
def sidebar(gtk, connector, monkeypatch):
    """A sidebar on one SQLite connection whose loads run inline, so a
    reveal resolves without a main loop."""
    from sqlide.frontend import sidebar as sidebar_module

    def inline(work, on_success, on_error):
        try:
            result = work()
        except Exception as exc:  # pragma: no cover - a test bug
            on_error(exc)
        else:
            on_success(result)

    monkeypatch.setattr(sidebar_module, "run_async", inline)

    def unused(*_args, **_kwargs):
        raise AssertionError("following a tab must open nothing")

    profile = ConnectionProfile("shop", "sqlite", file_path=":memory:")
    bar = sidebar_module.Sidebar(
        ensure_connector=lambda _profile: connector,
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
    return bar, profile


def _follow(bar, target) -> None:
    """follow_object() with its debounce spent — the tab switch has
    settled and the walk runs."""
    from gi.repository import GLib

    bar.follow_object(target)
    if bar._follow_source:
        GLib.source_remove(bar._follow_source)
        bar._follow_source = 0
        bar._follow_step()


def _selected(bar):
    from gi.repository import Gtk

    position = bar._view.get_model().get_selected()
    if position == Gtk.INVALID_LIST_POSITION:
        return None
    return bar._tree.get_row(position).get_item()


def _child(node, label: str):
    from sqlide.frontend.sidebar import _items

    return next((n for n in _items(node.store) if n.label == label), None)


# Revealing


def test_a_table_tab_selects_its_row(sidebar) -> None:
    bar, _profile = sidebar
    _follow(bar, ("shop", "table", "orders"))
    node = _selected(bar)
    assert node is not None
    assert (node.kind, node.label) == ("table", "orders")


def test_the_rows_on_the_way_are_expanded(sidebar) -> None:
    bar, _profile = sidebar
    _follow(bar, ("shop", "table", "orders"))
    root = bar._roots.get_item(0)
    assert bar._row_for(root).get_expanded()
    tables = _child(root, "Tables")
    assert bar._row_for(tables).get_expanded()


def test_a_view_is_found_in_its_own_folder(sidebar) -> None:
    bar, _profile = sidebar
    _follow(bar, ("shop", "view", "recent"))
    assert _selected(bar).label == "recent"


def test_a_qualified_name_still_matches_the_row(sidebar) -> None:
    """A tab opened as "main.orders" is the row called "orders"."""
    bar, _profile = sidebar
    _follow(bar, ("shop", "table", "main.orders"))
    assert _selected(bar).label == "orders"


def test_switching_again_moves_the_highlight(sidebar) -> None:
    bar, _profile = sidebar
    _follow(bar, ("shop", "table", "orders"))
    _follow(bar, ("shop", "table", "customers"))
    assert _selected(bar).label == "customers"


def test_an_unknown_object_leaves_the_selection_alone(sidebar) -> None:
    bar, _profile = sidebar
    _follow(bar, ("shop", "table", "orders"))
    _follow(bar, ("shop", "table", "nope"))
    assert _selected(bar).label == "orders"


def test_an_unknown_connection_does_nothing(sidebar) -> None:
    bar, _profile = sidebar
    _follow(bar, ("elsewhere", "table", "orders"))
    assert _selected(bar) is None


# Not fighting the user


def test_a_tab_with_no_object_clears_the_highlight(sidebar) -> None:
    """A query console is about no one row; the last one must not go on
    looking current."""
    bar, _profile = sidebar
    _follow(bar, ("shop", "table", "orders"))
    _follow(bar, None)
    assert _selected(bar) is None


def test_only_the_folders_on_the_path_are_loaded(sidebar) -> None:
    """No cascade: revealing a table asks the connection for its
    listing and nothing else — Functions and Indexes stay unloaded."""
    bar, _profile = sidebar
    _follow(bar, ("shop", "table", "orders"))
    root = bar._roots.get_item(0)
    for label in ("Functions", "Indexes"):
        folder = _child(root, label)
        if folder is not None:
            assert not folder.loaded, label


def test_a_row_already_on_screen_is_not_scrolled_to(sidebar) -> None:
    """The reveal moves the scroll only for a row the user cannot
    already see, so a tab switch never yanks the tree out from under
    them."""
    bar, _profile = sidebar
    scrolled: list[int] = []
    bar._view.scroll_to = lambda position, *_args: scrolled.append(position)

    _follow(bar, ("shop", "table", "orders"))
    assert scrolled, "an off-screen row is scrolled into view"

    scrolled.clear()
    bar._bound_rows.update(
        bar._tree.get_row(i) for i in range(bar._tree.get_n_items())
    )
    _follow(bar, ("shop", "table", "customers"))
    assert _selected(bar).label == "customers"
    assert scrolled == []


def test_the_setting_turns_it_off(sidebar, monkeypatch) -> None:
    from sqlide.backend.settings import store as settings_store

    bar, _profile = sidebar
    monkeypatch.setattr(
        settings_store,
        "settings",
        Settings(sidebar_follow_active_tab=False),
        raising=False,
    )
    bar._settings_changed(settings_store.settings)
    _follow(bar, ("shop", "table", "orders"))
    assert _selected(bar) is None


# The setting itself


def test_following_is_on_by_default() -> None:
    assert Settings().sidebar_follow_active_tab is True


def test_the_setting_reads_from_the_file() -> None:
    assert (
        Settings.from_dict(
            {"sidebar_follow_active_tab": False}
        ).sidebar_follow_active_tab
        is False
    )


# Which row a tab is about


def test_a_tab_key_names_the_row_to_follow() -> None:
    from sqlide.frontend.window import _follow_target

    class Tab:
        def __init__(self, key) -> None:
            self.tab_key = key

    assert _follow_target(Tab(("table", "shop", "orders"))) == (
        "shop", "table", "orders"
    )
    assert _follow_target(Tab(("definition", "shop", "orders"))) == (
        "shop", "table", "orders"
    )
    assert _follow_target(Tab(("function", "shop", "total"))) == (
        "shop", "function", "total"
    )
    assert _follow_target(
        Tab(("object", "shop", "index", "orders_pk", "orders"))
    ) == ("shop", "index", "orders_pk")


def test_a_console_has_no_row_to_follow() -> None:
    from sqlide.frontend.window import _follow_target

    class Tab:
        tab_key = ("history",)

    assert _follow_target(Tab()) is None
    assert _follow_target(object()) is None
    assert _follow_target(None) is None
