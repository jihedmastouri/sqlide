"""The destructive-action ladder.

Three rungs, chosen by what the statement does and how much a mistake
on that connection costs (backend/sql_risk.py decides which):

1. **inform** — it just runs, and the app reports afterwards.
2. **confirm** — an Adw.AlertDialog naming the connection, the database
   and the environment, with the exact statement shown.
3. **type to confirm** — the same, plus the object's name typed out.
   Reserved for production DROP/TRUNCATE and unfiltered writes. It asks
   for the object's name rather than the word "yes" so the user has to
   look at what they are destroying.

Two rules hold everywhere and are the reason this lives in one place:
the exact statement is always shown before it runs, and the default
response is Cancel — never the destructive one.
"""

from __future__ import annotations

from typing import Callable

from gi.repository import Adw, Gtk

from sqlide.backend import identity, settings as app_settings, sql_risk
from sqlide.backend.connections import ConnectionProfile


def describe_connection(profile: ConnectionProfile | None) -> str:
    """"production (analytics-primary / app_prod)" — the phrase every
    confirmation ends with, so the user reads where this is going."""
    if profile is None:
        return "an unknown connection"
    where = profile.name
    if profile.database:
        where += f" / {profile.database}"
    elif profile.file_path:
        where += f" / {profile.file_path}"
    environment = identity.normalize_environment(profile.environment)
    if environment == identity.UNSET:
        return where
    return f"{identity.ENVIRONMENT_LABELS[environment].lower()} ({where})"


def level_for(
    risk: sql_risk.Risk, profile: ConnectionProfile | None
) -> str:
    """The rung this statement has to climb on this connection."""
    return sql_risk.confirmation_level(
        risk,
        identity.normalize_environment(
            profile.environment if profile is not None else identity.UNSET
        ),
        app_settings.store.settings.confirm_destructive,
    )


def confirm_statements(
    parent: Gtk.Widget,
    statements: list[str],
    profile: ConnectionProfile | None,
    on_confirm: Callable[[], None],
) -> None:
    """Run `on_confirm` once the user has cleared whatever rung these
    statements need. Nothing to clear: it runs immediately, in this
    same turn, so the caller can rely on the order."""
    risk = sql_risk.worst(statements)
    level = level_for(risk, profile)
    if level == "none":
        on_confirm()
        return
    present(
        parent,
        heading=f"Run {risk.describe()}?",
        body=(
            f"This runs on {describe_connection(profile)} and cannot be "
            "undone."
        ),
        statement=";\n".join(s.strip() for s in statements) + ";",
        confirm_label="Run",
        level=level,
        type_target=risk.target,
        on_confirm=on_confirm,
    )


def present(
    parent: Gtk.Widget,
    *,
    heading: str,
    body: str,
    statement: str = "",
    confirm_label: str = "Continue",
    level: str = "confirm",
    type_target: str = "",
    on_confirm: Callable[[], None],
) -> Adw.AlertDialog:
    """One confirmation, at the given rung. Returns the dialog so
    callers can add to it before it is answered."""
    dialog = Adw.AlertDialog(heading=heading, body=body)
    extra = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    if statement:
        extra.append(_statement_view(statement))

    entry: Gtk.Entry | None = None
    if level == "type" and type_target:
        prompt = Gtk.Label(
            label=f"Type “{type_target}” to confirm.", xalign=0, wrap=True
        )
        entry = Gtk.Entry(placeholder_text=type_target)
        extra.append(prompt)
        extra.append(entry)
    dialog.set_extra_child(extra)

    dialog.add_response("cancel", "Cancel")
    dialog.add_response("confirm", confirm_label)
    dialog.set_response_appearance(
        "confirm", Adw.ResponseAppearance.DESTRUCTIVE
    )
    # Cancel, always: a destructive action must never be one Enter away.
    dialog.set_default_response("cancel")
    dialog.set_close_response("cancel")

    if entry is not None:
        dialog.set_response_enabled("confirm", False)
        entry.connect(
            "changed",
            lambda e: dialog.set_response_enabled(
                "confirm", e.get_text().strip() == type_target
            ),
        )

    def respond(_dialog, response: str) -> None:
        if response == "confirm":
            on_confirm()

    dialog.connect("response", respond)
    dialog.present(parent)
    return dialog


def _statement_view(statement: str) -> Gtk.Widget:
    """The exact statement, verbatim, selectable and scrollable — the
    user must be able to read and copy what is about to run."""
    label = Gtk.Label(
        label=statement, xalign=0, wrap=True, selectable=True
    )
    label.add_css_class("monospace")
    scroller = Gtk.ScrolledWindow(child=label, max_content_height=180)
    scroller.set_propagate_natural_height(True)
    scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    return scroller
