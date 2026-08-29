"""Closing every tab on one connection (CORE-07).

Close all related tabs joins Disconnect on the connection row's
context menu, carrying the number of tabs the window would close and
dead when that number is zero. Tabs holding work that was never
written are listed in one confirmation before any of them goes, and
Save means each tab writes what it has: a console writes its editor
to its file (a scratch .sql file when it never had one) and a table
tab applies its pending cell edits.

The pieces are testable apart from the window: the menu the sidebar
builds from a tab count, the action's enabled state, and each tab
type's answer to "what would closing you lose?".
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from sqlide.backend.db.sqlite.connector import SqliteConnector


@pytest.fixture()
def sqlite_db(tmp_path):
    path = tmp_path / "tabs.db"
    sqlite3.connect(path).close()
    connector = SqliteConnector(str(path))
    connector.connect()
    connector.execute(
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, item TEXT)"
    )
    connector.execute("INSERT INTO orders (item) VALUES ('mug')")
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
def profile():
    from sqlide.backend.connections import ConnectionProfile

    return ConnectionProfile("shop", "sqlite", file_path=":memory:")


def make_sidebar(gtk, sqlite_db, profile, tab_count: int):
    """A sidebar whose window reports `tab_count` open tabs on the
    connection, plus the list the menu item appends its profile to."""
    from sqlide.frontend.sidebar import Sidebar

    def unused(*_args, **_kwargs):
        raise AssertionError("callback should not fire")

    closed: list[str] = []
    bar = Sidebar(
        ensure_connector=lambda _profile: sqlite_db,
        on_open_table=unused,
        on_open_object=unused,
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
        on_close_tabs=lambda p: closed.append(p.name),
        count_tabs=lambda _name: tab_count,
        on_remove_connection=unused,
        on_add_connection=unused,
        show_error=unused,
    )
    bar.add_profile(profile)
    return bar, closed


def _labels(menu) -> list[str]:
    out = []
    for i in range(menu.get_n_items()):
        value = menu.get_item_attribute_value(i, "label", None)
        if value is not None:
            out.append(value.get_string())
    return out


# The menu


def test_the_item_carries_the_tab_count(gtk, sqlite_db, profile) -> None:
    bar, _closed = make_sidebar(gtk, sqlite_db, profile, 7)
    node = bar._roots.get_item(0)
    assert "Close all 7 related tabs" in _labels(bar._menu_for(node))


def test_one_tab_reads_as_one_tab(gtk, sqlite_db, profile) -> None:
    bar, _closed = make_sidebar(gtk, sqlite_db, profile, 1)
    node = bar._roots.get_item(0)
    assert "Close the 1 related tab" in _labels(bar._menu_for(node))


def test_only_a_connection_row_offers_it(gtk, sqlite_db, profile) -> None:
    from sqlide.frontend.sidebar import Node

    bar, _closed = make_sidebar(gtk, sqlite_db, profile, 3)
    table = Node("table", "orders", profile=profile)
    assert not [
        label
        for label in _labels(bar._menu_for(table))
        if label.startswith("Close ")
    ]


def test_it_is_dead_without_tabs_and_live_with_them(
    gtk, sqlite_db, profile
) -> None:
    bar, _closed = make_sidebar(gtk, sqlite_db, profile, 0)
    node = bar._roots.get_item(0)
    bar.set_menu_node(node)
    assert not bar._actions.lookup_action("close-tabs").get_enabled()

    bar, _closed = make_sidebar(gtk, sqlite_db, profile, 2)
    node = bar._roots.get_item(0)
    bar.set_menu_node(node)
    assert bar._actions.lookup_action("close-tabs").get_enabled()


def test_taking_it_asks_the_window(gtk, sqlite_db, profile) -> None:
    bar, closed = make_sidebar(gtk, sqlite_db, profile, 2)
    node = bar._roots.get_item(0)
    bar.set_menu_node(node)
    bar._menu_close_tabs()
    assert closed == [profile.name]


# What a console would lose


def _console(gtk):
    from sqlide.frontend.query_console import QueryConsole

    return QueryConsole(
        connection_names=gtk.StringList.new(["shop"]),
        find_connection=lambda _name: None,
        ensure_connector=lambda _profile: None,
    )


def test_an_empty_console_has_nothing_to_lose(gtk) -> None:
    console = _console(gtk)
    assert console.unsaved_work() == ""
    console._editor.set_text("   \n")
    assert console.unsaved_work() == ""


def test_typed_sql_with_no_file_is_unsaved_work(gtk) -> None:
    console = _console(gtk)
    console._editor.set_text("SELECT 1;")
    assert console.unsaved_work() == "unsaved query text"


def test_saving_a_console_with_no_file_writes_a_scratch_file(gtk) -> None:
    console = _console(gtk)
    console._editor.set_text("SELECT 1;")
    console.save_unsaved_work()
    path = console._file_path
    assert path is not None
    try:
        assert path.read_text(encoding="utf-8") == "SELECT 1;"
    finally:
        path.unlink(missing_ok=True)
    # Written out, so there is nothing left to lose.
    assert console.unsaved_work() == ""


def test_a_console_matching_its_file_has_nothing_to_lose(
    gtk, tmp_path
) -> None:
    console = _console(gtk)
    target = tmp_path / "report.sql"
    console._editor.set_text("SELECT 1;")
    console._write_file(target)
    assert console.unsaved_work() == ""

    console._editor.set_text("SELECT 2;")
    assert console.unsaved_work() == "unsaved changes to report.sql"

    console.save_unsaved_work()
    assert target.read_text(encoding="utf-8") == "SELECT 2;"
    assert console.unsaved_work() == ""
    assert Path(console._file_path) == target


# What a table tab would lose
#
# A whole TableTab loads itself on a worker thread the moment it is
# built, so these bind its two methods onto a stand-in holding just
# the state they read: the pending edits and the way to the connector.


def _pending_update(pk_values, column, value):
    """One row's pending cell edit, as TableTab holds it."""
    from sqlide.frontend.data_grid import _PendingRow

    return _PendingRow("update", pk_values, {column: value})


