## CORE-44 — Format SQL in the editor

- **Status:** Done
- **From:** RS-04 (see `docs/dbeaver-comparison.md`)

### Problem

Nothing in sqlide formats SQL — there is no formatter, beautifier or
pretty-printer anywhere in the tree. SQL pasted from a log, a
framework's query dump or a colleague's message arrives as one long
line and stays that way. DBeaver's Ctrl+Shift+F is a reflex for most
of its users, and its absence is noticed immediately.

It also matters beyond the editor: the generated SQL in the query
builder and the DDL in `frontend/definition_tab.py` are hand-assembled
strings whose layout is baked into the code that builds them.

### Goal

A formatter for the editor, and a single implementation the generated
SQL can also route through.

### Approach

- `sqlide/backend/sql_format.py`, pure, built on the tokenizer that
  already exists in `backend/sql_split.py` — `tokens()` already knows
  strings, comments, dollar-quoted bodies and quoted identifiers,
  which is where a naive formatter goes wrong. No new dependency.
- A deliberately modest formatter, because a wrong reformat is worse
  than none: keyword casing, one clause per line
  (`SELECT`/`FROM`/`WHERE`/`GROUP BY`/`HAVING`/`ORDER BY`/`LIMIT`),
  indented `JOIN`/`ON`, `AND`/`OR` aligned under `WHERE`, comma
  placement, parenthesis-depth indentation for subqueries, and
  comments preserved in place. It does *not* rewrite expressions,
  reorder anything, or touch a statement it cannot tokenize cleanly —
  such a statement is returned unchanged, reported, never mangled.
- Settings: keyword case (upper / lower / leave), indent width,
  comma-leading or trailing. Stored in `settings.toml` beside the
  other editor settings.
- Wire it to Format (default Ctrl+Shift+F, remappable through
  `frontend/keymap.py`) over the selection if there is one, otherwise
  the statement under the cursor — the same rule Run already uses.
- Route the DDL preview text and the query builder's rendered SQL
  through it, so one definition of "how our SQL looks" exists.

### Acceptance criteria

- [x] `backend/sql_format.py` imports no GTK and adds no dependency;
      it reuses `sql_split.tokens`.
- [x] Formatting is idempotent: formatting twice equals formatting
      once, for every fixture.
- [x] Formatting never changes what a statement means — the fixtures
      include strings containing keywords, dollar-quoted bodies,
      `DELIMITER` scripts, block and line comments, and a `CASE`
      expression.
- [x] A statement that cannot be tokenized cleanly is returned
      unchanged with a reported reason, not partially formatted.
- [x] Format applies to the selection, or the statement under the
      cursor when there is none, matching Run's rule.
- [x] Covered by `tests/test_sql_format.py`, no database server.
