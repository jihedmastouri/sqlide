"""Small helpers shared across the app."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from gi.repository import Gio, GLib, Gtk


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


def main_menu_button(with_history: bool = False) -> Gtk.MenuButton:
    """Hamburger button for a header bar: Preferences, Keyboard
    Shortcuts, Help and About (application-level actions, see
    application.py). Main windows also get Query History, the
    close-every-tab item and the XML transfer items, backed by
    window-level actions (the launcher has no workspace and no tabs,
    hence the flag)."""
    menu = Gio.Menu()
    if with_history:
        tabs = Gio.Menu()
        tabs.append("Split View", "win.split-view")
        tabs.append("Close All Tabs", "win.close-all-tabs")
        menu.append_section(None, tabs)
        connections = Gio.Menu()
        connections.append("Refresh Schemas", "win.refresh-schema")
        menu.append_section(None, connections)
        transfer = Gio.Menu()
        transfer.append("Export Workspace…", "win.export-workspace")
        transfer.append("Export Connections…", "win.export-connections")
        transfer.append("Import Connections…", "win.import-connections")
        menu.append_section(None, transfer)
    menu.append("Preferences", "app.preferences")
    if with_history:
        menu.append("Query History", "win.history")
    menu.append("Keyboard Shortcuts", "app.shortcuts")
    menu.append("Help", "app.help")
    menu.append("About sqlide", "app.about")
    button = Gtk.MenuButton(
        icon_name="open-menu-symbolic", menu_model=menu
    )
    describe(button, "Main menu")
    return button


def sidebar_menu_button(with_history: bool = False) -> Gtk.MenuButton:
    """Combined sidebar button: opening/creating workspaces plus the
    application-level menu that used to live in the content header
    (Preferences, Keyboard Shortcuts, Help, About, and — on a main
    window — Query History, tab bulk actions and the XML transfer
    items). The sidebar is the one control that is always on screen,
    so it is the natural home for both."""
    menu = Gio.Menu()
    workspaces = Gio.Menu()
    workspaces.append("Workspaces…", "app.show-launcher")
    menu.append_section(None, workspaces)
    if with_history:
        tabs = Gio.Menu()
        tabs.append("Split View", "win.split-view")
        tabs.append("Close All Tabs", "win.close-all-tabs")
        menu.append_section(None, tabs)
        connections = Gio.Menu()
        connections.append("Refresh Schemas", "win.refresh-schema")
        menu.append_section(None, connections)
        transfer = Gio.Menu()
        transfer.append("Export Workspace…", "win.export-workspace")
        transfer.append("Export Connections…", "win.export-connections")
        transfer.append("Import Connections…", "win.import-connections")
        menu.append_section(None, transfer)
    menu.append("Preferences", "app.preferences")
    if with_history:
        menu.append("Query History", "win.history")
    menu.append("Keyboard Shortcuts", "app.shortcuts")
    menu.append("Help", "app.help")
    menu.append("About sqlide", "app.about")
    button = Gtk.MenuButton(
        icon_name="open-menu-symbolic", menu_model=menu
    )
    describe(button, "Workspaces and settings")
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
