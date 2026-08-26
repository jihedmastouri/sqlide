## CORE-11 — Grantees section on object properties

- **Status:** todo
- **Depends on:** CORE-04, CORE-10

### Goal

The inverse view of CORE-10: from a table/index/function, see who has access.

### Acceptance criteria

- [ ] Properties/info views for grantable objects include a **Permissions**
      section listing principal, privilege(s), direct vs inherited, and grantor.
- [ ] Public/`PUBLIC` grants are shown explicitly.
- [ ] A row links through to that principal in the CORE-10 editor, pre-scoped to
      this object.
- [ ] Hidden on engines/objects without a grant model.


