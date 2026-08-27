"""The Backups tab: jobs on the left, the selected job on the right.

What a job *is* lives in backend/backups/; this is the view over it.
The shape follows what a person actually does here, in order:

    pick what to back up  ->  pick where it goes  ->  say when
    ->  watch it run      ->  restore one later

so the editor reads top to bottom in those five groups, with the exact
pg_dump/mysqldump line shown at the bottom. That preview is not
decoration: a backup you cannot explain to your DBA is a backup nobody
trusts, and it is also the fastest way to see that "data only" or a
table selection landed the way you meant.

Long operations (dumping, uploading, listing a bucket) go through
run_async like every other blocking call in the app; progress lines
from the runner arrive on a worker thread and are marshalled back with
GLib.idle_add.

The tab is session-only (tab_state() returns None): reopening a
workspace should not silently reopen a window onto every credential
the backup manager knows about.
"""

from __future__ import annotations

from typing import Callable

from gi.repository import Adw, GLib, Gtk

from sqlide.backend.backups import dump, runner, schedule as scheduling
from sqlide.backend.backups.jobs import (
    COMPRESSIONS,
    CONTENTS,
    KIND_CONFIG,
    KIND_DATABASE,
    BackupStore,
    Job,
    Schedule,
)
from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.db.base import Connector
from sqlide.backend.workspaces import Workspace
from sqlide.frontend.backup_destinations import DestinationsWindow
from sqlide.frontend.backup_restore import RestoreWindow
from sqlide.frontend.util import icon_button, run_async
from sqlide.i18n import _

_CONTENT_LABELS = ("Schema and data", "Schema only", "Data only")
_COMPRESSION_LABELS = ("None (.sql)", "gzip (.sql.gz)")
_KIND_LABELS = ("A database", "sqlide's own configuration")
_JOB_KINDS = (KIND_DATABASE, KIND_CONFIG)
_SCHEDULE_MODES = ("off", "interval", "hourly", "daily", "weekly")
_SCHEDULE_LABELS = (
    "Manual only",
    "Every N minutes",
    "Hourly",
    "Daily",
    "Weekly",
)
_WEEKDAY_LABELS = (
    "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday",
)


