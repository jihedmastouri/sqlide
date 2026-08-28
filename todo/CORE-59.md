## CORE-59 — Unify the object info view and the properties view

- **Status:** done
- **Depends on:** CORE-47, CORE-49, CORE-56

### Problem

`ObjectInfoTab` and `PropertiesView` show nearly the same thing — a header, a
key/value summary, detail sections, DDL — from the same descriptors, through two
code paths that have already drifted. Every change to how an object is presented
has to be made twice, and CORE-56 added a third caller shape on top.

### Goal

One renderer for "what this object is", hosted in whichever surface asked for it.

### Approach

Treat the difference as host, not content. Keep one widget driven by the CORE-01
descriptor and CORE-49's tabular sections, and give it the small number of knobs
the hosts actually differ on:

- density — the side panel is narrow, a tab is not
- which sections are shown — CORE-53 already drops grants for principals in the
  panel
- whether a section can be deep-linked to (CORE-05)

`ObjectInfoTab` and `PropertiesView` become thin hosts around it, or one of them
disappears entirely if the other's host is sufficient. Keep the per-object
surface identity from CORE-50 and the tab-vs-info routing from CORE-56 — this
ticket unifies rendering, not destinations.

This is a refactor: the visible result should be that the two surfaces agree,
not that either grows features.

### Acceptance criteria

- [x] One renderer builds both surfaces; no duplicated section-building logic
      remains between them.
- [x] The panel and a tab showing the same object agree on content, differing
      only by density and the documented section rules.
- [x] CORE-53's rule (no grants for principals in the panel) still holds and is
      expressed as a host rule, not a second code path.
- [x] Deep-links (CORE-05) still land on the right section.
- [x] CORE-56's routing is unchanged: collections open as grid tabs, single
      objects as info.
- [x] Existing tests for both surfaces pass, consolidated where they duplicated
      each other.
