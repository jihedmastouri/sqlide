## CORE-47 — Table properties live in the right side panel, not a tab toggle

- **Status:** todo
- **Depends on:** CORE-04, CORE-09
- **Supersedes:** part of CORE-04 (the Data|Properties toggle) and CORE-05's
  in-tab deep-link target

### Problem

CORE-04 put properties behind a Data|Properties toggle inside the table tab. That
buries them: you lose sight of the data to look at a column type, and the toggle
is a mode the tab has to remember. The right side panel already exists (notes,
CORE-09) and is the natural home for "information about the thing in front of
you".

### Goal

Properties are a panel you glance at beside the data, and can be torn off into a
window of their own when you want room to work.

### Approach

- Move the property sections into a section of the right side panel that follows
  the active tab's object.
- Remove the Data|Properties toggle from the table tab; the tab shows data.
- Add "Properties" / "Properties (Window)" to the sidebar right-click menu and to
  the tab context menu. The window reuses the same widget and stays live.
- Editing happens in the detached window (read-only in the panel is acceptable
  for now if editing is not yet implemented for that section) — say which in the
  ticket notes once implemented.
- CORE-05's deep-link should now scroll the panel (or open the window) to the
  requested section rather than switching a tab mode. Update CORE-05's doc notes.

### Acceptance criteria

- [ ] The table tab has no Data|Properties toggle; opening a table shows data.
- [ ] The right panel shows the active object's properties and follows tab
      switches, including to non-table objects.
- [ ] Sidebar right-click offers Properties and Properties (Window); both work
      from any node type that has properties.
- [ ] A detached properties window survives its originating tab being closed, or
      closes with a clear reason — pick one and document it.
- [ ] Deep-links from CORE-05 land on the right section in whichever surface is
      showing.
- [ ] Panel width is persisted through the config layer.
