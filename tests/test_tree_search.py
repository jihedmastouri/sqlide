"""Sidebar search: matching, scoping and in-tree pruning.

The matching rules live in frontend/tree_search.py precisely so they
can be tested without a display; the pruning tests build a real Sidebar
and are skipped where GTK has no display to open.
"""

from __future__ import annotations

import pytest

from sqlide.frontend import tree_search


# Matching


def test_contiguous_match_beats_scattered():
    tight = tree_search.match("ord", "orders")
    loose = tree_search.match("ord", "old_records")
    assert tight is not None and loose is not None
    assert tight[0] < loose[0]


def test_match_is_case_insensitive_and_subsequence():
    assert tree_search.match("USR", "users") is not None
    assert tree_search.match("zz", "users") is None
    assert tree_search.match("", "users") is None


def test_ranges_cover_the_matched_letters():
    _key, ranges = tree_search.match("usr", "users")
    assert ranges == ((0, 2), (3, 4))  # "us" then "r", merged where adjacent
    _key, contiguous = tree_search.match("ser", "users")
    assert contiguous == ((1, 4),)


def test_highlight_bolds_the_match_and_escapes_the_rest():
    markup = tree_search.highlight("a<b>c", ((0, 1),))
    assert markup == "<b>a</b>&lt;b&gt;c"


# Scopes


def test_empty_scope_is_all():
    assert tree_search.in_scope("table", frozenset())
    assert tree_search.in_scope("column", frozenset())
    assert tree_search.scope_label(frozenset()) == "All"


def test_scope_narrows_to_the_chosen_kinds():
    scopes = frozenset({"tables"})
    assert tree_search.in_scope("table", scopes)
    assert not tree_search.in_scope("view", scopes)
    assert not tree_search.in_scope("column", scopes)
    assert tree_search.scope_label(scopes) == "Tables"
    assert tree_search.scope_label(frozenset({"tables", "views"})) == "2 kinds"


def test_grouping_rows_never_match_on_their_own():
    # Categories and placeholders are furniture, not objects.
    assert not tree_search.in_scope("category", frozenset())
    assert not tree_search.in_scope("note", frozenset())


def test_every_scope_key_is_unique_and_labelled():
    keys = [key for key, _label, _kinds in tree_search.SCOPES]
    assert len(keys) == len(set(keys)) == len(tree_search.SCOPE_KEYS)
    assert {"connections", "databases", "schemas", "tables", "views",
            "indexes", "functions", "columns"} == set(keys)


# In-tree pruning


@pytest.fixture
def sidebar():
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    if not Gtk.init_check():
        pytest.skip("no display for GTK")
    from sqlide.backend.connections import ConnectionProfile
    from sqlide.frontend.sidebar import Node, Sidebar

    def unused(*_args, **_kwargs):  # the callbacks no search touches
        raise AssertionError("callback should not fire")

    bar = Sidebar(
        ensure_connector=unused,
        on_open_table=unused,
        on_open_object=unused,
        on_open_section=unused,
        on_new_query=unused,
        on_open_cli=unused,
        on_open_definition=unused,
        on_edit_table=unused,
        on_open_function=unused,
        on_relation_graph=unused,
        on_view_indexes=unused,
        on_query_builder=unused,
        on_drop_object=unused,
        on_new_object=unused,
        on_mcp_server=unused,
        on_manage_users=unused,
        on_monitor=unused,
        on_open_schema=unused,
        on_edit_connection=unused,
        on_disconnect=unused,
        on_close_tabs=unused,
        count_tabs=lambda _name: 0,
        on_remove_connection=unused,
        on_add_connection=unused,
        show_error=unused,
    )
    profile = ConnectionProfile("shop", "sqlite", file_path=":memory:")
    bar.add_profile(profile)

    # Stand in for a loaded schema: one Tables category holding two
    # tables, one of which has its columns loaded.
    from gi.repository import Gio

    root = bar._roots.get_item(0)
    root.store = Gio.ListStore(item_type=Node)
    tables = Node("category", "Tables", category="tables", profile=profile)
    tables.store = Gio.ListStore(item_type=Node)
    users = Node("table", "users", profile=profile)
    users.store = Gio.ListStore(item_type=Node)
    users.store.append(Node("column", "user_id", detail="integer"))
    users.store.append(Node("column", "email", detail="text"))
    tables.store.append(users)
    tables.store.append(Node("table", "orders", profile=profile))
    root.store.append(tables)
    return bar


def _labels(sidebar):
    """Every row the view currently shows, top to bottom."""
    model = sidebar._view.get_model()
    return [
        model.get_item(i).get_item().label for i in range(model.get_n_items())
    ]


def test_match_is_shown_with_its_ancestors(sidebar):
    sidebar.set_filter("email")
    assert _labels(sidebar) == ["shop", "Tables", "users", "email"]


def test_match_carries_highlight_ranges(sidebar):
    sidebar.set_filter("ord")
    model = sidebar._view.get_model()
    node = [
        model.get_item(i).get_item()
        for i in range(model.get_n_items())
        if model.get_item(i).get_item().label == "orders"
    ][0]
    assert node.search_ranges == ((0, 3),)


def test_scope_excludes_other_kinds(sidebar):
    sidebar.set_filter("user", frozenset({"columns"}))
    # The table "users" matches the text but is out of scope; only the
    # column and its ancestors survive.
    assert _labels(sidebar) == ["shop", "Tables", "users", "user_id"]


def test_no_match_says_so(sidebar):
    sidebar.set_filter("zzz")
    assert _labels(sidebar) == ["(no matches in loaded connections)"]


def test_clearing_restores_the_tree(sidebar):
    tree_labels = _labels(sidebar)
    sidebar.set_filter("email")
    sidebar.clear_filter()
    assert _labels(sidebar) == tree_labels


def test_search_never_loads_anything(sidebar):
    # The callbacks all raise; an unloaded connection must simply
    # contribute nothing rather than reaching for a connector.
    from sqlide.backend.connections import ConnectionProfile

    sidebar.add_profile(ConnectionProfile("cold", "sqlite", file_path=":x:"))
    sidebar.set_filter("users")
    assert "cold" not in _labels(sidebar)
