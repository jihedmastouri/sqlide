"""Reading a wide value, and a whole row, without leaving the tab
(CORE-42).

Two halves. The renderer (`describe_value`) is pure and asserted
without a display: it decides from the value alone — text, JSON,
binary — which is what keeps engine knowledge out of the frontend. The
widget half is asserted through a real ResultGrid, because the point
of the feature is that the value views and the grid agree: the same
read-only rules, the same pending edit, no second write path.
"""

from __future__ import annotations

import struct

import gi
import pytest

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from sqlide.frontend.value_view import (  # noqa: E402
    MAX_TEXT,
    CellValue,
    describe_value,
    hex_dump,
)


@pytest.fixture()
def gtk():
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gtk

    if not Gtk.init_check():
        pytest.skip("no display for GTK")
    return Gtk


def _point(x: float, y: float, srid: int = 4326) -> str:
    head = b"\x01" + struct.pack("<I", 1 | 0x20000000) + struct.pack("<i", srid)
    return (head + struct.pack("<dd", x, y)).hex()


class TestRenderer:
    def test_null_is_named_rather_than_shown_as_empty(self):
        render = describe_value(None)
        assert render.kind == "null"
        assert render.detail == "NULL"

    def test_json_is_pretty_printed_for_reading(self):
        render = describe_value('{"b": 1, "a": [2, 3]}')
        assert render.kind == "json"
        assert render.text.splitlines()[0] == "{"
        assert '"a": [' in render.text

    def test_json_keeps_the_original_text_to_edit(self):
        # The pretty form is for reading; a write starts from what is
        # stored, so a document that would not survive a reformat is
        # never reformatted behind the user's back.
        raw = '{"a":1}'
        render = describe_value(raw)
        assert render.raw == raw
        assert render.formattable

    def test_text_that_only_looks_like_json_stays_verbatim(self):
        render = describe_value("{not json at all}")
        assert render.kind == "text"
        assert render.text == "{not json at all}"

    def test_plain_text_counts_its_lines(self):
        render = describe_value("a\nb\nc")
        assert render.kind == "text"
        assert "3 lines" in render.detail

    def test_binary_shows_hex_ascii_and_the_byte_length(self):
        render = describe_value(b"\x00\x01hi")
        assert render.kind == "binary"
        assert "4 bytes" in render.detail
        assert "00 01 68 69" in render.text
        assert render.text.endswith("..hi")

    def test_a_geometry_carries_the_description_geo_produces(self):
        render = describe_value(bytes.fromhex(_point(2.35, 48.85)), geometry=True)
        assert "Point, SRID 4326, 1 point" in render.detail

    def test_a_huge_value_is_truncated_and_says_so(self):
        render = describe_value("x" * (MAX_TEXT + 500))
        assert render.truncated
        assert len(render.text) == MAX_TEXT
        assert str(MAX_TEXT + 500) in render.detail

    def test_hex_dump_lines_carry_their_offset(self):
        dump = hex_dump(bytes(range(20)))
        assert dump.splitlines()[1].startswith("00000010")


class TestGridHandsOverTheFocusedCell:
    def _grid(self, gtk, on_value=None, editable=False):
        from sqlide.frontend.data_grid import ResultGrid

        edits = []
        grid = ResultGrid(
            on_edit=lambda row, col, text: edits.append((col, text)),
            on_value=on_value,
        )
        grid.set_result(
            ["id", "doc"],
            [(1, '{"a": 1}'), (2, None)],
            editable=editable,
        )
        if editable:
            grid.set_unlocked(True)
        return grid, edits

    def test_focusing_a_cell_announces_it(self, gtk):
        seen = []
        grid, _edits = self._grid(gtk, on_value=lambda cell, live: seen.append(
            (cell, live)
        ))
        grid.focus_cell(0, 1)
        cell, live = seen[-1]
        assert live is True
        assert cell.column == "doc"
        assert cell.row == 1
        assert cell.value == '{"a": 1}'

    def test_a_locked_grid_offers_no_edit(self, gtk):
        grid, _edits = self._grid(gtk)
        cell = grid.cell_value(0, 1)
        assert cell.apply is None
        assert not cell.editable

    def test_an_unlocked_cell_edits_through_the_grids_own_path(self, gtk):
        grid, edits = self._grid(gtk, editable=True)
        cell = grid.cell_value(0, 1)
        assert cell.editable
        cell.apply('{"a": 2}')
        # The same on_edit an inline commit calls, so the edit lands in
        # the tab's pending list and its Save preview.
        assert edits == [(1, '{"a": 2}')]

    def test_set_null_from_the_panel_is_an_edit_too(self, gtk):
        grid, edits = self._grid(gtk, editable=True)
        grid.cell_value(0, 1).apply(None)
        assert edits == [(1, None)]

    def test_a_new_result_clears_the_page(self, gtk):
        seen = []
        grid, _edits = self._grid(gtk, on_value=lambda cell, live: seen.append(
            cell
        ))
        grid.focus_cell(0, 1)
        grid.set_result(["id"], [(9,)])
        assert seen[-1] is None


