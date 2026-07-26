---
title: Create/Drop DDL
description: Building and dropping schema objects from the sidebar.
order: 5
---

Right-click a connection row for **New ▸**: tables, views, indexes,
triggers, and — where the dialect supports them — functions, procedures,
and events.

- **Tables** open a small designer tab: name, columns, types,
  primary-key/nullable/default flags, and a live preview of the
  generated `CREATE TABLE`.
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
