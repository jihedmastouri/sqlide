---
title: PostgreSQL
description: Connecting to PostgreSQL, schemas and the search path, roles, extensions, PostGIS and pg_dump.
order: 0.3
---

Needs `pip install "psycopg[binary]"`. Tested against PostgreSQL 10
through 16. This is the engine sqlide supports most fully.

## Connecting

Host, port, user, password, a database — and a **Schema** field that
matters more than it looks. Optionally an SSH tunnel, a colour and an
environment marking.

**Create demo database** builds a database called `demo` on the server
described by the fields.

## The tree

A connection reaches a **server**, and PostgreSQL has one more level
than MySQL:

```
connection
└── database
    └── schema
        ├── Tables
        ├── Views
        ├── Functions
        ├── Indexes
        ├── Triggers
        ├── Sequences
        └── …
```

Databases sit under the connection, the one the profile opened first
marked *current*. Each is its own connection underneath, named
`connection · database` — in PostgreSQL another database is not
reachable without reconnecting, and every catalog query is scoped to
the one it is attached to. A console's **Database** dropdown produces
the same name, so the two share one connection.

**Extensions** and **Roles** hang off a database; **Administer** off
the connection.

## Schemas and the search path

A database holds many schemas, and two of them can hold a table of the
same name. sqlide works in one schema at a time:

- **Left blank**, the connection dialog's Schema field means the
  server's own `search_path` applies — usually `"$user", public` — and
  objects outside it are not listed.
- **Set**, it becomes the connection's `search_path`, so the sidebar,
  the grid, completion and your own unqualified SQL all agree on which
  schema is meant.
- A query console's **Schema** dropdown switches it for that console
  alone.

Where several schemas are on the search path, an unqualified name
resolves the way PostgreSQL resolves it — first match wins — and the
sidebar lists it once, not once per schema holding the name.

System schemas (`pg_catalog`, `information_schema`) are shown by
default; Preferences can hide them.

## Paging

Beyond the primary key, sqlide will use any total NOT NULL unique
index as a row key, so more tables get stable keyset paging — deep
pages cost what the first one did — instead of falling back to
`OFFSET`.

## Roles and permissions

**Users & Permissions…** lists roles, login roles and the groups they
belong to alike, with role attributes and memberships alongside
server-wide, per-database and per-table grants.

Create, set password, grant, revoke and drop all build the statement
and open it in a query console for you to read and run rather than
executing it. The Grant/Revoke dialog offers each database and each
schema as a scope, and only the privileges PostgreSQL names.

There is also a permission editor: one principal, the object tree, and
a grid of privilege checkboxes.

## Extensions

The **Extensions** folder shows what is installed, with version,
schema and whether a newer version is on disk. **Available
Extensions** shows the rest.

Install, update and drop build plain `CREATE`/`ALTER`/`DROP EXTENSION`
for a confirmation dialog, and are offered only where your account
could actually run them.

Extensions sqlide recognises unlock *features* rather than being
special-cased by name — spatial support, statement statistics,
hypertables, vectors, scheduled jobs, extra types. An unrecognised
extension still lists and still opens its info view; it just turns
nothing on. Objects owned by an extension say so in their summary
rather than appearing from nowhere.

## Maps

With a spatial extension installed, `geometry` and `geography` columns
get a **Map** tab beside Data. sqlide parses WKB and PostGIS EWKB
itself, so nothing needs installing on your machine — a cell reads
*Point, SRID 4326, 1 point*, and selection runs both ways between the
map and the grid. A connection whose server has no spatial extension
never grows the toggle.

## Open Schema

The generated script adds foreign keys after every table exists, so
tables referencing each other in a cycle replay cleanly. A definition
the server refuses to hand over becomes a comment saying so.

**Drop…** dialogs carry a CASCADE checkbox.

## Completion

Install the `postgrestools` binary from
[Postgres Language Server](https://github.com/supabase-community/postgres-language-server).
sqlide runs `postgrestools lsp-proxy` with a generated
`postgrestools.jsonc` carrying the connection's host, user and
database, so completion knows your schema.

## Backups

The dump is `pg_dump`, with the exact command shown before it runs;
restore uses `psql`. A job can scope itself to a database, one schema,
or a chosen list of tables, and to schema, data, or both.

Where the server is reached through sqlide's own SSH tunnel,
`pg_dump` cannot be driven against it, and a one-off backup falls back
to a portable snapshot through the open connection — saying which
method it used and why.

## Monitoring

The fullest of the three: sessions, throughput, cache hit ratio, locks
and storage, with cancel and kill where your role is permitted. A
`pg_stat_reset()` or a restart sends counters backwards; sqlide breaks
the line with a banner rather than drawing a negative spike.
