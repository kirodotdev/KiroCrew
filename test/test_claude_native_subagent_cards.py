"""Claude Code's native subagents become Activity cards on the claude backend.

The adapter announces a child only to a client that declares
``clientCapabilities.subagents``, and then sends the child's own frames on the child's
session. ``kiro_crew.acp.client_native_children`` reads that form on ``AcpClient``:
the capability (section 1), the frames of one live capture turned into the roster
and activity events the native cards read (section 2), the state table and the
bounds (section 3), the cards those events draw (section 4), the approval gate a
running child leaves as it is (section 5), and the held text a turn cut short puts on
the card (section 6).

The capture is ``test/fixtures/claude_native_subagents/agent-task-0.94.0.jsonl``,
claude-agent-acp 0.94.0 with ``subagents`` declared and one Agent call.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from turn_harness import (
    CANCEL_TURN,
    REJECTED,
    Do,
    Raise,
    ScriptedProvider,
    TurnScript,
    run_turn,
)

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.client_native_children import (
    CLAUDE_CLIENT_CAPABILITIES,
    _label,
    _standard_state_status,
)
from kiro_crew.acp.session_handle_native_children import (
    NATIVE_CHILD_LABEL_CAP,
    NATIVE_CHILD_ROSTER_CAP,
)
from kiro_crew.acp.types import (
    ACP_CLIENT_CAPABILITIES,
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_SUBAGENT_ACTIVITY,
    EVENT_SUBAGENT_LIST,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    STOP_REASON_COMPACTION_FAILED,
    STOP_REASON_END_TURN,
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
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.dashboard.chat_runner import _native_subagent_sync
from kiro_crew.hook_runtime.hook_types import HooksConfig
from kiro_crew.hooks import HookManager

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


async def _turn(
    client: AcpClient, messages: list[JsonRpcMessage], stop_reason: str | None = "end_turn"
) -> list[AcpEvent]:
    """Run one ``_dispatch_events`` turn over *messages*, then the prompt's answer.

    A *stop_reason* of None sends no answer: the backend abandoned the turn.
    """
    frames = [("update", msg) for msg in messages]
    if stop_reason is not None:
        frames.append(("complete", JsonRpcMessage(id=1, result={"stopReason": stop_reason})))

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


# A credential URL; its password alone is the secret a broken redaction shows.
_DB_URL = "postgres://admin:s3cretPassw0rd@db.internal.example.com:5432/app"


def _child_chunk(text: str) -> JsonRpcMessage:
    return _update(
        CHILD, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}
    )


def _child_text(events: list[AcpEvent]) -> str:
    return "".join(e.text for e in events if e.kind == EVENT_SUBAGENT_ACTIVITY and e.text)


@pytest.mark.asyncio
async def test_a_credential_split_across_two_child_chunks_is_redacted() -> None:
    """Each chunk redacted alone lets the password through: the first holds no
    secret yet and the second does not read as a URL. The child's own stream
    redactor sees both."""
    _, frames = _capture()
    messages = [JsonRpcMessage.from_dict(frame) for frame in frames]
    head, tail = _DB_URL.split("s3cret")
    split = [_child_chunk(f"connect to {head}"), _child_chunk(f"s3cret{tail} now")]
    events = await _turn(_client(), messages[:9] + split + messages[9:])
    text = _child_text(events)
    assert "s3cretPassw0rd" not in text and "[REDACTED" in text
    assert text.startswith("connect to ") and text.endswith(" now")


@pytest.mark.asyncio
async def test_a_childs_held_tail_is_flushed_before_the_roster_that_closes_it() -> None:
    """The redactor holds back a chunk that may still be a credential's head; that
    tail reaches the card before the roster that marks the child done."""
    _, frames = _capture()
    messages = [JsonRpcMessage.from_dict(frame) for frame in frames]
    client = _client()
    events = await _turn(client, messages[:9] + [_child_chunk("pong")] + messages[9:])
    kinds = [
        "text" if e.kind == EVENT_SUBAGENT_ACTIVITY and e.text else e.subagents[0]["status"]["type"]
        for e in events
        if (e.kind == EVENT_SUBAGENT_ACTIVITY and e.text) or e.kind == EVENT_SUBAGENT_LIST
    ]
    assert kinds.index("text") < kinds.index("completed")
    assert client._native_child_redactors == {}


@pytest.mark.asyncio
async def test_a_turn_cut_short_gets_every_held_tail_from_the_flush() -> None:
    """No roster flushes a tail when the turn is cut short, so its release asks for
    the held text: a short reply held whole, and a credential's tail, redacted."""
    _, frames = _capture()
    messages = [JsonRpcMessage.from_dict(frame) for frame in frames]
    client = _client()
    events = await _turn(client, messages[:9] + [_child_chunk("pong")])
    assert _child_text(events) == ""
    flushed = client.flush_native_child_text()
    assert [(e.kind, e.sub_session_id, e.text) for e in flushed] == [
        (EVENT_SUBAGENT_ACTIVITY, CHILD, "pong")
    ]
    assert client._native_child_redactors == {} and client.flush_native_child_text() == []
    events = await _turn(client, [_child_chunk(f"see {_DB_URL}")])
    text = _child_text(events) + "".join(e.text for e in client.flush_native_child_text())
    assert text.startswith("see ") and "s3cretPassw0rd" not in text and "[REDACTED" in text


