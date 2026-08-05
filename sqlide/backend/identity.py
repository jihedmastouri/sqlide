"""Identity colours and environment classes.

Two databases that look alike are the reason people run the wrong
statement against the wrong server. A workspace and a connection can
each carry a colour from a small fixed palette, and a connection also
carries an environment class (development / staging / production) that
changes how much friction destructive actions get (see
frontend/confirm.py).

The palette is fixed and named rather than free-form hex for three
reasons, all of them checked by tests/test_identity.py:

1. every colour reaches 3:1 against the window background in the light,
   dark and high-contrast variants (WCAG non-text contrast),
2. the pairs that colour-vision deficiencies confuse — red/green,
   blue/purple, yellow/orange — stay apart under simulated protanopia,
   deuteranopia and tritanopia,
3. two connections picked at random stay distinguishable.

GTK4 CSS has no light/dark media query, so the light and dark tables
are rendered into a stylesheet at runtime (`stylesheet()`) and reloaded
when the colour scheme changes; frontend/identity.py owns that provider.
Colour is never the only cue anywhere it is used — the name is always
next to it.
"""

from __future__ import annotations

NONE = "none"

# Palette names in picker order. "none" is the default and the fallback
# for anything unrecognised.
COLOR_NAMES: tuple[str, ...] = (
    NONE,
    "red",
    "orange",
    "yellow",
    "green",
    "teal",
    "blue",
    "purple",
    "pink",
)

COLOR_LABELS: dict[str, str] = {
    NONE: "None",
    "red": "Red",
    "orange": "Orange",
    "yellow": "Yellow",
    "green": "Green",
    "teal": "Teal",
    "blue": "Blue",
    "purple": "Purple",
    "pink": "Pink",
}

# Conventional meanings, shown as the picker's subtitle. Suggestions,
# not rules: the environment class is what actually changes behaviour.
COLOR_HINTS: dict[str, str] = {
    "red": "production",
    "orange": "staging",
    "green": "development",
}

# Light theme: dark enough for 3:1 against #fafafa and #ffffff.
LIGHT_COLORS: dict[str, str] = {
    "red": "#d3303f",
    "orange": "#a33800",
    "yellow": "#a67d00",
    "green": "#1f7a4d",
    "teal": "#00707f",
    "blue": "#1c71d8",
    "purple": "#9141ac",
    "pink": "#c4267f",
}

# Dark theme: light enough for 3:1 against #242424 and #000000.
DARK_COLORS: dict[str, str] = {
    "red": "#f66151",
    "orange": "#e08a3c",
    "yellow": "#f8e45c",
    "green": "#8ff0a4",
    "teal": "#5bc8d8",
    "blue": "#99c1f1",
    "purple": "#cf7ae8",
    "pink": "#ff9ec4",
}

# Backgrounds the palette is verified against: libadwaita's
# window_bg_color in each theme, plus the high-contrast extremes.
LIGHT_BACKGROUNDS: tuple[str, ...] = ("#fafafa", "#ffffff")
DARK_BACKGROUNDS: tuple[str, ...] = ("#242424", "#1d1d20", "#000000")

# Every surface a colour is allowed to appear on, and the non-colour
# cue that appears with it. Colour is pre-attentive but it is also the
# first thing a colour-blind user loses, so it never encodes anything
# on its own. frontend/identity.py takes one of these keys on every
# widget it builds, and tests/test_identity.py checks the two lists
# agree — a new coloured surface cannot be added without naming its
# cue.
COLOR_SURFACES: dict[str, str] = {
    "window-stripe": "the workspace name in the window title and its tooltip",
    "launcher-dot": "the workspace name on the same row",
    "sidebar-bar": "the connection name on (or above) the row",
    "tab-icon": "the connection name in the tab title",
}


def check_surface(surface: str) -> str:
    """Guard on the frontend's colour helpers: a surface must be
    registered above, with its non-colour cue, before it can be
    coloured."""
    if surface not in COLOR_SURFACES:
        raise ValueError(
            f"Unregistered identity surface: {surface!r}. Add it to "
            "COLOR_SURFACES with the non-colour cue that goes with it."
        )
    return surface


UNSET = "unset"
ENVIRONMENTS: tuple[str, ...] = (UNSET, "development", "staging", "production")

ENVIRONMENT_LABELS: dict[str, str] = {
    UNSET: "Unset",
    "development": "Development",
    "staging": "Staging",
    "production": "Production",
}

# Short, upper-case badge text. Development gets none: a badge on every
# connection is a badge nobody reads.
ENVIRONMENT_BADGES: dict[str, str] = {
    UNSET: "",
    "development": "",
    "staging": "STAGING",
    "production": "PRODUCTION",
}

# Substrings that make the connection dialog *suggest* production.
PRODUCTION_HINTS: tuple[str, ...] = ("prod", "production", "live")

