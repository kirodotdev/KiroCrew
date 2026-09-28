"""The agy backend: vocabulary, launch record, host auth, and adapter protocol.

Google Antigravity CLI (agy) as an ACP backend in Kiro Crew.
"""

from __future__ import annotations

import pytest

from kiro_crew import acp_backends
from kiro_crew.acp.adapters.agy import AgyAcpServer
from kiro_crew.acp.types import PROVIDER_LABEL_AGY, PROVIDER_LABEL_BY_BACKEND
from kiro_crew.agent_sdk import backends as sdk_backends
from kiro_crew.agent_sdk import host_auth

AGY = acp_backends.ACP_BACKEND_AGY


def test_agy_is_a_known_and_selectable_backend() -> None:
    """Known gates the kwarg; selectable is what the switch may persist."""
    assert AGY == "agy"
    assert AGY in sdk_backends.ACP_BACKENDS_KNOWN
    assert AGY in sdk_backends.BASELINE_SELECTABLE_BACKENDS


def test_agy_provider_label() -> None:
    """Label used in logs, cards and UI."""
    assert PROVIDER_LABEL_AGY == "agy"
    assert PROVIDER_LABEL_BY_BACKEND[AGY] == "agy"


def test_agy_routing() -> None:
    """Routing disposition in ACP_BACKEND_ROUTING."""
    assert sdk_backends.routing_for(AGY) is sdk_backends.Routing.SEEDED_SETTINGS


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
    assert ".gemini/antigravity-cli/settings.json" in decl.credential_leaves
    assert ".gemini/antigravity-cli/cache/onboarding.json" in decl.credential_leaves
    assert decl.host_logout_retires_children is False


def test_agy_capability_sets() -> None:
    """Verify agy membership across capability sets."""
    assert AGY in sdk_backends.ACP_BACKENDS_LOAD_WITHOUT_MODES
    assert AGY in sdk_backends.ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION
    assert AGY in sdk_backends.ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION
    assert AGY in sdk_backends.ACP_BACKENDS_HARNESS_OWNED_SESSIONS
    assert AGY in sdk_backends.ACP_BACKENDS_SESSION_MCP_ARRAY
    assert AGY in sdk_backends.ACP_BACKENDS_MEMBER_DISPATCH

    assert AGY in sdk_backends.ACP_BACKENDS_STEER
    assert AGY in sdk_backends.ACP_BACKENDS_COMPACT
    assert AGY in sdk_backends.ACP_BACKENDS_INLINE_COMPACTION
    assert AGY in sdk_backends.ACP_BACKENDS_MARKDOWN_AGENT_SPECS
    assert AGY in sdk_backends.ACP_BACKENDS_SIDE_READONLY

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


def test_agy_mirror_and_mcp_projection() -> None:
    """Verify AgyMirror is registered and projects MCP servers."""
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
    servers = params["mcpServers"]
    assert any(s.get("name") == "stub1" for s in servers)


