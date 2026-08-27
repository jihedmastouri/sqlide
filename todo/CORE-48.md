## CORE-48 — Keyword case for SQL autocompletion

- **Status:** done
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

- [x] The setting exists in the TOML config and in Preferences, defaulting to
      `upper`.
- [x] All three modes behave as described, including `follow` with an empty prefix
      (falls back to `upper`).
- [x] Identifier completions are unaffected by the setting.
- [x] Changing the setting takes effect without a restart.
- [x] Tests cover each mode, plus the empty-prefix and mixed-case cases.

### Notes

settings.toml is flat, so the key is spelled `sql_keyword_case`
(documented in docs/configuration.md) rather than `sql.keyword_case`.
The mode is applied in one place — `backend.settings.
apply_keyword_case()` — which the completion controller runs over every
suggestion whose detail is `keyword`, so the built-in word list and a
language server's keyword items agree and identifier suggestions are
left in the case the catalog reported. The setting is read per request,
so a change reaches the next popup without a restart.
