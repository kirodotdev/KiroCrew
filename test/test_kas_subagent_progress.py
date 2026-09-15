"""KAS native sub-agent progress (Group B).

KAS delivers sub-agent lifecycle as ``session/update`` discriminants carrying
``_meta.kiro.agentSubtaskId`` (individual) or ``_meta.kiro.pipeline`` (pipeline).
These tests pin that the KAS-gated interception in
``AcpSessionHandle._handle_update`` routes them to EVENT_SUBAGENT_LIST /
EVENT_SUBAGENT_ACTIVITY, and that the kiro path is untouched.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    EVENT_SUBAGENT_ACTIVITY,
    EVENT_SUBAGENT_LIST,
    EVENT_TEXT_CHUNK,
    JsonRpcMessage,
)


def _handle(backend: str) -> AcpSessionHandle:
    """A handle over a fake runtime pinned to the given backend."""
    rt = MagicMock()
    rt.acp_backend = backend
    rt.pid = None
    rt.is_alive = MagicMock(return_value=True)
    rt.send_notification = AsyncMock()
    return AcpSessionHandle("sB", asyncio.Queue(), rt)


def _update(handle: AcpSessionHandle, update: dict) -> list:
    return handle._handle_update(
        JsonRpcMessage(method="session/update", params={"update": update})
    )


# ── Individual agent-subtask spawn → EVENT_SUBAGENT_LIST ─────────────────────


def test_individual_agent_subtask_emits_subagent_list() -> None:
    handle = _handle(ACP_BACKEND_KAS)
    events = _update(handle, {
        "sessionUpdate": "tool_call",
        "toolCallId": "tc1",
        "title": "Sub-agent: researcher",
        "status": "in_progress",
        "_meta": {"kiro": {"kind": "agent-subtask", "agentSubtaskId": "sa-1"}},
    })
    assert len(events) == 1
    assert events[0].kind == EVENT_SUBAGENT_LIST
    assert events[0].subagents is not None
    assert len(events[0].subagents) == 1
    entry = events[0].subagents[0]
    assert entry["sessionId"] == "sa-1"
    assert entry["sessionName"] == "Sub-agent: researcher"
    assert entry["agentName"] == "researcher"
    assert entry["initialQuery"] == "Sub-agent: researcher"
    assert entry["status"]["type"] == "in_progress"


# ── Completed frame → entry completed ────────────────────────────────────────


def test_completed_agent_subtask_updates_roster() -> None:
    handle = _handle(ACP_BACKEND_KAS)
    # Spawn
    _update(handle, {
        "sessionUpdate": "tool_call",
        "toolCallId": "tc1",
        "title": "Sub-agent: coder",
        "status": "in_progress",
        "_meta": {"kiro": {"kind": "agent-subtask", "agentSubtaskId": "sa-2"}},
    })
    # Complete
    events = _update(handle, {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "tc1",
        "title": "Sub-agent: coder",
        "status": "completed",
        "_meta": {"kiro": {"kind": "agent-subtask", "agentSubtaskId": "sa-2"}},
    })
    assert len(events) == 1
    assert events[0].kind == EVENT_SUBAGENT_LIST
    entry = events[0].subagents[0]
    assert entry["sessionId"] == "sa-2"
    assert entry["status"]["type"] == "completed"


# ── Pipeline frame → one entry per stage ─────────────────────────────────────


def test_pipeline_frame_creates_entries_per_stage() -> None:
    handle = _handle(ACP_BACKEND_KAS)
    events = _update(handle, {
        "sessionUpdate": "tool_call",
        "toolCallId": "tc-pipe",
        "title": "Pipeline",
        "_meta": {"kiro": {"pipeline": {
            "groupId": "g1",
            "stages": [
                {"name": "research", "role": "researcher", "status": "in_progress",
                 "dependsOn": [], "agentSubtaskId": "ps-1"},
                {"name": "code", "role": "coder", "status": "pending",
                 "dependsOn": ["ps-1"], "agentSubtaskId": "ps-2"},
            ],
        }}},
    })
    assert len(events) == 1
    assert events[0].kind == EVENT_SUBAGENT_LIST
    assert len(events[0].subagents) == 2
    ids = {e["sessionId"] for e in events[0].subagents}
    assert ids == {"ps-1", "ps-2"}
    statuses = {e["sessionId"]: e["status"]["type"] for e in events[0].subagents}
    assert statuses["ps-1"] == "in_progress"
    assert statuses["ps-2"] == "pending"


# ── Child nested tool_call → activity prefix + cache populated + tool event ──


def test_child_nested_tool_call_emits_activity() -> None:
    """A child nested tool_call emits ONLY activity (not a top-level tool event)
    yet still populates the security caches via a side-effect parser call."""
    from kiro_crew.acp.types import EVENT_TOOL_CALL

    handle = _handle(ACP_BACKEND_KAS)
    events = _update(handle, {
        "sessionUpdate": "tool_call",
        "toolCallId": "child-tc-1",
        "title": "read_file src/main.py",
        "status": "in_progress",
        "_meta": {"kiro": {"agentSubtaskId": "sa-1"}},
    })
    # Activity only — the tool must NOT surface as a top-level tool call.
    activity_events = [e for e in events if e.kind == EVENT_SUBAGENT_ACTIVITY]
    tool_events = [e for e in events if e.kind == EVENT_TOOL_CALL]
    assert len(activity_events) == 1
    assert activity_events[0].sub_session_id == "sa-1"
    assert activity_events[0].tool_call_id == "child-tc-1"
    assert activity_events[0].title == "read_file src/main.py"
    assert tool_events == []
    # The shell cache is deliberately NOT populated: this update carried no
    # `kind`, so the classification is UNRESOLVED. Caching the miss-default
    # False here would let the later permission frame read it as a RESOLVED
    # non-shell (shell_classified=True) and skip the low-fidelity downgrade
    # without any classification having happened.
    # (Cache keys are origin-scoped: "<frame sessionId>|<toolCallId>".)
    assert "sB|child-tc-1" not in handle._tool_call_is_shell
    assert "child-tc-1" not in handle._tool_call_is_shell
    # With a usable `kind`, the side-effect parse DOES populate the cache.
    _update(handle, {
        "sessionUpdate": "tool_call",
        "toolCallId": "child-tc-2",
        "title": "run tests",
        "kind": "execute",
        "status": "in_progress",
        "_meta": {"kiro": {"agentSubtaskId": "sa-1"}},
    })
    assert handle._tool_call_is_shell.get("sB|child-tc-2") is True


# ── Child agent_message_chunk → EVENT_SUBAGENT_ACTIVITY w/ redacted text ─────


def test_child_agent_message_chunk_emits_activity() -> None:
    handle = _handle(ACP_BACKEND_KAS)
    events = _update(handle, {
        "sessionUpdate": "agent_message_chunk",
        "content": {"type": "text", "text": "Working on the fix..."},
        "_meta": {"kiro": {"agentSubtaskId": "sa-1"}},
    })
    assert len(events) == 1
    assert events[0].kind == EVENT_SUBAGENT_ACTIVITY
    assert events[0].sub_session_id == "sa-1"
    assert events[0].text == "Working on the fix..."


# ── Kiro-backend parity: KAS-shaped frame NOT intercepted on kiro ────────────


def test_kas_subagent_frame_not_intercepted_on_kiro_backend() -> None:
    handle = _handle(ACP_BACKEND_KIRO)
    # On kiro, a tool_call with _meta.kiro.agentSubtaskId falls through to the
    # shared parser (no EVENT_SUBAGENT_LIST, just a normal tool_call event).
    events = _update(handle, {
        "sessionUpdate": "tool_call",
        "toolCallId": "tc1",
        "title": "Sub-agent: researcher",
        "status": "in_progress",
        "_meta": {"kiro": {"kind": "agent-subtask", "agentSubtaskId": "sa-1"}},
    })
    # Should NOT produce EVENT_SUBAGENT_LIST — kiro path processes it as a
    # normal tool call.
    subagent_events = [e for e in events if e.kind == EVENT_SUBAGENT_LIST]
    assert subagent_events == []


def test_kas_subagent_chunk_not_intercepted_on_kiro_backend() -> None:
    handle = _handle(ACP_BACKEND_KIRO)
    events = _update(handle, {
        "sessionUpdate": "agent_message_chunk",
        "content": {"type": "text", "text": "hello"},
        "_meta": {"kiro": {"agentSubtaskId": "sa-1"}},
    })
    # On kiro, this is just a regular text chunk, NOT subagent activity.
    assert len(events) == 1
    assert events[0].kind == EVENT_TEXT_CHUNK
    assert events[0].text == "hello"


# ── Failed status in roster ──────────────────────────────────────────────────


def test_failed_agent_subtask_updates_roster() -> None:
    handle = _handle(ACP_BACKEND_KAS)
    _update(handle, {
        "sessionUpdate": "tool_call",
        "toolCallId": "tc1",
        "title": "Sub-agent: builder",
        "status": "in_progress",
        "_meta": {"kiro": {"kind": "agent-subtask", "agentSubtaskId": "sa-3"}},
    })
    events = _update(handle, {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "tc1",
        "title": "Sub-agent: builder",
        "status": "failed",
        "_meta": {"kiro": {"kind": "agent-subtask", "agentSubtaskId": "sa-3"}},
    })
    assert events[0].subagents[0]["status"]["type"] == "failed"


# ── No agentSubtaskId → normal tool_call pass-through ────────────────────────


def test_normal_tool_call_not_intercepted_on_kas() -> None:
    handle = _handle(ACP_BACKEND_KAS)
    events = _update(handle, {
        "sessionUpdate": "tool_call",
        "toolCallId": "tc-normal",
        "title": "read_file",
        "status": "in_progress",
        "_meta": {"kiro": {}},
    })
    # Falls through to parse_session_update → normal EVENT_TOOL_CALL
    from kiro_crew.acp.types import EVENT_TOOL_CALL
    assert any(e.kind == EVENT_TOOL_CALL for e in events)


# ── Regression: roster reset per turn (BLOCKER) ──────────────────────────────


def _status_of(events: list, sid: str) -> str | None:
    for ev in events:
        for entry in (ev.subagents or []):
            if entry.get("sessionId") == sid:
                return entry["status"]["type"]
    return None


def test_roster_cleared_between_turns_excludes_prior_completed() -> None:
    handle = _handle(ACP_BACKEND_KAS)
    # Turn 1: sub-agent A spawns then completes.
    _update(handle, {"sessionUpdate": "tool_call", "toolCallId": "tcA", "title": "Sub-agent: A",
                     "status": "in_progress", "_meta": {"kiro": {"kind": "agent-subtask", "agentSubtaskId": "A"}}})
    _update(handle, {"sessionUpdate": "tool_call_update", "toolCallId": "tcA", "title": "Sub-agent: A",
                     "status": "completed", "_meta": {"kiro": {"kind": "agent-subtask", "agentSubtaskId": "A"}}})
    assert "A" in handle._kas_subagent_roster
    # Turn boundary: prompt()'s per-turn reset block clears the roster (parity
    # with kiro-cli's authoritative full list each turn).
    handle._kas_subagent_roster.clear()
    # Turn 2: a new sub-agent B — its LIST must NOT resurrect the completed A.
    events = _update(handle, {"sessionUpdate": "tool_call", "toolCallId": "tcB", "title": "Sub-agent: B",
                              "status": "in_progress", "_meta": {"kiro": {"kind": "agent-subtask", "agentSubtaskId": "B"}}})
    ids = {e.get("sessionId") for ev in events for e in (ev.subagents or [])}
    assert ids == {"B"}


# ── Regression: child thinking chunk not surfaced (BLOCKER) ──────────────────


def test_child_thinking_chunk_not_surfaced() -> None:
    handle = _handle(ACP_BACKEND_KAS)
    events = _update(handle, {"sessionUpdate": "agent_message_chunk",
                              "_meta": {"kiro": {"agentSubtaskId": "c1"}},
                              "content": {"type": "thinking", "text": "private reasoning"}})
    assert events == []


def test_child_text_chunk_is_surfaced() -> None:
    handle = _handle(ACP_BACKEND_KAS)
    events = _update(handle, {"sessionUpdate": "agent_message_chunk",
                              "_meta": {"kiro": {"agentSubtaskId": "c1"}},
                              "content": {"type": "text", "text": "hello from child"}})
    assert len(events) == 1
    assert events[0].kind == EVENT_SUBAGENT_ACTIVITY
    assert events[0].sub_session_id == "c1"
    assert events[0].text == "hello from child"


# ── Pipeline stage status transition reflected in the list ───────────────────


def test_pipeline_stage_status_transition() -> None:
    handle = _handle(ACP_BACKEND_KAS)
    stage_running = {"name": "s1", "role": "r1", "status": "in_progress", "agentSubtaskId": "st1"}
    stage_done = {"name": "s1", "role": "r1", "status": "completed", "agentSubtaskId": "st1"}
    _update(handle, {
        "sessionUpdate": "tool_call", "toolCallId": "tcP", "title": "Orchestrate Sub-agent",
        "_meta": {"kiro": {"pipeline": {"groupId": "g1", "stages": [stage_running]}}},
    })
    events = _update(handle, {
        "sessionUpdate": "tool_call_update", "toolCallId": "tcP",
        "_meta": {"kiro": {"pipeline": {"groupId": "g1", "stages": [stage_done]}}},
    })
    assert _status_of(events, "st1") == "completed"
