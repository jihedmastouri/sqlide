# sqlide

A minimal, clean SQL IDE built with Python, GTK4, and libadwaita.
SQLite, MySQL, and PostgreSQL are fully supported; a generic JDBC bridge is
included (experimental). See [PLAN.md](PLAN.md) for design and status.

## Requirements

- Python 3.12+
- GTK4 + libadwaita + PyGObject (usually system packages, e.g.
  `sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1`)
- Optional drivers:
  - MySQL: `pip install PyMySQL`
  - PostgreSQL: `pip install "psycopg[binary]"`
  - JDBC: `pip install JayDeBeApi` + a Java runtime + the driver jar
  - MCP server: `pip install "sqlide[mcp]"` (the `mcp` SDK + uvicorn)
  - System keyring for connection passwords: `pip install "sqlide[keyring]"`

SQLite needs nothing extra.

On a machine with Nix, none of the above is needed: `nix run .` launches
the app and `nix develop` gives you a shell with GTK, the drivers and
the dev tools already on `PATH`. See [docs/nix.md](docs/nix.md).

## Try it (SQLite)

```sh
make demo    # writes ./demo.db
make run     # launches the app
```

`make` on its own lists every development entry point: `install`
(venv + drivers + pytest), `test`, `check`, `lint`, `servers` (the
throwaway MySQL/PostgreSQL containers), `init-db`, `flatpak`, `web`.
`make run-fresh` launches against a throwaway config directory, which
is how you get a real first run back once you have workspaces on file
(all state lives in `$XDG_CONFIG_HOME/sqlide`, `~/.config/sqlide` by
default). Without make, the same two steps are:

```sh
python3 scripts/make_demo_db.py          # writes ./demo.db
python3 -m sqlide
```

Or skip the command line entirely: the connection dialog has a
**Create demo database** button that builds the same sample database
for whichever engine is selected, and fills the dialog in with what it
made — no path or name to invent first. SQLite gets a new file under
`~/.local/share/sqlide`; MySQL and PostgreSQL get a new database called
`demo` on the server the fields describe. Pressing it again builds
another one (`demo-2.db`, `demo_2`) rather than touching the first,
which may by then have something in it.

Then, in the app:

1. A first run opens the home page: name your first workspace, pick a
   colour, **Create Workspace** (or **Import…** in the header, if you
   are moving from another machine). A workspace groups its own
   connections and remembers your open tabs. Every later launch skips
   the home page and reopens the workspace you were last in; the grid
   icon in the sidebar header lists them all, for renaming,
   recolouring or adding another.
2. In the workspace window, click **+** in the sidebar header → type stays
   **SQLite** → browse to `demo.db` → **Test connection** → **Save**.
3. Expand the connection in the sidebar; click a table (e.g. `customers`)
   to open it in a grid tab.
4. Click into a cell, edit, press Enter — the change is written with a
   primary-key UPDATE. The `log` table has no primary key and should show
   as read-only. `order_totals` is a view (also read-only).
5. Click the terminal icon on the connection row for a query console;
   type SQL and press **Ctrl+Enter** (or Run).
6. Close and restart — you land back in the workspace you were last in,
   with the connections *and* the tabs you left open restored, query
   console text included (`~/.config/sqlide/workspaces/<id>.json`).
   Other workspaces stay out of the way behind the grid icon in the
   sidebar header, which opens the workspace list.

## MySQL and PostgreSQL to try it against

`docker-compose.yml` runs throwaway servers (PostgreSQL 10–16, MySQL
5.7 and 8.0) on fixed ports, all with user/password `sqlide`/`sqlide`:

```sh
make servers              # postgres16 + mysql8
make servers-all          # every version
docker compose up -d postgres14   # or just one
```

Each server holds two databases: `sqlide`, which the tests reseed, and
`demo`, which carries the same schema as the SQLite demo — a table with
a primary key and one without, a view, an index, a trigger, a stored
function and a foreign key, so every part of the app has something to
show. Containers get it at first start; `make init-db` (or
`python3 scripts/init_databases.py`) rebuilds it on servers that are
already running.

## Moving a setup to another machine

Workspaces live in `~/.config/sqlide/workspaces/` as JSON keyed by a
local id, with passwords in the keyring — not something to copy
around. **Export Workspace…** and **Export Connections…**
(Preferences → General → Workspace Transfer) write a small, readable XML file
instead; the folder button in the workspace list imports one as a new
workspace,
and **Import Connections…** merges connections into the open one.
Passwords are left out unless the export explicitly asks for them, and
an import never overwrites what is already there. See
[docs/transfer.md](docs/transfer.md).

## Language servers (smarter completion)

