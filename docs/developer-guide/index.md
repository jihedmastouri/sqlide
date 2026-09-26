---
title: Developer Guide
description: How sqlide is put together, how to build and test it, and how to add to it — for contributors and coding agents.
order: 100
---

This is the guide for changing sqlide. It covers the shape of the
codebase, the invariants that hold it together, the development
workflow, and step-by-step recipes for the changes people actually
make. If you only want to *use* the app, read the
[User Guide](/docs/user-guide/) instead.

[Architecture](/docs/architecture/) is the deeper reference for the
backend interfaces; this guide is the entry point and points into it.

---

## 1. Orientation

**What it is.** A GTK4 + libadwaita desktop SQL client, ~62k lines of
Python across two packages. No compiled extensions, no web view, no
ORM. Databases are reached through their own drivers (`sqlite3`,
PyMySQL, psycopg, JayDeBeApi) behind one interface.

**Repository layout**

```
sqlide/            the application package
  backend/         pure Python — NO GTK imports, ever
  frontend/        all GTK4 / libadwaita
  lsp/             LSP client and server management (no GTK)
  i18n.py          gettext domain + locale-aware formatting
  locale/          compiled catalogues, built from po/
tests/             pytest; ~74 modules
scripts/           demo database builders, server seeding
docs/              this documentation (the web/ site renders it)
web/               Astro site that publishes docs/
po/                translation catalogues
build-aux/         Flatpak manifest
data/              desktop entry, icons, metainfo
docker-compose.yml throwaway MySQL/PostgreSQL servers for tests
flake.nix          Nix dev shell and app
Makefile           every development entry point
```

**Where to start reading.** In order:

1. `sqlide/backend/db/base.py` — the `Connector` ABC, the shared
   dataclasses, the catalog cache, the paging plan. Everything
   database-shaped passes through here.
2. `sqlide/backend/db/metadata.py` — the `MetadataProvider`, which is
   how the UI walks an object tree without knowing which engine it is.
3. `sqlide/frontend/window.py` — one workspace: sidebar, tabs,
   connector cache. The hub every UI feature attaches to.
4. `sqlide/frontend/data_grid.py` — `ResultGrid` and `TableTab`, the
   widget reused by tables, query results and object listings.

---

## 2. Getting set up

### The Makefile is the interface

`make` on its own lists every target. The important ones:

```sh
make install       # venv + drivers + test extras, editable install
make run           # launch the app
make run-fresh     # launch against a throwaway config dir (a real first run)
make demo          # build demo.db
make test          # pytest (server tests skip if no server is up)
make test-sqlite   # only the tests that need no server
make check         # compile every module + import the GTK entry point
make lint          # ruff, read-only
make fmt           # ruff --fix
make servers       # docker: postgres16 + mysql8
make servers-all   # every version in docker-compose.yml
make init-db       # rebuild the demo schema on running servers
make i18n          # extract, merge and compile translation catalogues
make flatpak       # build the Flatpak
make web           # serve the docs site
make clean
```

### The one venv subtlety

GTK, libadwaita and PyGObject are **system packages**. A virtualenv
cannot pip-install them. So the venv is created with
`--system-site-packages`, and the drivers plus pytest are installed
into it. Everything — app, tests, scripts — runs on `.venv/bin/python`.

Running the app on the system `python3` directly would find GTK but
none of the drivers. Running it in a plain venv would find the drivers
but not GTK. `make` gets this right; if you set things up by hand,
this is the thing to get right.

`uv` is used automatically when it is on `$PATH`.

### Nix

`nix develop` gives a shell with GTK, the drivers and the dev tools
already there; `nix run .` launches the app. This is the shortest path
to a working environment on a machine where the system packages are
awkward.

### Test servers

`docker-compose.yml` runs PostgreSQL 10–16 and MySQL 5.7/8.0 on fixed
ports, all with user/password `sqlide`/`sqlide`. Each holds two
databases: `sqlide`, which the tests reseed, and `demo`, the sample
schema.

