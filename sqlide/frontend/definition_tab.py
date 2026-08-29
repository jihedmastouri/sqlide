"""Definition tabs: editable table/view DDL and function definitions.

DefinitionTab is opened from the sidebar's context menu (Table
Definition), and it is the escape hatch: the CREATE statement
(Connector.get_ddl, with SQL highlighting) in an editable buffer.
Editing it and pressing Save generates the rename-old / create-new /
copy-columns / drop-old rebuild sequence (views: DROP VIEW + the
edited CREATE); columns are matched by name between the old catalog
and the edited statement. That rebuild is a SQLite workaround: the
engines with a real ALTER TABLE (supports_table_rebuild = False) get
ADD/DROP COLUMN for the columns the edit added or removed instead.
Whatever the path, the SQL is shown in an UpdatePreviewDialog before
anything runs, and Refresh reloads the definition, discarding edits.

The column-grid mode this tab used to carry is gone (CORE-26): editing
a table column by column is what the table designer does, over a model
that knows the engine's types, its constraints and its indexes, and it
classifies what each change costs instead of leaving it to a caption.
Sidebar ▸ Edit Table opens it. What stays here is the one thing the
designer deliberately does not do — hand-writing the definition.

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
from sqlide.backend.db.base import Connector, ConnectorError
from sqlide.backend.sql_split import split_statements
from sqlide.backend.sql_format import format_sql, options_from_settings
from sqlide.backend.workspaces import TabState
from sqlide.frontend.data_grid import UpdatePreviewDialog
from sqlide.frontend.sql_editor import SqlEditor
from sqlide.frontend.util import describe, run_async
from sqlide.i18n import _

_REBUILD_CAPTION = (
    "Review carefully: a table rebuild carries only columns and the "
    "primary key — defaults, foreign keys and other constraints must "
    "be part of the CREATE statement to survive."
)

_ALTER_CAPTION = (
    "This engine edits tables in place, so only the columns you added "
    "or removed are applied. Changes to an existing column's type, "
    "nullability or position belong in the table designer "
    "(Edit Table…), which applies them as a diff."
)


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

        # Editable, highlighted CREATE statement (SqlEditor scrolls
        # itself).
        self._text = SqlEditor()
        self._text.set_vexpand(True)
        self._text.set_hexpand(True)

        bar = Gtk.Box(
            spacing=6,
            margin_top=6,
            margin_bottom=6,
            margin_start=6,
            margin_end=6,
        )
        save = Gtk.Button(label=_("Save"))
        save.add_css_class("suggested-action")
        save.set_tooltip_text(
            "Turn the edited definition into SQL and show it for review "
            "before running"
        )
        save.connect("clicked", self._on_save_clicked)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh.add_css_class("flat")
        describe(refresh, _("Reload the definition (discards edits)"))
        refresh.connect("clicked", lambda *_: self.reload())
        bar.append(Gtk.Box(hexpand=True))
        bar.append(save)
        bar.append(refresh)
        self.append(bar)
        self.append(self._text)

        self.reload()

    def tab_state(self) -> TabState:
        return TabState(
            kind="definition", connection=self.profile.name, table=self.table
        )

    def reload(self) -> None:
        def work():
            connector = self._ensure(self.profile)
            return connector.get_ddl(self.table)

        def done(ddl):
            # One definition of how our SQL looks (CORE-44): what the
            # server hands back is laid out the same way the editor's
            # Format lays a statement out. The formatted text is what
            # an edit is compared against, so re-saving an untouched
            # definition still counts as no change.
            self._original_ddl = format_sql(
                (ddl or "").strip(), options_from_settings()
            ).text.strip()
            self._text.set_text(
                self._original_ddl or f"-- No DDL available for {self.table}"
            )

        run_async(work, done, lambda exc: self._show_error(str(exc)))

    # Saving

    def _on_save_clicked(self, *_args) -> None:
        self._save_text()

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
                ], ""
            old_names = [c.name for c in connector.list_columns(self.table)]
            if not connector.supports_table_rebuild:
                return _alter_statements(connector, self.table, old_names, new_ddl)
            pairs = [
                (name, name)
                for name in _columns_from_ddl(new_ddl)
                if name in old_names
            ]
            return connector.wrap_rebuild(
                connector.rebuild_table_statements(self.table, new_ddl, pairs)
            ), _REBUILD_CAPTION

        def done(previewable) -> None:
            self._preview(*previewable)

        run_async(work, done, lambda exc: self._show_error(str(exc)))

    def _preview(self, statements: list[str], caption: str = "") -> None:
        statements = [s.rstrip().rstrip(";") + ";" for s in statements]
        dialog = UpdatePreviewDialog(
            statements,
            lambda: self._execute(statements),
            caption=caption or (
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
                    result = connector.execute(sql)
                    # Some checks report rather than raise (SQLite's
                    # foreign_key_check): silence from execute() is not
                    # the same as a clean rebuild.
                    problem = connector.rebuild_check_failure(sql, result)
                    if problem:
                        raise ConnectorError(problem)
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


def _alter_statements(
    connector: Connector, table: str, old_names: list[str], new_ddl: str
) -> tuple[list[str], str]:
    """The edited CREATE expressed as ALTERs, for the engines that edit
    tables in place instead of rebuilding them.

    Only the column *set* is diffed. A surviving column's definition
    cannot be compared honestly — the catalog spells types its own way
    (`varchar(40)` against Postgres' `character varying(40)`), so
    guessing at a difference would either miss real edits or invent
    ones. Those edits belong to the table designer, which reads the
    model it writes back and diffs it (CORE-26); the caption says so
    rather than leaving the user to notice.
    """
    entries = _column_defs_from_ddl(new_ddl)
    new_names = [name for name, _definition in entries]
    statements = [
        connector.add_column_sql(table, definition)
        for name, definition in entries
        if name not in old_names
    ]
    statements += [
        connector.drop_column_sql(table, name)
        for name in old_names
        if name not in new_names
    ]
    if not statements:
        raise ConnectorError(
            "No column was added or removed. This engine applies "
            "definition changes in place — change a column's type or "
            "nullability in the table designer (Edit Table…)."
        )
    return statements, _ALTER_CAPTION


def _columns_from_ddl(ddl: str) -> list[str]:
    """Column names declared in a CREATE TABLE statement."""
    return [name for name, _definition in _column_defs_from_ddl(ddl)]


def _column_defs_from_ddl(ddl: str) -> list[tuple[str, str]]:
    """Every column declared in a CREATE TABLE statement as (name, the
    entry as written): the first identifier of each top-level
    comma-separated entry of the parenthesized body, skipping the
    table-constraint entries.

    The entry is kept verbatim because ADD COLUMN wants the column
    spelled exactly as the user wrote it — type, DEFAULT, collation and
    all — and re-rendering it from the parts we recognize would drop
    whatever we don't.
    """
    start = ddl.find("(")
    end = ddl.rfind(")")
    if start == -1 or end <= start:
        return []
    entries = _split_top_level(ddl[start + 1 : end])
    constraint_starters = {
        "PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT",
    }
    columns = []
    for entry in entries:
        entry = entry.strip()
        name = _first_identifier(entry)
        if name and name.upper() not in constraint_starters:
            columns.append((name, entry))
    return columns


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
        save = Gtk.Button(label=_("Save"))
        save.add_css_class("suggested-action")
        save.set_tooltip_text(
            "Replace the stored definition with the editor's text "
            "(shows the statements first)"
        )
        save.connect("clicked", self._on_save_clicked)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh.add_css_class("flat")
        describe(refresh, _("Reload the stored definition"))
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
            self._status.set_text(_("Saved"))
            self.reload()

        def failed(exc: Exception) -> None:
            self._show_error(str(exc))

        run_async(work, done, failed)
