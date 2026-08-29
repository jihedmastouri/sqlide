"""IDE-like schema tree sidebar.

Gtk.TreeListModel + Gtk.ListView + Gtk.TreeExpander (the GTK4 idiom
for lazy trees). Shape:

    connection → Tables / Views / Functions / Indexes / Triggers /
    Events → object → property sections → columns / indexes / triggers

The Indexes/Triggers/Events categories appear only when the adapter's
ddl_kinds() advertises the kind (known after connect) and it supports
dropping (JDBC stays template-only).

A MySQL or PostgreSQL connection reaches a whole server, not one
database, so there the shape gains a level and the categories hang off
a database instead:

    connection → database → Tables / Views / … → object → sections

Every database the server has is listed, the connection's own first
and marked "current"; tables never sit at the connection root, because
a table belongs to a database and not to the server. Each database row
loads through a connection of its own (see _database_profile — a
profile copy named "connection · database", which is also what a query
console's database dropdown builds, so the two share a connector); the
current database's row reuses the connection's own profile.

Where the engine has schemas as a level of their own — PostgreSQL, and
whatever else declares the `schemas` capability (PG-01) — one more
level appears between the two, and the categories hang off a schema:

    connection → database → schema → Tables / Views / … → object

The level is the provider's answer, not a name check: MySQL calls a
schema a database and SQLite has neither, so on those the tree keeps
exactly the shape above and no phantom level appears. A schema row
loads through its own derived profile too (schema_profile — a copy
named "connection · database · schema" with `schema` pinned, which is
what the console's schema dropdown builds), so the connection behind
it has that schema on its search path and every listing under the row
is that schema's, resolved rather than guessed.

Accounts are not schema objects, so they never sit among the tables
people came for: an engine that shows them at all hangs them off the
*connection* row, in the folder its provider declares (Roles on
PostgreSQL, Users on MySQL — PG-02, MY-01). "Users & Permissions…" on
the connection menu still opens them in a tab of their own
(frontend/users_tab.py).

Where a schema *is* a database — MySQL — the server's own schemas are
databases in this tree, so a database row is dimmed, sorted last and
hidden by the same setting a system schema is (PG-03, MY-01).

Under a table sit its Properties sections (CORE-05) — Columns,
Constraints, Foreign keys, Indexes, … — exactly the sections that
engine's Properties view has (registry.property_sections, no
connection needed). Opening one opens the table's tab on the
Properties side, scrolled to that section; the three that hold objects
of their own (Columns, Indexes, Triggers) also expand into them, and
opening one of those rows opens that object's info view instead.

Column rows show "name  type" with a PK marker and are informational.
Rows lead with a per-kind icon (connections also get a connection
status dot) and expandable rows end with a caret; the built-in
expander arrow is hidden. A connection row also carries a + button:
left-clicking it opens the same "New ▸" list as the context menu —
tables, views, indexes, triggers and, where the dialect has them,
functions, procedures and events — so creating an object never
depends on knowing that the row has a right-click menu.

A single click selects a row and toggles its expansion — a leaf row
just selects, and nothing opens (CORE-52). Because a double click
delivers that press too, the toggle waits out the double-click
interval and a second press cancels it (CORE-58): a double click (or
Enter) opens the row and leaves expansion exactly as it was. Every
kind opens something: a table/view opens a data tab, a function opens
its definition in an editable tab, and everything else — categories,
columns, indexes, triggers, events, and any kind added later — opens
the read-only object info view (frontend/object_info.py, "Object Info"
on every context menu). Opening something already open focuses its tab
(CORE-01). The caret does the same toggle a click on the row does, at
once and with no wait, since it can only mean expansion.

Every menu of a row that opens something starts with Open and Open
(Window); a row that opens nothing — a "Loading…" placeholder — has
neither. Open (Window) hands the opening to the window with "in a
window of its own" forced on, which is the tear-out path a dragged tab
and a Shift-click already take (window.open_in_window), rather than a
second way to make a pop-out. Under them, every row that has an object
behind it offers Properties and Properties (Window): the right side
panel pointed at that object, or the same surface torn off into its own
window (CORE-47). A section row under a table targets its table on that
section, which is what CORE-05's deep link now means.

Right-clicking a table or view then offers View Data /
Query Console / Table Definition; right-clicking a connection offers
a new query console (new consoles otherwise come from the header-bar
button), the connection's relation graph, an MCP Server tab
preselecting that connection, a "New ▸" submenu of the adapter's
creatable kinds, Refresh (drops and reloads the subtree), Edit… (the
connection dialog pre-filled, applied in place so open tabs keep
working) and Remove… (confirmed, drops the profile from the
workspace). Every droppable object row gets "Drop…". The Indexes
category also gets "View All…", opening every index on the connection
— name, table and CREATE INDEX text — in one read-only tab, since an
individual index row is browse-to-drop only. Context menus
are built per popup
because their items depend on the connection's capabilities. Hovering
a table/view shows its DDL in a tooltip (fetched lazily, cached on the
node); hovering a connection shows a short summary; hovering any other
row whose name is long enough to run past a narrow sidebar shows the
name in full.

Name labels are never ellipsized, so the tree keeps its natural width
and the sidebar (a Gtk.ScrolledWindow) grows a horizontal scrollbar
when that is wider than the panel, and a vertical one when it is
taller. The secondary label beside a name — type, row count, detail —
is the exception: it ellipsizes and asks for next to no width, so it
yields to the name instead of widening the tree, and carries the
untruncated text in a tooltip (CORE-51).

set_filter() switches the view to search mode: the same tree, pruned
to the rows whose names match the query (subsequence match, matched
letters bold) together with their ancestors, optionally narrowed to
chosen object kinds (frontend/tree_search.py). Only already-loaded
schema is searched. clear_filter() — Exit, or Escape, in the sidebar's
search row — brings the tree back with the expansion it had when
search opened.

follow_object() is the other direction of CORE-52's opening: the
window hands the active tab's object over on every tab switch and the
tree reveals it — ancestors expanded, the row selected. It is
deliberately quiet about it. The call is debounced, so cycling through
tabs with the keyboard resolves only the tab you stop on rather than
one path per keystroke; the walk expands exactly the rows on the path
and asks for nothing sideways, so a deep PostgreSQL path costs one
listing per level and no cascade; and a row already on screen is
selected where it is rather than scrolled to, so the tree the user
navigated to stays where they left it. A tab with no object behind it
(a query console) clears the selection instead of leaving a stale
highlight, and the sync is one-way: selecting in the tree never
switches tabs. `sidebar_follow_active_tab` (settings.toml,
"Follow the Active Tab" in Preferences) turns the whole thing off.

Lazy loading: GTK probes create_func just to decide whether a row gets
an expander arrow, so it must stay cheap — it only creates (and caches)
an empty child store. The actual database work (list_tables /
list_columns / list_functions via run_async) starts when a row is
actually expanded, watched through TreeListRow's expanded property.
A failed load leaves the node unloaded, so collapsing and re-expanding
retries.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Callable

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango

from sqlide.backend import identity
from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.db import metrics, objects, registry
from sqlide.backend.db.base import Connector
from sqlide.backend.db.metadata import NodeRef
from sqlide.backend.settings import store as settings_store
from sqlide.backend.table_templates import store as template_store
from sqlide.frontend import identity as identity_ui
from sqlide.frontend import tree_search
from sqlide.frontend.util import describe, run_async
from sqlide.i18n import _

_EXPANDABLE = (
    "connection", "database", "schema", "category", "table", "view",
    "section",
)

# Leading icon per row kind; kinds not listed (category, column, note)
# show no icon.
_KIND_ICONS = {
    "connection": "network-server-symbolic",
    "database": "drive-multidisk-symbolic",
    "schema": "folder-symbolic",
    "table": "view-grid-symbolic",
    "view": "view-reveal-symbolic",
    "function": "system-run-symbolic",
    "index": "view-continuous-symbolic",
    "trigger": "media-playback-start-symbolic",
    "event": "alarm-symbolic",
}

# "New ▸" submenu labels, in ddl_kinds order. The ellipsis marks the
# table designer; the rest prefill a query console with a template.
_NEW_LABELS = {
    "table": "Table…",
    "view": "View",
    "index": "Index",
    "trigger": "Trigger",
    "function": "Function",
    "procedure": "Procedure",
    "event": "Event",
}

# What the + button offers before the connection has reported its own
# kinds: the three every dialect creates.
_DEFAULT_NEW_KINDS = ("table", "view", "index")

# A table's children are its Properties sections (CORE-05): the ones
# whose members are objects expand into them, the rest are leaves that
# only deep-link. Which sections exist at all is the engine's answer
# (registry.property_sections), and "general"/"ddl" are the summary and
# the definition, not lists — they are dropped here.
_SECTION_CHILD_KINDS = objects.SECTION_CHILD_KINDS
_NON_LIST_SECTIONS = ("general", "ddl")

# Lazily loaded category → the object row kind it holds. The catalog
# folders (PG-02) are all lazy: each is one listing the adapter reads on
# expand, so a connection costs nothing for the folders nobody opens.
# The relation folders — Tables, Views, Foreign Tables, Materialized
# Views — are filled from the listing the row above already made, so
# they are not here.
_LAZY_CATEGORIES = {
    "functions": "function",
    "procedures": "procedure",
    "indexes": "index",
    "triggers": "trigger",
    "events": "event",
} | {
    slug: kind
    for slug, (_label, kind) in objects.CATALOG_CATEGORIES.items()
    if slug not in objects.RELATION_FOLDERS and slug != "administer"
}


# Following the active tab (CORE-55). The window reports a tab switch
# straight away; the tree waits this long (milliseconds) before
# resolving it, so holding Ctrl+Tab through ten tabs walks one path,
# not ten.
_FOLLOW_DELAY = 180

# A single click toggles a row's expansion (CORE-52), but a double
# click delivers that first press too, so acting on it immediately made
# opening an object also expand or collapse it (CORE-58). The toggle
# waits out the double-click interval instead: if a second press lands,
# it is a double click and the pending toggle is dropped before
# anything moves. Falls back to GTK's own default when no Gtk.Settings
# is available (no display, a test harness).
_DOUBLE_CLICK_TIME = 400

# Which folders an object of a given kind can be sitting in, in the
# order they are worth looking in. A relation is a table or a view and
# the tab key does not say which; an engine with catalog folders
# (PG-02) keeps materialized views and foreign tables in folders of
# their own, so those are searched too.
_FOLLOW_CATEGORIES = {
    "table": ("tables", "foreign_tables", "views", "materialized_views"),
    "view": ("views", "materialized_views", "tables", "foreign_tables"),
    "function": ("functions", "procedures", "aggregates"),
    "procedure": ("procedures", "functions"),
    "index": ("indexes",),
    "trigger": ("triggers",),
    "event": ("events",),
    "sequence": ("sequences",),
    "data_type": ("data_types",),
    "extension": ("extensions",),
    "principal": ("roles", "users"),
}


# From how many characters a plain row's name earns a tooltip of its
# own — about what fits in the sidebar at its default width.
_LONG_LABEL = 28

# How wide the secondary label may grow before it ellipsizes, in
# characters. It never asks for more than this, so a long type or a
# six-digit row count cannot widen the tree past the width the sidebar
# was dragged to; and it can shrink to an ellipsis when the name needs
# the room, because the name is the part worth reading (CORE-51).
_DETAIL_MAX_CHARS = 18


class Node(GObject.Object):
    """One tree row; kind decides expandability, look, and activation.

    kind: "connection" | "database" | "category" | "table" | "view"
        | "section" (one Properties section of the table above it)
        | "function" | "index" | "trigger" | "event"
        | "column" | "note" (dim placeholder: loading/empty/error)
    """

    def __init__(
        self,
        kind: str,
        label: str,
        *,
        detail: str = "",
        profile: ConnectionProfile | None = None,
        category: str = "",  # category nodes: "tables"|"views"|"functions"|…
        # section nodes: the PROPERTY_SECTIONS slug ("indexes", …)
        payload: list | None = None,  # tables/views categories: TableInfo list
        is_pk: bool = False,
        table: str = "",  # index/trigger rows: owning table (for DROP)
        system: bool = False,  # a system schema, or a row inside one
    ) -> None:
        super().__init__()
        self.kind = kind
        self.label = label
        self.detail = detail
        self.profile = profile
        self.category = category
        self.payload = payload
        self.is_pk = is_pk
        self.table = table
        # A schema the server owns rather than the user, or anything
        # under one: drawn dimmed, sorted last, and left out of search
        # unless it is asked for (PG-03). Dimmed is not disabled — the
        # row expands, opens and refreshes like any other.
        self.system = system
        self.store: Gio.ListStore | None = None  # cached child model
        # The row this one was loaded under, so Refresh on a leaf can
        # refetch the category that owns it. Roots keep None.
        self.parent: Node | None = None
        self.loaded = False
        self.loading = False
        self.connected = False  # connection rows: status dot state
        self.ddl: str | None = None  # table/view rows: None = not fetched
        self.ddl_loading = False
        # Connection rows: adapter capabilities, known once loaded.
        self.ddl_kinds: tuple[str, ...] = ()
        self.supports_drop = False
        # Search mode: this row is a throwaway clone of `source`, and
        # search_ranges are the letters of its label the query lit up.
        self.filtered = False
        self.source: Node | None = None
        self.search_ranges: tuple[tuple[int, int], ...] = ()


class Sidebar(Gtk.ScrolledWindow):
    def __init__(
        self,
        ensure_connector: Callable[[ConnectionProfile], Connector],
        on_open_table: Callable[[ConnectionProfile, str], None],
        on_open_object: Callable[..., None],  # (profile, ObjectRef, path)
        # (profile, table, section slug): a table's Properties view,
        # opened on one section (CORE-05).
        on_open_section: Callable[[ConnectionProfile, str, str], None],
        on_new_query: Callable[..., None],  # (profile, sql="")
        on_open_cli: Callable[[ConnectionProfile], None],
        on_open_definition: Callable[[ConnectionProfile, str], None],
        # (profile, the table's NodeRef): the designer, opened on a
        # table that already exists (CORE-26).
        on_edit_table: Callable[[ConnectionProfile, NodeRef], None],
        on_open_function: Callable[[ConnectionProfile, str], None],
        on_relation_graph: Callable[[ConnectionProfile], None],
        on_view_indexes: Callable[[ConnectionProfile], None],
        on_query_builder: Callable[..., None],  # (profile, table="")
        on_drop_object: Callable[
            [ConnectionProfile, str, str, str], None
        ],  # (profile, kind, name, owning table)
        # (profile, kind, the node the menu was opened on)
        on_new_object: Callable[[ConnectionProfile, str, NodeRef], None],
        on_mcp_server: Callable[[ConnectionProfile], None],
        on_manage_users: Callable[[ConnectionProfile], None],
        on_monitor: Callable[[ConnectionProfile], None],
        on_open_schema: Callable[[ConnectionProfile], None],
        on_edit_connection: Callable[[ConnectionProfile], None],
        on_disconnect: Callable[[ConnectionProfile], None],
        on_close_tabs: Callable[[ConnectionProfile], None],
        count_tabs: Callable[[str], int],
        on_remove_connection: Callable[[ConnectionProfile], None],
        on_add_connection: Callable[[], None],
        show_error: Callable[[str], None],
        # (profile, "install"|"update"|"drop", extension name). Last and
        # optional so a harness that only walks the tree need not supply
        # one: the menu items simply do nothing without it (PG-05).
        on_extension_action: (
            Callable[[ConnectionProfile, str, str], None] | None
        ) = None,
        # The engine's settings surface, where it has one — SQLite's
        # PRAGMAs (SQ-02). Optional for the same reason as the line
        # above: a harness that only walks the tree need not supply
        # one, and the menu item then does nothing.
        on_pragmas: Callable[[ConnectionProfile], None] | None = None,
        # "Import Data…": a CSV file into the table this row names
        # (CORE-37). Optional like the rest of this tail — a harness
        # that only walks the tree leaves the menu item inert.
        on_import_data: Callable[[ConnectionProfile, str], None] | None = None,
        # "Open (Window)": runs an opener with "in a window of its own"
        # forced on, the same path Shift-clicking a row takes
        # (window._place_new_page). Optional like the two above — a
        # harness without a window then just opens the tab.
        on_open_window: Callable[[Callable[[], None]], None] | None = None,
        # "Properties" / "Properties (Window)": the row's object in the
        # right side panel, or torn off into a window (CORE-47). A
        # harness with no window behind it leaves them out.
        on_open_properties: Callable[..., None] | None = None,
        on_open_properties_window: Callable[..., None] | None = None,
        # "Find Data…": the cross-table value search (CORE-45), on the
        # scope the row stands for. Optional like the rest of this tail
        # — a harness that only walks the tree leaves it inert.
        on_data_search: Callable[[ConnectionProfile], None] | None = None,
        # "Duplicate Structure…": the table this row names, loaded into
        # a designer as a new table (CORE-29). Optional like the rest of
        # this tail — a harness that only walks the tree leaves it
        # inert.
        on_duplicate_table: (
            Callable[[ConnectionProfile, NodeRef], None] | None
        ) = None,
        # "New ▸ From Template ▸ …": a saved table shape, opened in a
        # designer on this connection (CORE-29).
        on_new_from_template: (
            Callable[[ConnectionProfile, str, NodeRef], None] | None
        ) = None,
    ) -> None:
        super().__init__(vexpand=True)
        # Both scrollbars, on demand. Row name labels are not ellipsized
        # (a truncated "customer_order_line_items" tells you nothing),
        # so a deep tree or a long name scrolls rather than being cut
        # to fit whatever width the sidebar was dragged to. The
        # ListView recycles rows, so only what is on screen is built
        # however big the tree gets.
        self.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self._ensure = ensure_connector
        # Profiles whose connector still has to drop its catalog cache
        # (CORE-41). Refresh is a main-loop action and a connector may
        # not even exist yet, so the invalidation is deferred to the
        # worker thread that next asks for one — see _connector().
        self._stale_catalogs: set[str] = set()
        self._on_open_table = on_open_table
        self._on_open_object = on_open_object
        self._on_open_section = on_open_section
        self._on_new_query = on_new_query
        self._on_open_cli = on_open_cli
        self._on_open_definition = on_open_definition
        self._on_edit_table = on_edit_table
        self._on_open_function = on_open_function
        self._on_relation_graph = on_relation_graph
        self._on_view_indexes = on_view_indexes
        self._on_query_builder = on_query_builder
        self._on_drop_object = on_drop_object
        self._on_import_data = on_import_data
        self._on_new_object = on_new_object
        self._on_duplicate_table = on_duplicate_table
        self._on_new_from_template = on_new_from_template
        self._on_extension_action = on_extension_action
        self._on_mcp_server = on_mcp_server
        self._on_manage_users = on_manage_users
        self._on_monitor = on_monitor
        self._on_data_search = on_data_search
        self._on_pragmas = on_pragmas
        self._on_open_window = on_open_window
        self._on_open_properties = on_open_properties
        self._on_open_properties_window = on_open_properties_window
        self._on_open_schema = on_open_schema
        self._on_edit_connection = on_edit_connection
        self._on_disconnect = on_disconnect
        self._on_close_tabs = on_close_tabs
        self._count_tabs = count_tabs
        self._on_remove_connection = on_remove_connection
        self._show_error = show_error
        # Currently bound status dot per connection name, so
        # set_connected() can restyle a visible row.
        self._dots: dict[str, Gtk.Box] = {}
        # Connection node behind each bound + button (rows are
        # recycled, so the handler resolves the node through this
        # rather than through a captured reference).
        self._new_buttons: dict[Gtk.Widget, Node] = {}

        # Following the active tab (CORE-55): the object the window
        # last reported, the debounce timer resolving it, the store
        # whose load the walk is waiting on, and the tree rows GTK has
        # bound — which is what "already on screen" means when deciding
        # whether the reveal may move the scroll.
        # The expansion toggle a single click has asked for, still
        # waiting to see whether a second press turns it into a double
        # click (CORE-58): the timer and the row it would toggle.
        self._toggle_source = 0
        self._toggle_row: Gtk.TreeListRow | None = None
        self._follow_target: tuple[str, str, str] | None = None
        self._follow_source = 0
        self._follow_wait: tuple[Gio.ListStore, int] | None = None
        self._bound_rows: set[Gtk.TreeListRow] = set()

        # Search mode: the tree expansion to put back on the way out.
        self._filtering = False
        self._saved_expansion: frozenset[str] = frozenset()

        self._roots = Gio.ListStore(item_type=Node)
        self._tree = Gtk.TreeListModel.new(
            self._roots,
            passthrough=False,
            autoexpand=False,
            create_func=self._create_children,
        )
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._setup_row)
        factory.connect("bind", self._bind_row)
        factory.connect("unbind", self._unbind_row)
        # autoselect off, unselecting allowed: "no row is current" has
        # to be a state the tree can be in, because a tab with no
        # object behind it clears the highlight (CORE-55) — with
        # autoselect the first row would quietly take its place.
        self._view = Gtk.ListView(
            model=Gtk.SingleSelection(
                model=self._tree, autoselect=False, can_unselect=True
            ),
            factory=factory,
        )
        self._view.add_css_class("navigation-sidebar")
        self._view.add_css_class("schema-tree")
        self._view.connect("activate", self._on_activate)

        add_connection = Gtk.Button(
            label=_("Add Connection"), halign=Gtk.Align.CENTER
        )
        add_connection.add_css_class("suggested-action")
        add_connection.add_css_class("pill")
        add_connection.connect("clicked", lambda *_: on_add_connection())
        self._empty_page = Adw.StatusPage(
            icon_name="network-server-symbolic",
            title=_("No connections yet"),
            description="A workspace holds the databases you work on "
            "together. Add the first one to browse its tables.",
            child=add_connection,
        )
        self.set_child(self._empty_page)

        # Context menu (right-click on a table/view or connection row).
        self._menu_node: Node | None = None
        self._actions = actions = Gio.SimpleActionGroup()
        for name, callback in (
            ("open", self._menu_open),
            ("open-window", self._menu_open_window),
            ("properties", self._menu_properties),
            ("properties-window", self._menu_properties_window),
            ("object-info", self._menu_object_info),
            ("view-data", self._menu_view_data),
            ("view-section", self._menu_view_section),
            ("query-console", self._menu_query_console),
            ("cli-console", self._menu_cli_console),
            ("definition", self._menu_definition),
            ("edit-table", self._menu_edit_table),
            ("duplicate-table", self._menu_duplicate_table),
            ("edit-function", self._menu_edit_function),
            ("relation-graph", self._menu_relation_graph),
            ("view-indexes", self._menu_view_indexes),
            ("query-builder", self._menu_query_builder),
            ("import-data", self._menu_import_data),
            ("drop-object", self._menu_drop),
            ("install-extension", self._menu_install_extension),
            ("update-extension", self._menu_update_extension),
            ("drop-extension", self._menu_drop_extension),
            ("refresh", self._menu_refresh),
            ("mcp-server", self._menu_mcp_server),
            ("manage-users", self._menu_manage_users),
            ("monitor", self._menu_monitor),
            ("data-search", self._menu_data_search),
            ("pragmas", self._menu_pragmas),
            ("open-schema", self._menu_open_schema),
            ("edit-connection", self._menu_edit_connection),
            ("disconnect", self._menu_disconnect),
            ("close-tabs", self._menu_close_tabs),
            ("remove-connection", self._menu_remove_connection),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            actions.add_action(action)
        new_object = Gio.SimpleAction.new(
            "new-object", GLib.VariantType.new("s")
        )
        new_object.connect("activate", self._menu_new_object)
        actions.add_action(new_object)
        # One action for every saved template, the name as its target,
        # so the submenu is rebuilt per popup and no action has to be
        # added or removed as templates come and go (CORE-29).
        new_template = Gio.SimpleAction.new(
            "new-from-template", GLib.VariantType.new("s")
        )
        new_template.connect("activate", self._menu_new_from_template)
        actions.add_action(new_template)
        self._view.insert_action_group("schema", actions)

        self._popover = Gtk.PopoverMenu.new_from_model(Gio.Menu())
        self._popover.set_parent(self._view)
        self._popover.set_has_arrow(False)
        self._popover.add_css_class("schema-popover")
        self._view.connect("destroy", lambda *_: self._popover.unparent())

        # Hiding or showing the server's own schemas changes what a
        # database row lists, so a flip of the setting reloads the
        # rows that are already there (PG-03). The listener outlives
        # nothing: it goes when the sidebar's view does.
        self._show_system = settings_store.settings.show_system_schemas
        self._follow_enabled = settings_store.settings.sidebar_follow_active_tab
        settings_store.subscribe(self._settings_changed)
        self._view.connect(
            "destroy",
            lambda *_: settings_store.unsubscribe(self._settings_changed),
        )

    def _settings_changed(self, settings) -> None:
        """Re-list the schemas when the system-schema setting flips;
        every other setting leaves the tree alone."""
        self._follow_enabled = settings.sidebar_follow_active_tab
        if not self._follow_enabled:
            self._cancel_follow()
        if settings.show_system_schemas == self._show_system:
            return
        self._show_system = settings.show_system_schemas
        for node in _items(self._roots):
            self._reload_schema_lists(node)

    def _reload_schema_lists(self, node: Node) -> None:
        """Refetch the rows of every node that lists schemas — a
        database on PostgreSQL, a connection on an engine with no
        database level — and leave the rest of the tree, and every
        connection never opened, alone."""
        if not node.loaded:
            return
        if any(child.kind == "schema" for child in _items(node.store)):
            self.refresh_node(node)
            return
        for child in list(_items(node.store)):
            self._reload_schema_lists(child)

    def add_profile(self, profile: ConnectionProfile) -> None:
        self._roots.append(
            Node("connection", profile.name, detail=profile.kind, profile=profile)
        )
        self._refresh_empty_state()

    def remove_profile(self, name: str) -> None:
        """Drop a connection's root row — after it's removed from the
        workspace, or as the first half of an edit (re-added fresh via
        add_profile so renamed/re-kinded connections get a clean
        reload instead of a stale cached schema)."""
        self._dots.pop(name, None)
        for i in range(self._roots.get_n_items()):
            if self._roots.get_item(i).label == name:
                self._roots.remove(i)
                break
        self._refresh_empty_state()

    def _refresh_empty_state(self) -> None:
        """An empty tree is a blank panel; say what to do instead."""
        self.set_child(
            self._view if self._roots.get_n_items() else self._empty_page
        )

    def set_connected(self, name: str, connected: bool) -> None:
        """Flip a connection row's status dot (main thread only; the
        window marshals here with GLib.idle_add from worker threads)."""
        for i in range(self._roots.get_n_items()):
            node = self._roots.get_item(i)
            if node.label == name:
                node.connected = connected
                break
        dot = self._dots.get(name)
        if dot is not None:
            _style_dot(dot, connected)

    def set_filter(
        self, text: str, scopes: frozenset[str] = frozenset()
    ) -> None:
        """Search mode: keep the tree's shape but show only the rows
        whose names match, each under its own ancestors, with the
        matched letters bold. `scopes` narrows the hunt to object kinds
        (see frontend/tree_search.py); the empty set is "All".

        Only what a connection has already loaded is searched — the
        sidebar never connects behind the user's back — and the tree
        expansion of the moment search opened is remembered, so leaving
        search puts the sidebar back exactly as it was."""
        text = text.strip()
        if not text:
            self.clear_filter()
            return
        if not self._filtering:
            self._saved_expansion = self._expansion_state()
            self._filtering = True
        roots = Gio.ListStore(item_type=Node)
        for conn in _items(self._roots):
            clone = self._filter_node(conn, text, scopes)
            if clone is not None:
                roots.append(clone)
        if not roots.get_n_items():
            roots.append(Node("note", "(no matches in loaded connections)"))
        filtered = Gtk.TreeListModel.new(
            roots,
            passthrough=False,
            autoexpand=True,  # a match is no use hidden under its parents
            create_func=_filtered_children,
        )
        self._view.set_model(Gtk.SingleSelection(model=filtered))

    def clear_filter(self) -> None:
        """Leave search mode: the real tree comes back, expanded the
        way the user left it."""
        self._view.set_model(Gtk.SingleSelection(model=self._tree))
        if not self._filtering:
            return
        self._filtering = False
        expansion, self._saved_expansion = self._saved_expansion, frozenset()
        self._restore_expansion(expansion)

    def _filter_node(
        self, node: Node, query: str, scopes: frozenset[str]
    ) -> Node | None:
        """A clone of `node` holding its matching descendants, or None
        when neither it nor anything under it matches. Ancestors of a
        match are kept even when they don't match themselves — a bare
        column name says nothing without the table above it."""
        if node.system and tree_search.SYSTEM_SCOPE not in scopes:
            # The server's own schemas are not what a search is for
            # unless the filter says so, and skipping the schema skips
            # everything under it in one go (PG-03).
            return None
        children = Gio.ListStore(item_type=Node)
        for child in self._search_children(node):
            clone = self._filter_node(child, query, scopes)
            if clone is not None:
                children.append(clone)
        hit = (
            tree_search.match(query, node.label)
            if tree_search.in_scope(node.kind, scopes, system=node.system)
            else None
        )
        if hit is None and not children.get_n_items():
            return None
        clone = _clone(node)
        clone.search_ranges = hit[1] if hit is not None else ()
        clone.store = children
        _adopt(clone)
        return clone

    def _search_children(self, node: Node):
        """What search sees under a row: the children it has loaded,
        or — for a Tables/Views category never expanded — the objects
        the row above it already fetched into the payload."""
        if node.kind not in _EXPANDABLE:
            return
        loaded = [child for child in _items(node.store) if child.kind != "note"]
        if loaded:
            yield from loaded
            return
        if node.kind == "category" and node.category in objects.RELATION_FOLDERS:
            kind = _relation_kind(node.category)
            for info in _category_rows(node):
                yield Node(
                    kind, info.name,
                    detail=getattr(info, "detail", ""),
                    profile=node.profile,
                )

    def _expansion_state(self) -> frozenset[str]:
        """Which rows are open right now, by path, so the set survives
        the model being swapped out from under them."""
        return frozenset(
            _node_path(row.get_item())
            for row in _rows(self._tree)
            if row.get_expanded()
        )

    def _restore_expansion(self, paths: frozenset[str]) -> None:
        # Expanding a row reveals more rows, which may themselves need
        # expanding, so the walk re-reads the model's length as it goes.
        index = 0
        while index < self._tree.get_n_items():
            row = self._tree.get_row(index)
            if (
                row is not None
                and not row.get_expanded()
                and _node_path(row.get_item()) in paths
            ):
                row.set_expanded(True)
            index += 1

    def expand_profile(self, name: str) -> None:
        """Expand (and thereby connect/load) the row for a profile."""
        for i in range(self._roots.get_n_items()):
            node = self._roots.get_item(i)
            if node.label == name:
                row = self._tree.get_child_row(i)
                if row is not None:
                    row.set_expanded(True)
                return

    def reload_connection(self, name: str) -> None:
        """Drop and re-create a connection node's children, so
        create/drop results appear (also the connection menu's
        Refresh). A collapsed node just forgets its stale children;
        an expanded one reloads immediately."""
        for i in range(self._roots.get_n_items()):
            node = self._roots.get_item(i)
            if node.label != name:
                continue
            node.loaded = False
            node.loading = False
            self._mark_catalog_stale(name)
            if node.store is not None:
                node.store.remove_all()
            row = self._tree.get_child_row(i)
            if row is not None and row.get_expanded():
                self._load_children(node)
            return

    def reload_all(self) -> None:
        """Refetch every connection's schema (the sidebar header's
        refresh button). Connections that were never expanded just drop
        their cache; nothing reconnects behind the user's back."""
        for i in range(self._roots.get_n_items()):
            self.reload_connection(self._roots.get_item(i).label)

    def _connector(self, profile) -> Connector:
        """The connection for `profile`, with any Refresh the user
        asked for since the last one applied to its catalog cache.

        Called from worker threads only, like _ensure itself: dropping
        the cache eagerly in refresh_node() would mean connecting on
        the main loop just to invalidate a cache that may not exist.
        """
        connector = self._ensure(profile)
        if profile.name in self._stale_catalogs:
            self._stale_catalogs.discard(profile.name)
            connector.invalidate_catalog()
        return connector

    def _mark_catalog_stale(self, name: str) -> None:
        """Note that `name`'s cached catalog is not to be trusted after
        this — the user pressed Refresh."""
        self._stale_catalogs.add(name)

    def refresh_node(self, node: Node) -> None:
        """Refetch one row's children: the connection, one category, or
        a table's columns. An expanded row reloads immediately, a
        collapsed one just forgets what it cached."""
        if node.kind == "connection":
            self.reload_connection(node.label)
            return
        if node.kind == "category" and node.category in ("tables", "views"):
            # Both are filled from the row above them, out of one
            # list_tables() call, so only reloading that row refetches
            # them.
            parent = node.parent or self._root_node(node.profile)
            if parent is not None and parent.kind == "database":
                self.refresh_node(parent)
            elif parent is not None:
                self.reload_connection(parent.label)
            return
        if node.kind not in _EXPANDABLE:
            # Leaves (a function, an index) carry no children of their
            # own; refreshing one means refetching the list it came from.
            if node.parent is not None:
                self.refresh_node(node.parent)
            return
        node.loaded = False
        node.loading = False
        if node.profile is not None:
            self._mark_catalog_stale(node.profile.name)
        node.ddl = None  # the hover DDL is stale too
        if node.store is not None:
            node.store.remove_all()
        row = self._row_for(node)
        if row is not None and row.get_expanded():
            self._load_children(node)

    def _row_for(self, node: Node) -> Gtk.TreeListRow | None:
        """The tree row showing `node`, if it is currently on screen —
        the model only exposes rows under expanded parents, which is
        exactly the set worth reloading eagerly."""
        for i in range(self._tree.get_n_items()):
            row = self._tree.get_row(i)
            if row is not None and row.get_item() is node:
                return row
        return None

    def _root_node(self, profile: ConnectionProfile | None) -> Node | None:
        """The connection row a (possibly nested) node belongs to —
        the keeper of the adapter's capability flags."""
        if profile is None:
            return None
        for i in range(self._roots.get_n_items()):
            node = self._roots.get_item(i)
            if (
                node.profile is profile
                or node.label == profile.name
                # A database row's profile is a copy named
                # "connection · database" (see _database_profile); the
                # capability flags still live on the connection row.
                or profile.name.startswith(node.label + _DATABASE_SEPARATOR)
            ):
                return node
        return None

    # Tree model

    def _create_children(self, node: Node) -> Gio.ListStore | None:
        # Called both on expansion and by is_expandable probes: no I/O
        # here, just the cached store (see module docstring).
        if node.kind not in _EXPANDABLE:
            return None
        if node.kind == "section" and (
            node.category not in _SECTION_CHILD_KINDS
        ):
            # A section like Constraints or Policies has no rows of its
            # own in the tree: it is a leaf that opens the Properties
            # view on itself.
            return None
        if node.store is None:
            node.store = Gio.ListStore(item_type=Node)
            if (
                node.kind == "category"
                and node.category not in _LAZY_CATEGORIES
            ):
                self._fill_category(node)
            elif node.kind in ("table", "view"):
                self._fill_sections(node)
        return node.store

    def _fill_category(self, node: Node) -> None:
        """A folder whose rows need no query of its own: the relation
        folders, which share the one list_tables() call the row above
        made, and Administer, which holds folders rather than objects
        (PG-02). Filling is synchronous either way."""
        if node.category == "administer":
            for slug, label in _administer_categories(node.profile):
                node.store.append(Node(
                    "category", label,
                    profile=node.profile, category=slug,
                ))
            if not node.store.get_n_items():
                node.store.append(Node("note", "(nothing to administer)"))
            _adopt(node)
            node.loaded = True
            return
        kind = _relation_kind(node.category)
        rows = _category_rows(node)
        engine = node.profile.kind if node.profile is not None else ""
        for info in rows:
            node.store.append(Node(
                kind, info.name,
                # A partitioned table says so next to its name: it is
                # still a table, and the note is what tells it from a
                # plain one without a second icon vocabulary (PG-02).
                detail=getattr(info, "detail", ""),
                profile=node.profile,
                # An object the engine owns rather than the user —
                # SQLite's sqlite_* tables, which share the one
                # namespace there is — is dimmed like a system schema
                # (SQ-01, PG-03).
                system=node.system or _is_system_object(engine, info.name),
            ))
        if not rows:
            node.store.append(Node("note", "(none)"))
        _adopt(node)
        node.loaded = True

    def _fill_sections(self, node: Node) -> None:
        """A table's children: the Properties sections this engine has
        (CORE-05), so every row under a table maps to one section of
        its properties — and the ones that hold objects (Columns,
        Indexes, Triggers) still expand into them.

        Opening such a row opens that listing as a tab of its own
        (CORE-56); "Open in Properties" is what still sends it to the
        side panel.

        Which sections exist is a capability question the provider layer
        already answers without a connection, so filling is
        synchronous; the rows inside a section are fetched when it is
        expanded.
        """
        kind = node.profile.kind if node.profile is not None else ""
        try:
            sections = registry.property_sections(kind)
        except Exception:  # an adapter the registry doesn't know
            sections = ()
        for slug, label in objects.PROPERTY_SECTIONS:
            if slug not in sections or slug in _NON_LIST_SECTIONS:
                continue
            node.store.append(Node(
                "section", label,
                profile=node.profile, category=slug, table=node.label,
            ))
        if not node.store.get_n_items():
            node.store.append(Node("note", "(no properties)"))
        _adopt(node)
        node.loaded = True

    def _on_row_expanded(
        self, row: Gtk.TreeListRow, _pspec, list_item: Gtk.ListItem
    ) -> None:
        _set_caret(list_item.caret, row.get_expanded())
        if row.get_expanded():
            self._load_children(row.get_item())

    def _load_children(self, node: Node) -> None:
        if node.kind not in _EXPANDABLE or node.loaded or node.loading:
            return
        if node.kind == "category" and node.category not in _LAZY_CATEGORIES:
            # Payload-filled (Tables/Views): nothing to fetch, the row
            # above already did it.
            if node.store is None:
                node.store = Gio.ListStore(item_type=Node)
            self._fill_category(node)
            return
        if node.kind in ("table", "view"):
            # The sections are a capability answer, not a query.
            if node.store is None:
                node.store = Gio.ListStore(item_type=Node)
            node.store.remove_all()
            self._fill_sections(node)
            return
        if node.kind == "section" and (
            node.category not in _SECTION_CHILD_KINDS
        ):
            return
        if node.store is None:
            node.store = Gio.ListStore(item_type=Node)
        node.loading = True
        store = node.store
        store.remove_all()
        store.append(Node("note", "Loading…"))

        if node.kind in ("connection", "database", "schema"):
            # On a server that hosts several databases the connection
            # row lists them and stops there: its children are
            # databases, and the object categories belong to each of
            # those (see the module docstring). Where list_databases()
            # comes back empty — SQLite, JDBC — one connection is one
            # database and the categories sit at the root instead.
            #
            # A database row on an engine with schemas stops one level
            # short too, for the same reason: its objects belong to a
            # schema, not to the database (PG-01). A schema row is
            # where the categories finally hang, so it never lists
            # either.
            root = node.kind == "connection"
            wants_schemas = node.kind != "schema" and _has_schemas(node.profile)

            def work():
                connector = self._connector(node.profile)
                databases = (
                    # The server's own databases come back too and are
                    # filtered (or not) below, exactly as its own
                    # schemas are: in MySQL a schema *is* a database
                    # (MY-01, PG-03).
                    connector.list_databases(include_system=True)
                    if root
                    else []
                )
                schemas = (
                    # The server's own schemas come back too and are
                    # filtered (or not) below: whether they are shown
                    # is a setting, and toggling it must not need a
                    # reconnect (PG-03).
                    connector.catalog_schemas(include_system=True)
                    if wants_schemas and not databases
                    else []
                )
                current_schema = (
                    connector.current_schema() if schemas else ""
                )
                return (
                    [] if databases or schemas
                    else connector.catalog_tables(),
                    connector.ddl_kinds(),
                    connector.supports_drop,
                    databases,
                    schemas,
                    current_schema,
                )

            def fill(loaded):
                (
                    relations, kinds, supports_drop,
                    databases, schemas, current_schema,
                ) = loaded
                node.ddl_kinds = kinds
                node.supports_drop = supports_drop
                # The folders this level shows beside the rows under it
                # — Administer under a connection, Extensions under a
                # database, Sequences under a schema — are the engine's
                # declaration (registry.level_categories, PG-02), so the
                # tree grows them without naming an engine here.
                folders = _level_categories(node.profile, node.kind)
                if databases:
                    current = node.profile.database
                    kind = node.profile.kind if node.profile else ""
                    show_system = settings_store.settings.show_system_schemas
                    for name in sorted(
                        databases,
                        key=lambda n: (
                            _is_system_database(kind, n), n != current, n
                        ),
                    ):
                        system = _is_system_database(kind, name)
                        if system and not show_system:
                            continue
                        store.append(Node(
                            "database", name,
                            detail="current" if name == current else "",
                            profile=_database_profile(node.profile, name),
                            system=system,
                        ))
                    _append_folders(store, node, folders, relations)
                    return
                if schemas:
                    # The one bare names already resolve in comes first
                    # and says so: it is the schema every unqualified
                    # reference in a console on this row will hit. The
                    # server's own schemas come last, and only when
                    # they are wanted (PG-03).
                    kind = node.profile.kind if node.profile else ""
                    show_system = settings_store.settings.show_system_schemas
                    for name in sorted(
                        schemas,
                        key=lambda n: (
                            _is_system_schema(kind, n), n != current_schema, n
                        ),
                    ):
                        system = _is_system_schema(kind, name)
                        if system and not show_system:
                            continue
                        store.append(Node(
                            "schema", name,
                            detail=(
                                "current" if name == current_schema else ""
                            ),
                            profile=schema_profile(node.profile, name),
                            system=system,
                        ))
                    _append_folders(store, node, folders, relations)
                    return
                if folders:
                    # An engine that declares this level's folders gets
                    # exactly those, in its own order: Foreign Tables
                    # belongs next to Tables, not after everything the
                    # generic list happens to know about.
                    _append_folders(store, node, folders, relations)
                    return
                tables = [t for t in relations if t.kind != "view"]
                views = [t for t in relations if t.kind == "view"]
                store.append(Node(
                    "category", "Tables",
                    profile=node.profile, category="tables", payload=tables,
                ))
                store.append(Node(
                    "category", "Views",
                    profile=node.profile, category="views", payload=views,
                ))
                store.append(Node(
                    "category", "Functions",
                    profile=node.profile, category="functions",
                ))
                # Browse-to-drop categories, only where the adapter can
                # actually drop the kind.
                if supports_drop:
                    for category, kind in (
                        ("indexes", "index"),
                        ("triggers", "trigger"),
                        ("events", "event"),
                    ):
                        if kind in kinds:
                            store.append(Node(
                                "category", category.capitalize(),
                                profile=node.profile, category=category,
                            ))
        elif node.kind == "category":  # the lazy categories
            slug = node.category
            schema = node.profile.schema if node.profile is not None else ""

            def work():
                connector = self._connector(node.profile)
                if slug in ("functions", "procedures"):
                    # Two folders where the engine has two kinds of
                    # routine, one listing where it has one (MY-01).
                    return connector.list_routines(_LAZY_CATEGORIES[slug])
                if slug == "indexes":
                    return connector.list_indexes()
                if slug == "triggers":
                    return connector.list_triggers()
                if slug == "events":
                    return connector.list_events()
                if slug in ("roles", "users"):
                    # The same accounts under whichever name the engine
                    # gives the folder (MY-01).
                    return connector.list_users()
                # A catalog folder: one listing, named by its slug
                # (PG-02). An adapter with no such folder answers
                # empty and the row shows its empty state.
                return connector.list_catalog(slug, schema)

            def fill(found):
                kind = _LAZY_CATEGORIES[slug]
                for obj in found:
                    table = ""  # only index/trigger rows own one
                    if slug == "events":  # plain names
                        name, detail = obj, ""
                    elif slug in ("functions", "procedures"):
                        name, detail = obj.name, ""
                    elif slug in ("indexes", "triggers"):
                        name, table = obj.name, obj.table
                        detail = table
                    else:  # ObjectSummary | UserInfo
                        name = obj.name
                        detail = getattr(obj, "detail", "")
                    store.append(Node(
                        kind, name,
                        detail=detail, profile=node.profile, table=table,
                        category=slug,
                    ))
                if not found:
                    store.append(Node("note", "(none)"))
        else:  # section: the objects of one Properties section
            table = node.table
            slug = node.category

            def work():
                connector = self._connector(node.profile)
                if slug == "columns":
                    return connector.catalog_columns(table)
                if slug == "indexes":
                    return [
                        index for index in connector.list_indexes()
                        if index.table == table
                    ]
                if slug == "partitions":
                    # The pieces of a partitioned table, nested under
                    # the table they belong to (PG-02); a plain table
                    # answers empty and the section says so.
                    return connector.list_partitions(table)
                return [
                    trigger for trigger in connector.list_triggers()
                    if trigger.table == table
                ]

            def fill(found):
                for item in found:
                    if slug == "columns":
                        store.append(Node(
                            "column", item.name,
                            detail=item.type, is_pk=item.is_pk,
                            # Carried so the column row can open its own
                            # info view: a column is only identifiable
                            # together with the table it belongs to.
                            profile=node.profile, table=table,
                        ))
                    else:
                        store.append(Node(
                            _SECTION_CHILD_KINDS[slug], item.name,
                            detail=getattr(item, "detail", ""),
                            profile=node.profile, table=table,
                        ))
                if not found:
                    store.append(Node("note", f"(no {slug})"))

        def done(result):
            node.loading = False
            node.loaded = True
            store.remove_all()
            fill(result)
            _adopt(node)

        def failed(exc):
            node.loading = False  # loaded stays False: re-expand retries
            store.remove_all()
            store.append(Node("note", "(failed to load)"))
            self._show_error(str(exc))

        run_async(work, done, failed)

    # Rows

    def _setup_row(self, _factory, list_item: Gtk.ListItem) -> None:
        # The connection's identity colour runs down the leading edge of
        # its row and of every row under it; the connection name at the
        # top of that block is the non-colour cue.
        row_box = Gtk.Box(spacing=0)
        identity_bar = identity_ui.bar(identity.NONE)
        row_box.append(identity_bar)
        expander = Gtk.TreeExpander(hexpand=True)
        # The caret lives at the end of the row instead (icons stay
        # aligned at the left edge).
        expander.set_hide_expander(True)
        expander.set_indent_for_icon(False)
        expander.set_has_tooltip(True)
        expander.connect("query-tooltip", self._query_tooltip, list_item)
        box = Gtk.Box(spacing=6)
        dot = Gtk.Box(width_request=8, height_request=8)
        dot.set_valign(Gtk.Align.CENTER)
        dot.add_css_class("conn-dot")
        dot.set_visible(False)
        icon = Gtk.Image()
        icon.set_visible(False)
        label = Gtk.Label(xalign=0, hexpand=True)
        pk = Gtk.Label(label=_("PK"))
        pk.add_css_class("caption")
        pk.add_css_class("accent")
        badge = identity_ui.environment_badge(identity.UNSET)
        badge.set_valign(Gtk.Align.CENTER)
        detail = Gtk.Label(hexpand=False)
        detail.add_css_class("dim-label")
        detail.add_css_class("caption")
        # The name label above expands and is never ellipsized, so it
        # keeps its full width request; this one ellipsizes and asks
        # for almost nothing, so the box takes the room back here
        # first. Indentation is the expander's business, so a deeply
        # nested row yields exactly the same way a top-level one does.
        detail.set_ellipsize(Pango.EllipsizeMode.END)
        detail.set_max_width_chars(_DETAIL_MAX_CHARS)
        detail.set_width_chars(0)
        new_button = Gtk.Button(icon_name="list-add-symbolic")
        new_button.add_css_class("flat")
        new_button.add_css_class("row-action")
        new_button.set_valign(Gtk.Align.CENTER)
        new_button.set_visible(False)
        new_button.connect("clicked", self._new_pressed)
        caret = Gtk.Image(icon_name="pan-end-symbolic")
        caret.add_css_class("dim-label")
        caret.set_visible(False)
        # Expands table/view columns without opening a data tab (row
        # activation), so the two gestures stay distinct.
        caret_click = Gtk.GestureClick()
        caret_click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        caret_click.connect("pressed", self._caret_pressed, list_item)
        caret.add_controller(caret_click)
        # A single left click toggles the row's expansion (CORE-52).
        # Bubble phase and never claimed: the caret's own gesture (and
        # the + button) get the press first, and the ListView still
        # selects the row and still sees the double click behind it.
        row_click = Gtk.GestureClick(button=Gdk.BUTTON_PRIMARY)
        row_click.connect("pressed", self._row_pressed, list_item)
        expander.add_controller(row_click)
        menu_click = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        menu_click.connect("pressed", self._row_menu_pressed, list_item)
        expander.add_controller(menu_click)
        for child in (dot, icon, label, pk, badge, detail, new_button, caret):
            box.append(child)
        expander.set_child(box)
        row_box.append(expander)
        list_item.set_child(row_box)
        # The row's content minus the identity bar: what a system row
        # dims, so the connection colour keeps its full strength.
        list_item.content = expander
        list_item.identity_bar = identity_bar
        list_item.dot = dot
        list_item.dot_name = ""
        list_item.icon = icon
        list_item.label = label
        list_item.pk = pk
        list_item.badge = badge
        list_item.detail = detail
        list_item.new_button = new_button
        list_item.caret = caret
        list_item.row_handler = 0

    def _bind_row(self, _factory, list_item: Gtk.ListItem) -> None:
        row = list_item.get_item()  # TreeListRow (passthrough=False)
        node = row.get_item()
        list_item.get_child().get_last_child().set_list_row(row)
        profile = _row_profile(row)
        identity_ui.set_color(
            list_item.identity_bar,
            profile.color if profile is not None else identity.NONE,
        )
        if node.kind == "connection":
            _style_dot(list_item.dot, node.connected)
            list_item.dot.set_visible(True)
            list_item.dot_name = node.label
            self._dots[node.label] = list_item.dot
            identity_ui.set_environment(
                list_item.badge,
                profile.environment if profile is not None else identity.UNSET,
            )
            self._new_buttons[list_item.new_button] = node
            list_item.new_button.set_visible(True)
            describe(
                list_item.new_button, f"New object in “{node.label}”"
            )
        else:
            list_item.dot.set_visible(False)
            list_item.badge.set_visible(False)
            self._new_buttons.pop(list_item.new_button, None)
            list_item.new_button.set_visible(False)
        icon_name = _KIND_ICONS.get(node.kind)
        if icon_name:
            list_item.icon.set_from_icon_name(icon_name)
        list_item.icon.set_visible(bool(icon_name))
        if node.search_ranges:
            list_item.label.set_markup(
                tree_search.highlight(node.label, node.search_ranges)
            )
        else:
            list_item.label.set_text(node.label)
        if node.kind == "note":
            list_item.label.add_css_class("dim-label")
        else:
            list_item.label.remove_css_class("dim-label")
        # Dimmed, not disabled: a system row is quieter than the rest
        # and behaves exactly like it (PG-03). Rows are recycled, so
        # the class is set on every bind either way.
        if node.system:
            list_item.content.add_css_class("system-row")
        else:
            list_item.content.remove_css_class("system-row")
        list_item.pk.set_visible(node.is_pk)
        _show_detail(list_item.detail, node.detail)
        list_item.caret.set_visible(
            node.kind in _EXPANDABLE
            and (not node.filtered or bool(node.store.get_n_items()))
        )
        _set_caret(list_item.caret, row.get_expanded())
        self._bound_rows.add(row)
        list_item.row_handler = row.connect(
            "notify::expanded", self._on_row_expanded, list_item
        )
        if row.get_expanded():  # expanded while unbound (e.g. scrolled away)
            self._load_children(node)

    def _unbind_row(self, _factory, list_item: Gtk.ListItem) -> None:
        self._bound_rows.discard(list_item.get_item())
        if list_item.row_handler:
            list_item.get_item().disconnect(list_item.row_handler)
            list_item.row_handler = 0
        self._new_buttons.pop(list_item.new_button, None)
        if list_item.dot_name:
            if self._dots.get(list_item.dot_name) is list_item.dot:
                del self._dots[list_item.dot_name]
            list_item.dot_name = ""

    # Context menu

    def _menu_for(self, node: Node) -> Gio.Menu | None:
        """The context menu for a row, built per popup: which items
        appear depends on the connection's capabilities (ddl_kinds and
        supports_drop, known once its schema loaded)."""
        root = self._root_node(node.profile)
        can_drop = root is not None and root.supports_drop
        menu = self._menu_body(node, root, can_drop)
        if menu is None:
            return None
        if not self.is_openable(node):
            # Nothing to open — a placeholder row, or one with no
            # connection behind it — so the two items are left off
            # rather than shown dead (CORE-52).
            return menu
        # Open and Open (Window) sit above everything else, in the
        # same flat list as the rest — prepended in reverse so Open
        # ends up first.
        if self.properties_target(node) is not None:
            menu.prepend("Properties (Window)", "schema.properties-window")
            menu.prepend("Properties", "schema.properties")
        menu.prepend("Open (Window)", "schema.open-window")
        menu.prepend("Open", "schema.open")
        return menu

    def _menu_body(
        self, node: Node, root: Node | None, can_drop: bool
    ) -> Gio.Menu | None:
        """The kind-specific half of a row's menu, under Open."""
        if node.kind in ("table", "view"):
            menu = Gio.Menu()
            menu.append("View Data", "schema.view-data")
            menu.append("Object Info", "schema.object-info")
            menu.append("Query Console", "schema.query-console")
            if node.kind == "table":
                # The designer, on this table: columns, constraints and
                # indexes as a form, applied as a diff (CORE-26).
                menu.append("Edit Table…", "schema.edit-table")
                # A new table shaped like this one: the same columns,
                # constraints and indexes in a designer, under a new
                # name (CORE-29). Structure only — data is transfer's
                # job, not the designer's.
                menu.append("Duplicate Structure…", "schema.duplicate-table")
            menu.append("Table Definition", "schema.definition")
            menu.append("Query Builder", "schema.query-builder")
            # A view is not something rows can be inserted into here,
            # so the file half of the menu belongs to tables only.
            if node.kind == "table":
                menu.append("Import Data…", "schema.import-data")
            menu.append("Refresh", "schema.refresh")
            if can_drop:
                menu.append("Drop…", "schema.drop-object")
            return menu
        if node.kind == "section":
            menu = Gio.Menu()
            menu.append("Open in Properties", "schema.view-section")
            menu.append("View Data", "schema.view-data")
            menu.append("Refresh", "schema.refresh")
            return menu
        if node.kind == "category":
            menu = Gio.Menu()
            menu.append("Object Info", "schema.object-info")
            menu.append("Refresh", "schema.refresh")
            if node.category in ("tables", "views") and root is not None:
                menu.append_submenu(
                    "New", _new_items(root.ddl_kinds or _DEFAULT_NEW_KINDS)
                )
            if node.category == "indexes" and node.profile is not None:
                menu.append("View All…", "schema.view-indexes")
            return menu
        if node.kind in ("connection", "database", "schema"):
            menu = Gio.Menu()
            menu.append("Object Info", "schema.object-info")
            menu.append("New Query Console", "schema.query-console")
            menu.append("New CLI Client", "schema.cli-console")
            menu.append("Relation Graph", "schema.relation-graph")
            menu.append("Query Builder", "schema.query-builder")
            menu.append("MCP Server", "schema.mcp-server")
            # The value search reads the tables this row's scope
            # reaches, so it sits on every level that has tables under
            # it — connection, database and schema alike (CORE-45).
            menu.append("Find Data…", "schema.data-search")
            menu.append("Open Schema", "schema.open-schema")
            menu.append_submenu(
                "New", _new_items(node.ddl_kinds or _DEFAULT_NEW_KINDS)
            )
            menu.append("Refresh", "schema.refresh")
            if node.kind == "connection":
                # Accounts belong to the server and the profile is the
                # workspace's, so both stop at the connection row: a
                # database row is a view onto the same server.
                menu.append("Users & Permissions…", "schema.manage-users")
                # Sessions, throughput and storage are the server's, so
                # monitoring stops at the connection row too — and only
                # for the engines that have a server to report on.
                if metrics.supported(node.profile.kind if node.profile else ""):
                    menu.append("Monitoring…", "schema.monitor")
                # The engine's own settings surface, where it has one:
                # SQLite's PRAGMAs (SQ-02). A capability answer, so no
                # engine is named here.
                if node.profile is not None and registry.capabilities(
                    node.profile.kind
                ).pragmas:
                    menu.append("PRAGMAs…", "schema.pragmas")
                menu.append("Edit…", "schema.edit-connection")
                # Always listed, so the menu keeps its shape; only live
                # while the connection actually has something open.
                menu.append("Disconnect", "schema.disconnect")
                # The count is part of the label: what "all related
                # tabs" means is exactly the number the window would
                # close, and zero of them leaves the item dead.
                open_tabs = self._count_tabs(node.label)
                menu.append(
                    f"Close all {open_tabs} related tabs"
                    if open_tabs != 1
                    else "Close the 1 related tab",
                    "schema.close-tabs",
                )
                menu.append("Remove…", "schema.remove-connection")
            return menu
        if node.kind == "function" and node.profile is not None:
            menu = Gio.Menu()
            menu.append("Object Info", "schema.object-info")
            # A function is editable and droppable only where the
            # engine has stored functions at all. SQLite's are built
            # into the library or registered by the process: they list
            # and they open, and there is nothing to edit or drop
            # (SQ-01).
            if "function" in root.ddl_kinds:
                menu.append("Edit Definition", "schema.edit-function")
            menu.append("Refresh", "schema.refresh")
            if can_drop and "function" in root.ddl_kinds:
                menu.append("Drop…", "schema.drop-object")
            return menu
        if node.kind == "trigger" and node.profile is not None:
            menu = Gio.Menu()
            menu.append("Object Info", "schema.object-info")
            menu.append("Edit Definition", "schema.edit-function")
            menu.append("Refresh", "schema.refresh")
            if can_drop:
                menu.append("Drop…", "schema.drop-object")
            return menu
        if node.kind == "extension" and node.profile is not None:
            # Install / Update / Drop, per the folder the row came from
            # (PG-05). Whether the account may actually run them is a
            # catalog question, so it is asked when the item is chosen
            # rather than while the menu is being built — the dialog
            # says no, off the main loop, instead of the menu guessing.
            menu = Gio.Menu()
            menu.append("Object Info", "schema.object-info")
            if node.category == "available_extensions":
                menu.append("Install…", "schema.install-extension")
            else:
                if "update to" in node.detail:
                    menu.append("Update…", "schema.update-extension")
                menu.append("Drop…", "schema.drop-extension")
            menu.append("Refresh", "schema.refresh")
            return menu
        if node.kind in ("index", "event"):
            menu = Gio.Menu()
            menu.append("Object Info", "schema.object-info")
            menu.append("Refresh", "schema.refresh")
            if can_drop:
                menu.append("Drop…", "schema.drop-object")
            return menu
        if node.kind != "note" and node.profile is not None:
            # Anything else the tree grows — a kind this menu has never
            # heard of — still opens its info view.
            menu = Gio.Menu()
            menu.append("Object Info", "schema.object-info")
            menu.append("Refresh", "schema.refresh")
            return menu
        return None

    def _row_menu_pressed(
        self, gesture, _n_press, x, y, list_item: Gtk.ListItem
    ) -> None:
        row = list_item.get_item()
        if row is None:
            return
        node = row.get_item()
        menu = self._menu_for(node)
        if menu is None:
            return
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self.set_menu_node(node)
        self._popover.set_menu_model(menu)
        ok, bounds = gesture.get_widget().compute_bounds(self._view)
        rect = Gdk.Rectangle()
        rect.x = int(bounds.origin.x + x) if ok else 0
        rect.y = int(bounds.origin.y + y) if ok else 0
        rect.width = rect.height = 1
        self._popover.set_pointing_to(rect)
        self._popover.popup()

    def _new_pressed(self, button: Gtk.Button) -> None:
        """The connection row's + button: the "New ▸" list, one left
        click away."""
        node = self._new_buttons.get(button)
        if node is None or node.profile is None:
            return
        self._menu_node = node
        ok, bounds = button.compute_bounds(self._view)
        rect = Gdk.Rectangle()
        rect.x = int(bounds.origin.x) if ok else 0
        rect.y = int(bounds.origin.y + bounds.size.height) if ok else 0
        rect.width = rect.height = 1

        if node.ddl_kinds:
            self._popup_new_menu(rect, node.ddl_kinds)
            return

        # Which kinds a connection can create is the adapter's answer,
        # and the adapter only answers once we have connected. Asking
        # first costs one round trip and is the difference between
        # offering triggers, functions and procedures and offering the
        # three-kind guess every dialect shares.
        def done(loaded) -> None:
            node.ddl_kinds, node.supports_drop = loaded
            self._popup_new_menu(rect, node.ddl_kinds)

        def failed(exc: Exception) -> None:
            self._popup_new_menu(rect, _DEFAULT_NEW_KINDS)
            self._show_error(str(exc))

        def work():
            connector = self._connector(node.profile)
            return connector.ddl_kinds(), connector.supports_drop

        run_async(work, done, failed)
        self._load_children(node)  # the tree fills while the menu opens

    def _popup_new_menu(
        self, rect: Gdk.Rectangle, kinds: tuple[str, ...]
    ) -> None:
        menu = Gio.Menu()
        menu.append_section("New", _new_items(kinds))
        self._popover.set_menu_model(menu)
        self._popover.set_pointing_to(rect)
        self._popover.popup()

    def _menu_open(self, *_args) -> None:
        if self._menu_node is not None:
            self.open_node(self._menu_node)

    def _menu_open_window(self, *_args) -> None:
        if self._menu_node is not None:
            self.open_node_in_window(self._menu_node)

    def _menu_properties(self, *_args) -> None:
        self._open_properties(self._on_open_properties)

    def _menu_properties_window(self, *_args) -> None:
        self._open_properties(self._on_open_properties_window)

    def _open_properties(self, callback) -> None:
        target = (
            None if self._menu_node is None
            else self.properties_target(self._menu_node)
        )
        if target is None or callback is None:
            return
        callback(*target)

    def _menu_object_info(self, *_args) -> None:
        if self._menu_node is not None:
            self.open_object_info(self._menu_node)

    def _menu_view_data(self, *_args) -> None:
        node = self._menu_node
        if node is None or node.profile is None:
            return
        if node.kind in ("table", "view"):
            self._on_open_table(node.profile, node.label)
        elif node.kind == "section" and node.table:
            self._on_open_table(node.profile, node.table)

    def _menu_view_section(self, *_args) -> None:
        node = self._menu_node
        if node is not None and node.kind == "section" and (
            node.profile is not None
        ):
            self._on_open_section(node.profile, node.table, node.category)

    def _menu_query_console(self, *_args) -> None:
        node = self._menu_node
        if node is None or node.profile is None:
            return
        if node.kind in ("table", "view"):
            self._on_new_query(node.profile, f"SELECT * FROM {node.label};")
        else:
            self._on_new_query(node.profile)

    def _menu_cli_console(self, *_args) -> None:
        node = self._menu_node
        if node is not None and node.profile is not None:
            self._on_open_cli(node.profile)

    def _menu_edit_table(self, *_args) -> None:
        node = self._menu_node
        if node is not None and node.kind == "table" and node.profile:
            self._on_edit_table(node.profile, _node_ref(node))

    def _menu_duplicate_table(self, *_args) -> None:
        node = self._menu_node
        if (
            node is not None
            and node.kind == "table"
            and node.profile
            and self._on_duplicate_table is not None
        ):
            self._on_duplicate_table(node.profile, _node_ref(node))

    def _menu_new_from_template(self, _action, param) -> None:
        node = self._menu_node
        if (
            node is not None
            and node.profile is not None
            and self._on_new_from_template is not None
        ):
            self._on_new_from_template(
                node.profile, param.get_string(), _node_ref(node)
            )

    def _menu_definition(self, *_args) -> None:
        node = self._menu_node
        if node is not None and node.kind in ("table", "view"):
            self._on_open_definition(node.profile, node.label)

    def _menu_edit_function(self, *_args) -> None:
        node = self._menu_node
        if node is not None and node.kind in ("function", "trigger") and node.profile:
            self._on_open_function(node.profile, node.label)

    def _menu_relation_graph(self, *_args) -> None:
        node = self._menu_node
        if node is not None and node.profile is not None:
            self._on_relation_graph(node.profile)

    def _menu_view_indexes(self, *_args) -> None:
        node = self._menu_node
        if node is not None and node.profile is not None:
            self._on_view_indexes(node.profile)

    def _menu_query_builder(self, *_args) -> None:
        node = self._menu_node
        if node is None or node.profile is None:
            return
        table = node.label if node.kind in ("table", "view") else ""
        self._on_query_builder(node.profile, table)

    def _menu_import_data(self, *_args) -> None:
        node = self._menu_node
        if node is None or node.profile is None:
            return
        if self._on_import_data is not None:
            self._on_import_data(node.profile, node.label)

    def _menu_drop(self, *_args) -> None:
        node = self._menu_node
        if node is None or node.profile is None:
            return
        self._on_drop_object(node.profile, node.kind, node.label, node.table)

    def _extension_action(self, action: str) -> None:
        node = self._menu_node
        if node is None or node.profile is None:
            return
        if self._on_extension_action is not None:
            self._on_extension_action(node.profile, action, node.label)

    def _menu_install_extension(self, *_args) -> None:
        self._extension_action("install")

    def _menu_update_extension(self, *_args) -> None:
        self._extension_action("update")

    def _menu_drop_extension(self, *_args) -> None:
        self._extension_action("drop")

    def _menu_new_object(self, _action, param) -> None:
        node = self._menu_node
        if node is not None and node.profile is not None:
            self._on_new_object(
                node.profile, param.get_string(), _node_ref(node)
            )

    def _menu_refresh(self, *_args) -> None:
        if self._menu_node is not None:
            self.refresh_node(self._menu_node)

    def _menu_mcp_server(self, *_args) -> None:
        node = self._menu_node
        if node is not None and node.profile is not None:
            self._on_mcp_server(node.profile)

    def _menu_manage_users(self, *_args) -> None:
        node = self._menu_node
        if node is not None and node.profile is not None:
            self._on_manage_users(node.profile)

    def _menu_pragmas(self, *_args) -> None:
        node = self._menu_node
        if node is not None and node.profile is not None and self._on_pragmas:
            self._on_pragmas(node.profile)

    def _menu_data_search(self, *_args) -> None:
        node = self._menu_node
        if (
            node is not None
            and node.profile is not None
            and self._on_data_search is not None
        ):
            self._on_data_search(node.profile)

    def _menu_monitor(self, *_args) -> None:
        node = self._menu_node
        if node is not None and node.profile is not None:
            self._on_monitor(node.profile)

    def _menu_open_schema(self, *_args) -> None:
        node = self._menu_node
        if node is not None and node.profile is not None:
            self._on_open_schema(node.profile)

    def set_menu_node(self, node: Node) -> None:
        """Point the menu actions at the row that was right-clicked,
        and re-derive the enabled ones from its state: Disconnect only
        means anything while the connection is open, and Close all
        related tabs only while it has tabs."""
        self._menu_node = node
        disconnect = self._actions.lookup_action("disconnect")
        if disconnect is not None:
            disconnect.set_enabled(
                node.kind == "connection" and bool(node.connected)
            )
        close_tabs = self._actions.lookup_action("close-tabs")
        if close_tabs is not None:
            close_tabs.set_enabled(
                node.kind == "connection"
                and self._count_tabs(node.label) > 0
            )

    def collapse_connection(self, name: str) -> None:
        """Fold a connection row back up and forget the schema it
        cached: what the tree showed came from a session that is now
        closed, and expanding again reconnects and refetches it."""
        for i in range(self._roots.get_n_items()):
            node = self._roots.get_item(i)
            if node.label != name:
                continue
            node.loaded = False
            node.loading = False
            node.ddl_kinds = ()
            if node.store is not None:
                node.store.remove_all()
            row = self._tree.get_child_row(i)
            if row is not None:
                row.set_expanded(False)
            return

    def _menu_close_tabs(self, *_args) -> None:
        node = self._menu_node
        if node is not None and node.profile is not None:
            self._on_close_tabs(node.profile)

    def _menu_disconnect(self, *_args) -> None:
        node = self._menu_node
        if node is not None and node.profile is not None:
            self._on_disconnect(node.profile)

    def _menu_edit_connection(self, *_args) -> None:
        node = self._menu_node
        if node is not None and node.kind == "connection" and node.profile:
            self._on_edit_connection(node.profile)

    def _menu_remove_connection(self, *_args) -> None:
        node = self._menu_node
        if node is not None and node.kind == "connection" and node.profile:
            self._on_remove_connection(node.profile)

    def _query_tooltip(
        self, widget, _x, _y, _keyboard, tooltip: Gtk.Tooltip,
        list_item: Gtk.ListItem,
    ) -> bool:
        row = list_item.get_item()
        if row is None:
            return False
        node = row.get_item()
        if node.kind == "connection":
            tooltip.set_text(_connection_summary(node))
            return True
        if node.kind not in ("table", "view"):
            return _name_tooltip(node, tooltip)
        if node.ddl is None:
            # First hover: kick off the fetch and show a placeholder;
            # the widget re-queries the tooltip when the DDL arrives.
            self._fetch_ddl(node, widget)
            tooltip.set_text(_("Loading DDL…"))
            return True
        if not node.ddl:
            return _name_tooltip(node, tooltip)
        label = Gtk.Label(label=_clamp_lines(node.ddl, 30), xalign=0)
        label.add_css_class("monospace")
        tooltip.set_custom(label)
        return True

    def _fetch_ddl(self, node: Node, widget: Gtk.Widget) -> None:
        if node.ddl_loading:
            return
        node.ddl_loading = True

        def done(ddl: str) -> None:
            node.ddl_loading = False
            node.ddl = ddl or ""
            # Replace the "Loading…" placeholder if the pointer is
            # still on the row (re-queries whatever is hovered now).
            widget.trigger_tooltip_query()

        def failed(_exc: Exception) -> None:
            node.ddl_loading = False  # ddl stays None: re-hover retries

        run_async(
            lambda: self._ensure(node.profile).get_ddl(node.label),
            done,
            failed,
        )

    def _caret_pressed(
        self, gesture, _n_press, _x, _y, list_item: Gtk.ListItem
    ) -> None:
        row = list_item.get_item()
        if row is None:
            return
        if row.get_item().kind in _EXPANDABLE:
            # Claim so the press never bubbles into row activation.
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            row.set_expanded(not row.get_expanded())

    def _row_pressed(
        self, _gesture, n_press: int, _x, _y, list_item: Gtk.ListItem
    ) -> None:
        """A left click on a row: select it (the ListView's own job, so
        the press is never claimed) and toggle its expansion (CORE-52).
        A leaf row only selects.

        A double click delivers its first press here as well, so the
        toggle is not applied yet — it is held for the double-click
        interval (CORE-58). The second press cancels it, and what the
        user sees is the object opening with the tree exactly where it
        was. Holding costs expansion a fraction of a second; undoing the
        toggle afterwards would cost a visible flicker instead.
        """
        self._cancel_toggle()
        if n_press != 1:
            return
        row = list_item.get_item()
        if row is None or row.get_item().kind not in _EXPANDABLE:
            return
        self._toggle_row = row
        self._toggle_source = GLib.timeout_add(
            self._double_click_time(), self._toggle_timeout
        )

    def _double_click_time(self) -> int:
        """How long a second press may take to arrive, in
        milliseconds — the desktop's own setting where there is one."""
        settings = Gtk.Settings.get_default()
        if settings is None:
            return _DOUBLE_CLICK_TIME
        return settings.get_property("gtk-double-click-time")

    def _toggle_timeout(self) -> bool:
        """No second press came: the click was a single one after all."""
        self._toggle_source = 0
        row, self._toggle_row = self._toggle_row, None
        if row is not None:
            row.set_expanded(not row.get_expanded())
        return GLib.SOURCE_REMOVE

    def _cancel_toggle(self) -> None:
        """Drop the toggle a press asked for without applying it: the
        second press of a double click, or a click somewhere else."""
        if self._toggle_source:
            GLib.source_remove(self._toggle_source)
            self._toggle_source = 0
        self._toggle_row = None

    def _on_activate(self, _view, position: int) -> None:
        """Double-click or Enter on a row. Every kind opens something:
        a table or view opens its data, a function opens its editable
        definition, and everything else — categories, columns,
        indexes, triggers, events, and any kind added later — opens the
        read-only info view (frontend/object_info.py).

        Activation only opens: expansion is left exactly as it was
        (CORE-58), so the tree does not move under the pointer at the
        moment the object arrives. Expanding is the caret's job, a
        single click's, or the right arrow key's.
        """
        self._cancel_toggle()  # the press behind this one asked for a toggle
        row = self._view.get_model().get_item(position)
        self.open_node(row.get_item())

    # Following the active tab (CORE-55)

    def follow_object(self, target: tuple[str, str, str] | None) -> None:
        """Reveal the row the active tab is showing: `target` is
        (connection profile name, node kind, object name), or None for
        a tab with no object behind it — a query console, the history
        — which clears the highlight rather than leaving a stale one.

        The resolution is debounced and happens on the next idle
        stretch, so a burst of tab switches walks one path.
        """
        if not self._follow_enabled or self._filtering:
            return
        self._cancel_follow()
        self._follow_target = target
        if target is None:
            self._clear_selection()
            return
        self._follow_source = GLib.timeout_add(
            _FOLLOW_DELAY, self._follow_timeout
        )

    def _follow_timeout(self) -> bool:
        self._follow_source = 0
        self._follow_step()
        return GLib.SOURCE_REMOVE

    def _cancel_follow(self) -> None:
        """Forget the walk in flight: a newer tab switch supersedes it,
        and nothing half-resolved should land after it."""
        if self._follow_source:
            GLib.source_remove(self._follow_source)
            self._follow_source = 0
        if self._follow_wait is not None:
            store, handler = self._follow_wait
            store.disconnect(handler)
            self._follow_wait = None
        self._follow_target = None

    def _clear_selection(self) -> None:
        model = self._view.get_model()
        model.set_selected(Gtk.INVALID_LIST_POSITION)

    def _follow_step(self) -> None:
        """One pass at the path, from the connection row down.

        Every level is either already loaded — walk on — or expanded
        and left to load, with the walk resuming when its rows arrive
        (_wait_for). So the whole reveal costs one listing per level of
        the path and none beside it: a deep tree is not a reason to
        query the catalog for the branches nobody asked about.
        """
        target = self._follow_target
        if target is None:
            return
        profile_name, kind, name = target
        segments = profile_name.split(_DATABASE_SEPARATOR)
        node = next(
            (n for n in _items(self._roots) if n.label == segments[0]), None
        )
        if node is None:
            return
        # The database and schema levels, where the profile name has
        # them: a tab opened under "prod · billing · public" sits three
        # rows down (see _database_profile / schema_profile).
        for label in segments[1:]:
            child = self._follow_into(node, lambda n: n.label == label)
            if child is None:
                return
            node = child
        # The folders of that level are its own listing, so it has to
        # be loaded before there is anywhere to look.
        self._follow_into(node, lambda _n: False)
        if not node.loaded:
            return
        for slug in _FOLLOW_CATEGORIES.get(kind, ()):
            category = next(
                (
                    child for child in _items(node.store)
                    if child.kind == "category" and child.category == slug
                ),
                None,
            )
            if category is None:
                continue
            found = self._follow_into(
                category, lambda child: _same_object(child, name)
            )
            if found is not None:
                self._select(found)
                return

    def _follow_into(
        self, node: Node, match: Callable[[Node], bool]
    ) -> Node | None:
        """The child of `node` that `match` picks, expanding and
        loading `node` first. None means "not there, or not yet": a
        pending load resumes the walk itself when its rows land."""
        if not node.loaded:
            row = self._row_for(node)
            if row is not None and not row.get_expanded():
                row.set_expanded(True)
            self._load_children(node)
            if not node.loaded:
                self._wait_for(node)
                return None
        for child in _items(node.store):
            if match(child):
                row = self._row_for(child)
                if row is None:
                    # The child exists but its row is not in the model
                    # yet, because an ancestor is collapsed: expanding
                    # this node put it there, so a later pass finds it.
                    return None
                return child
        return None

    def _wait_for(self, node: Node) -> None:
        """Resume the walk when a level finishes loading. A failed load
        appends its own "(failed to load)" row, so the walk restarts,
        finds nothing to match and quietly stops."""
        if node.store is None or self._follow_wait is not None:
            return
        store = node.store

        def arrived(*_args) -> None:
            if self._follow_wait is None:
                return
            waiting, handler = self._follow_wait
            if waiting is not store:
                return
            self._follow_wait = None
            store.disconnect(handler)
            self._follow_step()

        self._follow_wait = (store, store.connect("items-changed", arrived))

    def _select(self, node: Node) -> None:
        """Highlight the row for `node`, scrolling it into view only
        when it is off screen. Selection alone never opens anything —
        the sync is one-way (CORE-55)."""
        model = self._view.get_model()
        for position in range(self._tree.get_n_items()):
            row = self._tree.get_row(position)
            if row is None or row.get_item() is not node:
                continue
            model.set_selected(position)
            if row not in self._bound_rows:
                # Off screen: this is the one case worth moving the
                # scroll for. A row the user can already see is
                # highlighted where it is, so a tab switch never yanks
                # the position they navigated to.
                self._view.scroll_to(
                    position, Gtk.ListScrollFlags.NONE, None
                )
            return

    def is_openable(self, node: Node) -> bool:
        """Does this row open anything? "Loading…" and "(none)" rows
        are placeholders, and a row with no profile behind it has
        nothing to open — the menu leaves Open off those (CORE-52)."""
        return node.kind != "note" and node.profile is not None

    def properties_target(self, node: Node) -> tuple | None:
        """What this row's Properties item acts on (CORE-47):
        (profile, ObjectRef, section slug), or None for a row with no
        object behind it.

        A section row under a table (CORE-05) is not an object of its
        own: it targets its table, on that section.
        """
        if node.profile is None or node.kind == "note":
            return None
        if node.kind == "section":
            if not node.table:
                return None
            return (
                node.profile,
                objects.ObjectRef(kind="table", name=node.table),
                node.category,
            )
        return (
            node.profile,
            objects.ObjectRef(
                kind=node.kind,
                name=node.label,
                table=node.table,
                category=node.category,
            ),
            "",
        )

    def open_node(self, node: Node) -> None:
        """Open one row's object — the Open action, and what a double
        click or Enter does. Opening something that is already open
        focuses its tab rather than stacking a second copy (CORE-01),
        which the window handles for every kind here.

        A section row under a table — Indexes, Columns, Constraints —
        opens as a tab of its own showing that listing in the result
        grid (CORE-56), not as a page of the side panel. The panel
        route is still there, as the row's explicit "Open in
        Properties" menu item (CORE-05).
        """
        if not self.is_openable(node):
            return
        if node.kind in ("table", "view"):
            self._on_open_table(node.profile, node.label)
        elif node.kind == "function":
            self._on_open_function(node.profile, node.label)
        else:
            self.open_object_info(node)

    def open_node_in_window(self, node: Node) -> None:
        """Open one row's object in a window of its own — the same
        tear-out path a dragged tab and a Shift-click take, so there is
        one way a tab ends up popped out (CORE-52). Without a window to
        ask (a harness that only walks the tree), it opens as a tab."""
        if not self.is_openable(node):
            return
        if self._on_open_window is None:
            self.open_node(node)
            return
        self._on_open_window(lambda: self.open_node(node))

    def open_object_info(self, node: Node) -> None:
        """The info view for one tree row, with the path it sits at."""
        if node.profile is None or node.kind == "note":
            return
        self._on_open_object(
            node.profile,
            objects.ObjectRef(
                kind=node.kind,
                name=node.label,
                table=node.table,
                category=node.category,
            ),
            _node_path(node),
        )


