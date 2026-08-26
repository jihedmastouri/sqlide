"""The PostgreSQL metadata provider.

Driver-free on purpose (see db/metadata.py): the UI reads these flags
before psycopg is ever imported, so nothing here may touch connector.py.

Shape: connection → database → schema → object. A schema is a level of
its own — the same table name lives in as many schemas as you like, and
flattening them is how `public.orders` and `staging.orders` become one
row that resolves to whichever the search path found first.

Minimum supported server: PostgreSQL 10, the oldest in the test matrix
(tests/conftest.py). Partitioned tables — relkind 'p' — arrived in 10,
so every supported version answers the catalog queries the adapter
makes for these listings.
"""

from __future__ import annotations

from sqlide.backend.db.metadata import Capabilities, MetadataProvider, NodeRef


class PostgresMetadata(MetadataProvider):
    HIERARCHY = ("connection", "database", "schema", "object")
    #: A role's attributes as pg_roles records them. No host column:
    #: where a role may connect from is pg_hba.conf's business, not the
    #: catalog's.
    PRINCIPAL_COLUMNS = (
        "Name", "Type", "Login", "Superuser", "Create DB", "Create role",
        "Member of", "Valid until", "Connection limit",
    )
    CAPABILITIES = Capabilities(
        databases=True,
        schemas=True,
        materialized_views=True,
        procedures=True,
        grants=True,
        roles=True,
        extensions=True,
        partitions=True,
        constraints=True,
        rules=True,
        policies=True,
        dependencies=True,
        related_functions=True,
        permission_editor=True,
        # DDL is transactional here, GRANT included: the editor's
        # statements either all land or none of them do.
        transactional_grants=True,
    )

    #: What PostgreSQL lets you grant, per object kind. A database
    #: takes CONNECT/CREATE/TEMPORARY, a schema USAGE/CREATE, a
    #: relation the seven table privileges, a routine EXECUTE, and a
    #: column the four privileges that can name one.
    OBJECT_PRIVILEGES = {
        "database": ("CONNECT", "CREATE", "TEMPORARY"),
        "schema": ("USAGE", "CREATE"),
        "table": (
            "SELECT", "INSERT", "UPDATE", "DELETE",
            "TRUNCATE", "REFERENCES", "TRIGGER",
        ),
        "view": (
            "SELECT", "INSERT", "UPDATE", "DELETE",
            "TRUNCATE", "REFERENCES", "TRIGGER",
        ),
        "function": ("EXECUTE",),
        "procedure": ("EXECUTE",),
    }
    # Columns are left out on purpose: information_schema.column_privileges
    # also reports the columns a *table*-level grant covers, so a column
    # checkbox here could not tell "granted on this column" from "granted
    # on the table", and unticking one would build a REVOKE that does not
    # do what the box says. Column grants stay in the Grant… dialog.

    def grant_target(self, ref: NodeRef) -> str:
        """The object as GRANT names it. The connection row is not one:
        cluster-wide rights are role attributes (SUPERUSER, CREATEDB),
        set with ALTER ROLE rather than granted on a target."""
        quote = self.connector.quote_ident
        if ref.kind == "database":
            return f"DATABASE {quote(ref.name)}"
        if ref.kind == "schema":
            return f"SCHEMA {quote(ref.name)}"
        schema = ref.schema or self._safe_schema()
        if ref.kind in ("table", "view"):
            return f"TABLE {quote(schema)}.{quote(ref.name)}"
        if ref.kind in ("function", "procedure"):
            keyword = "FUNCTION" if ref.kind == "function" else "PROCEDURE"
            return f"{keyword} {quote(schema)}.{quote(ref.name)}"
        return ""

    def _safe_schema(self) -> str:
        """The schema a node that names none belongs to: the one the
        search path resolves it in, as the sidebar found it."""
        try:
            return self.connector.current_schema() or "public"
        except Exception:
            return "public"

    def _current_database(self) -> str:
        return getattr(self.connector, "database", "") or ""

    def _database_children(self, ref: NodeRef) -> list[NodeRef]:
        """Schemas, not categories: here the objects belong to a schema
        and the tree has to say which one."""
        current = self.connector.current_schema()
        return [
            NodeRef(
                "schema", name,
                database=ref.database or ref.name,
                schema=name,
                detail="current" if name == current else "",
            )
            for name in self.connector.list_schemas()
        ]

    def _objects(self, ref: NodeRef):
        """The tables and views of the schema this folder was opened
        under; the search path's, for a folder that names none."""
        if ref.schema:
            return self.connector.list_tables_in(ref.schema)
        return self.connector.list_tables()
