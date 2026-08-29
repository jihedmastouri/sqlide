---
title: Create/Drop DDL
description: Building and dropping schema objects from the sidebar.
order: 5
---

The **+** button on a connection row — or **New ▸** on its right-click
menu, the same list either way — offers tables, views, indexes,
triggers, and, where the dialect supports them, functions, procedures
and events. A connection that has not been opened yet shows the three
kinds every dialect has and connects in the background, so the next
press lists what that adapter can really create.

- **Tables** open a small designer tab with three views of the same
  table — **Columns** (name, type, primary-key/nullable/default flags),
  **Constraints** (primary key, unique, check and foreign key, named
  and composite, with the referenced table and its columns offered from
  the catalog) and **Indexes** (name, columns with a direction, unique,
  and the access method or partial predicate where the engine has one)
  — over a live preview of the statements they generate: the
  `CREATE TABLE` and a `CREATE INDEX` for each index. Which constraint
  kinds and index fields appear depends on the engine.
  The same designer changes a table that already exists: **Edit
  Table…** on a table's right-click menu loads it into the form, the
  button becomes **Apply**, and what runs is the difference between the
  table as it is and the table as you left it — an added column, a
  rename, a type change, a dropped index. The review dialog groups the
  plan by what it costs, dangerous first: what loses data, what the
  existing rows can refuse (a `NOT NULL` over nulls, a `UNIQUE` over
  duplicates — the dialog counts the offending rows for you before you
  apply), and what rewrites the table. On SQLite, where `ALTER TABLE`
  cannot express most of it, the plan is the rename/create/copy/drop
  rebuild inside the pragmas that make it atomic. **Table Definition**
  is still there for hand-writing the `CREATE` itself.
  A designer tab is saved with the workspace: close it, or restart, and
  the table you were designing comes back as you left it, alter
  sessions included. An alter session reloads the table from the
  catalog on restore, so the plan is made against today's schema; a
  saved edit the table can no longer support — a rename of a column
  that has since been dropped, an index over one — is left out and
  said so beside the preview.
  A table need not be started from nothing. **Duplicate Structure…**
  on a table's right-click menu opens a designer on a *new* table with
  that table's columns, constraints and options — the same statement
  under a new name, with the index and constraint names rewritten so
  they cannot clash — and asks first whether its indexes and its
  foreign keys are coming. Rows never are; copying data is Transfer's
  job.
  The designer's **save** button (beside Create) saves the design
  itself as a **template**, under a name, in the config directory —
  one plain TOML file per template in `table_templates/`, so a shape
  can be written by hand, mailed or committed. Saved templates are
  listed under **New ▸ Table ▸ From Template**, on any connection. A
  template records the engine it was saved on: opening one elsewhere
  is allowed and keeps the columns, drops the options that engine does
  not offer, and leaves types it cannot translate in **Custom…**,
  marked, with a line beside the preview naming the columns to check —
  rather than an error, or invalid DDL.
- **Everything else** opens a query console prefilled with a commented,
  dialect-correct skeleton to fill in and run.

Right-click any object — including rows under the sidebar's
Indexes/Triggers/Events categories — for **Drop…**, which shows the
exact statement (with a CASCADE checkbox on PostgreSQL) before running
it.

**Refresh** on the connection menu reloads the sidebar subtree after
either operation.

JDBC connections get templates only — there's no reliable, portable
dialect knowledge to build a safe DROP over an arbitrary JDBC driver.
