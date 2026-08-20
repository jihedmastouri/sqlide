# sqlide — Minimal SQL IDE

A minimal, clean SQL IDE (in the spirit of DBeaver/DataGrip, but basic) built
with Python, GTK4, and libadwaita. Supports SQLite, MySQL, and PostgreSQL
through a generic connector interface, plus a generic JDBC bridge for
anything else with a JDBC driver.

## Goals

- Connect to SQLite, MySQL, and PostgreSQL databases (JDBC as a generic escape hatch).
- Browse schemas: tables, views, routines, indexes, triggers and their columns.
- View table data in a grid (paged, filtered, sorted).
- Edit data in the grid — batches of inserts/updates/deletes staged, previewed,
  and applied in one transaction.
- Edit schema from the UI: create/alter tables, indexes and foreign keys, with
  per-dialect capability gating.
- Query console: multi-statement scripts, run all / current / selection,
  parameters, transaction control, history.
- Move data in and out: streaming export to files, import from files, native
  dump/restore.
- Minimal, clean libadwaita design; nothing that isn't pulling its weight.

## Non-goals

- **No cloud.** No hosted workspaces, no team sync, no per-item sharing or
  permissions, no accounts, no billing. Workspaces are local files; the user
  syncs them with their own tooling if they want to — `backend/exchange.py`
  exports one as portable XML for exactly that.
- **No embedded AI agent.** The read-only MCP server already exposes our
  databases to whatever agent the user runs, with better isolation and no
  provider SDK, API-key storage, or per-provider maintenance. That is the
  answer to "does it do AI", and it is a better one.
- **No engines beyond SQLite, MySQL/MariaDB and PostgreSQL.** JDBC stays an
  experimental escape hatch, not a supported path.
- **No telemetry.** Not now, not opt-in, not anonymised.
- No auto-update machinery, install channels, or license enforcement.
- No cloud-vendor auth methods (SSO/Entra/IAM) — follows from the engine scope.

Superseded non-goals, kept so nobody re-litigates them: the original plan ruled
out a query builder, ER diagrams, DDL editing, SSH tunnels, SSL configuration,
smarter-than-keyword completion, and multi-statement scripts. All seven now
exist. The rule that replaced them: a feature earns its place if it is something
a person does daily against a real database.

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

- **`application.py`**: owns the `WorkspaceStore`; startup opens the
  last-used workspace directly (or the first one on file), one main
  window per open workspace.
- **`welcome.py`**: the home page, shown only when no workspace exists
  yet — what the app is, what it does, and the name/colour form that
  creates the first one (or imports it).
- **`launcher.py`**: small window listing workspaces; create a new one by
  name. Opened from a main window's Workspaces button — this is the only
  place other workspaces are visible.
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
├── Makefile                   # venv, tests, app, compose servers, lint
├── pyproject.toml
├── docker-compose.yml         # throwaway MySQL/PostgreSQL servers
├── scripts/
│   ├── make_demo_db.py        # builds demo.db for manual testing
│   ├── init_databases.py      # replays the demo schema onto running servers
│   └── init/                  # that schema, per engine (also mounted by compose)
└── sqlide/
    ├── __init__.py
    ├── __main__.py            # python -m sqlide
    ├── backend/               # NO GTK in here
    │   ├── __init__.py
    │   ├── connections.py     # ConnectionProfile dataclass
    │   ├── identity.py        # colour palette + environment classes
    │   ├── sql_risk.py        # destructive-statement classifier + ladder
    │   ├── workspaces.py      # Workspace/TabState + per-file JSON store
    │   ├── settings.py        # global settings store (theme, font, vim…)
    │   ├── saved.py           # saved snippets/queries (global JSON stores)
    │   ├── placeholders.py    # :name / ? scanner + literal substitution
    │   ├── backup.py          # config zip export/restore
    │   ├── exchange.py        # portable XML workspace/connection transfer
    │   ├── secrets.py         # connection passwords: system keyring or plain text
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
        ├── welcome.py         # first-run home page
        ├── launcher.py        # in-app workspace switcher
        ├── window.py          # one workspace: split view, tabs, connector cache
        ├── util.py            # run_async worker-thread helper
        ├── identity.py        # palette provider + the coloured surfaces
        ├── confirm.py         # the destructive-action ladder's dialogs
        ├── feedback.py        # which surface carries which message
        ├── status_bar.py      # identity · context · jobs · status zones
        ├── shortcuts.py       # the keyboard shortcuts window
        ├── sidebar.py         # lazy schema tree (TreeListModel)
        ├── data_grid.py       # ResultGrid + TableTab
        ├── canvas.py          # palette + cairo primitives for the diagrams
        ├── plan_graph.py      # EXPLAIN output parsed and drawn as a tree
        ├── transfer.py        # the import/export file dialogs
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

