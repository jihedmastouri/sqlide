"""Schema as a level of its own, above the UI (PG-01).

PostgreSQL is server → database → schema → table; MySQL calls a schema
a database and SQLite has neither. The sidebar asks the provider layer
how deep the tree goes rather than naming an engine, and it reaches a
schema through a profile pinned to it — the same profile the console's
schema dropdown builds, so a console and a tree row on one schema share
a connector.

What is asserted here is the shape and the addressing: that the level
appears exactly where the engine has one, that nothing else grows a
phantom one, and that a name pinned to a schema is qualified wherever a
person reads it back.
"""

from __future__ import annotations

import pytest

from sqlide.backend.connections import ConnectionProfile
from sqlide.frontend.sidebar import (
    _EXPANDABLE,
    _has_schemas,
    schema_profile,
)
from sqlide.frontend.window import _qualified


def _profile(kind: str, **extra) -> ConnectionProfile:
    return ConnectionProfile(
        name="prod", kind=kind, database="sales", **extra
    )


# Where the level exists at all


@pytest.mark.parametrize(
    ("kind", "expected"),
    [("postgres", True), ("mysql", False), ("sqlite", False)],
)
def test_the_tree_adds_a_schema_level_only_where_the_engine_has_one(
    kind: str, expected: bool
) -> None:
    assert _has_schemas(_profile(kind)) is expected


def test_no_connection_means_no_level() -> None:
    assert _has_schemas(None) is False


def test_a_schema_row_is_a_container() -> None:
    """It holds the object categories, so it has to expand — a level
    that cannot be opened is a level nobody can get past."""
    assert "schema" in _EXPANDABLE


# Reaching one


def test_a_schema_row_gets_a_connection_pinned_to_it() -> None:
    derived = schema_profile(_profile("postgres"), "staging")
    assert derived.schema == "staging"
    assert derived.database == "sales"
    # The name the console's dropdown builds too, so the two share a
    # connector rather than opening the same schema twice.
    assert derived.name == "prod · staging"


def test_the_schema_already_pinned_reuses_the_connection() -> None:
    profile = _profile("postgres", schema="staging")
    assert schema_profile(profile, "staging") is profile


# Reading a name back


def test_a_pinned_schema_qualifies_what_a_tab_is_called() -> None:
    profile = _profile("postgres", schema="staging")
    assert _qualified(profile, "orders") == "staging.orders"


def test_an_unpinned_connection_titles_the_bare_name() -> None:
    """MySQL, SQLite, and a PostgreSQL connection left on the server's
    own search path: no prefix appears where none was chosen."""
    for kind in ("postgres", "mysql", "sqlite"):
        assert _qualified(_profile(kind), "orders") == "orders"
