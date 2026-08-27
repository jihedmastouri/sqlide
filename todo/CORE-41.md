## CORE-41 — Per-connector catalog cache

- **Status:** todo
- **From:** RS-04 (see `docs/dbeaver-comparison.md`)

### Problem

Every `fetch_rows` calls `_assert_known_table(table)`, which is
`list_tables()` — a full catalog listing — and, when any filter or
sort is set, `_assert_filter_columns`, which is `list_columns(table)`.
There is no cache of any kind in the connectors (nothing matching
`cache`/`lru` in `backend/db/postgres/connector.py`). So scrolling one
table re-reads `information_schema` every 500 rows, and `update_cell`
re-reads a table's columns on every single cell.

The validation itself is right and must stay — it is what keeps
unvalidated identifiers out of SQL text. Paying a catalog round trip
for it every time is what should stop.

### Goal

Catalog reads answered from memory within a connection, with an
invalidation story explicit enough that a stale answer cannot cause a
wrong write.

### Approach

- A small `CatalogCache` on the `Connector` base: keyed by
  (database, schema, kind, name), storing `list_tables`,
  `list_columns`, `list_relations` and `list_schemas` results with the
  connector's own lock. No TTL — see invalidation.
- Invalidate on the events that can change the catalog and that we
  already observe: any statement `sql_risk.classify()` labels DDL,
  every path in `frontend/definition_tab.py` and the table designer,
  drop dialogs, extension install/drop, a database or schema switch,
  and the sidebar's explicit Refresh. Coarse invalidation (drop
  everything for the connection) is fine and is what a DDL statement
  should do.
- A miss on validation must be a *reload*, not a rejection: if a
  column is not in the cache, re-read once before raising "unknown
  column", so a column added by another session is picked up rather
  than blocking the user.
- Nothing outside the connector may hold catalog results across a
  refresh; audit the existing ad-hoc caches (the console's hover DDL
  cache, `_reset_hover_cache`) and point them at this one.

### Acceptance criteria

- [ ] Paging a table N times issues one `list_tables` and at most one
      `list_columns`, asserted with a counting fake connector.
- [ ] Saving M cell edits issues one `list_columns`, not M.
- [ ] Any statement classified as DDL invalidates the connection's
      cache; verified by adding a column in one statement and seeing
      it in the next `list_columns`.
- [ ] A validation miss retries against the server once before
      raising `ConnectorError`.
- [ ] The sidebar's Refresh clears the cache for that connection.
- [ ] No behaviour change is observable other than fewer queries —
      the existing metadata and tree tests pass unchanged.
