"""A claude MCP call's (server, tool) identity, read off claude-agent-acp's own ``_meta``.

claude-agent-acp's ``tool_call`` frame names an MCP tool only in its fused
``_meta.claudeCode.toolName``; its ``session/request_permission`` adds
``_meta.claudeCode.mcpServer`` ``{name, source}``. ``build_permission_event`` adopts
that pair on a claude session only when the server is one Crew placed on the
session array (``source == "dynamic"`` and a placed name) and the tool name is
exactly ``mcp__<server>__<tool>`` as the tool_call frame stamped it. Every other
frame keeps the empty identity it had before.

The positive cases replay the two live captures (claude-agent-acp 0.94.0); the
gate-level cases run the real hook gate, heartbeat allowlist and permission
resolution on the event the parser built.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import kiro_crew.sel as sel_mod
from kiro_crew.acp import _dispatch as d
from kiro_crew.acp.client_permissions import _foreign_mcp_identity
from kiro_crew.acp.types import JsonRpcMessage
from kiro_crew.acp_backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_GOOSE, ACP_BACKEND_KIRO
from kiro_crew.hook_runtime.tool_identity import hook_gate_kwargs
from kiro_crew.hooks import TOOL_ALLOW, TOOL_AUTO_APPROVE, TOOL_DENY, HookManager, HooksConfig
from kiro_crew.llm_helpers import ToolApprovalPolicy, _resolve_permission
from kiro_crew.slack.gateway import _is_heartbeat_safe_tool

_CAPTURES = Path(__file__).parent / "fixtures" / "acp_frames" / "claude"


def _capture_pair(name: str) -> tuple[dict, dict]:
    """The MCP call's ``tool_call`` update and its permission request, from a capture."""
    frames = [json.loads(line) for line in (_CAPTURES / f"{name}.jsonl").read_text().splitlines()]
    permission = next(f for f in frames if f.get("method") == "session/request_permission")
    call_id = permission["params"]["toolCall"]["toolCallId"]
    tool_call = next(
        f["params"]["update"]
        for f in frames
        if f.get("method") == "session/update"
        and f["params"]["update"].get("sessionUpdate") == "tool_call"
        and f["params"]["update"].get("toolCallId") == call_id
    )
    return tool_call, permission


def _replay(
    tool_call: dict | None,
    permission: dict,
    *,
    placed: tuple[str, ...] = ("crew-probe",),
    backend: str = ACP_BACKEND_CLAUDE,
):
    caches: dict = dict(
        tool_input_cache={},
        shell_cache={},
        raw_params_cache={},
        mcp_server_name_cache={},
        tool_name_cache={},
        tool_input_redacted_cache={},
        diff_path_cache={},
        harness_tool_name_cache={},
    )
    if tool_call is not None:
        d._build_tool_call_event(copy.deepcopy(tool_call), **caches)
    event, _ = d.build_permission_event(
        JsonRpcMessage.from_dict(copy.deepcopy(permission)),
        harness_backend=backend,
        harness_mcp_servers=placed,
        **caches,
    )
    return event


def _claude_meta(permission: dict) -> dict:
    return permission["params"]["toolCall"]["_meta"]["claudeCode"]


_PROBE_CALL, _PROBE_PERMISSION = _capture_pair("mcp-stdio-mount-live")


def _with_meta(**changes) -> dict:
    """The probe's permission request with its ``claudeCode`` block changed."""
    permission = copy.deepcopy(_PROBE_PERMISSION)
    _claude_meta(permission).update(changes)
    return permission


def _core_pair(tool: str, args: dict, *, server: str = "kirocrew-core") -> tuple[dict, dict]:
    """A claude MCP call on *server*, shaped exactly as the probe capture is."""
    fused = f"mcp__{server}__{tool}"
    tool_call = copy.deepcopy(_PROBE_CALL)
    tool_call.update(name=fused, title=fused, rawInput=args)
    tool_call["_meta"]["claudeCode"]["toolName"] = fused
    permission = copy.deepcopy(_PROBE_PERMISSION)
    permission["params"]["toolCall"].update(name=fused, title=fused, rawInput=args)
    _claude_meta(permission).update(toolName=fused, mcpServer={"name": server, "source": "dynamic"})
    return tool_call, permission