```sh
make servers                      # one recent version of each
docker compose up -d postgres14   # or just one
make init-db                      # rebuild demo on what is running
```

You do not need them to work on most things — `make test-sqlite` is a
full pass over everything that does not need a server.

---

## 3. The two-package rule

This is the invariant everything else rests on.

> **`sqlide/backend/` must not import GTK.** Not `gi`, not
> `gi.repository`, not indirectly.

The backend is pure Python: adapters, profiles, persistence, the SQL
utilities, the MCP server, the backup machinery. It is unit-testable
without a display, and it is what makes the app generic rather than a
pile of dialogs with SQL in them.

`sqlide/frontend/` is all the GTK. It reaches the backend only through
`Connector`, `MetadataProvider` and the store modules.

`sqlide/lsp/` is a third, smaller pure-Python package: the LSP client
and per-connection server management. `frontend/lsp_completion.py` is
the editor-facing bridge.

**How to check you did not break it:**

```sh
grep -rn "import gi\|gi\.repository" sqlide/backend sqlide/lsp
```

should print nothing.

### Threading

> **Every database call happens on a worker thread.**

`frontend/util.run_async` runs a callable off the UI thread and
marshals the result back with `GLib.idle_add`. The window's
`ensure_connector()` is a *blocking* accessor — it is handed to child
widgets on the explicit understanding that they only call it from
inside a `run_async` worker.

Anything a `MetadataProvider` does is a catalog query, so it lives
under the same rule as the connector beneath it.

Symptoms of getting this wrong are a frozen UI on a slow server, and
GTK warnings or crashes from touching widgets off the main loop.

---

## 4. The backend

### The connector

Each engine is a folder under `backend/db/` exporting a `Connector`
from `connector.py`. Dialect differences — identifier quoting, catalog
queries, pagination syntax — live entirely inside that folder.
`registry.py` maps a kind string to its adapter and reports which
optional drivers are importable.

```python
class Connector(ABC):
    def connect(self) -> None
    def close(self) -> None
    def list_tables(self) -> list[TableInfo]
    def list_columns(self, table) -> list[ColumnInfo]
    def list_functions(self) -> list[FunctionInfo]
    def list_routines(self, kind) -> list[FunctionInfo]
    def fetch_rows(self, table, offset, limit, ..., cursor) -> ResultSet
    def row_key_columns(self, table) -> list[str]
    def paging_strategy(self, table, order_by) -> PagePlan
    def execute(self, sql) -> ResultSet | int
    def update_cell(self, table, pk_values, column, value) -> None
    def quote_ident(self, name) -> str
```

All driver errors are re-raised as `ConnectorError` with a readable
message. That is the only exception type the frontend should have to
know about.

Two subsystems in `base.py` deserve reading in full in
[Architecture](/docs/architecture/):

- **The catalog cache.** Per-connector, keyed by `(kind, database,
  schema, name)`, invalidated *whole* on every observable event rather
  than by TTL — because a clock cannot know when someone ran an
  `ALTER`. Critically, validation never rejects from cache alone: a
  name the cache does not know is re-read from the server once before
  an error is raised, so a table created in another session costs a
  round trip, not a failure.
- **Paging.** The *adapter* decides how to page, not the grid, so two
  pages of one table agree about where a row belongs. Keyset paging
  where the order is unique-prefixed and uniform; `OFFSET` where it is
  not; `stable=False` and a visible "order not guaranteed" where the
  relation has no key at all.

### The metadata provider

`backend/db/metadata.py` sits above the connector and answers "what
does this engine's object tree look like" in a form the UI can walk
without naming an engine. One implementation per engine, in that
engine's folder.

Two mechanisms carry most of the weight:

