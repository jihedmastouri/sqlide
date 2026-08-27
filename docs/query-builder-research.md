---
title: Query Builder Research
description: What the visual query builder does today, what it should become, and the tickets that get it there.
order: 14
---

This is the write-up of RS-01. It is research, not implementation: it
records what `sqlide/frontend/query_builder.py` can and cannot do
today, what comparable tools chose, and a scoped v1 direction, and it
files the follow-up tickets (CORE-17 … CORE-22) that carry it out.

The one-line answer: **keep generation one-way (builder → SQL), and
make the builder itself round-trip by persisting its model, not by
parsing SQL back.** Everything else follows from that decision.

## What exists today

The builder is a single tab, `QueryBuilderTab`
(`sqlide/frontend/query_builder.py`, 623 lines), opened from the Tabs
menu (`sqlide/frontend/window.py:433`), the `<primary><alt>b`
keybinding (`sqlide/frontend/keymap.py:59`) and the sidebar's schema
context menus (`sqlide/frontend/sidebar.py:1235`, `:1263`).

It can:

- pick one base table from a flat dropdown, with `DISTINCT` and a
  `LIMIT` spin button (`query_builder.py:206-222`);
- add join lines — `INNER`/`LEFT`/`RIGHT` only
  (`query_builder.py:46`) — each a single `ON a = b` equality, with the
  columns prefilled from a foreign key when one connects the joined
  table to a table already in the query (`_relation_for`,
  `query_builder.py:399-415`);
- tick columns to project, defaulting to `*`
  (`_rebuild_column_checks`, `:426`);
- add filter and sort lines, reusing the table tab's `_FilterRow` /
  `_SortRow` widgets (`query_builder.py:463-509`);
- render SQL live into a read-only preview (`build_sql`, `:523-577`),
  run it into a `ResultGrid`, or hand the text to a fresh query console
  (`_open_in_console`, `:617`).

The whole catalog is loaded once up front on a worker thread
(`_load_catalog`, `:310-344`), so no interaction costs a round trip.

## Concrete shortcomings

1. **It bypasses the MetadataProvider.** `_load_catalog` calls
   `connector.list_tables()`, `list_columns()`, `list_relations()`
   directly (`query_builder.py:313-322`) rather than the provider
   abstraction added in CORE-02 (`sqlide/backend/db/metadata.py`).
   Consequences: no capability flags, no schemas, and no reuse of the
   per-engine catalog work PG-01/PG-02/MY-01/SQ-01 landed.

2. **Schema-blind.** `list_tables()` returns the connected database's
   tables unqualified (`sqlide/backend/db/base.py:368`); the
   schema-aware `list_tables_in()` (`base.py:389`) is never called, and
   `RelationInfo.schema` / `ref_schema` (`base.py:157-163`) are
   ignored. On PostgreSQL two same-named tables in different schemas
   collide in one dropdown and the generated SQL is unqualified.

3. **Views and materialized views are filtered out** —
   `if t.kind == "table"` (`query_builder.py:316-317`) — even though
   they are perfectly good SELECT sources.

4. **SQL generation lives in the widget.** `build_sql`
   (`query_builder.py:523`) reads GTK dropdown state directly, so
   there is no query model, nothing to persist, and nothing to unit
   test. Grep confirms no test touches the builder: `tests/` mentions
   it only as an `unused` callback
   (`tests/test_disconnect.py:65`, `tests/test_close_related_tabs.py:80`).

5. **Literals are interpolated, not parameterised.** Filter values go
   through `_sql_literal` (`sqlide/frontend/data_grid.py:1260-1272`)
   and are pasted into the statement, while the connector's own filter
   path composes a parametrised `WHERE` (`base.py:333-344`). A typed
   value in the builder is therefore quoted by guesswork — and the
   blob-literal comment at `data_grid.py:1268` documents that it is
   already wrong for PostgreSQL `bytea`.

6. **The expressible query is small.** No aliases, no self-joins (the
   same table twice is indistinguishable, since identity is the bare
   name — `_qualified_columns`, `:359`), no multi-condition `ON`, no
   `FULL`/`CROSS` join, no `GROUP BY`/`HAVING`, no aggregate or
   computed columns, no `OFFSET`, no `IN`/`BETWEEN`/`NOT` (the operator
   set is `FILTER_OPERATORS`, `base.py:272`), no filter grouping — the
   conjunctions fold strictly left-associatively
   (`query_builder.py:560-562`), so `a AND (b OR c)` cannot be
   expressed. No subqueries, no CTEs, no window functions.

7. **Nothing survives a restart.** `tab_state()`
   (`query_builder.py:187`) stores only `kind` and the base table name,
   and restore re-opens an empty builder on that table
   (`window.py:1730-1732`). Joins, checked columns, filters and sorts
   are lost.

8. **The preview is a dead end.** It is a read-only `Gtk.TextView`
   (`query_builder.py:261`): you cannot select-and-copy-edit in place,
   and `Open in Console` is a one-way door with no way back.

9. **Layout does not scale.** The controls sit in a scroller capped at
   420px (`query_builder.py:291`) with sections stacked vertically; ten
   joins and ten filters is unusable, and the columns `FlowBox`
   (`:234`) has no search, no per-table grouping and no select-all.

