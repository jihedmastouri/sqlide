## CORE-48 — Keyword case for SQL autocompletion

- **Status:** todo
- **Depends on:** CORE-13

### Problem

Completion inserts keywords in whatever case the completion source happens to
hold, which fights with people who type `select` and expect `SELECT`.

### Goal

Keyword completions are inserted in a case the user chose.

### Approach

Add a `sql.keyword_case` setting with three values:

- `upper` (default) — always insert `SELECT`
- `lower` — always insert `select`
- `follow` — match the case of the prefix the user has typed (all-lower prefix →
  lower, any leading capital → upper, mixed → upper)

Apply it to keywords only. Identifiers — table, column, schema names — always keep
the case the catalog reports, since they can be case-sensitive.

### Acceptance criteria

- [ ] The setting exists in the TOML config and in Preferences, defaulting to
      `upper`.
- [ ] All three modes behave as described, including `follow` with an empty prefix
      (falls back to `upper`).
- [ ] Identifier completions are unaffected by the setting.
- [ ] Changing the setting takes effect without a restart.
- [ ] Tests cover each mode, plus the empty-prefix and mixed-case cases.