def test_agy_setup_mcp_servers(tmp_path: any) -> None:
    """Verify _setup_mcp_servers creates and formats .mcp.json in cwd."""
    import json

    from kiro_crew.acp.adapters.agy import _setup_mcp_servers

    mcp_servers = [
        {
            "name": "srv1",
            "command": "node",
            "args": ["server.js"],
            "env": [{"name": "PORT", "value": "3000"}],
        }
    ]
    gemini_cfg = str(tmp_path / "gemini_mcp.json")
    with open(gemini_cfg, "w", encoding="utf-8") as f:
        json.dump({"mcpServers": {}}, f)

    mcp_file, orig, added_keys = _setup_mcp_servers(
        str(tmp_path), mcp_servers, gemini_config_path=gemini_cfg
    )
    assert mcp_file == str(tmp_path / ".mcp.json")
    assert orig is None
    assert "srv1" in added_keys

    with open(mcp_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert "mcpServers" in data
    assert "srv1" in data["mcpServers"]
    assert data["mcpServers"]["srv1"]["command"] == "node"
    assert data["mcpServers"]["srv1"]["env"] == {"PORT": "3000"}

    with open(gemini_cfg, "r", encoding="utf-8") as f:
        gdata = json.load(f)
    assert "srv1" in gdata["mcpServers"]


@pytest.mark.asyncio
async def test_agy_steer_handling() -> None:
    """Verify handle_session_steer emits lifecycle notifications and injects message."""
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

    session = AgySession(session_id="test_sess_1", proc=mock_proc, cwd="/tmp")
    server.sessions["test_sess_1"] = session

    await server.dispatch_request(
        {
            "jsonrpc": "2.0",
            "id": 42,
            "method": "_session/steer",
            "params": {"sessionId": "test_sess_1", "message": "focus on tests"},
        }
    )

    assert len(responses) == 1
    assert responses[0] == (42, {"queued": True})

    assert len(notifications) == 2
    assert notifications[0][0] == "session/update"
    assert notifications[0][1]["update"]["sessionUpdate"] == "steering_queued"
    assert notifications[0][1]["update"]["content"] == "focus on tests"

    assert notifications[1][0] == "session/update"
    assert notifications[1][1]["update"]["sessionUpdate"] == "steering_consumed"
    assert notifications[1][1]["update"]["content"] == "focus on tests"

    mock_stdin.write.assert_called_once()
    written_data = mock_stdin.write.call_args[0][0].decode("utf-8")
    assert "focus on tests" in written_data


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
async def test_agy_large_prompt_buffer_support() -> None:
    """Verify lines larger than asyncio default 64KB (e.g. 200KB) are supported without LimitOverrunError."""
    import json
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

    # Simulate a 100KB stdout line from agy (e.g. large file or response)
    large_text = "x" * (100 * 1024)
    lines = [
        json.dumps(
            {
                "event": "step_update",
                "step_update": {
                    "step_type": "agent_response",
                    "text_delta": large_text,
                },
            }
        ).encode("utf-8")
        + b"\n",
        json.dumps(
            {
                "event": "result",
                "result": {"response": "done"},
            }
        ).encode("utf-8")
        + b"\n",
    ]

    mock_stdout = MagicMock()
    mock_stdout.readline = AsyncMock(side_effect=lines + [b""])
    mock_proc.stdout = mock_stdout

    session = AgySession(session_id="test_large_1", proc=mock_proc, cwd="/tmp")
    server.sessions["test_large_1"] = session

    # A prompt with > 64KB content
    large_prompt = "p" * (80 * 1024)
    await server.dispatch_request(
        {
            "jsonrpc": "2.0",
            "id": 101,
            "method": "session/prompt",
            "params": {
                "sessionId": "test_large_1",
                "prompt": [{"type": "text", "text": large_prompt}],
            },
        }
    )

    assert len(responses) == 1
    assert responses[0] == (101, {"stopReason": "end_turn"})


@pytest.mark.asyncio
async def test_agy_session_cancel() -> None:
    """Verify session/cancel halts execution and marks in-flight prompt cancelled."""
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.acp.adapters.agy import AgyAcpServer, AgySession

    server = AgyAcpServer()
    responses: list[tuple[any, dict]] = []
    server._write_response = lambda req_id, res: responses.append((req_id, res))

    mock_proc = MagicMock()
    mock_proc.returncode = None
    mock_proc.terminate = MagicMock()
    mock_stdin = MagicMock()
    mock_stdin.drain = AsyncMock()
    mock_proc.stdin = mock_stdin

    session = AgySession(session_id="test_cancel_1", proc=mock_proc, cwd="/tmp")
    server.sessions["test_cancel_1"] = session

    # Dispatch cancel
    await server.dispatch_request(
        {
            "jsonrpc": "2.0",
            "id": 201,
            "method": "session/cancel",
            "params": {"sessionId": "test_cancel_1"},
        }
    )

    assert session.cancelled is True
    mock_proc.terminate.assert_called_once()
    assert len(responses) == 1
    assert responses[0] == (201, {})


@pytest.mark.asyncio
async def test_agy_premature_eof_returns_error() -> None:
    """Verify that if agy process terminates before result, an error response is sent to req_id."""
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.acp.adapters.agy import AgyAcpServer, AgySession

    server = AgyAcpServer()
    errors: list[tuple[any, int, str]] = []
    server._write_error = lambda req_id, code, msg: errors.append((req_id, code, msg))

    mock_proc = MagicMock()
    mock_proc.returncode = None
    mock_stdin = MagicMock()
    mock_stdin.drain = AsyncMock()
    mock_proc.stdin = mock_stdin

    # Subprocess stdout reaches EOF mid-turn when process crashes with exit code 1
    async def fake_readline() -> bytes:
        mock_proc.returncode = 1
        return b""

    mock_stdout = MagicMock()
    mock_stdout.readline = AsyncMock(side_effect=fake_readline)
    mock_proc.stdout = mock_stdout
    mock_proc.stderr = MagicMock()
    mock_proc.stderr.read = AsyncMock(return_value=b"Process crashed")

    session = AgySession(session_id="test_eof_1", proc=mock_proc, cwd="/tmp")
    server.sessions["test_eof_1"] = session

    await server.dispatch_request(
        {
            "jsonrpc": "2.0",
            "id": 301,
            "method": "session/prompt",
            "params": {"sessionId": "test_eof_1", "prompt": "test eof"},
        }
    )

    assert len(errors) == 1
    assert errors[0][0] == 301
    assert errors[0][1] == -32000
    assert "Process crashed" in errors[0][2]


@pytest.mark.asyncio
async def test_agy_auto_reconnect_inactive_session() -> None:
    """Verify that an inactive/dead session is reconnected via --conversation on prompt."""
    import json
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.acp.adapters.agy import AgyAcpServer, AgySession

    server = AgyAcpServer()
    responses: list[tuple[any, dict]] = []
    server._write_response = lambda req_id, res: responses.append((req_id, res))

    # Old dead process
    dead_proc = MagicMock()
    dead_proc.returncode = 137  # terminated
    session = AgySession(session_id="conv-12345", proc=dead_proc, cwd="/tmp")
    server.sessions["conv-12345"] = session

    # Fresh revived process
    new_proc = MagicMock()
    new_proc.returncode = None
    mock_stdin = MagicMock()
    mock_stdin.drain = AsyncMock()
    new_proc.stdin = mock_stdin

    result_line = (
        json.dumps(
            {
                "event": "result",
                "result": {"response": "recovered!"},
            }
        ).encode("utf-8")
        + b"\n"
    )
    mock_stdout = MagicMock()
    mock_stdout.readline = AsyncMock(side_effect=[result_line, b""])
    new_proc.stdout = mock_stdout

    spawn_mock = AsyncMock(return_value=(new_proc, "conv-12345"))
    server._spawn_agy_process = spawn_mock

    await server.dispatch_request(
        {
            "jsonrpc": "2.0",
            "id": 401,
            "method": "session/prompt",
            "params": {"sessionId": "conv-12345", "prompt": "hello after restart"},
        }
    )

    spawn_mock.assert_called_once_with(
        cwd="/tmp",
        conversation_id="conv-12345",
        model=None,
        effort=None,
    )
    assert server.sessions["conv-12345"].proc is new_proc
    assert len(responses) == 1
    assert responses[0] == (401, {"stopReason": "end_turn"})


def test_agy_setup_mcp_servers_overwrites_existing(tmp_path: any) -> None:
    """Verify _setup_mcp_servers refreshes existing entries with updated tokens/ports."""
    import json

    from kiro_crew.acp.adapters.agy import _setup_mcp_servers

    gemini_cfg = str(tmp_path / "gemini_mcp.json")
    with open(gemini_cfg, "w", encoding="utf-8") as f:
        json.dump(
            {
                "mcpServers": {
                    "kirocrew-core": {
                        "command": "old_cmd",
                        "env": {"KIROCREW_STUB_SESSION_TOKEN": "old_stale_token"},
                    }
                }
            },
            f,
        )

    mcp_servers = [
        {
            "name": "kirocrew-core",
            "command": "new_cmd",
            "env": {"KIROCREW_STUB_SESSION_TOKEN": "fresh_valid_token"},
        }
    ]

    _setup_mcp_servers(str(tmp_path), mcp_servers, gemini_config_path=gemini_cfg)

    with open(gemini_cfg, "r", encoding="utf-8") as f:
        data = json.load(f)

    assert data["mcpServers"]["kirocrew-core"]["command"] == "new_cmd"
    assert (
        data["mcpServers"]["kirocrew-core"]["env"]["KIROCREW_STUB_SESSION_TOKEN"]
        == "fresh_valid_token"
    )
