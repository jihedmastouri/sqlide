---
title: Introduction
description: What sqlide is and what it isn't.
order: 1
---

sqlide is a minimal, clean SQL IDE built with Python, GTK4, and libadwaita.
It connects to SQLite, MySQL, and PostgreSQL, with a generic JDBC bridge for
anything else with a JDBC driver.

It's built in the spirit of DBeaver or DataGrip, but deliberately basic: a
schema browser, a paged data grid you can edit in place, and a query
console. No visual query builder, no ER diagrams, no SSH tunnel wizards, no
IDE-grade intellisense. If you want a small, fast tool to look at a
database and run some SQL, that's the whole pitch.

## Core features

- **Schema browser** — tables, views, and functions per connection, with
  columns shown inline (type, primary key, nullability).
- **Data grid** — paged table browsing with inline cell editing, committed
  as primary-key `UPDATE`s. Tables without a primary key, and views, are
  read-only.
- **Query console** — a SQL editor (GtkSourceView 5 highlighting when
  available) with keyword completion and results shown in the same grid
  widget used for tables.
- **Workspaces** — a workspace groups its own connections and remembers
  which tabs you had open, restoring them on the next launch.
- **Create/drop DDL** — a small table designer plus dialect-correct
  skeletons for views, indexes, triggers, functions, procedures, and
  events; drop dialogs show the exact statement before running it.
- **Language server completion** — schema-aware completion via
  [Postgres Language Server](https://github.com/supabase-community/postgres-language-server)
  or [sqls](https://github.com/sqls-server/sqls), merged into the built-in
  keyword completion.
- **Monitoring dashboard** — sessions, throughput, cache hit ratio, locks
  and storage per connection, with cancel/kill where the account may,
  and an explicit reason wherever a panel cannot be filled.
- **MCP server** — expose a workspace's connections to an AI assistant
  over the [Model Context Protocol](https://modelcontextprotocol.io/),
  read-only by construction.
- **Keyring-backed passwords** — connection passwords stored in the
  system keyring when one is available, instead of plain JSON.

See [Installation](/docs/installation/) to get it running, or jump straight
to [Getting Started](/docs/getting-started/) for a walkthrough.
