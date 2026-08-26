"""Putting a dump back: the vendor client, pointed at a database.

Restoring is deliberately blunter than backing up. The artifact is a
SQL script, so it goes through `psql` / `mysql` / `sqlite3` exactly as
the vendor's own documentation would tell you to run it, and whatever
the script does — drop, create, insert — is what happens.

That makes a restore the most destructive thing this app can do, so
the flow around it is: pick the artifact, pick the *target* connection
(defaulting to the one the job came from, but any connection can be
chosen — restoring production into a scratch database is the common
case), and read the warning the frontend builds from
`describe_target()`, which spells out the environment class of what is
about to be overwritten. Nothing here runs until the caller has done
that.
"""

from __future__ import annotations

import gzip
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from sqlide.backend import identity
from sqlide.backend.backups.dump import TOOLS, DumpCommand, DumpError
from sqlide.backend.connections import ConnectionProfile

_LOG_TAIL = 4000


class RestoreError(Exception):
    pass


def client_for(kind: str) -> str:
    return TOOLS.get(kind, ("", ""))[1]


def unsupported_reason(profile: ConnectionProfile) -> str:
    """Why this connection cannot be restored into, or ""."""
    if profile.kind not in TOOLS:
        return f"{profile.kind} connections have no restore client sqlide can drive."
    if profile.use_ssh:
        return (
            "This connection goes through sqlide's own SSH tunnel, which "
            "the restore client cannot reach."
        )
    if not shutil.which(client_for(profile.kind)):
        return f"{client_for(profile.kind)} is not installed on this machine."
    return ""


def describe_target(profile: ConnectionProfile, database: str = "") -> str:
    """The sentence the confirmation shows: what gets overwritten, and
    how much that costs. The environment class is the same one the
    destructive-SQL ladder uses (backend/identity.py)."""
    where = database or profile.database or profile.file_path or profile.host
    label = identity.ENVIRONMENT_LABELS.get(profile.environment, "Unset").lower()
    return (
        f"Running this script against {profile.name} ({where}) may drop and "
        f"recreate objects there. That connection is marked {label}."
    )


def command_for(
    profile: ConnectionProfile, database: str = ""
) -> DumpCommand:
    """The client command that reads a SQL script on stdin."""
    reason = unsupported_reason(profile)
    if reason:
        raise RestoreError(reason)
    if profile.kind == "postgres":
        argv = [
            client_for("postgres"),
            "--host", profile.host,
            "--port", str(profile.port or 5432),
            # Stop at the first error rather than plough on leaving a
            # half-restored database that looks fine.
            "--set", "ON_ERROR_STOP=1",
            "--quiet",
        ]
        if profile.user:
            argv += ["--username", profile.user]
        argv += ["--dbname", database or profile.database or "postgres"]
        env = {"PGPASSWORD": profile.password} if profile.password else {}
        return DumpCommand(argv, env)
    if profile.kind == "mysql":
        target = database or profile.database
        if not target:
            raise RestoreError("Choose a database to restore into.")
        argv = [
            client_for("mysql"),
            "--host", profile.host,
            "--port", str(profile.port or 3306),
            "--default-character-set=utf8mb4",
        ]
        if profile.user:
            argv += ["--user", profile.user]
        argv.append(target)
        env = {"MYSQL_PWD": profile.password} if profile.password else {}
        return DumpCommand(argv, env)
    if not profile.file_path:
        raise RestoreError("This SQLite connection has no file.")
    return DumpCommand([client_for("sqlite"), profile.file_path], {})


def run_restore(
    profile: ConnectionProfile,
    script: Path,
    *,
    database: str = "",
    on_progress: Callable[[str], None] | None = None,
) -> str:
    """Feed `script` (plain or .gz) to the vendor client. Returns the
    client's output; raises RestoreError with it on failure."""
    say = on_progress or (lambda _text: None)
    command = command_for(profile, database)
    env = dict(os.environ)
    env.update(command.env)
    opener = gzip.open if script.suffix == ".gz" else open
    say(f"Restoring {script.name} into {profile.name}…")
    process = subprocess.Popen(
        command.argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
    )
    try:
        with opener(script, "rb") as handle:
            shutil.copyfileobj(handle, process.stdin)
        process.stdin.close()
        output = process.stdout.read().decode("utf-8", "replace")[-_LOG_TAIL:]
        code = process.wait()
    except (OSError, EOFError) as exc:
        process.kill()
        raise RestoreError(str(exc)) from exc
    if code != 0:
        raise RestoreError(
            output.strip() or f"{command.argv[0]} exited with status {code}"
        )
    return output


# Re-exported so callers can catch one error type from this module.
__all__ = [
    "RestoreError",
    "DumpError",
    "client_for",
    "command_for",
    "describe_target",
    "run_restore",
    "unsupported_reason",
]
