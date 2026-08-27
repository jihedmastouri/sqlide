## CORE-49 — Render tabular object information as a real table

- **Status:** done
- **Depends on:** CORE-01

### Problem

Opening a folder or a collection-shaped object (indexes, constraints, grants,
triggers, columns) renders a list or a stack of key/value blocks even when the
data is plainly rows and columns. It cannot be sorted, sized or copied the way
the result grid can.

### Goal

Anything that is a list of like-shaped records is shown in the same grid widget
the app already uses for results.

### Approach

Extend the descriptor from CORE-01 so a detail section can declare itself
tabular with typed columns, and render those through the shared grid component
rather than a bespoke list. Keep the scalar summary block for genuinely
single-record information.

Reuse what the result grid already gives for free: column sorting, resizing,
copy-as (CSV/JSON/Markdown), and the value display rules.

### Acceptance criteria

- [x] Index, constraint, trigger, column, grant and permission listings render in
      the grid.
- [x] Rows still open the child object's info view (CORE-01's behaviour is kept).
- [x] Sorting and copy-as work in those grids.
- [x] A section with a single record still renders as a key/value block, not a
      one-row table.
- [x] Sections that are not tabular are untouched.
