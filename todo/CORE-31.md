## CORE-31 — Cairo chart canvas, shared with the monitoring sparkline

- **Status:** todo
- **Depends on:** CORE-30
- **From:** RS-03 (see `docs/charting-research.md`)

### Problem

RS-03 chose to draw charts with cairo rather than add a charting
library (matplotlib would add ~100 MB and numpy to an app whose only
runtime dependency is PyGObject, and would not follow the libadwaita
theme). That means we own the axes. Meanwhile
`monitor_tab._ChartCard._draw` (`frontend/monitor_tab.py:711-745`)
already contains hand-rolled autoscaling and series drawing; a second
copy in a new module is how the two drift apart.

### Goal

One drawing module that renders a `ChartSpec` plus its series to any
cairo context — on screen, or to a file — theme-aware, with the
monitoring sparkline rewritten to use it.

### Approach

- `sqlide/frontend/chart_canvas.py`, using `frontend/canvas.py` for the
  light/dark palette (picked at draw time, never cached — the style can
  flip while a tab is open), `rgb()` and the Pango text helpers.
- A nice-number scale/tick algorithm shared by both axes, with date and
  time tick labels for a temporal axis.
- Mark drawing for line, area (stacked), bar (grouped and stacked,
  vertical and horizontal), scatter and pie/donut, plus a legend and a
  categorical series palette that holds up on both light and dark
  backgrounds.
- Decimation for line/area past a pixel-density threshold (min/max per
  column); scatter is never decimated silently.
- Hit-testing: `at(x, y)` returns the series and data index under a
  point, so the view layer can drive tooltips and selection without
  knowing the geometry.
- A `sparkline=True` mode: no axes, no ticks, no legend, one series —
  and `_ChartCard._draw` rewritten to call it, deleting its own
  scaling code. Percentage charts stay pinned to 0–100 as they are now.

### Acceptance criteria

- [ ] No new runtime dependency is added to `pyproject.toml`.
- [ ] All five chart types render, in both light and dark, and redraw
      on a live theme change.
- [ ] Tick selection is unit-tested for representative ranges (tiny,
      huge, negative, zero-span, single point) with no crash and no
      duplicate labels.
- [ ] The monitoring dashboard looks and behaves as it does today, with
      `_ChartCard` holding no scaling or path code of its own.
- [ ] Hit-testing returns the expected point for a click inside and
      `None` outside the plot area.
- [ ] Drawing is a pure function of (spec, data, context, size) — the
      same call renders to a `Gtk.DrawingArea` and to an
      `ImageSurface`, which is what CORE-34 needs.
