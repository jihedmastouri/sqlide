---
title: MCP Server
description: Read-only database access for AI assistants.
order: 6
---

sqlide can expose a read-only [Model Context Protocol](https://modelcontextprotocol.io/)
server so an AI assistant (Claude, etc.) can browse and query your
databases without ever being able to write to them. Needs the optional
`mcp` extra:

```sh
pip install "sqlide[mcp]"
```

Open it from the header bar's network icon (blank form) or a
connection's context menu ("MCP Server", preselecting that connection).
Each tab is a **fresh, independent instance** — its own connectors, its
own port — that never touches the connections cached by the rest of the
app. Several tabs can run side by side without sharing state, and
closing a tab (or Stop) shuts that instance down. Nothing is persisted
across restarts.

## The form

- **Connections** — which of the workspace's connections this instance
  exposes; pick one or more.
- **Port** — `0` (default) picks a free port automatically.
- **Listen on all interfaces** — off (default) binds `127.0.0.1`, only
  reachable from this machine; on binds `0.0.0.0` and forces a bearer
  token (sqlide refuses to start otherwise).
- **Enable the query tool** — off exposes only the catalog
  (`list_tables` / `list_columns` / `get_ddl`), no arbitrary `SELECT`.
- **Row limit** — caps how many rows one query returns.
- **Require a bearer token** — checked on every request; wrong or
  missing → 401. A token is generated for you (regenerate any time), or
  you can type your own.

Once started, the tab shows the server URL, a copy-ready client JSON
snippet (also saveable to a file) for `~/.claude.json` or similar:

```json
{"mcpServers": {"sqlide-<workspace>": {
    "url": "http://127.0.0.1:PORT/mcp",
    "headers": {"Authorization": "Bearer <token>"}}}}
```

...and a live request log (tool, connection, duration; denied attempts
included).

## Security model (defense in depth)

1. **The query tool's guard** rejects anything that isn't exactly one
   `SELECT`/`WITH`/`EXPLAIN` statement (plus `SHOW` on MySQL) with no
   write keyword anywhere in it — including inside a PostgreSQL
   data-modifying CTE (`WITH x AS (DELETE …) SELECT …`).
2. **The database connection itself is opened read-only** where the
   driver supports it: SQLite via `mode=ro`, PostgreSQL via
   `default_transaction_read_only=on`, MySQL via `SET SESSION
   TRANSACTION READ ONLY`. This is the real backstop if the guard were
   ever bypassed. JDBC has no portable read-only mode, so JDBC instances
   rely on the guard alone.
3. **`0.0.0.0` without a token is refused outright.**
