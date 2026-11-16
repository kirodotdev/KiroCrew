"""A Claude native child's thinking reaches its own Subagents card, and nothing else.

claude-agent-acp 0.107.0 sends an Agent-tool child's thinking as
``agent_thought_chunk`` on the child's session, stamped with the Agent call it serves.
``acp.native_child_thinking`` reads it into ``EVENT_SUBAGENT_THINKING`` naming the
child (section 1, the client); the dashboard's native-card sink redacts it per card and
sends it as ``subagent_thinking`` to subagent subscribers (section 2, the cards). It is
never the parent's thinking, the card's output, a slot row or the transcript.

No live capture carries a child's thought yet (the CI pin 0.94.0 ignores the thinking
display), so the frames here are built from the 0.107.0 shapes.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from turn_harness import ScriptedProvider, TurnScript, run_turn

from kiro_crew.acp import native_child_thinking
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.client_native_children import _child_text
from kiro_crew.acp.native_child_thinking import EVENT_SUBAGENT_THINKING
from kiro_crew.acp.session_handle_native_children import NATIVE_CHILD_ROSTER_CAP
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_SUBAGENT_ACTIVITY,
    EVENT_SUBAGENT_LIST,
    EVENT_TEXT_CHUNK,
    EVENT_THINKING_CHUNK,
    STOP_REASON_END_TURN,
    UPDATE_AGENT_MESSAGE_CHUNK,
    UPDATE_AGENT_THOUGHT_CHUNK,
    AcpEvent,
    JsonRpcMessage,
)
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKENDS_NATIVE_CHILD_THINKING,
)
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.dashboard import chat_runner
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

PARENT = "parent-0001"
CHILD = "child-0001"
AGENT_CALL = "toolu_0001"
CARD = f"native:{CHILD}"

# A credential URL; its password alone is the secret a broken redaction shows.
_DB_URL = "postgres://admin:s3cretPassw0rd@db.internal.example.com:5432/app"


def _update(session_id: str, update: dict) -> JsonRpcMessage:
    return JsonRpcMessage(
        method="session/update", params={"sessionId": session_id, "update": update}
    )


def _client() -> AcpClient:
    client = AcpClient(acp_backend=ACP_BACKEND_CLAUDE)
    client._session_id = PARENT
    return client


async def _turn(client: AcpClient, messages: list[JsonRpcMessage]) -> list[AcpEvent]:
    """One ``_dispatch_events`` turn over *messages*, then the prompt's answer."""
    frames = [("update", msg) for msg in messages]
    frames.append(("complete", JsonRpcMessage(id=1, result={"stopReason": "end_turn"})))

    async def _fake_loop(req_id, timeout):
        for frame in frames:
            yield frame

    client._prompt_loop = _fake_loop  # type: ignore[assignment]
    events = client._dispatch_events(req_id=1, timeout=FALLBACK_TIMEOUT_SECS)
    return [event async for event in events]


def _announce(child: str = CHILD, call: str = AGENT_CALL) -> list[JsonRpcMessage]:
    """The adapter's announce of a running child and the prompt it was handed."""
    return [
        _update(
            PARENT,
            {
                "sessionUpdate": "subagent_update",
                "sessionId": child,
                "title": "general-purpose",
                "description": "solve the puzzle",
                "state": {"state": "running"},
            },
        ),
        _update(
            child,
            {
                "sessionUpdate": "session_message",
                "messageId": call,
                "content": [{"type": "text", "text": "solve the puzzle"}],
            },
        ),
    ]


def _thought(session_id: str, text: str, kind: str = UPDATE_AGENT_THOUGHT_CHUNK) -> JsonRpcMessage:
    """A thought chunk as 0.107.0 sends a subagent's: stamped with its Agent call."""
    content_type = "thinking" if kind == UPDATE_AGENT_MESSAGE_CHUNK else "text"
    return _update(
        session_id,
        {
            "sessionUpdate": kind,
            "content": {"type": content_type, "text": text},
            "messageId": "msg_0001",
            "_meta": {"claudeCode": {"parentToolUseId": AGENT_CALL}},
        },
    )


def _parent_text(text: str) -> JsonRpcMessage:
    return _update(
        PARENT, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}
    )


def _thinking(events: list[AcpEvent]) -> list[tuple[str, str]]:
    return [(e.sub_session_id, e.text) for e in events if e.kind == EVENT_SUBAGENT_THINKING]


def _kinds(events: list[AcpEvent], kind: str) -> list[AcpEvent]:
    return [e for e in events if e.kind == kind]


# ── 1. The client ──────────────────────────────────────────────────────────────


