"""Connection-password storage in backend/secrets.py: the keyring path
is exercised against an in-memory fake (never the real OS keyring —
these tests must stay hermetic and must not leave entries behind in a
developer's actual secret store), plus the AVAILABLE=False fallback
that keeps everything a no-op when no keyring backend is usable."""

from __future__ import annotations

import pytest

from sqlide.backend import secrets
from sqlide.backend.connections import ConnectionProfile


class _FakeKeyring:
    """Minimal in-memory stand-in for the `keyring` module surface
    secrets.py uses: set/get/delete_password plus the errors module."""

    class errors:
        class PasswordDeleteError(Exception):
            pass

    def __init__(self):
        self.store: dict[tuple[str, str], str] = {}

    def set_password(self, service, key, value):
        self.store[(service, key)] = value

    def get_password(self, service, key):
        return self.store.get((service, key))

    def delete_password(self, service, key):
        try:
            del self.store[(service, key)]
        except KeyError:
            raise self.errors.PasswordDeleteError()


@pytest.fixture
def fake_keyring(monkeypatch):
    fake = _FakeKeyring()
    monkeypatch.setattr(secrets, "keyring", fake)
    monkeypatch.setattr(secrets, "AVAILABLE", True)
    return fake


def test_set_and_get_secret(fake_keyring):
    secrets.set_secret("ws1", "conn1", "password", "hunter2")
    assert secrets.get_secret("ws1", "conn1", "password") == "hunter2"


def test_set_blank_value_deletes(fake_keyring):
    secrets.set_secret("ws1", "conn1", "password", "hunter2")
    secrets.set_secret("ws1", "conn1", "password", "")
    assert secrets.get_secret("ws1", "conn1", "password") == ""


def test_set_blank_on_missing_key_is_noop(fake_keyring):
    # No PasswordDeleteError should escape.
    secrets.set_secret("ws1", "conn1", "password", "")
    assert secrets.get_secret("ws1", "conn1", "password") == ""


def test_store_and_load_profile_secrets(fake_keyring):
    profile = ConnectionProfile(
        name="conn1", kind="postgres", password="p", ssh_password="s"
    )
    secrets.store_profile_secrets("ws1", profile)

    loaded = ConnectionProfile(name="conn1", kind="postgres")
    secrets.load_profile_secrets("ws1", loaded)
    assert loaded.password == "p"
    assert loaded.ssh_password == "s"


def test_drop_profile_secrets(fake_keyring):
    profile = ConnectionProfile(
        name="conn1", kind="postgres", password="p", ssh_password="s"
    )
    secrets.store_profile_secrets("ws1", profile)
    secrets.drop_profile_secrets("ws1", "conn1")

    loaded = ConnectionProfile(name="conn1", kind="postgres")
    secrets.load_profile_secrets("ws1", loaded)
    assert loaded.password == ""
    assert loaded.ssh_password == ""


def test_hydrate_pulls_when_json_blank(fake_keyring):
    profile = ConnectionProfile(name="conn1", kind="postgres", password="p")
    secrets.store_profile_secrets("ws1", profile)

    loaded = ConnectionProfile(name="conn1", kind="postgres")  # blank, as from JSON
    secrets.hydrate("ws1", loaded)
    assert loaded.password == "p"


def test_hydrate_migrates_legacy_plaintext(fake_keyring):
    # A profile freshly parsed from a pre-keyring (or foreign-machine)
    # JSON file still carries the plaintext password.
    loaded = ConnectionProfile(name="conn1", kind="postgres", password="legacy")
    secrets.hydrate("ws1", loaded)
    assert loaded.password == "legacy"  # untouched in memory
    # ...but the keyring now has it, so future saves can blank the JSON.
    assert secrets.get_secret("ws1", "conn1", "password") == "legacy"


def test_redact_blanks_password_fields(fake_keyring):
    data = {"name": "conn1", "password": "p", "ssh_password": "s", "host": "x"}
    redacted = secrets.redact(data)
    assert redacted["password"] == ""
    assert redacted["ssh_password"] == ""
    assert redacted["host"] == "x"
    assert data["password"] == "p"  # original dict untouched


def test_no_keyring_backend_is_a_full_noop(monkeypatch):
    monkeypatch.setattr(secrets, "AVAILABLE", False)
    secrets.set_secret("ws1", "conn1", "password", "hunter2")
    assert secrets.get_secret("ws1", "conn1", "password") == ""

    data = {"password": "p", "ssh_password": "s"}
    assert secrets.redact(data) is data  # unchanged, same object

    profile = ConnectionProfile(name="conn1", kind="postgres", password="legacy")
    secrets.hydrate("ws1", profile)
    assert profile.password == "legacy"  # left as loaded from JSON
