# sqlide — Minimal SQL IDE

A minimal, clean SQL IDE (in the spirit of DBeaver/DataGrip, but basic) built
with Python, GTK4, and libadwaita. Supports SQLite, MySQL, and PostgreSQL
through a generic connector interface, plus a generic JDBC bridge for
anything else with a JDBC driver.

## Goals

- Connect to SQLite, MySQL, and PostgreSQL databases (JDBC as a generic escape hatch).
- Browse schemas: list tables, views, and their columns in a sidebar.
- View table data in a grid (paged).
- Edit data in the grid (cell edits committed via primary-key UPDATEs).
- Query console: type SQL, run it, see results in a grid.
- Minimal, clean libadwaita design. No plugins, no ER diagrams, nothing fancy.

## Non-goals (keep it basic)

- No visual query builder, ER diagrams, or DDL editors.
- No SSH tunnels, SSL config UIs, or exotic auth.
- No LSP-grade intellisense (keyword completion and SQL highlighting exist;
  nothing smarter).
- No multi-statement script tooling beyond "run what's in the console".

## Stack

| Concern      | Choice                                        |
|--------------|-----------------------------------------------|
| Language     | Python 3.12                                   |
| UI           | GTK4 + libadwaita (PyGObject)                 |
| SQLite       | `sqlite3` (stdlib)                            |
| MySQL        | `PyMySQL` (pure Python, easy install)         |
| PostgreSQL   | `psycopg` v3 (binary extra)                   |
| JDBC bridge  | `JayDeBeApi` (+ JPype, needs a Java runtime)  |
| Config       | JSON file in `$XDG_CONFIG_HOME/sqlide/`       |
| Packaging    | `pyproject.toml`, run with `python -m sqlide` |

MySQL/PostgreSQL/JDBC drivers are optional extras — the app runs with SQLite
alone and shows a friendly error if a driver for a chosen kind is missing.

## Architecture

Two top-level packages, strictly separated:

1. **`sqlide/backend/`** — pure Python, **no GTK imports allowed**. Database
   adapters, the connector interface, connection profiles and their JSON
   persistence. Unit-testable on its own; this separation is what makes the
   app generic.
2. **`sqlide/frontend/`** — all GTK4/libadwaita code. Talks to the backend
   only through the `Connector` interface and `connections` module, and
   always via a worker thread (`frontend/util.run_async`, which marshals
   results back to the main loop with `GLib.idle_add`) so the UI never blocks.

### Connector interface (`backend/db/base.py`)

```python
class Connector(ABC):
    def connect(self) -> None
    def close(self) -> None
    def list_tables(self) -> list[TableInfo]          # tables + views
    def list_columns(self, table) -> list[ColumnInfo] # name, type, pk, nullable
    def list_functions(self) -> list[FunctionInfo]    # concrete default: []
    def fetch_rows(self, table, offset, limit) -> ResultSet
    def execute(self, sql) -> ResultSet | int         # rows or affected count
    def update_cell(self, table, pk_values, column, value) -> None
    def quote_ident(self, name) -> str
```

Shared dataclasses: `TableInfo(name, kind)`, `ColumnInfo(name, type, is_pk,
nullable)`, `FunctionInfo(name)`, `ResultSet(columns, rows)`. All driver
errors are re-raised as `ConnectorError` with a readable message.
`list_functions()` has a concrete default returning `[]` so adapters without
a function catalog need no override; MySQL (`information_schema.routines`)
and Postgres fill it in. Postgres reports its full set of programmable
objects — PL/pgSQL (and other-language) functions, procedures (`pg_proc`)
and triggers (`pg_trigger`) — and `get_ddl()` reconstructs a runnable
CREATE for each (`pg_get_functiondef` / `pg_get_triggerdef` /
`pg_get_viewdef`, a synthesized `CREATE TABLE` for tables), so the
definition tab round-trips a PL/pgSQL body edit.

Each database is a **folder** under `backend/db/`, exposing its `Connector`
implementation from `connector.py`. Dialect differences (identifier quoting,
catalog queries, pagination syntax) live entirely inside each folder.
`registry.py` maps a kind string to its adapter and reports which optional
drivers are importable.

The **JDBC adapter** (`backend/db/jdbc/`) is the generic one: it bridges to
any JDBC driver jar via JayDeBeApi/JPype and gets its catalog information
from `java.sql.DatabaseMetaData` instead of dialect SQL. Pagination is
emulated client-side (no portable LIMIT/OFFSET). Experimental; requires a
JVM and the driver jar path in the profile.

### Workspaces and connections (`backend/workspaces.py`, `backend/connections.py`)

A **workspace** is the unit you open: it owns 0..n connection profiles and
remembers its open tabs (table tabs and query consoles, including console
SQL text and the selected tab), so reopening a workspace restores it as it
was left. It also keeps a query history capped at 200 entries. Each
workspace persists as its own JSON file in
`~/.config/sqlide/workspaces/<id>.json`.

- `Workspace` dataclass: `id, name, connections, tabs, selected_tab,
  history`. Connection names are deduplicated per workspace.
