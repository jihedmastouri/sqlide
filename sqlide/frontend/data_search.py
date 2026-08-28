"""Find a value across a database's tables (CORE-45).

A tab, not a modal. A scan of somebody's schema takes as long as it
takes, the results arrive table by table, and every one of them is a
place to go next — none of which a dialog does well.

What the tab is responsible for is the honesty around the scan; the
decisions themselves belong to `backend/db/search.py`, which is pure
and tested without a server:

* **It says the size first.** Before anything runs, the tab states how
  many tables it will read and how many rows it will take from each.
  A search that quietly touches four hundred tables is the version of
  this feature that gets a client banned from a production server.
* **It asks on production.** A connection classed production goes
  through the same confirmation ladder as a destructive statement
  (frontend/confirm.py) — this is the one read-only feature here that
  can put real load on a server.
* **It never blocks the UI.** The catalog read, the plan and every
  statement run on a worker thread (frontend/util.run_async); hits are
  handed back through the main loop in batches, and Stop is checked
  between tables, so cancelling costs at most the statement in flight.
* **It never hides a gap.** A table the account cannot read, a view
  left out, a table with no column the term could be in — each is a
  line under "Not searched", with the reason. A missing table would
  otherwise read as "the value is not in there", which is the one
  answer this must not fake.

Opening a hit reuses CORE-43's path: the row becomes an ordinary
filter and the table tab opens on it, so nothing new is persisted and
the filter stays visible and editable.
"""

from __future__ import annotations

from typing import Callable

from gi.repository import Adw, GLib, Gtk

from sqlide.backend import identity
from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.db import search
from sqlide.backend.db.base import Connector
from sqlide.backend.db.relations import RelationTarget
from sqlide.backend.db.search import (
    Hit,
    SearchOptions,
    SearchPlan,
    SearchTable,
)
from sqlide.backend.workspaces import TabState
from sqlide.frontend import confirm
from sqlide.frontend.data_grid import ResultGrid
from sqlide.frontend.util import describe, run_async
from sqlide.i18n import _

#: How often the results grid catches up with the hits found so far.
#: Rebuilding it per hit would spend the scan redrawing; a fifth of a
#: second still reads as "streaming in".
_FLUSH_MS = 200


