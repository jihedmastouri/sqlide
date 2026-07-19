"""Fixtures for the adapter integration tests.

The `postgres` and `mysql` fixtures are parametrized over the server
versions declared in docker-compose.yml, so every test using one runs
once per version. A server that isn't running (or whose driver isn't
installed) skips its tests rather than failing, so starting a subset —
`docker compose up -d postgres16 mysql8` — tests just those.

Each fixture connects, reseeds the fixed demo schema (users, orders,
a view and a stored function) and yields (version, connector).
"""

from __future__ import annotations

import pytest

from sqlide.backend.db.base import Connector

CREDENTIALS = {"user": "sqlide", "password": "sqlide", "database": "sqlide"}

# version -> (compose service, host port); must match docker-compose.yml.
POSTGRES_SERVERS = {
    "10": ("postgres10", 54310),
    "11": ("postgres11", 54311),
    "12": ("postgres12", 54312),
    "13": ("postgres13", 54313),
    "14": ("postgres14", 54314),
    "15": ("postgres15", 54315),
    "16": ("postgres16", 54316),
}
MYSQL_SERVERS = {
    "5.7": ("mysql5", 33057),
    "8.0": ("mysql8", 33080),
}

_POSTGRES_SEED = (
    "DROP VIEW IF EXISTS big_orders",
    "DROP TABLE IF EXISTS orders",
    "DROP TABLE IF EXISTS users",
    "CREATE TABLE users ("
    " id integer PRIMARY KEY,"
    " name varchar(40) NOT NULL,"
    " email varchar(80))",
    "CREATE TABLE orders ("
    " id integer PRIMARY KEY,"
    " user_id integer NOT NULL REFERENCES users(id),"
    " amount integer)",
    "CREATE VIEW big_orders AS SELECT * FROM orders WHERE amount > 100",
    "INSERT INTO users VALUES"
    " (1, 'ada', 'ada@example.com'),"
    " (2, 'brian', NULL),"
    " (3, 'carol', 'carol@example.com')",
    "INSERT INTO orders VALUES (1, 1, 50), (2, 1, 150), (3, 2, 200)",
    "CREATE OR REPLACE FUNCTION add_amounts(a integer, b integer)"
    " RETURNS integer AS $$ BEGIN RETURN a + b; END $$ LANGUAGE plpgsql",
)

_MYSQL_SEED = (
    "DROP VIEW IF EXISTS big_orders",
    "DROP TABLE IF EXISTS orders",
    "DROP TABLE IF EXISTS users",
    "CREATE TABLE users ("
    " id integer PRIMARY KEY,"
    " name varchar(40) NOT NULL,"
    " email varchar(80))",
    "CREATE TABLE orders ("
    " id integer PRIMARY KEY,"
    " user_id integer NOT NULL,"
    " amount integer,"
    " FOREIGN KEY (user_id) REFERENCES users(id))",
    "CREATE VIEW big_orders AS SELECT * FROM orders WHERE amount > 100",
    "INSERT INTO users VALUES"
    " (1, 'ada', 'ada@example.com'),"
    " (2, 'brian', NULL),"
    " (3, 'carol', 'carol@example.com')",
    "INSERT INTO orders VALUES (1, 1, 50), (2, 1, 150), (3, 2, 200)",
    "DROP FUNCTION IF EXISTS add_amounts",
    # DETERMINISTIC keeps binlog-enabled servers (MySQL 8 default)
    # from rejecting the CREATE for the unprivileged sqlide user.
    "CREATE FUNCTION add_amounts(a integer, b integer)"
    " RETURNS integer DETERMINISTIC RETURN a + b",
)


def _connect_or_skip(connector: Connector, service: str) -> None:
    try:
        connector.connect()
    except Exception as exc:
        pytest.skip(
            f"{service} not reachable ({exc}); "
            f"start it with: docker compose up -d {service}"
        )


def _seed(connector: Connector, statements: tuple[str, ...]) -> None:
    for sql in statements:
        connector.execute(sql)


@pytest.fixture(
    scope="session",
    params=sorted(POSTGRES_SERVERS, key=int),
    ids=lambda version: f"pg{version}",
)
def postgres(request):
    pytest.importorskip("psycopg", reason="postgres driver not installed")
    from sqlide.backend.db.postgres.connector import PostgresConnector

    version = request.param
    service, port = POSTGRES_SERVERS[version]
    connector = PostgresConnector(host="127.0.0.1", port=port, **CREDENTIALS)
    _connect_or_skip(connector, service)
    _seed(connector, _POSTGRES_SEED)
    yield version, connector
    connector.close()


@pytest.fixture(
    scope="session",
    params=sorted(MYSQL_SERVERS),
    ids=lambda version: f"mysql{version}",
)
def mysql(request):
    pytest.importorskip("pymysql", reason="mysql driver not installed")
    from sqlide.backend.db.mysql.connector import MysqlConnector

    version = request.param
    service, port = MYSQL_SERVERS[version]
    connector = MysqlConnector(host="127.0.0.1", port=port, **CREDENTIALS)
    _connect_or_skip(connector, service)
    _seed(connector, _MYSQL_SEED)
    yield version, connector
    connector.close()
