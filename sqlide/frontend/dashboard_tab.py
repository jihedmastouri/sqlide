"""The dashboard tab: several saved charts, refreshed together (CORE-35).

The last thing between a saved query that carries a chart (CORE-33) and
a useful operational screen is a place to put several of them at once.
This is that place, and it is deliberately thin: the dashboard's
definition lives in a TOML file (`backend/dashboards.py`), the chart
mapping lives on the saved query, and the drawing is the shared canvas
(`frontend/chart_canvas.py`, CORE-31). This module owns only the grid,
the refresh, and what a cell says when it cannot draw.

Four rules it exists to keep, three of them borrowed from the
monitoring dashboard this is modelled on (`frontend/monitor_tab.py`):

1. **Never a blank cell.** A cell is either a chart or a sentence: a
   query that failed, a saved query that was deleted, a chart mapping
   that no longer fits its columns. The sentence is in the cell.
2. **One slow cell never blocks the others.** The queries run
   sequentially on one worker thread over the dashboard's own
   connection, and each cell is redrawn as its own result arrives
   rather than at the end of the sweep. A cell that fails does not stop
   the sweep.
3. **A dashboard nobody is looking at stops querying.** Closing the
   tab stops the timer and hands the connection back; pausing stops the
   timer and leaves the last drawing on screen.
4. **Every edit persists immediately.** Adding, removing, resizing or
   reordering a cell writes the TOML file, so the layout survives a
   restart and shows up in a diff.

The dashboard's connection is its own, opened when the tab opens: a
dashboard polling on the connection the user is running transactions on
would interleave with them.
"""

from __future__ import annotations

from typing import Any, Callable, Sequence

from gi.repository import Adw, GLib, Gtk, Pango

from sqlide.backend import charts, dashboards, placeholders
from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.db import metrics, registry
from sqlide.backend.db.base import Connector, ConnectorError, ResultSet
from sqlide.backend.saved import queries as queries_store
from sqlide.backend.settings import max_chart_rows
from sqlide.backend.workspaces import TabState
from sqlide.frontend import chart_canvas
from sqlide.frontend.util import describe, icon_button, run_async
from sqlide.i18n import _

#: Rows a cell's query is allowed to bring back. The same cap the Chart
#: view honours (CORE-32): a dashboard is a picture, and a cell that
#: pulls a million rows every interval is an outage, not a chart.
CELL_ROW_CAP = 5000


