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

from sqlide.backend.db.base import ConnectorError, ResultSet
from sqlide.backend.db.table_model import (
    SQLITE_COLUMN_OPTIONS,
    SQLITE_TABLE_OPTIONS,
)
from sqlide.backend.db.metadata import (
    CATALOG_CATEGORIES,
    Capabilities,
    MetadataProvider,
    NodeRef,
)
from sqlide.backend.db.sqlite import pragmas


class SqliteMetadata(MetadataProvider):
    HIERARCHY = ("connection", "object")
    CAPABILITIES = Capabilities(pragmas=True, constraints=True, keys=True)
    #: WITHOUT ROWID and STRICT, and the one auto-numbered column
    #: SQLite has (CORE-27).
    TABLE_OPTIONS = SQLITE_TABLE_OPTIONS
    COLUMN_OPTIONS = SQLITE_COLUMN_OPTIONS

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

    # PRAGMAs (SQ-02)
    #
    # The catalog in sqlite/pragmas.py says what each pragma is; this
    # reads the values off the connection and writes the ones the user
    # changes. Two rules the ticket asks for live here rather than in
    # the UI, so a caller that skips the UI keeps them:
    #
    #   * a name is only ever one the catalog declares, and a value
    #     only ever one `normalize()` accepted, so nothing user-typed
    #     reaches the SQL text (a PRAGMA takes no bound parameters);
    #   * a change is followed by a read, so the value shown is the
    #     one the database has and not the one that was asked for —
    #     SQLite quietly ignores `page_size` on a populated file and
    #     refuses `journal_mode` inside a transaction.

    def list_pragmas(self, advanced: bool = False) -> list[pragmas.PragmaState]:
        return [
            pragmas.PragmaState(spec=spec)
            if spec.kind == pragmas.CHECK
            else self._pragma_state(spec)
            for spec in pragmas.listed(advanced)
        ]

    def set_pragma(self, name: str, value) -> pragmas.PragmaState:
        spec = pragmas.spec(name)
        if spec is None:
            raise ConnectorError(f"Unknown pragma: {name}")
        try:
            statement = pragmas.statement(spec, value)
        except pragmas.PragmaError as exc:
            raise ConnectorError(str(exc)) from exc
        self.connector.execute(statement)
        return self._pragma_state(spec)

    def run_pragma_check(self, name: str) -> ResultSet:
        spec = pragmas.spec(name)
        if spec is None or spec.kind not in (pragmas.CHECK, pragmas.READONLY):
            raise ConnectorError(f"{name} is not an informational pragma")
        result = self.connector.execute(f"PRAGMA {spec.name}")
        if isinstance(result, ResultSet):
            return result
        return ResultSet(columns=[spec.name], rows=[])

    def _pragma_state(self, spec: pragmas.PragmaSpec) -> pragmas.PragmaState:
        """One row's current value, or the reason there isn't one — a
        pragma this build was compiled without answers with no rows,
        and a row that says so beats a row that is silently blank."""
        try:
            result = self.connector.execute(f"PRAGMA {spec.name}")
        except ConnectorError as exc:
            return pragmas.PragmaState(spec=spec, error=str(exc))
        rows = result.rows if isinstance(result, ResultSet) else []
        if not rows or rows[0][0] is None:
            return pragmas.PragmaState(
                spec=spec, error="not available in this SQLite build"
            )
        return pragmas.PragmaState(spec=spec, value=str(rows[0][0]))
