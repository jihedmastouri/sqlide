## CORE-03 — Sidebar search mode

- **Status:** todo
- **Depends on:** —

### Problem

The sidebar toolbar keeps showing all its buttons while searching, wasting the
row, and search can't be scoped.

### Goal

Clicking search in the left sidebar turns that toolbar row into a search row.

### Acceptance criteria

- [ ] Clicking the search button hides the other buttons in that row and shows a
      search input, an **Exit** control and a **Filter** control.
- [ ] Filter lets the user scope results by object type (connections, databases,
      schemas where applicable, tables, views, indexes, functions, columns) —
      multi-select, with an "All" default.
- [ ] Exit (and `Esc`) restores the normal toolbar, clears the query and restores
      the previous tree expansion state.
- [ ] Results are filtered in-tree, showing matching nodes with their ancestors,
      match text highlighted.
- [ ] Search is debounced and does not block the UI on large trees.
- [ ] The chosen filter scope persists for the session.
