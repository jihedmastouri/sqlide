## CORE-32 — Chart view in the result tab, beside Data and Properties

- **Status:** done
- **Depends on:** CORE-30, CORE-31
- **From:** RS-03 (see `docs/charting-research.md`)

### Problem

There is no way to see a result as anything but a grid. The place for
one already exists: the table tab is a `Gtk.Stack` behind a view
switcher (`frontend/data_grid.py:1682`) and PG-04's map view
(`frontend/map_view.py`) is the precedent for a second pane over the
same loaded rows, complete with two-way selection and a "Showing N of
M" notice.

### Goal

A Chart view in that stack, offered wherever a `ResultGrid` is — table
tabs, query console results, query builder results — showing the
inferred chart immediately and letting the mapping be changed by hand.

### Approach

- A `ChartView` widget fed the loaded rows and the classified columns,
  drawing through CORE-31 and holding a `ChartSpec` from CORE-30 as its
  only state.
- A mapping bar above the canvas: chart type, X, series, split,
  aggregation. It starts on `infer()`'s result and shows what was
  inferred rather than presenting a blank form. Editing it edits the
  spec and redraws.
- Two-way selection following the map's contract: clicking a mark calls
  back into the grid's `select_row`, and `on_row_selected` highlights
  the corresponding mark.
- Notice bar, never a blank canvas: "Showing N of M rows",
  "No numeric column to plot", "Result has no rows".
- Row volume per the research: draw the rows the grid holds, with a
  **Load all for chart** action capped by a new `chart_max_rows`
  setting (default 50 000, documented in `docs/configuration.md`) that
  refuses past the cap with the row count and points at `GROUP BY`.
- Switching away and back keeps the chart and its mapping — the stack
  keeps both children alive, as Data/Properties already relies on.

### Acceptance criteria

- [x] A Chart tab appears in the view switcher for table tabs, console
      results and builder results.
- [x] Opening it on a `GROUP BY` result draws a sensible chart with no
      configuration.
- [x] Every mapping control changes the drawing, and a mapping that
      cannot be drawn explains why instead of blanking.
- [x] Clicking a mark selects its rows in the grid; selecting a row
      highlights its mark.
- [x] A partially loaded result says so; **Load all for chart** honours
      the cap and refuses past it with actionable text.
- [x] Switching Data → Chart → Data preserves grid scroll position,
      filters and unsaved edits, as it does for Properties today.
- [x] No engine-specific code in the view.
