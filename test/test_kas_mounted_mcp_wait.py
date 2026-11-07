"""Session start waits, best effort, for mounted servers outside the required barrier.

A Connection bridge (``github``) or a user server reports after Crew's own
servers. Without this wait the first prompt went out 1.4-1.9 s before the
bridge's ``tools/list`` answered, so turn 1 had none of its tools. The wait is
bounded by one shared cap and never fails the start; the required barrier is
unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import time

import pytest

from kiro_crew.acp import runtime_session_load
from kiro_crew.acp.mcp_session_report import McpSessionReport
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.session_handle_runtime_contract import AcpRuntimeDead, AcpRuntimeError
from kiro_crew.acp.types import ACP_BACKEND_KAS, METHOD_KAS_MCP_STATUS, JsonRpcMessage
from kiro_crew.mcp_cleanup import KIROCREW_BIN_MCP_SERVERS

_SID = "sess-mounted"
_CORE = KIROCREW_BIN_MCP_SERVERS[0]
_CLIENT = {"kiro": {"resource": {"source": {"origin": "client"}}}}
# Resumed shape, so a failure only unregisters (no backend round trip).
_PARAMS = {"sessionId": _SID, "mcpServers": [{"name": _CORE}, {"name": "github"}]}


def _status(**states: str) -> JsonRpcMessage:
    servers = []
    for name, status in states.items():
        entry = {"name": name, "status": status, "_meta": _CLIENT}
        if status == "connected":
            entry["tools"] = [{"name": "ping", "description": "probe", "disabled": False}]
        servers.append(entry)
    return JsonRpcMessage(
        method=METHOD_KAS_MCP_STATUS, params={"sessionId": _SID, "servers": servers}
    )


def _handle(*frames: JsonRpcMessage) -> AcpSessionHandle:
    handle = AcpSessionHandle.__new__(AcpSessionHandle)
    handle._session_id = _SID
    handle._mcp_report = McpSessionReport()
    handle._mcp_report.begin_session([])
    handle._queue = asyncio.Queue()
    for frame in frames:
        handle._queue.put_nowait(frame)
    handle._apply_init_notification = lambda *_a, **_k: None  # type: ignore[method-assign]
    return handle


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_session_load, "_declared_governed", lambda: False)
    return AcpRuntime(work_dir=tmp_path / "ws", sandbox_mode="off", acp_backend=ACP_BACKEND_KAS)


@pytest.mark.asyncio
async def test_a_slow_connection_is_waited_for_before_turn_one(runtime):
    handle = _handle(_status(**{_CORE: "connected", "github": "connecting"}))
    asyncio.get_running_loop().call_later(
        0.3, handle._queue.put_nowait, _status(**{_CORE: "connected", "github": "connected"})
    )
    start = time.monotonic()
    await runtime._wait_managed_mcp(handle, _PARAMS, "default", 5.0, 0)
    assert time.monotonic() - start >= 0.3
    assert handle._mcp_report.unreported(("github",)) == ()


@pytest.mark.asyncio
async def test_a_server_ready_during_the_required_wait_costs_nothing(runtime, monkeypatch):
    # Its frame was consumed by the required barrier; the report still has it.
    monkeypatch.setattr(runtime_session_load, "MOUNTED_MCP_WAIT_SECS", 5.0)
    handle = _handle(
        _status(**{_CORE: "connecting", "github": "connected"}),
        _status(**{_CORE: "connected", "github": "connected"}),
    )
    start = time.monotonic()
    await runtime._wait_managed_mcp(handle, _PARAMS, "default", 5.0, 0)
    assert time.monotonic() - start < 1.0


@pytest.mark.asyncio
async def test_a_server_never_ready_costs_at_most_the_cap(runtime, monkeypatch, caplog):
    monkeypatch.setattr(runtime_session_load, "MOUNTED_MCP_WAIT_SECS", 0.2)
    handle = _handle(_status(**{_CORE: "connected", "github": "connecting"}))
    start = time.monotonic()
    with caplog.at_level(logging.INFO):
        await runtime._wait_managed_mcp(handle, _PARAMS, "default", 5.0, 0)
    assert time.monotonic() - start < 1.0
    assert "github" in caplog.text


@pytest.mark.asyncio
async def test_runtime_death_while_waiting_still_raises(runtime):
    handle = _handle(_status(**{_CORE: "connected", "github": "connecting"}), None)
    with pytest.raises(AcpRuntimeDead):
        await runtime._wait_managed_mcp(handle, _PARAMS, "default", 5.0, 0)


@pytest.mark.asyncio
async def test_a_failed_required_server_still_fails_the_start(runtime):
    handle = _handle(_status(**{_CORE: "failed", "github": "connected"}))
    with pytest.raises(AcpRuntimeError, match=_CORE):
        await runtime._wait_managed_mcp(handle, _PARAMS, "default", 5.0, 0)
