"""The geo viewer: result geometries drawn over OpenStreetMap tiles.

A `geometry` column in a grid is a wall of hex (PG-04). This widget is
the other half of the answer: give it a result set and it parses the
WKB (backend/db/geo.py), transforms what it can to WGS84, and draws
points, lines, polygons and their Multi- forms on a slippy map.

How it behaves is mostly about what it refuses to do:

* **It never blocks on the network.** Tiles are drawn from the cache
  synchronously; a missing one is fetched on a worker thread and the
  canvas redraws when it lands (backend/tiles.py).
* **It works offline.** With no network — or with tiles turned off in
  Preferences — the geometries are drawn on a plain graticule
  background and the notice bar says why there are no tiles. Nothing
  hangs and nothing is retried in a loop.
* **It attributes.** The tile source's credit line is drawn over the
  bottom-right corner for as long as tiles are on screen.
* **It draws a bounded number of features.** Past the cap
  (`settings.map_max_features`) the notice reads "Showing N of M".
* **It says what it could not place.** A geometry in an SRID the app
  cannot transform is counted and named, never drawn in the wrong
  ocean.

Selection is two-way: clicking a feature reports its row through
`on_select`, and `select_row` highlights (and pans to) the feature of a
row selected in the grid.
"""

from __future__ import annotations

import math
from collections.abc import Callable

from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, Gtk

from sqlide.backend import settings as settings_module
from sqlide.backend import tiles
from sqlide.backend.db import geo
from sqlide.frontend.util import describe, run_async

#: Feature colours, drawn over tiles so they have to hold up on both a
#: light and a dark basemap.
_FEATURE_RGBA = (0.11, 0.44, 0.85, 0.85)
_FEATURE_FILL = (0.11, 0.44, 0.85, 0.22)
_SELECTED_RGBA = (0.90, 0.30, 0.10, 0.95)
_SELECTED_FILL = (0.90, 0.30, 0.10, 0.30)
_POINT_RADIUS = 5.0
#: How close a click has to land to count as hitting a feature.
_HIT_SLOP = 8.0
#: Tiles requested at once. A pan asks for a screenful; the policy
#: (and courtesy) is not to open a socket per tile all at once.
_MAX_IN_FLIGHT = 4


