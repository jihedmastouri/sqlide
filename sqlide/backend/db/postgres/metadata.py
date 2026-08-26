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
    )

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
