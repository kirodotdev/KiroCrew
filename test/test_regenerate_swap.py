"""A Regenerate replaces the previous reply only once its turn produced a new one.

``chat_turn.regenerate_swap.settle_regenerate`` runs at the turn's end
(``_end_turn_tail``). These tests drive it directly on a real slot: the empty
turn, a reply from a flush or from a partial-reply / command seam, the pinned
write refusing a recreated slot, a successor that does not own the target, and
the crew-log ordinal that must not count the reply being replaced.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from chat_regenerate_cov80_helpers import state_fixture  # noqa: F401

from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat import _history_key_for, _save_slot_to_history
from kiro_crew.dashboard.chat_turn.regenerate_swap import (
    RegenerateTarget,
    rows_before_regenerate_target,
)


def _arm(state, name: str = "s1", linked: str | None = None):
    """A slot holding ``user -> old`` with a Regenerate target armed on it."""
    slot = state.get_or_create_slot(name, linked_session_key=linked)
    slot.append("user", "hi")
    slot.append("assistant", "old")
    slot.drain()
    _save_slot_to_history(state, slot)
    slot._pending_variants = [{"content": "old", "ts": slot.messages[-1].get("ts", "")}]
    slot._regenerate_target = RegenerateTarget(
        generation=slot._turn_generation,
        rows=[slot.messages[-1]],
        history_key=chat_runner.slot_history_key(slot),
        slot_name=name,
    )
    return slot


def _disk(state, slot) -> list:
    return [
        m.get("content") for m in state.conversation_log.read_messages(_history_key_for(slot.key))
    ]


@pytest.mark.asyncio
async def test_an_empty_turn_leaves_the_previous_reply(state) -> None:
    slot = _arm(state)
    saved = AsyncMock(return_value=True)
    with patch.object(chat_runner, "save_slot_off_loop", new=saved):
        await chat_runner.settle_regenerate(state, slot)
    assert saved.await_count == 0
    assert [m["content"] for m in slot.messages] == ["hi", "old"]
    assert slot._regenerate_target is None
    assert slot._pending_variants == []


@pytest.mark.asyncio
async def test_a_partial_reply_replaces_the_previous_one_and_keeps_it_as_a_variant(state) -> None:
    """A reply no flush wrote (a Stop mid-stream persists it through
    ``_note_reply_row``) still replaces the old one and adopts its variants."""
    slot = _arm(state)
    chat_runner._note_reply_row(slot, slot.append("assistant", "partial"))
    await chat_runner.settle_regenerate(state, slot)
    assert [m["content"] for m in slot.messages] == ["hi", "partial"]
    assert [v["content"] for v in slot.messages[-1]["variants"]] == ["old", "partial"]
    assert _disk(state, slot) == ["hi", "partial"]
    assert slot._regenerate_target is None


@pytest.mark.asyncio
async def test_a_flushed_reply_keeps_the_variants_the_flush_attached(state) -> None:
    slot = _arm(state)
    row = slot.append("assistant", "first")
    row["variants"] = chat_runner._adopt_pending_variants(slot, {"content": "first", "ts": ""})
    row["variant_idx"] = 1
    chat_runner._note_reply_row(slot, row)
    # A second segment of the same turn.
    chat_runner._note_reply_row(slot, slot.append("assistant", "second"))
    await chat_runner.settle_regenerate(state, slot)
    assert [m["content"] for m in slot.messages] == ["hi", "first", "second"]
    assert [v["content"] for v in slot.messages[1]["variants"]] == ["old", "first"]
    assert "variants" not in slot.messages[2]


@pytest.mark.asyncio
async def test_the_pinned_write_refuses_a_recreated_slot(state) -> None:
    """A same-name recreate during the turn keeps the transcript key, so only the
    slot-identity pin stops the replace write. Refused, it changes nothing:
    both replies stay, and the replacement's transcript is not rewritten."""
    slot = _arm(state, linked="orig:key")
    chat_runner._note_reply_row(slot, slot.append("assistant", "new"))
    replacement = state.get_or_create_slot("s1b", linked_session_key="orig:key")
    state._slots["s1"] = replacement
    before = _disk(state, slot)
    await chat_runner.settle_regenerate(state, slot)
    assert [m["content"] for m in slot.messages] == ["hi", "old", "new"]
    assert _disk(state, slot) == before
    assert slot._regenerate_target is None


@pytest.mark.asyncio
async def test_the_write_carries_the_authorized_pins(state) -> None:
    slot = _arm(state, linked="orig:key")
    chat_runner._note_reply_row(slot, slot.append("assistant", "new"))
    saved = AsyncMock(return_value=True)
    with patch.object(chat_runner, "save_slot_off_loop", new=saved):
        await chat_runner.settle_regenerate(state, slot)
    kwargs = saved.await_args.kwargs
    assert kwargs["expected_history_key"] == "orig:key"
    assert kwargs["expected_slot_name"] == "s1"
    assert kwargs["best_effort"] is False
    snapshot = saved.await_args.args[2]
    assert [m["content"] for m in snapshot] == ["hi", "new"]
    # The frozen-prefix boundary is read with the snapshot, so a front-trim
    # during the write's await refuses it instead of duplicating rows.
    assert kwargs["expected_disk_older_count"] == slot._disk_older_count


@pytest.mark.asyncio
async def test_a_front_trim_during_the_write_refuses_it(state) -> None:
    slot = _arm(state)
    chat_runner._note_reply_row(slot, slot.append("assistant", "new"))
    real = chat_runner.save_slot_off_loop

    async def _trim_then_save(*args, **kwargs):
        slot._disk_older_count += 1  # what an append at the window cap does
        return await real(*args, **kwargs)

    with patch.object(chat_runner, "save_slot_off_loop", new=_trim_then_save):
        await chat_runner.settle_regenerate(state, slot)
    assert [m["content"] for m in slot.messages] == ["hi", "old", "new"]


