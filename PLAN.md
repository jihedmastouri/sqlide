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
- No autocomplete/intellisense in v1 (maybe keyword highlighting later).
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
    def fetch_rows(self, table, offset, limit) -> ResultSet
    def execute(self, sql) -> ResultSet | int         # rows or affected count
    def update_cell(self, table, pk_values, column, value) -> None
    def quote_ident(self, name) -> str
```

Shared dataclasses: `TableInfo(name, kind)`, `ColumnInfo(name, type, is_pk,
nullable)`, `ResultSet(columns, rows)`. All driver errors are re-raised as
`ConnectorError` with a readable message.

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
was left. Each workspace persists as its own JSON file in
`~/.config/sqlide/workspaces/<id>.json`.

- `Workspace` dataclass: `id, name, connections, tabs, selected_tab`.
  Connection names are deduplicated per workspace.
- `TabState` dataclass: `kind` ("table"/"query"), `connection`, `table`, `sql`.
- `WorkspaceStore`: one file per workspace; on first run it migrates the
  legacy flat `connections.json` (one workspace per old profile).

`ConnectionProfile` dataclass: `name, kind, file_path` (sqlite),
`host/port/user/password/database` (server DBs), `jdbc_url/driver_class/
jar_path` (JDBC). Profiles live inside their workspace's file (password
stored plainly for v1 — known limitation).

### UI layout (`frontend/`)

```
┌──────────────────────────────────────────────────┐
│ HeaderBar   [+ Connection]                       │
├───────────────┬──────────────────────────────────┤
│ Sidebar       │  Adw.TabView + TabBar            │
│  ▸ conn A     │   ┌ Tab: "users" ──────────────┐ │
│    users      │   │ Gtk.ColumnView data grid   │ │
│    orders     │   │ [◀ 1–500 ▶]     [refresh]  │ │
│  ▸ conn B     │   └────────────────────────────┘ │
│               │   ┌ Tab: "Query — conn A" ─────┐ │
│               │   │ SQL TextView   [Run ⏎]     │ │
│               │   │ results grid / status line │ │
└───────────────┴──────────────────────────────────┘
```

- **`application.py`**: owns the `WorkspaceStore`; startup shows the
  workspace launcher, one main window per open workspace.
- **`launcher.py`**: small startup window listing workspaces; create a new
  one by name. Reopened from a main window's Workspaces button — this is
  the only place other workspaces are visible.
- **`window.py`**: `Adw.ApplicationWindow` + `Adw.OverlaySplitView` for one
  workspace. Sidebar shows only that workspace's connections. Restores the
  workspace's saved tabs on open and saves them back on change/close. Owns
  a lock-guarded cache of open connectors (`ensure_connector()` — blocking,
  called only from worker threads). With no tabs open, the content area
  shows a plain "Nothing Open" status message.
- **`sidebar.py`**: `Gtk.ListBox` of `Adw.ExpanderRow`s, one per connection.
  Expanding connects and lists tables; activating a table opens a data tab;
  a per-connection button opens a query console.
- **`data_grid.py`**: `ResultGrid` (a `Gtk.ColumnView` with columns built at
  runtime — reused everywhere results are shown) and `TableTab` (paged
  loading, refresh, PK-based cell editing via `Gtk.EditableLabel`). Tables
  without a primary key are read-only and say so.
- **`query_console.py`**: monospace `Gtk.TextView` over a `ResultGrid`,
  Run button + Ctrl+Enter, status line for row counts and errors.
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
    │   └── db/
    │       ├── __init__.py
    │       ├── base.py        # Connector ABC + dataclasses + ConnectorError
    │       ├── registry.py    # kind -> adapter, driver availability
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
        ├── sidebar.py
        ├── data_grid.py       # ResultGrid + TableTab
        ├── query_console.py
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
7. **MySQL + PostgreSQL adapters** — stubs in place, to implement. JDBC
   adapter written but experimental/untested (needs JVM + jaydebeapi).
8. **Polish** — connection edit/remove in the sidebar, empty-string vs NULL
   handling when editing, keyboard shortcuts, about dialog, close
   connectors on exit.

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
