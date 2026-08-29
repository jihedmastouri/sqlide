"""One-off backups: the portable engine, and picking between engines.

The portable path (backend/backups/snapshot.py) exists so that JDBC
and SSH-tunnelled connections — the two a vendor dump tool cannot
reach — can still be backed up. It is exercised here against SQLite,
which is the connector that needs no server, and the round trip is the
real test: snapshot a database, apply the script to an empty one, and
compare what came back, binary values and awkward strings included.
"""

from __future__ import annotations

import gzip
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from sqlide.backend.backups import oneoff
from sqlide.backend.backups.jobs import (
    LOCAL,
    ONE_OFF_ID,
    BackupStore,
    Destination,
)
from sqlide.backend.backups.snapshot import (
    SnapshotError,
    SnapshotSpec,
    apply_script,
    literal,
    write_snapshot,
)
from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.db.sqlite.connector import SqliteConnector


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "shop.db"
    path.touch()
    connector = SqliteConnector(str(path))
    connector.connect()
    connector.execute(
        "CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT, "
        "avatar BLOB, active BOOLEAN)"
    )
    connector.execute(
        "INSERT INTO users(name, avatar, active) VALUES "
        "('ada', X'DEADBEEF', 1), ('o''brien', NULL, 0)"
    )
    connector.execute("CREATE TABLE notes(body TEXT)")
    connector.execute("INSERT INTO notes VALUES ('back\\slash')")
    return connector, ConnectionProfile("shop", "sqlite", file_path=str(path))


@pytest.fixture()
def empty(tmp_path):
    path = tmp_path / "restored.db"
    path.touch()
    connector = SqliteConnector(str(path))
    connector.connect()
    return connector


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr("sqlide.backend.secrets.AVAILABLE", False)
    return BackupStore(tmp_path / "backups.json")


# The portable engine


def test_snapshot_round_trips_through_the_connector(db, empty, tmp_path):
    connector, _profile = db
    artifact = tmp_path / "snap.sql"
    write_snapshot(connector, "sqlite", SnapshotSpec(compression="none"), artifact)

    apply_script(empty, artifact, "sqlite")
    rows = empty.execute(
        "SELECT name, hex(avatar), active FROM users ORDER BY id"
    ).rows
    assert rows[0] == ("ada", "DEADBEEF", 1)
    # A quote in a value must survive as a value, not end the literal.
    assert rows[1][0] == "o'brien"
    assert empty.execute("SELECT body FROM notes").rows[0][0] == "back\\slash"


def test_snapshot_can_be_limited_to_chosen_tables(db, tmp_path):
    connector, _profile = db
    artifact = tmp_path / "snap.sql"
    write_snapshot(
        connector,
        "sqlite",
        SnapshotSpec(tables=["users"], compression="none"),
        artifact,
    )
    text = artifact.read_text()
    assert "users" in text and "notes" not in text


def test_schema_only_and_data_only(db, tmp_path):
    connector, _profile = db
    schema = tmp_path / "schema.sql"
    write_snapshot(
        connector, "sqlite",
        SnapshotSpec(content="schema", compression="none"), schema,
    )
    assert "CREATE TABLE" in schema.read_text()
    assert "INSERT INTO" not in schema.read_text()

    data = tmp_path / "data.sql"
    write_snapshot(
        connector, "sqlite",
        SnapshotSpec(content="data", compression="none"), data,
    )
    assert "INSERT INTO" in data.read_text()
    assert "CREATE TABLE" not in data.read_text()


def test_gzip_is_the_default_and_really_is_gzip(db, tmp_path):
    connector, _profile = db
    artifact = tmp_path / "snap.sql.gz"
    write_snapshot(connector, "sqlite", SnapshotSpec(), artifact)
    assert "CREATE TABLE" in gzip.open(artifact, "rt").read()


def test_a_table_with_no_primary_key_says_so_in_the_script(db, tmp_path):
    # Offset paging is not stable under concurrent writes; the script
    # admits that rather than pretending otherwise.
    connector, _profile = db
    artifact = tmp_path / "snap.sql"
    write_snapshot(connector, "sqlite", SnapshotSpec(compression="none"), artifact)
    assert "no primary key" in artifact.read_text()


def test_paging_covers_a_table_larger_than_one_page(db, empty, tmp_path):
    connector, _profile = db
    connector.execute("CREATE TABLE many(id INTEGER PRIMARY KEY, x TEXT)")
    connector.execute(
        "WITH RECURSIVE s(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM s "
        "WHERE n < 1200) INSERT INTO many(x) SELECT 'row' || n FROM s"
    )
    artifact = tmp_path / "snap.sql"
    write_snapshot(
        connector, "sqlite",
        SnapshotSpec(tables=["many"], compression="none"), artifact,
    )
    apply_script(empty, artifact, "sqlite")
    assert empty.execute("SELECT COUNT(*) FROM many").rows[0][0] == 1200


