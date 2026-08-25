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

A first run opens the home page — the app's one introduction, and the
form that gets you past it. Name your first workspace, optionally give
it an identity colour, and click **Create Workspace**. (Moving from
another machine? **Import…** in the header reads an exported XML
workspace instead.)

A workspace groups its own connections and remembers your open tabs —
it's the unit you open and close. Every launch after this one skips
the home page and reopens the workspace you were last in.

The grid icon in the sidebar header opens the workspace list: rename
or recolour the one you're in (the pencil on its row), or click **+**
to make another. Other workspaces stay out of the way until you go
looking for them there.

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

While a statement is running, **Cancel** appears next to Run and asks
the server to stop it (a cancel request on Postgres, `KILL QUERY` on
MySQL, `interrupt` on SQLite). A run that is cancelled — or one you
supersede by starting another — never puts its rows in the grid, even
if the server finishes it anyway.

A statement that returns more rows than **Preferences ▸ General ▸
Maximum Rows Fetched** (5000 by default) is fetched only up to that
many, and the result is shown under a banner saying so, so a capped
result is never mistaken for the whole answer. Set it to 0 to fetch
everything.

Timestamps come back in the zone named by **Preferences ▸ General ▸
Session Time Zone**, which a new connection asks the server for. The
default, *This Computer*, makes every server agree with the clock on
your screen; *UTC* is the choice to make when you want one reading
everywhere; *Server Default* takes whatever the server is configured
with, the way a bare `psql` or `mysql` session does. The setting
applies on the next connect, not to open connections.

Binary columns — `BLOB`, `bytea`, MySQL's binary collations — show as
hex (`0x89504E47`), abbreviated to a byte count once they are too long
to read. They are display-only: a blob cannot be edited in the grid,
since what the cell shows is a summary rather than its contents.
Copying or exporting one writes its full hex.

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

Close and relaunch — you land back in the workspace you were last in,
with both the connections and the tabs you left open restored,
including query console text. State lives in
`~/.config/sqlide/workspaces/<id>.json`. To land somewhere else next
time, switch workspaces from the grid icon in the sidebar header.

## Next steps

- [Language Servers](/docs/language-servers/) for schema-aware completion.
- [Create/Drop DDL](/docs/ddl/) for building and dropping schema objects.
- [MCP Server](/docs/mcp-server/) to let an AI assistant query read-only.
- [Connection Security](/docs/connection-security/) for keyring-backed
  passwords.
- [Import and Export](/docs/transfer/) to carry a workspace to another
  machine.
