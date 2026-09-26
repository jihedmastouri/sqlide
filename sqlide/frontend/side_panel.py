"""Right side panel of the main window.

Two tabs behind an `Adw.ViewSwitcher` in the panel's header — Files and
Info — chosen because the panel answers two unrelated questions: "what
SQL have I saved?" and "what am I looking at?". Mixing them into one
flat row of eight pages made the switcher unreadable and put Snippets
next to Aggregate for no reason.

**Files** is the saved-SQL store as a two-folder tree: Snippets
(fragments inserted into the console at the cursor) and Scripts (whole
queries, opened in a console of their own). A search entry filters both
folders by name and by SQL text, and every item has a pin toggle;
pinned items sort to the top of their folder and nowhere else. The +
button on a folder saves the console's selection (or whole editor)
into it.

**Info** is everything about the active tab, as sub-pages the tab's
kind decides (the window reports it through set_context):

- Properties: everything about the active tab's object — the sections
  its engine has, its DDL, its children (CORE-47) — beside the data
  rather than instead of it. The widget is an
  object_info.PropertiesSurfaces the window owns: one PropertiesView
  per object, swapped as tabs change rather than one view retargeted
  (CORE-50). This is the compact host of the one properties renderer
  (CORE-59); asking for Properties outright opens the full
  object_info.PropertiesTab instead. It absorbed the old Info page:
  connection details and a tab's DDL are properties of the object the
  tab is about, and were being rendered twice.
- Aggregation: the count/sum/avg/min/max summary of the cells selected
  in the active grid. set_aggregate() keeps it current as the selection
  changes — so opening the panel is enough to read it — and
  show_aggregate() (the grid's Aggregate menu item) additionally brings
  the page to the front.
- Record: the focused grid row read down the page instead of across it
  (CORE-42), over the focused *cell* in full — wrapped text,
  pretty-printed JSON, or a hex/ASCII dump with the byte length. The
  record half is the active tab's own value_view.RecordView, handed in
  by the window and swapped as tabs change (set_record), so each tab
  keeps its own position in its own rows; the cell half is the panel's
  and is filled by the grid on every selection change. This used to be
  a Data | Record toggle inside the table tab and a separate Value
  page; both were about one row of one grid, so they are one page.
- History: the query-history list (HistoryPanel) with its own
  scope/clear controls.
- Filters: the workspace's saved filter sets for the active table
  (keyed connection.database.table). Activating one applies it; the
  + button saves the table's current filter under a name. The window
  owns the storage (Workspace.saved_filters) and hands entries in
  through set_filter_target.

Which sub-pages a tab offers:

- query console ("console"): Properties, Aggregation, Record, History
- table data tab ("table"):  the same, plus Filters
- other result grids ("grid"): Properties, Aggregation, Record, History
- everything else ("other"): Properties, History
"""

from __future__ import annotations

from typing import Callable

from gi.repository import Adw, Gtk, Pango

from sqlide.backend.saved import SavedItem, SavedStore
from sqlide.backend.saved import queries as queries_store
from sqlide.backend.saved import snippets as snippets_store
from sqlide.backend.workspaces import HistoryEntry
from sqlide.frontend.history_panel import HistoryPanel
from sqlide.frontend.util import describe
from sqlide.frontend.value_view import CellValue, ValuePage
from sqlide.i18n import _

_CONTEXT_PAGES = {
    "console": ("properties", "aggregate", "record", "history"),
    "table": ("properties", "aggregate", "record", "history", "filters"),
    "grid": ("properties", "aggregate", "record", "history"),
    "other": ("properties", "history"),
}


# Where the Record page splits between the row and the focused cell.
# Enough for a handful of columns before the cell view takes over; the
# divider is draggable from there.
_RECORD_SPLIT = 320


def _placeholder(text: str) -> Gtk.Label:
    """The dim "nothing to show yet" label the pages share."""
    label = Gtk.Label(label=text, margin_top=24, wrap=True)
    label.add_css_class("dim-label")
    return label


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


