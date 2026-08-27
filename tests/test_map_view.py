"""The map widget and the grid cells that lead to it (PG-04).

Widget tests, so they skip without a display — and they still make no
network request: the map is handed a tile loader whose transport fails
loudly and whose online probe says no, which is exactly the offline
case the viewer has to survive.
"""

from __future__ import annotations

import struct

import pytest

from sqlide.backend import tiles
from sqlide.backend.db import geo


@pytest.fixture()
def gtk():
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gtk

    if not Gtk.init_check():
        pytest.skip("no display for GTK")
    return Gtk


def _point(x: float, y: float, srid: int = 4326) -> str:
    head = b"\x01" + struct.pack("<I", 1 | 0x20000000) + struct.pack("<i", srid)
    return (head + struct.pack("<dd", x, y)).hex()


def _offline_loader(tmp_path) -> tiles.TileLoader:
    return tiles.TileLoader(
        tiles.TileSource(url_template="https://t.test/{z}/{x}/{y}.png"),
        directory=tmp_path,
        transport=lambda _url: pytest.fail("no tile request may be made"),
        online=lambda: False,
    )


class _FakeGesture:
    """Enough of Gtk.GestureClick for the click handler: a press that
    nothing else claimed."""

    def get_current_sequence(self):
        return None

    def get_sequence_state(self, _sequence):
        from gi.repository import Gtk

        return Gtk.EventSequenceState.NONE


def _map(gtk, tmp_path, on_select=None):
    from sqlide.frontend.map_view import MapView

    return MapView(on_select=on_select, loader=_offline_loader(tmp_path))


class TestMapView:
    def test_an_empty_result_shows_the_empty_page(self, gtk, tmp_path):
        view = _map(gtk, tmp_path)
        view.set_features(geo.build_features(["id"], [(1,)]))
        assert view._stack.get_visible_child_name() == "empty"

    def test_features_fit_the_view_and_say_they_are_offline(self, gtk, tmp_path):
        view = _map(gtk, tmp_path)
        rows = [(1, _point(2.35, 48.85)), (2, _point(2.40, 48.90))]
        view.set_features(geo.build_features(["id", "geom"], rows))
        assert view._stack.get_visible_child_name() == "map"
        # Centred between the two points, and told the user why the
        # background is blank rather than hanging on a tile request.
        assert view._center[0] == pytest.approx(2.375, abs=1e-3)
        assert view._notice.get_visible()
        assert "network" in view._notice.get_text()
        # No tiles on screen means nobody's credit to print.
        assert not view._attribution.get_visible()

    def test_the_cap_notice_reaches_the_user(self, gtk, tmp_path):
        view = _map(gtk, tmp_path)
        rows = [(i, _point(i / 100, 10)) for i in range(20)]
        view.set_features(geo.build_features(["id", "geom"], rows, cap=5))
        assert "Showing 5 of 20 features" in view._notice.get_text()

    def test_clicking_a_feature_reports_its_row(self, gtk, tmp_path):
        picked: list[int] = []
        view = _map(gtk, tmp_path, on_select=picked.append)
        rows = [(1, _point(0.0, 0.0)), (2, _point(40.0, 40.0))]
        view.set_features(geo.build_features(["id", "geom"], rows))
        view._center = (0.0, 0.0)
        view._zoom = 4
        width, height = view._viewport()
        hit = view._to_screen((0.0, 0.0), width, height)
        assert view.feature_at(*hit) == 0
        # The other point is elsewhere, and empty space hits nothing.
        other = view._to_screen((40.0, 40.0), width, height)
        assert view.feature_at(*other) == 1
        assert view.feature_at(hit[0] + 60, hit[1] + 60) is None
        view._on_click(_FakeGesture(), 1, *hit)
        assert picked == [0]

    def test_select_row_highlights_and_clears(self, gtk, tmp_path):
        view = _map(gtk, tmp_path)
        rows = [(1, _point(1.0, 1.0)), (2, _point(2.0, 2.0))]
        view.set_features(geo.build_features(["id", "geom"], rows))
        view.select_row(1)
        assert view.selected_row == 1
        view.select_row(None)
        assert view.selected_row is None
        view.select_row(99)  # a row with no geometry
        assert view.selected_row is None

    def test_tiles_turned_off_is_stated_not_hidden(self, gtk, tmp_path):
        from sqlide.frontend.map_view import MapView

        loader = tiles.TileLoader(
            tiles.TileSource(enabled=False),
            directory=tmp_path,
            transport=lambda _url: pytest.fail("tiles are off"),
            online=lambda: pytest.fail("must not probe"),
        )
        view = MapView(loader=loader)
        view.set_features(
            geo.build_features(["id", "geom"], [(1, _point(3.0, 3.0))])
        )
        assert "turned off" in view._notice.get_text()


class TestGeometryCells:
    def _grid(self, gtk, geo_enabled: bool):
        from sqlide.frontend.data_grid import ResultGrid

        grid = ResultGrid()
        grid.geo_enabled = geo_enabled
        grid.set_result(
            ["id", "geom"], [(1, _point(2.35, 48.85)), (2, None)]
        )
        return grid

    def test_a_geometry_cell_reads_as_a_summary(self, gtk):
        grid = self._grid(gtk, geo_enabled=True)
        assert grid.geometry_columns() == ["geom"]
        assert grid._cell_display(1, _point(2.35, 48.85)) == (
            "Point, SRID 4326, 1 point"
        )
        assert grid.geometry_at(0, 1).kind == "Point"
        assert grid.geometry_at(1, 1) is None  # NULL

    def test_without_the_extension_nothing_is_treated_as_geometry(self, gtk):
        grid = self._grid(gtk, geo_enabled=False)
        assert grid.geometry_columns() == []
        assert grid.geometry_at(0, 1) is None
        assert grid._cell_display(1, _point(2.35, 48.85)).startswith("0101")

    def test_show_on_map_is_live_only_over_a_geometry_cell(self, gtk):
        grid = self._grid(gtk, geo_enabled=True)
        opened: list[tuple[int, str]] = []
        grid.on_show_map = lambda row, column: opened.append((row, column))
        grid._menu_cell = (0, 0)
        grid._refresh_map_action()
        assert not grid._show_map_action.get_enabled()
        grid._menu_cell = (0, 1)
        grid._refresh_map_action()
        assert grid._show_map_action.get_enabled()
        grid._on_show_map()
        assert opened == [(0, "geom")]

    def test_no_map_callback_means_no_menu_item(self, gtk):
        grid = self._grid(gtk, geo_enabled=True)
        grid._menu_cell = (0, 1)
        grid._refresh_map_action()
        assert not grid._show_map_action.get_enabled()
