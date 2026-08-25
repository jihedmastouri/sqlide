"""The execute(max_rows=…) row cap and cancel(), on SQLite.

SQLite is the one adapter with no server to start, so the shared
contract — a capped fetch reports itself as truncated, and a cancel
from another thread actually unblocks the running statement — is
pinned here. The Postgres and MySQL versions of these tests live
beside their own fixtures.
"""

from __future__ import annotations

import threading
import time

import pytest

from sqlide.backend.db.base import ConnectorError, ResultSet
from sqlide.backend.db.sqlite.connector import SqliteConnector


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "cap.db"
    path.touch()
    connector = SqliteConnector(str(path))
    connector.connect()
    connector.execute("CREATE TABLE t(x INTEGER)")
    connector.execute(
        "WITH RECURSIVE s(x) AS ("
        "  SELECT 1 UNION ALL SELECT x + 1 FROM s WHERE x < 500"
        ") INSERT INTO t SELECT x FROM s"
    )
    yield connector
    connector.close()


def test_cap_stops_the_fetch_and_says_so(db):
    result = db.execute("SELECT * FROM t", max_rows=100)
    assert isinstance(result, ResultSet)
    assert len(result) == 100
    assert result.truncated


def test_a_result_exactly_at_the_cap_is_not_truncated(db):
    # The off-by-one that matters: 100 rows fetched under a cap of 100
    # is the whole answer, not a clipped one.
    result = db.execute("SELECT * FROM t LIMIT 100", max_rows=100)
    assert len(result) == 100
    assert not result.truncated


def test_no_cap_fetches_everything(db):
    result = db.execute("SELECT * FROM t")
    assert len(result) == 500
    assert not result.truncated


def test_cap_leaves_non_row_statements_alone(db):
    assert db.execute("UPDATE t SET x = x WHERE x <= 5", max_rows=10) == 5


def test_cancel_unblocks_a_running_statement(db):
    error: list[Exception] = []
    started = threading.Event()

    def run() -> None:
        started.set()
        try:
            db.execute("SELECT count(*) FROM t a, t b, t c, t d")
        except Exception as exc:  # the cancelled statement's own failure
            error.append(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    started.wait(5)
    time.sleep(0.2)  # let it get past the lock and into the scan
    db.cancel()
    worker.join(10)

    assert not worker.is_alive(), "cancel did not unblock the statement"
    assert isinstance(error[0], ConnectorError)
    # The connection survives its cancelled statement.
    assert len(db.execute("SELECT * FROM t", max_rows=5)) == 5


def test_cancel_with_nothing_running_is_harmless(db):
    db.cancel()
    assert len(db.execute("SELECT * FROM t", max_rows=5)) == 5
