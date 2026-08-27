## CORE-27 — Engine-specific table and column options

- **Status:** todo
- **Depends on:** CORE-23, CORE-24
- **From:** RS-02 (see `docs/table-creator-research.md`)

### Problem

No dialect overrides `create_table_sql()`: every engine gets the base
implementation (`backend/db/base.py:833`), which knows columns, types,
defaults, NOT NULL and a primary key and nothing else. So none of the
options that actually distinguish these engines can be set —
PostgreSQL `GENERATED … AS IDENTITY` (only the `serial` type entry
exists, `postgres/connector.py:1601`), generated columns, `UNLOGGED`,
tablespaces and partitioning; MySQL storage engine, charset/collation,
`AUTO_INCREMENT` seed, row format and column comments; SQLite
`WITHOUT ROWID` and `STRICT`. RS-02 named all of these; today the
answer to each is "leave the designer and write it by hand".

### Goal

Each engine's own table and column options, offered by the engine
rather than hard-coded in the frontend.

### Approach

- A declarative descriptor in `backend/db/metadata.py`:
  `OptionSpec(name, label, scope, kind, choices, default, note)`,
  where `scope` is table or column and `kind` is boolean / choice /
  text / integer. `MetadataProvider.table_options()` and
  `column_options()` return them.
- Each provider declares its own set; values live in the
  `TableModel`/`ColumnModel` option maps (CORE-23) and the renderer
  emits them in the dialect's own syntax.
- The designer renders an **Options** group from the descriptors and
  never branches on engine — an unknown option kind is simply not
  shown.
- Identity/auto-increment is a column option, not a type: PostgreSQL's
  `serial` entries stay in the type list for people who look for them,
  but the designer prefers `GENERATED … AS IDENTITY` for new tables
  and says why in the note.
- Where an option only makes sense with a capability
  (`Capabilities.partitions`), gate it on that flag.

### Acceptance criteria

- [ ] Creating a table on MySQL can set engine, charset, collation and
      an `AUTO_INCREMENT` seed, and the preview shows them.
- [ ] Creating a table on PostgreSQL can declare an identity column
      and a generated column.
- [ ] Creating a table on SQLite can set `WITHOUT ROWID` and `STRICT`.
- [ ] No option name appears in `frontend/`; adding one to a provider
      is enough to make it appear in the designer.
- [ ] Rendering of each engine's options is covered by tests over the
      model, with no database connection.
