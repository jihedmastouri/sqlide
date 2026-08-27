## CORE-33 — Persist chart specs: with the tab, and on saved queries

- **Status:** todo
- **Depends on:** CORE-30, CORE-32
- **From:** RS-03 (see `docs/charting-research.md`)

### Problem

A chart configured by hand dies with the tab, exactly as the query
builder's model does (CORE-19) and the table designer's does
(CORE-28). And there is no way to keep "the weekly signups chart"
around at all: `backend/saved.py` stores SQL text and nothing else, so
re-running a saved query gives back a grid and a mapping to redo.

### Goal

A chart comes back when its tab comes back, and a saved query can carry
the chart it is meant to be seen as.

### Approach

- Serialise the `ChartSpec` to JSON into a new `TabState` field
  (`backend/workspaces.py:43`), beside `sql`, exactly as CORE-19 and
  CORE-28 do for their models. Session churn, so it stays in
  `workspaces/<id>/state.json` rather than the TOML config (CORE-13).
- `SavedItem` (`backend/saved.py:28`) grows an optional `chart` field.
  Saving a query from a tab with a chart offers to save the chart with
  it; opening such a saved query opens the console with the Chart view
  selected and configured. Older files without the field load
  unchanged.
- Restore is column-name based, so a re-run refreshes the chart with no
  extra machinery. A spec naming a column the result no longer has
  restores with those parts dropped and the notice bar saying so —
  never an error dialog.
- The `version` field from CORE-30 is checked on load; an unknown
  version is discarded with a message, not raised on.

### Acceptance criteria

- [ ] A configured chart restores identically after a restart.
- [ ] A workspace or saved-query file written by an older build still
      opens, with no chart.
- [ ] A saved query with a chart opens showing that chart, and
      re-running it redraws from the new rows.
- [ ] A restored spec whose columns have changed opens with the missing
      parts removed and an explanation shown.
- [ ] Covered by a test that round-trips a `TabState` through the
      workspace layer and a `SavedItem` through `SavedStore`.
