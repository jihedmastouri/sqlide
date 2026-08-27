"""Tabular sections of the object info view (CORE-49).

A listing of like-shaped records — columns, indexes, constraints,
grants — is the same shape as a result, so it is drawn in the result
grid rather than a bespoke list: it sorts, its columns resize and it
copies as CSV/JSON/Markdown. What stays untouched matters as much: a
section holding one record is still a key/value block, and a section
the descriptor never called tabular is still a plain list.
"""

from __future__ import annotations

import sqlite3

import pytest

from sqlide.backend.db import objects, registry
from sqlide.backend.db.base import Connector
from sqlide.backend.db.metadata import NodeRef
from sqlide.backend.db.sqlite.connector import SqliteConnector


@pytest.fixture()
def sqlite_db(tmp_path):
    path = tmp_path / "grid.db"
    sqlite3.connect(path).close()
    connector = SqliteConnector(str(path))
    connector.connect()
    connector.execute(
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, sku TEXT NOT NULL)"
    )
    connector.execute("CREATE INDEX orders_sku ON orders (sku)")
    yield connector
    connector.close()


def _section(info: objects.ObjectInfo, title: str) -> objects.DetailTable:
    for table in info.tables:
        if table.title.lower() == title.lower():
            return table
    raise AssertionError(f"no {title} section in {[t.title for t in info.tables]}")


# The descriptor side


def test_record_listings_declare_themselves_tabular(
    sqlite_db: Connector,
) -> None:
    provider = registry.create_provider("sqlite", sqlite_db)
    info = provider.table_properties(NodeRef("table", "orders"))
    for title in ("Columns", "Indexes", "Constraints"):
        assert _section(info, title).tabular, title


def test_a_folder_listing_is_tabular(sqlite_db: Connector) -> None:
    info = objects.describe(sqlite_db, "category", "Indexes", category="indexes")
    assert _section(info, "Indexes").tabular


def test_a_listing_of_folders_is_not_tabular(sqlite_db: Connector) -> None:
    info = objects.describe(
        sqlite_db, "category", "Administer", category="administer"
    )
    assert not _section(info, "Administer").tabular


def test_a_typed_column_is_declared(sqlite_db: Connector) -> None:
    info = objects.describe(sqlite_db, "category", "Tables", category="tables")
    table = _section(info, "Tables")
    assert table.column_type(table.columns.index("Columns")) == "number"
    assert table.column_type(0) == "text"


def test_one_record_is_not_a_grid() -> None:
    single = objects.DetailTable(
        title="Indexes", columns=["Name"], rows=[("orders_sku",)], tabular=True
    )
    many = objects.DetailTable(
        title="Indexes",
        columns=["Name"],
        rows=[("orders_sku",), ("orders_id",)],
        tabular=True,
    )
    plain = objects.DetailTable(
        title="Administer", columns=["Name"], rows=[("a",), ("b",)]
    )
    assert not single.as_grid
    assert many.as_grid
    assert not plain.as_grid


# The rendered side


@pytest.fixture()
def info_body():
    gi = pytest.importorskip("gi")
    gi.require_version("Adw", "1")
    from sqlide.frontend.object_info import InfoBody

    opened: list[objects.ObjectRef] = []
    return InfoBody(opened.append), opened


def _render(body, table: objects.DetailTable) -> None:
    body.render(
        objects.ObjectInfo(
            kind="table", name="orders", type_label="Table", tables=[table]
        )
    )


def test_a_listing_is_drawn_in_the_result_grid(info_body) -> None:
    body, _opened = info_body
    _render(body, objects.DetailTable(
        title="Indexes",
        columns=["Name", "Table"],
        rows=[("orders_sku", "orders"), ("orders_id", "orders")],
        links=[
            objects.ObjectRef("index", "orders_sku", "orders"),
            objects.ObjectRef("index", "orders_id", "orders"),
        ],
        tabular=True,
    ))
    assert len(body._grids) == 1
    grid = body._grids[0].grid
    assert grid._column_names == ["Name", "Table"]
    # Copy-as is the grid's own, so the section gets it for free.
    assert grid.table_name == "Indexes"


def test_a_grid_row_opens_the_child_object(info_body) -> None:
    body, opened = info_body
    _render(body, objects.DetailTable(
        title="Indexes",
        columns=["Name"],
        rows=[("orders_sku",), ("orders_id",)],
        links=[
            objects.ObjectRef("index", "orders_sku", "orders"),
            objects.ObjectRef("index", "orders_id", "orders"),
        ],
        tabular=True,
    ))
    body._grids[0]._activate(1)
    assert opened == [objects.ObjectRef("index", "orders_id", "orders")]


def test_sorting_reorders_rows_and_keeps_their_links(info_body) -> None:
    body, opened = info_body
    _render(body, objects.DetailTable(
        title="Tables",
        columns=["Name", "Columns"],
        rows=[("orders", "9"), ("users", "10"), ("audit", "2")],
        links=[
            objects.ObjectRef("table", name)
            for name in ("orders", "users", "audit")
        ],
        types=("text", "number"),
        tabular=True,
    ))
    section = body._grids[0]
    section._sort([("Name", False)])
    assert [row[0] for row, _link in section._rows] == [
        "audit", "orders", "users"
    ]
    # A numeric column sorts by value, not by spelling: 9 before 10.
    section._sort([("Columns", False)])
    assert [row[1] for row, _link in section._rows] == ["2", "9", "10"]
    # …and a row still opens what it always opened.
    section._activate(0)
    assert opened == [objects.ObjectRef("table", "audit")]
    section._sort([])
    assert [row[0] for row, _link in section._rows] == [
        "orders", "users", "audit"
    ]


def test_a_single_record_stays_a_key_value_block(info_body) -> None:
    body, opened = info_body
    _render(body, objects.DetailTable(
        title="Indexes",
        columns=["Name", "Table"],
        rows=[("orders_sku", "orders")],
        links=[objects.ObjectRef("index", "orders_sku", "orders")],
        tabular=True,
    ))
    assert body._grids == []
    # The link the row would have carried is still reachable.
    group = body._box.get_last_child()
    group.get_header_suffix().emit("clicked")
    assert opened == [objects.ObjectRef("index", "orders_sku", "orders")]


def test_a_section_that_is_not_tabular_is_untouched(info_body) -> None:
    body, _opened = info_body
    _render(body, objects.DetailTable(
        title="Administer",
        columns=["Name", "Holds"],
        rows=[("Roles", "principal"), ("Storage", "tablespace")],
    ))
    assert body._grids == []
