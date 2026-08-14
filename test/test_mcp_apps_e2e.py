"""End-to-end MCP Apps test against a REAL stdio child process.

Unlike ``test_mcp_gateway_apps_spool.py`` (mock stdin/stdout), these tests
spawn ``test/fake_mcp_app_server.py`` as an actual subprocess and run the real
``Backend`` stdout pump against it, proving the whole Milestone 1 pipeline on
a live pipe pair:

    initialize (ui capability injected — observed by the SERVER)
      -> tools/call draw
      -> result carries _meta.ui.resourceUri
      -> gateway-originated resources/read answered by the real server
      -> spool file written
      -> marker appended to the delivered result
      -> mcp_apps_render.load_spool round-trips the payload

Plus the two guard rails: flag-off is a byte-identical no-op, and a broken
``resources/read`` still delivers the original (un-marked) result promptly.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

from kiro_crew.mcp_apps_render import find_marker, load_spool
from kiro_crew.mcp_caller import CallerContext
from kiro_crew.mcp_gateway import apps
from kiro_crew.mcp_gateway.backend import (
    MCP_APPS_ENV_FLAG,
    Backend,
)
from kiro_crew.mcp_gateway.pool import PoolKey

SERVER_PATH = Path(__file__).parent / "fake_mcp_app_server.py"

pytestmark = pytest.mark.asyncio


@pytest.fixture
def spool_tmp(tmp_path, monkeypatch):
    """Isolated spool dir, seen by BOTH the gateway writer and the dashboard
    reader (they share the same env override)."""
    d = tmp_path / "mcp-apps"
    monkeypatch.setenv(apps.SPOOL_ENV, str(d))
    return d


@pytest.fixture
def apps_flag_on(monkeypatch):
    monkeypatch.setenv(MCP_APPS_ENV_FLAG, "1")


def _pool_key() -> PoolKey:
    return PoolKey(
        server_name="fake-mcp-app",
        agent_name="test-agent",
        command_args_hash="abc123",
        effective_env_hash="def456",
        work_dir="/tmp/test",
        binary_version="1.0",
        os_uid=1000,
        sandbox_mode="none",
        autoapprove_set_hash="ghi789",
        approval_mode="reads",
        trust_all_tools=False,
        user_identity="testuser",
        config_snapshot_hash="jkl012",
    )


class _LiveServer:
    """A real fake-MCP-App subprocess wired into a real Backend + stdout pump."""

    def __init__(self, backend: Backend, process, pump: asyncio.Task):
        self.backend = backend
        self.process = process
        self.pump = pump

    async def aclose(self) -> None:
        self.pump.cancel()
        try:
            await self.pump
        except (asyncio.CancelledError, Exception):
            pass
        if self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()


async def _spawn_live_server(*server_args: str) -> _LiveServer:
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(SERVER_PATH), *server_args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    now = time.monotonic()
    backend = Backend(
        pool_key=_pool_key(),
        process=process,
        stdin=process.stdin,
        stdout=process.stdout,
        created_at=now,
        last_used_at=now,
    )
    pump = asyncio.get_running_loop().create_task(backend.run_stdout_pump())
    return _LiveServer(backend, process, pump)


async def _recv(inbox: "asyncio.Queue[bytes]", timeout: float = 10.0) -> dict:
    data = await asyncio.wait_for(inbox.get(), timeout=timeout)
    return json.loads(data.decode("utf-8"))


_INIT_FRAME = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "kiro-cli", "version": "0.0.0"},
    },
}

_DRAW_FRAME = {
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/call",
    "params": {"name": "draw", "arguments": {}},
}


async def _handshake_and_draw(live: _LiveServer) -> dict:
    """Drive initialize + tools/call draw through the stub seam; return the
    delivered tools/call response."""
    inbox = await live.backend.attach_stub("s1")

    await live.backend.forward_from_stub("s1", dict(_INIT_FRAME))
    init = await _recv(inbox)
    assert init["id"] == 1
    assert init["result"]["serverInfo"]["name"] == "fake-mcp-app"

    caller = CallerContext(session_key="dashboard:sess-e2e")
    await live.backend.forward_from_stub("s1", dict(_DRAW_FRAME), caller=caller)
    reply = await _recv(inbox)
    assert reply["id"] == 2
    return reply


async def test_full_pipeline_marker_and_spool(apps_flag_on, spool_tmp):
    """Flag on: the SERVER observes the injected ui capability; the delivered
    draw result is marked; the spool file round-trips through load_spool."""
    live = await _spawn_live_server()
    try:
        reply = await _handshake_and_draw(live)

        text = reply["result"]["content"][0]["text"]
        # The real server saw capabilities.extensions[io.modelcontextprotocol/ui]
        # on ITS side of the pipe — injection proven end-to-end.
        assert "ui-ext-seen=true" in text, text

        # Marker appended by the gateway after a real resources/read round-trip.
        spool_id = find_marker(text)
        assert spool_id is not None, f"no marker in delivered text: {text!r}"

        # Spool file exists and round-trips via the dashboard-side reader.
        record = load_spool(spool_id)
        assert record is not None
        assert record["html"] == "<!DOCTYPE html><html><body>fake app</body></html>"
        assert record["csp"] == {"resourceDomains": ["https://esm.sh"]}
        assert record["server"] == "fake-mcp-app"
        assert record["tool"] == "draw"
        assert record["session_key"] == "dashboard:sess-e2e"
        assert record["structured_content"] == {"shapes": 1}

        # Structured content preserved on the wire result too.
        assert reply["result"]["structuredContent"] == {"shapes": 1}
    finally:
        await live.aclose()


async def test_flag_off_is_a_noop(spool_tmp, monkeypatch):
    """Kill-switch set: no capability injection (server sees no ui extension),
    no marker, no spool file — byte-identical passthrough. The gate defaults to
    ON, so disabling requires an explicit falsy value."""
    monkeypatch.setenv(MCP_APPS_ENV_FLAG, "0")
    live = await _spawn_live_server()
    try:
        reply = await _handshake_and_draw(live)

        text = reply["result"]["content"][0]["text"]
        assert "ui-ext-seen=false" in text, text
        assert find_marker(text) is None
        # The ui _meta passes through untouched for a non-Apps-aware client.
        assert reply["result"]["_meta"]["ui"]["resourceUri"] == "ui://fake/app.html"
        assert not spool_tmp.exists() or not list(spool_tmp.glob("*.json"))
    finally:
        await live.aclose()


async def test_broken_resources_read_delivers_original(apps_flag_on, spool_tmp):
    """resources/read erroring on the REAL server must not wedge the call:
    the original (un-marked) result is still delivered, and nothing spools."""
    live = await _spawn_live_server("--break-resources")
    try:
        reply = await _handshake_and_draw(live)

        text = reply["result"]["content"][0]["text"]
        # Injection still happened (flag on)...
        assert "ui-ext-seen=true" in text, text
        # ...but the failed fetch means no marker and no spool file.
        assert find_marker(text) is None
        assert not spool_tmp.exists() or not list(spool_tmp.glob("*.json"))
    finally:
        await live.aclose()


async def test_declared_only_server_still_intercepted(apps_flag_on, spool_tmp):
    """Real-world shape (pdf-server, Excalidraw): the ui:// association is
    declared ONLY on the tool definition in tools/list; the call result has
    no _meta.ui. The gateway must harvest the declaration and still spool."""
    live = await _spawn_live_server("--declared-only")
    try:
        inbox = await live.backend.attach_stub("s1")
        await live.backend.forward_from_stub("s1", dict(_INIT_FRAME))
        await _recv(inbox)

        # kiro-cli always lists tools after initialize — replicate that.
        await live.backend.forward_from_stub(
            "s1", {"jsonrpc": "2.0", "id": 5, "method": "tools/list", "params": {}}
        )
        listed = await _recv(inbox)
        # Coherence check: the declared-only server still DECLARES the uri in the listing…
        draw_meta = {t["name"]: t.get("_meta") for t in listed["result"]["tools"]}["draw"]
        assert draw_meta["ui"]["resourceUri"] == "ui://fake/app.html"

        caller = CallerContext(session_key="dashboard:sess-declared")
        await live.backend.forward_from_stub("s1", dict(_DRAW_FRAME), caller=caller)
        reply = await _recv(inbox)

        result = reply["result"]
        # …and really omits the result-side association.
        assert "ui" not in (result.get("_meta") or {})
        text = result["content"][0]["text"]
        spool_id = find_marker(text)
        assert spool_id is not None, f"declared-uri fallback did not fire: {text!r}"
        record = load_spool(spool_id)
        assert record is not None and record["tool"] == "draw"
    finally:
        await live.aclose()


async def test_declared_only_without_tools_list_no_interception(apps_flag_on, spool_tmp):
    """No tools/list seen -> no declaration harvested -> original delivery
    (documents the fallback's precondition rather than guessing)."""
    live = await _spawn_live_server("--declared-only")
    try:
        reply = await _handshake_and_draw(live)
        assert find_marker(reply["result"]["content"][0]["text"]) is None
        assert not spool_tmp.exists() or not list(spool_tmp.glob("*.json"))
    finally:
        await live.aclose()


async def test_app_only_tool_withheld_from_agent_listing(apps_flag_on, spool_tmp):
    """The agent's tools/list omits save_state (visibility ["app"]) but keeps
    draw, while the app-call path's own listing still sees both.

    Both halves matter and they pull in opposite directions: the model must not
    be offered an app-only tool (SEP-1865 MUST), and app_call's authorization
    snapshot must still contain it or every app-only call would be refused as
    non-existent.
    """
    live = await _spawn_live_server()
    try:
        inbox = await live.backend.attach_stub("s1")
        await live.backend.forward_from_stub("s1", dict(_INIT_FRAME))
        await _recv(inbox)  # initialize response

        await live.backend.forward_from_stub(
            "s1", {"jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": {}}
        )
        reply = await _recv(inbox)
        tools = {t["name"]: t for t in reply["result"]["tools"]}
        assert tools["draw"]["_meta"]["ui"]["visibility"] == ["model", "app"]
        assert "save_state" not in tools

        # The app-call stub prefix is what exempts a listing from the filter.
        app_inbox = await live.backend.attach_stub("__app_call__e2e0")
        await live.backend.forward_from_stub(
            "__app_call__e2e0",
            {"jsonrpc": "2.0", "id": 8, "method": "tools/list", "params": {}},
        )
        app_reply = await _recv(app_inbox)
        app_tools = {t["name"]: t for t in app_reply["result"]["tools"]}
        assert app_tools["save_state"]["_meta"]["ui"]["visibility"] == ["app"]
        assert "draw" in app_tools
    finally:
        await live.aclose()
