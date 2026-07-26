"""End-to-end check that Workspace's connection lifecycle (add / edit /
remove) keeps the keyring and the JSON round trip in sync. Uses the
same in-memory fake keyring as test_secrets.py — never the real OS
keyring."""

from __future__ import annotations

import pytest

from sqlide.backend import secrets
from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.workspaces import Workspace
from tests.test_secrets import _FakeKeyring


@pytest.fixture
def fake_keyring(monkeypatch):
    fake = _FakeKeyring()
    monkeypatch.setattr(secrets, "keyring", fake)
    monkeypatch.setattr(secrets, "AVAILABLE", True)
    return fake


def test_add_connection_blanks_password_in_json(fake_keyring):
    ws = Workspace(name="w")
    ws.add_connection(
        ConnectionProfile(name="db", kind="postgres", password="hunter2")
    )
    data = ws.to_dict()
    assert data["connections"][0]["password"] == ""
    # ...but the real value round-trips through from_dict via the keyring.
    restored = Workspace.from_dict(data)
    assert restored.connections[0].password == "hunter2"


def test_remove_connection_drops_keyring_entry(fake_keyring):
    ws = Workspace(name="w")
    ws.add_connection(
        ConnectionProfile(name="db", kind="postgres", password="hunter2")
    )
    ws.remove_connection("db")
    assert secrets.get_secret(ws.id, "db", "password") == ""


def test_rename_moves_the_keyring_entry(fake_keyring):
    ws = Workspace(name="w")
    profile = ConnectionProfile(name="db", kind="postgres", password="hunter2")
    ws.add_connection(profile)

    profile.name = "renamed"
    ws.sync_renamed_connection_secrets("db", profile)

    assert secrets.get_secret(ws.id, "db", "password") == ""
    assert secrets.get_secret(ws.id, "renamed", "password") == "hunter2"


def test_no_keyring_falls_back_to_plaintext_json(monkeypatch):
    monkeypatch.setattr(secrets, "AVAILABLE", False)
    ws = Workspace(name="w")
    ws.add_connection(
        ConnectionProfile(name="db", kind="postgres", password="hunter2")
    )
    data = ws.to_dict()
    assert data["connections"][0]["password"] == "hunter2"
    restored = Workspace.from_dict(data)
    assert restored.connections[0].password == "hunter2"
