## CORE-51 — Truncate the sidebar's secondary text instead of widening the tree

- **Status:** todo
- **Depends on:** CORE-08

### Problem

The small dimmed text beside a node's name (type, row count, detail) grows the
tree's natural width, so a long value pushes the sidebar wider than the user set
it and the name itself gets pushed out of view.

### Goal

The sidebar's width is the user's decision. Secondary text yields to it.

### Approach

Give the secondary label ellipsizing and a zero natural width so it never
contributes to the tree's width request, and let the name take priority when
space is short. The full text goes in a tooltip.

Check this against the deepest indentation level, not just top-level rows.

### Acceptance criteria

- [ ] Long secondary text ellipsizes; the sidebar keeps its configured width.
- [ ] The node name is never truncated before the secondary text is.
- [ ] The untruncated value is available on hover.
- [ ] Resizing the sidebar narrower does not clip the name away entirely; there is
      a sensible minimum.
- [ ] Deeply nested rows behave the same as top-level ones.
