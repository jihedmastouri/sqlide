"""Table designer tab: a form that generates CREATE TABLE.

The one create flow that earns a form (everything else gets a dialect
template in a query console). Each column is a two-line card: name,
type and remove/reorder on top; primary key, NOT NULL and DEFAULT
below. The type is picked from the adapter's own list
(`column_type_specs`), and a type that takes arguments — VARCHAR's
length, DECIMAL's precision and scale, ENUM's values — grows the
entries for exactly those arguments, prefilled with a sane default.
"Custom…" keeps free text available for anything the list misses.

Below the columns, a live read-only preview of the generated statement,
rebuilt on every keystroke from the tab's TableModel through
`backend/db/table_model.render_create`, so the model, the quoting and
the dialect quirks all stay in the backend. While the form is incomplete
the preview says *what* is missing and Create is insensitive with the
same reason — never a generic "something is wrong". Create shows the
statement in an UpdatePreviewDialog before anything runs; on success
the window reloads the sidebar and opens the new table's data tab.

Session-only: tab_state() returns None, the tab is not restored.
"""

from __future__ import annotations

from typing import Callable

from gi.repository import Gdk, Gtk

from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.db.base import Connector, TypeSpec
from sqlide.backend.db.table_model import (
    ColumnDefault,
    ColumnModel,
    TableModel,
    render_create,
)
from sqlide.frontend.data_grid import UpdatePreviewDialog
from sqlide.frontend.sql_editor import SqlEditor
from sqlide.frontend.util import describe, run_async
from sqlide.i18n import _

# Last entry of every type dropdown: free text, for the types no list
# can enumerate (domains, extensions, arrays).
_CUSTOM = "Custom…"

# How many argument entries a row keeps around; no dialect type in the
# list takes more (precision + scale is the widest).
_MAX_PARAMS = 2


