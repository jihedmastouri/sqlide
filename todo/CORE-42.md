## CORE-42 — Value panel and record view for wide cells

- **Status:** todo
- **From:** RS-04 (see `docs/dbeaver-comparison.md`)

### Problem

A cell wider than its column cannot be read. `_display_text` in
`frontend/data_grid.py` ellipsizes into a label; the only editor is
the one-line inline entry `_setup_editable` installs; binary values
become a hex string via `_hex`. So a JSON document, a long
description, a stack trace in a log table or a blob is effectively
invisible, and the row with forty columns has to be read by scrolling
sideways.

DBeaver answers both with two of its most-used views: a value panel
beside the grid showing the focused cell in full, and a record view
that pivots one row into a name/value list.

### Goal

Read and edit the focused cell in full, and read one row as a vertical
list, without leaving the tab.

### Approach

- **Value page in the existing right side panel.** `side_panel.py`
  already switches its pages on tab context ("table", "grid",
  "console"); add a Value page for the grid contexts, fed by the
  focused cell the grid already tracks for selection.
- Renderers picked from the value, not the engine: plain text
  (wrapped, monospace), JSON (parsed and pretty-printed through
  GtkSourceView with the existing highlighting, falling back to plain
  text when it does not parse), and binary (hex + ASCII columns,
  size, and the type name the geo parser reports for a geometry —
  `backend/db/geo.py` already describes these, e.g. *Point, SRID 4326*).
- Editing from the panel writes back into the same `_pending`
  mechanism as an inline edit, so it inherits the preview dialog,
  CORE-39's transaction and the read-only gating. No second write
  path.
- **Record view** as a toggle in the grid's own view switch, beside
  Data / Properties / Map: the focused row as column-name / value
  rows, with the same value renderers and the same editability. Up and
  down move between rows.
- Value-to-file and file-to-value for blobs is CORE-36/CORE-37's
  machinery, not new code here.

### Acceptance criteria

- [ ] Focusing a cell fills the Value page; it follows the focus
      without the user re-opening it.
- [ ] JSON is pretty-printed when parseable and shown verbatim when
      not — never silently reformatted into something that does not
      round-trip.
- [ ] Binary shows hex, byte length and, for a geometry column, the
      description `backend/db/geo.py` already produces.
- [ ] Editing in the panel produces the same pending edit as editing
      inline, and appears in the same preview dialog.
- [ ] Record view shows every column of the focused row, respects the
      read-only rules, and moves with up/down.
- [ ] A very large value is truncated for display with the full size
      stated, rather than freezing the UI.
