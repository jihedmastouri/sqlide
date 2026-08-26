"""When a job is next due, and who fires it.

Two clocks, and a job uses exactly one:

- The **in-app scheduler**. `due_jobs()` is called from a one-minute
  GLib tick while sqlide is open (frontend/application.py). It works
  off the recorded run history rather than a timer, so closing the app
  over the weekend and reopening it on Monday runs the missed daily
  backup once — catch-up, not a storm of eight.
- A **systemd user timer**, for jobs that must run with sqlide closed.
  `install()` writes ~/.config/systemd/user/sqlide-backup-<id>.{service,
  timer}, which calls the `sqlide-backup run <id>` entry point.
  A job with `schedule.systemd` set is skipped by the in-app scheduler,
  so the two never both fire it.

`next_due()` is pure and takes "now" as an argument, which is how the
tests pin behaviour that would otherwise depend on the wall clock.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from sqlide.backend.backups.jobs import BackupStore, Job, Schedule

UNIT_DIR = Path(
    os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
) / "systemd" / "user"


def next_due(
    schedule: Schedule, last_run: datetime | None, now: datetime
) -> datetime | None:
    """When this schedule should next fire, or None if it never should.

    A job that has never run is due immediately: the alternative — wait
    for the first scheduled slot — leaves a freshly created nightly
    backup with nothing at the destination until tomorrow, and no way
    to tell "not due yet" from "broken".
    """
    if not schedule.enabled:
        return None
    if last_run is None:
        return now
    if schedule.mode == "interval":
        return last_run + timedelta(minutes=max(1, schedule.every_minutes))
    if schedule.mode == "hourly":
        slot = last_run.replace(
            minute=min(59, max(0, schedule.minute)), second=0, microsecond=0
        )
        while slot <= last_run:
            slot += timedelta(hours=1)
        return slot
    hour, minute = _parse_at(schedule.at)
    slot = last_run.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if schedule.mode == "daily":
        while slot <= last_run:
            slot += timedelta(days=1)
        return slot
    if schedule.mode == "weekly":
        while slot <= last_run or slot.weekday() != schedule.weekday % 7:
            slot += timedelta(days=1)
        return slot
    return None


def due_jobs(store: BackupStore, now: datetime | None = None) -> list[Job]:
    """Every enabled job whose next slot has passed, systemd-driven
    jobs excluded (their timer owns them)."""
    now = now or datetime.now()
    due = []
    for job in store.jobs:
        if not job.enabled or not job.schedule.enabled or job.schedule.systemd:
            continue
        last = store.last_run(job.id)
        when = next_due(
            job.schedule, _parse_stamp(last.started) if last else None, now
        )
        if when is not None and when <= now:
            due.append(job)
    return due


def describe(schedule: Schedule) -> str:
    """The schedule as a sentence, for the job list's subtitle."""
    if not schedule.enabled:
        return "Manual only"
    if schedule.mode == "interval":
        minutes = max(1, schedule.every_minutes)
        if minutes % 60 == 0:
            hours = minutes // 60
            return f"Every {hours} hour{'s' if hours != 1 else ''}"
        return f"Every {minutes} minutes"
    if schedule.mode == "hourly":
        return f"Hourly at :{schedule.minute:02d}"
    if schedule.mode == "daily":
        return f"Daily at {schedule.at}"
    if schedule.mode == "weekly":
        return f"{_WEEKDAYS[schedule.weekday % 7]} at {schedule.at}"
    return "Manual only"


_WEEKDAYS = (
    "Mondays", "Tuesdays", "Wednesdays", "Thursdays",
    "Fridays", "Saturdays", "Sundays",
)


def _parse_at(value: str) -> tuple[int, int]:
    try:
        hour, minute = value.split(":")
        return max(0, min(23, int(hour))), max(0, min(59, int(minute)))
    except (ValueError, AttributeError):
        return 2, 0


