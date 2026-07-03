# sqlide

A minimal, clean SQL IDE built with Python, GTK4, and libadwaita.
SQLite works today; MySQL/PostgreSQL are stubbed; a generic JDBC bridge is
included (experimental). See [PLAN.md](PLAN.md) for design and status.

## Requirements

- Python 3.12+
- GTK4 + libadwaita + PyGObject (usually system packages, e.g.
  `sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1`)
- Optional drivers:
  - MySQL: `pip install PyMySQL` (adapter not implemented yet)
  - PostgreSQL: `pip install "psycopg[binary]"` (adapter not implemented yet)
  - JDBC: `pip install JayDeBeApi` + a Java runtime + the driver jar

SQLite needs nothing extra.

## Try it (SQLite)

Nothing here has been executed yet — this is the intended manual test path:

```sh
# 0. optional sanity check: everything should compile and import
python3 -m compileall -q sqlide && python3 -c "import sqlide.backend.db.registry"

# 1. create a demo database
python3 scripts/make_demo_db.py          # writes ./demo.db

# 2. launch
python3 -m sqlide
```

Then, in the app:

1. The launcher opens first: click **+** (or **Create Workspace**), give
   the workspace a name. A workspace groups its own connections and
   remembers your open tabs.
2. In the workspace window, click **+** in the sidebar header → type stays
   **SQLite** → browse to `demo.db` → **Test connection** → **Save**.
3. Expand the connection in the sidebar; click a table (e.g. `customers`)
   to open it in a grid tab.
4. Click into a cell, edit, press Enter — the change is written with a
   primary-key UPDATE. The `log` table has no primary key and should show
   as read-only. `order_totals` is a view (also read-only).
5. Click the terminal icon on the connection row for a query console;
   type SQL and press **Ctrl+Enter** (or Run).
6. Close and restart — the launcher lists the workspace; opening it
   restores the connections *and* the tabs you left open, including query
   console text (`~/.config/sqlide/workspaces/<id>.json`). Other
   workspaces are only visible in the launcher (the grid icon in the
   sidebar header reopens it).

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

Each query console has two extra dropdowns next to the connection
picker (both session-only, not saved with the workspace):

- **LSP** — pin the completion server for that console: *auto* (the
  resolution order above), *off*, or any plugin / detected binary.
- **Database** — for MySQL/PostgreSQL connections, whose one server
  hosts many databases: queries and completions run against the chosen
  database. Hidden for SQLite, where one file is one database.

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

## Layout

- `sqlide/backend/` — connectors, connection profiles, and the workspace
  store (one JSON file per workspace), zero GTK. One folder per database
  under `backend/db/`.
- `sqlide/frontend/` — all GTK/libadwaita UI.
- `sqlide/lsp/` — the LSP client and per-connection server management
  (zero GTK); `frontend/lsp_completion.py` is the editor-facing bridge.
- `scripts/make_demo_db.py` — builds the demo SQLite database.