def test_only_claude_reads_a_childs_thinking() -> None:
    assert ACP_BACKENDS_NATIVE_CHILD_THINKING == frozenset({ACP_BACKEND_CLAUDE})


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [UPDATE_AGENT_THOUGHT_CHUNK, UPDATE_AGENT_MESSAGE_CHUNK])
async def test_a_childs_thought_becomes_its_thinking_event_and_never_the_parents(kind) -> None:
    """A thought chunk, or a message chunk whose content is a reasoning type."""
    messages = _announce() + [
        _thought(CHILD, "Dave is in seat 1. ", kind),
        _thought(CHILD, "Eve sits in seat 2.", kind),
        _parent_text("Alice"),
    ]
    events = await _turn(_client(), messages)
    assert _thinking(events) == [(CHILD, "Dave is in seat 1. "), (CHILD, "Eve sits in seat 2.")]
    assert not _kinds(events, EVENT_THINKING_CHUNK)
    assert "".join(e.text or "" for e in _kinds(events, EVENT_TEXT_CHUNK)) == "Alice"
    assert not [e for e in _kinds(events, EVENT_SUBAGENT_ACTIVITY) if e.text]


@pytest.mark.asyncio
async def test_a_thought_stamped_with_the_agent_call_on_the_parent_session_is_the_childs() -> None:
    events = await _turn(_client(), _announce() + [_thought(PARENT, "on the parent session")])
    assert _thinking(events) == [(CHILD, "on the parent session")]
    assert not _kinds(events, EVENT_THINKING_CHUNK)


@pytest.mark.asyncio
async def test_the_parents_own_thought_is_still_the_parents_thinking() -> None:
    own = _update(
        PARENT,
        {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "mine"}},
    )
    events = await _turn(_client(), _announce() + [own])
    assert [e.text for e in _kinds(events, EVENT_THINKING_CHUNK)] == ["mine"]
    assert not _thinking(events)


@pytest.mark.asyncio
async def test_off_the_set_a_childs_thought_is_dropped_as_before(monkeypatch) -> None:
    monkeypatch.setattr(native_child_thinking, "ACP_BACKENDS_NATIVE_CHILD_THINKING", frozenset())
    events = await _turn(_client(), _announce() + [_thought(CHILD, "hidden")])
    assert not _thinking(events)
    assert not _kinds(events, EVENT_THINKING_CHUNK)
    assert not [e for e in _kinds(events, EVENT_SUBAGENT_ACTIVITY) if e.text]


@pytest.mark.asyncio
async def test_a_thought_of_a_child_past_the_cap_is_dropped() -> None:
    client = _client()
    client._native_child_sids.update(f"c-{i}" for i in range(NATIVE_CHILD_ROSTER_CAP))
    late = _announce(child="c-late", call="toolu_late")
    events = await _turn(client, late + [_thought("c-late", "late thought")])
    assert client._native_child_overflow == 1
    assert not _thinking(events)
    assert not _kinds(events, EVENT_THINKING_CHUNK)


@pytest.mark.asyncio
async def test_an_empty_thought_makes_no_event() -> None:
    events = await _turn(_client(), _announce() + [_thought(CHILD, "")])
    assert not _thinking(events)
    assert not _kinds(events, EVENT_THINKING_CHUNK)


@pytest.mark.asyncio
async def test_the_text_only_reader_drops_a_childs_thought() -> None:
    client = _client()
    loop = [("update", m) for m in _announce() + [_thought(CHILD, "Dave"), _parent_text("Alice")]]
    loop.append(("complete", JsonRpcMessage(id=1, result={"stopReason": "end_turn"})))

    async def _fake_loop(req_id, timeout):
        for frame in loop:
            yield frame

    client._prompt_loop = _fake_loop  # type: ignore[assignment]
    assert await client._read_prompt_response(1, FALLBACK_TIMEOUT_SECS) == "Alice"


@pytest.mark.parametrize("kind", [UPDATE_AGENT_THOUGHT_CHUNK, UPDATE_AGENT_MESSAGE_CHUNK])
def test_a_childs_card_text_never_carries_its_thinking(kind) -> None:
    update = _thought(CHILD, "reasoning", kind).params["update"]
    assert _child_text(ACP_BACKEND_CLAUDE, kind, update) == ""


# ── 2. The cards ───────────────────────────────────────────────────────────────


class _ClaudeProvider(ScriptedProvider):
    """A scripted provider whose capabilities are claude's."""

    capabilities = capabilities_for(ACP_BACKEND_CLAUDE)


def _roster(status: str, *children: str) -> AcpEvent:
    return AcpEvent(
        kind=EVENT_SUBAGENT_LIST,
        subagents=[
            {
                "sessionId": child,
                "role": "general-purpose",
                "initialQuery": "solve the puzzle",
                "status": {"type": status},
            }
            for child in children or (CHILD,)
        ],
    )


