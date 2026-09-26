"""Properties: one read-only view for every node in the tree.

There is a single properties screen (CORE-59), hosted twice: as a tab
of its own (`PropertiesTab`) and, compactly, as the side panel's
Properties page beside a tab's data (`PropertiesView`). Double-clicking
(or pressing Enter on) any sidebar row, and asking for Properties
outright, both open the tab: the descriptor
`backend/db/objects.describe` — or `table_properties` for a table or a
view, which knows every section the engine has — rendered the same way
whatever the node was.

As a tab that is `SectionedBody`: a header (the object's icon, its
name, a pill saying what kind of thing it is, where it sits, and the
actions — open a query on it, re-read it, and for a connection its
settings), a row of tabs carrying each section's row count, and a page
per section — General as a three-column property grid, each listing
filling its page in the shared result grid over a filter, and the
definition with its copy button. A connection and a database add to
their General page the counts and live meters their descriptor brought
(`backend/db/overview.py`): what is asked of a server is how big it is
and how it is doing, not a list of eight attributes. The panel, which
is narrow, keeps every section in one scroll instead (`InfoBody`).

There is deliberately no screen per object type. An index, a column, a
trigger and a folder called "Indexes" all differ only in what the
descriptor put in them, so a kind the backend has no builder for still
opens (the generic fallback) instead of doing nothing — and so a table
with no meters and a connection with no definition are the same screen
with different pages in it.

Rows in a detail table are links: a folder lists what is inside it and
activating a row opens that child's own info view, so the tree can also
be walked from the main area. A section the descriptor calls tabular —
a listing of columns, indexes, constraints, grants — is drawn in the
same result grid the query console uses (CORE-49), so it sorts, its
columns resize and it copies as CSV/JSON/Markdown for free; a section
holding a single record stays a key/value block rather than becoming a
table one row tall. Read-only throughout — editing an object stays with
the definition tab and the table designer.

The tab and the right side panel are the same widget (`ObjectSurface`)
under two `Host` descriptions (CORE-59): they differ on density, on the
sections a host shows, on where the descriptor is read from and on
whether a listing may take over the page — never on how a section is
built.

An object whose whole content *is* a listing — a folder, or one
properties section of a table (Indexes, Columns, Constraints) — opens
as that listing alone, filling the tab the way the data tab does,
rather than as a page of the side panel (CORE-56); it is headed and
filtered like any other page, so a folder and the table beside it read
as one screen. Which of the two a node is comes from
`objects.grid_listing`.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable

from gi.repository import Adw, Gio, GLib, GObject, Graphene, Gtk, Pango

from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.db import objects, registry
from sqlide.backend.db.base import Connector
from sqlide.backend.db.metadata import MetadataProvider, NodeRef
from sqlide.backend.workspaces import TabState
from sqlide.frontend.sql_editor import SqlEditor
from sqlide.frontend.util import describe, run_async
from sqlide.i18n import N_, _

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
        compact: bool = False,
        expand_grids: bool = False,
    ) -> None:
        super().__init__(vexpand=True)
        self._on_open_link = on_open_link
        self._summary_title = summary_title
        # A body that is a whole page (one section per tab) lets its
        # grid take the room; a body stacking every section keeps each
        # one capped so the sections under it stay reachable.
        self._expand_grids = expand_grids
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
        # Density is the only thing the narrow side panel changes about
        # the body (CORE-59): the same sections, drawn closer together
        # because there is less room, not a different renderer.
        margin = 12 if compact else 18
        self._box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12 if compact else 18,
            margin_top=6,
            margin_bottom=margin,
            margin_start=margin,
            margin_end=margin,
        )
        self.set_child(
            Adw.Clamp(maximum_size=600 if compact else 900, child=self._box)
        )

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
        section = _GridSection(
            table, self._on_open_link, expand=self._expand_grids
        )
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
        *,
        expand: bool = False,
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
        # What the grid is showing of the origin, kept apart so a sort
        # and a filter compose instead of replacing one another.
        self._order: list[tuple[str, bool]] = []
        self._needle = ""
        self.grid = ResultGrid(
            table_name=table.slug or table.title,
            on_header_sort=self._sort,
            on_row_activated=self._activate,
        )
        # A section inside an info view is one of several and is
        # capped; a listing opened as a tab of its own (CORE-56) is the
        # whole page and takes all the room there is.
        self.grid.set_vexpand(expand)
        # A capped section asks for exactly the rows it has; an
        # expanding one still asks for a floor, since a grid inside a
        # scrolling page (one section per tab) has no height of its own
        # to grow from.
        rows = min(len(self._rows), self.MAX_ROWS)
        self.grid.set_size_request(
            -1, self.ROW_HEIGHT * ((max(rows, 6) if expand else rows) + 1)
        )
        self._load()

    def _load(self) -> None:
        self.grid.set_result(
            list(self._table.columns), [row for row, _link in self._rows]
        )

    def filter(self, text: str) -> int:
        """Show only the rows holding `text`, and say how many that is.

        A listing that fills a page (a database's tables) is searched,
        not scrolled: matching is over every cell, case-folded, so
        typing "view" finds the kind column as readily as a name.
        """
        self._needle = text.strip().lower()
        self._apply()
        return len(self._rows)

    def _sort(self, order: list[tuple[str, bool]]) -> None:
        self._order = list(order)
        self._apply()
        self.grid.set_sort_state(order)

    def _apply(self) -> None:
        rows = list(self._origin)
        # Least significant column first, so the primary one decides:
        # Python's sort is stable, which is what composes the order.
        for name, descending in reversed(self._order):
            if name not in self._table.columns:
                continue
            index = self._table.columns.index(name)
            kind = self._table.column_type(index)
            rows.sort(
                key=lambda pair, i=index, k=kind: _sort_key(pair[0], i, k),
                reverse=descending,
            )
        if self._needle:
            rows = [
                pair for pair in rows
                if any(self._needle in str(cell).lower() for cell in pair[0])
            ]
        self._rows = rows
        self._load()

    def _activate(self, index: int) -> None:
        if not 0 <= index < len(self._rows):
            return
        link = self._rows[index][1]
        if link is not None:
            self._on_open_link(link)


#: What a size suffix is worth, for sorting a column of them: "8.2 MB"
#: has to come before "1.1 GB", which sorting the text never does.
_SIZE_UNITS = {
    "b": 1.0, "kb": 1024.0, "mb": 1024.0 ** 2,
    "gb": 1024.0 ** 3, "tb": 1024.0 ** 4,
}


def _sort_key(row: tuple, index: int, kind: str):
    """One cell as something sortable: a number where the descriptor
    said the column holds numbers (so 9 comes before 10), the text
    case-folded otherwise. An unparsable number sorts last.

    A column the descriptor already formatted — "12,480" rows, "8.2 MB"
    on disk — is sorted by what it means rather than by how it reads:
    the number is what was there before it was made legible.
    """
    value = row[index] if index < len(row) else ""
    if kind in ("number", "count", "size"):
        number = _numeric(str(value), kind)
        if number is not None:
            return (0, number, "")
        return (1, 0.0, str(value).lower())
    return (0, 0.0, str(value).lower())


def _numeric(value: str, kind: str) -> float | None:
    text = value.replace(",", "").strip()
    if kind == "size":
        parts = text.split()
        if len(parts) == 2 and parts[1].lower() in _SIZE_UNITS:
            try:
                return float(parts[0]) * _SIZE_UNITS[parts[1].lower()]
            except ValueError:
                return None
    try:
        return float(text)
    except ValueError:
        return None


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


class SectionedBody(Gtk.Box):
    """One object as a tab of its own: header, tabs, pages (CORE-59).

    The properties tab has a whole window to itself, so its sections
    are not one long scroll and not a list of headings down the left
    edge either. What a tab shows is:

        header      the object's icon, its name, what kind of thing it
                    is, where it sits, and the things you came to do
                    with it — open a query on it, re-read it, and (for
                    a connection or a database) its settings
        tabs        General, then one per section the descriptor has,
                    each carrying how many rows are behind it, then
                    the definition
        General     the attributes as a three-column grid — and, where
                    the descriptor brought them, the counts as tiles
                    and the live numbers as meters above them
        a section   a listing fills the page in the shared result grid
                    over a filter; a section that is one record is a
                    property grid like General's

    Every object is drawn this way — a table, an index, a folder, a
    connection — and the only thing that varies is what its descriptor
    put in it: a table has no live meters, a connection has no
    definition, and neither has a page it does not fill. The sections
    themselves are built by the same code the narrow side panel uses
    (`_GridSection`, `InfoBody`), so a listing sorts and copies the
    same way wherever it is read.
    """

    #: Kinds whose header says whether they are connected. For a table
    #: or an index the answer is "obviously, you are looking at it";
    #: for the two roots of the tree it is the first thing asked.
    _LIVE_KINDS = ("connection", "database")

    def __init__(
        self,
        on_open_link: Callable[[objects.ObjectRef], None],
        *,
        summary_title: str = "General",
        on_refresh: Callable[[], None] | None = None,
        on_open_query: Callable[[], None] | None = None,
        on_settings: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._on_open_link = on_open_link
        self._summary_title = summary_title
        self._on_refresh = on_refresh
        self._on_open_query = on_open_query
        self._on_settings = on_settings
        self._wanted = ""
        self._grids: list[_GridSection] = []
        self._pages: dict[str, Gtk.Widget] = {}
        self._tabs: dict[str, Gtk.ToggleButton] = {}
        self._switching = False

        self._header = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            margin_top=12,
            margin_start=18,
            margin_end=18,
        )
        self.append(self._header)
        self._tab_bar = Gtk.Box(spacing=4, margin_start=18, margin_end=18)
        self._tab_bar.add_css_class("overview-tabs")
        # A table can have a dozen sections: the row scrolls sideways
        # rather than squeezing their names past reading.
        self._tab_scroller = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.EXTERNAL,
            vscrollbar_policy=Gtk.PolicyType.NEVER,
            propagate_natural_height=True,
            child=self._tab_bar,
        )
        self.append(self._tab_scroller)
        self.append(Gtk.Separator())
        self._stack = Gtk.Stack(vexpand=True)
        self._stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.append(self._stack)

    # What the surface asks of a body

    def render(
        self, info: objects.ObjectInfo, header: Gtk.Widget | None = None
    ) -> None:
        """Draw one descriptor. The `header` a host may pass is ignored
        here: this body builds its own, from the same descriptor, so a
        tab's header and its pages cannot disagree."""
        self._clear()
        self._header.append(self._title_bar(info))
        if info.summary or info.note or (info.overview and info.overview):
            self._add_page(
                "general", _(self._summary_title), "",
                _OverviewPage(info, self.select_section),
            )
        for index, table in enumerate(info.tables):
            self._add_page(
                table.slug or f"section-{index}",
                table.title,
                str(len(table.rows)) if table.rows else "",
                self._section_page(table),
            )
        if info.ddl:
            self._add_page(
                "ddl", _("Definition"), "", _DefinitionPage(info.ddl)
            )
        if not self._pages:
            self.show_message(_("Nothing to show for this object"))
            return
        self._tab_scroller.set_visible(True)
        self.select_section(self._wanted or next(iter(self._pages)))

    def select_section(self, slug: str) -> None:
        """Bring one named section to the front, remembering a slug
        that arrived before the descriptor did (CORE-05)."""
        self._wanted = slug
        if slug not in self._pages:
            return
        self._wanted = ""
        self._stack.set_visible_child(self._pages[slug])
        self._switching = True
        for name, tab in self._tabs.items():
            tab.set_active(name == slug)
        self._switching = False

    def show_message(self, text: str) -> None:
        """A one-line body — "loading…", or why there is nothing."""
        self._clear()
        self._tab_scroller.set_visible(False)
        label = Gtk.Label(
            label=text, xalign=0, wrap=True, valign=Gtk.Align.START,
            margin_top=12, margin_start=18, margin_end=18,
        )
        label.add_css_class("dim-label")
        self._stack.add_named(label, "message")

    @property
    def sections(self) -> list[str]:
        """The slugs this body is currently paged by, in order."""
        return list(self._pages)

    # Building

    def _title_bar(self, info: objects.ObjectInfo) -> Gtk.Widget:
        return _object_header(
            info,
            on_refresh=self._on_refresh,
            on_open_query=self._on_open_query,
            on_settings=self._on_settings,
            live=info.kind in self._LIVE_KINDS,
        )

    def _add_page(
        self, slug: str, title: str, badge: str, page: Gtk.Widget
    ) -> None:
        self._pages[slug] = page
        self._stack.add_named(page, slug)
        tab = Gtk.ToggleButton()
        tab.add_css_class("overview-tab")
        tab.add_css_class("flat")
        label = Gtk.Box(spacing=6)
        label.append(Gtk.Label(label=title))
        if badge:
            count = Gtk.Label(label=badge)
            count.add_css_class("dim-label")
            count.add_css_class("caption")
            label.append(count)
        tab.set_child(label)
        describe(tab, title)
        tab.connect("toggled", self._tab_toggled, slug)
        self._tabs[slug] = tab
        self._tab_bar.append(tab)

    def _tab_toggled(self, tab: Gtk.ToggleButton, slug: str) -> None:
        if self._switching:
            return
        if not tab.get_active():
            # A tab cannot be turned off, only replaced: clicking the
            # one already showing keeps it showing.
            self._switching = True
            tab.set_active(True)
            self._switching = False
            return
        self.select_section(slug)

    def _section_page(self, table: objects.DetailTable) -> Gtk.Widget:
        """One section, as what its rows actually are (CORE-49): a
        listing over a filter, a record as a property grid, and the
        plain list for a section the descriptor never called tabular.
        """
        if table.as_grid:
            return self._listing_page(table)
        if table.tabular and len(table.rows) == 1:
            row = table.rows[0]
            return _PropertyPage([
                (name, str(row[index]) if index < len(row) else "")
                for index, name in enumerate(table.columns)
            ])
        body = InfoBody(self._on_open_link, expand_grids=True)
        body.render(objects.ObjectInfo(
            kind="", name="", type_label="", tables=[table]
        ))
        return body

    def _listing_page(self, table: objects.DetailTable) -> Gtk.Widget:
        """One listing, filling the page, with a filter over it — a
        listing long enough to be its own page is long enough that
        finding a row by eye is the wrong way to find it."""
        section = _GridSection(table, self._on_open_link, expand=True)
        self._grids.append(section)
        return _listing_box(section, table)

    def _clear(self) -> None:
        while child := self._header.get_first_child():
            self._header.remove(child)
        while child := self._tab_bar.get_first_child():
            self._tab_bar.remove(child)
        while child := self._stack.get_first_child():
            self._stack.remove(child)
        self._pages.clear()
        self._tabs.clear()
        self._grids = []


def _object_header(
    info: objects.ObjectInfo,
    *,
    on_refresh: Callable[[], None] | None = None,
    on_open_query: Callable[[], None] | None = None,
    on_settings: Callable[[], None] | None = None,
    live: bool = False,
) -> Gtk.Widget:
    """The line every properties screen leads with: the object's icon,
    its name, what kind of thing it is, where it sits — and the things
    you came to do with it.

    Shared by the sectioned body and by a listing opened as a tab of
    its own (CORE-56), so a folder is headed like the table beside it
    rather than opening as a bare grid.
    """
    box = Gtk.Box(spacing=12)
    avatar = Gtk.Image.new_from_icon_name(
        _KIND_ICONS.get(info.kind, "application-x-addon-symbolic")
    )
    avatar.set_pixel_size(24)
    avatar.add_css_class("overview-avatar")
    box.append(avatar)

    names = Gtk.Box(
        orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True
    )
    line = Gtk.Box(spacing=10)
    title = Gtk.Label(
        label=info.name, xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE
    )
    title.add_css_class("title-1")
    describe(title, info.name)
    line.append(title)
    kind = Gtk.Label(label=info.type_label.upper(), valign=Gtk.Align.CENTER)
    kind.add_css_class("kind-pill")
    line.append(kind)
    if live:
        # Whether the connection is up is the first thing asked of a
        # server or a database; of a table it is not a question.
        status = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        dot = Gtk.Label(label="●")
        dot.add_css_class("status-dot")
        status.append(dot)
        status.append(Gtk.Label(label=_("connected")))
        status.add_css_class("dim-label")
        line.append(status)
    names.append(line)
    where = info.overview.endpoint if info.overview else ""
    path = Gtk.Label(
        label=where or info.path or info.type_label, xalign=0, wrap=True
    )
    path.add_css_class("overview-path")
    path.add_css_class("dim-label")
    names.append(path)
    box.append(names)

    actions = Gtk.Box(spacing=6, valign=Gtk.Align.START)
    if on_open_query is not None:
        button = Gtk.Button(label=_("Open query"))
        button.add_css_class("suggested-action")
        describe(button, _("Open a query console on this connection"))
        button.connect("clicked", lambda *_a: on_open_query())
        actions.append(button)
    if on_refresh is not None:
        button = Gtk.Button(label=_("Refresh"))
        describe(button, _("Read this object's information again"))
        button.connect("clicked", lambda *_a: on_refresh())
        actions.append(button)
    # Settings is the connection's own dialog, so it is offered on the
    # two nodes that are a connection — not on a table, whose settings
    # are its designer.
    if on_settings is not None and live:
        button = Gtk.Button(label=_("Settings"))
        describe(button, _("Edit this connection's settings"))
        button.connect("clicked", lambda *_a: on_settings())
        actions.append(button)
    box.append(actions)
    return box


def _listing_box(
    section: "_GridSection", table: objects.DetailTable
) -> Gtk.Widget:
    """A listing filling a page: the shared result grid under a filter
    and the count of what the filter left."""
    box = Gtk.Box(
        orientation=Gtk.Orientation.VERTICAL, spacing=10,
        margin_top=12, margin_bottom=12, margin_start=18, margin_end=18,
    )
    bar = Gtk.Box(spacing=12)
    entry = Gtk.SearchEntry(
        placeholder_text=_("Filter %s…") % table.title.lower(),
        width_request=260,
    )
    counted = Gtk.Label(xalign=0)
    counted.add_css_class("dim-label")

    def filtered(*_a) -> None:
        shown = section.filter(entry.get_text())
        counted.set_label(
            _("%d items") % shown if shown == len(table.rows)
            else _("%(shown)d of %(total)d") % {
                "shown": shown, "total": len(table.rows)
            }
        )

    entry.connect("search-changed", filtered)
    bar.append(entry)
    bar.append(counted)
    box.append(bar)
    frame = Gtk.Frame(vexpand=True)
    frame.set_child(section.grid)
    box.append(frame)
    filtered()
    return box


class _PropertyPage(Gtk.ScrolledWindow):
    """A page that is one key/value block — General for most objects,
    and any section holding a single record (CORE-49)."""

    def __init__(self, summary: list[tuple[str, str]]) -> None:
        super().__init__(vexpand=True)
        self.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=10,
            margin_top=16, margin_bottom=18, margin_start=18, margin_end=18,
        )
        box.append(_property_grid(summary))
        self.set_child(box)


