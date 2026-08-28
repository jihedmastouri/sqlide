## CORE-18 — Builder reads the MetadataProvider (schemas, views, capabilities)

- **Status:** Done
- **Depends on:** CORE-02, CORE-17
- **From:** RS-01 (see `docs/query-builder-research.md`)

### Problem

`QueryBuilderTab._load_catalog()` (`frontend/query_builder.py:310-344`)
goes straight to the connector: `list_tables()`, `list_columns()`,
`list_relations()`. It therefore misses everything CORE-02 and PG-01
built. Three concrete failures:

- schemas do not exist for it — `list_tables_in()`
  (`backend/db/base.py:389`) is never called, and
  `RelationInfo.schema`/`ref_schema` (`base.py:157`) are dropped, so on
  PostgreSQL two same-named tables in different schemas collide in one
  dropdown and the SQL it writes is unqualified;
- views and materialized views are filtered out by
  `if t.kind == "table"` (`query_builder.py:316`) despite being valid
  SELECT sources;
- there are no capability flags, so nothing can be hidden or shown per
  engine.

### Goal

The builder's catalog comes from the `MetadataProvider`, and its source
list is schema-qualified wherever the engine has schemas.

### Approach

- Load through `registry.create_provider(profile.kind, connector)` and
  walk `hierarchy()` / `list_children()` instead of the flat connector
  calls, on the same worker thread as today.
- Make a table reference a `(schema, name)` pair in the query model
  (CORE-17), rendering the schema only where `capabilities().schemas`.
- Include views and materialized views as sources, gated on the
  `materialized_views` capability, marked in the picker so it is clear
  what is being selected.
- Keep the up-front single load: fetch the connected database's
  schemas and their objects once, as `_load_catalog` does now.

### Acceptance criteria

- [x] No direct `connector.list_tables()`/`list_columns()` call remains
      in `frontend/query_builder.py`.
- [x] On PostgreSQL the source picker shows `schema.table`, and the
      generated SQL qualifies names.
- [x] Views (and materialized views where supported) can be selected as
      sources and joined.
- [x] Foreign-key prefill honours `RelationInfo.schema`/`ref_schema`
      and works across schemas.
- [x] On SQLite and MySQL the picker reads exactly as it does today.
