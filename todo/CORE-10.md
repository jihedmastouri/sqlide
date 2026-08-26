## CORE-10 — Permission editor: split screen tree + permission grid

- **Status:** todo
- **Depends on:** CORE-02

### Goal

Managing a user's/role's permissions should not mean hand-writing GRANT
statements.

### Layout

Split screen. Left: a tree of objects (the same hierarchy as the sidebar, scoped
to the selected principal's reachable objects). Right: the permissions that
principal holds on the selected object, as toggleable checkboxes.

### Acceptance criteria

- [ ] A principal (user/role) is selected first; the split screen is scoped to it.
- [ ] Selecting any object in the left tree loads its permission set on the right,
      with the engine-correct privilege list (PG: SELECT/INSERT/UPDATE/DELETE/
      TRUNCATE/REFERENCES/TRIGGER, plus USAGE/CREATE on schemas, EXECUTE on
      functions, etc.; MySQL: its own grant list at global/db/table/column level).
- [ ] Privileges inherited from a role are shown distinctly from direct grants and
      are not silently editable as if direct.
- [ ] Grant-option ("WITH GRANT OPTION") is representable.
- [ ] Changes accumulate across multiple objects without saving each time; changed
      objects are marked in the tree.
- [ ] **Save** opens a confirmation dialog listing every pending change grouped by
      object, together with the exact SQL that will run.
- [ ] Statements run in a transaction where the engine allows it; on failure
      nothing is half-applied and the error names the failing statement.
- [ ] A Cancel/Revert discards pending changes.
- [ ] For SQLite the feature is hidden via `capabilities()`.


