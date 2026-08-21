"""Adw.Application subclass and app entry point.

Owns the WorkspaceStore. Startup opens a workspace's main window
straight away — the one last used, or the first on file — because a
picker with a single row on it is a door with nothing behind it. A
first run, with nothing to open, gets the home page (welcome.py)
instead. The workspace launcher is in-app UI from then on: the
sidebar's Workspaces button opens it to switch or create workspaces,
and picking one there opens (or focuses) that workspace's window.

Also owns the presentation side of the settings store: it applies the
theme and editor font at startup and on every change, and provides the
app.preferences / app.about actions that windows put in their menus.
"""

import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, Gtk  # noqa: E402

from sqlide import APP_ID
from sqlide.backend import settings as app_settings
from sqlide.backend.workspaces import Workspace, WorkspaceStore
from sqlide.frontend import identity
from sqlide.frontend.help import help_dialog
from sqlide.frontend import keymap
from sqlide.frontend.launcher import WorkspaceLauncher
from sqlide.frontend.preferences import PreferencesDialog, about_dialog
from sqlide.frontend.shortcuts import shortcuts_dialog
from sqlide.frontend.welcome import WelcomeWindow
from sqlide.frontend.window import MainWindow

_COLOR_SCHEMES = {
    "system": Adw.ColorScheme.DEFAULT,
    "light": Adw.ColorScheme.FORCE_LIGHT,
    "dark": Adw.ColorScheme.FORCE_DARK,
}


class SqlideApplication(Adw.Application):
    def __init__(self) -> None:
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        self.workspace_store = WorkspaceStore()
        self.store_error: str | None = None
        self._launcher: WorkspaceLauncher | None = None
        self._workspace_windows: dict[str, MainWindow] = {}

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        provider = Gtk.CssProvider()
        provider.load_from_path(str(Path(__file__).parent / "style.css"))
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        identity.install_provider()
        try:
            self.workspace_store.load()
        except Exception as exc:
            self.store_error = f"Could not load workspaces: {exc}"
        try:
            app_settings.store.load()
        except Exception as exc:
            self.store_error = f"Could not load settings: {exc}"

        # Settings that restyle the app; re-applied on every change.
        self._font_provider = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            self._font_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        self._apply_settings(app_settings.store.settings)
        app_settings.store.subscribe(self._apply_settings)

        for name, callback in (
            ("preferences", self._show_preferences),
            ("shortcuts", self._show_shortcuts),
            ("help", self._show_help),
            ("about", self._show_about),
            ("show-launcher", lambda *_: self.show_launcher()),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)
        # Every accelerator — these actions, window.close, and every
        # win.* action a workspace window defines — comes from the
        # keymap registry, kept live so a shortcut edited in
        # Preferences takes effect without a restart.
        keymap.apply_app_accels(self)
        app_settings.store.subscribe(lambda _s: keymap.apply_app_accels(self))

    def _apply_settings(self, settings: app_settings.Settings) -> None:
        Adw.StyleManager.get_default().set_color_scheme(
            _COLOR_SCHEMES.get(settings.theme, Adw.ColorScheme.DEFAULT)
        )
        self._font_provider.load_from_string(
            "textview.sqlide-editor {"
            f" font-size: {settings.editor_font_size}pt; }}"
        )

    def _show_preferences(self, *_args) -> None:
        PreferencesDialog().present(self.get_active_window())

    def _show_shortcuts(self, *_args) -> None:
        shortcuts_dialog().present(self.get_active_window())

    def _show_help(self, *_args) -> None:
        help_dialog().present(self.get_active_window())

    def _show_about(self, *_args) -> None:
        about_dialog().present(self.get_active_window())

    def do_activate(self) -> None:
        window = self.get_active_window()
        if window is not None:
            window.present()
            return
        workspace = self._startup_workspace()
        if workspace is None:
            self.show_welcome()
            return
        window = self.open_workspace(workspace)
        if (error := self.take_store_error()) is not None:
            window.show_error(error)

    def _startup_workspace(self) -> Workspace | None:
        """The workspace to land in: the one last used, else the first
        on file. None on a first run — nothing to open yet, so the home
        page takes the screen instead."""
        workspaces = self.workspace_store.workspaces
        last = app_settings.store.settings.last_workspace
        for workspace in workspaces:
            if workspace.id == last:
                return workspace
        return workspaces[0] if workspaces else None

    def take_store_error(self) -> str | None:
        """The load error, once: whichever window opens first reports
        it, and it is not repeated on every later launcher visit."""
        error, self.store_error = self.store_error, None
        return error

    # Window management

    def show_welcome(self) -> None:
        """The first-run home page. Not cached like the launcher: it is
        shown once, and closes itself the moment a workspace exists."""
        WelcomeWindow(application=self).present()

    def show_launcher(self) -> None:
        if self._launcher is None:
            self._launcher = WorkspaceLauncher(application=self)
            self._launcher.connect("close-request", self._launcher_closed)
        self._launcher.present()

    def _launcher_closed(self, *_args) -> bool:
        self._launcher = None
        return False

    def open_workspace(self, workspace: Workspace) -> MainWindow:
        """Open (or re-focus) a workspace's window and return it.

        Also remembers it as the one to reopen next launch; a failure
        to persist that costs the next startup its memory and nothing
        else, so it must not take the window down with it."""
        if app_settings.store.settings.last_workspace != workspace.id:
            try:
                app_settings.store.update(last_workspace=workspace.id)
            except Exception:
                pass
        window = self._workspace_windows.get(workspace.id)
        if window is None:
            window = MainWindow(application=self, workspace=workspace)
            window.connect("close-request", self._workspace_closed, workspace.id)
            self._workspace_windows[workspace.id] = window
        window.present()
        return window

    def _workspace_closed(self, _window, workspace_id: str) -> bool:
        self._workspace_windows.pop(workspace_id, None)
        return False


def main() -> int:
    app = SqlideApplication()
    return app.run(sys.argv)
