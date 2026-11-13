"""Successor turns own their admission; nested calls retain their parent's work."""

from __future__ import annotations

import asyncio
import contextvars
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from telegram_helpers import _drop_live_config_snapshot  # noqa: F401
from telegram_helpers import _dispatcher, _dm
from test_shared_crew_entry_points import ALICE
from test_shared_crew_entry_points import service as _service

from kiro_crew import session_allocation
from kiro_crew.dashboard.handlers import side
from kiro_crew.dashboard.side_state import SideState
from kiro_crew.messaging import queue_drain
from kiro_crew.shared_crew import admission_entry, turn_gate
from kiro_crew.shared_crew.admission import OperationState
from kiro_crew.shared_crew.contracts import Principal
from kiro_crew.testing.wait import async_wait_until, await_until

service = _service


@pytest.mark.asyncio
async def test_side_successors_each_hold_their_own_reservation(service, monkeypatch):
    await service.set_limits(replace(service.limits, crew_active=1))
    state = SimpleNamespace(_background_tasks=set())
    slot = SimpleNamespace(key="side-successors", _side=SideState(open=True))
    slot._side.queue_append("second")
    slot._side.queue_append("third")
    monkeypatch.setattr(side, "broadcast_side_result", Mock())
    monkeypatch.setattr(side, "broadcast_side_queue", Mock())
    started = []
    tasks = []
    release = asyncio.Event()

    async def run(_state, _slot, run_id, question, **kwargs):
        started.append((question, turn_gate.admitted_work()))
        if question == "second":
            await await_until(release)
        slot._side.is_complete = True
        side._drain_side_queue(state, slot, run_id)
        if question != "third":
            tasks.append(slot._side.task)
        if question == "first":
            await async_wait_until(
                lambda: len(started) > 1
                or service.scheduler.usage(Principal.unknown().id)[1].queued == 1,
                describe=lambda: started,
            )
            assert len(started) == 1

    monkeypatch.setattr(side, "_run_side_turn", run)
    with turn_gate.initiated_by(ALICE):
        side._dispatch_side_turn(state, slot, "first")
    tasks.append(slot._side.task)
    try:
        await async_wait_until(lambda: len(started) >= 2, describe=lambda: started)
        assert service.scheduler.usage(Principal.unknown().id)[1].active == 1
        release.set()
        await async_wait_until(lambda: len(started) == 3, describe=lambda: started)
        await await_until(asyncio.gather(*tasks))
        assert [text for text, _ in started] == ["first", "second", "third"]
        assert len({work for _, work in started}) == 3
        records = await service.list_work(session_key="side:side-successors")
        assert len(records) == 3
        assert all(work.state is OperationState.COMPLETED for work in records)
        assert sorted(work.budget_key for work in records) == sorted(
            [ALICE, Principal.unknown().id, Principal.unknown().id]
        )
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await await_until(asyncio.gather(*tasks, return_exceptions=True))


@pytest.mark.asyncio
@pytest.mark.parametrize("dispatch", ["lease_release", "held_retry"])
async def test_channel_successor_drops_admission_and_reentry_marks(service, monkeypatch, dispatch):
    monkeypatch.setattr(queue_drain, "_DRAINS", {})
    monkeypatch.setattr(queue_drain, "_HELD_RETRIES", {})
    seen = []
    tasks = []

    async def successor():
        seen.append((turn_gate.admitted_work(), turn_gate.in_channel_turn()))
        await turn_gate.run_admitted(_answer(), session_key="successor")

    async def _answer():
        return "answer"

    async def drain(*args, **kwargs):
        await successor()

    queue_drain.register_drain("telegram", drain)
    monkeypatch.setattr(session_allocation, "wake_other_drains", drain)

    async def parent():
        parent_work = turn_gate.admitted_work()
        assert parent_work and turn_gate.in_channel_turn()
        if dispatch == "held_retry":
            assert queue_drain.drain_when_free(
                channel="telegram", session_key="parent", is_busy=lambda _key: False
            )
            tasks.extend(queue_drain._HELD_RETRIES.values())
        else:
            session = SimpleNamespace(
                semaphore=Mock(), queue=[("id", "next", {"queued_channel": "telegram"})]
            )
            owner = SimpleNamespace(
                _fold_key=lambda key: key,
                absorb_orphaned_release=lambda key: False,
                _background_tasks=set(),
            )
            allocation = SimpleNamespace(_owner=owner, _sessions={"parent": session})
            session_allocation.SessionAllocationService.release(
                allocation, "parent", wake_queue=True
            )
            tasks.extend(owner._background_tasks)
        assert turn_gate.admitted_work() == parent_work
        assert turn_gate.in_channel_turn()

    async def refused(reason):
        pytest.fail(str(reason))

    await turn_gate.run_channel_turn(
        session_key="parent", text="first", automation=False, run=parent, refused=refused
    )
    await await_until(asyncio.gather(*tasks))
    assert seen == [("", False)]
    [work] = await service.list_work(session_key="successor")
    assert work.state is OperationState.COMPLETED


