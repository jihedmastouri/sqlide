"""Restoring a backup: pick the file, pick the target, be told what it
will cost, then run it.

This is the most destructive thing the app can do — a dump script may
drop and recreate every object it touches — so the flow refuses to be
a single button. Three deliberate steps:

1. Choose a destination and one artifact sitting on it (or a file from
   this machine, for a backup that arrived some other way).
2. Choose the connection to restore *into*. It defaults to the one the
   job backs up, but any connection in the workspace can be picked:
   restoring production into a scratch database is the common case,
   and making that the easy path is the point.
3. Confirm, with the target's environment class spelled out — the same
   production/staging marking the destructive-SQL ladder uses.

The restore itself runs through the vendor client (psql / mysql /
sqlite3) on a worker thread, with its output shown as it finishes.
"""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path
from typing import Callable

from gi.repository import Adw, GLib, Gtk

from sqlide.backend.backups import restore as restoring
from sqlide.backend.backups import snapshot, targets
from sqlide.backend.backups.jobs import BackupStore, Job
from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.db.base import Connector
from sqlide.backend.workspaces import Workspace
from sqlide.frontend.util import run_async
from sqlide.i18n import _, format_datetime, format_size


class RestoreWindow(Adw.Window):
    def __init__(
        self,
        store: BackupStore,
        workspace: Workspace,
        job: Job | None,
        ensure_connector: Callable[[ConnectionProfile], Connector],
        **kwargs,
    ) -> None:
        super().__init__(
            title=_("Restore Backup"),
            default_width=560,
            default_height=620,
            **kwargs,
        )
        self._store = store
        self._workspace = workspace
        self._job = job
        self._ensure = ensure_connector
        self._artifacts: list[targets.Artifact] = []
        self._local_file: Path | None = None

        page = Adw.PreferencesPage()

        source = Adw.PreferencesGroup(
            title=_("What to restore"),
            description="A backup already at one of your destinations, "
            "or a dump file on this machine.",
        )
        self._destinations = list(store.destinations)
        self._destination = Adw.ComboRow(
            title=_("Destination"),
            model=Gtk.StringList.new(
                [d.name for d in self._destinations] or ["No destinations"]
            ),
        )
        self._destination.connect(
            "notify::selected", lambda *_: self._load_artifacts()
        )
        source.add(self._destination)
        self._artifact = Adw.ComboRow(
            title=_("Backup"), model=Gtk.StringList.new(["—"])
        )
        source.add(self._artifact)
        file_row = Adw.ActionRow(
            title=_("Use a file instead…"),
            subtitle=_("Any .sql or .sql.gz dump"),
            activatable=True,
        )
        file_row.add_suffix(Gtk.Image(icon_name="document-open-symbolic"))
        file_row.connect("activated", lambda *_: self._pick_file())
        source.add(file_row)
        page.add(source)

        target = Adw.PreferencesGroup(
            title=_("Where to restore it"),
            description="The script runs against this connection. It does "
            "not have to be the one the backup came from.",
        )
        self._connections = list(workspace.connections)
        self._connection = Adw.ComboRow(
            title=_("Connection"),
            model=Gtk.StringList.new(
                [c.name for c in self._connections] or ["No connections"]
            ),
        )
        self._connection.connect(
            "notify::selected", lambda *_: self._describe_target()
        )
        target.add(self._connection)
        self._database = Adw.EntryRow(
            title=_("Database (leave empty for the connection's own)")
        )
        target.add(self._database)
        self._warning = Gtk.Label(
            xalign=0, wrap=True,
            margin_start=12, margin_end=12, margin_top=6, margin_bottom=6,
        )
        self._warning.add_css_class("warning")
        target.add(_wrap(self._warning))
        page.add(target)

        self._output = Gtk.Label(
            xalign=0, wrap=True, selectable=True,
            margin_start=12, margin_end=12, margin_bottom=12,
        )
        self._output.add_css_class("monospace")
        self._output.add_css_class("dim-label")

        self._restore_button = Gtk.Button(label=_("Restore…"))
        self._restore_button.add_css_class("destructive-action")
        self._restore_button.connect("clicked", lambda *_: self._confirm())
        header = Adw.HeaderBar()
        header.pack_end(self._restore_button)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(page)
        content.append(self._output)
        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(
            Gtk.ScrolledWindow(child=content, vexpand=True)
        )
        self.set_content(view)

        self._preselect()
        self._load_artifacts()
        self._describe_target()

    def _preselect(self) -> None:
        """Open on the job the user came from: its destination, and the
        connection it backs up as the restore target."""
        if self._job is None:
            return
        for index, destination in enumerate(self._destinations):
            if destination.id == self._job.destination_id:
                self._destination.set_selected(index)
                break
        for index, profile in enumerate(self._connections):
            if profile.name == self._job.connection:
                self._connection.set_selected(index)
                break

    # Source

    def _current_destination(self):
        index = self._destination.get_selected()
        if 0 <= index < len(self._destinations):
            return self._destinations[index]
        return None

    def _load_artifacts(self) -> None:
        destination = self._current_destination()
        if destination is None:
            return
        self._local_file = None
        self._artifact.set_model(Gtk.StringList.new(["Loading…"]))

        def loaded(artifacts: list[targets.Artifact]) -> None:
            # A destination is usually shared; when the user came from
            # a job, its own backups float to the top.
            if self._job is not None:
                prefix = self._job.slug() + "-"
                artifacts.sort(
                    key=lambda a: (not a.name.startswith(prefix), a.name),
                )
            self._artifacts = artifacts
            self._artifact.set_model(
                Gtk.StringList.new(
                    [_artifact_label(a) for a in artifacts] or ["Nothing there"]
                )
            )

        run_async(
            lambda: targets.open_target(destination).listing(),
            loaded,
            lambda exc: self._say(str(exc), error=True),
        )

    def _pick_file(self) -> None:
        dialog = Gtk.FileDialog(title=_("Open Dump File"))

        def picked(dialog: Gtk.FileDialog, result) -> None:
            try:
                file = dialog.open_finish(result)
            except GLib.Error:
                return  # cancelled
            self._local_file = Path(file.get_path())
            self._artifact.set_model(
                Gtk.StringList.new([self._local_file.name])
            )
            self._artifacts = []

        dialog.open(self, None, picked)

    # Target

    def _profile(self) -> ConnectionProfile | None:
        index = self._connection.get_selected()
        if 0 <= index < len(self._connections):
            return self._connections[index]
        return None

    def _describe_target(self) -> None:
        profile = self._profile()
        if profile is None:
            self._warning.set_text(_("This workspace has no connections."))
            self._restore_button.set_sensitive(False)
            return
        # A connection with no vendor client to pipe into — JDBC, or one
        # behind sqlide's own SSH tunnel — is restored by running the
        # script's statements over the connector instead. That is the
        # same path the portable snapshot writes for, so the two halves
        # of the feature cover the same connections.
        self._over_connector = bool(restoring.unsupported_reason(profile))
        self._restore_button.set_sensitive(True)
        warning = restoring.describe_target(
            profile, self._database.get_text().strip()
        )
        if self._over_connector:
            warning += _(
                " The script runs statement by statement over sqlide's own "
                "connection, stopping at the first error."
            )
        self._warning.set_text(warning)

    # Running

    def _confirm(self) -> None:
        profile = self._profile()
        if profile is None:
            return
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading=f"Restore into {profile.name}?",
            body=restoring.describe_target(
                profile, self._database.get_text().strip()
            )
            + "\n\nThis cannot be undone from sqlide.",
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("restore", _("Restore"))
        dialog.set_response_appearance(
            "restore", Adw.ResponseAppearance.DESTRUCTIVE
        )
        dialog.connect(
            "response",
            lambda _d, response: response == "restore" and self._run(profile),
        )
        dialog.present()

    def _run(self, profile: ConnectionProfile) -> None:
        database = self._database.get_text().strip()
        local = self._local_file
        destination = self._current_destination()
        index = self._artifact.get_selected()
        artifact = (
            self._artifacts[index]
            if local is None and 0 <= index < len(self._artifacts)
            else None
        )
        if local is None and artifact is None:
            self._say("Choose a backup to restore.", error=True)
            return
        self._restore_button.set_sensitive(False)
        self._say("Restoring…")

        over_connector = self._over_connector

        def apply(path: Path) -> str:
            if not over_connector:
                return restoring.run_restore(profile, path, database=database)
            count = snapshot.apply_script(
                self._ensure(profile), path, profile.kind
            )
            return _("{count} statement(s) run.").format(count=count)

        def work() -> str:
            if local is not None:
                return apply(local)
            with tempfile.TemporaryDirectory(prefix="sqlide-restore-") as tmp:
                path = Path(tmp) / artifact.name
                targets.open_target(destination).download(artifact.name, path)
                return apply(path)

        def done(output: str) -> None:
            self._restore_button.set_sensitive(True)
            self._say(output.strip() or "Restored.")

        def failed(exc: Exception) -> None:
            self._restore_button.set_sensitive(True)
            self._say(str(exc), error=True)

        run_async(work, done, failed)

    def _say(self, text: str, error: bool = False) -> None:
        self._output.set_text(text)
        self._output.set_visible(bool(text))
        if error:
            self._output.add_css_class("error")
            self._output.remove_css_class("dim-label")
        else:
            self._output.remove_css_class("error")
            self._output.add_css_class("dim-label")


def _artifact_label(artifact: targets.Artifact) -> str:
    parts = [artifact.name]
    if artifact.size:
        parts.append(format_size(artifact.size))
    if artifact.modified:
        parts.append(_format_modified(artifact.modified))
    return "  ·  ".join(parts)


def _format_modified(stamp: str) -> str:
    """An artifact's ISO timestamp in the reader's own date order.
    Anything unparseable is shown as it came, minus the T."""
    try:
        return format_datetime(datetime.fromisoformat(stamp))
    except ValueError:
        return stamp.replace("T", " ")[:16]


def _wrap(widget: Gtk.Widget) -> Gtk.ListBoxRow:
    row = Gtk.ListBoxRow(activatable=False, selectable=False)
    row.set_child(widget)
    return row
