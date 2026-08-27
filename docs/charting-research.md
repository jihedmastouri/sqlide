---
title: Charting Research
description: What BI/charting would mean in a database client, how it would be drawn, and the tickets that get it there.
order: 16
---

This is the write-up of RS-03. It is research, not implementation: it
answers the questions on the ticket, records what the app already has
that a chart can be built out of, and files the follow-up tickets
(CORE-30 … CORE-35) that carry it out.

The one-line answer: **chart the result set, not the database.** A
serialisable `ChartSpec` in the backend maps result columns to axes and
series; a cairo canvas in the frontend draws it; the chart is a third
view in the tab stack the grid already has, beside Data, Properties and
Map. No charting library, no web view, no new runtime dependency. A
dashboard of several saved charts is a later, separate ticket that
reuses all of it — not the thing v1 ships.

This is deliberately the same shape RS-01 and RS-02 chose
(`docs/query-builder-research.md`, `docs/table-creator-research.md`): a
model in the backend, a pure renderer, a widget that only edits the
model.

## Where we stop

The ticket asks where a database client stops on the road to being a BI
tool. The honest line is drawn by what the app already is: a thing that
shows you a result set. Everything a chart needs is already on screen —
the rows, the SQL that produced them, a Refresh button — so charting
those rows is a small feature. Everything past that point is a
different product: it needs a semantic layer, a scheduler, sharing, and
a data model that spans sources.

**In scope**

- Chart the rows of the result currently loaded in a grid — a query
  console result, a table tab's data, a query builder run.
- A small, fixed set of chart types, with columns mapped to axes and
  series either by inference or by hand.
- Saving that mapping so it comes back with the tab and can be attached
  to a saved query, and exporting the picture as PNG or SVG.
- Later, and only later: a dashboard tab that lays out several saved
  query+chart pairs and refreshes them together (CORE-35).

**Out of scope, and stated as such in the docs**

- Charts spanning more than one connection, or joining across sources.
- A semantic/metric layer — measures, dimensions, hierarchies, drill
  paths. The SQL *is* the semantic layer here.
- Scheduled refresh, emailed reports, published/shared dashboards,
  alerting on a threshold.
- Pivot tables and OLAP cubes. A `GROUP BY` and the grid do that job.
- Interactive drill-down that rewrites the query. Placeholders
  (`backend/placeholders.py`) already parameterise a query; re-running
  with different values is the drill-down we have.

If someone needs the out-of-scope half, they need Metabase or Superset,
and we should say so rather than build a worse one.

## Chart types worth supporting

The five that cover essentially every chart a person draws from a SQL
result, and no more:

| Type | Needs | Typical query |
|---|---|---|
| Line | one ordered axis (time or number) + ≥1 numeric series | a time series from `date_trunc`/`GROUP BY` |
| Bar (grouped or stacked) | one categorical axis + ≥1 numeric series | counts per category |
| Area (stacked) | as line | composition over time |
| Scatter | two numeric columns, optional third for colour | correlation between two measures |
| Pie / donut | one categorical + one numeric, few slices | share of total |

Horizontal bars are an orientation flag on bar, not a sixth type. Pie
gets a hard slice cap with an "other" bucket, because a 200-slice pie is
a bug report waiting to happen.

Deliberately not in v1: combo axes, dual Y scales, box plots,
histograms (which need binning, i.e. a computation, not a mapping),
heatmaps, treemaps, gauges, funnels.

### How columns map to axes

**Infer, then let the user override, and always show what was
inferred.** Pure auto-detection guesses wrong the moment a query
returns two numeric columns; pure manual configuration means every
chart starts as a blank form. So:

1. Classify each result column as *temporal*, *numeric* or
   *categorical*. The classification comes from the backend, from the
   column type the `MetadataProvider` reports where it can and from the
   Python values otherwise — never from the widget sniffing strings, and
   never with a per-engine branch in the UI. This is the same rule the
   grid already follows for geometry columns (PG-04) and for the
   encoding/timezone handling landed on `base.py`.