class DataSearchTab(Gtk.Box):
    def __init__(
        self,
        profile: ConnectionProfile,
        ensure_connector: Callable[[ConnectionProfile], Connector],
        show_error: Callable[[str], None],
        on_open_hit: Callable[[ConnectionProfile, RelationTarget], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.profile = profile
        self._ensure = ensure_connector
        self._show_error = show_error
        self._on_open_hit = on_open_hit

        self._plan: SearchPlan | None = None
        self._hits: list[Hit] = []
        self._pending: list[Hit] = []
        self._skipped: list[search.SkippedTable] = []
        self._tables: list[SearchTable] = []
        self._running = False
        self._cancelled = False
        self._flush_source = 0

        header = Adw.HeaderBar()
        self._entry = Gtk.SearchEntry(
            placeholder_text=_("Value to find in this database"),
            hexpand=True,
            width_request=280,
        )
        self._entry.connect("activate", lambda *_: self.start())
        header.set_title_widget(self._entry)
        self._button = Gtk.Button(label=_("Search"))
        self._button.add_css_class("suggested-action")
        self._button.connect("clicked", lambda *_: self._toggle())
        header.pack_end(self._button)
        header.pack_start(self._options_button())

        self._banner = Adw.Banner(revealed=False)
        self._status = Gtk.Label(xalign=0, wrap=True)
        self._status.add_css_class("dim-label")
        self._progress = Gtk.ProgressBar(visible=False, show_text=False)

        self._grid = ResultGrid(on_row_activated=self._open_hit)
        self._grid.set_vexpand(True)

        self._skipped_label = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self._skipped_label.add_css_class("dim-label")
        self._skipped_label.add_css_class("caption")
        self._skipped_row = Adw.ExpanderRow(
            title=_("Not searched"), visible=False
        )
        self._skipped_row.add_row(
            Gtk.ListBoxRow(
                child=self._skipped_label,
                activatable=False,
                selectable=False,
            )
        )
        skipped_group = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        skipped_group.add_css_class("boxed-list")
        skipped_group.append(self._skipped_row)
        skipped_group.set_margin_start(12)
        skipped_group.set_margin_end(12)
        skipped_group.set_margin_bottom(12)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        body.append(self._banner)
        for widget in (self._status, self._progress):
            widget.set_margin_start(12)
            widget.set_margin_end(12)
            body.append(widget)
        body.append(self._grid)
        body.append(skipped_group)

        view = Adw.ToolbarView(content=body)
        view.add_top_bar(header)
        self.append(view)

        self.connect("destroy", lambda *_: self.stop())
        self._declare()

    # Options

    def _options_button(self) -> Gtk.Widget:
        """Exact/contains, case, views and the per-table row cap. A
        small set on purpose: every one of them changes either what a
        match means or how much the server is asked to do."""
        self._exact = Gtk.CheckButton(label=_("Whole value"))
        self._case = Gtk.CheckButton(label=_("Case sensitive"))
        self._views = Gtk.CheckButton(label=_("Include views"))
        self._rows = Gtk.SpinButton.new_with_range(1, 10000, 10)
        self._rows.set_value(SearchOptions.max_rows)
        rows_box = Gtk.Box(spacing=6)
        rows_box.append(Gtk.Label(label=_("Rows per table"), xalign=0))
        rows_box.append(self._rows)

        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=6,
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        for widget in (self._exact, self._case, self._views, rows_box):
            box.append(widget)
        popover = Gtk.Popover(child=box)
        # A changed option changes the size of the scan, so the
        # declaration above the grid is re-derived rather than left
        # describing the previous settings.
        for toggle in (self._exact, self._case, self._views):
            toggle.connect("toggled", lambda *_: self._declare())
        self._rows.connect("value-changed", lambda *_: self._declare())
        button = Gtk.MenuButton(
            icon_name="preferences-system-symbolic", popover=popover
        )
        describe(button, _("Search options"))
        return button

    def options(self) -> SearchOptions:
        return SearchOptions(
            exact=self._exact.get_active(),
            case_sensitive=self._case.get_active(),
            include_views=self._views.get_active(),
            max_rows=int(self._rows.get_value()),
        )

    # Declaring the scan before it runs

    def _declare(self) -> None:
        """Say what a search would cost, before there is a term.

        The table count is a catalog question, so it is read on a
        worker like everything else; until it answers, the tab says so
        rather than showing a number it has not got.
        """
        production = (
            identity.normalize_environment(self.profile.environment)
            == "production"
        )
        self._banner.set_title(
            _("Production connection — a scan reads every table listed below.")
        )
        self._banner.set_revealed(production)
        self._status.set_label(_("Reading the table list…"))

        def work():
            return self._catalog()

        def done(tables) -> None:
            self._tables = tables
            searchable = [
                t for t in tables
                if not t.system
                and (t.kind == "table" or self._views.get_active())
            ]
            count = len(searchable)
            noun = _("table") if count == 1 else _("tables")
            self._status.set_label(
                _("{count} {noun} on {where}; a search reads each one and "
                  "takes at most {rows} rows from it.").format(
                    count=count,
                    noun=noun,
                    where=self.profile.name,
                    rows=int(self._rows.get_value()),
                )
            )

        run_async(work, done, self._failed)

    def _catalog(self) -> list[SearchTable]:
        """The tables of the connected scope, with their columns.

        Read through the connector's cached catalog (CORE-41), so a
        second search on the same connection costs no catalog queries
        at all.
        """
        connector = self._ensure(self.profile)
        tables = []
        for table in connector.catalog_tables():
            tables.append(
                SearchTable(
                    name=table.name,
                    columns=tuple(connector.catalog_columns(table.name)),
                    schema=self.profile.schema,
                    kind=table.kind,
                )
            )
        return tables

    # Running

    def _toggle(self) -> None:
        if self._running:
            self.stop()
        else:
            self.start()

    def start(self) -> None:
        """Plan the scan, then run it once the connection's environment
        class has been cleared."""
        if self._running:
            return
        term = self._entry.get_text().strip()
        if not term:
            return
        options = self.options()

        def work():
            connector = self._ensure(self.profile)
            return search.plan(
                self._catalog(),
                term,
                options,
                quote=connector.quote_ident,
                placeholder=connector.placeholder,
            )

        def planned(plan: SearchPlan) -> None:
            self._plan = plan
            if not plan.queries:
                self._skipped = list(plan.skipped)
                self._render_skipped()
                self._status.set_label(
                    _("No table on this connection has a column {term!r} "
                      "could be in.").format(term=term)
                )
                return
            self._ask_then_scan(plan)

        run_async(work, planned, self._failed)

    def _ask_then_scan(self, plan: SearchPlan) -> None:
        """Production asks first. The question names the size of the
        scan, which is the part a person needs in order to answer it."""
        environment = identity.normalize_environment(self.profile.environment)
        if environment != "production":
            self._scan(plan)
            return
        confirm.present(
            self,
            heading=_("Search {count} tables on production?").format(
                count=plan.table_count
            ),
            body=_(
                "This reads every one of them on {where}. Nothing is "
                "written, but the server does the work."
            ).format(where=confirm.describe_connection(self.profile)),
            statement=(
                plan.queries[0].display + "\n…"
                if len(plan.queries) > 1
                else plan.queries[0].display
            ),
            confirm_label=_("Search"),
            level="confirm",
            on_confirm=lambda: self._scan(plan),
        )

    def _scan(self, plan: SearchPlan) -> None:
        self._running = True
        self._cancelled = False
        self._hits, self._pending, self._skipped = [], [], []
        self._grid.set_result([], [])
        self._button.set_label(_("Stop"))
        self._progress.set_visible(True)
        self._progress.set_fraction(0.0)
        self._flush_source = GLib.timeout_add(_FLUSH_MS, self._flush)

        def work():
            connector = self._ensure(self.profile)

            def execute(query):
                result = connector.run_bound(query.sql, query.params)
                return result.columns, result.rows

            return search.scan(
                plan,
                execute,
                on_progress=lambda index, query: GLib.idle_add(
                    self._progressed, index, query.label
                ),
                on_hit=self._pending.append,
                should_cancel=lambda: self._cancelled,
            )

        run_async(work, self._finished, self._failed)

    def stop(self) -> None:
        """Ask the worker to stop between tables. The flag is the whole
        mechanism: nothing is killed mid-statement, so no connection is
        left in a state the next query has to recover from."""
        self._cancelled = True

    def _progressed(self, index: int, label: str) -> bool:
        total = self._plan.table_count if self._plan else 0
        if total:
            self._progress.set_fraction(index / total)
        self._status.set_label(
            _("Scanning {position} of {total}: {table} — {hits} so far")
            .format(
                position=index + 1,
                total=total,
                table=label,
                hits=len(self._hits) + len(self._pending),
            )
        )
        return GLib.SOURCE_REMOVE

    def _flush(self) -> bool:
        """Move whatever the worker has found into the grid. Called on
        a timer rather than per hit: a thousand rebuilds of a column
        view would cost more than the scan."""
        if self._pending:
            self._hits.extend(self._pending)
            self._pending = []
            self._render_hits()
        return GLib.SOURCE_CONTINUE if self._running else GLib.SOURCE_REMOVE

    def _finished(self, report: search.SearchReport) -> None:
        self._running = False
        if self._flush_source:
            GLib.source_remove(self._flush_source)
            self._flush_source = 0
        self._hits = list(report.hits)
        self._pending = []
        self._skipped = list(report.skipped)
        self._render_hits()
        self._render_skipped()
        self._button.set_label(_("Search"))
        self._progress.set_visible(False)
        count = len(report.hits)
        parts = [
            _("{count} hits in {scanned} tables").format(
                count=count, scanned=report.scanned
            )
        ]
        if report.cancelled:
            parts.append(_("stopped"))
        if report.truncated:
            parts.append(_("stopped at the hit cap"))
        if report.skipped:
            parts.append(
                _("{count} not searched").format(count=len(report.skipped))
            )
        self._status.set_label(" · ".join(parts))

    def _failed(self, exc: Exception) -> None:
        self._running = False
        if self._flush_source:
            GLib.source_remove(self._flush_source)
            self._flush_source = 0
        self._button.set_label(_("Search"))
        self._progress.set_visible(False)
        self._show_error(str(exc))

    # Results

    def _render_hits(self) -> None:
        self._grid.set_result(
            [_("Table"), _("Column"), _("Value")],
            [
                (hit.label, hit.column, _display(hit.value))
                for hit in self._hits
            ],
        )

    def _render_skipped(self) -> None:
        self._skipped_row.set_visible(bool(self._skipped))
        self._skipped_row.set_subtitle(
            _("{count} tables").format(count=len(self._skipped))
        )
        self._skipped_label.set_label(
            "\n".join(f"{s.label} — {s.reason}" for s in self._skipped)
        )

    def _open_hit(self, index: int) -> None:
        """Activating a hit opens its table filtered to that row — the
        same "open filtered" path foreign-key navigation uses."""
        if not 0 <= index < len(self._hits):
            return
        hit = self._hits[index]
        filters = search.hit_filters(hit)
        if not filters:
            self._show_error(
                _("That hit has no value to filter the table by.")
            )
            return
        # The catalog was read through this tab's own profile, so the
        # hit is in the scope that profile already points at: the table
        # opens on it, unqualified, exactly as the sidebar would open it.
        self._on_open_hit(
            self.profile,
            RelationTarget(table=hit.table, schema="", filters=filters),
        )

    def tab_state(self) -> TabState:
        return TabState(kind="datasearch", connection=self.profile.name)


def _display(value) -> str:
    """A matched value as one line of the results list. Long text is
    cut here rather than in the grid: the point of the column is that
    the term is in there, and the whole value is one click away in the
    table it came from."""
    if value is None:
        return "NULL"
    text = str(value).replace("\n", " ")
    return text if len(text) <= 200 else text[:200] + "…"
