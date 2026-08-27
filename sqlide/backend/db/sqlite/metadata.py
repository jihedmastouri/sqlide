"""The SQLite metadata provider.

Driver-free on purpose (see db/metadata.py).

Shape: connection → object. One file is one database, so a database
level would be an empty step the user has to click through.

Nothing here has accounts: SQLite's permissions are the file's, which
means `grants` and `roles` are off and list_grants()/list_principals()
answer with empty lists rather than a screen that cannot be filled.
PRAGMAs are the settings surface instead (SQ-02).

The tree it declares is one level deep: the connection is the
database, so its folders hang straight off the connection row (SQ-01).
What they hold is SQLite as it is rather than as other engines are —
Sequences is `sqlite_sequence` and is empty where nothing declared
AUTOINCREMENT, Functions is the built-in and registered functions and
says it is read-only, Data Types is the storage classes and the
affinity rules, and the triggers folder is called Table Triggers
because a table's is the only kind there is. Keys are a folder of
their own under a table, beside the full constraint list.

There is no constraint catalog either, but the `constraints` capability
is on: the adapter reads them back off the PRAGMAs (see
sqlite/connector.py), so a table's properties can still list its keys.

Minimum supported version: SQLite 3.25 — the release that gave
ALTER TABLE … RENAME COLUMN, which the definition tab already relies on.
"""

from __future__ import annotations

from sqlide.backend.db.metadata import (
    CATALOG_CATEGORIES,
    Capabilities,
    MetadataProvider,
    NodeRef,
)


class SqliteMetadata(MetadataProvider):
    HIERARCHY = ("connection", "object")
    CAPABILITIES = Capabilities(pragmas=True, constraints=True, keys=True)

    #: The folders the connection shows. There is no database or
    #: schema level to hang them off — one file is one database — so
    #: this is the whole tree above the objects (SQ-01). Indexes and
    #: Table Triggers are the same listings a table's own sections
    #: show, read whole instead of one table at a time: one
    #: implementation, two ways in.
    LEVEL_CATEGORIES = {
        "connection": (
            "tables",
            "views",
            "indexes",
            "functions",
            "sequences",
            "triggers",
            "data_types",
        ),
    }
    #: A trigger here always belongs to a table, and a table's own
    #: Triggers section is right under it, so the top-level folder says
    #: which triggers it means.
    CATEGORY_LABEL_OVERRIDES = {"triggers": "Table Triggers"}

    #: SQLite keeps its bookkeeping in the one namespace there is:
    #: `sqlite_sequence`, `sqlite_stat1`, the autoindexes. Dimmed and
    #: sorted last rather than hidden, the treatment a system schema
    #: gets on the engines that have schemas (PG-03).
    _INTERNAL_PREFIX = "sqlite_"

    @classmethod
    def is_system_object(cls, name: str) -> bool:
        return name.lower().startswith(cls._INTERNAL_PREFIX)

    def tree_categories(self, ref: NodeRef) -> list[NodeRef]:
        return [
            self.category(ref, slug)
            for slug in self.LEVEL_CATEGORIES.get(ref.kind, ())
        ]

    def categories(self, ref: NodeRef) -> list[NodeRef]:
        """The connection's folders. Overridden so they come back in
        the declared order whichever vocabulary each slug belongs to,
        and so the ones the generic set gates on `supports_drop` —
        Indexes, Triggers — are simply there: SQLite drops both."""
        if ref.kind == "connection":
            return self.tree_categories(ref)
        return super().categories(ref)

    def category(self, ref: NodeRef, slug: str) -> NodeRef:
        """One folder row, whichever vocabulary its slug comes from."""
        if slug in CATALOG_CATEGORIES:
            return self.catalog_category(ref, slug)
        return ref.child("category", self.category_label(slug), category=slug)
