"""The Export dialog (CORE-36): these rows, as a file.

Reachable from any grid — the cell menu's "Export…" — and from a table
tab's action bar. Three questions, in the order they matter: which
rows (the selection, what is loaded, or the whole query), in what
shape (CSV, JSON, INSERT, Markdown, with the CSV knobs that decide
whether another program can read the file), and where. A live preview
of the first lines sits under them, so the answer to "will this open
in my spreadsheet" is on screen before the file exists.

Nothing here formats anything: every byte comes from
backend/export.py, which is also what the clipboard uses. The dialog's
own job is the thread — the export runs through run_async with a
progress line and a Cancel, and a cancelled or failed run leaves no
file behind (backend.export.export_to_path renames a temporary into
place only when the last row is written).

The whole-query scope streams: it hands the writer the table tab's own
paging generator, so exporting a million rows never holds more than
one page, and it carries the tab's filters and sort so the file
matches what the grid is showing.
"""

from __future__ import annotations

import itertools
import threading
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

from gi.repository import Adw, GLib, Gtk

from sqlide.backend import export
from sqlide.frontend.util import run_async
from sqlide.i18n import _

# (label, Format). Ordered by how often people want them.
FORMATS = (
    ("CSV", export.Format.CSV),
    ("JSON", export.Format.JSON),
    ("SQL INSERT", export.Format.INSERT),
    ("Markdown", export.Format.MARKDOWN),
)

ENCODINGS = ("utf-8", "utf-8-sig", "utf-16", "latin-1", "cp1252")

# Lines of the file shown before it is written.
PREVIEW_LINES = 12

# A scope is (label, source) where source() gives (columns, rows). Rows
# may be a list or a generator: the generator is what makes the whole
# query streamable, so the source is called fresh for the preview and
# again for the export rather than being iterated twice.
Source = Callable[[], tuple[list[str], Iterable[Any]]]


