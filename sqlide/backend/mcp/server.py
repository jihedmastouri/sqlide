"""One read-only MCP server instance over the official `mcp` SDK.

McpInstance owns everything it needs: its own connectors (created
fresh from the profiles, never shared with the app's windows — that is
what makes instances independent), an HTTP server (streamable-HTTP
transport) on a background thread, and a request log callback. Several
instances run side by side on different ports and share no state; an
instance runs until stop() (the tab closing) shuts it down.

Defense in depth for "read-only":
- each connector is opened read-only at the driver level where the
  dialect can (sqlite mode=ro, postgres default_transaction_read_only,
  mysql SET SESSION TRANSACTION READ ONLY; JDBC has no portable form
  and relies on the guard alone),
- the query tool admits only guard-approved statements (guard.py),
- results are capped at row_limit rows.

The `mcp` SDK (and uvicorn) are an optional extra — installed with
`pip install sqlide[mcp]` — mirroring the optional DB drivers; start()
raises a friendly error when they are missing.

Threading: HTTP handling happens on the server thread's event loop;
connectors are not thread-safe, so all DB work is serialized behind
one lock per instance (the same discipline as the window's
ensure_connector).
"""

from __future__ import annotations

import ipaddress
import json
import secrets
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.db import registry
from sqlide.backend.db.base import Connector, ResultSet
from sqlide.backend.mcp.guard import GuardError, check_read_only


class McpError(Exception):
    """Instance-level failure (config, missing SDK, startup)."""


@dataclass
class McpConfig:
    """Everything one instance needs; the tab's form edits this."""

    profiles: list[ConnectionProfile] = field(default_factory=list)
    bind_host: str = "127.0.0.1"  # or "0.0.0.0" (needs a token)
    port: int = 0  # 0 = pick a free port
    token: str = ""  # "" = no authentication (loopback only)
    row_limit: int = 500
    allow_query: bool = True  # off = catalog-only instance


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def client_config_json(server_name: str, url: str, token: str = "") -> str:
    """The JSON snippet an MCP client (Claude, etc.) needs."""
    entry: dict[str, Any] = {"url": url}
    if token:
        entry["headers"] = {"Authorization": f"Bearer {token}"}
    return json.dumps({"mcpServers": {server_name: entry}}, indent=2)


