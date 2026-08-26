"""Database backups: what to dump, where to put it, and when.

The pieces, in the order a backup travels through them:

- `jobs.py`   — the persisted model. Destinations, jobs, run history,
                and the JSON store they all live in.
- `dump.py`   — turning a connection profile plus a selection into an
                argv for pg_dump / mysqldump / sqlite3, and running it.
- `targets.py`— local directory, S3-compatible bucket, SFTP and
                FTP(S): upload, list, download, delete.
- `runner.py` — one job end to end: dump, compress, upload, prune old
                artifacts, record the run.
- `restore.py`— the other direction: a dump file back into a database
                through psql / mysql / sqlite3.
- `schedule.py`— when a job is next due, and the systemd user timer
                that fires it while sqlide is closed.
- `cli.py`    — `sqlide-backup run <job>`, the headless entry point
                those timers call.

Nothing in here imports GTK; the frontend tab
(frontend/backups_tab.py) is a view over this package.
"""
