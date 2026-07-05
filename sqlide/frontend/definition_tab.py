"""Definition tabs: editable table/view DDL and function definitions.

DefinitionTab is opened from the sidebar's context menu (Table
Definition). A switcher in the top bar flips between two modes, and
both are editable — every save path only ever produces SQL that is
shown to the user in an UpdatePreviewDialog before anything runs:

- Text shows the CREATE statement (Connector.get_ddl, with SQL
  highlighting). Editing it and pressing Save generates the
  rename-old / create-new / copy-columns / drop-old rebuild sequence
  (views: DROP VIEW + the edited CREATE). Columns are matched by name
  between the old catalog and the edited statement.
- Table shows the column catalog (name, type, nullable, primary key)
  in an editable ResultGrid. Edited names become RENAME COLUMN
  statements; type/nullability edits become the dialect's in-place
  ALTER when it has one (MySQL) and fall back to a table rebuild when
  it doesn't (SQLite). Primary-key changes must go through the text.

Both modes are loaded together on open and by Refresh, which also
discards unsaved edits.

FunctionTab is opened by activating a function in the sidebar: the
object's DDL in an editable, highlighted SQL editor. Save replaces
the object — the adapter's drop_function_sql() statement first (when
it has one), then every statement in the buffer — after showing them
for review, then reloads the stored definition.
"""

from __future__ import annotations

from typing import Callable

from gi.repository import Gtk

from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.db.base import ColumnInfo, Connector
from sqlide.backend.sql_split import split_statements
from sqlide.backend.workspaces import TabState
from sqlide.frontend.data_grid import ResultGrid, RowItem, UpdatePreviewDialog
from sqlide.frontend.sql_editor import SqlEditor
from sqlide.frontend.util import run_async

_REBUILD_CAPTION = (
    "Review carefully: a table rebuild carries only columns and the "
    "primary key — defaults, foreign keys and other constraints must "
    "be part of the CREATE statement to survive."
)

# Grid column positions in the Table mode.
_COL_NAME, _COL_TYPE, _COL_NULLABLE, _COL_PK = range(4)


