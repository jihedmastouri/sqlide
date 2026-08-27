"""Extensions: what the server has installed, and what that means.

A popular extension changes what a database *is* — PostGIS makes a hex
column a map, TimescaleDB makes a table a hypertable, pgvector adds a
type the grid should print rather than dump. PG-05 makes that a
declaration instead of a special case: `REGISTRY` maps an extension
name to the behaviour it unlocks, and everything above reads the
*feature*, never the extension's name.

Two pieces live here:

* `ExtensionState` — one row of the Extensions folder: the name, the
  installed version (empty when it is merely available), the schema it
  was installed into, the newest version the server has on disk, and
  the comment the server ships with it. `update_available` is the
  comparison, so no UI has to make it.
* `REGISTRY` — the known extensions and the features they bring. An
  extension that is not in it is not a problem: `trait()` answers a
  generic trait, the folder lists it like any other, and no feature
  turns on. That is the "unknown extensions get the generic listing"
  acceptance criterion, and it is the default rather than a fallback
  path anyone has to remember to write.

SQL generation is here too (`install_sql`, `update_sql`, `drop_sql`).
It is plain CREATE/ALTER/DROP EXTENSION — the SQL standard has nothing
to say about extensions, so PostgreSQL's spelling is the only one, and
an engine without the `extensions` capability never reaches this code.
Nothing here executes anything: the statements are returned for the
confirmation dialog to show and the caller to run, the same
review-then-run contract the rest of the DDL surface keeps.

No driver imports, no GTK: this module is read by the metadata layer
(which must answer before psycopg exists) and by the frontend alike.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

#: The features an extension can unlock. Named for what the UI does
#: with them, not for the extension that happens to provide them —
#: another spatial extension would light the same lamp.
FEATURE_LABELS = {
    "spatial": "Geometry columns can be shown on a map",
    "statements": "Query statistics for the monitor",
    "hypertables": "Hypertables and chunks in the object tree",
    "vectors": "Vector columns shown as vectors",
    "jobs": "Scheduled jobs",
    "types": "Type-aware display for the types it adds",
}


@dataclass(frozen=True)
class ExtensionTrait:
    """What is known about one extension beyond its name.

    `features` is what having it installed turns on; `types` are the
    type names it introduces, so a column of one can be printed as
    what it is instead of as an opaque blob. Both empty for an
    extension nobody has taught us about, which is what makes the
    unknown case work without a branch anywhere.
    """

    name: str
    label: str = ""
    summary: str = ""
    features: tuple[str, ...] = ()
    types: tuple[str, ...] = ()

    @property
    def known(self) -> bool:
        return bool(self.label)

    @property
    def title(self) -> str:
        return self.label or self.name

    def has(self, feature: str) -> bool:
        return feature in self.features


#: The extensions sqlide knows something about. Everything else gets
#: the generic listing (see `trait`).
REGISTRY: dict[str, ExtensionTrait] = {
    trait.name: trait
    for trait in (
        ExtensionTrait(
            "postgis",
            label="PostGIS",
            summary=(
                "Geometry and geography types. Geometry columns get the "
                "map view (PG-04)."
            ),
            features=("spatial", "types"),
            types=("geometry", "geography", "box2d", "box3d"),
        ),
        ExtensionTrait(
            "pg_stat_statements",
            label="Query statistics",
            summary=(
                "Per-statement execution counts and timings, which the "
                "monitor's statement panel reads (CORE-15)."
            ),
            features=("statements",),
        ),
        ExtensionTrait(
            "timescaledb",
            label="TimescaleDB",
            summary=(
                "Hypertables: one logical table stored as time-ranged "
                "chunks, browsable as the pieces they are."
            ),
            features=("hypertables",),
        ),
        ExtensionTrait(
            "vector",
            label="pgvector",
            summary=(
                "The vector type for embeddings, printed as a vector "
                "with its dimension rather than as a long literal."
            ),
            features=("vectors", "types"),
            types=("vector", "halfvec", "sparsevec"),
        ),
        ExtensionTrait(
            "pg_cron",
            label="pg_cron",
            summary="Scheduled jobs, run by the server on a cron schedule.",
            features=("jobs",),
        ),
        ExtensionTrait(
            "uuid-ossp",
            label="UUID generation",
            summary="Functions that generate UUIDs.",
            features=("types",),
            types=("uuid",),
        ),
        ExtensionTrait(
            "hstore",
            label="hstore",
            summary="A key/value type, shown as its pairs.",
            features=("types",),
            types=("hstore",),
        ),
        ExtensionTrait(
            "citext",
            label="citext",
            summary="Case-insensitive text, shown as text.",
            features=("types",),
            types=("citext",),
        ),
    )
}


def trait(name: str) -> ExtensionTrait:
    """What is known about `name` — a generic trait for an extension
    nobody has registered, never None. Callers can therefore ask about
    any extension the server reports without checking first."""
    return REGISTRY.get(name, ExtensionTrait(name))


@dataclass(frozen=True)
class ExtensionState:
    """One extension as the server reports it.

    Installed and merely-available extensions are the same shape: the
    installed ones carry a `version`, the available ones do not, and
    `default_version` is what the server would install (or upgrade to)
    for both.
    """

    name: str
    version: str = ""  # installed version, "" when not installed
    schema: str = ""
    default_version: str = ""
    comment: str = ""

    @property
    def installed(self) -> bool:
        return bool(self.version)

    @property
    def update_available(self) -> bool:
        """Is a newer version on disk than the one installed?

        A plain inequality, deliberately: extension versions are free
        text ("3.4.2", "1.0-1", "unpackaged"), so ordering them is
        guesswork, while "the default differs from what is installed"
        is exactly what ALTER EXTENSION … UPDATE would act on.
        """
        return bool(
            self.installed
            and self.default_version
            and self.default_version != self.version
        )

    @property
    def trait(self) -> ExtensionTrait:
        return trait(self.name)

    def detail(self) -> str:
        """The one-line note the tree and the info view show."""
        parts = []
        if self.installed:
            parts.append(self.version)
            if self.schema:
                parts.append(f"in {self.schema}")
            if self.update_available:
                parts.append(f"· update to {self.default_version}")
        elif self.default_version:
            parts.append(f"{self.default_version} available")
        else:
            parts.append("available")
        return " ".join(parts)


def features(states) -> set[str]:
    """Every feature the installed extensions in `states` unlock."""
    found: set[str] = set()
    for state in states:
        if state.installed:
            found.update(state.trait.features)
    return found


def type_owner(type_name: str, states) -> str:
    """Which installed extension owns `type_name`, "" for none — how a
    column of an extension type is attributed without the grid knowing
    any extension by name."""
    lowered = (type_name or "").strip().lower()
    for state in states:
        if state.installed and lowered in state.trait.types:
            return state.name
    return ""


Quote = Callable[[str], str]


def _quoted(name: str, quote: Quote | None) -> str:
    return quote(name) if quote is not None else f'"{name}"'


def install_sql(
    name: str, *, schema: str = "", quote: Quote | None = None
) -> str:
    """CREATE EXTENSION. `IF NOT EXISTS` is left out on purpose: the
    user is being shown this statement and asked to confirm it, so a
    name that is already installed should say so rather than silently
    do nothing."""
    sql = f"CREATE EXTENSION {_quoted(name, quote)}"
    if schema:
        sql += f" SCHEMA {_quoted(schema, quote)}"
    return sql


def update_sql(
    name: str, *, version: str = "", quote: Quote | None = None
) -> str:
    """ALTER EXTENSION … UPDATE, to the newest version on disk unless
    one is named. The version is a literal, not an identifier: it is a
    value the catalog reported ("3.4.2", "1.0-1"), not a name."""
    sql = f"ALTER EXTENSION {_quoted(name, quote)} UPDATE"
    if version:
        escaped = version.replace("'", "''")
        sql += f" TO '{escaped}'"
    return sql


def drop_sql(
    name: str, *, cascade: bool = False, quote: Quote | None = None
) -> str:
    """DROP EXTENSION, taking its objects with it under CASCADE."""
    sql = f"DROP EXTENSION {_quoted(name, quote)}"
    return sql + " CASCADE" if cascade else sql
