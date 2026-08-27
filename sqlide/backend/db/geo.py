"""Geometry values, read without PostGIS on this side of the wire.

A `geometry`/`geography` column arrives in a grid as EWKB — either a
hex string (`0101000020E6100000…`, which is what psycopg hands back for
a bare `SELECT geom`) or the same bytes. As text that is unreadable,
and as a cell it is worse than useless: it is wide, it is all the same
prefix, and no two rows can be told apart by looking.

So this module parses it. Nothing here imports a driver, GDAL or
shapely: WKB is a small, fully specified format, and the app only needs
enough of it to say what a value *is* (`summary`) and to draw it
(`Geometry.shapes`). It reads:

* plain WKB and PostGIS EWKB, either byte order;
* the SRID flag (0x20000000) and Z/M flags (0x80000000/0x40000000), and
  the ISO 1000/2000/3000 type offsets that mean the same thing;
* Point, LineString, Polygon, their Multi- forms and GeometryCollection,
  nested to any depth.

Coordinates are longitude/latitude, in that order, as PostGIS stores
them for SRID 4326. `to_wgs84` transforms what it can — 4326 is already
there, 3857/900913 is plain inverse Mercator — and *reports* anything
else as untransformable rather than drawing it in the wrong place: a
map that silently puts a British National Grid point in the Atlantic is
worse than a map that says it cannot place it (PG-04).
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field

#: WKB geometry type codes, ISO offsets stripped.
_TYPE_NAMES = {
    1: "Point",
    2: "LineString",
    3: "Polygon",
    4: "MultiPoint",
    5: "MultiLineString",
    6: "MultiPolygon",
    7: "GeometryCollection",
}

_EWKB_SRID = 0x20000000
_EWKB_Z = 0x80000000
_EWKB_M = 0x40000000

#: SRIDs this module can put on a WGS84 map by itself. 0 means "no
#: SRID declared", which PostGIS leaves to the caller to interpret;
#: taking it as lon/lat is the only reading that draws anything.
WGS84 = 4326
_MERCATOR = (3857, 900913, 3785, 102100)
TRANSFORMABLE_SRIDS = (0, WGS84, *_MERCATOR)

#: Half the circumference of the Mercator sphere, in metres.
_MERCATOR_SPAN = 20037508.342789244


class GeometryError(ValueError):
    """A value that claimed to be a geometry and could not be read."""


@dataclass(frozen=True)
class Shape:
    """One drawable piece of a geometry.

    `kind` is "point", "line" or "polygon"; `coords` is its outline and
    `holes` the interior rings of a polygon (empty otherwise). A
    Multi-anything and a GeometryCollection flatten into several
    shapes, because drawing them is the same work either way.
    """

    kind: str
    coords: tuple[tuple[float, float], ...]
    holes: tuple[tuple[tuple[float, float], ...], ...] = ()

    @property
    def points(self) -> int:
        return len(self.coords) + sum(len(h) for h in self.holes)


@dataclass(frozen=True)
class Geometry:
    """A parsed geometry: what it is, where it is, and how to draw it."""

    kind: str
    srid: int = 0
    shapes: tuple[Shape, ...] = field(default_factory=tuple)
    #: Dimensions beyond x/y that were present and dropped. Kept so the
    #: summary can say "3D" rather than pretending the value was flat.
    has_z: bool = False
    has_m: bool = False

    @property
    def point_count(self) -> int:
        return sum(shape.points for shape in self.shapes)

    @property
    def bounds(self) -> tuple[float, float, float, float] | None:
        """(min x, min y, max x, max y), or None for an empty geometry."""
        xs: list[float] = []
        ys: list[float] = []
        for shape in self.shapes:
            for ring in (shape.coords, *shape.holes):
                for x, y in ring:
                    xs.append(x)
                    ys.append(y)
        if not xs:
            return None
        return (min(xs), min(ys), max(xs), max(ys))

    @property
    def transformable(self) -> bool:
        """Whether this module can put the value on a WGS84 map."""
        return self.srid in TRANSFORMABLE_SRIDS

    def summary(self) -> str:
        """The one line a grid cell shows in place of the hex.

        Type, SRID and point count — the three things that tell two
        geometry rows apart at a glance.
        """
        parts = [self.kind]
        if self.has_z or self.has_m:
            parts[0] += " " + ("ZM" if self.has_z and self.has_m else
                               "Z" if self.has_z else "M")
        if self.srid:
            parts.append(f"SRID {self.srid}")
        count = self.point_count
        parts.append(f"{count} point{'' if count == 1 else 's'}")
        return ", ".join(parts)

    def to_wgs84(self) -> Geometry:
        """The same geometry in lon/lat, for the map.

        Raises `GeometryError` for an SRID this module cannot transform
        — the caller shows that as a row it could not place, never as a
        point somewhere it is not.
        """
        if self.srid in (0, WGS84):
            return self
        if self.srid in _MERCATOR:
            return Geometry(
                kind=self.kind,
                srid=WGS84,
                shapes=tuple(
                    Shape(
                        shape.kind,
                        tuple(_unmercator(p) for p in shape.coords),
                        tuple(
                            tuple(_unmercator(p) for p in hole)
                            for hole in shape.holes
                        ),
                    )
                    for shape in self.shapes
                ),
                has_z=self.has_z,
                has_m=self.has_m,
            )
        raise GeometryError(
            f"SRID {self.srid} cannot be transformed to WGS84 here; "
            "select ST_Transform(geom, 4326) to map it"
        )


def _unmercator(point: tuple[float, float]) -> tuple[float, float]:
    """Web Mercator metres back to lon/lat."""
    x, y = point
    lon = x * 180.0 / _MERCATOR_SPAN
    lat = math.degrees(2 * math.atan(math.exp(math.radians(
        y * 180.0 / _MERCATOR_SPAN
    ))) - math.pi / 2)
    return (lon, lat)


_HEX_DIGITS = set("0123456789abcdefABCDEF")


def looks_like_geometry(value) -> bool:
    """Whether a cell value could be WKB, cheaply enough to ask per cell.

    A hex EWKB string is even-length hex, at least a header and a
    point, and starts with a byte-order marker of 00 or 01. Bytes are
    checked the same way. Anything else — a number, a name, a JSON blob
    — is rejected without parsing.
    """
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        return len(raw) >= 9 and raw[0] in (0, 1)
    if isinstance(value, str):
        text = value.strip()
        if len(text) < 18 or len(text) % 2 or text[:2] not in ("00", "01"):
            return False
        return all(c in _HEX_DIGITS for c in text)
    return False


def parse(value) -> Geometry:
    """A cell value as a `Geometry`, from hex EWKB or raw WKB bytes."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
    elif isinstance(value, str):
        try:
            raw = bytes.fromhex(value.strip())
        except ValueError as exc:
            raise GeometryError(f"not hex-encoded WKB: {exc}") from exc
    else:
        raise GeometryError(f"not a geometry value: {type(value).__name__}")
    reader = _Reader(raw)
    geometry = reader.geometry()
    return geometry


