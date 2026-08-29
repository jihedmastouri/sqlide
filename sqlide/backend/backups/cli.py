"""`sqlide-backup` — running jobs without the app.

This is what a systemd timer executes (see schedule.py), and it is
also the honest answer to "can I put this in my own cron?": yes, this
command is the whole interface.

    sqlide-backup list                 jobs and destinations
    sqlide-backup run <job-id|name>    run one job now
    sqlide-backup run --all            run every enabled job
    sqlide-backup due                  run only what the schedule says
                                       is due (for a single cron entry)
    sqlide-backup history [job]        recent runs

It imports no GTK, so it works on a headless server with the app
never installed as a desktop application. Exit status is 0 only when
every job it ran succeeded — that is what makes a failing timer show
up in `systemctl --user list-units --failed`.
"""

from __future__ import annotations

import argparse
import sys

from sqlide.backend.backups import schedule
from sqlide.backend.backups.jobs import ONE_OFF_ID, BackupStore, Job
from sqlide.backend.backups.runner import run_job


def _find(store: BackupStore, wanted: str) -> Job | None:
    job = store.job(wanted)
    if job is not None:
        return job
    return next(
        (j for j in store.jobs if j.name.lower() == wanted.lower()), None
    )


def _run(store: BackupStore, jobs: list[Job]) -> int:
    failures = 0
    for job in jobs:
        print(f"==> {job.name}")
        run = run_job(store, job, on_progress=lambda text: print(f"    {text}"))
        if not run.ok:
            failures += 1
            print(f"    FAILED: {run.message}", file=sys.stderr)
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sqlide-backup",
        description="Run sqlide backup jobs without the desktop app.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="show jobs and destinations")

    run_parser = sub.add_parser("run", help="run one job, or all of them")
    run_parser.add_argument("job", nargs="?", help="job id or name")
    run_parser.add_argument(
        "--all", action="store_true", help="run every enabled job"
    )

    sub.add_parser("due", help="run the jobs the schedule says are due")

    history = sub.add_parser("history", help="recent runs")
    history.add_argument("job", nargs="?", help="job id or name")

    args = parser.parse_args(argv)
    store = BackupStore()

    if args.command == "list":
        for destination in store.destinations:
            print(f"{destination.id}  {destination.name}  {destination.describe()}")
        for job in store.jobs:
            state = "enabled" if job.enabled else "disabled"
            print(
                f"{job.id}  {job.name}  [{state}] "
                f"{schedule.describe(job.schedule)}"
            )
        return 0

    if args.command == "run":
        if args.all:
            return _run(store, [j for j in store.jobs if j.enabled])
        if not args.job:
            parser.error("name a job, or pass --all")
        job = _find(store, args.job)
        if job is None:
            print(f"No such job: {args.job}", file=sys.stderr)
            return 2
        return _run(store, [job])

    if args.command == "due":
        due = schedule.due_jobs(store)
        if not due:
            print("Nothing due.")
            return 0
        return _run(store, due)

    job = _find(store, args.job) if args.job else None
    if args.job and job is None:
        print(f"No such job: {args.job}", file=sys.stderr)
        return 2
    runs = store.runs_for(job.id) if job else list(reversed(store.runs))
    for run in runs[:20]:
        mark = "ok  " if run.ok else "FAIL"
        job = store.job(run.job_id)
        label = job.name if job else (
            "one-off" if run.job_id == ONE_OFF_ID else run.job_id
        )
        print(f"{mark} {run.started}  {label}  {run.message}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
