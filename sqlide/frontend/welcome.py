"""First-run home page.

Shown once, when there is no workspace on file at all (see
application.py — every later launch goes straight into the workspace
you were last in). It is the one screen that has to introduce the app
rather than get out of its way, so it does more than the workspace
list does: what sqlide is, what it does for you, and the single form
that gets you inside — name, colour, Create.

Deliberately not the launcher with different words. The launcher is a
switcher for people who already have workspaces and know what one is;
this page is for someone who has just opened the app and does not.
Once a workspace exists this window is never shown again, which is the
point: an intro you have to walk past every morning is a toll booth.
"""

from __future__ import annotations

from pathlib import Path

from gi.repository import Adw, Gdk, Gtk

from sqlide import APP_ID
from sqlide.backend import identity
from sqlide.backend.workspaces import Workspace
from sqlide.frontend import identity as identity_ui
from sqlide.frontend import transfer
from sqlide.frontend.connection_dialog import ConnectionForm
from sqlide.frontend.util import (
    describe,
    main_menu_button,
    open_workspace_from,
)

TAGLINE = "A minimal SQL IDE for SQLite, MySQL and PostgreSQL."

def app_icon(size: int) -> Gtk.Image:
    """The app icon at `size` px. Installed builds have it in the icon
    theme; a run from a source checkout does not, so fall back to the
    file the packaging installs, and to a stock icon if even that is
    missing (an installed-without-data-files run)."""
    display = Gdk.Display.get_default()
    theme = Gtk.IconTheme.get_for_display(display) if display else None
    if theme is not None and theme.has_icon(APP_ID):
        image = Gtk.Image.new_from_icon_name(APP_ID)
    else:
        svg = (
            Path(__file__).resolve().parents[2]
            / "data"
            / "icons"
            / f"{APP_ID}.svg"
        )
        image = (
            Gtk.Image.new_from_file(str(svg))
            if svg.exists()
            else Gtk.Image.new_from_icon_name("network-server-symbolic")
        )
    image.set_pixel_size(size)
    return image


class WelcomeWindow(Adw.ApplicationWindow):
    """Two steps, one Adw.NavigationView: name-and-colour, then a
    connection (the same form the app uses everywhere else, via
    ConnectionForm). Back returns to step 1 without losing anything —
    the workspace itself is not created until step 2 is saved, so
    going back and changing your mind leaves nothing behind."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.set_title("Welcome to sqlide")
        self.set_default_size(420, 560)

        self._pending_name = ""
        self._pending_color = identity.NONE

        self._nav = Adw.NavigationView()
        self._nav.add(self._build_step1())
        # The connection form has a lot more in it than name-and-colour,
        # so the window grows for step 2 and shrinks back on Back.
        self._nav.connect("pushed", lambda *_: self.set_default_size(480, 680))
        self._nav.connect("popped", lambda *_a: self.set_default_size(420, 560))

        self._toasts = Adw.ToastOverlay(child=self._nav)
        self.set_content(self._toasts)

        # Type the name and press Enter: the whole first run, if you
        # are in a hurry.
        self.connect("map", self._on_map)

    def _on_map(self, *_args) -> None:
        self._name.grab_focus()
        if (error := self.get_application().take_store_error()) is not None:
            self._toast(error)

    # Step 1: name and colour

    def _build_step1(self) -> Adw.NavigationPage:
        header = Adw.HeaderBar()
        header.add_css_class("flat")
        # No title: the hero underneath says it once, larger.
        header.set_title_widget(Gtk.Label())
        # Coming from another machine is the other way to start, so it
        # is on the page — but in the header, where the launcher keeps
        # it too, rather than below the fold under the pitch.
        import_button = Gtk.Button(
            child=Adw.ButtonContent(
                icon_name="document-open-symbolic", label="Import…"
            )
        )
        import_button.add_css_class("flat")
        describe(
            import_button,
            "Import a workspace exported from another machine (XML)",
        )
        import_button.connect("clicked", lambda *_: self._import())
        header.pack_start(import_button)
        header.pack_end(main_menu_button())

        body = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=28,
            valign=Gtk.Align.CENTER,
            vexpand=True,
            margin_bottom=24,
            margin_start=12,
            margin_end=12,
        )
        body.append(self._hero())
        body.append(self._form())

        clamp = Adw.Clamp(child=body, maximum_size=360)

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(clamp)
        return Adw.NavigationPage(child=view, title="Welcome", tag="start")

    def _hero(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        icon = app_icon(64)
        # Centred, so the rounded shadow hugs the icon instead of
        # stretching across the whole clamp.
        icon.set_halign(Gtk.Align.CENTER)
        icon.add_css_class("welcome-icon")
        box.append(icon)

        title = Gtk.Label(label="sqlide")
        title.add_css_class("title-1")
        box.append(title)

        tagline = Gtk.Label(
            label=TAGLINE, wrap=True, justify=Gtk.Justification.CENTER
        )
        tagline.add_css_class("body")
        tagline.add_css_class("dim-label")
        box.append(tagline)
        return box

    def _form(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        group = Adw.PreferencesGroup(title="Create your first workspace")
        self._name = Adw.EntryRow(title="Workspace name")
        self._name.connect("entry-activated", lambda *_: self._continue())
        group.add(self._name)
        self._color = identity_ui.ColorRow(subtitle="Window colour")
        self._color.set_color(identity.NONE)
        group.add(self._color)
        box.append(group)

        continue_button = Gtk.Button(label="Continue", halign=Gtk.Align.CENTER)
        continue_button.add_css_class("suggested-action")
        continue_button.add_css_class("pill")
        continue_button.connect("clicked", lambda *_: self._continue())
        box.append(continue_button)
        return box

    # Step 2: a connection, via the same form the rest of the app uses

    def _build_step2(self) -> Adw.NavigationPage:
        header = Adw.HeaderBar()
        header.pack_end(main_menu_button())

        self._connection_form = ConnectionForm(
            toast=self._toast,
            primary_action=("Connect", self._finish),
        )

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(self._connection_form.page)
        return Adw.NavigationPage(
            child=view, title="Add a connection", tag="connection"
        )

    # Actions

    def _continue(self) -> None:
        self._pending_name = (
            self._name.get_text().strip()
            or self.get_application().workspace_store.default_name()
        )
        self._pending_color = self._color.get_color()
        self._nav.push(self._build_step2())
        self._connection_form.grab_focus()

    def _finish(self) -> None:
        store = self.get_application().workspace_store
        workspace = Workspace(name=self._pending_name, color=self._pending_color)
        workspace.add_connection(self._connection_form.build_profile())
        store.workspaces.append(workspace)
        try:
            store.save(workspace)
        except Exception as exc:
            store.workspaces.remove(workspace)
            self._toast(f"Could not create the workspace: {exc}")
            return
        open_workspace_from(self, workspace)

    def _import(self) -> None:
        transfer.import_workspace(self, self._imported, self._toast)

    def _imported(self, workspace: Workspace) -> None:
        store = self.get_application().workspace_store
        store.workspaces.append(workspace)
        try:
            store.save(workspace)
        except Exception as exc:
            store.workspaces.remove(workspace)
            self._toast(f"Could not save the imported workspace: {exc}")
            return
        open_workspace_from(self, workspace)

    def _toast(self, message: str) -> None:
        self._toasts.add_toast(Adw.Toast(title=message))
