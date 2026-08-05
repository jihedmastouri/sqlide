"""Query history list (the History page of the right side panel).

A Gtk.ListBox over the workspace's HistoryEntry list, newest first.
A scope dropdown above the list switches between the history of the
current tab ("This panel", matched by the panel name recorded with
each entry) and the whole workspace ("All panels"); the clear-history
button sits in that same control row, below the panel's header, not
next to the History/Aggregate switcher.

Row title is the first line of the SQL (ellipsized), subtitle is
"connection · time" (plus the panel name in All-panels scope); failed
runs get an error marker. Activating a row hands the entry back to
the window, which loads it into a query console.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from sqlide.frontend.util import describe
from gi.repository import Adw, Gtk

from sqlide.backend.workspaces import HistoryEntry

_SCOPES = ("This panel", "All panels")


class HistoryPanel(Gtk.Box):
    def __init__(
        self,
        on_activate: Callable[[HistoryEntry], None],
        on_clear: Callable[[], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._on_activate = on_activate
        self._all_entries: list[HistoryEntry] = []  # oldest first (store order)
        self._entries: list[HistoryEntry] = []  # shown, newest first, row order
        self._active_panel = ""

        controls = Gtk.Box(
            spacing=6,
            margin_top=6,
            margin_bottom=6,
            margin_start=6,
            margin_end=6,
        )
        self._scope = Gtk.DropDown(
            model=Gtk.StringList.new(list(_SCOPES)), hexpand=True
        )
        self._scope.set_tooltip_text("Which queries to list")
        self._scope.connect("notify::selected", lambda *_: self._rebuild())
        clear_button = Gtk.Button(icon_name="user-trash-symbolic")
        clear_button.add_css_class("flat")
        describe(clear_button, "Clear history")
        clear_button.connect("clicked", lambda *_: on_clear())
        controls.append(self._scope)
        controls.append(clear_button)
        self.append(controls)

        self._list = Gtk.ListBox()
        self._list.set_selection_mode(Gtk.SelectionMode.NONE)
        self._list.add_css_class("navigation-sidebar")
        self._list.connect("row-activated", self._row_activated)
        placeholder = Gtk.Label(label="No queries yet", margin_top=24)
        placeholder.add_css_class("dim-label")
        self._list.set_placeholder(placeholder)
        scroller = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        scroller.set_child(self._list)
        self.append(scroller)

    def set_entries(self, entries: list[HistoryEntry]) -> None:
        """Rebuild the list from the workspace history (stored oldest
        first; shown newest first)."""
        self._all_entries = list(entries)
        self._rebuild()

    def set_active_panel(self, name: str) -> None:
        """Tab title of the currently selected tab; keys the This-panel
        scope."""
        if name != self._active_panel:
            self._active_panel = name
            if self._scope.get_selected() == 0:
                self._rebuild()

    def _rebuild(self) -> None:
        # Entries whose tab was closed leave the panel scopes entirely;
        # the workspace-wide History tab still lists them.
        local = self._scope.get_selected() == 0
        entries = [
            e
            for e in self._all_entries
            if not e.panel_closed
            and (not local or e.panel == self._active_panel)
        ]
        self._entries = list(reversed(entries))
        while (row := self._list.get_row_at_index(0)) is not None:
            self._list.remove(row)
        for entry in self._entries:
            self._list.append(self._make_row(entry, with_panel=not local))

    def _make_row(self, entry: HistoryEntry, with_panel: bool) -> Adw.ActionRow:
        first_line = next(
            (line for line in entry.sql.strip().splitlines() if line), "(empty)"
        )
        row = Adw.ActionRow(activatable=True)
        row.set_use_markup(False)  # SQL may contain <, & …
        row.set_title(first_line)
        row.set_title_lines(1)
        subtitle = f"{entry.connection} · {self._format_time(entry.timestamp)}"
        if with_panel and entry.panel:
            subtitle = f"{entry.panel} · {subtitle}"
        row.set_subtitle(subtitle)
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
