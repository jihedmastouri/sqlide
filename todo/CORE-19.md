## CORE-19 — Persist the builder's query model in the workspace

- **Status:** Done
- **Depends on:** CORE-17
- **From:** RS-01 (see `docs/query-builder-research.md`)

### Problem

`QueryBuilderTab.tab_state()` (`frontend/query_builder.py:187`) saves
`kind` and the base table name and nothing else, and restore re-opens
an empty builder on that table (`frontend/window.py:1730-1732`). Every
join, checked column, filter and sort is lost on restart — which is
the whole query. RS-01 decided the builder is one-way (we never parse
SQL back into it), so persisting the model is the *only* way a built
query survives.

### Goal

Close and reopen a builder tab, or restart the app, and get the query
back exactly as it was built.

### Approach

- Serialise the CORE-17 model to JSON and carry it in a new
  `TabState` field (`backend/workspaces.py:43`), beside the existing
  `sql` field the console uses. Keep `table` populated for
  backwards compatibility.
- Restore by rehydrating the model after the catalog load completes,
  dropping any part of it whose table or column no longer exists and
  saying so in the status label rather than failing.
- Version the serialised form so a future model change can migrate or
  discard cleanly.

### Acceptance criteria

- [x] A builder with joins, checked columns, filters and sorts restores
      identically after a restart.
- [x] A workspace file written by an older build still opens; the
      builder falls back to base-table-only restore.
- [x] A restored query referring to a dropped table/column opens with
      the missing parts removed and an explanation shown, not an error
      dialog.
- [x] Covered by a test in `tests/` that round-trips a `TabState`
      through the workspace layer.
