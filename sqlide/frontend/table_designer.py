"""Table designer tab: a form that generates CREATE TABLE.

The one create flow that earns a form (everything else gets a dialect
template in a query console). Each column is a two-line card: name,
type and remove/reorder on top; primary key, NOT NULL and DEFAULT
below. The type is picked from the list the MetadataProvider gives
(`column_type_specs`), and a type that takes arguments — VARCHAR's
length, DECIMAL's precision and scale, ENUM's values — grows the
entries for exactly those arguments, however many it declares,
prefilled with a sane default. "Custom…" keeps free text available for
anything the list misses.

Everything the tab knows about the target database comes through the
provider (CORE-24): the type list, the capability flags, and the
schemas. Where the `schemas` capability is on, the top bar gets a
schema chooser filled from the provider and opened on the schema of
the sidebar node the designer was launched from, and the chosen schema
goes into `TableModel.schema` so the renderer qualifies the name. Where
it is off — MySQL, SQLite — no chooser appears and the name stays
bare. There is no engine name anywhere in this file.

Columns are one of three views of the same TableModel, switched in the
top bar (CORE-25): **Constraints** is a list of rows — a kind, an
optional name, the columns it covers, and the fields that kind needs:
a CHECK expression, or a referenced table (offered from the provider's
own sources, with its columns read from the catalog) plus ON DELETE and
ON UPDATE actions. **Indexes** is name, columns with a direction each,
unique, and the method and partial predicate where the engine has them.
Which constraint kinds are offered and which index fields appear come
from the backend `Dialect`'s flags, never from an engine name. The
per-column primary-key checkbox stays and mirrors both ways: ticking it
shows up as a PRIMARY KEY row in the Constraints view, and editing that
row ticks the boxes.

Below the views, a live read-only preview of the generated statements,
rebuilt on every keystroke from the tab's TableModel through
`backend/db/table_model.plan`, so the model, the quoting and the
dialect quirks all stay in the backend. Indexes are objects of their
own on every engine we speak to, so what the preview shows — and what
the confirmation dialog lists — is a script: the CREATE TABLE and a
CREATE INDEX for each index. While the form is incomplete
the preview says *what* is missing and Create is insensitive with the
same reason — never a generic "something is wrong". Create shows every
statement in a PlanPreviewDialog before anything runs; on success
the window reloads the sidebar and opens the table's data tab.

The same designer edits a table that already exists (CORE-26). Opened
with a `table_ref`, it loads that table through the MetadataProvider
(`TableModel.from_provider`) and shows it as rows; the button says
**Apply**, and what it applies is `plan(loaded, edited)` — the same
call a create makes with `None` for the loaded model. So a rename, a
type change and a dropped column are diffs, not a second feature, and
the dialog groups the plan by how dangerous each statement is with the
destructive group first and Apply unfocused. Where a statement can be
refused by the rows already there — a NOT NULL over nulls, a UNIQUE
over duplicates — the dialog asks the server for the offending count
first (`preflight`) instead of letting the server say no.

Session-only: tab_state() returns None, the tab is not restored.
"""

from __future__ import annotations

from typing import Callable

from gi.repository import Adw, Gdk, Gtk

from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.db import registry
from sqlide.backend.db.base import Connector, TypeSpec
from sqlide.backend.db.metadata import Capabilities, NodeRef
from sqlide.backend.db.table_model import (
    CASCADE_ACTIONS,
    CLASSIFICATIONS,
    ColumnDefault,
    ColumnModel,
    ConstraintModel,
    GENERIC,
    IndexModel,
    Statement,
    TableModel,
    dialect_for,
    plan,
    preflight,
    render_create,
    worst,
)
from sqlide.frontend.sql_editor import SqlEditor
from sqlide.frontend.util import describe, run_async
from sqlide.i18n import _, ngettext

# Last entry of every type dropdown: free text, for the types no list
# can enumerate (domains, extensions, arrays).
_CUSTOM = "Custom…"


#: How each classification reads in the plan dialog, worst first. The
#: notes the planner attaches are untranslated by design (it is the
#: backend); the headings are this side of the line, so they go
#: through gettext here.
_GROUPS = (
    ("destructive", "Loses data"),
    ("may_fail", "May be refused by the rows already there"),
    ("rewrite", "Rewrites the table"),
    ("safe", "Safe"),
)


