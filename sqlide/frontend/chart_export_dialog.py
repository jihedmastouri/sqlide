"""The "Export Chart" dialog: this picture, as a file or on the
clipboard (CORE-34).

The shape follows `frontend/export_dialog.py`, the precedent CORE-36
set for anything leaving the app: the questions in the order they
matter, a `Gtk.FileDialog` for the destination, the write on a worker
thread through `run_async`, and a status line that names the path and
the reason when it fails. The questions here are fewer — format, size,
theme — because the rows were already chosen upstream: the picture is
whatever the Chart view is showing.

Two answers are decided rather than asked:

- the size starts at the on-screen canvas, so **Export** with no
  fiddling gives back the chart that is on screen;
- the theme starts on **light**, because a dark-background PNG pasted
  into a white document is the usual complaint. The switch is there
  for the person who wants the dark one.

**Copy** is the same render with the same size and theme, put on the
clipboard as a PNG `Gdk.Texture` so it pastes into another app rather
than arriving as a file path.

Nothing here draws: every pixel comes from `frontend/chart_image.py`,
which is the same `chart_canvas.render()` call the `Gtk.DrawingArea`
makes.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from gi.repository import Adw, Gdk, GLib, Gtk

from sqlide.backend import charts
from sqlide.frontend import chart_image
from sqlide.frontend.util import run_async
from sqlide.i18n import _

# (label, Format).
FORMATS = (
    ("PNG", chart_image.Format.PNG),
    ("SVG", chart_image.Format.SVG),
)

#: PNG scale factors offered. 2x is the one slides want.
SCALES = ("1×", "1.5×", "2×", "3×")
SCALE_VALUES = (1.0, 1.5, 2.0, 3.0)

#: What the size fields fall back to when the canvas has no allocation
#: yet — a chart exported from a tab that was never shown.
DEFAULT_WIDTH = 960
DEFAULT_HEIGHT = 540


def copy_chart(
    widget: Gtk.Widget,
    spec: charts.ChartSpec,
    data: charts.ChartData,
    width: float,
    height: float,
    *,
    scale: float = 1.0,
    dark: bool = False,
) -> bool:
    """Put the chart on the clipboard as an image.

    A `Gdk.Texture` rather than text, so the paste target is another
    application's document and not a file name. Returns False when the
    display refuses it, which is the only thing a caller can react to.
    """
    display = widget.get_display() if widget is not None else None
    if display is None:
        return False
    try:
        payload = chart_image.png_bytes(
            spec, data, width, height, scale=scale, dark=dark
        )
        texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(payload))
        # A content provider rather than `set_texture`, which is a C
        # convenience the bindings do not carry.
        provider = Gdk.ContentProvider.new_for_value(texture)
    except Exception:
        # A display with no image clipboard, or a size cairo refused:
        # the caller says so in a line of status, never a traceback.
        return False
    display.get_clipboard().set_content(provider)
    return True


class ChartExportDialog(Adw.Dialog):
    """Format, size, theme, destination — and the write on a thread."""

    def __init__(
        self,
        source: Callable[[], tuple[charts.ChartSpec, charts.ChartData]],
        width: float = DEFAULT_WIDTH,
        height: float = DEFAULT_HEIGHT,
        *,
        dark: bool = False,
        suggested_name: str = "chart",
    ) -> None:
        super().__init__(
            title=_("Export Chart"), content_width=480, content_height=560
        )
        self._source = source
        # The current theme, offered but not chosen: see the module
        # docstring.
        self._current_dark = bool(dark)
        self._suggested = suggested_name or "chart"
        self._path: Path | None = None
        self._running = False

        start_width, start_height = chart_image.clamp_size(
            width or DEFAULT_WIDTH, height or DEFAULT_HEIGHT
        )

        page = Adw.PreferencesPage()

        picture = Adw.PreferencesGroup(title=_("Picture"))
        self._format = Adw.ComboRow(
            title=_("Format"),
            subtitle=_("SVG keeps the text and the marks as vectors"),
            model=Gtk.StringList.new([label for label, _f in FORMATS]),
        )
        self._format.connect("notify::selected", self._on_format_changed)
        picture.add(self._format)

        self._width = Adw.SpinRow.new_with_range(
            chart_image.MIN_WIDTH, chart_image.MAX_SIZE, 10
        )
        self._width.set_title(_("Width"))
        self._width.set_value(start_width)
        picture.add(self._width)

        self._height = Adw.SpinRow.new_with_range(
            chart_image.MIN_HEIGHT, chart_image.MAX_SIZE, 10
        )
        self._height.set_title(_("Height"))
        self._height.set_value(start_height)
        picture.add(self._height)

        self._scale = Adw.ComboRow(
            title=_("Scale"),
            subtitle=_("A 2× image for a slide or a high-density screen"),
            model=Gtk.StringList.new(list(SCALES)),
        )
        picture.add(self._scale)

        self._dark = Adw.SwitchRow(
            title=_("Use the current theme"),
            subtitle=_(
                "Off, the picture is drawn light, so it reads on a white page"
            ),
            active=False,
        )
        picture.add(self._dark)
        page.add(picture)

        destination = Adw.PreferencesGroup(title=_("Destination"))
        self._file_row = Adw.ActionRow(
            title=_("File"), subtitle=_("No file chosen")
        )
        choose = Gtk.Button(label=_("Choose…"), valign=Gtk.Align.CENTER)
        choose.connect("clicked", self._on_choose_file)
        self._file_row.add_suffix(choose)
        self._file_row.set_activatable_widget(choose)
        destination.add(self._file_row)
        page.add(destination)

        header = Adw.HeaderBar()
        self._copy_button = Gtk.Button(label=_("Copy"))
        self._copy_button.connect("clicked", self._on_copy_clicked)
        header.pack_start(self._copy_button)
        self._export_button = Gtk.Button(label=_("Export"))
        self._export_button.add_css_class("suggested-action")
        self._export_button.connect("clicked", self._on_export_clicked)
        header.pack_end(self._export_button)

        self._status = Gtk.Label(
            xalign=0,
            margin_start=12,
            margin_end=12,
            margin_bottom=8,
            wrap=True,
            visible=False,
        )
        self._status.add_css_class("dim-label")

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.append(page)
        body.append(self._status)
        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(body)
        self.set_child(view)

        self._on_format_changed()

    # Choices

    def _format_choice(self) -> chart_image.Format:
        return FORMATS[self._format.get_selected()][1]

    def _scale_choice(self) -> float:
        index = self._scale.get_selected()
        if 0 <= index < len(SCALE_VALUES):
            return SCALE_VALUES[index]
        return 1.0

    def _dark_choice(self) -> bool:
        return self._current_dark if self._dark.get_active() else False

    def _size(self) -> tuple[int, int]:
        return chart_image.clamp_size(
            self._width.get_value(), self._height.get_value()
        )

    def _on_format_changed(self, *_args) -> None:
        fmt = self._format_choice()
        # An SVG has no pixels to multiply: it is already resolution
        # independent, so the scale row would be a lie.
        self._scale.set_visible(fmt is chart_image.Format.PNG)
        if self._path is not None:
            self._path = self._path.with_suffix(fmt.suffix)
            self._file_row.set_subtitle(str(self._path))

    # Destination

    def _on_choose_file(self, *_args) -> None:
        fmt = self._format_choice()
        dialog = Gtk.FileDialog(
            title=_("Export Chart"),
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

    # Doing it

    def _say(self, text: str, error: bool = False) -> None:
        self._status.set_visible(bool(text))
        self._status.set_text(text)
        if error:
            self._status.add_css_class("error")
        else:
            self._status.remove_css_class("error")

    def _on_copy_clicked(self, *_args) -> None:
        spec, data = self._source()
        width, height = self._size()
        if copy_chart(
            self,
            spec,
            data,
            width,
            height,
            scale=self._scale_choice(),
            dark=self._dark_choice(),
        ):
            self._say(_("Chart copied to the clipboard."))
        else:
            self._say(_("The chart could not be copied."), error=True)

    def _on_export_clicked(self, *_args) -> None:
        if self._running:
            return
        if self._path is None:
            self._say(_("Choose a file to export to."), error=True)
            return
        fmt = self._format_choice()
        spec, data = self._source()
        width, height = self._size()
        scale = self._scale_choice()
        dark = self._dark_choice()
        path = self._path
        self._running = True
        self._export_button.set_sensitive(False)
        self._say(_("Exporting…"))

        def work() -> Path:
            return chart_image.write_chart(
                path, fmt, spec, data, width, height, scale=scale, dark=dark
            )

        run_async(work, self._done, self._failed)

    def _done(self, path: Path) -> None:
        self._running = False
        self._export_button.set_sensitive(True)
        self._say(_("Chart exported to %s") % path)

    def _failed(self, exc: Exception) -> None:
        self._running = False
        self._export_button.set_sensitive(True)
        self._say(str(exc), error=True)
