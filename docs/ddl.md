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
