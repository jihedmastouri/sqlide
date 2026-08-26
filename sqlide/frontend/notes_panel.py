"""The Notes page of the right side panel, and its editor dialog.

Notes are free-form Markdown attached to a connection, a table, or
nothing in particular; backend/notes.py owns them and the file they
live in (``notes.toml`` in the config directory, so they can be
committed to git — see docs/configuration.md).

The page is a filter bar over a list: the scope filter (All / This
connection / This table, the last two disabled when the active tab has
no such object) and a text filter over title and body. Each row shows
the title, the scope badge and when it was last changed, with edit and
delete buttons; delete asks first.

**Markdown, not rich text.** The body is plain text and the editor's
toolbar only inserts markers — heading, bold, italic, bullet and
numbered lists, code block — so a note diffs and merges in git like the
rest of the config, and stays readable in the file.

A note whose connection is no longer in the workspace keeps its place
in the list and is marked "orphaned" instead of being hidden or
deleted; the window tells the page which connections exist
(set_target).
"""

from __future__ import annotations

from typing import Callable

from gi.repository import Adw, Gtk, Pango

from sqlide.backend import notes as notes_backend
from sqlide.backend.notes import Note, NotesStore
from sqlide.frontend.util import describe

# The scope filter, in the order the drop-down shows it.
_FILTERS = (
    (notes_backend.GLOBAL, "All"),
    (notes_backend.CONNECTION, "This connection"),
    (notes_backend.TABLE, "This table"),
)


def _when(stamp: str) -> str:
    """"2026-08-26 14:03" out of an ISO timestamp, kept as-is if it is
    something a hand edit put there."""
    return stamp.replace("T", " ")[:16] or "never"


