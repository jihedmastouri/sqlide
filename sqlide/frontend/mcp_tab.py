"""MCP server tab: config page → running page over Gtk.Stack.

Every launch is a fresh McpInstance with its own connectors — never
the window's connector cache, so several tabs run independent servers
on different ports without sharing state. Starting/stopping happens on
a worker thread (run_async); the instance's own request log arrives
through on_log, marshaled onto the main loop with GLib.idle_add since
it fires from the instance's HTTP server thread.

Session-only: tab_state() returns None. The last-used form values
(bind host, row limit, query tool, auth mode) are remembered in
settings so repeat instances don't need re-configuring; the bearer
token itself is never persisted.

McpServerWindow hosts the tab in its own top-level window rather than
a workspace tab: a running server is a background process the user
wants to keep an eye on independent of whatever tab layout they're
working in, and a separate window makes that visible (and keeps it
alive/closable) without competing for TabView space.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from gi.repository import Adw, GLib, Gtk

from sqlide.backend import settings
from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.mcp.server import (
    McpConfig,
    McpError,
    McpInstance,
    client_config_json,
    generate_token,
)
from sqlide.frontend.util import describe, run_async


class McpServerTab(Gtk.Box):
    def __init__(
        self,
        workspace_name: str,
        connections: list[ConnectionProfile],
        show_error: Callable[[str], None],
        initial_profile: ConnectionProfile | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._workspace_name = workspace_name
        self._connections = connections
        self._show_error = show_error
        self._instance: McpInstance | None = None
        self._checks: dict[str, Gtk.CheckButton] = {}

        self._stack = Gtk.Stack(vexpand=True)
        self._stack.add_named(
            self._build_config_page(initial_profile), "config"
        )
        self._stack.add_named(self._build_running_page(), "running")
        self.append(self._stack)
        self._stack.set_visible_child_name("config")

    # tab_state / lifecycle

    def tab_state(self) -> None:
        return None  # session-only, like other short-lived tool tabs

    @property
    def running(self) -> bool:
        return self._instance is not None and self._instance.running

    def stop_instance(self) -> None:
        """Best-effort synchronous stop (window close / forced tab
        close). Safe to call when nothing is running."""
        if self._instance is not None:
            try:
                self._instance.stop()
            except Exception:
                pass

    # Config page

    def _build_config_page(
        self, initial_profile: ConnectionProfile | None
    ) -> Gtk.Widget:
        defaults = settings.store.settings.mcp_defaults
        page = Adw.PreferencesPage()

        connections_group = Adw.PreferencesGroup(
            title="Connections",
            description="Which connections this server exposes "
            "(read-only, regardless of what they allow elsewhere).",
        )
        if not self._connections:
            connections_group.add(Adw.ActionRow(
                title="No connections in this workspace",
            ))
        for profile in self._connections:
            check = Gtk.CheckButton(
                active=initial_profile is not None
                and profile.name == initial_profile.name
            )
            row = Adw.ActionRow(title=profile.name, subtitle=profile.kind)
            row.add_prefix(check)
            row.set_activatable_widget(check)
            connections_group.add(row)
            self._checks[profile.name] = check
        page.add(connections_group)

        access_group = Adw.PreferencesGroup(
            title="Access Control",
            description="Port, bind address and what the query tool "
            "is allowed to touch.",
        )
        self._port_row = Adw.SpinRow(
            title="Port",
            subtitle="0 picks a free port automatically",
            adjustment=Gtk.Adjustment(
                lower=0, upper=65535, step_increment=1, value=0
            ),
        )
        access_group.add(self._port_row)

        self._public_switch = Gtk.Switch(
            valign=Gtk.Align.CENTER,
            active=defaults.get("bind_host") == "0.0.0.0",
        )
        self._public_switch.connect(
            "notify::active", lambda *_: self._update_public_warning()
        )
        public_row = Adw.ActionRow(
            title="Listen on all interfaces",
            subtitle="Off: 127.0.0.1 only (this machine). On: "
            "0.0.0.0 — reachable from the network; requires a "
            "bearer token.",
        )
        public_row.add_suffix(self._public_switch)
        public_row.set_activatable_widget(self._public_switch)
        access_group.add(public_row)
        self._public_warning = Adw.ActionRow(
            title="⚠ Anyone reaching this port can query the "
            "database(s) above without a token disabled.",
        )
        self._public_warning.add_css_class("warning")
        access_group.add(self._public_warning)

        self._query_switch = Gtk.Switch(
            valign=Gtk.Align.CENTER,
            active=defaults.get("allow_query", "1") != "0",
        )
        query_row = Adw.ActionRow(
            title="Enable the query tool",
            subtitle="Off: catalog only (list_tables/list_columns/"
            "get_ddl) — no arbitrary SELECT",
        )
        query_row.add_suffix(self._query_switch)
        query_row.set_activatable_widget(self._query_switch)
        access_group.add(query_row)

        self._row_limit_row = Adw.SpinRow(
            title="Row limit",
            subtitle="Maximum rows the query tool returns at once",
            adjustment=Gtk.Adjustment(
                lower=1, upper=100000, step_increment=100,
                value=int(defaults.get("row_limit", "500")),
            ),
        )
        access_group.add(self._row_limit_row)
        page.add(access_group)

        auth_group = Adw.PreferencesGroup(
            title="Authentication",
            description="Checked on every request; a wrong or "
            "missing token gets a 401.",
        )
        self._auth_switch = Gtk.Switch(
            valign=Gtk.Align.CENTER,
            active=defaults.get("auth_mode", "none") == "token"
            or self._public_switch.get_active(),
        )
        self._auth_switch.connect(
            "notify::active", lambda *_: self._update_token_visibility()
        )
        auth_row = Adw.ActionRow(
            title="Require a bearer token",
            subtitle="Off: no authentication (127.0.0.1 only)",
        )
        auth_row.add_suffix(self._auth_switch)
        auth_row.set_activatable_widget(self._auth_switch)
        auth_group.add(auth_row)

        self._token_entry = Gtk.Entry(
            text=generate_token(), hexpand=True, valign=Gtk.Align.CENTER
        )
        self._token_entry.set_visibility(False)
        regenerate = Gtk.Button(icon_name="view-refresh-symbolic")
        regenerate.add_css_class("flat")
        describe(regenerate, "Generate a new token")
        regenerate.connect(
            "clicked",
            lambda *_: self._token_entry.set_text(generate_token()),
        )
        token_box = Gtk.Box(spacing=6)
        token_box.append(self._token_entry)
        token_box.append(regenerate)
        self._token_row = Adw.ActionRow(title="Token")
        self._token_row.add_suffix(token_box)
        auth_group.add(self._token_row)
        page.add(auth_group)

        self._start_button = Gtk.Button(
            label="Start Server", margin_top=12, margin_bottom=24,
            margin_start=24, margin_end=24, halign=Gtk.Align.CENTER,
        )
        self._start_button.add_css_class("suggested-action")
        self._start_button.add_css_class("pill")
        self._start_button.connect("clicked", self._on_start_clicked)

        self._update_public_warning()
        self._update_token_visibility()

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        scroller = Gtk.ScrolledWindow(child=page, vexpand=True)
        content.append(scroller)
        content.append(self._start_button)
        return content

    def _update_public_warning(self) -> None:
        public = self._public_switch.get_active()
        self._public_warning.set_visible(
            public and not self._auth_switch.get_active()
        )
        if public:
            self._auth_switch.set_active(True)

    def _update_token_visibility(self) -> None:
        self._token_row.set_visible(self._auth_switch.get_active())
        self._update_public_warning()

    def _selected_connections(self) -> list[ConnectionProfile]:
        return [
            profile for profile in self._connections
            if self._checks[profile.name].get_active()
        ]

    def _on_start_clicked(self, *_args) -> None:
        profiles = self._selected_connections()
        if not profiles:
            self._show_error("Select at least one connection to expose")
            return
        token = (
            self._token_entry.get_text().strip()
            if self._auth_switch.get_active() else ""
        )
        config = McpConfig(
            profiles=profiles,
            bind_host="0.0.0.0" if self._public_switch.get_active() else "127.0.0.1",
            port=int(self._port_row.get_value()),
            token=token,
            row_limit=int(self._row_limit_row.get_value()),
            allow_query=self._query_switch.get_active(),
        )
        settings.store.update(mcp_defaults={
            "bind_host": config.bind_host,
            "row_limit": str(config.row_limit),
            "allow_query": "1" if config.allow_query else "0",
            "auth_mode": "token" if token else "none",
        })
        self._start_button.set_sensitive(False)
        self._instance = McpInstance(config, on_log=self._log)

        def work():
            return self._instance.start()

        def done(url: str) -> None:
            self._start_button.set_sensitive(True)
            self._on_started(url, token)

        def failed(exc: Exception) -> None:
            self._start_button.set_sensitive(True)
            self._instance = None
            message = str(exc) if isinstance(exc, McpError) else (
                f"Could not start the server: {exc}"
            )
            self._show_error(message)

        run_async(work, done, failed)

    # Running page

    def _build_running_page(self) -> Gtk.Widget:
        page = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=6,
            margin_top=12, margin_bottom=12, margin_start=12, margin_end=12,
        )

        url_group = Adw.PreferencesGroup(title="Server URL")
        self._url_row = Adw.ActionRow(title="", selectable=True)
        copy_url = Gtk.Button(icon_name="edit-copy-symbolic")
        copy_url.add_css_class("flat")
        describe(copy_url, "Copy URL")
        copy_url.connect("clicked", self._copy_url)
        self._url_row.add_suffix(copy_url)
        url_group.add(self._url_row)
        page.append(url_group)

        config_group = Adw.PreferencesGroup(
            title="Client Configuration",
            description="Paste into your MCP client's config "
            "(e.g. Claude Desktop/Code).",
        )
        self._config_view = Gtk.TextView(
            editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR,
            top_margin=8, bottom_margin=8, left_margin=8, right_margin=8,
        )
        config_scroller = Gtk.ScrolledWindow(
            child=self._config_view, min_content_height=120
        )
        config_scroller.add_css_class("card")
        config_buttons = Gtk.Box(spacing=6, margin_top=6)
        copy_config = Gtk.Button(label="Copy JSON")
        copy_config.connect("clicked", self._copy_config)
        save_config = Gtk.Button(label="Save JSON…")
        save_config.connect("clicked", self._save_config)
        config_buttons.append(copy_config)
        config_buttons.append(save_config)
        config_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        config_box.append(config_scroller)
        config_box.append(config_buttons)
        config_row = Adw.PreferencesRow(activatable=False)
        config_row.set_child(config_box)
        config_group.add(config_row)
        page.append(config_group)

        log_label = Gtk.Label(
            label="Request log", xalign=0, margin_top=6
        )
        log_label.add_css_class("dim-label")
        log_label.add_css_class("caption")
        page.append(log_label)
        self._log_view = Gtk.TextView(
            editable=False, monospace=True, cursor_visible=False,
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
        )
        log_scroller = Gtk.ScrolledWindow(
            child=self._log_view, vexpand=True, min_content_height=120
        )
        log_scroller.add_css_class("card")
        page.append(log_scroller)

        stop_button = Gtk.Button(
            label="Stop Server", margin_top=6, halign=Gtk.Align.CENTER,
        )
        stop_button.add_css_class("destructive-action")
        stop_button.add_css_class("pill")
        stop_button.connect("clicked", lambda *_: self._on_stop_clicked())
        page.append(stop_button)
        return page

    def _on_started(self, url: str, token: str) -> None:
        self._url_row.set_title(url)
        server_name = f"sqlide-{self._workspace_name}"
        self._config_view.get_buffer().set_text(
            client_config_json(server_name, url, token)
        )
        self._log_view.get_buffer().set_text("")
        self._stack.set_visible_child_name("running")

    def _on_stop_clicked(self) -> None:
        instance, self._instance = self._instance, None
        if instance is None:
            self._stack.set_visible_child_name("config")
            return
        run_async(
            instance.stop,
            lambda _r: self._stack.set_visible_child_name("config"),
            lambda exc: self._show_error(str(exc)),
        )

    def _log(self, line: str) -> None:
        # Called on the instance's server thread.
        GLib.idle_add(self._append_log, line)

    def _append_log(self, line: str) -> bool:
        buffer = self._log_view.get_buffer()
        end = buffer.get_end_iter()
        prefix = "\n" if buffer.get_char_count() else ""
        buffer.insert(end, f"{prefix}{line}")
        self._log_view.scroll_to_iter(
            buffer.get_end_iter(), 0, False, 0, 0
        )
        return GLib.SOURCE_REMOVE

    def _copy_url(self, *_args) -> None:
        self.get_clipboard().set(self._url_row.get_title())

    def _copy_config(self, *_args) -> None:
        buffer = self._config_view.get_buffer()
        text = buffer.get_text(
            buffer.get_start_iter(), buffer.get_end_iter(), False
        )
        self.get_clipboard().set(text)

    def _save_config(self, *_args) -> None:
        dialog = Gtk.FileDialog(
            title="Save Client Configuration",
            initial_name=f"sqlide-{self._workspace_name}-mcp.json",
        )
        dialog.save(self.get_root(), None, self._save_config_finished)

    def _save_config_finished(self, dialog: Gtk.FileDialog, result) -> None:
        try:
            file = dialog.save_finish(result)
        except GLib.Error:
            return  # cancelled
        buffer = self._config_view.get_buffer()
        text = buffer.get_text(
            buffer.get_start_iter(), buffer.get_end_iter(), False
        )
        try:
            Path(file.get_path()).write_text(text, encoding="utf-8")
        except OSError as exc:
            self._show_error(f"Could not save: {exc}")


class McpServerWindow(Adw.Window):
    def __init__(
        self,
        workspace_name: str,
        connections: list[ConnectionProfile],
        show_error: Callable[[str], None],
        initial_profile: ConnectionProfile | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.set_title("MCP Server")
        self.set_default_size(480, 560)

        self.tab = McpServerTab(
            workspace_name, connections, show_error, initial_profile
        )

        header = Adw.HeaderBar()
        toolbar_view = Adw.ToolbarView()
        toolbar_view.add_top_bar(header)
        toolbar_view.set_content(self.tab)
        self.set_content(toolbar_view)

        self.connect("close-request", self._on_close_request)
        self._close_confirmed = False

    def _on_close_request(self, *_args) -> bool:
        if self._close_confirmed or not self.tab.running:
            return False
        dialog = Adw.AlertDialog(
            heading="MCP Server Running",
            body="This server is still running. Close the window "
            "anyway and stop it?",
        )
        dialog.add_response("cancel", "Keep Open")
        dialog.add_response("stop", "Stop and Close")
        dialog.set_response_appearance(
            "stop", Adw.ResponseAppearance.DESTRUCTIVE
        )
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def respond(_dialog, response: str) -> None:
            if response == "stop":
                self.tab.stop_instance()
                self._close_confirmed = True
                self.close()

        dialog.connect("response", respond)
        dialog.present(self)
        return True
