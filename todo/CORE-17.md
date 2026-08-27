## CORE-17 — Query model and SQL renderer in the backend

- **Status:** todo
- **Depends on:** —
- **Blocks:** CORE-18, CORE-19, CORE-20, CORE-21, CORE-22
- **From:** RS-01 (see `docs/query-builder-research.md`)

### Problem

The query builder generates SQL straight out of GTK widget state:
`QueryBuilderTab.build_sql()` (`frontend/query_builder.py:523`) reads
dropdowns and check buttons as it writes the statement. So there is no
query to save, nothing to unit test (no test in `tests/` exercises the
builder), and no way for anything but that one widget to produce the
same SQL. Filter values are pasted in through `_sql_literal`
(`frontend/data_grid.py:1260`) rather than parameterised the way the
connector's own filter path is (`backend/db/base.py:333-344`).

### Goal

A serialisable query model and a dialect-aware renderer in the backend.
The widget edits the model; the SQL is a pure function of it.

### Approach

- New module `backend/db/query_model.py`: frozen dataclasses for
  sources (table ref + optional alias), joins (kind, source, list of ON
  conditions), projections (column or expression + optional alias),
  a filter *tree* (leaf conditions and AND/OR groups, so `a AND (b OR c)`
  is expressible), grouping, having, ordering, limit and offset.
- `render(model, *, quote, dialect) -> tuple[str, list[Any]]` returning
  the statement and its parameters. Identifiers go through the
  connector's `quote_ident`; values become placeholders, not literals.
- A display-only variant that inlines literals for the read-only
  preview, clearly separate from the executed form.
- Port `build_sql()` to construct a model and render it, so behaviour
  is unchanged and the widget shrinks.

### Acceptance criteria

- [ ] `backend/db/query_model.py` exists with the model and renderer,
      and imports nothing from `frontend/`.
- [ ] The builder renders through it; `build_sql()` no longer contains
      SQL string assembly.
- [ ] Executed queries pass filter values as parameters, not as
      interpolated literals.
- [ ] `tests/test_query_model.py` covers: projections vs `*`, DISTINCT,
      each join kind, nested AND/OR filter groups, ordering, limit, and
      quoting for each dialect — with no database connection required.