class McpInstance:
    def __init__(
        self,
        config: McpConfig,
        on_log: Callable[[str], None] | None = None,
    ) -> None:
        self._config = config
        self._on_log = on_log or (lambda line: None)
        self._connectors: dict[str, Connector] = {}
        self._kinds: dict[str, str] = {}  # connection name -> dialect
        self._db_lock = threading.Lock()
        self._server = None  # uvicorn.Server while running
        self._thread: threading.Thread | None = None
        self.url = ""

    @property
    def running(self) -> bool:
        return self._server is not None

    def start(self) -> str:
        """Open the connectors and serve; returns the MCP URL.
        Blocking (call from a worker thread). On any failure nothing
        is left running."""
        config = self._config
        if not config.profiles:
            raise McpError("Select at least one connection to expose")
        if not _is_loopback(config.bind_host) and not config.token:
            raise McpError(
                "Refusing to listen on a non-loopback address without "
                "a bearer token"
            )
        try:
            import uvicorn
            from mcp.server.fastmcp import FastMCP
        except ImportError as exc:
            raise McpError(
                "The MCP server needs the 'mcp' package. "
                "Install with: pip install sqlide[mcp]"
            ) from exc

        self._open_connectors()
        try:
            mcp = FastMCP(
                "sqlide", stateless_http=True, json_response=True
            )
            self._register_tools(mcp)
            app = _BearerAuth(
                mcp.streamable_http_app(), config.token, self._on_log
            )
            port = config.port or _free_port(config.bind_host)
            server = uvicorn.Server(uvicorn.Config(
                app,
                host=config.bind_host,
                port=port,
                log_level="warning",
            ))
            thread = threading.Thread(target=server.run, daemon=True)
            thread.start()
            deadline = time.monotonic() + 10
            while not server.started:
                if not thread.is_alive():
                    raise McpError(
                        f"Server failed to start on "
                        f"{config.bind_host}:{port}"
                    )
                if time.monotonic() > deadline:
                    server.should_exit = True
                    raise McpError("Server did not start within 10s")
                time.sleep(0.05)
        except Exception:
            self._close_connectors()
            raise
        self._server = server
        self._thread = thread
        host = "127.0.0.1" if _is_loopback(config.bind_host) else config.bind_host
        self.url = f"http://{host}:{port}/mcp"
        self._on_log(f"listening on {self.url}")
        return self.url

    def stop(self) -> None:
        """Shut the HTTP server down and close the connectors."""
        server, self._server = self._server, None
        thread, self._thread = self._thread, None
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(timeout=5)
        self._close_connectors()
        if server is not None:
            self._on_log("stopped")

    # Connectors

    def _open_connectors(self) -> None:
        try:
            for profile in self._config.profiles:
                params = profile.connect_params()
                if profile.kind == "sqlite":
                    params["read_only"] = True
                connector = registry.create_connector(profile.kind, **params)
                connector.connect()
                if profile.kind == "postgres":
                    connector.execute(
                        "SET default_transaction_read_only = on"
                    )
                elif profile.kind == "mysql":
                    connector.execute("SET SESSION TRANSACTION READ ONLY")
                self._connectors[profile.name] = connector
                self._kinds[profile.name] = profile.kind
        except Exception:
            self._close_connectors()
            raise

    def _close_connectors(self) -> None:
        for connector in self._connectors.values():
            try:
                connector.close()
            except Exception:
                pass
        self._connectors.clear()
        self._kinds.clear()

    def _connector(self, connection: str) -> Connector:
        connector = self._connectors.get(connection)
        if connector is None:
            raise ValueError(
                f"Unknown connection {connection!r}; "
                f"available: {', '.join(self._connectors) or 'none'}"
            )
        return connector

    def _call(self, tool: str, connection: str, work: Callable[[], Any]) -> Any:
        """Serialize DB work and log the request (tool, connection,
        duration; failures included)."""
        started = time.monotonic()
        try:
            with self._db_lock:
                result = work()
        except Exception as exc:
            self._on_log(f"{tool}({connection}) failed: {exc}")
            raise
        duration = int((time.monotonic() - started) * 1000)
        self._on_log(f"{tool}({connection}) {duration}ms")
        return result

    # Tools

    def _register_tools(self, mcp) -> None:
        instance = self

        @mcp.tool()
        def list_connections() -> list[str]:
            """Names of the database connections this server exposes."""
            instance._on_log("list_connections()")
            return list(instance._connectors)

        @mcp.tool()
        def list_tables(connection: str) -> list[dict]:
            """Tables and views of a connection, as {name, kind} with
            kind "table" or "view"."""
            return instance._call(
                "list_tables", connection,
                lambda: [
                    {"name": t.name, "kind": t.kind}
                    for t in instance._connector(connection).list_tables()
                ],
            )

        @mcp.tool()
        def list_columns(connection: str, table: str) -> list[dict]:
            """Columns of a table or view, as {name, type, nullable,
            primary_key}."""
            return instance._call(
                "list_columns", connection,
                lambda: [
                    {
                        "name": c.name,
                        "type": c.type,
                        "nullable": c.nullable,
                        "primary_key": c.is_pk,
                    }
                    for c in instance._connector(connection).list_columns(
                        table
                    )
                ],
            )

        @mcp.tool()
        def get_ddl(connection: str, name: str) -> str:
            """CREATE statement of a table, view or stored object
            (empty when unknown)."""
            return instance._call(
                "get_ddl", connection,
                lambda: instance._connector(connection).get_ddl(name),
            )

        if not self._config.allow_query:
            return

        @mcp.tool()
        def query(connection: str, sql: str) -> dict:
            """Run one read-only SQL statement (SELECT/WITH/EXPLAIN,
            plus SHOW on MySQL) and return {columns, rows, truncated}.
            Rows are capped at the instance's row limit."""
            connector = instance._connector(connection)
            dialect = instance._kinds[connection]
            try:
                text = check_read_only(sql, dialect)
            except GuardError as exc:
                instance._on_log(f"DENIED query({connection}): {exc}")
                raise ValueError(
                    f"Rejected as not read-only: {exc}"
                ) from exc
            result = instance._call(
                "query", connection, lambda: connector.execute(text)
            )
            limit = instance._config.row_limit
            if isinstance(result, ResultSet):
                rows = result.rows[:limit]
                return {
                    "columns": result.columns,
                    "rows": [[_jsonable(v) for v in row] for row in rows],
                    "truncated": len(result.rows) > limit,
                }
            return {"columns": [], "rows": [], "truncated": False}


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _is_loopback(host: str) -> bool:
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host if host else "127.0.0.1", 0))
        return sock.getsockname()[1]


class _BearerAuth:
    """ASGI wrapper: every request must carry the configured bearer
    token; wrong or missing → 401. A no-op when no token is set."""

    def __init__(
        self, app, token: str, on_log: Callable[[str], None]
    ) -> None:
        self._app = app
        self._expected = f"Bearer {token}".encode() if token else b""
        self._on_log = on_log

    async def __call__(self, scope, receive, send) -> None:
        if self._expected and scope.get("type") == "http":
            headers = {
                key.lower(): value
                for key, value in scope.get("headers", [])
            }
            if headers.get(b"authorization") != self._expected:
                self._on_log("DENIED request: missing or wrong token")
                await send({
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"text/plain"),
                        (b"www-authenticate", b"Bearer"),
                    ],
                })
                await send({
                    "type": "http.response.body",
                    "body": b"unauthorized",
                })
                return
        await self._app(scope, receive, send)
