"""Pluggable completion for the SQL editor.

CompletionController drives a suggestion popup over any Gtk.TextView.
Suggestions come from registered CompletionProvider implementations —
the built-in keyword provider and the language-server provider (see
lsp_completion.py). Providers run on a worker thread (via run_async),
so complete() may block on I/O.

Keyword suggestions — from either provider — are spelled the way the
sql_keyword_case setting asks (see backend/settings.apply_keyword_case).
That happens here, once, on everything a provider marks as a keyword,
so identifier suggestions are left in the case the catalog reported.

Triggers: automatically after typing a word of MIN_WORD characters,
or Ctrl+Space. Up/Down select, Tab/Enter accept, Escape dismisses.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from gi.repository import Gdk, Gtk

from sqlide.backend.settings import apply_keyword_case
from sqlide.frontend.util import run_async

MIN_WORD = 2
# The detail a provider marks a keyword suggestion with. Matching it is
# what makes the case setting apply to a suggestion.
KEYWORD_DETAIL = "keyword"
MAX_RESULTS = 50


@dataclass(frozen=True)
class CompletionContext:
    text: str  # whole buffer
    offset: int  # cursor position, in characters
    word: str  # word fragment before the cursor (may be "")


@dataclass(frozen=True)
class Completion:
    text: str  # replaces the current word when accepted
    detail: str = ""  # dim annotation, e.g. "keyword" or "table"

    @property
    def is_keyword(self) -> bool:
        """Whether the case setting applies to this suggestion. Only
        keywords: an identifier keeps the catalog's own spelling."""
        return self.detail == KEYWORD_DETAIL


class CompletionProvider(ABC):
    @abstractmethod
    def complete(self, context: CompletionContext) -> list[Completion]:
        """Suggestions for the context, best first. Worker thread."""


class KeywordCompletionProvider(CompletionProvider):
    """Prefix-matches a fixed word list. Case is not this provider's
    business — the controller spells every keyword suggestion the way
    the setting asks."""

    def __init__(self, keywords: list[str]) -> None:
        self._keywords = sorted(k.lower() for k in keywords)

    def complete(self, context: CompletionContext) -> list[Completion]:
        prefix = context.word.lower()
        return [
            Completion(k, detail=KEYWORD_DETAIL)
            for k in self._keywords
            if k.startswith(prefix) and k != prefix
        ]


