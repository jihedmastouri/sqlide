"""Maps connection kinds to adapter classes and reports driver availability."""

from __future__ import annotations

import importlib.util

from sqlide.backend.db.base import Connector

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
