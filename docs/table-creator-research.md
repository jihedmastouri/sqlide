---
title: Table Creator Research
description: What the table designer does today, what it should become, and the tickets that get it there.
order: 15
---

This is the write-up of RS-02. It is research, not implementation: it
records what `sqlide/frontend/table_designer.py` can and cannot do
today, what comparable tools chose, and a scoped direction, and it
files the follow-up tickets (CORE-23 … CORE-29) that carry it out.

The one-line answer: **one designer, two modes, over a single
serialisable `TableModel` in the backend — create is the diff from an
empty table, alter is the diff from a loaded one — with the model
sourced from the `MetadataProvider` and rendered to DDL per dialect.**
This is deliberately the same shape RS-01 chose for the query builder
(`docs/query-builder-research.md`): a backend model, a pure renderer,
a widget that only edits the model.

## What exists today

`TableDesignerTab` (`sqlide/frontend/table_designer.py`, 439 lines) is
opened from the sidebar's **New ▸ Table**
(`sqlide/frontend/window.py:2407` → `:2420`) and documented in
`docs/ddl.md`. It is the only create flow with a form; every other
object kind gets a commented template in a query console.

It can:

- name a table and add/remove/reorder column rows
  (`table_designer.py:319-351`), order being table order;
- pick a type from the adapter's own list — `column_type_specs()`
  (`backend/db/base.py:804`, overridden per engine at
  `postgres/connector.py:1597`, `mysql/connector.py:812`,
  `sqlite/connector.py:511`) — with the arguments that type takes
  growing exactly the entries it needs, prefilled from `TypeSpec`
  (`base.py:127`), plus a `Custom…` free-text escape
  (`table_designer.py:137-179`);
- set primary key, NOT NULL (forced on for a PK,
  `table_designer.py:181-187`) and a free-text `DEFAULT` expression;
- show a live preview, rebuilt on every keystroke through the
  adapter's `create_table_sql()` (`base.py:833`), with an explicit
  reason when the form is incomplete (`_problem`,
  `table_designer.py:355`) rather than a generic error;
- copy the statement, and run it only after an `UpdatePreviewDialog`
  (`table_designer.py:409-422`), then reload the sidebar and open the
  new table.

The separate ALTER story lives in `DefinitionTab`
(`sqlide/frontend/definition_tab.py`): an editable CREATE statement
whose save path either diffs the *column set* into ADD/DROP COLUMN
(`_alter_statements`, `definition_tab.py:351`) or, on SQLite, runs the
rename/create/copy/drop rebuild (`base.py:920`).

## Concrete shortcomings

1. **The model is the widget.** `_build_sql()`
   (`table_designer.py:377`) reads GTK state and hands it to
   `create_table_sql()`. There is no table object, so nothing to
   persist, nothing to diff, and nothing to unit test: grep finds no
   test that touches the designer at all — `tests/test_ddl.py:105-149`
   tests `create_table_sql()` directly, never the tab.

2. **`ColumnInfo` is too thin to describe a column.** It carries
   name/type/is_pk/nullable and nothing else (`base.py:25`), so the
   default has to travel beside it as a `dict[str, str]` keyed by name
   (`table_designer.py:383`, `base.py:833`). There is no place for a
   comment, a collation, an identity/auto-increment flag, a generated
   expression, or a UNIQUE/CHECK marker, which is why none of those
   are offered.

3. **No constraints beyond the primary key.** `create_table_sql()`
   emits columns and one `PRIMARY KEY (…)` clause (`base.py:842-862`)
   — no UNIQUE, no CHECK, no FOREIGN KEY, no named constraints, no
   composite anything except the PK. A table with a foreign key cannot
   be created in the designer at all; you have to leave for a console.

4. **No indexes.** Creating a table and its indexes is one task and
   two tools today (`IndexesTab` is explicitly read-only,
   `frontend/indexes_tab.py:1-16`).

5. **Schema-blind.** The designer takes a bare table name and quotes
   it (`base.py:859`). PG-01 made schemas a level of their own, but
   the designer has no schema field and no idea which schema the
   sidebar node it was launched from belongs to; on PostgreSQL the
   table lands in whatever `search_path` says.

