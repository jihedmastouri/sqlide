# next steps — backlog

Ordered; this is the short list, and it is what to pick from next. Detailed
design notes for each milestone are kept locally in `confidential/`, which is
outside version control — read the notes for a milestone before starting it.

---

## Now — milestone 11, data editing completeness

- [ ] `ChangeSet` model in `backend/db/base.py` (inserts / updates / deletes,
      `KeyRef` row identity) plus `apply_changes()` and `changes_to_sql()` on
      `Connector`, implemented for SQLite, MySQL and Postgres. One transaction
      per apply, ordered inserts → updates → deletes, re-select updated rows,
      rollback on any failure with the offending statement in the error.
- [ ] `supports_row_editing()` — PK strategy per table, SQLite `rowid` fallback,
      `WITHOUT ROWID` detection, and a human reason string when a table is
      read-only.
- [ ] Grid staging UI in `frontend/data_grid.py`: added/removed/modified row and
      cell decorations, a change counter, Apply / Reset / Copy to SQL, and a
      confirmation before sort/filter/page/close discards pending changes.
- [ ] Row operations: add row (`+`), clone, delete, multi-row selection.
- [ ] Clipboard in: paste over a range (`ctrl+v`) and paste as new rows
      (`ctrl+shift+v`), TSV-then-CSV parsing, NULL sentinels, read-only columns
      skipped with a count reported.
- [ ] `ColumnInfo` gains `generated`, `auto_increment`, `default`,
      `enum_values`; generated columns become read-only, enum columns get a
      dropdown, `NOT NULL` without a default blocks Apply until filled.
- [ ] Large-value modal editor (`shift+enter`): GtkSourceView, JSON
      validate/format/minify, byte count.
- [ ] Binary display setting (hex | base64) and `HEX()`/`encode()` wrapping in
      filters against binary columns.
- [ ] `result_edit_map()` — make console `SELECT` results editable by resolving
      output fields back to base tables; conservative, with a specific reason
      shown on every non-editable column.
- [ ] "Create IN clause" from a cell selection.

## Next — milestone 12, schema editing

- [ ] **Fix the SQLite rebuild data loss first**, on its own, with a regression
      test: capture indexes and triggers from `sqlite_master` before
      `DROP TABLE` and recreate them after, plus `PRAGMA foreign_key_check`
      before commit. This is a live bug in `rebuild_table_statements()`.
- [ ] `TableAlterSpec` / `IndexAlterSpec` / `RelationAlterSpec` and their
      `*_sql()` generators per dialect. MySQL `MODIFY COLUMN` must re-emit every
      unchanged attribute — dedicated test.
- [ ] `Capabilities` matrix per adapter, version-gated (SQLite `RENAME COLUMN`
      3.25 / `DROP COLUMN` 3.35, MySQL `RENAME COLUMN` 8.0, MariaDB 10.5,
      Postgres `NULLS NOT DISTINCT` 15). Every unsupported operation is disabled
      with a reason, never hidden.
- [ ] Structure tab: columns / indexes / foreign keys / triggers, staged and
      previewed like the grid. The DDL-text editor stays as an advanced path.
- [ ] Object operations: rename, truncate, duplicate (structure and with data).
- [ ] Table properties panel — size, index size, row estimate, comment (editable
      where supported), owner, engine/collation.

## After that

Milestones 13–17: data movement (streaming cursors → export → import), editor
maturity, navigation, configuration and security, then backup/restore and
possibly plugins. Do not start one before the milestone above it is shippable.

## Not doing

Decided, not deferred — see PLAN.md "Non-goals": cloud workspaces and team
sharing, an embedded AI agent (the MCP server is our answer), engines beyond
SQLite/MySQL/Postgres, telemetry, auto-update, cloud-vendor auth.

---

## Done

- [x] Milestone 10, UI foundations and identity (2026-08-05): the identity
      palette (`backend/identity.py`, contrast and colour-blind separation
      asserted by tests) and its runtime CSS provider; `color` on workspaces,
      `color` + `environment` on connections; colour pickers and the production
      *suggestion* in the connection dialog; colour on the window stripe,
      launcher dot, sidebar rows and tabs, each with a non-colour cue a test
      enumerates; the destructive-action ladder (`backend/sql_risk.py` +
      `frontend/confirm.py`) keyed to the environment class and defeatable from
      Preferences; the persistent status bar; the feedback rules in
      `frontend/feedback.py`; empty states; the shortcuts window and accessible
      labels on every icon-only button.
      Two rows of the environment table are **not** in it, because the features
      they switch do not exist yet: the privacy-mode default and the read-only
      suggestion for new connections. Both belong to milestone 16 and must be
      wired to `environment` when that lands.
- [x] Connection & workspace management UI: edit/rename/remove a saved
      connection from the sidebar (connection_dialog.py's "edit" mode,
      pre-filled from the profile, applied in place so open tabs keep
      working); rename a workspace from a pencil button on its
      launcher row.
- [x] Let grid cell edits set a value to NULL: a "Set Cell to NULL"
      item on the cell's right-click menu (enabled only while editing
      is unlocked) — typing still can't tell NULL from "" in an
      EditableLabel, so this goes through on_edit(row, col, None)
      instead.
- [x] Move stored connection passwords out of plaintext JSON into the
      system keyring (new backend/secrets.py, optional `keyring`
      extra), falling back to plaintext when no keyring backend is
      available. Renaming/removing a connection moves/drops its
      keyring entries.
- [x] Product review and feature specifications for milestones 10–17
      (2026-08-05), kept in `confidential/` outside version control.
