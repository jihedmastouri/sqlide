"""The properties tab: one screen, and the panels a root object adds.

Every object opens the same way (frontend/object_info.SectionedBody):
a header saying what it is and where, a row of tabs, and a page per
section — General, each listing over a filter, the definition. A
connection and a database add to that what is actually asked of them:
counts as tiles and live numbers as meters, which their descriptor
brings from backend/db/overview.py.

The engine-specific numbers are covered by the engine suites; what is
tested here is the shape both engines answer in, the formatting the
panels are read through, and that a table and a database really are
the same screen — over a SQLite file, so it runs without a server.
"""

from __future__ import annotations

import sqlite3

import pytest

from sqlide.backend.db import objects, overview, registry


@pytest.fixture()
def connector(tmp_path):
    db = tmp_path / "shop.db"
    raw = sqlite3.connect(db)
    raw.executescript(
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, sku TEXT);"
        "CREATE TABLE customers (id INTEGER PRIMARY KEY);"
        "CREATE VIEW recent AS SELECT 1 AS x;"
        "CREATE INDEX orders_sku ON orders (sku);"
    )
    raw.commit()
    raw.close()
    conn = registry.create_connector("sqlite", file_path=str(db))
    conn.connect()
    yield conn
    conn.close()


# What the numbers are read as


def test_a_size_reads_in_the_unit_it_fits() -> None:
    assert overview.format_size(512) == "512 B"
    assert overview.format_size(8.2 * 1024 ** 2) == "8.2 MB"
    assert overview.format_size(138 * 1024 ** 2) == "138 MB"
    assert overview.format_size(None) == "—"


def test_an_uptime_reads_in_the_two_units_that_matter() -> None:
    assert overview.format_uptime(90) == "1m"
    assert overview.format_uptime(3 * 3600 + 600) == "3h 10m"
    assert overview.format_uptime(12 * 86400 + 4 * 3600) == "12d 4h"


def test_a_round_trip_keeps_a_decimal_only_while_it_is_small() -> None:
    assert overview.format_ms(1.43) == "1.4 ms"
    assert overview.format_ms(41.2) == "41 ms"
    assert overview.format_ms(2400) == "2.4 s"


def test_a_row_count_is_grouped_and_a_missing_one_is_a_dash() -> None:
    assert overview.format_count(1204900) == "1,204,900"
    assert overview.format_count(None) == "—"


def test_a_meter_with_a_ceiling_carries_the_fraction_of_it() -> None:
    meter = overview._pool_meter(3, 100)
    assert meter.value == "3 / 100"
    assert meter.fraction == pytest.approx(0.03)


def test_a_pool_that_is_nearly_full_is_marked_as_a_warning() -> None:
    assert overview._pool_meter(95, 100).tone == "warn"
    assert overview._pool_meter(5, 100).tone == ""


def test_a_meter_without_a_ceiling_asks_for_no_bar() -> None:
    assert overview._pool_meter(7, None).fraction == overview.NO_BAR


def test_a_cache_ratio_needs_something_to_have_happened() -> None:
    assert overview._cache_meter(0, 0, "cluster") is None
    meter = overview._cache_meter(992.0, 8.0, "cluster")
    assert meter.value == "99.2 %"
    assert meter.tone == "good"


def test_an_overview_with_nothing_in_it_is_falsy() -> None:
    """What decides whether the tab draws the dashboard at all."""
    assert not overview.Overview()
    assert overview.Overview(counts=(overview.Count("Tables", "4"),))


# What the descriptor carries


def test_a_database_carries_a_count_for_each_kind_it_holds(connector) -> None:
    info = objects.describe(connector, "database", "shop")
    counts = {count.label: count.value for count in info.overview.counts}
    assert counts["Tables"] == "2"
    assert counts["Views"] == "1"
    assert counts["Indexes"] == "1"


def test_a_database_leaves_out_a_count_its_engine_has_no_such_thing_for(
    connector,
) -> None:
    """A "SEQUENCES 0" tile on SQLite would read as "none of them"
    rather than "no such thing", which is the wrong claim."""
    info = objects.describe(connector, "database", "shop")
    assert "Sequences" not in {c.label for c in info.overview.counts}


