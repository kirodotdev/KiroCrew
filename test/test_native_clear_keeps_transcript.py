"""A native ``/clear`` empties the live pane but keeps the cleared turns in the transcript.

After kiro-cli confirms a clear (``_on_clear_status``, then ``_keep_cleared_turns``),
every row the window held -- the earlier turns and the typed ``/clear`` row -- stays
on disk above the "Conversation cleared." row. The full read (no ``limit``) and the paged read
(``?limit=``) serve the same rows, before and after later saves, with or without
another writer's line in the transcript; and a slot restored from the file still
holds the ``/clear`` row, so the session card's source-link chips start over at it.

The window is built through real ``_run_chat`` turns, so it carries the transient
``done`` rows the save never writes.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state, pin_kiro_cli_harness

from kiro_crew.acp.types import EVENT_CLEAR_STATUS, EVENT_TEXT_CHUNK, AcpEvent
from kiro_crew.crew_log import emit
from kiro_crew.dashboard.chat_handlers import _hydrate_slot_from_history
from kiro_crew.dashboard.chat_persistence import save_slot_off_loop
from kiro_crew.dashboard.chat_runner import _run_chat
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.slot_persistence import write_guards
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.dashboard.state_parts import slot_transcript
from kiro_crew.history_schema import monotonic_transcript_ts

SESSION = "acp-clear-keeps-0001"
PROMPT = "open the widgets fix"
REPLY = "opened https://github.com/acme/widgets/pull/11"
CONFIRMATION = "🗑️ Conversation cleared."
OUTSIDE = "outside writer's note"
ARRIVED = "a card that landed during the clear's save"


@pytest.fixture(autouse=True)
def _crew_log_caches():
    """No crew-log state carried between tests (the rootdir floor pins the data home)."""
    emit.reset_caches()
    yield
    emit.reset_caches()


def _state_and_slot(tmp_path: Path, events: list[AcpEvent]) -> tuple[Any, Any]:
    """A slot whose backend streams *events* (the shape ``test_crew_log_order`` uses)."""
    pin_kiro_cli_harness()
    state = _make_state(tmp_path)
    client = MagicMock()
    client.session_id = SESSION
    client.client._session_id = SESSION
    client.shutdown = AsyncMock()

    async def _stream(*_a, **_kw):
        for event in list(events):
            yield event

    client.stream = _stream
    client.stream_command = _stream
    state.sessions.get_or_create = AsyncMock(return_value=(client, False, False))
    state.sessions.release = MagicMock()
    state.sessions.reset = AsyncMock()
    state.sessions.set_approval_policy = MagicMock()
    state.sessions.check_context_usage = MagicMock()
    state.sessions.get_slack_link = MagicMock(return_value=(None, None))
    state.sessions.record_failure = AsyncMock()
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.is_yolo_active = MagicMock(return_value=False)
    state._background_tasks = set()
    return state, state.get_or_create_slot("clear-slot")


def _next_ts(previous: str) -> str:
    """A ts one microsecond after *previous*: ordering set, not left to the clock."""
    return monotonic_transcript_ts(previous, datetime.fromisoformat(previous))


async def _first_turn(state: Any, slot: Any, events: list[AcpEvent]) -> None:
    """An ordinary turn whose reply names a pull request, saved."""
    events[:] = [AcpEvent(kind=EVENT_TEXT_CHUNK, text=REPLY)]
    slot.append("user", PROMPT, "msg msg-u")
    await _run_chat(state, slot, PROMPT)
    assert await save_slot_off_loop(state, slot, force=True)


async def _outside_append(state: Any, slot: Any) -> None:
    """Another writer's row, stamped after the reply and before ``/clear`` is typed."""
    reply_ts = next(m["ts"] for m in slot.messages if m.get("content") == REPLY)
    ts = _next_ts(reply_ts)
    entry = {"role": "assistant", "content": OUTSIDE, "cls": "msg msg-a", "ts": ts}
    entry["meta"] = {"mid": "outside-1"}
    assert await asyncio.to_thread(
        state.conversation_log.append_if_absent,
        slot_history_key(slot),
        "assistant",
        OUTSIDE,
        cls="msg msg-a",
        mid="outside-1",
        entry=entry,
    )
    # The next row the slot stamps sorts after the outside row.
    slot._disk_tail_ts = ts


async def _clear_turn(state: Any, slot: Any, events: list[AcpEvent]) -> None:
    """The typed ``/clear`` row (appended at send time, as the send handler does)."""
    events[:] = [AcpEvent(kind=EVENT_CLEAR_STATUS)]
    slot.append("user", "/clear", "msg msg-u")
    await _run_chat(state, slot, "/clear")


async def _settle(state: Any, slot: Any) -> None:
    """Save, then an outside metadata write, then a save that must re-read the file."""
    assert await save_slot_off_loop(state, slot, force=True)
    key = slot_history_key(slot)
    assert await asyncio.to_thread(
        state.conversation_log.update_metadata_if, key, {"outside_mark": 1}, lambda _m: True
    )
    assert await save_slot_off_loop(state, slot, force=True)


