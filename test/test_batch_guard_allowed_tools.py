"""The batch write guard sees a write the agent spec auto-approves.

A tool the spec's ``allowedTools`` names is approved by the engine itself: on KAS
the projection turns it into a ``permissions`` ``allow`` rule, so no permission
request reaches Kiro Crew and ``batch_guard.screen_approval`` never runs. With
``hooks.batch_write_guard`` on, the projection must keep every write-class tool of
the guard's table out of that auto-approval, so the call comes back as a request.
Crew then answers it itself: alone it is approved with no prompt, and batched with
a read it is refused with the guard's cause. With the guard off nothing changes,
and read-class tools stay auto-approved either way.
"""

from __future__ import annotations

import asyncio
import importlib
import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp import kas_agents
from kiro_crew.acp.types import (
    EVENT_PERMISSION_REQUEST,
    AcpEvent,
    JsonRpcMessage,
)
from kiro_crew.constants import DENY_CAUSE_BATCH_READ_WRITE
from kiro_crew.hooks import (
    TOOL_ALLOW,
    TOOL_AUTO_APPROVE,
    HookManager,
    HooksConfig,
    hook_gate_kwargs,
)

_SEND = ("kirocrew-dashboard", "session_send")
_LEDGER_READ = ("kirocrew-work", "work_ledger_read")


def _guard():
    return importlib.import_module("kiro_crew.acp.batch_guard")


@pytest.fixture
def guard_on(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_guard(), "guard_settings", lambda: (True, ()))


@pytest.fixture
def guard_off(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_guard(), "guard_settings", lambda: (False, ()))


def _spec(**extra: Any) -> dict[str, Any]:
    return {
        "name": "lead",
        "prompt": "p",
        "tools": ["@kirocrew-dashboard", "@kirocrew-work", "@kirocrew-core"],
        "allowedTools": ["@kirocrew-dashboard/session_send", "@kirocrew-work"],
        **extra,
    }


def _project(spec: dict[str, Any]) -> dict[str, Any]:
    return kas_agents.to_client_custom_agent("lead", spec, "p")


def _glob(pattern: str) -> re.Pattern[str]:
    # KAS resource globs: ``*`` stays inside one path segment.
    return re.compile("".join("[^/]*" if c == "*" else re.escape(c) for c in pattern) + r"\Z")


def _effect(entry: dict[str, Any], server: str, tool: str) -> str:
    """What the projected policy resolves for one MCP resource, as KAS does:
    the most restrictive matching rule, ``ask`` when nothing matches."""
    resource = f"{server}/{tool}"
    effects = set()
    for rule in (entry.get("permissions") or {}).get("rules", []):
        if rule.get("capability") != "mcp":
            continue
        match = rule.get("match")
        if match and not any(_glob(p).match(resource) for p in match):
            continue
        if any(_glob(p).match(resource) for p in rule.get("exclude") or ()):
            continue
        effects.add(rule["effect"])
    for effect in ("deny", "ask", "allow"):
        if effect in effects:
            return effect
    return "ask"


def _permission(request_id: int, call_id: str, server: str, tool: str, granted) -> AcpEvent:
    return AcpEvent(
        kind=EVENT_PERMISSION_REQUEST,
        request_id=request_id,
        tool_call_id=call_id,
        title=f"Running: @{server}/{tool}",
        mcp_server_name=server,
        tool_name=tool,
        mcp_identity_trusted=True,
        kas_builtin_ids=True,
        spec_granted_builtins=frozenset(granted),
    )


def _judge(event: AcpEvent):
    return HookManager(HooksConfig()).on_tool_call(event.title, **hook_gate_kwargs(event))


def _frame(call_id: str, server: str, tool: str) -> JsonRpcMessage:
    return JsonRpcMessage(
        method="session/update",
        params={
            "sessionId": "s1",
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": call_id,
                "title": f"Running: @{server}/{tool}",
                "rawInput": {},
                "_meta": {"kiro": {"toolName": tool, "mcpServerName": server}},
            },
        },
    )


def _handle():
    from kiro_crew.acp.session_handle import AcpSessionHandle

    runtime = MagicMock()
    runtime.send_response = AsyncMock()
    runtime.send_error = AsyncMock()
    return AcpSessionHandle("s1", asyncio.Queue(), runtime)


# ── (a) a write batched with a read, both granted ──────────────────────────────


@pytest.mark.asyncio
async def test_a_granted_write_batched_with_a_granted_read_is_refused(guard_on, monkeypatch):
    entry = _project(_spec())
    # The engine asks before the write runs, and still runs the read on its own.
    assert _effect(entry, *_SEND) == "ask"
    assert _effect(entry, *_LEDGER_READ) == "allow"
    granted = kas_agents.projected_crew_answered([entry], "lead")

    handle = _handle()
    monkeypatch.setattr(handle, "steer", AsyncMock(return_value=True), raising=False)
    handle._handle_update(_frame("tc-read", *_LEDGER_READ))
    handle._handle_update(_frame("tc-send", *_SEND))
    event = _permission(51, "tc-send", *_SEND, granted)
    # Crew answers the request it now receives, so the transport is asked ...
    assert _judge(event).action == TOOL_AUTO_APPROVE
    handle._permission_gate_events[51] = event
    # ... and refuses it, with the guard's own cause.
    assert await handle.approve_tool(51) is False
    reason = handle.take_batch_refusal(51)
    assert reason and "batch guard" in reason
    _outcome, metadata = _guard().refusal_outcome(reason)
    assert metadata == {"mechanism": "batch_write_guard", "cause": DENY_CAUSE_BATCH_READ_WRITE}


