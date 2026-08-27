---
title: DBeaver Comparison
description: Feature-by-feature against DBeaver, the gaps that matter for someone switching, and the tickets that close them.
order: 17
---

This is the write-up of RS-04. It is research, not implementation. It
compares sqlide as it stands today against DBeaver Community (with a
note wherever a feature is DBeaver Enterprise only), answers the three
questions on the ticket, and files the follow-up tickets
(CORE-36 … CORE-45) worth acting on.

Every claim about sqlide below was checked against the code and cites
the file. Where sqlide does not have something, it says so plainly
rather than describing an adjacent feature as if it counted.

The one-line answer: **sqlide is closer to DBeaver than its own
README claims, and the gap that actually hurts a switcher is not
breadth — it is the data grid.** DBeaver's grid is a full editor with
row insert/delete, a value panel, foreign-key navigation and export to
a file. sqlide's grid edits one cell at a time, cannot add or remove a
row, and cannot write a result anywhere but the clipboard. Three of
the four smallest tickets here fix that.

## Scope, and what is deliberately not compared

sqlide targets SQLite, MySQL and PostgreSQL, plus a generic JDBC
escape hatch (`backend/db/jdbc/`). DBeaver targets roughly ninety
drivers including NoSQL and cloud warehouses. That is a product
decision, not a gap, and nothing below counts it as one. Everywhere
DBeaver's breadth shows up as *depth* — a better PostgreSQL screen,
not a Cassandra one — it is fair game and is compared.

## The matrix

Legend: **yes** = shipped and grounded in a cited file; **partial** =
exists but materially narrower than DBeaver's; **no** = absent.

### Browsing and metadata

| Feature | DBeaver | sqlide | Notes |
|---|---|---|---|
| Object tree, lazy | yes | **yes** | `frontend/sidebar.py`, lazy `TreeListModel`; full node sets for PG/MySQL/SQLite (PG-02, MY-01, SQ-01) |
| Engine-agnostic object model | internal | **yes** | `backend/db/metadata.py` — `MetadataProvider`, `Capabilities`, `NodeRef`; no UI module branches on engine name |
| Object properties/info screens | yes | **yes** | `backend/db/objects.py` (1011 lines of descriptors), `frontend/object_info.py` |
| DDL of any object | yes | **yes** | `Connector.get_ddl`, shown in the side panel's Info page (`frontend/side_panel.py`) |
| Sidebar search/filter | yes | **yes** | `frontend/tree_search.py`, with scopes and match highlighting |
| ER diagram | yes | **yes** | `frontend/relation_graph.py` — cairo canvas, Graphviz only for initial placement |
| Edit the schema *through* the ER diagram | yes | **no** | Ours is read-only; see "Not filed" |
| Dim system schemas | yes | **yes** | PG-03, `show_system_schemas` setting |
| Extension awareness | partial | **yes** | `backend/db/extensions.py` — features, not names; install/update/drop, review-then-run |
| SQLite PRAGMA editor | partial | **yes** | `backend/db/sqlite/pragmas.py`, `frontend/pragmas_tab.py` — better than DBeaver's |

### The data grid

