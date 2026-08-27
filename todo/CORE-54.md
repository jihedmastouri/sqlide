## CORE-54 — Users/roles page: scope the tree and name the subject

- **Status:** done
- **Depends on:** CORE-10, CORE-12

### Problem

The permission editor's object tree lists everything, including objects that
cannot carry a grant for the selected principal, so most of what you scroll past
is noise. And once you have scrolled, nothing on screen says whose permissions
you are editing.

### Goal

The tree shows only what you can actually grant on, and the subject is always
visible.

### Approach

- Filter the tree by what the provider says is grantable for this engine and this
  principal: hide node types with no grant model, and hide branches whose whole
  subtree is ungrantable. Drive this from the capability flags, not a hard-coded
  list — SQLite, which supports no grants at all, should say so rather than show
  an empty tree.
- Put a header above the split view naming the principal (name, type, and whether
  it is a login role), pinned while the tree scrolls.

### Acceptance criteria

- [x] Object types that cannot take a grant do not appear in the tree.
- [x] A folder whose children are all ungrantable is hidden too, not shown empty.
- [x] The filtering comes from provider capabilities; no engine names in the UI.
- [x] The selected principal is named at the top of the page at all times.
- [x] An engine with no grant support shows an explanation, not a blank tree.
