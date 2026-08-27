## CORE-29 — Copy structure from an existing table, and saved templates

- **Status:** todo
- **Depends on:** CORE-23, CORE-28
- **From:** RS-02 (see `docs/table-creator-research.md`)

### Problem

Creating a table like an existing one means opening its DDL in one tab
(`frontend/definition_tab.py`) and retyping it in the designer, which
cannot express most of what you would be copying anyway. There is no
notion of a reusable table shape — the audit columns every table in a
project has, the lookup-table pattern — so the same six columns get
typed again every time.

### Goal

Start a new table from an existing one, or from a shape you saved
earlier.

### Approach

- **Duplicate structure** on a table's sidebar context menu: load the
  table into a `TableModel` (CORE-26's `from_provider`), clear the
  name, open a designer on it. Offer whether to carry indexes and
  foreign keys; carrying data is explicitly out of scope (that is what
  `docs/transfer.md` is for).
- **Save as template** in the designer: the serialised model
  (CORE-28) written to the config directory as TOML, listed in a
  **New from template** submenu under the sidebar's New ▸ Table.
- Templates are engine-tagged. Opening one on a different engine is
  allowed but maps unknown types onto `Custom…` and says which columns
  it could not translate, rather than silently producing invalid DDL.
- Ship no built-in templates in v1; the saved ones are the feature.

### Acceptance criteria

- [ ] Duplicate structure on a table opens a designer whose preview
      differs from the source's DDL only in the table name.
- [ ] A saved template appears under New ▸ Table and restores its
      columns, constraints and options.
- [ ] A template saved on PostgreSQL and opened on SQLite opens with
      untranslatable types marked and an explanation, not an error.
- [ ] Template files are plain TOML in the config directory,
      consistent with CORE-13.
