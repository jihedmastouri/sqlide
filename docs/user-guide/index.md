---
title: User Guide
description: How to use sqlide — install, connect, browse, query, and everything on the menus.
order: 0
---

The complete guide to using sqlide. Engine-specific behaviour lives on
its own page:
[SQLite](/docs/user-guide/sqlite/) ·
[MySQL](/docs/user-guide/mysql/) ·
[PostgreSQL](/docs/user-guide/postgres/) ·
[JDBC](/docs/user-guide/jdbc/).

---

## Install

Needs Python 3.12+ and GTK4, libadwaita and PyGObject as **system**
packages:

```sh
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1   # Debian/Ubuntu
sudo dnf install python3-gobject gtk4 libadwaita          # Fedora
pip install sqlide
sqlide
```

SQLite works with nothing more. Add extras as needed:

| For | Install |
|---|---|
| MySQL | `pip install PyMySQL` |
| PostgreSQL | `pip install "psycopg[binary]"` |
| JDBC | `pip install JayDeBeApi` + a JRE + the driver jar |
| SSH tunnels | `pip install "sqlide[ssh]"` |
| Keyring passwords | `pip install "sqlide[keyring]"` |
| MCP server | `pip install "sqlide[mcp]"` |
| S3 backups | `pip install "sqlide[s3]"` |
| All of it | `pip install "sqlide[all]"` |

With Nix, `nix run .` needs none of this. A Flatpak manifest is in
`build-aux/flatpak/`.

---

## First run

**Get a database.** The connection dialog's **Create demo database**
button builds a sample database for the selected engine and fills the
form in with what it made. Pressing it again makes another one rather
than overwriting the first.

**Make a workspace.** A workspace owns a set of connections, the tabs
you had open (console text included) and a query history. It is how
you keep *work* and *side project* apart. Later launches reopen the
last one directly; the grid icon in the sidebar header lists them all
for switching, renaming and recolouring.

**Add a connection.** Click **+** in the sidebar header, pick the
type, fill in the fields, **Test connection**, **Save**. Optional:
an SSH tunnel, a colour, and an *environment* marking — the last is
not decoration, it is what destructive confirmations quote back at you.

Right-click a connection later for **Edit…** (renaming is safe, open
tabs keep working) and **Remove…**.

---

## The window

- **Sidebar** — this workspace's connections and their objects. The
  header has the workspace switcher and settings; below it, Add
  Connection, search and Refresh. Drag its inner edge to resize.
- **Tabs** — everything you open. `Ctrl+Alt+S` splits the tab area in
  two; a tab can also pop out into its own window.
- **Side panel** — follows the active tab and shows only what fits it:

| Page | |
|---|---|
| Properties | the active object's properties |
| Info | its summary, columns and DDL |
| Value | the focused cell in full — JSON, a stack trace, a blob — editable where the grid allows it |
| Snippets / Queries | saved SQL |
| Notes | free-form Markdown |
| History | everything that ran, this tab or the whole workspace |
| Aggregate | statistics over the selected cells |
| Filters | a table tab's filter rows |

---

## Browsing

Expand a connection for its object tree. The depth depends on the
engine — see the engine pages — but the leaves are always Tables,
Views, Functions, Indexes, Triggers and whatever else that engine has.
The tree is lazy: a level is read when you expand it. `F5` refreshes.

The sidebar's search icon filters the tree. **Data search** is a
different thing: a tab that scans a schema's *contents* for a value,
with results arriving table by table as it goes.

---

## The data grid

Click a table to open it.

**Editing** — click a cell, type, Enter. Written as a primary-key
`UPDATE`. Edits can be staged and applied together, with a change
counter and **Apply** / **Reset** / **Copy to SQL**. Tables with no
primary key, and views, are read-only and say so.

**Paging** — the engine decides the page order, so pages never repeat
or skip rows. Where a relation has no key to order by, the status line
says **order not guaranteed** instead of pretending.

**Columns** — drag to reorder, drag edges to resize, either mouse
button on a header for the menu. `Ctrl+C` copies selected cells.

**Export** — the cell menu or the action bar. Asks which rows (the
selection, what is loaded, or the whole query), what shape (CSV, JSON,
`INSERT`, Markdown) and where, with a live preview of the first lines.

**Import** — from a table's menu. Pick the file and how to read it,
check the parsed preview, map source columns to table columns, and
choose whether to keep or empty the existing rows.

**Chart** and **Map** tabs sit alongside **Data** on the same result.

---

## Query console

`Ctrl+T`, or the terminal icon on a connection row.

| | |
|---|---|
| `Ctrl+Enter` | run the selection, or the statement at the cursor |
| `Ctrl+Shift+Enter` | run everything |
| `Ctrl+Shift+F` | format |
| `Ctrl+O` / `Ctrl+S` | open / save a `.sql` file |

