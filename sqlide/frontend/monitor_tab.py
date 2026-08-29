"""The monitoring dashboard: what this server is doing right now.

One tab per connection, opened from the connection row in the sidebar
(CORE-15). It draws what the spike in docs/monitoring-spike.md found a
plain client connection can see, and nothing else:

* **Sessions** — every backend/thread with its user, database, state,
  duration and SQL, sortable and filterable, with Cancel Query and End
  Session on the selected row once the privilege check says they would
  work.
* **Throughput and health** — transactions or queries per second,
  sessions against `max_connections`, cache hit ratio and lock waits,
  each a sparkline over a rolling five minutes.
* **Storage** — database sizes and the largest tables, on their own
  60-second timer because that query is the expensive one and the
  numbers barely move.
* **Not available** — one row per source the probe
  (`backend/db/monitoring.py`) says this connection cannot read, saying
  which grant, extension or server version would fix it.

Three rules this file exists to keep:

1. **Never a blank chart.** A panel is either drawn from data or
   replaced by the reason it is not. The reason comes from the probe,
   which runs once when the tab opens.
2. **Never silent degradation.** PostgreSQL blanks other sessions' SQL
   and MySQL hides other accounts' threads instead of refusing, and both
   look like an idle server. Every sample carries that verdict and the
   banner above the sessions list shows it.
3. **Never a faked host metric.** CPU, RAM and disk of the machine are
   not on offer over SQL; the footer says so once, in words.

The tab keeps a connection of its own — polling must never interleave
with the user's transactions — opened when it opens and closed when it
closes, along with both timers.
"""

from __future__ import annotations

from typing import Callable

from gi.repository import Adw, Gio, GLib, GObject, Gtk, Pango

from sqlide.backend import charts
from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.db import metrics, monitoring, registry
from sqlide.backend.db.base import Connector, ConnectorError
from sqlide.backend.settings import store as settings_store
from sqlide.backend.workspaces import TabState
from sqlide.frontend import chart_canvas, confirm
from sqlide.frontend.util import describe, run_async
from sqlide.i18n import _, format_size


#: The card's sparkline is drawn by the shared chart renderer
#: (`frontend/chart_canvas.py`, CORE-31) in its `sparkline=True` mode:
#: no axes, no ticks, no legend, one series. The colour and the
#: geometry live there, so the dashboard and the result chart cannot
#: drift apart.
_SPARK_SPEC = charts.ChartSpec(type="line", series=("value",))


