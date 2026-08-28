## CORE-20 — Joins: aliases, self-joins, multi-condition ON, all join kinds

- **Status:** done
- **Depends on:** CORE-17, CORE-18
- **From:** RS-01 (see `docs/query-builder-research.md`)

### Problem

A join today is one row: a kind out of `INNER`/`LEFT`/`RIGHT`
(`frontend/query_builder.py:46`) and a single `ON a = b`
(`build_sql`, `:546-551`). That excludes composite foreign keys, any
`ON` with more than one condition, `FULL` and `CROSS` joins, and — because
a column's identity is the bare `table.column` string
(`_qualified_columns`, `:359`) — every self-join, since the same table
twice is indistinguishable.

### Goal

Express the joins people actually have: composite keys, the same table
more than once, and more than one condition per join.

### Approach

- Give every source in the model an alias (auto-generated, editable),
  and key columns by alias rather than table name.
- Turn each join row into a small group: kind, source, alias, and one
  or more ON conditions with an operator (not just `=`), add/remove per
  condition.
- Extend the kinds to `INNER`, `LEFT`, `RIGHT`, `FULL` and `CROSS`,
  hiding the ones an engine lacks (SQLite gained `RIGHT`/`FULL` only in
  3.39) — a capability question, so declare a flag rather than
  branching on engine in the widget.
- Foreign-key prefill (`_relation_for`, `:399`) fills *all* the columns
  of a composite key as separate conditions, and stays a suggestion the
  user can overwrite (the existing `on_touched` behaviour, `:100`).

### Acceptance criteria

- [x] The same table can be joined to itself, with distinct aliases,
      and the generated SQL is valid.
- [x] A join can carry two or more ON conditions; a composite foreign
      key prefills all of them.
- [x] `FULL` and `CROSS` are offered where the engine supports them and
      absent where it does not.
- [x] Filters, sorts and the column checklist all address columns by
      alias, so a self-joined table's two sides never merge.
