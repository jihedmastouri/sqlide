"""Minimal Language Server Protocol client over stdio.

Speaks just enough JSON-RPC for completions: initialize/initialized,
textDocument/didOpen and didChange (full sync), textDocument/completion,
shutdown/exit. Server-to-client requests are answered with empty results
so servers that probe for configuration don't stall; notifications
(diagnostics etc.) are ignored for now.

No GTK here. Callers use it from worker threads (see frontend/util
run_async); requests carry timeouts so a wedged server can't hang a
completion worker forever.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from typing import Any


class LspError(Exception):
    pass


class _Pending:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.message: dict | None = None


class LspClient:
    def __init__(
        self,
        command: list[str],
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self._proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=cwd,
            env=env,
        )
        self._write_lock = threading.Lock()
        self._id_lock = threading.Lock()
        self._next_id = 0
        self._pending: dict[int, _Pending] = {}
        threading.Thread(target=self._reader, daemon=True).start()

    @property
    def alive(self) -> bool:
        return self._proc.poll() is None

    def initialize(self, root_uri: str | None = None, timeout: float = 15.0) -> Any:
        result = self.request(
            "initialize",
            {
                "processId": os.getpid(),
                "clientInfo": {"name": "sqlide"},
                "rootUri": root_uri,
                "workspaceFolders": (
                    [{"uri": root_uri, "name": "sqlide"}] if root_uri else None
                ),
                "capabilities": {
                    "textDocument": {
                        "completion": {
                            "completionItem": {"snippetSupport": False}
                        }
                    }
                },
            },
            timeout=timeout,
        )
        self.notify("initialized", {})
        return result

    def request(self, method: str, params: Any, timeout: float = 5.0) -> Any:
        with self._id_lock:
            self._next_id += 1
            request_id = self._next_id
        pending = _Pending()
        self._pending[request_id] = pending
        try:
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params,
                }
            )
            if not pending.event.wait(timeout):
                raise LspError(f"{method} timed out after {timeout:g}s")
        finally:
            self._pending.pop(request_id, None)
        if pending.message is None:
            raise LspError("language server exited")
        if "error" in pending.message:
            error = pending.message["error"]
            raise LspError(f"{method}: {error.get('message', error)}")
        return pending.message.get("result")

    def notify(self, method: str, params: Any) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def shutdown(self) -> None:
        try:
            self.request("shutdown", None, timeout=2.0)
            self.notify("exit", None)
        except LspError:
            pass
        try:
            self._proc.stdin.close()
        except OSError:
            pass
        try:
            self._proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            self._proc.kill()

    # Wire protocol

    def _send(self, message: dict) -> None:
        data = json.dumps(message).encode("utf-8")
        with self._write_lock:
            if self._proc.poll() is not None:
                raise LspError("language server exited")
            try:
                self._proc.stdin.write(
                    b"Content-Length: %d\r\n\r\n" % len(data) + data
                )
                self._proc.stdin.flush()
            except (OSError, ValueError) as exc:
                raise LspError(f"write to language server failed: {exc}")

    def _reader(self) -> None:
        stdout = self._proc.stdout
        try:
            while True:
                length = None
                while True:
                    line = stdout.readline()
                    if not line:
                        return
                    line = line.strip()
                    if not line:
                        break
                    name, _, value = line.partition(b":")
                    if name.lower() == b"content-length":
                        length = int(value)
                if length is None:
                    continue
                body = stdout.read(length)
                if body is None or len(body) < length:
                    return
                self._dispatch(json.loads(body))
        except Exception:
            pass
        finally:
            for pending in list(self._pending.values()):
                pending.event.set()

    def _dispatch(self, message: dict) -> None:
        if "method" in message:
            if "id" in message:  # server -> client request: placate it
                if message["method"] == "workspace/configuration":
                    items = (message.get("params") or {}).get("items") or []
                    result: Any = [None] * len(items)
                else:
                    result = None
                try:
                    self._send(
                        {"jsonrpc": "2.0", "id": message["id"], "result": result}
                    )
                except LspError:
                    pass
            return
        pending = self._pending.pop(message.get("id"), None)
        if pending is not None:
            pending.message = message
            pending.event.set()
