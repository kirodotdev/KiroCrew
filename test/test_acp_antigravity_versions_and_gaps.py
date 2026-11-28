"""Contract tests for Antigravity version floor, discovery, and gap fixes."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from acp_frame_replay_harness import CORPUS, expected_path, read_fixture, replay_frames

from kiro_crew import context as context_mod
from kiro_crew import steering_plan
from kiro_crew.acp import _dispatch
from kiro_crew.acp import client as client_mod
from kiro_crew.acp import client_claude_session, client_turn_control
from kiro_crew.acp._dispatch import agent_version_from_init
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.client_antigravity import _refuse_unverified_antigravity_version
from kiro_crew.acp.harness import antigravity as antigravity_mod
from kiro_crew.acp.harness.antigravity import (
    ANTIGRAVITY_ACP_BIN_ENV,
    ANTIGRAVITY_HARNESS_PATH_ENV,
    ANTIGRAVITY_INSTALL_HINT,
    ANTIGRAVITY_VERIFIED_VERSION,
    antigravity_not_found_message,
    probe_antigravity_runtime,
    resolve_antigravity_runtime,
)
from kiro_crew.acp.provider_labels import PROVIDER_LABEL_ANTIGRAVITY
from kiro_crew.acp.session_handle import session_models_envelope
from kiro_crew.acp.transport_errors import AcpError, AcpTimeoutError, AcpToolGateUnroutable
from kiro_crew.acp.types import (
    EVENT_COMPACTION_STATUS,
    EVENT_COMPLETE,
    EVENT_MCP_OAUTH_REQUEST,
    EVENT_MCP_SERVER_INIT_FAILURE,
    EVENT_MCP_SERVER_INITIALIZED,
    EVENT_SUBAGENT_ACTIVITY,
    EVENT_SUBAGENT_LIST,
    EVENT_TEXT_CHUNK,
    EVENT_THINKING_CHUNK,
    JsonRpcMessage,
)
from kiro_crew.agent_sdk import backends, host_auth
from kiro_crew.agent_sdk.backends import ACP_BACKEND_ANTIGRAVITY
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

_NEW_FIXTURES = (
    "handshake-1.4.5-live.jsonl",
    "handshake-1.5.0-live.jsonl",
    "session-load-mcp-reload-1.4.5-live.jsonl",
    "session-load-mcp-reload-1.5.0-live.jsonl",
    "unstartable-mcp-prompt-1.4.5-live.jsonl",
    "unstartable-mcp-prompt-1.5.0-live.jsonl",
)


def _antigravity_client(version: object) -> AcpClient:
    client = AcpClient(acp_backend=ACP_BACKEND_ANTIGRAVITY)
    client._agent_version = version  # type: ignore[assignment]
    client._agent_version_read = True
    return client


def test_antigravity_version_constants_exports_and_hints() -> None:
    assert antigravity_mod.ANTIGRAVITY_MIN_VERSION == (1, 4, 4)  # flake-ok: version tuple
    assert ANTIGRAVITY_VERIFIED_VERSION == "1.5.0"
    assert "ANTIGRAVITY_MIN_VERSION" in antigravity_mod.__all__
    assert "ANTIGRAVITY_VERIFIED_VERSION" in antigravity_mod.__all__
    assert "1.4.4" in ANTIGRAVITY_INSTALL_HINT
    assert "only" not in ANTIGRAVITY_INSTALL_HINT.lower()
    assert "refuses other versions" not in ANTIGRAVITY_INSTALL_HINT.lower()

    remedy = host_auth.sign_in_remedy(ACP_BACKEND_ANTIGRAVITY)
    assert "1.4.4 or newer" in remedy
    assert "only" not in remedy.lower()
    assert "refuses other versions" not in remedy.lower()


@pytest.mark.parametrize("version", ["1.4.4", "1.4.5", "1.5.0", "1.6.0", "2.0.0", " 1.5.0 "])
def test_refuse_unverified_antigravity_version_accepts_floor_and_newer(version: str) -> None:
    _refuse_unverified_antigravity_version(_antigravity_client(version))


@pytest.mark.parametrize("version", ["1.4.3", "1.3.0", "0.9.0"])
def test_refuse_unverified_antigravity_version_refuses_below_floor(version: str) -> None:
    with pytest.raises(AcpToolGateUnroutable) as exc_info:
        _refuse_unverified_antigravity_version(_antigravity_client(version))
    exc = exc_info.value
    assert exc.transient is False
    assert exc.user_worded is True
    assert f"{version} is older than the minimum version 1.4.4" in str(exc)
    assert "only" not in str(exc).lower()


@pytest.mark.parametrize("version", ["", None, "dev", "1.4", "\u0661.\u0664.\u0665", 123])
def test_refuse_unverified_antigravity_version_refuses_absent_or_unreadable(
    version: object,
) -> None:
    """A runtime with no readable version is refused, as before the floor replaced the pin."""
    with pytest.raises(AcpToolGateUnroutable) as exc_info:
        _refuse_unverified_antigravity_version(_antigravity_client(version))
    exc = exc_info.value
    assert exc.transient is False
    assert exc.user_worded is True
    assert "did not report a version Kiro Crew can read" in str(exc)
    assert ANTIGRAVITY_INSTALL_HINT in str(exc)


@pytest.mark.parametrize(
    "version",
    ["1.4.4", "1.4.5", "1.5.0", "1.9.9", "", None, "dev"],
)
def test_untested_version_issue_is_empty_inside_range_or_unparseable(
    version: object,
) -> None:
    assert client_turn_control.ANTIGRAVITY_TESTED_VERSION == "1.5.0"
    assert client_turn_control.ANTIGRAVITY_TESTED_RANGE == ((1, 4, 4), (2, 0, 0))
    assert client_turn_control.untested_version_issue(ACP_BACKEND_ANTIGRAVITY, version) == ""  # type: ignore[arg-type]


def test_untested_version_issue_warns_at_or_above_2_0_0() -> None:
    notice = client_turn_control.untested_version_issue(ACP_BACKEND_ANTIGRAVITY, "2.0.0")
    assert "Google Antigravity 2.0.0" in notice
    assert "1.4.4 to 1.x" in notice
    assert "A Kiro Crew update moves the tested range." in notice


def test_find_main_executable_uses_localappdata_fallback_and_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    localappdata = tmp_path / "localappdata"
    fallback_dir = localappdata / "agy" / "bin"
    fallback_dir.mkdir(parents=True)
    fallback_server = fallback_dir / "agy_acp_server.exe"
    fallback_helper = fallback_dir / "localharness_external.exe"
    fallback_server.write_bytes(b"fallback-server")
    fallback_helper.write_bytes(b"fallback-helper")

    path_dir = tmp_path / "path_bin"
    path_dir.mkdir()
    path_server = path_dir / "agy_acp_server.exe"
    path_server.write_bytes(b"path-server")
    # shutil.which needs the execute bit on POSIX hosts, where this Windows test also runs.
    path_server.chmod(0o755)

    override_server = tmp_path / "override_agy_acp_server.exe"
    override_server.write_bytes(b"override-server")

    monkeypatch.setattr(antigravity_mod.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(antigravity_mod.platform_compat, "is_executable_file", os.path.isfile)
    monkeypatch.setattr(antigravity_mod, "ANTIGRAVITY_ACP_BIN", "agy_acp_server.exe")
    monkeypatch.setattr(antigravity_mod, "ANTIGRAVITY_HARNESS_BIN", "localharness_external.exe")
    monkeypatch.setattr(client_mod, "_mise_which", lambda _name: None)

    env_fallback = {"LOCALAPPDATA": str(localappdata)}
    found_fallback = antigravity_mod._find_main_executable(env_fallback, "")
    assert found_fallback is not None
    assert os.path.samefile(found_fallback, fallback_server)

    found_path = antigravity_mod._find_main_executable(env_fallback, str(path_dir))
    assert found_path is not None
    assert os.path.samefile(found_path, path_server)

    env_override = {
        "LOCALAPPDATA": str(localappdata),
        ANTIGRAVITY_ACP_BIN_ENV: str(override_server),
    }
    found_override = antigravity_mod._find_main_executable(env_override, str(path_dir))
    assert found_override is not None
    assert os.path.samefile(found_override, override_server)

    empty_home = tmp_path / "empty_home"
    empty_home.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(localappdata))
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("HOME", str(empty_home))
    monkeypatch.setenv("USERPROFILE", str(empty_home))
    monkeypatch.setattr(client_mod, "augmented_path", lambda path="": path)
    monkeypatch.delenv(ANTIGRAVITY_ACP_BIN_ENV, raising=False)
    monkeypatch.delenv(ANTIGRAVITY_HARNESS_PATH_ENV, raising=False)
    monkeypatch.setattr(antigravity_mod, "_cache", None)
    monkeypatch.setattr(antigravity_mod, "_cache_generation", antigravity_mod._cache_generation + 1)
    resolution = resolve_antigravity_runtime()
    assert resolution.server is not None
    assert resolution.helper is not None
    assert os.path.samefile(resolution.server, fallback_server)
    assert os.path.samefile(resolution.helper, fallback_helper)


@pytest.mark.parametrize("fixture_name", _NEW_FIXTURES)
def test_new_1_4_5_and_1_5_0_fixtures_replay_match_snapshots(fixture_name: str) -> None:
    fixture = CORPUS / "antigravity" / fixture_name
    meta, frames = read_fixture(fixture)
    replayed = replay_frames(frames, gate_envelope_nonce=meta.get("gate_envelope_nonce"))
    expected = json.loads(expected_path(fixture).read_text(encoding="utf-8"))
    assert replayed == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fixture_name", "expected_version"),
    [("handshake-1.4.5-live.jsonl", "1.4.5"), ("handshake-1.5.0-live.jsonl", "1.5.0")],
)
async def test_live_1_4_5_and_1_5_0_handshakes_pass_gate_and_models(
    fixture_name: str, expected_version: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.acp.client_model_selection import model_registry

    monkeypatch.setattr(model_registry, "refresh_advertised_models", lambda *args, **kwargs: False)
    fixture = CORPUS / "antigravity" / fixture_name
    _meta, frames = read_fixture(fixture)
    init_result = frames[0]["result"]
    session_new_result = frames[1]["result"]

    assert agent_version_from_init(init_result) == expected_version
    envelope = session_models_envelope(session_new_result, ACP_BACKEND_ANTIGRAVITY)
    assert [row["modelId"] for row in envelope["availableModels"]] == ["gemini-3.8-flash"]
    assert envelope["currentModelId"] == "gemini-3.8-flash"

    client = _antigravity_client(expected_version)
    client._acp_config_options = list(session_new_result["configOptions"])
    client.set_config_option = AsyncMock(return_value={})
    await client._apply_session_permission_routing()
    client.set_config_option.assert_awaited_once_with("mode", "default")
    client._capture_available_models(session_new_result)
    assert [row["modelId"] for row in client._available_models] == ["gemini-3.8-flash"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fixture_name",
    ["unstartable-mcp-prompt-1.4.5-live.jsonl", "unstartable-mcp-prompt-1.5.0-live.jsonl"],
)
async def test_live_unstartable_mcp_prompt_frames_pass_client_tripwire(
    fixture_name: str,
) -> None:
    fixture = CORPUS / "antigravity" / fixture_name
    meta, frames = read_fixture(fixture)
    client = _antigravity_client(meta["agent_version"])
    client._session_id = "antigravity-session-1"
    client._session_key = "dashboard:test"
    client._kill_process = AsyncMock()
    client._unasked_shell_tripwire = True

    for raw in frames:
        if raw.get("method") != "session/update":
            continue
        msg = JsonRpcMessage.from_dict(raw)
        await client._tripwire_pi_gate(msg)
        await client._tripwire_permission_state(msg)

    client._kill_process.assert_not_awaited()


def _stream_client(
    tmp_path: Path, backend: str = ACP_BACKEND_ANTIGRAVITY
) -> tuple[AcpClient, asyncio.StreamReader]:
    client = AcpClient(work_dir=tmp_path, acp_backend=backend)
    reader = asyncio.StreamReader()
    proc = MagicMock()
    proc.stdout = reader
    proc.returncode = None
    client._process = proc
    client._session_id = "antigravity-session-1"
    client._turn_done.clear()
    return client, reader


def _feed_prompt_frames(reader: asyncio.StreamReader, frames: list[dict[str, object]]) -> int:
    prompt_id = 0
    for frame in frames:
        reader.feed_data((json.dumps(frame) + "\n").encode())
        if frame.get("id") is not None and "result" in frame:
            prompt_id = int(frame["id"])  # type: ignore[arg-type]
    reader.feed_eof()
    return prompt_id


def test_antigravity_closed_guards_and_verified_capability_sets() -> None:
    """Keep closed parity guards off and verified transport sets on."""
    closed_sets = (
        backends.ACP_BACKENDS_HONOR_ZERO_TOOL_BAN,
        backends.ACP_BACKENDS_SIDE_READONLY,
        backends.ACP_BACKENDS_CREW_FIRES_SPEC_HOOKS,
        backends.ACP_BACKENDS_META_IDENTITY,
        backends.ACP_BACKENDS_MCP_RELAUNCH_RELOAD,
        backends.ACP_BACKENDS_MCP_CONFIG_HOT_RELOAD,
        backends.ACP_BACKENDS_RELOAD_DISPOSED_SESSION,
        backends.ACP_BACKENDS_LOAD_WITHOUT_MODES,
        backends.ACP_BACKENDS_CWD_BOUND_SESSIONS,
        backends.ACP_BACKENDS_NO_MCP_INIT_FRAMES,
        backends.ACP_BACKENDS_UNATTRIBUTED_TERMINAL_ERROR,
        backends.ACP_BACKENDS_MEMBER_CAPABILITIES,
        backends.ACP_BACKENDS_MEMBER_DISPATCH,
        backends.ACP_BACKENDS_MEMBER_PANEL,
        backends.ACP_BACKENDS_CREW_LOADS_SPEC_STEERING,
    )
    for guard_set in closed_sets:
        assert ACP_BACKEND_ANTIGRAVITY not in guard_set

    assert ACP_BACKEND_ANTIGRAVITY in backends.ACP_BACKENDS_SESSION_MCP_ARRAY
    assert ACP_BACKEND_ANTIGRAVITY in backends.ACP_BACKENDS_PRUNE_UNSTARTABLE_MCP
    assert ACP_BACKEND_ANTIGRAVITY in backends.ACP_BACKENDS_CONFIG_MODEL_SELECT_IS_CANONICAL
    assert ACP_BACKEND_ANTIGRAVITY in backends.ACP_BACKENDS_UNASKED_NATIVE_TOOLS
    assert ACP_BACKEND_ANTIGRAVITY in backends.ACP_BACKENDS_CREW_FIRES_SPEC_OBSERVER_HOOKS


def test_folder_steering_reach_and_tree_verdict_for_antigravity(tmp_path: Path) -> None:
    """Keep folder steering active while spec resource steering stays closed."""
    assert (
        context_mod._project_steering_delivered(PROVIDER_LABEL_ANTIGRAVITY, False, str(tmp_path))
        is False
    )
    assert (
        context_mod._project_steering_delivered(
            PROVIDER_LABEL_ANTIGRAVITY, False, str(tmp_path), agent="custom-agent"
        )
        is False
    )
    assert steering_plan.tree_verdict(
        provider_type=PROVIDER_LABEL_ANTIGRAVITY,
        native_steering=False,
        project=str(tmp_path),
        agent=None,
        is_member=False,
    ) == (steering_plan.REACHES_NO, steering_plan.REASON_HARNESS_HAS_NO_PATH)


@pytest.mark.parametrize(
    "fixture_name", ["handshake-1.4.5-live.jsonl", "handshake-1.5.0-live.jsonl"]
)
def test_antigravity_auth_declaration_covers_all_four_methods(fixture_name: str) -> None:
    """Verify the auth declaration covers oauth-business, agent-platform, and consumer auth."""
    _meta, frames = read_fixture(CORPUS / "antigravity" / fixture_name)
    method_ids = {entry["id"] for entry in frames[0]["result"]["authMethods"]}
    assert method_ids == {"oauth-personal", "oauth-business", "gemini-api-key", "agent-platform"}

    decl = host_auth.declaration_for(ACP_BACKEND_ANTIGRAVITY)
    assert ".gemini/antigravity-acp/acp_token.json" in decl.credential_leaves
    assert ".gemini/antigravity-acp/acp_business_token.json" in decl.credential_leaves
    assert decl.adapter_own_leaves == decl.credential_leaves
    assert decl.home_override_env_vars == ("GEMINI_HOME",)
    for token in ("oauth-personal", "oauth-business", "gemini-api-key", "agent-platform"):
        assert token in decl.sign_in_remedy
        assert token in decl.signed_out_message
    assert host_auth.reports_signed_out(
        ACP_BACKEND_ANTIGRAVITY, "No authentication method selected"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fixture_name",
    ["unstartable-mcp-prompt-1.4.5-live.jsonl", "unstartable-mcp-prompt-1.5.0-live.jsonl"],
)
async def test_antigravity_unstartable_mcp_prompt_fails_turn(
    tmp_path: Path, fixture_name: str
) -> None:
    """Fail the turn when Antigravity reports a failed MCP server and returns end_turn."""
    _meta, frames = read_fixture(CORPUS / "antigravity" / fixture_name)
    turn_frames = frames[2:]
    error_chunk = next(
        f
        for f in turn_frames
        if f.get("method") == "session/update"
        and f["params"]["update"].get("sessionUpdate") == "agent_message_chunk"
    )
    assert _dispatch.is_antigravity_unattributed_terminal_error(
        error_chunk["params"]["update"]["content"]["text"]
    )
    client, reader = _stream_client(tmp_path)
    prompt_id = _feed_prompt_frames(reader, turn_frames)

    with pytest.raises(AcpError) as exc_info:
        async for _event in client._dispatch_events(prompt_id, timeout=FALLBACK_TIMEOUT_SECS):
            pass

    assert exc_info.value.transient is False
    assert "one of this session's MCP servers" in str(exc_info.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fixture_name",
    ["session-load-mcp-reload-1.4.5-live.jsonl", "session-load-mcp-reload-1.5.0-live.jsonl"],
)
async def test_antigravity_invalid_api_key_prompt_fails_turn(
    tmp_path: Path, fixture_name: str
) -> None:
    """Fail the turn when Antigravity reports an invalid API key and returns end_turn."""
    _meta, frames = read_fixture(CORPUS / "antigravity" / fixture_name)
    turn_frames = [frames[3], frames[4]]
    assert _dispatch.is_antigravity_unattributed_terminal_error(
        turn_frames[0]["params"]["update"]["content"]["text"]
    )
    client, reader = _stream_client(tmp_path)
    prompt_id = _feed_prompt_frames(reader, turn_frames)

    with pytest.raises(AcpError) as exc_info:
        async for _event in client._dispatch_events(prompt_id, timeout=FALLBACK_TIMEOUT_SECS):
            pass

    assert exc_info.value.transient is False
    assert "API key not valid" in str(exc_info.value)


@pytest.mark.asyncio
async def test_antigravity_multichunk_prose_with_error_prefix_completes_normally(
    tmp_path: Path,
) -> None:
    """Deliver normal streamed prose when a matching first chunk has follow-up chunks."""
    client, reader = _stream_client(tmp_path)
    frames = [
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "antigravity-session-1",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {
                        "type": "text",
                        "text": "API key not valid. Please pass a valid API key.",
                    },
                },
            },
        },
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "antigravity-session-1",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": " That message means your key is wrong."},
                },
            },
        },
        {"jsonrpc": "2.0", "id": 7, "result": {"stopReason": "end_turn"}},
    ]
    prompt_id = _feed_prompt_frames(reader, frames)

    events = [
        event async for event in client._dispatch_events(prompt_id, timeout=FALLBACK_TIMEOUT_SECS)
    ]
    texts = [e.text for e in events if e.kind == EVENT_TEXT_CHUNK]
    assert "".join(texts) == (
        "API key not valid. Please pass a valid API key. That message means your key is wrong."
    )
    assert events[-1].kind == EVENT_COMPLETE
    assert events[-1].stop_reason == "end_turn"


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", [backends.ACP_BACKEND_KIRO, backends.ACP_BACKEND_CLAUDE])
async def test_other_backends_keep_antigravity_error_text_untouched(
    tmp_path: Path, backend: str
) -> None:
    """Do not reinterpret unattributed Antigravity error text on other backends."""
    client, reader = _stream_client(tmp_path, backend=backend)
    frames = [
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "antigravity-session-1",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {
                        "type": "text",
                        "text": "API key not valid. Please pass a valid API key.",
                    },
                },
            },
        },
        {"jsonrpc": "2.0", "id": 8, "result": {"stopReason": "end_turn"}},
    ]
    prompt_id = _feed_prompt_frames(reader, frames)

    events = [
        event async for event in client._dispatch_events(prompt_id, timeout=FALLBACK_TIMEOUT_SECS)
    ]
    texts = [e.text for e in events if e.kind == EVENT_TEXT_CHUNK]
    assert texts == ["API key not valid. Please pass a valid API key."]
    assert events[-1].kind == EVENT_COMPLETE
    assert events[-1].stop_reason == "end_turn"


def _tripwire_test_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AcpClient:
    from kiro_crew import sel as sel_module

    recorder = MagicMock()
    monkeypatch.setattr(sel_module, "sel", lambda: recorder)
    client = _antigravity_client("1.5.0")
    client._work_dir = tmp_path
    client._session_id = "antigravity-session-1"
    client._session_key = "dashboard:test"
    client._kill_process = AsyncMock()
    client._send_response = AsyncMock()
    client._unasked_shell_tripwire = True
    return client


@pytest.mark.asyncio
async def test_antigravity_tripwire_refuses_unreadable_mcp_identity_even_with_read_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _tripwire_test_client(tmp_path, monkeypatch)
    call_msg = JsonRpcMessage.from_dict(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "antigravity-session-1",
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "call-unreadable",
                    "kind": "read",
                    "title": "Running view_file",
                    "status": "in_progress",
                    "_meta": {"mcp": {"server": "s" * 513, "tool": "view_file"}},
                },
            },
        }
    )
    end_msg = JsonRpcMessage.from_dict(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "antigravity-session-1",
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "call-unreadable",
                    "status": "completed",
                },
            },
        }
    )
    client._extract_tool_event(call_msg)
    await client._tripwire_pi_gate(call_msg)
    with pytest.raises(AcpToolGateUnroutable, match="without asking Kiro Crew's gate"):
        await client._tripwire_pi_gate(end_msg)
    client._kill_process.assert_awaited_once_with(force=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["edit", "delete", "fetch", "other", "execute", ""])
async def test_antigravity_tripwire_refuses_non_read_kinds_even_with_read_title(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    client = _tripwire_test_client(tmp_path, monkeypatch)
    call_update: dict[str, object] = {
        "sessionUpdate": "tool_call",
        "toolCallId": "call-bad-kind",
        "title": "Running view_file",
        "status": "in_progress",
    }
    if kind:
        call_update["kind"] = kind
    call_msg = JsonRpcMessage.from_dict(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionId": "antigravity-session-1", "update": call_update},
        }
    )
    end_msg = JsonRpcMessage.from_dict(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "antigravity-session-1",
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "call-bad-kind",
                    "status": "completed",
                },
            },
        }
    )
    client._extract_tool_event(call_msg)
    await client._tripwire_pi_gate(call_msg)
    with pytest.raises(AcpToolGateUnroutable, match="without asking Kiro Crew's gate"):
        await client._tripwire_pi_gate(end_msg)
    client._kill_process.assert_awaited_once_with(force=True)


@pytest.mark.asyncio
async def test_antigravity_tripwire_refuses_kind_switch_to_edit_on_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _tripwire_test_client(tmp_path, monkeypatch)
    call_msg = JsonRpcMessage.from_dict(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "antigravity-session-1",
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "call-switch",
                    "kind": "read",
                    "title": "Running view_file",
                    "status": "in_progress",
                },
            },
        }
    )
    end_msg = JsonRpcMessage.from_dict(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "antigravity-session-1",
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "call-switch",
                    "kind": "edit",
                    "status": "completed",
                },
            },
        }
    )
    client._extract_tool_event(call_msg)
    await client._tripwire_pi_gate(call_msg)
    with pytest.raises(AcpToolGateUnroutable, match="without asking Kiro Crew's gate"):
        await client._tripwire_pi_gate(end_msg)
    client._kill_process.assert_awaited_once_with(force=True)


@pytest.mark.asyncio
async def test_antigravity_tripwire_lets_search_kind_read_only_builtin_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _tripwire_test_client(tmp_path, monkeypatch)
    call_msg = JsonRpcMessage.from_dict(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "antigravity-session-1",
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "call-search",
                    "kind": "search",
                    "title": "Run grep_search?",
                    "status": "in_progress",
                },
            },
        }
    )
    end_msg = JsonRpcMessage.from_dict(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "antigravity-session-1",
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "call-search",
                    "status": "completed",
                },
            },
        }
    )
    client._extract_tool_event(call_msg)
    await client._tripwire_pi_gate(call_msg)
    await client._tripwire_pi_gate(end_msg)
    client._kill_process.assert_not_awaited()


@pytest.mark.asyncio
async def test_antigravity_send_message_stream_extracts_tool_events_for_tripwire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew import sel as sel_module

    recorder = MagicMock()
    monkeypatch.setattr(sel_module, "sel", lambda: recorder)
    client, reader = _stream_client(tmp_path)
    client._session_key = "dashboard:test"
    assert not client._judges_permission_requests
    client._unasked_shell_tripwire = True
    client._kill_process = AsyncMock()
    client._send_prompt = AsyncMock(return_value=11)
    client.ensure_ready = AsyncMock()

    frames = [
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "antigravity-session-1",
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "call-stream-read",
                    "kind": "read",
                    "title": "Running view_file",
                    "status": "in_progress",
                },
            },
        },
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "antigravity-session-1",
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "call-stream-read",
                    "status": "completed",
                },
            },
        },
        {"jsonrpc": "2.0", "id": 11, "result": {"stopReason": "end_turn"}},
    ]
    _feed_prompt_frames(reader, frames)

    async for _chunk in client.send_message_stream("hi", timeout=FALLBACK_TIMEOUT_SECS):
        pass
    client._kill_process.assert_not_awaited()
    assert client._tool_call_harness_tool_name["call-stream-read"] == "view_file"
    assert client._tool_call_kind["call-stream-read"] == "read"

    client2, reader2 = _stream_client(tmp_path)
    client2._session_key = "dashboard:test"
    assert not client2._judges_permission_requests
    client2._unasked_shell_tripwire = True
    client2._kill_process = AsyncMock()
    client2._send_prompt = AsyncMock(return_value=12)
    client2.ensure_ready = AsyncMock()
    frames2 = [
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "antigravity-session-1",
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "call-stream-bad",
                    "kind": "read",
                    "title": "Running view_file",
                    "status": "in_progress",
                    "_meta": {"mcp": {"server": "s" * 513, "tool": "view_file"}},
                },
            },
        },
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "antigravity-session-1",
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "call-stream-bad",
                    "status": "completed",
                },
            },
        },
        {"jsonrpc": "2.0", "id": 12, "result": {"stopReason": "end_turn"}},
    ]
    _feed_prompt_frames(reader2, frames2)

    with pytest.raises(AcpToolGateUnroutable, match="without asking Kiro Crew's gate"):
        async for _chunk in client2.send_message_stream("hi", timeout=FALLBACK_TIMEOUT_SECS):
            pass
    client2._kill_process.assert_awaited_once_with(force=True)


def test_find_main_and_helper_block_windows_cwd_hijack_and_off_directory_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Block CWD hijack, skip non-executable mise hits, validate LOCALAPPDATA, and pair helpers."""
    cwd_dir = tmp_path / "cwd"
    cwd_dir.mkdir()
    (cwd_dir / "agy_acp_server.exe").write_bytes(b"evil-cwd-server")
    (cwd_dir / "localharness_external.exe").write_bytes(b"evil-cwd-helper")

    path_dir = tmp_path / "path_bin"
    path_dir.mkdir()
    (path_dir / "localharness_external.exe").write_bytes(b"off-dir-helper")

    localappdata = tmp_path / "localappdata"
    fallback_dir = localappdata / "agy" / "bin"
    fallback_dir.mkdir(parents=True)
    fallback_server = fallback_dir / "agy_acp_server.exe"
    fallback_server.write_bytes(b"real-fallback-server")

    mise_bad = tmp_path / "mise_non_exec.exe"
    mise_bad.write_bytes(b"not-executable")

    monkeypatch.chdir(cwd_dir)
    monkeypatch.setattr(antigravity_mod.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(
        antigravity_mod.platform_compat,
        "is_executable_file",
        lambda path: os.path.isfile(path) and not os.path.samefile(path, mise_bad),
    )
    monkeypatch.setattr(antigravity_mod, "ANTIGRAVITY_ACP_BIN", "agy_acp_server.exe")
    monkeypatch.setattr(antigravity_mod, "ANTIGRAVITY_HARNESS_BIN", "localharness_external.exe")
    monkeypatch.setattr(client_mod, "_mise_which", lambda _name: str(mise_bad))

    # Ignore relative PATH entries and CWD binaries. Let non-executable mise hits fall through.
    env = {"LOCALAPPDATA": f"  {localappdata}  "}
    found_server = antigravity_mod._find_main_executable(
        env, f".{os.pathsep}rel_dir{os.pathsep}{path_dir}"
    )
    assert found_server is not None
    assert os.path.samefile(found_server, fallback_server)

    # Without LOCALAPPDATA, CWD server must never match through PATH or shutil.which.
    assert antigravity_mod._find_main_executable({}, str(path_dir)) is None

    # A relative LOCALAPPDATA value must be rejected, not resolved against CWD.
    rel_env = {"LOCALAPPDATA": "relative_appdata"}
    rel_dir = cwd_dir / "relative_appdata" / "agy" / "bin"
    rel_dir.mkdir(parents=True)
    (rel_dir / "agy_acp_server.exe").write_bytes(b"rel-server")
    assert antigravity_mod._find_main_executable(rel_env, "") is None

    # When a server resolves without a sibling helper, do not pair an off-directory helper.
    assert antigravity_mod._find_helper(env, str(fallback_server), str(path_dir)) is None

    # When no server resolved, CWD helper must not match empty or missing directories.
    empty_dir = tmp_path / "empty_dir"
    empty_dir.mkdir()
    assert antigravity_mod._find_helper(env, None, f".{os.pathsep}{empty_dir}") is None

    # Non-default ANTIGRAVITY_ACP_BIN names the fallback binary under %LOCALAPPDATA%\agy\bin.
    custom_server = fallback_dir / "custom_agy.exe"
    custom_server.write_bytes(b"custom")
    monkeypatch.setattr(antigravity_mod, "ANTIGRAVITY_ACP_BIN", "custom_agy.exe")
    monkeypatch.setattr(client_mod, "_mise_which", lambda _name: None)
    found_custom = antigravity_mod._find_main_executable({"LOCALAPPDATA": str(localappdata)}, "")
    assert found_custom is not None
    assert os.path.samefile(found_custom, custom_server)

    # Error message reports the Windows %LOCALAPPDATA% lookup path when missing.
    msg = antigravity_not_found_message(
        antigravity_mod.AntigravityResolution(server=None, helper=None, search_path=str(path_dir))
    )
    assert os.path.join("%LOCALAPPDATA%", "agy", "bin", "custom_agy.exe") in msg

    # End-to-end probe skips CWD binaries even when both exist beside the gateway.
    monkeypatch.setattr(client_mod, "augmented_path", lambda _path: str(path_dir))
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("LOCALAPPDATA", "")
    monkeypatch.delenv(ANTIGRAVITY_ACP_BIN_ENV, raising=False)
    monkeypatch.delenv(ANTIGRAVITY_HARNESS_PATH_ENV, raising=False)
    monkeypatch.setattr(antigravity_mod, "ANTIGRAVITY_ACP_BIN", "agy_acp_server.exe")
    probed = probe_antigravity_runtime()
    assert probed.server is None
    assert probed.helper is None


def test_antigravity_unattributed_error_regex_and_fixed_exception() -> None:
    """Reject multiline prose and drop untrusted detail text from raised errors."""
    multiline_prose = (
        "The MCP server 'foo' failed to initialize: first line.\n\n"
        "Second paragraph of assistant explanation.\n"
        "Fix the MCP server, or remove it from your configuration and start a new session, "
        "to continue."
    )
    assert _dispatch.is_antigravity_unattributed_terminal_error(multiline_prose) is False

    spoofed = (
        "The MCP server 'missing-mcp' failed to initialize: "
        "401 Unauthorized, 503 Service Unavailable, quota exceeded, please login, context limit. "
        "Fix the MCP server, or remove it from your configuration and start a new session, "
        "to continue."
    )
    assert _dispatch.is_antigravity_unattributed_terminal_error(spoofed) is True
    exc = _dispatch.antigravity_unattributed_error_exception(spoofed)
    assert isinstance(exc, AcpError)
    assert exc.transient is False
    assert exc.user_worded is True
    for forbidden in (
        "missing-mcp",
        "401",
        "503",
        "quota exceeded",
        "please login",
        "context limit",
    ):
        assert forbidden not in str(exc)


@pytest.mark.parametrize("server", ["rate-limit", "throttled 503", "connection reset"])
def test_antigravity_mcp_startup_error_is_never_a_provider_retry(server: str) -> None:
    """A server name shaped like a provider error does not reach the dependency classifier."""
    from kiro_crew.taskq.dependency import classify_exception

    chunk = (
        f"The MCP server '{server}' failed to initialize: spawn failed. "
        "Fix the MCP server, or remove it from your configuration and start a new session, "
        "to continue."
    )
    exc = _dispatch.antigravity_unattributed_error_exception(chunk)
    assert server not in str(exc)
    signal = classify_exception(exc)
    assert signal is None or signal.terminal


def test_antigravity_invalid_api_key_error_names_where_the_key_is_read() -> None:
    exc = _dispatch.antigravity_unattributed_error_exception(
        "API key not valid. Please pass a valid API key."
    )
    assert "GEMINI_API_KEY" in str(exc)
    assert "launch environment" in str(exc)
    assert "Settings" not in str(exc)


@pytest.mark.asyncio
async def test_antigravity_spoofed_error_chunk_does_not_leak_into_acp_error(
    tmp_path: Path,
) -> None:
    """Do not pass single-chunk error details through _raise_acp_error on end_turn."""
    client, reader = _stream_client(tmp_path)
    spoofed = (
        "The MCP server 'missing-mcp' failed to initialize: "
        "503 Service Unavailable quota exceeded please login context limit. "
        "Fix the MCP server, or remove it from your configuration and start a new session, "
        "to continue."
    )
    frames = [
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "antigravity-session-1",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": spoofed},
                },
            },
        },
        {"jsonrpc": "2.0", "id": 15, "result": {"stopReason": "end_turn"}},
    ]
    prompt_id = _feed_prompt_frames(reader, frames)

    with pytest.raises(AcpError) as exc_info:
        async for _event in client._dispatch_events(prompt_id, timeout=FALLBACK_TIMEOUT_SECS):
            pass

    assert exc_info.value.transient is False
    assert "missing-mcp" not in str(exc_info.value)
    assert "503" not in str(exc_info.value)
    assert "quota exceeded" not in str(exc_info.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["thinking", "usage_update_nonzero", "result_usage"])
