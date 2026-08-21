"""The keyboard shortcuts window.

One table of every binding, grouped by where it applies, opened with
`ctrl+?` or from the main menu. It is the discovery surface for the
keyboard: anything reachable only from a context menu belongs in the
`keymap` registry as well, so this file (built from that registry)
doubles as the list to check when a new action lands. Bindings are
computed fresh each time the dialog opens, so a shortcut edited in
Preferences shows up here immediately.

Rendered with Adw.ShortcutsDialog where libadwaita has it (1.8+) and a
plain grouped dialog otherwise, so the app still runs on older
runtimes.
"""

from __future__ import annotations

from gi.repository import Adw, Gtk

from sqlide.frontend import keymap


def shortcuts_dialog() -> Adw.Dialog:
    if hasattr(Adw, "ShortcutsDialog"):
        return _native_dialog()
    return _fallback_dialog()


def _native_dialog() -> Adw.Dialog:
    dialog = Adw.ShortcutsDialog()
    for title, items in keymap.grouped():
        section = Adw.ShortcutsSection(title=title)
        for action, accelerator in items:
            if not accelerator:
                continue
            section.add(
                Adw.ShortcutsItem(title=action, accelerator=accelerator)
            )
        dialog.add(section)
    return dialog


def _fallback_dialog() -> Adw.Dialog:
    """Same content, plain widgets: one row per binding with the keys
    spelled out."""
    dialog = Adw.Dialog(
        title="Keyboard Shortcuts", content_width=480, content_height=560
    )
    page = Adw.PreferencesPage()
    for title, items in keymap.grouped():
        group = Adw.PreferencesGroup(title=title)
        for action, accelerator in items:
            if not accelerator:
                continue
            row = Adw.ActionRow(title=action)
            keys = Gtk.Label(label=keymap.spell(accelerator))
            keys.add_css_class("dim-label")
            keys.add_css_class("monospace")
            row.add_suffix(keys)
            group.add(row)
        page.add(group)
    view = Adw.ToolbarView()
    view.add_top_bar(Adw.HeaderBar())
    view.set_content(page)
    dialog.set_child(view)
    return dialog