class _Folder:
    """One folder of the Files tree: an `Adw.ExpanderRow` over one
    saved-SQL store, rebuilt whenever the store changes or the search
    text does.

    Pinned items come first, each group in the store's own order — the
    order things were saved in — so pinning promotes an item without
    otherwise shuffling the folder under the user.
    """

    def __init__(
        self,
        title: str,
        icon: str,
        store: SavedStore,
        save_tooltip: str,
        empty_text: str,
        on_use: Callable[[SavedItem], None],
        on_save: Callable[[], None],
    ) -> None:
        self.store = store
        self._empty_text = empty_text
        self._on_use = on_use
        self._rows: list[Gtk.Widget] = []
        self.row = Adw.ExpanderRow(
            title=title, icon_name=icon, expanded=True
        )
        add = Gtk.Button(icon_name="list-add-symbolic", valign=Gtk.Align.CENTER)
        add.add_css_class("flat")
        describe(add, save_tooltip)
        add.connect("clicked", lambda *_: on_save())
        self.row.add_suffix(add)

    def fill(self, items: list[SavedItem], search: str) -> None:
        for row in self._rows:
            self.row.remove(row)
        self._rows = []
        needle = search.casefold()
        matches = [
            item
            for item in items
            if not needle
            or needle in item.name.casefold()
            or needle in item.sql.casefold()
        ]
        matches.sort(key=lambda item: not item.pinned)
        self.row.set_subtitle(
            _("{n} of {total}").format(n=len(matches), total=len(items))
            if needle
            else _("{n} saved").format(n=len(items))
        )
        if not matches:
            # An Adw.ActionRow, not a bare label: add_row() wraps a
            # plain widget in a list row of its own making, which
            # remove() then cannot find again.
            empty = Adw.ActionRow(
                title=_("Nothing here") if needle else self._empty_text,
                activatable=False,
            )
            empty.set_use_markup(False)
            empty.set_title_lines(0)
            empty.add_css_class("dim-label")
            self._add(empty)
            return
        for item in matches:
            self._add(self._item_row(item))

    def _add(self, widget: Gtk.Widget) -> None:
        self.row.add_row(widget)
        self._rows.append(widget)

    def _item_row(self, item: SavedItem) -> Gtk.Widget:
        row = Adw.ActionRow(activatable=True)
        row.set_use_markup(False)
        row.set_title(item.name)
        row.set_title_lines(1)
        row.set_subtitle(_first_line(item.sql))
        row.set_subtitle_lines(1)
        row.set_tooltip_text(item.sql)
        row.connect("activated", lambda *_: self._on_use(item))
        pin = Gtk.ToggleButton(
            icon_name="view-pin-symbolic",
            active=item.pinned,
            valign=Gtk.Align.CENTER,
        )
        pin.add_css_class("flat")
        describe(pin, _("Unpin") if item.pinned else _("Pin to the top"))
        pin.connect(
            "toggled",
            lambda button, it=item: self.store.set_pinned(
                it, button.get_active()
            ),
        )
        row.add_suffix(pin)
        delete = Gtk.Button(
            icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER
        )
        delete.add_css_class("flat")
        describe(delete, _("Delete"))
        delete.connect("clicked", lambda _b, it=item: self.store.remove(it))
        row.add_suffix(delete)
        return row


