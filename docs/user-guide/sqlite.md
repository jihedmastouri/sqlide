---
title: SQLite
description: Connecting to SQLite, what a file-per-database means for the app, and the PRAGMA editor.
order: 0.1
---

SQLite needs no driver and no server — it uses Python's own `sqlite3`
module. It is the shortest path to seeing what sqlide does.

## Connecting

One field: the path to the file. **Create demo database** makes one
for you under `~/.local/share/sqlide` and fills the path in.

There is no host, no user, no password, and no SSH tunnel — the file
is either readable or it is not.

## The tree

One connection is one database, so the categories sit directly under
the connection row:

```
connection
├── Tables
├── Views
├── Functions
├── Indexes
└── Triggers
```

No databases level, no schemas level — SQLite has neither.

## Editing data

Rows are identified by the primary key, and where a table has none
sqlide falls back to SQLite's implicit `rowid`. That fallback is why
more SQLite tables are editable here than you might expect.

`WITHOUT ROWID` tables have no such fallback: with no primary key
they are read-only, and the tab says so.

## Schema changes

SQLite has no real `ALTER TABLE`, so **Table Definition** does the
rebuild for you. Editing the `CREATE` statement and pressing Save
generates the rename-old / create-new / copy-columns / drop-old
sequence, matching columns by name between the old catalog and your
edited statement. Views are simpler: `DROP VIEW` plus the edited
`CREATE`.

The statement is shown before it runs, like everything else.

## PRAGMAs

SQLite's settings surface is a tab of its own, in three groups:

- **Settings** — the pragmas that take a value: a switch for booleans,
  a select for enumerations, an entry for numbers. Every value is
  validated before any SQL exists.
- **Information** — page counts, encoding, the data version. Read-only
  questions with answers.
- **Checks** — `integrity_check` and friends. These read the whole
  file, so they are never run just to draw the list: each has a Run
  button and opens its rows in a dialog.

Three things it is careful about:

1. **Nothing is applied silently.** A pragma whose scope is wider than
   this connection — stored in the file, applied only at connect time,
   or one that rewrites the file — goes through a confirmation
   carrying both the warning and the exact statement. Cancelling puts
   the control back.
2. **The dangerous ones are behind Advanced.** `writable_schema` can
   corrupt a database in a way SQL cannot undo, so it is hidden until
   you turn Advanced on, and carries its warning when shown.
3. **The database is asked, not assumed.** Every apply re-reads the
   pragma and redraws the row from the answer — SQLite ignores
   `page_size` on a populated file and refuses `journal_mode` inside a
   transaction, both without an error, and a row showing what you
   asked for would be lying.

**Save as Defaults** writes the settings that differ from SQLite's own
onto the connection profile, and they are applied on every connect.

## Completion

Install [sqls](https://github.com/sqls-server/sqls) for schema-aware
completion; sqlide generates its config from the connection.
[sql-language-server](https://github.com/joe-re/sql-language-server) is
tried as a fallback. Without either, keyword completion still works.

## Backups

The dump is `sqlite3 .dump` — a plain SQL script, gzipped by default,
restorable with `sqlite3` with or without sqlide. All the destinations
and schedules on the [Backups](/docs/user-guide/#backups) section apply
unchanged.

## Not available

No accounts, so no Users & Permissions. No extensions folder. No
schemas. Monitoring has little to show — SQLite exposes almost nothing
about itself to a client — so most panels explain that rather than
appearing empty.