- `TabState` dataclass: `kind` ("table"/"query"), `connection`, `table`, `sql`.
- `HistoryEntry` dataclass: `sql, connection, timestamp` (ISO), `ok` —
  failed runs are recorded too; a missing `history` key defaults to `[]`
  so old files load fine.
- `WorkspaceStore`: one file per workspace; on first run it migrates the
  legacy flat `connections.json` (one workspace per old profile).

`ConnectionProfile` dataclass: `name, kind, file_path` (sqlite),
`host/port/user/password/database` (server DBs), `jdbc_url/driver_class/
jar_path` (JDBC). Profiles live inside their workspace's file (password
stored plainly for v1 — known limitation).

### UI layout (`frontend/`)

```
┌────────────────────────────────────────────────────────────────┐
│ HeaderBar   [+ Connection] [new query]        [history] [tabs] │
├───────────────┬───────────────────────────────┬────────────────┤
│ Schema tree   │  Adw.TabView + TabBar         │ History        │
│  ▸ conn A     │   ┌ Tab: "users" ───────────┐ │  (right panel, │
│    ▸ Tables   │   │ Gtk.ColumnView grid     │ │   hidden by    │
│      ▸ users  │   │ [◀ 1–500 ▶]  [refresh]  │ │   default)     │
│        id  PK │   └─────────────────────────┘ │  SELECT …      │
│    ▸ Views    │   ┌ Tab: "query · conn A" ──┐ │   conn A · 9:14│
│    ▸ Functions│   │ SQL editor [Run][conn ▾]│ │  SELECT …      │
│  ▸ conn B     │   │ results grid / status   │ │   conn B · 9:02│
└───────────────┴───────────────────────────────┴────────────────┘
```

- **`application.py`**: owns the `WorkspaceStore`; startup shows the
  workspace launcher, one main window per open workspace.
- **`launcher.py`**: small startup window listing workspaces; create a new
  one by name. Reopened from a main window's Workspaces button — this is
  the only place other workspaces are visible.
- **`window.py`**: `Adw.ApplicationWindow` for one workspace; a right
  `Adw.OverlaySplitView` (query history, hidden by default) wraps the left
  one (schema sidebar). Restores the workspace's saved tabs on open and
  saves them back on change/close. Owns a lock-guarded cache of open
  connectors (`ensure_connector()` — blocking, called only from worker
  threads), the shared `Gtk.StringList` of connection names that all query
  consoles' dropdowns observe, and the workspace history (records each
  console run, loads activated entries back into a console). With no tabs
  open, the content area shows a plain "Nothing Open" status message.
- **`sidebar.py`**: IDE-like schema tree — `Gtk.TreeListModel` +
  `Gtk.ListView` + `Gtk.TreeExpander`, shaped connection → Tables / Views /
  Functions → object → columns (columns show `name  type` + PK marker and
  are informational). Nodes load lazily on expansion via `run_async`;
  activating a table/view opens a data tab; a per-connection button opens
  a query console.
- **`data_grid.py`**: `ResultGrid` (a `Gtk.ColumnView` with columns built at
  runtime — reused everywhere results are shown) and `TableTab` (paged
  loading, refresh, PK-based cell editing via `Gtk.EditableLabel`). Tables
  without a primary key are read-only and say so.
- **`query_console.py`**: `SqlEditor` over a `ResultGrid`, Run button +
  Ctrl+Enter, status line for row counts and errors. Not tied to one
  connection: a toolbar dropdown picks the target from the workspace's
  connection names, resolved to a profile at run time (a console can exist
  with zero connections); every run is reported through `on_ran` for the
  history.
- **`sql_editor.py`**: editor widget behind a tiny interface
  (`get_text`/`set_text`); GtkSourceView 5 (SQL highlighting, line numbers,
  dark-aware scheme) when installed, otherwise a monospace `Gtk.TextView`
  with a small regex highlighter.
- **`completion.py`**: pluggable completion popup over any text view;
  keyword provider built in.
- **`history_panel.py`**: right-panel `Gtk.ListBox` over the workspace
  history, newest first — first SQL line as title, `connection · time` as
  subtitle, error marker on failed runs, clear button; activating a row
  loads it into a console.
- **`connection_dialog.py`**: kind dropdown; field group switches between
  SQLite (file chooser), server (host/port/…), and JDBC (url/driver/jar).
  Includes "Test connection".

## Project structure

