"""The right side panel: Files over the saved SQL, Info over the tab.

The panel used to be one flat row of eight pages, Snippets next to
Aggregate. It is two tabs now — Files (Snippets and Scripts, searchable
and pinnable) and Info (the pages the active tab's kind offers) — and
Notes is gone entirely.

Widget tests, so they need a display; the store tests underneath them
do not.
"""

from __future__ import annotations

import pytest

from sqlide.backend.saved import SavedItem, SavedStore
from sqlide.frontend.side_panel import _CONTEXT_PAGES


# Pinning, as a store


def test_a_saved_item_is_unpinned_until_it_is_pinned(tmp_path) -> None:
    store = SavedStore("snippets.json", tmp_path)
    item = store.add("count", "SELECT count(*) FROM orders")
    assert item.pinned is False
    store.set_pinned(item, True)
    assert SavedStore("snippets.json", tmp_path).load()[0].pinned is True


def test_a_file_written_before_pinning_existed_still_loads(tmp_path) -> None:
    (tmp_path / "snippets.json").write_text(
        '[{"name": "count", "sql": "SELECT 1"}]', encoding="utf-8"
    )
    assert SavedStore("snippets.json", tmp_path).load() == [
        SavedItem(name="count", sql="SELECT 1")
    ]


def test_pinning_notifies_subscribers(tmp_path) -> None:
    """Every window's Files tree reorders together, so a pin is a
    change like an add or a remove."""
    store = SavedStore("snippets.json", tmp_path)
    item = store.add("count", "SELECT 1")
    seen: list[list[SavedItem]] = []
    store.subscribe(seen.append)
    store.set_pinned(item, True)
    assert len(seen) == 1
    store.set_pinned(item, True)  # already pinned: nothing changed
    assert len(seen) == 1


# Which Info pages a tab offers


def test_notes_are_gone_from_every_context() -> None:
    assert all("notes" not in pages for pages in _CONTEXT_PAGES.values())


def test_a_tab_with_a_grid_offers_the_record_page() -> None:
    for context in ("console", "table", "grid"):
        assert "record" in _CONTEXT_PAGES[context]
    assert "record" not in _CONTEXT_PAGES["other"]


def test_only_a_table_tab_offers_saved_filters() -> None:
    assert "filters" in _CONTEXT_PAGES["table"]
    assert all(
        "filters" not in pages
        for name, pages in _CONTEXT_PAGES.items()
        if name != "table"
    )


# The widget


@pytest.fixture()
def panel(tmp_path, monkeypatch):
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gtk

    if not Gtk.init_check():
        pytest.skip("no display for GTK")

    from sqlide.frontend import side_panel as module

    snippets = SavedStore("snippets.json", tmp_path)
    queries = SavedStore("saved_queries.json", tmp_path)
    monkeypatch.setattr(module, "snippets_store", snippets)
    monkeypatch.setattr(module, "queries_store", queries)

    used: list[str] = []
    widget = module.SidePanel(
        on_activate=lambda entry: None,
        on_clear=lambda: None,
        on_insert_snippet=used.append,
        on_open_query=lambda item: used.append(item.sql),
        get_console_sql=lambda: "SELECT 1",
        on_error=lambda message: None,
        on_apply_filter=lambda entry: None,
        on_save_filter=lambda name: None,
        on_delete_filter=lambda entry: None,
    )
    yield widget, snippets, queries, used


def _folder(panel, index: int):
    return panel._files._folders[index]


def _titles(folder) -> list[str]:
    return [
        row.get_title()
        for row in folder._rows
        if hasattr(row, "get_title")
    ]


def test_the_panel_has_two_tabs(panel) -> None:
    widget, _snippets, _queries, _used = panel
    names = []
    pages = widget._top.get_pages()
    for i in range(pages.get_n_items()):
        names.append(pages.get_item(i).get_name())
    assert names == ["files", "info"]


def test_the_files_tab_has_a_snippets_and_a_scripts_folder(panel) -> None:
    widget, _snippets, _queries, _used = panel
    assert [f.row.get_title() for f in widget._files._folders] == [
        "Snippets",
        "Scripts",
    ]


def test_a_saved_snippet_shows_up_in_its_folder(panel) -> None:
    widget, snippets, _queries, _used = panel
    snippets.add("count", "SELECT count(*) FROM orders")
    assert _titles(_folder(widget, 0)) == ["count"]


def test_pinned_items_sort_to_the_top_of_their_folder(panel) -> None:
    widget, snippets, _queries, _used = panel
    snippets.add("first", "SELECT 1")
    later = snippets.add("later", "SELECT 2")
    assert _titles(_folder(widget, 0)) == ["first", "later"]
    snippets.set_pinned(later, True)
    assert _titles(_folder(widget, 0)) == ["later", "first"]


def test_search_filters_by_name_and_by_sql(panel) -> None:
    widget, snippets, _queries, _used = panel
    snippets.add("count", "SELECT count(*) FROM orders")
    snippets.add("recent", "SELECT * FROM shipments")

    widget._files._search.set_text("shipments")  # matches on the SQL
    assert _titles(_folder(widget, 0)) == ["recent"]

    widget._files._search.set_text("cou")  # matches on the name
    assert _titles(_folder(widget, 0)) == ["count"]

    widget._files._search.set_text("")
    assert len(_titles(_folder(widget, 0))) == 2


def test_the_record_page_hosts_the_active_tab_s_view(panel) -> None:
    """The view belongs to the tab, so the panel only parents it — a
    tab with no grid puts the placeholder back."""
    from gi.repository import Gtk

    widget, _snippets, _queries, _used = panel
    view = Gtk.Label(label="a record")
    widget.set_record(view)
    assert view.get_parent() is widget._record_host
    assert not widget._record_placeholder.get_visible()

    widget.set_record(None)
    assert view.get_parent() is None
    assert widget._record_placeholder.get_visible()


def test_showing_a_value_opens_the_record_page_under_info(panel) -> None:
    widget, _snippets, _queries, _used = panel
    widget.set_context("table")
    widget._top.set_visible_child_name("files")
    widget.show_value(None)
    assert widget._top.get_visible_child_name() == "info"
    assert widget._stack.get_visible_child_name() == "record"


def test_a_context_without_a_grid_drops_the_previous_tab_s_row(panel) -> None:
    from gi.repository import Gtk

    widget, _snippets, _queries, _used = panel
    view = Gtk.Label(label="a record")
    widget.set_context("table")
    widget.set_record(view)
    widget.set_aggregate(["count 3"])

    widget.set_context("other")

    assert view.get_parent() is None
    assert not widget._agg_label.get_visible()
