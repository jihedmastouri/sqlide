---
title: Architecture
description: How the backend and frontend are split, and where things live.
order: 11
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
    def list_routines(self, kind) -> list[FunctionInfo]  # one kind of routine
    def fetch_rows(self, table, offset, limit, ..., cursor) -> ResultSet
    def row_key_columns(self, table) -> list[str]     # what identifies a row
    def paging_strategy(self, table, order_by) -> PagePlan
    def execute(self, sql) -> ResultSet | int         # rows or affected count
    def update_cell(self, table, pk_values, column, value) -> None
    def quote_ident(self, name) -> str
```

Shared dataclasses: `TableInfo(name, kind)`, `ColumnInfo(name, type,
is_pk, nullable)`, `FunctionInfo(name)`, `ResultSet(columns, rows)`. All
driver errors are re-raised as `ConnectorError` with a readable message.

### Paging the grid

The grid reads a table one page at a time, and two pages of the same
table have to agree about where a row belongs — otherwise page two
repeats or skips rows and the user has no way to tell. So the adapter,
not the grid, decides how to page: `paging_strategy()` returns a
`PagePlan` saying what order it will apply and how it will walk it.

* The plan always orders. The user's sort comes first; the row key —
  `row_key_columns()`, the primary key by default, PostgreSQL's other
  total NOT NULL unique indexes, SQLite's `rowid` — is appended as a
  tiebreaker so the order is total.
* Where that order is unique-prefixed and uniform in direction, the
  page is a keyset page: `WHERE (k1, k2) > (v1, v2) … LIMIT n`, no
  OFFSET, carrying `ResultSet.cursor` forward from the previous page.
  Deep pages then cost what the first one did.
* Mixed ASC/DESC sorts, a key the projection does not carry (SQLite's
  `rowid`) and a jump to an arbitrary page number keep `OFFSET`, still
  in the deterministic order.
* A relation with no key at all — a view, a heap with no primary
  key — pages by offset in an order no engine guarantees. That is not
  papered over: the result comes back `stable=False` and the tab's
  status line says "order not guaranteed".

`ResultSet.statement` carries the statement that actually ran, values
inlined, so the tab's "describe query" line and the query history show
the tiebreaker and the key comparison rather than an idealised SELECT.

## The metadata provider

`backend/db/metadata.py` sits one level above the connector: it turns
"what does this engine's object tree look like" into an interface the
UI can walk without naming an engine.

```python
class MetadataProvider:
    def hierarchy(self) -> tuple[str, ...]   # the levels this engine has
    def capabilities(self) -> Capabilities   # feature flags
    def list_children(self, ref) -> list[NodeRef]
    def describe(self, ref) -> ObjectInfo    # the info view (db/objects.py)
    def property_sections(self) -> tuple[str, ...]   # the properties panel
    def table_properties(self, ref) -> ObjectInfo    # …and its descriptor
    def get_ddl(self, ref) -> str
    def list_grants(self, ref) -> list[PrivilegeInfo]
    def object_grants(self, ref) -> list[GrantEntry]  # who holds what here
    def list_principals(self) -> list[UserInfo]
    def permission_set(self, user, ref) -> PermissionSet   # the editor
    def permission_statements(self, user, current, desired) -> list[str]
    def apply_permissions(self, statements) -> None
    def qualified_name(self, ref) -> str       # "staging.orders" or "orders"
    def quoted_name(self, ref) -> str          # the same, per-part quoted
    def level_categories(level) -> tuple      # the folders a level shows