class _CellCard(Gtk.Box):
    """One cell: its title, its chart, and its own controls.

    It holds no scale and no path of its own — the drawing is
    `chart_canvas.render`, exactly as the monitoring card's sparkline
    is — and no query: the tab runs those and hands the rows down.
    """

    def __init__(
        self,
        bound: dashboards.Bound,
        *,
        on_refresh: Callable[[], None],
        on_open: Callable[[], None],
        on_resize: Callable[[int, int], None],
        on_move: Callable[[int], None],
        on_remove: Callable[[], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.bound = bound
        self.add_css_class("card")
        self.set_margin_top(4)
        self.set_margin_bottom(4)
        self.set_margin_start(4)
        self.set_margin_end(4)

        self._spec: charts.ChartSpec | None = None
        self._data = charts.ChartData()

        header = Gtk.Box(spacing=4, margin_top=8, margin_start=12, margin_end=8)
        title = Gtk.Label(label=bound.cell.label(), xalign=0, hexpand=True)
        title.add_css_class("heading")
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.set_tooltip_text(bound.cell.query)
        header.append(title)
        refresh = icon_button(
            "view-refresh-symbolic", _("Refresh this cell"), on_refresh, flat=True
        )
        refresh.set_sensitive(bound.item is not None)
        header.append(refresh)
        open_button = icon_button(
            "document-edit-symbolic",
            _("Open this query in a console"),
            on_open,
            flat=True,
        )
        open_button.set_sensitive(bound.item is not None)
        header.append(open_button)
        header.append(
            self._menu_button(on_resize, on_move, on_remove)
        )
        self.append(header)

        self._notice = Gtk.Label(xalign=0, wrap=True, visible=False)
        self._notice.add_css_class("dim-label")
        self._notice.add_css_class("caption")
        self._notice.set_margin_start(12)
        self._notice.set_margin_end(12)
        self.append(self._notice)

        self._area = Gtk.DrawingArea(vexpand=True, hexpand=True)
        self._area.set_draw_func(self._draw)
        self._area.set_margin_bottom(8)
        self.append(self._area)

        style = Adw.StyleManager.get_default()
        handler = style.connect("notify::dark", lambda *_: self._area.queue_draw())
        self._area.connect("destroy", lambda *_: style.disconnect(handler))

        if bound.problem:
            # A cell whose saved query is gone says so and stays on the
            # grid: removing it would lose the layout the moment someone
            # renamed a query.
            self.show_message(bound.problem)
        else:
            self.show_message(_("Not refreshed yet."))

    def _menu_button(
        self,
        on_resize: Callable[[int, int], None],
        on_move: Callable[[int], None],
        on_remove: Callable[[], None],
    ) -> Gtk.Widget:
        """Resize, reorder and remove, as a popover rather than six more
        icons in a header that is already narrow."""
        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=6,
                      margin_bottom=6, margin_start=6, margin_end=6)
        for label, callback in (
            (_("Wider"), lambda: on_resize(1, 0)),
            (_("Narrower"), lambda: on_resize(-1, 0)),
            (_("Taller"), lambda: on_resize(0, 1)),
            (_("Shorter"), lambda: on_resize(0, -1)),
            (_("Move Earlier"), lambda: on_move(-1)),
            (_("Move Later"), lambda: on_move(1)),
            (_("Remove Cell"), on_remove),
        ):
            button = Gtk.Button(label=label)
            button.add_css_class("flat")
            button.get_child().set_xalign(0)
            button.connect(
                "clicked",
                lambda _b, fn=callback: (popover.popdown(), fn()),
            )
            box.append(button)
        popover.set_child(box)
        menu = Gtk.MenuButton(icon_name="view-more-symbolic", popover=popover)
        menu.add_css_class("flat")
        describe(menu, _("Cell layout"))
        return menu

    # What the cell shows

    def show_message(self, text: str) -> None:
        """A sentence instead of a chart. Never a blank cell."""
        self._spec = None
        self._data = charts.ChartData(reason=text)
        self._notice.set_visible(False)
        self._area.queue_draw()

    def show_result(
        self, columns: Sequence[str], rows: Sequence[Sequence[Any]], chart: str
    ) -> None:
        """Draw these rows through the saved query's chart.

        A saved chart naming a column the result no longer has is
        reported in the notice line and replaced by the inferred one —
        the same rule CORE-33 restores a tab's chart by — because a
        dashboard cell that goes blank after a schema change tells
        nobody anything.
        """
        result = _Rows(columns, rows)
        spec = charts.load_state(chart)
        message = ""
        if spec is not None:
            problems = charts.validate(spec, list(result.columns))
            if problems:
                message = " ".join(problems)
                spec = None
        if spec is None:
            inference = charts.infer(result)
            spec = inference.spec
            if spec is None:
                self.show_message(
                    inference.reason or _("This result has nothing to plot.")
                )
                self._notice.set_text(message)
                self._notice.set_visible(bool(message))
                return
        self._spec = spec
        self._data = charts.series_from(spec, result)
        notice = message
        if self._data.dropped or self._data.capped:
            counts = []
            if self._data.dropped:
                counts.append(_("%d rows dropped") % self._data.dropped)
            if self._data.capped:
                counts.append(_("%d beyond the cap") % self._data.capped)
            notice = " ".join(filter(None, [message, ", ".join(counts) + "."]))
        self._notice.set_text(notice)
        self._notice.set_visible(bool(notice))
        self._area.queue_draw()

    def _draw(self, _area, cr, width: int, height: int) -> None:
        spec = self._spec or charts.ChartSpec()
        chart_canvas.render(
            cr,
            spec,
            self._data,
            width,
            height,
            dark=Adw.StyleManager.get_default().get_dark(),
        )


class _Rows:
    """The little of a ResultSet the chart mapping reads."""

    def __init__(self, columns: Sequence[str], rows: Sequence[Sequence[Any]]):
        self.columns = [str(c) for c in columns]
        self.rows = list(rows)


class DashboardTab(Gtk.Box):
    def __init__(
        self,
        dashboard: dashboards.Dashboard,
        profile: ConnectionProfile | None,
        show_error: Callable[[str], None],
        on_open_query: Callable[[ConnectionProfile | None, str, str], None],
        store: dashboards.DashboardStore | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.dashboard = dashboard
        self.profile = profile
        self._show_error = show_error
        self._on_open_query = on_open_query
        self._store = store or dashboards.store
        self._connector: Connector | None = None
        self._cards: list[_CellCard] = []
        self._source = 0
        self._running = False
        self._closed = False
        #: Bumped on every refresh and every rebuild, so a result from
        #: a sweep the user has already moved on from is dropped rather
        #: than drawn into a cell that now holds something else.
        self._generation = 0

        header = Adw.HeaderBar()
        self._pause = Gtk.ToggleButton(icon_name="media-playback-pause-symbolic")
        describe(self._pause, _("Pause refreshing"))
        self._pause.connect("toggled", self._toggle_pause)
        header.pack_start(self._pause)
        header.pack_start(self._interval_control())
        add = Gtk.Button(label=_("Add Cell"))
        add.connect("clicked", lambda *_: self._add_cell())
        header.pack_end(add)
        refresh = icon_button(
            "view-refresh-symbolic",
            _("Refresh every cell now"),
            self.refresh_now,
            flat=True,
        )
        header.pack_end(refresh)
        self._status = Gtk.Label(xalign=1)
        self._status.add_css_class("dim-label")
        header.pack_end(self._status)

        self._banner = Adw.Banner(revealed=False)
        self._grid = Gtk.Grid(
            column_homogeneous=True,
            row_spacing=6,
            column_spacing=6,
            margin_top=8,
            margin_bottom=8,
            margin_start=8,
            margin_end=8,
        )
        self._empty = Adw.StatusPage(
            title=_("No cells yet"),
            description=_(
                "Add a cell from a saved query that carries a chart. "
                "Saving a query with its chart is the Chart view's "
                "“save with chart” action in a query console."
            ),
            icon_name="view-grid-symbolic",
            vexpand=True,
        )
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.append(self._banner)
        body.append(self._grid)
        body.append(self._empty)
        scroller = Gtk.ScrolledWindow(vexpand=True, child=body)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        view = Adw.ToolbarView(content=scroller)
        view.add_top_bar(header)
        self.append(view)

        self._store.subscribe(self._on_store_changed)
        queries_store.subscribe(self._on_queries_changed)
        self.connect("destroy", lambda *_: self.shutdown())

        self._rebuild()
        self._open()

    def tab_state(self) -> TabState:
        return TabState(
            kind="dashboard",
            connection=self.dashboard.connection,
            dashboard=self.dashboard.id,
        )

    # Building the grid

    def _interval_control(self) -> Gtk.Widget:
        box = Gtk.Box(spacing=6)
        label = Gtk.Label(label=_("Every"))
        label.add_css_class("dim-label")
        adjustment = Gtk.Adjustment(
            value=self.dashboard.interval,
            lower=0,
            upper=metrics.MAX_INTERVAL,
            step_increment=1,
        )
        self._spin = Gtk.SpinButton(adjustment=adjustment, climb_rate=1)
        describe(
            self._spin,
            _("Seconds between refreshes; 0 refreshes only on demand"),
        )
        self._spin.connect("value-changed", self._interval_changed)
        box.append(label)
        box.append(self._spin)
        seconds = Gtk.Label(label="s")
        seconds.add_css_class("dim-label")
        box.append(seconds)
        return box

    def _rebuild(self) -> None:
        """Lay the cells out again from the model. Cheap enough to be
        the answer to every edit: a dashboard is a handful of cells."""
        self._generation += 1
        while (child := self._grid.get_first_child()) is not None:
            self._grid.remove(child)
        self._cards = []
        bound = dashboards.bind(self.dashboard, queries_store.load())
        placements = dashboards.layout(self.dashboard)
        for placement in placements:
            entry = bound[placement.index]
            card = self._make_card(placement.index, entry)
            card.set_size_request(-1, placement.height * dashboards.ROW_HEIGHT)
            self._grid.attach(
                card,
                placement.column,
                placement.row,
                placement.width,
                placement.height,
            )
            self._cards.append(card)
        self._grid.set_visible(bool(placements))
        self._empty.set_visible(not placements)

    def _make_card(self, index: int, entry: dashboards.Bound) -> _CellCard:
        return _CellCard(
            entry,
            on_refresh=lambda: self.refresh_now(only=index),
            on_open=lambda: self._open_query(entry),
            on_resize=lambda dw, dh: self._resize(index, dw, dh),
            on_move=lambda offset: self._move(index, offset),
            on_remove=lambda: self._remove(index),
        )

    # Editing the layout

    def _persist(self) -> None:
        try:
            self._store.save(self.dashboard)
        except OSError as exc:
            self._show_error(f"Could not save the dashboard: {exc}")

    def _resize(self, index: int, dwidth: int, dheight: int) -> None:
        if not 0 <= index < len(self.dashboard.cells):
            return
        cell = self.dashboard.cells[index]
        cell.width = max(1, min(self.dashboard.columns, cell.width + dwidth))
        cell.height = max(
            1, min(dashboards.MAX_CELL_HEIGHT, cell.height + dheight)
        )
        self._persist()
        self._rebuild()
        self.refresh_now()

    def _move(self, index: int, offset: int) -> None:
        if self.dashboard.move(index, offset):
            self._persist()
            self._rebuild()
            self.refresh_now()

    def _remove(self, index: int) -> None:
        if not 0 <= index < len(self.dashboard.cells):
            return
        self.dashboard.remove_cell(self.dashboard.cells[index])
        self._persist()
        self._rebuild()

    def _add_cell(self) -> None:
        """Pick a saved query to add. Only queries that carry a chart
        are offered: one without would draw an inferred chart nobody
        chose, which is not what a dashboard cell is for."""
        items = dashboards.chartable(queries_store.load())
        taken = {cell.query for cell in self.dashboard.cells}
        offered = [item for item in items if item.name not in taken]
        if not offered:
            self._show_error(
                _(
                    "No saved query with a chart is left to add. Save a "
                    "query together with its chart from a query console "
                    "first."
                )
                if not items
                else _("Every saved query with a chart is already on this dashboard.")
            )
            return
        dialog = Adw.AlertDialog(
            heading=_("Add a cell"),
            body=_("Which saved query should this cell show?"),
        )
        names = [item.name for item in offered]
        dropdown = Gtk.DropDown.new_from_strings(names)
        dropdown.set_margin_top(6)
        dialog.set_extra_child(dropdown)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("add", _("Add"))
        dialog.set_default_response("add")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance(
            "add", Adw.ResponseAppearance.SUGGESTED
        )

        def responded(_dialog, response: str) -> None:
            if response != "add":
                return
            chosen = offered[dropdown.get_selected()]
            self.dashboard.add_cell(chosen.name)
            self._persist()
            self._rebuild()
            self.refresh_now()

        dialog.connect("response", responded)
        dialog.present(self)

    def _open_query(self, entry: dashboards.Bound) -> None:
        """"Open the query": a console on the dashboard's connection,
        with the saved SQL and the saved chart, so a cell that looks
        wrong is one click from the thing that produced it."""
        if entry.item is None:
            self._show_error(entry.problem)
            return
        self._on_open_query(self.profile, entry.sql, entry.chart)

    # Stores changing underneath

    def _on_store_changed(self, _dashboards) -> None:
        current = self._store.get(self.dashboard.id)
        if current is None or self._closed:
            return
        if current is not self.dashboard:
            # Re-read from disk (someone hand-edited the file, or
            # another window saved it): adopt it and lay out again.
            self.dashboard = current
            self._rebuild()

    def _on_queries_changed(self, _items) -> None:
        if not self._closed:
            self._rebuild()
            self.refresh_now()

    # The connection

    def _open(self) -> None:
        if self.profile is None:
            self._fail(
                _("This dashboard's connection, “%s”, is not in this workspace.")
                % (self.dashboard.connection or "—")
            )
            return
        self._status.set_text(_("Connecting…"))

        def work():
            if not registry.driver_available(self.profile.kind):
                raise ConnectorError(
                    f"No driver installed for {self.profile.kind} connections"
                )
            connector = registry.create_connector(
                self.profile.kind, **self.profile.connect_params()
            )
            connector.connect()
            return connector

        def done(connector: Connector) -> None:
            if self._closed:
                run_async(connector.close, lambda _r: None, lambda _e: None)
                return
            self._connector = connector
            self._status.set_text("")
            self._start_timer()
            self.refresh_now()

        def failed(exc: Exception) -> None:
            self._fail(str(exc))

        run_async(work, done, failed)

    def _fail(self, message: str) -> None:
        self._status.set_text(_("Not refreshing"))
        self._banner.set_title(message)
        self._banner.set_revealed(True)
        for card in self._cards:
            card.show_message(message)

    # Refreshing

    def _start_timer(self) -> None:
        self._stop_timer()
        interval = dashboards.clamp_interval(self.dashboard.interval)
        if self._closed or interval <= 0 or self._pause.get_active():
            return
        self._source = GLib.timeout_add_seconds(interval, self._tick)

    def _stop_timer(self) -> None:
        if self._source:
            GLib.source_remove(self._source)
        self._source = 0

    def _tick(self) -> bool:
        self.refresh_now()
        return GLib.SOURCE_CONTINUE

    def refresh_now(self, only: int | None = None) -> None:
        """Re-run every cell (or just one), sequentially, off the UI
        thread. Each cell is drawn as its own result arrives, so a slow
        cell delays nothing but itself, and a cell that raises reports
        in place while the sweep carries on."""
        connector = self._connector
        if connector is None or self._closed or self._running:
            return
        plan: list[tuple[int, str]] = []
        for index, card in enumerate(self._cards):
            if only is not None and index != only:
                continue
            entry = card.bound
            if entry.item is None:
                card.show_message(entry.problem)
                continue
            problem = _refusal(entry.sql)
            if problem:
                card.show_message(problem)
                continue
            card.show_message(_("Refreshing…"))
            plan.append((index, entry.sql))
        if not plan:
            return
        self._running = True
        self._generation += 1
        generation = self._generation
        cap = min(CELL_ROW_CAP, max_chart_rows())

        def work():
            for index, sql in plan:
                try:
                    result = connector.execute(sql, max_rows=cap)
                except Exception as exc:  # every driver has its own
                    GLib.idle_add(
                        self._cell_failed, generation, index, str(exc)
                    )
                    continue
                GLib.idle_add(self._cell_done, generation, index, result)

        def done(_result) -> None:
            self._running = False
            if not self._closed:
                self._status.set_text("")

        def failed(exc: Exception) -> None:
            self._running = False
            self._stop_timer()
            self._fail(str(exc))

        self._status.set_text(_("Refreshing…"))
        run_async(work, done, failed)

    def _cell_done(self, generation: int, index: int, result) -> bool:
        if generation != self._generation or self._closed:
            return GLib.SOURCE_REMOVE
        if 0 <= index < len(self._cards):
            card = self._cards[index]
            if isinstance(result, ResultSet):
                card.show_result(result.columns, result.rows, card.bound.chart)
            else:
                # An UPDATE saved as a "query": there is nothing to plot,
                # and saying so beats an empty chart.
                card.show_message(
                    _("This statement returned no rows to chart.")
                )
        return GLib.SOURCE_REMOVE

    def _cell_failed(self, generation: int, index: int, message: str) -> bool:
        if generation != self._generation or self._closed:
            return GLib.SOURCE_REMOVE
        if 0 <= index < len(self._cards):
            self._cards[index].show_message(message)
        return GLib.SOURCE_REMOVE

    # Pausing, interval, teardown

    def _toggle_pause(self, button: Gtk.ToggleButton) -> None:
        paused = button.get_active()
        button.set_icon_name(
            "media-playback-start-symbolic"
            if paused
            else "media-playback-pause-symbolic"
        )
        describe(
            button, _("Resume refreshing") if paused else _("Pause refreshing")
        )
        if paused:
            self._stop_timer()
            self._status.set_text(_("Paused"))
        else:
            self._status.set_text("")
            self._start_timer()
            self.refresh_now()

    def _interval_changed(self, spin: Gtk.SpinButton) -> None:
        interval = dashboards.clamp_interval(int(spin.get_value()))
        if interval == self.dashboard.interval:
            return
        self.dashboard.interval = interval
        self._persist()
        if not self._pause.get_active():
            self._start_timer()
        self._status.set_text(
            _("Refreshing every %d s") % interval
            if interval
            else _("Refresh on demand")
        )

    def shutdown(self) -> None:
        """Stop the timer and hand the connection back. A dashboard
        nobody is looking at must not keep querying."""
        if self._closed:
            return
        self._closed = True
        self._stop_timer()
        self._store.unsubscribe(self._on_store_changed)
        queries_store.unsubscribe(self._on_queries_changed)
        connector, self._connector = self._connector, None
        if connector is None:
            return
        run_async(connector.close, lambda _result: None, lambda _exc: None)


def _refusal(sql: str) -> str:
    """Why this cell will not be run, or "".

    A saved query with placeholders needs values, and a dashboard has
    nowhere to ask for them — better to say that than to run the query
    with NULLs and chart the answer to a different question.
    """
    if placeholders.find_placeholders(sql):
        return _(
            "This query has placeholders, which a dashboard cannot fill. "
            "Open it in a console to run it."
        )
    return ""


def present_dashboards(
    parent: Gtk.Widget,
    connections: Sequence[ConnectionProfile],
    on_open: Callable[[dashboards.Dashboard], None],
    on_error: Callable[[str], None],
    store: dashboards.DashboardStore | None = None,
) -> Adw.Dialog:
    """The one door to dashboards: open an existing one, create one, or
    delete one.

    They are global configuration rather than a workspace's tabs, so
    they are not in the connections tree; a single dialog is both the
    list and the "New…" form, which keeps the New menu to one item.
    """
    store = store or dashboards.store
    dialog = Adw.Dialog(title=_("Dashboards"), content_width=460)
    body = Gtk.Box(
        orientation=Gtk.Orientation.VERTICAL,
        spacing=18,
        margin_top=12,
        margin_bottom=12,
        margin_start=12,
        margin_end=12,
    )
    existing = Adw.PreferencesGroup(
        title=_("Open a dashboard"),
        description=_(
            "Each is a TOML file under the config directory, which can be "
            "hand-edited and committed."
        ),
    )
    body.append(existing)

    def fill() -> None:
        for row in list(getattr(fill, "rows", [])):
            existing.remove(row)
        rows = []
        items = store.load()
        for board in items:
            row = Adw.ActionRow(
                title=board.name,
                subtitle=_("%(connection)s · %(cells)d cells")
                % {
                    "connection": board.connection or _("no connection"),
                    "cells": len(board.cells),
                },
                activatable=True,
            )
            row.connect(
                "activated",
                lambda _r, b=board: (dialog.close(), on_open(b)),
            )
            delete = icon_button(
                "user-trash-symbolic",
                _("Delete this dashboard"),
                lambda b=board: (store.remove(b), fill()),
                flat=True,
            )
            delete.set_valign(Gtk.Align.CENTER)
            row.add_suffix(delete)
            existing.add(row)
            rows.append(row)
        if not items:
            row = Adw.ActionRow(
                title=_("No dashboards yet"),
                subtitle=_("Create one below."),
                sensitive=False,
            )
            existing.add(row)
            rows.append(row)
        fill.rows = rows

    fill()

    creation = Adw.PreferencesGroup(title=_("New dashboard"))
    name_row = Adw.EntryRow(title=_("Name"))
    name_row.set_text(store.unique_name(_("Dashboard")))
    creation.add(name_row)
    names = [profile.name for profile in connections]
    connection_row = Adw.ComboRow(
        title=_("Connection"),
        subtitle=_("Every cell runs on this one connection."),
        model=Gtk.StringList.new(names or [_("No connections")]),
    )
    connection_row.set_sensitive(bool(names))
    creation.add(connection_row)
    create = Gtk.Button(label=_("Create"), halign=Gtk.Align.END)
    create.add_css_class("suggested-action")
    creation.add(create)
    body.append(creation)

    def created(*_args) -> None:
        if not names:
            on_error(_("Add a connection to this workspace first."))
            return
        chosen = names[connection_row.get_selected()]
        board = store.create(name_row.get_text().strip() or _("Dashboard"), chosen)
        dialog.close()
        on_open(board)

    create.connect("clicked", created)

    scroller = Gtk.ScrolledWindow(child=body, propagate_natural_height=True)
    view = Adw.ToolbarView(content=scroller)
    view.add_top_bar(Adw.HeaderBar())
    dialog.set_child(view)
    dialog.present(parent)
    return dialog
