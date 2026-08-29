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
statement in an UpdatePreviewDialog before anything runs; on success
the window reloads the sidebar and opens the new table's data tab.

Session-only: tab_state() returns None, the tab is not restored.
"""

from __future__ import annotations

from typing import Callable

from gi.repository import Gdk, Gtk

from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.db import registry
from sqlide.backend.db.base import Connector, TypeSpec
from sqlide.backend.db.metadata import Capabilities, NodeRef
from sqlide.backend.db.table_model import (
    CASCADE_ACTIONS,
    ColumnDefault,
    ColumnModel,
    ConstraintModel,
    GENERIC,
    IndexModel,
    TableModel,
    dialect_for,
    plan,
    render_create,
)
from sqlide.frontend.data_grid import UpdatePreviewDialog
from sqlide.frontend.sql_editor import SqlEditor
from sqlide.frontend.util import describe, run_async
from sqlide.i18n import _

# Last entry of every type dropdown: free text, for the types no list
# can enumerate (domains, extensions, arrays).
_CUSTOM = "Custom…"


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

    def set_columns(self, names: list[str]) -> None:
        """Choose exactly `names` (used to mirror the per-column
        primary-key checkboxes into the constraints view)."""
        self._chosen = [n for n in names]
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
        self._wanted_schema = (
            (ref.name if ref.kind == "schema" else ref.schema) if ref else ""
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
        self._create = Gtk.Button(label=_("Create"))
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
            )

        def ready(loaded) -> None:
            (
                self._connector,
                self._specs,
                self._caps,
                self._schemas,
                self._sources,
            ) = loaded
            self._dialect = dialect_for(self._connector)
            for row in self._rows:
                row.set_specs(self._specs)
            self._sync_dialect()
            self._sync_schema_chooser()
            self._refresh()

        run_async(work, ready, lambda exc: self._show_error(str(exc)))
        self._refresh()

    def tab_state(self) -> None:
        return None  # session-only

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

    def _add_row(self, focus: bool = False) -> None:
        row = _ColumnRow(
            self._specs,
            self._refresh,
            self._remove_row,
            self._move_row,
            self._pk_toggled,
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

    def _statements(self) -> list[str]:
        """Everything the designer will run, in order: the CREATE, then
        the CREATE INDEX for each index and whatever else the engine
        needs beside it. `plan()` decides — indexes are objects of
        their own on every engine we speak to, so the preview is a
        script, not one statement."""
        if self._problem():
            return []
        return [
            statement.sql
            for statement in plan(None, self.model(), self._connector)
        ]

    def _refresh(self) -> None:
        # Constraints and indexes cover columns, so every view follows
        # the column names as they are typed.
        names = self._column_names()
        for row in self._constraint_rows:
            row.set_available(names)
        for row in self._index_rows:
            row.set_available(names)
        problem = self._problem()
        self._create.set_sensitive(not problem)
        self._create.set_tooltip_text(
            problem
            or "Show the generated statements for review, then run them"
        )
        if problem:
            self._preview.set_text(f"-- {problem}")
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
        statements = self._statements()
        count = len(statements)
        UpdatePreviewDialog(
            [sql + ";" for sql in statements],
            lambda: self._execute(statements),
            caption=(
                "Runs one CREATE TABLE statement on "
                f"“{self.profile.name}”."
                if count == 1
                else f"Runs {count} statements on “{self.profile.name}” — "
                "the table and its indexes."
            ),
            width=720,
            height=520,
        ).present(self)

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
            self._on_created(table, schema)

        def failed(exc: Exception) -> None:
            self._show_error(str(exc))
            if self.on_ran is not None:
                self.on_ran(script, False)

        run_async(work, done, failed)
