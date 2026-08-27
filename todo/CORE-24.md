## CORE-24 — Designer reads the MetadataProvider (schemas, types, capabilities)

- **Status:** todo
- **Depends on:** CORE-02, CORE-23
- **From:** RS-02 (see `docs/table-creator-research.md`)

### Problem

`TableDesignerTab` talks to the connector directly — its loader calls
`connector.column_type_specs()` (`frontend/table_designer.py:301-311`)
and the create runs `connector.execute()` (`:437`) — so the
`MetadataProvider` abstraction (`backend/db/metadata.py`) and its
capability flags never reach it. Two consequences: the designer is
schema-blind (it quotes a bare table name, `backend/db/base.py:859`,
even though PG-01 made schemas a level of their own), and it cannot
know what the engine supports before it offers it.

### Goal

Everything the designer knows about the target database comes from the
provider, including which schema the table is being created in.

### Approach

- Take the launching `NodeRef` from the sidebar rather than a bare
  profile (`frontend/window.py:2407`, `:2420`), so the designer opens
  on the schema the user right-clicked.
- A schema chooser in the top bar, populated from the provider and
  shown only where `Capabilities.schemas` is on; the chosen schema
  goes into `TableModel.schema` and the renderer qualifies the name.
- Expose the type list through the provider (`column_type_specs()`
  reachable from `MetadataProvider`), so per-engine catalog knowledge
  has one door.
- Lift the two-argument cap on type parameters (`_MAX_PARAMS`,
  `table_designer.py:41`) so a type spec can declare as many as it
  needs.

### Acceptance criteria

- [ ] On PostgreSQL, creating from a schema node creates the table in
      that schema, and the preview shows the qualified name.
- [ ] The schema chooser is absent on MySQL and SQLite.
- [ ] The designer makes no direct `connector.*` catalog calls.
- [ ] A type declaring three or more parameters renders correctly,
      covered by a test.
