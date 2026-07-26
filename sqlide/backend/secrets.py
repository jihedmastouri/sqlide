"""Connection-password storage: the system keyring when one is usable
on this machine, plaintext JSON (sqlide's original behavior) otherwise.
Availability is probed once at import so a headless box or a dev
machine with no keyring daemon just keeps working exactly as before —
this is opt-in hardening, not a hard requirement. Needs the optional
`keyring` extra: `pip install "sqlide[keyring]"`.

Secrets are scoped to (workspace id, connection name); Workspace's
add_connection/remove_connection and window.py's rename handling call
the helpers below at every point a connection's identity changes, so
the keyring never accumulates entries for a connection that no longer
matches by that name.

Known limitation: a keyring entry lives on the machine that wrote it.
Copying a workspace's JSON file to another machine carries the
blanked password field, not the secret — the connection needs its
password re-entered once there.
"""

from __future__ import annotations

from sqlide.backend.connections import ConnectionProfile

try:
    import keyring
    import keyring.errors
except ImportError:  # pragma: no cover - exercised via AVAILABLE=False
    keyring = None

_SERVICE = "sqlide"
_PROBE_KEY = "__sqlide_probe__"


def _probe() -> bool:
    if keyring is None:
        return False
    try:
        keyring.set_password(_SERVICE, _PROBE_KEY, "x")
        keyring.delete_password(_SERVICE, _PROBE_KEY)
        return True
    except Exception:
        return False


AVAILABLE = _probe()


def _key(workspace_id: str, connection_name: str, field: str) -> str:
    return f"{workspace_id}:{connection_name}:{field}"


def set_secret(
    workspace_id: str, connection_name: str, field: str, value: str
) -> None:
    """Store one secret, or clear it when `value` is blank. Best-effort:
    a keyring backend error never blocks saving the workspace."""
    if not AVAILABLE:
        return
    key = _key(workspace_id, connection_name, field)
    try:
        if value:
            keyring.set_password(_SERVICE, key, value)
        else:
            keyring.delete_password(_SERVICE, key)
    except keyring.errors.PasswordDeleteError:
        pass  # nothing was stored there
    except Exception:
        pass


def get_secret(workspace_id: str, connection_name: str, field: str) -> str:
    if not AVAILABLE:
        return ""
    try:
        return (
            keyring.get_password(_SERVICE, _key(workspace_id, connection_name, field))
            or ""
        )
    except Exception:
        return ""


def store_profile_secrets(workspace_id: str, profile: ConnectionProfile) -> None:
    set_secret(workspace_id, profile.name, "password", profile.password)
    set_secret(workspace_id, profile.name, "ssh_password", profile.ssh_password)


def load_profile_secrets(workspace_id: str, profile: ConnectionProfile) -> None:
    if not profile.password:
        profile.password = get_secret(workspace_id, profile.name, "password")
    if not profile.ssh_password:
        profile.ssh_password = get_secret(workspace_id, profile.name, "ssh_password")


def drop_profile_secrets(workspace_id: str, name: str) -> None:
    set_secret(workspace_id, name, "password", "")
    set_secret(workspace_id, name, "ssh_password", "")


def hydrate(workspace_id: str, profile: ConnectionProfile) -> None:
    """After JSON parse: pull password/ssh_password from the keyring
    when the file carried them blank, or push them into the keyring
    when the file still carries plaintext (a pre-keyring workspace, or
    a keyring that just became available on this machine) so the next
    save can safely blank the JSON."""
    if not AVAILABLE:
        return
    if profile.password or profile.ssh_password:
        store_profile_secrets(workspace_id, profile)
    else:
        load_profile_secrets(workspace_id, profile)


def redact(data: dict) -> dict:
    """The JSON-safe copy of one connection dict: password fields
    blanked when the keyring is the real store for them."""
    if not AVAILABLE:
        return data
    data = dict(data)
    data["password"] = ""
    data["ssh_password"] = ""
    return data