class _DefinitionPage(Gtk.Box):
    """The object's DDL, filling the page, with the copy button the
    side panel's Definition group has."""

    def __init__(self, ddl: str) -> None:
        super().__init__(
            orientation=Gtk.Orientation.VERTICAL, spacing=10,
            margin_top=12, margin_bottom=12, margin_start=18, margin_end=18,
        )
        bar = Gtk.Box(spacing=12)
        caption = Gtk.Label(label=_("Definition").upper(), xalign=0,
                            hexpand=True)
        caption.add_css_class("section-caption")
        caption.add_css_class("dim-label")
        bar.append(caption)
        copy = Gtk.Button(icon_name="edit-copy-symbolic")
        copy.add_css_class("flat")
        describe(copy, _("Copy the definition to the clipboard"))
        copy.connect("clicked", lambda *_a: self._copy(ddl))
        bar.append(copy)
        self.append(bar)
        frame = Gtk.Frame(vexpand=True)
        frame.set_child(SqlEditor(ddl, editable=False))
        self.append(frame)

    def _copy(self, text: str) -> None:
        display = self.get_display()
        if display is not None:
            display.get_clipboard().set(text)


class _OverviewPage(Gtk.ScrolledWindow):
    """The General page: the object's attributes, under the counts and
    live meters where its descriptor brought any (a connection, a
    database) and on their own where it did not (everything else)."""

    def __init__(
        self,
        info: objects.ObjectInfo,
        on_section: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(vexpand=True)
        self.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=10,
            margin_top=16, margin_bottom=18, margin_start=18, margin_end=18,
        )
        panels = info.overview
        if panels is not None and panels.counts:
            box.append(_tiles(panels.counts, on_section))
        if panels is not None and panels.meters:
            box.append(_caption(_("Live")))
            box.append(_meters(panels.meters))
        if info.note:
            note = Gtk.Label(label=info.note, xalign=0, wrap=True)
            note.add_css_class("dim-label")
            box.append(note)
        if info.summary:
            box.append(_caption(_("Properties")))
            box.append(_property_grid(info.summary))
        self.set_child(box)