class ExportDialog(Adw.Dialog):
    def __init__(
        self,
        scopes: list[tuple[str, Source]],
        options: export.Options | None = None,
        suggested_name: str = "export",
    ) -> None:
        super().__init__(
            title=_("Export Rows"), content_width=560, content_height=640
        )
        self._scopes = scopes
        self._base = options or export.Options()
        self._suggested = suggested_name or "export"
        self._path: Path | None = None
        self._cancel = threading.Event()
        self._running = False

        page = Adw.PreferencesPage()
        rows = Adw.PreferencesGroup(title=_("Rows"))
        self._scope = Adw.ComboRow(
            title=_("Export"),
            model=Gtk.StringList.new([label for label, _src in scopes]),
        )
        self._scope.connect("notify::selected", self._refresh_preview)
        rows.add(self._scope)
        self._format = Adw.ComboRow(
            title=_("Format"),
            model=Gtk.StringList.new([label for label, _f in FORMATS]),
        )
        self._format.connect("notify::selected", self._on_format_changed)
        rows.add(self._format)
        page.add(rows)

        self._csv_group = Adw.PreferencesGroup(title=_("CSV"))
        self._delimiter = Adw.EntryRow(title=_("Delimiter"))
        self._delimiter.set_text(self._base.delimiter)
        self._delimiter.connect("changed", self._refresh_preview)
        self._csv_group.add(self._delimiter)
        self._null = Adw.EntryRow(title=_("NULL is written as"))
        self._null.set_text(self._base.null_text)
        self._null.connect("changed", self._refresh_preview)
        self._csv_group.add(self._null)
        self._header = Adw.SwitchRow(
            title=_("Header row"), active=self._base.header
        )
        self._header.connect("notify::active", self._refresh_preview)
        self._csv_group.add(self._header)
        self._quote_all = Adw.SwitchRow(
            title=_("Quote every field"), active=self._base.quote_all
        )
        self._quote_all.connect("notify::active", self._refresh_preview)
        self._csv_group.add(self._quote_all)
        page.add(self._csv_group)

        destination = Adw.PreferencesGroup(title=_("Destination"))
        self._encoding = Adw.ComboRow(
            title=_("Encoding"),
            subtitle=_("Recorded here so nothing is lost silently"),
            model=Gtk.StringList.new(list(ENCODINGS)),
        )
        destination.add(self._encoding)
        self._file_row = Adw.ActionRow(
            title=_("File"), subtitle=_("No file chosen")
        )
        choose = Gtk.Button(label=_("Choose…"), valign=Gtk.Align.CENTER)
        choose.connect("clicked", self._on_choose_file)
        self._file_row.add_suffix(choose)
        self._file_row.set_activatable_widget(choose)
        destination.add(self._file_row)
        page.add(destination)

        preview_group = Adw.PreferencesGroup(title=_("Preview"))
        self._preview = Gtk.TextView(
            editable=False,
            monospace=True,
            cursor_visible=False,
            left_margin=8,
            right_margin=8,
            top_margin=8,
            bottom_margin=8,
        )
        frame = Gtk.Frame(
            child=Gtk.ScrolledWindow(
                child=self._preview, height_request=160, hexpand=True
            )
        )
        preview_group.add(frame)
        page.add(preview_group)

        header = Adw.HeaderBar()
        self._export_button = Gtk.Button(label=_("Export"))
        self._export_button.add_css_class("suggested-action")
        self._export_button.connect("clicked", self._on_export_clicked)
        header.pack_end(self._export_button)

        self._status = Gtk.Label(
            xalign=0, margin_start=12, margin_end=12, margin_bottom=8,
            wrap=True, visible=False,
        )
        self._status.add_css_class("dim-label")
        self._cancel_button = Gtk.Button(
            label=_("Cancel export"),
            halign=Gtk.Align.START,
            margin_start=12,
            margin_bottom=8,
            visible=False,
        )
        self._cancel_button.connect(
            "clicked", lambda *_a: self._on_cancel_clicked()
        )

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.append(page)
        body.append(self._status)
        body.append(self._cancel_button)
        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(body)
        self.set_child(view)

        self._on_format_changed()

    # Construction from a grid

    @classmethod
    def for_grid(cls, grid, whole: tuple[str, Source] | None = None):
        """The dialog for one ResultGrid.

        Every grid can offer its selection and the rows it holds; only
        an owner that knows how to re-run the query can add a whole
        scope, which is why that one is passed in rather than guessed.
        """
        scopes: list[tuple[str, Source]] = []
        selection = grid.selection_rows()
        if selection is not None:
            scopes.append((_("Selection"), lambda: selection))
        scopes.append((_("Loaded rows"), grid.loaded_rows))
        if whole is not None:
            scopes.append(whole)
        name = (grid.table_name or "export").replace(".", "_")
        return cls(scopes, grid.export_options(), name)

    # Options

    def _format_choice(self) -> export.Format:
        return FORMATS[self._format.get_selected()][1]

    def _options(self) -> export.Options:
        delimiter = self._delimiter.get_text() or ","
        encodings = self._encoding.get_model()
        encoding = encodings.get_string(self._encoding.get_selected())
        return export.Options(
            delimiter=delimiter[0],
            quote_all=self._quote_all.get_active(),
            header=self._header.get_active(),
            null_text=self._null.get_text(),
            encoding=encoding,
            table_name=self._base.table_name,
        )

    def _on_format_changed(self, *_args) -> None:
        fmt = self._format_choice()
        self._csv_group.set_visible(fmt is export.Format.CSV)
        if self._path is not None:
            self._path = self._path.with_suffix(fmt.suffix)
            self._file_row.set_subtitle(str(self._path))
        self._refresh_preview()

    # Preview

    def _refresh_preview(self, *_args) -> None:
        """The first lines of the file, built off the main loop.

        A whole-query scope reads the database to preview itself, so
        this goes through run_async like every other read; only the
        first rows are ever pulled, so the preview never reads a table.
        """
        fmt = self._format_choice()
        options = self._options()
        source = self._scopes[self._scope.get_selected()][1]

        def work() -> str:
            columns, rows = source()
            chunks = export.iter_chunks(
                fmt,
                columns,
                itertools.islice(iter(rows), PREVIEW_LINES),
                options,
            )
            return "".join(itertools.islice(chunks, PREVIEW_LINES * 4))

        run_async(
            work,
            lambda text: self._preview.get_buffer().set_text(text),
            # A dead connection is a line in the preview, not a crash.
            lambda exc: self._preview.get_buffer().set_text(str(exc)),
        )

    # Destination

    def _on_choose_file(self, *_args) -> None:
        fmt = self._format_choice()
        dialog = Gtk.FileDialog(
            title=_("Export Rows"),
            initial_name=f"{self._suggested}{fmt.suffix}",
        )

        def picked(chooser: Gtk.FileDialog, result) -> None:
            try:
                file = chooser.save_finish(result)
            except GLib.Error:
                return  # cancelled
            self._path = Path(file.get_path())
            self._file_row.set_subtitle(str(self._path))

        dialog.save(self._root_window(), None, picked)

    def _root_window(self):
        root = self.get_root()
        return root if isinstance(root, Gtk.Window) else None

    # Running it

    def _say(self, text: str, error: bool = False) -> None:
        self._status.set_visible(bool(text))
        self._status.set_text(text)
        if error:
            self._status.add_css_class("error")
        else:
            self._status.remove_css_class("error")

    def _on_cancel_clicked(self) -> None:
        self._cancel.set()
        self._say(_("Cancelling…"))

    def _on_export_clicked(self, *_args) -> None:
        if self._running:
            return
        if self._path is None:
            self._say(_("Choose a file to export to."), error=True)
            return
        fmt = self._format_choice()
        options = self._options()
        source = self._scopes[self._scope.get_selected()][1]
        path = self._path
        self._cancel = threading.Event()
        self._running = True
        self._export_button.set_sensitive(False)
        self._cancel_button.set_visible(True)
        self._say(_("Exporting…"))

        def progress(count: int) -> None:
            if count % 500:
                return
            GLib.idle_add(
                lambda: (self._say(_("Exported %d rows…") % count), False)[1]
            )

        def work() -> int:
            columns, rows = source()
            return export.export_to_path(
                path,
                fmt,
                columns,
                rows,
                options,
                on_row=progress,
                cancelled=self._cancel.is_set,
            )

        run_async(work, self._done, self._failed)

    def _done(self, count: int) -> None:
        self._running = False
        self._cancel_button.set_visible(False)
        self._export_button.set_sensitive(True)
        self._say(_("Exported %(rows)d rows to %(path)s") % {
            "rows": count, "path": self._path
        })

    def _failed(self, exc: Exception) -> None:
        self._running = False
        self._cancel_button.set_visible(False)
        self._export_button.set_sensitive(True)
        if isinstance(exc, export.ExportCancelled):
            self._say(_("Export cancelled; no file was written."))
            return
        self._say(str(exc), error=True)


def page_source(
    connector_for: Callable[[], Any],
    table: str,
    columns: Callable[[], list[str]],
    filters=None,
    order_by=None,
    page_size: int = 500,
) -> Source:
    """A whole-query source: the table re-read page by page.

    `connector_for` is called on the worker thread, so the connection
    is opened (or reused) off the main loop like every other read.
    """

    def source() -> tuple[list[str], Iterator[Any]]:
        connector = connector_for()
        rows = export.iter_pages(
            connector,
            table,
            filters=filters,
            order_by=order_by,
            page_size=page_size,
        )
        return columns(), rows

    return source
