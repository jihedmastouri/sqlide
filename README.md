# sqlide

A minimal, clean SQL IDE built with Python, GTK4 and libadwaita.

sqlide is in the spirit of DBeaver or DataGrip, but deliberately small: a
schema browser, a paged data grid you can edit in place, and a query
console — plus the things a database client is actually asked for daily
(backups, monitoring, charts, DDL templates). SQLite, MySQL and
PostgreSQL are fully supported; a generic JDBC bridge covers anything
else with a driver (experimental).

## Install

**Requirements**

- Python 3.12+
- GTK4 + libadwaita + PyGObject — usually system packages, e.g.
  `sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1`

```sh
pip install sqlide
sqlide
```

SQLite needs nothing extra. Install only the extras you need:

| Target | Install |
|---|---|
| MySQL | `pip install PyMySQL` |
| PostgreSQL | `pip install "psycopg[binary]"` |
| JDBC (experimental) | `pip install JayDeBeApi` + a Java runtime + the driver jar |
| MCP server | `pip install "sqlide[mcp]"` |
| Passwords in the system keyring | `pip install "sqlide[keyring]"` |
| S3 backup destinations | `pip install "sqlide[s3]"` (SFTP uses `ssh`; FTP needs nothing) |

Pick a connection kind whose driver is missing and sqlide says so
rather than crashing.

On a machine with Nix, none of the above is needed: `nix run .` launches
the app. See [docs/nix.md](docs/nix.md).

## First run

The connection dialog has a **Create demo database** button that builds
a sample database for whichever engine is selected and fills the dialog
in with what it made — no path or name to invent first. SQLite gets a
file under `~/.local/share/sqlide`; MySQL and PostgreSQL get a database
called `demo` on the server the fields describe.

Then:

1. Name your first workspace, pick a colour, **Create Workspace** (or
   **Import…**, if you are moving from another machine). A workspace
   groups its own connections and remembers your open tabs.
2. Click **+** in the sidebar header to add a connection → **Test
   connection** → **Save**.
3. Expand the connection in the sidebar and click a table to open it in
   a grid tab. Click into a cell, edit, press Enter — the change is
   written with a primary-key `UPDATE`. Tables without a primary key,
   and views, are read-only.
4. Click the terminal icon on a connection row for a query console;
   type SQL and press **Ctrl+Enter**.
5. Close and restart — you land back in the workspace you were last in,
   with the connections *and* the tabs you left open restored, query
   console text included.

All state lives in `$XDG_CONFIG_HOME/sqlide` (`~/.config/sqlide` by
default), as TOML and JSON you can read, edit or commit — see
[docs/configuration.md](docs/configuration.md).

## What it does

**Browse and edit.** Tables, views, functions, indexes and triggers per
connection, columns inline. A paged grid with in-place editing, sorting
and filtering.

**Query console.** Multi-statement scripts, syntax highlighting, and
completion that is schema-aware when a language server is available —
[Postgres Language Server](https://github.com/supabase-community/postgres-language-server)
for PostgreSQL, [sqls](https://github.com/sqls-server/sqls) for
MySQL/SQLite, found on `$PATH`. Per-console dropdowns switch the LSP,
the database and (PostgreSQL) the schema. Any other server can be
dropped into `~/.config/sqlide/lsp/`. See
[docs/language-servers.md](docs/language-servers.md).

**Servers, databases and schemas.** A MySQL or PostgreSQL connection
reaches a whole server, so the sidebar lists its databases under the
connection row and browses each one separately. PostgreSQL connections
have a **Schema** field, and consoles a schema dropdown.

**DDL, without surprises.** **New ▸** opens a small table designer
(with a live `CREATE TABLE` preview) or a dialect-correct skeleton in a
console. **Open Schema** captures a database's whole structure as a
replayable CREATE script in a console. **Drop…** shows the exact
statement — CASCADE checkbox on PostgreSQL — before running it. Nothing
is executed behind your back. See [docs/ddl.md](docs/ddl.md).

**Users and permissions.** Right-click a connection →
**Users & Permissions…** lists every account the server reports, with
server-wide rights, per-database and per-table grants, and PostgreSQL
role attributes and memberships. Create, grant, revoke and drop all
build the statement and open it in a console for you to read and run.

**Backups and restore.** Real database dumps, using the vendor's own
tool — `pg_dump`, `mysqldump`, `sqlite3 .dump` — with the exact command
shown before it runs. Destinations are a local folder, any S3-compatible
bucket, SFTP or FTP/FTPS, each keeping the newest N and pruning the
rest. Jobs run on an in-app schedule or a systemd user timer; the
`sqlide-backup` command (`list`, `run`, `due`, `history`) is the
headless interface for your own cron. **One-off Backup…** works for
every connection kind, falling back to a portable snapshot where no
vendor tool can be driven. **Restore…** goes the other way, into any
connection you pick.

**Monitoring.** Sessions, throughput, cache hit ratio, locks and storage
per connection, with cancel/kill where the account may — and an explicit
reason wherever a panel cannot be filled. See
[docs/monitoring.md](docs/monitoring.md).

**Charts and dashboards.** Chart a result set, save the chart, put
several on a dashboard, export to PNG or SVG.

**MCP server.** Expose a workspace's connections to an AI assistant over
the [Model Context Protocol](https://modelcontextprotocol.io/),
read-only by construction: a guard that admits only single
`SELECT`/`WITH`/`EXPLAIN` statements, a connection opened read-only in
the driver where possible, and a refusal to bind `0.0.0.0` without a
bearer token. Each instance is separate, with its own port and its own
connectors. See [docs/mcp-server.md](docs/mcp-server.md).

**Passwords in the keyring.** With the `keyring` extra and a backend
available (GNOME Keyring, KWallet, macOS Keychain…), connection and SSH
tunnel passwords go there instead of the workspace file. Without one,
sqlide falls back to plain text; nothing needs configuring either way.
See [docs/connection-security.md](docs/connection-security.md).

**Moving to another machine.** **Export Workspace…** and **Export
Connections…** (Preferences → General → Workspace Transfer) write a
small, readable XML file. Importing never overwrites what is already
there, and passwords are left out unless the export explicitly asks for
them. See [docs/transfer.md](docs/transfer.md).

## Documentation

Two guides, plus a page per feature under [docs/](docs/):

- **[User Guide](docs/user-guide/index.md)** — everything the app
  does, from installing it to backing up a production database, with a
  page per engine — [SQLite](docs/user-guide/sqlite.md),
  [MySQL](docs/user-guide/mysql.md),
  [PostgreSQL](docs/user-guide/postgres.md),
  [JDBC](docs/user-guide/jdbc.md).
- **[Developer Guide](docs/developer-guide/index.md)** — the shape of
  the codebase, the invariants, and how to add to it.