class CompletionController:
    """Owns the popup and key handling for one text view."""

    def __init__(self, view: Gtk.TextView) -> None:
        self._view = view
        self._buffer = view.get_buffer()
        self._providers: list[CompletionProvider] = []
        self._items: list[Completion] = []
        self._seq = 0  # discards results of superseded requests
        self._inserting = False

        self._list = Gtk.ListBox()
        self._list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._list.connect("row-activated", self._on_row_activated)
        scroller = Gtk.ScrolledWindow(child=self._list)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_max_content_height(200)
        scroller.set_propagate_natural_height(True)
        scroller.set_min_content_width(220)
        # autohide off: focus must stay in the text view while it shows.
        self._popover = Gtk.Popover(
            child=scroller, has_arrow=False, autohide=False
        )
        self._popover.set_position(Gtk.PositionType.BOTTOM)
        self._popover.set_parent(view)
        view.connect("destroy", lambda *_: self._popover.unparent())

        keys = Gtk.EventControllerKey()
        # Capture phase: accepting with Enter must win over other key
        # handlers on the view (the console's Ctrl+Enter controller).
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key_pressed)
        view.add_controller(keys)
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", lambda *_: self._hide())
        view.add_controller(focus)
        self._buffer.connect("changed", self._on_changed)

    def add_provider(self, provider: CompletionProvider) -> None:
        self._providers.append(provider)

    # Triggering

    def _on_changed(self, _buffer) -> None:
        if not self._inserting:
            self._refresh(min_word=MIN_WORD)

    def _refresh(self, min_word: int) -> None:
        context = self._context()
        if len(context.word) < min_word or not self._providers:
            self._hide()
            return
        self._seq += 1
        seq = self._seq
        providers = list(self._providers)

        def cased(item: Completion, typed: str) -> Completion:
            """Read per request, so a change to the setting reaches the
            next popup without a restart."""
            if not item.is_keyword:
                return item
            return Completion(
                apply_keyword_case(item.text, typed), detail=item.detail
            )

        def work():
            items: list[Completion] = []
            for provider in providers:
                items.extend(provider.complete(context))
            items = [cased(c, context.word) for c in items]
            seen: set[str] = set()
            unique = [
                c for c in items
                if c.text not in seen and not seen.add(c.text)
            ]
            return unique[:MAX_RESULTS]

        def done(items):
            if seq != self._seq:
                return
            if items:
                self._show(items)
            else:
                self._hide()

        run_async(work, done, lambda _exc: self._hide())

    def _context(self) -> CompletionContext:
        cursor = self._buffer.get_iter_at_mark(self._buffer.get_insert())
        word_start = self._word_start(cursor)
        start, end = self._buffer.get_bounds()
        return CompletionContext(
            text=self._buffer.get_text(start, end, False),
            offset=cursor.get_offset(),
            word=self._buffer.get_text(word_start, cursor, False),
        )

    def _word_start(self, cursor: Gtk.TextIter) -> Gtk.TextIter:
        start = cursor.copy()
        while not start.is_start():
            prev = start.copy()
            prev.backward_char()
            ch = prev.get_char()
            if not (ch.isalnum() or ch == "_"):
                break
            start = prev
        return start

    # Popup

    def _show(self, items: list[Completion]) -> None:
        self._items = items
        while (row := self._list.get_row_at_index(0)) is not None:
            self._list.remove(row)
        for item in items:
            box = Gtk.Box(
                spacing=12,
                margin_start=8,
                margin_end=8,
                margin_top=2,
                margin_bottom=2,
            )
            label = Gtk.Label(label=item.text, xalign=0, hexpand=True)
            box.append(label)
            if item.detail:
                detail = Gtk.Label(label=item.detail)
                detail.add_css_class("dim-label")
                detail.add_css_class("caption")
                box.append(detail)
            self._list.append(box)
        self._list.select_row(self._list.get_row_at_index(0))

        cursor = self._buffer.get_iter_at_mark(self._buffer.get_insert())
        location = self._view.get_iter_location(self._word_start(cursor))
        x, y = self._view.buffer_to_window_coords(
            Gtk.TextWindowType.WIDGET, location.x, location.y
        )
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = x, y, 1, location.height
        self._popover.set_pointing_to(rect)
        self._popover.popup()

    def _hide(self) -> None:
        self._popover.popdown()

    # Keys / acceptance

    def _on_key_pressed(self, _controller, keyval, _keycode, state) -> bool:
        if keyval == Gdk.KEY_space and state & Gdk.ModifierType.CONTROL_MASK:
            self._refresh(min_word=1)
            return True
        if not self._popover.get_visible():
            return False
        if keyval == Gdk.KEY_Escape:
            self._hide()
            return True
        if keyval == Gdk.KEY_Down:
            self._move(1)
            return True
        if keyval == Gdk.KEY_Up:
            self._move(-1)
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_Tab):
            if state & Gdk.ModifierType.CONTROL_MASK:
                self._hide()  # Ctrl+Enter: let the console run the query
                return False
            self._accept()
            return True
        if keyval in (Gdk.KEY_Left, Gdk.KEY_Right):
            self._hide()
        return False

    def _move(self, delta: int) -> None:
        row = self._list.get_selected_row()
        index = row.get_index() if row is not None else 0
        index = max(0, min(len(self._items) - 1, index + delta))
        self._list.select_row(self._list.get_row_at_index(index))

    def _on_row_activated(self, _list, row) -> None:
        self._list.select_row(row)
        self._accept()

    def _accept(self) -> None:
        row = self._list.get_selected_row()
        if row is None:
            return
        completion = self._items[row.get_index()]
        cursor = self._buffer.get_iter_at_mark(self._buffer.get_insert())
        word_start = self._word_start(cursor)
        self._inserting = True
        try:
            self._buffer.begin_user_action()
            self._buffer.delete(word_start, cursor)
            self._buffer.insert(word_start, completion.text)
            self._buffer.end_user_action()
        finally:
            self._inserting = False
        self._hide()
