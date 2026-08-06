"""Workspace launcher window.

The small window shown at startup (and from the sidebar's Workspaces
button): pick an existing workspace or create a new one by name and
identity colour. Activating a workspace asks the application to open
its main window, waits for that window to appear, and only then closes
itself and raises it — otherwise the workspace opens behind whatever
was under the launcher (see _open). Each row shows its colour as a dot
beside the name and has an edit button for both. The list is
rebuilt every time the window is mapped so it stays in sync with
workspaces created elsewhere.
"""

from __future__ import annotations

from gi.repository import Adw, GLib, Gtk

from sqlide.backend import identity
from sqlide.backend.workspaces import Workspace
from sqlide.frontend import identity as identity_ui
from sqlide.frontend.util import describe, main_menu_button


class WorkspaceLauncher(Adw.ApplicationWindow):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.set_title("sqlide")
        self.set_default_size(400, 480)
        self._store_error_shown = False

        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Label(label="Workspaces"))
        new_button = Gtk.Button(icon_name="list-add-symbolic")
        describe(new_button, "New workspace")
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
        # Colour is never the only cue: the dot sits next to the name.
        row.add_prefix(identity_ui.dot(workspace.color))
        edit = Gtk.Button(icon_name="document-edit-symbolic")
        describe(edit, "Edit workspace name and colour")
        edit.add_css_class("flat")
        edit.set_valign(Gtk.Align.CENTER)
        edit.connect("clicked", lambda *_: self._edit_workspace(workspace))
        row.add_suffix(edit)
        row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        row.connect("activated", lambda *_: self._open(workspace))
        return row

    def _edit_workspace(self, workspace: Workspace) -> None:
        dialog = Adw.AlertDialog(
            heading="Edit Workspace",
            body="Connections and open tabs stay the same.",
        )
        name, color = self._identity_fields(workspace.name, workspace.color)
        dialog.set_extra_child(self._identity_group(name, color))
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("save", "Save")
        dialog.set_response_appearance(
            "save", Adw.ResponseAppearance.SUGGESTED
        )
        dialog.set_default_response("save")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._edit_response, workspace, name, color)
        dialog.present(self)

    def _edit_response(
        self,
        _dialog,
        response: str,
        workspace: Workspace,
        name: Adw.EntryRow,
        color: identity_ui.ColorRow,
    ) -> None:
        if response != "save":
            return
        text = name.get_text().strip()
        if text:
            workspace.name = text
        workspace.color = color.get_color()
        self.get_application().workspace_store.save(workspace)
        self._refresh()

    def _new_workspace(self) -> None:
        dialog = Adw.AlertDialog(
            heading="New Workspace",
            body="Connections and open tabs are kept per workspace.",
        )
        name, color = self._identity_fields("", identity.NONE)
        dialog.set_extra_child(self._identity_group(name, color))
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("create", "Create")
        dialog.set_response_appearance(
            "create", Adw.ResponseAppearance.SUGGESTED
        )
        dialog.set_default_response("create")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._create_response, name, color)
        dialog.present(self)

    def _create_response(
        self,
        _dialog,
        response: str,
        name: Adw.EntryRow,
        color: identity_ui.ColorRow,
    ) -> None:
        if response != "create":
            return
        workspace = self.get_application().workspace_store.create(
            name.get_text().strip() or "Workspace", color.get_color()
        )
        self._open(workspace)

    @staticmethod
    def _identity_fields(
        name: str, color: str
    ) -> tuple[Adw.EntryRow, identity_ui.ColorRow]:
        name_row = Adw.EntryRow(title="Name", text=name)
        color_row = identity_ui.ColorRow(
            subtitle="Tints this workspace's window and launcher row"
        )
        color_row.set_color(color)
        return name_row, color_row

    @staticmethod
    def _identity_group(
        name: Adw.EntryRow, color: identity_ui.ColorRow
    ) -> Gtk.Widget:
        group = Adw.PreferencesGroup()
        group.add(name)
        group.add(color)
        return group

    def _open(self, workspace: Workspace) -> None:
        """Open a workspace and hand it the foreground.

        The order matters. Closing the launcher first gives focus back
        to whatever was behind it, and the workspace window — mapped a
        moment later, without a user event of its own to point at —
        stays where the compositor first put it, which is behind
        everything. So: open it, wait until it is on screen, then close
        the launcher and present it once more, this time as the only
        window of the app that wants attention."""
        window = self.get_application().open_workspace(workspace)

        def foreground() -> bool:
            self.close()
            window.present()
            return GLib.SOURCE_REMOVE

        if window.get_mapped():
            GLib.idle_add(foreground)
            return
        handler = 0

        def mapped(*_args) -> None:
            window.disconnect(handler)
            GLib.idle_add(foreground)

        handler = window.connect("map", mapped)
