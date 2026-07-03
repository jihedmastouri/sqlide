"""New-connection dialog.

An Adw.Dialog (attached to the main window, Esc to dismiss) with a
kind dropdown (SQLite / MySQL / PostgreSQL / JDBC); the visible field
group follows the kind. "Test connection" opens and closes a throwaway
connector on a worker thread.
"""

from __future__ import annotations

import os
from typing import Callable

from gi.repository import Adw, GLib, Gtk

from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.db import registry
from sqlide.backend.db.base import ConnectorError
from sqlide.frontend.util import run_async

KIND_LABELS = ["SQLite", "MySQL", "PostgreSQL", "JDBC (generic)"]
KIND_IDS = ["sqlite", "mysql", "postgres", "jdbc"]


class ConnectionDialog(Adw.Dialog):
    def __init__(
        self,
        on_save: Callable[[ConnectionProfile], None],
    ) -> None:
        super().__init__(
            title="New Connection",
            content_width=460,
            content_height=600,
        )
        self._on_save = on_save

        header = Adw.HeaderBar()
        header.set_show_start_title_buttons(False)
        header.set_show_end_title_buttons(False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        save = Gtk.Button(label="Save")
        save.add_css_class("suggested-action")
        save.connect("clicked", self._save)
        header.pack_start(cancel)
        header.pack_end(save)

        self._name = Adw.EntryRow(title="Name")
        self._kind = Adw.ComboRow(
            title="Type", model=Gtk.StringList.new(KIND_LABELS)
        )
        self._kind.connect("notify::selected", self._on_kind_changed)
        general = Adw.PreferencesGroup()
        general.add(self._name)
        general.add(self._kind)

        # SQLite
        self._file = Adw.EntryRow(title="Database file")
        browse = Gtk.Button(icon_name="document-open-symbolic")
        browse.set_tooltip_text("Browse…")
        browse.add_css_class("flat")
        browse.set_valign(Gtk.Align.CENTER)
        browse.connect("clicked", self._browse)
        self._file.add_suffix(browse)
        self._sqlite_group = Adw.PreferencesGroup(title="SQLite")
        self._sqlite_group.add(self._file)

        # MySQL / PostgreSQL
        self._host = Adw.EntryRow(title="Host", text="localhost")
        self._port = Adw.EntryRow(title="Port (blank for default)")
        self._user = Adw.EntryRow(title="User")
        self._password = Adw.PasswordEntryRow(title="Password")
        self._database = Adw.EntryRow(title="Database")
        self._server_group = Adw.PreferencesGroup(title="Server")
        for row in (self._host, self._port, self._user, self._password, self._database):
            self._server_group.add(row)

        # JDBC
        self._jdbc_url = Adw.EntryRow(title="JDBC URL (jdbc:…)")
        self._driver_class = Adw.EntryRow(title="Driver class (e.g. org.h2.Driver)")
        self._jar_path = Adw.EntryRow(title="Driver jar path")
        self._jdbc_user = Adw.EntryRow(title="User")
        self._jdbc_password = Adw.PasswordEntryRow(title="Password")
        self._jdbc_group = Adw.PreferencesGroup(title="JDBC")
        for row in (
            self._jdbc_url,
            self._driver_class,
            self._jar_path,
            self._jdbc_user,
            self._jdbc_password,
        ):
            self._jdbc_group.add(row)

        self._test_button = Gtk.Button(
            label="Test connection", halign=Gtk.Align.CENTER
        )
        self._test_button.add_css_class("pill")
        self._test_button.connect("clicked", self._test)
        test_group = Adw.PreferencesGroup()
        test_group.add(self._test_button)

        page = Adw.PreferencesPage()
        for group in (
            general,
            self._sqlite_group,
            self._server_group,
            self._jdbc_group,
            test_group,
        ):
            page.add(group)

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(page)
        self._toasts = Adw.ToastOverlay(child=view)
        self.set_child(self._toasts)
        self._on_kind_changed()

    def _kind_id(self) -> str:
        return KIND_IDS[self._kind.get_selected()]

    def _on_kind_changed(self, *_args) -> None:
        kind = self._kind_id()
        self._sqlite_group.set_visible(kind == "sqlite")
        self._server_group.set_visible(kind in ("mysql", "postgres"))
        self._jdbc_group.set_visible(kind == "jdbc")

    def _browse(self, *_args) -> None:
        dialog = Gtk.FileDialog(title="Select database file")
        root = self.get_root()
        parent = root if isinstance(root, Gtk.Window) else None
        dialog.open(parent, None, self._browse_finished)

    def _browse_finished(self, dialog, result) -> None:
        try:
            file = dialog.open_finish(result)
        except GLib.Error:
            return  # cancelled
        if file is not None:
            self._file.set_text(file.get_path() or "")

    def _build_profile(self) -> ConnectionProfile:
        kind = self._kind_id()
        try:
            port = int(self._port.get_text().strip())
        except ValueError:
            port = 0
        name = self._name.get_text().strip()
        if not name:
            if kind == "sqlite":
                name = os.path.basename(self._file.get_text().strip()) or "sqlite"
            elif kind == "jdbc":
                name = self._jdbc_url.get_text().strip() or "jdbc"
            else:
                name = self._database.get_text().strip() or kind
        jdbc = kind == "jdbc"
        return ConnectionProfile(
            name=name,
            kind=kind,
            file_path=self._file.get_text().strip(),
            host=self._host.get_text().strip() or "localhost",
            port=port,
            user=(self._jdbc_user if jdbc else self._user).get_text().strip(),
            password=(self._jdbc_password if jdbc else self._password).get_text(),
            database=self._database.get_text().strip(),
            jdbc_url=self._jdbc_url.get_text().strip(),
            driver_class=self._driver_class.get_text().strip(),
            jar_path=self._jar_path.get_text().strip(),
        )

    def _test(self, *_args) -> None:
        profile = self._build_profile()
        self._test_button.set_sensitive(False)

        def work():
            if not registry.driver_available(profile.kind):
                raise ConnectorError(
                    f"No driver installed for {profile.kind} connections"
                )
            connector = registry.create_connector(
                profile.kind, **profile.connect_params()
            )
            connector.connect()
            connector.close()

        def done(_result):
            self._test_button.set_sensitive(True)
            self._toasts.add_toast(Adw.Toast(title="Connection OK"))

        def failed(exc):
            self._test_button.set_sensitive(True)
            self._toasts.add_toast(Adw.Toast(title=str(exc)))

        run_async(work, done, failed)

    def _save(self, *_args) -> None:
        self._on_save(self._build_profile())
        self.close()
