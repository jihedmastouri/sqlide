"""Reading one value, and one row, in full (CORE-42).

A grid cell is a single ellipsized line, so a JSON document, a stack
trace or a blob is effectively invisible in it, and a forty-column row
has to be read by scrolling sideways. Two views answer that, both fed
by the cell the grid already tracks for selection:

- `ValuePage` — the side panel's Value page: the focused cell rendered
  in full and, where the grid would let an inline edit through, edited
  there. It never writes on its own; the callback the grid hands over
  is the same `on_edit` path an `EditableLabel` commit takes, so the
  edit lands in the tab's pending list, the Save preview and CORE-39's
  transaction like any other.
- `RecordView` — the focused row pivoted into a name/value list, with
  the same renderers and the same editability, and up/down to walk the
  loaded rows.

How a value is rendered is decided from the value, never from the
engine: `describe_value` returns plain text, pretty-printed JSON, or a
hex+ASCII dump with the byte length and — for a geometry — the
description backend/db/geo.py already produces. JSON is *displayed*
pretty-printed and the original text is one toggle away; nothing is
written back until the buffer is edited and Apply is pressed, so a
document that does not round-trip through a reformat is never
reformatted behind the user's back.

Very large values are truncated for display with the full size stated
rather than handed to a text view that would freeze on them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

import gi
from gi.repository import Adw, Gtk, Pango

from sqlide.backend import export
from sqlide.backend.db import geo
from sqlide.frontend.util import describe
from sqlide.i18n import _

try:
    gi.require_version("GtkSource", "5")
    from gi.repository import GtkSource

    GtkSource.init()
except (ValueError, ImportError):  # pragma: no cover - depends on the host
    GtkSource = None

#: Characters of text shown before the display is truncated. Big
#: enough that a real document is whole, small enough that a 200 MB
#: column does not go into a text view.
MAX_TEXT = 200_000
#: Bytes dumped before a blob is summarised by size instead.
MAX_BYTES = 64 * 1024
#: Bytes per line of the hex dump.
_DUMP_WIDTH = 16


@dataclass(frozen=True)
class CellValue:
    """The focused cell, as the grid describes it to the value views.

    `apply` is the grid's own edit path (None where the cell cannot be
    edited right now); it takes the new text, or None for NULL.
    """

    column: str
    row: int  # 1-based, as the row-number column counts
    value: Any
    #: Whether the grid resolved this column as WKB (PG-04), which is
    #: what turns the hex dump's header into a geometry description.
    geometry: bool = False
    apply: Callable[[str | None], None] | None = None

    @property
    def editable(self) -> bool:
        return self.apply is not None


@dataclass(frozen=True)
class ValueRender:
    """How one value should be shown.

    `text` is what goes on screen (already truncated), `raw` the text
    an edit starts from — the two differ only for JSON, which is
    pretty-printed for reading and left alone for writing.
    """

    kind: str  # "null" | "text" | "json" | "binary"
    text: str
    raw: str
    detail: str
    truncated: bool = False

    @property
    def formattable(self) -> bool:
        """Whether the pretty/raw toggle has two sides to offer."""
        return self.kind == "json" and self.text != self.raw


def _truncate(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_TEXT:
        return text, False
    return text[:MAX_TEXT], True


def hex_dump(raw: bytes, width: int = _DUMP_WIDTH) -> str:
    """Bytes as offset / hex / ASCII columns, the way every hex viewer
    shows them — the printable side is what tells a UTF-8 blob from a
    PNG at a glance."""
    lines = []
    for start in range(0, len(raw), width):
        chunk = raw[start : start + width]
        hexed = " ".join(f"{b:02X}" for b in chunk)
        ascii_ = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{start:08X}  {hexed:<{width * 3 - 1}}  {ascii_}")
    return "\n".join(lines)


def describe_value(value: Any, geometry: bool = False) -> ValueRender:
    """Pick a rendering from the value itself.

    `geometry` says the column is one the grid resolved as WKB, which
    is the only hint an engine gives here — everything else is decided
    from the bytes or the characters in hand.
    """
    if value is None:
        return ValueRender("null", "", "", _("NULL"))

    if export.is_binary(value):
        raw = bytes(value)
        parts = [_("{n} bytes").format(n=len(raw))]
        if geometry:
            summary = geo.summarize(raw)
            if summary:
                parts.append(summary)
        shown = raw[:MAX_BYTES]
        return ValueRender(
            "binary",
            hex_dump(shown),
            "",
            " · ".join(parts),
            truncated=len(shown) < len(raw),
        )

    if geometry:
        summary = geo.summarize(value)
        if summary:
            text, cut = _truncate(str(value))
            return ValueRender("binary", text, "", summary, truncated=cut)

    text = str(value)
    stripped = text.strip()
    if stripped[:1] in ("{", "[") and stripped[-1:] in ("}", "]"):
        try:
            parsed = json.loads(stripped)
        except (ValueError, RecursionError):
            parsed = None
        if parsed is not None or stripped in ("{}", "[]"):
            pretty = json.dumps(parsed, indent=2, ensure_ascii=False)
            shown, cut = _truncate(pretty)
            return ValueRender(
                "json",
                shown,
                text,
                _("JSON · {n} characters").format(n=len(text)),
                truncated=cut,
            )

    shown, cut = _truncate(text)
    lines = text.count("\n") + 1
    detail = _("{n} characters").format(n=len(text))
    if lines > 1:
        detail = f"{detail} · " + _("{n} lines").format(n=lines)
    return ValueRender("text", shown, text, detail, truncated=cut)


class TextArea(Gtk.ScrolledWindow):
    """A monospace, wrapping text view — GtkSourceView with the JSON
    language when its typelib is there, a plain TextView otherwise. The
    same widget reads a value and edits it; `set_editable` is the only
    difference between the two jobs."""

    def __init__(self, editable: bool = False) -> None:
        super().__init__(vexpand=True, hexpand=True)
        if GtkSource is not None:
            self._buffer = GtkSource.Buffer()
            self.view = GtkSource.View(buffer=self._buffer)
        else:  # pragma: no cover - depends on the host
            self._buffer = Gtk.TextBuffer()
            self.view = Gtk.TextView(buffer=self._buffer)
        self._style = Adw.StyleManager.get_default()
        self._style.connect("notify::dark", self._on_dark_changed)
        self._on_dark_changed(self._style)
        self.view.set_monospace(True)
        self.view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.view.set_left_margin(8)
        self.view.set_right_margin(8)
        self.view.set_top_margin(8)
        self.view.add_css_class("sqlide-editor")
        self.set_editable(editable)
        self.set_child(self.view)

    def set_editable(self, editable: bool) -> None:
        self.view.set_editable(editable)
        self.view.set_cursor_visible(editable)

    def set_language(self, name: str) -> None:
        """Highlight as `name` ("json"), or as nothing with ""."""
        if GtkSource is None:  # pragma: no cover - depends on the host
            return
        language = None
        if name:
            language = GtkSource.LanguageManager.get_default().get_language(
                name
            )
        self._buffer.set_language(language)

    def get_text(self) -> str:
        start, end = self._buffer.get_bounds()
        return self._buffer.get_text(start, end, False)

    def set_text(self, text: str) -> None:
        self._buffer.set_text(text)

    def connect_changed(self, callback: Callable[[], None]) -> None:
        self._buffer.connect("changed", lambda *_: callback())

    def _on_dark_changed(self, style_manager, *_args) -> None:
        if GtkSource is None:  # pragma: no cover - depends on the host
            return
        name = "Adwaita-dark" if style_manager.get_dark() else "Adwaita"
        scheme = GtkSource.StyleSchemeManager.get_default().get_scheme(name)
        if scheme is not None:
            self._buffer.set_style_scheme(scheme)


class ValuePage(Gtk.Box):
    """The side panel's Value page: the focused cell in full.

    Filled on every selection change, whether or not anyone is looking
    — opening the panel is then enough to read the cell. Apply is live
    only once the buffer differs from what was loaded, so pressing it
    is always a deliberate write and a pretty-printed document is never
    saved back reformatted by accident.
    """

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._cell: CellValue | None = None
        self._render: ValueRender | None = None
        self._loaded = ""  # what the buffer was filled with
        self._formatted = True

        self._placeholder = Gtk.Label(
            label=_("Select a cell to see its value"),
            margin_top=24,
            wrap=True,
        )
        self._placeholder.add_css_class("dim-label")
        self.append(self._placeholder)

        head = Gtk.Box(
            spacing=6,
            margin_top=6,
            margin_bottom=6,
            margin_start=8,
            margin_end=8,
        )
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        self._title = Gtk.Label(
            xalign=0, ellipsize=Pango.EllipsizeMode.END
        )
        self._title.add_css_class("heading")
        self._detail = Gtk.Label(
            xalign=0, wrap=True, selectable=True
        )
        self._detail.add_css_class("dim-label")
        self._detail.add_css_class("caption")
        titles.append(self._title)
        titles.append(self._detail)
        head.append(titles)
        self._raw_toggle = Gtk.ToggleButton(label=_("Raw"))
        describe(self._raw_toggle, _("Show the value exactly as stored"))
        self._raw_toggle.connect("toggled", self._on_raw_toggled)
        head.append(self._raw_toggle)
        self._head = head
        self.append(head)

        self._text = TextArea()
        self._text.connect_changed(self._on_changed)
        self.append(self._text)

        self._actions = Gtk.Box(
            spacing=6,
            margin_top=6,
            margin_bottom=6,
            margin_start=8,
            margin_end=8,
            halign=Gtk.Align.END,
        )
        self._null = Gtk.Button(label=_("Set NULL"))
        self._null.connect("clicked", lambda *_: self._write(None))
        self._apply = Gtk.Button(label=_("Apply"))
        self._apply.add_css_class("suggested-action")
        self._apply.connect("clicked", lambda *_: self._write(self._text.get_text()))
        self._actions.append(self._null)
        self._actions.append(self._apply)
        self.append(self._actions)

        self.set_value(None)

    def set_value(self, cell: CellValue | None) -> None:
        self._cell = cell
        showing = cell is not None
        self._placeholder.set_visible(not showing)
        for widget in (self._head, self._text, self._actions):
            widget.set_visible(showing)
        if cell is None:
            self._render = None
            return
        self._render = describe_value(cell.value, geometry=cell.geometry)
        self._title.set_text(_("{column} · row {row}").format(
            column=cell.column, row=cell.row
        ))
        detail = self._render.detail
        if self._render.truncated:
            detail = f"{detail} · " + _("truncated for display")
        self._detail.set_text(detail)
        self._raw_toggle.set_visible(self._render.formattable)
        self._raw_toggle.set_active(False)
        self._fill()

    def _fill(self) -> None:
        render = self._render
        if render is None:
            return
        editable = (
            self._cell is not None
            and self._cell.editable
            and render.kind != "binary"
        )
        raw = self._raw_toggle.get_active() and render.formattable
        self._loaded = render.raw if raw else render.text
        self._text.set_language("json" if render.kind == "json" else "")
        self._text.set_text(self._loaded)
        # A truncated display is a fragment; applying it would write the
        # fragment over the value.
        self._text.set_editable(editable and not render.truncated)
        self._actions.set_visible(editable)
        self._null.set_sensitive(editable)
        self._on_changed()

    def _on_raw_toggled(self, *_args) -> None:
        self._fill()

    def _on_changed(self) -> None:
        changed = self._text.get_text() != self._loaded
        self._apply.set_sensitive(
            changed and self._text.view.get_editable()
        )

    def _write(self, text: str | None) -> None:
        if self._cell is None or self._cell.apply is None:
            return
        self._cell.apply(text)


class RecordView(Gtk.Box):
    """One row of a grid as a name/value list.

    The grid is the source: which columns it has, what the row holds,
    and whether a cell can be edited right now are all its answers, so
    the record view inherits the read-only rules, the pending-edit
    mechanism and the value renderers without a second copy of any of
    them. Up and down (or the two buttons) move between loaded rows.
    """

    def __init__(self, grid) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._grid = grid
        self._position = 0
        self._rows: list[tuple[Gtk.Widget, int]] = []

        bar = Gtk.Box(
            spacing=6,
            margin_top=6,
            margin_bottom=6,
            margin_start=8,
            margin_end=8,
        )
        self._prev = Gtk.Button(icon_name="go-up-symbolic")
        describe(self._prev, _("Previous row"))
        self._prev.connect("clicked", lambda *_: self.step(-1))
        self._next = Gtk.Button(icon_name="go-down-symbolic")
        describe(self._next, _("Next row"))
        self._next.connect("clicked", lambda *_: self.step(1))
        self._label = Gtk.Label(xalign=0, hexpand=True)
        self._label.add_css_class("heading")
        bar.append(self._prev)
        bar.append(self._next)
        bar.append(self._label)
        self.append(bar)

        self._list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self._list.add_css_class("boxed-list")
        self._list.set_margin_start(8)
        self._list.set_margin_end(8)
        self._list.set_margin_bottom(8)
        placeholder = Gtk.Label(
            label=_("No row to show"), margin_top=24, wrap=True
        )
        placeholder.add_css_class("dim-label")
        self._list.set_placeholder(placeholder)
        self.append(
            Gtk.ScrolledWindow(child=self._list, vexpand=True, hexpand=True)
        )

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)

    # Navigation

    def reset(self) -> None:
        """Back to the first row without touching the grid's selection —
        what a fresh load leaves behind."""
        self._position = 0
        self.refresh()

    def step(self, delta: int) -> None:
        self.show_row(self._position + delta)

    def show_row(self, position: int) -> None:
        count = self._grid.row_count()
        if count:
            position = max(0, min(count - 1, position))
        self._position = position
        self.refresh()
        if count:
            self._grid.focus_cell(position, 0)

    def refresh(self) -> None:
        """Rebuild the list from the grid as it is now — called on
        every load, every edit and every move."""
        while (row := self._list.get_row_at_index(0)) is not None:
            self._list.remove(row)
        columns = self._grid.columns()
        values = self._grid.row_values(self._position)
        count = self._grid.row_count()
        self._prev.set_sensitive(self._position > 0)
        self._next.set_sensitive(self._position + 1 < count)
        if values is None:
            self._label.set_text(_("No rows"))
            return
        self._label.set_text(
            _("Row {n} of {total}").format(n=self._position + 1, total=count)
        )
        for index, name in enumerate(columns):
            self._list.append(self._build_row(index, name, values[index]))

    def _build_row(self, index: int, name: str, value) -> Gtk.Widget:
        render = describe_value(
            value, geometry=self._grid.is_geometry_column(index)
        )
        row = Adw.ActionRow(title=name)
        row.set_use_markup(False)
        row.set_title_lines(1)
        editable = self._grid.cell_editable(index, value)
        if render.kind == "null":
            body = Gtk.Label(label=_("NULL"), xalign=0)
            body.add_css_class("dim-label")
        elif editable and not render.truncated and render.kind != "binary":
            body = Gtk.TextView(monospace=True)
            body.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
            body.get_buffer().set_text(render.raw)
            body.add_css_class("card")
            focus = Gtk.EventControllerFocus()
            focus.connect(
                "leave", self._on_body_leave, body, index, render.raw
            )
            body.add_controller(focus)
        else:
            body = Gtk.Label(
                label=render.text, xalign=0, selectable=True, wrap=True
            )
            body.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            if render.kind == "binary":
                body.add_css_class("monospace")
        body.set_hexpand(True)
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True
        )
        box.append(body)
        if render.detail:
            detail = Gtk.Label(label=render.detail, xalign=0)
            detail.add_css_class("dim-label")
            detail.add_css_class("caption")
            box.append(detail)
        row.add_suffix(box)
        return row

    def _on_body_leave(self, _controller, view, index: int, original: str) -> None:
        buffer = view.get_buffer()
        start, end = buffer.get_bounds()
        text = buffer.get_text(start, end, False)
        if text == original:
            return
        self._grid.edit_cell(self._position, index, text)

    def _on_key(self, _controller, keyval, _keycode, _state) -> bool:
        from gi.repository import Gdk

        if keyval == Gdk.KEY_Up:
            self.step(-1)
            return True
        if keyval == Gdk.KEY_Down:
            self.step(1)
            return True
        return False
