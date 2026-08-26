"""The backup manager's engine: model, dump commands, schedule, and a
real round trip.

The end-to-end tests use SQLite, because sqlite3 is the one client
that needs no server — a job dumps a real database to a local
destination, retention prunes it, and the artifact restores into a
second file. Postgres and MySQL are covered at the level that has
actual logic in it, the argv builders: whether --schema-only or
--no-data is right for a dialect is decided here, not at 2am.
"""

from __future__ import annotations

import gzip
import shutil
import subprocess

import pytest

from sqlide.backend.backups import dump, restore, runner, schedule, targets
from sqlide.backend.backups.jobs import (
    CONTENT_DATA,
    CONTENT_SCHEMA,
    KIND_CONFIG,
    LOCAL,
    BackupStore,
    Destination,
    Job,
    Schedule,
)
from sqlide.backend.connections import ConnectionProfile

from datetime import datetime, timedelta

needs_sqlite3 = pytest.mark.skipif(
    not shutil.which("sqlite3"), reason="sqlite3 client not installed"
)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    # Keep the test off the developer's real keyring and config dir.
    monkeypatch.setattr("sqlide.backend.secrets.AVAILABLE", False)
    return BackupStore(tmp_path / "backups.json")


@pytest.fixture()
def database(tmp_path):
    path = tmp_path / "shop.db"
    subprocess.run(
        ["sqlite3", str(path)],
        input=(
            "CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT);"
            "INSERT INTO users(name) VALUES('ada'),('grace');"
            "CREATE TABLE audit(id INTEGER PRIMARY KEY, note TEXT);"
            "INSERT INTO audit(note) VALUES('kept');"
        ),
        text=True,
        check=True,
    )
    return ConnectionProfile("shop", "sqlite", file_path=str(path))


def _job(store, tmp_path, **kwargs) -> Job:
    destination = store.add_destination(
        Destination("Disk", LOCAL, path=str(tmp_path / "vault"))
    )
    job = Job("Nightly", destination_id=destination.id, **kwargs)
    return store.add_job(job)


# The model


def test_store_round_trips_jobs_and_destinations(store, tmp_path):
    job = _job(store, tmp_path, compression="none", keep=3)
    reopened = BackupStore(store.path)
    assert [j.name for j in reopened.jobs] == ["Nightly"]
    assert reopened.job(job.id).keep == 3
    assert reopened.destinations[0].kind == LOCAL
    # Schedule survives as a dataclass, not the dict it was written as.
    assert isinstance(reopened.job(job.id).schedule, Schedule)


def test_unknown_fields_from_a_newer_version_are_ignored(store, tmp_path):
    store.path.write_text(
        '{"jobs": [{"name": "Later", "id": "abc", "quantum": true}]}'
    )
    reopened = BackupStore(store.path)
    assert reopened.job("abc").name == "Later"


def test_removing_a_destination_orphans_its_jobs_rather_than_deleting_them(
    store, tmp_path
):
    job = _job(store, tmp_path)
    store.remove_destination(store.destinations[0].id)
    assert store.job(job.id).destination_id == ""


def test_artifact_name_is_filename_safe_and_stamped():
    job = Job("Nightly: Prod DB", compression="gzip")
    name = job.artifact_name(datetime(2026, 8, 26, 2, 0, 0))
    assert name == "nightly--prod-db-20260826-020000.sql.gz"
    assert Job("x", compression="none").extension() == ".sql"
    assert Job("x", kind=KIND_CONFIG).extension() == ".zip"


# Dump commands


def test_postgres_command_selects_tables_within_the_jobs_schema():
    profile = ConnectionProfile(
        "p", "postgres", host="db", user="app", password="s3cr3t",
        database="shop", schema="sales",
    )
    command = dump.command_for(profile, Job("j", objects=["orders", "public.x"]))
    assert "--table" in command.argv
    assert "sales.orders" in command.argv  # unqualified name gets the schema
    assert "public.x" in command.argv  # an explicit one is left alone
    # The password never reaches argv.
    assert "s3cr3t" not in command.argv
    assert command.env["PGPASSWORD"] == "s3cr3t"
    assert "***" in command.preview() and "s3cr3t" not in command.preview()


def test_postgres_content_modes():
    profile = ConnectionProfile("p", "postgres", database="shop")
    assert "--schema-only" in dump.command_for(
        profile, Job("j", content=CONTENT_SCHEMA)
    ).argv
    assert "--data-only" in dump.command_for(
        profile, Job("j", content=CONTENT_DATA)
    ).argv


def test_mysql_command_puts_tables_after_the_database_unqualified():
    profile = ConnectionProfile(
        "m", "mysql", host="db", user="app", password="pw", database="shop"
    )
    command = dump.command_for(profile, Job("j", objects=["shop.orders"]))
    assert command.argv[-2:] == ["shop", "orders"]
    assert "--single-transaction" in command.argv
    assert command.env["MYSQL_PWD"] == "pw"


