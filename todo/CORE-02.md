## CORE-02 — Metadata provider abstraction per engine

- **Status:** todo
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

- [ ] The three providers implement the interface; UI code contains no
      `if (engine === 'postgres')` branching outside the provider layer.
- [ ] Capability flags cover at least: schemas, materialized views, procedures,
      events, grants, roles, extensions, partitions, pragmas.
- [ ] Catalog queries are parameterised — object names from the catalog are never
      string-concatenated into SQL.
- [ ] Version differences are handled or degraded gracefully (state the minimum
      supported server versions in the ticket's notes once decided).

