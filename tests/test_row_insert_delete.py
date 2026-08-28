"""Adding and removing whole rows from the grid (CORE-38).

`insert_row` and `delete_row` are the standalone counterparts of
`update_cell`, and the same three kinds ride through `apply_changes`
as one batch so a Save keeps its all-or-nothing promise. Asserted on
SQLite, which needs no server; the statements are built in
backend/db/base.py and shared by every adapter.
"""

from __future__ import annotations

import sqlite3

import pytest

from sqlide.backend.db.base import BatchError, ConnectorError, RowOperation
from sqlide.backend.db.sqlite.connector import SqliteConnector


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "core38.db"
    sqlite3.connect(path).close()
    connector = SqliteConnector(str(path))
    connector.connect()
    connector.execute(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, "
        "email TEXT, role TEXT DEFAULT 'reader')"
    )
    connector.execute(
        "INSERT INTO users (id, name, email) VALUES (1, 'ada', 'ada@x'),"
        " (2, 'brian', NULL), (3, 'carol', 'carol@x')"
    )
    yield connector
    connector.close()


def ids(db) -> list:
    return [row[0] for row in db.execute("SELECT id FROM users ORDER BY id").rows]


def test_insert_row_writes_the_values(db):
    db.insert_row("users", {"id": 4, "name": "dana", "email": "dana@x"})
    assert ids(db) == [1, 2, 3, 4]
    row = db.execute("SELECT name, role FROM users WHERE id = 4").rows[0]
    # A column left out takes the table's default rather than NULL.
    assert row == ("dana", "reader")


def test_insert_row_refuses_an_empty_row(db):
    with pytest.raises(ConnectorError):
        db.insert_row("users", {})


def test_insert_row_rejects_an_unknown_column(db):
    with pytest.raises(ConnectorError):
        db.insert_row("users", {"id": 9, "nope": "x"})
    assert ids(db) == [1, 2, 3]


def test_insert_row_binds_rather_than_interpolates(db):
    db.insert_row("users", {"id": 5, "name": "'); DROP TABLE users; --"})
    assert ids(db) == [1, 2, 3, 5]
    assert db.execute("SELECT name FROM users WHERE id = 5").rows[0][0] == (
        "'); DROP TABLE users; --"
    )


def test_delete_row_removes_exactly_that_row(db):
    db.delete_row("users", {"id": 2})
    assert ids(db) == [1, 3]


def test_delete_row_refuses_an_empty_key(db):
    with pytest.raises(ConnectorError):
        db.delete_row("users", {})
    assert ids(db) == [1, 2, 3]


def test_delete_row_asserts_one_affected_row(db):
    with pytest.raises(ConnectorError):
        db.delete_row("users", {"id": 999})
    assert ids(db) == [1, 2, 3]


def test_delete_row_rejects_an_unknown_column(db):
    with pytest.raises(ConnectorError):
        db.delete_row("users", {"nope": 1})


# The batch path: inserts, updates and deletes in one transaction.


def test_batch_runs_the_three_kinds_in_order(db):
    db.apply_changes(
        "users",
        [
            RowOperation(kind="insert", values={"id": 4, "name": "dana"}),
            RowOperation(pk_values={"id": 1}, column="name", value="ADA"),
            RowOperation(kind="delete", pk_values={"id": 2}),
        ],
    )
    assert ids(db) == [1, 3, 4]
    assert db.execute("SELECT name FROM users WHERE id = 1").rows[0][0] == "ADA"
    assert not db.in_transaction()


def test_a_failed_delete_rolls_the_whole_batch_back(db):
    before = ids(db)
    with pytest.raises(BatchError) as caught:
        db.apply_changes(
            "users",
            [
                RowOperation(kind="insert", values={"id": 4, "name": "dana"}),
                RowOperation(kind="delete", pk_values={"id": 999}),
            ],
        )
    assert caught.value.index == 1
    assert ids(db) == before
    assert not db.in_transaction()


def test_a_failed_insert_rolls_the_whole_batch_back(db):
    with pytest.raises(BatchError) as caught:
        db.apply_changes(
            "users",
            [
                RowOperation(kind="delete", pk_values={"id": 3}),
                # Duplicate primary key.
                RowOperation(kind="insert", values={"id": 1, "name": "x"}),
            ],
        )
    assert caught.value.index == 1
    assert ids(db) == [1, 2, 3]


def test_batch_rejects_an_unknown_insert_column(db):
    with pytest.raises(ConnectorError):
        db.apply_changes(
            "users", [RowOperation(kind="insert", values={"nope": 1})]
        )
    assert ids(db) == [1, 2, 3]


