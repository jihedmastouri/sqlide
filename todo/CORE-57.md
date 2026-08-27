## CORE-57 — Sidebar rows are too short and clip their text

- **Status:** todo
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

### Acceptance criteria

- [ ] No glyph is clipped at the top or bottom of a sidebar row, including
      capitals and descenders.
- [ ] Row height follows the interface font size rather than a fixed constant —
      verified at a noticeably larger font setting.
- [ ] Rows stay visually compact; this is a clipping fix, not an invitation to
      double the row height.
- [ ] The dimmed secondary text is not clipped either.
- [ ] CORE-51's horizontal truncation still holds.
