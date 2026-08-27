"""Where backups are written: the destination list and its editor.

A destination is a machine-level thing (a folder, a bucket, a backup
server), not a workspace one, so it is managed here in a window of its
own rather than inside any single job — several jobs point at the same
destination, and changing the bucket's key should not mean editing
five jobs.

The form shows only the fields the chosen kind actually uses; Test
proves the credentials before a scheduled job discovers at 2am that
they were wrong.
"""

from __future__ import annotations

from typing import Callable

from gi.repository import Adw, Gtk

from sqlide.backend.backups import targets
from sqlide.backend.backups.jobs import (
    DESTINATION_KINDS,
    FTP,
    LOCAL,
    S3,
    SFTP,
    BackupStore,
    Destination,
)
from sqlide.frontend.util import run_async
from sqlide.i18n import _

_KIND_LABELS = {
    LOCAL: "This machine",
    S3: "S3-compatible storage",
    SFTP: "SFTP",
    FTP: "FTP / FTPS",
}


class DestinationsWindow(Adw.Window):
    """The destination list: add, edit, remove."""

    def __init__(
        self,
        store: BackupStore,
        on_changed: Callable[[], None],
        **kwargs,
    ) -> None:
        super().__init__(
            title=_("Backup Destinations"),
            default_width=520,
            default_height=560,
            **kwargs,
        )
        self._store = store
        self._on_changed = on_changed
        # PreferencesGroup has no "remove everything", so the rows it
        # currently holds are tracked here and removed by hand.
        self._rows: list[Gtk.Widget] = []

        self._group = Adw.PreferencesGroup(
            title=_("Destinations"),
            description="Where backup files are written. A destination "
            "can be shared by any number of jobs.",
        )
        page = Adw.PreferencesPage()
        page.add(self._group)

        add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text=_("Add"))
        add.connect("clicked", lambda *_: self._edit(None))
        header = Adw.HeaderBar()
        header.pack_start(add)

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(page)
        self.set_content(view)
        self._reload()

    def _reload(self) -> None:
        for row in self._rows:
            self._group.remove(row)
        self._rows = []
        if not self._store.destinations:
            empty = Adw.ActionRow(
                title=_("No destinations yet"),
                subtitle=_(
                    "Add a folder, a bucket or a server to back up to."
                ),
            )
            self._group.add(empty)
            self._rows.append(empty)
            return
        for destination in self._store.destinations:
            row = Adw.ActionRow(
                title=destination.name,
                subtitle=destination.describe(),
                activatable=True,
            )
            row.add_prefix(Gtk.Image(icon_name=_icon(destination.kind)))
            remove = Gtk.Button(
                icon_name="user-trash-symbolic",
                valign=Gtk.Align.CENTER,
                tooltip_text=_("Remove"),
            )
            remove.add_css_class("flat")
            remove.connect(
                "clicked", lambda _b, d=destination: self._remove(d)
            )
            row.add_suffix(remove)
            row.connect("activated", lambda _r, d=destination: self._edit(d))
            self._group.add(row)
            self._rows.append(row)

    def _remove(self, destination: Destination) -> None:
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading=f"Remove {destination.name}?",
            body=_("Jobs pointing at it keep their history but will need a "
            "new destination before they can run. Nothing already "
            "written there is deleted."),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("remove", _("Remove"))
        dialog.set_response_appearance(
            "remove", Adw.ResponseAppearance.DESTRUCTIVE
        )

        def answered(_dialog, response: str) -> None:
            if response == "remove":
                self._store.remove_destination(destination.id)
                self._reload()
                self._on_changed()

        dialog.connect("response", answered)
        dialog.present()

    def _edit(self, destination: Destination | None) -> None:
        def saved() -> None:
            self._reload()
            self._on_changed()

        DestinationEditor(self._store, destination, saved,
                          transient_for=self, modal=True).present()


def _icon(kind: str) -> str:
    return {
        LOCAL: "folder-symbolic",
        S3: "network-server-symbolic",
        SFTP: "network-transmit-receive-symbolic",
        FTP: "network-transmit-receive-symbolic",
    }.get(kind, "folder-symbolic")