class NoteDialog(Adw.Dialog):
    """Add or edit one note: title, scope, and a Markdown body with a
    marker-inserting toolbar."""

    def __init__(
        self,
        note: Note | None,
        connection: str,
        table: str,
        on_save: Callable[[str, str, str, str, str], None],
    ) -> None:
        super().__init__(
            title="Edit Note" if note is not None else "New Note",
            content_width=640,
            content_height=560,
        )
        self._on_save = on_save

        # Scope choices: general always, the connection and the table
        # when there is one — either the active tab's, or (editing) the
        # note's own, so an orphaned note keeps its target on save.
        self._connection = note.connection if note is not None else ""
        self._table = note.table if note is not None else ""
        if not self._connection:
            self._connection = connection
        if not self._table:
            self._table = table
        self._choices: list[tuple[str, str]] = [
            (notes_backend.GLOBAL, "General (no object)")
        ]
        if self._connection:
            self._choices.append(
                (notes_backend.CONNECTION, f"Connection: {self._connection}")
            )
        if self._connection and self._table:
            self._choices.append(
                (notes_backend.TABLE, f"Table: {self._table}")
            )

        self._title = Adw.EntryRow(title="Title")
        if note is not None:
            self._title.set_text(note.title)

        self._scope = Adw.ComboRow(
            title="Scope",
            subtitle="What this note is about",
            model=Gtk.StringList.new([label for _scope, label in self._choices]),
        )
        # A new note defaults to the selected object; an edited one
        # keeps its own scope.
        wanted = (
            note.scope
            if note is not None
            else (
                notes_backend.TABLE
                if self._table and self._connection
                else notes_backend.CONNECTION
                if self._connection
                else notes_backend.GLOBAL
            )
        )
        for index, (scope, _label) in enumerate(self._choices):
            if scope == wanted:
                self._scope.set_selected(index)

        group = Adw.PreferencesGroup()
        group.add(self._title)
        group.add(self._scope)

        self._buffer = Gtk.TextBuffer()
        if note is not None:
            self._buffer.set_text(note.body)
        body = Gtk.TextView(
            buffer=self._buffer,
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
            monospace=True,
            top_margin=8,
            bottom_margin=8,
            left_margin=8,
            right_margin=8,
        )
        scroller = Gtk.ScrolledWindow(child=body, vexpand=True, hexpand=True)
        scroller.add_css_class("card")

        toolbar = Gtk.Box(spacing=6, margin_top=6, margin_bottom=6)
        for icon, label, before, after, line in (
            ("font-x-generic-symbolic", "Heading", "## ", "", True),
            ("format-text-bold-symbolic", "Bold", "**", "**", False),
            ("format-text-italic-symbolic", "Italic", "*", "*", False),
            ("view-list-symbolic", "Bullet list", "- ", "", True),
            ("view-list-ordered-symbolic", "Numbered list", "1. ", "", True),
            ("utilities-terminal-symbolic", "Code block", "```\n", "\n```", False),
        ):
            button = Gtk.Button(icon_name=icon)
            button.add_css_class("flat")
            describe(button, label)
            button.connect(
                "clicked",
                lambda _b, before=before, after=after, line=line: self._insert(
                    before, after, line
                ),
            )
            toolbar.append(button)
        hint = Gtk.Label(label="Markdown", xalign=1, hexpand=True)
        hint.add_css_class("dim-label")
        toolbar.append(hint)

        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=6,
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        content.append(group)
        content.append(toolbar)
        content.append(scroller)

        header = Adw.HeaderBar()
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)
        save = Gtk.Button(label="Save")
        save.add_css_class("suggested-action")
        save.connect("clicked", self._save)
        header.pack_end(save)

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(content)
        self.set_child(view)

    def _insert(self, before: str, after: str, line: bool) -> None:
        """Wrap the selection (or the cursor) in a Markdown marker. A
        line marker — heading, list item — goes at the start of the
        line instead."""
        buffer = self._buffer
        bounds = buffer.get_selection_bounds()
        if line:
            start = buffer.get_iter_at_mark(buffer.get_insert())
            start.set_line_offset(0)
            buffer.insert(start, before)
            return
        if bounds:
            start, end = bounds
            text = buffer.get_text(start, end, False)
            buffer.delete(start, end)
            buffer.insert(start, f"{before}{text}{after}")
            return
        cursor = buffer.get_iter_at_mark(buffer.get_insert())
        buffer.insert(cursor, before + after)
        cursor = buffer.get_iter_at_mark(buffer.get_insert())
        cursor.backward_chars(len(after))
        buffer.place_cursor(cursor)

    def _save(self, *_args) -> None:
        scope = self._choices[self._scope.get_selected()][0]
        body = self._buffer.get_text(
            self._buffer.get_start_iter(), self._buffer.get_end_iter(), False
        )
        self._on_save(
            self._title.get_text(),
            body,
            scope,
            self._connection if scope != notes_backend.GLOBAL else "",
            self._table if scope == notes_backend.TABLE else "",
        )
        self.close()