## What comparable tools do

Surveyed from their documented behaviour and UI; no attempt was made
to benchmark them.

**DBeaver — visual query builder.** A canvas of table boxes with FK
lines you draw joins on, plus a grid of column/alias/criteria rows
underneath. Good: joins are inferred from foreign keys and are
genuinely fast to assemble; the diagram is the mental model people
already have from ER views. Bad: it round-trips through a SQL parser,
so hand edits in the console frequently come back mangled or refuse to
re-open in the builder at all, and anything past a plain join
(subqueries, set operations) drops out of the visual view. The lesson
is that the parser is where the trust goes to die.

**DataGrid/DataGrip — no visual builder at all.** JetBrains bet
everything on completion, parameter hints, and "run this fragment"
instead. Good: nothing to un-learn, and the editor stays the single
source of truth. Bad: it is useless to someone who does not already
know SQL, and it makes multi-join exploration a typing exercise. Worth
noting sqlide already has the completion half of this bet (language
server integration, `docs/language-servers.md`).

**Metabase — the notebook editor.** A vertical stack of steps: data →
join → filter → summarize → sort → limit, each step a small form, with
the SQL viewable but not editable. Good: the step model reads
top-to-bottom like the query executes, aggregates are first-class
(most business questions are `GROUP BY`), and it is explicit that
converting to SQL is a *one-way* door — the UI says so before you do
it. Bad: strictly one-way, so a query that leaves the builder can
never return.

**pgAdmin — Query Tool graphical editor.** A canvas plus a tabbed
grid (Data Output / Explain / Query History). Good: living in the same
tool as the SQL editor, sharing one result grid and one history.
Bad: PostgreSQL-only assumptions everywhere, and the same
generate-then-lose-it problem.

The consistent pattern: **the tools that try to parse SQL back into a
builder are the ones users complain about; the tools that are honest
about one-way generation are the ones people keep using.** Metabase's
model — a persisted structured query that renders to SQL, plus an
explicit one-way escape hatch — is the closest fit for sqlide.

## Answers to RS-01's questions

**What is it for?** Exploration and SQL scaffolding, in that order. The
person reaching for it either does not want to write the join by hand
or does not know the schema yet. Both are served by "get me a correct
statement fast", not by "be a complete SQL front end".

**Round-tripping.** One-way. builder → SQL only. We do not parse SQL
back. Instead the *builder model* is persisted (in `TabState`), so
closing and reopening a builder tab, or restarting the app, restores
the query as you built it. "Open in Console" stays the explicit
one-way door, and the UI should say so. Rationale: a round-trip parser
is a per-dialect SQL parser — a large, permanently-behind dependency
that DBeaver's own experience shows still frustrates people. sqlide's
whole pitch is being small (`docs/index.md`).

**Joins.** Inferred from foreign keys *as a suggestion*, always
overridable, and always editable as several `ON` conditions. Draw the
line at: multi-table, self-joins with aliases, all five join kinds,
composite keys. Out of scope for v1: subqueries, CTEs, window
functions, set operations — those are what `Open in Console` is for.

**Where does it live?** Its own tab, as today. It has a result grid, a
history entry and a connection of its own, and it is opened from the
sidebar on a specific table; a panel bolted to a console would have to
share or fight for all four. But it should adopt the console's
furniture — same results panel, same explain/plan views (CORE-16) —
and the console should gain a "Build a query like this" affordance
only in the sense of *starting* a builder, never of importing SQL.

## Proposed direction — scoped v1

A model-first builder:

1. A `QueryModel` dataclass tree in `sqlide/backend/db/` — sources,
   joins, projections, filter tree, group/having, ordering, limit —
   with a renderer that turns it into dialect-correct SQL via the
   connector's `quote_ident` and a typed literal formatter. The widget
   edits the model; SQL is a pure function of it. This is what makes
   the thing testable, persistable, and re-usable by anything else
   (the MCP server, for one).
2. The model is sourced from the `MetadataProvider`, so schemas,
   views, and capability flags come for free and each engine's catalog
   work is reused rather than re-implemented.
3. The model is serialised into `TabState`, so builder tabs restore
   fully.
4. The UI grows in independent slices — joins, aggregates, filter
   grouping, layout — each shippable on its own once (1)-(3) exist.

Explicit non-goals for v1: parsing SQL into the model; subqueries,
CTEs, window functions and set operations; `INSERT`/`UPDATE`/`DELETE`
building; a canvas/diagram editor (the relation graph already exists
and could later *launch* a builder, which is a much cheaper win).

## Follow-up tickets

| ID | Title | Depends on |
|---|---|---|
| CORE-17 | Query model and SQL renderer in the backend | — |
| CORE-18 | Builder reads the MetadataProvider (schemas, views, capabilities) | CORE-17 |
| CORE-19 | Persist the builder's query model in the workspace | CORE-17 |
| CORE-20 | Joins: aliases, self-joins, multi-condition ON, all join kinds | CORE-17, CORE-18 |
| CORE-21 | Aggregates: GROUP BY, HAVING, computed and aliased columns | CORE-17 |
| CORE-22 | Builder layout: grouped filters, column search, room to grow | CORE-17 |

CORE-17 is the keystone; the other five are independent of each other
once it lands.
