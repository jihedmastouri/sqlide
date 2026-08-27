"""Confirmation dialog for dropping a database object.

One uniform, form-free flow for every kind (table, view, index,
trigger, function, procedure, event): the exact DROP statement is
built by the adapter on a worker thread (Postgres resolves function
signatures from the catalog), shown on the destructive-action ladder
(frontend/confirm.py — a production connection asks for the object's
name) with a CASCADE checkbox where the dialect supports it, and
executed on a worker thread when confirmed. The caller records the run
in history and reloads the sidebar via `on_executed`.
"""

from __future__ import annotations

from typing import Callable

from gi.repository import Gtk

from sqlide.backend import sql_risk
from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.db.base import Connector
from sqlide.frontend import confirm
from sqlide.frontend.util import run_async
from sqlide.i18n import _


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
    statement = Gtk.Label(
        label=sql + ";", xalign=0, wrap=True, selectable=True
    )
    statement.add_css_class("monospace")
    cascade = None
    if cascade_sql:
        cascade = Gtk.CheckButton(
            label=_("Also drop dependent objects (CASCADE)")
        )
        cascade.connect(
            "toggled",
            lambda button: statement.set_label(
                (cascade_sql if button.get_active() else sql) + ";"
            ),
        )

    def run(*_args) -> None:
        chosen = (
            cascade_sql if cascade is not None and cascade.get_active()
            else sql
        )
        run_async(
            lambda: ensure_connector(profile).execute(chosen),
            lambda _result: on_executed(chosen, True),
            lambda exc: (show_error(str(exc)), on_executed(chosen, False)),
        )

    # A drop is always confirmed; on a production connection it also
    # asks for the object's name (frontend/confirm.py's top rung).
    level = confirm.level_for(sql_risk.classify(sql), profile)
    dialog = confirm.present(
        parent,
        heading=f"Drop {kind} “{name}”?",
        body=f"This permanently removes the {kind} from "
        f"{confirm.describe_connection(profile)}. The statement below "
        "runs as shown.",
        confirm_label="Drop",
        level=level,
        type_target=name,
        on_confirm=run,
    )
    extra = dialog.get_extra_child()
    extra.prepend(statement)
    if cascade is not None:
        extra.append(cascade)