#: Separator between a connection's name and the database a derived
#: profile points at. The query console builds the same names, so a
#: console opened on "prod · billing" and the sidebar row under it
#: share one connector.
_DATABASE_SEPARATOR = " · "


def _database_profile(
    profile: ConnectionProfile, database: str
) -> ConnectionProfile:
    """`profile` pointed at another database on the same server.

    MySQL and PostgreSQL connections reach a whole server, but a
    connector is attached to one database: its catalog queries are
    scoped to it, and in PostgreSQL another database is not reachable
    at all without reconnecting. So each database gets its own derived
    profile — and, through it, its own connector — rather than the
    tree pretending one connection covers them all. The schema is
    dropped: a schema pinned on one database means nothing in another.
    """
    if database == profile.database:
        return profile
    return replace(
        profile,
        name=f"{profile.name}{_DATABASE_SEPARATOR}{database}",
        database=database,
        schema="",
    )


def _level_categories(
    profile: ConnectionProfile | None, level: str
) -> tuple[tuple[str, str], ...]:
    """The folders this engine hangs off `level` — (slug, label), in
    display order. A capability answer like the rest (CORE-02): it
    needs no connection, so the level is laid out before the server is
    asked anything, and an engine that declares none keeps the generic
    Tables/Views/Functions set (PG-02)."""
    if profile is None:
        return ()
    try:
        return registry.level_categories(profile.kind, level)
    except Exception:  # an adapter the registry doesn't know
        return ()