class _ColumnRow(Gtk.ListBoxRow):
    """One column of the future table."""

    def __init__(
        self,
        specs: list[TypeSpec],
        on_changed: Callable[[], None],
        on_remove: Callable[["_ColumnRow"], None],
        on_move: Callable[["_ColumnRow", int], None],
    ) -> None:
        super().__init__(activatable=False, selectable=False)
        self._on_changed = on_changed
        self._specs: list[TypeSpec] = []

        outer = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=6,
            margin_top=8,
            margin_bottom=8,
            margin_start=8,
            margin_end=8,
        )

        top = Gtk.Box(spacing=6)
        self.name = Gtk.Entry(placeholder_text="column_name", hexpand=True)
        self.name.connect("changed", lambda *_: on_changed())
        self._type = Gtk.DropDown(model=Gtk.StringList.new([_CUSTOM]))
        self._type.set_expression(
            Gtk.PropertyExpression.new(Gtk.StringObject, None, "string")
        )
        self._type.set_enable_search(True)
        describe(self._type, _("Column type"))
        self._type.connect("notify::selected", self._on_type_selected)
        # One entry per argument the selected type takes; hidden for the
        # types that take none.
        self._params = []
        for _index in range(_MAX_PARAMS):
            entry = Gtk.Entry(width_chars=8, visible=False)
            entry.connect("changed", lambda *_: on_changed())
            self._params.append(entry)
        self._custom = Gtk.Entry(
            placeholder_text="type", width_chars=16, visible=False
        )
        self._custom.connect("changed", lambda *_: on_changed())
        up = Gtk.Button(icon_name="go-up-symbolic")
        up.add_css_class("flat")
        describe(up, _("Move this column up"))
        up.connect("clicked", lambda *_: on_move(self, -1))
        down = Gtk.Button(icon_name="go-down-symbolic")
        down.add_css_class("flat")
        describe(down, _("Move this column down"))
        down.connect("clicked", lambda *_: on_move(self, 1))
        remove = Gtk.Button(icon_name="user-trash-symbolic")
        remove.add_css_class("flat")
        describe(remove, _("Remove this column"))
        remove.connect("clicked", lambda *_: on_remove(self))
        for child in (
            self.name, self._type, *self._params, self._custom,
            up, down, remove,
        ):
            top.append(child)
        outer.append(top)

        below = Gtk.Box(spacing=12)
        self.pk = Gtk.CheckButton(label=_("Primary key"))
        self.pk.connect("toggled", self._on_pk_toggled)
        # NOT NULL rather than the "NULL" it negates: the checkbox
        # should read the way the DDL it writes does.
        self.not_null = Gtk.CheckButton(label=_("Not null"))
        self.not_null.connect("toggled", lambda *_: on_changed())
        default_label = Gtk.Label(label=_("Default"))
        default_label.add_css_class("dim-label")
        self.default = Gtk.Entry(
            placeholder_text=_("SQL expression"), width_chars=14
        )
        self.default.set_tooltip_text(
            "DEFAULT expression, inserted verbatim (quote string literals)"
        )
        self.default.connect("changed", lambda *_: on_changed())
        self._note = Gtk.Label(xalign=0, hexpand=True)
        self._note.add_css_class("dim-label")
        self._note.add_css_class("caption")
        for child in (
            self.pk, self.not_null, default_label, self.default, self._note
        ):
            below.append(child)
        outer.append(below)

        self.set_child(outer)
        self.set_specs(specs)

    # Type list

    def set_specs(self, specs: list[TypeSpec]) -> None:
        """Swap in the adapter's type list (it arrives after connect),
        keeping whatever type the row already shows."""
        previous = self.type_text()
        self._specs = list(specs)
        names = [spec.name for spec in self._specs] + [_CUSTOM]
        self._type.set_model(Gtk.StringList.new(names))
        index = next(
            (i for i, spec in enumerate(self._specs) if spec.name == previous),
            len(self._specs),  # no match: fall back to Custom…
        )
        self._type.set_selected(index)
        if index == len(self._specs) and previous:
            self._custom.set_text(previous)
        self._sync_type_fields(reset_params=index != len(self._specs))

    def _selected_spec(self) -> TypeSpec | None:
        """The chosen TypeSpec, or None while "Custom…" is selected."""
        index = self._type.get_selected()
        if 0 <= index < len(self._specs):
            return self._specs[index]
        return None

    def _on_type_selected(self, *_args) -> None:
        self._sync_type_fields(reset_params=True)
        self._on_changed()

    def _sync_type_fields(self, reset_params: bool) -> None:
        """Show exactly the argument entries the selected type takes,
        named and (on a fresh pick) prefilled from the spec."""
        spec = self._selected_spec()
        self._custom.set_visible(spec is None)
        params = spec.params if spec is not None else ()
        defaults = spec.defaults if spec is not None else ()
        for i, entry in enumerate(self._params):
            entry.set_visible(i < len(params))
            if i >= len(params):
                continue
            entry.set_placeholder_text(params[i])
            entry.set_tooltip_text(f"{params[i]} for {spec.name}")
            if reset_params:
                entry.set_text(defaults[i] if i < len(defaults) else "")
        self._note.set_text(spec.note if spec is not None else "")

    def _on_pk_toggled(self, *_args) -> None:
        # A primary key column is NOT NULL whether or not the DDL says
        # so; show that instead of letting the two contradict.
        if self.pk.get_active():
            self.not_null.set_active(True)
        self.not_null.set_sensitive(not self.pk.get_active())
        self._on_changed()

    # Values

    def name_text(self) -> str:
        return self.name.get_text().strip()

    def type_text(self) -> str:
        spec = self._selected_spec()
        if spec is None:
            return self._custom.get_text().strip()
        return spec.render([entry.get_text() for entry in self._params])

    def default_text(self) -> str:
        return self.default.get_text().strip()

    def is_empty(self) -> bool:
        """A row nobody has touched — ignored by validation so a spare
        blank row at the bottom is not an error."""
        return not self.name_text() and not self.default_text()

    def column(self) -> ColumnModel | None:
        """The row as a ColumnModel, or None while the name is empty."""
        if not self.name_text():
            return None
        default = self.default_text()
        return ColumnModel(
            name=self.name_text(),
            type=self.type_text(),
            primary_key=self.pk.get_active(),
            nullable=not self.not_null.get_active(),
            # The entry is free-text SQL the user vouches for, exactly
            # as it always was; a typed default picker is CORE-24's.
            default=(
                ColumnDefault("expression", default)
                if default
                else ColumnDefault()
            ),
        )