def _file_contents(state: Any, slot: Any) -> list[str]:
    path = state.conversation_log._path(slot_history_key(slot))
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return [json.loads(ln)["content"] for ln in lines[1:]]


async def _reads(state: Any, slot: Any) -> tuple[list[str], list[str]]:
    """The full read and the paged read of the slot, as message contents."""
    out = []
    async with TestClient(TestServer(_make_app(state))) as client:
        for query in ("", "?limit=200"):
            resp = await client.get(f"/api/chat/slots/{slot.key}{query}")
            assert resp.status == 200
            out.append([m["content"] for m in (await resp.json())["messages"]])
    return out[0], out[1]


async def _assert_kept(state: Any, slot: Any, expected: list[str]) -> None:
    on_disk = _file_contents(state, slot)
    assert on_disk == expected, f"the file holds {on_disk}"
    full, paged = await _reads(state, slot)
    assert full == expected, f"the full read serves {full}"
    assert paged == expected, f"the paged read serves {paged}"
    assert slot._disk_older_count == slot._disk_older_durable_count
    key = slot_history_key(slot)
    restored = _ChatSlot(key="clear-slot-restored")
    _hydrate_slot_from_history(
        state,
        restored,
        meta=state.conversation_log.get_metadata(key),
        all_messages=state.conversation_log.read_messages_chained(key),
    )
    assert "/clear" in [m.get("content") for m in restored.messages]
    assert restored.source_links_payload()["links"] == []


@pytest.mark.asyncio
async def test_a_native_clear_keeps_the_cleared_turns_on_disk_and_in_both_reads(tmp_path):
    events: list[AcpEvent] = []
    state, slot = _state_and_slot(tmp_path, events)
    await _first_turn(state, slot, events)
    await _clear_turn(state, slot, events)

    assert [m["content"] for m in slot.messages if m["role"] != "done"] == [CONFIRMATION]
    expected = [PROMPT, REPLY, "/clear", CONFIRMATION]
    await _assert_kept(state, slot, expected)
    await _settle(state, slot)
    await _assert_kept(state, slot, expected)


@pytest.mark.asyncio
@pytest.mark.parametrize("saved_before_clear", [False, True])
async def test_another_writers_row_above_the_clear_is_kept_once(tmp_path, saved_before_clear):
    events: list[AcpEvent] = []
    state, slot = _state_and_slot(tmp_path, events)
    await _first_turn(state, slot, events)
    await _outside_append(state, slot)
    if saved_before_clear:
        # An earlier save already kept the outside row as a foreign line.
        assert await save_slot_off_loop(state, slot, force=True)
    await _clear_turn(state, slot, events)

    expected = [PROMPT, REPLY, OUTSIDE, "/clear", CONFIRMATION]
    await _assert_kept(state, slot, expected)
    await _settle(state, slot)
    await _assert_kept(state, slot, expected)


@pytest.mark.asyncio
@pytest.mark.parametrize("at_cap", [False, True])
async def test_a_row_arriving_during_the_clears_save_stays_in_the_window(
    tmp_path, monkeypatch, at_cap
):
    events: list[AcpEvent] = []
    state, slot = _state_and_slot(tmp_path, events)
    await _first_turn(state, slot, events)
    armed = {"on": False}
    real_snapshot = write_guards.paired_window_snapshot

    def _snapshot_then_arrive(target, messages, expected_disk_older_count):
        snapshot = real_snapshot(target, messages, expected_disk_older_count)
        if armed["on"] and target is slot:
            armed["on"] = False
            row = slot.append("assistant", ARRIVED, "msg msg-a", broadcast=False)
            state.conversation_log.append_if_absent(
                slot_history_key(slot),
                "assistant",
                ARRIVED,
                cls="msg msg-a",
                mid=row["meta"]["mid"],
                entry=dict(row),
            )
        return snapshot

    monkeypatch.setattr(write_guards, "paired_window_snapshot", _snapshot_then_arrive)

    async def _arm_then_clear(*_a, **_kw):
        # Armed when the clear frame is delivered, so the next snapshot is the
        # one the clear handler's own save takes.
        armed["on"] = True
        yield AcpEvent(kind=EVENT_CLEAR_STATUS)

    client, _new, _reset = state.sessions.get_or_create.return_value
    client.stream = _arm_then_clear
    client.stream_command = _arm_then_clear
    slot.append("user", "/clear", "msg msg-u")
    if at_cap:
        # The window is full, so the row arriving during the save trims the
        # (saved) front row off it and credits it to the prefix itself.
        monkeypatch.setattr(slot_transcript, "_MAX_SLOT_MESSAGES", len(slot.messages))
    await _run_chat(state, slot, "/clear")
    assert not armed["on"], "the clear handler's save never took a window snapshot"

    window = [m["content"] for m in slot.messages if m["role"] != "done"]
    assert window == [ARRIVED, CONFIRMATION]
    expected = [PROMPT, REPLY, "/clear", ARRIVED, CONFIRMATION]
    await _settle(state, slot)
    await _assert_kept(state, slot, expected)
