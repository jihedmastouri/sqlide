"""Workspace launcher window.

The small window shown at startup (and from the sidebar's Workspaces
button): pick an existing workspace or create a new one by name.
Activating a workspace asks the application to open its main window
and closes the launcher. The list is rebuilt every time the window is
mapped so it stays in sync with workspaces created elsewhere.
"""

from __future__ import annotations

from gi.repository import Adw, Gtk

from sqlide.backend.workspaces import Workspace
from sqlide.frontend.util import main_menu_button


class WorkspaceLauncher(Adw.ApplicationWindow):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.set_title("sqlide")
        self.set_default_size(400, 480)
        self._store_error_shown = False

        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Label(label="Workspaces"))
        new_button = Gtk.Button(icon_name="list-add-symbolic")
        new_button.set_tooltip_text("New workspace")
        new_button.connect("clicked", lambda *_: self._new_workspace())
        header.pack_start(new_button)
        header.pack_end(main_menu_button())

        self._list = Gtk.ListBox()
        self._list.set_selection_mode(Gtk.SelectionMode.NONE)
        self._list.add_css_class("boxed-list")
        self._list.set_valign(Gtk.Align.START)
        clamp = Adw.Clamp(
            child=self._list,
            maximum_size=360,
            margin_top=18,
            margin_bottom=18,
            margin_start=12,
            margin_end=12,
        )
        scroller = Gtk.ScrolledWindow(child=clamp, vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        create_button = Gtk.Button(label="Create Workspace")
        create_button.add_css_class("suggested-action")
        create_button.add_css_class("pill")
        create_button.connect("clicked", lambda *_: self._new_workspace())
        empty = Adw.StatusPage(
            icon_name="folder-symbolic",
            title="No workspaces yet",
            description="A workspace groups your connections and remembers "
            "the tabs you had open.",
            child=create_button,
        )

        self._stack = Gtk.Stack()
        self._stack.add_named(scroller, "list")
        self._stack.add_named(empty, "empty")

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(self._stack)
        self._toasts = Adw.ToastOverlay(child=view)
        self.set_content(self._toasts)

        self.connect("map", lambda *_: self._refresh())

    def _refresh(self) -> None:
        app = self.get_application()
        if app.store_error and not self._store_error_shown:
            self._store_error_shown = True
            self._toasts.add_toast(Adw.Toast(title=app.store_error))

        while (row := self._list.get_row_at_index(0)) is not None:
            self._list.remove(row)
        workspaces = app.workspace_store.workspaces
        for workspace in workspaces:
            self._list.append(self._workspace_row(workspace))
        self._stack.set_visible_child_name("list" if workspaces else "empty")

    def _workspace_row(self, workspace: Workspace) -> Adw.ActionRow:
        count = len(workspace.connections)
        subtitle = "no connections" if count == 0 else (
            "1 connection" if count == 1 else f"{count} connections"
        )
        row = Adw.ActionRow(
            title=workspace.name,
            subtitle=subtitle,
            activatable=True,
        )
        row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        row.connect("activated", lambda *_: self._open(workspace))
        return row

    def _new_workspace(self) -> None:
        dialog = Adw.AlertDialog(
            heading="New Workspace",
            body="Connections and open tabs are kept per workspace.",
        )
        entry = Gtk.Entry(
            placeholder_text="Workspace name", activates_default=True
        )
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("create", "Create")
        dialog.set_response_appearance(
            "create", Adw.ResponseAppearance.SUGGESTED
        )
        dialog.set_default_response("create")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._create_response, entry)
        dialog.present(self)

    def _create_response(self, _dialog, response: str, entry: Gtk.Entry) -> None:
        if response != "create":
            return
        name = entry.get_text().strip() or "Workspace"
        workspace = self.get_application().workspace_store.create(name)
        self._open(workspace)

    def _open(self, workspace: Workspace) -> None:
        self.get_application().open_workspace(workspace)
        self.close()