2. Pick the first temporal or ordered column as X, every numeric column
   that is not X as a series, and a low-cardinality categorical column
   as the series-splitting dimension when exactly one exists.
3. Render that as a visible mapping bar — X, Y, Series — that the user
   can change. Changing it edits the spec; the spec is what gets saved.

A result with no numeric column charts nothing, and says so in words:
"No numeric column to plot" beats an empty canvas.

## Where charts live

Three candidate homes were considered.

1. **A view in the tab that already holds the result.** The table tab is
   already a `Gtk.Stack` with Data and Properties behind a view switcher
   (`frontend/data_grid.py:1682`), and PG-04's map view is the
   precedent for a third pane over the same loaded rows — including
   two-way selection (`ResultGrid.on_row_selected`, `select_row`) and
   the "showing N of M" cap notice. A chart is exactly that pattern
   again.
2. **A separate saved object with its own tab kind.** More BI-ish, but
   it duplicates the query-running machinery and makes "chart what I am
   looking at" a multi-step ceremony.
3. **A dashboard tab holding many charts.** Genuinely useful, genuinely
   later.

**Direction: (1) for v1, with (3) built on top of it once chart specs
persist.** (2) is not needed at all: a "saved chart" is a saved query
plus a chart spec, and both halves already have a home.

Concretely, the chart pane lives in the same stack as Data, keeps its
state when you switch away (the stack keeps both children alive, which
is why unsaved edits survive the trip to Properties today), and redraws
from the rows the grid has loaded. It is offered wherever a `ResultGrid`
is: table tabs, query console results, query builder results.

## Data volume

**Aggregate in SQL. Chart what is loaded, and never fetch more than the
grid did.**

The grid loads `PAGE_SIZE = 500` rows and grows by scrolling
(`data_grid.py`), so a chart built from "the loaded rows" is a chart of
a page unless something says otherwise. Rules:

- The chart draws exactly the rows the grid currently holds, and the
  notice bar says how many, in the same "Showing N of M" language PG-04
  established. A chart of a partial result that does not say so is a
  lie, which is the one failure mode worth engineering against.
- A **Load all for chart** action fetches up to a cap
  (`chart_max_rows`, default 50 000, configurable in settings.toml per
  CORE-13) and refuses past it with the row count and the advice:
  add a `GROUP BY`.
- Beyond ~2 000 points on a line the pixels stop distinguishing points;
  the renderer decimates for drawing (min/max per pixel column) but
  never silently for a scatter, where the point count is the message.
- Client-side aggregation is offered only in its trivial form — sum,
  count, avg, min, max over duplicate X values — because a bar chart of
  a non-aggregated result otherwise draws one bar per row. Anything
  more than that belongs in the query, and the query builder's
  aggregates (CORE-21) is the place we point people at.

## Rendering: what we draw with

This is the decision with the longest tail, so it gets the most space.

The constraint is the dependency list in `pyproject.toml`: the runtime
dependency of sqlide is **PyGObject and nothing else**. Every driver is
an extra. The app ships as a Flatpak (`make flatpak`), where every
dependency is a manifest entry that has to be built.

### Option A — draw it ourselves with cairo

What it costs: nothing new. GTK4 gives every `Gtk.DrawingArea` a cairo
context, PangoCairo does the text, and the app already has
`frontend/canvas.py` — a shared light/dark palette picked at draw time,
`rgb()`, `rounded_rect()`, `draw_text()`, `text_size()` — built for the
relation graph and the query plan graph, plus the plan/relation graphs
themselves (900 lines of working cairo) and the monitoring sparkline
(`monitor_tab.py:665-745`) and the map's feature drawing
(`map_view.py`) as evidence this is a road the project has already
driven down four times.

What it costs in work: axes, ticks and tick labels, a nice-number scale
algorithm, a legend, hit-testing for tooltips. That is real work but it
is bounded and well-trodden — call it a few hundred lines for the five
chart types, most of it in one module shared by all of them.