A console is not tied to one connection — a toolbar dropdown picks the
target at run time. Next to it sit up to three session-only dropdowns:
**LSP** (pin or disable the completion server), **Database** and
**Schema**, each shown only where the engine has that level.

Completion always offers keywords, plus schema-aware suggestions when
a language server is available. `EXPLAIN` draws its plan as a graph
rather than rows. Preferences has a Vim mode for the editor.

Two variants: `Ctrl+Alt+T` opens a **CLI console** — a
`psql`/`mysql`/`sqlite3`-style terminal that answers meta-commands
(`\dt`, `.tables`, …) as well as SQL. `Ctrl+Alt+B` opens the **query
builder**: pick a base table, add joins prefilled from foreign keys,
tick columns, add filters and sorts, and watch the SQL preview update.

---

## Creating and dropping

Right-click a connection for **New ▸**. Tables open a designer — each
column a card with type, primary key, NOT NULL and default, and a live
`CREATE TABLE` preview. Everything else opens a query console with a
commented, dialect-correct skeleton.

**Drop…** on any object shows the exact statement before running it.
**Table Definition** opens a table's `CREATE` statement in an editable
buffer and generates whatever rebuild getting there requires.

**Open Schema** captures a whole database's structure as a replayable
`CREATE` script in a console — foreign keys ordered so cycles are not
a problem, and any definition the server refuses to hand over left as
a comment saying so rather than silently missing.

Nothing generated here runs until you press Run.

---

## Language servers

Keyword completion is built in. Install the right binary and
schema-aware completion is merged in automatically — see the engine
pages for which one.

To use a different server, drop an executable into
`~/.config/sqlide/lsp/` named after the connection kind (`postgres`,
`mysql`, `sqlite`, `jdbc`) or `default`. It is spawned with no
arguments, must speak LSP over stdio, and gets the connection in
environment variables: `SQLIDE_DB_KIND`, `SQLIDE_DB_NAME`,
`SQLIDE_DB_HOST`, `SQLIDE_DB_PORT`, `SQLIDE_DB_USER`,
`SQLIDE_DB_PASSWORD`, `SQLIDE_DB_DATABASE`, `SQLIDE_DB_FILE`,
`SQLIDE_DB_JDBC_URL`. A wrapper script covers servers needing flags:

```sh
#!/bin/sh
# ~/.config/sqlide/lsp/mysql
exec sql-language-server up --method stdio
```

A server that misbehaves is disabled for the session; keywords keep
working.

---

## Charts and dashboards

Any result has a **Chart** tab. sqlide infers a mapping from the
column types; a bar above changes it and a notice says what was
drawn and what was dropped. Selection runs both ways with the grid.

Charts draw the rows the grid holds. **Load all for chart** fetches
the rest up to `chart_max_rows`; past that the app says to aggregate
in SQL rather than quietly drawing a sample.

**Export Chart** gives PNG at a scale factor or SVG as real vectors,
with a size and a theme (light by default), or copies it to the
clipboard.

**Dashboards** put several saved queries on one refreshing grid. A
cell that fails becomes a sentence in that cell; the rest still draw.
Each dashboard is one TOML file under `dashboards/`.

---

## Maps

A `geometry`/`geography` column is hex in a grid and a map on the
**Map** tab. sqlide parses WKB and PostGIS EWKB itself — no PostGIS,
GDAL or shapely on your machine. Selection runs both ways with the
grid. Needs an engine that supports it *and* a spatial extension
actually installed.

Tiles come from someone else's server, so it is all visible: the URL
template is a setting, the attribution is always drawn, tiles are
cached on disk, and being offline is decided before a request — you
get your geometries on a plain background, not a hang. Tiles can be
turned off.

---

## Monitoring

**Monitoring** on a connection opens a live view of sessions,
throughput, cache hit ratio, locks and storage, with cancel and kill
where your account is allowed to.

A panel it cannot fill says *why* rather than showing zeros, and a
counter that goes backwards (a restart, a `pg_stat_reset()`) breaks
the line with a banner instead of drawing a spike that never happened.

---

## Backups

**Backups** in the sidebar menu manages real database dumps.

A **job** says what to back up (a connection, optionally one schema or
a chosen list of tables, schema and data or either), where it goes,
and how often. The dump uses the engine's own tool, and the exact
command is shown before it runs. The artifact is a plain SQL script,
gzipped by default, restorable with or without sqlide.

**Destinations** are a local folder, any S3-compatible bucket (AWS,
MinIO, R2, B2, Wasabi), SFTP, or FTP/FTPS. Each job keeps its newest N
there and prunes the rest — only files it wrote. Credentials go to the
keyring.

**Schedules** — the in-app one runs while sqlide is open and catches
up on one missed run; **Install a system timer** writes a systemd user
timer that runs with the app closed.

**Headless**, for your own cron:

