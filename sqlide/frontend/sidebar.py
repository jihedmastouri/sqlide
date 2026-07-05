"""IDE-like schema tree sidebar.

Gtk.TreeListModel + Gtk.ListView + Gtk.TreeExpander (the GTK4 idiom
for lazy trees). Shape:

    connection → Tables / Views / Functions → object → columns

Column rows show "name  type" with a PK marker and are informational.
Rows lead with a per-kind icon (connections also get a connection
status dot) and expandable rows end with a caret; the built-in
expander arrow is hidden. Activating a table/view opens a data tab;
activating a connection or category toggles it; clicking the caret on
a table/view expands its columns without opening a tab. Activating a
function (or its Edit Definition context item) opens its definition
in an editable tab. Right-clicking a table or view opens View Data /
Query Console / Table Definition; right-clicking a connection offers
a new query console (new consoles otherwise come from the header-bar
button) and the connection's relation graph. Hovering a table/view shows its DDL in a tooltip (fetched
lazily, cached on the node); hovering a connection shows a short
summary.

set_filter() switches the view to fuzzy-find mode: a flat list of the
tables, views and functions whose names loosely match the query
(subsequence match), across every connection whose schema has already
been loaded. Clearing the query restores the tree.

Lazy loading: GTK probes create_func just to decide whether a row gets
an expander arrow, so it must stay cheap — it only creates (and caches)
an empty child store. The actual database work (list_tables /
list_columns / list_functions via run_async) starts when a row is
actually expanded, watched through TreeListRow's expanded property.
A failed load leaves the node unloaded, so collapsing and re-expanding
retries.
"""

from __future__ import annotations

from typing import Callable

from gi.repository import Gdk, Gio, GObject, Gtk, Pango

from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.db.base import Connector
from sqlide.frontend.util import run_async

_EXPANDABLE = ("connection", "category", "table", "view")

# Leading icon per row kind; kinds not listed (category, column, note)
# show no icon.
_KIND_ICONS = {
    "connection": "network-server-symbolic",
    "table": "view-grid-symbolic",
    "view": "view-reveal-symbolic",
    "function": "system-run-symbolic",
}


class Node(GObject.Object):
    """One tree row; kind decides expandability, look, and activation.

    kind: "connection" | "category" | "table" | "view" | "function"
        | "column" | "note" (dim placeholder: loading/empty/error)
    """

    def __init__(
        self,
        kind: str,
        label: str,
        *,
        detail: str = "",
        profile: ConnectionProfile | None = None,
        category: str = "",  # category nodes: "tables"|"views"|"functions"
        payload: list | None = None,  # tables/views categories: TableInfo list
        is_pk: bool = False,
    ) -> None:
        super().__init__()
        self.kind = kind
        self.label = label
        self.detail = detail
        self.profile = profile
        self.category = category
        self.payload = payload
        self.is_pk = is_pk
        self.store: Gio.ListStore | None = None  # cached child model
        self.loaded = False
        self.loading = False
        self.connected = False  # connection rows: status dot state
        self.ddl: str | None = None  # table/view rows: None = not fetched
        self.ddl_loading = False