def _administer_categories(
    profile: ConnectionProfile | None,
) -> tuple[tuple[str, str], ...]:
    """What the Administer folder holds on this engine."""
    if profile is None:
        return ()
    try:
        return registry.administer_categories(profile.kind)
    except Exception:
        return ()


def _relation_kind(category: str) -> str:
    """The row kind a relation folder holds — Materialized Views hold
    views, Foreign Tables hold tables."""
    folder = objects.RELATION_FOLDERS.get(category)
    return folder[0] if folder else "table"


def _category_rows(node: Node) -> list:
    """The relations that belong in one relation folder, out of the
    listing the row above it made: the kind the folder holds, carrying
    one of the notes that puts a row in *this* folder rather than a
    sibling — a partition is not listed beside the table it is part
    of (PG-02)."""
    folder = objects.RELATION_FOLDERS.get(node.category)
    if folder is None:
        return list(node.payload or [])
    kind, notes = folder
    return [
        info
        for info in node.payload or []
        if info.kind == kind and getattr(info, "detail", "") in notes
    ]


def _append_folders(
    store: Gio.ListStore,
    node: Node,
    folders: tuple[tuple[str, str], ...],
    relations: list,
) -> None:
    """The declared folders of one level, as rows. A relation folder
    carries the listing already made — so it fills without a second
    query, and can say how many rows it holds before it is opened;
    every other folder is lazy and says nothing it has not fetched."""
    for slug, label in folders:
        payload = list(relations) if slug in objects.RELATION_FOLDERS else None
        row = Node(
            "category", label,
            profile=node.profile, category=slug, payload=payload,
        )
        if payload is not None:
            row.detail = str(len(_category_rows(row)))
        store.append(row)


