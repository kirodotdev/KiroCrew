"""A crewmate heartbeat's quiet end leaves no chat message (RFC "Crewmate heartbeat").

Driven through the REAL ``_run_chat`` with the REAL directive applier:

* a heartbeat turn that writes a sentence and then calls ``nothing_to_do``
  delivers no ``chat_message``, no roster row and no card event, keeps no
  visible assistant row, and keeps the text on the quiet-end tool row;
* the same stream on a turn that is not a heartbeat is unchanged: the sentence
  is an ordinary assistant row, delivered;
* a heartbeat turn that ends with a reply instead shows its text as before.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from test_nothing_to_do_directive import _core_call, _result, _stub_state

from kiro_crew import session_directive
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    AcpEvent,
)
from kiro_crew.dashboard import heartbeat_quiet_end
from kiro_crew.dashboard.system_notices import is_speech_row

pytestmark = pytest.mark.xdist_group("nothing_to_do_directive")

_SENTENCE = "Checked the docs queue; nothing to move this hour."


async def _drive(
    state, slot, events, monkeypatch, *, heartbeat: bool, hidden: bool = False
) -> None:
    """Stream *events* as one hidden gateway turn (no person, no self-wake)."""
    from kiro_crew.dashboard import chat_runner

    async def _stream(_msg):
        for ev in events:
            yield ev

    client = MagicMock()
    client.stream = _stream
    client.stream_command = _stream
    client.context_usage_pct = MagicMock(return_value=1.0)
    client.client = None
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
    monkeypatch.setattr(chat_runner, "_start_next_queued_turn", AsyncMock(return_value=False))
    try:
        await chat_runner._run_chat(
            state,
            slot,
            "[Heartbeat] ...",
            _synthetic_payload=True,
            _turn_actor="gateway",
            _heartbeat_turn=heartbeat,
            _hidden_turn=hidden,
        )
    finally:
        tasks = list(state._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def _slot(state, key: str):
    slot = state.get_or_create_slot(key)
    slot._titled = True
    slot._has_reader = False
    slot._on_message = MagicMock()
    slot._on_row = MagicMock()
    slot._on_card_event = MagicMock()
    return slot


def _quiet_after_text() -> list[AcpEvent]:
    return [
        AcpEvent(kind=EVENT_TEXT_CHUNK, text=_SENTENCE),
        _core_call("nothing_to_do", "tc-quiet"),
        _result(
            session_directive.encode(
                "nothing_to_do", {"note": "no change"}, "Quiet end requested."
            ),
            "tc-quiet",
        ),
        AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
    ]


def _delivered_assistant(slot) -> list[dict]:
    return [
        c.args[1]
        for c in slot._on_message.call_args_list
        if c.args and isinstance(c.args[1], dict) and c.args[1].get("role") == "assistant"
    ]


def _speech(slot) -> list[dict]:
    return [
        m
        for m in slot.messages
        if m.get("role") == "assistant" and is_speech_row(m["role"], m["content"], m.get("meta"))
    ]


def _quiet_row(slot) -> dict:
    (row,) = [
        m
        for m in slot.messages
        if m.get("role") == "tool"
        and (m.get("meta") or {}).get("tool_call_id") == "tc-quiet"
        and (m.get("meta") or {}).get("ends_turn")
    ]
    return row


@pytest.mark.asyncio
async def test_a_heartbeat_quiet_end_folds_the_text_into_its_steps(tmp_path, monkeypatch):
    state = _stub_state(tmp_path)
    slot = _slot(state, "hb-quiet")
    await _drive(state, slot, _quiet_after_text(), monkeypatch, heartbeat=True)
    assert _speech(slot) == [], "the quiet heartbeat left a visible message"
    assert _delivered_assistant(slot) == []
    assert not any(
        c.args and isinstance(c.args[1], dict) and c.args[1].get("role") == "assistant"
        for c in slot._on_row.call_args_list
    ), "the roster recorded the quiet heartbeat as speech"
    assert ("assistant",) not in [c.args[1:] for c in slot._on_card_event.call_args_list]
    meta = _quiet_row(slot)["meta"]
    assert meta[heartbeat_quiet_end.FOLDED_REPLY_KEY] == _SENTENCE
    assert _SENTENCE in meta["output"] and "no change" in meta["output"]
    patches = [
        c.args[1]["meta"]
        for c in state.broadcast_ws.call_args_list
        if c.args
        and c.args[0] == "chat_message_update"
        and heartbeat_quiet_end.FOLDED_REPLY_KEY in (c.args[1].get("meta") or {})
    ]
    assert patches, "the open client was not told where the text went"


@pytest.mark.asyncio
async def test_the_same_quiet_end_outside_a_heartbeat_is_unchanged(tmp_path, monkeypatch):
    state = _stub_state(tmp_path)
    slot = _slot(state, "hb-other")
    await _drive(state, slot, _quiet_after_text(), monkeypatch, heartbeat=False)
    assert [m["content"] for m in _speech(slot)] == [_SENTENCE]
    assert [m["content"] for m in _delivered_assistant(slot)] == [_SENTENCE]
    meta = _quiet_row(slot)["meta"]
    assert heartbeat_quiet_end.FOLDED_REPLY_KEY not in meta
    assert _SENTENCE not in meta.get("output", "")


_NARRATION = "The session resumed. Let me report and record that."
_REPLY = "The main build is broken since this morning."


def _narrate_then_reply() -> list[AcpEvent]:
    return [
        AcpEvent(kind=EVENT_TEXT_CHUNK, text=_NARRATION),
        AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="tc-ci", title="Running: gh run list"),
        AcpEvent(
            kind=EVENT_TOOL_RESULT, tool_call_id="tc-ci", tool_output="failed", tool_final=True
        ),
        AcpEvent(kind=EVENT_TEXT_CHUNK, text=_REPLY),
        AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
    ]


def _ci_row(slot) -> dict:
    (row,) = [
        m
        for m in slot.messages
        if m.get("role") == "tool" and (m.get("meta") or {}).get("tool_call_id") == "tc-ci"
    ]
    return row


@pytest.mark.parametrize("heartbeat,hidden", [(True, False), (False, True)])
@pytest.mark.asyncio
async def test_a_hidden_turn_that_replies_shows_only_its_reply(
    tmp_path, monkeypatch, heartbeat, hidden
):
    state = _stub_state(tmp_path)
    slot = _slot(state, "hb-reply")
    await _drive(
        state, slot, _narrate_then_reply(), monkeypatch, heartbeat=heartbeat, hidden=hidden
    )
    assert [m["content"] for m in _speech(slot)] == [_REPLY]
    assert _NARRATION not in [m["content"] for m in _delivered_assistant(slot)]
    meta = _ci_row(slot)["meta"]
    assert meta[heartbeat_quiet_end.FOLDED_REPLY_KEY] == _NARRATION
    assert _NARRATION in meta["output"]


@pytest.mark.asyncio
async def test_a_hidden_turn_whose_only_words_come_before_a_tool_still_says_them(
    tmp_path, monkeypatch
):
    state = _stub_state(tmp_path)
    slot = _slot(state, "hb-only-words")
    events = [
        AcpEvent(kind=EVENT_TEXT_CHUNK, text=_REPLY),
        AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="tc-ci", title="Running: gh run list"),
        AcpEvent(
            kind=EVENT_TOOL_RESULT, tool_call_id="tc-ci", tool_output="failed", tool_final=True
        ),
        AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
    ]
    await _drive(state, slot, events, monkeypatch, heartbeat=False, hidden=True)
    assert [m["content"] for m in _speech(slot)] == [_REPLY]
    assert _REPLY in [m["content"] for m in _delivered_assistant(slot)]


@pytest.mark.asyncio
async def test_a_turn_a_person_opened_keeps_its_narration(tmp_path, monkeypatch):
    state = _stub_state(tmp_path)
    slot = _slot(state, "typed")
    await _drive(state, slot, _narrate_then_reply(), monkeypatch, heartbeat=False)
    assert [m["content"] for m in _speech(slot)] == [_NARRATION, _REPLY]
