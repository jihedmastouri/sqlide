"""Confirmation dialog for dropping a database object.

One uniform, form-free flow for every kind (table, view, index,
trigger, function, procedure, event): the exact DROP statement is
built by the adapter on a worker thread (Postgres resolves function
signatures from the catalog), shown in a destructive-styled
AlertDialog — with a CASCADE checkbox where the dialect supports it —
and executed on a worker thread when confirmed. The caller records
the run in history and reloads the sidebar via `on_executed`.
"""

from __future__ import annotations

from typing import Callable

from gi.repository import Adw, Gtk

from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.db.base import Connector
from sqlide.frontend.util import run_async


def present_drop_dialog(
    parent: Gtk.Widget,
    profile: ConnectionProfile,
    kind: str,
    name: str,
    table: str,
    ensure_connector: Callable[[ConnectionProfile], Connector],
    show_error: Callable[[str], None],
    on_executed: Callable[[str, bool], None],
) -> None:
    """Build the DROP statement(s) off the main thread, then show the
    confirmation. `on_executed(sql, ok)` fires after an execution
    attempt (not on cancel)."""

    def work():
        connector = ensure_connector(profile)
        sql = connector.drop_sql(kind, name, table=table)
        cascade_sql = (
            connector.drop_sql(kind, name, table=table, cascade=True)
            if connector.supports_drop_cascade
            else ""
        )
        return sql, cascade_sql

    def ready(built: tuple[str, str]) -> None:
        _present(
            parent, profile, kind, name, built[0], built[1],
            ensure_connector, show_error, on_executed,
        )

    run_async(work, ready, lambda exc: show_error(str(exc)))


def _present(
    parent: Gtk.Widget,
    profile: ConnectionProfile,
    kind: str,
    name: str,
    sql: str,
    cascade_sql: str,
    ensure_connector: Callable[[ConnectionProfile], Connector],
    show_error: Callable[[str], None],
    on_executed: Callable[[str, bool], None],
) -> None:
    dialog = Adw.AlertDialog(
        heading=f"Drop {kind} “{name}”?",
        body=f"This permanently removes the {kind} from "
        f"“{profile.name}”. The statement below runs as shown.",
    )
    statement = Gtk.Label(
        label=sql + ";", xalign=0, wrap=True, selectable=True
    )
    statement.add_css_class("monospace")

    extra = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    extra.append(statement)
    cascade = None
    if cascade_sql:
        cascade = Gtk.CheckButton(label="Also drop dependent objects (CASCADE)")
        cascade.connect(
            "toggled",
            lambda button: statement.set_label(
                (cascade_sql if button.get_active() else sql) + ";"
            ),
        )
        extra.append(cascade)
    dialog.set_extra_child(extra)

    dialog.add_response("cancel", "Cancel")
    dialog.add_response("drop", "Drop")
    dialog.set_response_appearance("drop", Adw.ResponseAppearance.DESTRUCTIVE)
    dialog.set_default_response("cancel")
    dialog.set_close_response("cancel")

    def respond(_dialog, response: str) -> None:
        if response != "drop":
            return
        chosen = (
            cascade_sql if cascade is not None and cascade.get_active()
            else sql
        )
        run_async(
            lambda: ensure_connector(profile).execute(chosen),
            lambda _result: on_executed(chosen, True),
            lambda exc: (show_error(str(exc)), on_executed(chosen, False)),
        )

    dialog.connect("response", respond)
    dialog.present(parent)
