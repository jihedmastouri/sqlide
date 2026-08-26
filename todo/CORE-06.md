## CORE-06 — Connection context menu: Disconnect

- **Status:** todo

### Acceptance criteria

- [ ] Right-clicking a connection shows **Disconnect**, enabled only when
      connected.
- [ ] Disconnect closes pooled connections, collapses the node and marks the
      connection disconnected in the tree.
- [ ] If queries are running, the user is warned and can cancel or force.
- [ ] Open tabs for that connection stay open but show a disconnected state with
      a reconnect affordance; they do not throw.
- [ ] Reconnecting from the same node restores the tree without an app restart.

