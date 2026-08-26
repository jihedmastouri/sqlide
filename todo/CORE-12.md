## CORE-12 — Users/roles list overview

- **Status:** done
- **Depends on:** CORE-02

### Goal

Clicking the Users/Roles node shows a useful list, not just a set of leaves.

### Acceptance criteria

- [x] A sortable, filterable table of principals.
- [x] Columns per engine capability: name, type (user/role/group), login allowed,
      superuser/admin, can create db/role, member of, valid until, connection
      limit; MySQL adds host, plugin, locked, password expiry.
- [x] A row opens that principal's info view, which links to CORE-10.
- [x] Handles hundreds of rows without lag.

### Notes

- The columns are a provider answer: `MetadataProvider.principal_columns()`
  names the attributes an engine records and `principal_table()` renders
  the rows, so `frontend/users_tab.py` draws MySQL's host/plugin/locked
  columns and PostgreSQL's superuser/create-db/valid-until ones without
  naming either engine. `registry.principal_columns(kind)` answers with
  no connection open, and SQLite (no `roles` capability) answers `()`.
- `UserInfo` grew the attributes behind those columns (kind, superuser,
  create_db, create_role, member_of, valid_until, connection_limit,
  plugin, locked, password_expiry); every one is optional, so an adapter
  fills what its catalog has. Memberships come from `pg_auth_members`
  and `mysql.role_edges`, each in its own query — a catalog this login
  cannot read costs that column, not the list.
- The list page is a `Gtk.ColumnView` over a ListStore behind a filter
  and a sort model, so only visible rows become widgets: hundreds of
  accounts scroll like ten. A row activates into the same account page
  as before, whose Permissions… button opens the CORE-10 editor.