10. **UI foundations and identity** — workspace and connection colours from a
    fixed palette whose 3:1 contrast (light, dark and both high-contrast
    extremes) and colour-blind separation are asserted by tests, generated into
    a `Gtk.CssProvider` at runtime and regenerated when the theme flips;
    development/staging/production environment classes that change how much
    friction destructive actions carry (confirm, then type-to-confirm, plus
    edit-lock re-arming on production); a persistent status bar that never
    shows a stale connection; one rule per feedback surface; empty states that
    distinguish an empty table from a filter matching nothing; a shortcuts
    window and an accessible label on every icon-only button. — **done**
    (the privacy-mode default and the read-only suggestion wait for the
    features they switch, in milestone 16.)

10b. **Backlog round (2026-08)** — grid pointer work (click-drag block
    selection, a cursor named after what a press will do, one column-header
    menu on both mouse buttons, a live selection summary in the Aggregate
    page); bulk tab closing from a tab's own menu, the main menu and
    ctrl+shift+w; "New ▸" on a left click in the sidebar; EXPLAIN drawn as a
    plan graph (`frontend/plan_graph.py`, sharing `frontend/canvas.py` with
    the relation graph); a `demo` database seeded onto every compose server
    from `sqlide/backend/demo/`; a Makefile over the project's routines;
    portable XML import/export of workspaces and connections
    (`backend/exchange.py`); the launcher handing the foreground to the
    workspace it opens. — **done**

10c. **Schemas, demo and saved structures (2026-08)** — PostgreSQL
    schema support: a profile's `schema` pins the connection's
    `search_path`, a console dropdown switches it, and name→relation
    resolution goes through `to_regclass()` so two schemas holding the
    same table name no longer merge into one phantom table with both
    sets of columns. The demo database moved into the package
    (`backend/demo/`, one `.sql` per dialect) as the single source for
    the app's own "Create demo database" button, the compose mounts and
    the seeding script. Saved schemas (`backend/schemas.py`): capture a
    database's structure under a name, reopen it in a console later;
    `Connector.schema_ddl()` per adapter, with PostgreSQL emitting
    foreign keys after the tables and MySQL bracketing its script with
    `SET FOREIGN_KEY_CHECKS` so cyclic references replay. — **done**

## Milestones — planned

Each phase is independently shippable. The detailed design notes behind them
are kept locally and are not part of this repository.
11. **Data editing completeness** — staged change sets (insert/update/delete)
    applied in one transaction, row add/clone/delete, spreadsheet paste, a
    modal editor for large/JSON values, editable query results, binary and
    enum/generated-column awareness.
12. **Schema editing** — a structured Structure tab replacing DDL-text editing:
    columns, indexes, foreign keys, per-dialect capability matrix, table
    properties. Includes fixing the SQLite rebuild path (see Risks).
13. **Data movement** — chunked cursors first, then streaming export
    (CSV/JSON/JSONL/SQL), multi-table export, run-to-file, and the file import
    pipeline.
14. **Editor maturity** — built-in schema-aware completion (so the LSP path is
    the better experience, not the only one), dialect-aware query parameters,
    formatter presets, manual-transaction safety net, result formatting
    directives.
15. **Navigation and workspace** — foreign-key navigation, record detail panel,
    sidebar filter/pin/hide, quick search, tab management, grid state
    persistence, connection URL import and sockets.
16. **Configuration and security** — layered TOML config with an administrator
    layer, configurable keybindings, read-only connections, PIN lock and
    auto-disconnect, privacy mode, SSH agent/config resolution.
17. **Backup/restore and plugins** — native `pg_dump`/`mysqldump`/`sqlite3`
    integration, then (lowest priority, only if warranted) a sandboxed plugin
    host.

## Risks / known limitations

Correctness bugs, to fix inside the milestone that touches them:

- **The SQLite table rebuild loses indexes and triggers.** `DROP TABLE` takes
  them with it and `rebuild_table_statements()` never recaptures them from
  `sqlite_master`. Any definition-tab edit that falls back to a rebuild
  silently drops them. Fix in milestone 12.
- **Result sets are fully materialised.** Every row of every result is read into
  Python lists, so a `SELECT *` against a large table will exhaust memory. The
  chunked cursor in milestone 13 is the fix; the row cap is the stopgap.
- Grid edits are applied as individual statements with no transaction boundary,
  so a failure mid-batch leaves a partial write. Fixed by milestone 11.

Standing limitations:

- Passwords are stored in plain text in the workspace JSON unless the `keyring`
  extra is installed and a backend is available (see README "Connection
  passwords"). Milestone 16 makes where-it-is-stored visible in the UI.
- JDBC: client-side pagination is slow on big offsets; JPype thread attachment
  with the worker-thread model is untested. Unsupported by policy.
- MySQL cannot roll back DDL — multi-statement schema changes are not atomic
  there, and the UI must say so rather than implying otherwise.
