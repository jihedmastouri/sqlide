"""Maps connection kinds to adapter classes and metadata providers, and
reports driver availability.

Adapters are imported lazily, so a missing optional driver only fails
when the connection is actually opened. The metadata providers are
imported the same way but stay driver-free (db/metadata.py), so
`capabilities()` and `hierarchy()` answer for an engine whose driver is
not installed and before anything is connected — which is what lets the
UI hide a feature instead of offering it and failing.
"""

from __future__ import annotations

import importlib.util

from sqlide.backend.db.base import Connector
from sqlide.backend.db.metadata import Capabilities, MetadataProvider

KINDS = ("sqlite", "mysql", "postgres", "jdbc")

_DRIVER_MODULES = {
    "sqlite": "sqlite3",
    "mysql": "pymysql",
    "postgres": "psycopg",
    "jdbc": "jaydebeapi",
}


def driver_available(kind: str) -> bool:
    return importlib.util.find_spec(_DRIVER_MODULES[kind]) is not None


def create_connector(kind: str, **params) -> Connector:
    """Instantiate the adapter for `kind`, importing it lazily so missing
    optional drivers only fail when actually used."""
    if kind == "sqlite":
        from sqlide.backend.db.sqlite import SqliteConnector

        return SqliteConnector(**params)
    if kind == "mysql":
        from sqlide.backend.db.mysql import MysqlConnector

        return MysqlConnector(**params)
    if kind == "postgres":
        from sqlide.backend.db.postgres import PostgresConnector

        return PostgresConnector(**params)
    if kind == "jdbc":
        from sqlide.backend.db.jdbc import JdbcConnector

        return JdbcConnector(**params)
    raise ValueError(f"Unknown connection kind: {kind!r}")


def provider_class(kind: str) -> type[MetadataProvider]:
    """The metadata provider for `kind`. JDBC gets the generic one:
    without dialect knowledge it can only offer what every engine has.
    """
    if kind == "sqlite":
        from sqlide.backend.db.sqlite.metadata import SqliteMetadata

        return SqliteMetadata
    if kind == "mysql":
        from sqlide.backend.db.mysql.metadata import MysqlMetadata

        return MysqlMetadata
    if kind == "postgres":
        from sqlide.backend.db.postgres.metadata import PostgresMetadata

        return PostgresMetadata
    if kind == "jdbc":
        return MetadataProvider
    raise ValueError(f"Unknown connection kind: {kind!r}")


def create_provider(kind: str, connector: Connector) -> MetadataProvider:
    """The provider for `kind`, bound to an open connector."""
    return provider_class(kind)(connector)


def capabilities(kind: str) -> Capabilities:
    """What `kind` can do, answerable without a connection."""
    return provider_class(kind).CAPABILITIES


def hierarchy(kind: str) -> tuple[str, ...]:
    """The levels `kind` nests, outermost first."""
    return provider_class(kind).HIERARCHY