def _caption(text: str) -> Gtk.Widget:
    label = Gtk.Label(label=text.upper(), xalign=0, margin_top=10)
    label.add_css_class("section-caption")
    label.add_css_class("dim-label")
    return label


def _tiles(
    counts, on_section: Callable[[str], None] | None = None
) -> Gtk.Widget:
    """The counts, as a row of tiles that wraps rather than shrinking:
    a number is unreadable once its tile is narrower than the word
    above it.

    A tile counting something the same descriptor lists is a way into
    that listing: "TABLES 4" opens the tab holding the four.
    """
    flow = Gtk.FlowBox(
        selection_mode=Gtk.SelectionMode.NONE,
        homogeneous=True,
        column_spacing=10,
        row_spacing=10,
        min_children_per_line=2,
        max_children_per_line=max(1, len(counts)),
    )
    # Homogeneous tiles share whatever the line is given, so a row of
    # six fills the page and a row of two would each be half of it —
    # a lone count blown up to 400 px reads as an error. Below three,
    # the row keeps its natural size and sits at the start.
    flow.set_halign(
        Gtk.Align.FILL if len(counts) >= 3 else Gtk.Align.START
    )
    for count in counts:
        tile = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=6,
            width_request=140,
        )
        tile.add_css_class("stat-tile")
        label = Gtk.Label(label=count.label.upper(), xalign=0)
        label.add_css_class("stat-label")
        label.add_css_class("dim-label")
        tile.append(label)
        value = Gtk.Label(label=count.value, xalign=0)
        value.add_css_class("stat-value")
        tile.append(value)
        if count.section and on_section is not None:
            button = Gtk.Button(child=tile)
            button.add_css_class("flat")
            button.add_css_class("stat-button")
            describe(button, f"Show {count.label.lower()}")
            button.connect(
                "clicked", lambda *_a, slug=count.section: on_section(slug)
            )
            flow.append(button)
            continue
        flow.append(tile)
    return flow


