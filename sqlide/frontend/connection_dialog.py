"""New/edit connection dialog.

An Adw.Dialog (attached to the main window, Esc to dismiss) with a
kind dropdown (SQLite / MySQL / PostgreSQL / JDBC); the visible field
group follows the kind. Above the credentials sits the Identity group
(colour + environment class): a connection that looks like production
gets a *suggestion* row there, which the user accepts or dismisses —
never an automatic classification, because a wrong one teaches people
the badge lies. "Test connection" opens and closes a throwaway
connector on a worker thread. Passing an existing `profile` switches
the dialog to edit mode: the title changes and every field is
pre-filled from it, but `on_save` still just receives a freshly built
ConnectionProfile — the caller decides how to apply it (see
window.py's `_connection_edited`).
"""

from __future__ import annotations

import os
from typing import Callable

from gi.repository import Adw, GLib, Gtk

from sqlide.backend import identity
from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.db import registry
from sqlide.backend.db.base import ConnectorError
from sqlide.frontend import identity as identity_ui
from sqlide.frontend.util import run_async

KIND_LABELS = ["SQLite", "MySQL", "PostgreSQL", "JDBC (generic)"]
KIND_IDS = ["sqlite", "mysql", "postgres", "jdbc"]

# Label -> ConnectionProfile.ssl_mode value ("" = driver default).
SSL_MODE_LABELS = {
    "Default": "",
    "Disable": "disable",
    "Require": "require",
    "Verify CA": "verify-ca",
    "Verify Full": "verify-full",
}
SSL_MODE_IDS = list(SSL_MODE_LABELS.values())