class NotesPage(Gtk.Box):
    """The side panel's Notes page. Follows the store live (so a note
    written in another window, or by hand in notes.toml, appears) and
    unsubscribes when destroyed."""

    def __init__(self, store: NotesStore | None = None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._store = store or notes_backend.store
        self._connection = ""
        self._table = ""
        self._connections: list[str] | None = None
        self._shown: list[Note] = []

        controls = Gtk.Box(
            spacing=6,
            margin_top=6,
            margin_bottom=6,
            margin_start=6,
            margin_end=6,
        )
        self._scope = Gtk.DropDown.new_from_strings(
            [label for _scope, label in _FILTERS]
        )
        describe(self._scope, "Which notes to list")
        self._scope.connect("notify::selected", lambda *_: self._rebuild())
        controls.append(self._scope)
        add = Gtk.Button(icon_name="list-add-symbolic", hexpand=True, halign=Gtk.Align.END)
        add.add_css_class("flat")
        describe(add, "Add a note")
        add.connect("clicked", lambda *_: self._add())
        controls.append(add)
        self.append(controls)

        self._search = Gtk.SearchEntry(
            placeholder_text="Filter title and body",
            margin_bottom=6,
            margin_start=6,
            margin_end=6,
        )
        self._search.connect("search-changed", lambda *_: self._rebuild())
        self.append(self._search)

        self._list = Gtk.ListBox()
        self._list.set_selection_mode(Gtk.SelectionMode.NONE)
        self._list.add_css_class("navigation-sidebar")
        self._list.connect("row-activated", self._row_activated)
        placeholder = Gtk.Label(
            label="No notes yet — the + button writes one",
            margin_top=24,
            wrap=True,
        )
        placeholder.add_css_class("dim-label")
        self._list.set_placeholder(placeholder)
        scroller = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        scroller.set_child(self._list)
        self.append(scroller)

        self._store.load()
        self._store.subscribe(self._store_changed)
        self.connect(
            "destroy", lambda *_: self._store.unsubscribe(self._store_changed)
        )
        self._rebuild()

    # The window's context

    def set_target(
        self,
        connection: str,
        table: str,
        connections: list[str] | None = None,
    ) -> None:
        """The object a new note defaults to, and the connections that
        exist (for the orphan badge). Called on every tab change."""
        self._connection = connection
        self._table = table
        self._connections = connections
        self._rebuild()

    # The list

    def _store_changed(self, _notes: list[Note]) -> None:
        self._rebuild()

    def _selected_filter(self) -> str:
        return _FILTERS[self._scope.get_selected()][0]

    def _rebuild(self) -> None:
        # A scope with no object behind it filters to nothing, so offer
        # it only when the active tab has one.
        scope = self._selected_filter()
        if scope == notes_backend.CONNECTION and not self._connection:
            scope = notes_backend.GLOBAL
        elif scope == notes_backend.TABLE and not self._table:
            scope = notes_backend.GLOBAL
        self._shown = self._store.filter(
            scope,
            self._connection,
            self._table,
            self._search.get_text(),
        )
        while (row := self._list.get_row_at_index(0)) is not None:
            self._list.remove(row)
        for note in self._shown:
            self._list.append(self._row(note))

    def _row(self, note: Note) -> Adw.ActionRow:
        row = Adw.ActionRow(activatable=True)
        row.set_use_markup(False)
        row.set_title(note.title)
        row.set_title_lines(1)
        row.set_subtitle(f"{note.scope_label} · {_when(note.updated)}")
        row.set_subtitle_lines(1)
        row.set_tooltip_text(note.body)
        if note.is_orphaned(self._connections):
            badge = Gtk.Label(label="orphaned")
            badge.add_css_class("dim-label")
            badge.add_css_class("caption")
            badge.set_ellipsize(Pango.EllipsizeMode.END)
            badge.set_tooltip_text(
                f"{note.scope_label} is not in this workspace any more — "
                "the note is kept"
            )
            row.add_suffix(badge)
        delete = Gtk.Button(icon_name="user-trash-symbolic")
        delete.add_css_class("flat")
        delete.set_valign(Gtk.Align.CENTER)
        describe(delete, "Delete")
        delete.connect("clicked", lambda _b, n=note: self._confirm_delete(n))
        row.add_suffix(delete)
        return row

    def _row_activated(self, _list, row) -> None:
        self._edit(self._shown[row.get_index()])

    # Add / edit / delete

    def _add(self) -> None:
        NoteDialog(
            None,
            self._connection,
            self._table,
            on_save=lambda title, body, scope, connection, table: (
                self._store.add(title, body, scope, connection, table)
            ),
        ).present(self)

    def _edit(self, note: Note) -> None:
        def save(title, body, scope, connection, table) -> None:
            self._store.update(
                note,
                title=title,
                body=body,
                scope=scope,
                connection=connection,
                table=table,
            )

        NoteDialog(note, self._connection, self._table, on_save=save).present(
            self
        )

    def _confirm_delete(self, note: Note) -> None:
        dialog = Adw.AlertDialog(
            heading="Delete note?",
            body=f"“{note.title}” will be removed from notes.toml.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance(
            "delete", Adw.ResponseAppearance.DESTRUCTIVE
        )
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect(
            "response",
            lambda _d, response: (
                self._store.remove(note) if response == "delete" else None
            ),
        )
        dialog.present(self)
