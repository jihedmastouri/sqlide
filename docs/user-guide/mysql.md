---
title: MySQL
description: Connecting to MySQL or MariaDB, the databases-under-a-connection tree, accounts, events and mysqldump.
order: 0.2
---

Needs `pip install PyMySQL`. Tested against MySQL 5.7 and 8.0.

## Connecting

Host, port, user, password and a database, plus optionally an SSH
tunnel, a colour and an environment marking.

**Create demo database** builds a database called `demo` on the server
the fields describe and fills the form in with it.

## The tree

A MySQL connection reaches a **server**, not one database. So the
databases sit under the connection row — the one the profile opened
first, marked *current*, then every other database on the server:

```
connection
└── database          ← the level that matters
    ├── Tables
    ├── Views
    ├── Functions
    ├── Indexes
    ├── Triggers
    └── Events
```

Tables hang off a database, never off the connection: on a server
there is no such thing as a table at the root.

Each database is its own connection underneath, named `connection ·
database`. A query console's **Database** dropdown produces the same
name, so the console and the sidebar share one connection rather than
opening two.

## Schemas

MySQL needs no schema handling: a schema *is* a database. The console's
**Database** dropdown is already the schema switcher, and no separate
Schema dropdown is shown.

## Events

MySQL's scheduled events get their own sidebar category, with the same
**New ▸** template and **Drop…** confirmation as everything else.

## Users and permissions

Right-click a connection → **Users & Permissions…** lists every account
the server reports. Accounts keep their host half — `'app'@'10.0.%'` is
a different account from `'app'@'localhost'`, and sqlide does not
flatten them. Opening one shows server-wide rights and per-database and
per-table grants.

**New User…**, **Set Password…**, **Grant…**, **Revoke…** and **Drop…**
build the statement and open it in a query console rather than running
it. A revoked privilege breaks whoever was relying on it, and that is
not visible from the statement — so account changes take the same
review-then-run path as generated DDL.

The Grant/Revoke dialog offers only the scopes the server actually has
and the privileges MySQL names.

## Open Schema

The generated script is bracketed with `SET FOREIGN_KEY_CHECKS`, so
tables that reference each other in a cycle replay without an ordering
problem.

## Completion

Install [sqls](https://github.com/sqls-server/sqls); sqlide generates a
config with the connection's DSN.
[sql-language-server](https://github.com/joe-re/sql-language-server) is
the fallback.

## Backups

The dump is `mysqldump`, with the exact command shown before it runs;
restore uses `mysql`. A job can scope itself to one database or a
chosen list of tables, and to schema, data, or both.

If a connection reaches its server through sqlide's own SSH tunnel,
`mysqldump` cannot be driven against it — a one-off backup falls back
to a portable snapshot read through the open connection, and says so.

## Monitoring

Sessions, throughput, cache hit ratio, locks and storage, with kill
where your account is permitted. Panels your grants do not cover
explain themselves rather than showing zeros.

## Not available

No extensions folder (that is a PostgreSQL concept), no schema level,
and no map view — geometry rendering requires a spatial extension
sqlide recognises.