class MapView(Gtk.Box):
    """A pannable, zoomable map of one result set's geometries."""

    def __init__(
        self,
        on_select: Callable[[int], None] | None = None,
        loader: tiles.TileLoader | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._on_select = on_select
        self._features: geo.FeatureSet = geo.FeatureSet()
        self._selected: int | None = None  # a result row index
        self._center = (0.0, 20.0)
        self._zoom = 2
        self._pan_origin: tuple[float, float] | None = None
        self._in_flight: set[tuple[int, int, int]] = set()
        self._failed: set[tuple[int, int, int]] = set()
        self._pixbufs: dict[tuple[int, int, int], GdkPixbuf.Pixbuf] = {}
        self._loader = loader or make_loader()

        self._notice = Gtk.Label(xalign=0, wrap=True)
        self._notice.add_css_class("dim-label")
        self._notice.set_margin_start(12)
        self._notice.set_margin_end(12)
        self._notice.set_margin_top(4)
        self._notice.set_margin_bottom(4)
        self._notice.set_visible(False)
        self.append(self._notice)

        self._canvas = Gtk.DrawingArea(vexpand=True, hexpand=True)
        self._canvas.set_draw_func(self._draw)
        drag = Gtk.GestureDrag(button=Gdk.BUTTON_PRIMARY)
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self._canvas.add_controller(drag)
        click = Gtk.GestureClick(button=Gdk.BUTTON_PRIMARY)
        click.connect("released", self._on_click)
        self._canvas.add_controller(click)
        scroll = Gtk.EventControllerScroll(
            flags=Gtk.EventControllerScrollFlags.VERTICAL
        )
        scroll.connect("scroll", self._on_scroll)
        self._canvas.add_controller(scroll)

        self._attribution = Gtk.Label(
            label=self._loader.source.attribution,
            halign=Gtk.Align.END,
            valign=Gtk.Align.END,
        )
        self._attribution.add_css_class("osd")
        self._attribution.add_css_class("caption")
        self._attribution.set_margin_end(6)
        self._attribution.set_margin_bottom(6)

        overlay = Gtk.Overlay(vexpand=True)
        overlay.set_child(self._canvas)
        overlay.add_overlay(self._attribution)
        overlay.add_overlay(self._zoom_controls())

        self._empty = Adw.StatusPage(
            icon_name="mark-location-symbolic",
            title="Nothing to Map",
            description="No geometry values in this result.",
            vexpand=True,
        )
        self._stack = Gtk.Stack(vexpand=True)
        self._stack.add_named(overlay, "map")
        self._stack.add_named(self._empty, "empty")
        self.append(self._stack)
        self._stack.set_visible_child_name("empty")

    # Controls

    def _zoom_controls(self) -> Gtk.Widget:
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            halign=Gtk.Align.END,
            valign=Gtk.Align.START,
            margin_top=6,
            margin_end=6,
        )
        box.add_css_class("linked")
        zoom_in = Gtk.Button(icon_name="zoom-in-symbolic")
        describe(zoom_in, "Zoom in")
        zoom_in.connect("clicked", lambda *_: self._step_zoom(1))
        zoom_out = Gtk.Button(icon_name="zoom-out-symbolic")
        describe(zoom_out, "Zoom out")
        zoom_out.connect("clicked", lambda *_: self._step_zoom(-1))
        fit = Gtk.Button(icon_name="zoom-fit-best-symbolic")
        describe(fit, "Fit every feature")
        fit.connect("clicked", lambda *_: self.fit_to_features())
        for button in (zoom_in, zoom_out, fit):
            button.add_css_class("osd")
            box.append(button)
        return box

    # Data

    def set_features(self, features: geo.FeatureSet) -> None:
        """Draw a new result. Resets the view to fit what it holds."""
        self._features = features
        self._selected = None
        self._failed.clear()
        self._stack.set_visible_child_name(
            "map" if features.features else "empty"
        )
        self._refresh_notice()
        self.fit_to_features()

    def clear(self) -> None:
        self.set_features(geo.FeatureSet())

    def select_row(self, row: int | None) -> None:
        """Highlight the feature of a result row (the grid's side of
        the two-way selection). A row with no geometry clears it."""
        match = next(
            (f for f in self._features.features if f.row == row), None
        )
        self._selected = None if match is None else row
        if match is not None:
            box = match.geometry.bounds
            if box and not self._visible(box):
                self._center = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
        self._canvas.queue_draw()

    @property
    def selected_row(self) -> int | None:
        return self._selected

    def _viewport(self) -> tuple[int, int]:
        """The canvas size, never zero: the widget is asked to fit
        features before it has ever been allocated (a tab is built
        before it is shown), and a zero-sized viewport would fit
        everything at zoom 0."""
        return (
            max(self._canvas.get_width(), 640),
            max(self._canvas.get_height(), 480),
        )

    def fit_to_features(self) -> None:
        bounds = self._features.bounds
        if bounds is None:
            self._canvas.queue_draw()
            return
        width, height = self._viewport()
        lon, lat, zoom = tiles.fit_bounds(
            bounds, width, height, self._loader.source
        )
        self._center = (lon, lat)
        self._zoom = zoom
        self._canvas.queue_draw()

    def _refresh_notice(self) -> None:
        parts = [self._features.notice]
        problem = self._loader.available()
        if problem:
            parts.append(problem)
        text = " · ".join(p for p in parts if p)
        self._notice.set_text(text)
        self._notice.set_visible(bool(text))
        self._attribution.set_visible(not problem)

    # Painting

    def _draw(self, _area, cr, width, height) -> None:
        dark = Adw.StyleManager.get_default().get_dark()
        shade = 0.16 if dark else 0.93
        cr.set_source_rgb(shade, shade, shade + 0.02)
        cr.paint()
        problem = self._loader.available()
        wanted = tiles.visible_tiles(self._center, self._zoom, width, height)
        if not problem or any(
            self._loader.cached(z, x, y) for z, x, y, _sx, _sy in wanted
        ):
            self._draw_tiles(cr, wanted)
        else:
            self._draw_graticule(cr, width, height)
        self._draw_features(cr, width, height)

    def _draw_tiles(self, cr, wanted) -> None:
        for z, x, y, sx, sy in wanted:
            pixbuf = self._tile(z, x, y)
            if pixbuf is None:
                continue
            cr.save()
            Gdk.cairo_set_source_pixbuf(cr, pixbuf, sx, sy)
            cr.paint()
            cr.restore()

    def _draw_graticule(self, cr, width, height) -> None:
        """The no-tiles background: a lon/lat grid, so an offline map
        still says where things are relative to each other."""
        cr.set_line_width(1)
        cr.set_source_rgba(0.5, 0.5, 0.5, 0.35)
        step = tiles.TILE_SIZE
        cx, cy = tiles.lonlat_to_pixel(*self._center, self._zoom)
        left, top = cx - width / 2, cy - height / 2
        offset_x = -(left % step)
        offset_y = -(top % step)
        x = offset_x
        while x < width:
            cr.move_to(x, 0)
            cr.line_to(x, height)
            x += step
        y = offset_y
        while y < height:
            cr.move_to(0, y)
            cr.line_to(width, y)
            y += step
        cr.stroke()

    def _tile(self, z: int, x: int, y: int):
        key = (z, x, y)
        if key in self._pixbufs:
            return self._pixbufs[key]
        data = self._loader.cached(z, x, y)
        if data is not None:
            pixbuf = _pixbuf(data)
            if pixbuf is not None:
                self._pixbufs[key] = pixbuf
            return pixbuf
        self._request(key)
        return None

    def _request(self, key) -> None:
        """Queue one tile fetch on a worker thread."""
        if (
            key in self._in_flight
            or key in self._failed
            or len(self._in_flight) >= _MAX_IN_FLIGHT
            or self._loader.available()
        ):
            return
        self._in_flight.add(key)
        z, x, y = key

        def work():
            return self._loader.fetch(z, x, y)

        def done(data) -> None:
            self._in_flight.discard(key)
            pixbuf = _pixbuf(data)
            if pixbuf is None:
                self._failed.add(key)
            else:
                self._pixbufs[key] = pixbuf
            self._canvas.queue_draw()

        def failed(_exc) -> None:
            self._in_flight.discard(key)
            self._failed.add(key)
            self._refresh_notice()
            self._canvas.queue_draw()

        run_async(work, done, failed)

    def _draw_features(self, cr, width, height) -> None:
        cr.set_line_join(1)  # cairo.LINE_JOIN_ROUND, without the import
        for feature in self._features.features:
            selected = feature.row == self._selected
            stroke = _SELECTED_RGBA if selected else _FEATURE_RGBA
            fill = _SELECTED_FILL if selected else _FEATURE_FILL
            cr.set_line_width(3.0 if selected else 2.0)
            for shape in feature.geometry.shapes:
                points = [self._to_screen(p, width, height) for p in shape.coords]
                if not points:
                    continue
                if shape.kind == "point":
                    cr.set_source_rgba(*fill)
                    cr.arc(
                        points[0][0], points[0][1],
                        _POINT_RADIUS + (2 if selected else 0),
                        0, 2 * math.pi,
                    )
                    cr.fill_preserve()
                    cr.set_source_rgba(*stroke)
                    cr.stroke()
                    continue
                cr.move_to(*points[0])
                for point in points[1:]:
                    cr.line_to(*point)
                if shape.kind == "polygon":
                    cr.close_path()
                    cr.set_source_rgba(*fill)
                    cr.fill_preserve()
                cr.set_source_rgba(*stroke)
                cr.stroke()

    def _to_screen(self, lonlat, width: int, height: int) -> tuple[float, float]:
        cx, cy = tiles.lonlat_to_pixel(*self._center, self._zoom)
        x, y = tiles.lonlat_to_pixel(lonlat[0], lonlat[1], self._zoom)
        return (x - cx + width / 2, y - cy + height / 2)

    def _visible(self, bounds) -> bool:
        width, height = self._viewport()
        x0, y0 = self._to_screen((bounds[0], bounds[3]), width, height)
        x1, y1 = self._to_screen((bounds[2], bounds[1]), width, height)
        return 0 <= x0 and x1 <= width and 0 <= y0 and y1 <= height

    # Interaction

    def _step_zoom(self, delta: int) -> None:
        zoom = tiles.clamp_zoom(self._zoom + delta, self._loader.source)
        if zoom != self._zoom:
            self._zoom = zoom
            self._canvas.queue_draw()

    def _on_scroll(self, _controller, _dx, dy) -> bool:
        self._step_zoom(-1 if dy > 0 else 1)
        return True

    def _on_drag_begin(self, _gesture, _x, _y) -> None:
        self._pan_origin = tiles.lonlat_to_pixel(*self._center, self._zoom)

    def _on_drag_update(self, _gesture, dx, dy) -> None:
        if self._pan_origin is None:
            return
        x, y = self._pan_origin
        span = tiles.TILE_SIZE * 2 ** self._zoom
        self._center = tiles.pixel_to_lonlat(
            (x - dx) % span, max(0.0, min(span, y - dy)), self._zoom
        )
        self._canvas.queue_draw()

    def _on_drag_end(self, _gesture, _dx, _dy) -> None:
        self._pan_origin = None

    def _on_click(self, gesture, n_press, x, y) -> None:
        if n_press != 1:
            return
        sequence = gesture.get_current_sequence()
        if gesture.get_sequence_state(sequence) == Gtk.EventSequenceState.DENIED:
            return
        row = self.feature_at(x, y)
        if row is None:
            return
        self._selected = row
        self._canvas.queue_draw()
        if self._on_select is not None:
            self._on_select(row)

    def feature_at(self, x: float, y: float) -> int | None:
        """The row of the feature under a canvas point, or None.

        Points win over lines and lines over polygons at the same
        place: the smaller the target, the harder it is to hit, so it
        gets priority once it is hit.
        """
        width, height = self._viewport()
        best: tuple[int, int, float] | None = None  # (rank, row, distance)
        for feature in self._features.features:
            for shape in feature.geometry.shapes:
                points = [self._to_screen(p, width, height) for p in shape.coords]
                if not points:
                    continue
                if shape.kind == "point":
                    distance = math.dist(points[0], (x, y))
                    if distance <= _POINT_RADIUS + _HIT_SLOP:
                        candidate = (0, feature.row, distance)
                    else:
                        continue
                elif shape.kind == "line":
                    distance = _distance_to_path(points, (x, y))
                    if distance > _HIT_SLOP:
                        continue
                    candidate = (1, feature.row, distance)
                else:
                    if _in_polygon(points, (x, y)):
                        candidate = (2, feature.row, 0.0)
                    elif _distance_to_path(points + points[:1], (x, y)) <= _HIT_SLOP:
                        candidate = (2, feature.row, _HIT_SLOP)
                    else:
                        continue
                if best is None or candidate[:1] + candidate[2:] < (
                    best[0], best[2]
                ):
                    best = candidate
        return None if best is None else best[1]