6. **It bypasses the MetadataProvider.** Its loader calls
   `connector.column_type_specs()` straight off the connector
   (`table_designer.py:301-311`) rather than going through
   `backend/db/metadata.py`, so no capability flags reach it and none
   of the per-engine catalog work is reused. Same defect RS-01 found
   in the query builder (CORE-18).

7. **No engine-specific table options.** No dialect overrides
   `create_table_sql()` — the base implementation is what every engine
   gets. So: no MySQL storage engine, charset/collation or
   `AUTO_INCREMENT` seed; no PostgreSQL `GENERATED … AS IDENTITY`
   (only the `serial` type entry, `postgres/connector.py:1601`),
   generated columns, `UNLOGGED` or partitioning; no SQLite
   `WITHOUT ROWID` or `STRICT`. RS-02's brief calls these out by name
   and today the answer to all of them is "write it by hand".

8. **`DEFAULT` is free text, inserted verbatim.** The tooltip says so
   (`table_designer.py:119-121`) and `base.py:846-847` pastes it in.
   Typing `hello` for a text column produces invalid DDL and the user
   finds out from the server. There is no type-aware default picker
   (literal / expression / `NULL` / `CURRENT_TIMESTAMP`).

9. **Type arguments are capped at two.** `_MAX_PARAMS = 2`
   (`table_designer.py:41`) is fine for `DECIMAL(p, s)` but forecloses
   anything wider, and array/domain types have to go through
   `Custom…`.

10. **Create and alter are separate features with different mental
    models.** The designer builds a `CREATE`; `DefinitionTab` edits
    text or a column grid and diffs it, with two different captions
    warning you about what will be lost
    (`definition_tab.py:47-59`). A user who wants "add a column to
    this table" cannot use the tool that knows about types.

11. **Nothing survives.** `tab_state()` returns `None`
    (`table_designer.py:314`) — the tab is session-only, so a
    half-designed 30-column table dies with the window, and there is
    no way to save a design, share it, or replay it on another
    connection.

12. **No copy-structure and no templates.** Creating a table like an
    existing one means reading its DDL in one tab and retyping it in
    another.

13. **Destructive changes are undifferentiated.** The alter paths
    warn in prose (`_REBUILD_CAPTION`, `_ALTER_CAPTION`) but nothing
    classifies a change as safe, lossy or blocking: adding a NOT NULL
    column to a populated table and renaming one look the same in the
    preview dialog.

## What comparable tools do

Surveyed from their documented behaviour and UI; no benchmarking.

**DBeaver — the table editor.** One editor with tabs (Columns, Keys,
Foreign Keys, Indexes, Triggers, DDL) that serves both a new table and
an existing one; edits accumulate as pending changes and a **Persist**
button shows the exact DDL script before it runs. Good: create and
alter are genuinely one surface, the DDL tab is always the truth, and
the script is reviewable and copyable. Bad: the pending-change model
is easy to lose track of across tabs, and per-engine options are
scattered into a properties grid of raw key/value pairs.

**pgAdmin — the CREATE Table dialog.** A modal with General / Columns
/ Constraints / Advanced / Partitions / Security tabs and, crucially,
a live **SQL** tab. Good: engine options are first class and grouped,
constraints are defined in the same dialog as columns, and the SQL tab
makes the modal honest. Bad: PostgreSQL-only by construction, and a
modal is a bad home for a task you want to leave and come back to.

**MySQL Workbench — the table editor.** A grid of columns with flag
checkboxes (PK, NN, UQ, B, UN, ZF, AI, G) plus tabbed Indexes,
Foreign Keys, Triggers, Partitioning and Options panes, and an
**Apply** step that shows the generated script. Good: the flag grid is
extremely fast for someone who knows the abbreviations, and the
alter-vs-create distinction is invisible because it is the same
editor. Bad: the abbreviations are unreadable to newcomers, and it
leans on MySQL specifics everywhere.

**DataGrip — the Modify Table dialog.** One dialog for create and
alter, with a preview of the DDL it will run and an explicit
"execute / copy to clipboard" choice. Good: the preview is the
contract, and it is engine-aware without being engine-specific in the
UI. Bad: everything advanced still ends in a hand-written statement.

