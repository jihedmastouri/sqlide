"""CLI client console tab: a psql/mysql/sqlite-style terminal.

A scrollback view on top, a single-line prompt entry at the bottom.
Each line the user submits is echoed after the prompt and answered by
the backend interpreter (backend/db/cli.py), which handles both
meta-commands (``\\dt``, ``\\d table``, ``.tables``, ``.schema`` …) and
plain SQL, returning text the way a real client prints it.

Like the query console, a CLI console is not welded to one connection:
a toolbar dropdown over the workspace's shared connection names picks
the target, resolved to a profile at submit time. Switching connection
reprints the prompt for the new session. Up/Down walk the input
history; commands run on a worker thread so the UI never blocks.
"""

from __future__ import annotations

from typing import Callable

from gi.repository import Gdk, GLib, Gtk

from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.db import cli
from sqlide.backend.db.base import Connector
from sqlide.backend.workspaces import TabState
from sqlide.frontend.util import describe, run_async


class CliConsole(Gtk.Box):
    def __init__(
        self,
        connection_names: Gtk.StringList,
        find_connection: Callable[[str], ConnectionProfile | None],
        ensure_connector: Callable[[ConnectionProfile], Connector],
        connection: str = "",
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._find_connection = find_connection
        self._ensure = ensure_connector
        # Set by the window after the tab page exists (updates the title).
        self.on_connection_changed: Callable[[str], None] | None = None
        self._history: list[str] = []
        self._history_index = 0  # == len(history): the fresh, unsent line
        self._draft = ""  # the in-progress line stashed while browsing history
        self._busy = False

        toolbar = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=6,
            margin_top=6,
            margin_bottom=6,
            margin_start=6,
            margin_end=6,
        )
        self._dropdown = Gtk.DropDown(model=connection_names)
        self._dropdown.set_tooltip_text("Connection this session talks to")
        label = Gtk.Label(label="CLI client")
        label.add_css_class("dim-label")
        clear = Gtk.Button(icon_name="edit-clear-all-symbolic")
        clear.add_css_class("flat")
        describe(clear, "Clear the scrollback")
        clear.connect("clicked", lambda *_: self._clear())
        toolbar.append(label)
        toolbar.append(self._dropdown)
        toolbar.append(Gtk.Box(hexpand=True))
        toolbar.append(clear)
        self.append(toolbar)

        # Scrollback: read-only monospace view that we only ever append
        # to, auto-scrolled to the bottom after each write.
        self._output = Gtk.TextView(
            editable=False,
            cursor_visible=False,
            monospace=True,
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
            top_margin=8,
            bottom_margin=8,
            left_margin=8,
            right_margin=8,
        )
        self._output.add_css_class("cli-output")
        self._buffer = self._output.get_buffer()
        self._tag_prompt = self._buffer.create_tag("prompt", weight=700)
        self._tag_error = self._buffer.create_tag("error")
        self._tag_error.set_property("foreground", "#e01b24")
        self._tag_dim = self._buffer.create_tag("dim")
        self._tag_dim.set_property("foreground", "#77767b")
        self._end_mark = self._buffer.create_mark(
            None, self._buffer.get_end_iter(), False
        )
        scroller = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        scroller.set_child(self._output)
        self.append(scroller)

        # Prompt + input line, mimicking a terminal's cursor line.
        entry_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=0,
            margin_start=8,
            margin_end=8,
            margin_bottom=8,
        )
        self._prompt_label = Gtk.Label(xalign=0)
        self._prompt_label.add_css_class("cli-prompt")
        self._entry = Gtk.Entry(hexpand=True)
        self._entry.add_css_class("cli-entry")
        self._entry.set_placeholder_text(
            "Type SQL or a meta-command (\\? for help)"
        )
        self._entry.connect("activate", self._on_submit)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key_pressed)
        self._entry.add_controller(keys)
        entry_row.append(self._prompt_label)
        entry_row.append(self._entry)
        self.append(entry_row)

        if connection:
            self.select_connection(connection)
        self._dropdown.connect(
            "notify::selected-item", self._connection_selected
        )
        self._banner()
        self._update_prompt()
        GLib.idle_add(self._focus_entry)

    # Connection

    def selected_connection(self) -> str:
        item = self._dropdown.get_selected_item()
        return item.get_string() if item is not None else ""

    def select_connection(self, name: str) -> None:
        model = self._dropdown.get_model()
        for i in range(model.get_n_items()):
            if model.get_string(i) == name:
                self._dropdown.set_selected(i)
                return

    def _profile(self) -> ConnectionProfile | None:
        return self._find_connection(self.selected_connection())

    def _connection_selected(self, *_args) -> None:
        name = self.selected_connection()
        profile = self._profile()
        if profile is not None:
            self._write(
                f"\nConnected to “{name}” ({profile.kind}).\n", self._tag_dim
            )
        self._update_prompt()
        if self.on_connection_changed is not None:
            self.on_connection_changed(name)

    def _update_prompt(self) -> None:
        profile = self._profile()
        if profile is None:
            self._prompt_label.set_text("(no connection) ")
            return
        self._prompt_label.set_text(
            cli.prompt_for(profile.kind, profile.database)
        )

    def tab_state(self) -> TabState:
        return TabState(kind="cli", connection=self.selected_connection())

    # Input handling

    def _on_key_pressed(self, _controller, keyval, _keycode, _state) -> bool:
        if keyval == Gdk.KEY_Up:
            self._recall(-1)
            return True
        if keyval == Gdk.KEY_Down:
            self._recall(1)
            return True
        return False

    def _recall(self, step: int) -> None:
        if not self._history:
            return
        if self._history_index == len(self._history):
            self._draft = self._entry.get_text()
        index = self._history_index + step
        index = max(0, min(index, len(self._history)))
        self._history_index = index
        text = self._draft if index == len(self._history) else self._history[index]
        self._entry.set_text(text)
        self._entry.set_position(-1)

    def _on_submit(self, *_args) -> None:
        if self._busy:
            return
        line = self._entry.get_text()
        self._entry.set_text("")
        if line.strip() and (not self._history or self._history[-1] != line):
            self._history.append(line)
        self._history_index = len(self._history)
        self._draft = ""

        profile = self._profile()
        prompt = self._prompt_label.get_text()
        self._write(prompt, self._tag_prompt)
        self._write(line + "\n")
        if not line.strip():
            return
        if profile is None:
            self._write(
                "No connection selected — pick one in the toolbar.\n",
                self._tag_error,
            )
            return

        self._set_busy(True)
        kind = profile.kind

        def work() -> cli.CliOutput:
            connector = self._ensure(profile)
            return cli.run_command(connector, kind, line)

        def done(output: cli.CliOutput) -> None:
            self._set_busy(False)
            if output.text:
                self._write(
                    output.text.rstrip("\n") + "\n",
                    None if output.ok else self._tag_error,
                )

        def failed(exc: Exception) -> None:
            self._set_busy(False)
            self._write(f"ERROR:  {exc}\n", self._tag_error)

        run_async(work, done, failed)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._entry.set_sensitive(not busy)
        if not busy:
            self._focus_entry()

    def _focus_entry(self) -> bool:
        self._entry.grab_focus()
        return False

    # Scrollback

    def _banner(self) -> None:
        self._write(
            "sqlide CLI client — meta-commands (\\?, \\dt, \\d, .tables, "
            ".schema …) or plain SQL.\n",
            self._tag_dim,
        )

    def _clear(self) -> None:
        self._buffer.set_text("")
        self._banner()
        self._focus_entry()

    def _write(self, text: str, tag: Gtk.TextTag | None = None) -> None:
        end = self._buffer.get_end_iter()
        if tag is not None:
            self._buffer.insert_with_tags(end, text, tag)
        else:
            self._buffer.insert(end, text)
        # Scroll the freshly appended text into view.
        self._buffer.move_mark(self._end_mark, self._buffer.get_end_iter())
        self._output.scroll_mark_onscreen(self._end_mark)
