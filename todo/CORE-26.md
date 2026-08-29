## CORE-26 — Alter mode: one designer for create and alter, over a diff

- **Status:** done
- **Depends on:** CORE-23, CORE-24
- **From:** RS-02 (see `docs/table-creator-research.md`)

### Problem

Creating a table and changing one are separate features with different
mental models. The designer builds a `CREATE`
(`frontend/table_designer.py`); changing an existing table means
`DefinitionTab` (`frontend/definition_tab.py`), which either edits raw
DDL text and diffs the column *set* into ADD/DROP COLUMN
(`_alter_statements`, `definition_tab.py:351`) or edits a four-column
grid whose type/nullability edits become an in-place ALTER or a full
table rebuild (`backend/db/base.py:920`). Neither knows about the type
list the designer has. Worse, nothing classifies risk: the warnings
are prose captions (`definition_tab.py:47-59`), so a rename and a
`NOT NULL` add on a populated table look identical in the preview.

### Goal

One designer. Open it on an existing table and it loads that table's
model; every edit is a diff, and the plan says how dangerous it is
before you run it.

### Approach

- `TableModel.from_provider(ref)` loading an existing table's columns,
  constraints, indexes and options.
- The designer's action becomes **Apply** rather than **Create**, and
  it renders `plan(current, target, connector)` (CORE-23). `current`
  is `None` for a new table, so create is the same code path.
- The preview dialog groups statements by classification and leads
  with the dangerous ones: dropping a column or constraint is
  `destructive`; a type narrowing or a `NOT NULL` add is `may_fail`; a
  SQLite rebuild is `rewrite`.
- Where the check is cheap, offer a pre-flight query before applying
  (`SELECT count(*) … WHERE col IS NULL` for a `NOT NULL` add,
  a distinct-count for a new UNIQUE) and report the row count that
  would block it.
- SQLite keeps the existing rebuild sequence
  (`sqlite/connector.py:541`) — the planner chooses it by asking the
  connector, not by branching in the frontend.
- Retire `DefinitionTab`'s Table (column-grid) mode in favour of this;
  keep its editable-DDL text mode as the escape hatch.

### Acceptance criteria

- [x] "Edit table" from the sidebar opens the designer populated from
      the catalog, and applying with no edits produces an empty plan.
- [x] Adding a column, renaming one, changing a type and dropping one
      each produce the right statements per engine, covered by tests
      over the planner.
- [x] Dropping a column or a constraint is labelled destructive in the
      preview dialog and is not the default focus.
- [x] A `NOT NULL` add against a table with nulls reports the
      offending row count instead of failing on the server.
- [x] On SQLite the plan is the rebuild sequence, wrapped as
      `wrap_rebuild()` requires.
