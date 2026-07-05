"""SQL editor widget for the query console.

Uses GtkSourceView 5 (SQL highlighting, line numbers, dark-aware style
scheme) when its typelib is installed. Otherwise degrades to a plain
monospace Gtk.TextView with a small regex-based highlighter (keywords,
strings, comments, numbers) so the console is still readable.
"""

from __future__ import annotations

import re

import gi
from gi.repository import Adw, Gtk, Pango

from sqlide.frontend.completion import (
    CompletionController,
    CompletionProvider,
    KeywordCompletionProvider,
)

try:
    gi.require_version("GtkSource", "5")
    from gi.repository import GtkSource

    GtkSource.init()
except (ValueError, ImportError):
    GtkSource = None

_KEYWORDS = (
    "select insert update delete from where and or not null is in like "
    "between exists group by having order limit offset join left right "
    "inner outer full cross on as distinct union all values into set "
    "create table view index drop alter add column primary key foreign "
    "references default unique check constraint if case when then else "
    "end begin commit rollback transaction pragma explain with recursive "
    "asc desc count avg sum min max cast"
).split()

# One pass, one match per token: a keyword inside a string or comment is
# consumed by the earlier alternative and never tagged as a keyword.
_TOKEN_RE = re.compile(
    r"(?P<comment>--[^\n]*|/\*.*?\*/)"
    r"|(?P<string>'(?:[^']|'')*')"
    r"|(?P<keyword>\b(?:" + "|".join(_KEYWORDS) + r")\b)"
    r"|(?P<number>\b\d+(?:\.\d+)?\b)",
    re.IGNORECASE | re.DOTALL,
)

_PALETTES = {
    # GNOME palette shades: readable on the default light/dark backgrounds.
    False: {"keyword": "#1c71d8", "string": "#26a269",
            "comment": "#77767b", "number": "#c64600"},
    True: {"keyword": "#62a0ea", "string": "#8ff0a4",
           "comment": "#9a9996", "number": "#ffbe6c"},
}


class SqlEditor(Gtk.ScrolledWindow):
    """Scrollable SQL editor; the text view is exposed as `.view` so the
    console can attach its key controller. With editable=False it is a
    highlighted read-only SQL viewer (definition tab, side panel)."""

    def __init__(self, sql: str = "", editable: bool = True) -> None:
        super().__init__()
        style_manager = Adw.StyleManager.get_default()
        if GtkSource is not None:
            self._buffer = GtkSource.Buffer()
            language = GtkSource.LanguageManager.get_default().get_language("sql")
            if language is not None:
                self._buffer.set_language(language)
            self.view = GtkSource.View(buffer=self._buffer)
            self.view.set_show_line_numbers(True)
            self.view.set_highlight_current_line(True)
            self.view.set_tab_width(4)
            self.view.set_auto_indent(True)
        else:
            self._buffer = Gtk.TextBuffer()
            self._tags = {
                "keyword": self._buffer.create_tag(
                    "keyword", weight=Pango.Weight.BOLD
                ),
                "string": self._buffer.create_tag("string"),
                "comment": self._buffer.create_tag(
                    "comment", style=Pango.Style.ITALIC
                ),
                "number": self._buffer.create_tag("number"),
            }
            self._buffer.connect("changed", lambda *_: self._highlight())
            self.view = Gtk.TextView(buffer=self._buffer)
        style_manager.connect("notify::dark", self._on_dark_changed)
        self._on_dark_changed(style_manager)
        # Font size comes from the settings-driven provider in
        # application.py, keyed on this class.
        self.view.add_css_class("sqlide-editor")
        self.view.set_monospace(True)
        self.view.set_left_margin(8)
        self.view.set_top_margin(8)
        if not editable:
            self.view.set_editable(False)
            self.view.set_cursor_visible(False)
            if GtkSource is not None:
                self.view.set_show_line_numbers(False)
                self.view.set_highlight_current_line(False)
        self.set_child(self.view)
        self._completion = CompletionController(self.view)
        if editable:
            self._completion.add_provider(KeywordCompletionProvider(_KEYWORDS))
        self.set_text(sql)

    def add_completion_provider(self, provider: CompletionProvider) -> None:
        """Plug in another suggestion source (schema, LSP, …)."""
        self._completion.add_provider(provider)

    def get_text(self) -> str:
        start, end = self._buffer.get_bounds()
        return self._buffer.get_text(start, end, False)

    def set_text(self, sql: str) -> None:
        self._buffer.set_text(sql)

    def get_selection(self) -> str:
        """Selected text, or "" when nothing is selected."""
        bounds = self._buffer.get_selection_bounds()
        if not bounds:
            return ""
        start, end = bounds
        return self._buffer.get_text(start, end, False)

    def get_cursor_offset(self) -> int:
        insert = self._buffer.get_iter_at_mark(self._buffer.get_insert())
        return insert.get_offset()

    def _on_dark_changed(self, style_manager, *_args) -> None:
        dark = style_manager.get_dark()
        if GtkSource is not None:
            name = "Adwaita-dark" if dark else "Adwaita"
            scheme = GtkSource.StyleSchemeManager.get_default().get_scheme(name)
            if scheme is not None:
                self._buffer.set_style_scheme(scheme)
        else:
            # Tags restyle live; no re-highlight needed.
            for kind, color in _PALETTES[dark].items():
                self._tags[kind].set_property("foreground", color)

    def _highlight(self) -> None:
        start, end = self._buffer.get_bounds()
        for tag in self._tags.values():
            self._buffer.remove_tag(tag, start, end)
        text = self._buffer.get_text(start, end, False)
        for match in _TOKEN_RE.finditer(text):
            self._buffer.apply_tag(
                self._tags[match.lastgroup],
                self._buffer.get_iter_at_offset(match.start()),
                self._buffer.get_iter_at_offset(match.end()),
            )
