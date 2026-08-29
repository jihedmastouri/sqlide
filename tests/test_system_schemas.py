"""System schemas: present, dimmed, last, and out of the way (PG-03).

`information_schema` and the server's own catalog belong in the tree —
they are worth reading — but they are never what someone came to the
sidebar for. So they sort after the user's schemas, draw dimmed, sit
behind a setting that can hide them outright, and are skipped by search
unless the filter asks for them. What is *not* true of them is that
they are disabled: a dimmed row expands and opens like any other.

Which names are system is the provider layer's answer, so nothing here
— and nothing in the UI — spells out an engine's catalog names.
"""

from __future__ import annotations

import pytest

from sqlide.backend.db import registry
from sqlide.backend.settings import Settings
from sqlide.frontend import tree_search
from sqlide.frontend.sidebar import _EXPANDABLE, _is_system_schema


# Which schemas the server owns


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("information_schema", True),
        ("INFORMATION_SCHEMA", True),
        ("pg_catalog", True),
        ("pg_toast", True),
        ("pg_temp_3", True),
        ("public", False),
        ("staging", False),
        ("pgboss", False),  # a user schema that merely starts with "pg"
    ],
)
def test_postgres_names_its_own_schemas(name: str, expected: bool) -> None:
    assert registry.is_system_schema("postgres", name) is expected


@pytest.mark.parametrize("kind", ["sqlite", "jdbc"])
def test_an_engine_without_the_level_has_no_system_schemas(kind: str) -> None:
    assert registry.is_system_schema(kind, "information_schema") is False


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("information_schema", True),
        ("performance_schema", True),
        ("mysql", True),
        ("sys", True),
        ("SYS", True),
        ("sqlide", False),
        ("mysqlish", False),  # a user database that merely starts "mysql"
    ],
)
def test_mysql_names_its_own_databases(name: str, expected: bool) -> None:
    """A schema *is* a database in MySQL, so its catalog schemas are
    databases in the tree and both questions have the one answer
    (MY-01)."""
    assert registry.is_system_schema("mysql", name) is expected
    assert registry.is_system_database("mysql", name) is expected


@pytest.mark.parametrize("kind", ["postgres", "sqlite", "jdbc"])
def test_a_database_is_not_a_schema_anywhere_else(kind: str) -> None:
    assert registry.is_system_database(kind, "information_schema") is False


def test_the_sidebar_asks_the_provider_and_never_the_engine() -> None:
    assert _is_system_schema("postgres", "pg_catalog") is True
    assert _is_system_schema("postgres", "public") is False
    assert _is_system_schema("", "pg_catalog") is False  # no profile yet


# The setting


def test_system_schemas_are_shown_by_default() -> None:
    assert Settings().show_system_schemas is True


def test_the_setting_is_read_from_the_file() -> None:
    assert (
        Settings.from_dict({"show_system_schemas": False}).show_system_schemas
        is False
    )
    # A junk value falls back to the default rather than failing the load.
    assert Settings.from_dict({"show_system_schemas": "no"}).show_system_schemas


# Search


def test_search_skips_system_rows_by_default() -> None:
    assert tree_search.in_scope("table", frozenset(), system=True) is False
    assert tree_search.in_scope("table", frozenset()) is True


def test_the_opt_in_lets_system_rows_back_in() -> None:
    scopes = frozenset({tree_search.SYSTEM_SCOPE})
    assert tree_search.in_scope("table", scopes, system=True) is True
    assert tree_search.in_scope("table", scopes) is True


def test_the_opt_in_is_not_a_kind_filter() -> None:
    """Ticking it alone must not narrow the kinds searched, and it
    never shows up in the Filter button's label."""
    scopes = frozenset({tree_search.SYSTEM_SCOPE})
    assert tree_search.in_scope("column", scopes) is True
    assert tree_search.scope_label(scopes) == "All"
    scopes = frozenset({tree_search.SYSTEM_SCOPE, "tables"})
    assert tree_search.scope_label(scopes) == "Tables"
    assert tree_search.in_scope("column", scopes) is False
    assert tree_search.in_scope("table", scopes, system=True) is True


def test_dimmed_is_not_disabled() -> None:
    """A system schema is still a container: it expands like any other
    schema row, and the setting hides it rather than freezing it."""
    assert "schema" in _EXPANDABLE


# The tree, with a display




def _tree_sidebar():
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gio, Gtk

    if not Gtk.init_check():
        pytest.skip("no display for GTK")
    from sqlide.backend.connections import ConnectionProfile
    from sqlide.frontend.sidebar import Node, Sidebar, _adopt

    def unused(*_args, **_kwargs):
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
    profile = ConnectionProfile("prod", "postgres", database="sales")
    bar.add_profile(profile)
    root = bar._roots.get_item(0)
    root.store = Gio.ListStore(item_type=Node)

    public = Node("schema", "public", profile=profile)
    public.store = Gio.ListStore(item_type=Node)
    public.store.append(Node("table", "users", profile=profile))
    _adopt(public)

    catalog = Node("schema", "pg_catalog", profile=profile, system=True)
    catalog.store = Gio.ListStore(item_type=Node)
    catalog.store.append(Node("table", "pg_user", profile=profile))
    _adopt(catalog)

    root.store.append(public)
    root.store.append(catalog)
    _adopt(root)
    return bar, catalog


def test_everything_under_a_system_schema_is_system_too() -> None:
    _bar, catalog = _tree_sidebar()
    assert catalog.store.get_item(0).system is True


def test_search_leaves_the_system_subtree_alone() -> None:
    bar, _catalog = _tree_sidebar()
    bar.set_filter("user")
    labels = _labels(bar)
    assert "users" in labels
    assert "pg_user" not in labels and "pg_catalog" not in labels


def test_the_opt_in_brings_the_system_subtree_back() -> None:
    bar, _catalog = _tree_sidebar()
    bar.set_filter("user", frozenset({tree_search.SYSTEM_SCOPE}))
    labels = _labels(bar)
    assert "pg_user" in labels and "pg_catalog" in labels


def _labels(bar):
    model = bar._view.get_model()
    return [
        model.get_item(i).get_item().label for i in range(model.get_n_items())
    ]