def _child_thought(text: str, child: str = CHILD) -> AcpEvent:
    return AcpEvent(kind=EVENT_SUBAGENT_THINKING, sub_session_id=child, text=text)


_DONE = AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)


async def _run(events: list) -> tuple[object, list[tuple[str, dict]]]:
    """Run a dashboard turn over *events*; return its record and the subscriber frames."""
    sent: list[tuple[str, dict]] = []

    def _subscribers(ctx) -> None:
        ctx.state.broadcast_ws_subagent_subscribers = lambda kind, data: sent.append((kind, data))

    record = await run_turn(TurnScript(events=events, setup=_subscribers, provider=_ClaudeProvider))
    return record, sent


def _card_thinking(sent: list[tuple[str, dict]]) -> str:
    return "".join(data["text"] for kind, data in sent if kind == "subagent_thinking")


@pytest.mark.asyncio
async def test_a_childs_thinking_reaches_only_its_card_as_subagent_thinking() -> None:
    record, sent = await _run(
        [
            _roster("working"),
            _child_thought("Dave is in seat 1. "),
            _child_thought("Eve sits in seat 2. "),
            _roster("completed"),
            _DONE,
        ]
    )
    frames = [data for kind, data in sent if kind == "subagent_thinking"]
    assert frames and {(f["id"], f["slot"]) for f in frames} == {(CARD, "chat-1")}
    assert all(set(data) == {"id", "slot", "text"} for data in frames)
    thinking = _card_thinking(sent)
    assert thinking == "Dave is in seat 1. Eve sits in seat 2. "
    assert not record.frames("chat_thinking")
    assert not [f for f in record.frames("subagent_chunk") if f.payload["id"] == CARD]
    [done] = record.frames("subagent_done")
    assert "seat" not in done.payload["result"]
    for row in (*record.history_rows, *record.window):
        assert "seat" not in str(row.get("content", ""))


@pytest.mark.asyncio
async def test_a_credential_split_across_two_thought_chunks_is_redacted() -> None:
    """Each chunk redacted alone lets the password through; the card's stream
    redactor sees both."""
    head, tail = _DB_URL.split("s3cret")
    _, sent = await _run(
        [
            _roster("working"),
            _child_thought(f"connect to {head}"),
            _child_thought(f"s3cret{tail} now"),
            _roster("completed"),
            _DONE,
        ]
    )
    thinking = _card_thinking(sent)
    assert "s3cretPassw0rd" not in thinking and "[REDACTED" in thinking
    assert thinking.startswith("connect to ") and thinking.endswith(" now")


@pytest.mark.asyncio
async def test_the_held_thinking_tail_is_sent_when_the_roster_closes_the_card() -> None:
    """The redactor holds back a run that may still be a credential's head; the roster
    that closes the card sends it."""
    _, held = await _run([_roster("working"), _child_thought("pong")])
    assert _card_thinking(held) == ""
    _, sent = await _run([_roster("working"), _child_thought("pong"), _roster("completed"), _DONE])
    assert _card_thinking(sent) == "pong"


@pytest.mark.asyncio
async def test_no_thinking_is_sent_for_a_child_with_no_card_or_a_closed_card() -> None:
    _, sent = await _run(
        [
            _child_thought("before any card"),
            _roster("working"),
            _child_thought("other child", child="child-0002"),
            _roster("completed"),
            _child_thought("after the card closed"),
            _DONE,
        ]
    )
    assert _card_thinking(sent) == ""


@pytest.mark.asyncio
async def test_thinking_redactors_are_bounded_and_the_refusal_is_counted_and_logged_once(
    monkeypatch, caplog
) -> None:
    monkeypatch.setattr(chat_runner, "_NATIVE_THINKING_CARD_CAP", 1)
    state = MagicMock()
    turn = SimpleNamespace(
        _native_tracker={CHILD: {"done": False}, "child-0002": {"done": False}},
        _native_thought_redactors={},
        _native_thought_overflow=0,
        state=state,
        slot=SimpleNamespace(key="chat-1"),
    )
    with caplog.at_level(logging.WARNING, logger=chat_runner.logger.name):
        for event in (
            _child_thought("first. "),
            _child_thought("second. ", child="child-0002"),
            _child_thought("third. ", child="child-0002"),
        ):
            await chat_runner._on_subagent_thinking(turn, event)
    assert list(turn._native_thought_redactors) == [CARD]
    assert turn._native_thought_overflow == 2
    warnings = [r for r in caplog.records if "native card thinking refused" in r.getMessage()]
    assert len(warnings) == 1
    ids = {c.args[1]["id"] for c in state.broadcast_ws_subagent_subscribers.call_args_list}
    assert ids <= {CARD}
