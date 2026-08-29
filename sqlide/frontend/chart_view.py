"""The Chart side of a result: the loaded rows, drawn (CORE-32).

A result was only ever a grid. This is the other view over the same
rows, in the same stack the table tab already holds Data and Record in
(`data_grid.TableTab`) and the third widget built to PG-04's contract,
which `frontend/map_view.py` established and this follows closely
enough that a reader of one recognises the other:

- **one view over the rows a `ResultGrid` already loaded**, living in
  the same stack, so switching away and back costs nothing and loses
  nothing — the grid's scroll position, its filters and its unsaved
  edits are still there;
- **two-way selection** — clicking a mark reports its row through
  `on_select`, and `select_row` highlights the mark of a row selected
  in the grid;
- **a notice bar rather than a blank canvas**: "Showing N of M rows",
  "No numeric column to plot", "This result has no rows". A chart of a
  partial result that does not say so is a lie, which is the one
  failure mode RS-03 asks us to engineer against.

Everything this widget knows about charts it is told:
`backend/charts.py` (CORE-30) classifies the columns, infers the
mapping and turns rows into series; `frontend/chart_canvas.py`
(CORE-31) draws them and hit-tests them. The widget owns a `ChartSpec`
and nothing else — no geometry, no type sniffing, and no branch on
which engine the rows came from. That is what makes the mapping
persistable (CORE-33) and the picture exportable (CORE-34) without
touching this file.

`ChartPane` is the same thing packaged for the query console and the
query builder, whose results are a bare `ResultGrid`: it puts the grid
and the chart behind one linked Data | Chart toggle and wires the
selection both ways. The table tab has a switcher of its own already,
so it holds a `ChartView` directly.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from gi.repository import Adw, Gtk

from sqlide.backend import charts
from sqlide.backend.settings import max_chart_rows
from sqlide.frontend import chart_canvas, chart_export_dialog
from sqlide.frontend.util import describe
from sqlide.i18n import _

#: The X entry that means "no column": the row number is the axis.
_ROW_NUMBER = "—"


def _type_labels() -> dict[str, str]:
    """The model's vocabulary in the user's. Built per call rather than
    at import: the translation catalogue is bound after this module
    is."""
    return {
        "line": _("Line"),
        "bar": _("Bar"),
        "area": _("Area"),
        "scatter": _("Scatter"),
        "pie": _("Pie"),
    }


def _aggregation_labels() -> dict[str, str]:
    return {
        "none": _("No aggregation"),
        "sum": _("Sum"),
        "count": _("Count"),
        "avg": _("Average"),
        "min": _("Minimum"),
        "max": _("Maximum"),
    }


class _Rows:
    """What `backend/charts.py` reads a result as: columns and rows.

    A duck for a `ResultSet`, so the mapping functions serve a table
    tab's paged window and a console result without either of them
    having to build a `ResultSet` they do not otherwise have.
    """

    def __init__(self, columns: Sequence[str], rows: Sequence[Sequence[Any]]):
        self.columns = list(columns)
        self.rows = list(rows)


class ChartView(Gtk.Box):
    """The loaded rows as a chart, over a mapping the user can change.

    `on_select(row)` is the click-a-mark half of the two-way selection
    (row indexes are into the rows this view was given, exactly as the
    map's are). `on_load_all(cap)` is offered as **Load all for chart**
    only when the caller can actually fetch more; it is expected to
    call `set_result` again, or `report()` with the reason it would
    not.
    """

    def __init__(
        self,
        on_select: Callable[[int], None] | None = None,
        on_load_all: Callable[[int], None] | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._on_select = on_select
        self._on_load_all = on_load_all

        self._columns: list[str] = []
        self._rows: list[Sequence[Any]] = []
        self._classes: dict[str, str] = {}
        self._spec: charts.ChartSpec | None = None
        self._data = charts.ChartData()
        self._keys: list[tuple[Any, str] | None] = []
        self._rendering: chart_canvas.Rendering | None = None
        self._marks: set[tuple[int, int]] = set()
        self._reason = _("This result has no rows to chart.")
        self._total: int | None = None
        self._more = False
        self._message = ""
        #: The file name the export dialog suggests; an owner that
        #: knows what it is showing (a table tab) sets it.
        self.export_name = "chart"
        self._series_checks: list[tuple[str, Gtk.CheckButton]] = []
        # Set while the controls are being filled in from the spec, so
        # that rebuilding them does not read back as the user editing
        # the mapping.
        self._loading = False

        self._bar = self._mapping_bar()
        self.append(self._bar)

        self._notice = Gtk.Label(xalign=0, wrap=True)
        self._notice.add_css_class("dim-label")
        self._notice.set_margin_start(12)
        self._notice.set_margin_end(12)
        self._notice.set_margin_bottom(4)
        self.append(self._notice)

        self._canvas = Gtk.DrawingArea(vexpand=True, hexpand=True)
        self._canvas.set_draw_func(self._draw)
        self._canvas.set_has_tooltip(True)
        self._canvas.connect("query-tooltip", self._on_tooltip)
        click = Gtk.GestureClick()
        click.connect("released", self._on_click)
        self._canvas.add_controller(click)
        self.append(self._canvas)

        # The style can flip while a tab is open, and the palette is
        # picked at draw time — so a theme change is a redraw.
        Adw.StyleManager.get_default().connect(
            "notify::dark", lambda *_a: self._canvas.queue_draw()
        )

    # Mapping bar

    def _mapping_bar(self) -> Gtk.Widget:
        """Chart type, X, series, split and aggregation, side by side.

        It shows what `infer()` chose rather than presenting a blank
        form — the point of inferring at all is that the first chart
        costs no configuration.
        """
        bar = Gtk.Box(
            spacing=6,
            margin_start=12,
            margin_end=12,
            margin_top=6,
            margin_bottom=6,
        )
        self._type = Gtk.DropDown(
            model=Gtk.StringList.new(list(_type_labels().values()))
        )
        describe(self._type, _("Chart type"))
        self._type.connect("notify::selected", self._on_mapping_changed)
        bar.append(self._type)

        bar.append(self._caption(_("X")))
        self._x = Gtk.DropDown(model=Gtk.StringList.new([]))
        describe(self._x, _("The column on the X axis"))
        self._x.connect("notify::selected", self._on_mapping_changed)
        bar.append(self._x)

        bar.append(self._caption(_("Values")))
        self._series = Gtk.MenuButton(label=_("Values"))
        describe(self._series, _("The columns drawn as values"))
        self._series_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=2,
            margin_start=8,
            margin_end=8,
            margin_top=8,
            margin_bottom=8,
        )
        self._series.set_popover(
            Gtk.Popover(
                child=Gtk.ScrolledWindow(
                    child=self._series_box,
                    propagate_natural_height=True,
                    propagate_natural_width=True,
                    max_content_height=320,
                )
            )
        )
        bar.append(self._series)

        bar.append(self._caption(_("Split")))
        self._split = Gtk.DropDown(model=Gtk.StringList.new([]))
        describe(self._split, _("A column whose values split the series"))
        self._split.connect("notify::selected", self._on_mapping_changed)
        bar.append(self._split)

        self._aggregation = Gtk.DropDown(
            model=Gtk.StringList.new(list(_aggregation_labels().values()))
        )
        describe(
            self._aggregation,
            _("How rows sharing an X value are combined"),
        )
        self._aggregation.connect("notify::selected", self._on_mapping_changed)
        bar.append(self._aggregation)

        self._stacked = Gtk.CheckButton(label=_("Stacked"))
        describe(
            self._stacked,
            _("Stack the values of a bar or area chart, or hollow a pie"),
        )
        self._stacked.connect("toggled", self._on_mapping_changed)
        bar.append(self._stacked)

        self._load_all = Gtk.Button(label=_("Load all for chart"))
        self._load_all.set_halign(Gtk.Align.END)
        self._load_all.set_hexpand(True)
        self._load_all.set_visible(False)
        self._load_all.connect("clicked", self._on_load_all_clicked)
        bar.append(self._load_all)

        # The picture's way out (CORE-34). Both draw the spec and the
        # series this view already holds, through the same renderer the
        # canvas below uses.
        self._copy_image = Gtk.Button(icon_name="edit-copy-symbolic")
        describe(self._copy_image, _("Copy the chart as an image"))
        self._copy_image.connect("clicked", lambda *_a: self.copy_image())
        bar.append(self._copy_image)

        self._export_image = Gtk.Button(icon_name="document-save-symbolic")
        describe(self._export_image, _("Export the chart as PNG or SVG"))
        self._export_image.connect("clicked", lambda *_a: self.open_export())
        bar.append(self._export_image)
        return bar

    @staticmethod
    def _caption(text: str) -> Gtk.Label:
        label = Gtk.Label(label=text)
        label.add_css_class("dim-label")
        return label

    # Data

    def set_result(
        self,
        columns: Sequence[str],
        rows: Sequence[Sequence[Any]],
        *,
        total: int | None = None,
        more: bool = False,
        provider: Any = None,
        table: str = "",
    ) -> None:
        """Draw these rows.

        The mapping survives a reload — a refresh, a scrolled-in page,
        a re-run of the same query — because the spec names columns
        rather than row indexes. It is re-inferred only when the
        result's shape changed enough that the old spec no longer fits,
        which is the same rule CORE-33 restores a saved spec by.
        """
        self._columns = [str(c) for c in columns]
        self._rows = list(rows)
        self._total = total
        self._more = more
        self._message = ""
        self._classes = charts.classify(
            self._columns, self._rows, provider=provider, table=table
        )
        if self._spec is None or charts.validate(self._spec, self._columns):
            inference = charts.infer(_Rows(self._columns, self._rows), self._classes)
            self._spec = inference.spec
            self._reason = inference.reason
        self._fill_controls()
        self._refresh()

    def clear(self) -> None:
        self._spec = None
        self.set_result([], [])

    def report(self, message: str) -> None:
        """Say something in the notice bar that is not about the rows —
        what **Load all for chart** answers with when it refuses."""
        self._message = message
        self._refresh_notice()

    @property
    def spec(self) -> charts.ChartSpec | None:
        """The mapping on screen. CORE-33 persists exactly this."""
        return self._spec

    def set_spec(self, spec: charts.ChartSpec | None) -> None:
        """Draw a mapping from somewhere else — a restored tab, a saved
        query. One that does not fit these columns is reported in the
        notice bar and replaced by the inferred one, never raised."""
        problems = charts.validate(spec, self._columns) if spec else []
        if spec is None or problems:
            self._message = " ".join(problems)
            self._spec = None
            inference = charts.infer(_Rows(self._columns, self._rows), self._classes)
            self._spec = inference.spec
            self._reason = inference.reason
        else:
            self._spec = spec
        self._fill_controls()
        self._refresh()

    # Selection

    def select_row(self, row: int | None) -> None:
        """Highlight the mark a grid row was drawn into (the grid's
        half of the two-way selection). A row the chart dropped
        highlights nothing rather than the wrong mark."""
        self._marks = set()
        key = self._keys[row] if row is not None and 0 <= row < len(self._keys) else None
        if key is not None:
            x_value, split = key
            for index, series in enumerate(self._data.series):
                if series.split != split:
                    continue
                for position, (x, _y) in enumerate(series.points):
                    if x == x_value:
                        self._marks.add((index, position))
        self._canvas.queue_draw()

    def rows_at(self, x: float, y: float) -> list[int]:
        """The result rows behind the mark under a canvas point."""
        if self._rendering is None:
            return []
        hit = self._rendering.at(x, y)
        if hit is None or hit.series >= len(self._data.series):
            return []
        split = self._data.series[hit.series].split
        return [
            index
            for index, key in enumerate(self._keys)
            if key is not None and key[1] == split and key[0] == hit.x
        ]

    def _on_click(self, _gesture, n_press: int, x: float, y: float) -> None:
        if n_press != 1 or self._rendering is None:
            return
        hit = self._rendering.at(x, y)
        if hit is None:
            return
        self._marks = {(hit.series, hit.index)}
        self._canvas.queue_draw()
        rows = self.rows_at(x, y)
        if rows and self._on_select is not None:
            self._on_select(rows[0])

    def _on_tooltip(self, _area, x, y, _keyboard, tooltip) -> bool:
        if self._rendering is None:
            return False
        hit = self._rendering.at(x, y)
        if hit is None:
            return False
        tooltip.set_text(
            _("%(series)s · %(x)s: %(y)s")
            % {
                "series": hit.name or "",
                "x": hit.x,
                "y": chart_canvas.format_tick(hit.y),
            }
        )
        return True

    # Mapping controls

    def _fill_controls(self) -> None:
        """Put the spec on screen. Nothing here edits the spec: the
        controls are a view of it, which is why the guard flag is
        enough to keep the round trip from looping."""
        self._loading = True
        try:
            spec = self._spec or charts.ChartSpec()
            types = list(_type_labels())
            self._type.set_selected(
                types.index(spec.type) if spec.type in types else 0
            )
            x_choices = [_ROW_NUMBER] + self._columns
            self._x.set_model(Gtk.StringList.new(x_choices))
            self._x.set_selected(
                x_choices.index(spec.x) if spec.x in x_choices else 0
            )
            split_choices = [_ROW_NUMBER] + self._columns
            self._split.set_model(Gtk.StringList.new(split_choices))
            self._split.set_selected(
                split_choices.index(spec.split)
                if spec.split in split_choices
                else 0
            )
            aggregations = list(_aggregation_labels())
            self._aggregation.set_selected(
                aggregations.index(spec.aggregation)
                if spec.aggregation in aggregations
                else 0
            )
            self._stacked.set_active(spec.stacked)
            self._fill_series(spec)
            for widget in (
                self._type,
                self._x,
                self._series,
                self._split,
                self._aggregation,
                self._stacked,
                self._copy_image,
                self._export_image,
            ):
                widget.set_sensitive(bool(self._columns))
        finally:
            self._loading = False

    def _fill_series(self, spec: charts.ChartSpec) -> None:
        child = self._series_box.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self._series_box.remove(child)
            child = following
        self._series_checks: list[tuple[str, Gtk.CheckButton]] = []
        for name in self._columns:
            check = Gtk.CheckButton(label=name, active=name in spec.series)
            check.connect("toggled", self._on_mapping_changed)
            self._series_box.append(check)
            self._series_checks.append((name, check))
        chosen = [name for name in spec.series if name in self._columns]
        self._series.set_label(
            ", ".join(chosen) if chosen else _("Values")
        )

    def _on_mapping_changed(self, *_args) -> None:
        """Every control edits the spec and redraws; a mapping that
        cannot be drawn explains why rather than blanking, because
        `series_from` reports instead of raising."""
        if self._loading:
            return
        types = list(_type_labels())
        aggregations = list(_aggregation_labels())
        x = self._selected(self._x, [""] + self._columns)
        split = self._selected(self._split, [""] + self._columns)
        series = tuple(
            name for name, check in self._series_checks if check.get_active()
        )
        self._spec = charts.ChartSpec(
            type=types[min(self._type.get_selected(), len(types) - 1)],
            x=x,
            series=series,
            split=split if split != x else "",
            aggregation=aggregations[
                min(self._aggregation.get_selected(), len(aggregations) - 1)
            ],
            stacked=self._stacked.get_active(),
            title=self._spec.title if self._spec else "",
        )
        self._message = ""
        self._series.set_label(", ".join(series) if series else _("Values"))
        self._refresh()

    @staticmethod
    def _selected(dropdown: Gtk.DropDown, values: Sequence[str]) -> str:
        index = dropdown.get_selected()
        return values[index] if 0 <= index < len(values) else ""

    # Export (CORE-34)

    def chart_source(self) -> tuple[charts.ChartSpec, charts.ChartData]:
        """The spec and the series as they are on screen.

        The export renders this, not the rows: what a file or the
        clipboard gets is by construction the picture the canvas is
        drawing, down to the notice a mapping that could not be drawn
        left in `ChartData.reason`.
        """
        spec = self._spec or charts.ChartSpec()
        data = self._data
        if self._spec is None and not data.reason:
            data = charts.ChartData(reason=self._reason)
        return spec, data

    def canvas_size(self) -> tuple[int, int]:
        """The on-screen size, which the export dialog starts from — a
        canvas that was never allocated falls back to the dialog's own
        default rather than to zero."""
        width = self._canvas.get_width()
        height = self._canvas.get_height()
        return (
            width if width > 1 else chart_export_dialog.DEFAULT_WIDTH,
            height if height > 1 else chart_export_dialog.DEFAULT_HEIGHT,
        )

    def copy_image(self) -> bool:
        """Put the chart on the clipboard, at the on-screen size and on
        the light palette — the same default the dialog opens on."""
        spec, data = self.chart_source()
        width, height = self.canvas_size()
        copied = chart_export_dialog.copy_chart(self, spec, data, width, height)
        self.report(
            _("Chart copied to the clipboard.")
            if copied
            else _("The chart could not be copied.")
        )
        return copied

    def open_export(self) -> None:
        """Export… — PNG or SVG, at a size the user picks."""
        width, height = self.canvas_size()
        dialog = chart_export_dialog.ChartExportDialog(
            self.chart_source,
            width,
            height,
            dark=Adw.StyleManager.get_default().get_dark(),
            suggested_name=self.export_name,
        )
        dialog.present(self)

    # Drawing

    def _refresh(self) -> None:
        result = _Rows(self._columns, self._rows)
        if self._spec is None:
            self._data = charts.ChartData(
                reason=self._reason or _("No numeric column to plot.")
            )
            self._keys = []
        else:
            self._data = charts.series_from(self._spec, result, self._classes)
            self._keys = charts.row_keys(self._spec, result, self._classes)
        self._marks = set()
        self._refresh_notice()
        self._canvas.queue_draw()

    def _refresh_notice(self) -> None:
        """"Showing N of M rows", in the language PG-04 established,
        plus whatever the mapping had to drop to draw at all."""
        parts: list[str] = []
        if not self._columns:
            parts.append(_("This result has no columns to chart."))
        elif not self._rows:
            parts.append(_("This result has no rows."))
        elif self._total is not None and self._total > len(self._rows):
            parts.append(
                _("Showing %(shown)d of %(total)d rows")
                % {"shown": len(self._rows), "total": self._total}
            )
        elif self._more:
            parts.append(
                _("Showing the first %d rows of a larger result")
                % len(self._rows)
            )
        else:
            parts.append(_("Showing %d rows") % len(self._rows))
        if self._data.dropped:
            parts.append(
                _("%d rows had no value to plot") % self._data.dropped
            )
        if self._data.capped:
            parts.append(
                _("%d points beyond the cap") % self._data.capped
            )
        if self._data.reason:
            parts.append(self._data.reason)
        if self._message:
            parts.append(self._message)
        self._notice.set_text(" · ".join(p for p in parts if p))
        self._load_all.set_visible(
            self._on_load_all is not None and (self._more or bool(self._message))
        )

    def _on_load_all_clicked(self, *_args) -> None:
        if self._on_load_all is not None:
            self._on_load_all(max_chart_rows())

    def _draw(self, _area, cr, width, height) -> None:
        dark = Adw.StyleManager.get_default().get_dark()
        spec = self._spec or charts.ChartSpec()
        data = self._data
        if self._spec is None and not data.reason:
            data = charts.ChartData(reason=self._reason)
        self._rendering = chart_canvas.render(
            cr, spec, data, width, height, dark=dark
        )
        chart_canvas.highlight(cr, self._rendering, self._marks, dark=dark)


class ChartPane(Gtk.Box):
    """A `ResultGrid` and its chart behind one linked Data | Chart
    toggle — what the query console and the query builder wrap their
    results in.

    The stack keeps both children alive, so the trip to the chart and
    back leaves the grid exactly as it was, and the selection is wired
    both ways here rather than in either caller.
    """

    def __init__(
        self,
        grid,
        *,
        on_load_all: Callable[[int], None] | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.grid = grid
        self.chart = ChartView(
            on_select=self._on_chart_row_selected, on_load_all=on_load_all
        )
        self.chart.export_name = (
            getattr(grid, "table_name", "") or "chart"
        ).replace(".", "_")
        grid.on_row_selected = self._on_grid_row_selected

        self._stack = Gtk.Stack(vexpand=True)
        self._stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._stack.add_named(grid, "data")
        self._stack.add_named(self.chart, "chart")

        row = Gtk.CenterBox(margin_top=4, margin_bottom=4)
        linked = Gtk.Box(spacing=0)
        linked.add_css_class("linked")
        self._data_toggle = Gtk.ToggleButton(label=_("Data"), active=True)
        describe(self._data_toggle, _("The result's rows"))
        self._chart_toggle = Gtk.ToggleButton(label=_("Chart"))
        describe(self._chart_toggle, _("The result's rows, drawn"))
        self._chart_toggle.set_group(self._data_toggle)
        self._data_toggle.connect("toggled", self._on_view_toggled)
        self._chart_toggle.connect("toggled", self._on_view_toggled)
        linked.append(self._data_toggle)
        linked.append(self._chart_toggle)
        row.set_center_widget(linked)
        self.append(row)
        self.append(self._stack)

    def set_result(
        self,
        columns: Sequence[str],
        rows: Sequence[Sequence[Any]],
        *,
        total: int | None = None,
        more: bool = False,
    ) -> None:
        self.chart.set_result(columns, rows, total=total, more=more)

    def show_chart(self) -> None:
        self._chart_toggle.set_active(True)

    def _on_view_toggled(self, button: Gtk.ToggleButton) -> None:
        # Grouped toggles fire on the button that lost the state too;
        # only the one that gained it is a switch.
        if not button.get_active():
            return
        self._stack.set_visible_child_name(
            "chart" if button is self._chart_toggle else "data"
        )

    def _on_chart_row_selected(self, row: int) -> None:
        self.grid.select_row(row)

    def _on_grid_row_selected(self, row: int | None) -> None:
        self.chart.select_row(row)