def _meters(meters) -> Gtk.Widget:
    """The live row: one card per number, wrapped to the width there is.

    The cards are read together, so they share a line while a line has
    room for them; when the page is narrower than that, the row wraps
    onto a second line rather than pushing the page sideways and
    cutting the last card off at the edge.
    """
    row = Gtk.FlowBox(
        selection_mode=Gtk.SelectionMode.NONE,
        homogeneous=True,
        column_spacing=10,
        row_spacing=10,
        min_children_per_line=1,
        max_children_per_line=max(1, len(meters)),
    )
    for meter in meters:
        card = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=6,
            width_request=200,
        )
        card.add_css_class("meter-card")
        line = Gtk.Box(spacing=12)
        label = Gtk.Label(
            label=meter.label, xalign=0, hexpand=True,
            max_width_chars=12, ellipsize=Pango.EllipsizeMode.END,
        )
        describe(label, meter.label)
        line.append(label)
        if meter.note:
            note = Gtk.Label(
                label=meter.note, xalign=1,
                max_width_chars=10, ellipsize=Pango.EllipsizeMode.END,
            )
            note.add_css_class("dim-label")
            note.add_css_class("caption")
            describe(note, meter.note)
            line.append(note)
        card.append(line)
        value = Gtk.Label(label=meter.value, xalign=0)
        value.add_css_class("meter-value")
        card.append(value)
        if meter.fraction >= 0:
            bar = Gtk.ProgressBar(fraction=min(1.0, meter.fraction))
            bar.add_css_class("meter-bar")
            if meter.tone:
                bar.add_css_class(f"meter-{meter.tone}")
            card.append(bar)
        row.append(card)
    return row


