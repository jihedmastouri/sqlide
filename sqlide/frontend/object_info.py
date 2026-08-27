"""Object info tabs: one read-only view for every node in the tree.

Double-clicking (or pressing Enter on) any sidebar row opens this: the
descriptor `backend/db/objects.describe` built for that node, rendered
the same way whatever the node was — a header, a key/value summary, any
number of detail tables, and the object's DDL with a copy button.

There is deliberately no screen per object type. An index, a column, a
trigger and a folder called "Indexes" all differ only in what the
descriptor put in them, so a kind the backend has no builder for still
opens (the generic fallback) instead of doing nothing.

Rows in a detail table are links: a folder lists what is inside it and
activating a row opens that child's own info view, so the tree can also
be walked from the main area. A section the descriptor calls tabular —
a listing of columns, indexes, constraints, grants — is drawn in the
same result grid the query console uses (CORE-49), so it sorts, its
columns resize and it copies as CSV/JSON/Markdown for free; a section
holding a single record stays a key/value block rather than becoming a
table one row tall. Read-only throughout — editing an object
stays with the definition tab and the table designer.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Callable

from gi.repository import Adw, Gio, GLib, GObject, Graphene, Gtk, Pango

from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.db import objects, registry
from sqlide.backend.db.base import Connector
from sqlide.backend.db.metadata import NodeRef
from sqlide.backend.workspaces import TabState
from sqlide.frontend.sql_editor import SqlEditor
from sqlide.frontend.util import describe, run_async
from sqlide.i18n import _

# Header icon per kind; mirrors the sidebar's own icons so an object
# looks the same in both places.
_KIND_ICONS = {
    "connection": "network-server-symbolic",
    "database": "drive-multidisk-symbolic",
    "category": "folder-symbolic",
    "table": "view-grid-symbolic",
    "view": "view-reveal-symbolic",
    "column": "view-list-symbolic",
    "function": "system-run-symbolic",
    "index": "view-continuous-symbolic",
    "trigger": "media-playback-start-symbolic",
    "event": "alarm-symbolic",
}


def tab_key(profile: ConnectionProfile, ref: objects.ObjectRef) -> tuple:
    """Identity of an object info tab: opening the same object twice
    focuses the open tab instead of making a second one."""
    return ("object", profile.name, ref.kind, ref.name, ref.table)


class _Row(GObject.Object):
    """One line of a detail table, with what it opens."""

    def __init__(
        self, values: tuple[str, ...], link: objects.ObjectRef | None
    ) -> None:
        super().__init__()
        self.values = values
        self.link = link


class InfoBody(Gtk.ScrolledWindow):
    """The rendered part of a descriptor: summary, detail tables, DDL.

    Split out of the info tab because the table tab's Properties side
    (CORE-04) shows the same shape for a table it already has open —
    one renderer, so a section looks and behaves the same wherever it
    is read, and a row opens the child's info view from both.
    """

    def __init__(
        self,
        on_open_link: Callable[[objects.ObjectRef], None],
        *,
        summary_title: str = "Summary",
    ) -> None:
        super().__init__(vexpand=True)
        self._on_open_link = on_open_link
        self._summary_title = summary_title
        # slug -> the group widget it was drawn as, so a deep link can
        # scroll to a named section (CORE-05); `_wanted` remembers a
        # link that arrived while the catalog read was still running.
        self._sections: dict[str, Gtk.Widget] = {}
        # The grid-backed sections of the current render, kept because
        # each owns the callbacks its grid sorts and links through.
        self._grids: list[_GridSection] = []
        self._wanted = ""
        self._selected: Gtk.Widget | None = None
        self.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self._box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=18,
            margin_top=6,
            margin_bottom=18,
            margin_start=18,
            margin_end=18,
        )
        self.set_child(Adw.Clamp(maximum_size=900, child=self._box))

    def render(
        self, info: objects.ObjectInfo, header: Gtk.Widget | None = None
    ) -> None:
        while child := self._box.get_first_child():
            self._box.remove(child)
        self._sections.clear()
        self._grids = []
        self._selected = None
        if header is not None:
            self._box.append(header)
        if info.note:
            note = Gtk.Label(label=info.note, xalign=0, wrap=True)
            note.add_css_class("dim-label")
            self._box.append(note)
        if info.summary:
            group = _summary_group(info.summary, self._summary_title)
            self._sections["general"] = group
            self._box.append(group)
        for table in info.tables:
            group = self._detail_group(table)
            if table.slug:
                self._sections[table.slug] = group
            self._box.append(group)
        if info.ddl:
            group = self._ddl_group(info.ddl)
            self._sections["ddl"] = group
            self._box.append(group)
        if self._wanted:
            self.select_section(self._wanted)

    def select_section(self, slug: str) -> None:
        """Scroll one section into view and mark it as the one that was
        asked for (CORE-05).

        Called before the descriptor has been read as well as after, so
        an unknown slug is remembered rather than dropped: the next
        render applies it.
        """
        self._wanted = slug
        group = self._sections.get(slug)
        if group is None:
            return
        self._wanted = ""
        if self._selected is not None:
            self._selected.remove_css_class("section-target")
        self._selected = group
        group.add_css_class("section-target")
        # The group may not be allocated yet on the frame it was
        # appended in; scrolling on idle gives it its position first.
        GLib.idle_add(self._scroll_to, group)

    def _scroll_to(self, group: Gtk.Widget) -> bool:
        if group.get_parent() is not self._box:  # re-rendered meanwhile
            return False
        ok, position = group.compute_point(
            self._box, Graphene.Point().init(0, 0)
        )
        if not ok:
            return False
        adjustment = self.get_vadjustment()
        top = max(0.0, position.y + self._box.get_margin_top() - 12)
        adjustment.set_value(
            min(top, max(0.0, adjustment.get_upper()
                         - adjustment.get_page_size()))
        )
        return False

    def show_message(self, text: str) -> None:
        """A one-line body — "loading…", or why there is nothing."""
        while child := self._box.get_first_child():
            self._box.remove(child)
        label = Gtk.Label(label=text, xalign=0, wrap=True)
        label.add_css_class("dim-label")
        self._box.append(label)

    def _detail_group(self, table: objects.DetailTable) -> Gtk.Widget:
        """One section, drawn as what its rows actually are: a real
        grid for a listing of like-shaped records, a key/value block
        for a single record, and the plain list for everything a
        descriptor has not called tabular (CORE-49)."""
        if table.as_grid:
            return self._grid_group(table)
        if table.tabular and len(table.rows) == 1:
            return self._record_group(table)
        return self._list_group(table)

    def _grid_group(self, table: objects.DetailTable) -> Gtk.Widget:
        group = Adw.PreferencesGroup(title=table.title)
        section = _GridSection(table, self._on_open_link)
        self._grids.append(section)
        frame = Gtk.Frame()
        frame.set_child(section.grid)
        group.add(frame)
        return group

    def _record_group(self, table: objects.DetailTable) -> Gtk.Widget:
        """A tabular section holding exactly one record: its columns
        read as a key/value block, the way the summary does. Where
        that record stands for an object, the group's header offers
        the link the row would have been."""
        row = table.rows[0]
        group = _summary_group(
            [
                (name, str(row[index]) if index < len(row) else "")
                for index, name in enumerate(table.columns)
            ],
            table.title,
        )
        link = table.link(0)
        if link is not None:
            button = Gtk.Button(label=_("Open"))
            button.add_css_class("flat")
            describe(button, f"Open {link.name}")
            button.connect("clicked", lambda *_: self._on_open_link(link))
            group.set_header_suffix(button)
        return group

    def _list_group(self, table: objects.DetailTable) -> Gtk.Widget:
        group = Adw.PreferencesGroup(title=table.title)
        if not table.rows:
            row = Adw.ActionRow(title=table.empty_note)
            row.set_sensitive(False)
            group.add(row)
            return group
        store = Gio.ListStore(item_type=_Row)
        for index, values in enumerate(table.rows):
            store.append(_Row(
                tuple(str(v) for v in values), table.link(index)
            ))
        view = Gtk.ColumnView(
            model=Gtk.SingleSelection(model=store), hexpand=True
        )
        view.add_css_class("data-table")
        view.set_show_row_separators(True)
        view.set_show_column_separators(True)
        for position, name in enumerate(table.columns):
            view.append_column(_column(position, name))
        view.connect("activate", self._row_activated, store)
        frame = Gtk.Frame()
        frame.set_child(view)
        group.add(frame)
        return group

    def _row_activated(
        self, _view, position: int, store: Gio.ListStore
    ) -> None:
        row = store.get_item(position)
        if row is None or row.link is None:
            return
        self._on_open_link(row.link)

    def _ddl_group(self, ddl: str) -> Gtk.Widget:
        group = Adw.PreferencesGroup(title=_("Definition"))
        copy = Gtk.Button(icon_name="edit-copy-symbolic")
        copy.add_css_class("flat")
        describe(copy, _("Copy the definition to the clipboard"))
        copy.connect("clicked", lambda *_: self._copy(ddl))
        group.set_header_suffix(copy)
        editor = SqlEditor(ddl, editable=False)
        editor.set_size_request(-1, 220)
        frame = Gtk.Frame()
        frame.set_child(editor)
        group.add(frame)
        return group

    def _copy(self, text: str) -> None:
        display = self.get_display()
        if display is not None:
            display.get_clipboard().set(text)


class ObjectInfoTab(Gtk.Box):
    def __init__(
        self,
        profile: ConnectionProfile,
        ref: objects.ObjectRef,
        ensure_connector: Callable[[ConnectionProfile], Connector],
        show_error: Callable[[str], None],
        on_open_object: Callable[[ConnectionProfile, objects.ObjectRef], None],
        *,
        path: str = "",
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.profile = profile
        self.ref = ref
        self.path = path
        self._ensure = ensure_connector
        self._show_error = show_error
        self._on_open_object = on_open_object

        bar = Gtk.Box(
            spacing=6,
            margin_top=6,
            margin_bottom=6,
            margin_start=6,
            margin_end=6,
        )
        self._title = Gtk.Label(label=ref.name, xalign=0, hexpand=True)
        self._title.add_css_class("heading")
        refresh = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh.add_css_class("flat")
        describe(refresh, _("Reload this object's information"))
        refresh.connect("clicked", lambda *_: self.reload())
        bar.append(self._title)
        bar.append(refresh)
        self.append(bar)

        self._body = InfoBody(self._open_link)
        self.append(self._body)

        self.reload()

    def tab_state(self) -> TabState:
        return TabState(
            kind="object",
            connection=self.profile.name,
            table=self.ref.name,
            object_kind=self.ref.kind,
            object_owner=self.ref.table,
            object_category=self.ref.category,
        )

    def reload(self) -> None:
        ref = self.ref

        def work() -> objects.ObjectInfo:
            # Through the provider rather than db/objects directly, so
            # an object that carries grants gets its Permissions
            # section here as well as in a table's Properties view
            # (CORE-11). The provider is the one that knows whether
            # this engine has a grant model at all.
            connector = self._ensure(self.profile)
            provider = registry.create_provider(self.profile.kind, connector)
            info = provider.describe(
                NodeRef(
                    kind=ref.kind,
                    name=ref.name,
                    table=ref.table,
                    category=ref.category,
                    # The connection is already pinned to the schema
                    # (window.open_object), so this only decides how
                    # the object is *named* back — qualified where the
                    # engine has schemas (PG-01).
                    schema=ref.schema or self.profile.schema,
                )
            )
            if self.path and not info.path:
                return replace(info, path=self.path)
            return info

        run_async(work, self._render, self._failed)

    def _failed(self, exc: Exception) -> None:
        # A catalog that cannot be read is still not a blank screen:
        # the header stays and the error becomes the body.
        self._render(
            objects.ObjectInfo(
                kind=self.ref.kind,
                name=self.ref.name,
                type_label=objects.TYPE_LABELS.get(
                    self.ref.kind, "Object"
                ),
                path=self.path,
                note=str(exc),
            )
        )
        self._show_error(str(exc))

    # Rendering

    def _render(self, info: objects.ObjectInfo) -> None:
        self._title.set_label(f"{info.name} · {info.type_label.lower()}")
        self._body.render(info, header=self._header(info))

    def _header(self, info: objects.ObjectInfo) -> Gtk.Widget:
        box = Gtk.Box(spacing=12, margin_top=6)
        icon = Gtk.Image.new_from_icon_name(
            _KIND_ICONS.get(info.kind, "application-x-addon-symbolic")
        )
        icon.set_pixel_size(32)
        box.append(icon)
        names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title = Gtk.Label(label=info.name, xalign=0)
        title.add_css_class("title-2")
        names.append(title)
        subtitle = Gtk.Label(
            label=info.path or f"{info.type_label} on {self.profile.name}",
            xalign=0,
            wrap=True,
        )
        subtitle.add_css_class("dim-label")
        names.append(subtitle)
        box.append(names)
        kind = Gtk.Label(label=info.type_label, valign=Gtk.Align.CENTER)
        kind.add_css_class("dim-label")
        box.append(kind)
        return box

    def _open_link(self, ref: objects.ObjectRef) -> None:
        self._on_open_object(self.profile, ref)


class _GridSection:
    """One tabular detail section, drawn in the result grid (CORE-49).

    The grid itself never sorts: it reports the column order it was
    asked for and expects its owner to re-query. There is nothing to
    re-query in a descriptor, so this sorts the rows it already holds —
    links travelling with them, so a row still opens its own object
    after a sort — and loads them again.
    """

    #: How tall a section is allowed to grow, in rows. A long listing
    #: scrolls inside its own grid rather than pushing the sections
    #: under it off the page.
    MAX_ROWS = 12
    ROW_HEIGHT = 32

    def __init__(
        self,
        table: objects.DetailTable,
        on_open_link: Callable[[objects.ObjectRef], None],
    ) -> None:
        # Imported here, not at module scope: data_grid imports this
        # module for the table tab's Properties side, so the pair can
        # only be tied together one way round.
        from sqlide.frontend.data_grid import ResultGrid

        self._table = table
        self._on_open_link = on_open_link
        self._origin = [
            (tuple(row), table.link(index))
            for index, row in enumerate(table.rows)
        ]
        self._rows = list(self._origin)
        self.grid = ResultGrid(
            table_name=table.slug or table.title,
            on_header_sort=self._sort,
            on_row_activated=self._activate,
        )
        self.grid.set_vexpand(False)
        self.grid.set_size_request(
            -1, self.ROW_HEIGHT * (min(len(self._rows), self.MAX_ROWS) + 1)
        )
        self._load()

    def _load(self) -> None:
        self.grid.set_result(
            list(self._table.columns), [row for row, _link in self._rows]
        )

    def _sort(self, order: list[tuple[str, bool]]) -> None:
        rows = list(self._origin)
        # Least significant column first, so the primary one decides:
        # Python's sort is stable, which is what composes the order.
        for name, descending in reversed(order):
            if name not in self._table.columns:
                continue
            index = self._table.columns.index(name)
            kind = self._table.column_type(index)
            rows.sort(
                key=lambda pair, i=index, k=kind: _sort_key(pair[0], i, k),
                reverse=descending,
            )
        self._rows = rows
        self._load()
        self.grid.set_sort_state(order)

    def _activate(self, index: int) -> None:
        if not 0 <= index < len(self._rows):
            return
        link = self._rows[index][1]
        if link is not None:
            self._on_open_link(link)


def _sort_key(row: tuple, index: int, kind: str):
    """One cell as something sortable: a number where the descriptor
    said the column holds numbers (so 9 comes before 10), the text
    case-folded otherwise. An unparsable number sorts last."""
    value = row[index] if index < len(row) else ""
    if kind == "number":
        try:
            return (0, float(value), "")
        except (TypeError, ValueError):
            return (1, 0.0, str(value).lower())
    return (0, 0.0, str(value).lower())


def _summary_group(
    summary: list[tuple[str, str]], title: str = "Summary"
) -> Gtk.Widget:
    group = Adw.PreferencesGroup(title=title)
    for key, value in summary:
        row = Adw.ActionRow(title=key, subtitle=str(value))
        row.add_css_class("property")
        group.add(row)
    return group


def _column(position: int, name: str) -> Gtk.ColumnViewColumn:
    factory = Gtk.SignalListItemFactory()

    def setup(_factory, list_item: Gtk.ListItem) -> None:
        list_item.set_child(
            Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, margin_start=6, margin_end=6)
        )

    def bind(_factory, list_item: Gtk.ListItem) -> None:
        values = list_item.get_item().values
        label = values[position] if position < len(values) else ""
        list_item.get_child().set_label(label)
        list_item.get_child().set_tooltip_text(label)

    factory.connect("setup", setup)
    factory.connect("bind", bind)
    column = Gtk.ColumnViewColumn(title=name, factory=factory)
    column.set_expand(True)
    column.set_resizable(True)
    return column


def properties_key(
    profile: ConnectionProfile, ref: objects.ObjectRef
) -> tuple:
    """Identity of a properties surface: the same object asked for
    twice lands on the one that is already open (CORE-47)."""
    return ("properties", profile.name, ref.kind, ref.name, ref.table)


class PropertiesView(Gtk.Box):
    """Everything about one object, in one scroll (CORE-47).

    The right side panel's Properties page and a detached properties
    window are both this widget: general information, then the sections
    this engine actually has — columns, constraints, keys, indexes,
    triggers, partitions, policies, the DDL — with every row opening
    that child object's own info view. Read-only: editing an object
    stays with the definition tab and the table designer.

    A table or a view is described by the provider's `table_properties`
    (the section set CORE-04 defined); anything else — an index, a
    function, a folder — by its own descriptor, so any node of the tree
    has properties to show.

    The panel retargets one of these as tabs change (`set_target`); the
    catalog is read when the widget is actually on screen, so a panel
    nobody has opened costs nothing.
    """

    def __init__(
        self,
        ensure_connector: Callable[[ConnectionProfile], Connector],
        show_error: Callable[[str], None],
        on_open_object: Callable[
            [ConnectionProfile, objects.ObjectRef], None
        ] | None = None,
        profile: ConnectionProfile | None = None,
        ref: objects.ObjectRef | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.profile = profile
        self.ref = ref
        self._ensure = ensure_connector
        self._show_error = show_error
        self._on_open_object = on_open_object
        self._loaded = False

        bar = Gtk.Box(
            spacing=6,
            margin_top=6,
            margin_bottom=6,
            margin_start=6,
            margin_end=6,
        )
        self._title = Gtk.Label(xalign=0, hexpand=True)
        self._title.add_css_class("heading")
        self._refresh = Gtk.Button(icon_name="view-refresh-symbolic")
        self._refresh.add_css_class("flat")
        describe(self._refresh, _("Re-read this object's properties"))
        self._refresh.connect("clicked", lambda *_: self.reload())
        bar.append(self._title)
        bar.append(self._refresh)
        self.append(bar)

        self._body = InfoBody(self._open_link, summary_title="General")
        self.append(self._body)
        # Read on the frame it first becomes visible, wherever it lives:
        # a hidden panel page and a hidden window make no queries.
        self.connect("map", lambda *_: self.ensure_loaded())
        self._show_target()

    # A detached properties window is session-only: it is a view of
    # something the workspace already remembers, not a tab to restore.
    def tab_state(self) -> None:
        return None

    def set_target(
        self,
        profile: ConnectionProfile | None,
        ref: objects.ObjectRef | None,
    ) -> None:
        """Point this view at another object — what the side panel does
        on every tab switch. The same object again is left alone, so
        switching away and back does not re-read the catalog."""
        if profile is None or ref is None:
            self.profile, self.ref = None, None
            self._loaded = False
            self._show_target()
            return
        if (
            self.profile is not None
            and self.ref is not None
            and properties_key(self.profile, self.ref)
            == properties_key(profile, ref)
        ):
            return
        self.profile, self.ref = profile, ref
        self._loaded = False
        self._show_target()
        if self.get_mapped():
            self.ensure_loaded()

    def _show_target(self) -> None:
        has_target = self.profile is not None and self.ref is not None
        self._refresh.set_visible(has_target)
        if not has_target:
            self._title.set_label("")
            self._body.show_message(
                "Open a tab, or pick an object in the tree, to see its "
                "properties"
            )
            return
        self._title.set_label(f"{self.ref.name} · properties")
        self._body.show_message("Loading…")

    def ensure_loaded(self) -> None:
        """Read the catalog the first time this view is shown; later
        looks show what is already there until Refresh is pressed."""
        if self._loaded or self.profile is None or self.ref is None:
            return
        self._loaded = True
        self.reload()

    def reload(self) -> None:
        profile, ref = self.profile, self.ref
        if profile is None or ref is None:
            return
        self._loaded = True

        def work() -> objects.ObjectInfo:
            connector = self._ensure(profile)
            provider = registry.create_provider(profile.kind, connector)
            if ref.kind in ("table", "view"):
                return provider.table_properties(
                    NodeRef("table", ref.name)
                )
            return provider.describe(
                NodeRef(
                    kind=ref.kind,
                    name=ref.name,
                    table=ref.table,
                    category=ref.category,
                    schema=ref.schema or profile.schema,
                )
            )

        run_async(work, self._body.render, self._failed)

    def _failed(self, exc: Exception) -> None:
        self._body.show_message(str(exc))
        self._show_error(str(exc))

    def select_section(self, slug: str) -> None:
        """Show one named section (CORE-05): the sidebar's Indexes row
        under a table lands here, on this table's Indexes."""
        self.ensure_loaded()
        self._body.select_section(slug)

    def _open_link(self, ref: objects.ObjectRef) -> None:
        if self._on_open_object is not None and self.profile is not None:
            self._on_open_object(self.profile, ref)


