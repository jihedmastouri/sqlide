## CORE-57 — Sidebar rows are too short and clip their text

- **Status:** done
- **Depends on:** CORE-51

### Problem

Sidebar rows are not tall enough for the font they render. Ascenders and capitals
— a plain `T` — are visibly cut off at the top. CORE-51 fixed horizontal
truncation; this is the vertical counterpart, and it is a straight rendering bug,
not a taste question.

### Goal

Every glyph in a sidebar row is fully drawn, at any font size the user has set.

### Approach

Find the actual cause before adding padding. Likely candidates: a fixed row
height or `height-request` that does not follow the font, a label with ellipsize
set but no room for its logical extents, or a negative margin from CORE-51's
truncation work. Whatever it is, the row height should derive from the font's
line height plus padding rather than a hard-coded pixel count, so it survives a
larger interface font or a different theme.

Check both the name and the dimmed secondary text, at the deepest indentation,
with a descender and a capital in the sample (`Tgjy`).

### Cause

Not CORE-51: the secondary label's ellipsize and zero width request are
horizontal only and cost the row no height. The clipping came from
`listview.schema-tree > row` in `frontend/style.css`, which pinned the row at
`min-height: 28px` with 1px of padding. 28px is ample at the default interface
font and nothing at a larger one: the row stopped growing where the label inside
it kept growing, so the label's box outgrew the row's content box and the
ListView clipped the overflow — the top bar of a `T`, the tail of a `g`.
Measured on a bound row, the slack between the name label's allocation and the
height its own text needs fell from 11px at 11pt to exactly 0 from about 20pt up.

### Fix

`min-height: 2em` (of the row's own 0.92em font) with 2px of padding. That is
the same ~28px at the default font — rows are no taller than they were — and it
tracks the font from there. Covered by `tests/test_sidebar_rows.py`, which
measures the real room a bound row's label gets at two interface font sizes.

### Acceptance criteria

- [x] No glyph is clipped at the top or bottom of a sidebar row, including
      capitals and descenders.
- [x] Row height follows the interface font size rather than a fixed constant —
      verified at a noticeably larger font setting.
- [x] Rows stay visually compact; this is a clipping fix, not an invitation to
      double the row height.
- [x] The dimmed secondary text is not clipped either.
- [x] CORE-51's horizontal truncation still holds.
