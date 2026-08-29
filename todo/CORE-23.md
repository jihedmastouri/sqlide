## CORE-23 — Table model and DDL renderer in the backend

- **Status:** done
- **Depends on:** —
- **Blocks:** CORE-24, CORE-25, CORE-26, CORE-27, CORE-28, CORE-29
- **From:** RS-02 (see `docs/table-creator-research.md`)

### Problem

The table designer has no model of a table. `_build_sql()`
(`frontend/table_designer.py:377`) reads GTK rows and hands names,
`ColumnInfo`s and a `defaults` dict to `Connector.create_table_sql()`
(`backend/db/base.py:833`). So there is nothing to persist, nothing to
diff against an existing table, and nothing to unit test — no test in
`tests/` exercises the tab; `tests/test_ddl.py:105` tests the
connector method instead. `ColumnInfo` (`base.py:25`) carries only
name/type/is_pk/nullable, which is why the default has to travel
beside it in a side dict and why comments, identity, generated
expressions and collations have nowhere to live.

### Goal

A serialisable table model and a dialect-aware DDL renderer in the
backend. The designer edits the model; the DDL is a pure function of
it.

### Approach

- New module `backend/db/table_model.py`: frozen dataclasses for
  `TableModel` (schema, name, columns, constraints, indexes,
  table-level options), `ColumnModel` (name, type, nullable, default
  as a tagged literal/expression/none, comment, collation, identity or
  auto-increment, generated expression, per-engine options) and
  `ConstraintModel` / `IndexModel` (kind, name, columns, referenced
  table/columns, on-delete/on-update, check expression, uniqueness,
  method).
- `render_create(model, connector) -> str` building the statement
  through the connector's `quote_ident`, superseding
  `create_table_sql()` (which stays as a thin shim over the model so
  nothing else breaks).
- `plan(current, target, connector) -> list[Statement]` where
  `current` may be `None` (a create). Each `Statement` carries its SQL
  and a classification: `safe`, `rewrite`, `may_fail` (a constraint
  that existing rows can violate) or `destructive`.
- `to_json()` / `from_json()` with a version field, for CORE-28.
- Port the designer to construct a model and render it, so behaviour
  is unchanged and the widget shrinks.

### Acceptance criteria

- [x] `backend/db/table_model.py` exists with the model, renderer and
      planner, and imports nothing from `frontend/`.
- [x] The designer renders through it; `_build_sql()` contains no SQL
      string assembly.
- [x] `Connector.create_table_sql()` still produces byte-identical
      output for the cases in `tests/test_ddl.py`.
- [x] `tests/test_table_model.py` covers: create rendering per
      dialect, quoting, composite primary keys, defaults as literal vs
      expression, JSON round-trip, and a plan for each classification
      — with no database connection required.
