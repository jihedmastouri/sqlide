---
title: Getting Started
description: A walkthrough with a demo SQLite database.
order: 3
---

This walks through sqlide using a throwaway demo database, from first
launch to editing a cell.

## 1. Create a demo database

```sh
python3 scripts/make_demo_db.py   # writes ./demo.db
python3 -m sqlide
```

## 2. Create a workspace

The launcher opens first. Click **+** (or **Create Workspace**) and give
the workspace a name. A workspace groups its own connections and
remembers your open tabs — it's the unit you open and close, and other
workspaces stay out of the way until you reopen the launcher.

## 3. Add a connection

In the workspace window, click **+** in the sidebar header. The type
stays **SQLite**; browse to `demo.db`, click **Test connection**, then
**Save**.

## 4. Browse the schema and edit data

Expand the connection in the sidebar and click a table, e.g. `customers`,
to open it in a grid tab. Click into a cell, edit it, and press Enter —
the change is written as a primary-key `UPDATE`.

The `log` table has no primary key and shows as read-only, as does
`order_totals`, which is a view.

## 5. Run a query

Click the terminal icon on the connection row to open a query console.
Type SQL and press **Ctrl+Enter** (or the Run button).

**Explain** runs the same statement as a plan instead, and shows it
three ways: as a **Graph** (the tree a plan actually is — zoomable,
with each step's full text on hover), as the **Table** the server
returned, and as **JSON**.

## 6. Read a selection

Selecting cells in any grid — click, click-and-drag, Shift+click, a
click on a row number or a column header — fills the side panel's
**Aggregate** page with a count/sum/avg/min/max of what is selected,
so opening the panel is enough to read it. **Ctrl+C** copies the
selection; the column header's menu (either mouse button) sorts,
copies and moves the column.

## 7. Restart and pick up where you left off

Close and relaunch — the launcher lists the workspace, and opening it
restores both the connections and the tabs you left open, including
query console text. State lives in
`~/.config/sqlide/workspaces/<id>.json`. The grid icon in the sidebar
header reopens the launcher to switch workspaces.

## Next steps

- [Language Servers](/docs/language-servers/) for schema-aware completion.
- [Create/Drop DDL](/docs/ddl/) for building and dropping schema objects.
- [MCP Server](/docs/mcp-server/) to let an AI assistant query read-only.
- [Connection Security](/docs/connection-security/) for keyring-backed
  passwords.
- [Import and Export](/docs/transfer/) to carry a workspace to another
  machine.
