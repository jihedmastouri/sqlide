"""The resizable connections sidebar (CORE-08).

The width lives in settings.toml like every other preference, so it is
clamped on the way in from a hand-edited file exactly as it is on the
way out of a drag, and it survives a restart because the store wrote
it. The sidebar itself scrolls in both directions rather than
truncating a name to fit the width it was dragged to, and a name too
long for a narrow panel gets a tooltip.
"""

from __future__ import annotations

import pytest

from sqlide.backend.settings import (
    DEFAULT_SIDEBAR_WIDTH,
    SIDEBAR_MAX_WIDTH,
    SIDEBAR_MIN_WIDTH,
    Settings,
    SettingsStore,
    clamp_sidebar_width,
)


# The setting


def test_the_default_width_is_within_the_allowed_range() -> None:
    assert SIDEBAR_MIN_WIDTH <= DEFAULT_SIDEBAR_WIDTH <= SIDEBAR_MAX_WIDTH
    assert Settings().sidebar_width == DEFAULT_SIDEBAR_WIDTH


def test_a_width_is_held_to_its_limits() -> None:
    assert clamp_sidebar_width(10) == SIDEBAR_MIN_WIDTH
    assert clamp_sidebar_width(10_000) == SIDEBAR_MAX_WIDTH
    assert clamp_sidebar_width(320) == 320


def test_an_out_of_range_width_on_disk_loads_clamped(tmp_path) -> None:
    path = tmp_path / "settings.toml"
    path.write_text("sidebar_width = 4000\n", encoding="utf-8")
    assert SettingsStore(path).load().sidebar_width == SIDEBAR_MAX_WIDTH


def test_a_width_that_is_not_a_number_falls_back(tmp_path) -> None:
    path = tmp_path / "settings.toml"
    path.write_text('sidebar_width = "wide"\ntheme = "dark"\n', "utf-8")
    settings = SettingsStore(path).load()
    assert settings.sidebar_width == DEFAULT_SIDEBAR_WIDTH
    assert settings.theme == "dark"  # the rest of the file still applies


def test_the_width_survives_a_restart(tmp_path) -> None:
    path = tmp_path / "settings.toml"
    store = SettingsStore(path)
    store.load()
    store.update(sidebar_width=345)
    assert SettingsStore(path).load().sidebar_width == 345


# The sidebar widget


@pytest.fixture()
def gtk():
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gtk

    if not Gtk.init_check():
        pytest.skip("no display for GTK")
    return Gtk


def make_sidebar():
    from sqlide.frontend.sidebar import Sidebar

    def unused(*_args, **_kwargs):
        raise AssertionError("callback should not fire")

    return Sidebar(
        ensure_connector=unused,
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
        on_close_tabs=unused,
        count_tabs=lambda _name: 0,
        on_remove_connection=unused,
        on_add_connection=unused,
        show_error=unused,
    )


def test_the_tree_scrolls_both_ways(gtk) -> None:
    bar = make_sidebar()
    assert bar.get_policy() == (
        gtk.PolicyType.AUTOMATIC,
        gtk.PolicyType.AUTOMATIC,
    )


def test_row_labels_are_not_ellipsized(gtk) -> None:
    from gi.repository import Pango

    bar = make_sidebar()
    item = gtk.ListItem()
    # Rows are built by the factory's setup handler; the label it
    # keeps on the item is the one a long name must not be cut in.
    bar._setup_row(None, item)
    assert item.label.get_ellipsize() == Pango.EllipsizeMode.NONE


def test_a_long_name_gets_a_tooltip_of_its_own(gtk) -> None:
    from sqlide.frontend.sidebar import Node, _name_tooltip

    tooltip = gtk.Tooltip()
    assert not _name_tooltip(Node("column", "id"), tooltip)
    assert _name_tooltip(
        Node("column", "customer_order_line_item_reference"), tooltip
    )