What it buys beyond the zero dependency cost:

- **Native theming.** Charts pick up light/dark and the libadwaita
  accent the same way every other canvas in the app already does, live,
  without restarting. This is not a small thing: an embedded chart that
  keeps a white background in dark mode looks broken.
- **Export is free.** `cairo.SVGSurface` and `ImageSurface.write_to_png`
  render the same draw function to a file. PNG and SVG export
  (CORE-34) is then a dozen lines rather than a second rendering path.
- **Interaction is ours.** Hover, click-to-select-row, and the two-way
  selection contract PG-04 established are all straightforward on a
  drawing area and awkward through a library's own event model.

### Option B — matplotlib

Matplotlib has had GTK4 backends (`GTK4Agg`, `GTK4Cairo`) since 3.6, so
embedding is genuinely supported, and it draws everything on the list
plus a hundred things that are not.

Cost: matplotlib pulls **numpy, pillow, contourpy, cycler, fonttools,
kiwisolver, pyparsing, packaging** — on the order of 90–120 MB
installed, the largest single addition the project would have made, in
an app whose entire current dependency set is a system GTK. Its licence
(PSF-style, BSD-compatible) is fine; the size is not. Beyond size:
matplotlib's default look is not a GNOME look, theming it to follow the
libadwaita palette live means restyling every artist by hand, its font
rendering is not Pango's so text next to the chart does not match, and
its interaction model would have to be bridged to the grid's selection.
For five chart types this is a lot of machinery imported to be fought
with.

### Option C — a JS charting library in a WebKitGTK view

Cost: WebKitGTK as a hard dependency (very large, and a security
surface), plus vendoring the library's JS, plus a Python↔JS bridge for
selection, plus an offline story. PG-04 explicitly refused a web view
for the map and drew tiles in cairo instead; the same reasoning applies
here with less to gain. Rejected.

### Option D — pygal / SVG generation + librsvg

Pure Python, MIT, tiny — but it produces a static SVG that then has to
be rasterised (another dependency, or a `GdkPixbuf` loader that may not
be present), it cannot follow the theme without regenerating, and it
gives us no hit-testing at all. Rejected.

### Direction

**Option A: cairo, ourselves.** The dependency cost is zero, export and
theming come free, the interaction contract matches the one the grid and
map already speak, and the project has four working cairo canvases to
copy structure from. The cost is a bounded amount of drawing code in one
shared module.

The escape hatch, if the drawing module ever turns out to be a mistake:
because the chart is defined by a serialisable `ChartSpec` and rendered
by a pure function of (spec, data, surface), swapping the renderer later
touches one module. That is the whole reason the spec lives in the
backend.

## Relation to CORE-15's monitoring dashboard

CORE-15 already draws live charts, and it must not be duplicated *or*
generalised into the new thing. Read
`sqlide/backend/db/metrics.py` and `frontend/monitor_tab.py:665`
before writing any of this.

What is there today:

- `metrics.Chart` is a *metric definition*: a name, a title, and a
  `kind` of `rate`, `gauge` or `percent` that says how cumulative server
  counters become a plotted number, plus the counter keys it reads and
  an optional ceiling gauge. There is a fixed tuple per engine
  (`_PG_CHARTS`, `_MYSQL_CHARTS`).
- `metrics.Series` keeps a rolling five-minute window per chart, does
  first differences, and restarts the line when a counter goes
  backwards.
- `_ChartCard` is a title, a big current value, and a **sparkline**: 30
  lines of cairo in `_draw()`, no axes, no ticks, no legend, no
  interaction, autoscaled to the window except for percentages which are
  pinned to 0–100.

The two are not the same object and must not be merged:

| | Monitoring (CORE-15) | Result charts (this) |
|---|---|---|
| Where the data comes from | polled server counters | rows of a result set |
| What defines a chart | a built-in metric, per engine | a user's mapping of columns |
| Lifetime | live, rolling, never persisted | as durable as the query |
| Drawing | sparkline, no axes | axes, ticks, legend, tooltips |

