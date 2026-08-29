"""The Chart view over a result (CORE-32).

Widget tests, so they skip without a display. They stay away from the
pixels — those are `tests/test_chart_canvas.py`'s job — and assert the
things the ticket is actually about: that the chart arrives inferred,
that every mapping control changes the drawing, that a mapping which
cannot be drawn explains itself instead of blanking, that selection
goes both ways, and that a partial result says so.
"""

from __future__ import annotations

import pytest

from sqlide.backend import charts


@pytest.fixture()
def gtk():
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gtk

    if not Gtk.init_check():
        pytest.skip("no display for GTK")
    return Gtk


COLUMNS = ["city", "kind", "sales"]
ROWS = [
    ("Paris", "web", 3),
    ("Paris", "shop", 4),
    ("Lyon", "web", 5),
    ("Lyon", "shop", 6),
]


def view(gtk, **kwargs):
    from sqlide.frontend.chart_view import ChartView

    return ChartView(**kwargs)


def notice(chart) -> str:
    return chart._notice.get_text()


def test_a_group_by_result_charts_itself_with_no_configuration(gtk):
    chart = view(gtk)
    chart.set_result(["city", "sales"], [("Paris", 3), ("Lyon", 5)])
    assert chart.spec is not None
    assert chart.spec.x == "city"
    assert chart.spec.series == ("sales",)
    assert chart.spec.type == "bar"
    assert chart._data.series


def test_a_result_with_no_numeric_column_says_so(gtk):
    chart = view(gtk)
    chart.set_result(["city", "kind"], [("Paris", "web")])
    assert chart.spec is None
    assert "numeric" in notice(chart).lower()


def test_an_empty_result_says_so_rather_than_drawing_nothing(gtk):
    chart = view(gtk)
    chart.set_result(["city", "sales"], [])
    assert "no rows" in notice(chart).lower()


def test_every_mapping_control_changes_the_drawing(gtk):
    chart = view(gtk)
    chart.set_result(COLUMNS, ROWS)
    before = chart._data.series

    types = list(charts.CHART_TYPES)
    chart._type.set_selected(types.index("line"))
    assert chart.spec.type == "line"

    chart._split.set_selected(1 + COLUMNS.index("kind"))
    assert chart.spec.split == "kind"
    assert len(chart._data.series) > 1

    chart._aggregation.set_selected(list(charts.AGGREGATIONS).index("max"))
    assert chart.spec.aggregation == "max"

    chart._x.set_selected(0)
    assert chart.spec.x == ""

    for name, check in chart._series_checks:
        check.set_active(name == "sales")
    assert chart.spec.series == ("sales",)
    assert chart._data.series != before


def test_a_mapping_that_cannot_be_drawn_explains_itself(gtk):
    chart = view(gtk)
    chart.set_result(COLUMNS, ROWS)
    for _name, check in chart._series_checks:
        check.set_active(False)
    assert chart.spec.series == ()
    assert notice(chart)
    assert chart._data.reason


def test_the_mapping_survives_a_reload_of_the_same_columns(gtk):
    chart = view(gtk)
    chart.set_result(COLUMNS, ROWS)
    chart._type.set_selected(list(charts.CHART_TYPES).index("scatter"))
    spec = chart.spec
    chart.set_result(COLUMNS, ROWS + [("Nice", "web", 7)])
    assert chart.spec == spec


def test_a_spec_that_no_longer_fits_is_replaced_not_raised(gtk):
    chart = view(gtk)
    chart.set_result(COLUMNS, ROWS)
    chart.set_result(["a", "b"], [(1, 2)])
    assert chart.spec is None or all(
        name in ("a", "b") for name in chart.spec.columns()
    )


def test_set_spec_reports_a_mapping_the_result_has_no_column_for(gtk):
    chart = view(gtk)
    chart.set_result(COLUMNS, ROWS)
    chart.set_spec(charts.ChartSpec(type="bar", x="region", series=("sales",)))
    assert "region" in notice(chart)
    assert chart.spec is None or chart.spec.x != "region"


def test_selecting_a_row_highlights_its_mark(gtk):
    chart = view(gtk)
    chart.set_result(COLUMNS, ROWS)
    chart.select_row(0)
    assert chart._marks
    chart.select_row(None)
    assert not chart._marks


def test_a_row_the_chart_dropped_highlights_nothing(gtk):
    chart = view(gtk)
    chart.set_result(COLUMNS, [("Paris", "web", 3), (None, "web", 4)])
    chart.select_row(1)
    assert not chart._marks


def test_clicking_a_mark_selects_its_rows_in_the_grid(gtk):
    cairo = pytest.importorskip("cairo")
    from sqlide.frontend import chart_canvas

    picked: list[int] = []
    chart = view(gtk, on_select=picked.append)
    chart.set_result(COLUMNS, ROWS)
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 400, 300)
    chart._rendering = chart_canvas.render(
        cairo.Context(surface),
        chart.spec,
        chart._data,
        400,
        300,
    )
    px, py, series, index = chart._rendering._points[0] if (
        chart._rendering._points
    ) else (0, 0, 0, 0)
    if chart._rendering._rects:
        rect, series, index = chart._rendering._rects[0]
        px, py = rect.x + rect.width / 2, rect.y + rect.height / 2
    rows = chart.rows_at(px, py)
    assert rows
    chart._on_click(None, 1, px, py)
    assert picked and picked[0] in rows
    # And a click on nothing selects nothing.
    assert chart.rows_at(-10, -10) == []


def test_a_partially_loaded_result_says_so(gtk):
    chart = view(gtk)
    chart.set_result(COLUMNS, ROWS, more=True)
    assert "larger result" in notice(chart)