#: How many key/value pairs a properties block puts on one line at
#: most. Three fits the widths a tab has and keeps a long value (a list
#: of creatable kinds) from being wrapped into a column of single
#: words; a narrower page gets fewer.
_PROPERTY_COLUMNS = 3

#: The width a key/value pair needs before it is worth keeping beside
#: another one: the key column plus room for a value.
_PROPERTY_PAIR_WIDTH = 260


def _property_grid(summary: list[tuple[str, str]]) -> Gtk.Widget:
    """The same pairs the side panel lists as rows, laid across the
    width a tab has: up to three to a line, key beside value, wrapping
    down onto the next line when the page is too narrow for three.

    Every key gets the width of the widest of them and every pair the
    same width as the others, so values line up down the page however
    long the keys above them are — a column of ragged values is the
    thing that makes a wide properties block unreadable.
    """
    flow = Gtk.FlowBox(
        selection_mode=Gtk.SelectionMode.NONE,
        homogeneous=True,
        column_spacing=24,
        row_spacing=6,
        margin_top=6,
        min_children_per_line=1,
        max_children_per_line=_PROPERTY_COLUMNS,
        valign=Gtk.Align.START,
    )
    keys = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
    for key, value in summary:
        pair = Gtk.Box(spacing=12, width_request=_PROPERTY_PAIR_WIDTH)
        # A long key wraps rather than widening its column: one
        # 24-character attribute name would otherwise take the width
        # the values of that column need.
        name = Gtk.Label(
            label=key, xalign=0, wrap=True, valign=Gtk.Align.START,
            width_request=110, max_width_chars=16,
        )
        name.add_css_class("property-key")
        name.add_css_class("dim-label")
        keys.add_widget(name)
        pair.append(name)
        shown = Gtk.Label(
            label=str(value), xalign=0, wrap=True, hexpand=True,
            valign=Gtk.Align.START, selectable=True,
        )
        shown.add_css_class("property-value")
        pair.append(shown)
        flow.append(pair)
    return flow


