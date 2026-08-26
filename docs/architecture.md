---
title: Architecture
description: How the backend and frontend are split, and where things live.
order: 9
---

Two top-level packages, strictly separated:

1. **`sqlide/backend/`** — pure Python, **no GTK imports allowed**.
   Database adapters, the connector interface, connection profiles, and
   their JSON persistence. Unit-testable on its own; this separation is
   what makes the app generic.
2. **`sqlide/frontend/`** — all GTK4/libadwaita code. Talks to the
   backend only through the `Connector` interface and `connections`
   module, and always via a worker thread
   (`frontend/util.run_async`, which marshals results back to the main
   loop with `GLib.idle_add`) so the UI never blocks.

## The connector interface

Each database is a folder under `backend/db/`, exposing a `Connector`
implementation from `connector.py`. Dialect differences — identifier
quoting, catalog queries, pagination syntax — live entirely inside each
folder. `registry.py` maps a kind string to its adapter and reports
which optional drivers are importable.

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

Shared dataclasses: `TableInfo(name, kind)`, `ColumnInfo(name, type,
is_pk, nullable)`, `FunctionInfo(name)`, `ResultSet(columns, rows)`. All
driver errors are re-raised as `ConnectorError` with a readable message.

The **JDBC adapter** (`backend/db/jdbc/`) is the generic escape hatch: it
bridges to any JDBC driver jar via JayDeBeApi/JPype and gets its catalog
information from `java.sql.DatabaseMetaData` instead of dialect SQL.
Pagination is emulated client-side. Experimental; requires a JVM and the
driver jar path in the profile.

## Workspaces and connections

A **workspace** is the unit you open: it owns zero or more connection
profiles and remembers its open tabs (table tabs and query consoles,
including console SQL text and the selected tab), so reopening a
workspace restores it as it was left. It also keeps a query history
capped at 200 entries. Each workspace persists as its own JSON file in
`~/.config/sqlide/workspaces/<id>.json`.

## Layout

```
sqlide/
├── backend/               # NO GTK in here
│   ├── connections.py     # ConnectionProfile dataclass
│   ├── workspaces.py      # Workspace/TabState + per-file JSON store
│   ├── settings.py        # global settings store (theme, font, vim…)
│   ├── saved.py           # saved snippets/queries
│   ├── secrets.py         # connection passwords: system keyring or plain text
│   ├── backup.py          # zip/restore of the config directory itself
│   ├── backups/           # database backups: jobs, dumps, destinations
│   │   ├── jobs.py        # Destination/Job/Run + backups.json store
│   │   ├── dump.py        # pg_dump / mysqldump / sqlite3 argv + streaming
│   │   ├── targets.py     # local, S3-compatible, SFTP, FTP(S)
│   │   ├── runner.py      # one job: dump -> upload -> prune -> record
│   │   ├── restore.py     # a dump back in through psql / mysql / sqlite3
│   │   ├── schedule.py    # next-due maths + systemd user timers
│   │   └── cli.py         # `sqlide-backup`, the headless entry point
│   ├── mcp/                # the read-only MCP server
│   └── db/
│       ├── base.py         # Connector ABC + dataclasses + ConnectorError
│       ├── registry.py     # kind -> adapter, driver availability
│       ├── sqlite/
│       ├── mysql/
│       ├── postgres/
│       └── jdbc/
└── frontend/               # all GTK/libadwaita UI
    ├── application.py      # Adw.Application entry point
    ├── welcome.py          # first-run home page
    ├── launcher.py         # in-app workspace switcher
    ├── window.py           # one workspace: split view, tabs, pop-outs, connectors
    ├── sidebar.py           # lazy schema tree (TreeListModel)
    ├── users_tab.py         # accounts + privileges (review-then-run DDL)
    ├── backups_tab.py       # backup manager: jobs, schedules, run history
    ├── backup_destinations.py  # destination list + per-kind editor
    ├── backup_restore.py    # pick artifact -> pick target -> confirm -> run
    ├── data_grid.py         # ResultGrid + TableTab
    ├── query_console.py
    ├── sql_editor.py        # GtkSourceView 5 with TextView fallback
    └── completion.py        # completion popup + keyword provider
```
