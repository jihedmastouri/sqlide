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

Layout: a full-width header bar spans the whole window; below it the
left OverlaySplitView (connections sidebar) wraps the content area,
and inside that a right OverlaySplitView (the side panel, hidden by
default) wraps the tab area.
The tab area holds one or more panes (each an Adw.TabBar over an
Adw.TabView) side by side in nested Gtk.Paned splitters: the Split
button moves the current tab into a new pane (two panes at most,
sized evenly), so e.g. two tables can be shown next to each other.
New tabs open in the last-clicked pane; a pane whose last tab is
closed or moved away is removed.
The window owns the shared
Gtk.StringList of connection names that every query console's dropdown
observes, records each console run into the workspace history, and
loads history entries back into a console when activated.
"""

from __future__ import annotations

import threading
from dataclasses import asdict, fields
from datetime import datetime
from typing import Callable

from gi.repository import Adw, Gio, GLib, GObject, Gtk

from sqlide.frontend.util import main_menu_button, run_async

from sqlide.backend import identity
from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.db import registry
from sqlide.backend.db.base import Connector, ConnectorError, FilterCondition
from sqlide.backend.workspaces import HistoryEntry, Workspace
from sqlide.frontend.cli_console import CliConsole
from sqlide.frontend.connection_dialog import ConnectionDialog
from sqlide.frontend.data_grid import ResultGrid, TableTab
from sqlide.frontend.definition_tab import DefinitionTab, FunctionTab
from sqlide.frontend import identity as identity_ui
from sqlide.frontend.drop_dialog import present_drop_dialog
from sqlide.frontend.mcp_tab import McpServerTab
from sqlide.frontend.query_builder import QueryBuilderTab
from sqlide.frontend.query_console import QueryConsole
from sqlide.frontend.relation_graph import RelationGraphTab
from sqlide.frontend.side_panel import SidePanel
from sqlide.frontend.sidebar import Sidebar
from sqlide.frontend.table_designer import TableDesignerTab


def _page_connection(child: Gtk.Widget | None) -> str:
    """Which connection a tab is on: consoles follow their dropdown,
    every other tab its profile. "" for tabs with no connection."""
    if isinstance(child, (QueryConsole, CliConsole)):
        return child.selected_connection()
    profile = getattr(child, "profile", None)
    return profile.name if profile is not None else ""


class _HistoryTab(Gtk.Box):
    """Workspace-wide query history as a read-only grid (opened from
    the main menu's Query History). Not persisted across sessions —
    tab_state() returns None and _save_state skips it."""

    def __init__(self, entries: list[HistoryEntry]) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._grid = ResultGrid()
        self.append(self._grid)
        self.set_entries(entries)

    def set_entries(self, entries: list[HistoryEntry]) -> None:
        self._grid.set_result(
            ["Timestamp", "Panel", "Query", "Status"],
            [
                (e.timestamp, e.panel, e.sql, "OK" if e.ok else "failed")
                for e in reversed(entries)
            ],
        )

    def tab_state(self) -> None:
        return None


class _TabPane(Gtk.Box):
    """One tab pane: its own tab bar over an Adw.TabView. The window
    shows a single pane normally and several side by side after Split."""

    def __init__(self) -> None:
        super().__init__(
            orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True
        )
        self.view = Adw.TabView(vexpand=True)
        self.bar = Adw.TabBar(view=self.view)
        self.append(self.bar)
        self.append(self.view)


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

        # Sidebar. Its header sits below the full-width top banner, so
        # it must not repeat the window controls.
        sidebar_header = Adw.HeaderBar()
        sidebar_header.set_show_start_title_buttons(False)
        sidebar_header.set_show_end_title_buttons(False)
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
            on_open_cli=self.open_cli,
            on_open_definition=self.open_definition,
            on_open_function=self.open_function,
            on_relation_graph=self.open_relation_graph,
            on_query_builder=self.open_query_builder,
            on_drop_object=self._drop_object,
            on_new_object=self._new_object,
            on_mcp_server=self.open_mcp_server,
            on_edit_connection=self._edit_connection,
            on_remove_connection=self._remove_connection,
            show_error=self.show_error,
        )
        search = Gtk.SearchEntry(placeholder_text="Find tables…")
        search.set_tooltip_text(
            "Fuzzy-find tables, views and functions in loaded connections"
        )
        search.connect(
            "search-changed",
            lambda entry: self._sidebar.set_filter(entry.get_text()),
        )
        search_bar = Gtk.Box(
            margin_top=6, margin_bottom=6, margin_start=6, margin_end=6
        )
        search_bar.append(search)
        search.set_hexpand(True)
        sidebar_view = Adw.ToolbarView()
        sidebar_view.add_top_bar(sidebar_header)
        sidebar_view.add_top_bar(search_bar)
        sidebar_view.set_content(self._sidebar)
        self._split.set_sidebar(sidebar_view)

        # Content: one or more tab panes (each with its own tab bar) in
        # nested Paned splitters, or a placeholder when nothing is open.
        self._panes: list[_TabPane] = []
        self._panes_root = Gtk.Box(hexpand=True, vexpand=True)
        self._active_pane = self._add_pane()

        placeholder = Adw.StatusPage(
            icon_name="folder-open-symbolic",
            title="Nothing Open",
            description="Pick a table from the sidebar, or open a query "
            "console from a connection row.",
        )
        self._stack = Gtk.Stack()
        self._stack.add_named(placeholder, "placeholder")
        self._stack.add_named(self._panes_root, "tabs")

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
        new_query_button = Gtk.Button(icon_name="document-edit-symbolic")
        new_query_button.set_tooltip_text("New query console")
        new_query_button.connect(
            "clicked", lambda *_: self.new_query(self._default_query_profile())
        )
        content_header.pack_start(new_query_button)
        cli_button = Gtk.Button(icon_name="utilities-terminal-symbolic")
        cli_button.set_tooltip_text("New CLI client console")
        cli_button.connect("clicked", self._new_cli_console)
        content_header.pack_start(cli_button)
        builder_button = Gtk.Button(icon_name="system-run-symbolic")
        builder_button.set_tooltip_text("New query builder")
        builder_button.connect("clicked", self._new_query_builder)
        content_header.pack_start(builder_button)
        split_button = Gtk.Button(icon_name="view-dual-symbolic")
        split_button.set_tooltip_text("Split: move current tab to a new pane")
        split_button.connect("clicked", self._split_current_tab)
        content_header.pack_start(split_button)
        mcp_button = Gtk.Button(icon_name="network-transmit-receive-symbolic")
        mcp_button.set_tooltip_text("New MCP server")
        mcp_button.connect("clicked", lambda *_: self.open_mcp_server())
        content_header.pack_start(mcp_button)

        content_header.pack_end(main_menu_button(with_history=True))

        self._tab_button = Adw.TabButton(view=self._active_pane.view)
        self._tab_button.set_tooltip_text("View open tabs")
        self._tab_button.connect(
            "clicked", lambda *_: self._overview.set_open(True)
        )
        content_header.pack_end(self._tab_button)

        # Side panel (history + aggregate) in a right sidebar, hidden by
        # default. It sits inside the content area (below the header bar
        # and tab bar), so its edge and toggle stay away from the window
        # controls.
        self._side_panel = SidePanel(
            on_activate=self._history_activated,
            on_clear=self._clear_history,
            on_insert_snippet=self._insert_snippet,
            on_open_query=lambda sql: self.new_query(
                self._default_query_profile(), sql=sql
            ),
            get_console_sql=self._current_console_sql,
            on_error=self.show_error,
            on_apply_filter=self._apply_saved_filter,
            on_save_filter=self._save_current_filter,
            on_delete_filter=self._delete_saved_filter,
        )
        self._side_panel.set_entries(workspace.history)
        self._history_split = Adw.OverlaySplitView(
            sidebar_position=Gtk.PackType.END, show_sidebar=False
        )
        self._history_split.set_min_sidebar_width(260)
        self._history_split.set_sidebar(self._side_panel)
        self._history_split.set_content(self._stack)

        self._split.set_content(self._history_split)

        history_toggle = Gtk.ToggleButton(
            icon_name="sidebar-show-right-symbolic"
        )
        history_toggle.set_tooltip_text("Toggle side panel")
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

        # The header bar spans the full window width, above both the
        # sidebar and the content area; the workspace's identity stripe
        # sits directly under it, full width. The window title carries
        # the workspace name, so the stripe is never the only cue.
        top_view = Adw.ToolbarView()
        top_view.add_top_bar(content_header)
        self._stripe = identity_ui.stripe(workspace.color)
        top_view.add_top_bar(self._stripe)
        self._refresh_workspace_identity()
        top_view.set_content(self._split)

        # Tab overview: zoomed-out grid of tab thumbnails. It must wrap
        # the widget tree that contains the TabView so open tabs stay
        # visible (scaled down) while the overview is shown.
        self._overview = Adw.TabOverview(
            view=self._active_pane.view,
            enable_new_tab=True,
            child=top_view,
        )
        self._overview.connect("create-tab", self._create_tab)

        self._toasts = Adw.ToastOverlay(child=self._overview)
        self.set_content(self._toasts)
        self._close_confirmed = False
        self.connect("close-request", self._on_close_request)

        history_action = Gio.SimpleAction.new("history", None)
        history_action.connect(
            "activate", lambda *_: self.open_history_tab()
        )
        self.add_action(history_action)

        # The launcher can recolour a workspace while its window is
        # open, so the stripe is refreshed whenever the window comes
        # back to the front.
        self.connect(
            "notify::is-active", lambda *_: self._refresh_workspace_identity()
        )
        identity_ui.subscribe(self._on_palette_changed)
        self.connect(
            "destroy", lambda *_: identity_ui.unsubscribe(
                self._on_palette_changed
            )
        )

        for profile in workspace.connections:
            self._sidebar.add_profile(profile)
        self._restore_tabs()
        self._update_active_panel()

    # Identity (colour + environment)

    def _refresh_workspace_identity(self) -> None:
        identity_ui.set_color(self._stripe, self.workspace.color)
        color = identity.COLOR_LABELS[
            identity.normalize_color(self.workspace.color)
        ]
        self._stripe.set_tooltip_text(
            f"Workspace “{self.workspace.name}” · colour {color}"
        )
        self.set_title(f"sqlide — {self.workspace.name}")

    def _on_palette_changed(self) -> None:
        """The colour scheme flipped: CSS-driven surfaces restyle
        themselves, tab icons are textures and must be redrawn."""
        self._refresh_tab_identity()

    def _refresh_tab_identity(self) -> None:
        for pane in self._panes:
            for i in range(pane.view.get_n_pages()):
                page = pane.view.get_nth_page(i)
                self._apply_page_identity(
                    page, _page_connection(page.get_child())
                )

    def _apply_page_identity(self, page: Adw.TabPage, connection: str) -> None:
        """A tab wears its connection's colour as a leading bar. The
        connection name is already in the tab title, which is the
        non-colour cue."""
        profile = self.workspace.find_connection(connection)
        color = profile.color if profile is not None else identity.NONE
        page.set_icon(identity_ui.tab_icon(color))

    # Tab panes (split screen)

    def _add_pane(self) -> _TabPane:
        pane = _TabPane()
        pane.view.connect("page-attached", self._on_pages_changed)
        pane.view.connect("page-detached", self._on_pages_changed)
        pane.view.connect("page-reordered", self._on_pages_changed)
        pane.view.connect("close-page", self._on_close_page)
        pane.view.connect(
            "notify::selected-page",
            lambda *_: self._update_active_panel(),
        )
        # Any click inside a pane makes it the target for new tabs.
        click = Gtk.GestureClick(button=0)
        click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        click.connect("pressed", self._on_pane_pressed, pane)
        pane.add_controller(click)
        self._panes.append(pane)
        self._rebuild_panes()
        return pane

    def _rebuild_panes(self) -> None:
        """Re-nest the panes into a chain of horizontal Paned splitters
        (a single pane sits in the root box directly)."""
        for pane in self._panes:
            parent = pane.get_parent()
            if isinstance(parent, Gtk.Paned):
                if parent.get_start_child() is pane:
                    parent.set_start_child(None)
                else:
                    parent.set_end_child(None)
            elif parent is not None:
                parent.remove(pane)
        old = self._panes_root.get_first_child()
        if old is not None:
            self._panes_root.remove(old)
        root: Gtk.Widget = self._panes[0]
        for pane in self._panes[1:]:
            # shrink=True: a pane's minimum width (e.g. a console
            # toolbar) must never lock the divider or force the window
            # wider — scrollable content scrolls, the rest clips.
            paned = Gtk.Paned(
                orientation=Gtk.Orientation.HORIZONTAL,
                hexpand=True,
                vexpand=True,
                resize_start_child=True,
                resize_end_child=True,
                shrink_start_child=True,
                shrink_end_child=True,
            )
            paned.set_start_child(root)
            paned.set_end_child(pane)
            root = paned
        self._panes_root.append(root)
        # In a split, every pane keeps its tab bar even with a single
        # tab, so each tab stays visible and closable.
        single = len(self._panes) == 1
        for pane in self._panes:
            pane.bar.set_autohide(single)
        if not single:
            # Tick, not idle: waits out the frames before the rebuilt
            # chain has its allocation, then sets positions once.
            self._panes_root.add_tick_callback(self._equalize_tick)

    def _equalize_tick(self, _widget, _clock) -> bool:
        if self._panes_root.get_width() <= 0:
            return GLib.SOURCE_CONTINUE
        self._equalize_panes()
        return GLib.SOURCE_REMOVE

    def _equalize_panes(self) -> None:
        """Place the Paned dividers so the panes share the width evenly
        (a fresh Paned would otherwise size its children by their
        natural widths, leaving e.g. a sliver next to a wide grid)."""
        count = len(self._panes)
        total = self._panes_root.get_width()
        node = self._panes_root.get_first_child()
        if count < 2 or total <= 0:
            return
        # The chain is left-heavy: the outermost Paned holds the last
        # pane on the right and everything else on the left.
        while isinstance(node, Gtk.Paned):
            node.set_position(total * (count - 1) // count)
            total = node.get_position()
            count -= 1
            node = node.get_start_child()

    def _prune_empty_panes(self) -> bool:
        """Idle callback: drop panes whose last tab was closed or moved
        away, keeping at least one."""
        keep = [p for p in self._panes if p.view.get_n_pages() > 0]
        if not keep:
            keep = [self._panes[0]]
        if len(keep) != len(self._panes):
            self._panes = keep
            self._rebuild_panes()
            if self._active_pane not in self._panes:
                self._set_active_pane(self._panes[-1])
        return False

    def _set_active_pane(self, pane: _TabPane) -> None:
        self._active_pane = pane
        self._overview.set_view(pane.view)
        self._tab_button.set_view(pane.view)
        self._update_active_panel()

    def _update_active_panel(self) -> None:
        """Tell the side panel which tab is current: the This-panel
        history scope, which pages to offer (context), the Info
        content, and the saved-filter target."""
        page = self._active_pane.view.get_selected_page()
        child = page.get_child() if page is not None else None
        self._side_panel.set_active_panel(
            page.get_title() if page is not None else ""
        )
        if isinstance(child, QueryConsole):
            context = "console"
        elif isinstance(child, TableTab):
            context = "table"
        elif isinstance(child, (QueryBuilderTab, _HistoryTab)):
            context = "grid"
        else:
            context = "other"
        self._side_panel.set_context(context)
        if isinstance(child, TableTab):
            self._side_panel.set_filter_target(
                child.filter_key,
                self.workspace.saved_filters.get(child.filter_key, []),
            )
        else:
            self._side_panel.set_filter_target("", [])
        if isinstance(child, (QueryConsole, CliConsole)):
            self._set_console_info(child)
        else:
            self._refresh_side_ddl()

    def _set_console_info(self, console: QueryConsole | CliConsole) -> None:
        """Fill the side panel's Info page with the console's
        connection details."""
        profile = self.workspace.find_connection(
            console.selected_connection()
        )
        if profile is None:
            self._side_panel.set_info(
                "No connection", "This console has no connection selected."
            )
            return
        parts = [f"Kind: {profile.kind}"]
        if profile.file_path:
            parts.append(f"File: {profile.file_path}")
        if profile.kind not in ("sqlite", "jdbc"):
            parts.append(f"Host: {profile.host}:{profile.port or 'default'}")
        if profile.database:
            parts.append(f"Database: {profile.database}")
        if profile.user:
            parts.append(f"User: {profile.user}")
        if profile.jdbc_url:
            parts.append(f"JDBC URL: {profile.jdbc_url}")
        parts.append(
            "Connected: "
            + ("yes" if self.is_connected(profile.name) else "not yet")
        )
        self._side_panel.set_info(profile.name, "\n".join(parts))

    # Saved snippets, queries and filters (side panel callbacks)

    def _insert_snippet(self, sql: str) -> None:
        """A snippet was activated: into the current console at the
        cursor, or a fresh console when none is active."""
        page = self._active_pane.view.get_selected_page()
        child = page.get_child() if page is not None else None
        if isinstance(child, QueryConsole):
            child.insert_sql(sql)
        else:
            self.new_query(self._default_query_profile(), sql=sql)

    def _current_console_sql(self) -> str:
        page = self._active_pane.view.get_selected_page()
        child = page.get_child() if page is not None else None
        return child.current_sql() if isinstance(child, QueryConsole) else ""

    def _active_table_tab(self) -> TableTab | None:
        page = self._active_pane.view.get_selected_page()
        child = page.get_child() if page is not None else None
        return child if isinstance(child, TableTab) else None

    def _apply_saved_filter(self, entry: dict) -> None:
        tab = self._active_table_tab()
        if tab is None:
            return
        tab.apply_saved_filters(
            [FilterCondition(**f) for f in entry.get("filters", [])]
        )

    def _save_current_filter(self, name: str) -> None:
        tab = self._active_table_tab()
        if tab is None:
            return
        filters = tab.current_filters()
        if not filters:
            self.show_error("No filter to save — apply one to the table first")
            return
        entry = {"name": name, "filters": [asdict(f) for f in filters]}
        self.workspace.saved_filters.setdefault(tab.filter_key, []).append(
            entry
        )
        self._save_state()
        self._side_panel.set_filter_target(
            tab.filter_key, self.workspace.saved_filters[tab.filter_key]
        )

    def _delete_saved_filter(self, entry: dict) -> None:
        tab = self._active_table_tab()
        if tab is None:
            return
        entries = self.workspace.saved_filters.get(tab.filter_key, [])
        if entry in entries:
            entries.remove(entry)
            if not entries:
                del self.workspace.saved_filters[tab.filter_key]
            self._save_state()
        self._side_panel.set_filter_target(
            tab.filter_key,
            self.workspace.saved_filters.get(tab.filter_key, []),
        )

    def _refresh_side_ddl(self) -> None:
        """Fill the side panel's DDL page with the active table's
        CREATE statement (fetched once per tab, cached on the tab)."""
        page = self._active_pane.view.get_selected_page()
        child = page.get_child() if page is not None else None
        if not isinstance(child, (TableTab, DefinitionTab)):
            self._side_panel.set_definition("", "")
            return
        table = child.table
        cached = getattr(child, "side_ddl", None)
        if cached is not None:
            self._side_panel.set_definition(table, cached)
            return
        self._side_panel.set_definition("", "")
        profile = child.profile

        def done(ddl: str) -> None:
            child.side_ddl = ddl or ""
            current = self._active_pane.view.get_selected_page()
            if current is not None and current.get_child() is child:
                self._side_panel.set_definition(table, child.side_ddl)

        run_async(
            lambda: self.ensure_connector(profile).get_ddl(table),
            done,
            lambda _exc: None,  # tooltip-grade: fail silently
        )

    def _on_close_page(self, view, page: Adw.TabPage) -> bool:
        """A tab is being closed. A query console whose connection has
        an open transaction, or a running MCP server tab, is held back
        behind a confirmation dialog; forcing the close rolls the
        transaction back / stops the server. Either way the closed
        tab's entries leave the side panel's history scopes
        (panel_closed) but stay in the workspace-wide History tab."""
        child = page.get_child()
        if isinstance(child, QueryConsole):
            name = child.open_transaction_connection()
            if name:
                self._confirm_console_close(view, page, name)
                return True  # close_page_finish decides later
        if isinstance(child, McpServerTab) and child.running:
            self._confirm_mcp_close(view, page, child)
            return True  # close_page_finish decides later
        self._mark_panel_closed(page)
        return False  # let the default handler close the page

    def _confirm_mcp_close(
        self, view: Adw.TabView, page: Adw.TabPage, tab: McpServerTab
    ) -> None:
        dialog = Adw.AlertDialog(
            heading="MCP Server Running",
            body="This server is still running. Close the tab anyway "
            "and stop it?",
        )
        dialog.add_response("cancel", "Keep Open")
        dialog.add_response("stop", "Stop and Close")
        dialog.set_response_appearance(
            "stop", Adw.ResponseAppearance.DESTRUCTIVE
        )
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def respond(_dialog, response: str) -> None:
            force = response == "stop"
            if force:
                tab.stop_instance()
                self._mark_panel_closed(page)
            view.close_page_finish(page, force)

        dialog.connect("response", respond)
        dialog.present(self)

    def _confirm_console_close(
        self, view: Adw.TabView, page: Adw.TabPage, name: str
    ) -> None:
        dialog = Adw.AlertDialog(
            heading="Open Transaction",
            body=f"The connection “{name}” has an open transaction. "
            "Close the console anyway and roll it back?",
        )
        dialog.add_response("cancel", "Keep Open")
        dialog.add_response("rollback", "Roll Back and Close")
        dialog.set_response_appearance(
            "rollback", Adw.ResponseAppearance.DESTRUCTIVE
        )
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def respond(_dialog, response: str) -> None:
            force = response == "rollback"
            if force:
                self._rollback_connections([name])
                self._mark_panel_closed(page)
            view.close_page_finish(page, force)

        dialog.connect("response", respond)
        dialog.present(self)

    def _mark_panel_closed(self, page: Adw.TabPage) -> None:
        title = page.get_title()
        changed = False
        for entry in self.workspace.history:
            if entry.panel == title and not entry.panel_closed:
                entry.panel_closed = True
                changed = True
        if changed:
            self._side_panel.set_entries(self.workspace.history)
            if not self._restoring:
                self._save_state()

    def _on_pane_pressed(self, _gesture, _n, _x, _y, pane: _TabPane) -> None:
        if pane is not self._active_pane:
            self._set_active_pane(pane)

    def _split_current_tab(self, *_args) -> None:
        pane = self._active_pane
        page = pane.view.get_selected_page()
        if page is None:
            self.show_error("Nothing to split — open a tab first")
            return
        if pane.view.get_n_pages() == 1 and len(self._panes) == 1:
            self.show_error("Open a second tab to split the view")
            return
        if len(self._panes) >= 2:
            self.show_error("Split view is limited to 2 panes")
            return
        new_pane = self._add_pane()
        pane.view.transfer_page(page, new_pane.view, 0)
        self._set_active_pane(new_pane)

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

    # Transactions

    def transaction_active(self, name: str) -> bool:
        """Non-blocking peek: does the cached connector for `name` have
        an open transaction? in_transaction() only reads driver state,
        so it is safe on the main thread."""
        with self._connectors_lock:
            connector = self._connectors.get(name)
        try:
            return connector is not None and connector.in_transaction()
        except Exception:
            return False

    def _open_transactions(self) -> list[str]:
        """Names of all cached connections with an open transaction."""
        with self._connectors_lock:
            names = list(self._connectors)
        return [name for name in names if self.transaction_active(name)]

    def _rollback_connections(
        self, names: list[str], then: Callable[[], None] | None = None
    ) -> None:
        """Roll back the open transaction on each named connection (on
        a worker thread), then run `then` on the main loop."""
        def work():
            for name in names:
                with self._connectors_lock:
                    connector = self._connectors.get(name)
                if connector is not None:
                    connector.rollback()

        def done(_result):
            for pane in self._panes:
                for i in range(pane.view.get_n_pages()):
                    child = pane.view.get_nth_page(i).get_child()
                    if isinstance(child, QueryConsole):
                        child.refresh_transaction_badge()
            if then is not None:
                then()

        def failed(exc):
            self.show_error(f"Rollback failed: {exc}")
            if then is not None:
                then()

        run_async(work, done, failed)

    # Workspace persistence

    def _restore_tabs(self) -> None:
        self._restoring = True
        try:
            for tab in self.workspace.tabs:
                profile = self.workspace.find_connection(tab.connection)
                if tab.kind == "table" and tab.table:
                    if profile is not None:
                        self.open_table(profile, tab.table)
                elif tab.kind == "definition" and tab.table:
                    if profile is not None:
                        self.open_definition(profile, tab.table)
                elif tab.kind == "function" and tab.table:
                    if profile is not None:
                        self.open_function(profile, tab.table)
                elif tab.kind == "relations":
                    if profile is not None:
                        self.open_relation_graph(profile)
                elif tab.kind == "querybuilder":
                    if profile is not None:
                        self.open_query_builder(profile, tab.table)
                elif tab.kind == "query":
                    # Restore the console even if its connection is gone.
                    self.new_query(profile, sql=tab.sql)
                elif tab.kind == "cli":
                    # Restore the CLI session even if its connection is gone.
                    self.open_cli(profile)
            selected = self.workspace.selected_tab
            view = self._active_pane.view
            if 0 <= selected < view.get_n_pages():
                view.set_selected_page(view.get_nth_page(selected))
        finally:
            self._restoring = False
        self._update_placeholder()

    def _save_state(self) -> None:
        # Tabs are stored flat, pane order first: a split restores as a
        # single pane on reopen. Session-only tabs (tab_state() is
        # None, e.g. the history view) are not saved.
        selected = self._active_pane.view.get_selected_page()
        self.workspace.selected_tab = -1
        tabs = []
        for pane in self._panes:
            for i in range(pane.view.get_n_pages()):
                page = pane.view.get_nth_page(i)
                state = page.get_child().tab_state()
                if state is None:
                    continue
                if page is selected:
                    self.workspace.selected_tab = len(tabs)
                tabs.append(state)
        self.workspace.tabs = tabs
        try:
            self._store.save(self.workspace)
        except Exception as exc:
            self.show_error(f"Could not save workspace: {exc}")

    def _on_pages_changed(self, *_args) -> None:
        self._update_placeholder()
        # Deferred: pruning re-parents widgets, which must not happen
        # inside the TabView signal emission.
        GLib.idle_add(self._prune_empty_panes)
        if not self._restoring:
            self._save_state()

    def _running_mcp_tabs(self) -> list[McpServerTab]:
        tabs = []
        for pane in self._panes:
            for i in range(pane.view.get_n_pages()):
                child = pane.view.get_nth_page(i).get_child()
                if isinstance(child, McpServerTab) and child.running:
                    tabs.append(child)
        return tabs

    def _on_close_request(self, *_args) -> bool:
        self._save_state()
        if self._close_confirmed:
            return False
        # Keep the window and ask; a confirming response closes it for
        # real (the flag makes the second close-request pass through).
        # Open transactions are called out and rolled back on confirm.
        transactions = self._open_transactions()
        body = (
            f"Close “{self.workspace.name}”? Open tabs are saved "
            "and restored next time."
        )
        if transactions:
            body += (
                "\n\nOpen transaction(s) on "
                + ", ".join(f"“{name}”" for name in transactions)
                + " will be rolled back."
            )
        dialog = Adw.AlertDialog(heading="Close Workspace?", body=body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response(
            "close", "Roll Back and Close" if transactions else "Close"
        )
        dialog.set_response_appearance(
            "close", Adw.ResponseAppearance.DESTRUCTIVE
        )
        dialog.set_default_response("close" if not transactions else "cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._close_confirmed_response)
        dialog.present(self)
        return True

    def _close_confirmed_response(self, _dialog, response: str) -> None:
        if response != "close":
            return

        def finish() -> None:
            # MCP instances die with their window: no separate
            # confirmation (unlike per-tab close), just stop them now
            # that the close is actually going through.
            for tab in self._running_mcp_tabs():
                tab.stop_instance()
            self._close_confirmed = True
            self.close()

        transactions = self._open_transactions()
        if transactions:
            self._rollback_connections(transactions, then=finish)
        else:
            finish()

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

    def _drop_connector(self, name: str) -> None:
        """Forget a cached connector (edit/remove) and close it in the
        background; nothing else in the app currently closes connectors
        explicitly, but a connection the user just removed shouldn't
        keep a server-side session open behind them."""
        with self._connectors_lock:
            connector = self._connectors.pop(name, None)
        if connector is not None:
            run_async(connector.close, lambda _r: None, lambda _e: None)

    def _edit_connection(self, profile: ConnectionProfile) -> None:
        ConnectionDialog(
            on_save=lambda edited: self._connection_edited(profile, edited),
            profile=profile,
        ).present(self)

    def _connection_edited(
        self, profile: ConnectionProfile, edited: ConnectionProfile
    ) -> None:
        # Mutate the existing profile object in place (rather than
        # replacing it in workspace.connections) so tabs already open
        # on this connection keep a valid reference and just pick up
        # the new details next time they connect.
        old_name = profile.name
        edited.name = self.workspace.unique_connection_name(
            edited.name, exclude=profile
        )
        for f in fields(profile):
            setattr(profile, f.name, getattr(edited, f.name))
        self.workspace.sync_renamed_connection_secrets(old_name, profile)
        self._store.save(self.workspace)

        for i in range(self._connection_names.get_n_items()):
            if self._connection_names.get_string(i) == old_name:
                self._connection_names.splice(i, 1, [profile.name])
                break
        if self._last_connection == old_name:
            self._last_connection = profile.name
        self._drop_connector(old_name)
        # A fresh root row (rather than patching the old one in place)
        # so a changed kind/host also resets the cached schema tree,
        # not just the label.
        self._sidebar.remove_profile(old_name)
        self._sidebar.add_profile(profile)
        self._sidebar.expand_profile(profile.name)
        self._refresh_tab_identity()  # the colour may have changed too

    def _remove_connection(self, profile: ConnectionProfile) -> None:
        dialog = Adw.AlertDialog(
            heading=f"Remove “{profile.name}”?",
            body="This removes the connection from this workspace. Tabs "
            "already open on it are left as-is and will show an error "
            "next time they run.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("remove", "Remove")
        dialog.set_response_appearance(
            "remove", Adw.ResponseAppearance.DESTRUCTIVE
        )
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._remove_connection_response, profile)
        dialog.present(self)

    def _remove_connection_response(
        self, _dialog, response: str, profile: ConnectionProfile
    ) -> None:
        if response != "remove":
            return
        self.workspace.remove_connection(profile.name)
        self._store.save(self.workspace)
        for i in range(self._connection_names.get_n_items()):
            if self._connection_names.get_string(i) == profile.name:
                self._connection_names.splice(i, 1, [])
                break
        self._drop_connector(profile.name)
        self._sidebar.remove_profile(profile.name)

    def _focus_tab(self, key: tuple) -> bool:
        """Select the open tab with this tab_key, if any."""
        for pane in self._panes:
            for i in range(pane.view.get_n_pages()):
                page = pane.view.get_nth_page(i)
                if getattr(page.get_child(), "tab_key", None) == key:
                    pane.view.set_selected_page(page)
                    self._set_active_pane(pane)
                    return True
        return False

    def _append_tab(
        self, tab: Gtk.Widget, key: tuple, title: str, tooltip: str
    ) -> Adw.TabPage:
        tab.tab_key = key
        view = self._active_pane.view
        page = view.append(tab)
        page.set_title(title)
        page.set_tooltip(tooltip)
        self._apply_page_identity(page, _page_connection(tab))
        view.set_selected_page(page)
        return page

    def open_table(self, profile: ConnectionProfile, table: str) -> None:
        key = ("table", profile.name, table)
        if self._focus_tab(key):
            return
        tab = TableTab(
            profile,
            table,
            self.ensure_connector,
            self.show_error,
            on_aggregate=self.show_aggregate,
        )
        page = self._append_tab(
            tab,
            key,
            f"{profile.name} ▸ {table}",
            f"{table} on {profile.name} ({profile.kind})",
        )
        # Bound after the page exists so grid loads (select, filter,
        # sort, paging) land in history under the tab's panel name.
        tab.on_ran = lambda sql, ok: self._query_ran(
            page.get_title(), sql, profile.name, ok
        )

    def open_definition(self, profile: ConnectionProfile, table: str) -> None:
        key = ("definition", profile.name, table)
        if self._focus_tab(key):
            return
        tab = DefinitionTab(
            profile, table, self.ensure_connector, self.show_error
        )
        self._append_tab(
            tab,
            key,
            f"{table} · definition",
            f"Definition of {table} on {profile.name}",
        )

    def open_function(self, profile: ConnectionProfile, name: str) -> None:
        key = ("function", profile.name, name)
        if self._focus_tab(key):
            return
        tab = FunctionTab(
            profile, name, self.ensure_connector, self.show_error
        )
        self._append_tab(
            tab,
            key,
            f"{name} · function",
            f"Definition of {name} on {profile.name}",
        )

    def _new_query_builder(self, *_args) -> None:
        profile = self._default_query_profile()
        if profile is None:
            self.show_error("Add a connection first")
            return
        self.open_query_builder(profile)

    def open_query_builder(
        self, profile: ConnectionProfile, table: str = ""
    ) -> None:
        # Not deduplicated by tab_key: several builders on the same
        # connection are fine, like query consoles.
        tab = QueryBuilderTab(
            profile,
            self.ensure_connector,
            self.show_error,
            table=table,
            on_aggregate=self.show_aggregate,
            on_open_console=lambda p, sql: self.new_query(p, sql=sql),
        )
        page = self._append_tab(
            tab,
            ("querybuilder", profile.name, id(tab)),
            f"builder · {profile.name}",
            f"Query builder on {profile.name}",
        )
        tab.on_ran = lambda sql, ok: self._query_ran(
            page.get_title(), sql, profile.name, ok
        )

    # Create/drop DDL (sidebar context menus)

    def _drop_object(
        self, profile: ConnectionProfile, kind: str, name: str, table: str
    ) -> None:
        def executed(sql: str, ok: bool) -> None:
            self._query_ran(f"{profile.name} ▸ drop", sql, profile.name, ok)
            if ok:
                self._sidebar.reload_connection(profile.name)

        present_drop_dialog(
            self, profile, kind, name, table,
            self.ensure_connector, self.show_error, executed,
        )

    def _new_object(self, profile: ConnectionProfile, kind: str) -> None:
        if kind == "table":
            self.open_table_designer(profile)
            return

        # Everything else: a query console prefilled with the
        # dialect's commented CREATE skeleton.
        run_async(
            lambda: self.ensure_connector(profile).create_template(kind),
            lambda template: self.new_query(profile, sql=template),
            lambda exc: self.show_error(str(exc)),
        )

    def open_table_designer(self, profile: ConnectionProfile) -> None:
        # Not deduplicated by tab_key: several designers on the same
        # connection are fine, like query consoles.
        tab = TableDesignerTab(
            profile,
            self.ensure_connector,
            self.show_error,
            on_created=lambda table: self._table_created(profile, table),
        )
        page = self._append_tab(
            tab,
            ("designer", profile.name, id(tab)),
            f"new table · {profile.name}",
            f"Table designer on {profile.name}",
        )
        tab.on_ran = lambda sql, ok: self._query_ran(
            page.get_title(), sql, profile.name, ok
        )

    def _table_created(self, profile: ConnectionProfile, table: str) -> None:
        self._sidebar.reload_connection(profile.name)
        self.open_table(profile, table)

    def open_mcp_server(
        self, profile: ConnectionProfile | None = None
    ) -> None:
        # Not deduplicated: several instances (different ports, maybe
        # different connection sets) can run side by side.
        tab = McpServerTab(
            self.workspace.name,
            self.workspace.connections,
            self.show_error,
            initial_profile=profile,
        )
        self._append_tab(
            tab,
            ("mcp", id(tab)),
            "MCP server",
            "Read-only MCP server instance",
        )

    def open_relation_graph(self, profile: ConnectionProfile) -> None:
        key = ("relations", profile.name)
        if self._focus_tab(key):
            return
        tab = RelationGraphTab(profile, self.ensure_connector, self.show_error)
        self._append_tab(
            tab,
            key,
            f"{profile.name} ▸ relations",
            f"Table relations of {profile.name}",
        )

    def open_history_tab(self) -> None:
        key = ("history",)
        if self._focus_tab(key):
            return
        self._append_tab(
            _HistoryTab(self.workspace.history),
            key,
            "History",
            f"Query history of {self.workspace.name}",
        )

    def new_query(
        self, profile: ConnectionProfile | None = None, sql: str = ""
    ) -> Adw.TabPage:
        console = QueryConsole(
            self._connection_names,
            self.workspace.find_connection,
            self.ensure_connector,
            sql=sql,
            connection=profile.name if profile is not None else "",
            on_aggregate=self.show_aggregate,
            transaction_active=self.transaction_active,
            placeholders=self.workspace.placeholders,
        )
        view = self._active_pane.view
        page = view.append(console)
        # Bound after the page exists so history entries carry the tab
        # (panel) title the query ran in.
        console.on_ran = lambda sql, conn, ok: self._query_ran(
            page.get_title(), sql, conn, ok
        )

        def set_title(name: str) -> None:
            page.set_title(f"query · {name}" if name else "query")
            page.set_tooltip(
                f"Query console on {name}" if name else "Query console"
            )
            self._apply_page_identity(page, name)
            if not self._restoring:
                self._save_state()
                self._update_active_panel()  # Info follows the dropdown

        console.on_connection_changed = set_title
        set_title(console.selected_connection())
        view.set_selected_page(page)
        return page

    def _new_cli_console(self, *_args) -> None:
        self.open_cli(self._default_query_profile())

    def open_cli(
        self, profile: ConnectionProfile | None = None
    ) -> Adw.TabPage:
        console = CliConsole(
            self._connection_names,
            self.workspace.find_connection,
            self.ensure_connector,
            connection=profile.name if profile is not None else "",
        )
        view = self._active_pane.view
        page = view.append(console)

        def set_title(name: str) -> None:
            page.set_title(f"cli · {name}" if name else "cli")
            page.set_tooltip(
                f"CLI client on {name}" if name else "CLI client"
            )
            self._apply_page_identity(page, name)
            if not self._restoring:
                self._save_state()
                self._update_active_panel()  # Info follows the dropdown

        console.on_connection_changed = set_title
        set_title(console.selected_connection())
        view.set_selected_page(page)
        return page

    def _default_query_profile(self) -> ConnectionProfile | None:
        """Connection preselected in a new console: the current tab's,
        else the last-used, else the workspace's first (else none)."""
        page = self._active_pane.view.get_selected_page()
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

    def _query_ran(
        self, panel: str, sql: str, connection: str, ok: bool
    ) -> None:
        self._last_connection = connection
        self.workspace.add_history(HistoryEntry(
            sql=sql,
            connection=connection,
            timestamp=datetime.now().isoformat(timespec="seconds"),
            ok=ok,
            panel=panel,
        ))
        self._save_state()
        self._side_panel.set_entries(self.workspace.history)
        self._refresh_history_tab()

    def _clear_history(self) -> None:
        self.workspace.history.clear()
        self._save_state()
        self._side_panel.set_entries([])
        self._refresh_history_tab()

    def _refresh_history_tab(self) -> None:
        for pane in self._panes:
            for i in range(pane.view.get_n_pages()):
                child = pane.view.get_nth_page(i).get_child()
                if isinstance(child, _HistoryTab):
                    child.set_entries(self.workspace.history)

    def _history_activated(self, entry: HistoryEntry) -> None:
        # Always a fresh console: loading into the current one would
        # silently overwrite whatever is being written there.
        self.new_query(
            self.workspace.find_connection(entry.connection),
            sql=entry.sql,
        )

    def _update_placeholder(self, *_args) -> None:
        has_tabs = any(p.view.get_n_pages() > 0 for p in self._panes)
        self._stack.set_visible_child_name(
            "tabs" if has_tabs else "placeholder"
        )
