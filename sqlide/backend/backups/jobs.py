"""The persisted backup model: destinations, jobs, runs, and the store.

Everything lives in one file, backups.json in the config directory (backend/config.py),
rather than inside a workspace: a destination (an S3 bucket, a
backup server) is a property of the machine, and a scheduled job has
to be findable by the headless runner without opening a workspace.
Jobs name their connection by workspace id + connection name, and
resolve it through the workspace store when they run.

Credentials — the S3 secret key, the SFTP/FTP password — follow the
same rule as connection passwords: the system keyring when one is
usable, plain text in this file otherwise (backend/secrets.py). The
JSON is written with the secret blanked whenever the keyring took it.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path

from sqlide.backend import config, secrets

# Keyring scope for destination credentials. secrets keys on
# (owner, name, field); backups own a namespace of their own so a
# destination can never collide with a connection's password.
_SECRET_OWNER = "backups"

# What a job dumps.
CONTENT_BOTH = "both"
CONTENT_SCHEMA = "schema"
CONTENT_DATA = "data"
CONTENTS = (CONTENT_BOTH, CONTENT_SCHEMA, CONTENT_DATA)

# What a job is a backup *of*: a database, or sqlide's own config
# (settings, saved queries, workspaces — see backend/backup.py).
KIND_DATABASE = "database"
KIND_CONFIG = "config"
KINDS = (KIND_DATABASE, KIND_CONFIG)

COMPRESSIONS = ("none", "gzip")

# Destination kinds.
LOCAL, S3, SFTP, FTP = "local", "s3", "sftp", "ftp"
DESTINATION_KINDS = (LOCAL, S3, SFTP, FTP)

MAX_RUNS = 200

# Run.job_id for a one-off backup: a backup taken now, from a dialog,
# with no stored job behind it. They share a bucket in the history so
# "what did I back up, and where did it go" has one answer.
ONE_OFF_ID = "__oneoff__"


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _from_dict(cls, data: dict):
    """Build a dataclass from a dict, ignoring keys it doesn't know.

    A file written by a newer sqlide keeps loading here, minus the
    fields this version has no idea what to do with — the same
    forgiveness backend/exchange.py extends to its XML.
    """
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Schedule:
    """When a job runs by itself. `mode` "off" means never.

    "interval" repeats every `every_minutes` from the last run;
    "hourly" runs at `minute` past each hour; "daily" at `at`
    (HH:MM local); "weekly" at `at` on `weekday` (0 = Monday).
    """

    mode: str = "off"  # off | interval | hourly | daily | weekly
    every_minutes: int = 60
    minute: int = 0
    at: str = "02:00"
    weekday: int = 0
    # Set by systemd.install(); the in-app scheduler skips a job whose
    # timer is installed so the two never both fire it.
    systemd: bool = False

    @property
    def enabled(self) -> bool:
        return self.mode != "off"


@dataclass
class Destination:
    """Where artifacts are written. One row in the manager's list."""

    name: str
    kind: str = LOCAL
    id: str = field(default_factory=_new_id)
    # local: the directory. s3/sftp/ftp: a key prefix / remote
    # directory, "" for the root.
    path: str = ""
    # s3
    bucket: str = ""
    endpoint_url: str = ""  # "" -> real AWS; else any S3-compatible host
    region: str = "us-east-1"
    access_key: str = ""
    secret_key: str = ""
    # sftp / ftp
    host: str = ""
    port: int = 0  # 0 -> kind default (22 / 21)
    user: str = ""
    password: str = ""
    key_path: str = ""  # sftp only
    tls: bool = True  # ftp only: FTPS (explicit TLS)

    def default_port(self) -> int:
        return self.port or {SFTP: 22, FTP: 21}.get(self.kind, 0)

    def describe(self) -> str:
        """One line for the manager list: where this actually writes."""
        if self.kind == LOCAL:
            return self.path or str(Path.home())
        if self.kind == S3:
            host = self.endpoint_url or "s3.amazonaws.com"
            return f"s3://{self.bucket}/{self.path}".rstrip("/") + f" ({host})"
        scheme = "sftp" if self.kind == SFTP else "ftps" if self.tls else "ftp"
        where = f"{scheme}://{self.user + '@' if self.user else ''}{self.host}"
        if self.port:
            where += f":{self.port}"
        return where + "/" + self.path.lstrip("/")

    def secret_fields(self) -> tuple[str, ...]:
        """Which of this destination's fields are credentials."""
        if self.kind == S3:
            return ("secret_key",)
        if self.kind in (SFTP, FTP):
            return ("password",)
        return ()

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Destination":
        return _from_dict(cls, data)