async def test_antigravity_thought_or_nonzero_usage_cancels_error_hold(
    tmp_path: Path, mode: str
) -> None:
    """Allow matching text after model reasoning or positive token usage."""
    client, reader = _stream_client(tmp_path)
    frames: list[dict[str, object]] = []
    if mode == "thinking":
        frames.append(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": "antigravity-session-1",
                    "update": {
                        "sessionUpdate": "agent_thought_chunk",
                        "content": {"type": "text", "text": "Checking examples."},
                    },
                },
            }
        )
    elif mode == "usage_update_nonzero":
        frames.append(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": "antigravity-session-1",
                    "update": {"sessionUpdate": "usage_update", "used": 256, "size": 1048576},
                },
            }
        )
    frames.append(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "antigravity-session-1",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {
                        "type": "text",
                        "text": "API key not valid. Please pass a valid API key.",
                    },
                },
            },
        }
    )
    result_payload: dict[str, object] = {"stopReason": "end_turn"}
    if mode == "result_usage":
        result_payload["usage"] = {"totalTokens": 120, "inputTokens": 100, "outputTokens": 20}
    frames.append({"jsonrpc": "2.0", "id": 16, "result": result_payload})

    prompt_id = _feed_prompt_frames(reader, frames)
    events = [
        event async for event in client._dispatch_events(prompt_id, timeout=FALLBACK_TIMEOUT_SECS)
    ]
    texts = [e.text for e in events if e.kind == EVENT_TEXT_CHUNK]
    assert texts == ["API key not valid. Please pass a valid API key."]
    if mode == "thinking":
        assert any(e.kind == EVENT_THINKING_CHUNK for e in events)
    assert events[-1].kind == EVENT_COMPLETE


