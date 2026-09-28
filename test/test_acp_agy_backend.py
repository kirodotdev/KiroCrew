"""The agy backend: vocabulary, launch record, host auth, and adapter protocol.

Google Antigravity CLI (agy) as an ACP backend in Kiro Crew.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import acp_backends
from kiro_crew.acp.adapters.agy import AgyAcpServer, AgySession
from kiro_crew.acp.types import PROVIDER_LABEL_AGY, PROVIDER_LABEL_BY_BACKEND
from kiro_crew.agent_sdk import backends as sdk_backends
from kiro_crew.agent_sdk import host_auth

AGY = acp_backends.ACP_BACKEND_AGY


def test_agy_is_known_and_unverified() -> None:
    """Known gates the kwarg; unverified routing holds it outside selectable baseline."""
    assert AGY == "agy"
    assert AGY in sdk_backends.ACP_BACKENDS_KNOWN
    assert AGY not in sdk_backends.BASELINE_SELECTABLE_BACKENDS


def test_agy_provider_label() -> None:
    """Label used in logs, cards and UI."""
    assert PROVIDER_LABEL_AGY == "agy"
    assert PROVIDER_LABEL_BY_BACKEND[AGY] == "agy"


def test_agy_routing() -> None:
    """Routing disposition in ACP_BACKEND_ROUTING is UNVERIFIED."""
    assert sdk_backends.routing_for(AGY) is sdk_backends.Routing.UNVERIFIED


def test_agy_launch_record() -> None:
    """SelfServedLaunch definition for agy."""
    launch = sdk_backends.launch_for(AGY)
    assert launch.binary == "agy-acp"
    assert launch.bin_env_var == "AGY_ACP_BIN"
    assert launch.spawn_label == "agy-acp"
    assert launch.install_command == "agy-acp"


def test_agy_host_auth_declaration() -> None:
    """Host auth declaration for agy."""
    decl = host_auth.declaration_for(AGY)
    assert decl.backend == "agy"
    assert decl.entitlement_source == host_auth.ENTITLEMENT_OWN_CREDENTIAL_FILE
    assert decl.credential_leaves == (".gemini/antigravity-cli/cache/onboarding.json",)
    assert decl.adapter_own_leaves == ()
    assert decl.host_logout_retires_children is False


def test_agy_capability_sets() -> None:
    """Verify agy membership across capability sets."""
    assert AGY in sdk_backends.ACP_BACKENDS_LOAD_WITHOUT_MODES
    assert AGY in sdk_backends.ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION
    assert AGY in sdk_backends.ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION
    assert AGY in sdk_backends.ACP_BACKENDS_HARNESS_OWNED_SESSIONS

    # Subtractions: withheld / outside per contract and AI review
    assert AGY not in sdk_backends.ACP_BACKENDS_SESSION_MCP_ARRAY
    assert AGY not in sdk_backends.ACP_BACKENDS_MEMBER_DISPATCH
    assert AGY not in sdk_backends.ACP_BACKENDS_STEER
    assert AGY not in sdk_backends.ACP_BACKENDS_COMPACT
    assert AGY not in sdk_backends.ACP_BACKENDS_INLINE_COMPACTION
    assert AGY not in sdk_backends.ACP_BACKENDS_MARKDOWN_AGENT_SPECS
    assert AGY not in sdk_backends.ACP_BACKENDS_SIDE_READONLY

    # Not in unverified runtime sharing or internal sandbox
    assert AGY not in sdk_backends.ACP_BACKENDS_ACP_RUNTIME
    assert AGY not in sdk_backends.ACP_BACKENDS_INTERNAL_SANDBOX
    assert AGY not in sdk_backends.ACP_BACKENDS_SESSION_SHARING
    assert AGY not in sdk_backends.ACP_BACKENDS_SESSION_EVICTION


@pytest.mark.asyncio
async def test_agy_adapter_initialize_and_session_lifecycle() -> None:
    """Test AgyAcpServer ACP JSON-RPC handling."""
    server = AgyAcpServer()
    written_responses: list[tuple[any, dict]] = []
    server._write_response = lambda req_id, res: written_responses.append((req_id, res))

    # initialize
    await server.dispatch_request(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}}
    )
    assert len(written_responses) == 1
    req_id, init_res = written_responses.pop(0)
    assert req_id == 1
    assert init_res["protocolVersion"] == 1
    assert init_res["agentCapabilities"]["loadSession"] is True
    assert init_res["agentInfo"]["name"] == "agy"

    # session/set_model
    await server.dispatch_request(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "session/set_model",
            "params": {"model": "auto"},
        }
    )
    assert len(written_responses) == 1
    req_id, model_res = written_responses.pop(0)
    assert req_id == 2
    assert model_res == {}
    assert server.default_model == "auto"

    # session/set_config_option
    await server.dispatch_request(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "session/set_config_option",
            "params": {"configId": "model", "value": "gemini-2.5-flash"},
        }
    )
    assert len(written_responses) == 1
    req_id, cfg_res = written_responses.pop(0)
    assert req_id == 3
    assert cfg_res == {}
    assert server.default_model == "gemini-2.5-flash"


@pytest.mark.asyncio
async def test_agy_prompt_cancel() -> None:
    """Verify session/cancel stops the active prompt turn."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.acp.adapters.agy import AgyAcpServer, AgySession

    server = AgyAcpServer()
    responses: list[tuple[any, dict]] = []
    server._write_response = lambda req_id, res: responses.append((req_id, res))

    mock_proc = MagicMock()
    mock_proc.returncode = None
    mock_stdin = MagicMock()
    mock_stdin.drain = AsyncMock()
    mock_proc.stdin = mock_stdin

    async def blocking_readline() -> bytes:
        await asyncio.sleep(10.0)
        return b""

    mock_stdout = MagicMock()
    mock_stdout.readline = AsyncMock(side_effect=blocking_readline)
    mock_proc.stdout = mock_stdout

    session = AgySession(session_id="test_cancel_sess", proc=mock_proc, cwd="/tmp")
    server.sessions["test_cancel_sess"] = session

    prompt_msg = {
        "jsonrpc": "2.0",
        "id": 101,
        "method": "session/prompt",
        "params": {
            "sessionId": "test_cancel_sess",
            "prompt": [{"type": "text", "text": "slow task"}],
        },
    }
    task = asyncio.create_task(server.dispatch_request(prompt_msg))
    session.active_prompt_task = task

    await asyncio.sleep(0.01)

    await server.dispatch_request(
        {
            "jsonrpc": "2.0",
            "method": "session/cancel",
            "params": {"sessionId": "test_cancel_sess"},
        }
    )

    try:
        await task
    except asyncio.CancelledError:
        pass

    assert any(res == (101, {"stopReason": "cancelled"}) for res in responses)


