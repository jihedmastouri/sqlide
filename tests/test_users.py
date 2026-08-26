"""Account management on the adapters that have no accounts.

The live MySQL and PostgreSQL behaviour lives in their own integration
tests; this covers the other half of the contract — an adapter that
does not manage accounts says so and refuses to build the statements,
rather than emitting SQL its server would reject.
"""

from __future__ import annotations

import pytest

from sqlide.backend.db.base import ConnectorError, UserInfo
from sqlide.backend.db.sqlite import SqliteConnector


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
