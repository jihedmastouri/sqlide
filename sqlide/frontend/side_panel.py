"""Right side panel of the main window.

An Adw.ViewStack behind an Adw.ViewSwitcher in the header bar. Which
pages are offered depends on the active tab (the window reports it
through set_context):

- query console ("console"): Info, Snippets, Queries, History, Aggregate
- table data tab ("table"):  Info, History, Aggregate, Filters
- other result grids ("grid"): Info, History, Aggregate
- everything else ("other"): Info, History

Pages:
- Info: details of the active tab — the DDL of the active table or
  definition tab (highlighted, read-only), or the connection details
  of a console. The window fills it on tab changes (set_definition /
  set_info).
- History: the query-history list (HistoryPanel) with its own
  scope/clear controls.
- Snippets / Queries: the global saved-SQL stores (backend/saved.py).
  Activating a snippet inserts it into the console at the cursor;
  activating a saved query opens it in a new console. The + button
  saves the console's selection (or whole editor) under a name.
- Aggregate: the count/sum/avg/min/max summary a grid's Aggregate
  menu item produces; show_aggregate() fills it and switches to it.
- Filters: the workspace's saved filter sets for the active table
  (keyed connection.database.table). Activating one applies it; the
  + button saves the table's current filter under a name. The window
  owns the storage (Workspace.saved_filters) and hands entries in
  through set_filter_target.
"""

from __future__ import annotations

from typing import Callable

from sqlide.frontend.util import describe
from gi.repository import Adw, Gtk, Pango

from sqlide.backend.saved import SavedItem, SavedStore
from sqlide.backend.saved import queries as queries_store
from sqlide.backend.saved import snippets as snippets_store
from sqlide.backend.workspaces import HistoryEntry
from sqlide.frontend.history_panel import HistoryPanel
from sqlide.frontend.sql_editor import SqlEditor

_CONTEXT_PAGES = {
    "console": ("info", "snippets", "queries", "history", "aggregate"),
    "table": ("info", "history", "aggregate", "filters"),
    "grid": ("info", "history", "aggregate"),
    "other": ("info", "history"),
}


def ask_name(
    parent: Gtk.Widget,
    heading: str,
    initial: str,
    on_done: Callable[[str], None],
) -> None:
    """Small name prompt used when saving snippets/queries/filters."""
    entry = Gtk.Entry(text=initial, activates_default=True)
    dialog = Adw.AlertDialog(heading=heading, extra_child=entry)
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("save", "Save")
    dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("save")
    dialog.set_close_response("cancel")

    def respond(_dialog, response: str) -> None:
        name = entry.get_text().strip()
        if response == "save" and name:
            on_done(name)

    dialog.connect("response", respond)
    dialog.present(parent)


def _first_line(sql: str) -> str:
    return next((line for line in sql.strip().splitlines() if line), "(empty)")


