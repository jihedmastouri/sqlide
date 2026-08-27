## CORE-22 — Builder layout: grouped filters, column search, room to grow

- **Status:** todo
- **Depends on:** CORE-17
- **From:** RS-01 (see `docs/query-builder-research.md`)

### Problem

Every builder control is stacked into one vertical box inside a
scroller capped at 420px (`frontend/query_builder.py:288-293`). With
several joins and several filters, the sections push each other out of
view and the SQL preview — the thing the user is checking — is the
first casualty. The column checklist is a flat `FlowBox`
(`:234-240`) with no search, no grouping by table and no select-all,
which is unusable on a wide table. Filters fold strictly
left-associatively (`:560-562`), so `a AND (b OR c)` cannot be
expressed at all. And the preview is a read-only `TextView` (`:261`):
`Open in Console` (`:617`) is the only way out, with no indication that
it is a one-way door.

### Goal

A layout that survives a real query, and a filter editor that can
express grouping.

### Approach

- Restructure into named, collapsible sections (or a step list in the
  Metabase spirit — sources, joins, columns, filters, summarise, sort),
  with the SQL preview pinned so it stays visible.
- Group the column checklist per source, with a search entry and
  select-all / select-none per group.
- Make the filter editor a tree: conditions inside AND/OR groups,
  groups nestable one level at minimum, rendering to the CORE-17 filter
  tree.
- Keep the preview read-only but make it selectable and copyable, and
  label `Open in Console` as the one-way handoff it is — the builder
  does not read SQL back (RS-01).

### Acceptance criteria

- [ ] Five joins and eight filters are all reachable without the SQL
      preview leaving the viewport.
- [ ] The column list can be searched and toggled per source table.
- [ ] `a AND (b OR c)` is expressible and renders with the right
      parentheses.
- [ ] The preview text can be selected and copied; the console handoff
      carries a tooltip or label saying edits there do not come back.