class PlanPreviewDialog(Adw.Dialog):
    """The plan, grouped by how dangerous it is, before it runs.

    The old preview was a flat list of SQL and left it to the reader to
    notice that one of the statements dropped a column. This one leads
    with the dangerous groups — destructive first, then what the
    existing rows can refuse, then what rewrites the table — and each
    statement carries the planner's one-line note. Apply is never the
    focused widget: Cancel is, so a reflexive Enter cancels rather than
    migrates (CORE-26).

    `checks` are the pre-flight counts: cheap questions asked of the
    server while the dialog is open, so a NOT NULL that would be
    refused says *how many* rows refuse it instead of failing later
    with a one-line server error.
    """

    def __init__(
        self,
        statements: list[Statement],
        on_execute: Callable[[], None],
        caption: str = "",
        run_checks: Callable[[Callable[[list[str]], None]], None] | None = None,
    ) -> None:
        super().__init__(
            title=_("Review Plan ({count})").format(count=len(statements)),
            content_width=720,
            content_height=560,
        )
        self._on_execute = on_execute
        header = Adw.HeaderBar()
        header.set_show_start_title_buttons(False)
        header.set_show_end_title_buttons(False)
        cancel = Gtk.Button(label=_("Cancel"))
        cancel.connect("clicked", lambda *_a: self.close())
        apply_button = Gtk.Button(label=_("Apply"))
        apply_button.add_css_class(
            "destructive-action"
            if worst(statements) in ("destructive", "may_fail")
            else "suggested-action"
        )
        apply_button.connect("clicked", self._on_apply)
        # Not the default and not focused: the dangerous button is the
        # one you have to aim at.
        apply_button.set_can_focus(True)
        header.pack_start(cancel)
        header.pack_end(apply_button)

        body = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        self._checks = Gtk.Label(xalign=0, wrap=True, visible=False)
        self._checks.add_css_class("dim-label")
        body.append(self._checks)
        for classification, heading in _GROUPS:
            group = [
                statement
                for statement in statements
                if statement.classification == classification
            ]
            if not group:
                continue
            body.append(self._group(classification, heading, group))
        scroller = Gtk.ScrolledWindow(child=body, vexpand=True)

        note = Gtk.Label(
            label=caption
            or _("The statements run in order, top to bottom."),
            xalign=0,
            wrap=True,
            margin_start=12,
            margin_end=12,
            margin_bottom=12,
        )
        note.add_css_class("dim-label")
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(scroller)
        content.append(note)
        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(content)
        self.set_child(view)
        self.set_focus(cancel)

        if run_checks is not None:
            self._checks.set_visible(True)
            self._checks.set_text(_("Checking the existing rows…"))
            run_checks(self._checked)

    def _group(
        self, classification: str, heading: str, group: list[Statement]
    ) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        title = Gtk.Label(
            label=f"{_(heading)} ({len(group)})", xalign=0
        )
        title.add_css_class("heading")
        if classification in ("destructive", "may_fail"):
            title.add_css_class("error")
        box.append(title)
        for statement in group:
            if statement.note:
                note = Gtk.Label(label=_(statement.note), xalign=0, wrap=True)
                note.add_css_class("dim-label")
                note.add_css_class("caption")
                box.append(note)
            text = Gtk.TextView(
                editable=False,
                monospace=True,
                cursor_visible=False,
                wrap_mode=Gtk.WrapMode.WORD_CHAR,
                left_margin=8,
                right_margin=8,
                top_margin=4,
                bottom_margin=8,
            )
            text.get_buffer().set_text(statement.sql.rstrip(";") + ";")
            box.append(text)
        return box

    def _checked(self, lines: list[str]) -> None:
        self._checks.set_text(
            "\n".join(lines)
            if lines
            else _("The existing rows satisfy every new constraint.")
        )
        if lines:
            self._checks.add_css_class("error")

    def _on_apply(self, *_args) -> None:
        self.close()
        self._on_execute()

class _ColumnRow(Gtk.ListBoxRow):
    """One column of the future table."""

    def __init__(
        self,
        specs: list[TypeSpec],
        on_changed: Callable[[], None],
        on_remove: Callable[["_ColumnRow"], None],
        on_move: Callable[["_ColumnRow", int], None],
        on_pk: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(activatable=False, selectable=False)
        self._on_changed = on_changed
        # The primary-key checkbox reports separately, because it also
        # has to reach the constraints view (CORE-25).
        self._on_pk = on_pk or on_changed
        self._specs: list[TypeSpec] = []
        # The column as the catalog handed it over, for a row that came
        # from an existing table. It is what a rename is measured
        # against (CORE-26); None for a column being invented here.
        self.loaded: ColumnModel | None = None

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
        # One entry per argument the selected type takes, built to fit
        # the spec rather than to a fixed cap: a type declaring three
        # or more arguments gets three or more entries (CORE-24).
        self._params: list[Gtk.Entry] = []
        self._param_box = Gtk.Box(spacing=6)
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
            self.name, self._type, self._param_box, self._custom,
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
        # Grow or shrink the row to the arity the spec declares. No
        # cap: DECIMAL(p, s) is not the widest type an engine can
        # offer, and a wider one used to be pushed into "Custom…".
        while len(self._params) < len(params):
            entry = Gtk.Entry(width_chars=8)
            entry.connect("changed", lambda *_: self._on_changed())
            self._params.append(entry)
            self._param_box.append(entry)
        while len(self._params) > len(params):
            entry = self._params.pop()
            self._param_box.remove(entry)
        for i, entry in enumerate(self._params):
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
        self._on_pk()

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

    def set_column(self, column: ColumnModel) -> None:
        """Fill the row from a column of an existing table.

        The type goes in as the catalog spells it: a `character
        varying(40)` that matches no entry in the type list lands in
        "Custom…" verbatim, which is exactly what keeps loading a table
        and applying it back unchanged from planning anything.
        """
        self.loaded = column
        self.name.set_text(column.name)
        self.set_specs(self._specs)  # re-pick the type list for this type
        self._select_type(column.type)
        self.pk.set_active(column.primary_key)
        self.not_null.set_active(not column.nullable)
        self.default.set_text(
            column.default.value if column.default.present else ""
        )

    def _select_type(self, type_text: str) -> None:
        index = next(
            (
                i
                for i, spec in enumerate(self._specs)
                if spec.name.lower() == type_text.strip().lower()
            ),
            len(self._specs),
        )
        self._type.set_selected(index)
        if index == len(self._specs):
            self._custom.set_text(type_text)
        self._sync_type_fields(reset_params=False)

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
            # as it always was; a typed default picker is still to come.
            default=(
                ColumnDefault("expression", default)
                if default
                else ColumnDefault()
            ),
            # Only when it actually moved: a column that kept its name
            # carries nothing, so nothing downstream has to special-case
            # "renamed to itself".
            renamed_from=(
                self.loaded.name
                if self.loaded is not None
                and self.loaded.name.lower() != self.name_text().lower()
                else ""
            ),
        )