@pytest.mark.asyncio
async def test_independent_context_restores_all_marks_and_preserves_other_context(service):
    unrelated = contextvars.ContextVar("unrelated", default="routing")
    entries = [{"meta": {admission_entry.QUEUED_WORK_META_KEY: "parked"}}]
    with (
        turn_gate.mark_admitted("parent"),
        turn_gate.initiated_by(ALICE),
        turn_gate.queued_work(entries),
    ):
        with pytest.raises(ValueError), turn_gate.independent_turn():
            assert turn_gate.admitted_work() == ""
            assert turn_gate._INITIATOR.get() == ""
            assert turn_gate._QUEUED_WORK.get() == ()
            assert unrelated.get() == "routing"
            raise ValueError("dispatch failed")
        assert turn_gate.admitted_work() == "parent"
        assert turn_gate._INITIATOR.get() == ALICE
        assert turn_gate._QUEUED_WORK.get() == ("parked",)

        async def nested():
            assert turn_gate.admitted_work() == "parent"

        await turn_gate.run_admitted(nested(), session_key="nested")
    assert await service.list_work(session_key="nested") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("drain", [True, False])
async def test_telegram_drain_runs_after_admission_settles(service, monkeypatch, drain):
    dispatcher, _client, _sessions = _dispatcher({7})
    seen = []

    async def run(*args, **kwargs):
        assert turn_gate.admitted_work()
        assert kwargs["drain"] is False
        seen.append("turn")
        return True

    async def drain_queue(key):
        assert not turn_gate.admitted_work()
        assert service.scheduler.usage(Principal.unknown().id)[1].active == 0
        seen.append("drain")

    monkeypatch.setattr(dispatcher, "_run_turn", run)
    monkeypatch.setattr(dispatcher, "_drain_queue", drain_queue)
    await asyncio.wait_for(dispatcher.handle_message(_dm("hello"), drain=drain), 30)
    assert seen == (["turn", "drain"] if drain else ["turn"])


@pytest.mark.asyncio
async def test_dashboard_queue_dispatch_discards_the_predecessor_admission(
    tmp_path, service, monkeypatch
):
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard import chat_runner

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("queued-successor")
    slot._queue.append({"id": "next", "content": "next question", "meta": {}})
    seen = []

    async def run(_state, _slot, message, **kwargs):
        async def body():
            seen.append(turn_gate.admitted_work())

        await turn_gate.gate_turn(_state, _slot, message, kwargs, body)

    monkeypatch.setattr(chat_runner, "_run_chat", run)
    monkeypatch.setattr(chat_runner, "_parked_queue_drain_admitted", lambda: True)
    monkeypatch.setattr(chat_runner, "subagents_hold_user_messages", lambda *_: False)
    monkeypatch.setattr("kiro_crew.dashboard.chat_slack.slot_is_live", lambda *_: True)
    with turn_gate.mark_admitted("predecessor"):
        assert await chat_runner._start_next_queued_turn(state, slot)
        assert turn_gate.admitted_work() == "predecessor"
    await await_until(slot.task)
    assert len(seen) == 1 and seen[0] and seen[0] != "predecessor"
    [work] = await service.list_work(session_key="dashboard:queued-successor")
    assert work.work_id == seen[0] and work.state is OperationState.COMPLETED


@pytest.mark.asyncio
async def test_a_queued_side_question_is_charged_to_its_sender(service, monkeypatch):
    """The sender's budget rides the queue entry and is restored at the drain."""
    state = SimpleNamespace(_background_tasks=set())
    slot = SimpleNamespace(key="side-sender", _side=SideState(open=True))
    slot._side.queue_append("queued", budget=ALICE)
    monkeypatch.setattr(side, "broadcast_side_result", Mock())
    monkeypatch.setattr(side, "broadcast_side_queue", Mock())

    async def run(_state, _slot, run_id, question, **kwargs):
        slot._side.is_complete = True
        if question == "first":
            side._drain_side_queue(state, slot, run_id)

    monkeypatch.setattr(side, "_run_side_turn", run)
    side._dispatch_side_turn(state, slot, "first")
    first = slot._side.task
    await await_until(first)
    await async_wait_until(lambda: slot._side.task is not first, describe=lambda: "no drain")
    await await_until(slot._side.task)
    records = await service.list_work(session_key="side:side-sender")
    assert sorted(work.budget_key for work in records) == sorted([ALICE, Principal.unknown().id])