def _is_system_schema(kind: str, name: str) -> bool:
    """Whether `name` is one of the server's own schemas on this
    engine — the provider layer's answer (registry.is_system_schema,
    PG-03), so no engine is named here."""
    if not kind:
        return False
    try:
        return registry.is_system_schema(kind, name)
    except Exception:  # an adapter the registry doesn't know
        return False


def _is_system_object(kind: str, name: str) -> bool:
    """Whether `name` is an object the engine owns rather than the user
    — the provider layer's answer again (registry.is_system_object,
    SQ-01). False on the engines that keep their catalog in a schema of
    its own, which the schema question already covers."""
    if not kind:
        return False
    try:
        return registry.is_system_object(kind, name)
    except Exception:  # an adapter the registry doesn't know
        return False


def _is_system_database(kind: str, name: str) -> bool:
    """Whether `name` is one of the server's own databases on this
    engine — the provider layer's answer again (MY-01). False wherever
    a database and a schema are different things; true for MySQL's
    catalog schemas, which are databases here.
    """
    if not kind:
        return False
    try:
        return registry.is_system_database(kind, name)
    except Exception:  # an adapter the registry doesn't know
        return False


def _has_schemas(profile: ConnectionProfile | None) -> bool:
    """Whether this engine puts schemas below the database — a
    capability question the provider layer answers with no connection
    open (CORE-02), so the tree knows how deep it goes before it asks
    the server anything."""
    if profile is None:
        return False
    try:
        return registry.capabilities(profile.kind).schemas
    except Exception:  # an adapter the registry doesn't know
        return False


