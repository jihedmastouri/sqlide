"""The file dialogs behind Export / Import (backend/exchange.py).

Kept out of the window and the launcher because both need the same
three things: ask where, read or write the XML, and say plainly what
happened. Everything user-visible about the transfer format — that
passwords are left out unless asked for, that importing never
overwrites an existing workspace — is decided here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from gi.repository import Adw, Gio, GLib, Gtk

from sqlide.backend import exchange
from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.workspaces import Workspace
from sqlide.i18n import _

_FILTER_NAME = "sqlide export (*.xml)"


def _xml_filters() -> Gio.ListStore:
    xml = Gtk.FileFilter(name=_FILTER_NAME)
    xml.add_suffix("xml")
    xml.add_mime_type("application/xml")
    every = Gtk.FileFilter(name="All files")
    every.add_pattern("*")
    filters = Gio.ListStore.new(Gtk.FileFilter)
    filters.append(xml)
    filters.append(every)
    return filters


def export_workspace(
    parent: Gtk.Widget,
    workspace: Workspace,
    on_done: Callable[[str], None],
    on_error: Callable[[str], None],
) -> None:
    """Ask whether to include passwords, then where to write, then
    write. The question comes first because it changes what the file
    is: with passwords it is a secret, without it is not."""

    def ask_where(include_passwords: bool) -> None:
        dialog = Gtk.FileDialog(
            title=_("Export Workspace"),
            initial_name=f"{_slug(workspace.name)}.xml",
        )
        dialog.set_filters(_xml_filters())

        def finished(dialog: Gtk.FileDialog, result) -> None:
            try:
                file = dialog.save_finish(result)
            except GLib.Error:
                return  # cancelled
            path = Path(file.get_path())
            try:
                exchange.write(
                    path,
                    exchange.workspace_to_xml(
                        workspace, include_passwords=include_passwords
                    ),
                )
            except exchange.ExchangeError as exc:
                on_error(str(exc))
                return
            on_done(f"Exported “{workspace.name}” to {path}")

        dialog.save(_root(parent), None, finished)

    _ask_about_passwords(parent, workspace, ask_where)


def export_connections(
    parent: Gtk.Widget,
    workspace: Workspace,
    on_done: Callable[[str], None],
    on_error: Callable[[str], None],
) -> None:
    """The connections alone, for merging into someone else's
    workspace."""

    def ask_where(include_passwords: bool) -> None:
        dialog = Gtk.FileDialog(
            title=_("Export Connections"),
            initial_name=f"{_slug(workspace.name)}-connections.xml",
        )
        dialog.set_filters(_xml_filters())

        def finished(dialog: Gtk.FileDialog, result) -> None:
            try:
                file = dialog.save_finish(result)
            except GLib.Error:
                return
            path = Path(file.get_path())
            try:
                exchange.write(
                    path,
                    exchange.connections_to_xml(
                        workspace.connections,
                        include_passwords=include_passwords,
                    ),
                )
            except exchange.ExchangeError as exc:
                on_error(str(exc))
                return
            count = len(workspace.connections)
            on_done(f"Exported {count} connection(s) to {path}")

        dialog.save(_root(parent), None, finished)

    _ask_about_passwords(parent, workspace, ask_where)


def import_workspace(
    parent: Gtk.Widget,
    on_imported: Callable[[Workspace], None],
    on_error: Callable[[str], None],
) -> None:
    """Read a file as a new workspace. Never merges into an existing
    one: an import that quietly changed a workspace already on the
    machine would be the one mistake nobody can undo."""

    def loaded(text: str) -> None:
        try:
            workspace = exchange.workspace_from_xml(text)
        except exchange.ExchangeError as exc:
            on_error(str(exc))
            return
        on_imported(workspace)

    _open_file(parent, "Import Workspace", loaded, on_error)


def import_connections(
    parent: Gtk.Widget,
    on_imported: Callable[[list[ConnectionProfile]], None],
    on_error: Callable[[str], None],
) -> None:
    """Read the connections out of a file, for adding to the workspace
    that is already open."""

    def loaded(text: str) -> None:
        try:
            profiles = exchange.connections_from_xml(text)
        except exchange.ExchangeError as exc:
            on_error(str(exc))
            return
        if not profiles:
            on_error("That file contains no connections")
            return
        on_imported(profiles)

    _open_file(parent, "Import Connections", loaded, on_error)


# Internals


def _ask_about_passwords(
    parent: Gtk.Widget, workspace: Workspace, then: Callable[[bool], None]
) -> None:
    """Passwords are the one thing an export can leak, so it is a
    decision, not a checkbox buried in a file dialog. Workspaces with
    no password stored anywhere skip the question."""
    if not any(
        profile.password or profile.ssh_password
        for profile in workspace.connections
    ):
        then(False)
        return
    dialog = Adw.AlertDialog(
        heading=_("Include passwords?"),
        body=_("Passwords are stored in plain text in the exported file. "
        "Without them the file is safe to send, and each connection "
        "asks for its password once on the machine it lands on."),
    )
    dialog.add_response("cancel", _("Cancel"))
    dialog.add_response("without", _("Without Passwords"))
    dialog.add_response("with", _("Include Passwords"))
    dialog.set_response_appearance(
        "with", Adw.ResponseAppearance.DESTRUCTIVE
    )
    dialog.set_default_response("without")
    dialog.set_close_response("cancel")
    dialog.connect(
        "response",
        lambda _d, response: (
            then(response == "with") if response != "cancel" else None
        ),
    )
    dialog.present(parent)


def _open_file(
    parent: Gtk.Widget,
    title: str,
    on_text: Callable[[str], None],
    on_error: Callable[[str], None],
) -> None:
    dialog = Gtk.FileDialog(title=title)
    dialog.set_filters(_xml_filters())

    def finished(dialog: Gtk.FileDialog, result) -> None:
        try:
            file = dialog.open_finish(result)
        except GLib.Error:
            return  # cancelled
        try:
            text = exchange.read(Path(file.get_path()))
        except exchange.ExchangeError as exc:
            on_error(str(exc))
            return
        on_text(text)

    dialog.open(_root(parent), None, finished)


def _root(widget: Gtk.Widget) -> Gtk.Window | None:
    root = widget.get_root()
    return root if isinstance(root, Gtk.Window) else None


def _slug(name: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in "-_" else "-" for c in name)
    return cleaned.strip("-").lower() or "workspace"