```

The folders hanging off a level — Tables and Sequences under a schema,
Extensions and Roles under a database, Administer under a connection —
are a declaration (`LEVEL_CATEGORIES`), so the sidebar grows an engine's
tree without naming it. Their rows come from `Connector.list_catalog`,
one shapeless listing per folder, and each row resolves to the same
generic info view every other node opens.

Where a node opens is decided by shape, not by a list of screens
(CORE-56). `objects.shape_of(kind)` answers "tabular" for a collection
— a folder, or one properties section of a table — and "scalar" for a
single record; `objects.grid_listing(kind, info)` turns that into the
listing a tab should draw in the shared `ResultGrid`, or None for the
info view. A kind that declares neither shape falls back to the
descriptor: it opens as a grid only where its whole body is one
tabular section with no summary and no DDL to lose. Nothing here
touches the right side panel, which keeps following the active tab
(CORE-47).

One implementation per engine, in that engine's folder
(`postgres/metadata.py`, …). PostgreSQL nests `connection → database →
schema → object`, MySQL `connection → database → object`, SQLite
`connection → object`; JDBC falls back to the generic provider. Each
declares a `Capabilities` — schemas, materialized views, procedures,
events, grants, roles, extensions, partitions, pragmas, permission
editor — so a screen an engine cannot fill is hidden instead of shown
broken. The permission editor is the fullest example: the provider says
which privileges an object kind can carry and how GRANT names it, so
`frontend/permission_editor.py` draws checkboxes for PostgreSQL and
MySQL alike without knowing either dialect, and SQLite never shows the
screen at all.

Those modules import nothing but `db.base`, which is what lets
`registry.capabilities(kind)` and `registry.hierarchy(kind)` answer
before a connection exists (or with the driver not installed):
`frontend/query_console.py` asks it whether to offer a database
switcher, and no UI module branches on the engine name.
Where a level exists, everything that names an object uses it. The
provider answers `qualified_name` / `quoted_name` for a `NodeRef` —
schema-first on PostgreSQL, the bare name on MySQL and SQLite — so tab
titles, breadcrumbs, headings and generated GRANT text agree without
any of them branching on the engine, and a reserved or capitalised name
is quoted one part at a time rather than as one string. The sidebar and
the console reach a schema the same way: a profile copy with `schema`
pinned (`frontend/sidebar.schema_profile`), whose connection puts that
schema on its search path, so bare names in existing catalog queries
and generated SQL resolve where the row says they do.
`registry.create_provider(kind, connector)` binds one to an open
connection. Everything a provider does is a catalog query, so it runs
on a worker thread like the connector underneath it.

The **JDBC adapter** (`backend/db/jdbc/`) is the generic escape hatch: it
bridges to any JDBC driver jar via JayDeBeApi/JPype and gets its catalog
information from `java.sql.DatabaseMetaData` instead of dialect SQL.
Pagination is emulated client-side. Experimental; requires a JVM and the
driver jar path in the profile.

## The geo viewer

A `geometry`/`geography` column is hex in a grid and a map everywhere
else. `backend/db/geo.py` parses WKB and PostGIS EWKB itself — no
PostGIS, GDAL or shapely on the client — so a cell can read *Point,
SRID 4326, 1 point* and a result can be drawn as features.
`frontend/map_view.py` draws them over slippy-map tiles, with selection
running both ways between the map and the grid.

Two gates keep it honest. The engine declares the `geometry`
capability, and the provider's `spatial_extension()` asks the server
whether a spatial extension is actually installed (since PG-05 that is
the extension registry's `spatial` feature, not a PostGIS lookup of
its own); a connection that answers "no"
never grows a Map toggle. And `backend/tiles.py` owns everything about
fetching somebody else's tiles: the URL template is a setting
(`map_tile_url`), the attribution travels with it and is always drawn,
tiles are cached on disk and re-used, and being offline is decided
*before* a request — so a disconnected machine gets geometries on a
plain background and a one-line notice instead of a hang. Nothing in
the test suite touches the network: the loader's transport and its
online probe are arguments.

## Extensions

`backend/db/extensions.py` is the registry: extension name -> what it
is called, what it is for, the *features* it unlocks and the types it
introduces. Everything above reads a feature (`spatial`, `statements`,
`hypertables`, `vectors`, `jobs`, `types`) and never an extension's
name, so a second spatial extension would be a registry entry and
nothing else. An extension nobody registered gets the generic trait:
it lists, it opens its info view, and it turns nothing on — no errors,
no special case.

One listing feeds all of it. `Connector.list_extensions()` returns
every extension the server has, installed or merely available, and the
provider splits it: the **Extensions** folder shows what is installed
with its version, schema and whether a newer version is on disk, and
**Available Extensions** shows the rest. The install/update/drop
actions build plain CREATE/ALTER/DROP EXTENSION for the confirmation
dialog to show (`frontend/extension_dialog.py`) and are offered only
where `can_manage_extensions()` says the account could run them —
review-then-run, like every other DDL surface here.

Extension-owned objects are attributed rather than mysterious: a
descriptor asks `Connector.extension_owner()` and an object that
belongs to an extension says so in its summary.

The per-extension UIs beyond that — TimescaleDB's chunks in the tree,
pg_cron's job list, vector columns rendered as vectors — are each
their own follow-up; the registry already declares which extension
brings which, so they hang off a feature flag rather than a new
mechanism.

## Workspaces and connections

A **workspace** is the unit you open: it owns zero or more connection
profiles and remembers its open tabs (table tabs and query consoles,
including console SQL text and the selected tab), so reopening a
workspace restores it as it was left. It also keeps a query history
capped at 200 entries. Each workspace is a directory of its own,
`workspaces/<id>/` in the config directory: `workspace.toml` (id, name,
colour), `connections.toml` (the connection definitions) and
`state.json` (tabs, history, filters — session state). See
[Configuration Files](configuration) for the full reference, and
`backend/config.py` for how the config directory is resolved.

## Layout

```
sqlide/
├── i18n.py                # gettext domain + locale-aware formatting
├── locale/                # compiled catalogues (built from po/ by `make i18n`)
├── backend/               # NO GTK in here
│   ├── config.py          # config dir resolution, TOML load, errors, watch
│   ├── tomlwrite.py       # comment-preserving TOML writer
│   ├── connections.py     # ConnectionProfile dataclass
│   ├── workspaces.py      # Workspace/TabState + per-workspace file store
│   ├── settings.py        # global settings store (settings.toml)
│   ├── saved.py           # saved snippets/queries
│   ├── notes.py           # free-form notes (notes.toml)
│   ├── secrets.py         # connection passwords: system keyring or plain text
│   ├── tiles.py           # map tiles: projection, disk cache, offline policy
│   ├── backup.py          # zip/restore of the config directory itself
│   ├── backups/           # database backups: jobs, dumps, destinations
│   │   ├── jobs.py        # Destination/Job/Run + backups.json store
│   │   ├── dump.py        # pg_dump / mysqldump / sqlite3 argv + streaming
│   │   ├── targets.py     # local, S3-compatible, SFTP, FTP(S)
│   │   ├── runner.py      # one job: dump -> upload -> prune -> record
│   │   ├── restore.py     # a dump back in through psql / mysql / sqlite3
│   │   ├── snapshot.py    # portable dump/restore through a Connector (JDBC, SSH)
│   │   ├── oneoff.py      # one backup now: picks vendor tool or snapshot
│   │   ├── schedule.py    # next-due maths + systemd user timers
│   │   └── cli.py         # `sqlide-backup`, the headless entry point
│   ├── mcp/                # the read-only MCP server
│   └── db/
│       ├── base.py         # Connector ABC + dataclasses + ConnectorError
│       ├── registry.py     # kind -> adapter, driver availability
│       ├── objects.py      # per-node object descriptors (the info view)
│       ├── metadata.py     # per-engine metadata providers (hierarchy, caps)
│       ├── geo.py          # WKB/EWKB -> drawable geometries (no PostGIS needed)
│       ├── extensions.py   # extension registry: features, types, DDL
│       ├── monitoring.py   # which monitoring sources a connection may read
│       ├── metrics.py      # sampling those sources: counters, sessions, sizes
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
    ├── tree_search.py       # sidebar search: matching, scopes, highlights
    ├── object_info.py       # info view for a node + the properties surface
    ├── users_tab.py         # accounts + privileges (review-then-run DDL)
    ├── permission_editor.py  # one principal: object tree + privilege grid
    ├── monitor_tab.py       # live dashboard: sessions, throughput, storage
    ├── backups_tab.py       # backup manager: jobs, schedules, run history
    ├── backup_destinations.py  # destination list + per-kind editor
    ├── backup_oneoff.py     # one-off backup dialog (every connection kind)
    ├── backup_restore.py    # pick artifact -> pick target -> confirm -> run
    ├── notes_panel.py       # side panel Notes page + Markdown editor
    ├── extension_dialog.py  # install/update/drop an extension, confirmed
    ├── side_panel.py        # right panel: Properties, Info, Notes, History…
    ├── data_grid.py         # ResultGrid + TableTab (Data | Map)
    ├── map_view.py          # geometries drawn on OpenStreetMap tiles
    ├── query_console.py
    ├── sql_editor.py        # GtkSourceView 5 with TextView fallback
    └── completion.py        # completion popup + keyword provider
```

## Translations

Every user-visible string in `frontend/` is marked with `_()` (or
`ngettext()` when it is counted, `N_()` when it has to be written
before the catalogue is bound). `sqlide/i18n.py` owns the gettext
domain and the locale-aware number, date and size formatters; `main()`
calls `install()` before the first widget exists, resolving the
language from `--language`, then `settings.toml`, then the system
locale, then English. Catalogues are edited in `po/` and compiled by
`make i18n`. See [Configuration Files](configuration#languages) for
how to add a language.
