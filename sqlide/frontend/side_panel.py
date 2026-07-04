"""Right side panel of the main window: History and Aggregate pages.

An Adw.ViewStack behind an Adw.ViewSwitcher in the header bar. The
History page is the query-history list (HistoryPanel); the Aggregate
page shows the count/sum/avg/min/max summary that the data grid's
context menu produces (see ResultGrid). The window fills the aggregate
page via show_aggregate(), which also switches the stack to it; the
clear-history button only shows on the History page.
"""

from __future__ import annotations

from typing import Callable

from gi.repository import Adw, Gtk

from sqlide.backend.workspaces import HistoryEntry
from sqlide.frontend.history_panel import HistoryPanel


class SidePanel(Gtk.Box):
    # Composes an Adw.ToolbarView rather than subclassing it (final type).
    def __init__(
        self,
        on_activate: Callable[[HistoryEntry], None],
        on_clear: Callable[[], None],
    ) -> None:
        super().__init__()

        self._history = HistoryPanel(on_activate=on_activate)

        self._agg_label = Gtk.Label(
            justify=Gtk.Justification.LEFT,
            xalign=0,
            yalign=0,
            selectable=True,
        )
        self._agg_label.add_css_class("aggregate-summary")
        self._agg_placeholder = Gtk.Label(
            label="Select cells in a grid and choose Aggregate", margin_top=24
        )
        self._agg_placeholder.add_css_class("dim-label")
        agg_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        agg_box.append(self._agg_placeholder)
        agg_box.append(self._agg_label)
        agg_page = Gtk.ScrolledWindow(child=agg_box, vexpand=True, hexpand=True)

        self._stack = Adw.ViewStack()
        self._stack.add_titled_with_icon(
            self._history, "history", "History",
            "document-open-recent-symbolic",
        )
        self._stack.add_titled_with_icon(
            agg_page, "aggregate", "Aggregate", "accessories-calculator-symbolic"
        )

        header = Adw.HeaderBar()
        header.set_title_widget(
            Adw.ViewSwitcher(stack=self._stack, policy=Adw.ViewSwitcherPolicy.NARROW)
        )
        self._clear_button = Gtk.Button(icon_name="user-trash-symbolic")
        self._clear_button.set_tooltip_text("Clear history")
        self._clear_button.connect("clicked", lambda *_: on_clear())
        header.pack_start(self._clear_button)
        self._stack.connect("notify::visible-child-name", self._page_changed)
        self._page_changed()

        view = Adw.ToolbarView(hexpand=True)
        view.add_top_bar(header)
        view.set_content(self._stack)
        self.append(view)

    def set_entries(self, entries: list[HistoryEntry]) -> None:
        self._history.set_entries(entries)

    def show_aggregate(self, lines: list[str]) -> None:
        """Fill the aggregate page and switch to it (the window reveals
        the panel itself)."""
        self._agg_label.set_text("\n".join(lines).expandtabs(12))
        self._agg_placeholder.set_visible(False)
        self._stack.set_visible_child_name("aggregate")

    def show_history(self) -> None:
        self._stack.set_visible_child_name("history")

    def _page_changed(self, *_args) -> None:
        self._clear_button.set_visible(
            self._stack.get_visible_child_name() == "history"
        )