def test_a_database_says_where_it_is_in_its_own_header(connector) -> None:
    info = objects.describe(connector, "database", "shop")
    assert info.overview.endpoint.endswith("shop")


def test_a_databases_listing_is_the_section_a_count_tile_opens(
    connector,
) -> None:
    info = objects.describe(connector, "database", "shop")
    tiles = {c.label: c.section for c in info.overview.counts}
    assert tiles["Tables"] == "objects"
    assert info.tables[0].slug == "objects"


def test_a_connection_describes_the_connection_itself(connector) -> None:
    info = objects.describe(connector, "connection", "shop")
    summary = dict(info.summary)
    assert summary["Driver"] == "sqlite"
    assert summary["Server version"]
    assert summary["SSL"] == "disabled"


def test_a_connection_leaves_out_the_roles_its_engine_does_not_have(
    connector,
) -> None:
    info = objects.describe(connector, "connection", "shop")
    assert "Roles" not in {c.label for c in info.overview.counts}


def test_a_file_engine_reports_no_server_to_be_live(connector) -> None:
    """SQLite is a file: it has no round trip to measure and no
    uptime, and a panel of zeroes would be a claim about a server that
    is not there."""
    assert overview.connection_meters("sqlite", connector) == ()
    meters = overview.database_meters("sqlite", connector, "shop")
    assert [meter.label for meter in meters] == ["Size on disk"]


def test_an_object_that_is_not_a_root_carries_no_overview(
    connector,
) -> None:
    info = objects.describe(connector, "table", "orders")
    assert info.overview is None


def test_a_catalog_question_the_engine_refuses_costs_only_that_panel(
    connector,
) -> None:
    """Every optional read is wrapped: a server that will not answer
    one of them still opens."""

    class Refusing:
        def execute(self, sql, max_rows=None):
            raise RuntimeError("nope")

    assert overview.object_sizes("postgres", Refusing()) == {}
    assert overview.server_version("postgres", Refusing()) == ""


# The tab


@pytest.fixture()
def gtk():
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gtk

    if not Gtk.init_check():
        pytest.skip("no display for GTK")
    return Gtk


def _inline(work, on_success, on_error):
    try:
        result = work()
    except Exception as exc:  # pragma: no cover - a test bug
        on_error(exc)
    else:
        on_success(result)


@pytest.fixture()
def tab(gtk, connector, monkeypatch):
    from sqlide.backend.connections import ConnectionProfile
    from sqlide.frontend import data_grid, object_info

    monkeypatch.setattr(object_info, "run_async", _inline)
    monkeypatch.setattr(data_grid, "run_async", _inline)
    profile = ConnectionProfile("shop", "sqlite", file_path="shop.db")
    return object_info.PropertiesTab(
        profile,
        objects.ObjectRef("database", "shop"),
        lambda _profile: connector,
        lambda _message: None,
        lambda *_args: None,
        on_open_query=lambda _profile: None,
        on_edit_connection=lambda _profile: None,
    )


def test_a_database_leads_with_its_panels(tab) -> None:
    """The tab's own title bar gives way to the body's header: one
    name, one row of actions."""
    assert tab._stack.get_visible_child_name() == "info"
    assert not tab._bar.get_visible()


def test_the_tab_pages_the_general_view_and_every_listing(tab) -> None:
    assert tab._body.sections == ["general", "objects"]


def test_a_deep_link_selects_a_page(tab) -> None:
    tab.select_section("objects")
    assert tab._body._stack.get_visible_child() is (
        tab._body._pages["objects"]
    )


def _tab_for(gtk, connector, monkeypatch, ref):
    from sqlide.backend.connections import ConnectionProfile
    from sqlide.frontend import data_grid, object_info

    monkeypatch.setattr(object_info, "run_async", _inline)
    monkeypatch.setattr(data_grid, "run_async", _inline)
    profile = ConnectionProfile("shop", "sqlite", file_path="shop.db")
    return object_info.PropertiesTab(
        profile,
        ref,
        lambda _profile: connector,
        lambda _message: None,
        lambda *_args: None,
    )


