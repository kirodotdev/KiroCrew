"""The safe scope of a claude Side Chat turn, end to end through the gate.

A claude tool_call frame and its permission request go through the real ACP
parser, the provider's event conversion and ``stream_and_collect``. On a
``side_read_only`` session under READ_ONLY the read built-ins run, and every
write, shell and MCP call is refused. On a normal (main-chat) session the frame
yields the same permission event as one with no claude stamp at all, so the
normal-turn gate sees exactly the input it saw before and reaches the same
verdict.
"""

from __future__ import annotations

import dataclasses

import pytest

from kiro_crew.acp import _dispatch
from kiro_crew.acp.types import JsonRpcMessage
from kiro_crew.llm_helpers import ToolApprovalPolicy, stream_and_collect
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

_READS = [("Read", "read"), ("WebFetch", "fetch"), ("WebSearch", "fetch")]
_WRITES_AND_EXEC = [
    ("Write", "edit"),
    ("Edit", "edit"),
    ("MultiEdit", "edit"),
    ("NotebookEdit", "edit"),
    ("Bash", "execute"),
    ("Task", "think"),
    ("mcp__docs__write_page", "other"),
]


def _claude_event(tool: str | None, kind: str, *, side_read_only: bool):
    """One claude frame pair through the real parser; ``tool=None`` is a frame
    that carries no ``_meta.claudeCode.toolName`` stamp."""
    names: dict = {}
    meta = {"claudeCode": {"toolName": tool}} if tool else {}
    _dispatch.parse_session_update(
        {
            "_meta": meta,
            "toolCallId": "toolu_1",
            "sessionUpdate": "tool_call",
            "rawInput": {},
            "status": "pending",
            "title": tool or "call",
            "kind": kind,
            "content": [],
            "locations": [],
        },
        harness_tool_name_cache=names,
    )
    msg = JsonRpcMessage(
        id=0,
        method="session/request_permission",
        params={
            "toolCall": {
                "toolCallId": "toolu_1",
                "title": tool or "call",
                "kind": kind,
                "rawInput": {},
            },
            "options": [
                {"optionId": "allow-once", "name": "Yes", "kind": "allow_once"},
                {"optionId": "reject-once", "name": "No", "kind": "reject_once"},
            ],
        },
    )
    event, _ = _dispatch.build_permission_event(
        msg,
        harness_tool_name_cache=names,
        harness_backend="claude",
        side_read_only=side_read_only,
    )
    assert event is not None
    return event


class _ScriptedProvider:
    def __init__(self, event: LLMEvent) -> None:
        self._events = [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="on it"),
            event,
            LLMEvent(kind=EVENT_COMPLETE, text=""),
        ]
        self.approved: list[str] = []
        self.rejected: list[str] = []

    async def stream(self, message: str):
        for event in self._events:
            yield event

    async def approve_tool(self, request_id: str) -> None:
        self.approved.append(request_id)

    async def reject_tool(self, request_id: str) -> None:
        self.rejected.append(request_id)


async def _gate(acp_event, policy: ToolApprovalPolicy) -> _ScriptedProvider:
    from kiro_crew.hooks import HookManager, HooksConfig

    llm_event = AcpProvider._to_llm_event(acp_event)
    provider = _ScriptedProvider(llm_event)
    await stream_and_collect(
        provider,  # type: ignore[arg-type]
        "q",
        approval_policy=policy,
        hooks=HookManager(HooksConfig()),
        retry_transient=False,
    )
    return provider


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool", "kind"), _READS)
async def test_a_claude_side_turn_runs_its_read_tools(tool, kind):
    provider = await _gate(
        _claude_event(tool, kind, side_read_only=True), ToolApprovalPolicy.READ_ONLY
    )
    assert provider.approved == [provider._events[1].request_id]
    assert provider.rejected == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool", "kind"), _WRITES_AND_EXEC)
async def test_a_claude_side_turn_refuses_writes_shell_and_mcp(tool, kind):
    event = _claude_event(tool, kind, side_read_only=True)
    assert event.harness_builtin_tool == ""
    provider = await _gate(event, ToolApprovalPolicy.READ_ONLY)
    assert provider.approved == []
    assert provider.rejected == [provider._events[1].request_id]


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool", "kind"), _WRITES_AND_EXEC)
async def test_a_claude_side_turn_refuses_a_write_dressed_as_a_read(tool, kind):
    """A write frame whose permission request claims ``kind="read"`` still has
    no read built-in behind it, so READ_ONLY refuses it."""
    event = _claude_event(tool, "read", side_read_only=True)
    assert event.harness_builtin_tool == ""
    provider = await _gate(event, ToolApprovalPolicy.READ_ONLY)
    assert provider.approved == []


@pytest.mark.parametrize(("tool", "kind"), _READS + _WRITES_AND_EXEC)
def test_a_normal_turn_event_is_unchanged_by_the_claude_stamp(tool, kind):
    """Off a side session the stamp names nothing, so the gate input of a
    main-chat claude call equals that of a frame with no stamp at all."""
    stamped = _claude_event(tool, kind, side_read_only=False)
    unstamped = _claude_event(None, kind, side_read_only=False)
    assert stamped.harness_builtin_tool == ""
    ignore = {"title", "wire_title"}
    as_dict = {k: v for k, v in dataclasses.asdict(stamped).items() if k not in ignore}
    bare = {k: v for k, v in dataclasses.asdict(unstamped).items() if k not in ignore}
    assert as_dict == bare


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", [ToolApprovalPolicy.HOOK_BASED, ToolApprovalPolicy.READ_ONLY])
@pytest.mark.parametrize(("tool", "kind"), _READS + _WRITES_AND_EXEC)
async def test_a_normal_turn_gate_decides_as_without_the_stamp(tool, kind, policy):
    """Off a side session the gate's verdict on a stamped claude call is the
    verdict on the same call with no stamp, under either policy."""
    stamped = await _gate(_claude_event(tool, kind, side_read_only=False), policy)
    bare = await _gate(_claude_event(None, kind, side_read_only=False), policy)
    assert (stamped.approved, stamped.rejected) == (bare.approved, bare.rejected)