| Feature | DBeaver | sqlide | Notes |
|---|---|---|---|
| Paged browsing | yes | **yes** | `PAGE_SIZE = 500`, `frontend/data_grid.py:84`; infinite-scroll append on edge |
| Filter builder | yes | **yes** | `_FilterRow`, whitelisted operators, parameter-bound (`base.build_filter_clauses`) |
| Saved filters | yes | **yes** | `Workspace.saved_filters`, and they survive an XML export (`backend/exchange.py`) |
| Multi-column sort | yes | **yes** | `_SortRow` + header sort, both feeding `order_by` |
| Column reorder / hide | yes | **partial** | Reorder by header drag (`_begin_header_drag`); no hide |
| Inline cell edit | yes | **yes** | `update_cell`, PK-addressed, preview dialog before write |
| **Insert a row** | yes | **no** | → CORE-38 |
| **Delete a row** | yes | **no** | → CORE-38 |
| **Edits as one transaction** | yes | **no** | N separate `UPDATE`s; a failure leaves a partial write (`_execute_updates` reloads "with whatever was applied") → CORE-39 |
| Set NULL | yes | **yes** | `_on_set_null` |
| Cell/block/column selection | yes | **yes** | `_select_block`, drag-select, row and column select |
| Aggregate of the selection | yes | **yes** | `_aggregate_lines` + the side panel's Aggregate page |
| Copy as CSV/JSON/Markdown/INSERT | yes | **yes** | `_format_csv`, `_format_json`, `_format_markdown`, `_format_insert` |
| **Export to a file** | yes | **no** | Clipboard only; nothing writes rows to disk → CORE-36 |
| **Import from a file** | yes | **no** | → CORE-37 |
| **Value panel (long text, JSON, blob)** | yes | **no** | A long cell is an ellipsized label; binary is hex (`_hex`) → CORE-42 |
| **Record (single-row) view** | yes | **no** | → CORE-42 |
| **Foreign-key navigation** | yes | **no** | `Connector.list_relations` / `list_references` exist and feed the ER diagram; the grid does not use them → CORE-43 |
| Geometry as a map | EE plugin | **yes** | `backend/db/geo.py` parses WKB/EWKB with no PostGIS client; `frontend/map_view.py` |
| Binary/encoding handling | yes | **yes** | `is_binary`, explicit hex; encoding and time zone handled deliberately (`tests/test_encoding.py`) |

### The SQL editor

| Feature | DBeaver | sqlide | Notes |
|---|---|---|---|
| Highlighting | yes | **yes** | GtkSourceView 5, `frontend/sql_editor.py`, with a TextView fallback |
| Run statement / run script | yes | **yes** | Ctrl+Enter runs the statement under the cursor, Ctrl+Shift+Enter the buffer |
| Correct statement splitting | yes | **yes** | `backend/sql_split.py` — dollar quoting, `BEGIN…END` nesting, `DELIMITER`, per-dialect backslash rules |
| One result tab per statement | yes | **yes** | Plus a Status tab with per-statement timing |
| Explain | yes | **yes** | Graph / Table / JSON; `frontend/plan_graph.py` parses all three shapes EXPLAIN answers in (CORE-16) |
| Schema-aware completion | yes | **yes** | LSP (`sqlide/lsp/`, postgres-language-server or sqls) merged with keyword completion |
| DDL tooltip on hover | no | **yes** | `_on_editor_tooltip` — DBeaver has no equivalent |
| Transactions (begin/commit/rollback) | yes | **yes** | Bottom-bar controls, an open-transaction banner, and a close guard |
| Cancel a running statement | yes | **yes** | `supports_cancel`, per-engine (`cancel_safe`, `KILL QUERY`, `interrupt`) |
| Row cap on results | yes | **yes** | `max_result_rows`, fetch-one-past to flag `truncated` |
| Open/save `.sql` files | yes | **yes** | `_open_file` / `_save_file` |
| Snippets and saved queries | yes | **yes** | `backend/saved.py`, global across workspaces |
| Query history | yes | **yes** | Per-workspace, capped at 200, scoped per panel (`frontend/history_panel.py`) |
| Parameter placeholders | yes | **yes** | `backend/placeholders.py` |
| **SQL formatter** | yes | **no** | Nothing in the tree formats SQL → CORE-44 |
| Vim keybindings | plugin | **yes** | `vim_mode` setting |
| Local client console (psql/mysql) | yes | **yes** | `frontend/cli_console.py` |

### Administration

| Feature | DBeaver | sqlide | Notes |
|---|---|---|---|
| Users/roles list | yes | **yes** | `frontend/users_tab.py` |
| Permission editor | EE | **yes** | `frontend/permission_editor.py` — engine-neutral, driven by provider capabilities |
| Grants shown on an object | EE | **yes** | `object_grants`, CORE-11 |
| Session/activity monitor | yes | **yes** | `frontend/monitor_tab.py` + `backend/db/metrics.py`; cancel/kill where the account may |
| Backups and restore | no | **yes** | `backend/backups/` — jobs, S3/SFTP/FTP destinations, systemd timers, headless CLI |
| Task scheduler for arbitrary SQL | EE | **no** | Deliberate; see "Not filed" |
| Schema compare / migration generation | EE | **no** | See "Not filed" |
| Mock data generator | EE | **no** | See "Not filed" |

