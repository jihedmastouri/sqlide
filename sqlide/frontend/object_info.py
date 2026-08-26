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
from sqlide.backend.db import objects
from sqlide.backend.db.base import Connector
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

        self._body = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=18,
            margin_top=6,
            margin_bottom=18,
            margin_start=18,
            margin_end=18,
        )
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(Adw.Clamp(maximum_size=900, child=self._body))
        self.append(scroller)

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
        while child := self._body.get_first_child():
            self._body.remove(child)
        self._title.set_label(f"{info.name} · {info.type_label.lower()}")
        self._body.append(self._header(info))
        if info.note:
            note = Gtk.Label(label=info.note, xalign=0, wrap=True)
            note.add_css_class("dim-label")
            self._body.append(note)
        if info.summary:
            self._body.append(_summary_group(info.summary))
        for table in info.tables:
            self._body.append(self._detail_group(table))
        if info.ddl:
            self._body.append(self._ddl_group(info.ddl))

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
        self._on_open_object(self.profile, row.link)

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


def _summary_group(summary: list[tuple[str, str]]) -> Gtk.Widget:
    group = Adw.PreferencesGroup(title="Summary")
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
