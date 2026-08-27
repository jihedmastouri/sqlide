## CORE-52 — Sidebar click behaviour: expand, open, and an explicit Open menu

- **Status:** done
- **Depends on:** CORE-01

### Problem

Click behaviour in the tree is inconsistent with what the rest of the app does,
and there is no explicit way to ask for a new window.

### Goal

Predictable, documented mouse behaviour, with the same actions reachable from the
context menu.

### Approach

- Single click: select and toggle expansion of the node (leaf nodes just select).
- Double click (and Enter): open the object — focusing the existing tab if one is
  already open for it, per CORE-01.
- Right click: the menu includes `Open` and `Open (Window)` at the top, above the
  existing entries.

`Open (Window)` reuses the tear-out-into-a-window machinery that already exists
for tabs rather than adding a second path.

### Acceptance criteria

- [x] Single click expands/collapses without opening a tab.
- [x] Double click opens, or focuses an already-open tab for that object.
- [x] Enter matches double click for the selected node.
- [x] Right-click menu has Open and Open (Window) on every openable node, and
      omits them (or disables them with a reason) on nodes that open nothing.
- [x] Keyboard navigation (arrows, Home/End) is unaffected.
