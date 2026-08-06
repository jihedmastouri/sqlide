"""The XML transfer format (backend/exchange.py).

These run without a database and without GTK: the format is meant to
be the thing you can reason about on its own.
"""

from __future__ import annotations

import pytest

from sqlide.backend import exchange, secrets
from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.workspaces import HistoryEntry, TabState, Workspace


@pytest.fixture(autouse=True)
def no_keyring(monkeypatch):
    """Importing a workspace stores its secrets; keep that out of the
    keyring of whoever runs the tests (the profile keeps the password
    in memory either way, which is what these assert on)."""
    monkeypatch.setattr(secrets, "AVAILABLE", False)


def _workspace() -> Workspace:
    workspace = Workspace(name="Work", color="blue")
    workspace.connections = [
        ConnectionProfile(
            name="reports",
            kind="postgres",
            host="db.example.com",
            port=5432,
            user="app",
            password="hunter2",
            database="reports",
            environment="production",
            color="red",
        ),
        ConnectionProfile(
            name="local", kind="sqlite", file_path="/tmp/demo.db"
        ),
    ]
    workspace.saved_filters = {
        "reports.reports.orders": [
            {
                "name": "open",
                "filters": [
                    {
                        "column": "status",
                        "op": "=",
                        "value": "open",
                        "conjunction": "AND",
                    }
                ],
            }
        ]
    }
    workspace.placeholders = {":since": "2026-01-01"}
    workspace.tabs = [TabState(kind="table", connection="local", table="t")]
    workspace.history = [
        HistoryEntry(sql="SELECT 1", connection="local", timestamp="now")
    ]
    return workspace


def test_round_trip_keeps_what_describes_the_connections():
    imported = exchange.workspace_from_xml(
        exchange.workspace_to_xml(_workspace())
    )
    assert imported.name == "Work"
    assert imported.color == "blue"
    assert [c.name for c in imported.connections] == ["reports", "local"]
    reports, local = imported.connections
    assert reports.kind == "postgres"
    assert reports.host == "db.example.com"
    assert reports.port == 5432
    assert reports.user == "app"
    assert reports.database == "reports"
    assert reports.environment == "production"
    assert reports.color == "red"
    assert local.kind == "sqlite"
    assert local.file_path == "/tmp/demo.db"
    assert local.port == 0  # default, never written
    assert imported.saved_filters["reports.reports.orders"][0]["name"] == "open"
    assert imported.placeholders == {":since": "2026-01-01"}


def test_passwords_stay_out_unless_asked_for():
    without = exchange.workspace_to_xml(_workspace())
    assert "hunter2" not in without
    assert exchange.workspace_from_xml(without).connections[0].password == ""

    with_secrets = exchange.workspace_to_xml(
        _workspace(), include_passwords=True
    )
    assert "hunter2" in with_secrets
    assert (
        exchange.workspace_from_xml(with_secrets).connections[0].password
        == "hunter2"
    )


def test_machine_local_state_is_not_exported():
    text = exchange.workspace_to_xml(_workspace())
    assert "SELECT 1" not in text  # history
    assert "<tab" not in text  # open tabs
    assert _workspace().id not in text  # the store's local id


def test_import_is_always_a_new_workspace():
    text = exchange.workspace_to_xml(_workspace())
    first = exchange.workspace_from_xml(text)
    second = exchange.workspace_from_xml(text)
    assert first.id != second.id
    assert exchange.workspace_from_xml(text, name="Copy").name == "Copy"


def test_duplicate_connection_names_are_made_unique():
    workspace = Workspace(name="W")
    workspace.connections = [
        ConnectionProfile(name="db", kind="sqlite"),
        ConnectionProfile(name="db", kind="sqlite", file_path="/other.db"),
    ]
    imported = exchange.workspace_from_xml(
        exchange.workspace_to_xml(workspace)
    )
    assert [c.name for c in imported.connections] == ["db", "db (2)"]


def test_connections_only_document():
    profiles = _workspace().connections
    text = exchange.connections_to_xml(profiles)
    assert [c.name for c in exchange.connections_from_xml(text)] == [
        "reports",
        "local",
    ]


def test_unknown_elements_and_attributes_are_skipped():
    text = """
    <sqlide format="99">
      <workspace name="Future" color="not-a-colour">
        <connection name="db" kind="sqlite" environment="mars">
          <file_path>/tmp/x.db</file_path>
          <quantum_mode>on</quantum_mode>
        </connection>
      </workspace>
    </sqlide>
    """
    imported = exchange.workspace_from_xml(text)
    profile = imported.connections[0]
    assert profile.file_path == "/tmp/x.db"
    # Unreadable values degrade to the neutral defaults instead of
    # failing the whole import.
    assert imported.color == "none"
    assert profile.environment == "unset"


@pytest.mark.parametrize(
    "text, message",
    [
        ("<not-sqlide/>", "Not a sqlide export"),
        ("<sqlide/>", "no <workspace>"),
        ("<sqlide", "Not valid XML"),
        (
            '<sqlide><workspace name="w"><connection kind="sqlite"/>'
            "</workspace></sqlide>",
            "missing its name or kind",
        ),
    ],
)
def test_broken_files_say_what_is_wrong(text, message):
    with pytest.raises(exchange.ExchangeError) as error:
        exchange.workspace_from_xml(text)
    assert message in str(error.value)
