# sqlide

A minimal, clean SQL IDE built with Python, GTK4, and libadwaita.
SQLite works today; MySQL/PostgreSQL are stubbed; a generic JDBC bridge is
included (experimental). See [PLAN.md](PLAN.md) for design and status.

## Requirements

- Python 3.12+
- GTK4 + libadwaita + PyGObject (usually system packages, e.g.
  `sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1`)
- Optional drivers:
  - MySQL: `pip install PyMySQL` (adapter not implemented yet)
  - PostgreSQL: `pip install "psycopg[binary]"` (adapter not implemented yet)
  - JDBC: `pip install JayDeBeApi` + a Java runtime + the driver jar

SQLite needs nothing extra.

## Try it (SQLite)

Nothing here has been executed yet — this is the intended manual test path:

```sh
# 0. optional sanity check: everything should compile and import
python3 -m compileall -q sqlide && python3 -c "import sqlide.backend.db.registry"

# 1. create a demo database
python3 scripts/make_demo_db.py          # writes ./demo.db

# 2. launch
python3 -m sqlide
```

Then, in the app:

1. The launcher opens first: click **+** (or **Create Workspace**), give
   the workspace a name. A workspace groups its own connections and
   remembers your open tabs.
2. In the workspace window, click **+** in the sidebar header → type stays
   **SQLite** → browse to `demo.db` → **Test connection** → **Save**.
3. Expand the connection in the sidebar; click a table (e.g. `customers`)
   to open it in a grid tab.
4. Click into a cell, edit, press Enter — the change is written with a
   primary-key UPDATE. The `log` table has no primary key and should show
   as read-only. `order_totals` is a view (also read-only).
5. Click the terminal icon on the connection row for a query console;
   type SQL and press **Ctrl+Enter** (or Run).
6. Close and restart — the launcher lists the workspace; opening it
   restores the connections *and* the tabs you left open, including query
   console text (`~/.config/sqlide/workspaces/<id>.json`). Other
   workspaces are only visible in the launcher (the grid icon in the
   sidebar header reopens it).

## Layout

- `sqlide/backend/` — connectors, connection profiles, and the workspace
  store (one JSON file per workspace), zero GTK. One folder per database
  under `backend/db/`.
- `sqlide/frontend/` — all GTK/libadwaita UI.
- `scripts/make_demo_db.py` — builds the demo SQLite database.