def _parse_stamp(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


# systemd user timers


class SystemdError(Exception):
    pass


def systemd_available() -> bool:
    """Whether user timers can be installed here — systemctl on PATH
    and a user manager actually running (it isn't, in a container)."""
    if not shutil.which("systemctl"):
        return False
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-system-running"],
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    # "degraded" and "starting" are still a working manager; only a
    # non-zero exit with no output means there is none.
    return bool(result.stdout.strip())


def unit_name(job: Job) -> str:
    return f"sqlide-backup-{job.id}"


def on_calendar(schedule: Schedule) -> str:
    """The schedule as a systemd OnCalendar= expression."""
    if schedule.mode == "interval":
        minutes = max(1, schedule.every_minutes)
        return f"*:0/{minutes}" if minutes < 60 else f"*-*-* *:{schedule.minute:02d}:00"
    if schedule.mode == "hourly":
        return f"*-*-* *:{schedule.minute:02d}:00"
    hour, minute = _parse_at(schedule.at)
    if schedule.mode == "weekly":
        day = _SYSTEMD_DAYS[schedule.weekday % 7]
        return f"{day} *-*-* {hour:02d}:{minute:02d}:00"
    return f"*-*-* {hour:02d}:{minute:02d}:00"


_SYSTEMD_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def unit_files(job: Job, executable: str) -> tuple[str, str]:
    """The .service and .timer contents for this job.

    Persistent=true is the point of using systemd here: a machine that
    was asleep at 02:00 runs the backup when it wakes, instead of
    skipping the night.
    """
    service = f"""\
[Unit]
Description=sqlide backup: {job.name}
Documentation=man:sqlide(1)

[Service]
Type=oneshot
ExecStart={executable} run {job.id}
"""
    timer = f"""\
[Unit]
Description=sqlide backup timer: {job.name}

[Timer]
OnCalendar={on_calendar(job.schedule)}
Persistent=true
AccuracySec=1min

[Install]
WantedBy=timers.target
"""
    return service, timer


def install(job: Job, executable: str | None = None) -> str:
    """Write and enable this job's timer. Returns the unit name."""
    if not systemd_available():
        raise SystemdError(
            "No systemd user manager here — the in-app scheduler is the "
            "only clock available on this machine."
        )
    if not job.schedule.enabled:
        raise SystemdError("This job has no schedule to install.")
    executable = executable or shutil.which("sqlide-backup") or ""
    if not executable:
        raise SystemdError(
            "sqlide-backup is not on PATH. Install sqlide (pip install -e .) "
            "so the timer has something to run."
        )
    name = unit_name(job)
    service, timer = unit_files(job, executable)
    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    (UNIT_DIR / f"{name}.service").write_text(service)
    (UNIT_DIR / f"{name}.timer").write_text(timer)
    _systemctl("daemon-reload")
    _systemctl("enable", "--now", f"{name}.timer")
    return name


def uninstall(job: Job) -> None:
    """Stop and remove this job's timer. Quiet if it was never there."""
    name = unit_name(job)
    if shutil.which("systemctl"):
        for args in (("disable", "--now", f"{name}.timer"),):
            try:
                _systemctl(*args)
            except SystemdError:
                pass  # already gone, or never enabled
    for suffix in (".service", ".timer"):
        (UNIT_DIR / f"{name}{suffix}").unlink(missing_ok=True)
    if shutil.which("systemctl"):
        try:
            _systemctl("daemon-reload")
        except SystemdError:
            pass


def status(job: Job) -> str:
    """What systemd says about this job's timer, for the editor."""
    name = unit_name(job)
    if not (UNIT_DIR / f"{name}.timer").exists():
        return ""
    try:
        result = subprocess.run(
            ["systemctl", "--user", "list-timers", "--all", f"{name}.timer"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "Timer installed (systemctl unavailable)"
    for line in result.stdout.splitlines():
        if name in line:
            return "Timer installed — next: " + " ".join(line.split()[:3])
    return "Timer installed"


def _systemctl(*args: str) -> None:
    try:
        result = subprocess.run(
            ["systemctl", "--user", *args],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SystemdError(str(exc)) from exc
    if result.returncode != 0:
        raise SystemdError(
            (result.stderr or result.stdout).strip()
            or f"systemctl {' '.join(args)} failed"
        )
