"""Install, update and drop an extension — review, then run.

The same shape as the drop dialog next door: the statement is built by
the metadata provider on a worker thread, shown verbatim on the
destructive-action ladder (frontend/confirm.py), and executed only once
the user has cleared it. Nothing runs before the SQL has been read.

Two gates, both asked before anything is shown:

* the engine must have the `extensions` capability at all, and
* the account must be allowed to manage them
  (`MetadataProvider.can_manage_extensions`) — a role that would be
  refused by the server is told so here instead of being handed a
  dialog that can only fail.
"""

from __future__ import annotations

from typing import Callable

from gi.repository import Gtk

from sqlide.backend import sql_risk
from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.db import registry
from sqlide.backend.db.base import Connector
from sqlide.frontend import confirm
from sqlide.frontend.util import run_async
from sqlide.i18n import _

#: Heading and button per action.
_ACTIONS = {
    "install": ("Install", "Install"),
    "update": ("Update", "Update"),
    "drop": ("Drop", "Drop"),
}


def present_extension_dialog(
    parent: Gtk.Widget,
    profile: ConnectionProfile,
    action: str,
    name: str,
    ensure_connector: Callable[[ConnectionProfile], Connector],
    show_error: Callable[[str], None],
    on_executed: Callable[[str, bool], None],
) -> None:
    """Confirm and run one extension action. `on_executed(sql, ok)`
    fires after an execution attempt, never on cancel."""
    if action not in _ACTIONS:
        return

    def work() -> tuple[list[str], list[str], bool]:
        connector = ensure_connector(profile)
        provider = registry.create_provider(profile.kind, connector)
        allowed = provider.can_manage_extensions()
        statements = provider.extension_statements(action, name)
        cascade = (
            provider.extension_statements(action, name, cascade=True)
            if action == "drop"
            else []
        )
        return statements, cascade, allowed

    def ready(built: tuple[list[str], list[str], bool]) -> None:
        statements, cascade, allowed = built
        if not statements:
            show_error(
                f"{profile.kind} connections cannot manage extensions."
            )
            return
        if not allowed:
            show_error(
                f"“{profile.name}” is connected as an account that may not "
                f"{action} extensions. Ask a superuser to run:\n"
                + statements[0] + ";"
            )
            return
        _present(
            parent, profile, action, name, statements[0],
            cascade[0] if cascade else "",
            ensure_connector, show_error, on_executed,
        )

    run_async(work, ready, lambda exc: show_error(str(exc)))


def _present(
    parent: Gtk.Widget,
    profile: ConnectionProfile,
    action: str,
    name: str,
    sql: str,
    cascade_sql: str,
    ensure_connector: Callable[[ConnectionProfile], Connector],
    show_error: Callable[[str], None],
    on_executed: Callable[[str, bool], None],
) -> None:
    verb, button = _ACTIONS[action]
    statement = Gtk.Label(
        label=sql + ";", xalign=0, wrap=True, selectable=True
    )
    statement.add_css_class("monospace")
    cascade = None
    if cascade_sql:
        cascade = Gtk.CheckButton(
            label=_("Also drop the objects the extension owns (CASCADE)")
        )
        cascade.connect(
            "toggled",
            lambda toggle: statement.set_label(
                (cascade_sql if toggle.get_active() else sql) + ";"
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

    body = {
        "install": "This adds the extension and every object it ships "
                   "to {where}. The statement below runs as shown.",
        "update": "This upgrades the installed extension on {where}. "
                  "The statement below runs as shown.",
        "drop": "This removes the extension from {where}, and with "
                "CASCADE everything it owns. The statement below runs "
                "as shown.",
    }[action].format(where=confirm.describe_connection(profile))

    dialog = confirm.present(
        parent,
        heading=f"{verb} extension “{name}”?",
        body=body,
        confirm_label=button,
        level=confirm.level_for(sql_risk.classify(sql), profile),
        type_target=name,
        on_confirm=run,
    )
    extra = dialog.get_extra_child()
    extra.prepend(statement)
    if cascade is not None:
        extra.append(cascade)
