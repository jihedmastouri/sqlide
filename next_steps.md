# next steps — five UI improvements

Prioritized by user impact vs. effort. Suggested implementation order:
**1 → 2 → 4 → 5 → 3** (1 restructures the right panel, 2 builds on it;
4 and 5 are one pass through the sidebar row code; 3 adds a backend
method and builds on the reworked sidebar rows).

## P1

### 1. Right side panel must not reach the top bar ✅ DONE

**Problem:** `_history_split` wraps the whole window (`window.py` —
`self._history_split.set_content(self._split)`), so when the history
panel opens, its edge and toggle sit right next to the window-control
close button and it reads as "close the panel" vs "close the app".

**Fix:** move the right split *inside* the content area so the panel
starts below the content header bar:

- In `window.py`, keep `content_view` (header + tab bar) at the top
  level of the content side. Its content becomes a new
  `Adw.OverlaySplitView(sidebar_position=END)` whose **content is
  `self._stack`** and whose sidebar is the panel — instead of the
  current arrangement where the split wraps everything.
- `Adw.TabOverview.child` goes back to `self._split`; the breakpoint
  setter for the right split stays.
- The history toggle stays in the content header (`pack_end`), now
  visually adjacent to the panel it controls, far from window controls.

Low effort, fixes an actively confusing/dangerous interaction.

### 5. Lazy connections with status indicators ✅ DONE

**Problem/goal:** opening a workspace must not activate every
connection; show a gray dot for inactive and green for active;
expanding a connection row auto-connects.

**Current state:** connecting is *already* lazy — `ensure_connector()`
only runs when a row is expanded or a tab needs it. So the work is the
indicator plus keeping it truthful:

- `sidebar.py` `_setup_row`/`_bind_row`: prepend a small status dot
  (a `Gtk.Image` or a 8px `Gtk.DrawingArea`/CSS circle) on connection
  rows; CSS classes `.conn-dot-active` (green) / `.conn-dot-idle`
  (gray) in `style.css`.
- `window.py` exposes `is_connected(name)` (checks `self._connectors`
  under the lock) and calls a new `sidebar.set_connected(name, True)`
  whenever `ensure_connector` creates a connector — it runs on worker
  threads, so marshal with `GLib.idle_add`.
- Expanding already triggers `self._ensure(profile)` in
  `_load_children`, i.e. auto-connect on expand is already the
  behavior; the dot flips green when the load completes.
- Restored table/query tabs also connect (they call `ensure_connector`
  when they load data) — the dot updates through the same hook, so it
  stays accurate without the sidebar doing anything special.

## P2

### 2. Aggregation result goes to the right side panel ✅ DONE

**Current state:** `data_grid.py` `_on_aggregate` shows
count/sum/avg/min/max in a `Gtk.Popover` anchored at the context-menu
position.

**Fix (after #1):** the right panel becomes a small multi-view panel:

- New `frontend/side_panel.py` (or grow `history_panel.py`): a
  `Gtk.Stack` + `Adw.ViewSwitcher` (or `Adw.InlineViewSwitcher`) with
  two pages: **History** (existing `HistoryPanel` content) and
  **Aggregate** (a label/list styled like the current popover text).
- `DataGrid` gains an `on_aggregate(lines: list[str])` callback (wired
  through `TableTab` and `QueryConsole` up to the window, same pattern
  as `on_ran`); `_on_aggregate` calls it instead of popping the
  popover. The window fills the Aggregate page, switches the stack to
  it, and reveals the panel.
- Drop `_agg_popover`/`_agg_label` from `data_grid.py` once wired.

### 4. Left sidebar: caret on the right, icons on the left ✅ DONE

All in `sidebar.py` (`_setup_row` / `_bind_row`) plus `style.css`:

- Hide the built-in expander arrow (`Gtk.TreeExpander.set_hide_expander(True)`,
  GTK ≥ 4.10 — we already require GTK 4.10+ idioms) and keep
  `set_indent_for_icon(False)` so rows align left.
- Add a per-kind `Gtk.Image` at the start of the row box:
  connection → `network-server-symbolic`, category → none/dim,
  table → `view-grid-symbolic` (or `table-symbolic` if themed),
  view → `view-reveal-symbolic`, function → `system-run-symbolic`,
  column → none (keep PK badge).
- Append a caret `Gtk.Image("pan-end-symbolic")` at the **end** of the
  row box for expandable kinds, rotated 90° when expanded (bind to the
  `TreeListRow.expanded` notify we already connect in `_bind_row`).
  Row activation already toggles expansion for connection/category;
  give table/view rows a click on the caret itself (small
  `Gtk.GestureClick` on the icon) so "open data tab" (row activate)
  and "expand columns" (caret) stay distinct.

## P3

### 3. DDL preview on hover ✅ DONE

Interpreting "database name" as the schema-tree object rows: hovering a
**table/view** shows its `CREATE …` DDL; a connection row shows a short
summary (path/kind + object count) since a whole-database DDL dump is
too big for a tooltip.

- **Backend:** add `get_ddl(name: str) -> str` to the `Connector` ABC
  (`db/base.py`) with a concrete default returning `""` (same pattern
  as `list_functions`). SQLite:
  `SELECT sql FROM sqlite_master WHERE name = ?`. Postgres/MySQL fill
  in later (`pg_get_viewdef`/`SHOW CREATE TABLE`).
- **Frontend (`sidebar.py`):** table/view rows get
  `set_has_tooltip(True)` + `query-tooltip`. DDL is fetched lazily:
  first hover kicks off `run_async(get_ddl)` and returns `False`;
  the result is cached on the `Node` (`node.ddl`), and once cached the
  handler sets a monospace tooltip (`tooltip.set_custom` with a
  `Gtk.Label` using CSS class `monospace`, ellipsized/clamped to ~30
  lines). Empty DDL → no tooltip.

## Files touched

`window.py` (1, 2, 5), `sidebar.py` (3, 4, 5), `data_grid.py` (2),
`history_panel.py` → panel with switcher (2), `style.css` (4, 5),
`db/base.py` + `db/sqlite/connector.py` (3), new/renamed
`side_panel.py` (2).
