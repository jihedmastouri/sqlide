## CORE-04 — Table tab: Data / Properties toggle

- **Status:** done (the toggle superseded by CORE-47)
- **Depends on:** CORE-01
- **Blocks:** CORE-05, CORE-11

### Goal

An open table has a toggle between its data grid and a Properties view holding
everything about the table.

### Acceptance criteria

- [x] A visible toggle (Data | Properties) on every open table tab.
- [x] Properties contains, per engine capability: general info (owner, size, row
      estimate, comment), Columns, Constraints, Foreign keys, References,
      Indexes, Triggers, Partitions, Rules, Policies, Dependencies, Source
      /related functions, and full DDL.
- [x] Sections a given engine doesn't support are omitted, not shown empty.
- [x] Switching to Properties does not discard unsaved grid edits or lose the
      grid's scroll position/filters when switching back.
- [x] Each row in a Properties section opens that child object's info view
      (CORE-01).

### Notes

- **Superseded by CORE-47**: the Data | Properties toggle is gone.
  Properties are a page of the right side panel that follows the
  active tab, and can be torn off into a window of their own; the
  table tab shows data. Everything below about *what* a properties
  view holds still stands — only where it is read changed.
- Which sections exist is a capability question, so it is answered by
  the provider layer: `MetadataProvider.property_sections()` (a
  classmethod, like `capabilities()`) filters
  `objects.PROPERTY_SECTIONS` by the new `constraints`, `rules`,
  `policies`, `dependencies` and `related_functions` flags, and
  `table_properties(ref)` fills them. `registry.property_sections(kind)`
  answers with no connection open.
- New optional `Connector` calls, all with empty defaults so every
  adapter keeps working: `table_stats`, `list_constraints`,
  `list_references` (derived from `list_relations`), `list_partitions`,
  `list_rules`, `list_policies`, `list_dependencies`,
  `list_table_functions`. PostgreSQL implements all of them, MySQL
  stats/constraints/partitions, SQLite stats and constraints read back
  off the PRAGMAs. Table names travel as parameters or quoted
  identifiers, never concatenated.
- Rendering is shared with CORE-01: `object_info.InfoBody` was split out
  of `ObjectInfoTab` and both it and `PropertiesView` use it, so a row in a Properties section
  opens the child's info view exactly as it does in the info tab.
- Sections an engine supports but that are currently empty keep their
  heading and say "(none)" — "this engine has no policies" and "this
  table has none yet" are different answers.
