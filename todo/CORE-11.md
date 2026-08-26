## CORE-11 — Grantees section on object properties

- **Status:** done
- **Depends on:** CORE-04, CORE-10

### Goal

The inverse view of CORE-10: from a table/index/function, see who has access.

### Acceptance criteria

- [x] Properties/info views for grantable objects include a **Permissions**
      section listing principal, privilege(s), direct vs inherited, and grantor.
- [x] Public/`PUBLIC` grants are shown explicitly.
- [x] A row links through to that principal in the CORE-10 editor, pre-scoped to
      this object.
- [x] Hidden on engines/objects without a grant model.



### Notes

- The section is filled by the provider layer, not by `db/objects.py`:
  who holds a grant is a question about accounts and role membership,
  which the plain `Connector` interface cannot answer.
  `MetadataProvider.object_grants(ref)` returns `GrantEntry` rows
  (principal, privilege, `via` role, grantor, grant option) and
  `_with_permissions` appends the section to both `describe()` and
  `table_properties()`, so an object's info view and a table tab's
  Properties side show the same thing.
- `permissions` is a new `objects.PROPERTY_SECTIONS` slug gated on the
  `grants` capability, so SQLite never offers it — in the properties
  view or as a sidebar section row (CORE-05).
- Inherited rows are derived, not read: a grant recorded against a role
  is reported a second time against every account that is a member of
  it, naming the role. PUBLIC is one row saying "everyone" rather than
  one row per account, and it links nowhere — there is no principal for
  the editor to open on.
- `PrivilegeInfo` grew a `grantor`; PostgreSQL reads it from
  `information_schema.table_privileges`, MySQL's copy of that view has
  no GRANTOR column so the cell stays empty rather than invented.
- The link is an `ObjectRef(kind="principal", …)` carrying the object
  in `table`/`category`; `window.open_principal_permissions` routes it
  to the users tab, which matches the grantee against the real account
  list ('app'@'%' and app find the same row) and pushes the CORE-10
  editor with `scope=` set, so the grid opens on this object.
