## CORE-58 — Double-click must not toggle a tree node

- **Status:** done
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

- [x] A double click opens the object and leaves expansion exactly as it was —
      whether the node started expanded or collapsed.
- [x] A single click still toggles expansion.
- [x] No visible flicker of the node expanding and collapsing during a double
      click.
- [x] The caret/disclosure control, the `+` button and keyboard navigation are
      unaffected.
- [x] Tests cover single click, double click on an expanded node, and double
      click on a collapsed one.

### Notes

Held the toggle rather than undoing it. A single click's expansion now
waits out the double-click interval (`gtk-double-click-time`, 400ms
where no Gtk.Settings answers) and a second press drops it before
anything moves, so a double click cannot flicker — there is no state to
undo, because none was applied. Undoing after the fact would have had
to expand and collapse a node whose children may load asynchronously,
which is exactly the jump the ticket is about; a fraction of a second
before a row opens is the cheaper cost.

Activation stopped expanding as well: `_on_activate` used to force a
container row open (CORE-52), which by itself broke "expansion exactly
as it was" for a collapsed node. Expanding is now the caret's job, a
single click's, or the right arrow key's. The caret still toggles at
once — a press there can only mean expansion, so it waits for nothing.
