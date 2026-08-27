"""The MySQL metadata provider.

Driver-free on purpose (see db/metadata.py).

Shape: connection → database → object. In MySQL a schema *is* a
database, so there is no schema level to add — a second one would only
repeat the first.

The folders each level shows are declared here (MY-01) rather than in
the sidebar: a database holds its relations, its routines — functions
and procedures are different things here, so each gets a folder — its
indexes, triggers and scheduled events; the connection holds its
databases plus what belongs to the server, which is its accounts, its
settings, and an Administer folder holding both one level down. Every
one of them is a plain category node, so it opens the generic object
info view like any other row (CORE-01).

Because a schema *is* a database, the server's own schemas are
databases in this tree: `information_schema`, `mysql`,
`performance_schema` and `sys` are listed, dimmed and sorted last, the
same treatment PostgreSQL's catalog schemas get (PG-03).

Minimum supported server: MySQL 5.7, the oldest in the test matrix
(tests/conftest.py). Roles arrived in 8.0: on 5.7 the role catalog is
missing and list_users() answers with accounts alone, which is a
shorter list rather than an error (db/metadata.py `_safe`).
"""

from __future__ import annotations

from sqlide.backend.db.metadata import (
    CATALOG_CATEGORIES,
    CATEGORY_LABELS,
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
    #: The databases the server owns rather than the user. A schema is
    #: a database here, so these answer `is_system_schema` and
    #: `is_system_database` alike: the tree shows them dimmed and last,
    #: and the console's database switcher leaves them out (MY-01).
    SYSTEM_SCHEMAS = (
        "information_schema", "performance_schema", "mysql", "sys",
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

    #: The folders each level hangs off itself, beside the rows under
    #: it (CORE-02):
    #:
    #: * a database holds everything that lives in it — a schema is a
    #:   database here, so its objects hang one level higher than they
    #:   do on PostgreSQL;
    #: * the connection, after its databases: the accounts, the
    #:   server's settings, and Administer holding both again one
    #:   folder down, so the row does not grow a listing per chore.
    LEVEL_CATEGORIES = {
        "connection": ("users", "administer", "system_info"),
        "database": (
            "tables",
            "views",
            "indexes",
            "procedures",
            "functions",
            "triggers",
            "events",
        ),
    }
    #: What Administer holds.
    ADMINISTER_CATEGORIES = ("users", "system_info")

    @classmethod
    def is_system_database(cls, name: str) -> bool:
        """A schema is a database here, so the two questions are one."""
        return cls.is_system_schema(name)

    def tree_categories(self, ref: NodeRef) -> list[NodeRef]:
        return [
            self.category(ref, slug)
            for slug in self.LEVEL_CATEGORIES.get(ref.kind, ())
        ]

    def categories(self, ref: NodeRef) -> list[NodeRef]:
        """A database's folders. Overridden so the shared categories
        and the catalog ones come back in one declared order, and so
        Procedures and Functions are two folders rather than the one
        the generic set has."""
        if ref.kind == "database":
            return self.tree_categories(ref)
        return super().categories(ref)

    def category(self, ref: NodeRef, slug: str) -> NodeRef:
        """One folder row, whichever vocabulary its slug comes from."""
        if slug in CATALOG_CATEGORIES:
            return self.catalog_category(ref, slug)
        return ref.child("category", CATEGORY_LABELS[slug], category=slug)

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