def test_the_reply_record_holds_bounded_ids(state) -> None:
    slot = _arm(state)
    with patch.object(chat_runner, "_MAX_SLOT_MESSAGES", 3):
        for i in range(5):
            chat_runner.note_regenerate_reply(slot, slot.append("assistant", f"seg {i}"))
    mids = slot._regenerate_target.reply_mids
    assert len(mids) == 3
    assert all(isinstance(m, str) for m in mids)


@pytest.mark.asyncio
async def test_a_failed_write_leaves_both_replies(state) -> None:
    slot = _arm(state)
    chat_runner._note_reply_row(slot, slot.append("assistant", "new"))
    with patch.object(
        chat_runner, "save_slot_off_loop", new=AsyncMock(side_effect=OSError("disk full"))
    ):
        await chat_runner.settle_regenerate(state, slot)
    assert [m["content"] for m in slot.messages] == ["hi", "old", "new"]
    assert "variants" not in slot.messages[-1]


@pytest.mark.asyncio
async def test_a_successor_turn_does_not_settle_the_target(state) -> None:
    """A turn of another generation (a queued successor) owns no target: its
    replies are not recorded and its end does not remove the old reply."""
    slot = _arm(state)
    slot._turn_generation += 1
    chat_runner._note_reply_row(slot, slot.append("assistant", "unrelated"))
    await chat_runner.settle_regenerate(state, slot)
    assert [m["content"] for m in slot.messages] == ["hi", "old", "unrelated"]
    assert slot._regenerate_target is not None
    assert slot._regenerate_target.reply_mids == []


@pytest.mark.asyncio
async def test_the_ordinal_rows_exclude_the_reply_being_replaced(state) -> None:
    """The re-run must draw the ordinal of the turn it repeats, so the crew log
    records it as a retry rather than a new turn."""
    slot = _arm(state)
    assert [m["content"] for m in rows_before_regenerate_target(slot, slot.messages)] == ["hi"]
    slot._turn_generation += 1
    assert len(rows_before_regenerate_target(slot, slot.messages)) == 2


@pytest.mark.asyncio
async def test_the_turn_end_settles_before_the_queue_hands_off(state) -> None:
    slot = _arm(state)
    order: list[str] = []

    async def _settle(*_a, **_k) -> None:
        order.append("settle")

    async def _hand_off(*_a, **_k) -> None:
        order.append("hand_off")

    turn_exit = chat_runner._TurnExit()
    with (
        patch.object(chat_runner, "settle_regenerate", new=_settle),
        patch.object(chat_runner, "_hand_off_queue", new=_hand_off),
    ):
        await chat_runner._end_turn_tail(state, slot, turn_exit)
        assert order == ["settle", "hand_off"]
        order.clear()
        nested = chat_runner._TurnExit()
        nested.depth = 1
        await chat_runner._end_turn_tail(state, slot, nested)
        assert order == ["hand_off"]


# ── through the real endpoint and turn runner ──


def _run_chat_state(tmp_path, monkeypatch):
    from unittest.mock import MagicMock

    from chat_test_helpers import _make_state

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.context_builder = None
    state.consolidator = None
    state._hook_store = None
    state._yolo = False
    return state


def _client_streaming(events):
    from unittest.mock import MagicMock

    from dashboard_chat_helpers import _provider_mock

    client = _provider_mock()
    client.context_usage_pct = MagicMock(return_value=10.0)

    async def _stream(msg):
        for ev in events:
            yield ev

    client.stream = _stream
    client.stream_command = _stream
    return client


async def _regenerate_through_the_runner(state, slot, events) -> None:
    from aiohttp.test_utils import TestClient, TestServer
    from chat_regenerate_cov80_helpers import _make_regen_app

    state.sessions.get_or_create = AsyncMock(return_value=(_client_streaming(events), True, False))
    with patch(
        "kiro_crew.dashboard.chat_regenerate.reject_if_kiro_unverified",
        new=AsyncMock(return_value=None),
    ):
        async with TestClient(TestServer(_make_regen_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")
            assert resp.status == 200
            await slot.task


@pytest.mark.asyncio
async def test_a_refused_regenerate_turn_keeps_the_previous_reply(tmp_path, monkeypatch) -> None:
    """End to end: the provider refuses before any text, so the previous
    reply is still in the window and on disk."""
    from kiro_crew.acp.types import STOP_REASON_REFUSAL
    from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

    state = _run_chat_state(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    slot.append("assistant", "old")
    slot.drain()
    _save_slot_to_history(state, slot)

    await _regenerate_through_the_runner(
        state, slot, [LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_REFUSAL)]
    )

    assistant = [m["content"] for m in slot.messages if m.get("role") == "assistant"]
    assert assistant == ["old"]
    assert "old" in _disk(state, slot)
    assert slot._regenerate_target is None
    assert slot._pending_variants == []


@pytest.mark.asyncio
async def test_a_regenerate_turn_with_a_reply_replaces_the_previous_one(
    tmp_path, monkeypatch
) -> None:
    from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

    state = _run_chat_state(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    slot.append("assistant", "old")
    slot.drain()
    _save_slot_to_history(state, slot)

    await _regenerate_through_the_runner(
        state,
        slot,
        [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="fresh answer"),
            LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ],
    )

    assistant = [m for m in slot.messages if m.get("role") == "assistant"]
    assert [m["content"] for m in assistant] == ["fresh answer"]
    assert [v["content"] for v in assistant[0]["variants"]] == ["old", "fresh answer"]
    disk = _disk(state, slot)
    assert "old" not in disk and "fresh answer" in disk
    assert slot._regenerate_target is None
