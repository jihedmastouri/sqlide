"""IDE-like schema tree sidebar.

Gtk.TreeListModel + Gtk.ListView + Gtk.TreeExpander (the GTK4 idiom
for lazy trees). Shape:

    connection → Tables / Views / Functions → object → columns

Column rows show "name  type" with a PK marker and are informational.
Activating a table/view opens a data tab; activating a connection or
category toggles it; each connection row keeps its "new query console"
button.

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

from gi.repository import Gio, GObject, Gtk, Pango

from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.db.base import Connector
from sqlide.frontend.util import run_async

_EXPANDABLE = ("connection", "category", "table", "view")


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


class Sidebar(Gtk.ScrolledWindow):
    def __init__(
        self,
        ensure_connector: Callable[[ConnectionProfile], Connector],
        on_open_table: Callable[[ConnectionProfile, str], None],
        on_new_query: Callable[[ConnectionProfile], None],
        show_error: Callable[[str], None],
    ) -> None:
        super().__init__(vexpand=True)
        self._ensure = ensure_connector
        self._on_open_table = on_open_table
        self._on_new_query = on_new_query
        self._show_error = show_error

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

    def add_profile(self, profile: ConnectionProfile) -> None:
        self._roots.append(
            Node("connection", profile.name, detail=profile.kind, profile=profile)
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

    def _on_row_expanded(self, row: Gtk.TreeListRow, _pspec) -> None:
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
                    store.append(Node("function", function.name))
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
        box = Gtk.Box(spacing=6)
        label = Gtk.Label(xalign=0, hexpand=True)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        pk = Gtk.Label(label="PK")
        pk.add_css_class("caption")
        pk.add_css_class("accent")
        detail = Gtk.Label()
        detail.add_css_class("dim-label")
        detail.add_css_class("caption")
        button = Gtk.Button(icon_name="utilities-terminal-symbolic")
        button.set_tooltip_text("New query console")
        button.add_css_class("flat")
        button.set_valign(Gtk.Align.CENTER)
        button.connect("clicked", self._query_clicked, list_item)
        for child in (label, pk, detail, button):
            box.append(child)
        expander.set_child(box)
        list_item.set_child(expander)
        list_item.label = label
        list_item.pk = pk
        list_item.detail = detail
        list_item.button = button
        list_item.row_handler = 0

    def _bind_row(self, _factory, list_item: Gtk.ListItem) -> None:
        row = list_item.get_item()  # TreeListRow (passthrough=False)
        node = row.get_item()
        list_item.get_child().set_list_row(row)
        list_item.label.set_text(node.label)
        if node.kind == "note":
            list_item.label.add_css_class("dim-label")
        else:
            list_item.label.remove_css_class("dim-label")
        list_item.pk.set_visible(node.is_pk)
        list_item.detail.set_text(node.detail)
        list_item.detail.set_visible(bool(node.detail))
        list_item.button.set_visible(node.kind == "connection")
        list_item.row_handler = row.connect(
            "notify::expanded", self._on_row_expanded
        )
        if row.get_expanded():  # expanded while unbound (e.g. scrolled away)
            self._load_children(node)

    def _unbind_row(self, _factory, list_item: Gtk.ListItem) -> None:
        if list_item.row_handler:
            list_item.get_item().disconnect(list_item.row_handler)
            list_item.row_handler = 0

    def _query_clicked(self, _button, list_item: Gtk.ListItem) -> None:
        self._on_new_query(list_item.get_item().get_item().profile)

    def _on_activate(self, _view, position: int) -> None:
        row = self._view.get_model().get_item(position)
        node = row.get_item()
        if node.kind in ("table", "view"):
            self._on_open_table(node.profile, node.label)
        elif node.kind in ("connection", "category"):
            row.set_expanded(not row.get_expanded())
