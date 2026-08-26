## CORE-05 — Deep-link from the sidebar into a properties section

- **Status:** done
- **Depends on:** CORE-04

### Goal

Opening e.g. *Tables → orders → Indexes* from the sidebar lands directly on that
table's Properties view with the Indexes section selected.

### Acceptance criteria

- [x] Every child node under a table in the sidebar maps to a properties section.
- [x] If the table tab is already open, it is reused and switched to Properties.
- [x] The target section is scrolled into view and visibly selected.
- [x] Opening a single child object (one specific index) opens that object's info
      view instead, per CORE-01.


### Notes

- A table's sidebar children are now its Properties sections rather
  than a bare column list: `registry.property_sections(kind)` answers
  which exist without a connection, so filling the rows is synchronous
  and an engine never grows a Policies row it cannot fill.
- Sections whose members are objects the tree already opens (Columns,
  Indexes, Triggers — `objects.SECTION_CHILD_KINDS`) still expand into
  them, so one specific index opens its own info view (CORE-01); the
  rest are leaves that only deep-link.
- Activating a section row calls `window.open_table_section`, which
  reuses the open table tab (`_tab_for`) and calls
  `TableTab.show_properties(section)`: the toggle flips to Properties
  and the grid keeps its rows, edits, filters and scroll position.
- The target section is found by slug: `DetailTable.slug` carries the
  `PROPERTY_SECTIONS` key through the descriptor, `InfoBody` keeps a
  slug → widget map and `select_section` scrolls to it and marks it
  with the `.section-target` accent rule. A link that arrives while the
  catalog read is still running is remembered and applied on render.
