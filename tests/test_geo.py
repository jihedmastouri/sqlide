"""The geo viewer's readable half (PG-04).

Everything here runs offline and stays offline: the WKB parser needs no
server, and the tile loader takes its transport and its online probe as
arguments, so not one test opens a socket.
"""

from __future__ import annotations

import struct

import pytest

from sqlide.backend import settings as settings_module
from sqlide.backend import tiles
from sqlide.backend.db import geo
from sqlide.backend.db.metadata import Capabilities, MetadataProvider
from sqlide.backend.db.postgres.metadata import PostgresMetadata


# WKB, built here rather than pasted, so the expectations are readable.

def _wkb(type_code: int, payload: bytes, srid: int | None = None) -> bytes:
    code = type_code | (0x20000000 if srid is not None else 0)
    head = b"\x01" + struct.pack("<I", code)
    if srid is not None:
        head += struct.pack("<i", srid)
    return head + payload


def _point(x: float, y: float, srid: int | None = 4326) -> bytes:
    return _wkb(1, struct.pack("<dd", x, y), srid)


def _linestring(points, srid: int | None = 4326) -> bytes:
    payload = struct.pack("<I", len(points))
    for x, y in points:
        payload += struct.pack("<dd", x, y)
    return _wkb(2, payload, srid)


def _polygon(rings, srid: int | None = 4326) -> bytes:
    payload = struct.pack("<I", len(rings))
    for ring in rings:
        payload += struct.pack("<I", len(ring))
        for x, y in ring:
            payload += struct.pack("<dd", x, y)
    return _wkb(3, payload, srid)


_SQUARE = ((0, 0), (0, 1), (1, 1), (1, 0), (0, 0))


class TestParsing:
    def test_point_from_hex(self):
        geometry = geo.parse(_point(2.35, 48.85).hex())
        assert geometry.kind == "Point"
        assert geometry.srid == 4326
        assert geometry.point_count == 1
        assert geometry.shapes[0].coords[0] == pytest.approx((2.35, 48.85))

    def test_summary_names_type_srid_and_points(self):
        assert geo.parse(_point(1, 2)).summary() == "Point, SRID 4326, 1 point"
        line = geo.parse(_linestring([(0, 0), (1, 1), (2, 2)]))
        assert line.summary() == "LineString, SRID 4326, 3 points"

    def test_polygon_keeps_its_holes(self):
        hole = ((0.2, 0.2), (0.2, 0.8), (0.8, 0.8), (0.2, 0.2))
        geometry = geo.parse(_polygon([_SQUARE, hole]))
        shape = geometry.shapes[0]
        assert shape.kind == "polygon"
        assert len(shape.coords) == 5
        assert len(shape.holes) == 1

    def test_multipolygon_flattens_to_drawable_shapes(self):
        members = _polygon([_SQUARE], srid=None) + _polygon(
            [((5, 5), (5, 6), (6, 6), (5, 5))], srid=None
        )
        raw = _wkb(6, struct.pack("<I", 2) + members, 4326)
        geometry = geo.parse(raw)
        assert geometry.kind == "MultiPolygon"
        assert len(geometry.shapes) == 2
        assert all(s.kind == "polygon" for s in geometry.shapes)

    def test_geometry_collection_nests(self):
        members = _point(1, 1, srid=None) + _linestring(
            [(0, 0), (1, 1)], srid=None
        )
        raw = _wkb(7, struct.pack("<I", 2) + members, 4326)
        geometry = geo.parse(raw)
        assert geometry.kind == "GeometryCollection"
        assert [s.kind for s in geometry.shapes] == ["point", "line"]

    def test_big_endian_and_iso_z_are_read(self):
        # Big-endian ISO PointZ: 1000 + 1, three doubles.
        raw = b"\x00" + struct.pack(">I", 1001) + struct.pack(">ddd", 3, 4, 5)
        geometry = geo.parse(raw)
        assert geometry.kind == "Point"
        assert geometry.has_z
        assert geometry.summary().startswith("Point Z")
        assert geometry.shapes[0].coords[0] == pytest.approx((3, 4))

    def test_bytes_and_memoryview_are_accepted(self):
        raw = _point(1, 2)
        assert geo.parse(memoryview(raw)).kind == "Point"
        assert geo.parse(bytearray(raw)).kind == "Point"

    def test_garbage_is_an_error_not_a_crash(self):
        with pytest.raises(geo.GeometryError):
            geo.parse("not hex at all")
        with pytest.raises(geo.GeometryError):
            geo.parse(_point(1, 2)[:6])  # truncated
        with pytest.raises(geo.GeometryError):
            geo.parse(42)
        assert geo.summarize("nonsense") == ""

    def test_looks_like_geometry_rejects_ordinary_values(self):
        assert not geo.looks_like_geometry("ada@example.com")
        assert not geo.looks_like_geometry(42)
        assert not geo.looks_like_geometry("0102")  # too short
        assert geo.looks_like_geometry(_point(1, 2).hex())