def summarize(value) -> str:
    """`parse(value).summary()`, with unreadable values named as such.

    Grid cells call this: a column that turned out not to be a geometry
    after all must not take the grid down with it.
    """
    try:
        return parse(value).summary()
    except GeometryError:
        return ""


class _Reader:
    """A cursor over WKB bytes. One instance per top-level value; the
    byte order is read per geometry, because a collection is allowed to
    mix them."""

    def __init__(self, raw: bytes) -> None:
        self._raw = raw
        self._at = 0

    def _take(self, count: int) -> bytes:
        end = self._at + count
        if end > len(self._raw):
            raise GeometryError("truncated WKB")
        chunk = self._raw[self._at:end]
        self._at = end
        return chunk

    def geometry(self) -> Geometry:
        order = self._take(1)[0]
        if order not in (0, 1):
            raise GeometryError(f"bad byte order marker: {order:#x}")
        endian = ">" if order == 0 else "<"
        (code,) = struct.unpack(endian + "I", self._take(4))
        has_z = bool(code & _EWKB_Z)
        has_m = bool(code & _EWKB_M)
        srid = 0
        if code & _EWKB_SRID:
            (srid,) = struct.unpack(endian + "i", self._take(4))
        base = code & 0xFFFF
        # ISO WKB says the same thing with 1000/2000/3000 offsets.
        if base >= 3000:
            has_z = has_m = True
            base -= 3000
        elif base >= 2000:
            has_m = True
            base -= 2000
        elif base >= 1000:
            has_z = True
            base -= 1000
        name = _TYPE_NAMES.get(base)
        if name is None:
            raise GeometryError(f"unknown geometry type {base}")
        dims = 2 + has_z + has_m
        shapes = self._shapes(endian, base, dims)
        return Geometry(
            kind=name, srid=srid, shapes=shapes, has_z=has_z, has_m=has_m
        )

    def _shapes(self, endian: str, base: int, dims: int) -> tuple[Shape, ...]:
        if base == 1:
            point = self._point(endian, dims)
            # An empty point is stored as NaN coordinates.
            if any(math.isnan(c) for c in point):
                return ()
            return (Shape("point", (point,)),)
        if base == 2:
            return (Shape("line", self._ring(endian, dims)),)
        if base == 3:
            return (self._polygon(endian, dims),)
        if base in (4, 5, 6, 7):
            (count,) = struct.unpack(endian + "I", self._take(4))
            shapes: list[Shape] = []
            for _ in range(count):
                # Every member carries its own header, collection or not.
                shapes.extend(self.geometry().shapes)
            return tuple(shapes)
        raise GeometryError(f"unknown geometry type {base}")

    def _point(self, endian: str, dims: int) -> tuple[float, float]:
        values = struct.unpack(endian + "d" * dims, self._take(8 * dims))
        return (values[0], values[1])

    def _ring(self, endian: str, dims: int) -> tuple[tuple[float, float], ...]:
        (count,) = struct.unpack(endian + "I", self._take(4))
        return tuple(self._point(endian, dims) for _ in range(count))

    def _polygon(self, endian: str, dims: int) -> Shape:
        (count,) = struct.unpack(endian + "I", self._take(4))
        rings = [self._ring(endian, dims) for _ in range(count)]
        if not rings:
            return Shape("polygon", ())
        return Shape("polygon", rings[0], tuple(rings[1:]))