def _node_ref(node: Node) -> NodeRef:
    """The tree row as the provider's NodeRef — its own kind and name,
    plus the database and schema the row sits in.

    Rows below a schema carry it on their derived profile
    (`schema_profile`), so the context a New ▸ Table needs is already
    there and no walk back up the tree is required (CORE-24).
    """
    profile = node.profile
    schema = node.label if node.kind == "schema" else (
        profile.schema if profile else ""
    )
    return NodeRef(
        kind=node.kind,
        name=node.label,
        database=profile.database if profile else "",
        schema=schema or "",
        category=node.category,
        table=node.table,
        system=node.system,
    )


def schema_profile(
    profile: ConnectionProfile, schema: str
) -> ConnectionProfile:
    """`profile` pointed at one schema of the database it already
    reaches (PG-01).

    Pinning the schema on the profile — rather than qualifying every
    name the tree sends down — puts that schema on the connection's
    search path, so the adapter's existing catalog queries, its DDL and
    the bare names in generated SQL all resolve in the schema the row
    stands for. The name matches what the console's schema dropdown
    builds, so a console opened on "prod · billing · staging" and this
    row share a connector.
    """
    if schema == profile.schema:
        return profile
    return replace(
        profile,
        name=f"{profile.name}{_DATABASE_SEPARATOR}{schema}",
        schema=schema,
    )