@pytest.mark.asyncio
async def test_antigravity_turn_two_catches_unattributed_error_after_prior_turn(
    tmp_path: Path,
) -> None:
    """Catch unattributed errors on turn two even after prior usage and tool flags."""
    client, reader = _stream_client(tmp_path)
    client._prompt_or_tool_seen = True
    client.last_prompt_stats.context_used_tokens = 30522
    frames = [
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "antigravity-session-1",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {
                        "type": "text",
                        "text": "API key not valid. Please pass a valid API key.",
                    },
                },
            },
        },
        {"jsonrpc": "2.0", "id": 17, "result": {"stopReason": "end_turn"}},
    ]
    prompt_id = _feed_prompt_frames(reader, frames)

    with pytest.raises(AcpError, match="API key not valid") as exc_info:
        async for _event in client._dispatch_events(prompt_id, timeout=FALLBACK_TIMEOUT_SECS):
            pass
    assert exc_info.value.transient is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extra_frame", "expected_kind"),
    [
        (
            {
                "jsonrpc": "2.0",
                "method": "_kiro.dev/compaction/status",
                "params": {"sessionId": "antigravity-session-1", "status": {"type": "started"}},
            },
            EVENT_COMPACTION_STATUS,
        ),
        (
            {
                "jsonrpc": "2.0",
                "method": "_kiro.dev/subagent/list_update",
                "params": {"sessionId": "antigravity-session-1", "subagents": []},
            },
            EVENT_SUBAGENT_LIST,
        ),
        (
            {
                "jsonrpc": "2.0",
                "method": "_kiro.dev/session/update",
                "params": {
                    "sessionId": "sub-1",
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "sub output"},
                    },
                },
            },
            EVENT_SUBAGENT_ACTIVITY,
        ),
        (
            {
                "jsonrpc": "2.0",
                "method": "_kiro.dev/mcp/oauth_request",
                "params": {
                    "serverName": "srv",
                    "oauthUrl": "https://accounts.google.com/o/oauth2/v2/auth",
                },
            },
            EVENT_MCP_OAUTH_REQUEST,
        ),
        (
            {
                "jsonrpc": "2.0",
                "method": "_kiro.dev/mcp/server_initialized",
                "params": {"serverName": "srv"},
            },
            EVENT_MCP_SERVER_INITIALIZED,
        ),
        (
            {
                "jsonrpc": "2.0",
                "method": "_kiro.dev/mcp/server_init_failure",
                "params": {"serverName": "srv", "error": "boom"},
            },
            EVENT_MCP_SERVER_INIT_FAILURE,
        ),
    ],
)
async def test_antigravity_held_chunk_flushes_before_notification_and_completes(
    tmp_path: Path, extra_frame: dict[str, object], expected_kind: str
) -> None:
    """Flush a held candidate chunk before non-text notifications."""
    client, reader = _stream_client(tmp_path)
    frames = [
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "antigravity-session-1",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {
                        "type": "text",
                        "text": "API key not valid. Please pass a valid API key.",
                    },
                },
            },
        },
        extra_frame,
        {"jsonrpc": "2.0", "id": 18, "result": {"stopReason": "end_turn"}},
    ]
    prompt_id = _feed_prompt_frames(reader, frames)
    events = [
        event async for event in client._dispatch_events(prompt_id, timeout=FALLBACK_TIMEOUT_SECS)
    ]
    kinds = [e.kind for e in events]
    assert kinds.index(EVENT_TEXT_CHUNK) < kinds.index(expected_kind)
    assert events[-1].kind == EVENT_COMPLETE


