## CORE-35 — Dashboard tab: several saved charts, refreshed together

- **Status:** todo
- **Depends on:** CORE-33
- **From:** RS-03 (see `docs/charting-research.md`)

### Problem

Once a saved query can carry a chart (CORE-33), the only thing between
sqlide and a useful operational dashboard is a place to put several of
them at once. Today each one costs a tab and a manual re-run.

### Goal

A dashboard tab: a grid of cells, each a saved query plus its chart,
refreshed on demand or on an interval, within one workspace.

### Approach

- A `dashboard` tab kind in `TabState`. The dashboard's own definition
  — its name, layout and the saved queries it references by name — is
  configuration, not churn, so it lives in a TOML file under the config
  directory per CORE-13, and is therefore git-diffable. The *open tab*
  is just a reference to it.
- Cells are laid out on a simple resizable grid; each shows its title,
  its chart (CORE-31), a per-cell refresh and an "open the query"
  action into a console.
- One connection per dashboard, chosen when it is created; queries run
  sequentially on a worker thread so a slow cell never blocks the
  others' redraw, and a failed cell shows its error inside its own cell.
- Refresh: a manual button plus an optional interval, reusing the
  clamped-interval and pause behaviour the monitoring dashboard
  established (`backend/db/metrics.py:50`). Refreshing stops when the
  tab is closed.
- Explicitly **not** included: monitoring panels (they are gated on the
  CORE-14 probe and a dedicated connection), cells from more than one
  connection, scheduling, sharing, alerting.

### Acceptance criteria

- [ ] A dashboard can be created, named, and given cells from saved
      queries that have charts.
- [ ] Cells can be added, removed, resized and reordered, and the
      layout persists.
- [ ] The dashboard definition is a readable TOML file that can be
      hand-edited and committed.
- [ ] Manual refresh re-runs every cell; the optional interval does the
      same and is pausable and stops on close.
- [ ] A cell whose query fails shows the error in place; the rest still
      refresh.
- [ ] A cell referencing a deleted saved query says so instead of
      disappearing or erroring the whole dashboard.
