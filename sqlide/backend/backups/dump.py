"""Dumping a database with the vendor's own command-line tool.

sqlide does not invent a dump format: a backup that only sqlide can
read is worth very little at 3am. Postgres goes through `pg_dump`,
MySQL/MariaDB through `mysqldump`, SQLite through `sqlite3 .dump` —
so every artifact is a plain SQL script the vendor's own client can
restore, with or without this app.

The argv builders are pure functions (`command_for`), which is what
the tests exercise and what the UI shows in its "command preview" —
seeing the exact pg_dump line is how a DBA decides whether to trust
the thing. `run_dump` executes one, streaming stdout straight to the
artifact (through gzip when asked) so a 40GB dump never lands in
memory, and keeps the tail of stderr for the run log.

Passwords never appear in argv — anything on a command line is
readable by every process on the box. They go through the environment
(PGPASSWORD / MYSQL_PWD) instead.

Not every connection can be dumped: JDBC has no vendor tool we can
assume, and a connection tunnelled over SSH would need the tool to
reach a port that only exists inside the app's tunnel. Both are
refused up front, with a reason, rather than half-working.
"""

from __future__ import annotations

import gzip
import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sqlide.backend.backups.jobs import (
    CONTENT_DATA,
    CONTENT_SCHEMA,
    Job,
)
from sqlide.backend.connections import ConnectionProfile

# The client program each connection kind is dumped and restored with.
TOOLS = {
    "postgres": ("pg_dump", "psql"),
    "mysql": ("mysqldump", "mysql"),
    "sqlite": ("sqlite3", "sqlite3"),
}

# Keep the last of a chatty tool's stderr, not all of it: a dump that
# warns once per table would otherwise write a novel into backups.json.
_LOG_TAIL = 4000


class DumpError(Exception):
    pass


@dataclass
class DumpCommand:
    argv: list[str]
    env: dict[str, str]  # extra environment, merged over os.environ
    # sqlite3 takes its script on stdin (".dump" as a command word
    # would be ambiguous with a filename); everything else is argv.
    stdin: str = ""

    def preview(self) -> str:
        """The command as a user would type it, secrets replaced by
        the environment variable that actually carries them."""
        shown = " ".join(_quote(a) for a in self.argv)
        prefix = " ".join(f"{k}=***" for k in sorted(self.env))
        return f"{prefix} {shown}".strip()


def _quote(arg: str) -> str:
    return f"'{arg}'" if " " in arg or not arg else arg


def tool_for(kind: str) -> str:
    """The dump program for a connection kind, or "" if we have none."""
    return TOOLS.get(kind, ("", ""))[0]


def tool_available(kind: str) -> str:
    """Absolute path of the dump tool, or "" when it isn't installed."""
    return shutil.which(tool_for(kind)) or ""


def unsupported_reason(
    profile: ConnectionProfile, *, require_tool: bool = True
) -> str:
    """Why this connection cannot be dumped, or "" when it can.

    Checked before a job is saved as well as before it runs, so the
    editor can refuse a selection instead of the scheduler failing at
    2am. `require_tool=False` asks only whether the *shape* of the
    connection is dumpable — building the command preview on a laptop
    that has no mysqldump is fine; running it there is not.
    """
    if profile.kind not in TOOLS:
        return (
            f"{profile.kind} connections have no dump tool sqlide can "
            "drive. Export the tables you need instead."
        )
    if profile.use_ssh:
        return (
            "This connection goes through an SSH tunnel, which only "
            "exists inside sqlide — the dump tool cannot reach it. "
            "Run the backup on the database host instead."
        )
    if require_tool and not tool_available(profile.kind):
        return (
            f"{tool_for(profile.kind)} is not installed on this machine "
            "(or not on PATH)."
        )
    return ""


def command_for(profile: ConnectionProfile, job: Job) -> DumpCommand:
    """The dump command for this connection and selection."""
    reason = unsupported_reason(profile, require_tool=False)
    if reason:
        raise DumpError(reason)
    if profile.kind == "postgres":
        return _postgres(profile, job)
    if profile.kind == "mysql":
        return _mysql(profile, job)
    return _sqlite(profile, job)


def _postgres(profile: ConnectionProfile, job: Job) -> DumpCommand:
    database = job.database or profile.database
    argv = [
        tool_for("postgres"),
        "--host", profile.host,
        "--port", str(profile.port or 5432),
        # Plain SQL, restorable with psql. --no-owner/--no-acl keep a
        # dump portable to a server where those roles do not exist,
        # which is the common case for a developer restoring one.
        "--format=plain",
        "--no-owner",
        "--no-acl",
    ]
    if profile.user:
        argv += ["--username", profile.user]
    if job.content == CONTENT_SCHEMA:
        argv.append("--schema-only")
    elif job.content == CONTENT_DATA:
        argv.append("--data-only")
    schema = job.schema or profile.schema
    if schema and not job.objects:
        argv += ["--schema", schema]
    for table in job.objects:
        # An unqualified name would follow the server's search_path;
        # pin it to the job's schema so the selection means what the
        # picker showed.
        argv += ["--table", _qualify(table, schema)]
    if database:
        argv += ["--dbname", database]
    env = {"PGPASSWORD": profile.password} if profile.password else {}
    if profile.ssl_mode:
        env["PGSSLMODE"] = profile.ssl_mode
    return DumpCommand(argv, env)