@dataclass(frozen=True)
class Feature:
    """One row's geometry, ready to draw and linked back to its row."""

    row: int  # index into the result's rows
    column: str  # the column the geometry came from
    geometry: Geometry  # already in WGS84
    label: str = ""


@dataclass(frozen=True)
class FeatureSet:
    """What a map is asked to draw, and what it had to leave out.

    `total` counts every row that held a geometry, `features` only the
    ones inside the cap — the "showing N of M" notice reads both — and
    `untransformable` names the SRIDs that were skipped, so a result in
    a projection this module cannot handle says so instead of coming
    back mysteriously empty.
    """

    features: tuple[Feature, ...] = ()
    total: int = 0
    untransformable: tuple[str, ...] = ()
    unreadable: int = 0

    @property
    def truncated(self) -> bool:
        return self.total > len(self.features)

    @property
    def notice(self) -> str:
        """The line the map shows above the tiles, "" when everything
        made it on screen."""
        parts = []
        if self.truncated:
            parts.append(f"Showing {len(self.features)} of {self.total} features")
        if self.untransformable:
            parts.append(
                "skipped " + ", ".join(sorted(set(self.untransformable)))
            )
        if self.unreadable:
            parts.append(f"{self.unreadable} unreadable value(s)")
        return "; ".join(parts)

    @property
    def bounds(self) -> tuple[float, float, float, float] | None:
        boxes = [
            box for box in (f.geometry.bounds for f in self.features) if box
        ]
        if not boxes:
            return None
        return (
            min(b[0] for b in boxes),
            min(b[1] for b in boxes),
            max(b[2] for b in boxes),
            max(b[3] for b in boxes),
        )


#: How many features a map draws before it stops and says so. A result
#: of a hundred thousand polygons is not a map, it is a hang.
DEFAULT_FEATURE_CAP = 2000


def geometry_columns(columns, rows, sample: int = 25) -> list[str]:
    """The columns of a result that hold geometries.

    Detection is by value, not by declared type: a `SELECT geom` comes
    back as a hex string with no type information attached, and an
    expression (`ST_Buffer(...)`) has no column type at all. A column
    counts when every non-NULL value in the sample parses.
    """
    found: list[str] = []
    for index, name in enumerate(columns):
        seen = 0
        for row in rows[:sample]:
            value = row[index] if index < len(row) else None
            if value is None:
                continue
            if not looks_like_geometry(value):
                seen = 0
                break
            try:
                parse(value)
            except GeometryError:
                seen = 0
                break
            seen += 1
        if seen:
            found.append(name)
    return found


def build_features(
    columns,
    rows,
    column: str = "",
    cap: int = DEFAULT_FEATURE_CAP,
    label_columns: int = 2,
) -> FeatureSet:
    """A result set as features to draw.

    `column` picks which geometry column to map; empty takes the first
    one found. Rows past `cap` are counted but not built, so the notice
    can say "showing N of M" without holding M geometries in memory.
    """
    names = list(columns)
    geo_names = geometry_columns(names, rows)
    if column and column in geo_names:
        target = column
    elif geo_names:
        target = geo_names[0]
    else:
        return FeatureSet()
    index = names.index(target)
    label_indexes = [
        i for i, name in enumerate(names) if name not in geo_names
    ][:label_columns]

    features: list[Feature] = []
    total = 0
    untransformable: list[str] = []
    unreadable = 0
    for position, row in enumerate(rows):
        value = row[index] if index < len(row) else None
        if value is None:
            continue
        try:
            geometry = parse(value)
        except GeometryError:
            unreadable += 1
            continue
        if not geometry.transformable:
            untransformable.append(f"SRID {geometry.srid}")
            continue
        total += 1
        if len(features) >= cap:
            continue
        label = " · ".join(
            str(row[i]) for i in label_indexes
            if i < len(row) and row[i] is not None
        )
        features.append(
            Feature(
                row=position,
                column=target,
                geometry=geometry.to_wgs84(),
                label=label,
            )
        )
    return FeatureSet(
        features=tuple(features),
        total=total,
        untransformable=tuple(untransformable),
        unreadable=unreadable,
    )