def test_agy_mirror_and_mcp_projection() -> None:
    """Verify AgyMirror is registered and MCP servers are withheld."""
    from kiro_crew.providers.mirrors import MIRRORS, PROJECTIONS, ProjectionKind, mirror_for
    from kiro_crew.providers.mirrors.agy import AgyMirror

    assert AGY in MIRRORS
    assert MIRRORS[AGY] is AgyMirror
    mirror = mirror_for(AGY)
    assert isinstance(mirror, AgyMirror)

    proj = PROJECTIONS[AGY]
    assert proj.kind == ProjectionKind.MIRROR

    params = mirror.session_params(
        None,
        stub_elements=[{"name": "stub1", "command": "python3", "args": []}],
    )
    assert "mcpServers" in params
    assert params["mcpServers"] == []


@pytest.mark.asyncio
async def test_agy_prompt_tool_unwrapping() -> None:
    """Verify call_mcp_tool events are unwrapped to mcp__<server>__<tool>."""
    import json
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.acp.adapters.agy import AgyAcpServer, AgySession

    server = AgyAcpServer()
    notifications: list[tuple[str, dict]] = []
    responses: list[tuple[any, dict]] = []
    server._write_notification = lambda method, params: notifications.append((method, params))
    server._write_response = lambda req_id, res: responses.append((req_id, res))

    mock_proc = MagicMock()
    mock_proc.returncode = None
    mock_stdin = MagicMock()
    mock_stdin.drain = AsyncMock()
    mock_proc.stdin = mock_stdin

    lines = [
        json.dumps(
            {
                "event": "step_update",
                "step_update": {
                    "step_type": "tool",
                    "step_index": 1,
                    "tool_name": "call_mcp_tool",
                    "state": "ACTIVE",
                    "tool_info": {
                        "parameters": {
                            "ServerName": "kirocrew-core",
                            "ToolName": "send_message",
                            "Arguments": {"text": "hello"},
                        }
                    },
                },
            }
        ).encode("utf-8")
        + b"\n",
        json.dumps(
            {
                "event": "step_update",
                "step_update": {
                    "step_type": "tool",
                    "step_index": 1,
                    "state": "DONE",
                    "tool_info": {"output": "ok"},
                },
            }
        ).encode("utf-8")
        + b"\n",
        json.dumps(
            {
                "event": "result",
                "result": {"response": "All done", "usage": {"total_tokens": 150}},
            }
        ).encode("utf-8")
        + b"\n",
    ]

    mock_stdout = MagicMock()
    mock_stdout.readline = AsyncMock(side_effect=lines + [b""])
    mock_proc.stdout = mock_stdout

    session = AgySession(session_id="test_sess_2", proc=mock_proc, cwd="/tmp")
    server.sessions["test_sess_2"] = session

    await server.dispatch_request(
        {
            "jsonrpc": "2.0",
            "id": 99,
            "method": "session/prompt",
            "params": {
                "sessionId": "test_sess_2",
                "prompt": [{"type": "text", "text": "run core tool"}],
            },
        }
    )

    assert len(responses) == 1
    assert responses[0] == (99, {"stopReason": "end_turn"})

    tool_active = [
        n[1]["update"]
        for n in notifications
        if n[1].get("update", {}).get("sessionUpdate") == "tool_call"
    ]
    assert len(tool_active) == 1
    assert tool_active[0]["name"] == "mcp__kirocrew-core__send_message"
    assert tool_active[0]["input"] == {"text": "hello"}

    tool_done = [
        n[1]["update"]
        for n in notifications
        if n[1].get("update", {}).get("sessionUpdate") == "tool_call_update"
    ]
    assert len(tool_done) == 1
    assert tool_done[0]["name"] == "mcp__kirocrew-core__send_message"


