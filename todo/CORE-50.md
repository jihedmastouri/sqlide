## CORE-50 — Stop recycling the table properties page

- **Status:** todo
- **Depends on:** CORE-47

### Problem

The properties surface is reused in place: opening properties for a second object
mutates the existing page. You cannot compare two objects, and back/forward
history is meaningless because there is only ever one page.

### Goal

Each object gets its own properties surface; opening another one does not destroy
the first.

### Approach

Give the properties view an identity keyed by object reference. Opening
properties for an object that already has a live surface focuses it (matching
CORE-01's no-duplicate-tabs rule) rather than either recycling the page or
opening a second copy. Detached windows (CORE-47) are per-object by the same key.

Make sure the panel-follows-active-tab behaviour from CORE-47 does not
re-introduce recycling by mutating one shared widget — the panel should swap in
the object's own surface.

### Acceptance criteria

- [ ] Opening properties for object B does not alter the surface showing object A.
- [ ] Re-opening properties for A focuses the existing surface.
- [ ] Two detached properties windows can be open side by side on different
      objects and both stay live.
- [ ] Closing one surface does not disturb another.
- [ ] Surfaces are released when their object's tab closes — no unbounded growth.
