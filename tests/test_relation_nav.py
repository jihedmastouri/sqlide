"""Foreign-key navigation in the grid (CORE-43).

Two halves, like CORE-42's. The decision — which navigations a cell
offers and what filter each one carries — is pure and asserted over
plain `RelationInfo` rows, including the composite and cross-schema
cases no engine here can be asked for on a test machine. The widget
half goes through a real ResultGrid over a real SQLite table, so the
menu, the header marker and the callback are asserted against the grid
the user actually right-clicks.
"""

from __future__ import annotations

import sqlite3

import pytest

from sqlide.backend.db.base import RelationInfo
from sqlide.backend.db.relations import (
    foreign_key_columns,
    incoming_targets,
    outgoing_targets,
)
from sqlide.backend.db.sqlite.connector import SqliteConnector


@pytest.fixture()
def gtk():
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gtk

    if not Gtk.init_check():
        pytest.skip("no display for GTK")
    return Gtk


SIMPLE = [
    RelationInfo("orders", "customer_id", "customers", "id"),
    RelationInfo("orders", "shipper_id", "shippers", "id"),
    RelationInfo("invoices", "order_id", "orders", "id"),
]

COMPOSITE = [
    RelationInfo("lines", "tenant", "orders", "tenant"),
    RelationInfo("lines", "order_no", "orders", "no"),
]

TWO_KEYS_ONE_TABLE = [
    RelationInfo("edges", "from_id", "nodes", "id"),
    RelationInfo("edges", "to_id", "nodes", "id"),
]

CROSS_SCHEMA = [
    RelationInfo("orders", "customer_id", "customers", "id", "sales", "crm"),
]


class TestOutgoing:
    def test_a_key_column_offers_its_target(self):
        [target] = outgoing_targets(
            SIMPLE, "orders", "customer_id", {"customer_id": 4812}
        )
        assert target.table == "customers"
        assert target.label == "customers"
        assert [(f.column, f.op, f.value) for f in target.filters] == [
            ("id", "=", "4812")
        ]

    def test_a_plain_column_offers_nothing(self):
        assert outgoing_targets(SIMPLE, "orders", "total", {"total": 9}) == []

    def test_a_null_key_offers_nothing_rather_than_an_empty_tab(self):
        assert (
            outgoing_targets(
                SIMPLE, "orders", "customer_id", {"customer_id": None}
            )
            == []
        )

    def test_a_composite_key_is_one_entry_with_one_condition_per_pair(self):
        [target] = outgoing_targets(
            COMPOSITE, "lines", "order_no", {"tenant": "acme", "order_no": 7}
        )
        assert target.table == "orders"
        assert [(f.column, f.value) for f in target.filters] == [
            ("tenant", "acme"),
            ("no", "7"),
        ]

    def test_a_composite_key_with_a_null_part_offers_nothing(self):
        assert (
            outgoing_targets(
                COMPOSITE, "lines", "order_no", {"tenant": None, "order_no": 7}
            )
            == []
        )

    def test_two_keys_into_one_table_stay_two_entries(self):
        row = {"from_id": 1, "to_id": 2}
        [first] = outgoing_targets(TWO_KEYS_ONE_TABLE, "edges", "from_id", row)
        [second] = outgoing_targets(TWO_KEYS_ONE_TABLE, "edges", "to_id", row)
        assert first.filters[0].value == "1"
        assert second.filters[0].value == "2"

    def test_a_cross_schema_key_names_the_schema_it_points_into(self):
        [target] = outgoing_targets(
            CROSS_SCHEMA,
            "orders",
            "customer_id",
            {"customer_id": 3},
            schema="sales",
        )
        assert (target.schema, target.table) == ("crm", "customers")
        assert target.label == "crm.customers"

    def test_a_same_schema_key_is_not_qualified(self):
        rows = [
            RelationInfo("orders", "customer_id", "customers", "id",
                         "sales", "sales")
        ]
        [target] = outgoing_targets(
            rows, "orders", "customer_id", {"customer_id": 3}, schema="sales"
        )
        assert target.schema == ""
        assert target.label == "customers"

    def test_another_schemas_table_of_the_same_name_is_not_ours(self):
        rows = [
            RelationInfo("orders", "customer_id", "customers", "id",
                         "archive", "archive")
        ]
        assert (
            outgoing_targets(
                rows, "orders", "customer_id", {"customer_id": 3},
                schema="sales",
            )
            == []
        )


