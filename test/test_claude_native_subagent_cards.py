"""Claude Code's native subagents become Activity cards on the claude backend.

The adapter announces a child only to a client that declares
``clientCapabilities.subagents``, and then sends the child's own frames on the child's
session. ``kiro_crew.acp.client_native_children`` reads that form on ``AcpClient``:
the capability (section 1), the frames of one live capture turned into the roster
and activity events the native cards read (section 2), the state table and the
bounds (section 3), and the cards those events draw (section 4).

The capture is ``test/fixtures/claude_native_subagents/agent-task-0.94.0.jsonl``,
claude-agent-acp 0.94.0 with ``subagents`` declared and one Agent call.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.client_native_children import (
    CLAUDE_CLIENT_CAPABILITIES,
    _standard_state_status,
)
from kiro_crew.acp.session_handle_native_children import NATIVE_CHILD_ROSTER_CAP
from kiro_crew.acp.types import (
    ACP_CLIENT_CAPABILITIES,
    EVENT_COMPLETE,
    EVENT_SUBAGENT_ACTIVITY,
    EVENT_SUBAGENT_LIST,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    AcpEvent,
    JsonRpcMessage,
)
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PI,
)
from kiro_crew.dashboard.chat_runner import _native_subagent_sync

CAPTURE = Path(__file__).parent / "fixtures" / "claude_native_subagents" / "agent-task-0.94.0.jsonl"
PARENT = "parent-0001"
CHILD = "child-0001"
AGENT_CALL = "toolu_0001"
HANDBACK_CALL = "toolu_0002"


def _capture() -> tuple[dict, list[dict]]:
    """The capture's header and its frames, up to the turn's end."""
    lines = [json.loads(line) for line in CAPTURE.read_text(encoding="utf-8").splitlines()]
    frames = [line for line in lines[1:] if "_turn_end" not in line]
    return lines[0]["_meta"], frames


def _update(session_id: str, update: dict) -> JsonRpcMessage:
    return JsonRpcMessage(
        method="session/update", params={"sessionId": session_id, "update": update}
    )


def _client(backend: str = ACP_BACKEND_CLAUDE) -> AcpClient:
    client = AcpClient(acp_backend=backend)
    client._session_id = PARENT
    return client


async def _turn(client: AcpClient, messages: list[JsonRpcMessage]) -> list[AcpEvent]:
    """Run one ``_dispatch_events`` turn over *messages*, then the prompt's answer."""
    frames = [("update", msg) for msg in messages]
    frames.append(("complete", JsonRpcMessage(id=1, result={"stopReason": "end_turn"})))

    async def _fake_loop(req_id, timeout):
        for frame in frames:
            yield frame

    client._prompt_loop = _fake_loop  # type: ignore[assignment]
    return [event async for event in client._dispatch_events(req_id=1, timeout=1.0)]


def _rosters(events: list[AcpEvent]) -> list[list[dict]]:
    return [event.subagents or [] for event in events if event.kind == EVENT_SUBAGENT_LIST]


def _text(events: list[AcpEvent]) -> str:
    return "".join(event.text or "" for event in events if event.kind == EVENT_TEXT_CHUNK)


# ── 1. The capability ──────────────────────────────────────────────────────────


def test_claude_declares_subagents_on_top_of_the_shared_capabilities() -> None:
    assert CLAUDE_CLIENT_CAPABILITIES == {**ACP_CLIENT_CAPABILITIES, "subagents": {}}
    assert "_meta" not in CLAUDE_CLIENT_CAPABILITIES
    params = _client()._initialize_params()
    assert params["clientCapabilities"] == CLAUDE_CLIENT_CAPABILITIES


@pytest.mark.parametrize(
    "backend",
    [
        ACP_BACKEND_KIRO,
        ACP_BACKEND_OPENCODE,
        ACP_BACKEND_GOOSE,
        ACP_BACKEND_PI,
        ACP_BACKEND_DEEPSEEK,
    ],
)
def test_every_other_client_backend_keeps_the_shared_capabilities(backend: str) -> None:
    params = _client(backend)._initialize_params()
    assert params["clientCapabilities"] is ACP_CLIENT_CAPABILITIES
    assert "subagents" not in ACP_CLIENT_CAPABILITIES


def test_the_capture_was_taken_with_the_capability_declared() -> None:
    meta, frames = _capture()
    assert meta["backend"] == ACP_BACKEND_CLAUDE and meta["recorded"] == "live"
    assert meta["client_capabilities_subagents"] is True
    assert len(frames) < 50


