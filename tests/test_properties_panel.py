"""Properties live in the right side panel, not in a tab mode (CORE-47).

The Data | Properties toggle CORE-04 put inside the table tab is gone:
a table tab shows rows. What a table (or an index, or a function) is
made of is read beside the data, in the right side panel, which follows
whichever tab is active — and can be torn off into a window of its own
through the same machinery a dragged tab uses (CORE-52).

The tests run a real MainWindow over a SQLite file in tmp_path, with
the catalog reads made synchronous so a render finishes inside the
test, and a settings store pointed at tmp_path so the panel width the
drags leave behind lands in a throwaway settings.toml.
"""

from __future__ import annotations

import sqlite3

import pytest

from sqlide.backend.settings import (
    DEFAULT_SIDE_PANEL_WIDTH,
    SIDE_PANEL_MAX_WIDTH,
    SIDE_PANEL_MIN_WIDTH,
    Settings,
    SettingsStore,
    clamp_side_panel_width,
)


# The width, as a setting


def test_the_default_panel_width_is_within_the_allowed_range() -> None:
    assert (
        SIDE_PANEL_MIN_WIDTH
        <= DEFAULT_SIDE_PANEL_WIDTH
        <= SIDE_PANEL_MAX_WIDTH
    )
    assert Settings().side_panel_width == DEFAULT_SIDE_PANEL_WIDTH


def test_a_panel_width_is_held_to_its_limits() -> None:
    assert clamp_side_panel_width(10) == SIDE_PANEL_MIN_WIDTH
    assert clamp_side_panel_width(10_000) == SIDE_PANEL_MAX_WIDTH
    assert clamp_side_panel_width(420) == 420


def test_an_out_of_range_panel_width_on_disk_loads_clamped(
    tmp_path,
) -> None:
    path = tmp_path / "settings.toml"
    path.write_text("side_panel_width = 5000\n", encoding="utf-8")
    loaded = SettingsStore(path).load()
    assert loaded.side_panel_width == SIDE_PANEL_MAX_WIDTH


def test_the_panel_width_survives_a_restart(tmp_path) -> None:
    path = tmp_path / "settings.toml"
    store = SettingsStore(path)
    store.load()
    store.update(side_panel_width=421)
    assert SettingsStore(path).load().side_panel_width == 421


# The window


@pytest.fixture()
def gtk():
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gtk

    if not Gtk.init_check():
        pytest.skip("no display for GTK")
    return Gtk


def _inline(work, on_success, on_error):
    try:
        result = work()
    except Exception as exc:  # pragma: no cover - a test bug
        on_error(exc)
    else:
        on_success(result)


