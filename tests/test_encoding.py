"""Encoding, binary values and session time zones.

None of these need a server: the binary cases go through the grid's
formatters, the text-factory case through a hand-built SQLite file, and
the time-zone cases through the settings helpers.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from sqlide.backend import settings as settings_mod
from sqlide.backend.db.sqlite import SqliteConnector
from sqlide.frontend import data_grid


# Binary values


def test_short_blob_shows_as_hex():
    assert data_grid._display_text(b"\x89PNG") == "0x89504E47"


def test_long_blob_is_summarised_by_size():
    text = data_grid._display_text(bytes(64))
    assert text.startswith("0x0000")
    assert text.endswith("(64 bytes)")


def test_copy_keeps_the_whole_blob():
    """The grid's own label truncates; a copy must not."""
    assert data_grid._cell_text(bytes(64)) == "0x" + "00" * 64


def test_memoryview_renders_like_bytes():
    assert data_grid._display_text(memoryview(b"\x01\x02")) == "0x0102"


def test_blob_sql_literal_is_hex_not_a_python_repr():
    assert data_grid._sql_literal(b"\x01\xff") == "X'01FF'"


def test_csv_export_holds_full_hex():
    csv = data_grid._format_csv(["data"], [[b"\x00" * 40]])
    assert csv.splitlines()[1] == "0x" + "00" * 40


def test_json_export_holds_full_hex():
    rows = json.loads(data_grid._format_json(["data"], [[b"\xde\xad"]]))
    assert rows == [{"data": "0xDEAD"}]


def test_json_export_keeps_non_ascii_text_as_text():
    rows = json.loads(data_grid._format_json(["name"], [["Ünïcødé 🎉"]]))
    assert rows == [{"name": "Ünïcødé 🎉"}]


# SQLite text


def test_invalid_utf8_text_does_not_lose_the_result_set(tmp_path):
    """A latin1 byte in a TEXT column used to raise out of the fetch and
    take the whole result with it."""
    path = tmp_path / "legacy.db"
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
    # CAST a blob to TEXT: stores bytes that are not valid UTF-8.
    raw.execute("INSERT INTO t VALUES (1, CAST(x'4a6f73e9' AS TEXT))")
    raw.execute("INSERT INTO t VALUES (2, 'ok')")
    raw.commit()
    raw.close()

    db = SqliteConnector(str(path))
    db.connect()
    try:
        rows = db.execute("SELECT id, name FROM t ORDER BY id").rows
    finally:
        db.close()
    assert [r[0] for r in rows] == [1, 2]
    assert rows[0][1].startswith("Jos")  # the bad byte replaced, not raised
    assert rows[1][1] == "ok"


def test_unicode_round_trips_through_sqlite(tmp_path):
    path = tmp_path / "u.db"
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE t (name TEXT)")
    raw.execute("INSERT INTO t VALUES (?)", ("Ünïcødé 🎉",))
    raw.commit()
    raw.close()

    db = SqliteConnector(str(path))
    db.connect()
    try:
        assert db.execute("SELECT name FROM t").rows[0][0] == "Ünïcødé 🎉"
    finally:
        db.close()


# Session time zone


@pytest.fixture
def zone(tmp_path, monkeypatch):
    """The settings store on a throwaway file, so the mode can be set
    without touching the user's real settings."""
    store = settings_mod.SettingsStore(tmp_path / "settings.json")
    monkeypatch.setattr(settings_mod, "store", store)
    return store


def test_utc_mode_pins_utc(zone):
    zone.update(time_zone="utc")
    assert settings_mod.session_time_zone() == "UTC"


def test_server_mode_asks_for_nothing(zone):
    zone.update(time_zone="server")
    assert settings_mod.session_time_zone() is None


def test_local_mode_names_a_zone(zone, monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Paris")
    assert settings_mod.session_time_zone() == "Europe/Paris"


def test_default_is_local(zone):
    assert zone.settings.time_zone == "local"


def test_unknown_mode_on_file_falls_back(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"time_zone": "Mars/Olympus"}))
    store = settings_mod.SettingsStore(path)
    store.load()
    assert store.settings.time_zone == "local"


def test_mysql_offset_fallback():
    pytest.importorskip("pymysql")
    from sqlide.backend.db.mysql.connector import _utc_offset

    assert _utc_offset("UTC") == "+00:00"
    assert _utc_offset("+05:30") == "+05:30"  # already an offset
    assert _utc_offset("Mars/Olympus") == ""  # nothing to try
