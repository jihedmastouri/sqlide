---
title: Language Servers
description: Schema-aware completion via external language servers.
order: 4
---

The query console always has built-in keyword completion. If a language
server is available for the connection's database, its suggestions
(tables, columns, functions…) are merged in. Servers are found on
`$PATH`, started lazily per connection, and reused across consoles.

## Built-in defaults

- **PostgreSQL** — [Postgres Language Server](https://github.com/supabase-community/postgres-language-server).
  Install the `postgrestools` binary; sqlide runs `postgrestools
  lsp-proxy` with a generated `postgrestools.jsonc` carrying the
  connection's host/user/database, so completion is schema-aware.
- **MySQL / SQLite** — [sqls](https://github.com/sqls-server/sqls).
  Install the `sqls` binary; sqlide generates a config from the
  connection's DSN. If `sqls` is missing,
  [sql-language-server](https://github.com/joe-re/sql-language-server) is
  tried as a fallback (`sql-language-server up --method stdio`).

Each query console has two extra dropdowns next to the connection picker
(both session-only, not saved with the workspace):

- **LSP** — pin the completion server for that console: *auto* (the
  resolution order above), *off*, or any plugin/detected binary.
- **Database** — for MySQL/PostgreSQL connections, whose one server
  hosts many databases: queries and completions run against the chosen
  database. Hidden for SQLite, where one file is one database.

## Custom plugins

To use any other server, or override the defaults, drop an executable
into `$XDG_CONFIG_HOME/sqlide/lsp/` (usually `~/.config/sqlide/lsp/`)
named after the connection kind — `postgres`, `mysql`, `sqlite`, `jdbc`
— or `default` as a catch-all for every kind without a specific plugin.
Plugins take precedence over the built-in defaults.

The program is spawned with no arguments and must speak LSP over stdio.
The active connection's details arrive in environment variables:
`SQLIDE_DB_KIND`, `SQLIDE_DB_NAME`, `SQLIDE_DB_HOST`, `SQLIDE_DB_PORT`,
`SQLIDE_DB_USER`, `SQLIDE_DB_PASSWORD`, `SQLIDE_DB_DATABASE`,
`SQLIDE_DB_FILE` (SQLite), `SQLIDE_DB_JDBC_URL`.

A wrapper script is enough when a server needs flags:

```sh
#!/bin/sh
# ~/.config/sqlide/lsp/mysql
exec sql-language-server up --method stdio
```

Remember `chmod +x`. A server that exits or misbehaves is disabled for
the rest of the session; keyword completion keeps working regardless.
