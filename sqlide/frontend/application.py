"""Adw.Application subclass and app entry point.

Owns the WorkspaceStore. Startup presents the small workspace
launcher; picking a workspace opens (or focuses) that workspace's main
window. The launcher can be reopened from any main window's sidebar
to switch or create workspaces.
"""

import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, Gtk  # noqa: E402

from sqlide import APP_ID
from sqlide.backend.workspaces import Workspace, WorkspaceStore
from sqlide.frontend.launcher import WorkspaceLauncher
from sqlide.frontend.window import MainWindow


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
        try:
            self.workspace_store.load()
        except Exception as exc:
            self.store_error = f"Could not load workspaces: {exc}"

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