The query console always has built-in keyword completion. If a language
server is available for the connection's database, its suggestions
(tables, columns, functions…) are merged in. Servers are found on
`$PATH`, started lazily per connection, and reused across consoles:

- **PostgreSQL** — [Supabase's Postgres Language Server](https://github.com/supabase-community/postgres-language-server):
  install the `postgrestools` binary. sqlide runs `postgrestools
  lsp-proxy` with a generated `postgrestools.jsonc` carrying the
  connection's host/user/database, so completion is schema-aware.
- **MySQL / SQLite** — [sqls](https://github.com/sqls-server/sqls):
  install the `sqls` binary. sqlide generates a config with the
  connection's DSN. If `sqls` is missing,
  [sql-language-server](https://github.com/joe-re/sql-language-server)
  is tried as a fallback (`sql-language-server up --method stdio`).

Each query console has three extra dropdowns next to the connection
picker (all session-only, not saved with the workspace):

- **LSP** — pin the completion server for that console: *auto* (the
  resolution order above), *off*, or any plugin / detected binary.
- **Database** — for MySQL/PostgreSQL connections, whose one server
  hosts many databases: queries and completions run against the chosen
  database. Hidden for SQLite, where one file is one database.
- **Schema** — for PostgreSQL, where a database holds many schemas.
  Hidden for SQLite (no schemas) and for MySQL, where a schema *is* a
  database and the dropdown to its left already switches it. See
  [Schemas](#schemas) below.

### LSP plugins

To use any other server (or override the defaults), drop an executable
into `$XDG_CONFIG_HOME/sqlide/lsp/` (usually `~/.config/sqlide/lsp/`)
named after the connection kind — `postgres`, `mysql`, `sqlite`,
`jdbc` — or `default` as a catch-all for every kind without a specific
plugin. Plugins take precedence over the built-in defaults.

The program is spawned with no arguments and must speak LSP over
stdio. The active connection's details arrive in environment
variables: `SQLIDE_DB_KIND`, `SQLIDE_DB_NAME`, `SQLIDE_DB_HOST`,
`SQLIDE_DB_PORT`, `SQLIDE_DB_USER`, `SQLIDE_DB_PASSWORD`,
`SQLIDE_DB_DATABASE`, `SQLIDE_DB_FILE` (sqlite), `SQLIDE_DB_JDBC_URL`.
A wrapper script is enough when a server needs flags:

```sh
#!/bin/sh
# ~/.config/sqlide/lsp/mysql
exec sql-language-server up --method stdio
```

(Remember `chmod +x`.) A server that exits or misbehaves is disabled
for the rest of the session; keyword completion keeps working.

## Schemas

In PostgreSQL a database holds many schemas, and two of them can hold
a table of the same name. sqlide works in one schema at a time:

- the connection dialog has a **Schema** field (PostgreSQL only). Left
  blank, the server's own `search_path` applies — usually `"$user",
  public` — and objects outside it are not listed. Set, it becomes the
  connection's `search_path`, so the sidebar, the grid, completion and
  your own unqualified SQL all agree on which schema is meant;
- a query console's **Schema** dropdown switches it for that console
  alone, the same way its Database dropdown does;
- where several schemas *are* on the search_path, an unqualified name
  resolves the way PostgreSQL resolves it — first match wins — and the
  sidebar lists it once, not once per schema holding the name.

MySQL needs none of this: there a schema and a database are the same
object, so the Database dropdown is already the schema switcher.
SQLite has no schemas at all.

## Saving a schema to use later

Right-click a connection → **Save Schema…** captures that database's
whole structure (tables with their constraints, indexes, views,
triggers, stored routines — no rows) as a named CREATE script. Saved
schemas are global, not per-workspace, and appear on the side panel's
**Schemas** page; activating one opens it in a query console to read
and run, so nothing is ever executed behind your back. A schema
captured from another engine still opens, after a warning that the
dialect will not match.

The scripts are written to replay cleanly: PostgreSQL adds foreign
keys after every table exists, MySQL brackets its script with
`SET FOREIGN_KEY_CHECKS`, so tables that reference each other in a
cycle are not an ordering problem. An object whose definition the
server refuses to hand over becomes a comment saying so rather than
disappearing from the script.

## Create/drop DDL

Right-click a connection row for **New ▸** (tables, views, indexes,
triggers, and — where the dialect supports them — functions,
procedures and events): tables open a small designer tab (name,
columns, types, PK/nullable/default, a live preview of the generated
`CREATE TABLE`); everything else opens a query console prefilled with
a commented, dialect-correct skeleton to fill in and run. Right-click
any object (including rows under the sidebar's Indexes/Triggers/Events
categories) for **Drop…**, which shows the exact statement — with a
CASCADE checkbox on PostgreSQL — before running it. **Refresh** on the
connection menu reloads the sidebar subtree after either. JDBC
connections get templates only (no reliable dialect knowledge to build
a safe DROP).

## MCP server

sqlide can expose a read-only [Model Context Protocol](https://modelcontextprotocol.io/)
server so an AI assistant (Claude, etc.) can browse and query your
databases without ever being able to write to them. Needs the optional
`mcp` extra: `pip install sqlide[mcp]`.

Open it from the header bar's network icon (blank form) or a
connection's context menu ("MCP Server", preselecting that
connection). Each tab is a **fresh, independent instance** — its own
connectors, its own port — that never touches the connections cached
by the rest of the app; several tabs run side by side without sharing
state, and closing a tab (or Stop) shuts that instance down. Nothing
is persisted across restarts.

The form:

- **Connections** — which of the workspace's connections this
  instance exposes; pick one or more.
- **Port** — 0 (default) picks a free port automatically.
- **Listen on all interfaces** — off (default) binds 127.0.0.1, only
  reachable from this machine; on binds 0.0.0.0 and forces a bearer
  token (sqlide refuses to start otherwise).
- **Enable the query tool** — off exposes only the catalog
  (`list_tables`/`list_columns`/`get_ddl`), no arbitrary `SELECT`.
- **Row limit** — caps how many rows one query returns.
- **Require a bearer token** — checked on every request; wrong or
  missing → 401. A token is generated for you (regenerate any time)
  or you can type your own.

Once started, the tab shows the server URL, a copy-ready client JSON
snippet (also saveable to a file) for `~/.claude.json` or similar:

```json
{"mcpServers": {"sqlide-<workspace>": {
    "url": "http://127.0.0.1:PORT/mcp",
    "headers": {"Authorization": "Bearer <token>"}}}}
```

and a live request log (tool, connection, duration; denied attempts
included).

### Security model (defense in depth)

1. **The query tool's guard** rejects anything that isn't exactly one
   `SELECT`/`WITH`/`EXPLAIN` statement (plus `SHOW` on MySQL) with no
   write keyword anywhere in it — including inside a PostgreSQL
   data-modifying CTE (`WITH x AS (DELETE …) SELECT …`).
2. **The database connection itself is opened read-only** where the
   driver supports it: SQLite via `mode=ro`, PostgreSQL via
   `default_transaction_read_only=on`, MySQL via `SET SESSION
   TRANSACTION READ ONLY`. This is the real backstop if the guard were
   ever bypassed. JDBC has no portable read-only mode, so JDBC
   instances rely on the guard alone.
3. **0.0.0.0 without a token is refused outright.**

## Connection passwords

Right-click a connection in the sidebar for **Edit…** (the same form
as adding one, pre-filled — renaming it there is safe, open tabs keep
working) and **Remove…** (confirmed). Workspaces can be renamed too,
from a pencil button on their row in the workspace list (the grid icon
in the sidebar header).

With the `keyring` extra installed and a backend available (GNOME
Keyring, KWallet, macOS Keychain, …), a connection's password and SSH
tunnel password are stored there instead of in the workspace's JSON
file, which keeps only a blank placeholder. Without a usable keyring
— the extra isn't installed, or no backend is running — sqlide falls
back to plain text in the JSON file, as in earlier versions; nothing
needs configuring either way. Keyring entries are per machine: copying
a workspace file elsewhere carries the blanked password field, not
the secret, so the connection needs its password re-entered once on
the new machine.

## Layout

- `sqlide/backend/` — connectors, connection profiles, and the workspace
  store (one JSON file per workspace), zero GTK. One folder per database
  under `backend/db/`; the read-only MCP server lives under `backend/mcp/`;
  `backend/exchange.py` is the portable XML transfer format.
- `sqlide/frontend/` — all GTK/libadwaita UI. The two cairo diagrams
  (`relation_graph.py` for schemas, `plan_graph.py` for explain plans)
  share their palette and primitives through `canvas.py`.
- `sqlide/lsp/` — the LSP client and per-connection server management
  (zero GTK); `frontend/lsp_completion.py` is the editor-facing bridge.
- `sqlide/backend/demo/` — the demo database: one `.sql` file per
  dialect and the code that builds them. Those files are the single
  source for all three ways in — the app's own "Create demo database"
  button, `scripts/make_demo_db.py` (SQLite, from the command line),
  and `scripts/init_databases.py` plus the `docker-compose.yml` mounts
  (the MySQL/PostgreSQL containers).