def _at(info: objects.ObjectInfo, path: str) -> objects.ObjectInfo:
    """The descriptor, told where in the tree it was opened from — the
    line the header shows under the object's name. A descriptor that
    already knows its own path keeps it.
    """
    if not path or info.path:
        return info
    return replace(info, path=path)


def properties_key(
    profile: ConnectionProfile, ref: objects.ObjectRef
) -> tuple:
    """Identity of a properties surface: the same object asked for
    twice lands on the one that is already open (CORE-47)."""
    return ("properties", profile.name, ref.kind, ref.name, ref.table)


@dataclass(frozen=True)
class Host:
    """What a surface's host differs on (CORE-59).

    The content of "what this object is" is one renderer; a host only
    says how much room it has, which sections it may show, and where
    its descriptor comes from. Everything else — the summary, the
    sections, the DDL, the links out of a row — is the same widget in
    both places, so the panel and a tab cannot drift apart again.
    """

    #: Heading of the key/value block: a tab calls it the summary, the
    #: panel calls it general information.
    summary_title: str
    #: Narrow (the side panel) or wide (a tab of its own).
    compact: bool
    #: Draw one section per page behind a switcher rather than as one
    #: scroll. Only a tab has the room for it.
    sectioned: bool
    #: Show the big icon/name/path header above the body. A tab has the
    #: room for it; the panel's own title bar already says the object.
    show_header: bool
    #: Route a listing to a full-tab grid instead of the info view
    #: (CORE-56). Only a tab is a destination, so only a tab routes.
    grid_listing: bool
    #: Read a table or a view through `table_properties` (the CORE-04
    #: section set) rather than the generic descriptor.
    table_properties: bool
    #: Wait for the surface to be shown before reading the catalog: a
    #: panel page nobody opened costs nothing.
    lazy: bool
    #: Drop the Permissions section for a user or a role (CORE-53):
    #: what an account may do is the permission editor's screen, not a
    #: listing inlined beside its attributes. A host rule, applied to
    #: the one descriptor both surfaces render — objects that carry
    #: grants keep their Permissions section here (CORE-11).
    drop_principal_grants: bool = False
    #: Tooltip of the Refresh button, marked for extraction and
    #: translated where it is shown: a module-level literal must not
    #: translate at import time (CORE-46).
    refresh_tip: str = ""


#: A tab: wide, headed, and the one host a listing may open in as a
#: grid of its own.
TAB_HOST = Host(
    summary_title="General",
    compact=False,
    sectioned=True,
    show_header=True,
    grid_listing=True,
    table_properties=True,
    lazy=False,
    refresh_tip=N_("Reload this object's information"),
)

#: The side panel (and a properties window torn off it): narrow, no
#: header of its own, and never a grid page — it is a summary beside
#: something else. Grants for a principal are dropped here (CORE-53):
#: what an account may do is the permission editor's screen, so the
#: panel states the rule once, for whatever descriptor it is handed.
PANEL_HOST = Host(
    summary_title="General",
    compact=True,
    sectioned=False,
    show_header=False,
    grid_listing=False,
    table_properties=True,
    lazy=True,
    drop_principal_grants=True,
    refresh_tip=N_("Re-read this object's properties"),
)


