"""Main window of one workspace: collapsible sidebar + tabbed content.

Opened by the application for a chosen workspace. The sidebar lists
only this workspace's connections; other workspaces are reached
through the launcher (Workspaces button). This window owns its cache
of open connectors. ensure_connector() is the blocking accessor handed
to child widgets; they must only call it from run_async worker threads.

Open tabs are part of the workspace: they are restored on open and
saved back to the workspace file whenever they change and when the
window closes (which also captures query-console SQL and the selected
tab). When no tabs are open the content area shows a status message.

Layout: the left OverlaySplitView (connections sidebar) wraps the
content area; inside it, below the content header and tab bar, a right
OverlaySplitView (query history, hidden by default) wraps the tab
stack — so the panel never reaches the window controls at the top.
The window owns the shared
Gtk.StringList of connection names that every query console's dropdown
observes, records each console run into the workspace history, and
loads history entries back into a console when activated.
"""

from __future__ import annotations

import threading
from datetime import datetime

from gi.repository import Adw, GLib, GObject, Gtk

from sqlide.frontend.util import main_menu_button

from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.db import registry
from sqlide.backend.db.base import Connector, ConnectorError
from sqlide.backend.workspaces import HistoryEntry, Workspace
from sqlide.frontend.connection_dialog import ConnectionDialog
from sqlide.frontend.data_grid import TableTab
from sqlide.frontend.query_console import QueryConsole
from sqlide.frontend.side_panel import SidePanel
from sqlide.frontend.sidebar import Sidebar


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, workspace: Workspace, **kwargs) -> None:
        super().__init__(**kwargs)
        self.workspace = workspace
        self.set_title(f"sqlide — {workspace.name}")
        self.set_default_size(1100, 700)

        self._store = self.get_application().workspace_store
        self._connectors: dict[str, Connector] = {}
        self._connectors_lock = threading.Lock()
        self._restoring = False
        self._last_connection = ""
        self._connection_names = Gtk.StringList.new(
            [p.name for p in workspace.connections]
        )

        self._split = Adw.OverlaySplitView()
        self._split.set_min_sidebar_width(220)

        # Sidebar
        sidebar_header = Adw.HeaderBar()
        sidebar_header.set_title_widget(Gtk.Label(label="Connections"))
        add_button = Gtk.Button(icon_name="list-add-symbolic")
        add_button.set_tooltip_text("Add connection")
        add_button.connect("clicked", self._add_connection)
        sidebar_header.pack_start(add_button)
        workspaces_button = Gtk.Button(icon_name="view-grid-symbolic")
        workspaces_button.set_tooltip_text("Workspaces")
        workspaces_button.connect(
            "clicked", lambda *_: self.get_application().show_launcher()
        )
        sidebar_header.pack_end(workspaces_button)

        self._sidebar = Sidebar(
            ensure_connector=self.ensure_connector,
            on_open_table=self.open_table,
            on_new_query=self.new_query,
            show_error=self.show_error,
        )
        sidebar_view = Adw.ToolbarView()
        sidebar_view.add_top_bar(sidebar_header)
        sidebar_view.set_content(self._sidebar)
        self._split.set_sidebar(sidebar_view)

        # Content: header + tab bar on top, tabs (or a placeholder) below
        self._tab_view = Adw.TabView()
        self._tab_view.connect("page-attached", self._on_pages_changed)
        self._tab_view.connect("page-detached", self._on_pages_changed)
        self._tab_view.connect("page-reordered", self._on_pages_changed)
        tab_bar = Adw.TabBar(view=self._tab_view)

        placeholder = Adw.StatusPage(
            icon_name="folder-open-symbolic",
            title="Nothing Open",
            description="Pick a table from the sidebar, or open a query "
            "console from a connection row.",
        )
        self._stack = Gtk.Stack()
        self._stack.add_named(placeholder, "placeholder")
        self._stack.add_named(self._tab_view, "tabs")

        content_header = Adw.HeaderBar()
        sidebar_toggle = Gtk.ToggleButton(icon_name="sidebar-show-symbolic")
        sidebar_toggle.set_tooltip_text("Toggle sidebar")
        self._split.bind_property(
            "show-sidebar",
            sidebar_toggle,
            "active",
            GObject.BindingFlags.SYNC_CREATE
            | GObject.BindingFlags.BIDIRECTIONAL,
        )
        content_header.pack_start(sidebar_toggle)
        new_query_button = Gtk.Button(icon_name="utilities-terminal-symbolic")
        new_query_button.set_tooltip_text("New query console")
        new_query_button.connect(
            "clicked", lambda *_: self.new_query(self._default_query_profile())
        )
        content_header.pack_start(new_query_button)

        content_header.pack_end(main_menu_button())

        overview_button = Adw.TabButton(view=self._tab_view)
        overview_button.set_tooltip_text("View open tabs")
        overview_button.connect(
            "clicked", lambda *_: self._overview.set_open(True)
        )
        content_header.pack_end(overview_button)

        # Side panel (history + aggregate) in a right sidebar, hidden by
        # default. It sits inside the content area (below the header bar
        # and tab bar), so its edge and toggle stay away from the window
        # controls.
        self._side_panel = SidePanel(
            on_activate=self._history_activated, on_clear=self._clear_history
        )
        self._side_panel.set_entries(workspace.history)
        self._history_split = Adw.OverlaySplitView(
            sidebar_position=Gtk.PackType.END, show_sidebar=False
        )
        self._history_split.set_min_sidebar_width(260)
        self._history_split.set_sidebar(self._side_panel)
        self._history_split.set_content(self._stack)

        content_view = Adw.ToolbarView()
        content_view.add_top_bar(content_header)
        content_view.add_top_bar(tab_bar)
        content_view.set_content(self._history_split)
        self._split.set_content(content_view)

        history_toggle = Gtk.ToggleButton(
            icon_name="document-open-recent-symbolic"
        )
        history_toggle.set_tooltip_text("Toggle query history")
        self._history_split.bind_property(
            "show-sidebar",
            history_toggle,
            "active",
            GObject.BindingFlags.SYNC_CREATE
            | GObject.BindingFlags.BIDIRECTIONAL,
        )
        content_header.pack_end(history_toggle)

        # Collapse the sidebars into overlays on narrow windows.
        breakpoint = Adw.Breakpoint.new(
            Adw.BreakpointCondition.parse("max-width: 860sp")
        )
        breakpoint.add_setter(self._split, "collapsed", True)
        breakpoint.add_setter(self._history_split, "collapsed", True)
        self.add_breakpoint(breakpoint)

        # Tab overview: zoomed-out grid of tab thumbnails. It must wrap
        # the widget tree that contains the TabView so open tabs stay
        # visible (scaled down) while the overview is shown.
        self._overview = Adw.TabOverview(
            view=self._tab_view,
            enable_new_tab=True,
            child=self._split,
        )
        self._overview.connect("create-tab", self._create_tab)

        self._toasts = Adw.ToastOverlay(child=self._overview)
        self.set_content(self._toasts)
        self.connect("close-request", self._on_close_request)

        for profile in workspace.connections:
            self._sidebar.add_profile(profile)
        self._restore_tabs()

    # Backend access (blocking — worker threads only)

    def ensure_connector(self, profile: ConnectionProfile) -> Connector:
        with self._connectors_lock:
            connector = self._connectors.get(profile.name)
            if connector is None:
                if not registry.driver_available(profile.kind):
                    raise ConnectorError(
                        f"No driver installed for {profile.kind} connections"
                    )
                connector = registry.create_connector(
                    profile.kind, **profile.connect_params()
                )
                connector.connect()
                self._connectors[profile.name] = connector
                # Runs on a worker thread; the sidebar dot must flip on
                # the main loop.
                GLib.idle_add(
                    self._sidebar.set_connected, profile.name, True
                )
            return connector

    def is_connected(self, name: str) -> bool:
        with self._connectors_lock:
            return name in self._connectors

    # Workspace persistence

    def _restore_tabs(self) -> None:
        self._restoring = True
        try:
            for tab in self.workspace.tabs:
                profile = self.workspace.find_connection(tab.connection)
                if tab.kind == "table" and tab.table:
                    if profile is not None:
                        self.open_table(profile, tab.table)
                elif tab.kind == "query":
                    # Restore the console even if its connection is gone.
                    self.new_query(profile, sql=tab.sql)
            selected = self.workspace.selected_tab
            if 0 <= selected < self._tab_view.get_n_pages():
                self._tab_view.set_selected_page(
                    self._tab_view.get_nth_page(selected)
                )
        finally:
            self._restoring = False
        self._update_placeholder()

    def _save_state(self) -> None:
        selected = self._tab_view.get_selected_page()
        self.workspace.selected_tab = -1
        tabs = []
        for i in range(self._tab_view.get_n_pages()):
            page = self._tab_view.get_nth_page(i)
            if page is selected:
                self.workspace.selected_tab = len(tabs)
            tabs.append(page.get_child().tab_state())
        self.workspace.tabs = tabs
        try:
            self._store.save(self.workspace)
        except Exception as exc:
            self.show_error(f"Could not save workspace: {exc}")

    def _on_pages_changed(self, *_args) -> None:
        self._update_placeholder()
        if not self._restoring:
            self._save_state()

    def _on_close_request(self, *_args) -> bool:
        self._save_state()
        return False

    # UI actions (main thread)

    def show_error(self, message: str) -> None:
        self._toasts.add_toast(Adw.Toast(title=message))

    def show_aggregate(self, lines: list[str]) -> None:
        """Route a grid's Aggregate summary into the side panel."""
        self._side_panel.show_aggregate(lines)
        self._history_split.set_show_sidebar(True)

    def _add_connection(self, *_args) -> None:
        ConnectionDialog(on_save=self._profile_added).present(self)

    def _profile_added(self, profile: ConnectionProfile) -> None:
        self.workspace.add_connection(profile)  # also deduplicates the name
        self._store.save(self.workspace)
        # All open consoles' dropdowns share this list and update live.
        self._connection_names.append(profile.name)
        self._sidebar.add_profile(profile)
        self._sidebar.expand_profile(profile.name)

    def open_table(self, profile: ConnectionProfile, table: str) -> None:
        key = ("table", profile.name, table)
        for i in range(self._tab_view.get_n_pages()):
            page = self._tab_view.get_nth_page(i)
            if getattr(page.get_child(), "tab_key", None) == key:
                self._tab_view.set_selected_page(page)
                return
        tab = TableTab(
            profile,
            table,
            self.ensure_connector,
            self.show_error,
            on_aggregate=self.show_aggregate,
        )
        tab.tab_key = key
        page = self._tab_view.append(tab)
        page.set_title(f"{profile.name} ▸ {table}")
        page.set_tooltip(f"{table} on {profile.name} ({profile.kind})")
        self._tab_view.set_selected_page(page)

    def new_query(
        self, profile: ConnectionProfile | None = None, sql: str = ""
    ) -> Adw.TabPage:
        console = QueryConsole(
            self._connection_names,
            self.workspace.find_connection,
            self.ensure_connector,
            sql=sql,
            connection=profile.name if profile is not None else "",
            on_ran=self._query_ran,
            on_aggregate=self.show_aggregate,
        )
        page = self._tab_view.append(console)

        def set_title(name: str) -> None:
            page.set_title(f"query · {name}" if name else "query")
            page.set_tooltip(
                f"Query console on {name}" if name else "Query console"
            )
            if not self._restoring:
                self._save_state()

        console.on_connection_changed = set_title
        set_title(console.selected_connection())
        self._tab_view.set_selected_page(page)
        return page

    def _default_query_profile(self) -> ConnectionProfile | None:
        """Connection preselected in a new console: the current tab's,
        else the last-used, else the workspace's first (else none)."""
        page = self._tab_view.get_selected_page()
        if page is not None:
            child = page.get_child()
            if isinstance(child, TableTab):
                return child.profile
            if isinstance(child, QueryConsole):
                profile = self.workspace.find_connection(
                    child.selected_connection()
                )
                if profile is not None:
                    return profile
        profile = self.workspace.find_connection(self._last_connection)
        if profile is not None:
            return profile
        if self.workspace.connections:
            return self.workspace.connections[0]
        return None

    def _create_tab(self, *_args) -> Adw.TabPage:
        return self.new_query(self._default_query_profile())

    # Query history

    def _query_ran(self, sql: str, connection: str, ok: bool) -> None:
        self._last_connection = connection
        self.workspace.add_history(HistoryEntry(
            sql=sql,
            connection=connection,
            timestamp=datetime.now().isoformat(timespec="seconds"),
            ok=ok,
        ))
        self._save_state()
        self._side_panel.set_entries(self.workspace.history)

    def _clear_history(self) -> None:
        self.workspace.history.clear()
        self._save_state()
        self._side_panel.set_entries([])

    def _history_activated(self, entry: HistoryEntry) -> None:
        page = self._tab_view.get_selected_page()
        child = page.get_child() if page is not None else None
        if isinstance(child, QueryConsole):
            child.set_sql(entry.sql)
            child.select_connection(entry.connection)
        else:
            self.new_query(
                self.workspace.find_connection(entry.connection),
                sql=entry.sql,
            )

    def _update_placeholder(self, *_args) -> None:
        has_tabs = self._tab_view.get_n_pages() > 0
        self._stack.set_visible_child_name(
            "tabs" if has_tabs else "placeholder"
        )
