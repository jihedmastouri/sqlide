"""Per-connection catalog cache (CORE-41).

Every page of a grid used to re-read the catalog twice: once to check
the table exists, once more to check the filter's columns. The check
itself is what keeps unvalidated identifiers out of SQL text and stays;
what stops is paying a round trip for it every time.

The interesting half is invalidation, so most of this file is about
when the cache is *dropped*: a statement that could have changed the
catalog, a reconnect, the sidebar's Refresh — and, for the one event
nobody can observe, a validation miss that re-reads before it rejects.

SQLite throughout: it needs no server, and the cache lives on the
shared Connector base, so what holds here holds for every adapter.
"""

from __future__ import annotations

import sqlite3

import pytest

from sqlide.backend import sql_risk
from sqlide.backend.db.base import (
    ConnectorError,
    FilterCondition,
    RowOperation,
    SortSpec,
)
from sqlide.backend.db.sqlite.connector import SqliteConnector


class CountingSqlite(SqliteConnector):
    """A real connector that counts the catalog listings it makes."""

    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.tables_read = 0
        self.columns_read = 0

    def list_tables(self):
        self.tables_read += 1
        return super().list_tables()

    def list_columns(self, table: str):
        self.columns_read += 1
        return super().list_columns(table)

    def reset_counts(self) -> None:
        self.tables_read = 0
        self.columns_read = 0


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "core41.db"
    sqlite3.connect(path).close()
    connector = CountingSqlite(str(path))
    connector.connect()
    connector.execute(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, team TEXT)"
    )
    connector.execute(
        "INSERT INTO users (id, name, team) VALUES "
        + ", ".join(f"({i}, 'n{i}', 't{i % 2}')" for i in range(1, 8))
    )
    connector.reset_counts()
    yield connector
    connector.close()


# What the cache saves


def test_paging_reads_the_catalog_once(db):
    for offset in range(0, 6, 2):
        db.fetch_rows("users", offset, 2)
    assert db.tables_read == 1
    assert db.columns_read <= 1


def test_a_filtered_page_still_reads_columns_once(db):
    for offset in range(0, 6, 2):
        db.fetch_rows(
            "users",
            offset,
            2,
            filters=[FilterCondition("team", "=", "t1")],
            order_by=[SortSpec("name")],
        )
    assert db.tables_read == 1
    assert db.columns_read == 1


def test_many_cell_edits_read_columns_once(db):
    for i in range(1, 5):
        db.update_cell("users", {"id": i}, "name", f"edited{i}")
    assert db.columns_read == 1


def test_a_batch_of_edits_reads_columns_once(db):
    db.apply_changes(
        "users",
        [
            RowOperation({"id": i}, "name", f"batch{i}")
            for i in range(1, 5)
        ],
    )
    assert db.columns_read == 1


def test_the_cache_is_per_scope_not_per_table(db):
    db.execute("CREATE TABLE notes (body TEXT)")
    db.reset_counts()
    db.fetch_rows("users", 0, 2, order_by=[SortSpec("name")])
    db.fetch_rows("notes", 0, 2, order_by=[SortSpec("body")])
    assert db.tables_read == 1  # one listing covers both tables
    assert db.columns_read == 2  # but columns are per table


# Invalidation


def test_ddl_through_the_app_invalidates(db):
    assert "note" not in {c.name for c in db.catalog_columns("users")}
    db.execute("ALTER TABLE users ADD COLUMN note TEXT")
    assert "note" in {c.name for c in db.catalog_columns("users")}


def test_a_dropped_table_stops_being_known(db):
    db.fetch_rows("users", 0, 2)
    db.execute("DROP TABLE users")
    with pytest.raises(ConnectorError):
        db.fetch_rows("users", 0, 2)


def test_reads_and_row_changes_keep_the_cache(db):
    db.catalog_tables()
    db.reset_counts()
    db.execute("SELECT 1")
    db.execute("INSERT INTO users (id, name) VALUES (99, 'x')")
    db.catalog_tables()
    assert db.tables_read == 0


def test_a_statement_the_classifier_cannot_name_invalidates(db):
    # "other" is treated as catalog-changing: guessing the safe way
    # round would put a dropped object's name into a statement.
    assert sql_risk.changes_catalog("RENAME TABLE users TO people")
    db.catalog_tables()
    db.reset_counts()
    db.execute("VACUUM")
    db.catalog_tables()
    assert db.tables_read == 1