def _mysql(profile: ConnectionProfile, job: Job) -> DumpCommand:
    database = job.database or profile.database
    if not database:
        raise DumpError("This MySQL connection has no database selected.")
    argv = [
        tool_for("mysql"),
        "--host", profile.host,
        "--port", str(profile.port or 3306),
        # Consistency without locking every table for the length of
        # the dump: InnoDB gives a snapshot per transaction.
        "--single-transaction",
        "--quick",
        "--routines",
        "--events",
        "--default-character-set=utf8mb4",
    ]
    if profile.user:
        argv += ["--user", profile.user]
    if job.content == CONTENT_SCHEMA:
        argv.append("--no-data")
    elif job.content == CONTENT_DATA:
        argv += ["--no-create-info", "--skip-routines", "--skip-events"]
    if profile.ssl_mode in ("require", "verify-ca", "verify-full"):
        argv.append("--ssl-mode=" + profile.ssl_mode.upper().replace("-", "_"))
    argv.append(database)
    # mysqldump takes tables as bare positional names after the
    # database, and only from that one database.
    argv += [_unqualify(t) for t in job.objects]
    env = {"MYSQL_PWD": profile.password} if profile.password else {}
    return DumpCommand(argv, env)


def _sqlite(profile: ConnectionProfile, job: Job) -> DumpCommand:
    if not profile.file_path:
        raise DumpError("This SQLite connection has no file.")
    if not Path(profile.file_path).is_file():
        raise DumpError(f"No such database file: {profile.file_path}")
    verb = ".schema" if job.content == CONTENT_SCHEMA else ".dump"
    script = [".bail on"]
    if job.objects:
        # sqlite3 takes one object per command; several .dump lines
        # concatenate cleanly into a single script.
        script += [f"{verb} {_unqualify(t)}" for t in job.objects]
    else:
        script.append(verb)
    if job.content == CONTENT_DATA:
        # sqlite3 has no data-only dump; the INSERTs are filtered out
        # of the full dump in run_dump's line filter instead.
        pass
    return DumpCommand(
        [tool_for("sqlite"), profile.file_path],
        {},
        stdin="\n".join(script) + "\n",
    )


def _qualify(table: str, schema: str) -> str:
    if "." in table or not schema:
        return table
    return f"{schema}.{table}"


def _unqualify(table: str) -> str:
    return table.rsplit(".", 1)[-1]


def run_dump(
    profile: ConnectionProfile,
    job: Job,
    dest: Path,
    *,
    on_progress: Callable[[int], None] | None = None,
    timeout: int | None = None,
) -> tuple[int, str]:
    """Run the job's dump into `dest`. Returns (bytes written, log).

    stdout is streamed to the file — gzipped when the job asks for it —
    so memory use stays flat whatever the database's size. A non-zero
    exit deletes the half-written artifact: a truncated dump that
    looks like a backup is worse than no backup at all.
    """
    reason = unsupported_reason(profile)
    if reason:
        raise DumpError(reason)
    command = command_for(profile, job)
    opener = gzip.open if job.compression == "gzip" else open
    # SQLite data-only: strip the schema statements back out (see
    # _sqlite). Everything else the tool already selected for us.
    strip_schema = profile.kind == "sqlite" and job.content == CONTENT_DATA
    written = 0
    # Statements that aren't just the wrapper every dump carries. A
    # dump with none of them restores to an empty database, which is
    # the shape a mistyped table name takes: sqlite3 dumps an object
    # that doesn't exist without complaining.
    statements = 0
    process = subprocess.Popen(
        command.argv,
        stdin=subprocess.PIPE if command.stdin else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_env(command),
    )
    try:
        if command.stdin:
            process.stdin.write(command.stdin.encode())
            process.stdin.close()
        with opener(dest, "wb") as out:
            for line in process.stdout:
                if strip_schema and not _is_insert(line):
                    continue
                out.write(line)
                written += len(line)
                statements += _is_statement(line)
                if on_progress is not None:
                    on_progress(written)
        log = process.stderr.read().decode("utf-8", "replace")[-_LOG_TAIL:]
        code = process.wait(timeout=timeout)
    except Exception:
        process.kill()
        dest.unlink(missing_ok=True)
        raise
    if code != 0 or _soft_failure(profile.kind, log):
        # sqlite3 reports a bad table name on stderr and still exits
        # 0, which would otherwise store an empty script as a good
        # backup. Any stderr from it is treated as a failure.
        dest.unlink(missing_ok=True)
        raise DumpError(
            log.strip()
            or f"{command.argv[0]} exited with status {code}"
        )
    if not statements:
        dest.unlink(missing_ok=True)
        raise DumpError(
            f"{command.argv[0]} produced nothing — check the selection."
        )
    return dest.stat().st_size, log


# The preamble sqlite3 and pg_dump emit whether or not they found
# anything to dump.
_BOILERPLATE = (b"PRAGMA", b"BEGIN", b"COMMIT", b"SET", b"SELECT PG_CATALOG")


def _is_statement(line: bytes) -> bool:
    head = line.strip().upper()
    if not head or head.startswith(b"--") or head.startswith(b"/*"):
        return False
    return not head.startswith(_BOILERPLATE)


def _soft_failure(kind: str, log: str) -> bool:
    return kind == "sqlite" and bool(log.strip())


def _is_insert(line: bytes) -> bool:
    head = line.lstrip()[:12].upper()
    return head.startswith(b"INSERT") or head.startswith(b"REPLACE")


def _env(command: DumpCommand) -> dict[str, str]:
    env = dict(os.environ)
    env.update(command.env)
    return env
