"""A claude session whose agent spec declares ``"tools": []`` mounts no tool at all.

claude's routing is not enforced by this core, and an allow rule in the user's or the
project's settings runs a call unasked, so ``_deny_zero_tools`` alone cannot hold the
ban there. The session is therefore started with nothing to call: ``tools`` empty (no
built-in), ``strictMcpConfig`` (no user, project or plugin server), bypass off and an
empty wire array, on an adapter verified to honour those options. The live capture
``acp_frames/claude/zero-tools-refused-live.jsonl`` is the evidence that the adapter
reads an empty ``tools`` as "none" rather than "unset".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from acp_session_mcp_helpers import agents_dir_fixture  # noqa: F401
from acp_session_mcp_helpers import _write_spec

from kiro_crew.acp import client as client_mod
from kiro_crew.acp import client_claude_session
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.knowledge.llm_pool import _get_acp_backend

_CAPTURE = (
    Path(__file__).parent / "fixtures" / "acp_frames" / "claude" / "zero-tools-refused-live.jsonl"
)

_ZERO_TOOL_OPTIONS = {
    "tools": [],
    "strictMcpConfig": True,
    "allowDangerouslySkipPermissions": False,
}


async def _session_new_params(client: AcpClient, tmp_path: Path, monkeypatch) -> list[dict]:
    """Drive the real ``session/new`` call site and return every params it sent."""
    sent: list[dict] = []

    async def _work_dir():
        return str(tmp_path)

    async def _send(_method, params):
        sent.append(params)
        return 1

    async def _wait(_rid, **_kw):
        return {"sessionId": "s-1"}

    monkeypatch.setattr(client, "_session_work_dir", _work_dir)
    monkeypatch.setattr(client, "_send_request", _send)
    monkeypatch.setattr(client, "_wait_for_response", _wait)
    await client._new_session_following_substitution()
    return sent


def _pin_harness(client: AcpClient, monkeypatch, version: str):
    """Record what the pin sends and kills, with *version* as the handshake's answer."""
    sent: list[tuple[str, dict]] = []
    killed: list[bool] = []

    async def _send(method, params):
        sent.append((method, params))
        return len(sent)

    async def _wait(_rid, **_kw):
        return {}

    async def _kill(*, force=False):
        killed.append(force)

    monkeypatch.setattr(client, "_send_request", _send)
    monkeypatch.setattr(client, "_wait_for_response", _wait)
    monkeypatch.setattr(client, "_kill_process", _kill)
    client._agent_version_read = True
    client._agent_version = version
    return sent, killed


@pytest.mark.asyncio
@pytest.mark.parametrize("servers", [{}, {"foo": {"command": "/bin/foo"}}])
async def test_a_zero_tool_session_is_sent_no_tool(tmp_path, agents_dir, monkeypatch, servers):
    """No built-in, no harness-owned server, bypass off and an empty wire array.

    A server the spec declares but its empty ``tools`` does not reference is
    withheld by the mirror, so it never reaches the wire either.
    """
    _write_spec(agents_dir, servers=servers, tools=[])
    client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
    [params] = await _session_new_params(client, tmp_path, monkeypatch)
    assert client.spec_zero_tools is True
    assert params["mcpServers"] == []
    assert params["_meta"] == {"claudeCode": {"options": _ZERO_TOOL_OPTIONS}}


@pytest.mark.asyncio
async def test_a_session_with_tools_keeps_its_envelope(tmp_path, agents_dir, monkeypatch):
    """Only a spec that declares the empty list changes the envelope."""
    _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
    client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
    [params] = await _session_new_params(client, tmp_path, monkeypatch)
    assert client.spec_zero_tools is False
    assert "tools" not in params["_meta"]["claudeCode"]["options"]


@pytest.mark.asyncio
async def test_a_zero_tool_session_composed_with_a_server_is_not_started(
    tmp_path, agents_dir, monkeypatch
):
    """The wire array is the one channel left that could mount a tool."""
    _write_spec(agents_dir, servers={}, tools=[])
    client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
    monkeypatch.setattr(
        client, "_pooled_mcp_servers", lambda: [{"name": "stray", "command": "/bin/stray"}]
    )
    with pytest.raises(client_mod.AcpError, match="1 MCP server"):
        await _session_new_params(client, tmp_path, monkeypatch)


def test_the_wire_guard_reads_only_a_zero_tool_session():
    refuse = client_claude_session.refuse_claude_zero_tool_mounts
    refuse(True, [])
    refuse(False, [{"name": "foo"}])
    with pytest.raises(client_mod.AcpError):
        refuse(True, [{"name": "foo"}])


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["0.83.9", ""])
async def test_a_zero_tool_session_on_an_unverified_adapter_is_stopped(
    tmp_path, monkeypatch, version
):
    """Below the options floor, or with no version, ``tools: []`` may be ignored."""
    client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
    client._spec_zero_tools = True
    sent, killed = _pin_harness(client, monkeypatch, version)
    with pytest.raises(client_mod.AcpError, match="not verified to honour"):
        await client._pin_claude_starting_mode(
            {"sessionId": "s-1", "modes": {"currentModeId": "auto"}}
        )
    assert killed == [True]
    assert sent == []


@pytest.mark.asyncio
async def test_a_zero_tool_session_on_a_verified_adapter_is_not_mode_pinned(tmp_path, monkeypatch):
    """It mounts no tool, so even an approving mode has nothing to approve."""
    client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
    client._spec_zero_tools = True
    sent, killed = _pin_harness(client, monkeypatch, "0.93.0")
    await client._pin_claude_starting_mode({"sessionId": "s-1", "modes": {"currentModeId": "auto"}})
    assert sent == []
    assert killed == []


def _capture() -> tuple[dict, list[dict]]:
    rows = [json.loads(line) for line in _CAPTURE.read_text(encoding="utf-8").splitlines()]
    return rows[0]["_meta"], rows[1:]


def test_the_capture_was_recorded_under_the_shipped_options(tmp_path):
    """The recorded envelope is the one ``_claude_session_meta`` builds today."""
    meta, _ = _capture()
    client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
    client._spec_zero_tools = True
    assert meta["options"] == client._claude_session_meta()["claudeCode"]["options"]
    assert {"Bash", "Write", "WebFetch"} <= set(meta["allow"])


def test_the_captured_session_made_no_call_and_wrote_nothing():
    """Asked for Bash, Write and WebFetch under allow rules: no call, nothing on disk."""
    meta, frames = _capture()
    updates = [f["params"]["update"]["sessionUpdate"] for f in frames if "method" in f]
    assert "tool_call" not in updates and "tool_call_update" not in updates
    assert not [f for f in frames if f.get("method") == "session/request_permission"]
    text = "".join(
        f["params"]["update"]["content"]["text"]
        for f in frames
        if f.get("method") == "session/update"
        and f["params"]["update"]["sessionUpdate"] == "agent_message_chunk"
    )
    assert text.count("NO_TOOL") == 3
    assert meta["files_after"] == [".claude"]
    assert frames[-1]["result"] == {"stopReason": "end_turn"}


def test_claude_honours_the_zero_tool_ban_and_the_knowledge_pool_keeps_it():
    assert capabilities_for(ACP_BACKEND_CLAUDE).honors_zero_tool_ban is True
    assert _get_acp_backend({"agent": {"acp_backend": ACP_BACKEND_CLAUDE}}) == ACP_BACKEND_CLAUDE
