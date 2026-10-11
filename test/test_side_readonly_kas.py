"""KAS side turns under READ_ONLY: what the gate approves, and what it still refuses.

KAS joins ``ACP_BACKENDS_SIDE_READONLY`` because, bound to the derived
``<agent>--readonly`` spec, every tool call it serves raises
``session/request_permission``. The frames below are trimmed copies of a live
capture (kiro-cli 2.29.0, ``--agent-engine v3``) taken under that spec. Each one is
replayed through the REAL dispatch builders (``kas_consent_meta`` on, as the KAS
transports set it), the provider's event conversion and the REAL ``HookManager``,
so the verdict asserted is the one a side turn on KAS would get.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from kiro_crew import platform_compat
from kiro_crew.acp._dispatch import build_permission_event, parse_session_update
from kiro_crew.acp.types import JsonRpcMessage
from kiro_crew.acp_backends import ACP_BACKEND_KAS, ACP_BACKENDS_SIDE_READONLY
from kiro_crew.dashboard.side_readonly_spec import derive_readonly_spec
from kiro_crew.llm_helpers import ToolApprovalPolicy, stream_and_collect
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

_OPTIONS = [
    {"optionId": "accept", "name": "Allow", "kind": "allow_once"},
    {"optionId": "always-accept", "name": "Always allow", "kind": "allow_always"},
    {"optionId": "reject", "name": "Deny", "kind": "reject_once"},
    {"optionId": "always-reject", "name": "Always deny", "kind": "reject_always"},
]


def _tool_call(call_id: str, title: str, kind: str, raw_input: dict[str, Any]) -> dict[str, Any]:
    return {
        "sessionUpdate": "tool_call",
        "toolCallId": call_id,
        "title": title,
        "kind": kind,
        "status": "pending",
        "rawInput": raw_input,
        "_meta": {"kiro": {"toolOrigin": "default"}},
    }


def _permission(call_id: str, title: str, kiro_meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": "7",
        "method": "session/request_permission",
        "params": {
            "sessionId": "sess_probe",
            "toolCall": {"toolCallId": call_id, "status": "pending", "title": title},
            "options": _OPTIONS,
            "_meta": {"kiro": kiro_meta},
        },
    }


def _shell(call_id: str, command: str) -> tuple[dict[str, Any], dict[str, Any]]:
    return (
        _tool_call(call_id, "Run command", "execute", {"command": command}),
        _permission(
            call_id,
            command,
            {
                "toolId": "run_command",
                "command": command,
                "consent": {"capability": "shell", "resource": command, "askType": "implicit"},
            },
        ),
    )


_READ_FILE = (
    _tool_call("read_file_t1", "Read File", "read", {"path": "probe.txt"}),
    _permission(
        "read_file_t1",
        "Read File",
        {
            "toolId": "read_file",
            "consent": {"capability": "fs_read", "resource": "probe.txt", "askType": "implicit"},
        },
    ),
)

_GREP = (
    _tool_call("grep_search_t1", "Grep Search", "search", {"pattern": "hello", "path": "."}),
    _permission(
        "grep_search_t1",
        "Grep Search",
        {
            "toolId": "grep_search",
            "consent": {"capability": "fs_read", "resource": ".", "askType": "implicit"},
        },
    ),
)

_WRITE = (
    _tool_call("fs_write_t1", "Write File", "edit", {"path": "out.txt", "content": "x"}),
    _permission(
        "fs_write_t1",
        "Write File",
        {
            "toolId": "fs_write",
            "consent": {"capability": "fs_write", "resource": "out.txt", "askType": "implicit"},
        },
    ),
)


def _replay(tool_call: dict[str, Any], permission: dict[str, Any]) -> LLMEvent:
    """The permission event a KAS transport hands the gate for this frame pair."""
    caches: dict[str, Any] = {
        "tool_input_cache": {},
        "tool_input_redacted_cache": {},
        "shell_cache": {},
        "raw_params_cache": {},
        "mcp_server_name_cache": {},
        "tool_name_cache": {},
        "diff_path_cache": {},
        "harness_tool_name_cache": {},
        "cache_scope": "sess_probe",
    }
    parse_session_update(tool_call, **caches)
    event, _recorded = build_permission_event(
        JsonRpcMessage.from_dict(permission),
        **caches,
        kas_consent_meta=True,
        harness_backend=ACP_BACKEND_KAS,
    )
    assert event is not None
    return AcpProvider._to_llm_event(event)


class _ScriptedProvider:
    def __init__(self, event: LLMEvent) -> None:
        self._events = [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="on it"),
            event,
            LLMEvent(kind=EVENT_COMPLETE, text=""),
        ]
        self.approved: list[str] = []
        self.rejected: list[str] = []

    async def stream(self, message: str) -> AsyncIterator[LLMEvent]:
        for event in self._events:
            yield event

    async def approve_tool(self, request_id: str) -> None:
        self.approved.append(request_id)

    async def reject_tool(self, request_id: str) -> None:
        self.rejected.append(request_id)


def _hooks():
    from kiro_crew.hooks import HookManager, HooksConfig

    return HookManager(HooksConfig())


async def _side_turn(pair: tuple[dict[str, Any], dict[str, Any]]) -> _ScriptedProvider:
    provider = _ScriptedProvider(_replay(*pair))
    await stream_and_collect(
        provider,  # type: ignore[arg-type]
        "q",
        approval_policy=ToolApprovalPolicy.READ_ONLY,
        hooks=_hooks(),
        retry_transient=False,
    )
    return provider


def test_kas_is_a_side_readonly_member():
    assert ACP_BACKEND_KAS in ACP_BACKENDS_SIDE_READONLY


def test_the_derived_spec_projects_no_kas_allow_rule():
    """The grant KAS would honour without asking is gone from the projected agent."""
    from kiro_crew.acp.kas_agents import to_client_custom_agent

    base = {
        "name": "probe",
        "prompt": "p",
        "tools": ["*"],
        "allowedTools": ["web_fetch", "use_subagent"],
        "permissions": {"rules": [{"capability": "web_fetch", "effect": "allow"}]},
    }
    derived = derive_readonly_spec(base, base_name="probe")
    agent = to_client_custom_agent(derived["name"], derived, "p")

    assert not (agent.get("permissions") or {}).get("rules")


@pytest.mark.skipif(
    platform_compat.IS_WINDOWS,
    reason="the name grant vouches for `ls` through a POSIX PATH lookup",
)
@pytest.mark.asyncio
async def test_kas_side_turn_approves_a_read_only_shell_command():
    provider = await _side_turn(_shell("run_command_t1", "ls -la"))

    assert provider.approved == ["7"]
    assert provider.rejected == []


@pytest.mark.parametrize(
    "pair",
    [
        _WRITE,
        _shell("run_command_t2", "touch out.txt"),
        _shell("run_command_t3", "rm -rf build"),
        _shell("run_command_t4", "python3 -c 'print(1)'"),
    ],
    ids=["fs_write", "shell_write", "shell_delete", "shell_exec"],
)
@pytest.mark.asyncio
async def test_kas_side_turn_refuses_writes_and_exec(pair):
    provider = await _side_turn(pair)

    assert provider.approved == []
    assert provider.rejected == ["7"]


@pytest.mark.parametrize("pair", [_READ_FILE, _GREP], ids=["read_file", "grep_search"])
@pytest.mark.asyncio
async def test_kas_side_turn_still_refuses_file_read_and_grep(pair):
    """The frames name no ``_meta.kiro.toolName``, so the gate cannot prove the read.
    Proving it from the engine's ``toolId`` is a separate, unmade change."""
    provider = await _side_turn(pair)

    assert provider.approved == []
    assert provider.rejected == ["7"]


@pytest.mark.parametrize(
    "pair",
    [_WRITE, _shell("run_command_t2", "touch out.txt"), _shell("run_command_t4", "python3 -V")],
    ids=["fs_write", "shell_write", "shell_exec"],
)
@pytest.mark.asyncio
async def test_kas_normal_turn_still_asks_before_a_write_or_exec(pair):
    """A main-chat turn (HOOK_BASED) is untouched: the call reaches the approver,
    and a declined card leaves it rejected rather than auto-approved."""
    asked: list[LLMEvent] = []

    async def _decline(event: LLMEvent) -> bool:
        asked.append(event)
        return False

    provider = _ScriptedProvider(_replay(*pair))
    await stream_and_collect(
        provider,  # type: ignore[arg-type]
        "q",
        approval_policy=ToolApprovalPolicy.HOOK_BASED,
        hooks=_hooks(),
        on_tool_approval=_decline,
        retry_transient=False,
    )

    assert len(asked) == 1
    assert provider.approved == []
    assert provider.rejected == ["7"]