def _pong_turn() -> list[JsonRpcMessage]:
    """The capture up to a running child, which then sends ``pong``."""
    _, frames = _capture()
    return [JsonRpcMessage.from_dict(frame) for frame in frames[:9]] + [_child_chunk("pong")]


@pytest.mark.parametrize(
    "stop_reason,flushed", [("cancelled", True), ("end_turn", False), ("max_tokens", False)]
)
@pytest.mark.asyncio
async def test_only_a_stop_flushes_the_held_text_before_its_completion(
    stop_reason, flushed
) -> None:
    """A Stop's ``cancelled`` completion closes every card, so the held text comes
    first. Any other completion keeps it held for a background child."""
    client = _client()
    events = await _turn(client, _pong_turn(), stop_reason=stop_reason)
    kinds = ["pong" if e.kind == EVENT_SUBAGENT_ACTIVITY and e.text else e.kind for e in events]
    assert kinds[-1] == EVENT_COMPLETE
    assert ("pong" in kinds) is flushed
    assert (CHILD in client._native_child_redactors) is not flushed


@pytest.mark.asyncio
async def test_a_compaction_failed_turn_flushes_the_held_text_before_its_completion() -> None:
    """The backend abandons the turn after a failed compaction, and the completion
    the client makes up for it closes every card, so the held text comes first."""
    client = _client()
    client._compaction_failed_turn = True
    events = await _turn(client, _pong_turn(), stop_reason=None)
    assert [(e.kind, e.text, e.stop_reason) for e in events[-2:]] == [
        (EVENT_SUBAGENT_ACTIVITY, "pong", ""),
        (EVENT_COMPLETE, "", STOP_REASON_COMPACTION_FAILED),
    ]
    assert client._native_child_redactors == {}


@pytest.mark.asyncio
async def test_a_child_tool_title_is_redacted() -> None:
    _, frames = _capture()
    messages = [JsonRpcMessage.from_dict(frame) for frame in frames]
    call = _update(
        CHILD,
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "toolu_0009",
            "title": f"psql {_DB_URL}",
            "kind": "execute",
            "status": "pending",
        },
    )
    events = await _turn(_client(), messages[:9] + [call] + messages[9:])
    titles = [e.title for e in events if e.kind == EVENT_SUBAGENT_ACTIVITY and e.tool_call_id]
    assert titles and all("s3cretPassw0rd" not in title for title in titles)


