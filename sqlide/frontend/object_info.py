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
be walked from the main area. Read-only throughout — editing an object
stays with the definition tab and the table designer.
"""

from __future__ import annotations

from typing import Callable

from gi.repository import Adw, Gio, GObject, Gtk, Pango

from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.db import objects, registry
from sqlide.backend.db.base import Connector
from sqlide.backend.db.metadata import NodeRef
from sqlide.backend.workspaces import TabState
from sqlide.frontend.sql_editor import SqlEditor
from sqlide.frontend.util import describe, run_async

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
        if header is not None:
            self._box.append(header)
        if info.note:
            note = Gtk.Label(label=info.note, xalign=0, wrap=True)
            note.add_css_class("dim-label")
            self._box.append(note)
        if info.summary:
            self._box.append(
                _summary_group(info.summary, self._summary_title)
            )
        for table in info.tables:
            self._box.append(self._detail_group(table))
        if info.ddl:
            self._box.append(self._ddl_group(info.ddl))

    def show_message(self, text: str) -> None:
        """A one-line body — "loading…", or why there is nothing."""
        while child := self._box.get_first_child():
            self._box.remove(child)
        label = Gtk.Label(label=text, xalign=0, wrap=True)
        label.add_css_class("dim-label")
        self._box.append(label)

    def _detail_group(self, table: objects.DetailTable) -> Gtk.Widget:
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
        group = Adw.PreferencesGroup(title="Definition")
        copy = Gtk.Button(icon_name="edit-copy-symbolic")
        copy.add_css_class("flat")
        describe(copy, "Copy the definition to the clipboard")
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
        describe(refresh, "Reload this object's information")
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
            return objects.describe(
                self._ensure(self.profile),
                ref.kind,
                ref.name,
                table=ref.table,
                category=ref.category,
                path=self.path,
            )

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


class TablePropertiesView(Gtk.Box):
    """The Properties side of a table tab (CORE-04).

    Everything about the open table in one scroll: general information,
    then the sections this engine actually has — the metadata provider
    decides which, so MySQL never shows a Policies heading and SQLite
    never shows Partitions. Rows link on into the child object's own
    info view, the same as anywhere else a detail table is drawn.

    Loaded lazily on the first switch and kept afterwards: the grid it
    shares a tab with keeps its rows, its edits and its scroll position
    while this is on screen, because both are children of one stack.
    """

    def __init__(
        self,
        profile: ConnectionProfile,
        table: str,
        ensure_connector: Callable[[ConnectionProfile], Connector],
        show_error: Callable[[str], None],
        on_open_object: Callable[
            [ConnectionProfile, objects.ObjectRef], None
        ] | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.profile = profile
        self.table = table
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
        title = Gtk.Label(label=f"{table} · properties", xalign=0, hexpand=True)
        title.add_css_class("heading")
        refresh = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh.add_css_class("flat")
        describe(refresh, "Re-read this table's properties")
        refresh.connect("clicked", lambda *_: self.reload())
        bar.append(title)
        bar.append(refresh)
        self.append(bar)

        self._body = InfoBody(self._open_link, summary_title="General")
        self._body.show_message("Loading…")
        self.append(self._body)

    def ensure_loaded(self) -> None:
        """Read the catalog the first time the view is shown; later
        switches show what is already there until Refresh is pressed."""
        if self._loaded:
            return
        self._loaded = True
        self.reload()

    def reload(self) -> None:
        self._loaded = True
        profile, table = self.profile, self.table

        def work() -> objects.ObjectInfo:
            connector = self._ensure(profile)
            provider = registry.create_provider(profile.kind, connector)
            return provider.table_properties(NodeRef("table", table))

        run_async(work, self._body.render, self._failed)

    def _failed(self, exc: Exception) -> None:
        self._body.show_message(str(exc))
        self._show_error(str(exc))

    def _open_link(self, ref: objects.ObjectRef) -> None:
        if self._on_open_object is not None:
            self._on_open_object(self.profile, ref)
