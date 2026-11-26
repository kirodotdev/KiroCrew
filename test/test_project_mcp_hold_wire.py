"""A held project server reaches the wire as a server that starts nothing.

The shared runtime adds it to the ``session/new`` and ``session/load`` arrays
(``agent_sdk.project_mcp_holds.hold_project_servers``), where it outranks the same-named
entry of the folder's ``.kiro/settings/mcp.json``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from test_acp_runtime import _start_reader, _stop_reader, kas_readiness_wire  # noqa: F401

from kiro_crew.acp.harness import SessionExtras
from kiro_crew.acp.types import METHOD_SESSION_LOAD, METHOD_SESSION_NEW
from kiro_crew.agent_sdk import project_mcp_holds
from kiro_crew.mcp_gateway.hashing import STUB_FLAGS_FLAG, decode_target_args
from kiro_crew.project_mcp_approvals import FolderCheck, ProjectServer
from kiro_crew.project_mcp_files import KIRO_PROJECT_MCP_FILE


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True], ids=["new", "load"])
async def test_a_held_project_server_is_sent_as_one_that_starts_nothing(
    kas_readiness_wire, monkeypatch, tmp_path, resume  # noqa: F811
):
    import kiro_crew.acp.runtime as runtime_mod

    wire = kas_readiness_wire
    rt = wire.runtime
    # An opted-in agent: its stamp does not exclude the file's servers.
    monkeypatch.setattr(
        rt,
        "_kas_custom_agents",
        AsyncMock(return_value=SessionExtras(custom_agents=[{"id": "worker", "tools": []}])),
    )
    held = ProjectServer(str(tmp_path), KIRO_PROJECT_MCP_FILE, "planted", {}, "0" * 64)
    monkeypatch.setattr(project_mcp_holds, "project_hold", lambda *a, **k: FolderCheck([held]))
    servers = [{"name": "kirocrew-dashboard"}]
    kwargs = {"cwd": tmp_path, "agent": "worker", "session_key": ""}
    if resume:
        monkeypatch.setattr(runtime_mod, "pooled_session_servers", lambda *_, **_kw: servers)
        start = rt.load_session("", "ready-session", **kwargs)
    else:
        start = rt.create_session(mcp_servers=servers, **kwargs)
    reader_task = await _start_reader(rt)
    task = asyncio.create_task(start)
    try:
        request = await wire.take(wire.sent)
        assert request["method"] == (METHOD_SESSION_LOAD if resume else METHOD_SESSION_NEW)
        by_name = {entry["name"]: entry for entry in request["params"]["mcpServers"]}
        assert "kirocrew-dashboard" in by_name
        [packed] = [a for a in by_name["planted"]["args"] if a.startswith(STUB_FLAGS_FLAG)]
        assert "--refuse" in decode_target_args(packed.split("=", 1)[1])
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await _stop_reader(reader_task)
