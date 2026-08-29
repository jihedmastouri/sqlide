"""The shared cairo chart renderer (CORE-31).

Two halves are testable without a display, and both are here:

- the geometry — `nice_ticks`, `time_ticks`, `format_tick`, `decimate`,
  `Scale` — which is plain arithmetic over the awkward ranges a real
  result hands it: tiny, huge, negative, zero-span, a single point;
- the drawing itself, rendered onto a `cairo.ImageSurface`, which is
  the same call a `Gtk.DrawingArea` makes (that equivalence is what
  CORE-34's export rests on) and which returns the `Rendering` whose
  `at(x, y)` the view layer hit-tests against.
"""

from __future__ import annotations

from datetime import datetime

import pytest

cairo = pytest.importorskip("cairo")
chart_canvas = pytest.importorskip("sqlide.frontend.chart_canvas")

from sqlide.backend import charts  # noqa: E402

decimate = chart_canvas.decimate
format_tick = chart_canvas.format_tick
nice_ticks = chart_canvas.nice_ticks
time_ticks = chart_canvas.time_ticks


def context(width=320, height=200):
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    return cairo.Context(surface), surface


def data(series, x_kind=charts.NUMERIC, labels=()):
    return charts.ChartData(
        series=tuple(
            charts.Series(name, tuple(points), name)
            for name, points in series
        ),
        x_kind=x_kind,
        x_labels=tuple(labels),
        rows=sum(len(points) for _name, points in series),
    )


# Tick selection


RANGES = [
    (0.0, 1.0),
    (0.0, 0.0),          # zero span
    (7.5, 7.5),          # zero span away from the origin
    (-3.0, 3.0),
    (-1e9, -1e6),        # negative and huge
    (0.0, 1e12),
    (1e-9, 2e-9),        # tiny
    (0.0, 1e-12),
    (5.0, 1.0),          # reversed
    (float("nan"), 3.0),
    (float("-inf"), float("inf")),
]


@pytest.mark.parametrize("low,high", RANGES)
def test_ticks_cover_the_range_without_duplicates(low, high) -> None:
    graph_low, graph_high, step, ticks = nice_ticks(low, high)
    assert ticks, "an axis always has at least one tick"
    assert step > 0
    assert graph_low <= graph_high
    assert len(set(ticks)) == len(ticks)
    labels = [format_tick(tick, step) for tick in ticks]
    assert len(set(labels)) == len(labels), labels
    assert all(graph_low - step <= t <= graph_high + step for t in ticks)


def test_a_single_point_gets_an_axis_around_it() -> None:
    low, high, _step, ticks = nice_ticks(42.0, 42.0)
    assert low < 42.0 < high
    assert len(ticks) >= 2


def test_a_tick_count_is_a_hint_not_a_promise() -> None:
    # A round step beats a fixed count, but the axis stays legible.
    for count in (2, 5, 6, 12):
        _low, _high, _step, ticks = nice_ticks(0.0, 97.0, count)
        assert 2 <= len(ticks) <= count * 3


def test_tick_labels_shorten_large_magnitudes() -> None:
    assert format_tick(1_200_000, 100_000) == "1.2M"
    assert format_tick(0, 1) == "0"
    assert format_tick(0.25, 0.05) == "0.25"
    assert format_tick(float("nan"), 1) == ""


# Temporal ticks


def test_time_ticks_are_human_units_and_never_repeat_a_label() -> None:
    start = datetime(2024, 3, 1).timestamp()
    ticks = time_ticks(start, start + 7 * 86400)
    assert ticks
    labels = [label for _at, label in ticks]
    assert len(set(labels)) == len(labels)
    assert all(start <= at <= start + 7 * 86400 for at, _label in ticks)


def test_time_ticks_survive_a_zero_span_and_a_broken_range() -> None:
    assert time_ticks(10.0, 10.0)
    assert time_ticks(float("nan"), 1.0) == ()


def test_a_narrow_span_ticks_in_seconds_and_a_wide_one_in_years() -> None:
    seconds = [label for _at, label in time_ticks(0.0, 2.0)]
    years = [label for _at, label in time_ticks(0.0, 10 * 31536000)]
    assert any(":" in label for label in seconds)
    assert all(label.isdigit() for label in years)


# Decimation


def test_a_dense_line_is_thinned_but_keeps_its_spikes() -> None:
    points = [(float(i), float(i % 7)) for i in range(20_000)]
    points[5000] = (5000.0, 999.0)
    thinned = decimate(points, 200)
    assert len(thinned) < len(points)
    assert max(y for _x, y in thinned) == 999.0
    assert [x for x, _y in thinned] == sorted(x for x, _y in thinned)


def test_a_short_line_is_left_alone() -> None:
    points = [(0.0, 1.0), (1.0, 2.0), (2.0, 3.0)]
    assert decimate(points, 400) == points


# Every chart type draws, light and dark


TYPES = [
    charts.ChartSpec(type="line", x="t", series=("a", "b")),
    charts.ChartSpec(type="area", x="t", series=("a", "b"), stacked=True),
    charts.ChartSpec(type="bar", x="t", series=("a", "b")),
    charts.ChartSpec(type="bar", x="t", series=("a",), orientation="horizontal"),
    charts.ChartSpec(type="scatter", x="t", series=("a",)),
    charts.ChartSpec(type="pie", x="t", series=("a",)),
]


