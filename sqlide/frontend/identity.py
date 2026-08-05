"""Identity colours on the GTK side: the palette provider and the four
widgets that are allowed to wear a colour.

GTK4 CSS has no light/dark media query, so the palette from
backend/identity.py is rendered into a Gtk.CssProvider at startup and
re-rendered whenever Adw.StyleManager flips `dark` (system preference,
or the theme setting).

The surfaces are deliberately few — a stripe under the window's header
bar, a bar at the leading edge of sidebar rows and tabs, a dot in the
launcher and the status bar — and every one of them sits next to the
name it stands for, so colour is never the only cue.
"""

from __future__ import annotations

from gi.repository import Adw, Gdk, Gtk

from sqlide.backend import identity

_provider: Gtk.CssProvider | None = None


def install_provider() -> None:
    """Load the palette for the current colour scheme and keep it in
    step with it. Called once, from the application's startup."""
    global _provider
    if _provider is not None:
        return
    _provider = Gtk.CssProvider()
    display = Gdk.Display.get_default()
    if display is not None:
        Gtk.StyleContext.add_provider_for_display(
            display, _provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
    manager = Adw.StyleManager.get_default()
    manager.connect("notify::dark", lambda *_: _reload())
    _reload()


def _reload() -> None:
    if _provider is None:
        return
    dark = Adw.StyleManager.get_default().get_dark()
    _provider.load_from_string(identity.stylesheet(dark))


def set_color(widget: Gtk.Widget, color: str) -> None:
    """Give `widget` exactly one identity colour class."""
    wanted = identity.css_class(color)
    for name in identity.COLOR_NAMES:
        css = identity.css_class(name)
        if css != wanted:
            widget.remove_css_class(css)
    widget.add_css_class(wanted)


def stripe(color: str) -> Gtk.Widget:
    """The workspace stripe: a full-width band under the header bar."""
    widget = Gtk.Box(height_request=4)
    widget.add_css_class("identity-stripe")
    set_color(widget, color)
    return widget


def bar(color: str) -> Gtk.Widget:
    """The connection bar: a vertical rule at a row's leading edge."""
    widget = Gtk.Box(width_request=3)
    widget.add_css_class("identity-bar")
    set_color(widget, color)
    return widget


def dot(color: str) -> Gtk.Widget:
    """The launcher/status-bar swatch, always beside its name."""
    widget = Gtk.Box(width_request=12, height_request=12)
    widget.set_valign(Gtk.Align.CENTER)
    widget.add_css_class("identity-dot")
    set_color(widget, color)
    return widget


def environment_badge(environment: str) -> Gtk.Label:
    """The environment's text badge — the non-colour half of the cue.
    Invisible for development and unset: a badge on every connection is
    a badge nobody reads."""
    environment = identity.normalize_environment(environment)
    text = identity.ENVIRONMENT_BADGES[environment]
    label = Gtk.Label(label=text, visible=bool(text))
    label.add_css_class("identity-badge")
    label.add_css_class(f"environment-{environment}")
    label.set_tooltip_text(
        f"{identity.ENVIRONMENT_LABELS[environment]} connection"
        if text
        else ""
    )
    return label


def set_environment(label: Gtk.Label, environment: str) -> None:
    """Retarget an existing badge (the status bar reuses one)."""
    environment = identity.normalize_environment(environment)
    for name in identity.ENVIRONMENTS:
        label.remove_css_class(f"environment-{name}")
    label.add_css_class(f"environment-{environment}")
    text = identity.ENVIRONMENT_BADGES[environment]
    label.set_text(text)
    label.set_visible(bool(text))
    label.set_tooltip_text(
        f"{identity.ENVIRONMENT_LABELS[environment]} connection"
        if text
        else ""
    )
