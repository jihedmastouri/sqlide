"""Account management on the adapters that have no accounts.

The live MySQL and PostgreSQL behaviour lives in their own integration
tests; this covers the other half of the contract — an adapter that
does not manage accounts says so and refuses to build the statements,
rather than emitting SQL its server would reject — and the accounts
overview's column sets, which are capability answers and need no
server at all.
"""

from __future__ import annotations

import pytest

from sqlide.backend.db.base import ConnectorError, UserInfo
from sqlide.backend.db.metadata import PRINCIPAL_COLUMN_NAMES
from sqlide.backend.db.mysql.metadata import MysqlMetadata
from sqlide.backend.db.postgres.metadata import PostgresMetadata
from sqlide.backend.db.sqlite import SqliteConnector
from sqlide.backend.db.sqlite.metadata import SqliteMetadata


@pytest.fixture
def sqlite(tmp_path):
    path = tmp_path / "test.db"
    path.touch()  # the adapter refuses to create a missing file
    connector = SqliteConnector(file_path=str(path))
    connector.connect()
    yield connector
    connector.close()


def test_sqlite_has_no_accounts(sqlite):
    assert not sqlite.supports_users
    assert sqlite.list_users() == []
    assert sqlite.list_privileges(UserInfo(name="anyone")) == []
    assert sqlite.grant_scopes() == []


def test_account_statements_refuse_to_build(sqlite):
    with pytest.raises(ConnectorError):
        sqlite.create_user_sql("app")
    with pytest.raises(ConnectorError):
        sqlite.drop_user_sql(UserInfo(name="app"))
    with pytest.raises(ConnectorError):
        sqlite.set_password_sql(UserInfo(name="app"), "secret")


def test_grant_needs_a_privilege_the_adapter_advertises(sqlite):
    # privilege_names() is empty here, so every privilege is unknown —
    # the check the dialects share, seen from the empty end.
    with pytest.raises(ConnectorError):
        sqlite.grant_sql(UserInfo(name="app"), ["SELECT"], "*.*")
    with pytest.raises(ConnectorError):
        sqlite.grant_sql(UserInfo(name="app"), [], "*.*")


# The accounts overview (CORE-12): which columns an engine has, and
# what a row of it says. Neither needs a server — the columns are a
# capability answer, and the cells are rendered from UserInfo alone.


class _Accounts:
    """A connector that only knows its account list — all the overview
    reads."""

    def __init__(self, users: list[UserInfo]) -> None:
        self._users = users

    def list_users(self) -> list[UserInfo]:
        return self._users


def test_columns_are_the_engines_own():
    from sqlide.backend.db import registry

    postgres = registry.principal_columns("postgres")
    mysql = registry.principal_columns("mysql")
    assert "Host" in mysql and "Host" not in postgres
    assert "Create DB" in postgres and "Create DB" not in mysql
    assert "Plugin" in mysql and "Plugin" not in postgres
    for columns in (postgres, mysql):
        assert columns[0] == "Name"
        assert set(columns) <= set(PRINCIPAL_COLUMN_NAMES)
    # SQLite has no accounts, so it has no table to draw either.
    assert registry.principal_columns("sqlite") == ()


def test_sqlite_overview_is_empty():
    provider = SqliteMetadata(_Accounts([UserInfo(name="ignored")]))
    assert provider.principal_table() == ((), [])


def test_postgres_row_renders_role_attributes():
    provider = PostgresMetadata(
        _Accounts(
            [
                UserInfo(
                    name="analysts",
                    can_login=False,
                    kind="group",
                    create_db=True,
                    member_of=("readers", "writers"),
                    valid_until="2030-01-01 00:00:00",
                    connection_limit="-1",
                )
            ]
        )
    )
    columns, rows = provider.principal_table()
    (user, cells) = rows[0]
    row = dict(zip(columns, cells))
    assert user.name == "analysts"  # the account rides along with its row
    assert row["Name"] == "analysts"
    assert row["Type"] == "group"
    assert row["Login"] == ""  # a false flag leaves the cell blank
    assert row["Create DB"] == "yes"
    assert row["Member of"] == "readers, writers"
    assert row["Valid until"] == "2030-01-01 00:00:00"
    assert row["Connection limit"] == "unlimited"


def test_mysql_row_renders_account_attributes():
    provider = MysqlMetadata(
        _Accounts(
            [
                UserInfo(
                    name="app",
                    host="10.0.%",
                    kind="user",
                    locked=True,
                    can_login=False,
                    plugin="caching_sha2_password",
                    password_expiry="expired",
                    member_of=("reporting@%",),
                )
            ]
        )
    )
    columns, rows = provider.principal_table()
    row = dict(zip(columns, rows[0][1]))
    assert row["Name"] == "app" and row["Host"] == "10.0.%"
    assert row["Locked"] == "yes" and row["Login"] == ""
    assert row["Plugin"] == "caching_sha2_password"
    assert row["Password expiry"] == "expired"
    assert row["Member of"] == "reporting@%"


def test_overview_survives_a_catalog_it_cannot_read():
    class Refuses:
        def list_users(self):
            raise ConnectorError("access denied to mysql.user")

    columns, rows = MysqlMetadata(Refuses()).principal_table()
    assert columns and rows == []