class BackupsTab(Gtk.Box):
    def __init__(
        self,
        workspace: Workspace,
        ensure_connector: Callable[[ConnectionProfile], Connector],
        show_error: Callable[[str], None],
        store: BackupStore | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.workspace = workspace
        self._ensure = ensure_connector
        self._show_error = show_error
        self._store = store or BackupStore()
        self._selected: Job | None = None
        self._running = False

        bar = Gtk.Box(
            spacing=6,
            margin_top=6, margin_bottom=6, margin_start=6, margin_end=6,
        )
        title = Gtk.Label(label=_("Backups"), xalign=0, hexpand=True)
        title.add_css_class("heading")
        bar.append(title)
        new_job = Gtk.Button(label=_("New Job"))
        new_job.connect("clicked", lambda *_: self._new_job())
        bar.append(new_job)
        destinations = Gtk.Button(label=_("Destinations…"))
        destinations.connect("clicked", lambda *_: self._open_destinations())
        bar.append(destinations)
        restore = Gtk.Button(label=_("Restore…"))
        restore.connect("clicked", lambda *_: self._open_restore(None))
        bar.append(restore)
        self.append(bar)
        self.append(Gtk.Separator())

        self._list = Gtk.ListBox(vexpand=True)
        self._list.add_css_class("navigation-sidebar")
        self._list.connect("row-selected", self._on_selected)
        left = Gtk.ScrolledWindow(
            child=self._list, hscrollbar_policy=Gtk.PolicyType.NEVER
        )
        left.set_size_request(240, -1)

        self._detail = Gtk.Stack(vexpand=True, hexpand=True)
        self._detail.add_named(self._empty_page(), "empty")
        self._editor = _JobEditor(self)
        self._detail.add_named(
            Gtk.ScrolledWindow(child=self._editor, vexpand=True), "job"
        )
        self._detail.set_visible_child_name("empty")

        paned = Gtk.Paned(position=260, vexpand=True)
        paned.set_start_child(left)
        paned.set_end_child(self._detail)
        paned.set_resize_start_child(False)
        self.append(paned)

        self._reload_list()

    # Tab plumbing

    def tab_state(self) -> None:
        return None  # session-only, like the other tool tabs

    @property
    def store(self) -> BackupStore:
        return self._store

    # The job list

    def _empty_page(self) -> Gtk.Widget:
        status = Adw.StatusPage(
            icon_name="drive-harddisk-symbolic",
            title=_("No backup job selected"),
            description="A job says what to dump, where to put it and how "
            "often. Start with New Job — or Restore… to put an existing "
            "backup back.",
        )
        return status

    def _reload_list(self, select: Job | None = None) -> None:
        self._list.remove_all()
        for job in self._store.jobs:
            row = Adw.ActionRow(
                title=job.name,
                subtitle=self._job_subtitle(job),
            )
            row.add_prefix(
                Gtk.Image(
                    icon_name="folder-symbolic"
                    if job.kind == KIND_CONFIG
                    else "drive-multidisk-symbolic"
                )
            )
            if not job.enabled:
                row.add_css_class("dim-label")
            row.job = job
            self._list.append(row)
        target = select or self._selected
        if target is not None:
            for row in _rows(self._list):
                if row.job.id == target.id:
                    self._list.select_row(row)
                    return
        self._selected = None
        self._detail.set_visible_child_name("empty")

    def _job_subtitle(self, job: Job) -> str:
        parts = [scheduling.describe(job.schedule)]
        last = self._store.last_run(job.id)
        if last is not None:
            when = last.started.replace("T", " ")[:16]
            parts.append(("Last run " if last.ok else "Failed ") + when)
        else:
            parts.append("Never run")
        return " · ".join(parts)

    def _on_selected(self, _list, row) -> None:
        if row is None:
            return
        self._selected = row.job
        self._editor.show_job(row.job)
        self._detail.set_visible_child_name("job")

    def _new_job(self) -> None:
        profile = self.workspace.connections[0] if self.workspace.connections else None
        job = Job(
            name="New backup",
            workspace_id=self.workspace.id,
            connection=profile.name if profile else "",
            destination_id=(
                self._store.destinations[0].id
                if self._store.destinations
                else ""
            ),
        )
        self._store.add_job(job)
        self._reload_list(select=job)

    # Windows this tab opens

    def _open_destinations(self) -> None:
        window = DestinationsWindow(self._store, self._destinations_changed)
        window.set_transient_for(self.get_root())
        window.present()

    def _destinations_changed(self) -> None:
        if self._selected is not None:
            self._editor.show_job(self._selected)

    def _open_restore(self, job: Job | None) -> None:
        window = RestoreWindow(
            self._store,
            self.workspace,
            job,
            self._ensure,
        )
        window.set_transient_for(self.get_root())
        window.present()

    # Running

    def run_job(self, job: Job, on_line: Callable[[str], None]) -> None:
        """Run one job on a worker thread, streaming progress back."""
        if self._running:
            self._show_error("A backup is already running in this tab.")
            return
        self._running = True

        def progress(text: str) -> None:
            GLib.idle_add(lambda: (on_line(text), False)[1])

        def done(run) -> None:
            self._running = False
            on_line(run.message)
            self._reload_list()
            if self._selected is not None:
                self._editor.show_history(self._selected)

        def failed(exc: Exception) -> None:
            self._running = False
            on_line(str(exc))
            self._show_error(str(exc))

        run_async(
            lambda: runner.run_job(self._store, job, on_progress=progress),
            done,
            failed,
        )


def _rows(listbox: Gtk.ListBox) -> list[Gtk.Widget]:
    rows, index = [], 0
    while (row := listbox.get_row_at_index(index)) is not None:
        rows.append(row)
        index += 1
    return rows


class _JobEditor(Adw.PreferencesPage):
    """The selected job's settings, its command preview and its runs.

    Edits are written straight through to the store on save rather
    than kept as a draft: the same file is read by the headless runner,
    and a job that looks scheduled in the UI but was never saved is
    exactly the failure this feature exists to prevent.
    """

    def __init__(self, tab: BackupsTab) -> None:
        super().__init__()
        self._tab = tab
        self._job: Job | None = None
        self._objects: list[str] = []
        # Filled by show_job. Declared here because building the form
        # below fires ComboRow::notify::selected, and the handlers read
        # both lists.
        self._connections: list[ConnectionProfile] = []
        self._destinations: list = []
        self._history_rows: list[Gtk.Widget] = []

        # What
        what = Adw.PreferencesGroup(title=_("What to back up"))
        self._name = Adw.EntryRow(title=_("Job name"))
        what.add(self._name)
        self._kind = Adw.ComboRow(
            title=_("Contents"),
            model=Gtk.StringList.new(list(_KIND_LABELS)),
        )
        self._kind.connect("notify::selected", lambda *_: self._sync_visibility())
        what.add(self._kind)
        self._connection = Adw.ComboRow(title=_("Connection"))
        self._connection.connect(
            "notify::selected", lambda *_: self._sync_visibility()
        )
        what.add(self._connection)
        self._database = Adw.EntryRow(
            title=_("Database"),
            # Left empty, the connection's own database is dumped —
            # the same one the sidebar shows.
        )
        what.add(self._database)
        self._schema = Adw.EntryRow(title=_("Schema (PostgreSQL)"))
        what.add(self._schema)
        self._content = Adw.ComboRow(
            title=_("Include"),
            model=Gtk.StringList.new(list(_CONTENT_LABELS)),
        )
        self._content.connect("notify::selected", lambda *_: self._refresh_preview())
        what.add(self._content)
        self._tables = Adw.ActionRow(
            title=_("Tables"),
            subtitle=_("Every table"),
            activatable=True,
        )
        choose = Gtk.Button(label=_("Choose…"), valign=Gtk.Align.CENTER)
        choose.connect("clicked", lambda *_: self._choose_tables())
        self._tables.add_suffix(choose)
        self._tables.set_activatable_widget(choose)
        what.add(self._tables)
        self.add(what)

        # Where
        where = Adw.PreferencesGroup(title=_("Where it goes"))
        self._destination = Adw.ComboRow(title=_("Destination"))
        manage = Gtk.Button(label=_("Manage…"), valign=Gtk.Align.CENTER)
        manage.connect("clicked", lambda *_: self._tab._open_destinations())
        self._destination.add_suffix(manage)
        where.add(self._destination)
        self._compression = Adw.ComboRow(
            title=_("Compression"),
            model=Gtk.StringList.new(list(_COMPRESSION_LABELS)),
        )
        self._compression.connect(
            "notify::selected", lambda *_: self._refresh_preview()
        )
        where.add(self._compression)
        self._keep = Adw.SpinRow(
            title=_("Keep"),
            subtitle=_("How many of this job's backups to keep there. "
            "0 keeps every one."),
            adjustment=Gtk.Adjustment(
                lower=0, upper=999, step_increment=1, value=7
            ),
        )
        where.add(self._keep)
        self.add(where)

        # When
        when = Adw.PreferencesGroup(
            title=_("When it runs"),
            description="The in-app schedule only fires while sqlide is "
            "open, and catches up on one missed run when it reopens. "
            "For backups that must happen with the app closed, install "
            "the system timer.",
        )
        self._enabled = Adw.SwitchRow(
            title=_("Job enabled"),
            subtitle=_("Off pauses the schedule; the job can still be run "
            "by hand."),
            active=True,
        )
        when.add(self._enabled)
        self._mode = Adw.ComboRow(
            title=_("Schedule"),
            model=Gtk.StringList.new(list(_SCHEDULE_LABELS)),
        )
        self._mode.connect("notify::selected", lambda *_: self._sync_visibility())
        when.add(self._mode)
        self._every = Adw.SpinRow(
            title=_("Every (minutes)"),
            adjustment=Gtk.Adjustment(
                lower=1, upper=10080, step_increment=5, value=60
            ),
        )
        when.add(self._every)
        self._minute = Adw.SpinRow(
            title=_("Minutes past the hour"),
            adjustment=Gtk.Adjustment(
                lower=0, upper=59, step_increment=1, value=0
            ),
        )
        when.add(self._minute)
        self._at = Adw.EntryRow(title=_("At (HH:MM)"))
        when.add(self._at)
        self._weekday = Adw.ComboRow(
            title=_("Day"), model=Gtk.StringList.new(list(_WEEKDAY_LABELS))
        )
        when.add(self._weekday)
        self._systemd = Adw.SwitchRow(
            title=_("Install a system timer"),
            subtitle=_("Runs this job through systemd, so it happens even "
            "when sqlide is closed."),
        )
        self._systemd.connect(
            "notify::active", lambda *_: self._toggle_systemd()
        )
        when.add(self._systemd)
        self.add(when)

        # The command, and the buttons that act on all of the above
        run_group = Adw.PreferencesGroup(title=_("Run"))
        self._preview = Gtk.Label(
            xalign=0, wrap=True, wrap_mode=2, selectable=True,
            margin_top=6, margin_bottom=6, margin_start=12, margin_end=12,
        )
        self._preview.add_css_class("monospace")
        self._preview.add_css_class("dim-label")
        preview_row = Adw.ExpanderRow(
            title=_("Command"),
            subtitle=_("Exactly what sqlide will run"),
        )
        preview_row.add_row(_wrap(self._preview))
        run_group.add(preview_row)

        self._status = Gtk.Label(xalign=0, wrap=True, margin_start=12,
                                 margin_end=12, margin_top=6)
        self._status.add_css_class("dim-label")

        buttons = Gtk.Box(spacing=6, margin_top=6, halign=Gtk.Align.END)
        save = Gtk.Button(label=_("Save"))
        save.add_css_class("suggested-action")
        save.connect("clicked", lambda *_: self._save())
        run_now = Gtk.Button(label=_("Back Up Now"))
        run_now.connect("clicked", lambda *_: self._run_now())
        restore = Gtk.Button(label=_("Restore…"))
        restore.connect(
            "clicked", lambda *_: self._tab._open_restore(self._job)
        )
        delete = icon_button(
            "user-trash-symbolic", _("Delete This Job"), self._delete
        )
        delete.add_css_class("flat")
        for button in (delete, restore, run_now, save):
            buttons.append(button)
        run_group.add(_wrap(self._status))
        run_group.add(_wrap(buttons))
        self.add(run_group)

        self._history = Adw.PreferencesGroup(title=_("History"))
        self.add(self._history)

    # Loading a job into the form

    def show_job(self, job: Job) -> None:
        self._job = job
        self._objects = list(job.objects)
        self._name.set_text(job.name)
        self._kind.set_selected(
            _JOB_KINDS.index(job.kind) if job.kind in _JOB_KINDS else 0
        )
        self._fill_connections(job)
        self._fill_destinations(job)
        self._database.set_text(job.database)
        self._schema.set_text(job.schema)
        self._content.set_selected(
            CONTENTS.index(job.content) if job.content in CONTENTS else 0
        )
        self._compression.set_selected(
            COMPRESSIONS.index(job.compression)
            if job.compression in COMPRESSIONS
            else 0
        )
        self._keep.set_value(job.keep)
        self._enabled.set_active(job.enabled)
        mode = job.schedule.mode if job.schedule.mode in _SCHEDULE_MODES else "off"
        self._mode.set_selected(_SCHEDULE_MODES.index(mode))
        self._every.set_value(job.schedule.every_minutes)
        self._minute.set_value(job.schedule.minute)
        self._at.set_text(job.schedule.at)
        self._weekday.set_selected(job.schedule.weekday % 7)
        self._systemd.set_active(job.schedule.systemd)
        self._set_status("")
        self._sync_visibility()
        self.show_history(job)

    def _fill_connections(self, job: Job) -> None:
        self._connections = list(self._tab.workspace.connections)
        names = [c.name for c in self._connections] or ["No connections"]
        self._connection.set_model(Gtk.StringList.new(names))
        for index, profile in enumerate(self._connections):
            if profile.name == job.connection:
                self._connection.set_selected(index)
                break

    def _fill_destinations(self, job: Job) -> None:
        self._destinations = list(self._tab.store.destinations)
        labels = [
            f"{d.name} — {d.describe()}" for d in self._destinations
        ] or ["No destinations yet"]
        self._destination.set_model(Gtk.StringList.new(labels))
        for index, destination in enumerate(self._destinations):
            if destination.id == job.destination_id:
                self._destination.set_selected(index)
                break

    def _profile(self) -> ConnectionProfile | None:
        index = self._connection.get_selected()
        if 0 <= index < len(self._connections):
            return self._connections[index]
        return None

    # Which rows make sense right now

    def _sync_visibility(self) -> None:
        kind = _JOB_KINDS[self._kind.get_selected()]
        database = kind == KIND_DATABASE
        profile = self._profile()
        self._connection.set_visible(database)
        self._database.set_visible(
            database and profile is not None and profile.kind != "sqlite"
        )
        self._schema.set_visible(
            database and profile is not None and profile.kind == "postgres"
        )
        self._content.set_visible(database)
        self._tables.set_visible(database)
        self._tables.set_subtitle(
            ", ".join(self._objects) if self._objects else "Every table"
        )

        mode = _SCHEDULE_MODES[self._mode.get_selected()]
        self._every.set_visible(mode == "interval")
        self._minute.set_visible(mode == "hourly")
        self._at.set_visible(mode in ("daily", "weekly"))
        self._weekday.set_visible(mode == "weekly")
        self._systemd.set_visible(mode != "off")
        self._refresh_preview()

    def _refresh_preview(self) -> None:
        job = self._collect(persist=False)
        if job is None:
            return
        if job.kind == KIND_CONFIG:
            self._preview.set_text(
                "No external tool: the configuration files are zipped "
                "directly."
            )
            return
        profile = self._profile()
        if profile is None:
            self._preview.set_text(_("Pick a connection."))
            return
        try:
            self._preview.set_text(dump.command_for(profile, job).preview())
        except dump.DumpError as exc:
            self._preview.set_text(str(exc))

    # Saving

    def _collect(self, persist: bool = True) -> Job | None:
        job = self._job
        if job is None:
            return None
        if not persist:
            # The preview needs a job-shaped value without touching the
            # stored one, so it gets a throwaway copy.
            job = Job.from_dict(job.to_dict())
        job.name = self._name.get_text().strip() or "Backup"
        job.kind = _JOB_KINDS[self._kind.get_selected()]
        profile = self._profile()
        job.connection = profile.name if profile else ""
        job.workspace_id = self._tab.workspace.id
        job.database = self._database.get_text().strip()
        job.schema = self._schema.get_text().strip()
        job.content = CONTENTS[self._content.get_selected()]
        job.objects = list(self._objects)
        job.compression = COMPRESSIONS[self._compression.get_selected()]
        job.keep = int(self._keep.get_value())
        job.enabled = self._enabled.get_active()
        index = self._destination.get_selected()
        if 0 <= index < len(self._destinations):
            job.destination_id = self._destinations[index].id
        job.schedule = Schedule(
            mode=_SCHEDULE_MODES[self._mode.get_selected()],
            every_minutes=int(self._every.get_value()),
            minute=int(self._minute.get_value()),
            at=self._at.get_text().strip() or "02:00",
            weekday=self._weekday.get_selected(),
            systemd=self._systemd.get_active(),
        )
        return job

    def _save(self) -> None:
        job = self._collect()
        if job is None:
            return
        self._tab.store.save()
        if job.schedule.systemd:
            # The unit files embed the schedule, so a saved change has
            # to be written through to systemd or the timer keeps the
            # old one.
            self._install_timer(job, quiet=True)
        self._tab._reload_list(select=job)
        self._set_status("Saved.")

    def _delete(self) -> None:
        job = self._job
        if job is None:
            return
        dialog = Adw.MessageDialog(
            transient_for=self.get_root(),
            heading=f"Delete {job.name}?",
            body=_("The job and its run history go away. Backups already "
            "written to the destination are left alone."),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("delete", _("Delete"))
        dialog.set_response_appearance(
            "delete", Adw.ResponseAppearance.DESTRUCTIVE
        )

        def answered(_dialog, response: str) -> None:
            if response != "delete":
                return
            if job.schedule.systemd:
                scheduling.uninstall(job)
            self._tab.store.remove_job(job.id)
            self._job = None
            self._tab._selected = None
            self._tab._reload_list()

        dialog.connect("response", answered)
        dialog.present()

    # systemd

    def _toggle_systemd(self) -> None:
        job = self._job
        if job is None:
            return
        if self._systemd.get_active():
            self._install_timer(job)
        elif job.schedule.systemd:
            scheduling.uninstall(job)
            job.schedule.systemd = False
            self._tab.store.save()
            self._set_status("System timer removed.")

    def _install_timer(self, job: Job, quiet: bool = False) -> None:
        try:
            scheduling.install(job)
        except scheduling.SystemdError as exc:
            job.schedule.systemd = False
            self._systemd.set_active(False)
            self._set_status(str(exc), error=True)
            return
        job.schedule.systemd = True
        self._tab.store.save()
        if not quiet:
            self._set_status(scheduling.status(job) or "System timer installed.")

    # Running and history

    def _run_now(self) -> None:
        job = self._collect()
        if job is None:
            return
        self._tab.store.save()
        self._set_status("Starting…")
        self._tab.run_job(job, self._set_status)

    def show_history(self, job: Job) -> None:
        for row in self._history_rows:
            self._history.remove(row)
        self._history_rows = []
        runs = self._tab.store.runs_for(job.id)[:10]
        if not runs:
            row = Adw.ActionRow(
                title=_("No runs yet"),
                subtitle=_("Back Up Now runs this job straight away."),
            )
            self._history.add(row)
            self._history_rows.append(row)
            return
        for run in runs:
            row = Adw.ExpanderRow(
                title=("Succeeded " if run.ok else "Failed ")
                + run.started.replace("T", " ")[:16],
                subtitle=run.message,
            )
            row.add_prefix(
                Gtk.Image(
                    icon_name="emblem-ok-symbolic"
                    if run.ok
                    else "dialog-warning-symbolic"
                )
            )
            if run.log:
                log = Gtk.Label(
                    label=run.log, xalign=0, wrap=True, selectable=True,
                    margin_top=6, margin_bottom=6,
                    margin_start=12, margin_end=12,
                )
                log.add_css_class("monospace")
                row.add_row(_wrap(log))
            self._history.add(row)
            self._history_rows.append(row)

    def _set_status(self, text: str, error: bool = False) -> None:
        self._status.set_text(text)
        self._status.set_visible(bool(text))
        if error:
            self._status.add_css_class("error")
            self._status.remove_css_class("dim-label")
        else:
            self._status.remove_css_class("error")
            self._status.add_css_class("dim-label")

    # Table picker

    def _choose_tables(self) -> None:
        profile = self._profile()
        if profile is None:
            self._set_status("Pick a connection first.", error=True)
            return
        self._set_status("Loading tables…")

        def loaded(objects) -> None:
            self._set_status("")
            _TablePicker(
                [o.name for o in objects if o.kind == "table"],
                self._objects,
                self._tables_chosen,
                transient_for=self.get_root(),
                modal=True,
            ).present()

        run_async(
            lambda: self._tab._ensure(profile).list_tables(),
            loaded,
            lambda exc: self._set_status(str(exc), error=True),
        )

    def _tables_chosen(self, chosen: list[str]) -> None:
        self._objects = chosen
        self._sync_visibility()


def _wrap(widget: Gtk.Widget) -> Gtk.ListBoxRow:
    """A plain widget as a preferences row, without the row chrome."""
    row = Gtk.ListBoxRow(activatable=False, selectable=False)
    row.set_child(widget)
    return row


class _TablePicker(Adw.Window):
    """Which tables a job dumps. Empty means all of them — and that is
    the default, because a selection silently going stale as tables are
    added is how a backup quietly stops covering the database."""

    def __init__(
        self,
        tables: list[str],
        chosen: list[str],
        on_done: Callable[[list[str]], None],
        **kwargs,
    ) -> None:
        super().__init__(
            title=_("Choose Tables"), default_width=380, default_height=520,
            **kwargs,
        )
        self._on_done = on_done
        self._checks: dict[str, Gtk.CheckButton] = {}

        group = Adw.PreferencesGroup(
            description="Nothing ticked backs up every table, including "
            "ones added later."
        )
        for table in tables:
            check = Gtk.CheckButton(active=table in chosen)
            row = Adw.ActionRow(title=table)
            row.add_prefix(check)
            row.set_activatable_widget(check)
            group.add(row)
            self._checks[table] = check
        page = Adw.PreferencesPage()
        page.add(group)

        clear = Gtk.Button(label=_("All Tables"))
        clear.connect("clicked", lambda *_: self._finish([]))
        apply = Gtk.Button(label=_("Use Selection"))
        apply.add_css_class("suggested-action")
        apply.connect("clicked", lambda *_: self._finish(self._selected()))
        header = Adw.HeaderBar()
        header.pack_start(clear)
        header.pack_end(apply)

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(page)
        self.set_content(view)

    def _selected(self) -> list[str]:
        return [name for name, check in self._checks.items() if check.get_active()]

    def _finish(self, chosen: list[str]) -> None:
        self._on_done(chosen)
        self.close()