class _ColumnChooser(Gtk.MenuButton):
    """Pick some of the table's columns, in the order they are ticked.

    A constraint covers columns and an index orders them, so both need
    the same thing: a live list of the columns the designer currently
    has, a tick per column, and — for an index — a direction beside
    each tick. The order is the order the boxes were ticked in, which
    is what a composite key and a multi-column index both care about.
    """

    def __init__(
        self,
        on_changed: Callable[[], None],
        *,
        directions: bool = False,
        placeholder: str = "",
    ) -> None:
        super().__init__()
        self._on_changed = on_changed
        self._directions = directions
        self._placeholder = placeholder or _("Columns")
        self._chosen: list[str] = []
        self._desc: set[str] = set()
        self._available: list[str] = []
        self._box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=4,
            margin_top=8,
            margin_bottom=8,
            margin_start=8,
            margin_end=8,
        )
        self._empty = Gtk.Label(label=_("Name a column first"))
        self._empty.add_css_class("dim-label")
        self._box.append(self._empty)
        self.set_popover(Gtk.Popover(child=self._box))
        describe(self, _("Columns this covers"))
        self._relabel()

    # State

    def columns(self) -> tuple[str, ...]:
        return tuple(self._chosen)

    def directions(self) -> tuple[str, ...]:
        return tuple(
            "DESC" if name in self._desc else "" for name in self._chosen
        )

    def set_columns(
        self, names: list[str], directions: tuple[str, ...] = ()
    ) -> None:
        """Choose exactly `names`, in that order — used to mirror the
        per-column primary-key checkboxes into the constraints view,
        and to fill the row from a table loaded for editing
        (CORE-26). `directions` is positional, like IndexModel's."""
        self._chosen = [n for n in names]
        self._desc = {
            name
            for position, name in enumerate(self._chosen)
            if position < len(directions)
            and directions[position].strip().upper() == "DESC"
        }
        self._rebuild()
        self._relabel()

    def set_available(self, names: list[str]) -> None:
        """The columns the designer currently has. Anything chosen and
        then renamed away drops out, so a constraint can never name a
        column the table does not have."""
        self._available = list(names)
        lowered = {n.lower() for n in names}
        kept = [n for n in self._chosen if n.lower() in lowered]
        # Follow a rename through: same position, new name.
        self._chosen = kept
        self._rebuild()
        self._relabel()

    # Widgets

    def _rebuild(self) -> None:
        child = self._box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._box.remove(child)
            child = nxt
        if not self._available:
            self._box.append(self._empty)
            return
        for name in self._available:
            line = Gtk.Box(spacing=6)
            check = Gtk.CheckButton(label=name, hexpand=True)
            check.set_active(any(c == name for c in self._chosen))
            check.connect("toggled", self._on_toggled, name)
            line.append(check)
            if self._directions:
                order = Gtk.ToggleButton(
                    label=_("DESC"), active=name in self._desc
                )
                order.add_css_class("flat")
                describe(order, _("Sort this column descending"))
                order.connect("toggled", self._on_direction, name)
                line.append(order)
            self._box.append(line)

    def _on_toggled(self, button: Gtk.CheckButton, name: str) -> None:
        if button.get_active():
            if name not in self._chosen:
                self._chosen.append(name)
        elif name in self._chosen:
            self._chosen.remove(name)
        self._relabel()
        self._on_changed()

    def _on_direction(self, button: Gtk.ToggleButton, name: str) -> None:
        if button.get_active():
            self._desc.add(name)
        else:
            self._desc.discard(name)
        self._relabel()
        self._on_changed()

    def _relabel(self) -> None:
        if not self._chosen:
            self.set_label(self._placeholder)
            return
        parts = [
            f"{name} DESC" if self._directions and name in self._desc else name
            for name in self._chosen
        ]
        self.set_label(", ".join(parts))