class _GridStub:
    """The parts of TableTab that unsaved_work and save_unsaved_work
    touch, and nothing else."""

    from sqlide.frontend.data_grid import TableTab as _real

    table = "orders"
    profile = None
    unsaved_work = _real.unsaved_work
    save_unsaved_work = _real.save_unsaved_work
    _pending_updates = _real._pending_updates
    _pending_count = _real._pending_count
    del _real

    def __init__(self, connector=None, pending=None) -> None:
        self._pending = pending or {}
        self._ensure = lambda _profile: connector
        self._show_error = lambda message: None

    def _update_save_button(self) -> None:
        pass


def test_a_clean_grid_has_nothing_to_lose(gtk) -> None:
    assert _GridStub().unsaved_work() == ""


def test_pending_cell_edits_are_unsaved_work(gtk) -> None:
    tab = _GridStub(pending={object(): _pending_update({"id": 1}, "item", "cup")})
    assert tab.unsaved_work() == "1 unsaved edit(s)"


def test_saving_a_grid_writes_its_pending_edits(gtk) -> None:
    written: list[tuple] = []
    done = threading.Event()

    class Recording:
        def apply_changes(self, table, operations):
            written.extend(
                (table, op.pk_values, op.column, op.value)
                for op in operations
            )
            done.set()

    tab = _GridStub(
        connector=Recording(),
        pending={object(): _pending_update({"id": 1}, "item", "cup")},
    )
    tab.save_unsaved_work()
    assert done.wait(5)
    assert written == [("orders", {"id": 1}, "item", "cup")]
    # Nothing is left pending, so the tab can close without a question.
    assert tab.unsaved_work() == ""
