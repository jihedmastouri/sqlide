"""Help dialog: how to use the app — features and keyboard shortcuts.

Opened through the app.help action (main menu, above About; also F1).
Static content only: a scrolled page of sections, each a titled list
of short "what · how" lines, plus a shortcuts table.
"""

from __future__ import annotations

from gi.repository import Adw, Gtk

from sqlide.frontend import keymap

# The bindings themselves live in keymap.py, which also backs the
# shortcuts window and the Preferences editor — one registry, so none
# of the three can drift apart. Computed fresh in _shortcuts_section()
# (not here) so an edited shortcut shows up without a restart.

_SECTIONS = (
    (
        "Workspaces & Connections",
        (
            "A workspace groups connections and remembers its open tabs "
            "and query history. Switch or create workspaces from the "
            "grid button in the sidebar header; the app reopens the "
            "one you were last in.",
            "Add a connection with the + button in the sidebar header. "
            "Expanding a connection loads its Tables, Views and "
            "Functions.",
        ),
    ),
    (
        "Browsing Data",
        (
            "Click a table or view to open its data in a tab; the caret "
            "at the end of the row expands its columns instead.",
            "Right-click a table or view for View Data, Query Console "
            "and Table Definition. The definition opens as a tab with a "
            "Text (DDL) and a Table (columns) mode; the right panel's "
            "DDL page mirrors the active table's CREATE statement.",
            "Activate a function (or trigger) under Functions to edit "
            "its definition; Save shows the replacing statements for "
            "review before they run.",
            "The magnifier beside Add Connection swaps that row for a "
            "search box: type to fuzzy-find tables, views and functions "
            "across loaded connections, Escape to put the buttons back.",
            "In a data tab, Filter and Sort in the bottom bar open "
            "separate panels; sorting supports several columns, and "
            "clicking a column header adds that column to the sort "
            "order (click again to flip the direction).",
            "Unlock editing with the pencil button; edits stay local "
            "until Save shows the UPDATE statements for review.",
        ),
    ),
    (
        "Query Console",
        (
            "Open a console with the terminal button in the header bar, "
            "or right-click a table. The dropdowns pick the connection "
            "(and database, for servers that host several).",
            "The editor holds any number of statements; Run executes "
            "the selection or the statement under the cursor, Run All "
            "the whole buffer, and Explain shows the plan instead of "
            "running.",
            "Begin, Commit and Rollback run the bare transaction "
            "statements over the console's connection.",
            "The open and save buttons load a file into the editor "
            "and write it back (the first save asks where).",
            "After a run the bottom panel shows a Status tab (each "
            "statement, its outcome and timing) followed by one result "
            "tab per statement.",
            "The gear menu at the right of the toolbar holds the "
            "editor's settings: the font size, and the completion "
            "language server for that console.",
        ),
    ),
    (
        "Results & History",
        (
            "Select cells by clicking and dragging (or Shift+click); "
            "the context menu copies as CSV, INSERT, JSON, pretty text "
            "or Markdown, and Aggregate summarizes the selection in "
            "the right panel.",
            "Reorder grid columns by dragging their headers, or with "
            "Move Column Left/Right in the context menu.",
            "The right panel's History page lists queries run in the "
            "current tab (switch it to All panels for the whole "
            "workspace); grid loads (filter, sort, paging) are "
            "recorded too. Activating an entry opens it in a new "
            "console. Closing a tab removes its entries from the "
            "panel scopes but not from the workspace-wide History "
            "tab.",
            "Query History in the sidebar's settings menu opens the "
            "workspace-wide "
            "history as a read-only table.",
            "Split in the header bar moves the current tab into a new "
            "pane so two tabs show side by side.",
        ),
    ),
)


def help_dialog() -> Adw.Dialog:
    dialog = Adw.Dialog(
        title="Help", content_width=560, content_height=620
    )
    box = Gtk.Box(
        orientation=Gtk.Orientation.VERTICAL,
        spacing=18,
        margin_top=18,
        margin_bottom=24,
        margin_start=24,
        margin_end=24,
    )
    for title, lines in _SECTIONS:
        box.append(_section(title, lines))
    box.append(_shortcuts_section())

    scroller = Gtk.ScrolledWindow(child=box, vexpand=True)
    scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    view = Adw.ToolbarView()
    view.add_top_bar(Adw.HeaderBar())
    view.set_content(scroller)
    dialog.set_child(view)
    return dialog


def _section(title: str, lines: tuple[str, ...]) -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    heading = Gtk.Label(label=title, xalign=0)
    heading.add_css_class("heading")
    box.append(heading)
    for line in lines:
        row = Gtk.Box(spacing=6)
        bullet = Gtk.Label(label="•", yalign=0)
        bullet.add_css_class("dim-label")
        text = Gtk.Label(label=line, xalign=0, wrap=True, hexpand=True)
        row.append(bullet)
        row.append(text)
        box.append(row)
    return box


def _shortcuts_section() -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    heading = Gtk.Label(label="Keyboard Shortcuts", xalign=0)
    heading.add_css_class("heading")
    box.append(heading)
    grid = Gtk.Grid(column_spacing=24, row_spacing=4)
    shortcuts = [
        (action, keymap.spell(accelerator))
        for _group, items in keymap.grouped()
        for action, accelerator in items
        if accelerator
    ]
    for i, (what, keys) in enumerate(shortcuts):
        label = Gtk.Label(label=what, xalign=0, hexpand=True)
        combo = Gtk.Label(label=keys, xalign=1)
        combo.add_css_class("dim-label")
        combo.add_css_class("monospace")
        grid.attach(label, 0, i, 1, 1)
        grid.attach(combo, 1, i, 1, 1)
    box.append(grid)
    return box
