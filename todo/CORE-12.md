## CORE-12 — Users/roles list overview

- **Status:** todo
- **Depends on:** CORE-02

### Goal

Clicking the Users/Roles node shows a useful list, not just a set of leaves.

### Acceptance criteria

- [ ] A sortable, filterable table of principals.
- [ ] Columns per engine capability: name, type (user/role/group), login allowed,
      superuser/admin, can create db/role, member of, valid until, connection
      limit; MySQL adds host, plugin, locked, password expiry.
- [ ] A row opens that principal's info view, which links to CORE-10.
- [ ] Handles hundreds of rows without lag.