# ── 2. One live capture ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_capture_lists_the_child_running_then_completed() -> None:
    _, frames = _capture()
    events = await _turn(_client(), [JsonRpcMessage.from_dict(frame) for frame in frames])
    rosters = _rosters(events)
    assert rosters[0] == [
        {
            "sessionId": CHILD,
            "sessionName": "say pong",
            "agentName": "general-purpose",
            "initialQuery": "say pong",
            "status": {"type": "working", "message": ""},
        }
    ]
    assert rosters[-1][0]["status"] == {"type": "completed", "message": ""}
    assert events[-1].kind == EVENT_COMPLETE


@pytest.mark.asyncio
async def test_the_childs_tool_call_is_attributed_before_it_renders() -> None:
    """The child's call keeps its ordinary tool row, as a kiro-cli child's does; the
    activity prefix comes first so the card owns the call before the row is drawn."""
    _, frames = _capture()
    events = await _turn(_client(), [JsonRpcMessage.from_dict(frame) for frame in frames])
    activity = [event for event in events if event.kind == EVENT_SUBAGENT_ACTIVITY]
    assert [(a.sub_session_id, a.tool_call_id, a.title) for a in activity] == [
        (CHILD, HANDBACK_CALL, "SubagentHandback")
    ]
    order = [
        event.kind
        for event in events
        if event.tool_call_id == HANDBACK_CALL
        and event.kind in (EVENT_SUBAGENT_ACTIVITY, EVENT_TOOL_CALL)
    ]
    assert order == [EVENT_SUBAGENT_ACTIVITY, EVENT_TOOL_CALL]


@pytest.mark.asyncio
async def test_the_parent_answer_is_only_the_parents_text() -> None:
    _, frames = _capture()
    messages = [JsonRpcMessage.from_dict(frame) for frame in frames]
    child_text = _update(
        CHILD,
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "pong"}},
    )
    events = await _turn(_client(), messages[:9] + [child_text] + messages[9:])
    assert _text(events) == 'The subagent replied "pong".'
    child = [e for e in events if e.kind == EVENT_SUBAGENT_ACTIVITY and e.text]
    assert [(e.sub_session_id, e.text) for e in child] == [(CHILD, "pong")]


@pytest.mark.asyncio
async def test_a_backend_that_declares_nothing_reads_the_frames_as_before() -> None:
    _, frames = _capture()
    events = await _turn(
        _client(ACP_BACKEND_OPENCODE), [JsonRpcMessage.from_dict(f) for f in frames]
    )
    assert not _rosters(events)
    assert not [event for event in events if event.kind == EVENT_SUBAGENT_ACTIVITY]
    assert HANDBACK_CALL in {
        event.tool_call_id for event in events if event.kind == EVENT_TOOL_CALL
    }


@pytest.mark.asyncio
async def test_the_text_only_reader_returns_only_the_parents_text() -> None:
    _, frames = _capture()
    client = _client()
    child_text = _update(
        CHILD,
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "pong"}},
    )
    messages = [JsonRpcMessage.from_dict(frame) for frame in frames]
    loop = [("update", msg) for msg in messages[:9] + [child_text] + messages[9:]]
    loop.append(("complete", JsonRpcMessage(id=1, result={"stopReason": "end_turn"})))

    async def _fake_loop(req_id, timeout):
        for frame in loop:
            yield frame

    client._prompt_loop = _fake_loop  # type: ignore[assignment]
    assert await client._read_prompt_response(1, 1.0) == 'The subagent replied "pong".'


# ── 3. States, tasks and bounds ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("state", "status"),
    [
        ({"state": "running"}, {"type": "working", "message": ""}),
        ({"state": "requires_action"}, {"type": "working", "message": ""}),
        ({"state": "idle", "stopReason": "end_turn"}, {"type": "completed", "message": ""}),
        ({"state": "idle", "stopReason": "max_tokens"}, {"type": "completed", "message": ""}),
        ({"state": "idle"}, {"type": "completed", "message": ""}),
        ({"state": "idle", "stopReason": "cancelled"}, {"type": "failed", "message": "cancelled"}),
        (
            {"state": "idle", "stopReason": "error", "error": {"message": "overloaded"}},
            {"type": "failed", "message": "overloaded"},
        ),
        ({"state": "idle", "stopReason": "refusal"}, {"type": "failed", "message": "refusal"}),
        (None, None),
    ],
)
def test_each_state_maps_to_a_card_status(state, status) -> None:
    assert _standard_state_status(state) == status