def make_loader() -> tiles.TileLoader:
    """A tile loader wired to the current settings and, where GIO can
    tell us, to the desktop's own idea of whether there is a network.

    Gio.NetworkMonitor answers instantly and correctly for a machine
    with no route at all, which is the case that otherwise costs the
    map a connect timeout per tile; the socket probe in backend/tiles.py
    stays the fallback.
    """

    def online() -> bool:
        try:
            monitor = Gio.NetworkMonitor.get_default()
            if not monitor.get_network_available():
                return False
        except Exception:
            pass
        return tiles.probe_online()

    return tiles.TileLoader(settings_module.tile_source(), online=online)


def _pixbuf(data: bytes):
    """Decoded tile bytes, or None if they were not an image the
    platform can read (a tile server's HTML error page, say)."""
    try:
        loader = GdkPixbuf.PixbufLoader()
        loader.write(GLib.Bytes.new(data).get_data())
        loader.close()
        return loader.get_pixbuf()
    except Exception:
        return None


def _distance_to_path(points, target) -> float:
    return min(
        (
            _distance_to_segment(points[i], points[i + 1], target)
            for i in range(len(points) - 1)
        ),
        default=math.dist(points[0], target) if points else math.inf,
    )


def _distance_to_segment(a, b, p) -> float:
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.dist(a, p)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.dist((ax + t * dx, ay + t * dy), p)


def _in_polygon(points, target) -> bool:
    """Ray casting, on screen coordinates."""
    x, y = target
    inside = False
    count = len(points)
    for i in range(count):
        x0, y0 = points[i]
        x1, y1 = points[(i + 1) % count]
        if (y0 > y) != (y1 > y):
            cross = x0 + (y - y0) * (x1 - x0) / (y1 - y0)
            if cross > x:
                inside = not inside
    return inside
