"""Backup and restore of the whole sqlide configuration.

A backup is a plain zip of the config directory (backend/config.py
resolves where that is): settings.toml, saved snippets/queries, the
backup jobs, and each workspace folder — workspaces/<id>/*.toml plus
its state.json (connection profiles, tabs and history). Restoring
extracts those files back over the config directory; anything not in the archive is left alone. Restored
files are only picked up on the next start (open windows keep their
in-memory state and would overwrite it on save), so the UI tells the
user to restart.
"""

from __future__ import annotations

import json
import re
import tomllib
import zipfile
from pathlib import Path

from sqlide.backend import config


# Members a backup may contain: top-level config files and workspace
# files one or two levels down. Anything else in an archive is ignored
# on restore, so a crafted zip cannot write outside the config dir.
_MEMBER_RE = re.compile(
    r"^(?:[\w.-]+|workspaces/[\w.-]+(?:/[\w.-]+)?)\.(?:json|toml)$"
)


class BackupError(Exception):
    pass


def create_backup(dest: Path) -> int:
    """Zip the config directory's files into dest; returns how many
    files went in."""
    directory = config.config_dir()
    files = sorted(directory.glob("*.json")) + sorted(
        directory.glob("*.toml")
    )
    for pattern in ("workspaces/*.json", "workspaces/*/*.json",
                    "workspaces/*/*.toml"):
        files += sorted(directory.glob(pattern))
    if not files:
        raise BackupError(f"Nothing to back up in {directory}")
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(directory).as_posix())
    return len(files)


def restore_backup(src: Path) -> int:
    """Extract a backup over the config directory; returns how many
    files were restored. Members that are not sqlide config files (or
    do not parse as what their extension claims) are rejected before
    anything is written."""
    with zipfile.ZipFile(src) as archive:
        names = [n for n in archive.namelist() if not n.endswith("/")]
        good = [
            n
            for n in names
            if _MEMBER_RE.match(n) and ".." not in n.split("/")
        ]
        if not good:
            raise BackupError("Not a sqlide backup (no config files inside)")
        contents: dict[str, bytes] = {}
        for name in good:
            data = archive.read(name)
            try:
                if name.endswith(".toml"):
                    tomllib.loads(data.decode("utf-8"))
                else:
                    json.loads(data)
            except (ValueError, UnicodeDecodeError) as exc:
                raise BackupError(f"{name} is not readable: {exc}") from exc
            contents[name] = data
    directory = config.config_dir()
    for name, data in contents.items():
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return len(contents)
