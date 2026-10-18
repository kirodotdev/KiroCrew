"""Creator wake-up acceptance tests for visible dashboard child sessions."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.crew_log import emit
from kiro_crew.crew_log import session_tree_projection as stp
from kiro_crew.dashboard import peer_completion_delivery as delivery
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_persistence import _save_slot_to_history
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.peer_completion import (
    PEER_TURN_COMPLETION_ATTESTED_KEY,
    PEER_TURN_COMPLETION_HELD_KEY,
    PEER_TURN_COMPLETION_KIND,
    PEER_TURN_COMPLETION_META_KEY,
    PEER_TURN_COMPLETION_PREPARING_KEY,
    event_from_entry,
    event_id,
    prompt_for_event,
)
from kiro_crew.dashboard.slot_queue_repository import (
    MAX_DURABLE_QUEUE_ENTRIES,
    durable_queue_entries,
    queue_persist_signature,
    sanitize_restored_queue,
)
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS


@pytest.fixture(autouse=True)
def _isolated_crew_log(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crew-home"))
    _floor_monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
    _floor_monkeypatch.setattr(sc, "session_control_enabled", lambda: True)
    _floor_monkeypatch.setattr(
        "kiro_crew.executors.maintenance_executor",
        lambda: type("_NoPool", (), {"submit": staticmethod(lambda *a, **k: None)}),
    )
    stp.reset_for_tests()
    stp.projection().ensure_seeded()
    yield
    emit.flush()
    stp.reset_for_tests()


def _slot(state, key: str):
    return state.get_or_create_slot(key)


def _persist_slot(state, slot) -> None:
    if not slot.messages:
        slot.append("user", f"open {slot.key}", broadcast=False)
    assert _save_slot_to_history(state, slot, force=True)


def _record_child_turn(creator, child, turn: int, *, settle: bool = True) -> str:
    child._created_by = creator.key
    creator_sid = f"sid-{creator.key}"
    child_sid = f"sid-{child.key}"
    emit.on_session_opened(
        creator_sid,
        agent="kirocrew",
        slot=creator.key,
        model="",
        cwd="/work",
    )
    emit.on_session_opened(
        child_sid,
        agent="kirocrew",
        slot=child.key,
        model="",
        cwd="/work",
        parent_slot=creator.key,
        parent_sid=creator_sid,
    )
    emit.on_message_received(
        child_sid,
        turn,
        role="user",
        text=f"turn {turn}",
        source="dashboard",
    )
    emit.on_turn_started(child_sid, turn, attempt=1, actor="crew")
    emit.on_message_sent(child_sid, turn, text=f"result {turn}")
    emit.on_turn_completed(child_sid, turn, stop_reason="end_turn")
    if settle:
        emit.flush()
    return child_sid


def _busy(slot) -> None:
    task = MagicMock()
    task.done.return_value = False
    slot.task = task


@pytest.mark.asyncio
async def test_creator_gets_one_pointer_callback_as_soon_as_child_turn_ends(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    creator = _slot(state, "chat-creator")
    child = _slot(state, "chat-child")
    _persist_slot(state, creator)
    sid = _record_child_turn(creator, child, 1, settle=False)
    start = AsyncMock(return_value=True)
    monkeypatch.setattr("kiro_crew.dashboard.chat_runner._start_next_queued_turn", start)

    prepared = await delivery.enqueue_turn_completed(
        state,
        child,
        child_session_id=sid,
        child_turn_id=1,
    )

    assert prepared is not None
    creator_key, queue_id = prepared
    assert creator_key == creator.key
    start.assert_awaited_once_with(state, creator, required_queue_id=queue_id)
    assert len(creator._queue) == 1
    event = event_from_entry(creator._queue[0])
    assert event is not None
    assert event["child_session"] == child.key
    assert event["transcript_index"] == 1
    assert creator._queue[0][PEER_TURN_COMPLETION_ATTESTED_KEY] is True
    assert "result 1" not in creator._queue[0]["content"]


@pytest.mark.asyncio
async def test_real_terminal_cycle_persists_creator_callback(tmp_path, monkeypatch):
    from kiro_crew.dashboard import chat_runner

    state = _make_state(tmp_path)
    creator = _slot(state, "chat-creator")
    child = _slot(state, "chat-child")
    _persist_slot(state, creator)
    sid = _record_child_turn(creator, child, 1)
    _busy(creator)
    child.task = MagicMock()
    child.task.done.return_value = False
    child._cycle_reached_provider = True
    child._titled = True
    monkeypatch.setattr(chat_runner, "title_then_refresh", AsyncMock())
    monkeypatch.setattr(chat_runner, "generate_session_summary", AsyncMock())
    state.push_slots_update = MagicMock()
    state.broadcast_ws = MagicMock()

    await chat_runner._finish_queue_cycle(
        state,
        child,
        child_session_id=sid,
        child_turn_id=1,
    )

    assert child.task is None
    assert len(creator._queue) == 1
    event = event_from_entry(creator._queue[0])
    assert event is not None and event["child_turn_id"] == 1
    metadata = state.conversation_log.get_metadata(slot_history_key(creator))
    assert metadata["queued_prompts"][0]["kind"] == PEER_TURN_COMPLETION_KIND


@pytest.mark.asyncio
async def test_persistence_failure_never_wakes_creator(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    creator = _slot(state, "chat-creator")
    child = _slot(state, "chat-child")
    _persist_slot(state, creator)
    sid = _record_child_turn(creator, child, 1)
    monkeypatch.setattr(delivery, "save_slot_off_loop", AsyncMock(return_value=False))
    start = AsyncMock(return_value=True)
    monkeypatch.setattr("kiro_crew.dashboard.chat_runner._start_next_queued_turn", start)

    prepared = await delivery.enqueue_turn_completed(
        state,
        child,
        child_session_id=sid,
        child_turn_id=1,
    )

    assert prepared is None
    assert creator._queue == []
    start.assert_not_awaited()


@pytest.mark.asyncio
async def test_callback_outside_durable_queue_bounds_is_not_woken(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    creator = _slot(state, "chat-creator")
    child = _slot(state, "chat-child")
    _persist_slot(state, creator)
    sid = _record_child_turn(creator, child, 1)
    for index in range(MAX_DURABLE_QUEUE_ENTRIES):
        creator.queue_append(f"queued user message {index}")
    start = AsyncMock(return_value=True)
    monkeypatch.setattr("kiro_crew.dashboard.chat_runner._start_next_queued_turn", start)

    prepared = await delivery.enqueue_turn_completed(
        state,
        child,
        child_session_id=sid,
        child_turn_id=1,
    )

    assert prepared is None
    assert all(entry.get("kind") != PEER_TURN_COMPLETION_KIND for entry in creator._queue)
    metadata = state.conversation_log.get_metadata(slot_history_key(creator))
    assert all(
        entry.get("kind") != PEER_TURN_COMPLETION_KIND for entry in metadata["queued_prompts"]
    )
    start.assert_awaited_once_with(state, creator)


@pytest.mark.asyncio
async def test_creator_drain_cannot_consume_callback_before_persistence(tmp_path, monkeypatch):
    from kiro_crew.dashboard import chat_runner

    state = _make_state(tmp_path)
    creator = _slot(state, "chat-creator")
    child = _slot(state, "chat-child")
    _persist_slot(state, creator)
    sid = _record_child_turn(creator, child, 1)
    creator.queue_append("earlier queued user message")
    save_entered = asyncio.Event()
    release_save = asyncio.Event()

    async def _save(_state, slot, *args, **kwargs):
        save_entered.set()
        await release_save.wait()
        slot._queue_persisted_sig = queue_persist_signature(durable_queue_entries(slot._queue))
        return True

    monkeypatch.setattr(delivery, "save_slot_off_loop", _save)
    enqueue = asyncio.create_task(
        delivery.enqueue_turn_completed(
            state,
            child,
            child_session_id=sid,
            child_turn_id=1,
            wake=False,
        )
    )
    try:
        await asyncio.wait_for(save_entered.wait(), timeout=FALLBACK_TIMEOUT_SECS)
        peer_entry = next(
            entry for entry in creator._queue if entry.get("kind") == PEER_TURN_COMPLETION_KIND
        )
        assert peer_entry[PEER_TURN_COMPLETION_PREPARING_KEY] is True

        assert await chat_runner._start_next_queued_turn(state, creator) is False
        assert len(creator._queue) == 2

        release_save.set()
        prepared = await asyncio.wait_for(enqueue, timeout=FALLBACK_TIMEOUT_SECS)
        assert prepared is not None
        assert PEER_TURN_COMPLETION_PREPARING_KEY not in peer_entry
        assert peer_entry[PEER_TURN_COMPLETION_ATTESTED_KEY] is True
    finally:
        if not enqueue.done():
            enqueue.cancel()
            with suppress(asyncio.CancelledError):
                await enqueue


@pytest.mark.asyncio
async def test_cancelled_persistence_releases_creator_queue(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    creator = _slot(state, "chat-creator")
    child = _slot(state, "chat-child")
    _persist_slot(state, creator)
    sid = _record_child_turn(creator, child, 1)
    creator.queue_append("earlier queued user message")
    save_entered = asyncio.Event()
    never_release = asyncio.Event()

    async def _save(*args, **kwargs):
        save_entered.set()
        await never_release.wait()
        return True

    monkeypatch.setattr(delivery, "save_slot_off_loop", _save)
    started: list[str] = []

    async def _start(_state, slot):
        started.append(slot._queue[0]["content"])
        slot.queue_remove_by_id(slot._queue[0]["id"])
        return True

    monkeypatch.setattr("kiro_crew.dashboard.chat_runner._start_next_queued_turn", _start)
    enqueue = asyncio.create_task(
        delivery.enqueue_turn_completed(
            state,
            child,
            child_session_id=sid,
            child_turn_id=1,
            wake=False,
        )
    )
    try:
        await asyncio.wait_for(save_entered.wait(), timeout=FALLBACK_TIMEOUT_SECS)
        enqueue.cancel()
        with pytest.raises(asyncio.CancelledError):
            await enqueue

        assert all(PEER_TURN_COMPLETION_PREPARING_KEY not in entry for entry in creator._queue)
        assert creator._queue == []
        assert started == ["earlier queued user message"]
    finally:
        if not enqueue.done():
            enqueue.cancel()
            with suppress(asyncio.CancelledError):
                await enqueue


@pytest.mark.asyncio
async def test_gateway_restart_keeps_and_delivers_callback_once(tmp_path, monkeypatch):
    first = _make_state(tmp_path)
    creator = _slot(first, "chat-creator")
    child = _slot(first, "chat-child")
    _persist_slot(first, creator)
    sid = _record_child_turn(creator, child, 1)
    _busy(creator)

    prepared = await delivery.enqueue_turn_completed(
        first,
        child,
        child_session_id=sid,
        child_turn_id=1,
    )
    assert prepared is not None
    metadata = first.conversation_log.get_metadata(slot_history_key(creator))
    restored_entries = sanitize_restored_queue(metadata["queued_prompts"])
    assert len(restored_entries) == 1
    assert restored_entries[0]["kind"] == PEER_TURN_COMPLETION_KIND
    assert PEER_TURN_COMPLETION_ATTESTED_KEY not in restored_entries[0]

    restarted = _make_state(tmp_path)
    restored_creator = _slot(restarted, creator.key)
    restored_creator._queue[:] = sanitize_restored_queue(
        [
            {"id": "restored-user", "content": "run only after user action"},
            *metadata["queued_prompts"],
        ]
    )
    stp.reset_for_tests()
    assert stp.projection().seeded_for_current_store is False
    started: list[str] = []

    async def _start(_state, slot, *, required_queue_id=None):
        queue_id = required_queue_id or slot._queue[0]["id"]
        started.append(queue_id)
        slot.queue_remove_by_id(queue_id)
        return True

    monkeypatch.setattr("kiro_crew.dashboard.chat_runner._start_next_queued_turn", _start)

    assert await delivery.recover_pending(restarted) == 1
    assert await delivery.recover_pending(restarted) == 0
    assert started == [prepared[1]]
    assert [entry["id"] for entry in restored_creator._queue] == ["restored-user"]


@pytest.mark.asyncio
async def test_memory_preparation_retries_exact_callback_not_restored_user(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    creator = _slot(state, "chat-creator")
    creator.queue_append("restored user")
    peer_id = creator.queue_append("peer callback", kind=PEER_TURN_COMPLETION_KIND)
    ready = asyncio.Event()

    async def _memory():
        await ready.wait()

    memory_task = asyncio.create_task(_memory())
    state.memory_startup_task = memory_task
    start = AsyncMock(return_value=True)
    monkeypatch.setattr("kiro_crew.dashboard.chat_runner._start_next_queued_turn", start)
    try:
        assert await delivery._wake_if_idle(state, creator, peer_id) is False
        start.assert_not_awaited()
        ready.set()
        await asyncio.wait_for(memory_task, timeout=FALLBACK_TIMEOUT_SECS)
        retries = [task for task in state._background_tasks if task is not memory_task]
        if retries:
            await asyncio.wait_for(asyncio.gather(*retries), timeout=FALLBACK_TIMEOUT_SECS)
        start.assert_awaited_once_with(state, creator, required_queue_id=peer_id)
    finally:
        ready.set()
        if not memory_task.done():
            memory_task.cancel()
            with suppress(asyncio.CancelledError):
                await memory_task


@pytest.mark.asyncio
async def test_two_child_turns_queue_two_ordered_distinct_callbacks(tmp_path):
    state = _make_state(tmp_path)
    creator = _slot(state, "chat-creator")
    child = _slot(state, "chat-child")
    _persist_slot(state, creator)
    _busy(creator)
    sid = _record_child_turn(creator, child, 1)
    await delivery.enqueue_turn_completed(
        state,
        child,
        child_session_id=sid,
        child_turn_id=1,
        wake=False,
    )
    _record_child_turn(creator, child, 2)
    await delivery.enqueue_turn_completed(
        state,
        child,
        child_session_id=sid,
        child_turn_id=2,
        wake=False,
    )

    events = [event_from_entry(entry) for entry in creator._queue]
    concrete = [event for event in events if event is not None]
    assert [event["child_turn_id"] for event in concrete] == [1, 2]
    assert [event["transcript_index"] for event in concrete] == [1, 2]
    assert len({event["event_id"] for event in concrete}) == 2


@pytest.mark.asyncio
async def test_only_crew_log_attested_creator_receives_callback(tmp_path):
    state = _make_state(tmp_path)
    attested = _slot(state, "chat-attested")
    other = _slot(state, "chat-other")
    child = _slot(state, "chat-child")
    _persist_slot(state, attested)
    _persist_slot(state, other)
    sid = _record_child_turn(attested, child, 1)
    child._created_by = other.key
    _busy(attested)
    _busy(other)

    prepared = await delivery.enqueue_turn_completed(
        state,
        child,
        child_session_id=sid,
        child_turn_id=1,
    )

    assert prepared is not None and prepared[0] == attested.key
    assert len(attested._queue) == 1
    assert other._queue == []


@pytest.mark.asyncio
async def test_child_parked_on_approval_emits_no_callback(tmp_path):
    state = _make_state(tmp_path)
    creator = _slot(state, "chat-creator")
    child = _slot(state, "chat-child")
    _persist_slot(state, creator)
    sid = _record_child_turn(creator, child, 1)
    pending = asyncio.get_running_loop().create_future()
    child._approval_futures["approval"] = pending
    try:
        assert (
            await delivery.enqueue_turn_completed(
                state,
                child,
                child_session_id=sid,
                child_turn_id=1,
            )
            is None
        )
        assert creator._queue == []
    finally:
        pending.cancel()


@pytest.mark.asyncio
async def test_terminal_child_is_completion_pending_not_stalled(tmp_path):
    state = _make_state(tmp_path)
    creator = _slot(state, "chat-creator")
    child = _slot(state, "chat-child")
    child._created_by = creator.key
    _persist_slot(state, creator)
    _busy(creator)
    sid = _record_child_turn(creator, child, 1)
    await delivery.enqueue_turn_completed(
        state,
        child,
        child_session_id=sid,
        child_turn_id=1,
        wake=False,
    )
    creator.task = None
    child.queue_append("follow-up work")

    result = await sc.created_session_status(state, caller_session_key=slot_history_key(creator))
    row = next(row for row in result["sessions"] if row["target"] == child.key)
    assert row["status"] == "completion_pending"


@pytest.mark.asyncio
async def test_idle_unfinished_and_evidence_backed_stalled_are_distinct(tmp_path, monkeypatch):
    from kiro_crew.dashboard import session_health

    state = _make_state(tmp_path)
    creator = _slot(state, "chat-creator")
    idle = _slot(state, "chat-idle")
    idle._created_by = creator.key
    stalled = _slot(state, "chat-stalled")
    stalled._created_by = creator.key
    _busy(stalled)
    monitor = session_health.SessionHealthMonitor(stall_after_secs=0, include_log_scan=False)
    monkeypatch.setattr(session_health, "default_monitor", lambda: monitor)

    result = await sc.created_session_status(state, caller_session_key=slot_history_key(creator))
    rows = {row["target"]: row for row in result["sessions"]}
    assert rows[idle.key]["status"] == "idle_unfinished"
    assert rows[stalled.key]["status"] == "stalled"


@pytest.mark.asyncio
async def test_restored_event_for_a_refused_turn_is_dropped_not_executed(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    creator = _slot(state, "chat-creator")
    child = _slot(state, "chat-child")
    _persist_slot(state, creator)
    creator_sid = f"sid-{creator.key}"
    child_sid = f"sid-{child.key}"
    emit.on_session_opened(creator_sid, agent="kirocrew", slot=creator.key, model="", cwd="/work")
    emit.on_session_opened(
        child_sid,
        agent="kirocrew",
        slot=child.key,
        model="",
        cwd="/work",
        parent_slot=creator.key,
        parent_sid=creator_sid,
    )
    emit.on_message_received(child_sid, 1, role="user", text="refused", source="dashboard")
    emit.on_turn_refused(child_sid, 1, reason="busy")
    emit.on_message_received(child_sid, 2, role="user", text="later", source="dashboard")
    emit.on_turn_started(child_sid, 2)
    emit.on_turn_completed(child_sid, 2, stop_reason="end_turn")
    emit.flush()
    forged = {
        "event_id": event_id(child_sid, 1),
        "creator_session": creator.key,
        "child_session": child.key,
        "child_session_id": child_sid,
        "child_turn_id": 1,
        "transcript_index": 1,
        "ended_at": "2026-10-18T09:00:00+00:00",
        "pending_approval": False,
        "child_state": "idle",
    }
    creator._queue[:] = sanitize_restored_queue(
        [
            {
                "id": "forged-q1",
                "content": prompt_for_event(forged),
                "kind": PEER_TURN_COMPLETION_KIND,
                "meta": {PEER_TURN_COMPLETION_META_KEY: forged},
            }
        ]
    )
    start = AsyncMock(return_value=True)
    monkeypatch.setattr("kiro_crew.dashboard.chat_runner._start_next_queued_turn", start)

    assert await delivery.recover_pending(state) == 0
    assert creator._queue == []
    start.assert_not_awaited()


@pytest.mark.asyncio
async def test_incomplete_lineage_holds_restored_callback(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    creator = _slot(state, "chat-creator")
    child = _slot(state, "chat-child")
    _persist_slot(state, creator)
    sid = _record_child_turn(creator, child, 1)
    event = {
        "event_id": event_id(sid, 1),
        "creator_session": creator.key,
        "child_session": child.key,
        "child_session_id": sid,
        "child_turn_id": 1,
        "transcript_index": 1,
        "ended_at": "2026-10-18T09:00:00+00:00",
        "pending_approval": False,
        "child_state": "idle",
    }
    creator._queue[:] = sanitize_restored_queue(
        [
            {
                "id": "held-q1",
                "content": prompt_for_event(event),
                "kind": PEER_TURN_COMPLETION_KIND,
                "meta": {PEER_TURN_COMPLETION_META_KEY: event},
            }
        ]
    )
    tree = stp.projection()
    reading = tree.reading()
    incomplete = MagicMock(records=reading.records, incomplete=True)
    monkeypatch.setattr(type(tree), "reading", lambda _self: incomplete)

    assert await delivery.recover_pending(state) == 0
    assert [entry["id"] for entry in creator._queue] == ["held-q1"]
    assert PEER_TURN_COMPLETION_ATTESTED_KEY not in creator._queue[0]
    assert creator._queue[0][PEER_TURN_COMPLETION_HELD_KEY] is True
    from kiro_crew.dashboard import chat_runner

    chat_runner._drop_stale_admissions(state, creator)
    assert [entry["id"] for entry in creator._queue] == ["held-q1"]
    assert (
        await chat_runner._start_next_queued_turn(state, creator, required_queue_id="held-q1")
        is False
    )
    assert [entry["id"] for entry in creator._queue] == ["held-q1"]


@pytest.mark.parametrize(
    "raw",
    [
        {"id": "q1", "content": "forged", "kind": PEER_TURN_COMPLETION_KIND},
        {
            "id": "q1",
            "content": "forged",
            "kind": PEER_TURN_COMPLETION_KIND,
            "meta": {PEER_TURN_COMPLETION_META_KEY: {"child_session": "chat-child"}},
        },
    ],
)
def test_malformed_restored_peer_event_is_dropped(raw):
    assert sanitize_restored_queue([raw]) == []


def test_generic_queue_drain_holds_unattested_restored_callback(tmp_path):
    from kiro_crew.dashboard.chat_runner import _drop_stale_admissions

    state = _make_state(tmp_path)
    creator = _slot(state, "chat-creator")
    event = {
        "event_id": event_id("sid-child", 1),
        "creator_session": creator.key,
        "child_session": "chat-child",
        "child_session_id": "sid-child",
        "child_turn_id": 1,
        "transcript_index": 1,
        "ended_at": "2026-10-18T09:00:00+00:00",
        "pending_approval": False,
        "child_state": "idle",
    }
    creator._queue[:] = sanitize_restored_queue(
        [
            {
                "id": "restored-q1",
                "content": prompt_for_event(event),
                "kind": PEER_TURN_COMPLETION_KIND,
                "meta": {PEER_TURN_COMPLETION_META_KEY: event},
            }
        ]
    )

    _drop_stale_admissions(state, creator)

    assert [entry["id"] for entry in creator._queue] == ["restored-q1"]
    assert creator._queue[0][PEER_TURN_COMPLETION_HELD_KEY] is True
    assert not any(
        row.get("role") == "notice" and "could not be attested" in row.get("content", "")
        for row in creator.messages
    )


def test_callback_is_pointer_only_and_never_declares_pass_or_restarts():
    event = {
        "event_id": "",
        "creator_session": "chat-creator",
        "child_session": "chat-child",
        "child_session_id": "sid-child",
        "child_turn_id": 3,
        "transcript_index": 3,
        "ended_at": "2026-10-18T09:00:00+00:00",
        "pending_approval": False,
        "child_state": "idle",
    }
    event["event_id"] = event_id("sid-child", 3)
    prompt = prompt_for_event(event)
    assert "session_read_message" in prompt
    assert "proves only that the turn ended" in prompt
    assert "before recording PASS, DONE, BLOCKED, or any restart" in prompt
    assert "result" not in event
