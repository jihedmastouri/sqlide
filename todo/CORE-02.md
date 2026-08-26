## CORE-02 — Metadata provider abstraction per engine

- **Status:** done
- **Depends on:** —
- **Blocks:** CORE-10, CORE-12, PG-01, MY-01, SQ-01

### Problem

Postgres, MySQL and SQLite disagree on hierarchy (schemas vs databases vs a
single file), on object types (materialized views, procedures, pragmas) and on
how permissions are modelled. Without a shared interface, every feature ticket
re-implements per-engine branching inline.

### Goal

One interface the UI talks to, three implementations.

### Approach

Define a provider exposing at minimum:

- `getHierarchy()` — the ordered levels this engine has (e.g. PG:
  `connection → database → schema → object`; MySQL: `connection → database →
  object`; SQLite: `connection → object`).
- `listChildren(nodeRef)` — children of any node, typed.
- `describe(objectRef)` — the descriptor consumed by CORE-01.
- `getDDL(objectRef)`.
- `listGrants(objectRef)` / `listPrincipals()` — for CORE-10 and CORE-11;
  SQLite returns empty and declares the capability unsupported.
- `capabilities()` — feature flags so the UI hides what an engine can't do
  instead of showing a broken screen.

### Acceptance criteria

- [x] The three providers implement the interface; UI code contains no
      `if (engine === 'postgres')` branching outside the provider layer.
- [x] Capability flags cover at least: schemas, materialized views, procedures,
      events, grants, roles, extensions, partitions, pragmas.
- [x] Catalog queries are parameterised — object names from the catalog are never
      string-concatenated into SQL.
- [x] Version differences are handled or degraded gracefully (state the minimum
      supported server versions in the ticket's notes once decided).

### Notes

- `backend/db/metadata.py` holds the interface (`MetadataProvider`,
  `NodeRef`, `Capabilities`); each engine's implementation lives in its
  own folder (`postgres/metadata.py`, `mysql/metadata.py`,
  `sqlite/metadata.py`), JDBC falls back to the generic provider.
- Those modules import only `db.base`, so `registry.capabilities(kind)`
  and `registry.hierarchy(kind)` answer with no connection open and no
  driver installed — that is what let `frontend/query_console.py` and
  `frontend/users_tab.py` drop their `kind == "mysql"` checks.
- Minimum supported servers: **PostgreSQL 10** and **MySQL 5.7** (the
  oldest in docker-compose.yml / tests/conftest.py), **SQLite 3.25**
  (RENAME COLUMN, which the definition tab already required). Above
  those, differences degrade instead of failing: every catalog call in
  the provider goes through `_safe`, so a catalog a version lacks
  (MySQL 5.7 has no roles) costs that list and nothing else.
- Per-object grants are new adapter calls (`list_object_grants`), and
  the object name goes in as a query parameter; PostgreSQL also gained
  `list_tables_in(schema)` for the schema level, likewise parameterised.
- Left for PG-01: teaching the sidebar to *show* the schema level. The
  provider already reports it and lists a named schema's objects.
