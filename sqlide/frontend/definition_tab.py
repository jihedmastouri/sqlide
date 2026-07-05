"""Definition tabs: table/view DDL and editable function definitions.

DefinitionTab is opened from the sidebar's context menu (Table
Definition). A switcher in the top bar flips between two read-only
modes: Text shows the CREATE statement (Connector.get_ddl, with SQL
highlighting), Table shows the column catalog (name, type, nullable,
primary key) in a ResultGrid. No editing in either mode. Both are
loaded together on open and by Refresh.

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
from sqlide.backend.db.base import Connector
from sqlide.backend.sql_split import split_statements
from sqlide.backend.workspaces import TabState
from sqlide.frontend.data_grid import ResultGrid, UpdatePreviewDialog
from sqlide.frontend.sql_editor import SqlEditor
from sqlide.frontend.util import run_async


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

        # Highlighted read-only SQL view (SqlEditor scrolls itself).
        self._text = SqlEditor(editable=False)
        text_page = self._text
        text_page.set_vexpand(True)
        text_page.set_hexpand(True)

        self._grid = ResultGrid()

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
        refresh = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh.add_css_class("flat")
        refresh.set_tooltip_text("Reload the definition")
        refresh.connect("clicked", lambda *_: self.reload())
        bar.append(switcher)
        bar.append(Gtk.Box(hexpand=True))
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
            self._text.set_text(
                ddl or f"-- No DDL available for {self.table}"
            )
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
            )

        run_async(work, done, lambda exc: self._show_error(str(exc)))


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