def test_the_secondary_text_ellipsizes_and_yields_the_width(gtk) -> None:
    """The name keeps its full width request; the detail beside it
    asks for next to nothing, so a long type or row count cannot push
    the tree wider than the sidebar the user sized (CORE-51)."""
    from gi.repository import Pango

    bar = make_sidebar()
    item = gtk.ListItem()
    bar._setup_row(None, item)
    detail = item.detail
    assert detail.get_ellipsize() == Pango.EllipsizeMode.END

    detail.set_text("character varying(255) collate en_US.utf8")
    minimum, natural, *_ = detail.measure(gtk.Orientation.HORIZONTAL, -1)
    item.label.set_text("customer_order_line_item_reference")
    name_minimum, *_ = item.label.measure(gtk.Orientation.HORIZONTAL, -1)
    # An ellipsis' worth, against a name that insists on all of itself.
    assert minimum < name_minimum / 4
    # And it never asks for the whole string either, however long.
    detail.set_text("character varying(255) collate en_US.utf8" * 4)
    assert detail.measure(gtk.Orientation.HORIZONTAL, -1)[1] == natural


def test_a_long_detail_barely_widens_the_row(gtk) -> None:
    """Measured on the row the factory builds — the same one at every
    depth, since indentation is the expander's business and the labels
    inside it are sized identically for a top-level connection and a
    column six levels down."""
    bar = make_sidebar()
    rows = []
    for detail in ("", "timestamp with time zone, not null, default now()"):
        item = gtk.ListItem()
        bar._setup_row(None, item)
        item.label.set_text("customer_order_line_item_reference")
        from sqlide.frontend.sidebar import _show_detail

        _show_detail(item.detail, detail)
        rows.append(
            item.get_child().measure(gtk.Orientation.HORIZONTAL, -1)[0]
        )
    plain, detailed = rows
    assert detailed - plain < 40  # an ellipsis and its spacing, no more


def test_the_full_detail_is_a_hover_away(gtk) -> None:
    from sqlide.frontend.sidebar import _show_detail

    bar = make_sidebar()
    item = gtk.ListItem()
    bar._setup_row(None, item)
    _show_detail(item.detail, "character varying(255)")
    assert item.detail.get_tooltip_text() == "character varying(255)"
    assert item.detail.get_visible()
    # Recycled into a row with nothing to say, it says nothing.
    _show_detail(item.detail, "")
    assert item.detail.get_tooltip_text() is None
    assert not item.detail.get_visible()


# The window's splitter


@pytest.fixture()
def window(gtk, tmp_path, monkeypatch):
    """A main window whose settings store writes to tmp_path, so the
    test's drags land in a throwaway settings.toml."""
    from sqlide.backend import settings as settings_backend
    from sqlide.backend.workspaces import Workspace

    store = SettingsStore(tmp_path / "settings.toml")
    store.load()
    monkeypatch.setattr(settings_backend, "store", store)
    from sqlide.frontend.application import SqlideApplication
    from sqlide.frontend.window import MainWindow

    app = SqlideApplication()
    app.workspace_store.directory = tmp_path / "workspaces"
    win = MainWindow(Workspace(name="test"), application=app)
    yield win, store
    win.destroy()


def test_the_sidebar_opens_at_the_remembered_width(window) -> None:
    win, store = window
    assert win._split.get_position() == store.settings.sidebar_width


def test_a_drag_past_the_limits_is_pulled_back(window) -> None:
    win, _store = window
    win._split.set_position(9000)
    assert win._split.get_position() == SIDEBAR_MAX_WIDTH
    win._split.set_position(20)
    assert win._split.get_position() == SIDEBAR_MIN_WIDTH


def test_a_drag_is_written_to_the_settings_file(window) -> None:
    win, store = window
    win._split.set_position(360)
    win._save_sidebar_width()  # what the debounce timer would run
    assert store.settings.sidebar_width == 360
    assert SettingsStore(store.path).load().sidebar_width == 360


def test_double_clicking_the_handle_restores_the_default(window) -> None:
    from gi.repository import Gtk

    win, _store = window
    win._split.set_position(420)
    gesture = Gtk.GestureClick()
    win._handle_clicked(gesture, 2, 421.0, 10.0)
    assert win._split.get_position() == DEFAULT_SIDEBAR_WIDTH


def test_a_double_click_inside_the_tree_changes_nothing(window) -> None:
    from gi.repository import Gtk

    win, _store = window
    win._split.set_position(420)
    gesture = Gtk.GestureClick()
    win._handle_clicked(gesture, 2, 100.0, 10.0)
    win._handle_clicked(gesture, 1, 421.0, 10.0)  # a single click, too
    assert win._split.get_position() == 420
