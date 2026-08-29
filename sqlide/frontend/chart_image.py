"""The chart as a picture: PNG and SVG bytes, and a file on disk
(CORE-34).

A chart that cannot leave the app is half a feature — the reason
anyone charts a query is to put the picture in a ticket, a slide or a
message. This is the whole of that exit, and it is small because
CORE-31 made drawing a pure function of (spec, data, context, size):
the same `chart_canvas.render()` call that fills a `Gtk.DrawingArea`
fills a `cairo.ImageSurface` and a `cairo.SVGSurface`. There is no
second rendering path, which was one of the reasons cairo won over a
charting library (`docs/charting-research.md`).

Three decisions live here rather than in the dialog, so that a test
and a script get them too:

- **Light by default.** A dark-background PNG pasted into a white
  document is the usual complaint, so `dark` defaults to `False` and
  the surface is painted with the palette's own background rather
  than left transparent.
- **A floor under the size.** The exported picture carries its legend
  and axis labels whatever the on-screen canvas had room for, so a
  request smaller than `MIN_WIDTH`x`MIN_HEIGHT` is raised to it.
- **All of the file or none of it**, exactly as `backend/export.py`
  writes rows: the bytes go to a `.part` beside the destination and
  are renamed over it only once they are complete, and a failure
  reports the path and the reason instead of raising an errno.

No GTK: this module imports cairo and the renderer and nothing else,
so `tests/test_chart_export.py` runs it with no display.
"""

from __future__ import annotations

import io
import os
from enum import Enum
from pathlib import Path

import cairo

from sqlide.backend import charts
from sqlide.frontend import chart_canvas
from sqlide.frontend.canvas import palette, rgb

__all__ = [
    "ChartExportError",
    "Format",
    "MAX_SIZE",
    "MIN_HEIGHT",
    "MIN_WIDTH",
    "chart_bytes",
    "png_bytes",
    "svg_bytes",
    "write_chart",
]

#: Below this the axis labels and the legend stop being legible, and a
#: picture that drops them is not the chart that was on screen.
MIN_WIDTH = 240
MIN_HEIGHT = 160
#: A guard against a typo in the size fields turning into a gigabyte of
#: pixels; cairo would refuse anyway, less politely.
MAX_SIZE = 10000
#: PNG only: 2x is the slide-friendly image the ticket asks for.
MIN_SCALE = 0.25
MAX_SCALE = 8.0


class Format(Enum):
    """A picture format, with the file extension it belongs in."""

    PNG = "png"
    SVG = "svg"

    @property
    def suffix(self) -> str:
        return ".png" if self is Format.PNG else ".svg"

    @property
    def mime(self) -> str:
        return "image/png" if self is Format.PNG else "image/svg+xml"


class ChartExportError(Exception):
    """A picture that could not be written, in words."""


def clamp_size(width: float, height: float) -> tuple[int, int]:
    """The size actually drawn: the request, floored so the legend and
    the axis labels fit and capped so a typo cannot ask for a gigabyte
    of pixels."""
    w = min(max(int(round(width or 0)), MIN_WIDTH), MAX_SIZE)
    h = min(max(int(round(height or 0)), MIN_HEIGHT), MAX_SIZE)
    return w, h


def clamp_scale(scale: float) -> float:
    try:
        value = float(scale)
    except (TypeError, ValueError):
        return 1.0
    if value != value:  # NaN
        return 1.0
    return min(max(value, MIN_SCALE), MAX_SCALE)


def _paint(cr, width: float, height: float, dark: bool) -> None:
    """The background the on-screen canvas gets from the window.

    A transparent PNG reads as a broken one in most of the places a
    chart is pasted, so the surface is filled with the palette's own
    row background before anything is drawn on it.
    """
    cr.save()
    cr.set_source_rgb(*rgb(palette(dark).row_bg))
    cr.rectangle(0, 0, width, height)
    cr.fill()
    cr.restore()


