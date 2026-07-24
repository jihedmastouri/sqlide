"""Integration tests for McpInstance: speak MCP over real HTTP against
an instance serving a temp-file SQLite database (no GTK involved).

Each test drives the official `mcp` SDK's streamable-HTTP client, so
these exercise the same wire protocol a real MCP client would use.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3

import pytest

pytest.importorskip("mcp", reason="mcp SDK not installed")
pytest.importorskip("uvicorn", reason="uvicorn not installed")

from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamablehttp_client  # noqa: E402

from sqlide.backend.connections import ConnectionProfile  # noqa: E402
from sqlide.backend.mcp.server import (  # noqa: E402
    McpConfig,
    McpError,
    McpInstance,
    client_config_json,
    generate_token,
)


@pytest.fixture()
def demo_db(tmp_path):
    path = tmp_path / "mcp_demo.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);"
        "INSERT INTO users VALUES (1, 'ada'), (2, 'brian');"
    )
    conn.commit()
    conn.close()
    return str(path)


@pytest.fixture()
def profile(demo_db):
    return ConnectionProfile(name="demo", kind="sqlite", file_path=demo_db)


async def _call(url: str, token: str, method: str, **kwargs):
    """One MCP round trip: initialize, call `method`, close.

    Errors are captured and re-raised only after the client's task
    group has torn down cleanly — raising while still inside the
    streamablehttp_client/ClientSession context managers turns into an
    ExceptionGroup from the SDK's own cleanup racing the raise.
    """
    headers = {"Authorization": f"Bearer {token}"} if token else None
    error: str | None = None
    value = None
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            if method == "tools/list":
                result = await session.list_tools()
                value = [t.name for t in result.tools]
            else:
                result = await session.call_tool(method, kwargs)
                if result.isError:
                    error = result.content[0].text if result.content else ""
                elif result.structuredContent is not None:
                    # FastMCP wraps a bare list return as {"result": [...]}
                    # and splits it into one content block per item; the
                    # structured form carries the whole value in one go.
                    structured = result.structuredContent
                    value = (
                        structured["result"]
                        if list(structured) == ["result"]
                        else structured
                    )
                else:
                    value = json.loads(result.content[0].text)
    if error is not None:
        raise RuntimeError(error)
    return value


def _run(coro):
    return asyncio.run(coro)


def test_start_returns_loopback_url_and_lists_tools(profile):
    instance = McpInstance(McpConfig(profiles=[profile], port=0))
    try:
        url = instance.start()
        assert url.startswith("http://127.0.0.1:")
        tools = _run(_call(url, "", "tools/list"))
        assert set(tools) >= {
            "list_connections", "list_tables", "list_columns", "get_ddl",
            "query",
        }
    finally:
        instance.stop()
    assert not instance.running


def test_list_tables_and_columns(profile):
    instance = McpInstance(McpConfig(profiles=[profile]))
    try:
        url = instance.start()
        tables = _run(_call(url, "", "list_tables", connection="demo"))
        assert any(
            t["name"] == "users" and t["kind"] == "table" for t in tables
        )
        columns = _run(
            _call(url, "", "list_columns", connection="demo", table="users")
        )
        names = {c["name"] for c in columns}
        assert names == {"id", "name"}
    finally:
        instance.stop()


def test_query_returns_rows(profile):
    instance = McpInstance(McpConfig(profiles=[profile]))
    try:
        url = instance.start()
        result = _run(_call(
            url, "", "query", connection="demo",
            sql="SELECT id, name FROM users ORDER BY id",
        ))
        assert result["columns"] == ["id", "name"]
        assert result["rows"] == [[1, "ada"], [2, "brian"]]
        assert result["truncated"] is False
    finally:
        instance.stop()


def test_query_row_limit_truncates(profile):
    instance = McpInstance(McpConfig(profiles=[profile], row_limit=1))
    try:
        url = instance.start()
        result = _run(_call(
            url, "", "query", connection="demo", sql="SELECT * FROM users"
        ))
        assert len(result["rows"]) == 1
        assert result["truncated"] is True
    finally:
        instance.stop()


def test_query_disabled_hides_tool(profile):
    instance = McpInstance(McpConfig(profiles=[profile], allow_query=False))
    try:
        url = instance.start()
        tools = _run(_call(url, "", "tools/list"))
        assert "query" not in tools
        assert "list_tables" in tools
    finally:
        instance.stop()


def test_write_statement_rejected_by_guard(profile):
    instance = McpInstance(McpConfig(profiles=[profile]))
    try:
        url = instance.start()
        with pytest.raises(RuntimeError, match="read-only"):
            _run(_call(
                url, "", "query", connection="demo",
                sql="INSERT INTO users VALUES (3, 'carol')",
            ))
        # The guard's rejection must not have let anything through.
        result = _run(_call(
            url, "", "query", connection="demo",
            sql="SELECT count(*) AS n FROM users",
        ))
        assert result["rows"] == [[2]]
    finally:
        instance.stop()


def test_driver_level_read_only_blocks_writes_even_bypassing_guard(profile):
    """Belt and braces: even a write statement that somehow reached
    execute() would be rejected by the read-only connection, not just
    the guard. Exercised by calling the connector directly (the guard
    already blocks the MCP path in the test above)."""
    instance = McpInstance(McpConfig(profiles=[profile]))
    try:
        instance.start()
        connector = instance._connectors["demo"]
        from sqlide.backend.db.base import ConnectorError

        with pytest.raises(ConnectorError):
            connector.execute("INSERT INTO users VALUES (3, 'carol')")
    finally:
        instance.stop()


def test_wrong_token_returns_401(profile):
    token = generate_token()
    instance = McpInstance(McpConfig(profiles=[profile], token=token))
    try:
        url = instance.start()
        with pytest.raises(Exception):
            _run(_call(url, "wrong-token", "tools/list"))
        # The right token still works.
        tools = _run(_call(url, token, "tools/list"))
        assert "list_tables" in tools
    finally:
        instance.stop()


def test_two_instances_run_side_by_side(profile, tmp_path, demo_db):
    other_db = tmp_path / "other.db"
    shutil.copy(demo_db, other_db)
    conn = sqlite3.connect(other_db)
    conn.execute("UPDATE users SET name = 'zed' WHERE id = 1")
    conn.commit()
    conn.close()
    other_profile = ConnectionProfile(
        name="other", kind="sqlite", file_path=str(other_db)
    )

    one = McpInstance(McpConfig(profiles=[profile]))
    two = McpInstance(McpConfig(profiles=[other_profile]))
    try:
        url_one = one.start()
        url_two = two.start()
        assert url_one != url_two
        rows_one = _run(_call(
            url_one, "", "query", connection="demo",
            sql="SELECT name FROM users WHERE id = 1",
        ))
        rows_two = _run(_call(
            url_two, "", "query", connection="other",
            sql="SELECT name FROM users WHERE id = 1",
        ))
        assert rows_one["rows"] == [["ada"]]
        assert rows_two["rows"] == [["zed"]]
    finally:
        one.stop()
        two.stop()


def test_refuses_nonloopback_bind_without_token(profile):
    instance = McpInstance(McpConfig(profiles=[profile], bind_host="0.0.0.0"))
    with pytest.raises(McpError, match="loopback"):
        instance.start()
    assert not instance.running


def test_start_without_profiles_raises(profile):
    instance = McpInstance(McpConfig(profiles=[]))
    with pytest.raises(McpError, match="connection"):
        instance.start()


def test_client_config_json_shape():
    text = client_config_json(
        "sqlide-demo", "http://127.0.0.1:1234/mcp", token="tok"
    )
    parsed = json.loads(text)
    entry = parsed["mcpServers"]["sqlide-demo"]
    assert entry["url"] == "http://127.0.0.1:1234/mcp"
    assert entry["headers"]["Authorization"] == "Bearer tok"


def test_client_config_json_no_token_omits_headers():
    text = client_config_json("sqlide-demo", "http://127.0.0.1:1234/mcp")
    parsed = json.loads(text)
    assert "headers" not in parsed["mcpServers"]["sqlide-demo"]