class TestTransform:
    def test_wgs84_and_unset_srid_pass_through(self):
        for srid in (0, 4326):
            geometry = geo.parse(_point(2.0, 48.0, srid=srid))
            assert geometry.transformable
            assert geometry.to_wgs84() is geometry

    def test_web_mercator_is_transformed(self):
        # Paris in EPSG:3857.
        geometry = geo.parse(_point(261848.15, 6250566.72, srid=3857))
        lon, lat = geometry.to_wgs84().shapes[0].coords[0]
        assert lon == pytest.approx(2.3522, abs=1e-3)
        assert lat == pytest.approx(48.8566, abs=1e-3)

    def test_unknown_srid_is_reported_not_guessed(self):
        geometry = geo.parse(_point(651409.0, 313177.0, srid=27700))
        assert not geometry.transformable
        with pytest.raises(geo.GeometryError) as excinfo:
            geometry.to_wgs84()
        assert "27700" in str(excinfo.value)
        assert "ST_Transform" in str(excinfo.value)


class TestFeatureSets:
    def test_geometry_columns_are_found_by_value(self):
        columns = ["id", "name", "geom"]
        rows = [(1, "ada", _point(1, 2).hex()), (2, "bri", _point(3, 4).hex())]
        assert geo.geometry_columns(columns, rows) == ["geom"]

    def test_a_column_of_hex_that_is_not_wkb_is_not_a_geometry(self):
        rows = [(1, "deadbeefdeadbeefdeadbeef")]
        assert geo.geometry_columns(["id", "hash"], rows) == []

    def test_features_carry_their_row_and_a_label(self):
        rows = [(1, "ada", _point(1, 2).hex()), (2, "bri", None)]
        built = geo.build_features(["id", "name", "geom"], rows)
        assert [f.row for f in built.features] == [0]
        assert built.features[0].column == "geom"
        assert built.features[0].label == "1 · ada"
        assert built.notice == ""

    def test_cap_reports_showing_n_of_m(self):
        rows = [(i, _point(i / 10, 1).hex()) for i in range(50)]
        built = geo.build_features(["id", "geom"], rows, cap=10)
        assert len(built.features) == 10
        assert built.total == 50
        assert built.truncated
        assert built.notice == "Showing 10 of 50 features"

    def test_untransformable_rows_are_skipped_and_named(self):
        rows = [
            (1, _point(1, 2, srid=4326).hex()),
            (2, _point(651409.0, 313177.0, srid=27700).hex()),
        ]
        built = geo.build_features(["id", "geom"], rows)
        assert len(built.features) == 1
        assert built.untransformable == ("SRID 27700",)
        assert "skipped SRID 27700" in built.notice

    def test_bounds_span_every_feature(self):
        rows = [(1, _point(-5, 10).hex()), (2, _point(7, -3).hex())]
        built = geo.build_features(["id", "geom"], rows)
        assert built.bounds == pytest.approx((-5, -3, 7, 10))

    def test_no_geometry_column_is_an_empty_set_not_an_error(self):
        built = geo.build_features(["id", "name"], [(1, "ada")])
        assert built.features == ()
        assert built.bounds is None


class TestCapabilityGate:
    def test_default_provider_offers_no_map(self):
        assert not Capabilities().geometry
        assert MetadataProvider(None).spatial_extension() == ""

    def test_postgres_declares_the_capability(self):
        assert PostgresMetadata.CAPABILITIES.geometry

    def test_postgres_answers_from_the_extension_catalog(self):
        class Catalog:
            def __init__(self, extensions):
                self._extensions = extensions

            def list_catalog(self, slug, schema=""):
                assert slug == "extensions"
                return self._extensions

        class Extension:
            def __init__(self, name, detail):
                self.name, self.detail = name, detail

        with_postgis = PostgresMetadata(
            Catalog([Extension("plpgsql", "1.0 in pg_catalog"),
                     Extension("postgis", "3.4.2 in public")])
        )
        assert with_postgis.spatial_extension() == "postgis 3.4.2"
        without = PostgresMetadata(Catalog([Extension("plpgsql", "1.0 in x")]))
        assert without.spatial_extension() == ""

    def test_a_server_that_refuses_the_catalog_has_no_map(self):
        class Broken:
            def list_catalog(self, slug, schema=""):
                raise RuntimeError("permission denied")

        assert PostgresMetadata(Broken()).spatial_extension() == ""


