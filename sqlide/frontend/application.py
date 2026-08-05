"""Adw.Application subclass and app entry point.

Owns the WorkspaceStore. Startup presents the small workspace
launcher; picking a workspace opens (or focuses) that workspace's main
window. The launcher can be reopened from any main window's sidebar
to switch or create workspaces.

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
from sqlide.frontend.launcher import WorkspaceLauncher
from sqlide.frontend.preferences import PreferencesDialog, about_dialog
from sqlide.frontend.shortcuts import shortcuts_dialog
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

        for name, callback, accels in (
            ("preferences", self._show_preferences, ["<primary>comma"]),
            ("shortcuts", self._show_shortcuts, ["<primary>question"]),
            ("help", self._show_help, ["F1"]),
            ("about", self._show_about, []),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)
            if accels:
                self.set_accels_for_action(f"app.{name}", accels)
        # GTK provides window.close itself; it just has no binding.
        self.set_accels_for_action("window.close", ["<primary>w"])
        # Tabs of a workspace window (no-ops in the launcher, which
        # defines neither action).
        self.set_accels_for_action("win.close-tab", ["<primary>F4"])
        self.set_accels_for_action(
            "win.close-all-tabs", ["<primary><shift>w"]
        )

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
        else:
            self.show_launcher()

    # Window management

    def show_launcher(self) -> None:
        if self._launcher is None:
            self._launcher = WorkspaceLauncher(application=self)
            self._launcher.connect("close-request", self._launcher_closed)
        self._launcher.present()

    def _launcher_closed(self, *_args) -> bool:
        self._launcher = None
        return False

    def open_workspace(self, workspace: Workspace) -> None:
        window = self._workspace_windows.get(workspace.id)
        if window is None:
            window = MainWindow(application=self, workspace=workspace)
            window.connect("close-request", self._workspace_closed, workspace.id)
            self._workspace_windows[workspace.id] = window
        window.present()

    def _workspace_closed(self, _window, workspace_id: str) -> bool:
        self._workspace_windows.pop(workspace_id, None)
        return False


def main() -> int:
    app = SqlideApplication()
    return app.run(sys.argv)
