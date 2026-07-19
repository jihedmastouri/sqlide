# next steps — backlog

## Features

- [x] Add backup/restore feature: under settings, open window
      (Preferences → General → Backup & Restore: zips settings +
      saved snippets/queries + every workspace file; restore extracts
      over the config dir, applied on next start)
- [ ] Integrate lib-ghostty instead of our terminal
      (skipped for now: libghostty is not yet shipped as an embeddable
      library with GTK4/PyGObject bindings; revisit when it is)
- [x] Add hover DDL in code editor
      (query console: hovering a table name shows its CREATE in a
      tooltip; catalog/DDL fetched lazily, cached per connection+db)
- [x] Max only 2 splits
- [ ] create/drop table, view, index, trigger, function, procedure, event
      (planned — see feature_plans.md §1: drop via sidebar context menu
      with confirm dialog; create via a table designer tab + dialect
      templates in the console for the rest)

## Quality of life

- [x] Saved code snippets (global store in snippets.json; side panel
      inserts at the console cursor)
- [x] Saved SQL queries (global store in saved_queries.json; side
      panel opens them in a new console)
- [x] Saved filters (by connection.db.table) (stored per workspace in
      Workspace.saved_filters; Filters page of the side panel on a
      table tab)
- [x] Vim mode (Preferences → General; GtkSource.VimIMContext on
      editable SQL editors)
- [x] If executing a query with a placeholder, show a dialog to input the
      placeholder value. Remember the value for next time. (how: the
      entered values are stored per placeholder name — ":name" / "?1" —
      in the workspace's JSON file, `placeholders` map, and prefill
      the dialog on the next run)
- [ ] MCP server: add a button to open the MCP server in a new tab. Read
      only. You can choose/customize the port, access control, and
      authentication method. (explain how to customize the port, access
      control, and authentication method). All this will create a new
      instance of the MCP server, and will not affect the current running
      MCP servers. Then you receive a new URL to access the new instance
      of the MCP server and a JSON file to use in the SQL IDE. The new
      instance of the MCP server runs until you close it.
      (planned — see feature_plans.md §2: backend/mcp instance model
      over the official MCP SDK as an optional extra, read-only guard +
      driver-level read-only, per-tab instances with port/bind/token
      config and generated client JSON)

## UI

- [x] Make the top banner full width
- [x] Remove the transaction buttons if not in a transaction, only keep
      Begin; move it to the bottom banner
- [x] Hide the result banner if there is no result
- [x] Add minimize/expand button to the result banner
- [x] Right side panel:
  - [x] SQL editor: Information, Saved Code Snippets, Saved SQL Queries,
        History
  - [x] Result data grid: Information, History, Aggregation
  - [x] Table data grid: Information, History, Aggregation, Saved Filters
  - [x] Console: Information, History