class _SavedSqlList(Gtk.Box):
    """One saved-SQL page (Snippets or Queries): a + button over the
    store's items; activating a row hands its SQL to on_use, the row's
    trash button deletes it. Follows the store live (any window) and
    unsubscribes when destroyed."""

    def __init__(
        self,
        store: SavedStore,
        save_tooltip: str,
        empty_text: str,
        on_use: Callable[[str], None],
        get_sql: Callable[[], str],
        on_error: Callable[[str], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._store = store
        self._on_use = on_use
        self._get_sql = get_sql
        self._on_error = on_error
        self._items: list[SavedItem] = []

        controls = Gtk.Box(
            spacing=6,
            margin_top=6,
            margin_bottom=6,
            margin_start=6,
            margin_end=6,
        )
        controls.append(Gtk.Box(hexpand=True))
        add = Gtk.Button(icon_name="list-add-symbolic")
        add.add_css_class("flat")
        describe(add, save_tooltip)
        add.connect("clicked", self._save_current)
        controls.append(add)
        self.append(controls)

        self._list = Gtk.ListBox()
        self._list.set_selection_mode(Gtk.SelectionMode.NONE)
        self._list.add_css_class("navigation-sidebar")
        self._list.connect("row-activated", self._row_activated)
        placeholder = Gtk.Label(label=empty_text, margin_top=24, wrap=True)
        placeholder.add_css_class("dim-label")
        self._list.set_placeholder(placeholder)
        scroller = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        scroller.set_child(self._list)
        self.append(scroller)

        self._rebuild(store.load())
        store.subscribe(self._rebuild)
        self.connect("destroy", lambda *_: store.unsubscribe(self._rebuild))

    def _rebuild(self, items: list[SavedItem]) -> None:
        self._items = list(items)
        while (row := self._list.get_row_at_index(0)) is not None:
            self._list.remove(row)
        for item in self._items:
            row = Adw.ActionRow(activatable=True)
            row.set_use_markup(False)
            row.set_title(item.name)
            row.set_title_lines(1)
            row.set_subtitle(_first_line(item.sql))
            row.set_subtitle_lines(1)
            row.set_tooltip_text(item.sql)
            delete = Gtk.Button(icon_name="user-trash-symbolic")
            delete.add_css_class("flat")
            describe(delete, "Delete")
            delete.connect(
                "clicked", lambda _b, it=item: self._store.remove(it)
            )
            row.add_suffix(delete)
            self._list.append(row)

    def _row_activated(self, _list, row) -> None:
        self._on_use(self._items[row.get_index()].sql)

    def _save_current(self, *_args) -> None:
        sql = self._get_sql().strip()
        if not sql:
            self._on_error("Nothing to save — the console is empty")
            return
        ask_name(
            self,
            "Save As",
            _first_line(sql)[:40],
            lambda name: self._store.add(name, sql),
        )


class _FiltersPage(Gtk.Box):
    """The Filters page: saved filter sets of the active table tab.
    The window supplies the entries (set_target) and owns the
    persistence; this page only raises the apply/save/delete calls."""

    def __init__(
        self,
        on_apply: Callable[[dict], None],
        on_save: Callable[[str], None],
        on_delete: Callable[[dict], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._on_apply = on_apply
        self._on_delete = on_delete
        self._entries: list[dict] = []

        controls = Gtk.Box(
            spacing=6,
            margin_top=6,
            margin_bottom=6,
            margin_start=6,
            margin_end=6,
        )
        self._target_label = Gtk.Label(
            xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END
        )
        self._target_label.add_css_class("dim-label")
        controls.append(self._target_label)
        add = Gtk.Button(icon_name="list-add-symbolic")
        add.add_css_class("flat")
        describe(add, "Save the table's current filter")
        add.connect(
            "clicked",
            lambda *_: ask_name(self, "Save Filter As", "", on_save),
        )
        controls.append(add)
        self.append(controls)

        self._list = Gtk.ListBox()
        self._list.set_selection_mode(Gtk.SelectionMode.NONE)
        self._list.add_css_class("navigation-sidebar")
        self._list.connect("row-activated", self._row_activated)
        placeholder = Gtk.Label(
            label="No saved filters for this table", margin_top=24, wrap=True
        )
        placeholder.add_css_class("dim-label")
        self._list.set_placeholder(placeholder)
        scroller = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        scroller.set_child(self._list)
        self.append(scroller)

    def set_target(self, key: str, entries: list[dict]) -> None:
        self._target_label.set_text(key)
        self._entries = list(entries)
        while (row := self._list.get_row_at_index(0)) is not None:
            self._list.remove(row)
        for entry in self._entries:
            conditions = entry.get("filters", [])
            row = Adw.ActionRow(activatable=True)
            row.set_use_markup(False)
            row.set_title(entry.get("name", "(unnamed)"))
            row.set_title_lines(1)
            parts = []
            for i, cond in enumerate(conditions):
                clause = (
                    f"{cond.get('column')} {cond.get('op')} "
                    f"{cond.get('value', '')}"
                ).strip()
                if i > 0:
                    clause = f"{cond.get('conjunction', 'AND')} {clause}"
                parts.append(clause)
            row.set_subtitle(" ".join(parts))
            row.set_subtitle_lines(2)
            delete = Gtk.Button(icon_name="user-trash-symbolic")
            delete.add_css_class("flat")
            describe(delete, "Delete")
            delete.connect(
                "clicked", lambda _b, e=entry: self._on_delete(e)
            )
            row.add_suffix(delete)
            self._list.append(row)

    def _row_activated(self, _list, row) -> None:
        self._on_apply(self._entries[row.get_index()])


class SidePanel(Gtk.Box):
    # Composes an Adw.ToolbarView rather than subclassing it (final type).
    def __init__(
        self,
        on_activate: Callable[[HistoryEntry], None],
        on_clear: Callable[[], None],
        on_insert_snippet: Callable[[str], None],
        on_open_query: Callable[[str], None],
        get_console_sql: Callable[[], str],
        on_error: Callable[[str], None],
        on_apply_filter: Callable[[dict], None],
        on_save_filter: Callable[[str], None],
        on_delete_filter: Callable[[dict], None],
    ) -> None:
        super().__init__()

        self._history = HistoryPanel(on_activate=on_activate, on_clear=on_clear)

        # Info page: title label over either the connection-details
        # text (consoles) or a read-only highlighted DDL view (table
        # and definition tabs); a placeholder until a tab is active.
        self._info_title = Gtk.Label(
            xalign=0,
            margin_top=6,
            margin_bottom=6,
            margin_start=8,
            margin_end=8,
        )
        self._info_title.add_css_class("heading")
        self._info_details = Gtk.Label(
            xalign=0,
            yalign=0,
            margin_start=8,
            margin_end=8,
            wrap=True,
            selectable=True,
        )
        self._ddl_view = SqlEditor(editable=False)
        self._info_placeholder = Gtk.Label(
            label="Open a tab to see its details", margin_top=24
        )
        self._info_placeholder.add_css_class("dim-label")
        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        info_box.append(self._info_placeholder)
        info_box.append(self._info_title)
        info_box.append(self._info_details)
        info_box.append(self._ddl_view)
        self._ddl_view.set_vexpand(True)
        self._show_info(title=False, details=False, ddl=False)
        info_page = info_box

        self._snippets = _SavedSqlList(
            snippets_store,
            "Save the console's selection (or whole editor) as a snippet",
            "No saved snippets yet — the + button saves the console's "
            "selection",
            on_use=on_insert_snippet,
            get_sql=get_console_sql,
            on_error=on_error,
        )
        self._queries = _SavedSqlList(
            queries_store,
            "Save the console's selection (or whole editor) as a query",
            "No saved queries yet — the + button saves the console's "
            "selection",
            on_use=on_open_query,
            get_sql=get_console_sql,
            on_error=on_error,
        )
        self._filters = _FiltersPage(
            on_apply=on_apply_filter,
            on_save=on_save_filter,
            on_delete=on_delete_filter,
        )

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
        for name, title, icon, child in (
            ("info", "Info", "dialog-information-symbolic", info_page),
            (
                "snippets",
                "Snippets",
                "insert-text-symbolic",
                self._snippets,
            ),
            (
                "queries",
                "Queries",
                "emblem-documents-symbolic",
                self._queries,
            ),
            (
                "history",
                "History",
                "document-open-recent-symbolic",
                self._history,
            ),
            (
                "aggregate",
                "Aggregate",
                "accessories-calculator-symbolic",
                agg_page,
            ),
            ("filters", "Filters", "edit-find-symbolic", self._filters),
        ):
            self._stack.add_titled_with_icon(child, name, title, icon)

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
        self.set_context("other")

    # Context (which pages the active tab offers)

    def set_context(self, context: str) -> None:
        names = _CONTEXT_PAGES.get(context, _CONTEXT_PAGES["other"])
        pages = self._stack.get_pages()
        for i in range(pages.get_n_items()):
            page = pages.get_item(i)
            page.set_visible(page.get_name() in names)
        current = self._stack.get_visible_child_name()
        if current not in names:
            self._stack.set_visible_child_name(names[0])

    # History

    def set_entries(self, entries: list[HistoryEntry]) -> None:
        self._history.set_entries(entries)

    def set_active_panel(self, name: str) -> None:
        """Tab title of the selected tab, for local history scope."""
        self._history.set_active_panel(name)

    # Info

    def set_definition(self, title: str, ddl: str) -> None:
        """Show a DDL on the Info page (or clear it, with empty args);
        the window calls this whenever the active tab changes."""
        if ddl:
            self._info_title.set_text(title)
            self._ddl_view.set_text(ddl)
        self._show_info(title=bool(ddl), details=False, ddl=bool(ddl))

    def set_info(self, title: str, details: str) -> None:
        """Show plain-text details (connection info) on the Info page."""
        self._info_title.set_text(title)
        self._info_details.set_text(details)
        self._show_info(title=True, details=True, ddl=False)

    def _show_info(self, title: bool, details: bool, ddl: bool) -> None:
        self._info_placeholder.set_visible(not (title or details or ddl))
        self._info_title.set_visible(title)
        self._info_details.set_visible(details)
        self._ddl_view.set_visible(ddl)

    # Filters

    def set_filter_target(self, key: str, entries: list[dict]) -> None:
        self._filters.set_target(key, entries)

    # Aggregate

    def show_aggregate(self, lines: list[str]) -> None:
        """Fill the aggregate page and switch to it (the window reveals
        the panel itself)."""
        self._agg_label.set_text("\n".join(lines).expandtabs(12))
        self._agg_placeholder.set_visible(False)
        self._stack.set_visible_child_name("aggregate")

    def show_history(self) -> None:
        self._stack.set_visible_child_name("history")