def _install_eof_timeout_clock(monkeypatch: pytest.MonkeyPatch, client: AcpClient) -> None:
    """Advance the client's clock past deadline once EOF is reached."""
    import time as real_time
    from types import SimpleNamespace

    now = [1000.0]
    fake_time = SimpleNamespace(
        **{name: getattr(real_time, name) for name in dir(real_time) if not name.startswith("__")}
    )
    fake_time.monotonic = lambda: now[0]
    # Patched through the facade: ``time`` is bound by several client owners.
    monkeypatch.setattr(client_mod, "time", fake_time)
    orig_read = client._read_message

    async def _fast_eof_read(timeout: float = 0.0):
        msg = await orig_read(timeout=timeout)
        if msg is None:
            now[0] += 10_000.0
        return msg

    monkeypatch.setattr(client, "_read_message", _fast_eof_read)


@pytest.mark.asyncio
async def test_antigravity_held_chunk_flushes_on_eof_and_refusal_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flush a held chunk on EOF timeout and on refusal-terminal errors."""
    client, reader = _stream_client(tmp_path)
    _install_eof_timeout_clock(monkeypatch, client)
    eof_frames = [
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "antigravity-session-1",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {
                        "type": "text",
                        "text": "API key not valid. Please pass a valid API key.",
                    },
                },
            },
        }
    ]
    _feed_prompt_frames(reader, eof_frames)

    eof_events = [
        event async for event in client._dispatch_events(19, timeout=FALLBACK_TIMEOUT_SECS)
    ]
    assert [e.text for e in eof_events if e.kind == EVENT_TEXT_CHUNK] == [
        "API key not valid. Please pass a valid API key."
    ]
    assert eof_events[-1].kind == EVENT_COMPLETE
    assert eof_events[-1].synthetic_completion is True

    client_err, reader_err = _stream_client(tmp_path)
    err_frames = [
        *eof_frames,
        {"jsonrpc": "2.0", "id": 24, "error": {"code": -32603, "message": "Internal error"}},
    ]
    _feed_prompt_frames(reader_err, err_frames)
    seen_err: list[str] = []
    with pytest.raises(AcpError):
        async for event in client_err._dispatch_events(24, timeout=FALLBACK_TIMEOUT_SECS):
            if event.kind == EVENT_TEXT_CHUNK and event.text:
                seen_err.append(event.text)
    assert seen_err == ["API key not valid. Please pass a valid API key."]

    from kiro_crew.acp.types import RefusalInfo

    client2, reader2 = _stream_client(tmp_path)
    monkeypatch.setattr(
        client2,
        "_track_metadata",
        lambda _msg: setattr(
            client2.last_prompt_stats,
            "refusal",
            RefusalInfo(category="CYBER", explanation="blocked"),
        ),
    )
    refusal_frames = [
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "antigravity-session-1",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {
                        "type": "text",
                        "text": "API key not valid. Please pass a valid API key.",
                    },
                },
            },
        },
        {
            "jsonrpc": "2.0",
            "method": "_kiro.dev/metadata",
            "params": {"sessionId": "antigravity-session-1"},
        },
        {"jsonrpc": "2.0", "id": 20, "error": {"code": -32603, "message": "Internal error"}},
    ]
    _feed_prompt_frames(reader2, refusal_frames)
    refusal_events = [
        event async for event in client2._dispatch_events(20, timeout=FALLBACK_TIMEOUT_SECS)
    ]
    assert [e.text for e in refusal_events if e.kind == EVENT_TEXT_CHUNK] == [
        "API key not valid. Please pass a valid API key."
    ]
    assert refusal_events[-1].kind == EVENT_COMPLETE


@pytest.mark.asyncio
async def test_antigravity_read_prompt_response_and_stream_handle_unattributed_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catch unattributed errors on both send_message and send_message_stream paths."""
    from kiro_crew.acp.client_turn import _read_prompt_response

    client, reader = _stream_client(tmp_path)
    _feed_prompt_frames(
        reader,
        [
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": "antigravity-session-1",
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {
                            "type": "text",
                            "text": "API key not valid. Please pass a valid API key.",
                        },
                    },
                },
            },
            {"jsonrpc": "2.0", "id": 21, "result": {"stopReason": "end_turn"}},
        ],
    )
    with pytest.raises(AcpError, match="API key not valid") as exc_info:
        await _read_prompt_response(client, 21, timeout=FALLBACK_TIMEOUT_SECS)
    assert exc_info.value.transient is False

    # Timeout in _read_prompt_response keeps the held chunk in partial_output.
    client_eof, reader_eof = _stream_client(tmp_path)
    _install_eof_timeout_clock(monkeypatch, client_eof)
    _feed_prompt_frames(
        reader_eof,
        [
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": "antigravity-session-1",
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {
                            "type": "text",
                            "text": "API key not valid. Please pass a valid API key.",
                        },
                    },
                },
            }
        ],
    )
    with pytest.raises(AcpTimeoutError) as timeout_info:
        await _read_prompt_response(client_eof, 22, timeout=FALLBACK_TIMEOUT_SECS)
    assert timeout_info.value.partial_output == "API key not valid. Please pass a valid API key."

    # send_message_stream catches single-chunk error on turn two.
    client_s, reader_s = _stream_client(tmp_path)
    client_s._prompt_or_tool_seen = True
    client_s._send_prompt = AsyncMock(return_value=23)
    client_s.ensure_ready = AsyncMock()
    _feed_prompt_frames(
        reader_s,
        [
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": "antigravity-session-1",
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {
                            "type": "text",
                            "text": "API key not valid. Please pass a valid API key.",
                        },
                    },
                },
            },
            {"jsonrpc": "2.0", "id": 23, "result": {"stopReason": "end_turn"}},
        ],
    )
    with pytest.raises(AcpError, match="API key not valid"):
        async for _chunk in client_s.send_message_stream("hi", timeout=FALLBACK_TIMEOUT_SECS):
            pass


def test_antigravity_tested_range_notice_reads_one_parser() -> None:
    """The floor and the tested-range notice read a version through one parser."""
    for silent in ("1.4.4", "1.10.0", "1.4.4+build.1", " 1.5.0 "):
        _refuse_unverified_antigravity_version(_antigravity_client(silent))
        assert client_turn_control.untested_version_issue(ACP_BACKEND_ANTIGRAVITY, silent) == ""

    for above in (" 2.0.0 ", "2.1.0"):
        notice = client_turn_control.untested_version_issue(ACP_BACKEND_ANTIGRAVITY, above)
        assert "Google Antigravity" in notice
        assert "1.4.4 to 1.x" in notice
        assert "A Kiro Crew update moves the tested range." in notice

    assert client_claude_session._adapter_version_at_least(" 2.0.0 ", (2, 0, 0)) is True
    assert (
        client_claude_session._adapter_version_at_least("\u0661.\u0664.\u0663", (0, 0, 0)) is False
    )
    assert client_turn_control.untested_version_issue(ACP_BACKEND_ANTIGRAVITY, 123) == ""  # type: ignore[arg-type]