def _draw(
    cr,
    spec: charts.ChartSpec,
    data: charts.ChartData,
    width: float,
    height: float,
    dark: bool,
) -> None:
    _paint(cr, width, height, dark)
    chart_canvas.render(cr, spec, data, width, height, dark=dark)


def png_bytes(
    spec: charts.ChartSpec,
    data: charts.ChartData,
    width: float,
    height: float,
    *,
    scale: float = 1.0,
    dark: bool = False,
) -> bytes:
    """The chart as a PNG, `scale`x the requested pixel size.

    The chart is drawn at its logical size and the context scaled, so
    a 2x image is the same picture with more pixels rather than the
    same pixels with bigger text.
    """
    logical_width, logical_height = clamp_size(width, height)
    factor = clamp_scale(scale)
    pixels_w = max(int(round(logical_width * factor)), 1)
    pixels_h = max(int(round(logical_height * factor)), 1)
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, pixels_w, pixels_h)
    cr = cairo.Context(surface)
    cr.scale(pixels_w / logical_width, pixels_h / logical_height)
    _draw(cr, spec, data, logical_width, logical_height, dark)
    surface.flush()
    buffer = io.BytesIO()
    surface.write_to_png(buffer)
    surface.finish()
    return buffer.getvalue()


def svg_bytes(
    spec: charts.ChartSpec,
    data: charts.ChartData,
    width: float,
    height: float,
    *,
    dark: bool = False,
) -> bytes:
    """The chart as an SVG — vector marks and vector text.

    Nothing is rasterised on the way: cairo's SVG surface emits the
    axes as paths and the labels as glyph outlines, so the file scales
    to a poster rather than being a bitmap in an XML wrapper.
    """
    logical_width, logical_height = clamp_size(width, height)
    buffer = io.BytesIO()
    surface = cairo.SVGSurface(buffer, logical_width, logical_height)
    cr = cairo.Context(surface)
    _draw(cr, spec, data, logical_width, logical_height, dark)
    surface.finish()
    return buffer.getvalue()


def chart_bytes(
    fmt: Format,
    spec: charts.ChartSpec,
    data: charts.ChartData,
    width: float,
    height: float,
    *,
    scale: float = 1.0,
    dark: bool = False,
) -> bytes:
    if fmt is Format.SVG:
        return svg_bytes(spec, data, width, height, dark=dark)
    return png_bytes(spec, data, width, height, scale=scale, dark=dark)


def write_chart(
    path: str | os.PathLike[str],
    fmt: Format,
    spec: charts.ChartSpec,
    data: charts.ChartData,
    width: float,
    height: float,
    *,
    scale: float = 1.0,
    dark: bool = False,
) -> Path:
    """Write the picture to `path`, all of it or none of it.

    The bytes are rendered first and land in a temporary file beside
    the destination, renamed over it only when they are complete: a
    full disk or an unwritable directory leaves the destination as it
    was rather than as a half a PNG that looks like an export.
    """
    target = Path(path)
    try:
        payload = chart_bytes(
            fmt, spec, data, width, height, scale=scale, dark=dark
        )
    except MemoryError as exc:  # a size cairo could not allocate
        raise ChartExportError(
            f"Could not draw {target}: the image is too large."
        ) from exc
    except cairo.Error as exc:
        raise ChartExportError(f"Could not draw {target}: {exc}") from exc

    temp = target.with_name(target.name + ".part")
    try:
        with open(temp, "wb") as handle:
            handle.write(payload)
        os.replace(temp, target)
    except OSError as exc:
        _discard(temp)
        raise ChartExportError(_reason(target, exc)) from exc
    except Exception:
        _discard(temp)
        raise
    return target


def _discard(temp: Path) -> None:
    try:
        temp.unlink()
    except OSError:
        pass


def _reason(target: Path, exc: OSError) -> str:
    detail = exc.strerror or str(exc)
    return f"Could not write {target}: {detail}"
