"""The PRAGMA viewer and editor: what this SQLite connection is set to,
and which of those settings are safe to change from here (SQ-02).

One tab per connection, offered wherever the provider declares the
`pragmas` capability — the sidebar asks the capability, never the
engine's name, so an engine that grows a settings surface of its own
would arrive here rather than in a branch.

Three groups, in the order a person needs them:

* **Settings** — the pragmas that take a value. A switch for the
  booleans, a select for the enumerations, an entry for the numbers,
  each validated against `db/sqlite/pragmas.py` before any SQL exists.
* **Information** — page counts, encoding, the data version: questions
  with answers, drawn read-only.
* **Checks** — `integrity_check` and friends. They read the whole file,
  so they are never part of drawing the list: each has a Run button and
  its rows open in a dialog.

Three rules the ticket asks for, and where they live:

* **Nothing is applied silently.** A pragma whose scope is anything
  but this connection — one stored in the file, one that only applies
  at connect time, one that rewrites the file — goes through a
  confirmation carrying the catalog's warning *and* the exact
  statement, the same shape confirm.py uses for destructive SQL. A
  cancelled confirmation puts the control back where it was.
* **The dangerous ones are behind Advanced.** `writable_schema` can
  corrupt a database in a way SQL cannot undo, so it is off the list
  until the user turns Advanced on, and carries its warning when shown.
* **The database is asked, not assumed.** Every apply re-reads the
  pragma and redraws the row from the answer. SQLite ignores
  `page_size` on a populated file and refuses `journal_mode` inside a
  transaction, both without an error: a row that showed what was asked
  for would be lying.

Save as Defaults writes the settings that differ from SQLite's own
defaults onto the connection profile (CORE-13), where the adapter
applies them on every connect.
"""

from __future__ import annotations

from typing import Callable

from gi.repository import Adw, Gtk

from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.db import registry
from sqlide.backend.db.base import Connector
from sqlide.backend.db.sqlite import pragmas as pragma_rules
from sqlide.backend.workspaces import TabState
from sqlide.frontend import confirm
from sqlide.frontend.data_grid import ResultGrid
from sqlide.frontend.util import describe, run_async

#: Which group a row belongs in, by the kind the catalog gives it.
_GROUPS = {
    pragma_rules.BOOLEAN: "settings",
    pragma_rules.ENUM: "settings",
    pragma_rules.INTEGER: "settings",
    pragma_rules.READONLY: "information",
    pragma_rules.CHECK: "checks",
}


def _subtitle(state: pragma_rules.PragmaState) -> str:
    """The line under a pragma's name: what it does, what it costs, and
    where it stands relative to SQLite's own default."""
    spec = state.spec
    parts = [spec.description]
    if spec.editable and spec.default:
        parts.append(f"Default: {state.display_default}.")
    if spec.scope != pragma_rules.SESSION:
        parts.append(f"Scope: {spec.scope_label}.")
    if spec.advanced:
        parts.append("Advanced — see the warning before changing this.")
    if state.error:
        parts.append(f"Unavailable: {state.error}.")
    return " ".join(part for part in parts if part)