class MonitorTab(Gtk.Box):
    def __init__(
        self,
        profile: ConnectionProfile,
        show_error: Callable[[str], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.profile = profile
        self._show_error = show_error
        self._connector: Connector | None = None
        self._series = metrics.Series(profile.kind)
        self._rights = metrics.SignalRights(False, "")
        self._interval = metrics.clamp_interval(
            settings_store.settings.monitor_interval
        )
        self._live_source = 0
        self._storage_source = 0
        self._sampling = False
        self._storing = False
        self._closed = False

        self._charts: dict[str, _ChartCard] = {}
        self._sessions = Gio.ListStore(item_type=_SessionRow)
        self._search_text = ""

        header = Adw.HeaderBar()
        self._pause = Gtk.ToggleButton(
            icon_name="media-playback-pause-symbolic"
        )
        describe(self._pause, _("Pause polling"))
        self._pause.connect("toggled", self._toggle_pause)
        header.pack_start(self._pause)
        header.pack_start(self._interval_control())
        refresh = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh.add_css_class("flat")
        describe(refresh, _("Sample the server now"))
        refresh.connect("clicked", lambda *_: self.refresh_now())
        header.pack_end(refresh)
        self._status = Gtk.Label(xalign=1)
        self._status.add_css_class("dim-label")
        header.pack_end(self._status)

        body = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=18,
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        self._banner = Adw.Banner(revealed=False)
        body.append(self._banner)
        body.append(self._charts_group())
        body.append(self._sessions_group())
        body.append(self._blocked_group())
        body.append(self._storage_group())
        self._unavailable = Adw.PreferencesGroup(
            title=_("Not available on this connection"),
            description="Each of these is a panel this account, server "
            "version or installation cannot fill. Nothing is hidden: the "
            "reason is the panel.",
            visible=False,
        )
        body.append(self._unavailable)
        footer = Gtk.Label(
            label=metrics.HOST_METRICS_NOTE, xalign=0, wrap=True
        )
        footer.add_css_class("dim-label")
        footer.add_css_class("caption")
        body.append(footer)

        scroller = Gtk.ScrolledWindow(vexpand=True, child=body)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        view = Adw.ToolbarView(content=scroller)
        view.add_top_bar(header)
        self.append(view)

        self.connect("destroy", lambda *_: self.shutdown())
        self._open()

    def tab_state(self) -> TabState:
        return TabState(kind="monitor", connection=self.profile.name)

    # Building the fixed parts

    def _interval_control(self) -> Gtk.Widget:
        box = Gtk.Box(spacing=6)
        label = Gtk.Label(label=_("Every"))
        label.add_css_class("dim-label")
        adjustment = Gtk.Adjustment(
            value=self._interval,
            lower=metrics.MIN_INTERVAL,
            upper=metrics.MAX_INTERVAL,
            step_increment=1,
        )
        self._spin = Gtk.SpinButton(adjustment=adjustment, climb_rate=1)
        describe(self._spin, _("Seconds between samples"))
        self._spin.connect("value-changed", self._interval_changed)
        box.append(label)
        box.append(self._spin)
        seconds = Gtk.Label(label="s")
        seconds.add_css_class("dim-label")
        box.append(seconds)
        return box

    def _charts_group(self) -> Gtk.Widget:
        group = Adw.PreferencesGroup(
            title=_("Throughput and health"),
            description="Counters are cumulative on both engines, so every "
            "line is the change since the previous sample over the last "
            f"{int(metrics.WINDOW_SECONDS / 60)} minutes.",
        )
        flow = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            homogeneous=True,
            column_spacing=12,
            row_spacing=12,
            min_children_per_line=2,
            max_children_per_line=3,
        )
        for chart in metrics.charts(self.profile.kind):
            card = _ChartCard(chart)
            self._charts[chart.name] = card
            flow.append(card)
        group.add(flow)
        return group

    def _sessions_group(self) -> Gtk.Widget:
        group = Adw.PreferencesGroup(title=_("Sessions"))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        controls = Gtk.Box(spacing=6)
        search = Gtk.SearchEntry(
            placeholder_text=_("Filter sessions"), hexpand=True
        )
        search.connect("search-changed", self._search_changed)
        controls.append(search)
        self._cancel_button = Gtk.Button(label=_("Cancel Query"))
        self._cancel_button.set_sensitive(False)
        self._cancel_button.connect(
            "clicked", lambda *_: self._signal_selected(terminate=False)
        )
        self._kill_button = Gtk.Button(label=_("End Session"))
        self._kill_button.add_css_class("destructive-action")
        self._kill_button.set_sensitive(False)
        self._kill_button.connect(
            "clicked", lambda *_: self._signal_selected(terminate=True)
        )
        controls.append(self._cancel_button)
        controls.append(self._kill_button)
        box.append(controls)

        self._rights_label = Gtk.Label(xalign=0, wrap=True, visible=False)
        self._rights_label.add_css_class("dim-label")
        self._rights_label.add_css_class("caption")
        box.append(self._rights_label)

        self._filter = Gtk.CustomFilter.new(self._matches)
        filtered = Gtk.FilterListModel(
            model=self._sessions, filter=self._filter
        )
        self._view = Gtk.ColumnView(hexpand=True)
        self._view.add_css_class("data-table")
        self._view.set_show_row_separators(True)
        sorted_model = Gtk.SortListModel(
            model=filtered, sorter=self._view.get_sorter()
        )
        self._selection = Gtk.SingleSelection(
            model=sorted_model, autoselect=False, can_unselect=True
        )
        self._selection.connect(
            "notify::selected", lambda *_: self._update_buttons()
        )
        self._view.set_model(self._selection)
        for index, name in enumerate(metrics.SESSION_COLUMNS):
            factory = Gtk.SignalListItemFactory()
            factory.connect("setup", _setup_cell)
            factory.connect("bind", _bind_cell, index)
            column = Gtk.ColumnViewColumn(title=name, factory=factory)
            column.set_resizable(True)
            column.set_expand(name == "Query")
            column.set_sorter(Gtk.CustomSorter.new(_cell_sorter(index)))
            self._view.append_column(column)
        scroller = Gtk.ScrolledWindow(child=self._view)
        scroller.set_size_request(-1, 260)
        scroller.add_css_class("card")
        box.append(scroller)
        group.add(box)
        return group

    def _blocked_group(self) -> Gtk.Widget:
        self._blocked = Adw.PreferencesGroup(
            title=_("Blocked sessions"),
            description="Sessions waiting on a lock another session holds.",
            visible=False,
        )
        self._blocked_rows: list[Gtk.Widget] = []
        return self._blocked

    def _storage_group(self) -> Gtk.Widget:
        self._storage = Adw.PreferencesGroup(
            title=_("Storage"),
            description="Sizes change slowly and cost the most to ask for, "
            f"so they are refreshed every {metrics.STORAGE_INTERVAL} "
            "seconds rather than with the panels above.",
        )
        self._storage_rows: list[Gtk.Widget] = []
        self._storage_note = Adw.ActionRow(
            title=_("Loading sizes…"), subtitle=""
        )
        self._storage.add(self._storage_note)
        self._storage_rows.append(self._storage_note)
        return self._storage

    # Opening: one probe, one connection, then the timers

    def _open(self) -> None:
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
            self._connector = connector
            statuses = monitoring.probe(self.profile.kind, connector)
            rights = metrics.signal_rights(self.profile.kind, connector)
            return statuses, rights, metrics.sample(self.profile.kind, connector)

        def done(result) -> None:
            statuses, rights, first = result
            if self._closed:
                return
            self._rights = rights
            self._rights_label.set_visible(bool(rights.detail))
            self._rights_label.set_text(rights.detail)
            self._show_unavailable(statuses)
            self._apply(first)
            self._start_timers()
            self._load_storage()

        run_async(work, done, self._failed)

    def _failed(self, exc: Exception) -> None:
        self._status.set_text(_("Not sampling"))
        self._banner.set_title(str(exc))
        self._banner.set_revealed(True)
        self._show_error(f"Monitoring {self.profile.name}: {exc}")

    def _show_unavailable(
        self, statuses: list[monitoring.SourceStatus]
    ) -> None:
        """One row per source that cannot be read, carrying the probe's
        own words. This is the "not available because X" panel: a source
        that answers partially keeps its panel and gets a caveat instead
        (see _apply)."""
        rows = 0
        for status in statuses:
            if status.available and not status.restricted:
                continue
            row = Adw.ActionRow(
                title=status.title,
                subtitle=status.detail or "Not available on this connection.",
                subtitle_lines=0,
            )
            row.add_prefix(
                Gtk.Image(
                    icon_name="dialog-information-symbolic"
                    if status.restricted
                    else "action-unavailable-symbolic"
                )
            )
            self._unavailable.add(row)
            rows += 1
        self._unavailable.set_visible(rows > 0)

    # Polling

    def _start_timers(self) -> None:
        self._stop_timers()
        if self._closed or self._pause.get_active():
            return
        self._live_source = GLib.timeout_add_seconds(
            self._interval, self._live_tick
        )
        self._storage_source = GLib.timeout_add_seconds(
            metrics.STORAGE_INTERVAL, self._storage_tick
        )

    def _stop_timers(self) -> None:
        for source in (self._live_source, self._storage_source):
            if source:
                GLib.source_remove(source)
        self._live_source = 0
        self._storage_source = 0

    def _live_tick(self) -> bool:
        self.refresh_now()
        return GLib.SOURCE_CONTINUE

    def _storage_tick(self) -> bool:
        self._load_storage()
        return GLib.SOURCE_CONTINUE

    def refresh_now(self) -> None:
        """One sample, off the timer's schedule. Also what Refresh does
        while paused: a snapshot on demand is not polling."""
        connector = self._connector
        if connector is None or self._sampling or self._closed:
            return
        self._sampling = True

        def work():
            return metrics.sample(self.profile.kind, connector)

        def done(sample) -> None:
            self._sampling = False
            if not self._closed:
                self._apply(sample)

        def failed(exc: Exception) -> None:
            self._sampling = False
            # A dropped connection would otherwise leave the last sample
            # on screen looking live. Stop, and say why.
            self._stop_timers()
            self._failed(exc)

        run_async(work, done, failed)

    def _load_storage(self) -> None:
        connector = self._connector
        if connector is None or self._storing or self._closed:
            return
        self._storing = True

        def work():
            return metrics.storage(self.profile.kind, connector)

        def done(result) -> None:
            self._storing = False
            if not self._closed:
                self._show_storage(result)

        def failed(exc: Exception) -> None:
            self._storing = False
            self._show_storage(metrics.Storage(detail=str(exc)))

        run_async(work, done, failed)

    # Rendering a sample

    def _apply(self, sample: metrics.Sample) -> None:
        self._series.add(sample)
        for name, card in self._charts.items():
            chart = card.chart
            card.update(
                self._series.points(name),
                self._series.latest(name),
                self._series.ceiling(chart),
            )
        self._show_sessions(sample)
        self._show_blocked(sample)
        if sample.masked:
            self._banner.set_title(sample.masked)
            self._banner.set_revealed(True)
        elif self._series.restarted:
            self._banner.set_title(
                "A counter went backwards — the server restarted or its "
                "statistics were reset. The charts start again from here."
            )
            self._banner.set_revealed(True)
        else:
            self._banner.set_revealed(False)
        self._status.set_text(
            "Paused" if self._pause.get_active()
            else f"Sampling every {self._interval} s"
        )

    def _show_sessions(self, sample: metrics.Sample) -> None:
        """Refill the list, putting the selected session back on the
        row it moved to — a list that reshuffles under the cursor every
        two seconds cannot have a Kill button on it."""
        selected = self._selected_session()
        wanted = selected.id if selected is not None else ""
        self._sessions.remove_all()
        for index, session in enumerate(sample.sessions):
            self._sessions.append(_SessionRow(index, session))
        if wanted:
            model = self._selection
            for position in range(model.get_n_items()):
                if model.get_item(position).session.id == wanted:
                    model.set_selected(position)
                    break
        self._update_buttons()

    def _show_blocked(self, sample: metrics.Sample) -> None:
        for row in self._blocked_rows:
            self._blocked.remove(row)
        self._blocked_rows = []
        for entry in sample.blocked:
            row = Adw.ActionRow(
                title=f"Session {entry.id} is blocked by {entry.blocked_by}",
                subtitle=" ".join(entry.query.split()) or "(no SQL shown)",
                subtitle_lines=0,
            )
            self._blocked.add(row)
            self._blocked_rows.append(row)
        self._blocked.set_visible(bool(sample.blocked))

    def _show_storage(self, storage: metrics.Storage) -> None:
        for row in self._storage_rows:
            self._storage.remove(row)
        self._storage_rows = []

        def add(title: str, subtitle: str) -> None:
            row = Adw.ActionRow(
                title=title, subtitle=subtitle, subtitle_lines=0
            )
            self._storage.add(row)
            self._storage_rows.append(row)

        if storage.detail:
            add("About these sizes", storage.detail)
        for name, size in storage.databases:
            add(name, _("database · %s") % format_size(size))
        for name, size in storage.tables[: metrics.TOP_TABLES]:
            add(name, _("table · %s") % format_size(size))
        if not storage.databases and not storage.tables:
            add("No sizes reported", "This connection sees no databases "
                "it may measure.")

    # Sessions: filtering, selection, and the two buttons

    def _search_changed(self, entry: Gtk.SearchEntry) -> None:
        self._search_text = entry.get_text().strip().casefold()
        self._filter.changed(Gtk.FilterChange.DIFFERENT)

    def _matches(self, row: "_SessionRow") -> bool:
        if not self._search_text:
            return True
        return any(
            self._search_text in cell.casefold() for cell in row.cells
        )

    def _selected_session(self) -> metrics.Session | None:
        item = self._selection.get_selected_item()
        return item.session if item is not None else None

    def _update_buttons(self) -> None:
        session = self._selected_session()
        allowed = session is not None and not session.is_self
        for button in (self._cancel_button, self._kill_button):
            button.set_sensitive(allowed)
        if session is not None and session.is_self:
            describe(
                self._kill_button,
                _("This is the dashboard's own connection"),
            )

    def _signal_selected(self, *, terminate: bool) -> None:
        session = self._selected_session()
        connector = self._connector
        if session is None or connector is None:
            return
        what = "End Session" if terminate else "Cancel Query"
        sql = " ".join(session.query.split())
        confirm.present(
            self,
            heading=f"{what} {session.id}?",
            body=(
                f"{session.user or 'A session'} on "
                f"{session.database or self.profile.name}, running for "
                f"{metrics.format_duration(session.seconds)}. "
                + (
                    "The connection is closed and anything uncommitted is "
                    "rolled back."
                    if terminate
                    else "The statement stops; the connection stays open."
                )
            ),
            statement=sql,
            confirm_label=what,
            on_confirm=lambda: self._run_signal(
                connector, session.id, terminate
            ),
        )

    def _run_signal(
        self, connector: Connector, session_id: str, terminate: bool
    ) -> None:
        def work() -> str:
            if terminate:
                return metrics.terminate_session(
                    self.profile.kind, connector, session_id
                )
            return metrics.cancel_session(
                self.profile.kind, connector, session_id
            )

        def done(message: str) -> None:
            self._status.set_text(message)
            self.refresh_now()

        run_async(work, done, lambda exc: self._show_error(str(exc)))

    # Pausing, interval, teardown

    def _toggle_pause(self, button: Gtk.ToggleButton) -> None:
        paused = button.get_active()
        button.set_icon_name(
            "media-playback-start-symbolic" if paused
            else "media-playback-pause-symbolic"
        )
        describe(button, _("Resume polling") if paused else _("Pause polling"))
        if paused:
            self._stop_timers()
            self._status.set_text(_("Paused"))
        else:
            self._start_timers()
            self.refresh_now()

    def _interval_changed(self, spin: Gtk.SpinButton) -> None:
        interval = metrics.clamp_interval(int(spin.get_value()))
        if interval == self._interval:
            return
        self._interval = interval
        # Remembered globally: the interval someone settles on is the
        # one the next dashboard should open with.
        settings_store.update(monitor_interval=interval)
        if not self._pause.get_active():
            self._start_timers()
        self._status.set_text(f"Sampling every {interval} s")

    def shutdown(self) -> None:
        """Stop both timers and hand the monitoring connection back.
        Called when the tab is closed: a dashboard nobody is looking at
        must not keep querying, and must not keep a connection open."""
        if self._closed:
            return
        self._closed = True
        self._stop_timers()
        connector, self._connector = self._connector, None
        if connector is None:
            return
        run_async(
            lambda: connector.close(), lambda _result: None,
            lambda _exc: None,
        )


class _SessionRow(GObject.Object):
    """One session, as a list-model row."""

    def __init__(self, index: int, session: metrics.Session) -> None:
        super().__init__()
        self.index = index
        self.session = session
        self.cells = session.cells


def _setup_cell(_factory, item: Gtk.ListItem) -> None:
    label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
    label.set_margin_start(6)
    label.set_margin_end(6)
    item.set_child(label)


def _bind_cell(_factory, item: Gtk.ListItem, index: int) -> None:
    row = item.get_item()
    text = row.cells[index] if index < len(row.cells) else ""
    label = item.get_child()
    label.set_text(text)
    label.set_tooltip_text(text)


def _cell_sorter(index: int) -> Callable[[object, object], int]:
    """Sort a column by its value, numerically where both cells are
    numbers — an ID or a duration column sorted as text puts 10 before
    2, which is the sort nobody wants."""

    def compare(left, right, _data=None) -> int:
        first = left.cells[index] if index < len(left.cells) else ""
        second = right.cells[index] if index < len(right.cells) else ""
        keys = _numbers(first, second)
        if keys is None:
            keys = (first.casefold(), second.casefold())
        if keys[0] == keys[1]:
            return 0
        return -1 if keys[0] < keys[1] else 1

    return compare


def _numbers(first: str, second: str) -> tuple[float, float] | None:
    try:
        return float(first), float(second)
    except ValueError:
        return None


class _ChartCard(Gtk.Box):
    """One metric: its name, its current value, and a sparkline of the
    rolling window under both."""

    def __init__(self, chart: metrics.Chart) -> None:
        super().__init__(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=2,
            margin_top=8,
            margin_bottom=8,
            margin_start=12,
            margin_end=12,
        )
        self.chart = chart
        self.add_css_class("card")
        title = Gtk.Label(label=chart.title, xalign=0)
        title.add_css_class("caption")
        title.add_css_class("dim-label")
        self._value = Gtk.Label(label="—", xalign=0)
        self._value.add_css_class("title-2")
        self._ceiling = Gtk.Label(xalign=0, visible=False)
        self._ceiling.add_css_class("caption")
        self._ceiling.add_css_class("dim-label")
        self._area = Gtk.DrawingArea(content_height=48, hexpand=True)
        self._area.set_draw_func(self._draw)
        self._points: list[tuple[float, float]] = []
        self.append(title)
        self.append(self._value)
        self.append(self._ceiling)
        self.append(self._area)

        style = Adw.StyleManager.get_default()
        handler = style.connect(
            "notify::dark", lambda *_: self._area.queue_draw()
        )
        self._area.connect("destroy", lambda *_: style.disconnect(handler))

    def update(
        self,
        points: list[tuple[float, float]],
        latest: float | None,
        ceiling: float | None,
    ) -> None:
        self._points = points
        self._value.set_text(metrics.format_value(self.chart, latest))
        if ceiling and self.chart.kind == "gauge":
            self._ceiling.set_text(f"of {ceiling:.0f} allowed")
            self._ceiling.set_visible(True)
        else:
            self._ceiling.set_visible(False)
        self._area.queue_draw()

    def _draw(self, _area, cr, width: int, height: int) -> None:
        """Hand the window to the shared renderer and nothing else.

        The card owns no scale and no path of its own (CORE-31): the
        only thing it still decides is that a percentage is read
        against 0–100 rather than against the narrow band the last five
        minutes happened to occupy.
        """
        data = charts.ChartData(
            series=(charts.Series("value", tuple(self._points), "value"),),
            x_kind=charts.NUMERIC,
            rows=len(self._points),
        )
        chart_canvas.render(
            cr,
            _SPARK_SPEC,
            data,
            width,
            height,
            dark=Adw.StyleManager.get_default().get_dark(),
            sparkline=True,
            y_range=(0.0, 100.0) if self.chart.kind == "percent" else None,
        )