```
sqlide/
├── PLAN.md
├── README.md
├── pyproject.toml
├── scripts/
│   └── make_demo_db.py        # builds demo.db for manual testing
└── sqlide/
    ├── __init__.py
    ├── __main__.py            # python -m sqlide
    ├── backend/               # NO GTK in here
    │   ├── __init__.py
    │   ├── connections.py     # ConnectionProfile dataclass
    │   ├── workspaces.py      # Workspace/TabState + per-file JSON store
    │   ├── settings.py        # global settings store (theme, font, vim…)
    │   ├── saved.py           # saved snippets/queries (global JSON stores)
    │   ├── placeholders.py    # :name / ? scanner + literal substitution
    │   ├── backup.py          # config zip export/restore
    │   └── db/
    │       ├── __init__.py
    │       ├── base.py        # Connector ABC + dataclasses + ConnectorError
    │       ├── registry.py    # kind -> adapter, driver availability
    │       ├── cli.py         # psql/mysql/sqlite-style meta-command engine
    │       ├── sqlite/
    │       │   ├── __init__.py
    │       │   └── connector.py
    │       ├── mysql/
    │       │   ├── __init__.py
    │       │   └── connector.py
    │       ├── postgres/
    │       │   ├── __init__.py
    │       │   └── connector.py
    │       └── jdbc/
    │           ├── __init__.py
    │           └── connector.py
    └── frontend/              # all GTK/libadwaita code
        ├── __init__.py
        ├── application.py     # Adw.Application, entry point
        ├── launcher.py        # workspace picker at startup
        ├── window.py          # one workspace: split view, tabs, connector cache
        ├── util.py            # run_async worker-thread helper
        ├── sidebar.py         # lazy schema tree (TreeListModel)
        ├── data_grid.py       # ResultGrid + TableTab
        ├── query_console.py
        ├── cli_console.py     # psql/mysql/sqlite-style CLI client tab
        ├── sql_editor.py      # GtkSourceView 5 with TextView fallback (+ Vim mode)
        ├── completion.py      # completion popup + keyword provider
        ├── history_panel.py   # query history list (a side panel page)
        ├── side_panel.py      # contextual right panel (Info/Snippets/…)
        ├── preferences.py     # preferences + about dialogs
        ├── backup_dialog.py   # Backup & Restore window
        ├── style.css
        └── connection_dialog.py
```

## Milestones

1. **Skeleton** — package layout, app + window run. — **done**
2. **DB layer** — `Connector` ABC, SQLite adapter complete, registry. — **done (unverified)**
3. **Connections** — profile store, connection dialog, sidebar. — **done (unverified)**
4. **Data grid** — table tabs, paged grid, refresh. — **done (unverified)**
5. **Query console** — run SQL, results/errors, Ctrl+Enter. — **done (unverified)**
6. **Editing** — editable cells, PK-based updates, error feedback. — **done (unverified)**
6b. **Workspaces** — workspace store (file per workspace), launcher,
   per-workspace connections, tab save/restore. — **done (unverified)**
6c. **Editor** — SqlEditor widget (GtkSourceView 5 + fallback), keyword
   completion. — **done**
6d. **Console/connection decoupling** — per-console connection dropdown
   over a shared name list, global "New query", consoles without
   connections. — **done**
6e. **Query history** — per-workspace capped history (failures included),
   right-side panel, activate-to-load. — **done**
6f. **Schema tree** — lazy TreeListModel sidebar (connection → Tables /
   Views / Functions → object → columns), `list_functions()` on the
   connector ABC. — **done**
7. **MySQL + PostgreSQL adapters** — MySQL (PyMySQL) and PostgreSQL
   (psycopg v3) implemented, including `list_functions()` and DDL for
   their programmable objects (Postgres: PL/pgSQL functions, procedures
   and triggers). JDBC adapter written but experimental/untested (needs
   JVM + jaydebeapi). — **done** (Postgres verified against the ABC and
   its transaction flow; live-server run pending a psycopg install).
7b. **CLI client console** — a psql/mysql/sqlite-style terminal tab
   (`frontend/cli_console.py` over `backend/db/cli.py`): meta-commands
   (`\dt`, `\d table`, `\l`, `\df`, `.tables`, `.schema`, both spellings
   accepted for any kind) answered from the connector catalog, plus SQL
   passthrough rendered as an aligned text table. Opened from the header
   bar (terminal icon) or a connection's context menu; persisted as a
   `cli` TabState. — **done**
8. **Polish** — connection edit/remove in the sidebar, empty-string vs NULL
   handling when editing, keyboard shortcuts, about dialog, close
   connectors on exit.
9. **Backlog round (2026-07)** — full-width header bar; split view
   capped at two panes; console results area hidden until a run, with
   a minimize/expand header; transaction controls in the console's
   bottom bar (Begin alone until a transaction opens); hover DDL
   tooltips in the console editor; placeholder (:name / ?) prompt with
   per-workspace remembered values; Vim mode setting
   (GtkSource.VimIMContext); Backup & Restore window (config zip);
   saved snippets and saved queries (global stores) and saved table
   filters (per workspace), surfaced in a contextual right side panel
   whose pages follow the active tab type. — **done**

"Unverified" = written but not yet executed; see README "Try it" for the
manual test path.

## Risks / known limitations (v1)

- **Nothing has been run yet** — expect small GTK API wrinkles on first
  launch (factory signal signatures, Adw widget availability by version).
- Cell edits are written back as **strings**; SQLite type affinity converts
  for typed columns, but there is no way yet to set a cell to NULL.
- Query console executes **one statement at a time** (sqlite3 `execute`).
- Passwords are stored in plain text in the config JSON.
- JDBC: client-side pagination is slow on big offsets; JPype thread
  attachment with the worker-thread model is untested.
- No connection or workspace edit/remove/rename UI yet — edit
  `~/.config/sqlide/workspaces/<id>.json` by hand for now.
