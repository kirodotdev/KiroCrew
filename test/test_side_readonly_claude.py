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
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.acp import _dispatch
from kiro_crew.acp.client_claude_session import _CLAUDE_SIDE_READ_ONLY_ASK_RULES
from kiro_crew.acp.harness_tool_names import CLAUDE_READ_ONLY_BUILTINS
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


# --- In-cwd reads reach the gate (live capture, claude-agent-acp 0.94.0) ---

_CAPTURES = Path(__file__).parent / "fixtures" / "acp_frames" / "claude"


def _capture(name: str) -> tuple[dict, list[dict]]:
    """A probe capture: its header and its agent-to-client frames."""
    head, *frames = (
        json.loads(line) for line in (_CAPTURES / f"{name}.jsonl").read_text().splitlines()
    )
    return head["_meta"], frames


def _calls(frames: list[dict]) -> dict[str, dict]:
    """Per tool call id: its path, final status and whether it asked."""
    calls: dict[str, dict] = {}
    for frame in frames:
        if frame.get("method") == "session/request_permission":
            calls[frame["params"]["toolCall"]["toolCallId"]]["asked"] = True
            continue
        update = frame["params"]["update"]
        call = calls.setdefault(update["toolCallId"], {"asked": False, "path": "", "status": ""})
        raw = update.get("rawInput") or {}
        call["path"] = raw.get("file_path") or raw.get("path") or call["path"]
        call["status"] = update.get("status") or call["status"]
    return calls


def test_every_claude_read_builtin_carries_a_side_ask_rule():
    assert set(CLAUDE_READ_ONLY_BUILTINS) <= set(_CLAUDE_SIDE_READ_ONLY_ASK_RULES)


def test_the_capture_was_recorded_under_the_shipped_ask_rules():
    meta, _ = _capture("side-read-ask-live")
    assert meta["ask"] == list(_CLAUDE_SIDE_READ_ONLY_ASK_RULES)


def test_without_the_read_ask_rules_in_cwd_reads_never_ask():
    """Why the rules exist: ``default`` mode runs in-cwd reads natively."""
    meta, frames = _capture("side-read-no-ask-live")
    assert "Read" not in meta["ask"]
    in_cwd = [c for c in _calls(frames).values() if c["path"].startswith("/work")]
    assert len(in_cwd) == 4
    assert all(c["status"] == "completed" and not c["asked"] for c in in_cwd)


def test_with_the_read_ask_rules_every_read_asks_and_a_denied_path_is_refused_first():
    _, frames = _capture("side-read-ask-live")
    calls = list(_calls(frames).values())
    denied = [c for c in calls if c["path"].startswith("/denied")]
    assert [(c["asked"], c["status"]) for c in denied] == [(False, "failed")] * 3
    rest = [c for c in calls if c not in denied]
    assert len(rest) == 5 and all(c["asked"] for c in rest)


@pytest.mark.asyncio
async def test_each_captured_side_read_gets_a_read_only_decision_and_an_sel_row():
    _, frames = _capture("side-read-ask-live")
    names: dict = {}
    events = []
    for frame in frames:
        if frame.get("method") != "session/request_permission":
            _dispatch.parse_session_update(frame["params"]["update"], harness_tool_name_cache=names)
            continue
        msg = JsonRpcMessage(id=frame["id"], method=frame["method"], params=frame["params"])
        event, _ = _dispatch.build_permission_event(
            msg, harness_tool_name_cache=names, harness_backend="claude", side_read_only=True
        )
        events.append(event)
    assert len(events) == 5
    for event in events:
        assert event.harness_builtin_tool in ("fs_read", "grep", "glob")
        with patch("kiro_crew.sel.sel") as sel:
            provider = await _gate(event, ToolApprovalPolicy.READ_ONLY)
        rows = [c.kwargs for c in sel.return_value.log_tool_invocation.call_args_list]
        assert provider.approved == [event.request_id] and provider.rejected == []
        assert [(r["request_id"], r["outcome"]) for r in rows] == [
            (event.request_id, "auto_approved")
        ]


@pytest.mark.parametrize("tool", ["Grep", "Glob"])
def test_only_a_side_session_reads_a_claude_search_as_read(tool):
    assert _claude_event(tool, "search", side_read_only=True).tool_kind == "read"
    assert _claude_event(tool, "search", side_read_only=False).tool_kind == "search"


@pytest.mark.parametrize("tool", ["Read", "WebFetch", "Bash", "Write", None])
def test_no_other_claude_call_has_its_search_kind_changed(tool):
    assert _claude_event(tool, "search", side_read_only=True).tool_kind == "search"


def _captured_search_event(tool: str, path: str):
    """The captured Grep or Glob request, re-aimed at *path*."""
    _, frames = _capture("side-read-ask-live")
    names: dict = {}
    raw: dict = {}
    for frame in frames:
        frame = json.loads(json.dumps(frame))
        if frame.get("method") != "session/request_permission":
            update = frame["params"]["update"]
            if (
                update.get("rawInput")
                and update.get("_meta", {}).get("claudeCode", {}).get("toolName") == tool
            ):
                update["rawInput"]["path"] = path
            _dispatch.parse_session_update(
                update, harness_tool_name_cache=names, raw_params_cache=raw
            )
            continue
        call = frame["params"]["toolCall"]
        if call["name"] != tool:
            continue
        call["rawInput"]["path"] = path
        msg = JsonRpcMessage(id=frame["id"], method=frame["method"], params=frame["params"])
        event, _ = _dispatch.build_permission_event(
            msg,
            harness_tool_name_cache=names,
            raw_params_cache=raw,
            harness_backend="claude",
            side_read_only=True,
        )
        return event
    raise AssertionError(f"no {tool} request in the capture")


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["Grep", "Glob"])
@pytest.mark.parametrize("leaf", [".aws", ".ssh", ".aws/credentials"])
async def test_a_read_folded_search_on_a_credential_path_is_still_refused(tool, leaf):
    """The gate's own sensitive-path deny binds a folded Grep/Glob too, so the
    fold adds nothing where claude's deny rules might not reach."""
    from kiro_crew.security import sandbox_credential_targets

    path = str(Path.home() / leaf)
    assert any(Path(path).is_relative_to(t) for t in sandbox_credential_targets())
    event = _captured_search_event(tool, path)
    assert event.tool_kind == "read"
    with patch("kiro_crew.sel.sel") as sel:
        provider = await _gate(event, ToolApprovalPolicy.READ_ONLY)
    assert provider.approved == [] and provider.rejected == [event.request_id]
    (row,) = [c.kwargs for c in sel.return_value.log_tool_invocation.call_args_list]
    assert row["metadata"]["mechanism"] == "always_deny_hook"
