"""Language server discovery and lifecycle.

Which server runs for a connection, in order:

1. A user plugin: an executable in $XDG_CONFIG_HOME/sqlide/lsp/ named
   after the connection kind ("postgres", "mysql", "sqlite", "jdbc"),
   or "default" as a catch-all. It is spawned with no arguments, must
   speak LSP over stdio, and receives the connection's details in
   SQLIDE_DB_* environment variables.
2. Built-in defaults: Supabase's Postgres Language Server
   (`postgrestools lsp-proxy`) for postgres; sqls for mysql and sqlite,
   falling back to sql-language-server. Both get a generated config
   with the connection's coordinates so completion is schema-aware.

The query console can also pin a server for its session: NONE turns
completion off, any name from available_servers() (a plugin executable
or a known PATH binary) overrides the resolution above. Application
settings sit in between (applied by lsp_completion.py): a global
enable switch, and a per-kind default that an "auto" console resolves
through before reaching steps 1–2.

One server per (connection profile, choice), started lazily on the
first completion request, reused across consoles, and shut down at exit.
Each server gets a throwaway working directory holding its config and
the console.sql scratch document. A server that dies stays off until
the app restarts (no crash-restart loops on every keystroke).
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

from sqlide.backend.connections import ConnectionProfile
from sqlide.lsp.client import LspClient


# Session choices understood by server_for(); anything else must be a
# name from available_servers().
AUTO = "auto"
NONE = "none"

# PATH binaries we know how to launch (and configure) by name.
_KNOWN_SERVERS = ("postgrestools", "sqls", "sql-language-server")


def plugin_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    return Path(base) / "sqlide" / "lsp"


def plugin_command(kind: str) -> list[str] | None:
    for name in (kind, "default"):
        path = plugin_dir() / name
        if path.is_file() and os.access(path, os.X_OK):
            return [str(path)]
    return None


def available_servers() -> list[str]:
    """Names a console can pin: plugin executables, then known PATH
    binaries. A plugin with a known binary's name shadows it."""
    names: list[str] = []
    directory = plugin_dir()
    if directory.is_dir():
        for path in sorted(directory.iterdir()):
            if path.is_file() and os.access(path, os.X_OK):
                names.append(path.name)
    for exe in _KNOWN_SERVERS:
        if exe not in names and shutil.which(exe) is not None:
            names.append(exe)
    return names


def _plugin_env(profile: ConnectionProfile) -> dict[str, str]:
    return {
        **os.environ,
        "SQLIDE_DB_KIND": profile.kind,
        "SQLIDE_DB_NAME": profile.name,
        "SQLIDE_DB_HOST": profile.host,
        "SQLIDE_DB_PORT": str(profile.port or ""),
        "SQLIDE_DB_USER": profile.user,
        "SQLIDE_DB_PASSWORD": profile.password,
        "SQLIDE_DB_DATABASE": profile.database,
        "SQLIDE_DB_FILE": profile.file_path,
        "SQLIDE_DB_JDBC_URL": profile.jdbc_url,
    }


def _sqls_config(profile: ConnectionProfile) -> str:
    if profile.kind == "sqlite":
        driver, dsn = "sqlite3", profile.file_path
    elif profile.kind == "postgres":
        driver = "postgresql"
        dsn = (
            f"host={profile.host} port={profile.port or 5432} "
            f"user={profile.user} password={profile.password} "
            f"dbname={profile.database}"
        )
    else:
        port = profile.port or 3306
        dsn = (
            f"{profile.user}:{profile.password}"
            f"@tcp({profile.host}:{port})/{profile.database}"
        )
        driver = "mysql"
    # json.dumps produces valid YAML double-quoted scalars.
    return (
        "lowercaseKeywords: false\n"
        "connections:\n"
        f"  - alias: {json.dumps(profile.name)}\n"
        f"    driver: {driver}\n"
        f"    dataSourceName: {json.dumps(dsn)}\n"
    )


def _postgrestools_config(profile: ConnectionProfile) -> str:
    return json.dumps(
        {
            "db": {
                "host": profile.host,
                "port": profile.port or 5432,
                "username": profile.user,
                "password": profile.password,
                "database": profile.database,
                "connTimeoutSecs": 10,
            }
        },
        indent=2,
    )


def _known_command(
    profile: ConnectionProfile, name: str, workdir: Path
) -> list[str] | None:
    """Launch spec (plus generated config) for a known PATH binary."""
    exe = shutil.which(name)
    if exe is None:
        return None
    if name == "postgrestools":
        (workdir / "postgrestools.jsonc").write_text(
            _postgrestools_config(profile)
        )
        return [exe, "lsp-proxy"]
    if name == "sqls":
        config = workdir / "sqls.yml"
        config.write_text(_sqls_config(profile))
        return [exe, "-config", str(config)]
    if name == "sql-language-server":
        return [exe, "up", "--method", "stdio"]
    return None