class PragmasTab(Gtk.Box):
    def __init__(
        self,
        profile: ConnectionProfile,
        ensure_connector: Callable[[ConnectionProfile], Connector],
        show_error: Callable[[str], None],
        on_save_defaults: Callable[[ConnectionProfile, list[str]], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.profile = profile
        self._ensure = ensure_connector
        self._show_error = show_error
        self._on_save_defaults = on_save_defaults
        self._advanced = False
        self._states: dict[str, pragma_rules.PragmaState] = {}
        # True while values are being written into the controls, so the
        # handlers can tell a redraw from a person turning a switch.
        self._loading = False

        self._page = Adw.PreferencesPage(vexpand=True)
        self._groups: dict[str, Adw.PreferencesGroup] = {}
        for slug, title, description in (
            (
                "settings",
                "Settings",
                "Values this connection can change. Anything beyond a "
                "session setting asks first.",
            ),
            (
                "information",
                "Information",
                "What the file reports about itself. Read-only.",
            ),
            (
                "checks",
                "Checks",
                "Run on request: each one reads the database rather "
                "than a header field.",
            ),
        ):
            group = Adw.PreferencesGroup(title=title, description=description)
            self._groups[slug] = group
            self._page.add(group)
        #: The widgets currently in each group, so a redraw can take
        #: them out again — a PreferencesGroup has no "empty me".
        self._rows: list[tuple[Adw.PreferencesGroup, Gtk.Widget]] = []

        header = Adw.HeaderBar()
        save = Gtk.Button(label="Save as Defaults")
        save.connect("clicked", lambda *_: self._save_defaults())
        describe(
            save,
            "Store the settings that differ from SQLite's defaults on this "
            "connection, and apply them on every connect",
        )
        header.pack_start(save)
        advanced = Gtk.ToggleButton(label="Advanced")
        describe(
            advanced,
            "Also list the pragmas that can corrupt a database rather than "
            "only slow it down",
        )
        advanced.connect("toggled", self._advanced_toggled)
        header.pack_end(advanced)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh.add_css_class("flat")
        describe(refresh, "Re-read every value from the database")
        refresh.connect("clicked", lambda *_: self.reload())
        header.pack_end(refresh)

        view = Adw.ToolbarView(content=self._page)
        view.add_top_bar(header)
        self.append(view)

        self.reload()

    def tab_state(self) -> TabState:
        return TabState(kind="pragmas", connection=self.profile.name)

    # Reading

    def reload(self) -> None:
        """Re-read every listed pragma and rebuild the rows from the
        answers — the only way values get onto the screen."""
        advanced = self._advanced

        def work():
            provider = registry.create_provider(
                self.profile.kind, self._ensure(self.profile)
            )
            return provider.list_pragmas(advanced)

        def done(states) -> None:
            self._states = {state.name: state for state in states}
            self._rebuild(states)

        run_async(work, done, lambda exc: self._show_error(str(exc)))

    def _advanced_toggled(self, button: Gtk.ToggleButton) -> None:
        self._advanced = button.get_active()
        self.reload()

    # Drawing

    def _rebuild(self, states) -> None:
        self._loading = True
        for group, row in self._rows:
            group.remove(row)
        self._rows = []
        for state in states:
            group = self._groups[_GROUPS.get(state.spec.kind, "information")]
            row = self._row(state)
            group.add(row)
            self._rows.append((group, row))
        self._loading = False

    def _row(self, state: pragma_rules.PragmaState) -> Gtk.Widget:
        spec = state.spec
        if spec.kind == pragma_rules.BOOLEAN:
            row = Adw.SwitchRow(title=spec.name, subtitle=_subtitle(state))
            row.set_active(state.value.lower() in ("1", "true", "yes", "on"))
            row.set_sensitive(not state.error)
            row.connect("notify::active", self._switch_changed, state)
            return row
        if spec.kind == pragma_rules.ENUM:
            labels = pragma_rules.choice_labels(spec)
            row = Adw.ComboRow(
                title=spec.name,
                subtitle=_subtitle(state),
                model=Gtk.StringList.new([label for _value, label in labels]),
            )
            current = (state.value or "").lower()
            for index, (value, _label) in enumerate(labels):
                if value == current:
                    row.set_selected(index)
                    break
            row.set_sensitive(not state.error)
            row.connect("notify::selected", self._combo_changed, state)
            return row
        if spec.kind == pragma_rules.INTEGER:
            row = Adw.EntryRow(title=spec.name, show_apply_button=True)
            row.set_text(state.value)
            row.set_sensitive(not state.error)
            row.connect("apply", self._entry_applied, state)
            return _with_note(row, _subtitle(state))
        row = Adw.ActionRow(title=spec.name, subtitle=_subtitle(state))
        if spec.kind == pragma_rules.CHECK:
            run = Gtk.Button(label="Run", valign=Gtk.Align.CENTER)
            run.connect("clicked", self._run_check, state)
            row.add_suffix(run)
        else:
            value = Gtk.Label(label=state.display_value, xalign=1)
            value.add_css_class("dim-label")
            row.add_suffix(value)
        return row


    # Changing

    def _switch_changed(self, row, _param, state) -> None:
        if self._loading:
            return
        self._apply(state, "1" if row.get_active() else "0")

    def _combo_changed(self, row, _param, state) -> None:
        if self._loading:
            return
        labels = pragma_rules.choice_labels(state.spec)
        index = row.get_selected()
        if 0 <= index < len(labels):
            self._apply(state, labels[index][0])

    def _entry_applied(self, row, state) -> None:
        if self._loading:
            return
        self._apply(state, row.get_text())

    def _apply(self, state: pragma_rules.PragmaState, value) -> None:
        """Validate, confirm where the change outlives the session, run
        it, then re-read. A refused value or a cancelled confirmation
        redraws the row rather than leaving the control lying."""
        spec = state.spec
        try:
            statement = pragma_rules.statement(spec, value)
        except pragma_rules.PragmaError as exc:
            self._show_error(str(exc))
            self.reload()
            return
        if pragma_rules.normalize(spec, value) == (state.value or "").lower():
            return  # a redraw's echo, or the value it already had
        if not spec.needs_confirmation and not spec.warning:
            self._run(spec, value)
            return
        confirm.present(
            self,
            heading=f"Change {spec.name}?",
            body=(
                (spec.warning + " " if spec.warning else "")
                + f"This change {spec.scope_label} on "
                + f"{confirm.describe_connection(self.profile)}."
            ),
            statement=statement + ";",
            confirm_label="Apply",
            level="confirm",
            on_confirm=lambda: self._run(spec, value),
        )
        # Whatever the answer, the row goes back to the database's
        # value until an apply has actually happened.
        self.reload()

    def _run(self, spec, value) -> None:
        def work():
            provider = registry.create_provider(
                self.profile.kind, self._ensure(self.profile)
            )
            return provider.set_pragma(spec.name, value)

        def done(_state) -> None:
            # Re-read the whole list rather than the one row: several
            # pragmas move together (journal_mode and synchronous, page
            # counts after a rewrite).
            self.reload()

        def failed(exc: Exception) -> None:
            self._show_error(f"{spec.name}: {exc}")
            self.reload()

        run_async(work, done, failed)

    # Checks

    def _run_check(self, button: Gtk.Button, state) -> None:
        button.set_sensitive(False)

        def work():
            provider = registry.create_provider(
                self.profile.kind, self._ensure(self.profile)
            )
            return provider.run_pragma_check(state.name)

        def done(result) -> None:
            button.set_sensitive(True)
            grid = ResultGrid()
            grid.set_result(list(result.columns), list(result.rows))
            dialog = Adw.Dialog(
                title=f"{state.name} on {self.profile.name}",
                content_width=640,
                content_height=420,
            )
            view = Adw.ToolbarView(content=grid)
            view.add_top_bar(Adw.HeaderBar())
            dialog.set_child(view)
            dialog.present(self)

        def failed(exc: Exception) -> None:
            button.set_sensitive(True)
            self._show_error(f"{state.name}: {exc}")

        run_async(work, done, failed)

    # Defaults (CORE-13)

    def _default_lines(self) -> list[str]:
        """The settings that differ from SQLite's own defaults, as the
        lines the profile stores. Only the differences: a file listing
        every pragma at its default says nothing, and goes stale the
        day SQLite changes one."""
        values = {
            state.name: state.value
            for state in self._states.values()
            if state.spec.editable and not state.error and not state.is_default
        }
        return pragma_rules.format_defaults(values)

    def _save_defaults(self) -> None:
        lines = self._default_lines()
        self._on_save_defaults(self.profile, lines)
        body = (
            "\n".join(lines)
            if lines
            else "Every setting is at SQLite's default, so nothing is stored."
        )
        dialog = Adw.AlertDialog(
            heading="Saved as connection defaults",
            body=(
                f"These are applied every time {self.profile.name} "
                "connects.\n\n" + body
                if lines
                else body
            ),
        )
        dialog.add_response("ok", "OK")
        dialog.present(self)


def _with_note(row: Adw.EntryRow, note: str) -> Gtk.Widget:
    """An EntryRow has no subtitle, so its description goes under it —
    a numeric pragma needs its units and its default spelled out as
    much as a switch does."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
    listbox.add_css_class("boxed-list")
    listbox.append(row)
    box.append(listbox)
    label = Gtk.Label(
        label=note,
        xalign=0,
        wrap=True,
        margin_top=4,
        margin_start=12,
        margin_bottom=6,
    )
    label.add_css_class("dim-label")
    label.add_css_class("caption")
    box.append(label)
    return box