class TestTheCapturedFramesCarryTheIdentity:
    def test_the_probe_capture_names_its_server_and_tool(self):
        event = _replay(_PROBE_CALL, _PROBE_PERMISSION)
        assert (event.mcp_server_name, event.tool_name) == ("crew-probe", "crew_probe_echo")
        assert event.mcp_identity_trusted is True
        assert d.identified_mcp_call(event) == ("crew-probe", "crew_probe_echo")
        assert d.is_mcp_tool_approval(JsonRpcMessage.from_dict(_PROBE_PERMISSION), event)

    def test_the_saved_spec_capture_names_its_server_and_tool(self):
        tool_call, permission = _capture_pair("saved-spec-projection-live")
        event = _replay(tool_call, permission, placed=("spec-probe",))
        assert (event.mcp_server_name, event.tool_name) == ("spec-probe", "spec_probe_echo")
        assert event.mcp_identity_trusted is True

    def test_a_placed_server_is_not_refused_as_foreign(self):
        event = _replay(_PROBE_CALL, _PROBE_PERMISSION)
        client = SimpleNamespace(
            backend=ACP_BACKEND_CLAUDE, _session_mcp_servers=lambda: [{"name": "crew-probe"}]
        )
        assert _foreign_mcp_identity(client, event) is False  # type: ignore[arg-type]


class TestEveryOtherFrameKeepsNoIdentity:
    """Each leg of the check fails closed to the pre-existing empty identity."""

    @pytest.mark.parametrize(
        "permission",
        [
            pytest.param(_with_meta(mcpServer={"name": "crew-probe", "source": "user"}), id="user"),
            pytest.param(_with_meta(mcpServer={"name": "crew-probe"}), id="no-source"),
            pytest.param(_with_meta(mcpServer="crew-probe"), id="server-not-a-block"),
            pytest.param(_with_meta(mcpServer={"name": "", "source": "dynamic"}), id="no-name"),
            pytest.param(
                _with_meta(toolName="mcp__other__crew_probe_echo"), id="prefix-names-another"
            ),
            pytest.param(_with_meta(toolName="mcp__crew-probe__"), id="no-tool-half"),
            pytest.param(_with_meta(toolName="crew_probe_echo"), id="unfused-name"),
            pytest.param(
                _with_meta(toolName="mcp__crew-probe__x" * 200), id="tool-name-differs-and-long"
            ),
        ],
    )
    def test_a_frame_failing_one_leg_names_nothing(self, permission):
        event = _replay(_PROBE_CALL, permission)
        assert (event.mcp_server_name, event.tool_name) == ("", "")
        assert d.identified_mcp_call(event) is None

    def test_an_unplaced_server_names_nothing_and_is_not_refused(self):
        # A user's own ``~/.claude`` server Crew never placed: today's reading,
        # and no foreign-server refusal starts firing on it.
        event = _replay(_PROBE_CALL, _PROBE_PERMISSION, placed=("kirocrew-core",))
        assert (event.mcp_server_name, event.tool_name) == ("", "")
        client = SimpleNamespace(
            backend=ACP_BACKEND_CLAUDE, _session_mcp_servers=lambda: [{"name": "kirocrew-core"}]
        )
        assert _foreign_mcp_identity(client, event) is False  # type: ignore[arg-type]

    def test_a_request_disagreeing_with_its_tool_call_names_nothing(self):
        tool_call = copy.deepcopy(_PROBE_CALL)
        tool_call["_meta"]["claudeCode"]["toolName"] = "mcp__crew-probe__other_tool"
        event = _replay(tool_call, _PROBE_PERMISSION)
        assert (event.mcp_server_name, event.tool_name) == ("", "")

    def test_a_request_no_tool_call_preceded_names_nothing(self):
        event = _replay(None, _PROBE_PERMISSION)
        assert (event.mcp_server_name, event.tool_name) == ("", "")
        assert event.mcp_identity_trusted is False

    @pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_GOOSE, ""])
    def test_the_same_meta_on_another_backend_names_nothing(self, backend):
        event = _replay(_PROBE_CALL, _PROBE_PERMISSION, backend=backend)
        assert (event.mcp_server_name, event.tool_name) == ("", "")

    def test_a_title_naming_another_server_cannot_change_the_pair(self):
        permission = copy.deepcopy(_PROBE_PERMISSION)
        permission["params"]["toolCall"]["title"] = "mcp__kirocrew-core__spawn_list"
        tool_call = copy.deepcopy(_PROBE_CALL)
        tool_call["title"] = "mcp__kirocrew-core__spawn_list"
        event = _replay(tool_call, permission, placed=("crew-probe", "kirocrew-core"))
        assert (event.mcp_server_name, event.tool_name) == ("crew-probe", "crew_probe_echo")