class _ConstraintRow(Gtk.ListBoxRow):
    """One constraint: its kind, an optional name, the columns it
    covers, and the fields that kind needs — a CHECK expression, or a
    referenced table, its columns and the referential actions."""

    def __init__(
        self,
        kinds: tuple[str, ...],
        on_changed: Callable[[], None],
        on_remove: Callable[["_ConstraintRow"], None],
        ref_tables: Callable[[], list[str]],
        ref_columns: Callable[[str, Callable[[list[str]], None]], None],
    ) -> None:
        super().__init__(activatable=False, selectable=False)
        self._on_changed = on_changed
        self._ref_columns = ref_columns
        self._ref_tables_of = ref_tables
        self._kinds = list(kinds)

        outer = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=6,
            margin_top=8,
            margin_bottom=8,
            margin_start=8,
            margin_end=8,
        )
        top = Gtk.Box(spacing=6)
        self._kind = Gtk.DropDown(model=Gtk.StringList.new(self._kinds))
        describe(self._kind, _("Constraint kind"))
        self._kind.connect("notify::selected", self._on_kind_changed)
        self.name = Gtk.Entry(
            placeholder_text=_("constraint name (optional)"), hexpand=True
        )
        self.name.connect("changed", lambda *_a: on_changed())
        self.columns = _ColumnChooser(on_changed)
        remove = Gtk.Button(icon_name="user-trash-symbolic")
        remove.add_css_class("flat")
        describe(remove, _("Remove this constraint"))
        remove.connect("clicked", lambda *_a: on_remove(self))
        for child in (self._kind, self.name, self.columns, remove):
            top.append(child)
        outer.append(top)

        # CHECK
        self._check_box = Gtk.Box(spacing=6, visible=False)
        check_label = Gtk.Label(label=_("Check"))
        check_label.add_css_class("dim-label")
        self._expression = Gtk.Entry(
            placeholder_text=_("SQL expression"), hexpand=True
        )
        self._expression.connect("changed", lambda *_a: on_changed())
        self._check_box.append(check_label)
        self._check_box.append(self._expression)
        outer.append(self._check_box)

        # FOREIGN KEY
        self._fk_box = Gtk.Box(spacing=6, visible=False)
        references = Gtk.Label(label=_("References"))
        references.add_css_class("dim-label")
        self._ref_table = Gtk.DropDown(model=Gtk.StringList.new([]))
        self._ref_table.set_expression(
            Gtk.PropertyExpression.new(Gtk.StringObject, None, "string")
        )
        self._ref_table.set_enable_search(True)
        describe(self._ref_table, _("Referenced table"))
        self._ref_table.connect("notify::selected", self._on_ref_table)
        self._ref_cols = _ColumnChooser(
            on_changed, placeholder=_("Referenced columns")
        )
        on_delete = Gtk.Label(label=_("On delete"))
        on_delete.add_css_class("dim-label")
        self._on_delete = self._action_dropdown(on_changed)
        on_update = Gtk.Label(label=_("On update"))
        on_update.add_css_class("dim-label")
        self._on_update = self._action_dropdown(on_changed)
        for child in (
            references, self._ref_table, self._ref_cols,
            on_delete, self._on_delete, on_update, self._on_update,
        ):
            self._fk_box.append(child)
        outer.append(self._fk_box)

        self.set_child(outer)
        self._sync_kind_fields()

    def _action_dropdown(self, on_changed) -> Gtk.DropDown:
        # The engine's own default is the first entry, so a foreign key
        # that says nothing renders without an ON … clause.
        names = [_("Default"), *CASCADE_ACTIONS]
        drop = Gtk.DropDown(model=Gtk.StringList.new(names))
        describe(drop, _("Referential action"))
        drop.connect("notify::selected", lambda *_a: on_changed())
        return drop

    @staticmethod
    def _action_of(drop: Gtk.DropDown) -> str:
        index = drop.get_selected()
        return CASCADE_ACTIONS[index - 1] if index >= 1 else ""

    def kind(self) -> str:
        index = self._kind.get_selected()
        if 0 <= index < len(self._kinds):
            return self._kinds[index]
        return self._kinds[0] if self._kinds else "UNIQUE"

    def set_kind(self, kind: str) -> None:
        if kind in self._kinds:
            self._kind.set_selected(self._kinds.index(kind))

    def set_kinds(self, kinds: tuple[str, ...]) -> None:
        """Swap in the kinds this engine enforces (they arrive with the
        connector). Never a branch on an engine name — the dialect
        declares them."""
        previous = self.kind()
        self._kinds = list(kinds)
        self._kind.set_model(Gtk.StringList.new(self._kinds))
        self.set_kind(previous if previous in self._kinds else self.kind())
        self._sync_kind_fields()

    def _on_kind_changed(self, *_args) -> None:
        self._sync_kind_fields()
        self._on_changed()

    def _sync_kind_fields(self) -> None:
        kind = self.kind()
        self._check_box.set_visible(kind == "CHECK")
        self._fk_box.set_visible(kind == "FOREIGN KEY")
        # A CHECK constrains an expression, not a column list.
        self.columns.set_visible(kind != "CHECK")
        if kind == "FOREIGN KEY":
            names = self._ref_tables_of()
            model = self._ref_table.get_model()
            listed = [model.get_string(i) for i in range(model.get_n_items())]
            if listed != names:
                self._ref_table.set_model(Gtk.StringList.new(names))

    def _on_ref_table(self, *_args) -> None:
        table = self.ref_table()
        if table:
            self._ref_columns(table, self._ref_cols.set_available)
        else:
            self._ref_cols.set_available([])
        self._on_changed()

    def ref_table(self) -> str:
        item = self._ref_table.get_selected_item()
        return item.get_string() if item is not None else ""

    def set_constraint(self, con: ConstraintModel) -> None:
        """Fill the row from a constraint of an existing table."""
        self.set_kind(con.kind.upper())
        self.name.set_text(con.name)
        self.columns.set_columns(list(con.columns))
        self._expression.set_text(con.expression)
        qualified = (
            f"{con.ref_schema}.{con.ref_table}"
            if con.ref_schema
            else con.ref_table
        )
        if qualified:
            names = self._ref_tables_of()
            if qualified not in names:
                # A key pointing somewhere the source listing missed is
                # still a key: offer it rather than dropping it.
                names = [qualified, *names]
            self._ref_table.set_model(Gtk.StringList.new(names))
            self._ref_table.set_selected(names.index(qualified))
            self._ref_cols.set_columns(list(con.ref_columns))
        self._set_action(self._on_delete, con.on_delete)
        self._set_action(self._on_update, con.on_update)
        self._sync_kind_fields()

    @staticmethod
    def _set_action(drop: Gtk.DropDown, action: str) -> None:
        action = action.strip().upper()
        drop.set_selected(
            CASCADE_ACTIONS.index(action) + 1
            if action in CASCADE_ACTIONS
            else 0
        )

    def set_available(self, names: list[str]) -> None:
        self.columns.set_available(names)

    def is_empty(self) -> bool:
        return (
            not self.columns.columns()
            and not self._expression.get_text().strip()
            and not self.name.get_text().strip()
        )

    def problem(self) -> str:
        """Why this row cannot be rendered yet, or ""."""
        kind = self.kind()
        if kind == "CHECK":
            if not self._expression.get_text().strip():
                return "A CHECK constraint needs an expression."
            return ""
        if not self.columns.columns():
            return f"A {kind} constraint needs columns."
        if kind == "FOREIGN KEY" and not self.ref_table():
            return "A foreign key needs a referenced table."
        return ""

    def constraint(self) -> ConstraintModel:
        qualified = self.ref_table()
        schema, _sep, table = qualified.rpartition(".")
        return ConstraintModel(
            kind=self.kind(),
            name=self.name.get_text().strip(),
            columns=self.columns.columns(),
            ref_schema=schema,
            ref_table=table,
            ref_columns=self._ref_cols.columns(),
            on_delete=self._action_of(self._on_delete),
            on_update=self._action_of(self._on_update),
            expression=self._expression.get_text().strip(),
        )


