"""The One-off Backup dialog: back this up, now, before I touch it.

Deliberately shorter than the job editor. There is no schedule, no
retention and no name to invent — the questions are what, where, and
go. It opens on the connection you were last looking at, and every
connection in the workspace is offered, including the JDBC and
SSH-tunnelled ones a scheduled job cannot cover: those fall back to
the portable engine (backend/backups/snapshot.py), and the Method row
says so rather than leaving the user to wonder why this one is
allowed when the job editor refused it.

The portable engine reads through the window's own connector, which is
why this dialog takes `ensure_connector`: an SSH tunnel or JDBC bridge
that is already up gets reused instead of being opened again.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from gi.repository import Adw, GLib, Gtk

from sqlide.backend.backups import oneoff
from sqlide.backend.backups.jobs import COMPRESSIONS, CONTENTS, BackupStore
from sqlide.backend.backups.snapshot import SnapshotSpec
from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.db.base import Connector
from sqlide.backend.workspaces import Workspace
from sqlide.i18n import N_, _
from sqlide.frontend.util import run_async

# Marked here, translated where they are shown: at import time the
# catalogue is not bound yet (see sqlide/i18n.py).
_CONTENT_LABELS = (
    N_("Schema and data"), N_("Schema only"), N_("Data only")
)
_COMPRESSION_LABELS = (N_("None (.sql)"), N_("gzip (.sql.gz)"))


class OneOffWindow(Adw.Window):
    def __init__(
        self,
        store: BackupStore,
        workspace: Workspace,
        ensure_connector: Callable[[ConnectionProfile], Connector],
        initial: ConnectionProfile | None = None,
        on_done: Callable[[], None] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(
            title=_("One-off Backup"),
            default_width=540,
            default_height=620,
            **kwargs,
        )
        self._store = store
        self._workspace = workspace
        self._ensure = ensure_connector
        self._on_done = on_done or (lambda: None)
        self._tables: list[str] = []
        self._running = False

        page = Adw.PreferencesPage()

        what = Adw.PreferencesGroup(
            title=_("What to back up"),
            description=_(
                "A copy taken right now. Nothing is scheduled and nothing "
                "is pruned — this file is yours to keep."
            ),
        )
        self._connections = list(workspace.connections)
        self._connection = Adw.ComboRow(
            title=_("Connection"),
            model=Gtk.StringList.new(
                [c.name for c in self._connections] or [_("No connections")]
            ),
        )
        self._connection.connect(
            "notify::selected", lambda *_: self._connection_changed()
        )
        what.add(self._connection)
        self._method = Adw.ActionRow(title=_("Method"), subtitle="—")
        what.add(self._method)
        self._content = Adw.ComboRow(
            title=_("Include"),
            model=Gtk.StringList.new([_(label) for label in _CONTENT_LABELS]),
        )
        what.add(self._content)
        self._tables_row = Adw.ActionRow(
            title=_("Tables"), subtitle=_("Every table"), activatable=True
        )
        choose = Gtk.Button(label=_("Choose…"), valign=Gtk.Align.CENTER)
        choose.connect("clicked", lambda *_: self._choose_tables())
        self._tables_row.add_suffix(choose)
        self._tables_row.set_activatable_widget(choose)
        what.add(self._tables_row)
        self._compression = Adw.ComboRow(
            title=_("Compression"),
            model=Gtk.StringList.new(
                [_(label) for label in _COMPRESSION_LABELS]
            ),
            selected=1,
        )
        what.add(self._compression)
        page.add(what)

        where = Adw.PreferencesGroup(
            title=_("Where to put it"),
            description=_(
                "One of your backup destinations, or straight to a file "
                "on this machine."
            ),
        )
        self._destinations = list(store.destinations)
        self._destination = Adw.ComboRow(
            title=_("Destination"),
            model=Gtk.StringList.new(
                [f"{d.name} — {d.describe()}" for d in self._destinations]
                or [_("No destinations yet")]
            ),
            sensitive=bool(self._destinations),
        )
        where.add(self._destination)
        self._to_file = Adw.SwitchRow(
            title=_("Save to a file instead"),
            active=not self._destinations,
        )
        self._to_file.connect(
            "notify::active", lambda *_: self._sync_where()
        )
        where.add(self._to_file)
        page.add(where)

        self._log = Gtk.Label(
            xalign=0, wrap=True, selectable=True,
            margin_start=12, margin_end=12, margin_bottom=12,
        )
        self._log.add_css_class("dim-label")

        self._run_button = Gtk.Button(label=_("Back Up Now"))
        self._run_button.add_css_class("suggested-action")
        self._run_button.connect("clicked", lambda *_: self._start())
        header = Adw.HeaderBar()
        header.pack_end(self._run_button)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(page)
        content.append(self._log)
        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(Gtk.ScrolledWindow(child=content, vexpand=True))
        self.set_content(view)

        if initial is not None:
            for index, profile in enumerate(self._connections):
                if profile.name == initial.name:
                    self._connection.set_selected(index)
                    break
        self._connection_changed()
        self._sync_where()

    # State

    def _profile(self) -> ConnectionProfile | None:
        index = self._connection.get_selected()
        if 0 <= index < len(self._connections):
            return self._connections[index]
        return None

    def _spec(self) -> SnapshotSpec:
        return SnapshotSpec(
            tables=list(self._tables),
            content=CONTENTS[self._content.get_selected()],
            compression=COMPRESSIONS[self._compression.get_selected()],
        )

    def _connection_changed(self) -> None:
        # A table selection means nothing once the connection changes.
        self._tables = []
        self._tables_row.set_subtitle(_("Every table"))
        profile = self._profile()
        if profile is None:
            self._method.set_subtitle(_("This workspace has no connections."))
            self._run_button.set_sensitive(False)
            return
        self._engine, why = oneoff.preferred_engine(profile)
        self._method.set_subtitle(why)
        self._run_button.set_sensitive(True)

    def _sync_where(self) -> None:
        to_file = self._to_file.get_active()
        self._destination.set_sensitive(
            bool(self._destinations) and not to_file
        )
        self._run_button.set_label(
            _("Back Up to File…") if to_file else _("Back Up Now")
        )

    # Tables

    def _choose_tables(self) -> None:
        profile = self._profile()
        if profile is None:
            return
        self._say(_("Loading tables…"))

        def loaded(objects) -> None:
            self._say("")
            from sqlide.frontend.backups_tab import TablePicker

            TablePicker(
                [o.name for o in objects if o.kind == "table"],
                self._tables,
                self._tables_chosen,
                transient_for=self,
                modal=True,
            ).present()

        run_async(
            lambda: self._ensure(profile).list_tables(),
            loaded,
            lambda exc: self._say(str(exc), error=True),
        )

    def _tables_chosen(self, chosen: list[str]) -> None:
        self._tables = chosen
        self._tables_row.set_subtitle(
            ", ".join(chosen) if chosen else _("Every table")
        )

    # Running

    def _start(self) -> None:
        profile = self._profile()
        if profile is None or self._running:
            return
        if self._to_file.get_active():
            self._pick_file(profile)
            return
        if not self._destinations:
            self._say(_("Add a destination, or save to a file."), error=True)
            return
        index = self._destination.get_selected()
        self._run(profile, destination=self._destinations[index])

    def _pick_file(self, profile: ConnectionProfile) -> None:
        dialog = Gtk.FileDialog(
            title=_("Save Backup"),
            initial_name=oneoff.artifact_name(profile, self._spec()),
        )

        def picked(dialog: Gtk.FileDialog, result) -> None:
            try:
                file = dialog.save_finish(result)
            except GLib.Error:
                return  # cancelled
            self._run(profile, file_path=Path(file.get_path()))

        dialog.save(self, None, picked)

    def _run(self, profile: ConnectionProfile, **where) -> None:
        spec = self._spec()
        engine = self._engine
        self._running = True
        self._run_button.set_sensitive(False)
        self._say(_("Starting…"))

        def progress(text: str) -> None:
            GLib.idle_add(lambda: (self._say(text), False)[1])

        def work():
            # The portable engine reads through the window's connector;
            # opening it here (on the worker thread) keeps the main loop
            # free even when a tunnel has to come up first.
            connector = (
                self._ensure(profile) if engine == oneoff.PORTABLE else None
            )
            return oneoff.run_oneoff(
                self._store,
                profile,
                spec,
                engine=engine,
                connector=connector,
                on_progress=progress,
                **where,
            )

        def done(run) -> None:
            self._running = False
            self._run_button.set_sensitive(True)
            self._say(run.message, error=not run.ok)
            self._on_done()

        def failed(exc: Exception) -> None:
            self._running = False
            self._run_button.set_sensitive(True)
            self._say(str(exc), error=True)

        run_async(work, done, failed)

    def _say(self, text: str, error: bool = False) -> None:
        self._log.set_text(text)
        self._log.set_visible(bool(text))
        if error:
            self._log.add_css_class("error")
            self._log.remove_css_class("dim-label")
        else:
            self._log.remove_css_class("error")
            self._log.add_css_class("dim-label")
