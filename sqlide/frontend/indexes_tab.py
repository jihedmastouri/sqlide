"""All-indexes tab: every index on a connection, table and CREATE INDEX
text side by side, one query instead of hovering rows one at a time.

Read-only — an index isn't really "edited in place" the way a table or
view's CREATE statement is (renaming a column means dropping and
recreating it), so this is a browsing surface, not DefinitionTab's
editable one. Opened from the sidebar's Indexes category ("View All…")
and deduplicated per connection like the relation graph.

The DDL a dialect can offer for an index varies: SQLite and Postgres
return the database's own CREATE INDEX text (sqlite_master.sql /
pg_get_indexdef); MySQL has no such statement, so its adapter
synthesizes one from information_schema.statistics. Connectors that
answer list_indexes() with an empty list (JDBC, unimplemented stubs)
just render an empty grid.
"""

from __future__ import annotations

from typing import Callable

from gi.repository import Gtk

from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.db.base import Connector
from sqlide.backend.workspaces import TabState
from sqlide.frontend.data_grid import ResultGrid
from sqlide.frontend.util import describe, run_async
from sqlide.i18n import _


class IndexesTab(Gtk.Box):
    def __init__(
        self,
        profile: ConnectionProfile,
        ensure_connector: Callable[[ConnectionProfile], Connector],
        show_error: Callable[[str], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.profile = profile
        self._ensure = ensure_connector
        self._show_error = show_error

        bar = Gtk.Box(
            spacing=6,
            margin_top=6,
            margin_bottom=6,
            margin_start=6,
            margin_end=6,
        )
        title = Gtk.Label(
            label=f"Indexes on {profile.name}", xalign=0, hexpand=True
        )
        title.add_css_class("heading")
        refresh = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh.add_css_class("flat")
        describe(refresh, _("Reload the index list"))
        refresh.connect("clicked", lambda *_: self.reload())
        bar.append(title)
        bar.append(refresh)
        self.append(bar)

        self._grid = ResultGrid()
        self._grid.set_vexpand(True)
        self.append(self._grid)

        self.reload()

    def tab_state(self) -> TabState:
        return TabState(kind="indexes", connection=self.profile.name)

    def reload(self) -> None:
        def work():
            return self._ensure(self.profile).list_indexes()

        def done(indexes) -> None:
            self._grid.set_result(
                ["Index", "Table", "Definition"],
                [
                    (i.name, i.table, i.ddl or "(no definition available)")
                    for i in indexes
                ],
            )

        run_async(work, done, lambda exc: self._show_error(str(exc)))