### Connections and configuration

| Feature | DBeaver | sqlide | Notes |
|---|---|---|---|
| SSH tunnel | yes | **yes** | `backend/ssh.py`, `sshtunnel` package or the `ssh` binary |
| TLS options | yes | **yes** | `ssl_mode`/`ssl_ca`/`ssl_cert`/`ssl_key` on the profile |
| Secure password storage | yes | **yes** | System keyring (`backend/secrets.py`) |
| Connection colours / environment marking | yes | **yes** | `backend/identity.py`, plus a workspace identity stripe |
| Destructive-action guard | partial | **yes** | `backend/sql_risk.py` — classifies the statement, escalates by environment; an unfiltered DELETE is the case it exists for |
| Projects / workspaces | yes | **yes** | A workspace owns connections, tabs, history, filters |
| Git-friendly text config | partial | **yes** | TOML per workspace (CORE-13) plus a hand-editable XML transfer format (`backend/exchange.py`) |
| Connection folders | yes | **no** | See "Not filed" |
| MCP / AI integration | partial | **yes** | `backend/mcp/` — read-only by construction, guarded (`mcp/guard.py`) |

### Window and workflow

| Feature | DBeaver | sqlide | Notes |
|---|---|---|---|
| Split editors side by side | yes | **yes** | Nested `Gtk.Paned` panes, Split button |
| Detach a tab into its own window | yes | **yes** | Drag off the tab bar, or Shift-open from anywhere |
| Session restore | yes | **yes** | Tabs, console SQL and selected tab restored per workspace |
| Notes attached to an object | no | **yes** | `backend/notes.py`, Markdown, scoped to a connection or table |
| Keyboard shortcut remapping | yes | **yes** | `frontend/keymap.py`, `keymap` in settings.toml |

## What DBeaver does better, honestly

Four things, in the order they will bite a switcher.

**1. The grid is an editor, not a viewer.** In DBeaver you add a row,
delete a row, edit ten cells, and press one Save that commits them
together and rolls back together. sqlide edits cells only, one
`UPDATE` per cell, unbatched: `_execute_updates` in
`frontend/data_grid.py` loops `connector.update_cell(...)` and its
failure handler reloads "with whatever was applied". That is not a
polish gap, it is a data-integrity gap, and it is the single strongest
reason someone bounces back to DBeaver. → CORE-38, CORE-39.

**2. Data cannot leave the app as a file.** sqlide formats a selection
as CSV, JSON, Markdown or `INSERT` statements — to the *clipboard*
(`data_grid.copy_selection`). Nothing writes a result set to disk, and
nothing reads a CSV in. The only file-shaped exits are `pg_dump`-class
backups (`backend/backups/dump.py`) and the workspace XML
(`backend/exchange.py`), neither of which is what "give me these rows
as a CSV" means. → CORE-36, CORE-37.

**3. A cell wider than a column is unreadable.** `_display_text`
ellipsizes; a JSON document, a long description or a blob has nowhere
to be shown in full and no way to be edited except in a one-line
inline entry. DBeaver's value panel and record view are used constantly
and cost little. → CORE-42.

**4. Data-grid pagination is unsound as well as slow.** `fetch_rows`
issues `SELECT * FROM t LIMIT %s OFFSET %s` with **no ORDER BY unless
the user set one** (`backend/db/postgres/connector.py`). Neither
PostgreSQL nor MySQL promises a stable row order between two such
statements, so page 2 can repeat or skip rows from page 1 — and since
the grid *appends* on scroll, the user sees the duplicates. DBeaver
avoids this by holding one server-side cursor and fetching forward
rather than re-issuing per page. Separately, `OFFSET n` makes the
server walk and discard n rows, so deep pages degrade linearly. →
CORE-40.

## The SQL we generate, examined