class TestTheGateReadsTheIdentity:
    def test_an_operator_grant_on_the_server_auto_approves(self):
        event = _replay(_PROBE_CALL, _PROBE_PERMISSION)
        manager = HookManager(HooksConfig(auto_approve_tools=["@crew-probe/*"]))
        assert manager.on_tool_call(event.title, **hook_gate_kwargs(event)).action == (
            TOOL_AUTO_APPROVE
        )

    def test_the_grant_does_not_reach_an_unplaced_server(self):
        event = _replay(_PROBE_CALL, _PROBE_PERMISSION, placed=())
        manager = HookManager(HooksConfig(auto_approve_tools=["@crew-probe/*"]))
        assert manager.on_tool_call(event.title, **hook_gate_kwargs(event)).action != (
            TOOL_AUTO_APPROVE
        )

    def test_an_operator_confirm_list_still_asks(self):
        event = _replay(_PROBE_CALL, _PROBE_PERMISSION)
        manager = HookManager(
            HooksConfig(auto_approve_tools=["@crew-probe/*"], confirm_tools=["@crew-probe"])
        )
        assert manager.on_tool_call(event.title, **hook_gate_kwargs(event)).action == TOOL_ALLOW

    def test_an_operator_deny_on_the_server_denies(self):
        event = _replay(_PROBE_CALL, _PROBE_PERMISSION)
        manager = HookManager(HooksConfig(auto_deny_tools=["@crew-probe/crew_probe_echo"]))
        assert manager.on_tool_call(event.title, **hook_gate_kwargs(event)).action == TOOL_DENY


class TestHeartbeatReadsTheIdentity:
    def test_a_core_read_is_heartbeat_safe(self):
        event = _replay(*_core_pair("spawn_list", {}), placed=("kirocrew-core",))
        assert (event.mcp_server_name, event.tool_name) == ("kirocrew-core", "spawn_list")
        assert _is_heartbeat_safe_tool(event) is True

    def test_the_same_name_on_another_server_is_not(self):
        event = _replay(*_core_pair("spawn_list", {}, server="notes"), placed=("notes",))
        assert (event.mcp_server_name, event.tool_name) == ("notes", "spawn_list")
        assert _is_heartbeat_safe_tool(event) is False

    def test_an_unplaced_core_name_is_not(self):
        event = _replay(*_core_pair("spawn_list", {}), placed=())
        assert _is_heartbeat_safe_tool(event) is False


class _Provider:
    def __init__(self) -> None:
        self.approved: list = []
        self.rejected: list = []

    async def approve_tool(self, request_id) -> None:
        self.approved.append(request_id)

    async def reject_tool(self, request_id) -> None:
        self.rejected.append(request_id)


async def _resolve(event) -> bool:
    sel_stub = MagicMock()
    with patch.object(sel_mod, "sel", lambda: sel_stub):
        return await _resolve_permission(
            _Provider(),  # type: ignore[arg-type]
            event,
            ToolApprovalPolicy.AUTO_APPROVE,
            None,
        )


_QUOTED = "Cleanup example: rm -rf /"


class TestTheDocumentBodyExemptionApplies:
    @pytest.mark.asyncio
    async def test_a_core_message_quoting_a_command_is_sent(self):
        args = {"text": _QUOTED, "channel": "general"}
        event = _replay(*_core_pair("send_message", args), placed=("kirocrew-core",))
        assert await _resolve(event) is True

    @pytest.mark.asyncio
    async def test_the_same_text_in_a_non_body_field_is_refused(self):
        args = {"text": "hello", "channel": _QUOTED}
        event = _replay(*_core_pair("send_message", args), placed=("kirocrew-core",))
        assert await _resolve(event) is False

    @pytest.mark.asyncio
    async def test_an_unplaced_core_server_keeps_the_full_scan(self):
        args = {"text": _QUOTED, "channel": "general"}
        event = _replay(*_core_pair("send_message", args), placed=())
        assert await _resolve(event) is False
