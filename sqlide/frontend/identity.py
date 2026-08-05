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

from typing import Callable

from gi.repository import Adw, Gdk, GLib, Gtk

from sqlide.backend import identity

_provider: Gtk.CssProvider | None = None
# Surfaces that cannot restyle themselves from CSS — currently the tab
# icons, which are textures — ask to be redrawn when the palette
# changes.
_listeners: list[Callable[[], None]] = []


def subscribe(listener: Callable[[], None]) -> None:
    _listeners.append(listener)


def unsubscribe(listener: Callable[[], None]) -> None:
    """Windows must drop their listener on teardown, or the module
    keeps them alive forever."""
    if listener in _listeners:
        _listeners.remove(listener)


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
    for listener in list(_listeners):
        listener()


def set_color(widget: Gtk.Widget, color: str) -> None:
    """Give `widget` exactly one identity colour class."""
    wanted = identity.css_class(color)
    for name in identity.COLOR_NAMES:
        css = identity.css_class(name)
        if css != wanted:
            widget.remove_css_class(css)
    widget.add_css_class(wanted)


def stripe(color: str, surface: str = "window-stripe") -> Gtk.Widget:
    """The workspace stripe: a full-width band under the header bar."""
    identity.check_surface(surface)
    widget = Gtk.Box(height_request=4)
    widget.add_css_class("identity-stripe")
    set_color(widget, color)
    return widget


def bar(color: str, surface: str = "sidebar-bar") -> Gtk.Widget:
    """The connection bar: a vertical rule at a row's leading edge."""
    identity.check_surface(surface)
    widget = Gtk.Box(width_request=3)
    widget.add_css_class("identity-bar")
    set_color(widget, color)
    return widget


def dot(color: str, surface: str = "launcher-dot") -> Gtk.Widget:
    """The launcher/status-bar swatch, always beside its name."""
    identity.check_surface(surface)
    widget = Gtk.Box(width_request=12, height_request=12)
    widget.set_valign(Gtk.Align.CENTER)
    widget.add_css_class("identity-dot")
    set_color(widget, color)
    return widget


def tab_icon(color: str, surface: str = "tab-icon") -> Gdk.Texture | None:
    """A tab's leading colour bar. Adw.TabPage takes a Gio.Icon and
    nothing else, and a symbolic icon would be recoloured by the theme,
    so the bar is a solid texture (GdkTexture is a Gio.Icon). None for
    "none", which leaves the tab as it was."""
    identity.check_surface(surface)
    hex_color = identity.color_hex(color, Adw.StyleManager.get_default().get_dark())
    if not hex_color:
        return None
    value = hex_color.lstrip("#")
    pixel = bytes(int(value[i : i + 2], 16) for i in (0, 2, 4)) + b"\xff"
    width, height = 3, 16
    return Gdk.MemoryTexture.new(
        width,
        height,
        Gdk.MemoryFormat.R8G8B8A8,
        GLib.Bytes.new(pixel * width * height),
        width * 4,
    )


class ColorRow(Adw.ComboRow):
    """Palette picker: a swatch *and* its name on every row, so the
    choice is readable without seeing the colour."""

    def __init__(self, title: str = "Colour", subtitle: str = "") -> None:
        super().__init__(
            title=title,
            subtitle=subtitle,
            model=Gtk.StringList.new(list(identity.COLOR_NAMES)),
            factory=_color_factory(hint=False),
            list_factory=_color_factory(hint=True),
        )

    def get_color(self) -> str:
        return identity.COLOR_NAMES[self.get_selected()]

    def set_color(self, color: str) -> None:
        self.set_selected(
            identity.COLOR_NAMES.index(identity.normalize_color(color))
        )


class EnvironmentRow(Adw.ComboRow):
    """Environment class picker. Unlike the colour it changes what the
    app does, so its subtitle says so."""

    def __init__(self) -> None:
        super().__init__(
            title="Environment",
            subtitle="Staging and production ask before destructive "
            "statements run",
            model=Gtk.StringList.new(
                [identity.ENVIRONMENT_LABELS[e] for e in identity.ENVIRONMENTS]
            ),
        )

    def get_environment(self) -> str:
        return identity.ENVIRONMENTS[self.get_selected()]

    def set_environment(self, environment: str) -> None:
        self.set_selected(
            identity.ENVIRONMENTS.index(
                identity.normalize_environment(environment)
            )
        )


def _color_factory(hint: bool) -> Gtk.SignalListItemFactory:
    factory = Gtk.SignalListItemFactory()

    def setup(_factory, item: Gtk.ListItem) -> None:
        box = Gtk.Box(spacing=9)
        swatch = dot(identity.NONE)
        label = Gtk.Label(xalign=0)
        box.append(swatch)
        box.append(label)
        if hint:
            note = Gtk.Label(xalign=0, hexpand=True, halign=Gtk.Align.END)
            note.add_css_class("dim-label")
            note.add_css_class("caption")
            box.append(note)
            item.note = note
        item.swatch = swatch
        item.name_label = label
        item.set_child(box)

    def bind(_factory, item: Gtk.ListItem) -> None:
        name = item.get_item().get_string()
        set_color(item.swatch, name)
        item.name_label.set_text(identity.COLOR_LABELS[name])
        if hint:
            item.note.set_text(identity.COLOR_HINTS.get(name, ""))

    factory.connect("setup", setup)
    factory.connect("bind", bind)
    return factory


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