The ticket asks directly whether the SQL sqlide builds from user
actions is correct and efficient. Read end to end, it is in better
shape than the question fears. The good parts first, because they are
the parts most tools get wrong:

- **Values are always parameters, never interpolated.**
  `build_filter_clauses` (`backend/db/base.py`) appends to a `params`
  list and emits a placeholder; `update_cell` binds the new value and
  every PK value. The `INSERT` text in the preview dialog is
  *display only* — the dialog says so, and the executed path re-binds.
- **Identifiers are validated against the catalog, not escaped and
  hoped for.** `_assert_filter_columns` rejects any filter or sort
  column the catalog does not vouch for; `update_cell` rejects unknown
  columns; `_assert_known_table` rejects unknown tables. `quote_ident`
  additionally refuses empty names and NUL bytes.
- **Operators and conjunctions are whitelists** (`FILTER_OPERATORS`,
  `CONJUNCTIONS`), so no user string ever becomes SQL syntax.
- **Filter grouping matches what the user sees.** Conditions fold
  left-associatively — `((l1 AND l2) OR l3)` — rather than inheriting
  SQL's AND-before-OR precedence, which would silently mean something
  other than the panel shows.
- **`update_cell` asserts `expect_rowcount=1`**, so an `UPDATE` that
  matched zero or many rows is an error rather than a silent
  mis-write.
- **Statement splitting is genuinely careful** (`backend/sql_split.py`)
  — dollar-quoted bodies, `BEGIN…END` nesting, `DELIMITER`, per-dialect
  backslash rules. This is where most clients are wrong and we are not.

Three real problems:

1. **No deterministic order behind `LIMIT/OFFSET`** — the correctness
   bug described above. → CORE-40.
2. **`OFFSET` deep paging** is O(offset) at the server. A keyset
   predicate (`WHERE (pk) > (last_pk)`) is available whenever the sort
   is unique-prefixed, which for the default (no user sort) is exactly
   the primary key we would be adding for problem 1. → CORE-40.
3. **Two catalog round trips per page.** Every `fetch_rows` calls
   `_assert_known_table` → `list_tables()`, and with any filter or
   sort also `list_columns()`. Neither result is cached anywhere in
   the connectors (no cache of any kind in
   `backend/db/postgres/connector.py`), so scrolling a table re-reads
   `information_schema` on every 500 rows. The validation is right;
   paying a catalog query for it every page is not. → CORE-41.

One thing that is *not* a problem, despite looking like one:
`SELECT *` rather than an explicit column list. For a grid that shows
every column it is the correct statement, it survives a column being
added between the catalog read and the fetch, and DBeaver does the
same.

## Where sqlide is already better, or has chosen differently on purpose

- **Backups are a first-class feature.** DBeaver shells out to
  `pg_dump` from a wizard and forgets. sqlide has scheduled jobs,
  S3/SFTP/FTP destinations, retention pruning, run history and a
  headless CLI (`backend/backups/`, `sqlide-backup`). Nothing in
  DBeaver Community is close.
- **The permission editor and object grants are free.** These are
  Enterprise features in DBeaver. Ours is engine-neutral because the
  provider declares which privileges an object kind carries
  (`MetadataProvider.permission_set` / `permission_statements`), which
  is also why SQLite simply never shows the screen instead of showing
  a broken one.
- **Capability flags instead of engine `if`s.** `Capabilities`
  defaults every flag to `False` so a provider that forgets a feature
  *hides* it. The failure mode is a missing screen, never a broken
  one. DBeaver's per-driver behaviour is spread across driver classes
  and shows up as greyed-out or erroring dialogs.
- **The geo viewer needs no PostGIS client.** `backend/db/geo.py`
  parses WKB/EWKB itself and `backend/tiles.py` decides offline
  *before* a request, so a disconnected machine draws geometries on a
  plain background instead of hanging. DBeaver's spatial viewer is a
  heavier stack for the same result.
- **The destructive-action ladder.** `backend/sql_risk.py` classifies
  a statement and escalates by connection environment — none / confirm
  / type-to-confirm. DBeaver has a blanket confirmation setting; ours
  knows that an unfiltered `DELETE` on production is a different event
  from one on a scratch database.