class _IndexRow(Gtk.ListBoxRow):
    """One index: a name, the columns it orders, whether it is unique,
    and — where the engine has them — an access method and a partial
    predicate."""

    def __init__(
        self,
        on_changed: Callable[[], None],
        on_remove: Callable[["_IndexRow"], None],
    ) -> None:
        super().__init__(activatable=False, selectable=False)
        self._on_changed = on_changed
        outer = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=6,
            margin_top=8,
            margin_bottom=8,
            margin_start=8,
            margin_end=8,
        )
        top = Gtk.Box(spacing=6)
        self.name = Gtk.Entry(placeholder_text=_("index name"), hexpand=True)
        self.name.connect("changed", lambda *_a: on_changed())
        self.columns = _ColumnChooser(on_changed, directions=True)
        self.unique = Gtk.CheckButton(label=_("Unique"))
        self.unique.connect("toggled", lambda *_a: on_changed())
        remove = Gtk.Button(icon_name="user-trash-symbolic")
        remove.add_css_class("flat")
        describe(remove, _("Remove this index"))
        remove.connect("clicked", lambda *_a: on_remove(self))
        for child in (self.name, self.columns, self.unique, remove):
            top.append(child)
        outer.append(top)

        below = Gtk.Box(spacing=6)
        self._method_label = Gtk.Label(label=_("Method"))
        self._method_label.add_css_class("dim-label")
        self._method = Gtk.Entry(placeholder_text=_("btree"), width_chars=10)
        self._method.connect("changed", lambda *_a: on_changed())
        self._where_label = Gtk.Label(label=_("Where"))
        self._where_label.add_css_class("dim-label")
        self._where = Gtk.Entry(
            placeholder_text=_("SQL predicate"), hexpand=True
        )
        self._where.connect("changed", lambda *_a: on_changed())
        for child in (
            self._method_label, self._method,
            self._where_label, self._where,
        ):
            below.append(child)
        outer.append(below)
        self.set_child(outer)
        self.set_features(method=False, partial=False)

    def set_features(self, *, method: bool, partial: bool) -> None:
        """Show only what this engine's CREATE INDEX accepts — the
        dialect's flags, not an engine name."""
        self._method_label.set_visible(method)
        self._method.set_visible(method)
        self._where_label.set_visible(partial)
        self._where.set_visible(partial)

    def set_index(self, index: IndexModel) -> None:
        """Fill the row from an index of an existing table."""
        self.name.set_text(index.name)
        self.columns.set_columns(list(index.columns), index.directions)
        self.unique.set_active(index.unique)
        self._method.set_text(index.method)
        self._where.set_text(index.where)

    def set_available(self, names: list[str]) -> None:
        self.columns.set_available(names)

    def is_empty(self) -> bool:
        return not self.columns.columns() and not self.name.get_text().strip()

    def problem(self) -> str:
        if not self.columns.columns():
            name = self.name.get_text().strip()
            return (
                f"Index “{name}” has no columns."
                if name
                else "An index needs columns."
            )
        return ""

    def index(self) -> IndexModel:
        return IndexModel(
            name=self.name.get_text().strip(),
            columns=self.columns.columns(),
            directions=self.columns.directions(),
            unique=self.unique.get_active(),
            method=self._method.get_text().strip(),
            where=self._where.get_text().strip(),
        )


