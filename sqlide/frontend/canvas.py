"""Drawing helpers shared by the cairo canvases (relation graph, query
plan graph).

Only the parts both diagrams agree on: the light/dark palette they
pick at draw time (never cached — the app's style can flip while a tab
is open), colour conversion, and the box/text primitives their nodes
are drawn from.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from gi.repository import Pango, PangoCairo

FONT = "Sans 10"


@dataclass
class Palette:
    fg: str
    border: str
    header_bg: str
    row_bg: str
    edge: str


def palette(dark: bool) -> Palette:
    if dark:
        return Palette("#eeeeec", "#5e5c64", "#3d3846", "#241f31", "#9a9996")
    return Palette("#241f31", "#9a9996", "#deddda", "#ffffff", "#5e5c64")


def rgb(hex_color: str) -> tuple[float, float, float]:
    return tuple(int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5))


def rounded_rect(cr, x: float, y: float, w: float, h: float, r: float) -> None:
    """Append a rounded rectangle to the current path."""
    r = min(r, w / 2, h / 2)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def draw_text(
    cr, layout: Pango.Layout, text: str, x: float, y: float, bold: bool = False
) -> None:
    layout.set_text(text, -1)
    attrs = Pango.AttrList()
    if bold:
        attrs.insert(Pango.attr_weight_new(Pango.Weight.BOLD))
    layout.set_attributes(attrs)
    cr.move_to(x, y)
    PangoCairo.show_layout(cr, layout)


def text_size(
    layout: Pango.Layout, text: str, bold: bool = False
) -> tuple[float, float]:
    layout.set_text(text, -1)
    attrs = Pango.AttrList()
    if bold:
        attrs.insert(Pango.attr_weight_new(Pango.Weight.BOLD))
    layout.set_attributes(attrs)
    _ink, logical = layout.get_pixel_extents()
    return logical.width, logical.height