@pytest.mark.asyncio
async def test_a_child_with_no_description_waits_for_its_prompt() -> None:
    """A card whose first report has no task is skipped for good, so the roster waits."""
    announce = _update(
        PARENT,
        {"sessionUpdate": "subagent_update", "sessionId": CHILD, "state": {"state": "running"}},
    )
    prompt = _update(
        CHILD,
        {
            "sessionUpdate": "session_message",
            "messageId": AGENT_CALL,
            "content": [{"type": "text", "text": "Count the files."}],
        },
    )
    rosters = _rosters(await _turn(_client(), [announce, prompt]))
    assert len(rosters) == 1 and rosters[0][0]["initialQuery"] == "Count the files."


@pytest.mark.asyncio
async def test_a_child_call_left_on_the_parent_session_is_still_the_childs() -> None:
    """The adapter keeps a child's call on the session where a permission request
    first made it; the Agent call it serves still names the child."""
    _, frames = _capture()
    messages = [JsonRpcMessage.from_dict(frame) for frame in frames[:7]]
    forced = _update(
        PARENT,
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "toolu_0009",
            "title": "Read file",
            "kind": "read",
            "status": "pending",
            "_meta": {"claudeCode": {"parentToolUseId": AGENT_CALL, "toolName": "Read"}},
        },
    )
    events = await _turn(_client(), messages + [forced])
    assert [
        (e.sub_session_id, e.tool_call_id) for e in events if e.kind == EVENT_SUBAGENT_ACTIVITY
    ] == [(CHILD, "toolu_0009")]


@pytest.mark.asyncio
async def test_a_child_with_no_task_holds_back_only_its_own_row() -> None:
    _, frames = _capture()
    taskless = _update(
        PARENT,
        {
            "sessionUpdate": "subagent_update",
            "sessionId": "child-0002",
            "state": {"state": "running"},
        },
    )
    messages = [JsonRpcMessage.from_dict(frame) for frame in frames]
    rosters = _rosters(await _turn(_client(), messages[:5] + [taskless] + messages[5:]))
    assert rosters and all([row["sessionId"] for row in roster] == [CHILD] for roster in rosters)
    assert rosters[-1][0]["status"]["type"] == "completed"


def test_a_session_that_declares_nothing_keeps_no_child_state() -> None:
    """The per-turn reset is a no-op off the claude backend, including on a client
    built with ``__new__`` that never ran ``__init__``."""
    _client(ACP_BACKEND_KIRO)._reset_native_child_turn()
    AcpClient.__new__(AcpClient)._reset_native_child_turn()


@pytest.mark.asyncio
async def test_a_background_child_is_listed_again_in_the_turn_that_reads_it() -> None:
    _, frames = _capture()
    client = _client()
    await _turn(client, [JsonRpcMessage.from_dict(frame) for frame in frames[:7]])
    later = _update(
        CHILD,
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "still here"}},
    )
    events = await _turn(client, [later])
    assert [row["sessionId"] for row in _rosters(events)[0]] == [CHILD]
    assert events[0].kind == EVENT_SUBAGENT_LIST and events[1].kind == EVENT_SUBAGENT_ACTIVITY


@pytest.mark.asyncio
async def test_a_turn_lists_only_the_children_seen_in_it() -> None:
    _, frames = _capture()
    client = _client()
    await _turn(client, [JsonRpcMessage.from_dict(frame) for frame in frames])
    other = _update(
        PARENT,
        {
            "sessionUpdate": "subagent_update",
            "sessionId": "child-0002",
            "title": "general-purpose",
            "description": "second",
            "state": {"state": "running"},
        },
    )
    rosters = _rosters(await _turn(client, [other]))
    assert [[row["sessionId"] for row in roster] for roster in rosters] == [["child-0002"]]