def _default_command(profile: ConnectionProfile, workdir: Path) -> list[str] | None:
    if profile.kind == "postgres":
        return _known_command(profile, "postgrestools", workdir)
    if profile.kind in ("mysql", "sqlite"):
        return _known_command(profile, "sqls", workdir) or _known_command(
            profile, "sql-language-server", workdir
        )
    return None


def _position(text: str, offset: int) -> dict:
    """LSP position (UTF-16 columns) for a character offset."""
    before = text[:offset]
    column = before.rsplit("\n", 1)[-1]
    return {
        "line": before.count("\n"),
        "character": sum(2 if ord(ch) > 0xFFFF else 1 for ch in column),
    }


class LspServer:
    """One running server bound to one connection profile, holding one
    open document (the console scratch buffer, full-sync'd per request)."""

    def __init__(self, client: LspClient, uri: str) -> None:
        self._client = client
        self._uri = uri
        self._lock = threading.Lock()
        self._version = 0

    @property
    def alive(self) -> bool:
        return self._client.alive

    def shutdown(self) -> None:
        self._client.shutdown()

    def completions(self, text: str, offset: int) -> list[dict]:
        with self._lock:
            self._sync(text)
            result = self._client.request(
                "textDocument/completion",
                {
                    "textDocument": {"uri": self._uri},
                    "position": _position(text, offset),
                },
                timeout=5.0,
            )
        if result is None:
            return []
        if isinstance(result, dict):
            return result.get("items") or []
        return result

    def _sync(self, text: str) -> None:
        if self._version == 0:
            self._version = 1
            self._client.notify(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": self._uri,
                        "languageId": "sql",
                        "version": 1,
                        "text": text,
                    }
                },
            )
        else:
            self._version += 1
            self._client.notify(
                "textDocument/didChange",
                {
                    "textDocument": {"uri": self._uri, "version": self._version},
                    "contentChanges": [{"text": text}],
                },
            )


class LspManager:
    def __init__(self) -> None:
        self._servers: dict[str, LspServer | None] = {}
        self._workdirs: list[Path] = []
        self._lock = threading.Lock()
        atexit.register(self.shutdown_all)

    def server_for(
        self, profile: ConnectionProfile, choice: str = AUTO
    ) -> LspServer | None:
        """The (possibly cached) server for this profile and session
        choice (AUTO, NONE, or an available_servers() name), or None
        when no server is available. Worker threads only: starting one
        blocks."""
        if choice == NONE:
            return None
        key = json.dumps(
            [choice, profile.kind, profile.connect_params()], sort_keys=True
        )
        with self._lock:
            if key in self._servers:
                server = self._servers[key]
                if server is not None and not server.alive:
                    self._servers[key] = server = None
                return server
            server = self._start(profile, choice)
            self._servers[key] = server
            return server

    def shutdown_all(self) -> None:
        with self._lock:
            servers = [s for s in self._servers.values() if s is not None]
            self._servers.clear()
            workdirs, self._workdirs = self._workdirs, []
        for server in servers:
            server.shutdown()
        for workdir in workdirs:
            shutil.rmtree(workdir, ignore_errors=True)

    def _start(
        self, profile: ConnectionProfile, choice: str
    ) -> LspServer | None:
        workdir = Path(tempfile.mkdtemp(prefix="sqlide-lsp-"))
        env = None
        if choice == AUTO:
            command = plugin_command(profile.kind)
            if command is not None:
                env = _plugin_env(profile)
            else:
                command = _default_command(profile, workdir)
        else:
            path = plugin_dir() / choice
            if path.is_file() and os.access(path, os.X_OK):
                command, env = [str(path)], _plugin_env(profile)
            else:
                command = _known_command(profile, choice, workdir)
        if command is None:
            shutil.rmtree(workdir, ignore_errors=True)
            return None
        document = workdir / "console.sql"
        document.write_text("")
        try:
            client = LspClient(command, cwd=str(workdir), env=env)
            client.initialize(root_uri=workdir.as_uri())
        except Exception as exc:
            print(
                f"sqlide: language server {command[0]!r} failed: {exc}",
                file=sys.stderr,
            )
            shutil.rmtree(workdir, ignore_errors=True)
            return None
        self._workdirs.append(workdir)
        return LspServer(client, document.as_uri())


manager = LspManager()
