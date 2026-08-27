"""The PostgreSQL metadata provider.

Driver-free on purpose (see db/metadata.py): the UI reads these flags
before psycopg is ever imported, so nothing here may touch connector.py.

Shape: connection → database → schema → object. A schema is a level of
its own — the same table name lives in as many schemas as you like, and
flattening them is how `public.orders` and `staging.orders` become one
row that resolves to whichever the search path found first.

The folders each level shows are declared here (PG-02) rather than in
the sidebar: a schema holds relations, routines, sequences, types and
aggregates; a database holds its schemas plus the things that belong
to the database and not to any one schema (event triggers, extensions,
storage, roles); the connection holds its databases plus what is the
server's own. Every one of them is a plain category node, so it opens
the generic object info view like any other row (CORE-01).

Minimum supported server: PostgreSQL 10, the oldest in the test matrix
(tests/conftest.py). Partitioned tables — relkind 'p' — arrived in 10,
so every supported version answers the catalog queries the adapter
makes for these listings.
"""

from __future__ import annotations

from dataclasses import replace

from sqlide.backend.db.metadata import (
    CATALOG_CATEGORIES,
    RELATION_FOLDERS,
    CATEGORY_LABELS,
    Capabilities,
    MetadataProvider,
    NodeRef,
    _safe,
)