class TestRecordView:
    def _grid(self, gtk, editable=False):
        from sqlide.frontend.data_grid import ResultGrid

        edits = []
        grid = ResultGrid(
            on_edit=lambda row, col, text: edits.append((col, text))
        )
        grid.set_result(
            ["id", "name", "doc"],
            [(1, "ada", '{"a": 1}'), (2, "brian", None), (3, "carol", "x")],
            editable=editable,
        )
        if editable:
            grid.set_unlocked(True)
        return grid, edits

    def _record(self, grid):
        from sqlide.frontend.value_view import RecordView

        view = RecordView(grid)
        view.reset()
        return view

    def test_it_shows_every_column_of_the_row(self, gtk):
        grid, _edits = self._grid(gtk)
        view = self._record(grid)
        titles = []
        index = 0
        while (row := view._list.get_row_at_index(index)) is not None:
            titles.append(row.get_title())
            index += 1
        assert titles == ["id", "name", "doc"]

    def test_down_and_up_walk_the_loaded_rows(self, gtk):
        grid, _edits = self._grid(gtk)
        view = self._record(grid)
        view.step(1)
        assert "Row 2 of 3" in view._label.get_text()
        view.step(-1)
        assert "Row 1 of 3" in view._label.get_text()

    def test_it_stops_at_the_ends(self, gtk):
        grid, _edits = self._grid(gtk)
        view = self._record(grid)
        view.step(-1)
        assert "Row 1 of 3" in view._label.get_text()
        for _ in range(10):
            view.step(1)
        assert "Row 3 of 3" in view._label.get_text()

    def test_moving_a_row_moves_the_grids_focus(self, gtk):
        grid, _edits = self._grid(gtk)
        view = self._record(grid)
        view.step(1)
        assert grid.focused_row() == 1

    def test_a_locked_grid_gives_a_read_only_record(self, gtk):
        grid, _edits = self._grid(gtk)
        assert not grid.cell_editable(1, "ada")
        grid, _edits = self._grid(gtk, editable=True)
        assert grid.cell_editable(1, "ada")

    def test_editing_a_record_field_goes_through_the_grid(self, gtk):
        grid, edits = self._grid(gtk, editable=True)
        grid.edit_cell(0, 1, "ADA")
        assert edits == [(1, "ADA")]


class TestValuePage:
    def test_it_says_what_a_cell_holds(self, gtk):
        from sqlide.frontend.value_view import ValuePage

        page = ValuePage()
        page.set_value(CellValue(column="doc", row=4, value='{"a": 1}'))
        assert "doc" in page._title.get_text()
        assert "row 4" in page._title.get_text()
        assert page._text.get_text().startswith("{\n")

    def test_apply_is_dead_until_the_buffer_changes(self, gtk):
        from sqlide.frontend.value_view import ValuePage

        written = []
        page = ValuePage()
        page.set_value(
            CellValue(
                column="doc",
                row=1,
                value="hello",
                apply=written.append,
            )
        )
        assert not page._apply.get_sensitive()
        page._text.set_text("goodbye")
        assert page._apply.get_sensitive()
        page._apply.emit("clicked")
        assert written == ["goodbye"]

    def test_a_read_only_cell_offers_no_buttons(self, gtk):
        from sqlide.frontend.value_view import ValuePage

        page = ValuePage()
        page.set_value(CellValue(column="doc", row=1, value="hello"))
        assert not page._actions.get_visible()

    def test_no_cell_leaves_a_placeholder(self, gtk):
        from sqlide.frontend.value_view import ValuePage

        page = ValuePage()
        page.set_value(None)
        assert page._placeholder.get_visible()
