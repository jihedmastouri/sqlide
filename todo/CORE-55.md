## CORE-55 — Follow the active tab in the sidebar

- **Status:** todo
- **Depends on:** CORE-01, CORE-52

### Problem

Switching to a tab tells you nothing about where that object lives. You have to
find it in the tree by hand, which on a deep Postgres tree is several expansions.

### Goal

The sidebar highlights the object belonging to whichever tab you are looking at.

### Approach

When the active tab changes, resolve its object reference to a tree node,
expanding ancestors as needed, select it and scroll it into view. Nodes not yet
loaded are lazily fetched along the path.

Two things to get right: this must not fight the user — if they have deliberately
navigated elsewhere in the tree, a tab switch may reveal and highlight but should
not yank their scroll position on every keystroke — and it must not trigger a
cascade of catalog queries on deep paths. Make the behaviour a setting
(`sidebar.follow_active_tab`, default on) so it can be turned off.

### Acceptance criteria

- [ ] Switching tabs highlights the corresponding sidebar node.
- [ ] Ancestors are expanded and the node is scrolled into view.
- [ ] Lazily-loaded levels are fetched along the path without blocking the UI.
- [ ] Tabs with no sidebar object (a query console, say) clear the highlight
      rather than leaving a stale one.
- [ ] The behaviour can be disabled in settings.
- [ ] Selecting in the tree still does not switch tabs — the sync is one-way.
