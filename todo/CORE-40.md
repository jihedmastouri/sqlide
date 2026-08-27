## CORE-40 — Stable, efficient data-grid pagination

- **Status:** done
- **From:** RS-04 (see `docs/dbeaver-comparison.md`)

### Problem

`fetch_rows` builds `SELECT * FROM t {where} {order} LIMIT %s OFFSET %s`
(`backend/db/postgres/connector.py`, and the same shape in mysql and
sqlite), where `{order}` is empty unless the user set a sort. Neither
PostgreSQL nor MySQL guarantees a stable row order between two such
statements, so:

- **It is a correctness bug.** Page 2 may repeat rows from page 1 or
  skip rows entirely. Because the grid *appends* pages on scroll
  (`_load_more`), the user sees the duplicates sitting next to each
  other and has no reason to suspect the tool.
- **It is slow at depth.** `OFFSET n` makes the server produce and
  discard n rows. Scrolling to row 200,000 of a table costs the server
  a scan of 200,000 rows on every page after it.

DBeaver sidesteps both by holding one server-side cursor and fetching
forward instead of re-issuing a statement per page.

### Goal

Paging that returns each row exactly once, in a stable order, without
the cost growing with how far down the user has scrolled.

### Approach

- **Always order.** When the user has set no sort, append a
  deterministic tiebreaker: the primary key columns from
  `list_columns`, or SQLite's `rowid` where the table has one. Where
  no such key exists (a view, a heap with no PK), keep `OFFSET` but
  surface it: the status line says the order is not guaranteed, which
  is honest and is what the user needs to know.
- **Keyset paging where the order is unique-prefixed.** Replace the
  offset with `WHERE (k1, k2, …) > (:last1, :last2, …)` carrying the
  last row's key values forward, which every engine here supports as a
  row comparison and which uses the index the key already has. The
  user's own sort columns lead the tuple; the PK tiebreaker closes it.
  Cost per page becomes constant.
- Keep the offset path for the cases keyset cannot cover (a view with
  no key, a jump to an arbitrary page number rather than a scroll),
  and let the adapter decide which it can do — one `paging_strategy()`
  on the connector rather than an engine check in the grid.
- Reset the carried key on filter change, sort change and Refresh; the
  grid already funnels those through `_first_page`.
- Mixed ASC/DESC sorts make a single row comparison unusable — fall
  back to offset there rather than emitting a wrong predicate.

### Acceptance criteria

- [x] With no user sort, the generated SQL always carries an
      `ORDER BY` on a key, or the UI declares the order unguaranteed.
- [x] Reading a fixture table page by page returns every row exactly
      once, on all three engines, including with a filter and with a
      non-unique user sort.
- [x] Keyset paging emits a row-comparison predicate and no `OFFSET`
      when the sort is unique-prefixed; deep-page SQL is identical in
      shape to first-page SQL.
- [x] Mixed-direction sorts, and orders with no unique suffix, fall
      back to offset rather than emitting a wrong predicate.
- [x] Filter change, sort change and Refresh reset the cursor.
- [x] The SQL shown by the tab's "describe query" line matches what is
      actually run.
