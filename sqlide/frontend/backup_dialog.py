"""Backup & Restore window, opened from the preferences dialog.

Two actions over backend/backup.py: export the whole configuration
(settings, saved snippets/queries, every workspace with its
connections and history) to a zip file, and restore such a zip over
the config directory. A restore only takes effect on the next start —
open windows keep their in-memory state and would overwrite restored
files on save — so the status line says to restart after restoring.
"""

from __future__ import annotations

import zipfile
from datetime import date
from pathlib import Path

from gi.repository import Adw, Gio, GLib, Gtk

from sqlide.backend import backup


class BackupWindow(Adw.Window):
    def __init__(self, **kwargs) -> None:
        super().__init__(
            title="Backup & Restore",
            default_width=460,
            default_height=360,
            **kwargs,
        )

        page = Adw.PreferencesPage()

        backup_group = Adw.PreferencesGroup(
            title="Backup",
            description="Save settings, saved snippets and queries, and "
            "every workspace (connections, tabs, history) to a zip file.",
        )
        backup_row = Adw.ActionRow(
            title="Back Up Everything",
            subtitle="Choose where to write the zip file",
            activatable=True,
        )
        backup_row.add_suffix(Gtk.Image(icon_name="document-save-symbolic"))
        backup_row.connect("activated", self._pick_backup_target)
        backup_group.add(backup_row)
        page.add(backup_group)

        restore_group = Adw.PreferencesGroup(
            title="Restore",
            description="Extract a backup zip over the current "
            "configuration. Files in the backup replace their current "
            "versions; everything else is kept.",
        )
        restore_row = Adw.ActionRow(
            title="Restore From Backup…",
            subtitle="Takes effect after restarting sqlide",
            activatable=True,
        )
        restore_row.add_suffix(Gtk.Image(icon_name="document-open-symbolic"))
        restore_row.connect("activated", self._pick_restore_source)
        restore_group.add(restore_row)
        page.add(restore_group)

        self._status = Gtk.Label(
            xalign=0,
            wrap=True,
            margin_top=12,
            margin_bottom=12,
            margin_start=24,
            margin_end=24,
        )
        self._status.add_css_class("dim-label")

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(page)
        content.append(self._status)
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        view.set_content(content)
        self.set_content(view)

    def _set_status(self, text: str, error: bool = False) -> None:
        self._status.set_text(text)
        if error:
            self._status.add_css_class("error")
            self._status.remove_css_class("dim-label")
        else:
            self._status.remove_css_class("error")
            self._status.add_css_class("dim-label")

    # Backup

    def _pick_backup_target(self, *_args) -> None:
        dialog = Gtk.FileDialog(
            title="Save Backup",
            initial_name=f"sqlide-backup-{date.today().isoformat()}.zip",
        )
        dialog.save(self, None, self._backup_picked)

    def _backup_picked(self, dialog: Gtk.FileDialog, result) -> None:
        try:
            file = dialog.save_finish(result)
        except GLib.Error:
            return  # cancelled
        path = Path(file.get_path())
        try:
            count = backup.create_backup(path)
        except (backup.BackupError, OSError) as exc:
            self._set_status(f"Backup failed: {exc}", error=True)
            return
        self._set_status(f"Backed up {count} file(s) to {path}")

    # Restore

    def _pick_restore_source(self, *_args) -> None:
        zips = Gtk.FileFilter()
        zips.set_name("sqlide backups (*.zip)")
        zips.add_suffix("zip")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(zips)
        dialog = Gtk.FileDialog(title="Restore Backup", filters=filters)
        dialog.open(self, None, self._restore_picked)

    def _restore_picked(self, dialog: Gtk.FileDialog, result) -> None:
        try:
            file = dialog.open_finish(result)
        except GLib.Error:
            return  # cancelled
        path = Path(file.get_path())
        try:
            count = backup.restore_backup(path)
        except (backup.BackupError, OSError, zipfile.BadZipFile) as exc:
            self._set_status(f"Restore failed: {exc}", error=True)
            return
        self._set_status(
            f"Restored {count} file(s) from {path.name} — restart sqlide "
            "to load them."
        )
