## CORE-30 — Chart model and data mapping in the backend

- **Status:** done
- **From:** RS-03 (see `docs/charting-research.md`)

### Problem

Nothing in the app turns a result set into something plottable. The
only chart definitions that exist are `metrics.Chart`
(`backend/db/metrics.py:131`), which describe server counters for the
monitoring dashboard and say nothing about columns, rows or axes. A
chart view built directly on GTK widget state would repeat the mistake
RS-01 found in the query builder: no model, nothing to persist, nothing
to test.

### Goal

A serialisable chart definition and a pure function that applies it to
a `ResultSet`, with no GTK import and no per-engine branching — the
third sibling of the proposed `backend/db/query_model.py` (CORE-17) and
`backend/db/table_model.py` (CORE-23).

### Approach

- `sqlide/backend/charts.py` with a `ChartSpec` dataclass: chart type
  (`line`, `bar`, `area`, `scatter`, `pie`), x column, series columns,
  optional split column, orientation, client aggregation
  (`none|sum|count|avg|min|max`), and per-type options (stacked, slice
  cap, point cap). A `version` field, as CORE-19/CORE-28 require.
- `classify(columns, rows, provider=None)` labels each result column
  `temporal`, `numeric` or `categorical`, preferring the declared type
  from the `MetadataProvider` (CORE-02) and falling back to the Python
  values. The UI never sniffs types itself and never branches on engine.
- `infer(result, classes) -> ChartSpec | None` implements the defaults
  from the research: first temporal/ordered column as X, numeric columns
  as series, a single low-cardinality categorical as the split. Returns
  `None`, with a reason string, when there is nothing numeric to plot.
- `series_from(spec, result) -> Chart data` produces named series of
  (x, y) pairs plus the axis kinds, applying the client aggregation over
  duplicate X values and reporting the counts it dropped or capped.
- `to_dict`/`from_dict` round-trip, tolerating unknown keys and unknown
  chart types by reporting rather than raising.

### Acceptance criteria

- [x] `backend/charts.py` imports no GTK and no driver.
- [x] Column classification is covered for temporal, numeric,
      categorical, all-NULL and mixed columns.
- [x] `infer` produces the documented mapping for a time series, a
      category/count result and a two-numeric-column result, and returns
      a reason for a result with no numeric column.
- [x] `series_from` aggregates duplicate X values for every supported
      aggregation, and reports capped/dropped counts rather than
      silently truncating.
- [x] A spec round-trips through `to_dict`/`from_dict`; a spec with an
      unknown type or a missing column loads as a reported failure, not
      an exception.
- [x] `metrics.Chart` is untouched — this is not a generalisation of it.
- [x] Covered by `tests/test_charts.py`, needing no database server.
