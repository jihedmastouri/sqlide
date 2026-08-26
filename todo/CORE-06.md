## CORE-06 — Connection context menu: Disconnect

- **Status:** done

### Acceptance criteria

- [x] Right-clicking a connection shows **Disconnect**, enabled only when
      connected.
- [x] Disconnect closes pooled connections, collapses the node and marks the
      connection disconnected in the tree.
- [x] If queries are running, the user is warned and can cancel or force.
- [x] Open tabs for that connection stay open but show a disconnected state with
      a reconnect affordance; they do not throw.
- [x] Reconnecting from the same node restores the tree without an app restart.



### Notes

- **Disconnect** sits on the connection row's context menu, below
  Edit…, and is always listed so the menu keeps its shape.
  `Sidebar.set_menu_node` re-derives the action's enabled state from
  the row it is pointed at, so it is only live on a connection row that
  is actually open (the same `node.connected` flag the status dot
  reads).
- Taking it calls `Window._disconnect_connection`. With a statement in
  flight on that connection (`QueryConsole.is_running`, counted across
  panes and pop-outs) the user gets an `Adw.AlertDialog` first: Keep
  Connected, or Disconnect Anyway — which cancels the runs through
  `cancel_run()` before closing.
- Closing itself reuses `_drop_connector` (pop the cached connector,
  `close()` on a worker thread, dot off, status bar refreshed) and adds
  `Sidebar.collapse_connection`: the row folds back up and forgets the
  schema and capability flags it cached, so expanding it again
  reconnects and refetches rather than showing a dead session's tree.
- Open tabs are left exactly as they are — rows, SQL, filters, scroll
  position — and grow an `Adw.Banner` at the top saying so, with a
  Reconnect button (`feedback.set_disconnected`, per the feedback
  table's "persistent condition" row). Nothing throws, because every
  backend call goes through `ensure_connector`, which simply opens a
  new session; that path also clears the banners, so reconnecting from
  the tab, from the status bar or by expanding the tree all leave the
  same state.