@pytest.fixture()
def window(gtk, tmp_path, monkeypatch):
    """A main window on one SQLite connection, with the catalog and
    grid reads run inline."""
    from sqlide.backend import settings as settings_backend
    from sqlide.backend.connections import ConnectionProfile
    from sqlide.backend.workspaces import Workspace
    from sqlide.frontend import data_grid, object_info

    db = tmp_path / "shop.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, sku TEXT)")
    conn.execute("CREATE INDEX orders_sku ON orders (sku)")
    conn.execute("CREATE TABLE customers (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    store = SettingsStore(tmp_path / "settings.toml")
    store.load()
    monkeypatch.setattr(settings_backend, "store", store)
    monkeypatch.setattr(object_info, "run_async", _inline)
    monkeypatch.setattr(data_grid, "run_async", _inline)

    from sqlide.frontend.application import SqlideApplication
    from sqlide.frontend.window import MainWindow

    profile = ConnectionProfile("shop", "sqlite", file_path=str(db))
    app = SqlideApplication()
    app.workspace_store.directory = tmp_path / "workspaces"
    workspace = Workspace(name="test", connections=[profile])
    win = MainWindow(workspace, application=app)
    yield win, profile, store
    win.destroy()


def _drain() -> None:
    """Run the pending idle callbacks — surfaces are released on idle,
    after the tab that closed has left the view."""
    from gi.repository import GLib

    context = GLib.MainContext.default()
    while context.pending():
        context.iteration(False)


def _panel(win):
    """The properties surface the panel is showing — one per object
    since CORE-50, so this is whichever one is in front."""
    return win._properties_view.current


# The tab no longer has a mode


def test_a_table_tab_has_no_properties_toggle(window) -> None:
    win, profile, _store = window
    tab = win.open_table(profile, "orders")
    assert not hasattr(tab, "_properties_toggle")
    assert not hasattr(tab, "show_properties")
    assert tab._stack.get_child_by_name("properties") is None
    assert tab._stack.get_visible_child_name() == "data"
    # And with no map to offer, the tab shows no switch at all.
    assert not tab._switch_row.get_visible()


# The panel follows the active tab


def test_the_panel_shows_the_active_tab_s_object(window) -> None:
    win, profile, _store = window
    win.open_table(profile, "orders")
    win._update_active_panel()
    assert _panel(win).ref.name == "orders"
    win.open_table(profile, "customers")
    win._update_active_panel()
    assert _panel(win).ref.name == "customers"


def test_the_panel_follows_a_non_table_object_too(window) -> None:
    from sqlide.backend.db import objects

    win, profile, _store = window
    win.open_object(profile, objects.ObjectRef(kind="index", name="orders_sku"))
    win._update_active_panel()
    view = _panel(win)
    assert (view.ref.kind, view.ref.name) == ("index", "orders_sku")


def test_a_tab_about_no_object_clears_the_panel(window) -> None:
    win, profile, _store = window
    win.open_table(profile, "orders")
    win._update_active_panel()
    win.new_query(profile)
    win._update_active_panel()
    assert _panel(win) is None


def test_the_properties_page_is_offered_in_every_context(window) -> None:
    from sqlide.frontend.side_panel import _CONTEXT_PAGES

    assert all("properties" in pages for pages in _CONTEXT_PAGES.values())


# Deep links (CORE-05) land on the panel


def test_a_deep_link_reveals_the_panel_on_its_section(window) -> None:
    win, profile, _store = window
    win.open_table_section(profile, "orders", "indexes")
    assert win._side_panel.get_visible()
    assert win._side_panel._stack.get_visible_child_name() == "properties"
    view = _panel(win)
    assert view.ref.name == "orders"
    assert view._body._sections["indexes"].has_css_class("section-target")
    # The data tab is open behind it, still showing data.
    tab = win._tab_for(("table", profile.name, "orders"))
    assert tab is not None and tab._stack.get_visible_child_name() == "data"


def test_a_deep_link_lands_in_an_open_properties_window(window) -> None:
    from sqlide.backend.db import objects

    win, profile, _store = window
    ref = objects.ObjectRef(kind="table", name="orders")
    view = win.open_properties_tab(profile, ref)
    win.open_table_section(profile, "orders", "indexes")
    assert view._body._sections["indexes"].has_css_class("section-target")


# Detached windows


def test_properties_open_in_a_window_of_their_own(window) -> None:
    from sqlide.backend.db import objects
    from sqlide.frontend.object_info import PropertiesView

    win, profile, _store = window
    ref = objects.ObjectRef(kind="table", name="orders")
    win.open_properties_window(profile, ref)
    popouts = win._popouts
    assert len(popouts) == 1
    page = popouts[0].pane.view.get_nth_page(0)
    view = page.get_child()
    assert isinstance(view, PropertiesView)
    assert view.ref.name == "orders"


def test_a_properties_window_outlives_the_tab_it_came_from(window) -> None:
    from sqlide.backend.db import objects

    win, profile, _store = window
    tab = win.open_table(profile, "orders")
    win.open_properties_window(
        profile, objects.ObjectRef(kind="table", name="orders")
    )
    pane = win._panes[0]
    page = next(
        pane.view.get_nth_page(i)
        for i in range(pane.view.get_n_pages())
        if pane.view.get_nth_page(i).get_child() is tab
    )
    pane.view.close_page(page)
    view = win._popouts[0].pane.view.get_nth_page(0).get_child()
    assert view.ref.name == "orders"
    assert view.profile.name == profile.name


def test_the_same_object_reuses_its_properties_surface(window) -> None:
    from sqlide.backend.db import objects

    win, profile, _store = window
    ref = objects.ObjectRef(kind="table", name="orders")
    first = win.open_properties_tab(profile, ref)
    second = win.open_properties_tab(profile, ref)
    assert first is second


def test_a_properties_window_is_not_saved_with_the_workspace(
    window,
) -> None:
    from sqlide.backend.db import objects

    win, profile, _store = window
    win.open_properties_tab(
        profile, objects.ObjectRef(kind="table", name="orders")
    )
    win._save_state()
    assert all(tab.kind != "properties" for tab in win.workspace.tabs)


# Surfaces are per object (CORE-50)


def _surfaces(win):
    return win._properties_view


def test_a_second_object_gets_its_own_surface(window) -> None:
    win, profile, _store = window
    win.open_table(profile, "orders")
    win._update_active_panel()
    first = _panel(win)
    win.open_table(profile, "customers")
    win._update_active_panel()
    second = _panel(win)
    assert first is not second
    # A's surface is untouched by B being opened.
    assert first.ref.name == "orders"
    assert second.ref.name == "customers"


def test_coming_back_to_an_object_focuses_its_surface(window) -> None:
    win, profile, _store = window
    win.open_table(profile, "orders")
    win._update_active_panel()
    first = _panel(win)
    win.open_table(profile, "customers")
    win._update_active_panel()
    win._focus_tab(("table", profile.name, "orders"))
    win._update_active_panel()
    assert _panel(win) is first


def test_two_detached_windows_show_different_objects(window) -> None:
    from sqlide.backend.db import objects

    win, profile, _store = window
    win.open_properties_window(
        profile, objects.ObjectRef(kind="table", name="orders")
    )
    win.open_properties_window(
        profile, objects.ObjectRef(kind="table", name="customers")
    )
    views = [
        popout.pane.view.get_nth_page(0).get_child()
        for popout in win._popouts
    ]
    assert len(views) == 2
    assert {view.ref.name for view in views} == {"orders", "customers"}
    assert views[0] is not views[1]


def test_closing_one_surface_leaves_the_other(window) -> None:
    from sqlide.backend.db import objects

    win, profile, _store = window
    kept = win.open_properties_tab(
        profile, objects.ObjectRef(kind="table", name="orders")
    )
    doomed = win.open_properties_tab(
        profile, objects.ObjectRef(kind="table", name="customers")
    )
    pane = win._panes[0]
    page = next(
        pane.view.get_nth_page(i)
        for i in range(pane.view.get_n_pages())
        if pane.view.get_nth_page(i).get_child() is doomed
    )
    pane.view.close_page(page)
    _drain()
    assert kept.ref.name == "orders"
    assert kept.get_parent() is not None


def test_a_surface_is_released_when_its_tab_closes(window) -> None:
    win, profile, _store = window
    tab = win.open_table(profile, "orders")
    win._update_active_panel()
    assert _panel(win).ref.name == "orders"
    pane = win._panes[0]
    page = next(
        pane.view.get_nth_page(i)
        for i in range(pane.view.get_n_pages())
        if pane.view.get_nth_page(i).get_child() is tab
    )
    pane.view.close_page(page)
    _drain()
    assert _surfaces(win)._views == {}


def test_a_surface_is_kept_while_another_tab_is_about_it(window) -> None:
    win, profile, _store = window
    tab = win.open_table(profile, "orders")
    win._update_active_panel()
    kept = _panel(win)
    win.open_definition(profile, "orders")  # same object, second tab
    pane = win._panes[0]
    page = next(
        pane.view.get_nth_page(i)
        for i in range(pane.view.get_n_pages())
        if pane.view.get_nth_page(i).get_child() is tab
    )
    pane.view.close_page(page)
    _drain()
    assert kept in _surfaces(win)._views.values()


def test_surfaces_do_not_grow_without_bound(window) -> None:
    from sqlide.backend.db import objects

    win, profile, _store = window
    surfaces = _surfaces(win)
    for i in range(surfaces._max + 4):
        surfaces.set_target(
            profile, objects.ObjectRef(kind="index", name=f"idx{i}")
        )
    assert len(surfaces._views) <= surfaces._max
    # The one on screen is the newest, and still live.
    assert surfaces.current.ref.name == f"idx{surfaces._max + 3}"


# The panel's width is a preference


def test_the_panel_opens_at_the_remembered_width(window) -> None:
    win, _profile, store = window
    store.update(side_panel_width=420)
    assert win._remembered_panel_width() == 420
    # A hand-edited file out of range is pulled back the same way the
    # sidebar's width is.
    store.update(side_panel_width=SIDE_PANEL_MAX_WIDTH + 500)
    assert win._remembered_panel_width() == SIDE_PANEL_MAX_WIDTH


def test_an_unallocated_panel_writes_no_width(window) -> None:
    """The divider is asked for its position on every notify, including
    before the window has been allocated; a width of zero is not a drag
    and must not overwrite what is on file."""
    win, _profile, store = window
    store.update(side_panel_width=420)
    win._set_side_panel_shown(True)
    win._save_side_panel_width()
    assert store.settings.side_panel_width == 420


# The sidebar's menu


def test_a_row_offers_properties_in_the_panel_or_a_window(window) -> None:
    from sqlide.frontend.sidebar import Node

    win, profile, _store = window
    node = Node("table", "orders", profile=profile)
    menu = win._sidebar._menu_for(node)
    labels = [
        menu.get_item_attribute_value(i, "label", None).get_string()
        for i in range(menu.get_n_items())
        if menu.get_item_attribute_value(i, "label", None) is not None
    ]
    assert labels[:4] == [
        "Open",
        "Open (Window)",
        "Properties",
        "Properties (Window)",
    ]


def test_a_section_row_targets_its_table_on_that_section(window) -> None:
    from sqlide.frontend.sidebar import Node

    win, profile, _store = window
    node = Node(
        "section", "Indexes", profile=profile, category="indexes",
        table="orders",
    )
    target = win._sidebar.properties_target(node)
    assert target is not None
    _profile, ref, section = target
    assert (ref.kind, ref.name, section) == ("table", "orders", "indexes")


def test_a_placeholder_row_has_no_properties(window) -> None:
    from sqlide.frontend.sidebar import Node

    win, _profile, _store = window
    assert win._sidebar.properties_target(Node("note", "(none)")) is None