- **`Capabilities`** — feature flags (schemas, materialized views,
  procedures, events, grants, roles, extensions, partitions, pragmas,
  permission editor, geometry). A screen an engine cannot fill is
  *hidden*, never shown broken. `registry.capabilities(kind)` and
  `registry.hierarchy(kind)` answer before a connection exists, and
  even with the driver not installed.
- **`LEVEL_CATEGORIES`** — the folders hanging off each level are a
  declaration, not code, so the sidebar grows an engine's tree without
  naming it.

Hierarchies differ: PostgreSQL nests `connection → database → schema →
object`, MySQL `connection → database → object`, SQLite `connection →
object`. JDBC falls back to the generic provider, getting its catalog
from `java.sql.DatabaseMetaData` rather than dialect SQL.

> **No UI module branches on the engine name.** If you find yourself
> writing `if kind == "postgres"` in `frontend/`, the answer belongs
> in a capability, a provider method or the extension registry.

### Where a node opens

Decided by *shape*, not by a list of screens.
`objects.shape_of(kind)` answers "tabular" for a collection and
"scalar" for a single record; `objects.grid_listing(kind, info)` turns
that into a listing for the shared `ResultGrid`, or `None` for the
info view. A kind that declares neither falls back to its descriptor.

### The rest of the backend

| Module | Responsibility |
|---|---|
| `config.py` | Config directory resolution, TOML load, errors, file watching |
| `tomlwrite.py` | Comment-preserving TOML writer |
| `connections.py` | `ConnectionProfile` |
| `workspaces.py` | `Workspace` / `TabState` + the per-workspace file store |
| `settings.py` | Global settings (`settings.toml`) |
| `secrets.py` | Passwords: system keyring, or plain text fallback |
| `saved.py` | Saved scripts and snippets, with their pins |
| `dashboards.py` | Dashboard definitions (one TOML per dashboard) |
| `charts.py` | `ChartSpec`, column classification, rows → series |
| `exchange.py` | The portable XML transfer format |
| `export.py`, `importer.py` | Streaming export and import |
| `sql_split.py` | Statement splitting |
| `sql_format.py` | The formatter |
| `sql_risk.py` | Is this statement a read, a row change, or DDL? |
| `table_templates.py` | Table templates |
| `placeholders.py` | Query parameters |
| `ssh.py` | SSH tunnels |
| `identity.py` | Connection identity / environment marking |
| `schemas.py` | Schema helpers |
| `tiles.py` | Map tiles: projection, disk cache, offline policy |
| `backup.py` | Zip/restore of the config directory itself |
| `backups/` | Database backups — see below |
| `mcp/` | The read-only MCP server |
| `db/geo.py` | WKB / PostGIS EWKB parsing, no PostGIS on the client |
| `db/extensions.py` | Extension registry: features, types, DDL |
| `db/monitoring.py`, `db/metrics.py` | Monitoring sources and sampling |
| `db/search.py` | Data search decisions (pure, tested without a server) |
| `db/query_model.py` | The query builder's model and renderer |
| `db/table_model.py` | The table designer's model |
| `db/cli.py` | The CLI console's interpreter (meta-commands + SQL) |

**`backups/`** is worth its own note because it is the most
subsystem-shaped part of the backend: `jobs.py` (the store),
`dump.py` (vendor-tool argv and streaming), `targets.py` (local, S3,
SFTP, FTP), `runner.py` (dump → upload → prune → record),
`restore.py`, `snapshot.py` (the portable fallback through a
`Connector`), `oneoff.py`, `schedule.py` (next-due maths + systemd
timers) and `cli.py` — the `sqlide-backup` entry point, which is a
real headless interface and must keep working without any GTK.

---

## 5. The frontend

`application.py` is the `Adw.Application` entry point. `welcome.py` is
the first-run home page, `launcher.py` the workspace switcher, and
`window.py` is one workspace: sidebar, tabs, split view, pop-outs, and
the cache of open connectors.

