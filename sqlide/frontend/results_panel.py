"""Collapsible results area shared by the query console and builder.

A thin header line ("Results" plus a minimize/expand toggle) above an
arbitrary content widget. The panel lives as the end child of a
vertical Gtk.Paned; minimizing hides the content and drops the divider
to the bottom so the upper pane reclaims the space (the paned clamps
the divider to keep the header visible), and expanding puts the
divider back where it was.
"""

from __future__ import annotations

from gi.repository import Gtk


class ResultsPanel(Gtk.Box):
    """Starts hidden; call reveal() when there is something to show."""

    def __init__(self, content: Gtk.Widget, paned: Gtk.Paned) -> None:
        super().__init__(
            orientation=Gtk.Orientation.VERTICAL, visible=False
        )
        self._content = content
        self._paned = paned
        self._saved_position = 160

        header = Gtk.Box(
            spacing=6, margin_start=8, margin_end=6,
            margin_top=2, margin_bottom=2,
        )
        title = Gtk.Label(label="Results", xalign=0, hexpand=True)
        title.add_css_class("dim-label")
        title.add_css_class("caption-heading")
        self._toggle = Gtk.ToggleButton(
            icon_name="go-down-symbolic", active=True
        )
        self._toggle.add_css_class("flat")
        self._toggle.set_tooltip_text("Minimize or expand the results")
        self._toggle.connect("toggled", self._on_toggled)
        header.append(title)
        header.append(self._toggle)
        self.append(header)
        self.append(content)

    def reveal(self) -> None:
        """Show the panel, expanded, ahead of new results."""
        self.set_visible(True)
        self._toggle.set_active(True)

    def _on_toggled(self, toggle: Gtk.ToggleButton) -> None:
        expanded = toggle.get_active()
        self._content.set_visible(expanded)
        toggle.set_icon_name(
            "go-down-symbolic" if expanded else "go-up-symbolic"
        )
        if expanded:
            self._paned.set_position(self._saved_position)
        else:
            self._saved_position = self._paned.get_position()
            self._paned.set_position(self._paned.get_height())
