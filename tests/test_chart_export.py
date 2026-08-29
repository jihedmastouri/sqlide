"""The chart as a picture: PNG and SVG, and the clipboard (CORE-34).

`frontend/chart_image.py` imports cairo and the renderer and no GTK, so
all of this runs with no display and no database — which is the point
of CORE-31 having made drawing a pure function of (spec, data, context,
size).

What is asserted is what the ticket asks for: that both formats come
out at the requested size carrying the legend and the axis labels, that
the SVG is vector text rather than a bitmap in an XML wrapper, that the
theme is honoured and defaults to light, and that a write which fails
names the path and the reason without leaving half a file behind.
"""

from __future__ import annotations

import struct

import pytest

cairo = pytest.importorskip("cairo")
chart_image = pytest.importorskip("sqlide.frontend.chart_image")

from sqlide.backend import charts  # noqa: E402

Format = chart_image.Format


SPEC = charts.ChartSpec(
    type="bar", x="city", series=("sales", "returns"), title="Sales by city"
)


def data() -> charts.ChartData:
    return charts.ChartData(
        series=(
            charts.Series("sales", (("Paris", 3.0), ("Lyon", 5.0)), "sales"),
            charts.Series("returns", (("Paris", 1.0), ("Lyon", 2.0)), "returns"),
        ),
        x_kind=charts.CATEGORICAL,
        x_labels=("Paris", "Lyon"),
        rows=2,
    )


def png_size(payload: bytes) -> tuple[int, int]:
    """Width and height out of the IHDR, so the test reads the file the
    way another program would rather than trusting the surface."""
    assert payload.startswith(b"\x89PNG\r\n\x1a\n")
    assert payload[12:16] == b"IHDR"
    return struct.unpack(">II", payload[16:24])


def pixel(payload: bytes, x: int, y: int) -> tuple[int, int, int]:
    surface = cairo.ImageSurface.create_from_png(__import__("io").BytesIO(payload))
    stride = surface.get_stride()
    buf = bytes(surface.get_data())
    offset = y * stride + x * 4
    blue, green, red, _alpha = buf[offset : offset + 4]
    return red, green, blue


# The file itself


def test_png_comes_out_at_the_size_that_was_asked_for() -> None:
    payload = chart_image.png_bytes(SPEC, data(), 800, 400)
    assert png_size(payload) == (800, 400)


def test_a_scaled_png_is_the_same_picture_with_more_pixels() -> None:
    payload = chart_image.png_bytes(SPEC, data(), 800, 400, scale=2)
    assert png_size(payload) == (1600, 800)


def test_the_size_is_floored_so_the_legend_and_the_axes_still_fit() -> None:
    payload = chart_image.png_bytes(SPEC, data(), 10, 10)
    assert png_size(payload) == (chart_image.MIN_WIDTH, chart_image.MIN_HEIGHT)


def test_an_absurd_size_is_capped_rather_than_allocated() -> None:
    assert chart_image.clamp_size(10**9, 10**9) == (
        chart_image.MAX_SIZE,
        chart_image.MAX_SIZE,
    )


def test_the_png_is_opaque_not_a_transparent_rectangle() -> None:
    payload = chart_image.png_bytes(SPEC, data(), 400, 300)
    surface = cairo.ImageSurface.create_from_png(__import__("io").BytesIO(payload))
    data_bytes = bytes(surface.get_data())
    assert data_bytes[3] == 255, "the corner pixel is painted, not transparent"


def test_the_svg_is_vector_text_and_marks_not_a_bitmap() -> None:
    payload = chart_image.svg_bytes(SPEC, data(), 640, 480)
    text = payload.decode("utf-8", "replace")
    assert text.lstrip().startswith("<?xml")
    assert "<svg" in text
    # A rasterised chart would arrive as one <image> element carrying a
    # base64 PNG; a vector one is paths and glyph definitions.
    assert "<image" not in text
    assert "base64" not in text
    assert "<path" in text or "<g" in text
    assert 'width="640' in text and 'height="480' in text


def test_the_svg_carries_the_legend_and_the_axis_labels() -> None:
    """Whatever the on-screen canvas had room for, the file has the
    legend and the labels."""
    text = chart_image.svg_bytes(SPEC, data(), 640, 480).decode()
    # Glyphs are drawn as outlines, so the words are not greppable;
    # what is countable is the glyph references themselves.
    notice = chart_image.svg_bytes(
        charts.ChartSpec(),
        charts.ChartData(reason="Nothing to plot."),
        640,
        480,
    ).decode()
    assert text.count("<use") > notice.count("<use")

    # The legend only exists for more than one series, so its arrival
    # is visible as extra glyphs over the same chart with one.
    one = chart_image.svg_bytes(
        charts.ChartSpec(type="bar", x="city", series=("sales",), title=SPEC.title),
        charts.ChartData(
            series=(
                charts.Series("sales", (("Paris", 3.0), ("Lyon", 5.0)), "sales"),
            ),
            x_kind=charts.CATEGORICAL,
            x_labels=("Paris", "Lyon"),
            rows=2,
        ),
        640,
        480,
    ).decode()
    assert text.count("<use") > one.count("<use")


# Theme


def test_the_default_is_light_and_dark_is_asked_for() -> None:
    light = chart_image.png_bytes(SPEC, data(), 400, 300)
    dark = chart_image.png_bytes(SPEC, data(), 400, 300, dark=True)
    assert light != dark
    # The top-left corner is background in both: light is near white,
    # dark is near black.
    assert sum(pixel(light, 1, 1)) > sum(pixel(dark, 1, 1))
    assert sum(pixel(light, 1, 1)) > 600


# Writing


def test_write_chart_puts_the_file_where_it_was_asked(tmp_path) -> None:
    for fmt in (Format.PNG, Format.SVG):
        target = tmp_path / f"chart{fmt.suffix}"
        written = chart_image.write_chart(target, fmt, SPEC, data(), 500, 320)
        assert written == target
        assert target.stat().st_size > 0
    assert (tmp_path / "chart.png").read_bytes().startswith(b"\x89PNG")


def test_a_failed_write_names_the_path_and_the_reason(tmp_path) -> None:
    target = tmp_path / "nowhere" / "chart.png"
    with pytest.raises(chart_image.ChartExportError) as raised:
        chart_image.write_chart(target, Format.PNG, SPEC, data(), 400, 300)
    message = str(raised.value)
    assert str(target) in message
    assert message != str(target)  # a reason came with it


def test_a_failed_write_leaves_no_partial_file(tmp_path, monkeypatch) -> None:
    target = tmp_path / "chart.png"

    def boom(source, destination):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(chart_image.os, "replace", boom)
    with pytest.raises(chart_image.ChartExportError) as raised:
        chart_image.write_chart(target, Format.PNG, SPEC, data(), 400, 300)
    assert "No space left on device" in str(raised.value)
    assert not target.exists()
    assert list(tmp_path.iterdir()) == [], "the .part is cleaned up too"


def test_a_chart_that_cannot_be_drawn_still_exports_its_notice(tmp_path) -> None:
    """The picture is whatever is on screen — including the sentence a
    mapping that could not be drawn leaves there."""
    target = tmp_path / "empty.png"
    chart_image.write_chart(
        target,
        Format.PNG,
        charts.ChartSpec(),
        charts.ChartData(reason="No numeric column to plot."),
        400,
        300,
    )
    assert target.stat().st_size > 0