class TableDesignerTab(Gtk.Box):
    def __init__(
        self,
        profile: ConnectionProfile,
        ensure_connector: Callable[[ConnectionProfile], Connector],
        show_error: Callable[[str], None],
        on_created: Callable[[str], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.profile = profile
        self._ensure = ensure_connector
        self._show_error = show_error
        self._on_created = on_created
        self.on_ran: Callable[[str, bool], None] | None = None
        self._connector: Connector | None = None
        self._specs: list[TypeSpec] = []
        self._rows: list[_ColumnRow] = []

        bar = Gtk.Box(
            spacing=6,
            margin_top=6,
            margin_bottom=6,
            margin_start=6,
            margin_end=6,
        )
        name_label = Gtk.Label(label=_("Table"))
        name_label.add_css_class("dim-label")
        self._table_name = Gtk.Entry(
            placeholder_text="table_name", hexpand=True
        )
        self._table_name.connect("changed", lambda *_: self._refresh())
        add = Gtk.Button(label=_("Add Column"))
        add.connect("clicked", lambda *_: self._add_row(focus=True))
        self._create = Gtk.Button(label=_("Create"))
        self._create.add_css_class("suggested-action")
        self._create.connect("clicked", self._on_create_clicked)
        bar.append(name_label)
        bar.append(self._table_name)
        bar.append(add)
        bar.append(self._create)
        self.append(bar)

        self._list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self._list.add_css_class("boxed-list-separate")
        columns = Gtk.ScrolledWindow(child=self._list, vexpand=True)

        preview_bar = Gtk.Box(
            spacing=6, margin_start=6, margin_end=6, margin_top=6
        )
        caption = Gtk.Label(
            label=_("Generated statement"), xalign=0, hexpand=True
        )
        caption.add_css_class("dim-label")
        caption.add_css_class("caption")
        copy = Gtk.Button(icon_name="edit-copy-symbolic")
        copy.add_css_class("flat")
        describe(copy, _("Copy the generated statement"))
        copy.connect("clicked", lambda *_: self._copy_sql())
        preview_bar.append(caption)
        preview_bar.append(copy)
        self._preview = SqlEditor(editable=False)
        preview_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        preview_box.append(preview_bar)
        preview_box.append(self._preview)
        self._preview.set_vexpand(True)

        # Columns above, the statement they generate below, with the
        # split under the user's control: some tables are 30 columns
        # long, some statements are 30 lines long.
        split = Gtk.Paned(
            orientation=Gtk.Orientation.VERTICAL,
            vexpand=True,
            position=340,
            shrink_start_child=False,
            shrink_end_child=False,
            resize_end_child=False,
        )
        split.set_start_child(columns)
        split.set_end_child(preview_box)
        self.append(split)

        self._add_row()

        def work():
            connector = self._ensure(self.profile)
            return connector, connector.column_type_specs()

        def ready(loaded) -> None:
            self._connector, self._specs = loaded
            for row in self._rows:
                row.set_specs(self._specs)
            self._refresh()

        run_async(work, ready, lambda exc: self._show_error(str(exc)))
        self._refresh()

    def tab_state(self) -> None:
        return None  # session-only

    # Columns

    def _add_row(self, focus: bool = False) -> None:
        row = _ColumnRow(
            self._specs, self._refresh, self._remove_row, self._move_row
        )
        self._rows.append(row)
        self._list.append(row)
        if focus:
            row.name.grab_focus()
        self._refresh()

    def _remove_row(self, row: _ColumnRow) -> None:
        if len(self._rows) == 1:
            # Removing the only column would leave a form that cannot
            # describe a table; clearing it is what was meant.
            row.name.set_text("")
            row.default.set_text("")
            row.pk.set_active(False)
            row.not_null.set_active(False)
            return
        self._rows.remove(row)
        self._list.remove(row)
        self._refresh()

    def _move_row(self, row: _ColumnRow, step: int) -> None:
        """Column order is table order, so it has to be editable."""
        index = self._rows.index(row)
        target = index + step
        if not 0 <= target < len(self._rows):
            return
        self._rows.insert(target, self._rows.pop(index))
        self._list.remove(row)
        self._list.insert(row, target)
        self._refresh()

    # Validation, preview, create

    def _problem(self) -> str:
        """Why the form cannot be turned into a statement yet, as a
        sentence naming the offending field — or "" when it can."""
        if self._connector is None:
            return "Connecting to the database…"
        if not self._table_name.get_text().strip():
            return "Give the table a name."
        filled = [row for row in self._rows if not row.is_empty()]
        if not filled:
            return "Add at least one column: a name and a type."
        seen: set[str] = set()
        for position, row in enumerate(filled, start=1):
            name = row.name_text()
            if not name:
                return f"Column {position} has no name."
            if not row.type_text():
                return f"Column “{name}” has no type."
            if name.lower() in seen:
                return f"Two columns are named “{name}”."
            seen.add(name.lower())
        return ""

    def model(self) -> TableModel:
        """The form as a TableModel. The one thing the widget produces;
        everything downstream — the preview, the statement that runs,
        and later a saved design (CORE-28) — is a function of it."""
        return TableModel(
            name=self._table_name.get_text().strip(),
            columns=tuple(c for c in (row.column() for row in self._rows) if c),
        )

    def _build_sql(self) -> str:
        """The CREATE statement for the current form, or "" while the
        form is incomplete (see _problem). No SQL is assembled here —
        the renderer in backend/db/table_model.py does all of it."""
        if self._problem():
            return ""
        return render_create(self.model(), self._connector)

    def _refresh(self) -> None:
        problem = self._problem()
        self._create.set_sensitive(not problem)
        self._create.set_tooltip_text(
            problem
            or "Show the generated CREATE TABLE for review, then run it"
        )
        if problem:
            self._preview.set_text(f"-- {problem}")
        else:
            self._preview.set_text(self._build_sql() + ";")

    def _copy_sql(self) -> None:
        display = Gdk.Display.get_default()
        if display is not None:
            display.get_clipboard().set(self._preview.get_text())

    def _on_create_clicked(self, *_args) -> None:
        problem = self._problem()
        if problem:
            self._show_error(problem)
            return
        sql = self._build_sql()
        UpdatePreviewDialog(
            [sql + ";"],
            lambda: self._execute(sql),
            caption="Runs one CREATE TABLE statement on "
            f"“{self.profile.name}”.",
            width=720,
            height=520,
        ).present(self)

    def _execute(self, sql: str) -> None:
        table = self._table_name.get_text().strip()

        def done(_result) -> None:
            if self.on_ran is not None:
                self.on_ran(sql, True)
            self._on_created(table)

        def failed(exc: Exception) -> None:
            self._show_error(str(exc))
            if self.on_ran is not None:
                self.on_ran(sql, False)

        run_async(
            lambda: self._ensure(self.profile).execute(sql), done, failed
        )