LOOPBACK_HOSTS: frozenset[str] = frozenset(
    {"", "localhost", "127.0.0.1", "::1", "ip6-localhost"}
)


def normalize_color(name: str | None) -> str:
    """A palette name, or "none" for anything unknown — a workspace
    file written by a newer version (or edited by hand) must still
    open."""
    return name if name in COLOR_NAMES else NONE


def normalize_environment(name: str | None) -> str:
    return name if name in ENVIRONMENTS else UNSET


def color_hex(name: str, dark: bool) -> str:
    """The colour's hex for one theme; "" for "none" and unknowns,
    which are rendered from the foreground colour instead."""
    table = DARK_COLORS if dark else LIGHT_COLORS
    return table.get(normalize_color(name), "")


def css_class(name: str) -> str:
    return f"identity-{normalize_color(name)}"


def stylesheet(dark: bool) -> str:
    """The identity palette as GTK CSS: one background-colour class per
    palette name. Regenerated whenever the colour scheme changes."""
    lines = [
        "/* Generated by sqlide.backend.identity — do not edit. */",
        # "none" is not a colour but a placeholder that must still take
        # up its slot, so surfaces do not jump when one is assigned.
        f".{css_class(NONE)} {{ background-color: alpha(currentColor, 0.15); }}",
    ]
    for name in COLOR_NAMES:
        if name == NONE:
            continue
        lines.append(
            f".{css_class(name)} {{ background-color: "
            f"{color_hex(name, dark)}; }}"
        )
    return "\n".join(lines) + "\n"


def suggests_production(
    name: str, host: str, database: str, kind: str
) -> str:
    """Why this connection looks like production, or "" if it doesn't.

    A hint for the connection dialog to show — never applied silently.
    A wrong automatic classification is worse than none, because it
    teaches people the badge is unreliable.
    """
    for field, text in (("name", name), ("host", host), ("database", database)):
        lowered = text.lower()
        for hint in PRODUCTION_HINTS:
            if hint in lowered:
                return f"the {field} contains “{hint}”"
    if kind in ("mysql", "postgres") and host.lower() not in LOOPBACK_HOSTS:
        return "it points at a remote host"
    return ""


# Colour maths. Only the palette's guarantees depend on this — it backs
# the assertions in tests/test_identity.py and nothing else at runtime.


def _channels(color: str) -> tuple[float, float, float]:
    value = color.lstrip("#")
    return tuple(int(value[i : i + 2], 16) / 255 for i in (0, 2, 4))


def _linear(channel: float) -> float:
    if channel <= 0.04045:
        return channel / 12.92
    return ((channel + 0.055) / 1.055) ** 2.4


def linear_rgb(color: str) -> tuple[float, float, float]:
    return tuple(_linear(c) for c in _channels(color))


def relative_luminance(color: str) -> float:
    red, green, blue = linear_rgb(color)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast_ratio(a: str, b: str) -> float:
    """WCAG contrast ratio between two hex colours (1.0 … 21.0)."""
    first, second = sorted(
        (relative_luminance(a), relative_luminance(b)), reverse=True
    )
    return (first + 0.05) / (second + 0.05)


# Viénot/Brettel/Mollon dichromat simulation matrices, applied to
# linear RGB.
CVD_MATRICES: dict[str, tuple[tuple[float, ...], ...]] = {
    "protanopia": (
        (0.11238, 0.88762, 0.0),
        (0.11238, 0.88762, 0.0),
        (0.00401, -0.00401, 1.0),
    ),
    "deuteranopia": (
        (0.29275, 0.70725, 0.0),
        (0.29275, 0.70725, 0.0),
        (-0.02234, 0.02234, 1.0),
    ),
    "tritanopia": (
        (1.0, 0.14461, -0.14461),
        (0.0, 0.85659, 0.14341),
        (0.0, 0.85659, 0.14341),
    ),
}


def simulate_cvd(color: str, deficiency: str) -> tuple[float, float, float]:
    """`color` as seen with `deficiency`, in linear RGB."""
    matrix = CVD_MATRICES[deficiency]
    source = linear_rgb(color)
    return tuple(
        min(1.0, max(0.0, sum(row[i] * source[i] for i in range(3))))
        for row in matrix
    )


def _lab(rgb: tuple[float, float, float]) -> tuple[float, float, float]:
    red, green, blue = rgb
    x = 0.4124 * red + 0.3576 * green + 0.1805 * blue
    y = 0.2126 * red + 0.7152 * green + 0.0722 * blue
    z = 0.0193 * red + 0.1192 * green + 0.9505 * blue

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116

    fx, fy, fz = f(x / 0.95047), f(y), f(z / 1.08883)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def delta_e(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    """CIE76 colour difference between two linear-RGB colours."""
    first, second = _lab(a), _lab(b)
    return sum((first[i] - second[i]) ** 2 for i in range(3)) ** 0.5