def test_a_label_cut_never_ends_inside_a_token() -> None:
    """A URL the bound would cut before its ``@`` does not read as a credential to
    the card's redactor, so the token is dropped whole."""
    padding = "x " * ((NATIVE_CHILD_LABEL_CAP - 20) // 2)
    label = _label(padding + _DB_URL)
    assert len(label) <= NATIVE_CHILD_LABEL_CAP
    assert "admin" not in label and label == padding
    on_a_space = "y" * NATIVE_CHILD_LABEL_CAP + " tail"
    assert _label(on_a_space) == "y" * NATIVE_CHILD_LABEL_CAP
    assert _label("short") == "short"


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
    the ordinary path, so the caches its approval is judged by still fill. The call
    is still marked as a child's, with no title, so the session-directive refusal
    applies to it."""
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
    marks = [
        (e.sub_session_id, e.tool_call_id, e.title, e.text)
        for e in events
        if e.kind == EVENT_SUBAGENT_ACTIVITY
    ]
    assert marks == [("c-late", "toolu_late", "", "")]
    assert "toolu_late" in {event.tool_call_id for event in events if event.kind == EVENT_TOOL_CALL}
    assert not client._native_child_redactors


def _read_call(session_id: str, meta: dict | None) -> JsonRpcMessage:
    update = {
        "sessionUpdate": "tool_call",
        "toolCallId": "toolu_0009",
        "title": "Read file",
        "kind": "read",
        "status": "pending",
    }
    if meta is not None:
        update["_meta"] = meta
    return _update(session_id, update)


@pytest.mark.asyncio
async def test_a_call_stamped_with_an_unlinked_agent_call_is_marked_as_a_childs() -> None:
    """A child's call left on the parent session names the Agent call it serves. With
    no link for that call (refused past the cap, say), the call is still not the
    parent's: the Agent call stands in for the child's id, and nothing is stored."""
    client = _client()
    stamped = _read_call(PARENT, {"claudeCode": {"parentToolUseId": "toolu_unlinked"}})
    events = await _turn(client, [stamped])
    marks = [
        (e.sub_session_id, e.tool_call_id) for e in events if e.kind == EVENT_SUBAGENT_ACTIVITY
    ]
    assert marks == [("toolu_unlinked", "toolu_0009")]
    assert not client._native_child_rows and not client._native_child_by_tool_use
    assert "toolu_0009" in {event.tool_call_id for event in events if event.kind == EVENT_TOOL_CALL}


@pytest.mark.parametrize(
    "meta",
    [None, {"claudeCode": {"toolName": "Read"}}, {"claudeCode": {"parentToolUseId": None}}],
    ids=["no-meta", "no-stamp", "null-stamp"],
)
@pytest.mark.asyncio
async def test_the_parents_own_call_is_never_marked_as_a_childs(meta) -> None:
    """The adapter stamps ``parentToolUseId`` as a string only on a subagent's frames,
    so a parent's directive is never refused as a child's."""
    events = await _turn(_client(), [_read_call(PARENT, meta)])
    assert not [event for event in events if event.kind == EVENT_SUBAGENT_ACTIVITY]
    assert "toolu_0009" in {event.tool_call_id for event in events if event.kind == EVENT_TOOL_CALL}


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


# ── 5. The approval gate ───────────────────────────────────────────────────────


class _ClaudeProvider(ScriptedProvider):
    """A scripted provider whose capabilities are claude's."""

    capabilities = capabilities_for(ACP_BACKEND_CLAUDE)


class _KiroProvider(ScriptedProvider):
    """A scripted provider whose capabilities are kiro-cli's."""

    capabilities = capabilities_for(ACP_BACKEND_KIRO)


def _hook_on(ctx) -> None:
    """Turn ``hooks.auto_approve_subagent_tools`` on for the request that follows.

    Set inside the stream, after the prompt is built, so the context builder this
    stands in for is read only by the approval gate.
    """
    ctx.state.context_builder = SimpleNamespace(
        hooks=HookManager(HooksConfig(auto_approve_subagent_tools=True))
    )


def _hook_off(ctx) -> None:
    ctx.state.context_builder = None


def _approval_turn(provider: type[ScriptedProvider]) -> TurnScript:
    """A child is running, and then the parent asks to run a shell command."""
    running = AcpEvent(
        kind=EVENT_SUBAGENT_LIST,
        subagents=[
            {
                "sessionId": CHILD,
                "role": "general-purpose",
                "initialQuery": "count the files",
                "status": {"type": "working"},
            }
        ],
    )
    parent_request = AcpEvent(
        kind=EVENT_PERMISSION_REQUEST,
        request_id="r1",
        title="touch out.txt",
        tool_name="shell",
        tool_kind="execute",
        is_shell=True,
        tool_input=json.dumps({"command": "touch out.txt"}),
    )
    done = AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)
    return TurnScript(
        events=[Do(_hook_on), running, parent_request, Do(_hook_off), done],
        answers={"r1": REJECTED},
        provider=provider,
    )