The consistent pattern across all four: **one editor for create and
alter, tabs for constraints/indexes/options rather than one long form,
and a live generated-DDL view that is always visible and always what
runs.** sqlide already has the last of those three; it has neither of
the first two.

## Answers to RS-02's questions

**Create vs alter: one feature or two?** One UI, two modes, over one
model. A `TableModel` describes a table; the designer always renders
`plan(current, target)`. For a new table `current` is `None` and the
plan is one `CREATE TABLE`; for an existing table `current` is loaded
from the provider and the plan is the migration. This is the answer
that makes both cheap, and it retires `DefinitionTab`'s column-grid
mode in favour of a surface that knows about types. The editable-DDL
text mode stays — it is the escape hatch, exactly like the query
builder's *Open in Console*.

**Previewing and confirming DDL.** Keep what already works: a live
preview pane rebuilt from the model on every edit, and an
`UpdatePreviewDialog` before anything runs. Add a *classification* per
statement — safe / rewrites the table / may fail on existing data /
loses data — computed from the diff, so the dialog can lead with what
is dangerous rather than trusting the user to read SQL carefully.
Where the engine can check cheaply (a `NOT NULL` add against a
`COUNT(*) WHERE col IS NULL`), offer the pre-flight check.

**Per-engine types and options.** Both through the
`MetadataProvider`. Types already have `TypeSpec`, which is nearly the
right shape — it needs to be reachable via the provider and to lose
the two-argument cap. Options need a new declarative descriptor
(name, label, kind, choices, default, note, scope: table or column)
that each provider returns, so the designer renders an options group
it does not have to understand. That keeps PG identity/partitioning,
MySQL engine/charset, and SQLite `WITHOUT ROWID`/`STRICT` out of the
frontend entirely.

**Constraints, indexes and FKs: inline or separate?** In the same
designer, in tabs beside Columns — that is what every comparable tool
converged on, and a foreign key is part of designing a table, not a
follow-up chore. They are part of the same `TableModel`, so the plan
covers them and the diff can tell an added index from a dropped one.

**Templates and copy-structure.** Worth having and cheap once the
model exists: "new table like this one" is `TableModel.from_provider()`
plus a rename, and a saved template is the serialised model. Both fall
out of CORE-23 and CORE-28 nearly for free.

## Proposed direction — scoped v1

1. `backend/db/table_model.py`: frozen dataclasses for a table —
   schema, name, columns (with default, comment, identity, generated,
   collation, per-engine options), constraints (PK/UNIQUE/CHECK/FK,
   named, composite), indexes, and a table-level options map — plus
   `render_create(model, connector)` and `plan(current, target,
   connector)` returning classified statements. No imports from
   `frontend/`.
2. The designer edits that model and nothing else; SQL is a pure
   function of it.
3. The model is sourced from and loaded through the
   `MetadataProvider`, so schemas, capability flags and per-engine
   catalog work are reused.
4. Engine differences — types, table/column options, what an ALTER
   can do in place — are declared by the provider, never branched on
   in the frontend.
5. The model serialises into `TabState`, so a design survives a
   restart and can seed a template or a copy-structure.

Explicit non-goals for v1: parsing arbitrary DDL back into the model
(the text mode stays the escape hatch, one-way, exactly as RS-01
decided for SQL); triggers, partitioned-table children, table
inheritance, and full migration-file generation; editing views or
materialized views through the designer.

## Follow-up tickets

| ID | Title | Depends on |
|---|---|---|
| CORE-23 | Table model and DDL renderer in the backend | — |
| CORE-24 | Designer reads the MetadataProvider (schemas, types, capabilities) | CORE-02, CORE-23 |
| CORE-25 | Constraints and indexes in the designer | CORE-23 |
| CORE-26 | Alter mode: one designer for create and alter, over a diff | CORE-23, CORE-24 |
| CORE-27 | Engine-specific table and column options | CORE-23, CORE-24 |
| CORE-28 | Persist the designer's table model in the workspace | CORE-23 |
| CORE-29 | Copy structure from an existing table, and saved templates | CORE-23, CORE-28 |

CORE-23 is the keystone; the rest are independent of each other once
it lands (CORE-26 and CORE-27 also want CORE-24's provider wiring).
