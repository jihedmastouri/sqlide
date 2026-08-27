## CORE-43 — Foreign-key navigation in the grid

- **Status:** todo
- **From:** RS-04 (see `docs/dbeaver-comparison.md`)

### Problem

The relation data is already loaded and already modelled:
`Connector.list_relations()` and `list_references()` return
`RelationInfo` (`backend/db/base.py`), the ER diagram draws edges from
them (`frontend/relation_graph.py`) and the query builder prefills
joins from them (`frontend/query_builder.py`). The grid ignores all of
it. Sitting on `orders.customer_id = 4812`, the only way to see that
customer is to open a console and write the SELECT — the move
everybody makes twenty times a day in DBeaver with one click.

### Goal

From a cell in a foreign-key column, open the referenced row; from a
row, open the rows in another table that reference it.

### Approach

- The grid asks the tab's connector for the table's outgoing
  (`list_constraints` / `list_relations`) and incoming
  (`list_references`) relations once when the table loads, cached by
  CORE-41.
- Cell context menu grows two entries, shown only when the column
  participates in a relation:
  - **Go to `customers`** — opens a table tab on the referenced table
    with a filter pinned to the referenced key columns equal to this
    row's values.
  - **References ▸** a submenu of the tables that point at this one,
    each opening a filtered tab the same way.
- Composite keys are the normal case, not an afterthought: the filter
  carries one condition per key column pair, built through the
  existing `FilterCondition` list so it renders in the filter panel
  and the user can see and edit it.
- A NULL foreign key offers nothing rather than opening an empty tab.
- The opened tab's filter is a normal saveable filter — nothing new to
  persist.
- Foreign-key columns get a subtle marker in the header, so the
  navigation is discoverable rather than hidden in a menu.

### Acceptance criteria

- [ ] Outgoing and incoming relations both offered, from the catalog,
      with no engine branching in the frontend.
- [ ] Composite foreign keys produce one `FilterCondition` per column
      pair, visible in the target tab's filter panel.
- [ ] Cross-schema relations resolve through the provider's
      `qualified_name`, so a PostgreSQL FK into another schema opens
      the right table.
- [ ] A NULL value offers no navigation entry.
- [ ] SQLite tables without declared foreign keys simply show no
      entries — no error, no empty submenu.
- [ ] Relation lookups are cached per table load, not per right-click.