def test_a_failed_ddl_statement_still_invalidates(db):
    db.catalog_tables()
    db.reset_counts()
    with pytest.raises(ConnectorError):
        db.execute("ALTER TABLE nope ADD COLUMN x TEXT")
    db.catalog_tables()
    assert db.tables_read == 1


def test_closing_empties_the_cache(db):
    db.catalog_tables()
    assert len(db.catalog_cache)
    db.close()
    assert len(db.catalog_cache) == 0
    db.connect()


# The one event nobody can observe: another session


def _behind_our_back(db, sql: str) -> None:
    """Run DDL on the same file through a second connection, which is
    exactly what the cache cannot be told about."""
    other = sqlite3.connect(db.file_path)
    other.execute(sql)
    other.commit()
    other.close()


def test_a_validation_miss_reloads_before_rejecting(db):
    db.fetch_rows("users", 0, 2, order_by=[SortSpec("name")])
    _behind_our_back(db, "ALTER TABLE users ADD COLUMN nickname TEXT")
    db.reset_counts()
    # Filtering on the new column must work: the miss is a reload.
    db.fetch_rows("users", 0, 2, filters=[FilterCondition("nickname", "IS NULL")])
    assert db.columns_read == 1


def test_a_table_another_session_created_can_be_opened(db):
    db.catalog_tables()
    _behind_our_back(db, "CREATE TABLE late (id INTEGER PRIMARY KEY)")
    db.reset_counts()
    db.fetch_rows("late", 0, 2)
    assert db.tables_read == 1


def test_an_unknown_column_retries_once_and_then_raises(db):
    db.fetch_rows("users", 0, 2, order_by=[SortSpec("name")])
    db.reset_counts()
    with pytest.raises(ConnectorError, match="Unknown column"):
        db.fetch_rows("users", 0, 2, order_by=[SortSpec("nope")])
    assert db.columns_read == 1  # one reload, not a listing per attempt


def test_an_unknown_table_retries_once_and_then_raises(db):
    db.catalog_tables()
    db.reset_counts()
    with pytest.raises(ConnectorError, match="No such table"):
        db.fetch_rows("ghost", 0, 2)
    assert db.tables_read == 1


# The sidebar's Refresh


@pytest.fixture()
def sidebar(db):
    """A sidebar on one connection, with every callback a no-op.

    Refresh is a main-loop action and the connector may not even exist
    yet, so the sidebar only marks the connection stale; the drop
    happens on the worker thread that next asks for the connector
    (Sidebar._connector), which is what this exercises.
    """
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gtk

    if not Gtk.init_check():
        pytest.skip("no display for GTK")
    from sqlide.backend.connections import ConnectionProfile
    from sqlide.frontend.sidebar import Sidebar

    def noop(*_args, **_kwargs):
        return None

    profile = ConnectionProfile("shop", "sqlite", file_path=db.file_path)
    names = (
        "on_open_table on_open_object on_open_section on_new_query "
        "on_open_cli on_open_definition on_open_function on_relation_graph "
        "on_view_indexes on_query_builder on_drop_object on_new_object "
        "on_mcp_server on_manage_users on_monitor on_open_schema "
        "on_edit_connection on_disconnect on_close_tabs on_remove_connection "
        "on_add_connection show_error"
    ).split()
    bar = Sidebar(
        ensure_connector=lambda _profile: db,
        count_tabs=lambda _name: 0,
        **{name: noop for name in names},
    )
    bar.add_profile(profile)
    return bar, profile


def test_sidebar_refresh_clears_the_cache(sidebar, db):
    bar, profile = sidebar
    db.catalog_tables()
    assert len(db.catalog_cache)
    bar.reload_connection(profile.name)
    # Still cached: nothing has gone near the connection yet.
    assert len(db.catalog_cache)
    assert bar._connector(profile) is db
    assert len(db.catalog_cache) == 0


def test_the_refresh_is_spent_once(sidebar, db):
    bar, profile = sidebar
    bar.reload_connection(profile.name)
    bar._connector(profile)
    db.catalog_tables()
    bar._connector(profile)  # no Refresh since, so the cache survives
    assert len(db.catalog_cache)