def _rows(tree: Gtk.TreeListModel):
    """Every row the tree model currently exposes (i.e. under an
    expanded parent)."""
    for i in range(tree.get_n_items()):
        row = tree.get_row(i)
        if row is not None:
            yield row


def _clone(node: Node) -> Node:
    """A search-mode copy of a row: same object behind it (so opening,
    dropping and refreshing still work), but with children of its own —
    the matches — instead of the live subtree."""
    copy = Node(
        node.kind,
        node.label,
        detail=node.detail,
        profile=node.profile,
        category=node.category,
        payload=node.payload,
        is_pk=node.is_pk,
        table=node.table,
        system=node.system,
    )
    copy.connected = node.connected
    copy.ddl = node.ddl
    copy.ddl_kinds = node.ddl_kinds
    copy.supports_drop = node.supports_drop
    copy.loaded = True  # search never loads: it filters what is there
    copy.filtered = True
    copy.source = node
    return copy


def _filtered_children(node: Node) -> Gio.ListStore | None:
    """Search mode's create_func: the matches already computed, and
    nothing to load."""
    if node.store is None or not node.store.get_n_items():
        return None
    return node.store


def _items(store: Gio.ListStore | None):
    """Every node in a (possibly unloaded) child store."""
    for i in range(store.get_n_items() if store is not None else 0):
        yield store.get_item(i)


