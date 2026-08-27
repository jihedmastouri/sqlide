## CORE-37 — Import a CSV file into a table

- **Status:** todo
- **From:** RS-04 (see `docs/dbeaver-comparison.md`)
- **Depends on:** CORE-36

### Problem

There is no way to get data *into* a table except by typing SQL.
`backend/exchange.py` imports workspaces and connections;
`backend/backups/restore.py` restores a whole dump through `psql` /
`mysql` / `sqlite3`. Neither loads a spreadsheet somebody sent you
into an existing table, which is the ordinary case.

### Goal

Pick a CSV file, map its columns onto an existing table's columns,
see what will be inserted, and run it — parameterised, batched, in one
transaction, with a readable report of what failed.

### Approach

- Extend `backend/export.py`'s companion with a reader in the same
  module family (`backend/importer.py`): sniff delimiter, quoting and
  header row; yield rows lazily; never load the file into memory.
- Mapping is a pure dataclass: source column index → target column,
  plus a per-column NULL token and a "skip" option. Default mapping
  matches on name, case-insensitively.
- Value coercion asks the target column's declared type via the
  `MetadataProvider` (CORE-02) and `Connector.column_type_specs`,
  rather than guessing from the text. A value that cannot be coerced
  is a reported row error, never a silent NULL.
- Execution batches with the driver's executemany, parameter-bound,
  inside one explicit transaction, with a configurable batch size.
  On error: rollback, and report the offending row number and value.
- `frontend/import_dialog.py`: file → preview grid of the first rows
  as parsed → mapping table → mode (append, or truncate-then-append
  behind the `sql_risk` confirmation ladder) → preview of the
  statement shape → run with progress and cancel.
- Reachable from a table node's context menu in the sidebar and from
  a table tab.

### Acceptance criteria

- [ ] Sniffing, mapping and coercion are pure and covered in
      `tests/test_import.py` with no database server.
- [ ] Files with a BOM, CRLF line endings, quoted embedded newlines,
      non-ASCII text and a missing trailing column are each handled or
      rejected with a named reason.
- [ ] Import runs in one transaction: a failure at row N leaves the
      table exactly as it was, verified against SQLite in the suite.
- [ ] Every value reaches the server as a bound parameter; no row
      content is ever interpolated into SQL text.
- [ ] Truncate-then-append goes through `backend/sql_risk.py`'s
      confirmation ladder and respects the connection's environment.
- [ ] The dialog reports rows inserted, rows skipped and the first
      error with its row number.
