"""The window's persistent status bar.

Four zones, left to right, always visible:

| Identity | connection colour swatch, connection and database names,
|          | environment badge, read-only badge, transaction state
| Context  | whatever the active tab is doing: a table tab's row range
|          | and filter/sort state, a console's last run
| Jobs     | the long-running operation in flight, with a spinner
| Status   | transient messages, cleared on a timeout

Two rules it exists to keep: the identity zone is never empty while a
tab is open and never stale — a connection that is not open says so
and offers to connect — and the status zone is not a log, so its text
clears itself.
"""

from __future__ import annotations

from typing import Callable

from gi.repository import GLib, Gtk

from sqlide.backend import identity
from sqlide.frontend import identity as identity_ui
from sqlide.i18n import _

# How long a transient status message stays before the zone clears.
STATUS_TIMEOUT_SECONDS = 8


class StatusBar(Gtk.Box):
    def __init__(self, on_connect: Callable[[str], None]) -> None:
        super().__init__(
            spacing=12,
            margin_top=3,
            margin_bottom=3,
            margin_start=9,
            margin_end=9,
        )
        self.add_css_class("status-bar")
        self._on_connect = on_connect
        self._connection = ""
        self._status_source = 0

        # Identity.
        self._swatch = identity_ui.dot(
            identity.NONE, surface="status-swatch"
        )
        self._swatch.set_size_request(10, 10)
        self._name = Gtk.Label(xalign=0)
        self._name.add_css_class("caption-heading")
        self._badge = identity_ui.environment_badge(identity.UNSET)
        self._read_only = Gtk.Label(label=_("READ-ONLY"), visible=False)
        self._read_only.add_css_class("identity-badge")
        self._transaction = Gtk.Label(visible=False)
        self._transaction.add_css_class("caption")
        self._transaction.add_css_class("warning")
        self._connect_button = Gtk.Button(
            label=_("Connect"), visible=False, valign=Gtk.Align.CENTER
        )
        self._connect_button.add_css_class("flat")
        self._connect_button.add_css_class("caption")
        self._connect_button.set_tooltip_text(
            "Open this connection now instead of on the next query"
        )
        self._connect_button.connect(
            "clicked", lambda *_: self._on_connect(self._connection)
        )
        for widget in (
            self._swatch,
            self._name,
            self._badge,
            self._read_only,
            self._transaction,
            self._connect_button,
        ):
            widget.set_valign(Gtk.Align.CENTER)
            self.append(widget)

        self.append(_separator())

        # Context.
        self._context = Gtk.Label(xalign=0, hexpand=True, ellipsize=3)
        self._context.add_css_class("caption")
        self._context.add_css_class("dim-label")
        self.append(self._context)

        # Jobs.
        self._spinner = Gtk.Spinner(visible=False)
        self._job = Gtk.Label(visible=False)
        self._job.add_css_class("caption")
        self.append(self._spinner)
        self.append(self._job)

        # Status.
        self._status = Gtk.Label(xalign=1)
        self._status.add_css_class("caption")
        self._status.set_ellipsize(3)
        self._status.set_max_width_chars(48)
        self.append(self._status)

    # Identity zone

    def set_identity(
        self,
        connection: str,
        *,
        database: str = "",
        color: str = identity.NONE,
        environment: str = identity.UNSET,
        connected: bool = False,
        transaction: str = "",
        read_only: bool = False,
    ) -> None:
        """The connection the active tab is on. `connection` is "" only
        when no tab is open — never when one is."""
        self._connection = connection
        identity_ui.set_color(self._swatch, color)
        identity_ui.set_environment(self._badge, environment)
        if not connection:
            self._name.set_text(_("No connection"))
            self._name.add_css_class("dim-label")
            self._swatch.set_visible(False)
            self._connect_button.set_visible(False)
            self._read_only.set_visible(False)
            self._transaction.set_visible(False)
            return
        self._swatch.set_visible(True)
        self._name.remove_css_class("dim-label")
        label = connection
        if database:
            label += f" · {database}"
        if not connected:
            label += " · disconnected"
        self._name.set_text(label)
        self._name.set_tooltip_text(
            f"{connection}: connected"
            if connected
            else f"{connection}: not connected yet"
        )
        self._connect_button.set_visible(not connected)
        self._read_only.set_visible(read_only)
        self._read_only.set_tooltip_text(
            "This table has no primary key, so its rows cannot be edited"
            if read_only
            else ""
        )
        self._transaction.set_visible(bool(transaction))
        self._transaction.set_text(transaction)

    # Context, jobs, status

    def set_context(self, text: str) -> None:
        self._context.set_text(text)
        self._context.set_tooltip_text(text)

    def set_job(self, text: str) -> None:
        """A long operation started ("" when it ends). One at a time is
        enough until there is a job list to open."""
        self._job.set_text(text)
        self._job.set_visible(bool(text))
        self._spinner.set_visible(bool(text))
        if text:
            self._spinner.start()
        else:
            self._spinner.stop()

    def set_status(self, text: str, error: bool = False) -> None:
        """A transient message. The bar is not a log: it clears itself,
        and anything the user must read and copy belongs inline in the
        results area instead."""
        self._status.set_text(text)
        self._status.set_tooltip_text(text)
        if error:
            self._status.add_css_class("error")
            self._status.remove_css_class("dim-label")
        else:
            self._status.remove_css_class("error")
            self._status.add_css_class("dim-label")
        if self._status_source:
            GLib.source_remove(self._status_source)
            self._status_source = 0
        if text:
            self._status_source = GLib.timeout_add_seconds(
                STATUS_TIMEOUT_SECONDS, self._clear_status
            )

    def _clear_status(self) -> bool:
        self._status.set_text("")
        self._status.set_tooltip_text("")
        self._status_source = 0
        return GLib.SOURCE_REMOVE


def _separator() -> Gtk.Widget:
    separator = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
    separator.set_margin_top(3)
    separator.set_margin_bottom(3)
    return separator
