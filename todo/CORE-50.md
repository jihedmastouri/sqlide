## CORE-50 — Stop recycling the table properties page

- **Status:** done
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

- [x] Opening properties for object B does not alter the surface showing object A.
- [x] Re-opening properties for A focuses the existing surface.
- [x] Two detached properties windows can be open side by side on different
      objects and both stay live.
- [x] Closing one surface does not disturb another.
- [x] Surfaces are released when their object's tab closes — no unbounded growth.

### Notes

- The panel now holds `object_info.PropertiesSurfaces`: a `Gtk.Stack`
  of one `PropertiesView` per object, keyed by `properties_key`, made
  on first sight and brought to the front afterwards. Nothing is
  retargeted, so opening B leaves A's surface — including its scroll
  and its already-read catalog — exactly as it was, and coming back to
  A costs no second read.
- Detached windows were already per object (`open_properties_tab`
  dedupes on the same key, and `_focus_tab` raises the pop-out), so two
  windows on different objects are two independent views and both stay
  live.
- Lifetime: `_release_properties_surface` drops an object's panel
  surface when a tab about it closes, unless another open tab is still
  about the same object (a table tab and its definition tab share one).
  It runs on idle, after the closing page has left the view and the
  panel has followed the tab taking its place. Surfaces made for
  objects that never had a tab — the sidebar's Properties item — are
  bounded by the stack's cap (8), least recently shown evicted first.
