"""Where configuration lives, and how it is read.

One module answers "which directory holds this install's config", so
every store (settings, workspaces, saved SQL, backups) agrees. The
directory is resolved once, in this order:

1. an explicit override — the ``--config-dir PATH`` command-line flag,
   applied through set_config_dir();
2. ``$SQLIDE_CONFIG_DIR``;
3. ``$XDG_CONFIG_HOME/sqlide`` when XDG_CONFIG_HOME is set (honoured on
   every OS, so a test or a script can redirect config the same way
   anywhere);
4. the platform default: ``~/.config/sqlide`` on Linux/BSD,
   ``~/Library/Application Support/sqlide`` on macOS,
   ``%APPDATA%\\sqlide`` on Windows.

Config files are TOML (see docs/configuration.md for the reference and
the reasoning). Reading goes through load_toml(), which never raises:
a syntax error, a wrong type or an unreadable file is recorded as a
ConfigError naming the file, the line and the key, and the caller
falls back to its defaults. errors() hands the collected list to the
UI so a broken file is reported rather than silently ignored.

FileWatcher is the "edit the file on disk and the app notices" half:
a poll-based watcher (no inotify, so it works over NFS and on every
platform GTK runs on) that the frontend ticks from a GLib timeout.
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

ENV_VAR = "SQLIDE_CONFIG_DIR"
CLI_FLAG = "--config-dir"

_override: Path | None = None


def set_config_dir(path: str | os.PathLike | None) -> None:
    """Point every store at `path` (the CLI flag). None restores the
    environment/platform resolution."""
    global _override
    _override = Path(path).expanduser() if path is not None else None


def config_dir() -> Path:
    """The directory holding this install's configuration."""
    if _override is not None:
        return _override
    env = os.environ.get(ENV_VAR, "").strip()
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg:
        return Path(xdg) / "sqlide"
    return _platform_config_dir()


def _platform_config_dir() -> Path:
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "sqlide"
    if os.name == "nt":
        base = os.environ.get("APPDATA", "").strip()
        return (Path(base) if base else home / "AppData" / "Roaming") / "sqlide"
    return home / ".config" / "sqlide"


def take_config_dir_argv(argv: list[str]) -> list[str]:
    """Consume ``--config-dir PATH`` / ``--config-dir=PATH`` from argv,
    apply it, and return the remaining arguments (GTK parses the rest,
    and would refuse an option it doesn't know)."""
    rest: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == CLI_FLAG:
            if i + 1 < len(argv):
                set_config_dir(argv[i + 1])
                i += 2
                continue
            raise SystemExit(f"{CLI_FLAG} needs a path")
        if arg.startswith(CLI_FLAG + "="):
            set_config_dir(arg.split("=", 1)[1])
            i += 1
            continue
        rest.append(arg)
        i += 1
    return rest


@dataclass(frozen=True)
class ConfigError:
    """One thing wrong with one config file, said precisely enough to
    fix by hand: which file, which line, which key."""

    path: Path
    message: str
    line: int = 0
    key: str = ""

    def __str__(self) -> str:
        where = str(self.path)
        if self.line:
            where += f":{self.line}"
        if self.key:
            where += f" [{self.key}]"
        return f"{where}: {self.message}"


_errors: list[ConfigError] = []


def record_error(
    path: Path, message: str, line: int = 0, key: str = ""
) -> ConfigError:
    """Note a config problem and keep going with defaults. Also goes to
    stderr, since a headless run (sqlide-backup) has no UI to show it."""
    error = ConfigError(path=path, message=message, line=line, key=key)
    _errors.append(error)
    print(f"sqlide: config error: {error}", file=sys.stderr)
    return error


def errors() -> list[ConfigError]:
    """Every config problem seen so far, newest last."""
    return list(_errors)


def clear_errors() -> None:
    _errors.clear()


def load_toml(path: Path) -> dict:
    """Parse a TOML config file. A missing file is an empty dict; a
    broken one is an empty dict plus a recorded ConfigError."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        record_error(path, f"cannot be read ({exc.strerror or exc})")
        return {}
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        line, key = _locate(text, exc)
        record_error(path, f"is not valid TOML: {exc}", line=line, key=key)
        return {}


def _locate(text: str, exc: tomllib.TOMLDecodeError) -> tuple[int, str]:
    """The line number and the key a TOML parse error points at.

    tomllib puts "(at line L, column C)" in the message on 3.11+ and
    exposes .lineno on 3.13+; take whichever is there, then read the
    key off that line so the message can name it."""
    line = int(getattr(exc, "lineno", 0) or 0)
    if not line:
        message = str(exc)
        marker = "at line "
        if marker in message:
            tail = message.split(marker, 1)[1]
            digits = ""
            for char in tail:
                if not char.isdigit():
                    break
                digits += char
            line = int(digits) if digits else 0
    lines = text.splitlines()
    key = ""
    if 1 <= line <= len(lines):
        candidate = lines[line - 1].strip()
        if "=" in candidate and not candidate.startswith("#"):
            key = candidate.split("=", 1)[0].strip()
        elif candidate.startswith("["):
            key = candidate.strip("[]").strip()
    return line, key


class FileWatcher:
    """Notices when a watched file changes on disk.

    Poll-based on purpose: the set of files is tiny, a stat() per file
    per tick costs nothing, and it behaves the same on every platform
    and file system. The frontend drives poll() from a GLib timeout;
    tests call it directly.
    """

    def __init__(self) -> None:
        self._stamps: dict[Path, tuple[float, int] | None] = {}
        self._callbacks: dict[Path, list[Callable[[Path], None]]] = {}

    def watch(self, path: Path, callback: Callable[[Path], None]) -> None:
        """Call `callback(path)` when `path` changes after this call.
        The file's current state is the baseline, so watching never
        fires for what is already on disk."""
        path = Path(path)
        self._stamps.setdefault(path, self._stamp(path))
        self._callbacks.setdefault(path, []).append(callback)

    def unwatch(self, path: Path) -> None:
        path = Path(path)
        self._stamps.pop(path, None)
        self._callbacks.pop(path, None)

    def forget(self, path: Path) -> None:
        """Re-baseline one file without notifying — what a store calls
        right after writing it, so its own save doesn't come back as an
        external edit."""
        path = Path(path)
        if path in self._stamps:
            self._stamps[path] = self._stamp(path)

    def poll(self) -> list[Path]:
        """Fire callbacks for every file that changed; returns those
        files. Always returns True-ish work regardless of errors, so a
        GLib timeout driving it can just ignore the result."""
        changed = []
        for path in list(self._stamps):
            stamp = self._stamp(path)
            if stamp != self._stamps[path]:
                self._stamps[path] = stamp
                changed.append(path)
        for path in changed:
            for callback in self._callbacks.get(path, []):
                callback(path)
        return changed

    @staticmethod
    def _stamp(path: Path) -> tuple[float, int] | None:
        try:
            info = path.stat()
        except OSError:
            return None
        return (info.st_mtime, info.st_size)


watcher = FileWatcher()
