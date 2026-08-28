## CORE-45 — Find a value across a database's tables

- **Status:** Done
- **From:** RS-04 (see `docs/dbeaver-comparison.md`)

### Problem

`frontend/tree_search.py` searches object *names* in the sidebar.
There is no way to search object *contents* — "which table has this
order id in it", the first move when tracing a record through a schema
somebody else designed. DBeaver's data search covers it and it is one
of the few of its features that has no substitute in a console
(writing it by hand means one SELECT per candidate table).

### Goal

Search a value across the tables of a database and get a list of
table/column/row hits you can open.

### Approach

- `sqlide/backend/db/search.py`, pure planning: given the catalog
  (tables and their columns, from the `MetadataProvider`) and a search
  term, decide which columns are searchable and produce one bounded
  parameterised `SELECT` per table.
- Column selection is type-driven and conservative: text columns for
  a substring search; numeric and date columns only when the term
  parses as that type; binary and geometry skipped. This is what keeps
  the search from casting every column to text, which is how this
  feature usually becomes a table scan of everything.
- Per-table statement shape:
  `SELECT <pk cols>, <matched cols> FROM t WHERE c1 LIKE %s OR c2 LIKE %s LIMIT n`
  — bound parameters, catalog-validated identifiers, a per-table row
  cap and an overall hit cap.
- Options, small set: exact / contains, case sensitivity, which
  schemas, which tables (default all non-system), max rows per table.
- Frontend: a search tab, not a modal — results stream in as a list of
  table · column · matched value, each row opening a table tab
  filtered to that hit (reusing CORE-43's "open filtered" path).
  Cancellable, running on a worker thread, showing which table it is
  on.
- Explicitly bounded and explicitly declared: the tab says how many
  tables it will scan before it starts, and a scan on a production
  connection warns first — this is the one feature here that can put
  real load on a server.

### Acceptance criteria

- [x] Planning is pure and covered in `tests/test_search.py` with no
      database server: given a catalog and a term, the chosen columns
      and generated statements are asserted.
- [x] A numeric-looking term searches numeric columns; a
      non-numeric one does not, and no column is cast to text to make
      it match.
- [x] Every statement is parameter-bound, identifier-validated and
      row-capped.
- [x] The scan runs on a worker thread, reports progress per table,
      and cancels promptly.
- [x] A table the account cannot read is skipped with a reported
      reason, not a failed scan.
- [x] Opening a hit lands on the right table tab with a filter that
      selects that row.
- [x] The tab states the table count up front and warns on a
      production-classed connection.
