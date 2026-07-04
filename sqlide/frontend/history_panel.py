"""Query history list (the History page of the right side panel).

A Gtk.ListBox over the workspace's HistoryEntry list, newest first.
Row title is the first line of the SQL (ellipsized), subtitle is
"connection · time"; failed runs get an error marker. Activating a
row hands the entry back to the window, which loads it into a query
console. The header (view switcher, clear-history button) belongs to
the enclosing SidePanel.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from gi.repository import Adw, Gtk

from sqlide.backend.workspaces import HistoryEntry


class HistoryPanel(Gtk.ScrolledWindow):
    def __init__(self, on_activate: Callable[[HistoryEntry], None]) -> None:
        super().__init__(vexpand=True, hexpand=True)
        self._on_activate = on_activate
        self._entries: list[HistoryEntry] = []  # newest first, row order

        self._list = Gtk.ListBox()
        self._list.set_selection_mode(Gtk.SelectionMode.NONE)
        self._list.add_css_class("navigation-sidebar")
        self._list.connect("row-activated", self._row_activated)
        placeholder = Gtk.Label(label="No queries yet", margin_top=24)
        placeholder.add_css_class("dim-label")
        self._list.set_placeholder(placeholder)
        self.set_child(self._list)

    def set_entries(self, entries: list[HistoryEntry]) -> None:
        """Rebuild the list from the workspace history (stored oldest
        first; shown newest first)."""
        self._entries = list(reversed(entries))
        while (row := self._list.get_row_at_index(0)) is not None:
            self._list.remove(row)
        for entry in self._entries:
            self._list.append(self._make_row(entry))

    def _make_row(self, entry: HistoryEntry) -> Adw.ActionRow:
        first_line = next(
            (line for line in entry.sql.strip().splitlines() if line), "(empty)"
        )
        row = Adw.ActionRow(activatable=True)
        row.set_use_markup(False)  # SQL may contain <, & …
        row.set_title(first_line)
        row.set_title_lines(1)
        row.set_subtitle(f"{entry.connection} · {self._format_time(entry.timestamp)}")
        row.set_subtitle_lines(1)
        row.set_tooltip_text(entry.sql)
        if not entry.ok:
            marker = Gtk.Image(icon_name="dialog-error-symbolic")
            marker.set_tooltip_text("This run failed")
            marker.add_css_class("dim-label")
            row.add_suffix(marker)
        return row

    @staticmethod
    def _format_time(timestamp: str) -> str:
        try:
            return datetime.fromisoformat(timestamp).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return timestamp

    def _row_activated(self, _list, row) -> None:
        self._on_activate(self._entries[row.get_index()])
