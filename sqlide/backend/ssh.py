"""SSH tunnels for server connections.

SshTunnel opens a local port that forwards to the database host as
seen from the SSH server. Two implementations, picked at start():

- the `sshtunnel` package (optional dependency, `sqlide[ssh]`) when
  importable — supports password and key authentication;
- otherwise the system `ssh` binary in BatchMode (key/agent
  authentication only, since no prompt can be answered).

Adapters call start() before connecting and point the driver at
("127.0.0.1", local_port); stop() must run on close. Failures raise
ConnectorError with a readable message.
"""

from __future__ import annotations

import importlib.util
import socket
import subprocess
import time

from sqlide.backend.db.base import ConnectorError

_START_TIMEOUT = 15.0  # seconds to wait for the forward to accept


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class SshTunnel:
    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        key_path: str,
        remote_host: str,
        remote_port: int,
    ) -> None:
        if not host:
            raise ConnectorError("SSH tunnel enabled but no SSH host set")
        self.host = host
        self.port = port or 22
        self.user = user
        self.password = password
        self.key_path = key_path
        self.remote_host = remote_host
        self.remote_port = remote_port
        self.local_port = 0
        self._forwarder = None  # sshtunnel.SSHTunnelForwarder
        self._process: subprocess.Popen | None = None

    def start(self) -> int:
        """Open the tunnel and return the local port to connect to."""
        if importlib.util.find_spec("sshtunnel") is not None:
            self._start_sshtunnel()
        else:
            self._start_subprocess()
        return self.local_port

    def stop(self) -> None:
        if self._forwarder is not None:
            try:
                self._forwarder.stop()
            except Exception:
                pass
            self._forwarder = None
        if self._process is not None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None

    def _start_sshtunnel(self) -> None:
        import sshtunnel

        kwargs: dict = {
            "ssh_username": self.user or None,
            "remote_bind_address": (self.remote_host, self.remote_port),
            "local_bind_address": ("127.0.0.1", 0),
        }
        if self.password:
            kwargs["ssh_password"] = self.password
        if self.key_path:
            kwargs["ssh_pkey"] = self.key_path
        try:
            forwarder = sshtunnel.SSHTunnelForwarder(
                (self.host, self.port), **kwargs
            )
            forwarder.start()
        except Exception as exc:
            raise ConnectorError(f"SSH tunnel failed: {exc}") from exc
        self._forwarder = forwarder
        self.local_port = forwarder.local_bind_port

    def _start_subprocess(self) -> None:
        if self.password:
            raise ConnectorError(
                "SSH password authentication needs the sshtunnel package "
                "(pip install 'sqlide[ssh]'); the system ssh fallback "
                "only supports key/agent authentication."
            )
        self.local_port = _free_port()
        command = [
            "ssh",
            "-N",
            "-o", "BatchMode=yes",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ConnectTimeout=10",
            "-L",
            f"127.0.0.1:{self.local_port}:{self.remote_host}:{self.remote_port}",
            "-p", str(self.port),
        ]
        if self.key_path:
            command += ["-i", self.key_path]
        command.append(f"{self.user}@{self.host}" if self.user else self.host)
        try:
            self._process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError:
            raise ConnectorError(
                "No `ssh` binary found and the sshtunnel package is not "
                "installed (pip install 'sqlide[ssh]')."
            ) from None
        self._wait_for_forward()

    def _wait_for_forward(self) -> None:
        """Poll the local port until ssh has the forward listening, or
        surface ssh's stderr if it exits first."""
        deadline = time.monotonic() + _START_TIMEOUT
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                stderr = (self._process.stderr.read() or b"").decode(
                    errors="replace"
                )
                self._process = None
                raise ConnectorError(
                    "SSH tunnel failed: " + (stderr.strip() or "ssh exited")
                )
            try:
                with socket.create_connection(
                    ("127.0.0.1", self.local_port), timeout=0.5
                ):
                    return
            except OSError:
                time.sleep(0.2)
        self.stop()
        raise ConnectorError("SSH tunnel timed out while starting")