@pytest.mark.asyncio
async def test_agy_prompt_child_eof_emits_error() -> None:
    """Prompt handles child EOF before result event by responding with an error."""
    server = AgyAcpServer()
    errors: list[tuple[Any, int, str]] = []
    server._write_error = lambda req_id, code, msg: errors.append((req_id, code, msg))

    mock_proc = MagicMock()
    mock_proc.returncode = None

    async def fake_wait():
        mock_proc.returncode = 137
        return 137

    mock_proc.wait = AsyncMock(side_effect=fake_wait)
    mock_stdin = MagicMock()
    mock_stdin.drain = AsyncMock()
    mock_proc.stdin = mock_stdin
    mock_stdout = MagicMock()
    mock_stdout.readline = AsyncMock(return_value=b"")
    mock_proc.stdout = mock_stdout

    session = AgySession(session_id="test_sess_eof", proc=mock_proc, cwd="/tmp")
    server.sessions["test_sess_eof"] = session

    await server.dispatch_request(
        {
            "jsonrpc": "2.0",
            "id": 101,
            "method": "session/prompt",
            "params": {
                "sessionId": "test_sess_eof",
                "prompt": [{"type": "text", "text": "hello"}],
            },
        }
    )

    assert len(errors) == 1
    assert errors[0][0] == 101
    assert errors[0][1] == -32000
    assert "137" in errors[0][2]


@pytest.mark.asyncio
async def test_agy_respawn_failure_emits_error() -> None:
    """Model/effort option change emits error on respawn failure."""
    server = AgyAcpServer()
    errors: list[tuple[Any, int, str]] = []
    server._write_error = lambda req_id, code, msg: errors.append((req_id, code, msg))

    mock_proc = MagicMock()
    mock_proc.wait = AsyncMock()
    session = AgySession(
        session_id="test_sess_respawn", proc=mock_proc, cwd="/tmp", model="old_model"
    )
    server.sessions["test_sess_respawn"] = session

    server._spawn_agy_process = AsyncMock(side_effect=RuntimeError("binary missing"))

    await server.dispatch_request(
        {
            "jsonrpc": "2.0",
            "id": 102,
            "method": "session/set_config_option",
            "params": {
                "sessionId": "test_sess_respawn",
                "configId": "model",
                "value": "new_model",
            },
        }
    )

    assert len(errors) == 1
    assert errors[0][0] == 102
    assert errors[0][1] == -32000
    assert "failed to respawn agy process" in errors[0][2]