def _adopt(node: Node) -> None:
    """Point a freshly filled node's children back at it, so Refresh on
    any of them knows which list to refetch."""
    if node.store is None:
        return
    for i in range(node.store.get_n_items()):
        child = node.store.get_item(i)
        child.parent = node
        # Everything under a system schema is system too: the folders,
        # their rows, and the columns below those (PG-03).
        child.system = child.system or node.system


def _new_items(kinds: tuple[str, ...]) -> Gio.Menu:
    """One "New ▸" item per creatable object kind, in the adapter's
    order (tables and views first, functions and procedures where the
    dialect has them)."""
    menu = Gio.Menu()
    for kind in kinds:
        item = Gio.MenuItem.new(_NEW_LABELS.get(kind, kind.capitalize()), None)
        item.set_action_and_target_value(
            "schema.new-object", GLib.Variant.new_string(kind)
        )
        menu.append_item(item)
        if kind == "table":
            # The shapes somebody saved, right under the thing they
            # make (CORE-29). No templates saved yet: no submenu, so
            # the menu keeps the shape it always had.
            templates = _template_items()
            if templates is not None:
                menu.append_submenu("From Template", templates)
    return menu


def _template_items() -> Gio.Menu | None:
    """One item per saved table template, or None when there are none.

    Read per popup, from the config directory: a template dropped in by
    hand shows up in the menu without a restart, and an unreadable file
    is simply not listed.
    """
    templates = template_store.templates()
    if not templates:
        return None
    menu = Gio.Menu()
    for template in templates:
        item = Gio.MenuItem.new(template.name, None)
        item.set_action_and_target_value(
            "schema.new-from-template",
            GLib.Variant.new_string(template.name),
        )
        menu.append_item(item)
    return menu