def test_load_all_is_offered_only_when_more_can_be_fetched(gtk):
    asked: list[int] = []
    chart = view(gtk, on_load_all=asked.append)
    chart.set_result(COLUMNS, ROWS)
    assert not chart._load_all.get_visible()
    chart.set_result(COLUMNS, ROWS, more=True)
    assert chart._load_all.get_visible()
    chart._on_load_all_clicked()
    assert asked and asked[0] > 0


def test_a_refusal_from_load_all_lands_in_the_notice_bar(gtk):
    chart = view(gtk, on_load_all=lambda _cap: None)
    chart.set_result(COLUMNS, ROWS)
    chart.report("This result has more than 50000 rows")
    assert "50000" in notice(chart)


def test_the_pane_switches_between_the_grid_and_the_chart(gtk):
    from sqlide.frontend.chart_view import ChartPane
    from sqlide.frontend.data_grid import ResultGrid

    grid = ResultGrid()
    pane = ChartPane(grid)
    grid.set_result(COLUMNS, ROWS)
    pane.set_result(COLUMNS, ROWS)
    assert pane._stack.get_visible_child_name() == "data"
    pane.show_chart()
    assert pane._stack.get_visible_child_name() == "chart"
    # Both children stay alive, which is what makes the trip back free.
    assert pane._stack.get_child_by_name("data") is grid
    # And the grid's selection reaches the chart.
    pane._on_grid_row_selected(0)
    assert pane.chart._marks


def test_the_view_holds_no_engine_specific_code():
    from sqlide.frontend import chart_view

    source = open(chart_view.__file__).read()
    for banned in ("postgres", "mysql", "sqlite", "psycopg", "pymysql"):
        assert banned not in source.lower()


# The picture's way out (CORE-34)


def test_the_view_exports_the_chart_it_is_showing(gtk, tmp_path):
    """`chart_source()` is what the export renders, so a file is the
    picture on screen by construction rather than by coincidence."""
    from sqlide.frontend import chart_image

    chart = view(gtk)
    chart.set_result(COLUMNS, ROWS)
    spec, data = chart.chart_source()
    assert spec is chart.spec
    assert data.series

    target = tmp_path / "chart.png"
    chart_image.write_chart(
        target, chart_image.Format.PNG, spec, data, 640, 400
    )
    assert target.read_bytes().startswith(b"\x89PNG")


def test_a_mapping_that_cannot_be_drawn_exports_its_notice(gtk):
    chart = view(gtk)
    chart.set_result(["name"], [("Paris",), ("Lyon",)])
    _spec, data = chart.chart_source()
    assert data.reason, "the sentence on screen is the one in the picture"


def test_the_export_starts_from_the_on_screen_size(gtk):
    from sqlide.frontend import chart_export_dialog

    chart = view(gtk)
    chart.set_result(COLUMNS, ROWS)
    # A canvas that was never allocated falls back rather than to zero.
    assert chart.canvas_size() == (
        chart_export_dialog.DEFAULT_WIDTH,
        chart_export_dialog.DEFAULT_HEIGHT,
    )


def test_the_export_dialog_defaults_to_light_at_the_size_it_was_given(gtk):
    from sqlide.frontend.chart_export_dialog import ChartExportDialog

    chart = view(gtk)
    chart.set_result(COLUMNS, ROWS)
    dialog = ChartExportDialog(chart.chart_source, 800, 500, dark=True)
    assert dialog._size() == (800, 500)
    # The current theme is offered, not chosen.
    assert dialog._dark.get_active() is False
    assert dialog._dark_choice() is False
    dialog._dark.set_active(True)
    assert dialog._dark_choice() is True


def test_the_dialog_offers_png_and_svg_and_hides_scale_for_svg(gtk):
    from sqlide.frontend import chart_image
    from sqlide.frontend.chart_export_dialog import ChartExportDialog

    chart = view(gtk)
    chart.set_result(COLUMNS, ROWS)
    dialog = ChartExportDialog(chart.chart_source, 640, 400)
    assert dialog._format_choice() is chart_image.Format.PNG
    assert dialog._scale.get_visible()
    dialog._format.set_selected(1)
    assert dialog._format_choice() is chart_image.Format.SVG
    # An SVG has no pixels to multiply.
    assert not dialog._scale.get_visible()


def test_export_without_a_destination_says_so_rather_than_writing(gtk):
    from sqlide.frontend.chart_export_dialog import ChartExportDialog

    chart = view(gtk)
    chart.set_result(COLUMNS, ROWS)
    dialog = ChartExportDialog(chart.chart_source, 640, 400)
    dialog._on_export_clicked()
    assert dialog._status.get_visible()
    assert "file" in dialog._status.get_text().lower()


def test_copy_puts_a_real_image_on_the_clipboard(gtk):
    """A `Gdk.Texture`, not a file name: what another application
    pastes is the picture. Decoding the PNG back into a texture of the
    right size is the checkable half of that; the paste itself is the
    display's business."""
    from gi.repository import Gdk, GLib

    from sqlide.frontend import chart_image

    chart = view(gtk)
    chart.set_result(COLUMNS, ROWS)
    spec, data = chart.chart_source()
    payload = chart_image.png_bytes(spec, data, 640, 400)
    texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(payload))
    assert (texture.get_width(), texture.get_height()) == (640, 400)

    assert chart.copy_image()
    assert "clipboard" in notice(chart).lower()
    formats = chart.get_display().get_clipboard().get_formats().to_string()
    assert "Texture" in formats, formats