class TableDesignerTab(Gtk.Box):
    def __init__(
        self,
        profile: ConnectionProfile,
        ensure_connector: Callable[[ConnectionProfile], Connector],
        show_error: Callable[[str], None],
        on_created: Callable[[str, str], None],
        ref: NodeRef | None = None,
        table_ref: NodeRef | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.profile = profile
        self._ensure = ensure_connector
        self._show_error = show_error
        self._on_created = on_created
        self.on_ran: Callable[[str, bool], None] | None = None
        self._connector: Connector | None = None
        # The table being edited, and the model it was loaded with.
        # `None` is a new table, and then `plan()` gets `None` as its
        # current state and yields a CREATE — create and alter are the
        # same code path over the same model (CORE-26).
        self._table_ref = table_ref
        self._current: TableModel | None = None
        self._specs: list[TypeSpec] = []
        self._rows: list[_ColumnRow] = []
        self._constraint_rows: list[_ConstraintRow] = []
        self._index_rows: list[_IndexRow] = []
        # Guards the two-way primary-key mirroring below: the checkbox
        # writes the constraint row and the constraint row writes the
        # checkbox, and neither may set the other off again.
        self._syncing = False
        self._dialect = GENERIC
        self._sources: list[NodeRef] = []
        # The node the designer was launched from, so a table created
        # off a schema row lands in that schema (CORE-24). Capabilities
        # and the schema list arrive with the catalog load below; until
        # then the chooser stays hidden rather than guessing.
        self._ref = ref
        self._caps = Capabilities()
        self._schemas: list[str] = []
        source = table_ref or ref
        self._wanted_schema = (
            (source.name if source.kind == "schema" else source.schema)
            if source
            else ""
        ) or profile.schema or ""

        bar = Gtk.Box(
            spacing=6,
            margin_top=6,
            margin_bottom=6,
            margin_start=6,
            margin_end=6,
        )
        # Shown only where the provider says schemas are a level of
        # their own; on MySQL and SQLite the chooser never appears.
        self._schema_label = Gtk.Label(label=_("Schema"), visible=False)
        self._schema_label.add_css_class("dim-label")
        self._schema = Gtk.DropDown(
            model=Gtk.StringList.new([]), visible=False
        )
        self._schema.set_expression(
            Gtk.PropertyExpression.new(Gtk.StringObject, None, "string")
        )
        self._schema.set_enable_search(True)
        describe(self._schema, _("Schema the table is created in"))
        self._schema.connect("notify::selected", lambda *_: self._refresh())
        name_label = Gtk.Label(label=_("Table"))
        name_label.add_css_class("dim-label")
        self._table_name = Gtk.Entry(
            placeholder_text="table_name", hexpand=True
        )
        self._table_name.connect("changed", lambda *_: self._refresh())
        # One switcher over three views of the same TableModel; the
        # preview below stays visible in all of them.
        self._stack = Gtk.Stack()
        self._stack.connect("notify::visible-child-name", self._on_view)
        switcher = Gtk.StackSwitcher(stack=self._stack)
        self._add = Gtk.Button(label=_("Add Column"))
        self._add.connect("clicked", lambda *_: self._add_current())
        self._create = Gtk.Button(
            label=_("Apply") if table_ref is not None else _("Create")
        )
        self._create.add_css_class("suggested-action")
        self._create.connect("clicked", self._on_create_clicked)
        bar.append(self._schema_label)
        bar.append(self._schema)
        bar.append(name_label)
        bar.append(self._table_name)
        bar.append(switcher)
        bar.append(self._add)
        bar.append(self._create)
        self.append(bar)

        self._list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self._list.add_css_class("boxed-list-separate")
        self._constraint_list = Gtk.ListBox(
            selection_mode=Gtk.SelectionMode.NONE
        )
        self._constraint_list.add_css_class("boxed-list-separate")
        self._index_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self._index_list.add_css_class("boxed-list-separate")
        for name, title, child in (
            ("columns", _("Columns"), self._list),
            ("constraints", _("Constraints"), self._constraint_list),
            ("indexes", _("Indexes"), self._index_list),
        ):
            page = self._stack.add_titled(
                Gtk.ScrolledWindow(child=child, vexpand=True), name, title
            )
            page.set_name(name)
        columns = self._stack

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
            # Everything the designer knows about the target database
            # comes through the provider (CORE-24): the type list, the
            # capability flags that decide whether a schema is a thing
            # here, and the schemas themselves (from the connection's
            # catalog cache, not a fresh query). The connector is kept
            # only to render and to run the statement.
            connector = self._ensure(self.profile)
            provider = registry.create_provider(self.profile.kind, connector)
            return (
                connector,
                provider.column_type_specs(),
                provider.capabilities(),
                provider.schemas(),
                # Foreign keys point at a table the provider knows
                # about, so the target list is the provider's, not a
                # free-text entry (CORE-24).
                provider.list_sources(),
                # An existing table comes in through the provider, not
                # through a catalog query of the designer's own: the
                # same columns, indexes and keys the sidebar reads.
                (
                    TableModel.from_provider(provider, self._table_ref)
                    if self._table_ref is not None
                    else None
                ),
            )

        def ready(loaded) -> None:
            (
                self._connector,
                self._specs,
                self._caps,
                self._schemas,
                self._sources,
                self._current,
            ) = loaded
            self._dialect = dialect_for(self._connector)
            for row in self._rows:
                row.set_specs(self._specs)
            self._sync_dialect()
            self._sync_schema_chooser()
            if self._current is not None:
                self._populate(self._current)
            self._refresh()

        run_async(work, ready, lambda exc: self._show_error(str(exc)))
        self._refresh()

    def tab_state(self) -> None:
        return None  # session-only

    # Loading an existing table

    @property
    def editing(self) -> bool:
        """Whether this designer was opened on a table that exists."""
        return self._table_ref is not None

    def _populate(self, model: TableModel) -> None:
        """Show `model` in the form — the loaded table, as rows.

        Everything the model carries gets a row: a column row per
        column, a constraint row per constraint, an index row per
        index. What comes back out of the form is compared against this
        same model, so opening a table and pressing Apply without
        touching anything plans nothing at all.
        """
        self._syncing = True
        try:
            self._table_name.set_text(model.name)
            for row in list(self._rows):
                self._rows.remove(row)
                self._list.remove(row)
            for column in model.columns:
                row = self._new_column_row()
                self._rows.append(row)
                self._list.append(row)
                row.set_column(column)
            if not self._rows:
                self._add_row()
            names = self._column_names()
            for constraint in model.constraints:
                if constraint.kind.upper() == "PRIMARY KEY" and (
                    model.primary_key
                ):
                    # Already shown by the columns' own checkboxes.
                    continue
                row = self._add_constraint()
                row.set_available(names)
                row.set_constraint(constraint)
            for index in model.indexes:
                row = self._add_index()
                row.set_available(names)
                row.set_index(index)
        finally:
            self._syncing = False
        self._columns_to_pk()
        self._refresh()

    # Schema

    def _sync_schema_chooser(self) -> None:
        """Fill and show the chooser where the engine has schemas.

        Gated on the capability flag, never on the engine's name: an
        engine without schemas has no list and gets no chooser, and the
        table name goes into the statement bare exactly as before.
        """
        show = bool(self._caps.schemas and self._schemas)
        self._schema_label.set_visible(show)
        self._schema.set_visible(show)
        if not show:
            return
        names = list(self._schemas)
        wanted = self._wanted_schema
        if wanted and wanted not in names:
            # The launching node's schema wins even when it is one the
            # listing leaves out (a system schema, say).
            names.insert(0, wanted)
        self._schema.set_model(Gtk.StringList.new(names))
        self._schema.set_selected(names.index(wanted) if wanted in names else 0)

    def schema(self) -> str:
        """The schema the table is being created in — "" where the
        engine has none, which is what leaves the name unqualified."""
        if not self._schema.get_visible():
            return ""
        item = self._schema.get_selected_item()
        return item.get_string() if item is not None else ""

    # Views

    def _view(self) -> str:
        return self._stack.get_visible_child_name() or "columns"

    def _on_view(self, *_args) -> None:
        """The Add button adds to whatever view is showing — one
        button, three meanings, so the bar does not grow three."""
        view = self._view()
        self._add.set_label(
            {
                "columns": _("Add Column"),
                "constraints": _("Add Constraint"),
                "indexes": _("Add Index"),
            }[view]
        )

    def _add_current(self) -> None:
        view = self._view()
        if view == "constraints":
            self._add_constraint(focus=True)
        elif view == "indexes":
            self._add_index(focus=True)
        else:
            self._add_row(focus=True)

    def _sync_dialect(self) -> None:
        """Push the dialect's capability flags into the rows: the
        constraint kinds the engine enforces, and whether CREATE INDEX
        here takes a method or a partial predicate. No engine name is
        ever tested in this file — the dialect declares all of it."""
        for row in self._constraint_rows:
            row.set_kinds(self._dialect.constraint_kinds)
        for row in self._index_rows:
            row.set_features(
                method=self._dialect.index_method,
                partial=self._dialect.partial_indexes,
            )

    # Constraints and indexes

    def _column_names(self) -> list[str]:
        return [row.name_text() for row in self._rows if row.name_text()]

    def _ref_tables(self) -> list[str]:
        """The tables a foreign key may point at, qualified where the
        engine has schemas."""
        names = []
        for ref in self._sources:
            names.append(
                f"{ref.schema}.{ref.name}"
                if self._dialect.schemas and ref.schema
                else ref.name
            )
        return sorted(dict.fromkeys(names))

    def _ref_columns(
        self, qualified: str, deliver: Callable[[list[str]], None]
    ) -> None:
        """The referenced table's columns, from the provider, off the
        main thread — the FK column chooser fills itself from the same
        catalog the sidebar reads."""
        schema, _sep, table = qualified.rpartition(".")

        def work():
            connector = self._ensure(self.profile)
            provider = registry.create_provider(self.profile.kind, connector)
            ref = NodeRef(kind="table", name=table, schema=schema)
            return [column.name for column in provider.columns_of(ref)]

        run_async(work, deliver, lambda exc: self._show_error(str(exc)))

    def _add_constraint(self, focus: bool = False) -> _ConstraintRow:
        row = _ConstraintRow(
            self._dialect.constraint_kinds,
            self._constraint_changed,
            self._remove_constraint,
            self._ref_tables,
            self._ref_columns,
        )
        row.set_available(self._column_names())
        self._constraint_rows.append(row)
        self._constraint_list.append(row)
        if focus:
            row.name.grab_focus()
        self._refresh()
        return row

    def _remove_constraint(self, row: _ConstraintRow) -> None:
        self._constraint_rows.remove(row)
        self._constraint_list.remove(row)
        if row.kind() == "PRIMARY KEY":
            self._pk_to_columns(())
        self._refresh()

    def _add_index(self, focus: bool = False) -> _IndexRow:
        row = _IndexRow(self._refresh, self._remove_index)
        row.set_features(
            method=self._dialect.index_method,
            partial=self._dialect.partial_indexes,
        )
        row.set_available(self._column_names())
        self._index_rows.append(row)
        self._index_list.append(row)
        if focus:
            row.name.grab_focus()
        self._refresh()
        return row

    def _remove_index(self, row: _IndexRow) -> None:
        self._index_rows.remove(row)
        self._index_list.remove(row)
        self._refresh()

    # The primary key, from both ends

    def _pk_row(self) -> _ConstraintRow | None:
        for row in self._constraint_rows:
            if row.kind() == "PRIMARY KEY":
                return row
        return None

    def _pk_to_columns(self, names: tuple[str, ...]) -> None:
        """Mirror the constraints view's primary key onto the column
        checkboxes."""
        wanted = {n.lower() for n in names}
        self._syncing = True
        try:
            for row in self._rows:
                row.pk.set_active(row.name_text().lower() in wanted)
        finally:
            self._syncing = False

    def _columns_to_pk(self) -> None:
        """Mirror the column checkboxes into the constraints view: the
        checkbox is the fast path, not a second source of truth, so
        ticking it shows up as a PRIMARY KEY row and clearing the last
        one takes the row away again."""
        flagged = [row.name_text() for row in self._rows if row.pk.get_active()]
        row = self._pk_row()
        self._syncing = True
        try:
            if flagged and row is None:
                row = self._add_constraint()
                row.set_kind("PRIMARY KEY")
            if row is None:
                return
            if flagged:
                row.columns.set_columns(flagged)
            else:
                self._constraint_rows.remove(row)
                self._constraint_list.remove(row)
        finally:
            self._syncing = False

    # Columns

    def _new_column_row(self) -> _ColumnRow:
        return _ColumnRow(
            self._specs,
            self._refresh,
            self._remove_row,
            self._move_row,
            self._pk_toggled,
        )

    def _add_row(self, focus: bool = False) -> None:
        row = self._new_column_row()
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

    def _pk_toggled(self) -> None:
        if self._syncing:
            return
        self._columns_to_pk()
        self._refresh()

    def _constraint_changed(self) -> None:
        if not self._syncing:
            row = self._pk_row()
            if row is not None:
                self._pk_to_columns(row.columns.columns())
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
        for row in self._constraint_rows:
            if row.is_empty():
                continue  # an untouched row is not an error
            problem = row.problem()
            if problem:
                return problem
        for row in self._index_rows:
            if row.is_empty():
                continue
            problem = row.problem()
            if problem:
                return problem
        return ""

    def _nothing_to_do(self) -> bool:
        """An edited table whose form still describes what is already
        there: the plan is empty, and there is nothing to apply."""
        return self.editing and not self._problem() and not self.statements()

    def model(self) -> TableModel:
        """The form as a TableModel. The one thing the widget produces;
        everything downstream — the preview, the statement that runs,
        and later a saved design (CORE-28) — is a function of it."""
        return TableModel(
            name=self._table_name.get_text().strip(),
            schema=self.schema(),
            columns=tuple(c for c in (row.column() for row in self._rows) if c),
            constraints=tuple(
                row.constraint()
                for row in self._constraint_rows
                if not row.is_empty() and not row.problem()
            ),
            indexes=tuple(
                row.index()
                for row in self._index_rows
                if not row.is_empty() and not row.problem()
            ),
        )

    def _build_sql(self) -> str:
        """The CREATE statement for the current form, or "" while the
        form is incomplete (see _problem). No SQL is assembled here —
        the renderer in backend/db/table_model.py does all of it."""
        if self._problem():
            return ""
        return render_create(self.model(), self._connector)

    def statements(self) -> list[Statement]:
        """The plan: the classified statements that turn the table as
        it is into the table the form describes. For a new table
        `self._current` is None and that is one CREATE — create and
        alter differ only in what is passed here (CORE-26)."""
        if self._problem():
            return []
        return plan(self._current, self.model(), self._connector)

    def _statements(self) -> list[str]:
        """Everything the designer will run, in order: the CREATE, then
        the CREATE INDEX for each index and whatever else the engine
        needs beside it. `plan()` decides — indexes are objects of
        their own on every engine we speak to, so the preview is a
        script, not one statement."""
        if self._problem():
            return []
        return [statement.sql for statement in self.statements()]

    def _refresh(self) -> None:
        # Constraints and indexes cover columns, so every view follows
        # the column names as they are typed.
        names = self._column_names()
        for row in self._constraint_rows:
            row.set_available(names)
        for row in self._index_rows:
            row.set_available(names)
        problem = self._problem()
        idle = self._nothing_to_do()
        self._create.set_sensitive(not problem and not idle)
        self._create.set_tooltip_text(
            problem
            or (
                "No changes yet — the form still describes the table as "
                "it is"
                if idle
                else "Show the plan for review, then run it"
            )
        )
        if problem:
            self._preview.set_text(f"-- {problem}")
        elif idle:
            self._preview.set_text("-- No changes.")
        else:
            self._preview.set_text(
                ";\n\n".join(self._statements()) + ";"
            )

    def _copy_sql(self) -> None:
        display = Gdk.Display.get_default()
        if display is not None:
            display.get_clipboard().set(self._preview.get_text())

    def _on_create_clicked(self, *_args) -> None:
        problem = self._problem()
        if problem:
            self._show_error(problem)
            return
        if self._nothing_to_do():
            self._show_error("No changes to apply")
            return
        statements = self.statements()
        count = len(statements)
        checks = (
            self._run_preflight
            if self.editing and preflight(
                self._current, self.model(), self._connector
            )
            else None
        )
        PlanPreviewDialog(
            statements,
            lambda: self._execute([s.sql for s in statements]),
            caption=ngettext(
                "Runs %d statement on “{name}”, in order.",
                "Runs %d statements on “{name}”, in order.",
                count,
            ).format(name=self.profile.name) % count,
            run_checks=checks,
        ).present(self)

    def _run_preflight(self, deliver: Callable[[list[str]], None]) -> None:
        """Ask the server the cheap questions a risky statement raises,
        while the dialog is open: how many rows are null under a new
        NOT NULL, how many groups are duplicated under a new UNIQUE.

        A count of zero is not reported — the interesting answer is the
        one that says the change will be refused, and by how much. A
        check that will not run (a permission, a view) is dropped
        rather than turned into an error: it was an offer, not a
        precondition.
        """
        checks = preflight(self._current, self.model(), self._connector)

        def work():
            connector = self._ensure(self.profile)
            lines = []
            for check in checks:
                try:
                    result = connector.execute(check.sql)
                    rows = getattr(result, "rows", None) or []
                    count = int(rows[0][0]) if rows and rows[0] else 0
                except Exception:
                    continue
                if count:
                    lines.append(f"{count} {check.label}.")
            return lines

        run_async(work, deliver, lambda exc: deliver([]))

    def _execute(self, statements: list[str]) -> None:
        table = self._table_name.get_text().strip()
        schema = self.schema()
        script = ";\n".join(statements) + ";"

        def work():
            connector = self._ensure(self.profile)
            for sql in statements:
                connector.execute(sql)
            return None

        def done(_result) -> None:
            if self.on_ran is not None:
                self.on_ran(script, True)
            if self.editing:
                # The form is now what the table is: re-read it, so the
                # next edit diffs against the applied state rather than
                # against the one it started from.
                self._current = self.model()
                self._refresh()
            self._on_created(table, schema)

        def failed(exc: Exception) -> None:
            self._show_error(str(exc))
            if self.on_ran is not None:
                self.on_ran(script, False)

        run_async(work, done, failed)
