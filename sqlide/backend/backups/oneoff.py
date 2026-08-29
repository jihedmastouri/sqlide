"""One backup, taken now, of whatever connection you are looking at.

A scheduled job is a commitment: a destination, a retention rule, a
clock. A one-off is an action — "put a copy of this somewhere before I
touch it" — and it is the thing you want before a migration, a schema
change or a risky DELETE.

The difference that matters is which engine takes it. `run_oneoff`
picks:

- the **vendor tool** (`dump.py`) when the connection is one it can
  drive, because a `pg_dump` script is the higher-fidelity artifact;
- the **portable snapshot** (`snapshot.py`) otherwise — JDBC and
  SSH-tunnelled connections, which no external tool can reach — by
  reading through the connector the app already holds open.

`preferred_engine()` is the same decision as a pure function, so the
dialog can say which one it will use, and why, before anything runs.

Either way the result is a SQL script that goes to the same places a
job's artifact goes (a destination, or a file on this machine) and is
recorded in the same run history, under `ONE_OFF_ID`.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from sqlide.backend.backups import dump, targets
from sqlide.backend.backups.jobs import (
    ONE_OFF_ID,
    BackupStore,
    Destination,
    Job,
    Run,
)
from sqlide.backend.backups.snapshot import SnapshotSpec, write_snapshot
from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.db.base import Connector

VENDOR, PORTABLE = "vendor", "portable"


def preferred_engine(profile: ConnectionProfile) -> tuple[str, str]:
    """(engine, why) for this connection, as the dialog shows it.

    The wording is written for someone who is about to take a backup,
    not for someone being refused one: the job editor's "no tool for
    this connection" is a dead end, while here it is simply the reason
    the other engine is being used.
    """
    if not dump.unsupported_reason(profile):
        return VENDOR, (
            f"{dump.tool_for(profile.kind)} — the database's own dump tool."
        )
    if profile.use_ssh:
        why = (
            "read through sqlide's connection, because the dump tool "
            "cannot reach a server behind sqlide's own SSH tunnel"
        )
    elif profile.kind not in dump.TOOLS:
        why = (
            f"read through sqlide's connection, because {profile.kind} "
            "connections have no dump tool to drive"
        )
    else:
        why = (
            f"read through sqlide's connection, because "
            f"{dump.tool_for(profile.kind)} is not installed here"
        )
    return PORTABLE, (
        f"Portable snapshot — {why}. Structure and rows only: no grants "
        "or storage settings."
    )


def artifact_name(
    profile: ConnectionProfile, spec: SnapshotSpec, when: datetime | None = None
) -> str:
    """`<connection>-<stamp>.sql[.gz]`, the one-off equivalent of a
    job's slug-stamped name."""
    safe = "".join(
        c if c.isalnum() or c in "-_" else "-" for c in profile.name.strip()
    ).strip("-").lower() or "backup"
    stamp = (when or datetime.now()).strftime("%Y%m%d-%H%M%S")
    suffix = ".sql.gz" if spec.compression == "gzip" else ".sql"
    return f"{safe}-{stamp}{suffix}"


def write_artifact(
    profile: ConnectionProfile,
    spec: SnapshotSpec,
    dest: Path,
    *,
    engine: str = "",
    connector: Connector | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> tuple[int, str]:
    """Produce one backup at `dest`. Returns (bytes, engine used).

    `connector` is only needed for the portable engine; the caller
    passes the window's own, so a tunnel or JDBC bridge that is already
    up is reused rather than opened a second time.
    """
    engine = engine or preferred_engine(profile)[0]
    if engine == VENDOR:
        job = Job(
            name=profile.name,
            connection=profile.name,
            objects=list(spec.tables),
            content=spec.content,
            compression=spec.compression,
        )
        size, _log = dump.run_dump(profile, job, dest)
        return size, VENDOR
    if connector is None:
        raise dump.DumpError(
            "The portable engine needs an open connection to read from."
        )
    size = write_snapshot(
        connector, profile.kind, spec, dest, on_progress=on_progress
    )
    return size, PORTABLE


def run_oneoff(
    store: BackupStore,
    profile: ConnectionProfile,
    spec: SnapshotSpec,
    *,
    destination: Destination | None = None,
    file_path: Path | None = None,
    engine: str = "",
    connector: Connector | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> Run:
    """Take one backup and record it. Exactly one of `destination` and
    `file_path` says where it goes.

    Like `runner.run_job`, an ordinary failure comes back as a failed
    Run rather than an exception — the caller shows `run.message`
    either way, and the history keeps the attempt.
    """
    say = on_progress or (lambda _text: None)
    run = Run(job_id=ONE_OFF_ID, started=_now())
    name = file_path.name if file_path else artifact_name(profile, spec)
    try:
        if (destination is None) == (file_path is None):
            raise ValueError("Choose a destination or a file, not both.")
        say(f"Backing up {profile.name}…")
        if file_path is not None:
            size, used = write_artifact(
                profile, spec, file_path, engine=engine,
                connector=connector, on_progress=say,
            )
            uri = str(file_path)
        else:
            with tempfile.TemporaryDirectory(prefix="sqlide-oneoff-") as tmp:
                local = Path(tmp) / name
                size, used = write_artifact(
                    profile, spec, local, engine=engine,
                    connector=connector, on_progress=say,
                )
                say(f"Uploading to {destination.name}…")
                uri = targets.open_target(destination).upload(local, name)
        run.ok = True
        run.size = size
        run.artifact = uri
        run.message = (
            f"{profile.name}: {size:,} bytes to {uri} "
            f"({'vendor tool' if used == VENDOR else 'portable snapshot'})"
        )
    except Exception as exc:
        run.ok = False
        run.message = f"{profile.name}: {exc}"
    finally:
        run.finished = _now()
        store.record(run)
    say(run.message)
    return run


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")
