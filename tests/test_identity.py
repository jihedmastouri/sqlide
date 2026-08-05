"""The identity palette's guarantees, asserted rather than eyeballed.

Contrast and colour-blind separation are computations, so they belong
in a test: any future edit to the palette tables has to keep them.
"""

from __future__ import annotations

import itertools

import pytest

from sqlide.backend import identity

# The pairs a colour-vision deficiency classically collapses; each is
# checked under the deficiency that collapses it.
CONFUSION_PAIRS = (
    ("red", "green", "protanopia"),
    ("red", "green", "deuteranopia"),
    ("blue", "purple", "protanopia"),
    ("blue", "purple", "deuteranopia"),
    ("yellow", "orange", "deuteranopia"),
    ("yellow", "orange", "tritanopia"),
)

# CIE76 ΔE. 10 is comfortably above the ~2.3 just-noticeable
# difference, so a confusable pair stays legible as two colours and
# not as one.
MIN_DELTA_E = 10.0


def _tables():
    return (
        (identity.LIGHT_COLORS, identity.LIGHT_BACKGROUNDS),
        (identity.DARK_COLORS, identity.DARK_BACKGROUNDS),
    )


def test_every_color_has_a_light_and_dark_value():
    named = [n for n in identity.COLOR_NAMES if n != identity.NONE]
    assert sorted(identity.LIGHT_COLORS) == sorted(named)
    assert sorted(identity.DARK_COLORS) == sorted(named)
    assert sorted(identity.COLOR_LABELS) == sorted(identity.COLOR_NAMES)


@pytest.mark.parametrize("colors,backgrounds", _tables())
def test_contrast_against_every_window_background(colors, backgrounds):
    """3:1 (WCAG non-text contrast) in the theme's own background and
    in its high-contrast variant."""
    for name, value in colors.items():
        for background in backgrounds:
            ratio = identity.contrast_ratio(value, background)
            assert ratio >= 3.0, f"{name} {value} on {background}: {ratio:.2f}"


@pytest.mark.parametrize("colors,_backgrounds", _tables())
def test_confusable_pairs_stay_apart(colors, _backgrounds):
    for first, second, deficiency in CONFUSION_PAIRS:
        difference = identity.delta_e(
            identity.simulate_cvd(colors[first], deficiency),
            identity.simulate_cvd(colors[second], deficiency),
        )
        assert difference >= MIN_DELTA_E, (
            f"{first}/{second} under {deficiency}: {difference:.1f}"
        )


@pytest.mark.parametrize("colors,_backgrounds", _tables())
def test_no_two_colors_are_the_same(colors, _backgrounds):
    for first, second in itertools.combinations(colors, 2):
        assert colors[first] != colors[second]


def test_unknown_values_load_as_defaults():
    assert identity.normalize_color("chartreuse") == identity.NONE
    assert identity.normalize_color(None) == identity.NONE
    assert identity.normalize_color("red") == "red"
    assert identity.normalize_environment("qa") == identity.UNSET
    assert identity.normalize_environment(None) == identity.UNSET
    assert identity.normalize_environment("staging") == "staging"


def test_stylesheet_covers_the_palette_in_both_themes():
    for dark in (False, True):
        css = identity.stylesheet(dark)
        for name in identity.COLOR_NAMES:
            assert f".{identity.css_class(name)} " in css
        for name, value in (
            identity.DARK_COLORS if dark else identity.LIGHT_COLORS
        ).items():
            assert value in css, name


def test_color_hex_falls_back_for_none():
    assert identity.color_hex("none", dark=False) == ""
    assert identity.color_hex("nonsense", dark=True) == ""
    assert identity.color_hex("red", dark=True) == identity.DARK_COLORS["red"]


def test_production_is_suggested_not_assumed():
    assert identity.suggests_production(
        "orders-prod", "db1.internal", "app", "postgres"
    )
    assert identity.suggests_production("orders", "localhost", "live", "sqlite")
    assert identity.suggests_production(
        "orders", "db1.internal", "app", "postgres"
    ) == "it points at a remote host"
    # A local development database looks like nothing in particular.
    assert (
        identity.suggests_production("orders", "localhost", "app", "postgres")
        == ""
    )
    assert identity.suggests_production("demo", "", "", "sqlite") == ""