@pytest.mark.asyncio
async def test_a_running_claude_child_leaves_the_parents_request_on_a_card() -> None:
    """With the hook on and a claude child running, the parent's request still asks.

    A claude request does not name the session that sent it, so the crew rule
    cannot tell the parent's request from a child's. The claude backend keeps the
    gate it has with no child running: every request gets a card.
    """
    record = await run_turn(_approval_turn(_ClaudeProvider))
    assert len(record.approval_cards) == 1
    assert record.calls("approve_tool") == []
    assert record.audits(request_id="r1", outcome="auto_approved") == []


@pytest.mark.asyncio
async def test_a_running_kiro_cli_child_still_auto_approves_under_the_hook() -> None:
    """The crew rule itself is unchanged: on kiro-cli the same turn approves at once."""
    record = await run_turn(_approval_turn(_KiroProvider))
    assert record.approval_cards == []
    assert [call.args for call in record.calls("approve_tool")] == [("r1",)]
    assert record.audits(request_id="r1", outcome="auto_approved")[0]["metadata"] == {
        "reason": "native_crew"
    }


# ── 6. A turn cut short ────────────────────────────────────────────────────────


async def _client_holding_pong() -> AcpClient:
    """A claude client whose running child sent ``pong``, which its redactor holds."""
    _, frames = _capture()
    client = _client()
    await _turn(client, [JsonRpcMessage.from_dict(f) for f in frames[:9]] + [_child_chunk("pong")])
    assert CHILD in client._native_child_redactors
    return client


def _over(client: AcpClient) -> type[ScriptedProvider]:
    """Claude's capabilities, with *client* as the provider's ``AcpClient``."""

    class _Provider(_ClaudeProvider):
        @property
        def client(self) -> AcpClient:
            return client

    return _Provider


_RUNNING = AcpEvent(
    kind=EVENT_SUBAGENT_LIST,
    subagents=[
        {
            "sessionId": CHILD,
            "role": "general-purpose",
            "initialQuery": "say pong",
            "status": {"type": "working"},
        }
    ],
)
_REQUEST = AcpEvent(
    kind=EVENT_PERMISSION_REQUEST,
    request_id="r1",
    title="touch out.txt",
    tool_name="shell",
    tool_kind="execute",
    is_shell=True,
    tool_input=json.dumps({"command": "touch out.txt"}),
)


def _done_results(record) -> list[str]:
    return [frame.payload["result"] for frame in record.frames("subagent_done")]


@pytest.mark.asyncio
async def test_a_cancelled_turn_puts_the_held_text_on_the_card() -> None:
    """The child said ``pong`` and the turn was cancelled before the roster that
    flushes it: the card the release closes still holds the reply."""
    client = await _client_holding_pong()
    record = await run_turn(
        TurnScript(events=[_RUNNING, _REQUEST], answers={"r1": CANCEL_TURN}, provider=_over(client))
    )
    assert _done_results(record) == ["pong"]
    assert client._native_child_redactors == {}


@pytest.mark.asyncio
async def test_a_turn_ended_by_a_provider_error_puts_the_held_text_on_the_card() -> None:
    client = await _client_holding_pong()
    record = await run_turn(
        TurnScript(events=[_RUNNING, Raise(RuntimeError("stream lost"))], provider=_over(client))
    )
    assert _done_results(record) == ["pong"]


@pytest.mark.asyncio
async def test_a_turn_that_ends_normally_keeps_the_text_held() -> None:
    """A background child keeps running into the next turn, where its redactor still
    needs the held text, so a normal end flushes nothing."""
    client = await _client_holding_pong()
    done = AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)
    record = await run_turn(TurnScript(events=[_RUNNING, done], provider=_over(client)))
    assert "pong" not in "".join(_done_results(record))
    assert CHILD in client._native_child_redactors


@pytest.mark.asyncio
async def test_a_stop_pressed_while_a_child_replies_keeps_the_reply_on_its_card() -> None:
    """End to end: the claude client's own events for a Stop, read by a dashboard
    turn. ``_on_complete`` closes the card, and the reply is already on it."""
    client = _client()
    events = await _turn(client, _pong_turn(), stop_reason="cancelled")
    record = await run_turn(TurnScript(events=events, provider=_over(client)))
    [result] = _done_results(record)
    assert result.endswith("\npong")