class Sidebar(Gtk.ScrolledWindow):
    def __init__(
        self,
        ensure_connector: Callable[[ConnectionProfile], Connector],
        on_open_table: Callable[[ConnectionProfile, str], None],
        on_new_query: Callable[..., None],  # (profile, sql="")
        on_open_definition: Callable[[ConnectionProfile, str], None],
        on_open_function: Callable[[ConnectionProfile, str], None],
        on_relation_graph: Callable[[ConnectionProfile], None],
        on_query_builder: Callable[..., None],  # (profile, table="")
        show_error: Callable[[str], None],
    ) -> None:
        super().__init__(vexpand=True)
        self._ensure = ensure_connector
        self._on_open_table = on_open_table
        self._on_new_query = on_new_query
        self._on_open_definition = on_open_definition
        self._on_open_function = on_open_function
        self._on_relation_graph = on_relation_graph
        self._on_query_builder = on_query_builder
        self._show_error = show_error
        # Currently bound status dot per connection name, so
        # set_connected() can restyle a visible row.
        self._dots: dict[str, Gtk.Box] = {}

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
        self._view = Gtk.ListView(
            model=Gtk.SingleSelection(model=self._tree), factory=factory
        )
        self._view.add_css_class("navigation-sidebar")
        self._view.add_css_class("schema-tree")
        self._view.set_single_click_activate(True)
        self._view.connect("activate", self._on_activate)
        self.set_child(self._view)

        # Context menu (right-click on a table/view or connection row).
        self._menu_node: Node | None = None
        actions = Gio.SimpleActionGroup()
        for name, callback in (
            ("view-data", self._menu_view_data),
            ("query-console", self._menu_query_console),
            ("definition", self._menu_definition),
            ("edit-function", self._menu_edit_function),
            ("relation-graph", self._menu_relation_graph),
            ("query-builder", self._menu_query_builder),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            actions.add_action(action)
        self._view.insert_action_group("schema", actions)

        self._object_menu = Gio.Menu()
        self._object_menu.append("View Data", "schema.view-data")
        self._object_menu.append("Query Console", "schema.query-console")
        self._object_menu.append("Table Definition", "schema.definition")
        self._object_menu.append("Query Builder", "schema.query-builder")
        self._connection_menu = Gio.Menu()
        self._connection_menu.append(
            "New Query Console", "schema.query-console"
        )
        self._connection_menu.append(
            "Relation Graph", "schema.relation-graph"
        )
        self._connection_menu.append(
            "Query Builder", "schema.query-builder"
        )
        self._function_menu = Gio.Menu()
        self._function_menu.append("Edit Definition", "schema.edit-function")
        self._popover = Gtk.PopoverMenu.new_from_model(self._object_menu)
        self._popover.set_parent(self._view)
        self._popover.set_has_arrow(False)
        self._view.connect("destroy", lambda *_: self._popover.unparent())

    def add_profile(self, profile: ConnectionProfile) -> None:
        self._roots.append(
            Node("connection", profile.name, detail=profile.kind, profile=profile)
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

    def set_filter(self, text: str) -> None:
        """Fuzzy-find mode: replace the tree with a flat list of the
        matching tables/views/functions of every loaded connection;
        an empty query restores the tree."""
        text = text.strip().lower()
        if not text:
            self._view.set_model(Gtk.SingleSelection(model=self._tree))
            return
        matches = []
        for node in self._search_candidates():
            key = _fuzzy_key(text, node.label.lower())
            if key is not None:
                matches.append((key, node))
        matches.sort(key=lambda pair: (pair[0], pair[1].label.lower()))
        store = Gio.ListStore(item_type=Node)
        for _key, node in matches:
            store.append(node)
        if not matches:
            store.append(Node("note", "(no matches in loaded connections)"))
        flat = Gtk.TreeListModel.new(
            store,
            passthrough=False,
            autoexpand=False,
            create_func=self._create_children,
        )
        self._view.set_model(Gtk.SingleSelection(model=flat))

    def _search_candidates(self):
        """Fresh table/view/function Nodes from every connection whose
        schema has been loaded (detail carries the connection name)."""
        for i in range(self._roots.get_n_items()):
            conn = self._roots.get_item(i)
            if conn.store is None:
                continue
            for j in range(conn.store.get_n_items()):
                category = conn.store.get_item(j)
                if category.kind != "category":
                    continue
                if category.category in ("tables", "views"):
                    kind = "table" if category.category == "tables" else "view"
                    for info in category.payload or []:
                        yield Node(
                            kind, info.name,
                            detail=conn.label, profile=conn.profile,
                        )
                elif category.store is not None:
                    for k in range(category.store.get_n_items()):
                        child = category.store.get_item(k)
                        if child.kind == "function":
                            yield Node(
                                "function", child.label,
                                detail=conn.label, profile=conn.profile,
                            )

    def expand_profile(self, name: str) -> None:
        """Expand (and thereby connect/load) the row for a profile."""
        for i in range(self._roots.get_n_items()):
            node = self._roots.get_item(i)
            if node.label == name:
                row = self._tree.get_child_row(i)
                if row is not None:
                    row.set_expanded(True)
                return

    # Tree model

    def _create_children(self, node: Node) -> Gio.ListStore | None:
        # Called both on expansion and by is_expandable probes: no I/O
        # here, just the cached store (see module docstring).
        if node.kind not in _EXPANDABLE:
            return None
        if node.store is None:
            node.store = Gio.ListStore(item_type=Node)
            if node.kind == "category" and node.category != "functions":
                self._fill_category(node)
        return node.store

    def _fill_category(self, node: Node) -> None:
        # Tables/Views got their objects from the connection's
        # list_tables() call; filling is synchronous.
        kind = "table" if node.category == "tables" else "view"
        for info in node.payload or []:
            node.store.append(Node(kind, info.name, profile=node.profile))
        if not node.payload:
            node.store.append(Node("note", "(none)"))
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
        if node.store is None:
            node.store = Gio.ListStore(item_type=Node)
        node.loading = True
        store = node.store
        store.remove_all()
        store.append(Node("note", "Loading…"))

        if node.kind == "connection":
            def work():
                return self._ensure(node.profile).list_tables()

            def fill(objects):
                tables = [t for t in objects if t.kind != "view"]
                views = [t for t in objects if t.kind == "view"]
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
        elif node.kind == "category":  # only the lazy Functions category
            def work():
                return self._ensure(node.profile).list_functions()

            def fill(functions):
                for function in functions:
                    store.append(
                        Node("function", function.name, profile=node.profile)
                    )
                if not functions:
                    store.append(Node("note", "(none)"))
        else:  # table | view
            def work():
                return self._ensure(node.profile).list_columns(node.label)

            def fill(columns):
                for column in columns:
                    store.append(Node(
                        "column", column.name,
                        detail=column.type, is_pk=column.is_pk,
                    ))
                if not columns:
                    store.append(Node("note", "(no columns)"))

        def done(result):
            node.loading = False
            node.loaded = True
            store.remove_all()
            fill(result)

        def failed(exc):
            node.loading = False  # loaded stays False: re-expand retries
            store.remove_all()
            store.append(Node("note", "(failed to load)"))
            self._show_error(str(exc))

        run_async(work, done, failed)

    # Rows

    def _setup_row(self, _factory, list_item: Gtk.ListItem) -> None:
        expander = Gtk.TreeExpander()
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
        label.set_ellipsize(Pango.EllipsizeMode.END)
        pk = Gtk.Label(label="PK")
        pk.add_css_class("caption")
        pk.add_css_class("accent")
        detail = Gtk.Label()
        detail.add_css_class("dim-label")
        detail.add_css_class("caption")
        caret = Gtk.Image(icon_name="pan-end-symbolic")
        caret.add_css_class("dim-label")
        caret.set_visible(False)
        # Expands table/view columns without opening a data tab (row
        # activation), so the two gestures stay distinct.
        caret_click = Gtk.GestureClick()
        caret_click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        caret_click.connect("pressed", self._caret_pressed, list_item)
        caret.add_controller(caret_click)
        menu_click = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        menu_click.connect("pressed", self._row_menu_pressed, list_item)
        expander.add_controller(menu_click)
        for child in (dot, icon, label, pk, detail, caret):
            box.append(child)
        expander.set_child(box)
        list_item.set_child(expander)
        list_item.dot = dot
        list_item.dot_name = ""
        list_item.icon = icon
        list_item.label = label
        list_item.pk = pk
        list_item.detail = detail
        list_item.caret = caret
        list_item.row_handler = 0

    def _bind_row(self, _factory, list_item: Gtk.ListItem) -> None:
        row = list_item.get_item()  # TreeListRow (passthrough=False)
        node = row.get_item()
        list_item.get_child().set_list_row(row)
        if node.kind == "connection":
            _style_dot(list_item.dot, node.connected)
            list_item.dot.set_visible(True)
            list_item.dot_name = node.label
            self._dots[node.label] = list_item.dot
        else:
            list_item.dot.set_visible(False)
        icon_name = _KIND_ICONS.get(node.kind)
        if icon_name:
            list_item.icon.set_from_icon_name(icon_name)
        list_item.icon.set_visible(bool(icon_name))
        list_item.label.set_text(node.label)
        if node.kind == "note":
            list_item.label.add_css_class("dim-label")
        else:
            list_item.label.remove_css_class("dim-label")
        list_item.pk.set_visible(node.is_pk)
        list_item.detail.set_text(node.detail)
        list_item.detail.set_visible(bool(node.detail))
        list_item.caret.set_visible(node.kind in _EXPANDABLE)
        _set_caret(list_item.caret, row.get_expanded())
        list_item.row_handler = row.connect(
            "notify::expanded", self._on_row_expanded, list_item
        )
        if row.get_expanded():  # expanded while unbound (e.g. scrolled away)
            self._load_children(node)

    def _unbind_row(self, _factory, list_item: Gtk.ListItem) -> None:
        if list_item.row_handler:
            list_item.get_item().disconnect(list_item.row_handler)
            list_item.row_handler = 0
        if list_item.dot_name:
            if self._dots.get(list_item.dot_name) is list_item.dot:
                del self._dots[list_item.dot_name]
            list_item.dot_name = ""

    # Context menu

    def _row_menu_pressed(
        self, gesture, _n_press, x, y, list_item: Gtk.ListItem
    ) -> None:
        row = list_item.get_item()
        if row is None:
            return
        node = row.get_item()
        if node.kind in ("table", "view"):
            menu = self._object_menu
        elif node.kind == "connection":
            menu = self._connection_menu
        elif node.kind == "function" and node.profile is not None:
            menu = self._function_menu
        else:
            return
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self._menu_node = node
        self._popover.set_menu_model(menu)
        ok, bounds = gesture.get_widget().compute_bounds(self._view)
        rect = Gdk.Rectangle()
        rect.x = int(bounds.origin.x + x) if ok else 0
        rect.y = int(bounds.origin.y + y) if ok else 0
        rect.width = rect.height = 1
        self._popover.set_pointing_to(rect)
        self._popover.popup()

    def _menu_view_data(self, *_args) -> None:
        node = self._menu_node
        if node is not None and node.kind in ("table", "view"):
            self._on_open_table(node.profile, node.label)

    def _menu_query_console(self, *_args) -> None:
        node = self._menu_node
        if node is None or node.profile is None:
            return
        if node.kind in ("table", "view"):
            self._on_new_query(node.profile, f"SELECT * FROM {node.label};")
        else:
            self._on_new_query(node.profile)

    def _menu_definition(self, *_args) -> None:
        node = self._menu_node
        if node is not None and node.kind in ("table", "view"):
            self._on_open_definition(node.profile, node.label)

    def _menu_edit_function(self, *_args) -> None:
        node = self._menu_node
        if node is not None and node.kind == "function" and node.profile:
            self._on_open_function(node.profile, node.label)

    def _menu_relation_graph(self, *_args) -> None:
        node = self._menu_node
        if node is not None and node.profile is not None:
            self._on_relation_graph(node.profile)

    def _menu_query_builder(self, *_args) -> None:
        node = self._menu_node
        if node is None or node.profile is None:
            return
        table = node.label if node.kind in ("table", "view") else ""
        self._on_query_builder(node.profile, table)

    def _query_tooltip(
        self, _widget, _x, _y, _keyboard, tooltip: Gtk.Tooltip,
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
            return False
        if node.ddl is None:
            # First hover: kick off the fetch; the tooltip shows from
            # the cache on the next hover.
            self._fetch_ddl(node)
            return False
        if not node.ddl:
            return False
        label = Gtk.Label(label=_clamp_lines(node.ddl, 30), xalign=0)
        label.add_css_class("monospace")
        tooltip.set_custom(label)
        return True

    def _fetch_ddl(self, node: Node) -> None:
        if node.ddl_loading:
            return
        node.ddl_loading = True

        def done(ddl: str) -> None:
            node.ddl_loading = False
            node.ddl = ddl or ""

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

    def _on_activate(self, _view, position: int) -> None:
        row = self._view.get_model().get_item(position)
        node = row.get_item()
        if node.kind in ("table", "view"):
            self._on_open_table(node.profile, node.label)
        elif node.kind == "function" and node.profile is not None:
            self._on_open_function(node.profile, node.label)
        elif node.kind in ("connection", "category"):
            row.set_expanded(not row.get_expanded())


def _fuzzy_key(query: str, name: str) -> tuple[int, int, int] | None:
    """Subsequence match of query in name (both lowercase). None when
    it doesn't match; otherwise a sort key — tighter, earlier, shorter
    matches first."""
    start = name.find(query)
    if start != -1:  # contiguous: always beats scattered matches
        return (0, start, len(name))
    position = 0
    first = -1
    for char in query:
        position = name.find(char, position)
        if position == -1:
            return None
        if first == -1:
            first = position
        position += 1
    spread = position - first - len(query)
    return (1 + spread, first, len(name))


def _set_caret(caret: Gtk.Image, expanded: bool) -> None:
    caret.set_from_icon_name(
        "pan-down-symbolic" if expanded else "pan-end-symbolic"
    )


def _connection_summary(node: Node) -> str:
    """Connection-row tooltip: kind + target, plus the object count
    once the schema has loaded (a whole-database DDL dump would be far
    too big for a tooltip)."""
    profile = node.profile
    target = profile.file_path or profile.jdbc_url or profile.host
    summary = f"{profile.kind} · {target}" if target else profile.kind
    if node.loaded and node.store is not None:
        count = sum(
            len(child.payload)
            for i in range(node.store.get_n_items())
            if (child := node.store.get_item(i)).kind == "category"
            and child.payload is not None
        )
        summary += f"\n{count} object(s)"
    return summary


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