def _row_profile(row: Gtk.TreeListRow) -> ConnectionProfile | None:
    """The connection a row belongs to. Column rows carry no profile of
    their own, so the walk goes up until one appears."""
    while row is not None:
        node = row.get_item()
        if node.profile is not None:
            return node.profile
        row = row.get_parent()
    return None


def _set_caret(caret: Gtk.Image, expanded: bool) -> None:
    caret.set_from_icon_name(
        "pan-down-symbolic" if expanded else "pan-end-symbolic"
    )


def _show_detail(label: Gtk.Label, detail: str) -> None:
    """Put a row's secondary text in its label. The text ellipsizes
    rather than widening the tree (CORE-51), and ellipsized text is
    only half an answer — so the whole of it goes in the label's own
    tooltip, which wins over the row's for the label's area."""
    label.set_text(detail)
    label.set_visible(bool(detail))
    label.set_tooltip_text(detail or None)


def _name_tooltip(node: Node, tooltip: Gtk.Tooltip) -> bool:
    """Fallback tooltip: the row's own name, for names long enough to
    run past a sidebar dragged narrow. Nothing is ellipsized — the row
    can always be scrolled to — but reading a name without reaching
    for the scrollbar is worth a tooltip."""
    text = node.label if not node.filtered or node.source is None else (
        node.source.label
    )
    if len(text) < _LONG_LABEL:
        return False
    tooltip.set_text(text)
    return True


def _connection_summary(node: Node) -> str:
    """Connection-row tooltip: kind + target, plus the object count
    once the schema has loaded (a whole-database DDL dump would be far
    too big for a tooltip)."""
    profile = node.profile
    target = profile.file_path or profile.jdbc_url or profile.host
    summary = f"{profile.kind} · {target}" if target else profile.kind
    if node.loaded and node.store is not None:
        count = sum(
            len(_category_rows(child))
            for i in range(node.store.get_n_items())
            if (child := node.store.get_item(i)).kind == "category"
            and child.payload is not None
        )
        summary += f"\n{count} object(s)"
    return summary


def _same_object(node: Node, name: str) -> bool:
    """Is this row the object a tab key names? The key carries the name
    the tab was opened with, which may be schema-qualified
    ("public.orders") where the row is not."""
    if node.kind == "note":
        return False
    return node.label == name or node.label == name.rsplit(".", 1)[-1]


def _node_path(node: Node) -> str:
    """Where a row sits, read back up its parents: "prod ▸ billing
    ▸ Indexes ▸ idx_orders_user". Only rows loaded through a
    parent have one (roots keep None), so a connection row is just its
    own name."""
    parts = [node.label]
    current = node.parent
    while current is not None:
        parts.append(current.label)
        current = current.parent
    return " ▸ ".join(reversed(parts))


def _clamp_lines(text: str, max_lines: int) -> str:
    lines = text.splitlines()
    if len(lines) > max_lines:
        lines = lines[:max_lines] + ["…"]
    return "\n".join(lines)


def _style_dot(dot: Gtk.Box, connected: bool) -> None:
    if connected:
        dot.add_css_class("conn-dot-active")
        dot.remove_css_class("conn-dot-idle")
    else:
        dot.add_css_class("conn-dot-idle")
        dot.remove_css_class("conn-dot-active")