class TestTileSource:
    def test_defaults_are_openstreetmap_with_attribution(self):
        source = tiles.TileSource()
        assert source.validate() == ""
        assert "openstreetmap" in source.url_template
        assert source.attribution

    def test_a_source_without_attribution_is_refused(self):
        source = tiles.TileSource(attribution="  ")
        assert "attribution" in source.validate()

    def test_a_url_missing_placeholders_is_refused(self):
        assert "{y}" in tiles.TileSource(
            url_template="https://example.test/{z}/{x}.png"
        ).validate()
        assert tiles.TileSource(url_template="ftp://x/{z}/{x}/{y}").validate()

    def test_url_substitutes_the_tile(self):
        source = tiles.TileSource(url_template="https://t.test/{z}/{x}/{y}.png")
        assert source.url(4, 8, 5) == "https://t.test/4/8/5.png"

    def test_sources_get_distinct_cache_keys(self):
        a = tiles.TileSource(url_template="https://a.test/{z}/{x}/{y}.png")
        b = tiles.TileSource(url_template="https://b.test/{z}/{x}/{y}.png")
        assert a.key != b.key

    def test_settings_build_the_source(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            settings_module.store,
            "settings",
            settings_module.Settings(
                map_tile_url="https://mine.test/{z}/{x}/{y}.png",
                map_attribution="Mine",
                map_tiles_enabled=False,
                map_max_features=7,
            ),
        )
        source = settings_module.tile_source()
        assert source.url_template == "https://mine.test/{z}/{x}/{y}.png"
        assert source.attribution == "Mine"
        assert not source.enabled
        assert settings_module.max_map_features() == 7


class TestProjection:
    def test_pixels_round_trip(self):
        lon, lat = 2.3522, 48.8566
        x, y = tiles.lonlat_to_pixel(lon, lat, 12)
        back = tiles.pixel_to_lonlat(x, y, 12)
        assert back[0] == pytest.approx(lon, abs=1e-6)
        assert back[1] == pytest.approx(lat, abs=1e-6)

    def test_fit_bounds_zooms_in_on_a_small_extent(self):
        wide = tiles.fit_bounds((-170, -80, 170, 80), 800, 600)[2]
        narrow = tiles.fit_bounds((2.34, 48.85, 2.36, 48.87), 800, 600)[2]
        assert narrow > wide

    def test_a_single_point_gets_a_readable_zoom(self):
        lon, lat, zoom = tiles.fit_bounds((5.0, 5.0, 5.0, 5.0), 800, 600)
        assert (lon, lat) == (5.0, 5.0)
        assert 10 <= zoom <= tiles.MAX_ZOOM

    def test_visible_tiles_cover_the_viewport_and_wrap(self):
        found = tiles.visible_tiles((0.0, 0.0), 3, 800, 600)
        assert found
        span = 2 ** 3
        assert all(0 <= x < span and 0 <= y < span for _z, x, y, _sx, _sy in found)


class TestTileLoader:
    def _loader(self, tmp_path, **kwargs):
        source = kwargs.pop(
            "source", tiles.TileSource(url_template="https://t.test/{z}/{x}/{y}.png")
        )
        return tiles.TileLoader(source, directory=tmp_path, **kwargs)

    def test_a_fetched_tile_is_cached_on_disk_and_served_from_it(self, tmp_path):
        calls = []

        def transport(url):
            calls.append(url)
            return b"tile-bytes"

        loader = self._loader(tmp_path, transport=transport, online=lambda: True)
        assert loader.fetch(2, 1, 1) == b"tile-bytes"
        # Second time: no request at all, and a fresh loader still has it.
        assert loader.fetch(2, 1, 1) == b"tile-bytes"
        again = self._loader(tmp_path, transport=transport, online=lambda: True)
        assert again.cached(2, 1, 1) == b"tile-bytes"
        assert calls == ["https://t.test/2/1/1.png"]

    def test_offline_serves_the_cache_and_never_calls_the_transport(
        self, tmp_path
    ):
        def refuse(url):
            raise AssertionError("no request may be made while offline")

        online = self._loader(
            tmp_path, transport=lambda _url: b"tile", online=lambda: True
        )
        online.fetch(1, 0, 0)
        offline = self._loader(tmp_path, transport=refuse, online=lambda: False)
        assert offline.available()
        assert offline.fetch(1, 0, 0) == b"tile"
        with pytest.raises(tiles.TileError):
            offline.fetch(1, 1, 1)  # not cached: reported, not hung on

    def test_tiles_turned_off_makes_no_request(self, tmp_path):
        loader = self._loader(
            tmp_path,
            source=tiles.TileSource(enabled=False),
            transport=lambda _url: pytest.fail("tiles are off"),
            online=lambda: pytest.fail("must not even probe"),
        )
        assert "turned off" in loader.available()
        with pytest.raises(tiles.TileError):
            loader.fetch(1, 0, 0)

    def test_an_unreachable_server_is_reported_once(self, tmp_path):
        attempts = []

        def transport(url):
            attempts.append(url)
            raise OSError("connection refused")

        loader = self._loader(tmp_path, transport=transport, online=lambda: True)
        with pytest.raises(tiles.TileError):
            loader.fetch(1, 0, 0)
        # The failure sticks, so a pan does not re-try every tile.
        assert loader.available()
        with pytest.raises(tiles.TileError):
            loader.fetch(1, 0, 1)
        assert len(attempts) == 1
        loader.reset()
        assert loader.available() == ""

    def test_a_misconfigured_source_is_refused_before_any_request(self, tmp_path):
        loader = self._loader(
            tmp_path,
            source=tiles.TileSource(url_template="https://t.test/tiles.png"),
            transport=lambda _url: pytest.fail("the URL is unusable"),
            online=lambda: True,
        )
        assert loader.available()
