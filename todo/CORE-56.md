## CORE-56 — Tabular objects open in their own tab, not the properties panel

- **Status:** done
- **Depends on:** CORE-49, CORE-50, CORE-52
- **Corrects:** CORE-50, whose intent was this rather than a per-object panel surface

### Problem

Clicking a collection node — Indexes, Constraints, Triggers, Columns, Grants —
routes to the properties surface in the right side panel. That is the wrong
destination for content that is plainly a table of rows: the panel is narrow, it
is a glance-at-it surface, and only one object's worth is in front of you at a
time. It also means the panel is doing double duty as a viewer, which is what
made the recycling complaint appear in the first place.

The right side panel stays what it is — a summary beside the tab you are on. It
is not where you go to read a list.

### Goal

Opening an object that has tabular content opens a tab of its own showing that
content in the grid, like the data tab. Objects without tabular content open the
info view. The properties panel is not the destination for either.

### Approach

Decide by shape, from the descriptor the metadata layer already returns (CORE-01)
and the tabular section work from CORE-49:

- The object's content is a list of like-shaped records → open a tab whose body
  is the shared result grid (`sqlide/frontend/data_grid.py`), with the same
  sorting, resizing and copy-as the data tab has. The tab is titled for the
  object and its parent, and follows CORE-01's rule: opening it twice focuses
  the existing tab.
- The object is a single record — one index, one trigger, one sequence → open
  the existing info view with its summary and DDL.
- A folder node whose children are objects (CORE-01's group/folder behaviour)
  is the tabular case: it opens a grid of its children, and a row opens that
  child.

The definition/DDL for an object with one belongs in the opened tab, not only in
the panel — a table's index list should be readable and its individual index's
definition reachable from the same place.

Leave the panel alone otherwise: it still shows the active tab's object summary
(CORE-47), still has no grants for principals (CORE-53), and per-object surfaces
from CORE-50 stay as the panel's own internal behaviour.

### Acceptance criteria

- [x] Clicking Indexes (or any collection node) opens a tab with a grid, not a
      panel page.
- [x] The grid supports sorting, resizing and copy-as, as the data tab does.
- [x] A row in that grid opens the individual object's info view.
- [x] A single, non-tabular object opens the info view directly.
- [x] Opening the same collection twice focuses the existing tab.
- [x] Nothing about opening these objects forces the side panel open or changes
      which panel page is showing.
- [x] The classification comes from the descriptor/capability layer, with a
      documented fallback for object types that declare neither shape.

### Notes

- **Classification is the capability layer's** (`backend/db/objects.py`):
  `TABULAR_KINDS` ("category", "section") and `SCALAR_KINDS` (every
  single-record kind — table, view, index, column, trigger, …) answer
  `shape_of(kind)` with no connection open, and `grid_listing(kind,
  info)` turns the descriptor into the listing a tab should draw, or
  None for the info view.
- **Fallback**: a kind that declares neither shape — a new adapter's
  own kind, anything `_generic` describes — is decided by the
  descriptor it came back with: it opens as a grid only where its whole
  body is one tabular section, with no summary and no DDL that the grid
  would drop. Anything less certain opens the info view, which shows
  the listing as one of its sections, so an undeclared kind is never
  worse off than before.
- A table's section row is described by itself rather than out of a
  whole `table_properties` read: `objects.table_section` for the
  sections the plain connector fills, and
  `MetadataProvider.section_listing` for the provider-only ones
  (Permissions), which is what `describe(NodeRef(kind="section", …))`
  routes to.
- The tab is `ObjectInfoTab` still — it holds a `Gtk.Stack` of the
  info body and a full-height `_GridSection`, and shows whichever the
  descriptor turned out to be, so there is one tab type and one dedupe
  key (`tab_key`): opening Indexes twice focuses the open tab. Sorting,
  resizing and copy-as come from `ResultGrid` itself; a row opens the
  child's own info view.
- Titled for the object and its parent — "orders · indexes".
- The panel is untouched: opening a listing neither reveals it nor
  changes its page. The panel beside a listing tab points at the table
  the listing came out of. CORE-05's deep link into the panel is still
  reachable, as the section row's explicit "Open in Properties" menu
  item.
