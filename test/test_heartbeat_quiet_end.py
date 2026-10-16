"""A crewmate heartbeat's quiet end leaves no chat message (RFC "Crewmate heartbeat").

Driven through the REAL ``_run_chat`` with the REAL directive applier:

* a heartbeat turn that writes a sentence and then calls ``nothing_to_do``
  delivers no ``chat_message``, no roster row and no card event, keeps no
  visible assistant row, and keeps the text on the quiet-end tool row;
* the same stream on a turn a person opened is unchanged: the sentence is an
  ordinary assistant row, delivered;
* a hidden turn that ends with a reply shows only that reply, its narration
  folded onto the tool row after it; words that were the turn's only words
  are still said.
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


async def _drive(state, slot, events, monkeypatch, *, hidden: bool) -> None:
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
    await _drive(state, slot, _quiet_after_text(), monkeypatch, hidden=True)
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
async def test_the_same_quiet_end_on_a_turn_a_person_opened_is_unchanged(tmp_path, monkeypatch):
    state = _stub_state(tmp_path)
    slot = _slot(state, "hb-other")
    await _drive(state, slot, _quiet_after_text(), monkeypatch, hidden=False)
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


@pytest.mark.asyncio
async def test_a_hidden_turn_that_replies_shows_only_its_reply(tmp_path, monkeypatch):
    state = _stub_state(tmp_path)
    slot = _slot(state, "hb-reply")
    await _drive(state, slot, _narrate_then_reply(), monkeypatch, hidden=True)
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
    await _drive(state, slot, events, monkeypatch, hidden=True)
    assert [m["content"] for m in _speech(slot)] == [_REPLY]
    assert _REPLY in [m["content"] for m in _delivered_assistant(slot)]


@pytest.mark.asyncio
async def test_a_turn_a_person_opened_keeps_its_narration(tmp_path, monkeypatch):
    state = _stub_state(tmp_path)
    slot = _slot(state, "typed")
    await _drive(state, slot, _narrate_then_reply(), monkeypatch, hidden=False)
    assert [m["content"] for m in _speech(slot)] == [_NARRATION, _REPLY]


# ── text a fold could not keep whole is never blanked ──


def _long(tag: str, size: int = 9_000) -> str:
    """*size* characters of plain words, starting and ending with *tag*."""
    body = ("plant notes stay in sync " * (size // 25 + 1))[: size - 2 * len(tag) - 2]
    return f"{tag} {body} {tag}"


def _kept(slot) -> str:
    """Every character the transcript still holds: row text plus folded steps."""
    parts = []
    for m in slot.messages:
        parts.append(str(m.get("content") or ""))
        parts.append(str((m.get("meta") or {}).get(heartbeat_quiet_end.FOLDED_REPLY_KEY) or ""))
    return "\n".join(parts)


def _tool(tcid: str) -> list[AcpEvent]:
    return [
        AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id=tcid, title="Running: git status"),
        AcpEvent(kind=EVENT_TOOL_RESULT, tool_call_id=tcid, tool_output="ok", tool_final=True),
    ]


@pytest.mark.asyncio
async def test_a_quiet_end_too_long_to_fold_keeps_every_character(tmp_path, monkeypatch):
    first, second = _long("FIRST-SEGMENT"), _long("SECOND-SEGMENT")
    assert len(first) + len(second) > heartbeat_quiet_end.MAX_FOLDED_CHARS
    events = [
        AcpEvent(kind=EVENT_TEXT_CHUNK, text=first),
        *_tool("tc-a"),
        AcpEvent(kind=EVENT_TEXT_CHUNK, text=second),
        *_quiet_after_text()[1:],
    ]
    state = _stub_state(tmp_path)
    slot = _slot(state, "hb-long-quiet")
    await _drive(state, slot, events, monkeypatch, hidden=True)
    kept = _kept(slot)
    assert first in kept and second in kept, "folding dropped narration past the cap"
    # Not folded, so said as the turn's own rows, in order.
    said = [m["content"] for m in _delivered_assistant(slot)]
    assert said == [first, second]
    assert heartbeat_quiet_end.FOLDED_REPLY_KEY not in _quiet_row(slot)["meta"]


@pytest.mark.asyncio
async def test_a_final_reply_keeps_every_character_of_its_narration(tmp_path, monkeypatch):
    first, second = _long("FIRST-SEGMENT"), _long("SECOND-SEGMENT")
    events = [
        AcpEvent(kind=EVENT_TEXT_CHUNK, text=first),
        *_tool("tc-a"),
        AcpEvent(kind=EVENT_TEXT_CHUNK, text=second),
        *_tool("tc-b"),
        AcpEvent(kind=EVENT_TEXT_CHUNK, text=_REPLY),
        AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
    ]
    state = _stub_state(tmp_path)
    slot = _slot(state, "hb-long-reply")
    await _drive(state, slot, events, monkeypatch, hidden=True)
    kept = _kept(slot)
    assert first in kept and second in kept
    # Each segment fits on the tool row after it, so each folds whole there.
    assert [m["content"] for m in _speech(slot)] == [_REPLY]


@pytest.mark.asyncio
async def test_one_segment_longer_than_a_fold_is_said_not_cut(tmp_path, monkeypatch):
    huge = _long("HUGE-SEGMENT", heartbeat_quiet_end.MAX_FOLDED_CHARS + 2_000)
    events = [
        AcpEvent(kind=EVENT_TEXT_CHUNK, text=huge),
        *_tool("tc-a"),
        AcpEvent(kind=EVENT_TEXT_CHUNK, text=_REPLY),
        AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
    ]
    state = _stub_state(tmp_path)
    slot = _slot(state, "hb-huge-reply")
    await _drive(state, slot, events, monkeypatch, hidden=True)
    assert huge in _kept(slot)
    assert huge in [m["content"] for m in _delivered_assistant(slot)]


# ── a turn that never settles still delivers what it held ──


@pytest.mark.asyncio
async def test_a_hidden_turn_that_fails_after_a_cut_delivers_the_held_row(tmp_path, monkeypatch):
    from kiro_crew.dashboard import chat_runner

    async def _stream(_msg):
        yield AcpEvent(kind=EVENT_TEXT_CHUNK, text=_NARRATION)
        for ev in _tool("tc-a"):
            yield ev
        raise RuntimeError("backend went away")

    client = MagicMock()
    client.stream = _stream
    client.stream_command = _stream
    client.context_usage_pct = MagicMock(return_value=1.0)
    client.client = None
    state = _stub_state(tmp_path)
    slot = _slot(state, "hb-fail")
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
    monkeypatch.setattr(chat_runner, "_start_next_queued_turn", AsyncMock(return_value=False))
    try:
        await chat_runner._run_chat(
            state,
            slot,
            "[Heartbeat] ...",
            _synthetic_payload=True,
            _turn_actor="gateway",
            _hidden_turn=True,
        )
    except Exception:  # noqa: BLE001 - only the delivery is under test
        pass
    finally:
        tasks = list(state._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    assert _NARRATION in [m["content"] for m in _delivered_assistant(slot)]
    assert any(
        c.args and isinstance(c.args[1], dict) and c.args[1].get("content") == _NARRATION
        for c in slot._on_row.call_args_list
    ), "the roster never heard the held row"


def test_release_is_a_no_op_on_a_turn_that_never_built_a_holder() -> None:
    class Turn:
        slot = None

    heartbeat_quiet_end.release_held_rows(Turn())


def test_a_fold_writes_the_steps_copy_before_it_blanks_any_row() -> None:
    """A history flush reads these dicts from another thread, so one copy always exists."""

    class Slot:
        key = "hb-order"
        _dirty = False

    target: dict = {"role": "tool", "ts": "", "meta": {"output": "ok"}}
    seen: list[str] = []

    class Row(dict):
        def __setitem__(self, key, value):
            if key == "content":
                seen.append(target["meta"].get(heartbeat_quiet_end.FOLDED_REPLY_KEY, ""))
            super().__setitem__(key, value)

    row = Row(role="assistant", content=_NARRATION)
    heartbeat_quiet_end._fold(MagicMock(), Slot(), [row], target)
    assert seen == [_NARRATION], "a source row was blanked before its text was kept"
    assert row["content"] == heartbeat_quiet_end.SAY_NOTHING
    assert target["meta"]["output"] == f"ok\n\n{_NARRATION}"


@pytest.mark.asyncio
async def test_narration_naming_an_image_is_said_not_folded(tmp_path, monkeypatch):
    """The save keeps a local image only from a row's own content, never from tool meta."""
    picture = f"Here is the chart. ![chart]({tmp_path / 'scratch' / 'chart.svg'})"
    events = [
        AcpEvent(kind=EVENT_TEXT_CHUNK, text=picture),
        *_quiet_after_text()[1:],
    ]
    state = _stub_state(tmp_path)
    slot = _slot(state, "hb-image")
    await _drive(state, slot, events, monkeypatch, hidden=True)
    assert [m["content"] for m in _delivered_assistant(slot)] == [picture]
    assert heartbeat_quiet_end.FOLDED_REPLY_KEY not in _quiet_row(slot)["meta"]