class _FilesPage(Gtk.Box):
    """The Files tab: a search entry over the Snippets and Scripts
    folders. Follows both stores live (a save in any window shows up
    here) and unsubscribes when destroyed."""

    def __init__(
        self,
        on_insert_snippet: Callable[[str], None],
        on_open_query: Callable[[SavedItem], None],
        get_sql: Callable[[], str],
        on_error: Callable[[str], None],
        get_chart: Callable[[], str] | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._get_sql = get_sql
        self._get_chart = get_chart
        self._on_error = on_error

        self._search = Gtk.SearchEntry(
            placeholder_text=_("Search snippets and scripts"),
            margin_top=6,
            margin_bottom=6,
            margin_start=6,
            margin_end=6,
        )
        # notify::text rather than search-changed: the folders hold a
        # handful of rows each, so there is nothing to debounce, and
        # filtering that lags the keystroke by GTK's delay reads as the
        # entry being stuck.
        self._search.connect("notify::text", lambda *_: self.refresh())
        self.append(self._search)

        self._folders = [
            _Folder(
                _("Snippets"),
                "insert-text-symbolic",
                snippets_store,
                _(
                    "Save the console's selection (or whole editor) as a "
                    "snippet"
                ),
                _(
                    "No saved snippets yet — the + button saves the "
                    "console's selection"
                ),
                on_use=lambda item: on_insert_snippet(item.sql),
                on_save=lambda: self._save_into(snippets_store, chart=False),
            ),
            _Folder(
                _("Scripts"),
                "emblem-documents-symbolic",
                queries_store,
                _(
                    "Save the console's selection (or whole editor) as a "
                    "script"
                ),
                _(
                    "No saved scripts yet — the + button saves the "
                    "console's selection"
                ),
                on_use=on_open_query,
                on_save=lambda: self._save_into(queries_store, chart=True),
            ),
        ]
        self._list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self._list.add_css_class("boxed-list")
        self._list.set_margin_start(6)
        self._list.set_margin_end(6)
        self._list.set_margin_bottom(6)
        for folder in self._folders:
            self._list.append(folder.row)
        self.append(
            Gtk.ScrolledWindow(child=self._list, vexpand=True, hexpand=True)
        )

        self.refresh()
        for folder in self._folders:
            folder.store.subscribe(self._store_changed)
        self.connect("destroy", self._unsubscribe)

    def refresh(self, *_args) -> None:
        search = self._search.get_text().strip()
        for folder in self._folders:
            folder.fill(folder.store.load(), search)

    def _store_changed(self, _items: list[SavedItem]) -> None:
        self.refresh()

    def _unsubscribe(self, *_args) -> None:
        for folder in self._folders:
            folder.store.unsubscribe(self._store_changed)

    def _save_into(self, store: SavedStore, chart: bool) -> None:
        """The + button of a folder: name the console's SQL and put it
        there. Scripts may carry the console's chart with them
        (CORE-33); snippets are SQL fragments and never do."""
        sql = self._get_sql().strip()
        if not sql:
            self._on_error("Nothing to save — the console is empty")
            return
        state = self._get_chart() if chart and self._get_chart else ""
        check: Gtk.CheckButton | None = None
        if state:
            check = Gtk.CheckButton(
                label=_("Save the chart with it"), active=True
            )
        ask_name(
            self,
            "Save As",
            _first_line(sql)[:40],
            lambda name: store.add(
                name,
                sql,
                state if check is not None and check.get_active() else "",
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
            self._properties = _placeholder(_("No properties to show"))

        self._files = _FilesPage(
            on_insert_snippet=on_insert_snippet,
            on_open_query=on_open_query,
            get_sql=get_console_sql,
            get_chart=get_console_chart,
            on_error=on_error,
        )
        self._history = HistoryPanel(on_activate=on_activate, on_clear=on_clear)
        self._filters = _FiltersPage(
            on_apply=on_apply_filter,
            on_save=on_save_filter,
            on_delete=on_delete_filter,
        )

        # Record page: the active tab's own RecordView above the focused
        # cell in full. The record half is swapped per tab (set_record)
        # so each tab keeps its place in its own rows; a tab without a
        # grid leaves the placeholder standing.
        self._record_placeholder = _placeholder(
            _("Open a table or run a query to read a row here")
        )
        self._record_host = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, vexpand=True
        )
        self._record_host.append(self._record_placeholder)
        self._record: Gtk.Widget | None = None
        self._value = ValuePage()
        record_page = Gtk.Paned(
            orientation=Gtk.Orientation.VERTICAL,
            start_child=self._record_host,
            end_child=self._value,
            resize_start_child=True,
            resize_end_child=True,
            shrink_start_child=False,
            shrink_end_child=False,
            position=_RECORD_SPLIT,
        )

        self._agg_label = Gtk.Label(
            justify=Gtk.Justification.LEFT,
            xalign=0,
            yalign=0,
            selectable=True,
        )
        self._agg_label.add_css_class("aggregate-summary")
        self._agg_placeholder = _placeholder(
            _("Select cells in a grid to summarise them")
        )
        agg_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        agg_box.append(self._agg_placeholder)
        agg_box.append(self._agg_label)
        agg_page = Gtk.ScrolledWindow(child=agg_box, vexpand=True, hexpand=True)

        # The Info tab's own pages, behind their own switcher: the outer
        # switcher is Files | Info and never changes, so which of these
        # a tab offers can be toggled freely underneath it.
        self._stack = Adw.ViewStack(vexpand=True)
        for name, title, icon, child in (
            (
                "properties",
                "Properties",
                "view-list-symbolic",
                self._properties,
            ),
            (
                "aggregate",
                "Aggregation",
                "accessories-calculator-symbolic",
                agg_page,
            ),
            ("record", "Record", "text-x-generic-symbolic", record_page),
            (
                "history",
                "History",
                "document-open-recent-symbolic",
                self._history,
            ),
            ("filters", "Filters", "edit-find-symbolic", self._filters),
        ):
            self._stack.add_titled_with_icon(child, name, title, icon)
        info_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        info_switcher = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            vscrollbar_policy=Gtk.PolicyType.NEVER,
            child=Adw.ViewSwitcher(
                stack=self._stack,
                policy=Adw.ViewSwitcherPolicy.NARROW,
                margin_top=4,
                margin_bottom=4,
            ),
        )
        info_page.append(info_switcher)
        info_page.append(self._stack)

        self._top = Adw.ViewStack()
        self._top.add_titled_with_icon(
            self._files, "files", "Files", "folder-symbolic"
        )
        self._top.add_titled_with_icon(
            info_page, "info", "Info", "dialog-information-symbolic"
        )
        self._top.set_visible_child_name("info")

        header = Adw.HeaderBar()
        # No window controls in the panel header: the panel sits inside
        # the content area, so the close button belongs to the window's
        # own header bar only.
        header.set_show_start_title_buttons(False)
        header.set_show_end_title_buttons(False)
        header.set_title_widget(
            Adw.ViewSwitcher(
                stack=self._top, policy=Adw.ViewSwitcherPolicy.WIDE
            )
        )

        view = Adw.ToolbarView(hexpand=True)
        view.add_top_bar(header)
        view.set_content(self._top)
        self.append(view)
        self.set_context("other")

    # Context (which Info pages the active tab offers)

    def set_context(self, context: str) -> None:
        names = _CONTEXT_PAGES.get(context, _CONTEXT_PAGES["other"])
        if "record" not in names:
            # The record and the cell are about a grid; a tab without one
            # must not leave the previous tab's row standing.
            self.set_value(None)
            self.set_record(None)
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
        self._top.set_visible_child_name("info")
        self._stack.set_visible_child_name("properties")
        if section and hasattr(self._properties, "select_section"):
            self._properties.select_section(section)

    # History

    def set_entries(self, entries: list[HistoryEntry]) -> None:
        self._history.set_entries(entries)

    def set_active_panel(self, name: str) -> None:
        """Tab title of the selected tab, for local history scope."""
        self._history.set_active_panel(name)

    def show_history(self) -> None:
        self._top.set_visible_child_name("info")
        self._stack.set_visible_child_name("history")

    # Filters

    def set_filter_target(self, key: str, entries: list[dict]) -> None:
        self._filters.set_target(key, entries)

    # Record and value (CORE-42)

    def set_record(self, view: Gtk.Widget | None) -> None:
        """Host the active tab's record view, or nothing for a tab with
        no grid. The widget belongs to the tab, so it is only unparented
        here, never destroyed."""
        if view is self._record:
            return
        if self._record is not None:
            self._record_host.remove(self._record)
        self._record = view
        if view is not None:
            self._record_host.append(view)
        self._record_placeholder.set_visible(view is None)

    def set_value(self, cell: CellValue | None) -> None:
        """Show the grid's focused cell without moving the panel: the
        grid calls this on every selection change, whether or not
        anyone is looking."""
        self._value.set_value(cell)

    def show_value(self, cell: CellValue | None) -> None:
        """Fill the cell half of the Record page and bring it to the
        front (the cell menu's "View Value")."""
        self.set_value(cell)
        self._top.set_visible_child_name("info")
        self._stack.set_visible_child_name("record")

    # Aggregation

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
        self._top.set_visible_child_name("info")
        self._stack.set_visible_child_name("aggregate")

    def show_files(self) -> None:
        self._top.set_visible_child_name("files")
