Now:
- [x] In data grid view you should be able to move columns for example A,B,C columns you can move A to be next C. so become B,A,C. (drag the header, or context menu → Move Column Left/Right)
- [x] Selecting, filtering, ordering are also queries and should be registered in history (every table-tab load — select, filter, sort, paging — is recorded)
- [x] when you close a panel history for that panel should be removed from all panels history. but not from global history
- [x] clicking a history should open a new query console with the query
- [x] Opening a table the right side panel should show the DDL listed as well (new DDL page in the right panel, follows the active tab)
- [x] Option to open and save files of any type (espacially sql files) query console should be able to save to a file. (open/save buttons in the console toolbar; first save asks where)
- [x] Add transaction buttons in query console. (Begin/Commit/Rollback; the sqlite connector now runs in autocommit mode so explicit transactions stay open)
- [ ] Using `GooCanvas` to create table-relation graph — BLOCKED: GooCanvas is GTK3-only, no GTK4 port exists (and no typelib installed); needs a different approach (e.g. custom Gtk.DrawingArea/Snapshot widget)
- [x] Add the option to explain the query instead of running it. (Explain button; EXPLAIN QUERY PLAN on sqlite via Connector.explain_prefix)
- [x] When using explain add the option to view as graph using `GooCanvas`. or json, or table of course. (Table/JSON switcher on each plan tab; graph part blocked, same GooCanvas issue as above. Grids also got Copy As → JSON)
- [x] Add text highlighting to DDL text and functions. (definition tab, side-panel DDL page and function tabs use the highlighted SQL editor)
- [x] Make it possible to update and save functions (trigger, functions, pl/sql and proc ofc) (functions in the sidebar open an editable definition tab; Save reviews DROP+CREATE before running. On sqlite that covers triggers — its only programmable objects; pl/sql & procs light up when the postgres/mysql connectors stop being stubs)

Future Features:
- [ ] make it possible to update DDL through changing text or the table. The result should be a query show to the user.
- [ ] Add MySQL support
- [ ] Add advanced connection settings: ssh tunnel, ssl certificates....
- [ ] Query Builder

Future fixes:
- [ ] hover table name to show DDL still not working
- [ ] Tabs in split view should be able to close them
- [ ] max 3 split views. and check scroll and size, it's broken
