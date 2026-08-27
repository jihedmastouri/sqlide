## CORE-58 — Double-click must not toggle a tree node

- **Status:** todo
- **Depends on:** CORE-52

### Problem

CORE-52 made a single click toggle expansion and a double click open the object.
Because a double click delivers a press first, opening a node also expands or
collapses it — the tree jumps under the pointer at the moment you asked for
something else entirely.

### Goal

Double click opens. It does not change expansion state.

### Approach

Distinguish the two gestures rather than acting on the first press and hoping.
Either hold the expansion toggle until the double-click interval has passed
without a second press, or undo/suppress the toggle when the second press
arrives — whichever reads better in this widget. The first option adds a small
delay to expansion; the second risks a visible flicker. Pick one, and say in the
ticket notes which and why.

Keyboard behaviour is unchanged: Enter opens, arrow keys expand and collapse.

### Acceptance criteria

- [ ] A double click opens the object and leaves expansion exactly as it was —
      whether the node started expanded or collapsed.
- [ ] A single click still toggles expansion.
- [ ] No visible flicker of the node expanding and collapsing during a double
      click.
- [ ] The caret/disclosure control, the `+` button and keyboard navigation are
      unaffected.
- [ ] Tests cover single click, double click on an expanded node, and double
      click on a collapsed one.