def test_an_authored_allow_on_a_whole_server_still_asks_for_its_writes(guard_on):
    # The shape of the generated team-lead spec: ``@kirocrew-core`` granted and
    # the derived block allowing ``kirocrew-core/*``.
    entry = _project(
        _spec(
            allowedTools=["@kirocrew-core", "@kirocrew-work/work_ledger_record"],
            permissions={
                "rules": [
                    {
                        "capability": "mcp",
                        "match": ["kirocrew-core/*", "kirocrew-work/work_ledger_record"],
                        "effect": "allow",
                    }
                ]
            },
        )
    )
    assert _effect(entry, "kirocrew-core", "send_message") == "ask"
    assert _effect(entry, "kirocrew-core", "session_ledger_record") == "ask"
    assert _effect(entry, "kirocrew-work", "work_ledger_record") == "ask"
    assert _effect(entry, "kirocrew-core", "memory_recall") == "allow"
    assert _effect(entry, "kirocrew-core", "spawn_run") == "allow"
    granted = kas_agents.projected_crew_answered([entry], "lead")
    assert {
        "@kirocrew-core/send_message",
        "@kirocrew-core/session_ledger_record",
        "@kirocrew-work/work_ledger_record",
    } <= granted


# ── (b) the same write alone: no prompt, no refusal ────────────────────────────


@pytest.mark.asyncio
async def test_a_granted_write_alone_is_approved_without_a_prompt(guard_on, monkeypatch):
    granted = kas_agents.projected_crew_answered([_project(_spec())], "lead")
    event = _permission(52, "tc-send", *_SEND, granted)
    verdict = _judge(event)
    assert verdict.action == TOOL_AUTO_APPROVE
    assert verdict.tier == "batch-guard-grant"

    handle = _handle()
    handle._handle_update(_frame("tc-send", *_SEND))
    handle._permission_gate_events[52] = event
    assert await handle.approve_tool(52) is True


def test_a_write_the_spec_never_granted_still_asks_a_person(guard_on):
    granted = kas_agents.projected_crew_answered(
        [_project(_spec(allowedTools=["@kirocrew-work/work_ledger_read"]))], "lead"
    )
    assert "@kirocrew-dashboard/session_send" not in granted
    verdict = _judge(_permission(53, "tc-send", *_SEND, granted))
    assert verdict.action == TOOL_ALLOW


def test_an_authored_ask_on_the_write_is_not_answered_by_crew(guard_on):
    entry = _project(
        _spec(
            permissions={
                "rules": [
                    {
                        "capability": "mcp",
                        "match": ["kirocrew-dashboard/session_send"],
                        "effect": "ask",
                    }
                ]
            }
        )
    )
    granted = kas_agents.projected_crew_answered([entry], "lead")
    assert "@kirocrew-dashboard/session_send" not in granted


@pytest.mark.parametrize(
    "rule",
    [
        {"capability": "mcp", "match": [], "effect": "ask"},
        {"capability": "mcp", "match": ["**"], "effect": "ask"},
        {"capability": "mcp", "match": ["kirocrew-*/session_*"], "effect": "deny"},
        {"capability": "mcp", "match": ["kirocrew-dashboard/[s]ession_send"], "effect": "ask"},
        {"capability": "all", "effect": "ask"},
    ],
    ids=["empty-match", "recursive-glob", "wildcards", "unread-glob", "meta-all"],
)
def test_an_authored_catch_all_hold_is_never_answered_by_crew(guard_on, rule):
    # KAS reads an empty match and ``**`` as every resource; a glob this code
    # cannot read counts as covering, so the author's hold keeps prompting.
    entry = _project(_spec(permissions={"rules": [rule]}))
    granted = kas_agents.projected_crew_answered([entry], "lead")
    assert "@kirocrew-dashboard/session_send" not in granted


# ── (c) guard off: the projection is unchanged ────────────────────────────────


def test_with_the_guard_off_the_projection_is_unchanged(guard_off, monkeypatch):
    spec = _spec()
    off = _project(spec)
    # The same projection with the guard's step taken out entirely.
    guard = _guard()
    monkeypatch.setattr(guard, "withhold_write_auto_approval", lambda policy, **_k: (policy, []))
    monkeypatch.setattr(guard, "guard_settings", lambda: (True, ()))
    assert _project(spec) == off
    assert _effect(off, *_SEND) == "allow"
    assert "@kirocrew-dashboard/session_send" not in kas_agents.projected_crew_answered(
        [off], "lead"
    )


# ── (d) read-class tools stay auto-approved ───────────────────────────────────


@pytest.mark.parametrize(
    "tool", ["work_brief", "work_ledger_read"], ids=["work_brief", "work_ledger_read"]
)
def test_read_class_tools_stay_auto_approved_with_the_guard_on(guard_on, tool):
    entry = _project(_spec())
    assert _effect(entry, "kirocrew-work", tool) == "allow"
    assert f"@kirocrew-work/{tool}" not in kas_agents.projected_crew_answered([entry], "lead")


# ── the guard fails open: an error in its step leaves the projection alone ────


def test_an_error_in_the_guard_step_leaves_the_projection_unchanged(guard_on, monkeypatch):
    def broken(policy, **_kwargs):
        raise TypeError("malformed policy")

    monkeypatch.setattr(_guard(), "withhold_write_auto_approval", broken)
    entry = _project(_spec())
    # No raise, and the spec's own grant stands as if the guard were off.
    assert _effect(entry, *_SEND) == "allow"
    assert "@kirocrew-dashboard/session_send" not in kas_agents.projected_crew_answered(
        [entry], "lead"
    )
