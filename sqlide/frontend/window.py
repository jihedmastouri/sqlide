"""Main window of one workspace: fixed sidebar + tabbed content.

Opened by the application for a chosen workspace. The sidebar lists
only this workspace's connections; other workspaces are reached
through its menu button (Workspaces…). This window owns its cache
of open connectors. ensure_connector() is the blocking accessor handed
to child widgets; they must only call it from run_async worker threads.
Disconnect on a connection's sidebar menu closes one again (asking
first if a query is still running): the tree row folds up and forgets
its schema, while tabs on it stay open behind a banner offering
Reconnect — the next call through ensure_connector would reopen the
session anyway, so nothing in them breaks.

Close all related tabs, next to it, goes the other way: every tab on
one connection closes at once, after a single confirmation listing the
ones holding work that was never written (typed SQL, pending grid
edits) with Save, Discard or Cancel.

Open tabs are part of the workspace: they are restored on open and
saved back to the workspace file whenever they change and when the
window closes (which also captures query-console SQL and the selected
tab). When no tabs are open the content area shows a status message.

Layout: the connections sidebar is the start child of the outermost
Gtk.Paned and runs the full window height with its own header bar
(Workspaces, settings menu) over a row that offers Add Connection, a
search icon and Refresh — clicking the search icon puts the table
filter where the first two were. It is not collapsible or closable,
but its inner edge is a drag handle like the side panel's: the width
is held between settings.SIDEBAR_MIN_WIDTH and SIDEBAR_MAX_WIDTH,
double-clicking the handle resets it to the default, and whatever it
is left at is written to settings.toml, so it survives a restart. To
its right, the content area has its own
header bar and the workspace's identity stripe under it, then a
Gtk.Paned whose draggable right-hand child is the side panel (hidden by
default) wraps the tab area. A persistent status bar
closes the content area at the bottom: the active tab's connection,
its state, running jobs and transient messages, refreshed on every tab
switch, run and connection change so it can never show a connection
that is no longer open.
The tab area holds one or more panes (each an Adw.TabBar over an
Adw.TabView) side by side in nested Gtk.Paned splitters: the Split
button moves the current tab into a new pane (two panes at most,
sized evenly), so e.g. two tables can be shown next to each other.
New tabs open in the last-clicked pane; a pane whose last tab is
closed or moved away is removed. Right-clicking a tab closes it, the
others, the ones to its right, or all of them across every pane; Close
All Tabs is on the sidebar menu and on ctrl+shift+w as well, and each
close still goes through the guards below (an open transaction or a
running MCP server asks first).

Any tab can also be popped out into a window of its own — "Move to New
Window" in its menu, dragging it off the tab bar, or holding Shift
while opening it from anywhere at all — and moved (or dragged) back. A
pop-out is a pane like any other, in a _PopoutWindow: this window still
owns the tabs in it, so history, saved state, tab colours and the close
guards all keep working, it wears the same workspace stripe, and it
closes itself when its last tab leaves. MCP servers are the one tab
that is always a window: a solo pop-out, with no tab bar, so a running
server can neither be buried behind another tab nor dragged into the
main window. Pop-outs are a session-level layout: on reopen every tab
is restored into the main window, exactly as a split is.
The window owns the shared
Gtk.StringList of connection names that every query console's dropdown
observes, records each console run into the workspace history, and
loads history entries back into a console when activated.
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, fields
from datetime import datetime
from typing import Callable

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from sqlide.frontend.util import (
    describe,
    run_async,
    sidebar_menu_button,
    workspaces_button,
)

from sqlide.backend import identity, schemas
from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.db import objects, registry
from sqlide.backend.db.base import Connector, ConnectorError, FilterCondition
from sqlide.backend.db.metadata import NodeRef
from sqlide.backend import settings as settings_backend
from sqlide.backend.workspaces import HistoryEntry, Workspace
from sqlide.frontend.cli_console import CliConsole
from sqlide.frontend.connection_dialog import ConnectionDialog
from sqlide.frontend.data_grid import ResultGrid, TableTab
from sqlide.frontend import feedback
from sqlide.frontend.definition_tab import DefinitionTab, FunctionTab
from sqlide.frontend.indexes_tab import IndexesTab
from sqlide.frontend import identity as identity_ui
from sqlide.frontend.drop_dialog import present_drop_dialog
from sqlide.frontend.backups_tab import BackupsTab
from sqlide.frontend.mcp_tab import McpServerTab
from sqlide.frontend.object_info import ObjectInfoTab, tab_key
from sqlide.frontend.query_builder import QueryBuilderTab
from sqlide.frontend.query_console import QueryConsole
from sqlide.frontend.relation_graph import RelationGraphTab
from sqlide.frontend.side_panel import SidePanel
from sqlide.frontend import tree_search
from sqlide.frontend.sidebar import Sidebar
from sqlide.frontend.status_bar import StatusBar
from sqlide.frontend.table_designer import TableDesignerTab
from sqlide.frontend.users_tab import UsersTab
from sqlide.frontend import transfer


# Default and floor width of the right side panel, in pixels. The
# floor is what a DDL listing needs before it starts wrapping every
# line; the default leaves the tab area the larger half on a 1100px
# window, which is the size this window opens at.
# How long a sidebar drag must be still before its width is written
# back to settings.toml, and how many pixels either side of the drag
# handle still count as "on the handle" for the double-click reset
# (GTK4 gives no handle width to ask for, and the divider is a few
# pixels wide however it is themed).
_SIDEBAR_WIDTH_SAVE_DELAY = 400
_HANDLE_GRAB = 8
_SIDE_PANEL_WIDTH = 340
_SIDE_PANEL_MIN_WIDTH = 260

# How long the sidebar's search box waits for the typing to stop before
# refiltering the tree: long enough that a fast typist rebuilds it once,
# short enough to feel immediate.
_SEARCH_DEBOUNCE_MS = 180


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


def _tab_menu(popped_out: bool = False) -> Gio.Menu:
    """Right-click menu of a tab. The bulk-close items are the only way
    to clear a workspace that has collected twenty tabs without closing
    them one by one; they are also on the main menu, so they can be
    found without knowing that tabs have a menu at all. The move items
    are the discoverable half of pop-out: dragging a tab off its bar
    does the same thing, but nothing on screen says so."""
    menu = Gio.Menu()
    menu.append("Close Tab", "win.close-tab")
    section = Gio.Menu()
    section.append("Close Other Tabs", "win.close-other-tabs")
    section.append("Close Tabs to the Right", "win.close-tabs-right")
    section.append("Close All Tabs", "win.close-all-tabs")
    menu.append_section(None, section)
    move = Gio.Menu()
    move.append("Move to New Window", "win.move-to-window")
    if popped_out:
        move.append("Move Back to Main Window", "win.move-to-main")
    menu.append_section(None, move)
    return menu


class _TabPane(Gtk.Box):
    """One tab pane: its own tab bar over an Adw.TabView. The window
    shows a single pane normally and several side by side after Split.

    tabbed=False leaves the bar out: the pane holds exactly one tab and
    never says so (the solo pop-out an MCP server lives in). Without a
    bar there is nothing to drag a tab out of, and nothing for a tab
    from another window to be dropped on, which is the point."""

    def __init__(self, tabbed: bool = True) -> None:
        super().__init__(
            orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True
        )
        self.view = Adw.TabView(vexpand=True)
        # expand-tabs off: tabs take the width of their titles instead
        # of stretching to fill the bar, so the names line up from the
        # left edge rather than floating in the middle of empty tabs.
        self.bar = Adw.TabBar(view=self.view, expand_tabs=False)
        self.tabbed = tabbed
        if tabbed:
            self.append(self.bar)
        self.append(self.view)


class _PopoutWindow(Adw.ApplicationWindow):
    """One tab pane in a top-level window of its own. Any kind of tab
    can live here: tabs arrive by the tab menu's Move to New Window, by
    being dragged off a tab bar, or by being opened with Shift held,
    and can be dragged (or moved) back. solo=True is the exception —
    one tab, no tab bar, nothing to drag in or out (see MCP below).

    The main window still owns them — the pane is wired to the same
    handlers, and the tabs stay in its history, its saved state and its
    close guards. The window closes itself once its last tab leaves."""

    def __init__(
        self, main: MainWindow, solo: bool = False, **kwargs
    ) -> None:
        super().__init__(application=main.get_application(), **kwargs)
        self.main = main
        self.solo = solo
        self.set_default_size(480 if solo else 900, 560 if solo else 620)

        self.pane = main.build_pane(popped_out=True, solo=solo)
        header = Adw.HeaderBar()
        toolbar_view = Adw.ToolbarView()
        toolbar_view.add_top_bar(header)
        # The workspace stripe, same colour as the window this was
        # opened from: a window of its own must still say which
        # workspace it belongs to, and the title alone gets lost in a
        # row of taskbar entries.
        self.stripe = identity_ui.stripe(main.workspace.color)
        toolbar_view.add_top_bar(self.stripe)
        toolbar_view.set_content(self.pane)
        self._toasts = Adw.ToastOverlay(child=toolbar_view)
        self.set_content(self._toasts)
        self._retitle()
        # The launcher can recolour a workspace while this is open.
        self.connect(
            "notify::is-active", lambda *_: main.refresh_workspace_identity()
        )

        main.install_tab_actions(self, self.pane)
        self.pane.view.connect(
            "notify::selected-page", lambda *_: self._retitle()
        )
        self.connect("close-request", self._on_close_request)

    @property
    def toasts(self) -> Adw.ToastOverlay:
        return self._toasts

    def _retitle(self) -> None:
        page = self.pane.view.get_selected_page()
        title = page.get_title() if page is not None else ""
        name = self.main.workspace.name
        self.set_title(f"{title} — {name}" if title else name)

    def _on_close_request(self, *_args) -> bool:
        """Close the tabs, not the window: each one still goes through
        its own guard (an open transaction, a running MCP server). The
        window is destroyed once the last page is actually gone, so a
        cancelled guard leaves the window standing."""
        view = self.pane.view
        pages = [view.get_nth_page(i) for i in range(view.get_n_pages())]
        if not pages:
            return False
        for page in pages:
            view.close_page(page)
        return True


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, workspace: Workspace, **kwargs) -> None:
        super().__init__(**kwargs)
        self.workspace = workspace
        self.set_title(f"sqlide — {workspace.name}")
        self.set_default_size(1100, 700)

        self._store = self.get_application().workspace_store
        self._connectors: dict[str, Connector] = {}
        self._connectors_lock = threading.Lock()
        # Tabs left standing on a connection the user disconnected,
        # and the banner each one grew (feedback.set_disconnected).
        self._disconnect_banners: dict[Gtk.Widget, Adw.Banner] = {}
        # Set to a connection's name while its tabs are being closed in
        # one go (Close all related tabs), so the per-tab transaction
        # confirmation stays out of the way: the user was asked once,
        # for all of them, already.
        self._closing_connection = ""
        self._restoring = False
        self._last_connection = ""
        self._connection_names = Gtk.StringList.new(
            [p.name for p in workspace.connections]
        )

        # A Gtk.Paned, not an Adw.OverlaySplitView: the sidebar is
        # never collapsed here, and a Paned is what gives its inner
        # edge a drag handle. The sidebar keeps its size when the
        # window is resized (the content area absorbs it) and can't be
        # dragged narrower than its size request.
        self._split = Gtk.Paned(
            orientation=Gtk.Orientation.HORIZONTAL,
            resize_start_child=False,
            shrink_start_child=False,
            resize_end_child=True,
            shrink_end_child=True,
        )
        self._split.set_position(
            settings_backend.clamp_sidebar_width(
                settings_backend.store.settings.sidebar_width
            )
        )
        self._split.connect("notify::position", self._sidebar_resized)
        self._sidebar_width_source = 0
        # Double-clicking the handle puts the default width back. The
        # gesture sits on the Paned itself and only answers clicks that
        # land on the handle, so a double click inside either child is
        # left alone.
        reset = Gtk.GestureClick()
        reset.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        reset.connect("pressed", self._handle_clicked)
        self._split.add_controller(reset)

        # Sidebar. It is not collapsible and spans the full window
        # height, with its own header at the very top — the window's
        # window-controls live here, not on a banner above it.
        sidebar_header = Adw.HeaderBar()
        sidebar_header.set_show_end_title_buttons(False)
        sidebar_header.set_title_widget(Gtk.Label(label="Connections"))
        # Two icons, two jobs: leave this workspace, or change how the
        # app behaves. Everything else the sidebar does is a row below.
        sidebar_header.pack_start(workspaces_button())
        sidebar_header.pack_end(sidebar_menu_button())

        # Sidebar search state: the chosen object-kind scope is kept
        # for the session (empty = All), and the pending debounce.
        self._sidebar_scopes: frozenset[str] = frozenset()
        self._sidebar_search: Gtk.SearchEntry | None = None
        self._sidebar_search_source = 0
        self._sidebar_filter_label = None

        self._sidebar = Sidebar(
            ensure_connector=self.ensure_connector,
            on_open_table=self.open_table,
            on_open_object=self.open_object,
            on_open_section=self.open_table_section,
            on_new_query=self.new_query,
            on_open_cli=self.open_cli,
            on_open_definition=self.open_definition,
            on_open_function=self.open_function,
            on_relation_graph=self.open_relation_graph,
            on_view_indexes=self.open_indexes,
            on_manage_users=self.open_users,
            on_query_builder=self.open_query_builder,
            on_drop_object=self._drop_object,
            on_new_object=self._new_object,
            on_mcp_server=self.open_mcp_server,
            on_open_schema=self._open_schema,
            on_edit_connection=self._edit_connection,
            on_disconnect=self._disconnect_connection,
            on_close_tabs=self._close_connection_tabs,
            count_tabs=self.count_connection_tabs,
            on_remove_connection=self._remove_connection,
            on_add_connection=self._add_connection,
            show_error=self.show_error,
        )
        sidebar_view = Adw.ToolbarView()
        sidebar_view.add_top_bar(sidebar_header)
        sidebar_view.add_top_bar(self._sidebar_actions())
        sidebar_view.set_content(self._sidebar)
        sidebar_view.set_size_request(settings_backend.SIDEBAR_MIN_WIDTH, -1)
        self._split.set_start_child(sidebar_view)

        # Content: one or more tab panes (each with its own tab bar) in
        # nested Paned splitters, or a placeholder when nothing is open.
        self._panes: list[_TabPane] = []
        self._syncing_split = False
        self._popouts: list[_PopoutWindow] = []
        self._panes_root = Gtk.Box(hexpand=True, vexpand=True)
        self._active_pane = self._add_pane()

        placeholder = Adw.StatusPage(
            icon_name="folder-open-symbolic",
            title="Nothing Open",
            description="Pick a table from the sidebar, or start a query "
            "console.",
            child=self._placeholder_actions(),
        )
        self._stack = Gtk.Stack()
        self._stack.add_named(placeholder, "placeholder")
        self._stack.add_named(self._panes_root, "tabs")

        content_header = Adw.HeaderBar()
        content_header.set_show_start_title_buttons(False)
        # One "New" menu instead of a row of icons nobody can decode: a
        # pen, a cog and a network arrow said nothing about query
        # consoles, CLI clients and MCP servers. Words do, and the
        # header bar's left corner stops being a puzzle.
        new_menu = Gio.Menu()
        tabs = Gio.Menu()
        tabs.append("Query Console", "win.new-query")
        tabs.append("CLI Client", "win.new-cli")
        tabs.append("Query Builder", "win.new-builder")
        new_menu.append_section(None, tabs)
        servers = Gio.Menu()
        servers.append("MCP Server", "win.new-mcp")
        new_menu.append_section(None, servers)
        new_button = Adw.SplitButton(
            label="New", menu_model=new_menu, tooltip_text="New query console"
        )
        new_button.connect(
            "clicked", lambda *_: self.new_query(self._default_query_profile())
        )
        # Split View sits at the very left of the content header, hard
        # against the connections sidebar, because it is a layout
        # control like the sidebar itself — not a thing you make. Its
        # old home in the menu meant nobody found it.
        self._split_button = Gtk.ToggleButton(
            icon_name="view-dual-symbolic"
        )
        self._split_button.add_css_class("flat")
        describe(self._split_button, "Split View")
        self._split_button.connect("toggled", self._on_split_toggled)
        content_header.pack_start(self._split_button)
        content_header.pack_start(new_button)

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
        # A Gtk.Paned rather than an Adw.OverlaySplitView, which has no
        # user-resizable width: a DDL or an aggregate is often wider
        # than any fixed default, so the divider is draggable. The panel
        # is hidden, not removed, when off, so its width survives being
        # toggled; the tab area is the child that absorbs the resize.
        self._history_split = Gtk.Paned(
            orientation=Gtk.Orientation.HORIZONTAL,
            resize_start_child=True,
            shrink_start_child=False,
            resize_end_child=False,
            shrink_end_child=False,
        )
        self._side_panel.set_size_request(_SIDE_PANEL_MIN_WIDTH, -1)
        self._side_panel.set_visible(False)
        self._history_split.set_start_child(self._stack)
        self._history_split.set_end_child(self._side_panel)

        self._history_toggle = Gtk.ToggleButton(
            icon_name="sidebar-show-right-symbolic"
        )
        self._history_toggle.set_tooltip_text("Toggle side panel")
        self._history_toggle.connect(
            "toggled",
            lambda button: self._set_side_panel_shown(button.get_active()),
        )
        content_header.pack_end(self._history_toggle)

        # The content header and the workspace's identity stripe span
        # only the content area, to the right of the sidebar — the
        # sidebar keeps its own header and runs the full window height.
        # The window title carries the workspace name, so the stripe is
        # never the only cue.
        top_view = Adw.ToolbarView()
        top_view.add_top_bar(content_header)
        self._stripe = identity_ui.stripe(workspace.color)
        top_view.add_top_bar(self._stripe)
        self.refresh_workspace_identity()
        top_view.set_content(self._history_split)

        # Persistent status bar: where the active tab's connection,
        # state and messages live, instead of a widget per tab.
        self._status_bar = StatusBar(on_connect=self._connect_now)
        top_view.add_bottom_bar(self._status_bar)
        # An open transaction is timed from when the window first sees
        # it, and the elapsed text is refreshed on a slow tick.
        self._transaction_since: dict[str, float] = {}
        GLib.timeout_add_seconds(20, self._status_tick)

        self._split.set_end_child(top_view)

        # Tab overview: zoomed-out grid of tab thumbnails. It must wrap
        # the widget tree that contains the TabView so open tabs stay
        # visible (scaled down) while the overview is shown.
        self._overview = Adw.TabOverview(
            view=self._active_pane.view,
            enable_new_tab=True,
            child=self._split,
        )
        self._overview.connect("create-tab", self._create_tab)

        self._toasts = Adw.ToastOverlay(child=self._overview)
        self.set_content(self._toasts)
        self._close_confirmed = False
        self.connect("close-request", self._on_close_request)

        # Tab the context menu was opened on; None means "the selected
        # one" (the menu bar and the keyboard).
        self._menu_tab_page: Adw.TabPage | None = None
        for name, callback in (
            ("history", lambda *_: self.open_history_tab()),
            ("backups", lambda *_: self.open_backups()),
            ("new-query", lambda *_: self.new_query(
                self._default_query_profile()
            )),
            ("new-cli", self._new_cli_console),
            ("new-builder", self._new_query_builder),
            ("new-mcp", lambda *_: self.open_mcp_server()),
            ("refresh-schema", lambda *_: self._sidebar.reload_all()),
            ("split-view", self._toggle_split),
            ("export-workspace", self._export_workspace),
            ("export-connections", self._export_connections),
            ("import-connections", self._import_connections),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)
        # The tab menu's own actions, installed here and again on every
        # pop-out window — its menu says "win." too, and a pop-out has
        # an action map of its own.
        self.install_tab_actions(self)

        # The launcher can recolour a workspace while its window is
        # open, so the stripe is refreshed whenever the window comes
        # back to the front.
        self.connect(
            "notify::is-active", lambda *_: self.refresh_workspace_identity()
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

    # Sidebar width

    def _sidebar_resized(self, *_args) -> None:
        """Keep a drag inside the allowed range and remember where it
        stopped. The write is debounced: a drag emits a position change
        per frame, and settings.toml is not a thing to rewrite sixty
        times a second."""
        position = self._split.get_position()
        clamped = settings_backend.clamp_sidebar_width(position)
        if clamped != position:
            self._split.set_position(clamped)
            return  # the set re-enters here with the clamped value
        if self._sidebar_width_source:
            GLib.source_remove(self._sidebar_width_source)
        self._sidebar_width_source = GLib.timeout_add(
            _SIDEBAR_WIDTH_SAVE_DELAY, self._save_sidebar_width
        )

    def _save_sidebar_width(self) -> bool:
        self._sidebar_width_source = 0
        width = settings_backend.clamp_sidebar_width(
            self._split.get_position()
        )
        if width != settings_backend.store.settings.sidebar_width:
            settings_backend.store.update(sidebar_width=width)
        return GLib.SOURCE_REMOVE

    def _handle_clicked(self, gesture, n_press: int, x: float, _y: float):
        """Double click on the divider — and only on the divider —
        restores the default width."""
        if n_press != 2:
            return
        position = self._split.get_position()
        if not (
            position - _HANDLE_GRAB <= x <= position + _HANDLE_GRAB
        ):
            return
        self._split.set_position(settings_backend.DEFAULT_SIDEBAR_WIDTH)
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    # Side panel

    def _set_side_panel_shown(self, shown: bool) -> None:
        """Show or hide the right side panel, keeping the toggle button
        in step (it is also driven from code, e.g. by the grid's
        Aggregate item). The first time it opens, the divider is placed
        at the panel's default width; after that whatever the user
        dragged it to is left alone."""
        if self._history_toggle.get_active() != shown:
            self._history_toggle.set_active(shown)
            return  # the toggle re-enters here with the new state
        if shown and not self._side_panel.get_visible():
            width = self._history_split.get_width()
            if width:
                self._history_split.set_position(
                    max(0, width - _SIDE_PANEL_WIDTH)
                )
        self._side_panel.set_visible(shown)

    # Sidebar toolbar

    def _sidebar_actions(self) -> Gtk.Widget:
        """The row under the sidebar header: "Add Connection", a search
        icon and Refresh — until the search icon is clicked, when the
        whole row becomes the search row: the entry, a Filter menu that
        scopes the hunt by object kind, and Exit. A search box that is
        always there costs a row of the sidebar to a control most
        sessions never touch, so it is summoned instead, and while it is
        up it gets the row to itself rather than fighting the buttons
        for it.

        Exit, Escape, or clearing the entry and leaving it, puts the
        buttons back, drops the filter and restores the tree's previous
        expansion; the sidebar is never left silently filtered by a box
        that is no longer visible. The chosen scope outlives one search
        and is kept for the session."""
        stack = Gtk.Stack(
            transition_type=Gtk.StackTransitionType.CROSSFADE,
        )

        search = Gtk.SearchEntry(placeholder_text="Find objects…", hexpand=True)
        search.set_tooltip_text(
            "Find objects by name in loaded connections"
        )
        # Typing re-filters the whole tree, so the keystrokes are
        # collected first: on a big schema every letter would otherwise
        # rebuild it.
        search.connect(
            "search-changed", lambda entry: self._queue_sidebar_search()
        )
        self._sidebar_search = search

        filter_button = Gtk.MenuButton(
            child=Adw.ButtonContent(
                icon_name="view-list-symbolic",
                label=tree_search.scope_label(self._sidebar_scopes),
            ),
            css_classes=["flat"],
            popover=self._sidebar_scope_popover(),
        )
        describe(filter_button, "Filter search by object kind")
        self._sidebar_filter_label = filter_button.get_child()

        def close_search(*_args) -> None:
            search.set_text("")
            self._cancel_sidebar_search()
            self._sidebar.clear_filter()
            stack.set_visible_child_name("actions")

        self._close_sidebar_search = close_search
        search.connect("stop-search", close_search)

        exit_search = Gtk.Button(icon_name="window-close-symbolic")
        exit_search.add_css_class("flat")
        describe(exit_search, "Exit search")
        exit_search.connect("clicked", close_search)

        # Escape reaches here even when the focus has moved on to the
        # Filter menu or the tree.
        escape = Gtk.EventControllerKey()

        def on_key(_controller, keyval, _code, _state) -> bool:
            if keyval == Gdk.KEY_Escape and (
                stack.get_visible_child_name() == "search"
            ):
                close_search()
                return True
            return False

        escape.connect("key-pressed", on_key)

        search_row = Gtk.Box(spacing=6)
        search_row.append(search)
        search_row.append(filter_button)
        search_row.append(exit_search)

        actions = Gtk.Box(spacing=6)
        add = Gtk.Button(
            child=Adw.ButtonContent(
                icon_name="list-add-symbolic", label="Add Connection"
            ),
            hexpand=True,
            css_classes=["flat"],
        )
        add.connect("clicked", self._add_connection)
        actions.append(add)

        open_search = Gtk.Button(icon_name="system-search-symbolic")
        open_search.add_css_class("flat")
        describe(open_search, "Search objects")

        def show_search(*_args) -> None:
            stack.set_visible_child_name("search")
            search.grab_focus()

        open_search.connect("clicked", show_search)
        actions.append(open_search)

        # Refresh rides in the button half of the row: reloading is
        # what you do when a search comes up empty because the tree is
        # stale, but during a search the row belongs to the search.
        refresh = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh.add_css_class("flat")
        describe(refresh, "Refresh schemas")
        refresh.connect("clicked", lambda *_: self._sidebar.reload_all())
        actions.append(refresh)

        stack.add_named(actions, "actions")
        stack.add_named(search_row, "search")
        stack.set_visible_child_name("actions")
        stack.set_hexpand(True)

        row = Gtk.Box(
            spacing=6,
            margin_top=6,
            margin_bottom=6,
            margin_start=6,
            margin_end=6,
        )
        row.append(stack)
        row.add_controller(escape)
        return row

    def _sidebar_scope_popover(self) -> Gtk.Popover:
        """The Filter menu: "All" plus a check button per object kind.
        Ticking any kind unticks All, and unticking the last one falls
        back to All — an empty scope would search nothing, which is
        never what the user meant."""
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=6,
            margin_top=6,
            margin_bottom=6,
            margin_start=6,
            margin_end=6,
        )
        buttons: dict[str, Gtk.CheckButton] = {}
        all_button = Gtk.CheckButton(label="All")
        all_button.set_active(not self._sidebar_scopes)
        box.append(all_button)
        box.append(Gtk.Separator())

        updating = False

        def apply() -> None:
            nonlocal updating
            chosen = frozenset(
                key for key, button in buttons.items() if button.get_active()
            )
            self._sidebar_scopes = chosen
            updating = True
            all_button.set_active(not chosen)
            updating = False
            if self._sidebar_filter_label is not None:
                self._sidebar_filter_label.set_label(
                    tree_search.scope_label(chosen)
                )
            self._run_sidebar_search()

        def toggled(_button) -> None:
            if not updating:
                apply()

        def all_toggled(_button) -> None:
            nonlocal updating
            if updating or not all_button.get_active():
                return
            updating = True
            for button in buttons.values():
                button.set_active(False)
            updating = False
            apply()

        for key, label, _kinds in tree_search.SCOPES:
            button = Gtk.CheckButton(label=label)
            button.set_active(key in self._sidebar_scopes)
            button.connect("toggled", toggled)
            buttons[key] = button
            box.append(button)
        all_button.connect("toggled", all_toggled)
        return Gtk.Popover(child=box)

    # Sidebar search, debounced

    def _queue_sidebar_search(self) -> None:
        self._cancel_sidebar_search()
        self._sidebar_search_source = GLib.timeout_add(
            _SEARCH_DEBOUNCE_MS, self._sidebar_search_elapsed
        )

    def _sidebar_search_elapsed(self) -> bool:
        self._sidebar_search_source = 0
        self._run_sidebar_search()
        return GLib.SOURCE_REMOVE

    def _cancel_sidebar_search(self) -> None:
        if self._sidebar_search_source:
            GLib.source_remove(self._sidebar_search_source)
            self._sidebar_search_source = 0

    def _run_sidebar_search(self) -> None:
        if self._sidebar_search is None:
            return
        self._sidebar.set_filter(
            self._sidebar_search.get_text(), self._sidebar_scopes
        )

    # Empty states

    def _placeholder_actions(self) -> Gtk.Widget:
        """The empty content area teaches the next step: with no
        connections there is only one useful action, so that is the
        only one offered."""
        box = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        if self.workspace.connections:
            query = Gtk.Button(label="New Query Console")
            query.add_css_class("suggested-action")
            query.add_css_class("pill")
            query.connect(
                "clicked", lambda *_: self.new_query(self._default_query_profile())
            )
            box.append(query)
        else:
            add = Gtk.Button(label="Add Connection")
            add.add_css_class("suggested-action")
            add.add_css_class("pill")
            add.connect("clicked", self._add_connection)
            box.append(add)
        return box

    def _refresh_placeholder_actions(self) -> None:
        """The first connection changes what the empty state offers."""
        placeholder = self._stack.get_child_by_name("placeholder")
        placeholder.set_child(self._placeholder_actions())

    # Identity (colour + environment)

    def refresh_workspace_identity(self) -> None:
        """The workspace stripe, on this window and on every pop-out:
        a tab in a window of its own still wears the colour of the
        workspace it came from."""
        color = identity.COLOR_LABELS[
            identity.normalize_color(self.workspace.color)
        ]
        tooltip = f"Workspace “{self.workspace.name}” · colour {color}"
        for stripe in [self._stripe] + [w.stripe for w in self._popouts]:
            identity_ui.set_color(stripe, self.workspace.color)
            stripe.set_tooltip_text(tooltip)
        self.set_title(f"sqlide — {self.workspace.name}")

    # Status bar

    def _status_tick(self) -> bool:
        """Keep the transaction timer honest while nothing else
        happens. Stops with the window."""
        if self.get_application() is None:
            return GLib.SOURCE_REMOVE
        self.refresh_status_bar()
        return GLib.SOURCE_CONTINUE

    def refresh_status_bar(self) -> None:
        """Re-read the active tab's state into the status bar. Called
        on every tab switch, run, connection change and slow tick, so
        the identity zone can never go stale."""
        page = self._active_pane.view.get_selected_page()
        child = page.get_child() if page is not None else None
        name = _page_connection(child)
        profile = self.workspace.find_connection(name)
        connected = bool(name) and self.is_connected(name)
        self._status_bar.set_identity(
            name,
            database=self._tab_database(child, profile),
            color=profile.color if profile is not None else identity.NONE,
            environment=(
                profile.environment if profile is not None else identity.UNSET
            ),
            connected=connected,
            transaction=self._transaction_text(name) if connected else "",
            read_only=bool(getattr(child, "read_only", False)),
        )
        context = getattr(child, "status_context", None)
        self._status_bar.set_context(context() if context is not None else "")

    @staticmethod
    def _tab_database(child, profile: ConnectionProfile | None) -> str:
        """The database the tab is really on: a console can switch it
        per tab, everything else follows the profile."""
        selected = getattr(child, "selected_database", None)
        if selected is not None and (database := selected()):
            return database
        return profile.database if profile is not None else ""

    def _transaction_text(self, name: str) -> str:
        if not self.transaction_active(name):
            self._transaction_since.pop(name, None)
            return ""
        started = self._transaction_since.setdefault(name, time.monotonic())
        minutes = int((time.monotonic() - started) // 60)
        return (
            "⏺ transaction open"
            if minutes < 1
            else f"⏺ transaction open {minutes} min"
        )

    def _connect_now(self, name: str) -> None:
        """The status bar's Connect button: open the connection now
        rather than on the next query."""
        profile = self.workspace.find_connection(name)
        if profile is None:
            return
        self._status_bar.set_job(f"Connecting to {name}…")
        run_async(
            lambda: self.ensure_connector(profile),
            lambda _c: (
                self._status_bar.set_job(""),
                self.refresh_status_bar(),
            ),
            lambda exc: (
                self._status_bar.set_job(""),
                self.show_error(str(exc)),
            ),
        )

    def _on_palette_changed(self) -> None:
        """The colour scheme flipped: CSS-driven surfaces restyle
        themselves, tab icons are textures and must be redrawn."""
        self._refresh_tab_identity()

    def _refresh_tab_identity(self) -> None:
        for pane in self._all_panes():
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

    def build_pane(
        self, popped_out: bool = False, solo: bool = False
    ) -> _TabPane:
        """A pane wired to this window's tab handling. Docked panes go
        on to join the splitter layout (_add_pane); a popped-out pane
        lives in a _PopoutWindow and shares only the handlers. A solo
        pane has no tab bar and holds one tab that stays put."""
        pane = _TabPane(tabbed=not solo)
        if not solo:
            pane.view.set_menu_model(_tab_menu(popped_out))
        # setup-menu names the tab the menu was opened on (and hands
        # back None when it closes); the actions fall back to the
        # selected tab of the pane they were invoked from, which is what
        # the menu bar and the shortcuts mean by "this tab".
        pane.view.connect("setup-menu", self._on_tab_menu)
        pane.view.connect("close-page", self._on_close_page)
        # Dragging a tab off its bar and dropping it on nothing: hand
        # libadwaita a fresh window's view to move the page into.
        pane.view.connect("create-window", self._on_create_window)
        if popped_out:
            # No autohide: a pop-out with one tab still needs the bar,
            # as the bar is where it is dragged back from.
            pane.bar.set_autohide(False)
            for signal in ("page-attached", "page-detached", "page-reordered"):
                pane.view.connect(signal, self._on_popout_pages_changed)
            return pane
        pane.view.connect("page-attached", self._on_pages_changed)
        pane.view.connect("page-detached", self._on_pages_changed)
        pane.view.connect("page-reordered", self._on_pages_changed)
        pane.view.connect(
            "notify::selected-page",
            lambda *_: self._update_active_panel(),
        )
        # Any click inside a pane makes it the target for new tabs.
        click = Gtk.GestureClick(button=0)
        click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        click.connect("pressed", self._on_pane_pressed, pane)
        pane.add_controller(click)
        return pane

    def _add_pane(self) -> _TabPane:
        pane = self.build_pane()
        self._panes.append(pane)
        self._rebuild_panes()
        return pane

    def _all_panes(self) -> list[_TabPane]:
        """Every pane that holds tabs: the docked ones plus one per
        pop-out window. Anything that walks all open tabs goes through
        here; the layout (splitters, placeholder, active pane) is about
        docked panes only."""
        return self._panes + [window.pane for window in self._popouts]

    # Pop-out windows

    def install_tab_actions(
        self, target: Gio.ActionMap, pane: _TabPane | None = None
    ) -> None:
        """Install the tab menu's actions on `target` (this window, or
        a pop-out). `pane` is the fallback target for an action that did
        not come from a tab's own context menu — for a pop-out, its own
        pane; for the main window, whichever pane is active."""
        for name, callback in (
            ("close-tab", self._close_menu_tab),
            ("close-other-tabs", self._close_other_tabs),
            ("close-tabs-right", self._close_tabs_right),
            ("close-all-tabs", self._close_all_tabs),
            ("move-to-window", self._move_tab_out),
            ("move-to-main", self._move_tab_back),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect(
                "activate", lambda _a, _p, cb=callback: cb(pane)
            )
            target.add_action(action)

    def _new_popout(self, solo: bool = False) -> _PopoutWindow:
        window = _PopoutWindow(self, solo=solo)
        self._popouts.append(window)
        self.refresh_workspace_identity()  # stripe tooltip, once listed
        window.connect("destroy", self._on_popout_destroyed)
        return window

    def _on_popout_destroyed(self, window: _PopoutWindow) -> None:
        if window in self._popouts:
            self._popouts.remove(window)

    def _on_create_window(self, _view) -> Adw.TabView:
        window = self._new_popout()
        window.present()
        return window.pane.view

    def _popout_for(self, view: Adw.TabView) -> _PopoutWindow | None:
        for window in self._popouts:
            if window.pane.view is view:
                return window
        return None

    def _on_popout_pages_changed(self, view: Adw.TabView, *_args) -> None:
        window = self._popout_for(view)
        if window is not None and view.get_n_pages() == 0:
            # Deferred: the window owns the view this signal is being
            # emitted on, so it cannot be torn down from inside it.
            GLib.idle_add(self._close_empty_popout, window)
        if not self._restoring:
            self._save_state()

    def _close_empty_popout(self, window: _PopoutWindow) -> bool:
        """Idle callback: a pop-out whose last tab left closes. It is
        dropped from the list here rather than in the destroy handler —
        a destroyed window still holding references does not always
        reach dispose, and ::destroy with it."""
        if window.pane.view.get_n_pages() == 0:
            self._on_popout_destroyed(window)
            window.destroy()
        return False

    def _move_tab_out(self, pane: _TabPane | None = None) -> None:
        """Move a tab into a window of its own."""
        target = self._target_tab(pane)
        if target is None:
            self.show_error("No tab to move")
            return
        source, page = target
        window = self._new_popout()
        window.present()
        source.view.transfer_page(page, window.pane.view, 0)

    def _move_tab_back(self, pane: _TabPane | None = None) -> None:
        """Move a popped-out tab back into the main window."""
        target = self._target_tab(pane)
        if target is None:
            return
        source, page = target
        if source in self._panes:
            self.show_error("That tab is already in the main window")
            return
        view = self._active_pane.view
        source.view.transfer_page(page, view, view.get_n_pages())
        view.set_selected_page(page)
        self.present()

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
        self._sync_split_button()
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

    def _sync_split_button(self) -> None:
        """The button shows whether the split is open. Panes are never
        dropped for going empty — only toggling the button closes the
        split — so this only follows a programmatic layout change."""
        button = getattr(self, "_split_button", None)
        if button is None:  # first pane, built before the header
            return
        self._syncing_split = True
        try:
            button.set_active(len(self._panes) > 1)
        finally:
            self._syncing_split = False

    def _set_active_pane(self, pane: _TabPane) -> None:
        self._active_pane = pane
        self._overview.set_view(pane.view)
        self._tab_button.set_view(pane.view)
        self._update_active_panel()

    def _update_active_panel(self) -> None:
        """Tell the side panel which tab is current: the This-panel
        history scope, which pages to offer (context), the Info
        content, and the saved-filter target. The status bar follows
        the same switch."""
        page = self._active_pane.view.get_selected_page()
        child = page.get_child() if page is not None else None
        self.refresh_status_bar()
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
        self._update_note_target(child)
        if isinstance(child, (QueryConsole, CliConsole)):
            self._set_console_info(child)
        else:
            self._refresh_side_ddl()

    def _update_note_target(self, child) -> None:
        """Tell the side panel's Notes page which object the active tab
        is about — a new note defaults to it — and which connections
        the workspace still has, so a note about a removed one is
        badged orphaned rather than dropped."""
        connection = ""
        table = ""
        if isinstance(child, (TableTab, DefinitionTab)):
            connection = child.profile.name
            table = child.table
        elif isinstance(child, (QueryConsole, CliConsole)):
            connection = child.selected_connection()
        self._side_panel.set_note_target(
            connection,
            table,
            [profile.name for profile in self.workspace.connections],
        )

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
        an open transaction — or an MCP tab whose server is running — is held
        back behind a confirmation dialog; forcing the close rolls the
        transaction back (or stops the server). Either way the
        closed tab's entries leave the side panel's history scopes
        (panel_closed) but stay in the workspace-wide History tab."""
        child = page.get_child()
        if isinstance(child, QueryConsole):
            name = child.open_transaction_connection()
            if name:
                if self._closing_connection == name:
                    # Part of a Close all related tabs run: the one
                    # confirmation covered this tab, so roll the
                    # transaction back and let the close through.
                    self._rollback_connections([name])
                else:
                    self._confirm_console_close(view, page, name)
                    return True  # close_page_finish decides later
        if isinstance(child, McpServerTab) and child.running:
            self._confirm_mcp_close(view, page, child)
            return True
        self._mark_panel_closed(page)
        return False  # let the default handler close the page

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
            view.close_page_finish(page, force)

        dialog.connect("response", respond)
        dialog.present(view.get_root() or self)

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

    # Closing tabs

    def _on_tab_menu(self, _view, page: Adw.TabPage | None) -> None:
        self._menu_tab_page = page

    def _target_tab(
        self, pane: _TabPane | None = None
    ) -> tuple[_TabPane, Adw.TabPage] | None:
        """The tab the tab actions act on: the one whose menu is open,
        else the selected tab of `pane` (the pane the action was
        installed for) or of the active pane. None when nothing is
        open."""
        page = self._menu_tab_page
        if page is None:
            pane = pane or self._active_pane
            page = pane.view.get_selected_page()
            if page is None:
                return None
            return pane, page
        for pane in self._all_panes():
            for i in range(pane.view.get_n_pages()):
                if pane.view.get_nth_page(i) is page:
                    return pane, page
        return None

    def _close_menu_tab(self, pane: _TabPane | None = None) -> None:
        target = self._target_tab(pane)
        if target is None:
            self.show_error("No tab to close")
            return
        pane, page = target
        pane.view.close_page(page)

    def _close_other_tabs(self, pane: _TabPane | None = None) -> None:
        target = self._target_tab(pane)
        if target is None:
            return
        pane, page = target
        pane.view.close_other_pages(page)

    def _close_tabs_right(self, pane: _TabPane | None = None) -> None:
        target = self._target_tab(pane)
        if target is None:
            return
        pane, page = target
        pane.view.close_pages_after(page)

    def _close_all_tabs(self, _pane: _TabPane | None = None) -> None:
        """Every tab in every pane, pop-out windows included. Pages are
        collected first: closing one mutates the views (and may re-enter
        through a close confirmation), so iterating them live would skip
        tabs."""
        pages = [
            (pane, pane.view.get_nth_page(i))
            for pane in self._all_panes()
            for i in range(pane.view.get_n_pages())
        ]
        if not pages:
            self.show_error("No tabs are open")
            return
        for pane, page in pages:
            pane.view.close_page(page)

    def _on_split_toggled(self, button: Gtk.ToggleButton) -> None:
        # The button is the split's state, so ignore the toggle it emits
        # when the code below (or a restore) syncs it to the layout.
        if self._syncing_split:
            return
        if button.get_active():
            self._split_view()
        else:
            self._unsplit_view()

    def _toggle_split(self, *_args) -> None:
        self._split_button.set_active(not self._split_button.get_active())

    def _split_view(self) -> None:
        """Open the second pane. The selected tab moves into it when
        there is another tab to leave behind; otherwise the new pane
        starts empty, waiting for a tab to be opened or dragged in."""
        if len(self._panes) >= 2:
            return
        pane = self._active_pane
        page = pane.view.get_selected_page()
        new_pane = self._add_pane()
        if page is not None and pane.view.get_n_pages() > 1:
            pane.view.transfer_page(page, new_pane.view, 0)
        self._set_active_pane(new_pane)
        self._update_placeholder()

    def _unsplit_view(self) -> None:
        """Close the split: every tab of the second pane joins the
        first, in order, and the pane goes."""
        if len(self._panes) < 2:
            return
        keep, extra = self._panes[0], self._panes[1:]
        selected = self._active_pane.view.get_selected_page()
        for pane in extra:
            while pane.view.get_n_pages():
                page = pane.view.get_nth_page(0)
                pane.view.transfer_page(page, keep.view, keep.view.get_n_pages())
        self._panes = [keep]
        self._rebuild_panes()
        self._set_active_pane(keep)
        if selected is not None:
            keep.view.set_selected_page(selected)
        self._update_placeholder()

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
                GLib.idle_add(self._mark_tabs_disconnected, profile.name, False)
                GLib.idle_add(self.refresh_status_bar)
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
            for pane in self._all_panes():
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
                elif tab.kind == "indexes":
                    if profile is not None:
                        self.open_indexes(profile)
                elif tab.kind == "users":
                    if profile is not None:
                        self.open_users(profile)
                elif tab.kind == "object":
                    if profile is not None:
                        self.open_object(profile, objects.ObjectRef(
                            kind=tab.object_kind,
                            name=tab.table,
                            table=tab.object_owner,
                            category=tab.object_category,
                        ))
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
        # Tabs are stored flat, pane order first (docked panes, then
        # each pop-out window): a split or a pop-out restores as a
        # single pane in the main window on reopen. Session-only tabs (tab_state() is
        # None, e.g. the history view) are not saved.
        selected = self._active_pane.view.get_selected_page()
        self.workspace.selected_tab = -1
        tabs = []
        for pane in self._all_panes():
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
        if not self._restoring:
            self._save_state()

    def _on_close_request(self, *_args) -> bool:
        self._save_state()
        if self._sidebar_width_source:
            GLib.source_remove(self._sidebar_width_source)
            self._save_sidebar_width()
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
            # The workspace is going: stop any MCP server still running
            # in one of its tabs and take the pop-out windows with it,
            # no separate per-tab confirmation.
            for pane in self._all_panes():
                for i in range(pane.view.get_n_pages()):
                    child = pane.view.get_nth_page(i).get_child()
                    if isinstance(child, McpServerTab):
                        child.stop_instance()
            for window in list(self._popouts):
                self._on_popout_destroyed(window)
                window.destroy()
            self._close_confirmed = True
            self.close()

        transactions = self._open_transactions()
        if transactions:
            self._rollback_connections(transactions, then=finish)
        else:
            finish()

    # UI actions (main thread)

    def _toast_overlay(self) -> Adw.ToastOverlay:
        """Toasts belong on the window the user is looking at: a
        pop-out's own overlay while it has the focus, otherwise the
        main window's. The status bar (main window only) says it too,
        so nothing is lost either way."""
        for window in self._popouts:
            if window.is_active():
                return window.toasts
        return self._toasts

    def show_error(self, message: str) -> None:
        self._toast_overlay().add_toast(Adw.Toast(title=message))
        self._status_bar.set_status(message, error=True)

    def show_message(self, message: str) -> None:
        """Something worked and left no visible trace of its own (an
        export writing a file, say) — the same two surfaces as an
        error, without the error styling."""
        self._toast_overlay().add_toast(Adw.Toast(title=message))
        self._status_bar.set_status(message, error=False)

    def show_aggregate(self, lines: list[str], live: bool = False) -> None:
        """Route a grid's summary of its selection into the side panel.

        Live summaries (every selection change) only fill the page, so
        the panel keeps showing whatever the user put there; the
        Aggregate menu item asks for the page and gets the panel
        opened on it."""
        if live:
            self._side_panel.set_aggregate(lines)
            return
        self._side_panel.show_aggregate(lines)
        self._set_side_panel_shown(True)

    # Transfer (frontend/transfer.py, backend/exchange.py)

    def _export_workspace(self, *_args) -> None:
        transfer.export_workspace(
            self, self.workspace, self.show_message, self.show_error
        )

    def _export_connections(self, *_args) -> None:
        if not self.workspace.connections:
            self.show_error("This workspace has no connections to export")
            return
        transfer.export_connections(
            self, self.workspace, self.show_message, self.show_error
        )

    def _import_connections(self, *_args) -> None:
        transfer.import_connections(
            self, self._connections_imported, self.show_error
        )

    def _connections_imported(self, profiles: list[ConnectionProfile]) -> None:
        """Add imported connections to this workspace. add_connection
        renames collisions rather than replacing anything, so importing
        a file twice cannot silently rewrite a connection that is
        already there."""
        for profile in profiles:
            self._profile_added(profile)
        self.show_message(
            f"Imported {len(profiles)} connection(s) into "
            f"“{self.workspace.name}”"
        )

    def _add_connection(self, *_args) -> None:
        ConnectionDialog(on_save=self._profile_added).present(self)

    def _profile_added(self, profile: ConnectionProfile) -> None:
        self.workspace.add_connection(profile)  # also deduplicates the name
        self._store.save(self.workspace)
        # All open consoles' dropdowns share this list and update live.
        self._connection_names.append(profile.name)
        self._sidebar.add_profile(profile)
        self._sidebar.expand_profile(profile.name)
        self._refresh_placeholder_actions()

    def _drop_connector(self, name: str) -> None:
        """Forget a cached connector (edit/remove) and close it in the
        background; nothing else in the app currently closes connectors
        explicitly, but a connection the user just removed shouldn't
        keep a server-side session open behind them."""
        with self._connectors_lock:
            connector = self._connectors.pop(name, None)
        if connector is not None:
            run_async(connector.close, lambda _r: None, lambda _e: None)
        # The connection is gone: the sidebar dot and the status bar
        # must say so rather than keep showing it as open.
        self._sidebar.set_connected(name, False)
        # Whatever banner the old session left is about a connection
        # that no longer exists in this form; the caller re-adds one if
        # the tabs are staying (see _do_disconnect).
        self._mark_tabs_disconnected(name, False)
        self._transaction_since.pop(name, None)
        self.refresh_status_bar()

    # Disconnecting (CORE-06)

    def _running_queries(self, name: str) -> int:
        """How many open consoles have a statement in flight on this
        connection. Closing the connection under them would abort those
        statements, so the user is asked first."""
        return sum(
            1
            for child in self._all_tab_children()
            if getattr(child, "is_running", False)
            and _page_connection(child) == name
        )

    def _disconnect_connection(self, profile: ConnectionProfile) -> None:
        """The connection menu's Disconnect: close the pooled
        connection and let the tree fold back up."""
        if not self.is_connected(profile.name):
            return
        running = self._running_queries(profile.name)
        if not running:
            self._do_disconnect(profile)
            return
        plural = "query is" if running == 1 else "queries are"
        dialog = Adw.AlertDialog(
            heading=f"Disconnect “{profile.name}”?",
            body=f"{running} {plural} still running on this connection. "
            "Disconnecting cancels the run and closes the session.",
        )
        dialog.add_response("cancel", "Keep Connected")
        dialog.add_response("disconnect", "Disconnect Anyway")
        dialog.set_response_appearance(
            "disconnect", Adw.ResponseAppearance.DESTRUCTIVE
        )
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect(
            "response",
            lambda _d, response, p=profile: (
                self._do_disconnect(p) if response == "disconnect" else None
            ),
        )
        dialog.present(self)

    def _do_disconnect(self, profile: ConnectionProfile) -> None:
        name = profile.name
        for child in self._all_tab_children():
            if _page_connection(child) != name:
                continue
            cancel = getattr(child, "cancel_run", None)
            if cancel is not None and getattr(child, "is_running", False):
                cancel()
        self._drop_connector(name)
        self._sidebar.collapse_connection(name)
        self._mark_tabs_disconnected(name, True)
        self.show_message(f"Disconnected from {name}")

    def _reconnect_connection(self, profile: ConnectionProfile) -> None:
        """A tab's Reconnect button: open the connection again and put
        its tree back, without anything being reopened or restarted."""
        self._status_bar.set_job(f"Connecting to {profile.name}…")

        def done(_connector) -> None:
            self._status_bar.set_job("")
            self._mark_tabs_disconnected(profile.name, False)
            self._sidebar.expand_profile(profile.name)
            self.refresh_status_bar()

        def failed(exc: Exception) -> None:
            self._status_bar.set_job("")
            self.show_error(str(exc))

        run_async(lambda: self.ensure_connector(profile), done, failed)

    def _mark_tabs_disconnected(self, name: str, disconnected: bool) -> None:
        """Give (or take away) every tab on this connection its
        disconnected banner. The tabs stay exactly as they are: nothing
        is closed, nothing is cleared and nothing throws — the next
        thing any of them asks the backend for reconnects."""
        profile = self.workspace.find_connection(name)
        title = (
            f"Disconnected from “{name}”." if disconnected and profile else ""
        )
        for child in self._all_tab_children():
            if _page_connection(child) != name:
                continue
            if not isinstance(child, Gtk.Box):
                continue
            feedback.set_disconnected(
                child,
                self._disconnect_banners,
                title,
                lambda p=profile: self._reconnect_connection(p),
            )

    def _all_tab_children(self) -> list[Gtk.Widget]:
        """Every open tab's content widget, across panes and pop-outs."""
        children = []
        for pane in self._all_panes():
            for i in range(pane.view.get_n_pages()):
                children.append(pane.view.get_nth_page(i).get_child())
        return children

    # Closing every tab on a connection (CORE-07)

    def _connection_pages(self, name: str) -> list[tuple[_TabPane, Adw.TabPage]]:
        """Every open tab on this connection, across panes and pop-out
        windows, paired with the pane it lives in. Collected up front:
        closing a page mutates the views it is iterating."""
        return [
            (pane, page)
            for pane in self._all_panes()
            for page in [
                pane.view.get_nth_page(i)
                for i in range(pane.view.get_n_pages())
            ]
            if _page_connection(page.get_child()) == name
        ]

    def count_connection_tabs(self, name: str) -> int:
        """How many open tabs belong to this connection. The sidebar
        asks before it builds the connection menu, so the item can
        carry the count and go dead when there is nothing to close."""
        return len(self._connection_pages(name))

    def _close_connection_tabs(self, profile: ConnectionProfile) -> None:
        """The connection menu's Close all related tabs: every tab on
        this connection and no other. Tabs holding work that was never
        written — typed SQL, edited grid cells — are listed in one
        confirmation first, rather than one dialog per tab."""
        pages = self._connection_pages(profile.name)
        if not pages:
            self.show_error(f"No tabs are open on {profile.name}")
            return
        unsaved = [
            (page, work)
            for _pane, page in pages
            for work in [getattr(page.get_child(), "unsaved_work", lambda: "")()]
            if work
        ]
        if not unsaved:
            self._do_close_connection_tabs(profile.name, pages)
            return
        listing = "\n".join(
            f"• {page.get_title()} — {work}" for page, work in unsaved
        )
        count = len(pages)
        dialog = Adw.AlertDialog(
            heading=f"Close {count} tab{'' if count == 1 else 's'} "
            f"on “{profile.name}”?",
            body="These tabs have work that was never written:\n\n"
            f"{listing}",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("discard", "Discard")
        dialog.add_response("save", "Save")
        dialog.set_response_appearance(
            "discard", Adw.ResponseAppearance.DESTRUCTIVE
        )
        dialog.set_response_appearance(
            "save", Adw.ResponseAppearance.SUGGESTED
        )
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def respond(_dialog, response: str) -> None:
            if response == "cancel":
                return
            if response == "save":
                for page, _work in unsaved:
                    save = getattr(page.get_child(), "save_unsaved_work", None)
                    if save is not None:
                        save()
            self._do_close_connection_tabs(profile.name, pages)

        dialog.connect("response", respond)
        dialog.present(self)

    def _do_close_connection_tabs(
        self, name: str, pages: list[tuple[_TabPane, Adw.TabPage]]
    ) -> None:
        self._closing_connection = name
        try:
            for pane, page in pages:
                # A closed tab keeps no disconnected banner: the
                # registry is keyed by widget and would otherwise hold
                # tabs that no longer exist.
                self._disconnect_banners.pop(page.get_child(), None)
                pane.view.close_page(page)
        finally:
            self._closing_connection = ""
        count = len(pages)
        self.show_message(
            f"Closed {count} tab{'' if count == 1 else 's'} on {name}"
        )

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
        self._refresh_placeholder_actions()

    def _focus_tab(self, key: tuple) -> bool:
        """Select the open tab with this tab_key, if any — including
        one that has been popped out, whose window is raised."""
        for pane in self._all_panes():
            for i in range(pane.view.get_n_pages()):
                page = pane.view.get_nth_page(i)
                if getattr(page.get_child(), "tab_key", None) != key:
                    continue
                pane.view.set_selected_page(page)
                if pane in self._panes:
                    self._set_active_pane(pane)
                else:
                    window = self._popout_for(pane.view)
                    if window is not None:
                        window.present()
                return True
        return False

    def _tab_for(self, key: tuple) -> Gtk.Widget | None:
        """The open tab with this key, in any pane or pop-out."""
        for pane in self._all_panes():
            for i in range(pane.view.get_n_pages()):
                child = pane.view.get_nth_page(i).get_child()
                if getattr(child, "tab_key", None) == key:
                    return child
        return None

    def _append_tab(
        self,
        tab: Gtk.Widget,
        key: tuple,
        title: str,
        tooltip: str,
        place: bool = True,
    ) -> Adw.TabPage:
        tab.tab_key = key
        view = self._active_pane.view
        page = view.append(tab)
        page.set_title(title)
        page.set_tooltip(tooltip)
        self._apply_page_identity(page, _page_connection(tab))
        view.set_selected_page(page)
        if place:
            self._place_new_page(page)
        return page

    def _shift_held(self) -> bool:
        """Is Shift down right now, on its own? Ctrl+Shift accelerators
        (Close All Tabs, Open History) are not "…and in a new window",
        so a held Ctrl takes Shift out of play."""
        display = self.get_display()
        seat = display.get_default_seat() if display is not None else None
        keyboard = seat.get_keyboard() if seat is not None else None
        if keyboard is None:
            return False
        state = keyboard.get_modifier_state()
        if state & Gdk.ModifierType.CONTROL_MASK:
            return False
        return bool(state & Gdk.ModifierType.SHIFT_MASK)

    def _place_new_page(self, page: Adw.TabPage) -> Adw.TabPage:
        """Shift held while a tab was opened — from the sidebar, a
        menu, anywhere — means "in a window of its own". The tab is
        made as a tab either way and moved after, so nothing that
        opens one has to know about pop-outs."""
        if self._restoring or not self._shift_held():
            return page
        window = self._new_popout()
        window.present()
        self._active_pane.view.transfer_page(page, window.pane.view, 0)
        return page

    def open_table(
        self, profile: ConnectionProfile, table: str
    ) -> TableTab | None:
        """Open (or focus) the tab showing one table's data, and return
        it — deep links (CORE-05) then switch it to a section."""
        key = ("table", profile.name, table)
        existing = self._tab_for(key)
        if existing is not None:
            self._focus_tab(key)
            return existing
        tab = TableTab(
            profile,
            table,
            self.ensure_connector,
            self.show_error,
            on_aggregate=self.show_aggregate,
            on_open_object=self.open_object,
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
        return tab

    def open_table_section(
        self, profile: ConnectionProfile, table: str, section: str
    ) -> None:
        """A sidebar row under a table — Indexes, Constraints, Columns
        — opens that table's Properties view on that section (CORE-05).

        The table tab is reused where it is already open, so the deep
        link never costs a second copy of the grid.
        """
        tab = self.open_table(profile, table)
        if tab is not None:
            tab.show_properties(section)

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
        # different connection sets) can run side by side. Always a
        # window of its own, and a solo one — no tab bar, nothing to
        # drag it in or out of: a running server is a background
        # process to keep an eye on, not something that can end up
        # buried behind the tab you are working in.
        tab = McpServerTab(
            self.workspace.name,
            self.workspace.connections,
            self.show_error,
            profile,
        )
        page = self._append_tab(
            tab,
            ("mcp", id(tab)),
            "MCP Server",
            f"MCP server for {self.workspace.name}",
            place=False,
        )
        window = self._new_popout(solo=True)
        window.present()
        self._active_pane.view.transfer_page(page, window.pane.view, 0)

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

    def open_indexes(self, profile: ConnectionProfile) -> None:
        key = ("indexes", profile.name)
        if self._focus_tab(key):
            return
        tab = IndexesTab(profile, self.ensure_connector, self.show_error)
        self._append_tab(
            tab,
            key,
            f"{profile.name} ▸ indexes",
            f"Indexes on {profile.name}",
        )

    def open_object(
        self,
        profile: ConnectionProfile,
        ref: objects.ObjectRef,
        path: str = "",
    ) -> None:
        """The read-only info view for one catalog object — any node of
        the sidebar tree, and any row of a group listing inside one.

        Deduplicated on (connection, kind, name, owning table): opening
        the same object again focuses the tab that is already showing
        it rather than stacking copies of one read-only screen.
        """
        if ref.kind == "principal":
            self.open_principal_permissions(profile, ref)
            return
        key = tab_key(profile, ref)
        if self._focus_tab(key):
            return
        tab = ObjectInfoTab(
            profile,
            ref,
            self.ensure_connector,
            self.show_error,
            self.open_object,
            path=path,
        )
        label = objects.TYPE_LABELS.get(ref.kind, "object").lower()
        self._append_tab(
            tab,
            key,
            f"{ref.name} · {label}",
            f"{label.capitalize()} {ref.name} on {profile.name}",
        )

    def open_principal_permissions(
        self, profile: ConnectionProfile, ref: objects.ObjectRef
    ) -> None:
        """A row of an object's Permissions section (CORE-11): the
        permission editor for the principal it names, opened on the
        object the row was read from.

        The users tab is where the editor lives, so this reuses it —
        one screen for grants, entered from either end."""
        tab = self.open_users(profile)
        if tab is None:
            return
        tab.open_permissions_for(
            ref.name,
            NodeRef(kind=ref.category or "table", name=ref.table),
        )

    def open_users(self, profile: ConnectionProfile) -> UsersTab | None:
        """The connection's accounts and their privileges. Deduplicated
        per connection: accounts are server-wide, so a second tab on
        the same server would show the same list."""
        key = ("users", profile.name)
        if self._focus_tab(key):
            return self._tab_for(key)
        tab = UsersTab(
            profile,
            self.ensure_connector,
            self.show_error,
            lambda target, sql: self.new_query(target, sql=sql),
        )
        self._append_tab(
            tab,
            key,
            f"{profile.name} ▸ users",
            f"Users and permissions on {profile.name}",
        )
        return tab

    def open_backups(self) -> None:
        """The backup manager. One per window: jobs, destinations and
        run history are workspace-wide, so a second tab would be two
        views of the same list disagreeing about what is selected."""
        key = ("backups",)
        if self._focus_tab(key):
            return
        tab = BackupsTab(self.workspace, self.ensure_connector, self.show_error)
        self._append_tab(
            tab,
            key,
            "Backups",
            f"Backup jobs for {self.workspace.name}",
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
        console.on_busy = lambda busy: self._status_bar.set_job(
            "Running…" if busy else ""
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
        return self._place_new_page(page)

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
        return self._place_new_page(page)

    # Whole-database schema (backend/schemas.py)

    def _open_schema(self, profile: ConnectionProfile) -> None:
        """Open a connection's whole structure in a console to read.

        The capture is a catalog walk — one round trip per object —
        so it runs on a worker thread and the console opens when the
        script is ready. Nothing is saved on the way: this is a query
        like any other, and the console's Save keeps it if it is
        worth keeping. Nothing is executed either — DDL is shown
        before it runs.
        """

        def work() -> str:
            connector = self.ensure_connector(profile)
            return schemas.capture(
                connector, kind=profile.kind, source=profile.name
            )

        run_async(
            work,
            lambda sql: self.new_query(profile, sql=sql),
            lambda exc: self.show_error(str(exc)),
        )

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
        self.refresh_status_bar()  # row counts, transaction, connected state
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
        for pane in self._all_panes():
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
        has_tabs = len(self._panes) > 1 or any(
            p.view.get_n_pages() > 0 for p in self._panes
        )
        self._stack.set_visible_child_name(
            "tabs" if has_tabs else "placeholder"
        )
