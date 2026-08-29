"""Right side panel of the main window.

An Adw.ViewStack behind an Adw.ViewSwitcher in the header bar. Which
pages are offered depends on the active tab (the window reports it
through set_context):

- query console ("console"): Properties, Info, Value, Snippets,
  Queries, Notes, History, Aggregate
- table data tab ("table"):  Properties, Info, Value, Notes, History,
  Aggregate, Filters
- other result grids ("grid"): Properties, Info, Value, Notes,
  History, Aggregate
- everything else ("other"): Properties, Info, Notes, History

Pages:
- Properties: everything about the active tab's object — the sections
  its engine has, its DDL, its children (CORE-47). The widget is an
  object_info.PropertiesSurfaces the window owns: one
  PropertiesView per object, swapped as tabs change rather than one
  view retargeted (CORE-50), and the same view class is what a
  detached properties window holds. Read-only, here and there.
- Info: details of the active tab — the DDL of the active table or
  definition tab (highlighted, read-only), or the connection details
  of a console. The window fills it on tab changes (set_definition /
  set_info).
- Value: the focused grid cell in full (CORE-42) — wrapped text,
  pretty-printed JSON, or a hex/ASCII dump with the byte length and a
  geometry's description. Filled by the grid on every selection
  change, so opening the panel is enough to read the cell; an edit
  made here goes back through the grid's own edit path and lands in
  the same pending list and Save preview as an inline one.
- History: the query-history list (HistoryPanel) with its own
  scope/clear controls.
- Snippets / Queries: the global saved-SQL stores (backend/saved.py).
  Activating a snippet inserts it into the console at the cursor;
  activating a saved query opens it in a new console. The + button
  saves the console's selection (or whole editor) under a name.
- Aggregate: the count/sum/avg/min/max summary of the cells selected
  in the active grid. set_aggregate() keeps it current as the
  selection changes — so opening the panel is enough to read it — and
  show_aggregate() (the grid's Aggregate menu item) additionally
  brings the page to the front.
- Notes: free-form Markdown notes (backend/notes.py, notes.toml)
  scoped to a connection, a table or nothing. The window hands it the
  active tab's object and the workspace's connection names through
  set_note_target, which is what a new note defaults to and what the
  orphan badge is decided against.
- Filters: the workspace's saved filter sets for the active table
  (keyed connection.database.table). Activating one applies it; the
  + button saves the table's current filter under a name. The window
  owns the storage (Workspace.saved_filters) and hands entries in
  through set_filter_target.
"""

from __future__ import annotations

from typing import Callable

from gi.repository import Adw, Gtk, Pango

from sqlide.backend.saved import SavedItem, SavedStore
from sqlide.backend.saved import queries as queries_store
from sqlide.backend.saved import snippets as snippets_store
from sqlide.backend.workspaces import HistoryEntry
from sqlide.frontend.history_panel import HistoryPanel
from sqlide.frontend.notes_panel import NotesPage
from sqlide.frontend.sql_editor import SqlEditor
from sqlide.frontend.util import describe
from sqlide.frontend.value_view import CellValue, ValuePage
from sqlide.i18n import _

_CONTEXT_PAGES = {
    "console": (
        "properties",
        "info",
        "value",
        "snippets",
        "queries",
        "notes",
        "history",
        "aggregate",
    ),
    "table": (
        "properties",
        "info",
        "value",
        "notes",
        "history",
        "aggregate",
        "filters",
    ),
    "grid": (
        "properties",
        "info",
        "value",
        "notes",
        "history",
        "aggregate",
    ),
    "other": ("properties", "info", "notes", "history"),
}


