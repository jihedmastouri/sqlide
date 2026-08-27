## CORE-38 — Insert and delete rows in the data grid

- **Status:** todo
- **From:** RS-04 (see `docs/dbeaver-comparison.md`)

### Problem

The grid edits existing cells and nothing else. `Connector` has
`update_cell` and no insert or delete counterpart
(`backend/db/base.py`), and `frontend/data_grid.py` has no add-row or
delete-row action — the only mutation path is `_pending` → one
`UPDATE` per changed cell. Adding a row to a lookup table, or removing
a bad one, means opening a console and typing SQL. In DBeaver both are
a keystroke, and it is the most-used thing in the tool.

### Goal

Add a row and delete a row from the grid, held as pending edits
alongside cell edits, shown in the same preview dialog, and written
through the same review-then-run path.

### Approach

- Two new `Connector` methods beside `update_cell`, with the same
  discipline: `insert_row(table, values)` and
  `delete_row(table, pk_values)` — identifiers validated against the
  catalog, values bound as parameters, `expect_rowcount=1` on delete.
- Pending state grows from `dict[key, changes]` to a small ordered
  list of operations (insert / update / delete) so the preview shows
  them in the order they will run and a row can be added then edited
  before saving.
- Grid: a "+" that appends a blank editable row (with the table's
  defaults left empty and NOT NULL columns marked), and a delete on
  the row-number context menu marking the row struck through until
  saved. Both only while the edit toggle is unlocked.
- Gating is the same one already applied to editing: a table with no
  primary key, and any view, stays read-only. Delete needs the PK;
  insert does not, but without one the inserted row cannot be
  addressed afterwards, so it reloads rather than pretending.
- Deleting goes through `backend/sql_risk.py` like any other
  destructive action, so a production connection asks harder.

### Acceptance criteria

- [ ] `insert_row` and `delete_row` exist on all four adapters
      (sqlite, mysql, postgres, jdbc) with catalog-validated
      identifiers and bound values.
- [ ] `delete_row` refuses an empty `pk_values` and asserts exactly
      one affected row.
- [ ] The preview dialog lists inserts, updates and deletes in
      execution order, with the same "values are bound as parameters"
      caption.
- [ ] Insert and delete are hidden for views and for tables with no
      primary key (delete), matching the existing read-only rule.
- [ ] A row added and then edited before saving produces one INSERT,
      not an INSERT plus UPDATEs.
- [ ] Deleting is subject to the environment-aware confirmation
      ladder.
- [ ] Covered against SQLite in the suite; no new server dependency.