class PostgresMetadata(MetadataProvider):
    HIERARCHY = ("connection", "database", "schema", "object")
    #: A role's attributes as pg_roles records them. No host column:
    #: where a role may connect from is pg_hba.conf's business, not the
    #: catalog's.
    PRINCIPAL_COLUMNS = (
        "Name", "Type", "Login", "Superuser", "Create DB", "Create role",
        "Member of", "Valid until", "Connection limit",
    )
    #: What the server owns rather than the user: the SQL-standard
    #: catalog and every `pg_*` schema (pg_catalog, pg_toast, a
    #: session's pg_temp_N). Shown dimmed and last (PG-03).
    SYSTEM_SCHEMAS = ("information_schema",)
    SYSTEM_SCHEMA_PREFIXES = ("pg_",)
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

    #: The keyword GRANT names each kind of target with.
    _GRANT_KEYWORDS = {
        "table": "TABLE", "view": "TABLE",
        "function": "FUNCTION", "procedure": "PROCEDURE",
    }

    def grant_target(self, ref: NodeRef) -> str:
        """The object as GRANT names it, schema-qualified and quoted
        part by part (PG-01) — `TABLE "staging"."orders"`, never the
        bare name whichever search path happens to resolve.

        The connection row is not a target: cluster-wide rights are
        role attributes (SUPERUSER, CREATEDB), set with ALTER ROLE
        rather than granted on anything.
        """
        quote = self.connector.quote_ident
        if ref.kind == "database":
            return f"DATABASE {quote(ref.name)}"
        if ref.kind == "schema":
            return f"SCHEMA {quote(ref.name)}"
        keyword = self._GRANT_KEYWORDS.get(ref.kind)
        if keyword is None:
            return ""
        if not ref.schema:
            # A node that names no schema is one the search path found,
            # so that is the schema it is in.
            ref = replace(ref, schema=self._safe_schema())
        return f"{keyword} {self.quoted_name(ref)}"

    def _safe_schema(self) -> str:
        """The schema a node that names none belongs to: the one the
        search path resolves it in, as the sidebar found it."""
        try:
            return self.connector.current_schema() or "public"
        except Exception:
            return "public"

    #: The folders each level of the tree shows (PG-02):
    #:
    #: * a schema: the relations first, then the routines and the
    #:   declarations. Tables, Views, Indexes and Functions are the
    #:   shared categories (metadata.CATEGORIES); the rest are catalog
    #:   folders (objects.CATALOG_CATEGORIES);
    #: * a database, after its schemas: what belongs to the database
    #:   rather than to any schema in it;
    #: * the connection, after its databases: what is the server's own,
    #:   with Administer holding the server-wide listings one folder
    #:   down so the connection row does not grow five of them.
    LEVEL_CATEGORIES = {
        "connection": ("administer", "system_info"),
        "database": (
            "event_triggers", "extensions", "storage", "system_info",
            "roles",
        ),
        "schema": (
            "tables",
            "foreign_tables",
            "views",
            "materialized_views",
            "indexes",
            "functions",
            "sequences",
            "data_types",
            "aggregates",
        ),
    }
    #: What Administer holds.
    ADMINISTER_CATEGORIES = ("roles", "storage", "system_info")

    def tree_categories(self, ref: NodeRef) -> list[NodeRef]:
        return [
            self.category(ref, slug)
            for slug in self.LEVEL_CATEGORIES.get(ref.kind, ())
        ]

    def categories(self, ref: NodeRef) -> list[NodeRef]:
        """A schema's folders. Overridden so the shared categories and
        the catalog ones come back interleaved in one order — Foreign
        Tables belongs next to Tables, not after everything the base
        class happens to know about."""
        if ref.kind == "schema":
            return self.tree_categories(ref)
        return super().categories(ref)

    def category(self, ref: NodeRef, slug: str) -> NodeRef:
        """One folder row, whichever vocabulary its slug comes from."""
        if slug in CATALOG_CATEGORIES:
            return self.catalog_category(ref, slug)
        return ref.child("category", CATEGORY_LABELS[slug], category=slug)

    def _category_children(self, ref: NodeRef) -> list[NodeRef]:
        """The relation folders read the schema's relations directly,
        so a partitioned table, a foreign table and a materialized
        view each land in the folder that names them instead of all
        four kinds sharing Tables and Views."""
        slug = ref.category or ref.name.lower()
        wanted = RELATION_FOLDERS.get(slug)
        if wanted is not None:
            kind, notes = wanted
            return [
                ref.child(
                    kind, info.name, category=slug,
                    detail=self.object_detail(info),
                )
                for info in self._objects(ref)
                if info.kind == kind and self.object_detail(info) in notes
            ]
        return super()._category_children(ref)

    def object_detail(self, info) -> str:
        """A relation's one-line note: what makes it a special case of
        its kind ("partitioned"), empty for a plain one. The sidebar
        shows it next to the name, which is how a partitioned table is
        told from a plain one without a second icon (PG-02)."""
        return getattr(info, "detail", "")

    def list_children(self, ref: NodeRef) -> list[NodeRef]:
        """A partitioned table's partitions hang under it, after its
        columns, so the pieces of one table are browsable as one thing
        and each of them opens as the table it is (PG-02). Every other
        node keeps the shared behaviour."""
        children = super().list_children(ref)
        if ref.kind == "table" and ref.detail == "partitioned":
            children += [
                ref.child(
                    "table", partition.name, table=ref.name,
                    detail=partition.detail or "partition",
                )
                for partition in _safe(
                    lambda: self.connector.list_partitions(ref.name), []
                )
            ]
        return children

    def _current_database(self) -> str:
        return getattr(self.connector, "database", "") or ""

    def _database_children(self, ref: NodeRef) -> list[NodeRef]:
        """Schemas, not categories: here the objects belong to a schema
        and the tree has to say which one."""
        current = self.connector.current_schema()
        names = self.connector.list_schemas(include_system=True)
        return [
            NodeRef(
                "schema", name,
                database=ref.database or ref.name,
                schema=name,
                detail="current" if name == current else "",
                system=self.is_system_schema(name),
            )
            # The server's own schemas come after the user's: they are
            # there to be looked into, not worked in (PG-03).
            for name in sorted(
                names,
                key=lambda n: (self.is_system_schema(n), n != current, n),
            )
        ] + self.tree_categories(ref)

    def _objects(self, ref: NodeRef):
        """The tables and views of the schema this folder was opened
        under; the search path's, for a folder that names none."""
        if ref.schema:
            return self.connector.list_tables_in(ref.schema)
        return self.connector.list_tables()
