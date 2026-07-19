"""Backup and restore of the whole sqlide configuration.

A backup is a plain zip of the JSON files under
$XDG_CONFIG_HOME/sqlide/ — settings.json, saved snippets/queries and
one file per workspace (workspaces/<id>.json, connection profiles and
history included). Restoring extracts those files back over the
config directory; anything not in the archive is left alone. Restored
files are only picked up on the next start (open windows keep their
in-memory state and would overwrite it on save), so the UI tells the
user to restart.
"""

from __future__ import annotations

import json
import os
import re
import zipfile
from pathlib import Path


def _config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    return Path(base) / "sqlide"


# Members a backup may contain: top-level JSON files and workspace
# files one level down. Anything else in an archive is ignored on
# restore, so a crafted zip cannot write outside the config dir.
_MEMBER_RE = re.compile(r"^(?:[\w.-]+|workspaces/[\w.-]+)\.json$")


class BackupError(Exception):
    pass


def create_backup(dest: Path) -> int:
    """Zip the config directory's JSON files into dest; returns how
    many files went in."""
    config = _config_dir()
    files = sorted(config.glob("*.json")) + sorted(
        config.glob("workspaces/*.json")
    )
    if not files:
        raise BackupError(f"Nothing to back up in {config}")
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(config).as_posix())
    return len(files)


def restore_backup(src: Path) -> int:
    """Extract a backup over the config directory; returns how many
    files were restored. Members that are not sqlide config files (or
    not valid JSON) are rejected before anything is written."""
    with zipfile.ZipFile(src) as archive:
        names = [n for n in archive.namelist() if not n.endswith("/")]
        good = [n for n in names if _MEMBER_RE.match(n)]
        if not good:
            raise BackupError("Not a sqlide backup (no config files inside)")
        contents: dict[str, bytes] = {}
        for name in good:
            data = archive.read(name)
            try:
                json.loads(data)
            except ValueError as exc:
                raise BackupError(f"{name} is not valid JSON: {exc}") from exc
            contents[name] = data
    config = _config_dir()
    for name, data in contents.items():
        target = config / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return len(contents)