@dataclass
class Job:
    """One backup: a selection, a destination, and optionally a clock.

    `objects` empty means the whole database; otherwise it is the
    exact list of tables (schema-qualified where the server uses
    schemas). A config job (kind "config") ignores every database
    field and zips the config directory instead.
    """

    name: str
    id: str = field(default_factory=_new_id)
    kind: str = KIND_DATABASE
    workspace_id: str = ""
    connection: str = ""  # ConnectionProfile.name within that workspace
    database: str = ""  # "" -> the connection's own database
    schema: str = ""  # postgres: restrict to this schema
    objects: list[str] = field(default_factory=list)
    content: str = CONTENT_BOTH
    compression: str = "gzip"
    destination_id: str = ""
    schedule: Schedule = field(default_factory=Schedule)
    # How many artifacts of this job to keep at the destination.
    # 0 keeps everything.
    keep: int = 7
    enabled: bool = True

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Job":
        job = _from_dict(cls, data)
        if isinstance(job.schedule, dict):
            job.schedule = _from_dict(Schedule, job.schedule)
        return job

    def slug(self) -> str:
        """Filename-safe stem for this job's artifacts."""
        safe = "".join(
            c if c.isalnum() or c in "-_" else "-" for c in self.name.strip()
        )
        return safe.strip("-").lower() or self.id

    def extension(self) -> str:
        if self.kind == KIND_CONFIG:
            return ".zip"  # already a container; never gzipped again
        return ".sql.gz" if self.compression == "gzip" else ".sql"

    def artifact_name(self, when: datetime | None = None) -> str:
        stamp = (when or datetime.now()).strftime("%Y%m%d-%H%M%S")
        return f"{self.slug()}-{stamp}{self.extension()}"


@dataclass
class Run:
    """One attempt, kept so the manager can show what happened."""

    job_id: str
    started: str  # local time, ISO format
    finished: str = ""
    ok: bool = False
    artifact: str = ""  # where it landed, as the destination describes it
    size: int = 0  # bytes uploaded
    message: str = ""  # error, or a short summary on success
    log: str = ""  # tail of the dump tool's stderr

    @classmethod
    def from_dict(cls, data: dict) -> "Run":
        return _from_dict(cls, data)


class BackupStore:
    """The backups.json file: destinations, jobs and run history.

    Mutations save immediately, because the other writer is a headless
    `sqlide-backup run` fired by a systemd timer — two processes share
    this file, and a run recorded only in memory would be lost.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (config.config_dir() / "backups.json")
        self.destinations: list[Destination] = []
        self.jobs: list[Job] = []
        self.runs: list[Run] = []
        self.load()

    # Loading and saving

    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        self.destinations = [
            Destination.from_dict(d) for d in data.get("destinations", [])
        ]
        self.jobs = [Job.from_dict(j) for j in data.get("jobs", [])]
        self.runs = [Run.from_dict(r) for r in data.get("runs", [])]
        for destination in self.destinations:
            self._load_secrets(destination)

    def save(self) -> None:
        payload = {
            "destinations": [
                self._public(d) for d in self.destinations
            ],
            "jobs": [j.to_dict() for j in self.jobs],
            "runs": [asdict(r) for r in self.runs[-MAX_RUNS:]],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload, indent=2))

    def _public(self, destination: Destination) -> dict:
        """The destination as it goes on disk: credentials stripped
        when the keyring is holding them instead."""
        data = destination.to_dict()
        for name in destination.secret_fields():
            secrets.set_secret(
                _SECRET_OWNER, destination.id, name, data.get(name, "")
            )
            if secrets.AVAILABLE:
                data[name] = ""
        return data

    def _load_secrets(self, destination: Destination) -> None:
        for name in destination.secret_fields():
            if getattr(destination, name):
                continue  # plain text in the file; nothing to look up
            value = secrets.get_secret(_SECRET_OWNER, destination.id, name)
            if value:
                setattr(destination, name, value)

    # Destinations

    def destination(self, destination_id: str) -> Destination | None:
        return next(
            (d for d in self.destinations if d.id == destination_id), None
        )

    def add_destination(self, destination: Destination) -> Destination:
        self.destinations.append(destination)
        self.save()
        return destination

    def remove_destination(self, destination_id: str) -> None:
        destination = self.destination(destination_id)
        if destination is None:
            return
        for name in destination.secret_fields():
            secrets.set_secret(_SECRET_OWNER, destination.id, name, "")
        self.destinations.remove(destination)
        for job in self.jobs:
            if job.destination_id == destination_id:
                job.destination_id = ""
        self.save()

    # Jobs

    def job(self, job_id: str) -> Job | None:
        return next((j for j in self.jobs if j.id == job_id), None)

    def add_job(self, job: Job) -> Job:
        self.jobs.append(job)
        self.save()
        return job

    def remove_job(self, job_id: str) -> None:
        self.jobs = [j for j in self.jobs if j.id != job_id]
        self.runs = [r for r in self.runs if r.job_id != job_id]
        self.save()

    # Runs

    def record(self, run: Run) -> Run:
        self.runs.append(run)
        del self.runs[:-MAX_RUNS]
        self.save()
        return run

    def runs_for(self, job_id: str) -> list[Run]:
        """Newest first, so the manager's history list needs no sort."""
        return [r for r in reversed(self.runs) if r.job_id == job_id]

    def last_run(self, job_id: str, *, ok_only: bool = False) -> Run | None:
        for run in self.runs_for(job_id):
            if run.finished and (run.ok or not ok_only):
                return run
        return None
