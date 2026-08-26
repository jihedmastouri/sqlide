"""Small helpers shared across the app."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from gi.repository import Gio, GLib, Gtk

from sqlide.backend.settings import DEFAULT_FONT_SIZE, store


def describe(widget: Gtk.Widget, label: str) -> Gtk.Widget:
    """Give an icon-only control both a tooltip and an accessible
    label. A screen reader never sees the icon, so every button that
    shows no text has to carry its name this way; the two are the same
    string on purpose."""
    widget.set_tooltip_text(label)
    widget.update_property([Gtk.AccessibleProperty.LABEL], [label])
    return widget


def icon_button(
    icon_name: str,
    label: str,
    on_click: Callable[[], None] | None = None,
    *,
    flat: bool = False,
    toggle: bool = False,
) -> Gtk.Button | Gtk.ToggleButton:
    """An icon-only button that is named for anyone who cannot see the
    icon (see describe)."""
    button = (
        Gtk.ToggleButton(icon_name=icon_name)
        if toggle
        else Gtk.Button(icon_name=icon_name)
    )
    if flat:
        button.add_css_class("flat")
    if on_click is not None:
        button.connect("clicked", lambda *_: on_click())
    describe(button, label)
    return button


# The SQL editor's font size is a stepper rather than a trip through
# Preferences, because it is the one appearance setting people reach
# for mid-query. It lives in the console's own gear popover (see
# query_console._settings_button), not in the global menu: the theme
# stays in Preferences, where a setting you change twice a year
# belongs.

_FONT_RANGE = (6, 32)


def font_size_stepper() -> Gtk.Box:
    """Editor font size as a -/+ stepper, clamped to the same range the
    Preferences spin row uses. Writes straight to the settings store,
    which restyles every open editor live."""
    low, high = _FONT_RANGE
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
    label = Gtk.Label(hexpand=True)
    label.add_css_class("font-stepper-value")

    minus = icon_button("list-remove-symbolic", "Smaller Editor Font", flat=True)
    plus = icon_button("list-add-symbolic", "Larger Editor Font", flat=True)
    minus.add_css_class("circular")
    plus.add_css_class("circular")

    def show(size: int) -> None:
        label.set_text(f"{size} pt")
        minus.set_sensitive(size > low)
        plus.set_sensitive(size < high)

    def step(delta: int) -> None:
        size = min(high, max(low, store.settings.editor_font_size + delta))
        store.update(editor_font_size=size)
        show(size)

    minus.connect("clicked", lambda *_: step(-1))
    plus.connect("clicked", lambda *_: step(1))
    show(store.settings.editor_font_size or DEFAULT_FONT_SIZE)

    box.append(minus)
    box.append(label)
    box.append(plus)
    return box


def _app_menu_items(menu: Gio.Menu) -> None:
    """The four items every window's menu ends with (application-level
    actions, see application.py)."""
    menu.append("Preferences", "app.preferences")
    menu.append("Keyboard Shortcuts", "app.shortcuts")
    menu.append("Help", "app.help")
    menu.append("About sqlide", "app.about")


def main_menu_button() -> Gtk.MenuButton:
    """Hamburger button for the welcome page and the launcher, which
    have no workspace and no tabs: the application-level items only."""
    menu = Gio.Menu()
    _app_menu_items(menu)
    button = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
    describe(button, "Main menu")
    return button


def workspaces_button() -> Gtk.Button:
    """Sidebar's leading button: back out to the workspace launcher.
    Its own icon rather than a menu item, because switching workspace
    is the one navigation the sidebar offers and hiding it behind a
    hamburger made it unfindable."""
    button = Gtk.Button(icon_name="view-grid-symbolic")
    button.add_css_class("flat")
    button.connect(
        "clicked", lambda b: b.activate_action("app.show-launcher", None)
    )
    describe(button, "Workspaces")
    return button


def sidebar_menu_button() -> Gtk.MenuButton:
    """Settings button at the top of a main window's sidebar. The
    sidebar is the one control that is always on screen, so the
    application menu lives here rather than in the content header.

    Deliberately short: Split View is a button in the content header,
    Refresh Schemas is one beside the search icon, the XML transfer
    items are in Preferences, and Workspaces… is the icon next to this
    one — a menu is not a place to keep everything that had nowhere
    else to go."""
    menu = Gio.Menu()
    tabs = Gio.Menu()
    tabs.append("Query History", "win.history")
    tabs.append("Backups", "win.backups")
    tabs.append("Close All Tabs", "win.close-all-tabs")
    menu.append_section(None, tabs)
    _app_menu_items(menu)
    button = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
    describe(button, "Settings")
    return button


def open_workspace_from(source: Gtk.Window, workspace) -> None:
    """Open `workspace` and hand it the foreground, closing `source`
    (the home page, or the workspace launcher).

    A workspace with no connections yet is useless once opened, so
    this is also the one gate that enforces "add a connection first":
    it blocks here, before the main window ever appears, rather than
    inside it.

    The order matters. Closing `source` first gives focus back to
    whatever was behind it, and the workspace window — mapped a moment
    later, without a user event of its own to point at — stays where
    the compositor first put it, which is behind everything. So: open
    it, wait until it is on screen, then close `source` and present it
    once more, this time as the only window of the app that wants
    attention."""
    if not workspace.connections:
        _require_connection(source, workspace)
        return

    window = source.get_application().open_workspace(workspace)

    def foreground() -> bool:
        source.close()
        window.present()
        return GLib.SOURCE_REMOVE

    if window.get_mapped():
        GLib.idle_add(foreground)
        return
    handler = 0

    def mapped(*_args) -> None:
        window.disconnect(handler)
        GLib.idle_add(foreground)

    handler = window.connect("map", mapped)


def _require_connection(source: Gtk.Window, workspace) -> None:
    """A workspace can't be opened empty: put up the connection dialog
    and only proceed once one is actually saved. Cancelling leaves
    `source` (welcome page or launcher) on screen with the workspace
    still there, unopened, to try again."""
    from sqlide.frontend.connection_dialog import ConnectionDialog

    def on_save(profile) -> None:
        workspace.add_connection(profile)
        source.get_application().workspace_store.save(workspace)
        open_workspace_from(source, workspace)

    ConnectionDialog(on_save=on_save).present(source)


def run_async(
    work: Callable[[], Any],
    on_success: Callable[[Any], None],
    on_error: Callable[[Exception], None],
) -> None:
    """Run `work()` on a daemon thread and deliver the result (or the
    exception) back on the GTK main loop.

    Every connector call made from the UI must go through this so the
    main loop never blocks on the database.
    """

    def dispatch(callback: Callable, value: Any) -> bool:
        callback(value)
        return GLib.SOURCE_REMOVE

    def runner() -> None:
        try:
            result = work()
        except Exception as exc:
            GLib.idle_add(dispatch, on_error, exc)
        else:
            GLib.idle_add(dispatch, on_success, result)

    threading.Thread(target=runner, daemon=True).start()