@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("spec", TYPES, ids=lambda s: f"{s.type}-{s.orientation}")
def test_every_type_renders_in_both_themes(spec, dark) -> None:
    if spec.type in ("bar", "pie"):
        points_a = [("one", 3.0), ("two", 5.0), ("three", 1.0)]
        points_b = [("one", 1.0), ("two", 2.0), ("three", 4.0)]
        payload = data(
            [("a", points_a), ("b", points_b)],
            charts.CATEGORICAL,
            ("one", "two", "three"),
        )
    else:
        points_a = [(float(i), float(i * i % 11)) for i in range(50)]
        points_b = [(float(i), float(i % 5)) for i in range(50)]
        payload = data([("a", points_a), ("b", points_b)])
    cr, surface = context()
    rendering = chart_canvas.render(cr, spec, payload, 320, 200, dark=dark)
    assert not rendering.reason
    surface.flush()
    assert surface.get_data(), "something was drawn"


def test_a_temporal_axis_draws_from_datetimes() -> None:
    start = datetime(2024, 1, 1)
    points = [
        (start.replace(day=1 + i), float(i)) for i in range(10)
    ]
    payload = data([("a", points)], charts.TEMPORAL)
    cr, _surface = context()
    spec = charts.ChartSpec(type="line", x="day", series=("a",), title="Signups")
    rendering = chart_canvas.render(cr, spec, payload, 400, 240)
    assert rendering.x_scale and rendering.y_scale
    assert not rendering.reason


def test_a_refused_spec_is_a_sentence_not_an_exception() -> None:
    cr, _surface = context()
    payload = charts.ChartData(reason="No numeric column to plot.")
    rendering = chart_canvas.render(
        cr, charts.ChartSpec(), payload, 300, 150
    )
    assert rendering.reason == "No numeric column to plot."


def test_the_same_call_renders_to_a_file_surface() -> None:
    """The equivalence CORE-34's export needs: an SVG surface takes the
    same call a drawing area does."""
    import io

    buffer = io.BytesIO()
    surface = cairo.SVGSurface(buffer, 300, 200)
    cr = cairo.Context(surface)
    payload = data([("a", [(0.0, 1.0), (1.0, 4.0), (2.0, 2.0)])])
    chart_canvas.render(
        cr, charts.ChartSpec(type="line", x="t", series=("a",)),
        payload, 300, 200,
    )
    surface.finish()
    assert buffer.getvalue().startswith(b"<?xml")


# Hit testing


def test_a_click_on_a_line_point_names_the_series_and_index() -> None:
    points = [(0.0, 1.0), (1.0, 5.0), (2.0, 3.0)]
    payload = data([("a", points)])
    cr, _surface = context()
    spec = charts.ChartSpec(type="line", x="t", series=("a",))
    rendering = chart_canvas.render(cr, spec, payload, 320, 200)

    px = rendering.x_scale.to_px(1.0)
    py = rendering.y_scale.to_px(5.0)
    hit = rendering.at(px, py)
    assert hit is not None
    assert (hit.series, hit.index) == (0, 1)
    assert hit.name == "a"
    assert hit.y == 5.0


def test_a_click_outside_the_plot_area_is_nothing() -> None:
    payload = data([("a", [(0.0, 1.0), (1.0, 5.0)])])
    cr, _surface = context()
    rendering = chart_canvas.render(
        cr, charts.ChartSpec(type="line", x="t", series=("a",)),
        payload, 320, 200,
    )
    assert rendering.at(1.0, 1.0) is None
    assert rendering.at(1000.0, 1000.0) is None
    # Inside the plot but far from every point is nothing too.
    middle = rendering.plot
    assert rendering.at(middle.x + 4, middle.y + 4) is None


def test_a_click_inside_a_bar_names_its_slot() -> None:
    payload = data(
        [("a", [("one", 3.0), ("two", 9.0)])],
        charts.CATEGORICAL,
        ("one", "two"),
    )
    cr, _surface = context()
    spec = charts.ChartSpec(type="bar", x="c", series=("a",))
    rendering = chart_canvas.render(cr, spec, payload, 320, 200)
    px = rendering.x_scale.to_px(1.0)
    py = rendering.y_scale.to_px(9.0) + 10
    hit = rendering.at(px, py)
    assert hit is not None and hit.index == 1
    assert hit.x == "two"


def test_a_click_inside_a_pie_slice_names_it() -> None:
    payload = data(
        [("a", [("one", 3.0), ("two", 1.0)])],
        charts.CATEGORICAL,
        ("one", "two"),
    )
    cr, _surface = context()
    rendering = chart_canvas.render(
        cr, charts.ChartSpec(type="pie", x="c", series=("a",)), payload, 300, 300
    )
    assert rendering._sectors
    cx, cy, _inner, outer = rendering._sectors[0][:4]
    hit = rendering.at(cx + outer * 0.5, cy - outer * 0.2)
    assert hit is not None and hit.name in ("one", "two")


# Sparkline mode — what the monitoring dashboard draws


def test_a_sparkline_has_no_axes_and_pins_a_percentage_to_0_100() -> None:
    points = [(float(i), 40.0 + i) for i in range(20)]
    payload = data([("value", points)])
    cr, _surface = context(160, 48)
    spec = charts.ChartSpec(type="line", series=("value",))
    rendering = chart_canvas.render(
        cr, spec, payload, 160, 48, sparkline=True, y_range=(0.0, 100.0)
    )
    assert rendering.y_scale.low == 0.0 and rendering.y_scale.high == 100.0
    assert rendering.x_scale.px0 == 0.0 and rendering.x_scale.px1 == 160


def test_a_sparkline_with_too_few_points_still_draws_its_baseline() -> None:
    cr, _surface = context(160, 48)
    spec = charts.ChartSpec(type="line", series=("value",))
    for points in ([], [(0.0, 1.0)]):
        rendering = chart_canvas.render(
            cr, spec, data([("value", points)]), 160, 48, sparkline=True
        )
        assert rendering.y_scale is None