def ask_name(
    parent: Gtk.Widget,
    heading: str,
    initial: str,
    on_done: Callable[[str], None],
    extra: Gtk.Widget | None = None,
) -> None:
    """Small name prompt used when saving snippets/queries/filters.

    `extra` is an optional widget shown under the entry — the "save the
    chart too" check when the console has one (CORE-33)."""
    entry = Gtk.Entry(text=initial, activates_default=True)
    child: Gtk.Widget = entry
    if extra is not None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.append(entry)
        box.append(extra)
        child = box
    dialog = Adw.AlertDialog(heading=heading, extra_child=child)
    dialog.add_response("cancel", _("Cancel"))
    dialog.add_response("save", _("Save"))
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
        on_use: Callable[[SavedItem], None],
        get_sql: Callable[[], str],
        on_error: Callable[[str], None],
        get_chart: Callable[[], str] | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._store = store
        self._on_use = on_use
        self._get_sql = get_sql
        self._on_error = on_error
        # Queries only: the chart the console is showing, offered
        # alongside the SQL when there is one (CORE-33).
        self._get_chart = get_chart
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
            describe(delete, _("Delete"))
            delete.connect(
                "clicked", lambda _b, it=item: self._store.remove(it)
            )
            row.add_suffix(delete)
            self._list.append(row)

    def _row_activated(self, _list, row) -> None:
        self._on_use(self._items[row.get_index()])

    def _save_current(self, *_args) -> None:
        sql = self._get_sql().strip()
        if not sql:
            self._on_error("Nothing to save — the console is empty")
            return
        chart = self._get_chart() if self._get_chart is not None else ""
        check: Gtk.CheckButton | None = None
        if chart:
            check = Gtk.CheckButton(
                label=_("Save the chart with it"), active=True
            )
        ask_name(
            self,
            "Save As",
            _first_line(sql)[:40],
            lambda name: self._store.add(
                name,
                sql,
                chart if check is not None and check.get_active() else "",
            ),
            extra=check,
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
        describe(add, _("Save the table's current filter"))
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
            label=_("No saved filters for this table"),
            margin_top=24,
            wrap=True,
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
            describe(delete, _("Delete"))
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
        on_open_query: Callable[[SavedItem], None],
        get_console_sql: Callable[[], str],
        on_error: Callable[[str], None],
        on_apply_filter: Callable[[dict], None],
        on_save_filter: Callable[[str], None],
        on_delete_filter: Callable[[dict], None],
        properties: Gtk.Widget | None = None,
        get_console_chart: Callable[[], str] | None = None,
    ) -> None:
        super().__init__()

        # The properties surface belongs to the window (it needs a
        # connector and an object to open links into), so it is handed
        # in; without one the page is a placeholder, which is what the
        # panel's tests and any harness without a window get.
        self._properties = properties
        if self._properties is None:
            placeholder = Gtk.Label(
                label=_("No properties to show"), margin_top=24, wrap=True
            )
            placeholder.add_css_class("dim-label")
            self._properties = placeholder

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
            label=_("Open a tab to see its details"), margin_top=24
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
            on_use=lambda item: on_insert_snippet(item.sql),
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
            get_chart=get_console_chart,
            on_error=on_error,
        )
        self._notes = NotesPage()
        self._value = ValuePage()
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
            label=_("Select cells in a grid to summarise them"),
            margin_top=24,
            wrap=True,
        )
        self._agg_placeholder.add_css_class("dim-label")
        agg_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        agg_box.append(self._agg_placeholder)
        agg_box.append(self._agg_label)
        agg_page = Gtk.ScrolledWindow(child=agg_box, vexpand=True, hexpand=True)

        self._stack = Adw.ViewStack()
        for name, title, icon, child in (
            (
                "properties",
                "Properties",
                "view-list-symbolic",
                self._properties,
            ),
            ("info", "Info", "dialog-information-symbolic", info_page),
            ("value", "Value", "text-x-generic-symbolic", self._value),
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
                "notes",
                "Notes",
                "view-paged-symbolic",
                self._notes,
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
        if "value" not in names:
            # The Value page is about a grid cell; a tab without a grid
            # must not leave the previous tab's cell standing.
            self.set_value(None)
        if "aggregate" not in names:
            # The summary belongs to a grid's selection; a tab without
            # one must not leave the previous tab's numbers standing.
            self.set_aggregate([])
        pages = self._stack.get_pages()
        for i in range(pages.get_n_items()):
            page = pages.get_item(i)
            page.set_visible(page.get_name() in names)
        current = self._stack.get_visible_child_name()
        if current not in names:
            self._stack.set_visible_child_name(names[0])

    # Properties

    def set_properties_target(self, profile, ref) -> None:
        """Show the active tab's object on the Properties page — its
        own surface, so the previous object's is left as it was
        (CORE-50) — or nothing (both None) for a tab about no
        object."""
        if hasattr(self._properties, "set_target"):
            self._properties.set_target(profile, ref)

    def show_properties(self, section: str = "") -> None:
        """Bring the Properties page to the front, on one section when
        a deep link named one (CORE-05/CORE-47)."""
        self._stack.set_visible_child_name("properties")
        if section and hasattr(self._properties, "select_section"):
            self._properties.select_section(section)

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

    # Notes

    def set_note_target(
        self,
        connection: str,
        table: str,
        connections: list[str] | None = None,
    ) -> None:
        """The object a new note defaults to (the active tab's), and
        the connections that still exist, for the orphan badge."""
        self._notes.set_target(connection, table, connections)

    # Filters

    def set_filter_target(self, key: str, entries: list[dict]) -> None:
        self._filters.set_target(key, entries)

    # Value (CORE-42)

    def set_value(self, cell: CellValue | None) -> None:
        """Show the grid's focused cell without moving the panel: the
        grid calls this on every selection change, whether or not
        anyone is looking."""
        self._value.set_value(cell)

    def show_value(self, cell: CellValue | None) -> None:
        """Fill the Value page and bring it to the front (the cell
        menu's "View Value")."""
        self.set_value(cell)
        self._stack.set_visible_child_name("value")

    # Aggregate

    def set_aggregate(self, lines: list[str]) -> None:
        """Fill (or, with no lines, empty) the aggregate page without
        moving the panel: the grid calls this on every selection
        change, whether or not anyone is looking."""
        self._agg_label.set_text("\n".join(lines).expandtabs(12))
        self._agg_label.set_visible(bool(lines))
        self._agg_placeholder.set_visible(not lines)

    def show_aggregate(self, lines: list[str]) -> None:
        """Fill the aggregate page and switch to it (the window reveals
        the panel itself)."""
        self.set_aggregate(lines)
        self._stack.set_visible_child_name("aggregate")

    def show_history(self) -> None:
        self._stack.set_visible_child_name("history")