def test_mysql_data_only_skips_routines_that_would_be_duplicated():
    profile = ConnectionProfile("m", "mysql", database="shop")
    argv = dump.command_for(profile, Job("j", content=CONTENT_DATA)).argv
    assert "--no-create-info" in argv and "--skip-routines" in argv


def test_a_job_with_no_mysql_database_is_refused_before_it_runs():
    profile = ConnectionProfile("m", "mysql")
    with pytest.raises(dump.DumpError):
        dump.command_for(profile, Job("j"))


def test_tunnelled_and_jdbc_connections_say_why_they_cannot_be_dumped():
    tunnelled = ConnectionProfile("p", "postgres", use_ssh=True, database="d")
    assert "SSH" in dump.unsupported_reason(tunnelled)
    assert "jdbc" in dump.unsupported_reason(ConnectionProfile("j", "jdbc"))


# Dumping and restoring for real


@needs_sqlite3
def test_dump_and_restore_round_trip(store, tmp_path, database):
    job = _job(store, tmp_path, compression="gzip")
    job.workspace_id, job.connection = "w", database.name

    artifact = tmp_path / "dump.sql.gz"
    size, _log = dump.run_dump(database, job, artifact)
    assert size > 0
    text = gzip.open(artifact, "rt").read()
    assert "CREATE TABLE users" in text and "ada" in text

    target = ConnectionProfile("copy", "sqlite", file_path=str(tmp_path / "c.db"))
    restore.run_restore(target, artifact)
    rows = subprocess.run(
        ["sqlite3", target.file_path, "SELECT name FROM users ORDER BY name"],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    assert rows == ["ada", "grace"]


@needs_sqlite3
def test_selecting_one_table_leaves_the_others_out(tmp_path, database):
    artifact = tmp_path / "users.sql"
    dump.run_dump(database, Job("j", objects=["users"], compression="none"), artifact)
    text = artifact.read_text()
    assert "users" in text and "audit" not in text


@needs_sqlite3
def test_sqlite_data_only_dump_keeps_the_rows_and_drops_the_schema(
    tmp_path, database
):
    artifact = tmp_path / "data.sql"
    dump.run_dump(
        database, Job("j", content=CONTENT_DATA, compression="none"), artifact
    )
    text = artifact.read_text()
    assert "INSERT INTO" in text and "CREATE TABLE" not in text


@needs_sqlite3
def test_a_failing_dump_leaves_no_half_written_artifact(tmp_path):
    missing = ConnectionProfile(
        "gone", "sqlite", file_path=str(tmp_path / "nope.db")
    )
    artifact = tmp_path / "out.sql"
    with pytest.raises(dump.DumpError):
        dump.run_dump(missing, Job("j", objects=["users"]), artifact)
    assert not artifact.exists()


@needs_sqlite3
def test_a_dump_of_a_table_that_does_not_exist_is_a_failure_not_an_empty_file(
    tmp_path, database
):
    # sqlite3 dumps a missing table without complaining or failing;
    # storing that as a good backup is how you find out at restore time.
    artifact = tmp_path / "typo.sql"
    with pytest.raises(dump.DumpError):
        dump.run_dump(
            database, Job("j", objects=["userz"], compression="none"), artifact
        )
    assert not artifact.exists()


# The runner, destinations and retention


@needs_sqlite3
def test_run_job_uploads_and_records_history(store, tmp_path, database, monkeypatch):
    job = _job(store, tmp_path, compression="none")
    monkeypatch.setattr(runner, "resolve_connection", lambda _job: database)

    run = runner.run_job(store, job)
    assert run.ok, run.message
    vault = tmp_path / "vault"
    assert len(list(vault.iterdir())) == 1
    # History is persisted, not just in memory: the headless runner is
    # a second process writing the same file.
    assert BackupStore(store.path).runs_for(job.id)[0].ok


def test_a_job_without_a_destination_fails_the_run_rather_than_raising(store):
    job = store.add_job(Job("Homeless", kind=KIND_CONFIG))
    run = runner.run_job(store, job)
    assert not run.ok and "destination" in run.message


@needs_sqlite3
def test_retention_keeps_the_newest_and_never_touches_another_job(
    store, tmp_path, database, monkeypatch
):
    monkeypatch.setattr(runner, "resolve_connection", lambda _job: database)
    job = _job(store, tmp_path, compression="none")
    job.keep = 2
    vault = tmp_path / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    # Three of ours, and one belonging to a job that shares the folder.
    for stamp in ("20260101-000000", "20260102-000000", "20260103-000000"):
        (vault / f"{job.slug()}-{stamp}.sql").write_text("-- old")
    (vault / "weekly-20260101-000000.sql").write_text("-- someone else's")

    target = targets.open_target(store.destinations[0])
    assert runner.prune(store, job, target) == 1
    names = sorted(p.name for p in vault.iterdir())
    assert "weekly-20260101-000000.sql" in names
    assert f"{job.slug()}-20260101-000000.sql" not in names


def test_local_destination_upload_list_download_delete(tmp_path):
    destination = Destination("Disk", LOCAL, path=str(tmp_path / "vault"))
    target = targets.open_target(destination)
    source = tmp_path / "a.sql"
    source.write_text("-- hello")

    uri = target.upload(source, "a.sql")
    assert (tmp_path / "vault" / "a.sql").read_text() == "-- hello"
    assert uri.endswith("a.sql")
    assert [a.name for a in target.listing()] == ["a.sql"]

    back = target.download("a.sql", tmp_path / "back.sql")
    assert back.read_text() == "-- hello"
    target.delete("a.sql")
    assert target.listing() == []


def test_unknown_destination_kind_is_an_error_not_a_crash():
    with pytest.raises(targets.TargetError):
        targets.open_target(Destination("Odd", "carrier-pigeon"))


# Scheduling


def test_a_job_that_never_ran_is_due_immediately():
    now = datetime(2026, 8, 26, 9, 0)
    assert next_due_daily(None, now) == now


def next_due_daily(last, now):
    return schedule.next_due(Schedule("daily", at="02:00"), last, now)


def test_daily_catches_up_once_after_the_app_was_closed_for_days():
    # Last run Friday night; reopened Monday morning. One run is due,
    # and after it the next slot is tomorrow — not a backlog of three.
    friday = datetime(2026, 8, 21, 2, 0)
    monday = datetime(2026, 8, 24, 9, 0)
    due = next_due_daily(friday, monday)
    assert due == datetime(2026, 8, 22, 2, 0) and due <= monday
    assert next_due_daily(monday, monday) == datetime(2026, 8, 25, 2, 0)


def test_weekly_lands_on_the_chosen_day():
    last = datetime(2026, 8, 24, 3, 30)  # a Monday
    weekly = Schedule("weekly", at="03:30", weekday=2)  # Wednesday
    assert schedule.next_due(weekly, last, last).weekday() == 2


def test_interval_and_hourly_count_from_the_last_run():
    last = datetime(2026, 8, 26, 9, 20)
    assert schedule.next_due(
        Schedule("interval", every_minutes=15), last, last
    ) == last + timedelta(minutes=15)
    assert schedule.next_due(
        Schedule("hourly", minute=5), last, last
    ) == datetime(2026, 8, 26, 10, 5)


def test_off_is_never_due():
    assert schedule.next_due(Schedule("off"), None, datetime.now()) is None


def test_due_jobs_skips_disabled_and_systemd_owned_jobs(store, tmp_path):
    manual = _job(store, tmp_path)
    manual.schedule = Schedule("interval", every_minutes=1)
    timed = store.add_job(
        Job("Timer", schedule=Schedule("daily", systemd=True))
    )
    off = store.add_job(Job("Paused", schedule=Schedule("daily"), enabled=False))

    due = {j.id for j in schedule.due_jobs(store)}
    assert manual.id in due
    assert timed.id not in due and off.id not in due


def test_systemd_units_call_the_headless_runner_and_survive_downtime():
    job = Job("Nightly", id="deadbeef", schedule=Schedule("daily", at="02:00"))
    service, timer = schedule.unit_files(job, "/usr/bin/sqlide-backup")
    assert "ExecStart=/usr/bin/sqlide-backup run deadbeef" in service
    assert "OnCalendar=*-*-* 02:00:00" in timer
    assert "Persistent=true" in timer  # a machine asleep at 02:00 catches up


def test_schedule_descriptions_read_as_sentences():
    assert schedule.describe(Schedule("off")) == "Manual only"
    assert schedule.describe(Schedule("daily", at="02:00")) == "Daily at 02:00"
    assert schedule.describe(Schedule("interval", every_minutes=120)) == (
        "Every 2 hours"
    )


# The config job kind


def test_config_job_packs_the_config_directory(store, tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    (config_dir / "workspaces" / "w").mkdir(parents=True)
    (config_dir / "settings.toml").write_text('theme = "dark"\n')
    (config_dir / "workspaces" / "w" / "workspace.toml").write_text(
        'name = "Work"\n'
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(
        "sqlide.backend.config.config_dir", lambda: config_dir
    )
    job = _job(store, tmp_path, kind=KIND_CONFIG)

    run = runner.run_job(store, job)
    assert run.ok, run.message
    written = list((tmp_path / "vault").iterdir())
    assert len(written) == 1 and written[0].suffix == ".zip"
