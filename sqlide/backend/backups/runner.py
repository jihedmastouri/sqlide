"""Running one job end to end.

    dump (or zip the config) -> a temporary file
    upload                   -> the job's destination
    prune                    -> keep only the newest `keep` artifacts
    record                   -> a Run in the store, success or failure

The temporary file is deliberate. Dumping straight to a remote would
mean a network hiccup leaves a truncated object that looks like a
backup; writing locally first means the only thing that can be
half-done is a file we delete ourselves. It lands in the system temp
directory, so a 40GB dump wants 40GB free there — the alternative
(streaming to S3) trades that for the truncation problem, and the
truncation problem is worse.

Both the UI and the headless `sqlide-backup` entry point call
`run_job`; it is the only place a Run is written, so history looks the
same however a backup was started.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from sqlide.backend import backup as config_backup
from sqlide.backend import workspaces
from sqlide.backend.backups import dump, targets
from sqlide.backend.backups.jobs import (
    KIND_CONFIG,
    BackupStore,
    Job,
    Run,
)
from sqlide.backend.connections import ConnectionProfile


class RunError(Exception):
    pass


def resolve_connection(job: Job) -> ConnectionProfile:
    """The profile a job backs up, loaded from its workspace file.

    Jobs keep a workspace id and a connection name rather than a copy
    of the profile: a password changed in the connection dialog has to
    reach tonight's backup, and a copy would silently go stale.
    """
    store = workspaces.WorkspaceStore()
    store.load()
    workspace = next(
        (w for w in store.workspaces if w.id == job.workspace_id), None
    )
    if workspace is None:
        raise RunError(f"The workspace this job belongs to is gone ({job.name}).")
    profile = next(
        (c for c in workspace.connections if c.name == job.connection), None
    )
    if profile is None:
        raise RunError(
            f"No connection named {job.connection!r} in "
            f"workspace {workspace.name!r} any more."
        )
    return profile


def run_job(
    store: BackupStore,
    job: Job,
    *,
    on_progress: Callable[[str], None] | None = None,
) -> Run:
    """Run `job` now and record the result. Never raises for an
    ordinary failure — a failed backup is history, not an exception —
    so a scheduler loop can call this for every due job in turn."""
    say = on_progress or (lambda _text: None)
    run = Run(job_id=job.id, started=_now())
    name = job.artifact_name()
    log = ""
    try:
        destination = store.destination(job.destination_id)
        if destination is None:
            raise RunError("This job has no destination.")
        target = targets.open_target(destination)
        with tempfile.TemporaryDirectory(prefix="sqlide-backup-") as tmp:
            local = Path(tmp) / name
            say(f"Dumping to {local.name}…")
            size, log = _produce(job, local, say)
            say(f"Uploading {_human(size)} to {destination.name}…")
            uri = target.upload(local, name)
        run.artifact = uri
        run.size = size
        run.ok = True
        run.message = f"{_human(size)} written to {uri}"
        pruned = prune(store, job, target)
        if pruned:
            run.message += f" ({pruned} older backup(s) removed)"
    except (
        RunError,
        dump.DumpError,
        targets.TargetError,
        OSError,
    ) as exc:
        run.ok = False
        run.message = str(exc)
    finally:
        run.log = log
        run.finished = _now()
        store.record(run)
    say(run.message)
    return run


def _produce(
    job: Job, local: Path, say: Callable[[str], None]
) -> tuple[int, str]:
    """Fill `local` with what this job backs up."""
    if job.kind == KIND_CONFIG:
        count = config_backup.create_backup(local)
        say(f"{count} configuration file(s) packed")
        return local.stat().st_size, ""
    profile = resolve_connection(job)
    return dump.run_dump(
        profile,
        job,
        local,
        on_progress=lambda written: say(f"Dumped {_human(written)}…"),
    )


def prune(store: BackupStore, job: Job, target: targets.Target) -> int:
    """Delete this job's artifacts past `keep`, newest kept.

    Only files whose name starts with the job's slug are touched — a
    destination is usually shared between jobs, and a retention rule
    that eats another job's backups is a data-loss bug, not a tidy-up.
    """
    if job.keep <= 0:
        return 0
    prefix = job.slug() + "-"
    mine = [a for a in target.listing() if a.name.startswith(prefix)]
    # listing() is newest first, but a destination that reports no
    # timestamps (an old FTP server) sorts by name instead — the
    # artifact stamp makes that the same order.
    mine.sort(key=lambda a: (a.modified or a.name), reverse=True)
    doomed = mine[job.keep:]
    for artifact in doomed:
        target.delete(artifact.name)
    return len(doomed)


def _human(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    value = float(size)
    for unit in ("KB", "MB", "GB", "TB"):
        value /= 1024
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
    return f"{value:.1f} TB"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")
