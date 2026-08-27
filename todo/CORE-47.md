## CORE-47 — Table properties live in the right side panel, not a tab toggle

- **Status:** done
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

- [x] The table tab has no Data|Properties toggle; opening a table shows data.
- [x] The right panel shows the active object's properties and follows tab
      switches, including to non-table objects.
- [x] Sidebar right-click offers Properties and Properties (Window); both work
      from any node type that has properties.
- [x] A detached properties window survives its originating tab being closed, or
      closes with a clear reason — pick one and document it.
- [x] Deep-links from CORE-05 land on the right section in whichever surface is
      showing.
- [x] Panel width is persisted through the config layer.

### Notes

- The surface is `object_info.PropertiesView`, grown out of CORE-04's
  `TablePropertiesView`: it takes a `(profile, ObjectRef)` target
  rather than a table name, so a table or a view is described by
  `table_properties` and anything else — an index, a function, a
  folder — by its own `describe` descriptor. It reads the catalog on
  the frame it first becomes visible (`map`), so a panel page nobody
  opens costs nothing.
- The window owns one instance for the panel and retargets it in
  `_update_active_panel` (`_properties_target` reads the object off
  the active tab); a detached window gets its own instance, so the two
  never fight over what is on screen. CORE-50 will key those per
  object — nothing here caches by identity beyond the tab dedupe key
  (`object_info.properties_key`).
- **Detached windows survive**: a properties window is its own
  `PropertiesView` holding nothing of the tab it was opened from, so
  closing that tab leaves it standing. It is session-only (`tab_state`
  returns None): it is a view of something the workspace already
  remembers, not a tab to restore.
- **Editability**: every properties surface is read-only, in the panel
  and in a detached window alike. Editing an object stays where it
  already was — the table designer, the definition tab, the permission
  editor and the data grid's cell editing — rather than a second way
  to write the same catalog. The detached window is room to read, not
  yet room to edit.
- Deep links (CORE-05) reveal the panel on the named section, unless a
  detached window for that object is already open, in which case it is
  focused and scrolled instead — a link lands where the user reads.
- Panel width is a preference like the sidebar's:
  `settings.side_panel_width`, clamped 260–900, debounced on drag and
  documented in docs/configuration.md.
- The tab's Map side (PG-04) stays a side of the tab, since it draws
  the rows loaded there; its Data | Map switch is hidden until a load
  finds geometries, so a plain table tab shows no switch at all.
