## CORE-39 — Grid edits saved as one transaction

- **Status:** todo
- **From:** RS-04 (see `docs/dbeaver-comparison.md`)

### Problem

`_execute_updates` in `frontend/data_grid.py` loops over pending edits
calling `connector.update_cell(...)` once per cell. There is no
transaction around the loop, so ten edited cells are ten independent
autocommitted `UPDATE`s. If the fourth fails, the first three are
already written — the failure handler says as much, reloading "to
resync with whatever was applied". The preview dialog shows the user a
block of statements that reads like one atomic change and then does
not behave like one.

It is also ten round trips for what should be one, plus the catalog
lookup `update_cell` performs on each call.

### Goal

Everything in one Save is applied atomically: all of it lands, or none
of it does, and the grid's state after a failure is exactly what it
was before.

### Approach

- A `Connector.apply_changes(table, operations)` that opens an
  explicit transaction, runs the operations in order, and commits —
  rolling back and re-raising as `ConnectorError` on the first
  failure, with the index of the operation that failed so the grid can
  point at the row.
- Resolve the identifier validation once per call rather than once per
  operation (one `list_columns` for the batch, which CORE-41's cache
  then makes free).
- SQLite's connector already runs in a transaction implicitly; make
  the boundary explicit there too so behaviour matches across engines.
- Interaction with the console's own transaction controls: the grid's
  connection is the same connector, so if a user-issued `BEGIN` is
  already open (`Connector.in_transaction`), do not open a nested one
  and do not commit — join the open transaction and let the user's
  Commit/Rollback decide. Say so in the preview dialog's caption.
- Fold CORE-38's inserts and deletes into the same call if that ticket
  has landed; the operation list is the same shape either way.

### Acceptance criteria

- [ ] A batch where operation N fails leaves the table byte-identical
      to its pre-save state, verified against SQLite and PostgreSQL.
- [ ] The error names which row/column failed, not just the driver
      message.
- [ ] Saving into an already-open user transaction neither commits nor
      nests, and the transaction banner stays up.
- [ ] Column validation happens once per batch, not once per
      operation.
- [ ] The `_execute_updates` per-cell loop is gone; nothing in the
      frontend calls `update_cell` in a loop any more.
- [ ] The preview dialog's caption states that the statements run as
      one transaction (or join the open one).