def test_batch_refuses_a_delete_with_no_key(db):
    with pytest.raises(BatchError):
        db.apply_changes("users", [RowOperation(kind="delete")])
    assert ids(db) == [1, 2, 3]


def test_batch_joins_an_open_transaction(db):
    db.execute("BEGIN")
    db.apply_changes(
        "users", [RowOperation(kind="delete", pk_values={"id": 3})]
    )
    assert db.in_transaction()  # nothing committed on the user's behalf
    db.execute("ROLLBACK")
    assert ids(db) == [1, 2, 3]


# The grid's pending list: what a Save turns the user's actions into.
#
# A whole TableTab loads itself on a worker thread the moment it is
# built, so these bind the editing methods onto a stand-in holding just
# the state they read.


@pytest.fixture()
def gtk():
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gtk

    if not Gtk.init_check():
        pytest.skip("no display for GTK")
    return Gtk


class _FakeGrid:
    """Only what the editing methods call back into."""

    def __init__(self, columns) -> None:
        self._columns = columns
        self.deleted: list = []

    def append_blank_row(self):
        from sqlide.frontend.data_grid import RowItem

        return RowItem(tuple([None] * len(self._columns)))

    def mark_modified(self, row, index) -> None:
        pass

    def mark_deleted(self, row, deleted: bool = True) -> None:
        self.deleted.append(row)


def _tab(gtk):
    from sqlide.backend.db.base import ColumnInfo
    from sqlide.frontend.data_grid import TableTab

    class Stub:
        table = "users"
        read_only = False
        _commit_edit = TableTab._commit_edit
        _on_insert_row = TableTab._on_insert_row
        _on_delete_row = TableTab._on_delete_row
        _pending_updates = TableTab._pending_updates
        _pending_count = TableTab._pending_count
        _preview_statement = TableTab._preview_statement

        def __init__(self) -> None:
            self._columns = [
                ColumnInfo("id", "INTEGER", is_pk=True),
                ColumnInfo("name", "TEXT"),
            ]
            self._result_names = ["id", "name"]
            self._pending = {}
            self._grid = _FakeGrid(self._result_names)
            self.errors: list[str] = []
            self._show_error = self.errors.append

        def _update_save_button(self) -> None:
            pass

    return Stub()


def test_a_row_added_then_edited_is_one_insert(gtk) -> None:
    tab = _tab(gtk)
    tab._on_insert_row()
    row = next(iter(tab._pending))
    tab._commit_edit(row, 0, "4")
    tab._commit_edit(row, 1, "dana")
    tab._commit_edit(row, 1, "danielle")  # edited twice, still one INSERT
    operations = tab._pending_updates()
    assert [op.kind for op in operations] == ["insert"]
    assert operations[0].values == {"id": "4", "name": "danielle"}
    assert tab._pending_count() == 1


def test_deleting_an_added_row_writes_nothing(gtk) -> None:
    tab = _tab(gtk)
    tab._on_insert_row()
    row = next(iter(tab._pending))
    tab._on_delete_row(row)
    assert tab._pending_updates() == []


def test_editing_then_deleting_a_row_is_one_delete(gtk) -> None:
    from sqlide.frontend.data_grid import RowItem

    tab = _tab(gtk)
    row = RowItem((1, "ada"))
    tab._commit_edit(row, 1, "ADA")
    tab._on_delete_row(row)
    operations = tab._pending_updates()
    assert [op.kind for op in operations] == ["delete"]
    assert operations[0].pk_values == {"id": 1}
    # And a later edit of a row on its way out changes nothing.
    tab._commit_edit(row, 1, "again")
    assert [op.kind for op in tab._pending_updates()] == ["delete"]


def test_the_preview_lists_them_in_execution_order(gtk) -> None:
    from sqlide.frontend.data_grid import RowItem

    tab = _tab(gtk)
    tab._on_insert_row()
    added = next(iter(tab._pending))
    tab._commit_edit(added, 1, "dana")
    existing = RowItem((1, "ada"))
    tab._commit_edit(existing, 1, "ADA")
    doomed = RowItem((2, "brian"))
    tab._on_delete_row(doomed)
    statements = [tab._preview_statement(op) for op in tab._pending_updates()]
    assert statements == [
        "INSERT INTO users (name) VALUES ('dana');",
        "UPDATE users SET name = 'ADA' WHERE id = 1;",
        "DELETE FROM users WHERE id = 2;",
    ]
