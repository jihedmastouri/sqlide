"""One cairo renderer for every chart the app draws.

RS-03 chose cairo over a charting library (see
`docs/charting-research.md`): matplotlib would add ~100 MB and numpy to
an app whose only runtime dependency is PyGObject, and would not follow
the libadwaita theme. That decision means we own the axes, and this is
where they live — once, so that the monitoring sparkline and the result
chart cannot drift apart.

The contract, and the reason CORE-34 gets export nearly for free:

- **Drawing is a pure function of (spec, data, context, size).** The
  same `render()` call fills a `Gtk.DrawingArea` and a
  `cairo.ImageSurface` or `SVGSurface`; nothing here touches a widget,
  a result set or a connection.
- **The model is rendered, never re-derived.** Series come from
  `backend/charts.series_from`; this module maps them to pixels and
  stops. It never reaches into rows.
- **The palette is picked at draw time**, from `frontend/canvas.py`,
  because the app's style can flip while a tab is open.
- **The geometry is pure Python.** `nice_ticks`, `time_ticks`,
  `format_tick`, `decimate` and `Scale` are plain functions over
  numbers, so the parts that can be wrong are unit-tested even though a
  cairo surface is not.

`render()` returns a `Rendering`, whose `at(x, y)` names the series and
data index under a point. That is what lets the view layer (CORE-32)
drive tooltips and selection without knowing anything about the
geometry.

`sparkline=True` is the mode `monitor_tab._ChartCard` draws in: no
axes, no ticks, no legend, one series, and an optional `y_range` for
the percentage metrics that are read against 0–100 rather than against
the narrow band the last five minutes happened to occupy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, time as _time, timedelta
from typing import Any, Iterable, Sequence

from gi.repository import Pango, PangoCairo

from sqlide.backend import charts
from sqlide.frontend.canvas import FONT, draw_text, palette, rgb, text_size
from sqlide.i18n import _

__all__ = [
    "Hit",
    "Rendering",
    "Scale",
    "decimate",
    "format_tick",
    "highlight",
    "nice_ticks",
    "plot_rect",
    "render",
    "series_colors",
    "time_ticks",
]


#: A categorical series palette that holds up on both backgrounds. The
#: first entry of each is the colour the monitoring sparkline has drawn
#: in since CORE-15 — the port must not change how the dashboard looks.
SERIES_LIGHT = (
    "#1c71d8",
    "#e66100",
    "#2ec27e",
    "#a51d2d",
    "#9141ac",
    "#986a44",
    "#c64600",
    "#613583",
)
SERIES_DARK = (
    "#99c1f1",
    "#ffbe6f",
    "#8ff0a4",
    "#ff7b63",
    "#dc8add",
    "#cdab8f",
    "#f9f06b",
    "#62a0ea",
)

#: Breathing room around the plot, in pixels.
PAD = 8
#: How far a tick mark sticks out of the axis.
TICK = 4
#: How close a click has to be to a point to count as hitting it.
HIT_RADIUS = 12
#: Roughly how many ticks an axis wants. The nice-number algorithm
#: treats it as a hint, not a promise: a round step beats a fixed count.
X_TICKS = 6
Y_TICKS = 5
#: Past this many points per pixel column a line is decimated (min/max
#: per column). Scatter is never decimated: its point count is the
#: message.
DECIMATE_PER_PIXEL = 2

_SPARK_TOP = 4
_SPARK_BOTTOM = 2


# Scales and geometry — pure, and unit-tested as such.


@dataclass(frozen=True)
class Scale:
    """A linear mapping of a value range onto a pixel range.

    Pixels grow rightwards for X and downwards for Y, so a Y scale is
    simply one built with `px0` below `px1` — there is no separate
    "inverted" flag to get wrong.
    """

    low: float
    high: float
    px0: float
    px1: float

    @property
    def span(self) -> float:
        return self.high - self.low

    def to_px(self, value: float) -> float:
        span = self.span
        if not span:
            return (self.px0 + self.px1) / 2
        return self.px0 + (float(value) - self.low) * (self.px1 - self.px0) / span

    def to_value(self, px: float) -> float:
        width = self.px1 - self.px0
        if not width:
            return self.low
        return self.low + (px - self.px0) * self.span / width


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    width: float
    height: float

    def contains(self, px: float, py: float) -> bool:
        return (
            self.x <= px <= self.x + self.width
            and self.y <= py <= self.y + self.height
        )


def plot_rect(
    width: float,
    height: float,
    left: float = 0.0,
    top: float = 0.0,
    right: float = 0.0,
    bottom: float = 0.0,
) -> Rect:
    """The drawing area minus its margins, never smaller than 1x1.

    A card narrower than its own axis labels is a layout bug, not a
    crash: the rect degenerates instead of going negative and sending
    cairo a backwards path.
    """
    x = left
    y = top
    w = max(width - left - right, 1.0)
    h = max(height - top - bottom, 1.0)
    return Rect(x, y, w, h)


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def nice_number(value: float, round_it: bool) -> float:
    """The 1/2/5x10^n "nice" number near `value`."""
    if value <= 0:
        return 1.0
    exponent = math.floor(math.log10(value))
    fraction = value / (10**exponent)
    if round_it:
        nice = 1.0 if fraction < 1.5 else 2.0 if fraction < 3 else 5.0 if fraction < 7 else 10.0
    else:
        nice = 1.0 if fraction <= 1 else 2.0 if fraction <= 2 else 5.0 if fraction <= 5 else 10.0
    return nice * (10**exponent)


def nice_ticks(
    low: float, high: float, count: int = Y_TICKS
) -> tuple[float, float, float, tuple[float, ...]]:
    """`(low, high, step, ticks)` covering [low, high] on round numbers.

    Deliberately unexcitable, because every awkward range reaches it
    from a real result: a zero span (one row, or a constant column) is
    padded rather than divided by; NaN and infinity — which a driver
    can hand back from a float column — fall back to 0–1; a reversed
    range is swapped. The tick list is de-duplicated after rounding, so
    a tiny step never produces two identically-labelled gridlines.
    """
    first = _finite(low)
    second = _finite(high)
    if first is None or second is None:
        first, second = 0.0, 1.0
    if second < first:
        first, second = second, first
    if second == first:
        pad = abs(first) * 0.5 if first else 0.5
        first, second = first - pad, second + pad
    steps = max(int(count), 2) - 1
    step = nice_number((second - first) / steps, True)
    if not step:
        step = 1.0
    graph_low = math.floor(first / step) * step
    graph_high = math.ceil(second / step) * step
    decimals = max(0, int(-math.floor(math.log10(step))) + 1)

    ticks: list[float] = []
    value = graph_low
    # A guard, not a limit: a step that fp-rounds to something tiny
    # would otherwise loop forever building gridlines nobody can see.
    for _index in range(1000):
        ticks.append(round(value, decimals))
        if value >= graph_high - step / 2:
            break
        value += step
    seen: set[float] = set()
    unique = tuple(t for t in ticks if not (t in seen or seen.add(t)))
    return graph_low, graph_high, step, unique


def format_tick(value: float, step: float = 1.0) -> str:
    """A tick label: as many decimals as the step needs, no more.

    Large magnitudes get a k/M/G/T suffix, because "1200000" on an axis
    is three characters of information and seven of noise.
    """
    number = _finite(value)
    if number is None:
        return ""
    if number == 0:
        return "0"
    size = _finite(step) or 1.0
    magnitude = abs(number)
    for limit, suffix in ((1e12, "T"), (1e9, "G"), (1e6, "M"), (1e3, "k")):
        if magnitude >= limit and abs(size) >= limit / 1000:
            scaled = number / limit
            text = f"{scaled:.1f}".rstrip("0").rstrip(".")
            return f"{text}{suffix}"
    if abs(size) < 1e-4:
        # Past four decimal places a fixed-point label is a row of
        # zeroes and every tick reads the same; an exponent is ugly but
        # it is the only form that still distinguishes them.
        return f"{number:.3g}"
    decimals = 0
    if abs(size) < 1:
        decimals = min(6, max(0, int(-math.floor(math.log10(abs(size))))))
    text = f"{number:.{decimals}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


#: Temporal tick steps, in seconds, from a second to a year. The label
#: format is chosen from the *span*, not the step, so neighbouring
#: ticks read as a series rather than as unrelated timestamps.
_TIME_STEPS = (
    1,
    2,
    5,
    10,
    15,
    30,
    60,
    120,
    300,
    600,
    900,
    1800,
    3600,
    7200,
    10800,
    21600,
    43200,
    86400,
    172800,
    604800,
    1209600,
    2592000,
    7776000,
    15552000,
    31536000,
)


def _epoch(value: Any) -> float | None:
    """A date/time as seconds, or None when it is not one.

    Naive values are read as naive — the app never invents a timezone
    for a column that has none — and a plain `date` is midnight.
    """
    if isinstance(value, datetime):
        try:
            if value.tzinfo:
                return value.timestamp()
            return (value - datetime(1970, 1, 1)).total_seconds()
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, date):
        return (
            datetime(value.year, value.month, value.day) - datetime(1970, 1, 1)
        ).total_seconds()
    if isinstance(value, _time):
        return value.hour * 3600 + value.minute * 60 + value.second
    return _finite(value)


def _time_format(span: float) -> str:
    if span <= 2:
        return "%H:%M:%S"
    if span < 2 * 86400:
        return "%H:%M"
    if span < 400 * 86400:
        return "%b %d"
    return "%Y"


def time_ticks(
    low: float, high: float, count: int = X_TICKS
) -> tuple[tuple[float, str], ...]:
    """Tick positions and labels for a temporal axis, in epoch seconds.

    The step comes from a fixed ladder of human time units — a chart
    that ticks every 86 400 s reads as a chart that ticks every day,
    while one that ticks every 100 000 s reads as noise.
    """
    first = _finite(low)
    second = _finite(high)
    if first is None or second is None:
        return ()
    if second < first:
        first, second = second, first
    if second == first:
        second = first + 1.0
    span = second - first
    want = max(int(count), 2)
    target = span / want
    step = _TIME_STEPS[-1]
    for candidate in _TIME_STEPS:
        if candidate >= target:
            step = candidate
            break
    fmt = _time_format(span)
    start = math.ceil(first / step) * step
    ticks: list[tuple[float, str]] = []
    value = start
    seen: set[str] = set()
    for _index in range(1000):
        if value > second:
            break
        try:
            stamp = datetime(1970, 1, 1) + timedelta(seconds=value)
            label = stamp.strftime(fmt)
        except (OverflowError, OSError, ValueError):
            label = format_tick(value, step)
        # Two ticks that render the same text are one tick with a
        # duplicate, which is worse than a sparser axis.
        if label not in seen:
            seen.add(label)
            ticks.append((float(value), label))
        value += step
    return tuple(ticks)


def decimate(
    points: Sequence[tuple[float, float]], pixels: float
) -> list[tuple[float, float]]:
    """Thin `points` to min/max per pixel column, preserving shape.

    Past a couple of points per pixel the extra ones cannot be seen but
    are still stroked, which is what makes a 50 000-point line stutter
    on resize. Keeping both extremes of each column keeps every spike
    the full line would have shown.
    """
    width = max(int(pixels), 1)
    limit = width * DECIMATE_PER_PIXEL
    if len(points) <= max(limit, 4):
        return list(points)
    low = min(p[0] for p in points)
    high = max(p[0] for p in points)
    span = high - low
    if not span:
        return list(points)
    columns: dict[int, list[tuple[float, float]]] = {}
    for x, y in points:
        column = int((x - low) * (width - 1) / span)
        bucket = columns.get(column)
        if bucket is None:
            columns[column] = [(x, y), (x, y)]
        else:
            if y < bucket[0][1]:
                bucket[0] = (x, y)
            if y > bucket[1][1]:
                bucket[1] = (x, y)
    thinned: list[tuple[float, float]] = []
    for column in sorted(columns):
        low_point, high_point = columns[column]
        pair = sorted((low_point, high_point), key=lambda p: p[0])
        thinned.append(pair[0])
        if pair[1] != pair[0]:
            thinned.append(pair[1])
    return thinned


def series_colors(dark: bool) -> tuple[str, ...]:
    return SERIES_DARK if dark else SERIES_LIGHT


# Hit testing


@dataclass(frozen=True)
class Hit:
    """What sits under a point: which series, and which of its points."""

    series: int
    index: int
    name: str = ""
    x: Any = None
    y: float = 0.0


@dataclass
class Rendering:
    """What a `render()` call drew, and where it drew it.

    The view layer keeps one of these from the last draw and asks it
    `at(x, y)`; it never recomputes a scale of its own.
    """

    plot: Rect = field(default_factory=lambda: Rect(0, 0, 1, 1))
    x_scale: Scale | None = None
    y_scale: Scale | None = None
    reason: str = ""
    _points: list[tuple[float, float, int, int]] = field(default_factory=list)
    _rects: list[tuple[Rect, int, int]] = field(default_factory=list)
    _sectors: list[tuple[float, float, float, float, float, int, int]] = field(
        default_factory=list
    )
    _labels: list[tuple[int, int, str, Any, float]] = field(default_factory=list)

    def _describe(self, series: int, index: int) -> Hit:
        for si, pi, name, x, y in self._labels:
            if si == series and pi == index:
                return Hit(series, index, name, x, y)
        return Hit(series, index)

    def at(self, x: float, y: float, radius: float = HIT_RADIUS) -> Hit | None:
        """The series and data index under (x, y), or None.

        A point outside the plot area is always None — that is what
        keeps a tooltip from following the cursor over the axis
        labels — and inside it, filled marks (bars, pie slices) win
        over proximity to a line's vertices.
        """
        for rect, series, index in self._rects:
            if rect.contains(x, y):
                return self._describe(series, index)
        for cx, cy, inner, outer, *rest in self._sectors:
            start, end, series, index = rest
            distance = math.hypot(x - cx, y - cy)
            if inner <= distance <= outer:
                angle = math.atan2(y - cy, x - cx) % (2 * math.pi)
                if start <= angle <= end or start <= angle + 2 * math.pi <= end:
                    return self._describe(series, index)
        if not self.plot.contains(x, y):
            return None
        best: tuple[float, int, int] | None = None
        for px, py, series, index in self._points:
            distance = math.hypot(px - x, py - y)
            if distance <= radius and (best is None or distance < best[0]):
                best = (distance, series, index)
        if best is None:
            return None
        return self._describe(best[1], best[2])


# Rendering


@dataclass
class _Plotted:
    """One series with its X values already mapped onto the axis."""

    name: str
    index: int
    points: list[tuple[float, float]]
    raw: list[Any]


def _axis_values(
    data: charts.ChartData, spec: charts.ChartSpec
) -> tuple[list[_Plotted], list[str]]:
    """Series with numeric X, plus the categorical labels if any.

    A categorical axis is drawn at integer slots in the order
    `ChartData.x_labels` fixed, so the renderer and the legend agree
    without either re-deriving it.
    """
    labels = list(data.x_labels)
    if data.x_kind == charts.CATEGORICAL and not labels:
        seen: list[str] = []
        for series in data.series:
            for x, _y in series.points:
                text = str(x)
                if text not in seen:
                    seen.append(text)
        labels = seen
    slots = {label: float(i) for i, label in enumerate(labels)}

    plotted: list[_Plotted] = []
    for index, series in enumerate(data.series):
        mapped: list[tuple[float, float]] = []
        raw: list[Any] = []
        for x, y in series.points:
            value = _finite(y)
            if value is None:
                continue
            if data.x_kind == charts.CATEGORICAL:
                position = slots.get(str(x))
                if position is None:
                    continue
            elif data.x_kind == charts.TEMPORAL:
                position = _epoch(x)
                if position is None:
                    continue
            else:
                position = _finite(x)
                if position is None:
                    continue
            mapped.append((position, value))
            raw.append(x)
        plotted.append(_Plotted(series.name, index, mapped, raw))
    return plotted, labels


def _value_range(plotted: list[_Plotted], stacked: bool) -> tuple[float, float]:
    """The Y extent, with stacks summed per X rather than per series."""
    if stacked:
        totals_up: dict[float, float] = {}
        totals_down: dict[float, float] = {}
        for series in plotted:
            for x, y in series.points:
                target = totals_up if y >= 0 else totals_down
                target[x] = target.get(x, 0.0) + y
        values = list(totals_up.values()) + list(totals_down.values())
    else:
        values = [y for series in plotted for _x, y in series.points]
    if not values:
        return 0.0, 1.0
    # The baseline is pulled into frame by the caller, for the types
    # (bar, area) whose heights misread without it.
    return min(values), max(values)


def _new_layout(cr) -> Pango.Layout:
    layout = PangoCairo.create_layout(cr)
    layout.set_font_description(Pango.FontDescription.from_string(FONT))
    return layout


def render(
    cr,
    spec: charts.ChartSpec,
    data: charts.ChartData,
    width: float,
    height: float,
    *,
    dark: bool = False,
    sparkline: bool = False,
    y_range: tuple[float, float] | None = None,
) -> Rendering:
    """Draw `data` for `spec` onto `cr`, and say where it landed.

    A pure function of its arguments: the same call fills a
    `Gtk.DrawingArea` and an `ImageSurface`, which is the whole of what
    CORE-34's export needs. It never raises on a chart it cannot draw —
    an empty result or a spec `series_from` refused comes back as a
    `Rendering` carrying the reason, which the notice bar shows in
    words.
    """
    colors = palette(dark)
    rendering = Rendering(plot=plot_rect(width, height))
    layout = _new_layout(cr)

    plotted: list[_Plotted] = []
    labels: list[str] = []
    if not data.reason:
        plotted, labels = _axis_values(data, spec)

    if sparkline:
        # A card with one sample, or none, still draws its baseline:
        # the dashboard's cards are the same height whether or not the
        # first poll has come back yet.
        drawn = _draw_sparkline(cr, plotted, width, height, colors, dark, y_range)
        drawn.reason = data.reason
        return drawn

    if data.reason:
        rendering.reason = data.reason
        _draw_notice(cr, layout, data.reason, width, height, colors)
        return rendering
    if not any(series.points for series in plotted):
        rendering.reason = _("Nothing to plot.")
        _draw_notice(cr, layout, rendering.reason, width, height, colors)
        return rendering

    if spec.type == "pie":
        return _draw_pie(cr, layout, spec, data, plotted, width, height, colors, dark)

    return _draw_axes_chart(
        cr, layout, spec, data, plotted, labels, width, height, colors, dark
    )


def highlight(
    cr,
    rendering: Rendering,
    marks: Iterable[tuple[int, int]],
    *,
    dark: bool = False,
) -> None:
    """Ring the marks named by `(series, index)` pairs on a rendering.

    The geometry stays here, with the code that drew it: the view layer
    (CORE-32) knows which rows the grid selected and nothing about
    where they landed in pixels, which is the same split `at()` keeps
    for the other direction.
    """
    wanted = {(int(series), int(index)) for series, index in marks}
    if not wanted:
        return
    colors = palette(dark)
    cr.save()
    cr.set_source_rgb(*rgb(colors.fg))
    cr.set_line_width(2.0)
    for rect, series, index in rendering._rects:
        if (series, index) in wanted:
            cr.rectangle(rect.x, rect.y, rect.width, rect.height)
            cr.stroke()
    for cx, cy, inner, outer, start, end, series, index in rendering._sectors:
        if (series, index) not in wanted:
            continue
        cr.arc(cx, cy, outer, start, end)
        if inner:
            cr.arc_negative(cx, cy, inner, end, start)
        else:
            cr.line_to(cx, cy)
        cr.close_path()
        cr.stroke()
    for px, py, series, index in rendering._points:
        if (series, index) in wanted:
            cr.arc(px, py, 5.0, 0, 2 * math.pi)
            cr.stroke()
    cr.restore()


def _draw_notice(cr, layout, text: str, width: float, height: float, colors) -> None:
    """A sentence where the chart would be. A blank canvas that does
    not say why it is blank is the failure mode worth engineering
    against (RS-03)."""
    cr.set_source_rgb(*rgb(colors.fg))
    text_width, text_height = text_size(layout, text)
    draw_text(
        cr,
        layout,
        text,
        max((width - text_width) / 2, PAD),
        max((height - text_height) / 2, PAD),
    )


def _draw_sparkline(
    cr,
    plotted: list[_Plotted],
    width: float,
    height: float,
    colors,
    dark: bool,
    y_range: tuple[float, float] | None,
) -> Rendering:
    """No axes, no ticks, no legend, one series.

    This is exactly what `monitor_tab._ChartCard` drew by hand until
    CORE-31, down to the baseline and the 1.6 px stroke; the geometry
    is kept identical on purpose, because the dashboard is expected to
    look the same after the port.
    """
    cr.set_source_rgb(*rgb(colors.border))
    cr.set_line_width(1.0)
    cr.move_to(0, height - 0.5)
    cr.line_to(width, height - 0.5)
    cr.stroke()

    rect = plot_rect(width, height, top=_SPARK_TOP, bottom=_SPARK_BOTTOM)
    rendering = Rendering(plot=rect)
    series = next((s for s in plotted if len(s.points) >= 2), None)
    if series is None:
        return rendering

    points = sorted(series.points, key=lambda p: p[0])
    xs = [p[0] for p in points]
    if y_range is not None:
        low, high = y_range
    else:
        values = [p[1] for p in points]
        low, high = min(values), max(values)
    if high <= low:
        high = low + 1.0
    x_scale = Scale(xs[0], xs[-1] if xs[-1] != xs[0] else xs[0] + 1.0, 0.0, width)
    y_scale = Scale(low, high, rect.y + rect.height, rect.y)
    rendering.x_scale, rendering.y_scale = x_scale, y_scale

    cr.set_source_rgb(*rgb(series_colors(dark)[0]))
    cr.set_line_width(1.6)
    for index, (x, y) in enumerate(decimate(points, width)):
        px, py = x_scale.to_px(x), y_scale.to_px(y)
        if index == 0:
            cr.move_to(px, py)
        else:
            cr.line_to(px, py)
    cr.stroke()
    return rendering


def _draw_axes_chart(
    cr,
    layout,
    spec: charts.ChartSpec,
    data: charts.ChartData,
    plotted: list[_Plotted],
    labels: list[str],
    width: float,
    height: float,
    colors,
    dark: bool,
) -> Rendering:
    horizontal = spec.type == "bar" and spec.orientation == "horizontal"
    stacked = spec.stacked and spec.type in ("bar", "area")
    zero_based = spec.type in ("bar", "area")

    low, high = _value_range(plotted, stacked)
    if zero_based:
        low, high = min(low, 0.0), max(high, 0.0)
    v_low, v_high, v_step, v_ticks = nice_ticks(low, high, Y_TICKS)

    categorical = data.x_kind == charts.CATEGORICAL
    if categorical:
        c_low, c_high = -0.5, len(labels) - 0.5
        c_ticks: tuple[tuple[float, str], ...] = tuple(
            (float(i), label) for i, label in enumerate(labels)
        )
    else:
        xs = [x for series in plotted for x, _y in series.points]
        if data.x_kind == charts.TEMPORAL:
            c_low, c_high = (min(xs), max(xs)) if xs else (0.0, 1.0)
            if c_high == c_low:
                c_high = c_low + 1.0
            c_ticks = time_ticks(c_low, c_high, X_TICKS)
        else:
            c_low, c_high, c_step, raw_ticks = nice_ticks(
                min(xs) if xs else 0.0, max(xs) if xs else 1.0, X_TICKS
            )
            c_ticks = tuple((t, format_tick(t, c_step)) for t in raw_ticks)

    # Margins are measured from the labels that will be drawn, so a
    # chart of millions does not clip its own axis.
    title_height = 0.0
    if spec.title:
        title_height = text_size(layout, spec.title, bold=True)[1] + PAD
    legend = [s.name for s in plotted]
    legend_height = 0.0
    if len(legend) > 1:
        legend_height = text_size(layout, legend[0])[1] + PAD

    if horizontal:
        left_labels = [text for _t, text in c_ticks]
        bottom_labels = [format_tick(t, v_step) for t in v_ticks]
    else:
        left_labels = [format_tick(t, v_step) for t in v_ticks]
        bottom_labels = [text for _t, text in c_ticks]
    left = PAD + TICK + max(
        [text_size(layout, text)[0] for text in left_labels] or [0]
    )
    label_height = max(
        [text_size(layout, text)[1] for text in bottom_labels] or [0]
    )
    bottom = PAD + TICK + label_height + legend_height
    rect = plot_rect(
        width, height, left=left, top=PAD + title_height, right=PAD * 2, bottom=bottom
    )

    if horizontal:
        x_scale = Scale(v_low, v_high, rect.x, rect.x + rect.width)
        y_scale = Scale(c_low, c_high, rect.y + rect.height, rect.y)
    else:
        x_scale = Scale(c_low, c_high, rect.x, rect.x + rect.width)
        y_scale = Scale(v_low, v_high, rect.y + rect.height, rect.y)

    rendering = Rendering(plot=rect, x_scale=x_scale, y_scale=y_scale)

    if spec.title:
        cr.set_source_rgb(*rgb(colors.fg))
        draw_text(cr, layout, spec.title, rect.x, PAD / 2, bold=True)

    value_scale = x_scale if horizontal else y_scale
    cat_scale = y_scale if horizontal else x_scale

    _draw_grid(
        cr,
        layout,
        rect,
        colors,
        value_scale,
        v_ticks,
        [format_tick(t, v_step) for t in v_ticks],
        c_ticks,
        cat_scale,
        horizontal,
        categorical,
    )

    colours = series_colors(dark)
    if spec.type == "bar":
        _draw_bars(
            cr, rendering, plotted, rect, cat_scale, value_scale, colours,
            stacked, horizontal, len(labels) or 1,
        )
    elif spec.type == "area":
        _draw_areas(cr, rendering, plotted, rect, x_scale, y_scale, colours, stacked)
    elif spec.type == "scatter":
        _draw_scatter(cr, rendering, plotted, x_scale, y_scale, colours)
    else:
        _draw_lines(cr, rendering, plotted, rect, x_scale, y_scale, colours)

    for series in plotted:
        for index, (x, y) in enumerate(series.points):
            rendering._labels.append(
                (series.index, index, series.name, series.raw[index], y)
            )

    if legend_height:
        _draw_legend(
            cr, layout, legend, colours, rect.x,
            rect.y + rect.height + TICK + label_height + PAD, width, colors,
        )
    return rendering


def _draw_grid(
    cr,
    layout,
    rect: Rect,
    colors,
    value_scale: Scale,
    v_ticks,
    v_labels,
    c_ticks,
    cat_scale: Scale,
    horizontal: bool,
    categorical: bool,
) -> None:
    cr.set_line_width(1.0)
    cr.set_source_rgb(*rgb(colors.fg))

    for tick, label in zip(v_ticks, v_labels):
        px = value_scale.to_px(tick)
        cr.set_source_rgba(*rgb(colors.border), 0.5)
        if horizontal:
            cr.move_to(round(px) + 0.5, rect.y)
            cr.line_to(round(px) + 0.5, rect.y + rect.height)
        else:
            cr.move_to(rect.x, round(px) + 0.5)
            cr.line_to(rect.x + rect.width, round(px) + 0.5)
        cr.stroke()
        cr.set_source_rgb(*rgb(colors.fg))
        text_width, text_height = text_size(layout, label)
        if horizontal:
            draw_text(
                cr, layout, label, px - text_width / 2,
                rect.y + rect.height + TICK,
            )
        else:
            draw_text(
                cr, layout, label, rect.x - TICK - text_width,
                px - text_height / 2,
            )

    # Category labels thin out rather than overlap: a bar chart of 200
    # categories is unreadable either way, but an axis of overprinted
    # text also looks broken.
    widths = [text_size(layout, text)[0] for _t, text in c_ticks]
    needed = sum(widths) + PAD * max(len(widths) - 1, 0)
    stride = 1
    if needed > rect.width and widths:
        stride = max(1, math.ceil(needed / max(rect.width, 1)))
    for index, (tick, label) in enumerate(c_ticks):
        if index % stride:
            continue
        px = cat_scale.to_px(tick)
        if not categorical:
            cr.set_source_rgba(*rgb(colors.border), 0.5)
            if horizontal:
                cr.move_to(rect.x, round(px) + 0.5)
                cr.line_to(rect.x + rect.width, round(px) + 0.5)
            else:
                cr.move_to(round(px) + 0.5, rect.y)
                cr.line_to(round(px) + 0.5, rect.y + rect.height)
            cr.stroke()
        cr.set_source_rgb(*rgb(colors.fg))
        text_width, text_height = text_size(layout, label)
        if horizontal:
            draw_text(
                cr, layout, label, rect.x - TICK - text_width,
                px - text_height / 2,
            )
        else:
            draw_text(
                cr, layout, label,
                min(max(px - text_width / 2, 0), rect.x + rect.width - text_width),
                rect.y + rect.height + TICK,
            )

    cr.set_source_rgb(*rgb(colors.border))
    cr.rectangle(rect.x + 0.5, rect.y + 0.5, rect.width, rect.height)
    cr.stroke()


def _draw_lines(
    cr, rendering: Rendering, plotted, rect: Rect, x_scale, y_scale, colours
) -> None:
    for series in plotted:
        points = sorted(series.points, key=lambda p: p[0])
        if not points:
            continue
        drawn = decimate(points, rect.width)
        cr.set_source_rgb(*rgb(colours[series.index % len(colours)]))
        cr.set_line_width(1.8)
        for index, (x, y) in enumerate(drawn):
            px, py = x_scale.to_px(x), y_scale.to_px(y)
            if index == 0:
                cr.move_to(px, py)
            else:
                cr.line_to(px, py)
        cr.stroke()
        _record(rendering, series, x_scale, y_scale)


def _draw_areas(
    cr, rendering: Rendering, plotted, rect: Rect, x_scale, y_scale, colours, stacked
) -> None:
    base: dict[float, float] = {}
    for series in plotted:
        points = sorted(series.points, key=lambda p: p[0])
        if not points:
            continue
        tops = []
        for x, y in points:
            bottom = base.get(x, 0.0) if stacked else 0.0
            tops.append((x, bottom + y, bottom))
        colour = rgb(colours[series.index % len(colours)])
        cr.set_source_rgba(*colour, 0.30)
        cr.move_to(x_scale.to_px(tops[0][0]), y_scale.to_px(tops[0][2]))
        for x, top, _bottom in tops:
            cr.line_to(x_scale.to_px(x), y_scale.to_px(top))
        for x, _top, bottom in reversed(tops):
            cr.line_to(x_scale.to_px(x), y_scale.to_px(bottom))
        cr.close_path()
        cr.fill()
        cr.set_source_rgb(*colour)
        cr.set_line_width(1.6)
        for index, (x, top, _bottom) in enumerate(tops):
            px, py = x_scale.to_px(x), y_scale.to_px(top)
            if index == 0:
                cr.move_to(px, py)
            else:
                cr.line_to(px, py)
        cr.stroke()
        for position, (x, top, _bottom) in enumerate(tops):
            rendering._points.append(
                (x_scale.to_px(x), y_scale.to_px(top), series.index, position)
            )
        if stacked:
            for x, top, _bottom in tops:
                base[x] = top


def _draw_scatter(cr, rendering: Rendering, plotted, x_scale, y_scale, colours) -> None:
    for series in plotted:
        cr.set_source_rgba(*rgb(colours[series.index % len(colours)]), 0.75)
        # Never decimated: how many points there are *is* the message
        # a scatter carries (RS-03).
        for index, (x, y) in enumerate(series.points):
            px, py = x_scale.to_px(x), y_scale.to_px(y)
            cr.arc(px, py, 3.0, 0, 2 * math.pi)
            cr.fill()
            rendering._points.append((px, py, series.index, index))


def _draw_bars(
    cr,
    rendering: Rendering,
    plotted,
    rect: Rect,
    cat_scale: Scale,
    value_scale: Scale,
    colours,
    stacked: bool,
    horizontal: bool,
    slots: int,
) -> None:
    slot = abs(cat_scale.px1 - cat_scale.px0) / max(slots, 1)
    groups = 1 if stacked else max(len(plotted), 1)
    thickness = max(slot * 0.8 / groups, 1.0)
    zero = value_scale.to_px(max(min(0.0, value_scale.high), value_scale.low))
    base_up: dict[float, float] = {}
    base_down: dict[float, float] = {}

    for order, series in enumerate(plotted):
        colour = rgb(colours[series.index % len(colours)])
        for index, (x, y) in enumerate(series.points):
            centre = cat_scale.to_px(x)
            if stacked:
                store = base_up if y >= 0 else base_down
                start = store.get(x, 0.0)
                store[x] = start + y
                near = value_scale.to_px(start)
                far = value_scale.to_px(start + y)
                offset = -slot * 0.4
                span = slot * 0.8
            else:
                near, far = zero, value_scale.to_px(y)
                offset = -slot * 0.4 + order * thickness
                span = thickness
            low, high = sorted((near, far))
            if horizontal:
                bar = Rect(low, centre + offset, max(high - low, 1.0), span)
            else:
                bar = Rect(centre + offset, low, span, max(high - low, 1.0))
            cr.set_source_rgb(*colour)
            cr.rectangle(bar.x, bar.y, bar.width, bar.height)
            cr.fill()
            rendering._rects.append((bar, series.index, index))


def _record(rendering: Rendering, series: _Plotted, x_scale, y_scale) -> None:
    for index, (x, y) in enumerate(series.points):
        rendering._points.append(
            (x_scale.to_px(x), y_scale.to_px(y), series.index, index)
        )


def _draw_pie(
    cr, layout, spec, data, plotted, width: float, height: float, colors, dark: bool
) -> Rendering:
    """One series as slices, with a donut hole when `stacked` is set —
    the same option a bar reads as "stack", a pie reads as "donut",
    rather than a second flag on the spec for one chart type."""
    series = next((s for s in plotted if s.points), None)
    rendering = Rendering(plot=plot_rect(width, height))
    if series is None:
        return rendering
    slices = [(raw, value) for (_x, value), raw in zip(series.points, series.raw)
              if value > 0]
    total = sum(value for _label, value in slices)
    if not total:
        rendering.reason = _("Nothing to plot.")
        _draw_notice(cr, layout, rendering.reason, width, height, colors)
        return rendering

    colours = series_colors(dark)
    names = [str(name) for name, _v in slices]
    legend_height = text_size(layout, names[0])[1] + PAD if names else 0.0
    title_height = text_size(layout, spec.title, bold=True)[1] + PAD if spec.title else 0
    if spec.title:
        cr.set_source_rgb(*rgb(colors.fg))
        draw_text(cr, layout, spec.title, PAD, PAD / 2, bold=True)

    rows = max(1, math.ceil(len(names) / max(int(width // 140), 1)))
    legend_block = legend_height * rows
    box = min(width - 2 * PAD, height - 2 * PAD - title_height - legend_block)
    radius = max(box / 2, 4.0)
    cx = width / 2
    cy = title_height + PAD + radius
    inner = radius * 0.55 if spec.stacked else 0.0

    angle = -math.pi / 2
    for index, (name, value) in enumerate(slices):
        sweep = 2 * math.pi * value / total
        cr.set_source_rgb(*rgb(colours[index % len(colours)]))
        cr.move_to(cx + inner * math.cos(angle), cy + inner * math.sin(angle))
        cr.arc(cx, cy, radius, angle, angle + sweep)
        if inner:
            cr.arc_negative(cx, cy, inner, angle + sweep, angle)
        else:
            cr.line_to(cx, cy)
        cr.close_path()
        cr.fill()
        rendering._sectors.append(
            (cx, cy, inner, radius, angle % (2 * math.pi),
             (angle % (2 * math.pi)) + sweep, series.index, index)
        )
        rendering._labels.append((series.index, index, str(name), name, value))
        angle += sweep

    _draw_legend(
        cr, layout, names, colours, PAD, cy + radius + PAD, width, colors
    )
    return rendering


def _draw_legend(
    cr, layout, names: list[str], colours, x: float, y: float, width: float, colors
) -> None:
    """Swatch and name per series, wrapping onto further rows rather
    than running off the right edge."""
    height = text_size(layout, names[0] if names else "x")[1]
    left = x
    top = y
    for index, name in enumerate(names):
        text_width = text_size(layout, name)[0]
        entry = height + 4 + text_width + PAD * 2
        if left > x and left + entry > width:
            left = x
            top += height + 2
        cr.set_source_rgb(*rgb(colours[index % len(colours)]))
        cr.rectangle(left, top + 2, height - 4, height - 4)
        cr.fill()
        cr.set_source_rgb(*rgb(colors.fg))
        draw_text(cr, layout, name, left + height + 2, top)
        left += entry
