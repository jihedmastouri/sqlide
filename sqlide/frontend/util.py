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
    application.py). Main windows also get Query History, backed by the
    window-level "win.history" action (the launcher has no workspace,
    hence the flag)."""
    menu = Gio.Menu()
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