class ObjectSurface(Gtk.Box):
    """One object, rendered once, hosted twice (CORE-59).

    A title bar, the descriptor read off the provider, and `InfoBody`
    drawing it — the summary, the detail sections CORE-49 draws as
    grids, and the DDL, every row a link into that child's own view.
    Read-only throughout (CORE-47): editing an object stays with the
    definition tab and the table designer.

    `PropertiesTab` and `PropertiesView` are this widget with a
    different `Host`; there is no second section-building path between
    them. What differs is density, which sections a host shows, where
    the descriptor comes from, and whether a listing may take over the
    page as a grid (CORE-56).
    """

    HOST = TAB_HOST

    def __init__(
        self,
        ensure_connector: Callable[[ConnectionProfile], Connector],
        show_error: Callable[[str], None],
        on_open_object: Callable[
            [ConnectionProfile, objects.ObjectRef], None
        ] | None = None,
        profile: ConnectionProfile | None = None,
        ref: objects.ObjectRef | None = None,
        *,
        path: str = "",
        on_open_query: Callable[[ConnectionProfile], None] | None = None,
        on_edit_connection: Callable[[ConnectionProfile], None] | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.profile = profile
        self.ref = ref
        self.path = path
        self._ensure = ensure_connector
        self._show_error = show_error
        self._on_open_object = on_open_object
        # Offered by the sectioned body's header, and only where the
        # host was given them: a properties screen is read-only about
        # the object it describes, but it is also the obvious place to
        # open a console on it — and, for a connection, to fix its
        # settings.
        self._on_open_query = on_open_query
        self._on_edit_connection = on_edit_connection
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
        describe(self._refresh, _(self.HOST.refresh_tip))
        self._refresh.connect("clicked", lambda *_: self.reload())
        bar.append(self._title)
        bar.append(self._refresh)
        self._bar = bar
        self.append(bar)

        self._body = (
            SectionedBody(
                self._open_link,
                summary_title=self.HOST.summary_title,
                on_refresh=self.reload,
                on_open_query=(
                    self._open_query if on_open_query is not None else None
                ),
                on_settings=(
                    self._edit_connection
                    if on_edit_connection is not None
                    else None
                ),
            )
            if self.HOST.sectioned
            else InfoBody(
                self._open_link,
                summary_title=self.HOST.summary_title,
                compact=self.HOST.compact,
            )
        )
        # A sectioned body carries its own header line, its actions and
        # its tabs, so the plain title bar above it would only say the
        # object's name a second time.
        self._bar.set_visible(not self.HOST.sectioned)
        # A listing opens as a grid and an object as the info view
        # (CORE-56); which of the two a descriptor is stays unknown
        # until it has been read, so the surface holds both and shows
        # the one that fits. A host that is not a destination never
        # leaves "info".
        self._grid_holder = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, vexpand=True
        )
        self._grid: _GridSection | None = None
        self._stack = Gtk.Stack(vexpand=True)
        self._stack.add_named(self._body, "info")
        self._stack.add_named(self._grid_holder, "grid")
        self.append(self._stack)

        self._show_target()
        if self.HOST.lazy:
            # Read on the frame it first becomes visible, wherever it
            # lives: a hidden panel page and a hidden window make no
            # queries.
            self.connect("map", lambda *_: self.ensure_loaded())
        else:
            self.reload()

    # What is being shown

    def set_target(
        self,
        profile: ConnectionProfile | None,
        ref: objects.ObjectRef | None,
    ) -> None:
        """Point this surface at another object — what the side panel
        does on every tab switch. The same object again is left alone,
        so switching away and back does not re-read the catalog."""
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
        self._title.set_label(self._waiting_title())
        self._stack.set_visible_child_name("info")
        self._body.show_message("Loading…")

    def _waiting_title(self) -> str:
        """The title bar before the descriptor has been read."""
        return f"{self.ref.name} · properties"

    def _heading(self, info: objects.ObjectInfo) -> str:
        """The title bar once the descriptor is in."""
        return self._waiting_title()

    # Reading

    def ensure_loaded(self) -> None:
        """Read the catalog the first time this surface is shown; later
        looks show what is already there until Refresh is pressed."""
        if self._loaded or self.profile is None or self.ref is None:
            return
        self.reload()

    def reload(self) -> None:
        profile, ref = self.profile, self.ref
        if profile is None or ref is None:
            return
        self._loaded = True

        def work() -> objects.ObjectInfo:
            # Through the provider rather than db/objects directly, so
            # an object that carries grants gets its Permissions
            # section here as well as in a table's Properties view
            # (CORE-11). The provider is the one that knows whether
            # this engine has a grant model at all.
            connector = self._ensure(profile)
            provider = registry.create_provider(profile.kind, connector)
            if self.HOST.table_properties and ref.kind in ("table", "view"):
                return _at(
                    provider.table_properties(NodeRef("table", ref.name)),
                    self.path,
                )
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
                    schema=ref.schema or profile.schema,
                )
            )
            return _at(info, self.path)

        run_async(work, self._render, self._failed)

    def _failed(self, exc: Exception) -> None:
        self._body.show_message(str(exc))
        self._show_error(str(exc))

    # Rendering

    def _render(self, info: objects.ObjectInfo) -> None:
        info = self._for_host(info)
        self._title.set_label(self._heading(info))
        table = (
            objects.grid_listing(self.ref.kind, info)
            if self.HOST.grid_listing and self.ref is not None
            else None
        )
        if table is None:
            self._show_grid(None)
            self._body.render(info, header=self._header(info))
            return
        self._show_grid(
            _GridSection(table, self._open_link, expand=True), info, table
        )

    def _for_host(self, info: objects.ObjectInfo) -> objects.ObjectInfo:
        """The descriptor as this host is allowed to show it: the
        sections this host does not show dropped, and nothing else
        touched."""
        if not self.HOST.drop_principal_grants or self.ref is None:
            return info
        if self.ref.kind not in MetadataProvider.PRINCIPAL_KINDS:
            return info
        tables = [t for t in info.tables if t.slug != "permissions"]
        if len(tables) == len(info.tables):
            return info
        return replace(info, tables=tables)

    def _show_grid(
        self,
        section: "_GridSection | None",
        info: objects.ObjectInfo | None = None,
        table: objects.DetailTable | None = None,
    ) -> None:
        """Put a listing on the page as a page of its own (CORE-56).

        Headed and filtered like every section of a properties tab, so
        a folder opened from the tree is the same screen as the object
        beside it — only with one listing instead of several.
        """
        while child := self._grid_holder.get_first_child():
            self._grid_holder.remove(child)
        self._grid = section
        if section is None:
            self._stack.set_visible_child_name("info")
            return
        if info is not None and self.HOST.sectioned:
            header = _object_header(
                info,
                on_refresh=self.reload,
                on_open_query=(
                    self._open_query
                    if self._on_open_query is not None
                    else None
                ),
            )
            header.set_margin_top(12)
            header.set_margin_start(18)
            header.set_margin_end(18)
            self._grid_holder.append(header)
            self._grid_holder.append(Gtk.Separator(margin_top=12))
        if table is not None:
            self._grid_holder.append(_listing_box(section, table))
        else:
            self._grid_holder.append(section.grid)
        self._stack.set_visible_child_name("grid")

    def _header(self, info: objects.ObjectInfo) -> Gtk.Widget | None:
        if not self.HOST.show_header:
            return None
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
        where = self.profile.name if self.profile is not None else ""
        subtitle = Gtk.Label(
            label=info.path or f"{info.type_label} on {where}",
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

    # Links and deep links

    def select_section(self, slug: str) -> None:
        """Show one named section (CORE-05): the sidebar's Indexes row
        under a table lands here, on this table's Indexes."""
        self.ensure_loaded()
        self._body.select_section(slug)

    def _open_query(self) -> None:
        if self._on_open_query is not None and self.profile is not None:
            self._on_open_query(self.profile)

    def _edit_connection(self) -> None:
        if self._on_edit_connection is not None and self.profile is not None:
            self._on_edit_connection(self.profile)

    def _open_link(self, ref: objects.ObjectRef) -> None:
        if self._on_open_object is not None and self.profile is not None:
            self._on_open_object(self.profile, ref)


class PropertiesTab(ObjectSurface):
    """Everything about one object, as a tab of its own: the wide host.

    There is one properties screen (CORE-59), and this is it opened as
    a page rather than beside one: the same descriptor the side panel
    renders — read through `table_properties` for a table or a view, so
    a tab shows every section the engine has and not the four the
    generic builder knows — with a header, the tab's own title line,
    one page per section behind a switcher, and a listing allowed to
    fill the page as a grid (CORE-56).

    A connection and a database are the exception, and open as the
    overview dashboard instead (`OverviewBody`): their descriptor
    carries counts and live meters, which is the answer to what is
    actually asked of a server or a database.

    The side panel keeps its own compact copy (`PropertiesView`) for
    reading an object beside its data; asking for properties outright
    opens this.
    """

    HOST = TAB_HOST

    def __init__(
        self,
        profile: ConnectionProfile,
        ref: objects.ObjectRef,
        ensure_connector: Callable[[ConnectionProfile], Connector],
        show_error: Callable[[str], None],
        on_open_object: Callable[[ConnectionProfile, objects.ObjectRef], None],
        *,
        path: str = "",
        on_open_query: Callable[[ConnectionProfile], None] | None = None,
        on_edit_connection: Callable[[ConnectionProfile], None] | None = None,
    ) -> None:
        super().__init__(
            ensure_connector,
            show_error,
            on_open_object,
            profile=profile,
            ref=ref,
            path=path,
            on_open_query=on_open_query,
            on_edit_connection=on_edit_connection,
        )

    def tab_state(self) -> TabState:
        return TabState(
            kind="object",
            connection=self.profile.name,
            table=self.ref.name,
            object_kind=self.ref.kind,
            object_owner=self.ref.table,
            object_category=self.ref.category,
        )

    def _waiting_title(self) -> str:
        return self.ref.name

    def _heading(self, info: objects.ObjectInfo) -> str:
        """The line above the body: the object and what it is — and,
        for a listing, the object it is a listing of (CORE-56)."""
        if self.ref.kind == "section" and self.ref.table:
            return f"{self.ref.table} · {info.name.lower()}"
        return f"{info.name} · {info.type_label.lower()}"

    def _failed(self, exc: Exception) -> None:
        # A catalog that cannot be read is still not a blank screen:
        # the header stays and the error becomes the body.
        self._render(
            objects.ObjectInfo(
                kind=self.ref.kind,
                name=self.ref.name,
                type_label=objects.TYPE_LABELS.get(self.ref.kind, "Object"),
                path=self.path,
                note=str(exc),
            )
        )
        self._show_error(str(exc))


class PropertiesView(ObjectSurface):
    """Everything about one object, in one scroll (CORE-47).

    The right side panel's Properties page and a detached properties
    window are both this widget: the same renderer a tab uses, in the
    narrow host — general information, then the sections this engine
    actually has, with every row opening that child object's own info
    view. Read-only: editing an object stays with the definition tab
    and the table designer.

    A table or a view is described by the provider's `table_properties`
    (the section set CORE-04 defined); anything else — an index, a
    function, a folder — by its own descriptor, so any node of the tree
    has properties to show. Grants are not part of what the panel shows
    for a user or a role (CORE-53).

    The panel retargets one of these as tabs change (`set_target`); the
    catalog is read when the widget is actually on screen, so a panel
    nobody has opened costs nothing.
    """

    HOST = PANEL_HOST

    # A detached properties window is session-only: it is a view of
    # something the workspace already remembers, not a tab to restore.
    def tab_state(self) -> None:
        return None


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
