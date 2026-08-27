## CORE-28 — Persist the designer's table model in the workspace

- **Status:** todo
- **Depends on:** CORE-23
- **From:** RS-02 (see `docs/table-creator-research.md`)

### Problem

`TableDesignerTab.tab_state()` returns `None`
(`frontend/table_designer.py:314`): the tab is session-only and is not
restored. A half-designed thirty-column table dies with the window, or
with an accidental tab close, and there is no way to keep a design
around at all. RS-01 made the same call for the query builder
(CORE-19): a builder-style tab that generates SQL one way has to
persist its *model*, because nothing can reconstruct it afterwards.

### Goal

Close and reopen a designer tab, or restart the app, and get the table
you were designing back exactly as it was.

### Approach

- Serialise the CORE-23 model to JSON and carry it in a `TabState`
  field (`backend/workspaces.py:43`), beside the `sql` field the console
  uses; `tab_state()` stops returning `None`.
- Restore rehydrates the model after the type list has loaded; a
  version field lets a future model change migrate or discard
  cleanly.
- In alter mode (CORE-26) store the target model and reload `current`
  from the catalog on restore, dropping edits that no longer apply and
  saying so in the status label rather than failing.

### Acceptance criteria

- [ ] A designer with several columns, constraints and options
      restores identically after a restart.
- [ ] A workspace file written by an older build still opens.
- [ ] A restored alter-mode designer whose table has changed
      underneath opens with the stale parts removed and an
      explanation shown, not an error dialog.
- [ ] Covered by a test that round-trips a `TabState` through the
      workspace layer.
