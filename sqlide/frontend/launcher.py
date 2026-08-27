"""Workspace launcher window.

The small window opened from the sidebar's Workspaces button: pick
another workspace or create a new one by name and identity colour.
Startup goes straight to a workspace instead (see application.py), so
this is a switcher, not a front door — it is normally shown over a
workspace window that stays open behind it. A first run, with no
workspace to switch to, gets the home page instead (welcome.py).

Activating a workspace opens its window and closes this one, in the
careful order util.open_workspace_from explains. Each row shows its
colour as a dot beside the name and has an edit button for both. The
list is rebuilt every time the window is mapped so it stays in sync
with workspaces created elsewhere.
"""

from __future__ import annotations

from gi.repository import Adw, Gtk

from sqlide.backend import identity
from sqlide.backend.workspaces import Workspace
from sqlide.frontend import identity as identity_ui
from sqlide.frontend import transfer
from sqlide.frontend.util import (
    describe,
    main_menu_button,
    open_workspace_from,
)
from sqlide.i18n import _, ngettext


def _unique_name(name: str, taken: list[str]) -> str:
    """Imported workspaces keep their name unless one like it is
    already listed — two rows with the same title would be a puzzle,
    not a convenience."""
    if name not in taken:
        return name
    n = 2
    while f"{name} ({n})" in taken:
        n += 1
    return f"{name} ({n})"


class WorkspaceLauncher(Adw.ApplicationWindow):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.set_title("sqlide")
        self.set_default_size(400, 480)

        header = Adw.HeaderBar()
        header.add_css_class("flat")
        header.set_title_widget(Gtk.Label(label=_("Workspaces")))
        new_button = Gtk.Button(icon_name="list-add-symbolic")
        describe(new_button, _("New workspace"))
        new_button.connect("clicked", lambda *_: self._new_workspace())
        header.pack_start(new_button)
        import_button = Gtk.Button(icon_name="document-open-symbolic")
        describe(import_button, _("Import a workspace from an XML file"))
        import_button.connect("clicked", lambda *_: self._import_workspace())
        header.pack_start(import_button)
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

        create_button = Gtk.Button(label=_("Create Workspace"))
        create_button.add_css_class("suggested-action")
        create_button.add_css_class("pill")
        create_button.connect("clicked", lambda *_: self._new_workspace())
        empty = Adw.StatusPage(
            icon_name="folder-symbolic",
            title=_("No workspaces yet"),
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
        if (error := app.take_store_error()) is not None:
            self._toasts.add_toast(Adw.Toast(title=error))

        while (row := self._list.get_row_at_index(0)) is not None:
            self._list.remove(row)
        workspaces = app.workspace_store.workspaces
        for workspace in workspaces:
            self._list.append(self._workspace_row(workspace))
        self._stack.set_visible_child_name("list" if workspaces else "empty")

    def _workspace_row(self, workspace: Workspace) -> Adw.ActionRow:
        count = len(workspace.connections)
        subtitle = (
            _("no connections")
            if count == 0
            else ngettext("%d connection", "%d connections", count) % count
        )
        row = Adw.ActionRow(
            title=workspace.name,
            subtitle=subtitle,
            activatable=True,
        )
        # Colour is never the only cue: the dot sits next to the name.
        row.add_prefix(identity_ui.dot(workspace.color))
        edit = Gtk.Button(icon_name="document-edit-symbolic")
        describe(edit, _("Edit workspace name and colour"))
        edit.add_css_class("flat")
        edit.set_valign(Gtk.Align.CENTER)
        edit.connect("clicked", lambda *_: self._edit_workspace(workspace))
        row.add_suffix(edit)
        row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        row.connect("activated", lambda *_: self._open(workspace))
        return row

    def _edit_workspace(self, workspace: Workspace) -> None:
        dialog = Adw.AlertDialog(heading=_("Edit Workspace"))
        name, color = self._identity_fields(workspace.name, workspace.color)
        dialog.set_extra_child(self._identity_group(name, color))
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("save", _("Save"))
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
        dialog = Adw.AlertDialog(heading=_("New Workspace"))
        name, color = self._identity_fields("", identity.NONE)
        dialog.set_extra_child(self._identity_group(name, color))
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("create", _("Create"))
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
        store = self.get_application().workspace_store
        workspace = store.create(
            name.get_text().strip() or store.default_name(), color.get_color()
        )
        self._open(workspace)

    @staticmethod
    def _identity_fields(
        name: str, color: str
    ) -> tuple[Adw.EntryRow, identity_ui.ColorRow]:
        name_row = Adw.EntryRow(title=_("Name"), text=name)
        color_row = identity_ui.ColorRow(subtitle=_("Window colour"))
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

    def _import_workspace(self) -> None:
        """Read a workspace out of an exported XML file. It arrives as
        a new workspace with its own id, so it never lands on top of
        one that is already here — copying a setup between machines and
        re-importing the same file are then the same, safe operation."""
        transfer.import_workspace(self, self._workspace_imported, self._toast)

    def _workspace_imported(self, workspace: Workspace) -> None:
        store = self.get_application().workspace_store
        workspace.name = _unique_name(
            workspace.name, [w.name for w in store.workspaces]
        )
        store.workspaces.append(workspace)
        try:
            store.save(workspace)
        except Exception as exc:
            self._toast(f"Could not save the imported workspace: {exc}")
            return
        self._refresh()
        count = len(workspace.connections)
        self._toast(f"Imported “{workspace.name}” ({count} connection(s))")

    def _toast(self, message: str) -> None:
        self._toasts.add_toast(Adw.Toast(title=message))

    def _open(self, workspace: Workspace) -> None:
        open_workspace_from(self, workspace)
