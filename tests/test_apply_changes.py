"""Grid edits applied as one transaction (CORE-39).

A Save is a batch: Connector.apply_changes() runs every operation
inside one explicit transaction, so a failure half way through leaves
the table exactly as it was rather than half-written. The rules that
matter to the grid are asserted here on SQLite (which needs no server);
tests/test_postgres.py runs the same ones against a real server.
"""

from __future__ import annotations

import sqlite3

import pytest

from sqlide.backend.db.base import BatchError, ConnectorError, RowOperation
from sqlide.backend.db.sqlite.connector import SqliteConnector


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "core39.db"
    sqlite3.connect(path).close()
    connector = SqliteConnector(str(path))
    connector.connect()
    connector.execute(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, email TEXT)"
    )
    connector.execute(
        "INSERT INTO users VALUES (1, 'ada', 'ada@example.com'),"
        " (2, 'brian', NULL), (3, 'carol', 'carol@example.com')"
    )
    yield connector
    connector.close()


def emails(db) -> list:
    return [row[0] for row in db.execute("SELECT email FROM users ORDER BY id").rows]


def update(pk: int, column: str, value) -> RowOperation:
    return RowOperation(pk_values={"id": pk}, column=column, value=value)


def test_batch_applies_every_operation(db):
    db.apply_changes(
        "users",
        [update(1, "email", "a@x"), update(2, "email", "b@x"), update(3, "name", "CAROL")],
    )
    assert emails(db) == ["a@x", "b@x", "carol@example.com"]
    assert db.execute("SELECT name FROM users WHERE id = 3").rows[0][0] == "CAROL"
    assert not db.in_transaction()


def test_failure_leaves_the_table_untouched(db):
    before = emails(db)
    with pytest.raises(BatchError) as caught:
        db.apply_changes(
            "users",
            [
                update(1, "email", "a@x"),
                update(2, "email", "b@x"),
                update(999, "email", "nobody@x"),  # no such row
                update(3, "email", "c@x"),
            ],
        )
    assert caught.value.index == 2
    assert emails(db) == before
    assert not db.in_transaction()


def test_error_names_the_row_and_column(db):
    with pytest.raises(BatchError, match=r"row \(id=999\) column email"):
        db.apply_changes("users", [update(999, "email", "nobody@x")])


def test_unknown_column_is_rejected_before_anything_runs(db):
    before = emails(db)
    with pytest.raises(ConnectorError, match="Unknown column"):
        db.apply_changes("users", [update(1, "email", "a@x"), update(2, "nope", "x")])
    assert emails(db) == before


def test_columns_are_validated_once_per_batch(db, monkeypatch):
    calls = []
    original = db.list_columns
    monkeypatch.setattr(
        db, "list_columns", lambda table: calls.append(table) or original(table)
    )
    db.apply_changes(
        "users", [update(1, "email", "a@x"), update(2, "email", "b@x"), update(3, "email", "c@x")]
    )
    assert calls == ["users"]


def test_batch_joins_an_open_user_transaction(db):
    db.execute("BEGIN")
    db.apply_changes("users", [update(1, "email", "a@x")])
    # Neither committed nor nested: the user's transaction is still the
    # one that decides, and rolling it back takes the batch with it.
    assert db.in_transaction()
    db.rollback()
    assert emails(db)[0] == "ada@example.com"


def test_failure_inside_a_user_transaction_keeps_it_open(db):
    db.execute("BEGIN")
    db.execute("UPDATE users SET email = 'typed@x' WHERE id = 2")
    with pytest.raises(BatchError):
        db.apply_changes("users", [update(1, "email", "a@x"), update(999, "email", "x")])
    # The savepoint undid the batch, not the statement the user typed.
    assert db.in_transaction()
    assert emails(db) == ["ada@example.com", "typed@x", "carol@example.com"]
    db.rollback()


def test_empty_batch_is_a_no_op(db):
    db.apply_changes("users", [])
    assert not db.in_transaction()
