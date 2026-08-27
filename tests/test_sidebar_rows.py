"""Sidebar rows are as tall as the text in them (CORE-57).

The height of a schema-tree row used to be a flat 28px. That is ample
room at the default interface font and none at all at a larger one:
the row stopped growing where the label inside it kept growing, so the
label's box outgrew the row's content box and the ListView clipped
whatever stuck out — the top bar of a capital T, the tail of a g. The
rule in style.css now asks for 2em instead, which is the same height
at the default font and follows the font from there.

The check is the room the label actually gets, not the number in the
stylesheet: a row that merely *asks* for more is no use if the widget
inside it is still allocated less than its line height.
"""

from __future__ import annotations

from pathlib import Path

import pytest

STYLESHEET = (
    Path(__file__).resolve().parent.parent
    / "sqlide" / "frontend" / "style.css"
)


def _row_rule() -> str:
    """The `listview.schema-tree > row` block of the stylesheet."""
    css = STYLESHEET.read_text(encoding="utf-8")
    start = css.index("listview.schema-tree > row {")
    return css[start:css.index("}", start)]


def test_the_row_height_is_font_relative_not_a_pixel_count() -> None:
    """A hard-coded height survives neither a larger interface font nor
    a theme with roomier metrics."""
    rule = _row_rule()
    assert "min-height" in rule
    height = rule.split("min-height:")[1].split(";")[0].strip()
    assert height.endswith("em"), height


# The rendered row


@pytest.fixture()
def gtk():
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gtk

    if not Gtk.init_check():
        pytest.skip("no display for GTK")
    return Gtk


@pytest.fixture()
def styled(gtk):
    """The application stylesheet, loaded onto the display the way
    application.py loads it, and taken off again afterwards."""
    from gi.repository import Gdk, Gtk

    display = Gdk.Display.get_default()
    provider = Gtk.CssProvider()
    provider.load_from_path(str(STYLESHEET))
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
    settings = Gtk.Settings.get_default()
    font = settings.get_property("gtk-font-name")
    yield settings
    settings.set_property("gtk-font-name", font)
    Gtk.StyleContext.remove_provider_for_display(display, provider)


def _labels(widget):
    from gi.repository import Gtk

    if isinstance(widget, Gtk.Label):
        yield widget
    child = widget.get_first_child()
    while child is not None:
        yield from _labels(child)
        child = child.get_next_sibling()


def _room_for(gtk, font: str, settings) -> int:
    """How many pixels of slack the name label of a bound row has: its
    allocated height less the height its own text needs. Zero means the
    glyphs are drawn right up against the edge the row clips at."""
    from gi.repository import GLib

    from tests.test_sidebar_width import make_sidebar
    from sqlide.frontend.sidebar import Node

    settings.set_property("gtk-font-name", font)
    sidebar = make_sidebar()
    sidebar._roots.append(Node("table", "Tgjy_customer", detail="Tgjy ts"))
    sidebar._refresh_empty_state()
    window = gtk.Window(default_width=320, default_height=200)
    window.set_child(sidebar)
    window.present()
    context = GLib.MainContext.default()
    for _ in range(2000):  # until the ListView has built and sized a row
        context.iteration(False)
        found = [
            label for label in _labels(sidebar)
            if label.get_text() == "Tgjy_customer"
        ]
        if found and found[0].get_height():
            break
    else:  # pragma: no cover - a display that never draws
        window.destroy()
        pytest.skip("the list view never got laid out")
    label = found[0]
    needed = label.measure(gtk.Orientation.VERTICAL, -1)[1]
    room = label.get_height() - needed
    window.destroy()
    return room


def test_a_row_leaves_room_above_and_below_its_text(gtk, styled) -> None:
    assert _room_for(gtk, "Cantarell 11", styled) >= 4


def test_the_room_survives_a_much_larger_interface_font(gtk, styled) -> None:
    """The regression itself: at 20pt the old fixed height left the
    label exactly its line box and clipped the overflow."""
    assert _room_for(gtk, "Cantarell 20", styled) >= 4