def test_every_object_is_drawn_the_same_way(gtk, connector, monkeypatch):
    """A table has no counts and no live meters, but it is the same
    screen: a header, tabs, and a page per section."""
    table = _tab_for(
        gtk, connector, monkeypatch, objects.ObjectRef("table", "orders")
    )
    assert not table._bar.get_visible()
    assert table._body.sections[0] == "general"
    assert "columns" in table._body.sections
    assert "ddl" in table._body.sections


def test_a_table_gets_no_panels_it_has_no_numbers_for(
    gtk, connector, monkeypatch
):
    from sqlide.frontend.object_info import _OverviewPage

    table = _tab_for(
        gtk, connector, monkeypatch, objects.ObjectRef("table", "orders")
    )
    page = table._body._pages["general"]
    assert isinstance(page, _OverviewPage)
    # No LIVE caption: an object with no meters draws no meter row.
    assert "LIVE" not in _captions(page)


def test_a_connection_gets_the_settings_button_and_a_table_does_not(
    gtk, connector, monkeypatch
):
    from sqlide.backend.connections import ConnectionProfile
    from sqlide.frontend import data_grid, object_info

    monkeypatch.setattr(object_info, "run_async", _inline)
    monkeypatch.setattr(data_grid, "run_async", _inline)
    profile = ConnectionProfile("shop", "sqlite", file_path="shop.db")

    def make(ref):
        return object_info.PropertiesTab(
            profile, ref,
            lambda _profile: connector,
            lambda _message: None,
            lambda *_args: None,
            on_open_query=lambda _profile: None,
            on_edit_connection=lambda _profile: None,
        )

    assert "Settings" in _buttons(make(objects.ObjectRef("connection", "shop")))
    assert "Settings" not in _buttons(make(objects.ObjectRef("table", "orders")))


def _walk(widget):
    yield widget
    child = widget.get_first_child()
    while child is not None:
        yield from _walk(child)
        child = child.get_next_sibling()


def _buttons(tab) -> set[str]:
    from gi.repository import Gtk

    return {
        widget.get_label()
        for widget in _walk(tab._body._header)
        if isinstance(widget, Gtk.Button) and widget.get_label()
    }


def _captions(page) -> set[str]:
    from gi.repository import Gtk

    return {
        widget.get_label()
        for widget in _walk(page)
        if isinstance(widget, Gtk.Label)
        and widget.has_css_class("section-caption")
    }


# The listing on a dashboard page


def _section(rows, columns=("Name", "Kind"), types=("text", "text")):
    from sqlide.frontend.object_info import _GridSection

    table = objects.DetailTable(
        tabular=True, title="Tables and views",
        columns=list(columns), types=types, rows=rows,
    )
    return _GridSection(table, lambda _ref: None)


def test_a_listing_can_be_filtered_down_to_what_matches(gtk) -> None:
    section = _section([("orders", "table"), ("recent", "view")])
    assert section.filter("view") == 1
    assert section._rows[0][0] == ("recent", "view")
    assert section.filter("") == 2


def test_a_filter_and_a_sort_compose(gtk) -> None:
    section = _section(
        [("orders", "table"), ("customers", "table"), ("recent", "view")]
    )
    section._sort([("Name", True)])
    section.filter("table")
    assert [row[0][0] for row in section._rows] == ["orders", "customers"]


def test_a_formatted_column_sorts_by_what_it_means() -> None:
    """"1,204,900" rows and "8.2 MB" on disk are numbers that were made
    legible; sorting them as text puts 1.1 GB below 8.2 MB."""
    from sqlide.frontend.object_info import _sort_key

    sizes = [("1.1 GB",), ("8.2 MB",), ("512 B",)]
    assert [row[0] for row in sorted(
        sizes, key=lambda row: _sort_key(row, 0, "size")
    )] == ["512 B", "8.2 MB", "1.1 GB"]
    counts = [("1,204,900",), ("84,210",)]
    assert [row[0] for row in sorted(
        counts, key=lambda row: _sort_key(row, 0, "count")
    )] == ["84,210", "1,204,900"]


def test_a_cell_that_is_no_number_at_all_sorts_last() -> None:
    from sqlide.frontend.object_info import _sort_key

    assert _sort_key(("—",), 0, "size") > _sort_key(("512 B",), 0, "size")
