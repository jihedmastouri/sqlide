## CORE-36 — Export a result set or table to a file

- **Status:** done
- **From:** RS-04 (see `docs/dbeaver-comparison.md`)

### Problem

Nothing in sqlide writes rows to a file. The grid can format a
selection as CSV, JSON, Markdown or `INSERT` statements
(`_format_csv`, `_format_json`, `_format_markdown`, `_format_insert`
in `frontend/data_grid.py`) but only onto the clipboard via
`copy_selection`. The two file-shaped exits that exist are
`pg_dump`-class backups (`backend/backups/dump.py`) and the workspace
XML (`backend/exchange.py`); neither answers "give me these rows as a
CSV". Anyone coming from DBeaver hits this within an hour.

Clipboard also caps the size: it holds the selection, which for a
table tab is at most what has been paged in (`PAGE_SIZE = 500` at a
time), not the table.

### Goal

Export to a file from any grid — a table tab, a query console result,
a query builder run — in CSV, JSON, SQL `INSERT` and Markdown, over
either the current selection, the loaded rows, or the *whole* query
re-run and streamed so a large table does not have to fit in memory.

### Approach

- New `sqlide/backend/export.py`, no GTK: a `Format` enum and one
  writer per format taking `(columns, row_iterable, sink)` and writing
  incrementally. Lift the existing pure formatters out of
  `frontend/data_grid.py` (`_format_csv`, `_format_json`,
  `_format_markdown`, `_format_insert`, `_sql_literal`, `_cell_text`)
  so the clipboard and the file share one implementation and cannot
  drift. Keep `copy_selection` calling into it.
- CSV options that matter and no more: delimiter, quoting, header
  row, NULL representation, encoding (default UTF-8, honouring the
  work already done in `tests/test_encoding.py`).
- Row source is an iterator, so "whole table" streams: re-run the
  tab's query with the tab's filters and sort, pulling
  `Connector.fetch_rows` pages, rather than materialising.
- `frontend/export_dialog.py`: scope (Selection / Loaded rows / Whole
  query), format, format options, destination, and a live preview of
  the first few lines. Runs on a worker thread through `run_async`
  with a progress row and a cancel, like the backup runner.
- Reachable from the grid context menu and the result tab header.
- Binary values follow the grid's own rule (`is_binary` → hex), and
  the chosen encoding is recorded in the dialog so nothing is silently
  lossy.

### Acceptance criteria

- [x] `backend/export.py` imports no GTK and no driver, and is the
      only implementation of each format — `data_grid.copy_selection`
      calls it.
- [x] CSV, JSON, SQL `INSERT` and Markdown each round-trip a fixture
      with NULLs, embedded delimiters, quotes, newlines, non-ASCII
      text and a binary column, covered in `tests/test_export.py`
      with no database server.
- [x] Whole-query export streams: exporting a table larger than
      `PAGE_SIZE` never holds more than one page of rows at a time
      (assert against a fake connector counting `fetch_rows` calls).
- [x] The export honours the tab's active filters and sort order.
- [x] A cancelled export leaves no partial file at the destination.
- [x] An unwritable destination reports a readable error rather than
      raising.