class PropertiesSurfaces(Gtk.Box):
    """One properties surface per object, swapped rather than recycled
    (CORE-50).

    The side panel used to hold a single `PropertiesView` retargeted on
    every tab switch, so opening B rewrote the surface that was showing
    A. Here each object gets a `PropertiesView` of its own, kept in a
    stack and brought to the front when that object is asked for: A is
    still A when you come back to it, and coming back costs no catalog
    read.

    Surfaces are released when their object's tab closes
    (`release`); what is left is bounded by `max_surfaces`, least
    recently shown first, so a long session browsing the tree does not
    grow without limit.
    """

    def __init__(
        self,
        make_view: Callable[
            [ConnectionProfile, objects.ObjectRef], "PropertiesView"
        ],
        max_surfaces: int = 8,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._make_view = make_view
        self._max = max_surfaces
        # Insertion order is recency: the least recently shown surface
        # is the first key, and the first to go when the cap is hit.
        self._views: dict[tuple, PropertiesView] = {}
        self._key: tuple | None = None
        self._stack = Gtk.Stack(vexpand=True, hexpand=True)
        self._placeholder = Gtk.Label(
            label=_("Open a tab, or pick an object in the tree, to see its "
            "properties"),
            margin_top=24,
            margin_start=12,
            margin_end=12,
            wrap=True,
        )
        self._placeholder.add_css_class("dim-label")
        self._stack.add_named(self._placeholder, "")
        self.append(self._stack)

    # What is on screen

    @property
    def current(self) -> "PropertiesView | None":
        """The surface showing now, or None when no object is shown."""
        return self._views.get(self._key) if self._key else None

    @property
    def ref(self) -> objects.ObjectRef | None:
        view = self.current
        return view.ref if view is not None else None

    @property
    def profile(self) -> ConnectionProfile | None:
        view = self.current
        return view.profile if view is not None else None

    def set_target(
        self,
        profile: ConnectionProfile | None,
        ref: objects.ObjectRef | None,
    ) -> None:
        """Show this object's own surface, making one the first time.
        Nothing already on screen is touched: the previous object's
        surface stays as it was, ready to be shown again."""
        if profile is None or ref is None:
            self._key = None
            self._stack.set_visible_child(self._placeholder)
            return
        key = properties_key(profile, ref)
        view = self._views.get(key)
        if view is None:
            view = self._make_view(profile, ref)
            self._views[key] = view
            self._stack.add_named(view, repr(key))
        else:
            self._views[key] = self._views.pop(key)  # most recent last
        self._key = key
        self._stack.set_visible_child(view)
        self._evict()

    def select_section(self, slug: str) -> None:
        """Deep links (CORE-05) land on the surface in front."""
        view = self.current
        if view is not None:
            view.select_section(slug)

    # Lifetime

    def release(self, key: tuple) -> None:
        """Drop one object's surface — what the window does when the
        last tab about that object closes."""
        view = self._views.pop(key, None)
        if view is None:
            return
        if self._key == key:
            self._key = None
            self._stack.set_visible_child(self._placeholder)
        self._stack.remove(view)

    def _evict(self) -> None:
        for key in list(self._views):
            if len(self._views) <= self._max:
                return
            if key != self._key:
                self.release(key)