Read `window.py`'s docstring before adding a tab type — it documents
the contracts around disconnect (tabs stay open behind a Reconnect
banner), close-all-related-tabs (one confirmation listing unsaved
work), and tab persistence (open tabs are part of the workspace and
are saved whenever they change).

**Shared widgets you should reuse rather than reinvent:**

| Module | What it gives you |
|---|---|
| `data_grid.py` | `ResultGrid` — a `Gtk.ColumnView` built from a result set, with sorting, reordering, selection, copy, export and optional in-place editing. Also `TableTab`. |
| `side_panel.py` | The right panel. Which pages exist is decided by the active tab's context (`"console"`, `"table"`, `"grid"`, `"other"`) via `set_context`. |
| `canvas.py` | Cairo primitives and the shared palette for `relation_graph.py` and `plan_graph.py` |
| `chart_canvas.py` | The chart renderer — a pure function of (spec, data, context, size), which is why PNG/SVG export is the same call |
| `confirm.py` | Destructive-action confirmation, honouring the environment marking and the confirm-mode setting |
| `feedback.py` | Toasts and error surfaces |
| `util.py` | `run_async` and friends |
| `keymap.py` | The single registry of every shortcut |
| `object_info.py` | The generic info view for any node |

**The keymap.** `frontend/keymap.py` is the *only* place a shortcut is
defined. It backs the Gio action accelerators, the ad hoc key
controllers in the console and the grid, the shortcuts window and the
Preferences editor — so they cannot drift. An `Action` has an id, a
label, a group, a default accelerator and a `scope` (`"app"`, `"win"`
or `"local"`). `RESERVED` holds the keys the editor owns outright
(undo, clipboard, caret movement, the Vim command keys, the completion
popup's navigation); those can never be assigned to an app action.

---

## 6. Design principles to preserve

These are not style preferences; breaking one usually means a user
gets lied to.

1. **Review then run.** Generated DDL, grants, revokes and drops are
   shown before they execute — usually by opening in a query console.
   The exceptions are deliberate and few (the table designer, whose
   preview is live). If you add a mutating action, ask what the user
   sees before it happens.
2. **Say why, don't show empty.** A panel that cannot be filled
   explains itself. `metrics.py` goes as far as breaking a line where
   a counter reset rather than drawing a spike that never happened.
3. **Capabilities, not engine names.** See §4.
4. **Registries, not special cases.** A second spatial extension
   should be a row in `db/extensions.py` and nothing else. Features
   (`spatial`, `statements`, `hypertables`, `vectors`, `jobs`,
   `types`) are what code reads; extension names are data.
5. **Config is text you can commit.** TOML, comment-preserving on
   write, hand-editable, watched while running. `state.json` is the
   one exception — it is session state, not configuration.
6. **Secrets are opt-in but never surprising.** Keyring when
   available; plain text otherwise, but the app is explicit about
   which you got. Exports omit passwords unless asked.
7. **The network is never assumed.** Map tiles decide offline *before*
   a request. Nothing in the test suite touches the network — the tile
   loader's transport and its online probe are arguments.
8. **Failures are partial, not total.** A dashboard cell that fails is
   a sentence in that cell; the sweep continues. A schema capture that
   cannot read one definition emits a comment saying so.

---

## 7. Testing

```sh
make test          # everything; server tests skip if nothing is up
make test-sqlite   # -k "not postgres and not mysql"
```

`tests/conftest.py` parametrizes the `postgres` and `mysql` fixtures
over the versions in `docker-compose.yml`, so a test using one runs
once per running version. **A server that is not up, or whose driver
is not installed, skips rather than fails** — that is deliberate, so
starting a subset tests just that subset. Each fixture connects,
reseeds a small fixed schema (users, orders, a view, a stored
function) and yields `(version, connector)`.

**What good tests look like here.** The suite is heavily weighted to
the backend, because the backend was designed to be testable without a
display or a server:

- Pure logic gets a plain unit test: `test_sql_split.py`,
  `test_sql_risk.py`, `test_sql_format.py`, `test_geo.py`,
  `test_charts.py`, `test_query_model.py`, `test_search.py`.
- Per-engine behaviour gets a fixture test: `test_postgres.py`,
  `test_mysql.py`, `test_paging.py`, `test_apply_changes.py`.
- Tree shape gets its own module per engine: `test_pg_tree.py`,
  `test_my_tree.py`, `test_sq_tree.py`.
- Design decisions that would be easy to regress get a test that reads
  like the decision — `test_monitoring.py` is explicitly the
  monitoring spike's evidence kept runnable; `test_metrics.py` asserts
  a reset must not draw a negative spike.

When you add behaviour, prefer putting the decision in a pure backend
module and testing it there over testing it through a widget.

**Before pushing**, the cheap full check is:

```sh
make lint && make check && make test
```

`make check` compiles every module and imports the GTK entry point —
it catches the import-order and syntax mistakes a headless test run
would not.

---

## 8. Recipes

### Add a database engine

1. Create `backend/db/<kind>/` with `connector.py` and
   `metadata.py`.
2. Implement `Connector`: connect/close, the `list_*` catalog reads,
   `fetch_rows`, `row_key_columns`, `paging_strategy`, `execute`,
   `update_cell`, `quote_ident`. Wrap every driver error as
   `ConnectorError`.
3. Implement `MetadataProvider`: `hierarchy()`, `capabilities()`,
   `list_children()`, `describe()`, the naming helpers. Declare only
   the capabilities you can actually fill.
4. Register the kind in `db/registry.py`, including its driver-import
   probe so a missing driver produces a message rather than a
   traceback.
5. Add the optional dependency to `pyproject.toml` (its own extra and
   `all`).
6. Add a tree test (`test_<kind>_tree.py`) and, if it has a server, a
   fixture in `conftest.py` and a service in `docker-compose.yml`.
7. Nothing in `frontend/` should need to change. If it does, that is
   the bug.

### Add a capability-gated screen

1. Add the flag to `Capabilities` in `backend/db/metadata.py` and set
   it on each engine's provider.
2. Add whatever provider methods the screen needs — all of them
   catalog queries.
3. Build the widget in `frontend/`, asking the capability (never the
   engine name) whether to offer it.
4. Call every backend method through `run_async`.
5. Give the tab a side-panel context in `set_context` if it should
   offer Properties/Aggregation/Record/History.

### Add a keyboard shortcut

Add an `Action` to `ACTIONS` in `frontend/keymap.py` with the right
`scope`, then wire it: a Gio action for `"app"`/`"win"`, or a
`matches()` check in the widget's key controller for `"local"`. It
shows up in the shortcuts window and Preferences automatically. Do not
hard-code an accelerator anywhere else, and do not use a key in
`RESERVED`.

### Add a setting

Add the field with its default to the settings dataclass in
`backend/settings.py`, surface it in `frontend/preferences.py`, and
document it in `docs/configuration.md`. Read it through the store, not
by parsing the file.

### Add a chart type or an export format

Charts: the spec and the rows→series work go in `backend/charts.py`;
the drawing goes in `frontend/chart_canvas.py`. Because drawing is a
pure function of (spec, data, context, size), PNG and SVG export come
for free through `chart_image.py`.

Exports: `backend/export.py` streams; the dialog in
`frontend/export_dialog.py` asks which rows, what shape, and where,
with a live preview. Keep the preview honest — it is the whole point
of the dialog.

### Add an extension

One row in `backend/db/extensions.py`: name, description, the features
it unlocks, the types it introduces. Code elsewhere reads the feature,
never the name.

### Add a backup destination kind

Implement it in `backend/backups/targets.py` (upload, list, delete,
so pruning works), add its editor to
`frontend/backup_destinations.py`, and put its credentials through
`backend/secrets.py`. It must work from `sqlide-backup` with no GTK
loaded.

---

## 9. Translations

Every user-visible string in `frontend/` is marked with `_()`, or
`ngettext()` when it is counted, or `N_()` when it must be written
before the catalogue is bound.

`sqlide/i18n.py` owns the gettext domain and the locale-aware number,
date and size formatters. `main()` calls `install()` before the first
widget exists, resolving the language from `--language`, then
`settings.toml`, then the system locale, then English.

After touching a user-visible string:

```sh
make i18n
```

That re-extracts `po/sqlide.pot`, merges it into each `po/<lang>.po`
(so a translator keeps their work and sees what is new) and compiles
into `sqlide/locale/`. The `.mo` files are committed, so a plain `pip
install .` ships them.

Adding a language is documented in
[Configuration files](/docs/configuration/#languages).

---

## 10. Style and conventions

- **Ruff** is the linter and formatter of record: `make lint`,
  `make fmt`. The only per-file ignores are two `E402`s where
  `gi.require_version()` must run before `gi.repository` is imported.
- `from __future__ import annotations` at the top of every module.
- Dataclasses for anything with a shape; `frozen=True` where it is
  really a value.
- **Module docstrings carry the design.** This codebase explains *why*
  at the top of a module rather than in scattered inline comments —
  read `data_grid.py`, `keymap.py` or `window.py` for the house style.
  A new module should open the same way: what this is, what it is
  responsible for, and what decision it encodes. Match the surrounding
  comment density rather than adding more.
- Names in generated SQL are quoted per part through the provider
  (`quoted_name`), never by string concatenation.
- Filter and parameter values are **bound**, not interpolated.

---

## 11. Packaging and distribution

- **pip** — `pyproject.toml`, setuptools. Extras: `mysql`, `postgres`,
  `jdbc`, `ssh`, `mcp`, `keyring`, `s3`, `all`, `test`. Two console
  scripts: `sqlide` (the app) and `sqlide-backup` (headless).
  Package data ships the compiled catalogues, `frontend/style.css` and
  the demo `.sql` files.
- **Nix** — `flake.nix` provides the app and the dev shell.
- **Flatpak** — `build-aux/flatpak/dev.jihed.sqlide.yml`, built with
  `make flatpak`.
- **Desktop integration** — `data/` holds the desktop entry, icons and
  AppStream metainfo.

The demo database lives in `sqlide/backend/demo/`, one `.sql` file per
dialect. Those files are the single source for all three ways in: the
app's own **Create demo database** button,
`scripts/make_demo_db.py` (SQLite, from the command line), and
`scripts/init_databases.py` plus the `docker-compose.yml` mounts.

---

## 12. Notes for coding agents

- **`make check` before you claim anything works.** It compiles every
  module and imports the GTK entry point. A headless test run does not
  catch a broken `frontend/` import.
- **The GTK parts cannot be verified headlessly.** If a change is in
  `frontend/`, say so plainly rather than implying you ran it. `make
  run` needs a display.
- **Server tests skip silently.** A green `make test` with no
  containers running means the SQLite and pure-logic tests passed and
  nothing else did. Start `make servers` if your change touches MySQL
  or PostgreSQL, and say which you actually exercised.
- **Read the module docstring before editing a module.** They encode
  decisions — invalidation policy, paging guarantees, threading
  contracts — that are not re-derivable from the code beneath them.
- **Do not branch on the engine name in `frontend/`.** Add a
  capability. This is the single easiest way to make a mess here.
- **Do not import GTK into `backend/` or `lsp/`**, however convenient
  it looks. The MCP server and `sqlide-backup` both run with no
  display.
- **Prefer putting a decision in a pure backend module** and testing
  it there. That is why so much of this app is testable at all.
- Config paths in tests go through `backend/config.py`, never
  `~/.config` directly.
