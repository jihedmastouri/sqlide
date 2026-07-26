"""Workspace connection management: adding/removing profiles and the
name-uniqueness rule that also backs the "Edit connection" UI rename
path (window.py's _connection_edited calls unique_connection_name
directly with `exclude` so renaming a profile to its own name is a
no-op, not a collision)."""

from __future__ import annotations

from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.workspaces import Workspace


def _profile(name: str) -> ConnectionProfile:
    return ConnectionProfile(name=name, kind="sqlite", file_path=f"{name}.db")


def test_add_connection_dedupes_name():
    ws = Workspace(name="w")
    ws.add_connection(_profile("db"))
    ws.add_connection(_profile("db"))
    ws.add_connection(_profile("db"))
    assert [p.name for p in ws.connections] == ["db", "db (2)", "db (3)"]


def test_remove_connection():
    ws = Workspace(name="w")
    ws.add_connection(_profile("a"))
    ws.add_connection(_profile("b"))
    ws.remove_connection("a")
    assert [p.name for p in ws.connections] == ["b"]


def test_remove_connection_missing_name_is_noop():
    ws = Workspace(name="w")
    ws.add_connection(_profile("a"))
    ws.remove_connection("does-not-exist")
    assert [p.name for p in ws.connections] == ["a"]


def test_unique_connection_name_excludes_self():
    ws = Workspace(name="w")
    a = _profile("a")
    ws.add_connection(a)
    ws.add_connection(_profile("b"))
    # Renaming "a" to its own current name must not be treated as a
    # collision with itself.
    assert ws.unique_connection_name("a", exclude=a) == "a"
    # Renaming "a" to the other profile's name still dedupes.
    assert ws.unique_connection_name("b", exclude=a) == "b (2)"


def test_unique_connection_name_no_collision():
    ws = Workspace(name="w")
    ws.add_connection(_profile("a"))
    assert ws.unique_connection_name("new") == "new"
