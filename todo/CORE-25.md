## CORE-25 — Constraints and indexes in the designer

- **Status:** todo
- **Depends on:** CORE-23
- **From:** RS-02 (see `docs/table-creator-research.md`)

### Problem

The designer can express exactly one constraint: a primary key, via a
per-column checkbox (`frontend/table_designer.py:108`) that
`create_table_sql()` folds into one `PRIMARY KEY (…)` clause
(`backend/db/base.py:842-862`). There is no UNIQUE, no CHECK, no
FOREIGN KEY, no named constraint, and no index — so a table with a
foreign key cannot be created in the designer at all, and the indexes
surface that exists is explicitly read-only
(`frontend/indexes_tab.py:1-16`). Every comparable tool (DBeaver,
pgAdmin, Workbench, DataGrip) defines these in the same editor as the
columns.

### Goal

Define a table's constraints and indexes where you define its columns,
and see them in the same generated statement.

### Approach

- A view switcher in the designer's top bar: **Columns / Constraints /
  Indexes**, all three editing one `TableModel` and all three feeding
  the one preview, which stays visible in every view.
- Constraints view: a list of rows, each a kind (PRIMARY KEY, UNIQUE,
  CHECK, FOREIGN KEY), an optional name, the columns it covers, and
  kind-specific fields — check expression, or referenced
  table/columns plus ON DELETE / ON UPDATE actions with the referenced
  table's columns offered from the provider.
- Indexes view: name, columns (with direction), unique flag, and the
  method where the engine has one.
- The per-column PK checkbox stays and stays in sync with the
  constraints view — it is the fast path, not a second source of
  truth.
- Constraints render inline in `CREATE TABLE` where the dialect
  allows; indexes render as trailing `CREATE INDEX` statements, so
  the preview shows a script rather than one statement.

### Acceptance criteria

- [ ] A table with a composite primary key, a unique constraint, a
      check and a foreign key can be created entirely in the designer.
- [ ] Indexes declared in the designer are created with the table, and
      the preview dialog lists every statement it will run.
- [ ] Ticking the PK checkbox on a column shows up in the Constraints
      view and vice versa.
- [ ] Rendering of each constraint kind per dialect is covered by
      tests over the model, with no database connection.
