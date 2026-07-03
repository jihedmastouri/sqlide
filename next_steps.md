# next steps — four features

Environment note: **GtkSourceView 5 is not installed on this machine** — the
code editor feature needs the system package (`gir1.2-gtksource-5` on
Debian/Ubuntu, `gtksourceview5` on Fedora/Arch). Build it with a graceful
fallback to the current `Gtk.TextView` so the app still runs without it.

## 1. Query history in a right side panel

**Backend** (`backend/workspaces.py`):

- New `HistoryEntry` dataclass: `sql`, `connection`, `timestamp` (ISO string),
  `ok` (bool — record failed runs too, like most IDEs).
- New `history: list[HistoryEntry]` field on `Workspace`, capped at ~200
  entries, serialized in `to_dict`/`from_dict` (missing key defaults to `[]`
  so old files load fine).

**Frontend**:

- New `frontend/history_panel.py`: a `Gtk.ListBox` of rows — title is the
  first line of the SQL (ellipsized), subtitle is `connection · time`, failed
  runs get a dim/error marker. A "clear history" button at the top.
- `window.py`: wrap the existing split view in a second
  `Adw.OverlaySplitView` with `sidebar_position=END`, hidden by default; a
  toggle button in the content header shows/hides it.
- Activating a history row: if the current tab is a query console, load the
  SQL into it (and switch its connection dropdown); otherwise open a new
  console pre-filled with it.
- `QueryConsole` gets an `on_ran(sql, connection, ok)` callback; the window
  appends to `workspace.history`, saves, and refreshes the panel.

## 2. IDE-like schema tree in the left sidebar

Replace the flat `Adw.ExpanderRow` sidebar with a proper tree:
`Gtk.TreeListModel` + `Gtk.ListView` + `Gtk.TreeExpander` (the GTK4 idiom for
lazy trees; nested ExpanderRows get visually ugly past two levels).

- Tree shape: **connection → Tables / Views / Functions → object → columns**
  (columns only under tables/views). Column rows show `name  type` with a PK
  marker; they're informational, not activatable.
- Lazy loading: expanding a node returns an empty store and fills it via
  `run_async` — connection expand calls `list_tables()`, table expand calls
  the existing `list_columns()`, Functions expand calls a new
  `list_functions()`.
- Activating a table/view still opens a data tab; the per-connection
  query-console button stays.

**Backend**: add `list_functions() -> list[FunctionInfo]` to the `Connector`
ABC with a **concrete default returning `[]`** (so the unimplemented
mysql/postgres/jdbc stubs don't break). SQLite returns `[]` (it has no stored
functions); Postgres (`pg_proc`) and MySQL (`information_schema.routines`)
fill it in when milestone 7 lands. Empty categories show "(none)".

## 3. Real code editor in the query console

- New `frontend/sql_editor.py` wrapping the editor behind a tiny interface
  (`get_text`/`set_text`): tries `gi.require_version("GtkSource", "5")`; on
  failure falls back to the current monospace `Gtk.TextView`.
- With GtkSourceView: SQL syntax highlighting (`language="sql"`), line
  numbers, current-line highlight, and the style scheme
  (`Adwaita`/`Adwaita-dark`) synced to `Adw.StyleManager`'s dark property.
- `query_console.py` swaps its `Gtk.TextView` for this widget; Ctrl+Enter
  handling unchanged.

## 4. Connection-independent query console

- `QueryConsole` drops the fixed `profile`. The toolbar gains a
  `Gtk.DropDown` of the workspace's connection names, placed next to Run.
- The window owns one shared `Gtk.StringList` of connection names and appends
  to it when a connection is added — so all open consoles' dropdowns update
  live (each `DropDown` keeps its own selection).
- Run resolves the selected name via `workspace.find_connection()`; with
  nothing selected the status line says "No connection selected" instead of
  running.
- Tab title follows the selection ("query · demo"). `tab_state()` saves the
  selected connection name (the `TabState.connection` field already exists);
  restore re-selects it.
- `window.new_query(profile=None)`: the sidebar's per-connection button
  preselects that connection; a new global "New query" button in the header
  and the tab-overview "+" open one with the first/last-used connection
  selected — and a console can now exist with zero connections.

## Order & files

Implement 4 → 3 → 1 → 2 (4 changes the console's contract, 3 and 1 build on
the console, 2 is the standalone sidebar rewrite).

Touched: `workspaces.py`, `db/base.py`, `db/sqlite/connector.py`,
`window.py`, `query_console.py`, `sidebar.py` (rewrite), new
`history_panel.py` + `sql_editor.py`, `style.css`, plus PLAN.md updates.

Manual test path: install the GtkSourceView package, then run against
`demo2.db`.