def test_a_failed_snapshot_leaves_no_partial_file(db, tmp_path):
    connector, _profile = db
    artifact = tmp_path / "snap.sql"
    with pytest.raises(Exception):
        write_snapshot(
            connector, "sqlite",
            SnapshotSpec(tables=["no_such_table"], compression="none"),
            artifact,
        )
    assert not artifact.exists()


def test_an_empty_connection_is_refused_rather_than_writing_nothing(
    empty, tmp_path
):
    with pytest.raises(SnapshotError):
        write_snapshot(empty, "sqlite", SnapshotSpec(), tmp_path / "snap.sql.gz")


def test_applying_a_broken_script_says_which_statement_failed(empty, tmp_path):
    script = tmp_path / "bad.sql"
    script.write_text("CREATE TABLE t(x);\nNOT SQL AT ALL;\n")
    with pytest.raises(SnapshotError) as caught:
        apply_script(empty, script, "sqlite")
    assert "Statement 2 of 2" in str(caught.value)


# Literals


@pytest.mark.parametrize(
    "value,kind,expected",
    [
        (None, "postgres", "NULL"),
        (True, "postgres", "TRUE"),
        (True, "mysql", "1"),  # MySQL has no boolean of its own
        (Decimal("1.50"), "postgres", "1.50"),
        (float("nan"), "postgres", "NULL"),  # no portable literal
        (b"\xde\xad", "postgres", "'\\xdead'::bytea"),
        (b"\xde\xad", "mysql", "X'dead'"),
        ("it's", "postgres", "'it''s'"),
        ("back\\slash", "mysql", "'back\\\\slash'"),  # MySQL escapes \
        ("back\\slash", "postgres", "'back\\slash'"),
        (datetime(2026, 8, 27, 2, 0), "postgres", "'2026-08-27 02:00:00'"),
    ],
)
def test_literals_are_written_for_the_dialect(value, kind, expected):
    assert literal(value, kind) == expected


# Engine choice


def test_a_normal_connection_prefers_the_vendor_tool(monkeypatch):
    monkeypatch.setattr(
        "sqlide.backend.backups.dump.tool_available", lambda _kind: "/usr/bin/pg_dump"
    )
    profile = ConnectionProfile("p", "postgres", database="shop")
    engine, why = oneoff.preferred_engine(profile)
    assert engine == oneoff.VENDOR and "pg_dump" in why


def test_jdbc_and_tunnelled_connections_fall_back_to_the_portable_engine():
    # The two cases a scheduled job refuses outright: here they are the
    # whole point, so they get an engine rather than an error.
    jdbc = ConnectionProfile("j", "jdbc", jdbc_url="jdbc:h2:/tmp/x")
    tunnelled = ConnectionProfile(
        "p", "postgres", database="shop", use_ssh=True, ssh_host="bastion"
    )
    for profile in (jdbc, tunnelled):
        engine, why = oneoff.preferred_engine(profile)
        assert engine == oneoff.PORTABLE
        assert why  # and it says why, for the dialog to show


def test_artifact_name_is_named_for_the_connection():
    profile = ConnectionProfile("Prod: reports", "sqlite")
    name = oneoff.artifact_name(
        profile, SnapshotSpec(), datetime(2026, 8, 27, 2, 0, 0)
    )
    assert name == "prod--reports-20260827-020000.sql.gz"


# The whole action


def test_run_oneoff_uploads_to_a_destination_and_records_history(
    store, db, tmp_path
):
    connector, profile = db
    destination = store.add_destination(
        Destination("Disk", LOCAL, path=str(tmp_path / "vault"))
    )
    run = oneoff.run_oneoff(
        store, profile, SnapshotSpec(compression="none"),
        destination=destination, engine=oneoff.PORTABLE, connector=connector,
    )
    assert run.ok, run.message
    assert len(list((tmp_path / "vault").iterdir())) == 1
    # Recorded in the shared history, under the one-off bucket.
    assert BackupStore(store.path).runs_for(ONE_OFF_ID)[0].ok


def test_run_oneoff_can_write_straight_to_a_file(store, db, tmp_path):
    connector, profile = db
    path = tmp_path / "manual.sql"
    run = oneoff.run_oneoff(
        store, profile, SnapshotSpec(compression="none"),
        file_path=path, engine=oneoff.PORTABLE, connector=connector,
    )
    assert run.ok and path.exists()
    assert "CREATE TABLE" in path.read_text()


def test_a_failure_is_recorded_rather_than_raised(store, db, tmp_path):
    connector, profile = db
    run = oneoff.run_oneoff(
        store, profile, SnapshotSpec(tables=["nope"], compression="none"),
        file_path=tmp_path / "x.sql", engine=oneoff.PORTABLE,
        connector=connector,
    )
    assert not run.ok and profile.name in run.message
    assert store.runs_for(ONE_OFF_ID)[0].ok is False


def test_the_portable_engine_needs_a_connector(store, db, tmp_path):
    _connector, profile = db
    run = oneoff.run_oneoff(
        store, profile, SnapshotSpec(), file_path=Path(tmp_path / "x.sql"),
        engine=oneoff.PORTABLE, connector=None,
    )
    assert not run.ok and "open connection" in run.message
