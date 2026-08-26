"""The MySQL metadata provider.

Driver-free on purpose (see db/metadata.py).

Shape: connection → database → object. In MySQL a schema *is* a
database, so there is no schema level to add — a second one would only
repeat the first.

Minimum supported server: MySQL 5.7, the oldest in the test matrix
(tests/conftest.py). Roles arrived in 8.0: on 5.7 the role catalog is
missing and list_users() answers with accounts alone, which is a
shorter list rather than an error (db/metadata.py `_safe`).
"""

from __future__ import annotations

from sqlide.backend.db.metadata import (
    Capabilities,
    MetadataProvider,
    NodeRef,
)


class MysqlMetadata(MetadataProvider):
    HIERARCHY = ("connection", "database", "object")
    #: An account here is 'name'@'host', so the host is a column of its
    #: own; the rest is what mysql.user records. Roles (8.0) are
    #: accounts too, and appear in the same table with type "role".
    PRINCIPAL_COLUMNS = (
        "Name", "Host", "Type", "Login", "Locked", "Plugin",
        "Password expiry", "Member of",
    )
    CAPABILITIES = Capabilities(
        databases=True,
        procedures=True,
        events=True,
        grants=True,
        roles=True,
        partitions=True,
        constraints=True,
        account_hosts=True,
        permission_editor=True,
        # GRANT commits as it runs here — MySQL gives DDL no
        # transaction to roll back — so the editor stops at the
        # statement that failed and says which one it was.
        transactional_grants=False,
    )

    #: MySQL's own grant list, per level: the same privileges narrow as
    #: the target does. Global takes the administrative rights too
    #: (RELOAD, PROCESS, SHUTDOWN); a database or a table takes only
    #: what applies there; a column takes the four that can name one.
    _TABLE_PRIVILEGES = (
        "SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "DROP",
        "ALTER", "INDEX", "REFERENCES", "CREATE VIEW", "SHOW VIEW",
        "TRIGGER",
    )
    _DATABASE_PRIVILEGES = _TABLE_PRIVILEGES + (
        "CREATE ROUTINE", "ALTER ROUTINE", "EXECUTE", "EVENT",
        "LOCK TABLES", "CREATE TEMPORARY TABLES",
    )
    OBJECT_PRIVILEGES = {
        "connection": _DATABASE_PRIVILEGES + (
            "RELOAD", "PROCESS", "SHUTDOWN", "SUPER",
            "CREATE USER", "REPLICATION CLIENT", "REPLICATION SLAVE",
        ),
        "database": _DATABASE_PRIVILEGES,
        "table": _TABLE_PRIVILEGES,
        "view": _TABLE_PRIVILEGES,
        "column": ("SELECT", "INSERT", "UPDATE", "REFERENCES"),
    }

    def grant_target(self, ref: NodeRef) -> str:
        """The object as MySQL's GRANT names it: `*.*` for the server,
        `db`.* for a database, `db`.`table` for a table — and, for a
        column, the table it belongs to, because the column goes in
        beside the privilege instead."""
        quote = self.connector.quote_ident
        if ref.kind == "connection":
            return "*.*"
        if ref.kind == "database":
            return f"{quote(ref.name)}.*"
        database = ref.database or self._current_database()
        if not database:
            return ""
        if ref.kind in ("table", "view"):
            return f"{quote(database)}.{quote(ref.name)}"
        if ref.kind == "column" and ref.table:
            return f"{quote(database)}.{quote(ref.table)}"
        return ""

    def privilege_suffix(self, ref: NodeRef) -> str:
        """A column grant names its column next to the privilege:
        GRANT SELECT (`total`) ON `sales`.`orders`."""
        if ref.kind == "column":
            return f" ({self.connector.quote_ident(ref.name)})"
        return ""

    def _current_database(self) -> str:
        return getattr(self.connector, "database", "") or ""
