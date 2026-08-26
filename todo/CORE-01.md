## CORE-01 — Object info view: every sidebar node opens something

- **Status:** todo
- **Depends on:** —
- **Blocks:** CORE-04, PG-02, MY-01, SQ-01

### Problem

Large parts of the sidebar tree are decorative — clicking a node does nothing, or
only expands it. There is no way to look at a single index, constraint, trigger
or sequence.

### Goal

Every node in the tree, including leaves like a single index or a single column,
opens a view in the main area with general information about that object.

### Approach

Introduce one generic `ObjectInfoView` driven by a descriptor the metadata layer
returns, rather than hand-writing a screen per object type:

- A header (icon, name, qualified path, object type).
- A key/value summary block (owner, size, created/modified where available,
  comment).
- Zero or more detail tables (e.g. an index shows its columns and their sort
  order; a trigger shows timing/event/function).
- A DDL block with copy-to-clipboard.

Unknown or not-yet-implemented object types fall back to a generic summary built
from whatever the catalog query returned, never a blank screen or an error.

### Acceptance criteria

- [ ] Single-click selects a node; double-click (or Enter) opens its info view.
- [ ] Every node type present in the PG, MySQL and SQLite trees resolves to a
      view — verified by walking the whole tree on a sample DB per engine.
- [ ] Group/folder nodes (e.g. "Indexes") open a list of their children with the
      most relevant columns, and rows in that list open the child's info view.
- [ ] Opening the same object twice focuses the existing tab instead of opening a
      duplicate.
- [ ] DDL is copyable and matches what the server reports.
- [ ] An object type with no specific descriptor renders the generic fallback.

### Out of scope

Editing anything from the info view — read-only for now.


