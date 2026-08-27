"""Which surface carries which message.

Inconsistent feedback is what users describe as "janky" without being
able to say why, so the choice is a rule rather than a per-call
judgement:

| Situation | Surface |
| --- | --- |
| Succeeded, nothing to follow up | toast, 3 s |
| Succeeded, follow-up useful | toast with one action button |
| Recoverable problem tied to a place | inline there, with a count |
| Persistent condition (read-only, transaction open, connection lost) | `Adw.Banner` at the top of the affected tab, until it clears |
| Database error from a statement the user ran | inline in the results area: the message verbatim, the statement, and a Copy button |
| Destructive confirmation | `Adw.AlertDialog` (frontend/confirm.py) |
| Long job started | toast plus the status bar's job zone |

Never: a modal dialog for something the user did not initiate, a bare
exception string with no context, or "an error occurred" without the
error. A database error in particular is never a toast — the user has
to read it, and usually to copy it.
"""

from __future__ import annotations

from typing import Callable

from gi.repository import Adw, Gdk, Gtk

from sqlide.i18n import _

TOAST_SECONDS = 3


def toast(
    widget: Gtk.Widget,
    message: str,
    *,
    action_label: str = "",
    on_action: Callable[[], None] | None = None,
) -> None:
    """Report something that went fine. Finds the nearest toast overlay
    from `widget`, so tabs do not need a reference to the window."""
    overlay = _toast_overlay(widget)
    if overlay is None:
        return
    item = Adw.Toast(title=message, timeout=TOAST_SECONDS)
    if action_label and on_action is not None:
        item.set_button_label(action_label)
        item.connect("button-clicked", lambda *_: on_action())
    overlay.add_toast(item)


def _toast_overlay(widget: Gtk.Widget | None) -> Adw.ToastOverlay | None:
    root = widget.get_root() if widget is not None else None
    content = root.get_content() if isinstance(root, Adw.ApplicationWindow) else None
    return content if isinstance(content, Adw.ToastOverlay) else None


def condition_banner(
    title: str = "",
    *,
    button_label: str = "",
    on_click: Callable[[], None] | None = None,
) -> Adw.Banner:
    """A banner for a condition that stays true until something
    changes: pack it at the top of the tab and call set_condition() as
    the condition comes and goes."""
    banner = Adw.Banner(title=title, revealed=bool(title))
    if button_label and on_click is not None:
        banner.set_button_label(button_label)
        banner.connect("button-clicked", lambda *_: on_click())
    return banner


def set_condition(banner: Adw.Banner, title: str) -> None:
    """Show the condition, or hide the banner when it has cleared."""
    if title:
        banner.set_title(title)
    banner.set_revealed(bool(title))


def error_page(message: str, statement: str = "") -> Gtk.Widget:
    """A database error, inline where the result would have been: the
    driver's message verbatim, the statement that produced it, and a
    Copy button — the three things a user needs to act on it."""
    box = Gtk.Box(
        orientation=Gtk.Orientation.VERTICAL,
        spacing=12,
        margin_top=12,
        margin_bottom=12,
        margin_start=12,
        margin_end=12,
        valign=Gtk.Align.START,
    )
    text = Gtk.Label(label=message, xalign=0, wrap=True, selectable=True)
    text.add_css_class("error")
    box.append(text)
    if statement:
        sql = Gtk.Label(label=statement, xalign=0, wrap=True, selectable=True)
        sql.add_css_class("monospace")
        sql.add_css_class("dim-label")
        box.append(sql)

    copy = Gtk.Button(label=_("Copy Error"), halign=Gtk.Align.START)
    payload = message if not statement else f"{message}\n\n{statement}"

    def copy_clicked(button: Gtk.Button) -> None:
        display = Gdk.Display.get_default()
        if display is not None:
            display.get_clipboard().set(payload)
        button.set_label(_("Copied"))

    copy.connect("clicked", copy_clicked)
    box.append(copy)
    return box


def message_page(text: str) -> Gtk.Widget:
    """The non-error counterpart: a plain outcome line ("3 rows
    affected")."""
    label = Gtk.Label(
        label=text,
        xalign=0,
        margin_top=8,
        margin_start=8,
        selectable=True,
        wrap=True,
        valign=Gtk.Align.START,
    )
    label.add_css_class("dim-label")
    return label


def set_disconnected(
    tab: Gtk.Box,
    banners: dict[Gtk.Widget, Adw.Banner],
    title: str,
    on_reconnect: Callable[[], None],
) -> None:
    """A tab whose connection was closed under it: a banner at the top
    of the tab saying so, with the way back on it.

    The tab itself is left alone — its rows, its SQL and its scroll
    position are all still there, and nothing in it throws, because
    every backend call goes through ensure_connector and would simply
    reconnect. `banners` is the caller's registry of the banners it has
    added, keyed by tab, so the same call clears one (title "") without
    the tab needing to know it ever had one.
    """
    banner = banners.get(tab)
    if not title:
        if banner is not None:
            tab.remove(banner)
            del banners[tab]
        return
    if banner is None:
        banner = condition_banner(
            title, button_label="Reconnect", on_click=on_reconnect
        )
        tab.prepend(banner)
        banners[tab] = banner
    set_condition(banner, title)