- **The explain graph draws the shape a plan actually has**, for all
  three formats the engines answer in, on the same canvas as the ER
  diagram (`frontend/canvas.py` shared by `plan_graph.py` and
  `relation_graph.py`).
- **Config is text and belongs in git.** TOML per workspace plus a
  documented XML transfer format, versus DBeaver's opaque project
  metadata and credentials store.
- **Notes and MCP.** Neither exists in DBeaver. The MCP server being
  read-only *by construction* (`backend/mcp/guard.py`) rather than by
  setting is the kind of choice worth keeping.
- **Startup and footprint.** A GTK4 app against three drivers, versus
  an Eclipse RCP. Not measured here, so no number is claimed — but it
  is the reason the project exists and no ticket below should cost it.

## Gaps deliberately NOT filed, with reasons

- **Schema compare / diff / migration generation.** The single largest
  DBeaver Enterprise feature and a plausible future direction, but it
  is a product of its own, not a gap. It needs a full DDL model on
  both sides — which CORE-23 (`table_model.py`) starts building. Revisit
  once CORE-23…CORE-27 have landed; filing it now would produce a
  ticket nobody could pick up.
- **Mock/test data generation.** Genuinely useful, genuinely large
  (a generator vocabulary, per-column rules, FK-aware ordering), and
  not what anybody switches clients for. Not filed.
- **Editing the schema through the ER diagram.** `relation_graph.py`
  is read-only and should stay so until the table designer's alter
  mode (CORE-26) exists; a diagram that generates DDL without a diff
  model underneath is how you drop a column by accident.
- **A task scheduler for arbitrary SQL.** We already have a scheduler
  (`backend/backups/schedule.py`, systemd user timers). Generalising it
  to "run this SQL nightly" means owning failure notification, retry
  policy and result retention — a cron replacement wearing a database
  client's clothes. Out of scope by choice.
- **Stored-procedure debugger.** DBeaver's is PostgreSQL-only, depends
  on `pldbgapi`, and is used by very few people. Cost/benefit fails.
- **Excel/XLSX export.** CORE-36 covers CSV/JSON/SQL/Markdown, which
  is the 95% case with no new dependency. XLSX means a new runtime
  dependency for a format a spreadsheet opens from CSV anyway.
- **Connection folders.** Real DBeaver users organise fifty
  connections this way; sqlide already has workspaces *and* colours
  *and* environment marking for the same job. Revisit if someone
  actually hits the limit.
- **More engines (Oracle, SQL Server, Mongo…).** Out of scope by
  design. The JDBC bridge is the answer and stays experimental.
- **Query builder, table designer, charting.** Already researched and
  filed — RS-01 (CORE-17…CORE-22), RS-02 (CORE-23…CORE-29), RS-03
  (CORE-30…CORE-35). This document does not re-file any of them, and
  where a DBeaver feature falls inside one of those it is cross-
  referenced rather than duplicated.

## Tickets filed

| ID | Title | Why it is here |
|---|---|---|
| CORE-36 | Export a result set or table to a file | The most-missed feature; clipboard-only today |
| CORE-37 | Import a CSV file into a table | The other half of CORE-36 |
| CORE-38 | Insert and delete rows in the data grid | The grid is update-only |
| CORE-39 | Grid edits saved as one transaction | Partial writes on failure today |
| CORE-40 | Stable, efficient data-grid pagination | `LIMIT/OFFSET` with no order is a correctness bug |
| CORE-41 | Per-connector catalog cache | Two catalog queries per page |
| CORE-42 | Value panel and record view | Long text, JSON and binary are unreadable |
| CORE-43 | Foreign-key navigation in the grid | The relation data is already loaded |
| CORE-44 | Format SQL in the editor | Daily-use editor feature, no dependency needed |
| CORE-45 | Find a value across a database's tables | DBeaver's data search; common triage move |

Suggested order: CORE-40 and CORE-39 first (they are correctness),
then CORE-36 and CORE-38 (they are what people ask for), then
CORE-42, CORE-41, CORE-43, CORE-37, CORE-44, CORE-45.
