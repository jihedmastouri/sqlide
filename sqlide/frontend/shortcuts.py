"""The keyboard shortcuts window.

One table of every binding, grouped by where it applies, opened with
`ctrl+?` or from the main menu. It is the discovery surface for the
keyboard: anything reachable only from a context menu belongs in a
group here as well, so this file doubles as the list to check when a
new action lands.

Rendered with Adw.ShortcutsDialog where libadwaita has it (1.8+) and a
plain grouped dialog otherwise, so the app still runs on older
runtimes.
"""

from __future__ import annotations

from gi.repository import Adw, Gtk

# (group, ((action, accelerator), …)). Accelerators are in
# Gtk.accelerator_parse() syntax so the native dialog can draw keys.
SHORTCUTS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (
        "General",
        (
            ("Preferences", "<primary>comma"),
            ("Keyboard shortcuts", "<primary>question"),
            ("Help", "F1"),
            ("Close the window", "<primary>w"),
        ),
    ),
    (
        "Tabs",
        (
            ("Close the current tab", "<primary>F4"),
            ("Close every tab", "<primary><shift>w"),
        ),
    ),
    (
        "Query console",
        (
            ("Run the selection or the statement at the cursor", "<primary>Return"),
            ("Run every statement in the editor", "<primary><shift>Return"),
            ("Open a file in the editor", "<primary>o"),
            ("Save the editor to a file", "<primary>s"),
        ),
    ),
    (
        "Results and data grids",
        (
            ("Copy the selected cells", "<primary>c"),
            ("Open the cell menu on the selection", "Menu"),
            ("Open the cell menu on the selection", "<shift>F10"),
        ),
    ),
    (
        "Dialogs",
        (
            ("Confirm", "Return"),
            ("Dismiss without acting", "Escape"),
        ),
    ),
)


def shortcuts_dialog() -> Adw.Dialog:
    if hasattr(Adw, "ShortcutsDialog"):
        return _native_dialog()
    return _fallback_dialog()


def _native_dialog() -> Adw.Dialog:
    dialog = Adw.ShortcutsDialog()
    for title, items in SHORTCUTS:
        section = Adw.ShortcutsSection(title=title)
        for action, accelerator in items:
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
    for title, items in SHORTCUTS:
        group = Adw.PreferencesGroup(title=title)
        for action, accelerator in items:
            row = Adw.ActionRow(title=action)
            keys = Gtk.Label(label=spell_accelerator(accelerator))
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


def spell_accelerator(accelerator: str) -> str:
    """"<primary><shift>Return" -> "Ctrl+Shift+Return"."""
    text = (
        accelerator.replace("<primary>", "Ctrl+")
        .replace("<control>", "Ctrl+")
        .replace("<shift>", "Shift+")
        .replace("<alt>", "Alt+")
        .replace("Pointer_Button1", "Click")
        .replace("comma", ",")
        .replace("question", "?")
    )
    return text
