---
title: JDBC
description: The generic escape hatch — any database with a JDBC driver, and the limits that come with it.
order: 0.4
---

**Experimental.** JDBC is the escape hatch for databases sqlide has no
adapter for. It works, but it knows less about your database than the
three native adapters do, and this page is mostly about what it cannot
do.

## Requirements

- `pip install JayDeBeApi`
- A Java runtime
- The driver jar for your database

## Connecting

The connection dialog asks for a JDBC URL and the path to the driver
jar. There is no per-engine field set, because sqlide does not know
which engine is behind the URL.

## How it differs

sqlide gets its catalog from `java.sql.DatabaseMetaData` rather than
from dialect SQL. That is what makes it generic, and also what limits
it: `DatabaseMetaData` answers what every database has in common, not
what yours has in particular.

**The tree** has no databases and no schemas level — one connection is
one database, and the categories sit directly under the connection row.

**Paging is emulated client-side**, so it does not have the keyset
guarantees the native adapters give.

**DDL is templates only.** You get the **New ▸** skeletons, but no
**Drop…** dialogs: building a safe `DROP` needs dialect knowledge
sqlide does not have behind a JDBC URL.

**No accounts.** There is no portable catalog for users and
privileges, so no Users & Permissions category appears.

**No extensions, no maps, no monitoring.** None of these have a
portable answer.

**Completion is keywords only** unless you supply a language server
yourself — drop an executable at `~/.config/sqlide/lsp/jdbc` (see
[Language servers](/docs/user-guide/#language-servers)). The
connection's URL arrives as `SQLIDE_DB_JDBC_URL`.

## Backups

No vendor tool can be driven through a JDBC bridge, so a backup is
always a **portable snapshot**: structure and rows read through the
connection sqlide already holds open, with no grants or storage
settings. The dialog tells you it is using a snapshot and why.

Restoring one runs the script statement by statement over that same
connection, stopping at the first error.

## What does work

Browsing tables, views and columns; the data grid with in-place
editing where a primary key is reported; the query console with
keyword completion; export and import; charts and dashboards over
result sets; and the MCP server — though on JDBC that relies on the
statement guard alone, since JDBC has no portable read-only mode for
the connection itself.

If you use one database through JDBC regularly, a native adapter for
it is a well-scoped contribution — see the
[Developer Guide](/docs/developer-guide/#add-a-database-engine).