class ConnectionDialog(Adw.Dialog):
    def __init__(
        self,
        on_save: Callable[[ConnectionProfile], None],
        profile: ConnectionProfile | None = None,
    ) -> None:
        super().__init__(
            title="Edit Connection" if profile is not None else "New Connection",
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

        # Identity: what this connection looks like in the sidebar and
        # the status bar, and how much friction destructive statements
        # get. Above the credentials on purpose — it is the first thing
        # you set, not an afterthought buried under SSL.
        self._color = identity_ui.ColorRow(
            subtitle="Marks this connection's rows, tabs and status bar"
        )
        self._environment = identity_ui.EnvironmentRow()
        self._environment.connect(
            "notify::selected", lambda *_: self._refresh_suggestion()
        )
        self._suggestion = Adw.ActionRow(visible=False)
        self._suggestion.add_prefix(
            Gtk.Image(icon_name="dialog-warning-symbolic")
        )
        accept = Gtk.Button(label="Set Production", valign=Gtk.Align.CENTER)
        accept.connect("clicked", self._accept_suggestion)
        dismiss = Gtk.Button(
            icon_name="window-close-symbolic", valign=Gtk.Align.CENTER
        )
        dismiss.add_css_class("flat")
        dismiss.set_tooltip_text("Dismiss this suggestion")
        dismiss.connect("clicked", self._dismiss_suggestion)
        self._suggestion.add_suffix(accept)
        self._suggestion.add_suffix(dismiss)
        self._suggestion_dismissed = False
        identity_group = Adw.PreferencesGroup(title="Identity")
        identity_group.add(self._color)
        identity_group.add(self._environment)
        identity_group.add(self._suggestion)
        self._kind.connect(
            "notify::selected", lambda *_: self._refresh_suggestion()
        )

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
        # Never classified silently: the fields that make a connection
        # look like production only ever re-run the *suggestion*.
        for row in (self._name, self._host, self._database):
            row.connect("changed", lambda *_: self._refresh_suggestion())

        # Advanced (MySQL / PostgreSQL): SSL and SSH tunnel
        self._ssl_mode = Adw.ComboRow(
            title="SSL mode",
            subtitle="Default keeps the driver's behaviour",
            model=Gtk.StringList.new(list(SSL_MODE_LABELS)),
        )
        self._ssl_ca = self._file_entry_row("CA certificate (PEM)")
        self._ssl_cert = self._file_entry_row("Client certificate (PEM)")
        self._ssl_key = self._file_entry_row("Client key (PEM)")
        self._ssl_group = Adw.PreferencesGroup(title="SSL")
        for row in (self._ssl_mode, self._ssl_ca, self._ssl_cert, self._ssl_key):
            self._ssl_group.add(row)

        self._ssh_enable = Adw.ExpanderRow(
            title="SSH tunnel",
            subtitle="Connect through an SSH host",
            show_enable_switch=True,
            enable_expansion=False,
        )
        self._ssh_host = Adw.EntryRow(title="SSH host")
        self._ssh_port = Adw.EntryRow(title="SSH port (blank for 22)")
        self._ssh_user = Adw.EntryRow(title="SSH user")
        self._ssh_password = Adw.PasswordEntryRow(
            title="SSH password (needs the sshtunnel package)"
        )
        self._ssh_key = self._file_entry_row("SSH private key file")
        for row in (
            self._ssh_host,
            self._ssh_port,
            self._ssh_user,
            self._ssh_password,
            self._ssh_key,
        ):
            self._ssh_enable.add_row(row)
        self._ssh_group = Adw.PreferencesGroup(title="SSH")
        self._ssh_group.add(self._ssh_enable)

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
            identity_group,
            self._sqlite_group,
            self._server_group,
            self._ssl_group,
            self._ssh_group,
            self._jdbc_group,
            test_group,
        ):
            page.add(group)

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(page)
        self._toasts = Adw.ToastOverlay(child=view)
        self.set_child(self._toasts)
        if profile is not None:
            self._prefill(profile)
        self._on_kind_changed()
        self._refresh_suggestion()

    def _prefill(self, profile: ConnectionProfile) -> None:
        self._name.set_text(profile.name)
        self._color.set_color(profile.color)
        self._environment.set_environment(profile.environment)
        self._kind.set_selected(KIND_IDS.index(profile.kind))
        self._file.set_text(profile.file_path)
        self._host.set_text(profile.host)
        self._port.set_text(str(profile.port) if profile.port else "")
        self._user.set_text(profile.user)
        self._password.set_text(profile.password)
        self._database.set_text(profile.database)
        self._jdbc_url.set_text(profile.jdbc_url)
        self._driver_class.set_text(profile.driver_class)
        self._jar_path.set_text(profile.jar_path)
        self._jdbc_user.set_text(profile.user)
        self._jdbc_password.set_text(profile.password)
        if profile.ssl_mode in SSL_MODE_IDS:
            self._ssl_mode.set_selected(SSL_MODE_IDS.index(profile.ssl_mode))
        self._ssl_ca.set_text(profile.ssl_ca)
        self._ssl_cert.set_text(profile.ssl_cert)
        self._ssl_key.set_text(profile.ssl_key)
        self._ssh_enable.set_enable_expansion(profile.use_ssh)
        self._ssh_host.set_text(profile.ssh_host)
        self._ssh_port.set_text(str(profile.ssh_port) if profile.ssh_port else "")
        self._ssh_user.set_text(profile.ssh_user)
        self._ssh_password.set_text(profile.ssh_password)
        self._ssh_key.set_text(profile.ssh_key_path)

    def _kind_id(self) -> str:
        return KIND_IDS[self._kind.get_selected()]

    # Production suggestion (never an automatic classification)

    def _refresh_suggestion(self) -> None:
        if (
            self._suggestion_dismissed
            or self._environment.get_environment() != identity.UNSET
        ):
            self._suggestion.set_visible(False)
            return
        reason = identity.suggests_production(
            self._name.get_text(),
            self._host.get_text(),
            self._database.get_text(),
            self._kind_id(),
        )
        self._suggestion.set_visible(bool(reason))
        if reason:
            self._suggestion.set_title("This looks like production")
            self._suggestion.set_subtitle(
                f"Suggested because {reason}. Marking it production asks "
                "before destructive statements run."
            )

    def _accept_suggestion(self, *_args) -> None:
        self._environment.set_environment("production")
        if self._color.get_color() == identity.NONE:
            self._color.set_color("red")
        self._refresh_suggestion()

    def _dismiss_suggestion(self, *_args) -> None:
        self._suggestion_dismissed = True
        self._suggestion.set_visible(False)

    def _on_kind_changed(self, *_args) -> None:
        kind = self._kind_id()
        server = kind in ("mysql", "postgres")
        self._sqlite_group.set_visible(kind == "sqlite")
        self._server_group.set_visible(server)
        self._ssl_group.set_visible(server)
        self._ssh_group.set_visible(server)
        self._jdbc_group.set_visible(kind == "jdbc")

    def _file_entry_row(self, title: str) -> Adw.EntryRow:
        """An EntryRow holding a file path, with a browse button."""
        row = Adw.EntryRow(title=title)
        browse = Gtk.Button(icon_name="document-open-symbolic")
        browse.set_tooltip_text("Browse…")
        browse.add_css_class("flat")
        browse.set_valign(Gtk.Align.CENTER)
        browse.connect("clicked", lambda *_: self._browse_into(row))
        row.add_suffix(browse)
        return row

    def _browse(self, *_args) -> None:
        self._browse_into(self._file, title="Select database file")

    def _browse_into(
        self, row: Adw.EntryRow, title: str = "Select file"
    ) -> None:
        dialog = Gtk.FileDialog(title=title)
        root = self.get_root()
        parent = root if isinstance(root, Gtk.Window) else None
        dialog.open(parent, None, self._browse_finished, row)

    def _browse_finished(self, dialog, result, row: Adw.EntryRow) -> None:
        try:
            file = dialog.open_finish(result)
        except GLib.Error:
            return  # cancelled
        if file is not None:
            row.set_text(file.get_path() or "")

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
        try:
            ssh_port = int(self._ssh_port.get_text().strip())
        except ValueError:
            ssh_port = 22
        jdbc = kind == "jdbc"
        return ConnectionProfile(
            name=name,
            kind=kind,
            color=self._color.get_color(),
            environment=self._environment.get_environment(),
            file_path=self._file.get_text().strip(),
            host=self._host.get_text().strip() or "localhost",
            port=port,
            user=(self._jdbc_user if jdbc else self._user).get_text().strip(),
            password=(self._jdbc_password if jdbc else self._password).get_text(),
            database=self._database.get_text().strip(),
            jdbc_url=self._jdbc_url.get_text().strip(),
            driver_class=self._driver_class.get_text().strip(),
            jar_path=self._jar_path.get_text().strip(),
            ssl_mode=SSL_MODE_IDS[self._ssl_mode.get_selected()],
            ssl_ca=self._ssl_ca.get_text().strip(),
            ssl_cert=self._ssl_cert.get_text().strip(),
            ssl_key=self._ssl_key.get_text().strip(),
            use_ssh=self._ssh_enable.get_enable_expansion(),
            ssh_host=self._ssh_host.get_text().strip(),
            ssh_port=ssh_port,
            ssh_user=self._ssh_user.get_text().strip(),
            ssh_password=self._ssh_password.get_text(),
            ssh_key_path=self._ssh_key.get_text().strip(),
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
