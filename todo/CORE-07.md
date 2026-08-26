## CORE-07 — Connection context menu: Close all related tabs

- **Status:** todo

### Acceptance criteria

- [ ] Right-clicking a connection shows **Close all related tabs**, with the
      count (e.g. "Close all 7 related tabs").
- [ ] Closes every tab belonging to that connection — table tabs, query consoles,
      properties/info views — and nothing belonging to other connections.
- [ ] Tabs with unsaved work (edited query text, pending grid edits) prompt once,
      listing them, with save / discard / cancel.
- [ ] Disabled when the connection has no open tabs.