class DestinationEditor(Adw.Window):
    """One destination's form. Fields appear per kind."""

    def __init__(
        self,
        store: BackupStore,
        destination: Destination | None,
        on_saved: Callable[[], None],
        **kwargs,
    ) -> None:
        super().__init__(
            title=_("Destination"),
            default_width=480,
            default_height=560,
            **kwargs,
        )
        self._store = store
        self._on_saved = on_saved
        self._new = destination is None
        self._destination = destination or Destination("New destination")

        page = Adw.PreferencesPage()

        general = Adw.PreferencesGroup()
        self._name = Adw.EntryRow(title=_("Name"), text=self._destination.name)
        general.add(self._name)
        self._kind = Adw.ComboRow(
            title=_("Kind"),
            model=Gtk.StringList.new(
                [_KIND_LABELS[k] for k in DESTINATION_KINDS]
            ),
            selected=DESTINATION_KINDS.index(self._destination.kind)
            if self._destination.kind in DESTINATION_KINDS
            else 0,
        )
        self._kind.connect("notify::selected", lambda *_: self._show_kind())
        general.add(self._kind)
        page.add(general)

        d = self._destination
        self._local = Adw.PreferencesGroup(title=_("Folder"))
        self._path_local = Adw.EntryRow(
            title=_("Directory"), text=d.path if d.kind == LOCAL else ""
        )
        browse = Gtk.Button(
            icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER
        )
        browse.add_css_class("flat")
        browse.connect("clicked", lambda *_: self._browse())
        self._path_local.add_suffix(browse)
        self._local.add(self._path_local)
        page.add(self._local)

        self._s3 = Adw.PreferencesGroup(
            title=_("Bucket"),
            description="Any S3-compatible service: AWS, MinIO, Backblaze "
            "B2, Cloudflare R2, Wasabi. Leave the endpoint empty for AWS.",
        )
        self._bucket = Adw.EntryRow(title=_("Bucket"), text=d.bucket)
        self._endpoint = Adw.EntryRow(
            title=_("Endpoint URL"), text=d.endpoint_url
        )
        self._region = Adw.EntryRow(title=_("Region"), text=d.region)
        self._access_key = Adw.EntryRow(
            title=_("Access key"), text=d.access_key
        )
        self._secret_key = Adw.PasswordEntryRow(
            title=_("Secret key"), text=d.secret_key
        )
        self._prefix = Adw.EntryRow(
            title=_("Key prefix"), text=d.path if d.kind != LOCAL else ""
        )
        for row in (
            self._bucket, self._endpoint, self._region,
            self._access_key, self._secret_key, self._prefix,
        ):
            self._s3.add(row)
        page.add(self._s3)

        self._server = Adw.PreferencesGroup(title=_("Server"))
        self._host = Adw.EntryRow(title=_("Host"), text=d.host)
        self._port = Adw.SpinRow(
            title=_("Port"),
            subtitle=_("0 uses the default (22 for SFTP, 21 for FTP)"),
            adjustment=Gtk.Adjustment(
                lower=0, upper=65535, step_increment=1, value=d.port
            ),
        )
        self._user = Adw.EntryRow(title=_("User"), text=d.user)
        self._password = Adw.PasswordEntryRow(
            title=_("Password"), text=d.password
        )
        self._key_path = Adw.EntryRow(
            title=_("Private key file"),
            text=d.key_path,
        )
        self._tls = Adw.SwitchRow(
            title=_("Use TLS (FTPS)"),
            subtitle=_("Plain FTP sends the password and the dump in the "
            "clear. Leave this on unless the server cannot do TLS."),
            active=d.tls,
        )
        self._remote_dir = Adw.EntryRow(
            title=_("Remote directory"), text=d.path if d.kind != LOCAL else ""
        )
        for row in (
            self._host, self._port, self._user, self._password,
            self._key_path, self._tls, self._remote_dir,
        ):
            self._server.add(row)
        page.add(self._server)

        self._status = Gtk.Label(xalign=0, wrap=True, margin_start=12,
                                 margin_end=12, margin_bottom=12)
        self._status.add_css_class("dim-label")

        test = Gtk.Button(label=_("Test"))
        test.connect("clicked", lambda *_: self._test())
        save = Gtk.Button(label=_("Save"))
        save.add_css_class("suggested-action")
        save.connect("clicked", lambda *_: self._save())
        header = Adw.HeaderBar()
        header.pack_start(test)
        header.pack_end(save)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(page)
        content.append(self._status)
        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(content)
        self.set_content(view)
        self._show_kind()

    def _current_kind(self) -> str:
        return DESTINATION_KINDS[self._kind.get_selected()]

    def _show_kind(self) -> None:
        kind = self._current_kind()
        self._local.set_visible(kind == LOCAL)
        self._s3.set_visible(kind == S3)
        self._server.set_visible(kind in (SFTP, FTP))
        self._key_path.set_visible(kind == SFTP)
        self._tls.set_visible(kind == FTP)
        missing = targets.missing_dependency(kind)
        if missing:
            self._set_status(missing, error=True)
        else:
            self._set_status("")

    def _browse(self) -> None:
        dialog = Gtk.FileDialog(title=_("Backup Folder"))

        def picked(dialog: Gtk.FileDialog, result) -> None:
            try:
                folder = dialog.select_folder_finish(result)
            except Exception:
                return  # cancelled
            self._path_local.set_text(folder.get_path() or "")

        dialog.select_folder(self, None, picked)

    def _collect(self) -> Destination:
        kind = self._current_kind()
        d = self._destination
        d.name = self._name.get_text().strip() or "Destination"
        d.kind = kind
        if kind == LOCAL:
            d.path = self._path_local.get_text().strip()
        elif kind == S3:
            d.path = self._prefix.get_text().strip()
            d.bucket = self._bucket.get_text().strip()
            d.endpoint_url = self._endpoint.get_text().strip()
            d.region = self._region.get_text().strip()
            d.access_key = self._access_key.get_text().strip()
            d.secret_key = self._secret_key.get_text()
        else:
            d.path = self._remote_dir.get_text().strip()
            d.host = self._host.get_text().strip()
            d.port = int(self._port.get_value())
            d.user = self._user.get_text().strip()
            d.password = self._password.get_text()
            d.key_path = self._key_path.get_text().strip()
            d.tls = self._tls.get_active()
        return d

    def _test(self) -> None:
        destination = self._collect()
        self._set_status("Testing…")
        run_async(
            lambda: targets.open_target(destination).check(),
            lambda summary: self._set_status(summary),
            lambda exc: self._set_status(str(exc), error=True),
        )

    def _save(self) -> None:
        destination = self._collect()
        if self._new:
            self._store.add_destination(destination)
        else:
            self._store.save()
        self._on_saved()
        self.close()

    def _set_status(self, text: str, error: bool = False) -> None:
        self._status.set_text(text)
        self._status.set_visible(bool(text))
        if error:
            self._status.add_css_class("error")
            self._status.remove_css_class("dim-label")
        else:
            self._status.remove_css_class("error")
            self._status.add_css_class("dim-label")