```sh
sqlide-backup list      # jobs and destinations
sqlide-backup run nightly
sqlide-backup due       # only what is due
sqlide-backup history
```

**One-off Backup…** does it once, for *every* connection kind: a
vendor tool where one can be driven, otherwise a portable snapshot
read through the open connection — and the dialog says which and why.
A snapshot is structure and rows, no grants or storage settings.

**Restore…** goes the other way — pick an artifact, pick the
connection to restore *into*, confirm with the target's environment
marking spelled out. sqlide's own configuration is one more job kind.

---

## MCP server

Exposes a workspace's connections to an AI assistant, read-only.
Needs `pip install "sqlide[mcp]"`. Open it from the header bar's
network icon or a connection's menu.

Each instance is independent — its own connectors, its own port, its
own window — and nothing is persisted across restarts. The form asks
which connections to expose, a port (0 picks a free one), whether to
bind all interfaces, whether to enable the query tool at all, a row
limit, and a bearer token. Once started you get the URL, a copy-ready
client JSON snippet, and a live request log.

```json
{"mcpServers": {"sqlide-<workspace>": {
    "url": "http://127.0.0.1:PORT/mcp",
    "headers": {"Authorization": "Bearer <token>"}}}}
```

Three independent layers keep it read-only: a guard that admits only
one `SELECT`/`WITH`/`EXPLAIN` with no write keyword anywhere (data
-modifying CTEs included); a connection opened read-only in the driver
where possible; and a refusal to bind `0.0.0.0` without a token.

---

## Passwords

With the `keyring` extra and a backend running (GNOME Keyring,
KWallet, Keychain…), connection passwords, SSH passwords and backup
credentials go there and the workspace file keeps a placeholder.
Without one, sqlide falls back to plain text — it works either way,
but it is worth knowing which you have.

Keyring entries are per machine, so a copied workspace needs its
password entered once on the new machine.

---

## Moving machines

**Export Workspace…** and **Export Connections…** (Preferences →
General → Workspace Transfer) write a small readable XML file. The
folder button in the workspace list imports one as a new workspace;
**Import Connections…** merges into the open one. Passwords are left
out unless the export asks for them, and an import never overwrites
what is there.

---

## Files and settings

```
~/.config/sqlide/
├── settings.toml            global settings
├── notes.toml
├── dashboards/*.toml
├── table_templates/*.toml
├── lsp/                     language server plugins
└── workspaces/<id>/
    ├── workspace.toml       id, name, colour
    ├── connections.toml
    └── state.json           tabs, history, filters
```

TOML files are meant to be read, edited and committed — the writer
keeps your comments, and edits made while the app runs are noticed.
`state.json` is session state, not configuration.

`Ctrl+,` opens Preferences: theme, language, editor font size and Vim
mode, how much confirmation destructive actions demand, max result
rows, time zone, the SQL formatter's keyword case and indent, monitor
interval, LSP defaults, whether system schemas are shown, map tiles
and their URL, chart row cap, import batch size, and the keymap. Full
reference: [Configuration files](/docs/configuration/).

---

## Shortcuts

All rebindable in Preferences (`Ctrl+?` lists them). Keys the editor
owns — undo, clipboard, caret movement, Vim commands, the completion
popup — are reserved and cannot be taken.

| | |
|---|---|
| `Ctrl+,` / `Ctrl+?` / `F1` | preferences / shortcuts / help |
| `Ctrl+Shift+O` | switch workspace |
| `Ctrl+T` | new query console |
| `Ctrl+Alt+T` / `Ctrl+Alt+B` | CLI console / query builder |
| `Ctrl+Alt+M` | MCP server window |
| `Ctrl+Shift+H` | history |
| `F5` | refresh schema |
| `Ctrl+Alt+S` | split view |
| `Ctrl+F4` / `Ctrl+Shift+W` | close tab / close all tabs |
| `Ctrl+W` | close window |
| `Ctrl+Enter` / `Ctrl+Shift+Enter` | run statement / run all |
| `Ctrl+Shift+F` | format |
| `Ctrl+O` / `Ctrl+S` | open / save file |
| `Ctrl+C` | copy cells |

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| "No driver for this connection kind" | Install that engine's extra; sqlide names the package |
| `import gi` fails | GTK is a system package — a virtualenv must be created with `--system-site-packages` |
| Grid says "order not guaranteed" | The relation has no key to order by. Add an `ORDER BY` |
| Table is read-only | No primary key, or it is a view |
| A monitoring panel is empty | It says why underneath — usually a missing privilege |
| Only keyword completion | No language server installed, or the one that started misbehaved and was disabled for the session |
| Map shows a plain background | Tiles disabled, or the machine is offline |
| A password is not remembered | No keyring backend running, or the extra is not installed |
| Sidebar disagrees with reality | `F5` |
