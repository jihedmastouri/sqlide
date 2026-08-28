## CORE-21 — Aggregates: GROUP BY, HAVING, computed and aliased columns

- **Status:** Done
- **Depends on:** CORE-17
- **From:** RS-01 (see `docs/query-builder-research.md`)

### Problem

The builder projects bare columns only — a checklist of names, or `*`
(`frontend/query_builder.py:426-446`, `533-541`). There is no way to
count, sum or average anything, no `GROUP BY`, no `HAVING`, and no
column alias. RS-01's survey found this is the single most common thing
a visual builder is reached for (Metabase's "Summarize" step is the
centre of its notebook editor), and it is the case where hand-writing
the SQL is most tedious.

### Goal

Answer "how many per X" without leaving the builder.

### Approach

- Extend the projection list from names to entries: source column *or*
  aggregate (`COUNT`, `COUNT DISTINCT`, `SUM`, `AVG`, `MIN`, `MAX`),
  plus an optional alias.
- Add a Group by section (an ordered column list) and a Having section
  reusing the filter row widget but over the aggregate entries.
- Derive as much as possible: when an aggregate is added, offer to
  group by the remaining plain projections rather than silently
  producing a statement the engine rejects.
- Offer a free-text expression projection as the escape hatch, passed
  through unchanged and clearly marked as unvalidated.
- Aggregate result columns must remain sortable — ordering by an alias
  where the dialect allows it, by the expression where it does not.

### Acceptance criteria

- [x] An aggregate projection with an alias renders correctly and runs.
- [x] `GROUP BY` and `HAVING` sections exist and render in the right
      order relative to `WHERE` and `ORDER BY`.
- [x] Adding an aggregate beside a plain column prompts for or applies
      the grouping instead of generating an invalid statement.
- [x] Renderer cases for aggregates, grouping and having are covered in
      `tests/test_query_model.py`.
