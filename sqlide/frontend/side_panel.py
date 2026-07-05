"""Right side panel of the main window: History, DDL and Aggregate.

An Adw.ViewStack behind an Adw.ViewSwitcher in the header bar. The
History page is the query-history list (HistoryPanel), which owns its
own scope/clear controls below the header; the DDL page shows the
CREATE statement of the active table/definition tab (highlighted,
read-only — the window fills it via set_definition on tab changes);
the Aggregate page shows the count/sum/avg/min/max summary that the
data grid's context menu produces (see ResultGrid). The window fills
the aggregate page via show_aggregate(), which also switches the
stack to it.
"""

from __future__ import annotations

from typing import Callable

from gi.repository import Adw, Gtk

from sqlide.backend.workspaces import HistoryEntry
from sqlide.frontend.history_panel import HistoryPanel
from sqlide.frontend.sql_editor import SqlEditor


class SidePanel(Gtk.Box):
    # Composes an Adw.ToolbarView rather than subclassing it (final type).
    def __init__(
        self,
        on_activate: Callable[[HistoryEntry], None],
        on_clear: Callable[[], None],
    ) -> None:
        super().__init__()

        self._history = HistoryPanel(on_activate=on_activate, on_clear=on_clear)

        # DDL page: title label over a read-only highlighted SQL view;
        # a placeholder until a table/definition tab is active.
        self._ddl_title = Gtk.Label(
            xalign=0,
            margin_top=6,
            margin_bottom=6,
            margin_start=8,
            margin_end=8,
        )
        self._ddl_title.add_css_class("heading")
        self._ddl_view = SqlEditor(editable=False)
        self._ddl_placeholder = Gtk.Label(
            label="Open a table to see its DDL", margin_top=24
        )
        self._ddl_placeholder.add_css_class("dim-label")
        ddl_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        ddl_box.append(self._ddl_placeholder)
        ddl_box.append(self._ddl_title)
        ddl_box.append(self._ddl_view)
        self._ddl_view.set_vexpand(True)
        self._set_ddl_visible(False)
        ddl_page = ddl_box

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
            ddl_page, "ddl", "DDL", "text-x-generic-symbolic"
        )
        self._stack.add_titled_with_icon(
            agg_page, "aggregate", "Aggregate", "accessories-calculator-symbolic"
        )

        header = Adw.HeaderBar()
        # No window controls in the panel header: the panel sits inside
        # the content area, so the close button belongs to the window's
        # own header bar only.
        header.set_show_start_title_buttons(False)
        header.set_show_end_title_buttons(False)
        header.set_title_widget(
            Adw.ViewSwitcher(stack=self._stack, policy=Adw.ViewSwitcherPolicy.NARROW)
        )

        view = Adw.ToolbarView(hexpand=True)
        view.add_top_bar(header)
        view.set_content(self._stack)
        self.append(view)

    def set_entries(self, entries: list[HistoryEntry]) -> None:
        self._history.set_entries(entries)

    def set_active_panel(self, name: str) -> None:
        """Tab title of the selected tab, for local history scope."""
        self._history.set_active_panel(name)

    def set_definition(self, title: str, ddl: str) -> None:
        """Fill (or clear, with empty args) the DDL page; the window
        calls this whenever the active tab changes."""
        if ddl:
            self._ddl_title.set_text(title)
            self._ddl_view.set_text(ddl)
        self._set_ddl_visible(bool(ddl))

    def _set_ddl_visible(self, has_ddl: bool) -> None:
        self._ddl_placeholder.set_visible(not has_ddl)
        self._ddl_title.set_visible(has_ddl)
        self._ddl_view.set_visible(has_ddl)

    def show_aggregate(self, lines: list[str]) -> None:
        """Fill the aggregate page and switch to it (the window reveals
        the panel itself)."""
        self._agg_label.set_text("\n".join(lines).expandtabs(12))
        self._agg_placeholder.set_visible(False)
        self._stack.set_visible_child_name("aggregate")

    def show_history(self) -> None:
        self._stack.set_visible_child_name("history")