@pytest.mark.asyncio
async def test_children_past_the_cap_are_counted_and_get_no_row() -> None:
    """The same bound and overflow count as the handle's native-child set: a child
    past the cap is counted and gets no row. Its text is dropped; its tool call keeps
    the ordinary path, so the caches its approval is judged by still fill."""
    client = _client()
    client._native_child_sids.update(f"c-{i}" for i in range(NATIVE_CHILD_ROSTER_CAP))
    late = _update(
        PARENT,
        {
            "sessionUpdate": "subagent_update",
            "sessionId": "c-late",
            "description": "x",
            "state": {"state": "running"},
        },
    )
    late_text = _update(
        "c-late",
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "late"}},
    )
    late_tool = _update(
        "c-late",
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "toolu_late",
            "title": "Read file",
            "kind": "read",
            "status": "pending",
        },
    )
    events = await _turn(client, [late, late_text, late_tool])
    assert not _rosters(events)
    assert client._native_child_overflow == 1
    assert "c-late" not in client._native_child_sids and "c-late" not in client._native_child_rows
    assert _text(events) == ""
    assert not [event for event in events if event.kind == EVENT_SUBAGENT_ACTIVITY]
    assert "toolu_late" in {event.tool_call_id for event in events if event.kind == EVENT_TOOL_CALL}


@pytest.mark.asyncio
async def test_an_overlong_link_is_ignored_and_one_past_the_cap_is_counted() -> None:
    """The Agent-call link map bounds each key as well as the key count."""
    _, frames = _capture()
    client = _client()
    await _turn(client, [JsonRpcMessage.from_dict(frame) for frame in frames[:7]])
    assert client._native_child_by_tool_use == {AGENT_CALL: CHILD}

    def _prompt(message_id: str) -> JsonRpcMessage:
        return _update(
            CHILD,
            {"sessionUpdate": "session_message", "messageId": message_id, "content": []},
        )

    await _turn(client, [_prompt("t" * 10_000)])
    assert "t" * 10_000 not in client._native_child_by_tool_use
    assert client._native_child_overflow == 0
    client._native_child_by_tool_use.update(
        {f"toolu_{i}": CHILD for i in range(NATIVE_CHILD_ROSTER_CAP - 1)}
    )
    await _turn(client, [_prompt("toolu_late")])
    assert "toolu_late" not in client._native_child_by_tool_use
    assert client._native_child_overflow == 1


@pytest.mark.asyncio
async def test_the_overflow_is_said_once_at_the_next_turn_and_cleared(caplog) -> None:
    client = _client()
    client._native_child_overflow = 2
    with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.client"):
        await _turn(client, [])
        await _turn(client, [])
    said = [r.getMessage() for r in caplog.records if "native child store made" in r.getMessage()]
    assert len(said) == 1 and "made 2 refusal(s)" in said[0]
    assert client._native_child_overflow == 0


@pytest.mark.parametrize("child_sid", [PARENT, "", 7, "x" * 10_000])
@pytest.mark.asyncio
async def test_an_unusable_child_id_is_ignored(child_sid) -> None:
    bad = _update(
        PARENT,
        {
            "sessionUpdate": "subagent_update",
            "sessionId": child_sid,
            "description": "x",
            "state": {"state": "running"},
        },
    )
    client = _client()
    assert not _rosters(await _turn(client, [bad]))
    assert not client._native_child_sids and not client._native_child_rows


# ── 4. The cards ───────────────────────────────────────────────────────────────


def _make_state():
    state = MagicMock()
    state.broadcast_ws = MagicMock()
    return state


def _make_slot():
    slot = MagicMock()
    slot.key = "slot-1"
    slot._native_subagent_tracker = {}
    slot._native_subagent_output = {}
    return slot


@pytest.mark.asyncio
async def test_the_rosters_draw_one_card_that_stays_running_while_it_waits() -> None:
    """``requires_action`` puts nothing on the card's tool line: nothing would clear it
    when the child resumes."""
    _, frames = _capture()
    messages = [JsonRpcMessage.from_dict(frame) for frame in frames]
    waiting = _update(
        PARENT,
        {
            "sessionUpdate": "subagent_update",
            "sessionId": CHILD,
            "state": {"state": "requires_action"},
        },
    )
    events = await _turn(_client(), messages[:7] + [waiting] + messages[7:])
    state, slot, tracker = _make_state(), _make_slot(), {}
    for roster in _rosters(events):
        _native_subagent_sync(state, slot, roster, tracker)
    sent = [(c.args[0], c.args[1]) for c in state.broadcast_ws.call_args_list]
    assert [kind for kind, _ in sent] == ["subagent_spawn", "subagent_done"]
    assert sent[0][1]["id"] == f"native:{CHILD}" and sent[0][1]["task"] == "say pong"
    assert sent[1][1]["error"] is None