class TestIncoming:
    def test_the_tables_pointing_here_are_offered(self):
        [target] = incoming_targets(SIMPLE, "orders", {"id": 12})
        assert target.table == "invoices"
        assert target.incoming is True
        assert [(f.column, f.value) for f in target.filters] == [
            ("order_id", "12")
        ]

    def test_a_composite_reference_filters_on_every_column(self):
        [target] = incoming_targets(
            COMPOSITE, "orders", {"tenant": "acme", "no": 7}
        )
        assert [(f.column, f.value) for f in target.filters] == [
            ("tenant", "acme"),
            ("order_no", "7"),
        ]

    def test_a_null_referenced_value_offers_nothing(self):
        assert incoming_targets(SIMPLE, "orders", {"id": None}) == []

    def test_a_table_nothing_points_at_offers_nothing(self):
        assert incoming_targets(SIMPLE, "invoices", {"id": 1}) == []


class TestHeaderMarking:
    def test_only_key_columns_are_marked(self):
        assert foreign_key_columns(SIMPLE, "orders") == {
            "customer_id",
            "shipper_id",
        }

    def test_a_table_without_keys_marks_nothing(self):
        assert foreign_key_columns(SIMPLE, "shippers") == set()


@pytest.fixture()
def db(tmp_path):
    """A SQLite database with a declared key and one without."""
    path = tmp_path / "core43.db"
    sqlite3.connect(path).close()
    connector = SqliteConnector(str(path))
    connector.connect()
    connector.execute("CREATE TABLE customers (id INTEGER PRIMARY KEY, "
                      "name TEXT)")
    connector.execute(
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER "
        "REFERENCES customers(id), total REAL)"
    )
    connector.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
    connector.execute("INSERT INTO customers VALUES (1, 'ada')")
    connector.execute(
        "INSERT INTO orders VALUES (1, 1, 9.5), (2, NULL, 3.0)"
    )
    yield connector
    connector.close()


class TestOverTheCatalog:
    def test_sqlite_declares_the_key_the_grid_navigates(self, db):
        relations = db.catalog_relations()
        [target] = outgoing_targets(
            relations, "orders", "customer_id", {"customer_id": 1}
        )
        assert target.table == "customers"
        [back] = incoming_targets(
            db.list_references("customers"), "customers", {"id": 1}
        )
        assert back.table == "orders"

    def test_a_table_with_no_declared_keys_offers_nothing(self, db):
        relations = db.catalog_relations()
        assert foreign_key_columns(relations, "notes") == set()
        assert outgoing_targets(relations, "notes", "body", {"body": "x"}) == []
        assert incoming_targets(
            db.list_references("notes"), "notes", {"id": 1}
        ) == []

    def test_the_relations_are_read_from_the_cache_not_per_right_click(
        self, db
    ):
        db.catalog_relations()
        calls = []
        original = db.list_relations
        db.list_relations = lambda: (calls.append(1), original())[1]
        db.catalog_relations()
        db.catalog_relations()
        assert calls == []


class TestGridMenu:
    """The widget half: the grid marks the columns and calls back."""

    def _grid(self, gtk, db):
        from sqlide.frontend.data_grid import ResultGrid

        grid = ResultGrid(table_name="orders")
        grid.set_relations(
            "orders", db.catalog_relations(), db.list_references("orders")
        )
        result = db.fetch_rows("orders")
        grid.set_result(result.columns, result.rows)
        return grid

    def _titles(self, grid) -> list[str]:
        columns = grid._view.get_columns()
        return [
            columns.get_item(i).get_title()
            for i in range(columns.get_n_items())
        ]

    def test_key_columns_are_marked_in_the_header(self, gtk, db):
        grid = self._grid(gtk, db)
        assert "customer_id ⇢" in self._titles(grid)
        assert "total" in self._titles(grid)

    def test_the_menu_offers_the_target_of_the_cell(self, gtk, db):
        grid = self._grid(gtk, db)
        followed = []
        grid.on_navigate = followed.append
        grid._menu_cell = (0, 1)  # customer_id of the first row
        menu = grid._cell_menu()
        assert grid._nav_targets and grid._nav_targets[0].table == "customers"
        assert menu.get_n_items() > 0
        grid._on_relate(None, _string("0"))
        assert followed and followed[0].filters[0].value == "1"

    def test_a_null_key_cell_offers_no_navigation(self, gtk, db):
        grid = self._grid(gtk, db)
        grid.on_navigate = lambda target: None
        grid._menu_cell = (1, 1)  # the row whose customer_id is NULL
        grid._cell_menu()
        assert grid._nav_targets == []

    def test_a_grid_told_no_relations_offers_none(self, gtk, db):
        from sqlide.frontend.data_grid import ResultGrid

        grid = ResultGrid(table_name="orders")
        result = db.fetch_rows("orders")
        grid.set_result(result.columns, result.rows)
        grid.on_navigate = lambda target: None
        grid._menu_cell = (0, 1)
        grid._cell_menu()
        assert grid._nav_targets == []


def _string(text: str):
    from gi.repository import GLib

    return GLib.Variant.new_string(text)
