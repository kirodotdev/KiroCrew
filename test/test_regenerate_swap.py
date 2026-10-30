"""A Regenerate replaces the previous reply only once its turn produced a new one.

``chat_turn.regenerate_swap.settle_regenerate`` runs at the turn's end
(``_end_turn_tail``). These tests drive it directly on a real slot: the empty
turn, a reply from a flush or from a partial-reply / command seam, the pinned
write refusing a recreated slot, a successor that does not own the target, and
the crew-log ordinal that must not count the reply being replaced.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_regenerate_cov80_helpers import state_fixture  # noqa: F401

from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat import _history_key_for, _save_slot_to_history
from kiro_crew.dashboard.chat_turn.regenerate_swap import (
    RegenerateTarget,
    rows_before_regenerate_target,
)
from kiro_crew.dashboard.slot_persistence import write_guards


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


def _disk_rows(state, slot) -> list:
    return state.conversation_log.read_messages(_history_key_for(slot.key))


def _disk(state, slot) -> list:
    return [m.get("content") for m in _disk_rows(state, slot)]


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
async def test_a_flushed_reply_gets_the_variants_only_from_the_committed_swap(state) -> None:
    """The segment flush of a Regenerate turn attaches no variants: the first
    reply gets them from the swap, and a later segment gets none."""
    slot = _arm(state)
    first = chat_runner._flush_segment(state, slot, "first", broadcast=False)
    # A second segment of the same turn.
    chat_runner._flush_segment(state, slot, "second", broadcast=False)
    assert "variants" not in first and "variant_idx" not in first
    assert [v["content"] for v in slot._pending_variants] == ["old"]
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


@pytest.mark.asyncio
async def test_a_row_persisted_during_the_write_survives_the_swap(state) -> None:
    """A workflow result appended to disk after the snapshot, before the write
    takes the transcript lock, stays on disk; only the previous reply goes."""
    slot = _arm(state)
    chat_runner._note_reply_row(slot, slot.append("assistant", "new"))
    _save_slot_to_history(state, slot)
    real = chat_runner.save_slot_off_loop
    key = _history_key_for(slot.key)

    async def _append_then_save(*args, **kwargs):
        assert state.conversation_log.append_if_absent(key, "assistant", "workflow result")
        return await real(*args, **kwargs)

    with patch.object(chat_runner, "save_slot_off_loop", new=_append_then_save):
        await chat_runner.settle_regenerate(state, slot)
    assert [m["content"] for m in slot.messages] == ["hi", "new"]
    assert _disk(state, slot) == ["hi", "new", "workflow result"]


@pytest.mark.asyncio
async def test_a_stale_window_save_after_the_swap_does_not_restore_the_old_reply(state) -> None:
    """A full-window save whose snapshot still holds the old reply (a tag edit
    or the dirty flush, copied before the swap's memory removal) and that takes
    the transcript lock after the swap committed writes the window without it."""
    slot = _arm(state)
    chat_runner._note_reply_row(slot, slot.append("assistant", "new"))
    _save_slot_to_history(state, slot)
    real = chat_runner.save_slot_off_loop
    stale_disk: list = []

    async def _commit_then_stale_save(*args, **kwargs):
        committed = await real(*args, **kwargs)
        # Memory still holds the old reply here: the swap applies it only after
        # this returns, so this save's snapshot is the pre-swap window.
        assert [m["content"] for m in slot.messages] == ["hi", "old", "new"]
        assert _save_slot_to_history(state, slot, force=True)
        stale_disk.extend(_disk(state, slot))
        return committed

    with patch.object(chat_runner, "save_slot_off_loop", new=_commit_then_stale_save):
        await chat_runner.settle_regenerate(state, slot)
    assert stale_disk == ["hi", "new"]
    # That save also wrote the variants the swap committed, before memory had them.
    assert [v["content"] for v in _disk_rows(state, slot)[-1]["variants"]] == ["old", "new"]
    assert [m["content"] for m in slot.messages] == ["hi", "new"]
    assert slot._window_swap_applying is None
    # A later ordinary save keeps it that way.
    slot._dirty = True
    assert _save_slot_to_history(state, slot, force=True)
    assert _disk(state, slot) == ["hi", "new"]


def test_a_window_copied_before_a_long_swap_is_retaken_under_the_lock(state) -> None:
    """A save that copied the window before a swap of many rows (a tool-heavy
    turn) and reaches the lock after it re-takes its copy: none of the retired
    rows come back, however many there were."""
    slot = state.get_or_create_slot("long")
    slot.append("user", "hi")
    old = [slot.append("assistant", f"old {i}") for i in range(70)]
    slot.drain()
    assert _save_slot_to_history(state, slot)
    new = slot.append("assistant", "new")
    real = write_guards.paired_window_snapshot
    calls = 0

    def _copy_then_swap(*args, **kwargs):
        nonlocal calls
        calls += 1
        copied = real(*args, **kwargs)
        if calls == 1:
            # The swap commits and memory catches up while this save waits.
            swapped = dict(new, variants=[{"content": "old 69"}, {"content": "new"}])
            write_guards.publish_swap(
                slot, write_guards.AppliedSwap(retired=tuple(old), reply=new, swapped=swapped)
            )
            slot.messages[:] = [m for m in slot.messages if all(m is not o for o in old)]
            new.update(swapped)
            write_guards.finish_swap(slot)
        return copied

    with patch.object(write_guards, "paired_window_snapshot", new=_copy_then_swap):
        assert _save_slot_to_history(state, slot, force=True)
    assert calls == 2
    assert _disk(state, slot) == ["hi", "new"]
    assert len(_disk_rows(state, slot)[-1]["variants"]) == 2


def test_a_swap_that_finishes_during_the_retake_still_corrects_it(state) -> None:
    """The event loop applies the swap and clears its record while the guard
    re-copies the window: the copy is corrected with the record read before it."""
    slot = _arm(state)
    old = slot.messages[1]
    new = slot.append("assistant", "new")
    _save_slot_to_history(state, slot)
    swapped = dict(new, variants=[{"content": "old"}, {"content": "new"}], variant_idx=1)
    real = write_guards.paired_window_snapshot
    calls = 0

    def _copy(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            # The first copy predates the commit, which lands while it waits.
            copied = real(*args, **kwargs)
            write_guards.publish_swap(
                slot, write_guards.AppliedSwap(retired=(old,), reply=new, swapped=swapped)
            )
            return copied
        # The retake copies the pre-swap window, then memory catches up and
        # the record is cleared before the correction runs.
        copied = real(*args, **kwargs)
        slot.messages[:] = [m for m in slot.messages if m is not old]
        new.update(swapped)
        write_guards.finish_swap(slot)
        return copied

    with patch.object(write_guards, "paired_window_snapshot", new=_copy):
        assert _save_slot_to_history(state, slot, force=True)
    assert calls == 2
    assert _disk(state, slot) == ["hi", "new"]
    assert len(_disk_rows(state, slot)[-1]["variants"]) == 2


@pytest.mark.asyncio
async def test_a_previous_reply_partly_trimmed_into_the_prefix_is_kept(state) -> None:
    """A row of the previous reply that left the window for the frozen prefix
    cannot be retired by a rewrite, so the swap is refused and both stay."""
    slot = _arm(state)
    target = slot._regenerate_target
    chat_runner._note_reply_row(slot, slot.append("assistant", "new"))
    # The window cap trimmed the previous reply off the front.
    slot.messages[:] = [m for m in slot.messages if m is not target.rows[0]]
    slot._disk_older_count += 1
    saved = AsyncMock(return_value=True)
    with patch.object(chat_runner, "save_slot_off_loop", new=saved):
        await chat_runner.settle_regenerate(state, slot)
    saved.assert_not_awaited()
    assert slot._regenerate_target is None and slot._pending_variants == []
    assert "variants" not in slot.messages[-1]


@pytest.mark.asyncio
async def test_appends_at_the_cap_during_the_swap_do_not_credit_retired_rows(state) -> None:
    """At the window cap, rows appended while the replace write runs (a Stop
    card, a workflow result) would trim the retired reply into the frozen
    prefix after the write removed it from disk. The trim waits for the swap,
    so later saves write each row once."""
    slot = _arm(state)
    chat_runner._note_reply_row(slot, slot.append("assistant", "new"))
    _save_slot_to_history(state, slot)
    real = chat_runner.save_slot_off_loop

    async def _append_during_the_write(*args, **kwargs):
        slot.append("assistant", "x")
        slot.append("assistant", "y")
        return await real(*args, **kwargs)

    with (
        patch("kiro_crew.dashboard.state._MAX_SLOT_MESSAGES", 3),
        patch.object(chat_runner, "save_slot_off_loop", new=_append_during_the_write),
    ):
        await chat_runner.settle_regenerate(state, slot)
        assert slot._disk_older_count == 0
        assert [m["content"] for m in slot.messages] == ["hi", "new", "x", "y"]
        assert _save_slot_to_history(state, slot, force=True)
        # The first append after the swap trims the excess it held back.
        slot.append("assistant", "z")
        assert [m["content"] for m in slot.messages] == ["x", "y", "z"]
        assert _save_slot_to_history(state, slot, force=True)
    assert _disk(state, slot) == ["hi", "new", "x", "y", "z"]


def test_a_callers_snapshot_from_before_a_swap_is_refused(state) -> None:
    """An explicit snapshot (rewind, fork) taken before a swap committed cannot
    be re-taken, so the save refuses it and writes nothing."""
    slot = _arm(state)
    new = slot.append("assistant", "new")
    _save_slot_to_history(state, slot)
    stale = list(slot.messages)
    write_guards.publish_swap(
        slot, write_guards.AppliedSwap(retired=(stale[1],), reply=new, swapped=dict(new))
    )
    assert not _save_slot_to_history(state, slot, messages=stale)
    assert _disk(state, slot) == ["hi", "old", "new"]


@pytest.mark.asyncio
async def test_a_recovery_replay_of_the_regenerate_settles_the_previous_reply(state) -> None:
    """A Regenerate turn that queues a replay of its own message (session not
    found, a refusal fallback) and ends empty hands its target to the replay:
    the replay's reply replaces the previous one, with the variants."""
    slot = _arm(state)
    target = slot._regenerate_target
    qid = slot.queue_insert(0, "hi")
    chat_runner.carry_regenerate_target(slot, qid)
    await chat_runner.settle_regenerate(state, slot)
    # The empty first attempt changed nothing and left the target waiting.
    assert [m["content"] for m in slot.messages] == ["hi", "old"]
    assert slot._regenerate_target is target and slot._pending_variants == []
    # The drain starts the replay: a new task, so a new generation.
    entry = slot._queue.pop(0)
    replay = asyncio.get_running_loop().create_future()
    slot._turn_generation += 1
    chat_runner.adopt_regenerate_target(slot, [entry], replay)
    assert chat_runner.owned_regenerate_target(slot) is target
    assert [v["content"] for v in slot._pending_variants] == ["old"]
    chat_runner._note_reply_row(slot, slot.append("assistant", "new"))
    await chat_runner.settle_regenerate(state, slot)
    assert [m["content"] for m in slot.messages] == ["hi", "new"]
    assert [v["content"] for v in slot.messages[-1]["variants"]] == ["old", "new"]
    assert _disk(state, slot) == ["hi", "new"]


@pytest.mark.asyncio
async def test_a_turn_that_already_replied_settles_with_its_own_reply(state) -> None:
    """A recovery queued after the turn produced a reply does not take the target:
    the swap uses the reply this turn wrote."""
    slot = _arm(state)
    chat_runner._note_reply_row(slot, slot.append("assistant", "new"))
    chat_runner.carry_regenerate_target(slot, slot.queue_insert(0, "continue"))
    await chat_runner.settle_regenerate(state, slot)
    assert [m["content"] for m in slot.messages] == ["hi", "new"]
    assert slot._regenerate_target is None


@pytest.mark.asyncio
async def test_the_first_attempts_done_callback_leaves_a_handed_over_target(state) -> None:
    slot = _arm(state)
    target = slot._regenerate_target
    release = chat_runner.release_regenerate_on_done(slot, target)
    qid = slot.queue_insert(0, "hi")
    chat_runner.carry_regenerate_target(slot, qid)
    await chat_runner.settle_regenerate(state, slot)
    done = asyncio.get_running_loop().create_future()
    done.set_result(None)
    release(done)
    assert slot._regenerate_target is target
    assert [v["content"] for v in target.carried_variants] == ["old"]


@pytest.mark.asyncio
async def test_a_replay_that_left_the_queue_drops_the_waiting_target(state) -> None:
    """The replay was cancelled before it ran: the previous reply simply stays."""
    slot = _arm(state)
    qid = slot.queue_insert(0, "hi")
    chat_runner.carry_regenerate_target(slot, qid)
    await chat_runner.settle_regenerate(state, slot)
    slot._queue.clear()
    other = asyncio.get_running_loop().create_future()
    slot._turn_generation += 1
    chat_runner.adopt_regenerate_target(slot, [{"id": "someone-else"}], other)
    assert slot._regenerate_target is None
    assert [m["content"] for m in slot.messages] == ["hi", "old"]


@pytest.mark.asyncio
async def test_the_swap_frame_names_the_rows_it_retired(state) -> None:
    """The committed swap's ``chat_variant_switch`` names the retired rows, so a
    client can drop a decided card of the replaced turn before its refetch
    re-seats it. A row is named by its mid; a decided row also by approval id."""
    slot = _arm(state)
    permission = slot.append("permission", "Run git log?", meta={"approval_id": "ap-1"})
    slot._regenerate_target.rows.append(permission)
    expected_mids = [chat_runner.row_mid(r) for r in slot._regenerate_target.rows]
    chat_runner._note_reply_row(slot, slot.append("assistant", "new"))
    state.broadcast_ws.reset_mock()
    await chat_runner.settle_regenerate(state, slot)
    frames = [
        c.args for c in state.broadcast_ws.call_args_list if c.args[0] == "chat_variant_switch"
    ]
    assert len(frames) == 1
    retired = frames[0][1]["retired"]
    assert all(expected_mids) and retired["mids"] == expected_mids
    assert retired["approval_ids"] == ["ap-1"]
    assert "Run git log?" not in [m["content"] for m in slot.messages]


@pytest.mark.asyncio
async def test_an_empty_turn_sends_no_swap_frame(state) -> None:
    """Nothing was retired, so no frame names rows to drop: the client keeps them."""
    slot = _arm(state)
    state.broadcast_ws.reset_mock()
    await chat_runner.settle_regenerate(state, slot)
    assert not [c for c in state.broadcast_ws.call_args_list if c.args[0] == "chat_variant_switch"]


@pytest.mark.asyncio
async def test_the_write_names_the_rows_it_retires(state) -> None:
    slot = _arm(state)
    old = slot._regenerate_target.rows[0]
    chat_runner._note_reply_row(slot, slot.append("assistant", "new"))
    saved = AsyncMock(return_value=True)
    with patch.object(chat_runner, "save_slot_off_loop", new=saved):
        await chat_runner.settle_regenerate(state, slot)
    retired = saved.await_args.kwargs["retired_rows"]
    assert len(retired) == 1 and retired[0] is old


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
        # A ``/prompts get`` re-entry (depth 1) runs the tail for an outer frame
        # that handed it off: it is the turn's end, so it settles too.
        nested = chat_runner._TurnExit()
        nested.depth = 1
        await chat_runner._end_turn_tail(state, slot, nested)
        assert order == ["settle", "hand_off"]


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


def _slash_slot(state, command: str):
    slot = state.get_or_create_slot("s1")
    slot.append("user", command)
    slot.append("assistant", "old")
    slot.drain()
    _save_slot_to_history(state, slot)
    return slot


@pytest.mark.asyncio
async def test_regenerating_a_command_answered_directly_replaces_its_reply(
    tmp_path, monkeypatch
) -> None:
    """``/prompts`` with no prompts is answered by the slash gate's own writer:
    that row is the Regenerate's reply, so it replaces the previous one."""
    from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

    state = _run_chat_state(tmp_path, monkeypatch)
    slot = _slash_slot(state, "/prompts")
    with patch.object(chat_runner, "_list_aim_prompts", return_value=[]):
        await _regenerate_through_the_runner(
            state, slot, [LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")]
        )
    assistant = [m for m in slot.messages if m.get("role") == "assistant"]
    assert len(assistant) == 1 and "No prompts found" in assistant[0]["content"]
    assert [v["content"] for v in assistant[0]["variants"]][0] == "old"
    assert "old" not in _disk(state, slot)


@pytest.mark.asyncio
async def test_regenerating_a_prompt_get_settles_in_its_nested_turn(tmp_path, monkeypatch) -> None:
    """``/prompts get <name>`` runs the prompt as a nested turn (depth 1) that
    runs the tail for the outer one: its reply replaces the previous one."""
    from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

    state = _run_chat_state(tmp_path, monkeypatch)
    slot = _slash_slot(state, "/prompts get tidy")
    with patch.object(
        chat_runner, "_resolve_prompt_mention", return_value=("tidy up the notes", "ok", "")
    ):
        await _regenerate_through_the_runner(
            state,
            slot,
            [
                LLMEvent(kind=EVENT_TEXT_CHUNK, text="notes tidied"),
                LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
            ],
        )
    assistant = [m for m in slot.messages if m.get("role") == "assistant"]
    assert [m["content"] for m in assistant] == ["notes tidied"]
    assert [v["content"] for v in assistant[0]["variants"]] == ["old", "notes tidied"]
    assert slot._regenerate_target is None


# ── a turn whose only output is a backend control notice ──


def _notice_target(state, name: str = "s1"):
    """``_arm`` plus the notice the Claude adapter streams when it compacts."""
    slot = _arm(state, name)
    chat_runner.note_regenerate_notice(slot, "Compacting...")
    return slot


@pytest.mark.asyncio
async def test_a_notice_only_reply_that_dies_keeps_the_previous_reply(state) -> None:
    """Process-dies path: the partial-reply seam saves the notice as a row. It
    answers nothing, so the turn ends empty and the previous reply stays."""
    slot = _notice_target(state)
    chat_runner._note_reply_row(slot, slot.append("assistant", "Compacting..."))
    assert slot._regenerate_target.reply_mids == []
    await chat_runner.settle_regenerate(state, slot)
    assert [m["content"] for m in slot.messages] == ["hi", "old", "Compacting..."]
    assert "variants" not in slot.messages[1] and "variants" not in slot.messages[2]
    assert slot._regenerate_target is None


@pytest.mark.asyncio
async def test_a_notice_only_reply_hands_the_target_to_the_continuation(state) -> None:
    """End-normally path: the compaction continuation queued after a notice-only
    reply takes the target, and its real reply replaces the previous one."""
    slot = _notice_target(state)
    target = slot._regenerate_target
    chat_runner._note_reply_row(slot, slot.append("assistant", "Compacting..."))
    qid = slot.queue_insert(0, "continue")
    chat_runner.carry_regenerate_target(slot, qid)
    assert target.recovery_qid == qid
    await chat_runner.settle_regenerate(state, slot)
    assert [m["content"] for m in slot.messages] == ["hi", "old", "Compacting..."]
    entry = slot._queue.pop(0)
    slot._turn_generation += 1
    chat_runner.adopt_regenerate_target(slot, [entry], asyncio.get_running_loop().create_future())
    chat_runner._note_reply_row(slot, slot.append("assistant", "the real answer"))
    await chat_runner.settle_regenerate(state, slot)
    assert [m["content"] for m in slot.messages] == ["hi", "Compacting...", "the real answer"]
    assert [v["content"] for v in slot.messages[-1]["variants"]] == ["old", "the real answer"]


def test_a_notice_plus_text_is_an_answer(state) -> None:
    slot = _notice_target(state)
    assert chat_runner.is_regenerate_answer(slot, "Compacting... here is the answer")
    assert not chat_runner.is_regenerate_answer(slot, "  Compacting...\n")


def test_a_notice_only_flush_leaves_the_stashed_variants_for_the_reply(state) -> None:
    slot = _notice_target(state)
    stashed = list(slot._pending_variants)
    row = chat_runner._flush_segment(state, slot, "Compacting...", broadcast=False)
    assert "variants" not in row
    assert slot._pending_variants == stashed
    assert slot._regenerate_target.reply_mids == []


@pytest.mark.asyncio
async def test_a_notice_only_regenerate_through_the_runner_waits_for_the_continuation(
    tmp_path, monkeypatch
) -> None:
    """End to end: a Claude Regenerate that streams only ``Compacting...`` and
    ends normally queues the compaction continuation, which takes the target;
    the previous reply is still the current one and carries no variants."""
    from test_dashboard_chat_empty_response import TestEmptyResponseRetry

    from kiro_crew.acp.types import (
        EVENT_COMPACTION_STATUS,
        EVENT_COMPLETE,
        EVENT_TEXT_CHUNK,
        STOP_REASON_END_TURN,
        AcpEvent,
    )
    from kiro_crew.dashboard.chat_utils import _COMPACTION_CONTINUE_MSG

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state, slot, client, run_chat = TestEmptyResponseRetry()._make_state_and_slot(tmp_path)
    slot.append("assistant", "old")
    slot.drain()
    _save_slot_to_history(state, slot)
    old = slot.messages[-1]
    slot._pending_variants = [{"content": "old", "ts": old.get("ts", "")}]
    slot._regenerate_target = RegenerateTarget(
        generation=slot._turn_generation,
        rows=[old],
        history_key=chat_runner.slot_history_key(slot),
        slot_name=slot.key,
    )
    events = [
        AcpEvent(kind=EVENT_COMPACTION_STATUS, text="started"),
        AcpEvent(kind=EVENT_TEXT_CHUNK, text="Compacting...", control_notice=True),
        AcpEvent(kind=EVENT_COMPACTION_STATUS, text="completed", synthesized=True),
        AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
    ]

    async def stream(_message: str):
        for event in events:
            yield event

    client.stream = stream
    client.stream_command = stream
    with patch(
        "kiro_crew.dashboard.chat_runner._start_next_queued_turn",
        new=AsyncMock(return_value=False),
    ):
        await run_chat(state, slot, "hello")
    assert any(e.get("content") == _COMPACTION_CONTINUE_MSG for e in slot._queue)
    target = slot._regenerate_target
    assert target is not None and target.recovery_qid
    assert old in slot.messages and "variants" not in old
    assert all("variants" not in m for m in slot.messages if m.get("role") == "assistant")
    assert [v["content"] for v in target.carried_variants] == ["old"]


@pytest.mark.asyncio
async def test_a_notice_only_regenerate_stopped_mid_compaction_saves_a_notice_row(
    tmp_path, monkeypatch
) -> None:
    """Stop lands on a Claude Regenerate that streamed only ``Compacting...``.
    The cancel seam saves that text, but tagged as a system notice, so the
    next Regenerate's scan passes over it to the reply it re-runs."""
    from test_dashboard_chat_empty_response import TestEmptyResponseRetry

    from kiro_crew.acp.types import EVENT_TEXT_CHUNK, AcpEvent
    from kiro_crew.dashboard.system_notices import is_system_notice

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state, slot, client, run_chat = TestEmptyResponseRetry()._make_state_and_slot(tmp_path)
    slot.append("assistant", "old")
    slot.drain()
    _save_slot_to_history(state, slot)
    old = slot.messages[-1]
    slot._pending_variants = [{"content": "old", "ts": old.get("ts", "")}]
    slot._regenerate_target = RegenerateTarget(
        generation=slot._turn_generation,
        rows=[old],
        history_key=chat_runner.slot_history_key(slot),
        slot_name=slot.key,
    )

    async def stream(_message: str):
        yield AcpEvent(kind=EVENT_TEXT_CHUNK, text="Compacting...", control_notice=True)
        slot._stopping = True
        raise asyncio.CancelledError()

    client.stream = stream
    client.stream_command = stream
    with patch(
        "kiro_crew.dashboard.chat_runner._start_next_queued_turn",
        new=AsyncMock(return_value=False),
    ):
        await run_chat(state, slot, "hello")
    notice = next(m for m in slot.messages if m.get("content") == "Compacting...")
    assert is_system_notice("assistant", notice.get("meta"))
    assert notice["meta"]["notice"] == chat_runner.REGENERATE_NOTICE_ONLY
    assert old in slot.messages and "variants" not in old
    assert slot._regenerate_target is None


def test_a_notice_only_flush_is_tagged_a_system_notice(state) -> None:
    from kiro_crew.dashboard.system_notices import is_system_notice

    slot = _notice_target(state)
    row = chat_runner._flush_segment(state, slot, "Compacting...", broadcast=False)
    assert is_system_notice("assistant", row.get("meta"))
    answer = chat_runner._flush_segment(state, slot, "Compacting... the answer", broadcast=False)
    assert not is_system_notice("assistant", answer.get("meta"))


def test_a_compaction_only_row_outside_a_regenerate_keeps_its_meta(state) -> None:
    slot = state.get_or_create_slot("plain")
    slot.append("user", "hi")
    row = chat_runner._flush_segment(state, slot, "Compacting...", broadcast=False)
    assert (row.get("meta") or {}).get("kind") is None


def _arm_after_an_earlier_turn(state):
    """``earlier q -> earlier a -> hi -> old`` with a Regenerate of ``hi`` armed."""
    slot = state.get_or_create_slot("s1")
    for role, text in (("user", "earlier q"), ("assistant", "earlier a")):
        slot.append(role, text)
    slot.append("user", "hi")
    slot.append("assistant", "old")
    slot.drain()
    _save_slot_to_history(state, slot)
    slot._regenerate_target = RegenerateTarget(
        generation=slot._turn_generation,
        rows=[slot.messages[-1]],
        history_key=chat_runner.slot_history_key(slot),
        slot_name=slot.key,
    )
    return slot


@pytest.mark.asyncio
async def test_a_cold_start_regenerate_replays_history_without_the_reply_it_replaces(
    state,
) -> None:
    """A Regenerate on a session that did not resume natively rebuilds history
    from the transcript. The previous reply is still there (it stays until the
    turn settles) but answers the question being asked again, so the replay
    leaves it out, from the window and from disk alike."""
    from types import SimpleNamespace

    slot = _arm_after_an_earlier_turn(state)
    state.sessions.consume_replay_suppression = MagicMock(return_value=False)
    question = slot.messages[2]
    replay = await chat_runner._session_replay_history(
        state,
        SimpleNamespace(conversation_log=state.conversation_log),
        slot,
        None,
        _history_key_for(slot.key),
        _context_is_new=True,
        _provider_has_history=False,
        _current_replay_message=question,
    )
    assert "earlier q" in replay and "earlier a" in replay
    assert "old" not in replay
    # Outside a Regenerate the same reply is ordinary history.
    slot._regenerate_target = None
    replay = await chat_runner._session_replay_history(
        state,
        SimpleNamespace(conversation_log=state.conversation_log),
        slot,
        None,
        _history_key_for(slot.key),
        _context_is_new=True,
        _provider_has_history=False,
        _current_replay_message=None,
    )
    assert "old" in replay


def test_the_interrupted_turn_preamble_of_a_regenerate_omits_the_reply_it_replaces(
    state,
) -> None:
    """A Regenerate's recovery replay on a natively resumed session restores the
    interrupted turn from the window, which must not carry the old reply as the
    partial output of the turn re-asking its question."""
    from kiro_crew.context import build_interrupted_turn_preamble
    from kiro_crew.dashboard.state import TURN_OPENING_INJECT_KINDS

    slot = _arm_after_an_earlier_turn(state)
    rows = chat_runner.rows_before_regenerate_target(slot, list(slot.messages))
    preamble = build_interrupted_turn_preamble(rows, opener_inject_kinds=TURN_OPENING_INJECT_KINDS)
    assert "hi" in preamble and "old" not in preamble


def test_a_row_kept_as_foreign_is_written_once_when_the_window_carries_it(state) -> None:
    """A workflow result appended while a swap's snapshot is held reaches disk
    first, so that save keeps it as a foreign line and caches it. When the
    window carries the same row, the next save (file unchanged, cached fast
    path) writes it once, not the cached line beside the window's own copy."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    slot.append("assistant", "old")
    slot.drain()
    _save_slot_to_history(state, slot)
    key = _history_key_for(slot.key)
    state.conversation_log.append_if_absent(key, "assistant", "workflow done", mid="wf-1")
    _save_slot_to_history(state, slot)
    assert _disk(state, slot).count("workflow done") == 1
    assert slot._frozen_prefix_cache is not None and slot._frozen_prefix_cache[4]
    slot.append("assistant", "workflow done", meta={"mid": "wf-1", "kind": "workflow_result"})
    _save_slot_to_history(state, slot)
    assert _disk(state, slot).count("workflow done") == 1
    _save_slot_to_history(state, slot)
    assert _disk(state, slot).count("workflow done") == 1


def test_a_cached_foreign_line_the_window_does_not_carry_is_kept(state) -> None:
    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    slot.drain()
    _save_slot_to_history(state, slot)
    key = _history_key_for(slot.key)
    state.conversation_log.append_if_absent(key, "assistant", "from cron", mid="cron-1")
    _save_slot_to_history(state, slot)
    slot.append("assistant", "from cron", meta={"mid": "other"})
    _save_slot_to_history(state, slot)
    assert _disk(state, slot).count("from cron") == 2


def _assert_no_variants_on_the_new_reply(state, slot, content: str) -> None:
    """In memory and, after the turn's dirty save, on disk."""
    row = next(m for m in slot.messages if m.get("content") == content)
    assert "variants" not in row and "variant_idx" not in row
    assert _save_slot_to_history(state, slot, force=True)
    disk = next(m for m in _disk_rows(state, slot) if m.get("content") == content)
    assert "variants" not in disk and "variant_idx" not in disk


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [{"return_value": False}, {"side_effect": OSError("disk full")}],
    ids=["refused", "raises"],
)
async def test_a_declined_write_after_a_real_flush_saves_no_variants(state, outcome) -> None:
    """The reply comes through the real segment flush, and the turn's dirty
    save runs before the swap is decided. A refused or failed replace write
    then keeps both replies, so the old one must not also be a variant of the
    new one, in memory or on disk."""
    slot = _arm(state)
    write = AsyncMock(**outcome)
    chat_runner._flush_segment(state, slot, "new", broadcast=False)
    # The dirty save of the running turn, before settle decides.
    assert _save_slot_to_history(state, slot, force=True)
    with patch.object(chat_runner, "save_slot_off_loop", new=write):
        await chat_runner.settle_regenerate(state, slot)
    write.assert_awaited_once()
    assert [m["content"] for m in slot.messages] == ["hi", "old", "new"]
    assert _disk(state, slot) == ["hi", "old", "new"]
    _assert_no_variants_on_the_new_reply(state, slot, "new")


@pytest.mark.asyncio
async def test_a_reply_trimmed_into_the_prefix_after_a_real_flush_saves_no_variants(
    state,
) -> None:
    """The real flush's append at the window cap trims part of the previous
    reply into the frozen prefix, so settle refuses the swap and both replies
    stay; the new reply must not carry the old one as a variant."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    slot.append("assistant", "old-a")
    slot.append("assistant", "old-b")
    slot.drain()
    _save_slot_to_history(state, slot)
    old_rows = slot.messages[1:]
    slot._pending_variants = [{"content": "old-b", "ts": old_rows[-1].get("ts", "")}]
    slot._regenerate_target = RegenerateTarget(
        generation=slot._turn_generation,
        rows=list(old_rows),
        history_key=chat_runner.slot_history_key(slot),
        slot_name="s1",
    )
    saved = AsyncMock(return_value=True)
    with (
        patch("kiro_crew.dashboard.state._MAX_SLOT_MESSAGES", 2),
        patch.object(chat_runner, "save_slot_off_loop", new=saved),
    ):
        chat_runner._flush_segment(state, slot, "new", broadcast=False)
        assert [m["content"] for m in slot.messages] == ["old-b", "new"]
        assert _save_slot_to_history(state, slot, force=True)
        await chat_runner.settle_regenerate(state, slot)
        saved.assert_not_awaited()
        assert slot._regenerate_target is None and slot._pending_variants == []
        assert _disk(state, slot) == ["hi", "old-a", "old-b", "new"]
        _assert_no_variants_on_the_new_reply(state, slot, "new")