So: **`metrics.Chart` stays exactly where it is** and does not become
the generic spec. What *is* shared is the pixels — scale selection,
nice-number ticks, the series path, the theme-aware colours. CORE-31
puts those in one drawing module (extending `frontend/canvas.py`'s
role) and then **rewrites `_ChartCard._draw` to call it** in its
"sparkline" mode: axes off, ticks off, one series. That is a strict
deletion of duplicated code, and it is an acceptance criterion of
CORE-31 rather than a nice-to-have, because a second copy of the scale
logic is exactly how these two drift apart.

The reverse direction — putting monitoring panels on the new dashboard
(CORE-35) — is explicitly not proposed. Monitoring's panels are gated
on the CORE-14 availability probe and on a dedicated connection; mixing
them into a user-built dashboard would drag that machinery along.

## Overlap with PG-04's map view

Real, and worth taking, but it is the *contract*, not the rendering:

- an alternate view over the rows a `ResultGrid` has already loaded,
  living in the same stack;
- two-way selection — `on_row_selected` into the view, `select_row` back
  out — so clicking a bar highlights its rows and vice versa;
- a bounded number of drawn things with a "Showing N of M" notice
  rather than a silent truncation;
- a notice bar that explains *why* nothing is drawn instead of leaving a
  blank canvas.

CORE-32 should follow `map_view.py`'s structure closely enough that a
reader of one recognises the other. The drawing itself shares nothing:
tiles and geometry projection have no counterpart in a bar chart.

## Persistence

Charts must survive a restart, and must be diffable in git where they
are configuration. Following CORE-13's split — TOML for configuration,
JSON for churn and for SQL people wrote — chart specs land in two
places:

1. **With the tab.** A `ChartSpec` serialised to JSON in a new
   `TabState` field (`backend/workspaces.py:43`), exactly as CORE-19
   does for the query model and CORE-28 for the table model. Reopening
   a tab reopens its chart. Session state, so it stays in
   `workspaces/<id>/state.json`.
2. **With a saved query.** `backend/saved.py`'s `SavedStore` holds
   named SQL in JSON in the config directory. A saved query grows an
   optional chart spec, so "the weekly signups chart" is one saved
   object: SQL plus mapping. Running it opens the console with the chart
   view already selected and already configured. That is also exactly
   what CORE-35's dashboard cells are made of, which is why the
   dashboard is cheap once this exists.

Every serialised spec carries a `version` so a later model change can
migrate or discard cleanly, and an unknown chart type or a mapping
naming a column the result no longer has degrades to "chart not
restored: column X is gone", never to an error dialog. This is the same
rule CORE-19 and CORE-28 already state.

Refresh-on-rerun falls out: the spec names columns, not row indices, so
re-running the query and redrawing is the whole refresh story, and a
result whose columns changed shape reports it in the notice bar.

## Proposed direction, in one place

1. `sqlide/backend/charts.py` — a serialisable `ChartSpec`
   (type, x, series, split, orientation, aggregation, options), column
   classification, and a pure `series_from(spec, ResultSet)` that turns
   rows into plottable series. No GTK import, fully unit-testable, the
   third sibling of `query_model.py` and `table_model.py`. **CORE-30.**
2. `sqlide/frontend/chart_canvas.py` — scales, nice-number ticks, axes,
   legend, the five mark types, light/dark from `canvas.py`, and a
   sparkline mode that `_ChartCard` is converted to use. **CORE-31.**
3. A Chart view in the result tab stack, beside Data/Properties/Map,
   with the mapping bar and the two-way selection contract from PG-04.
   **CORE-32.**
4. Persistence in `TabState` and on saved queries. **CORE-33.**
5. PNG/SVG export and copy-to-clipboard, straight off the same draw
   function. **CORE-34.**
6. A dashboard tab of saved query+chart cells, refreshed together.
   **CORE-35.**

1–5 are v1 and each is independently shippable in that order; 6 is the
BI-shaped part and is a deliberate second step, defensible to cut.