class DefinitionTab(Gtk.Box):
    def __init__(
        self,
        profile: ConnectionProfile,
        table: str,
        ensure_connector: Callable[[ConnectionProfile], Connector],
        show_error: Callable[[str], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.profile = profile
        self.table = table
        self._ensure = ensure_connector
        self._show_error = show_error
        self._original_ddl = ""
        # Grid rows with unsaved edits: row -> the column as loaded.
        self._pending: dict[RowItem, ColumnInfo] = {}

        # Editable, highlighted CREATE statement (SqlEditor scrolls
        # itself).
        self._text = SqlEditor()
        text_page = self._text
        text_page.set_vexpand(True)
        text_page.set_hexpand(True)

        self._grid = ResultGrid(on_edit=self._on_grid_edit)

        self._stack = Gtk.Stack(vexpand=True)
        self._stack.add_titled(text_page, "text", "Text")
        self._stack.add_titled(self._grid, "table", "Table")

        bar = Gtk.Box(
            spacing=6,
            margin_top=6,
            margin_bottom=6,
            margin_start=6,
            margin_end=6,
        )
        switcher = Gtk.StackSwitcher(stack=self._stack)
        save = Gtk.Button(label="Save")
        save.add_css_class("suggested-action")
        save.set_tooltip_text(
            "Turn the edits of the visible mode into SQL and show it "
            "for review before running"
        )
        save.connect("clicked", self._on_save_clicked)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh.add_css_class("flat")
        refresh.set_tooltip_text("Reload the definition (discards edits)")
        refresh.connect("clicked", lambda *_: self.reload())
        bar.append(switcher)
        bar.append(Gtk.Box(hexpand=True))
        bar.append(save)
        bar.append(refresh)
        self.append(bar)
        self.append(self._stack)

        self.reload()

    def tab_state(self) -> TabState:
        return TabState(
            kind="definition", connection=self.profile.name, table=self.table
        )

    def reload(self) -> None:
        def work():
            connector = self._ensure(self.profile)
            return connector.get_ddl(self.table), connector.list_columns(
                self.table
            )

        def done(loaded):
            ddl, columns = loaded
            self._original_ddl = (ddl or "").strip()
            self._text.set_text(
                self._original_ddl or f"-- No DDL available for {self.table}"
            )
            self._pending.clear()
            self._grid.set_result(
                ["Column", "Type", "Nullable", "Primary Key"],
                [
                    (
                        c.name,
                        c.type,
                        "yes" if c.nullable else "no",
                        "PK" if c.is_pk else "",
                    )
                    for c in columns
                ],
                editable=True,
            )
            self._grid.set_unlocked(True)

        run_async(work, done, lambda exc: self._show_error(str(exc)))

    # Table-mode editing

    def _on_grid_edit(self, row: RowItem, index: int, new_text: str) -> None:
        if index == _COL_PK:
            self._show_error(
                "Change the primary key by editing the DDL text"
            )
            return
        if index == _COL_NULLABLE:
            new_text = new_text.strip().lower()
            if new_text not in ("yes", "no"):
                self._show_error("Nullable must be “yes” or “no”")
                return
        if row not in self._pending:
            # Snapshot the loaded state before the first edit applies.
            self._pending[row] = ColumnInfo(
                name=str(row.values[_COL_NAME]),
                type=str(row.values[_COL_TYPE]),
                is_pk=row.values[_COL_PK] == "PK",
                nullable=row.values[_COL_NULLABLE] == "yes",
            )
        row.values[index] = new_text
        self._grid.mark_modified(row, index)

    def _edited_columns(self) -> list[tuple[ColumnInfo, ColumnInfo]]:
        """(as loaded, as edited) for every actually-changed grid row."""
        edits = []
        for row, original in self._pending.items():
            edited = ColumnInfo(
                name=str(row.values[_COL_NAME]).strip(),
                type=str(row.values[_COL_TYPE]).strip(),
                is_pk=original.is_pk,
                nullable=row.values[_COL_NULLABLE] == "yes",
            )
            if edited != original and edited.name:
                edits.append((original, edited))
        return edits

    # Saving

    def _on_save_clicked(self, *_args) -> None:
        if self._stack.get_visible_child_name() == "text":
            self._save_text()
        else:
            self._save_table()

    def _save_text(self) -> None:
        new_ddl = self._text.get_text().strip().rstrip(";")
        if not new_ddl or new_ddl.startswith("--"):
            self._show_error("Nothing to save — the editor is empty")
            return
        if new_ddl == self._original_ddl.rstrip(";"):
            self._show_error("No changes to save")
            return

        def work():
            connector = self._ensure(self.profile)
            kind = next(
                (
                    t.kind
                    for t in connector.list_tables()
                    if t.name == self.table
                ),
                "table",
            )
            if kind == "view":
                return [
                    f"DROP VIEW {connector.quote_ident(self.table)}",
                    new_ddl,
                ]
            old_names = {c.name for c in connector.list_columns(self.table)}
            pairs = [
                (name, name)
                for name in _columns_from_ddl(new_ddl)
                if name in old_names
            ]
            return [
                "BEGIN",
                *connector.rebuild_table_statements(
                    self.table, new_ddl, pairs
                ),
                "COMMIT",
            ]

        def done(statements: list[str]) -> None:
            self._preview(statements)

        run_async(work, done, lambda exc: self._show_error(str(exc)))

    def _save_table(self) -> None:
        edits = self._edited_columns()
        if not edits:
            self._show_error("No changes to save")
            return

        def work():
            connector = self._ensure(self.profile)
            statements = []
            rebuild = False
            for original, edited in edits:
                if edited.name != original.name:
                    statements.append(connector.rename_column_sql(
                        self.table, original.name, edited.name
                    ))
                if (
                    edited.type != original.type
                    or edited.nullable != original.nullable
                ):
                    sql = connector.modify_column_sql(self.table, edited)
                    if sql:
                        statements.append(sql)
                    else:
                        rebuild = True
            if not rebuild:
                return statements
            # No in-place column change in this dialect (SQLite): one
            # rebuild covers every edit, renames included.
            changed = {original.name: edited for original, edited in edits}
            current = connector.list_columns(self.table)
            target = [changed.get(c.name, c) for c in current]
            pairs = [
                (changed[c.name].name if c.name in changed else c.name, c.name)
                for c in current
            ]
            new_ddl = _synthesize_ddl(connector.quote_ident, self.table, target)
            return [
                "BEGIN",
                *connector.rebuild_table_statements(
                    self.table, new_ddl, pairs
                ),
                "COMMIT",
            ]

        def done(statements: list[str]) -> None:
            self._preview(statements)

        run_async(work, done, lambda exc: self._show_error(str(exc)))

    def _preview(self, statements: list[str]) -> None:
        statements = [s.rstrip().rstrip(";") + ";" for s in statements]
        rebuild = any(s.startswith("BEGIN") for s in statements)
        dialog = UpdatePreviewDialog(
            statements,
            lambda: self._execute(statements),
            caption=_REBUILD_CAPTION if rebuild else (
                "Statements run in order; the definition is reloaded "
                "afterwards."
            ),
        )
        dialog.present(self)

    def _execute(self, statements: list[str]) -> None:
        def work():
            connector = self._ensure(self.profile)
            try:
                for sql in statements:
                    connector.execute(sql)
            except Exception:
                # A failed rebuild must not leave the table renamed.
                try:
                    if connector.in_transaction():
                        connector.rollback()
                except Exception:
                    pass
                raise

        def done(_result) -> None:
            self.reload()

        def failed(exc: Exception) -> None:
            self._show_error(str(exc))
            self.reload()

        run_async(work, done, failed)


def _synthesize_ddl(
    quote: Callable[[str], str], table: str, columns: list[ColumnInfo]
) -> str:
    """A CREATE TABLE carrying exactly what the catalog grid shows:
    column names, types, NOT NULL and the primary key."""
    defs = []
    for column in columns:
        line = f"  {quote(column.name)} {column.type}".rstrip()
        if not column.nullable:
            line += " NOT NULL"
        defs.append(line)
    pks = [c.name for c in columns if c.is_pk]
    if pks:
        defs.append(
            "  PRIMARY KEY (" + ", ".join(quote(p) for p in pks) + ")"
        )
    return f"CREATE TABLE {quote(table)} (\n" + ",\n".join(defs) + "\n)"


def _columns_from_ddl(ddl: str) -> list[str]:
    """Column names declared in a CREATE TABLE statement: the first
    identifier of every top-level comma-separated entry in the
    parenthesized body, skipping table-constraint entries."""
    start = ddl.find("(")
    end = ddl.rfind(")")
    if start == -1 or end <= start:
        return []
    entries = _split_top_level(ddl[start + 1 : end])
    constraint_starters = {
        "PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT",
    }
    names = []
    for entry in entries:
        name = _first_identifier(entry.strip())
        if name and name.upper() not in constraint_starters:
            names.append(name)
    return names


def _split_top_level(body: str) -> list[str]:
    """Split on commas outside parentheses, quotes and brackets."""
    parts, current, depth, i = [], [], 0, 0
    closers = {'"': '"', "'": "'", "`": "`", "[": "]"}
    while i < len(body):
        char = body[i]
        if char in closers:
            closer = closers[char]
            j = body.find(closer, i + 1)
            j = j if j != -1 else len(body) - 1
            current.append(body[i : j + 1])
            i = j + 1
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append("".join(current))
            current = []
            i += 1
            continue
        current.append(char)
        i += 1
    if current:
        parts.append("".join(current))
    return [p for p in (part.strip() for part in parts) if p]


def _first_identifier(entry: str) -> str:
    """The leading (possibly quoted) identifier of a column entry."""
    if not entry:
        return ""
    char = entry[0]
    closers = {'"': '"', "'": "'", "`": "`", "[": "]"}
    if char in closers:
        end = entry.find(closers[char], 1)
        if end != -1:
            return entry[1:end].replace(closers[char] * 2, closers[char])
    return entry.split()[0] if entry.split() else ""


class FunctionTab(Gtk.Box):
    """Editable definition of a stored function/trigger/procedure."""

    def __init__(
        self,
        profile: ConnectionProfile,
        name: str,
        ensure_connector: Callable[[ConnectionProfile], Connector],
        show_error: Callable[[str], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.profile = profile
        self.name = name
        self._ensure = ensure_connector
        self._show_error = show_error

        bar = Gtk.Box(
            spacing=6,
            margin_top=6,
            margin_bottom=6,
            margin_start=6,
            margin_end=6,
        )
        save = Gtk.Button(label="Save")
        save.add_css_class("suggested-action")
        save.set_tooltip_text(
            "Replace the stored definition with the editor's text "
            "(shows the statements first)"
        )
        save.connect("clicked", self._on_save_clicked)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh.add_css_class("flat")
        refresh.set_tooltip_text("Reload the stored definition")
        refresh.connect("clicked", lambda *_: self.reload())
        self._status = Gtk.Label(xalign=1, hexpand=True)
        self._status.add_css_class("dim-label")
        bar.append(save)
        bar.append(self._status)
        bar.append(refresh)
        self.append(bar)

        self._editor = SqlEditor()
        self._editor.set_vexpand(True)
        self.append(self._editor)

        self.reload()

    def tab_state(self) -> TabState:
        return TabState(
            kind="function", connection=self.profile.name, table=self.name
        )

    def reload(self) -> None:
        def work():
            return self._ensure(self.profile).get_ddl(self.name)

        def done(ddl: str) -> None:
            self._editor.set_text(
                ddl or f"-- No definition available for {self.name}"
            )
            self._status.set_text("")

        run_async(work, done, lambda exc: self._show_error(str(exc)))

    def _on_save_clicked(self, *_args) -> None:
        statements = [
            s.text.rstrip().rstrip(";") + ";"
            for s in split_statements(self._editor.get_text())
        ]
        if not statements:
            self._show_error("Nothing to save — the editor is empty")
            return

        def work():
            connector = self._ensure(self.profile)
            drop = connector.drop_function_sql(self.name)
            return ([drop + ";"] if drop else []) + statements

        def done(planned: list[str]) -> None:
            dialog = UpdatePreviewDialog(
                planned,
                lambda: self._execute(planned),
                caption="Statements run in order; the definition is "
                "reloaded afterwards.",
            )
            dialog.present(self)

        run_async(work, done, lambda exc: self._show_error(str(exc)))

    def _execute(self, statements: list[str]) -> None:
        def work():
            connector = self._ensure(self.profile)
            for sql in statements:
                connector.execute(sql)

        def done(_result) -> None:
            self._status.set_text("Saved")
            self.reload()

        def failed(exc: Exception) -> None:
            self._show_error(str(exc))

        run_async(work, done, failed)
