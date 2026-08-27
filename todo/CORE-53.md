## CORE-53 — Keep grants out of the right side panel for users and roles

- **Status:** done
- **Depends on:** CORE-47, CORE-12

### Problem

Selecting a user or role fills the right side panel with its permissions. That
list is long, slow to fetch, and duplicates the permission editor, which is the
place actually built for it.

### Goal

The side panel stays a lightweight summary for principals.

### Approach

Drop the grants section from the panel's descriptor for user and role nodes, and
show the summary attributes instead (login/nologin, member-of, expiry, connection
limit — whatever the provider reports). Link to the permission editor rather than
inlining what it shows.

### Acceptance criteria

- [ ] Selecting a user or role shows no grants list in the right panel.
- [ ] The panel shows the principal's own attributes.
- [ ] A control takes the user to the permission editor for that principal.
- [ ] No grant queries are issued when a principal is merely selected.
- [ ] Grants for ordinary objects (CORE-11) are unaffected.
