"""Crewmate Send steers a running turn first and cancels only an unconsumed steer."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from test_responsive_chat import _busy
from test_responsive_chat import fixture as responsive_fixture  # noqa: F401  (registers it)

from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard import responsive_chat as rc


class _Clock:
    """The grace timer's clock: records each wait and returns only when released."""

    def __init__(self) -> None:
        self.delays: list[float] = []
        self.release = asyncio.Event()

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        await asyncio.wait_for(self.release.wait(), timeout=30)


@pytest.fixture
def env(responsive_fixture):  # noqa: F811  (the shared fixture, by its imported name)
    return responsive_fixture


@pytest.fixture
def clock(monkeypatch):
    gate = _Clock()
    monkeypatch.setattr(rc, "_grace_sleep", gate)
    return gate


def _steerable(slot):
    _busy(slot)
    wires: list[str] = []

    async def steer(message):
        wires.append(message)
        return True

    slot._acp_client.supports_steer = True
    slot._acp_client.steer = steer
    return wires


async def _send(request, slot, text, send_id=None):
    meta = {"sendId": send_id} if send_id else None
    return json.loads((await asyncio.wait_for(rc.accept(request, slot, text, meta), 30)).text)


async def _expire(clock, grace):
    clock.release.set()
    await asyncio.wait_for(asyncio.gather(grace, return_exceptions=True), timeout=30)


def _consume(state, slot, wire):
    chat_runner._settle_consumed_steers(slot, f"<user_message>\n{wire}\n</user_message>", state)


async def _drain_once(state, slot, monkeypatch):
    received = []

    async def run(st, sl, message, **kwargs):
        received.append((message, kwargs))

    monkeypatch.setattr(chat_runner, "_run_chat", run)
    slot.task.cancel()
    slot.task = None
    assert await chat_runner._start_next_queued_turn(state, slot)
    await asyncio.wait_for(slot.task, timeout=30)
    return received


@pytest.mark.asyncio
async def test_steer_consumed_within_grace_never_cancels(env, clock):
    state, slot, request, *_ = env
    wires = _steerable(slot)
    receipt = await _send(request, slot, "Quick question", "send-a")
    # The receipt is back before the grace timer has even started waiting.
    assert receipt == {"ok": True, "steered": True, "responsive": "steered"}
    grace = rc.turn_state(slot).grace
    assert grace is not None and not grace.done()
    await asyncio.wait_for(_until(lambda: clock.delays), timeout=30)
    assert clock.delays == [rc.STEER_GRACE_SECONDS]
    assert wires == [rc.RESPONSE_GUIDANCE + "Quick question"]
    assert slot._queue == []
    _consume(state, slot, wires[0])
    await _expire(clock, grace)
    state.sessions.cancel_owned_turn.assert_not_called()
    assert slot._queue == []
    assert [r["content"] for r in slot.messages if r["role"] == "user"] == [
        "Finish the original task",
        "Quick question",
    ]
    slot.task.cancel()


@pytest.mark.asyncio
async def test_unconsumed_steer_cancels_once_after_grace_and_runs_next_turn_once(
    env, clock, monkeypatch
):
    state, slot, request, *_ = env
    _steerable(slot)
    owner = rc.turn_state(slot).owner
    await _send(request, slot, "Answer me", "send-b")
    grace = rc.turn_state(slot).grace
    state.sessions.cancel_owned_turn.assert_not_called()
    await _expire(clock, grace)
    state.sessions.cancel_owned_turn.assert_awaited_once_with(
        rc.effective_session_key(slot), owner, wait_ack_timeout=rc.CANCEL_ACK_SECONDS
    )
    assert rc.turn_state(slot).interrupted
    # The cancelled turn's teardown hands the steer on through the requeue path.
    chat_runner._requeue_unconsumed_steers(state, slot)
    assert len(slot._queue) == 1 and rc.priority_entry(slot._queue[0])
    assert slot._queue[0]["meta"]["sendId"] == "send-b"
    received = await _drain_once(state, slot, monkeypatch)
    assert [message for message, _ in received] == ["Answer me"]
    assert received[0][1]["_responsive_human"] is True
    assert received[0][1]["_responsive_protected"] is True
    assert sum(r.get("content") == "Answer me" for r in slot.messages) == 1
    assert slot._queue == []


@pytest.mark.asyncio
async def test_turn_ending_during_grace_never_cancels_and_requeues_once(env, clock, monkeypatch):
    state, slot, request, *_ = env
    _steerable(slot)
    await _send(request, slot, "Before you finish")
    turn = rc.turn_state(slot)
    grace = turn.grace
    # What `_end_turn_tail` does for its own owner, before the queue hand-off.
    turn.owner = None
    turn.cancel_grace()
    await asyncio.wait_for(asyncio.gather(grace, return_exceptions=True), timeout=30)
    assert grace.cancelled()
    chat_runner._requeue_unconsumed_steers(state, slot)
    clock.release.set()
    received = await _drain_once(state, slot, monkeypatch)
    state.sessions.cancel_owned_turn.assert_not_called()
    assert [message for message, _ in received] == ["Before you finish"]
    assert sum(r.get("content") == "Before you finish" for r in slot.messages) == 1


@pytest.mark.asyncio
async def test_burst_shares_one_grace_timer_and_one_cancel(env, clock):
    state, slot, request, *_ = env
    wires = _steerable(slot)
    first = await _send(request, slot, "one", "s1")
    second = await _send(request, slot, "two", "s2")
    assert first["responsive"] == second["responsive"] == "steered"
    assert len(wires) == 2
    await asyncio.wait_for(_until(lambda: clock.delays), timeout=30)
    assert clock.delays == [rc.STEER_GRACE_SECONDS]
    _consume(state, slot, wires[0])
    await _expire(clock, rc.turn_state(slot).grace)
    assert state.sessions.cancel_owned_turn.await_count == 1
    # After the turn's one cancel a further send queues, as it did before.
    third = await _send(request, slot, "three", "s3")
    assert third["responsive"] == "queued" and third["queued"] is True
    assert len(wires) == 2
    assert state.sessions.cancel_owned_turn.await_count == 1
    slot.task.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["owner", "stop", "session"])
async def test_changed_turn_during_grace_is_not_cancelled(env, clock, change):
    state, slot, request, *_ = env
    _steerable(slot)
    await _send(request, slot, "late")
    grace = rc.turn_state(slot).grace
    if change == "owner":
        rc.turn_state(slot).owner = asyncio.get_running_loop().create_future()
    elif change == "stop":
        slot._stop_generation += 1
    else:
        slot._active_turn_session_key = "dashboard:another-session"
    await _expire(clock, grace)
    state.sessions.cancel_owned_turn.assert_not_called()
    assert slot._pending_steers == ["late"]
    slot.task.cancel()


@pytest.mark.asyncio
async def test_closing_slot_drops_its_grace_timer(env, clock):
    state, slot, request, *_ = env
    _steerable(slot)
    await _send(request, slot, "closing")
    grace = rc.turn_state(slot).grace
    slot.begin_close()
    await asyncio.wait_for(asyncio.gather(grace, return_exceptions=True), timeout=30)
    assert grace.cancelled()
    state.sessions.cancel_owned_turn.assert_not_called()
    slot.task.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "condition, expected",
    [
        ("approval", "waiting_for_input"),
        ("question", "waiting_for_input"),
        ("native_permission", "waiting_for_input"),
        ("stop", "stopping"),
        ("compaction", "compacting"),
        ("no_steer_backend", "cancel_requested"),
        ("idle", "queued"),
    ],
)
async def test_non_steerable_states_keep_queue_behaviour(
    env, clock, monkeypatch, condition, expected
):
    state, slot, request, *_ = env
    wires = _steerable(slot)
    pending = asyncio.get_running_loop().create_future()
    if condition == "approval":
        slot._approval_futures["approval"] = pending
    elif condition == "question":
        slot._question_pending["question"] = {"blocking": True}
    elif condition == "native_permission":
        slot._acp_client.awaiting_permission = True
    elif condition == "stop":
        slot._stop_state = "soft_pending"
    elif condition == "compaction":
        state.sessions.is_compacting = lambda key: True
    elif condition == "no_steer_backend":
        slot._acp_client.supports_steer = False
    else:
        slot.task.cancel()
        slot.task = None
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._start_next_queued_turn", AsyncMock(return_value=True)
        )
    receipt = await _send(request, slot, "plain send")
    assert receipt["responsive"] == expected
    assert receipt["queued"] is True and "steered" not in receipt
    assert wires == []
    assert clock.delays == []
    assert [entry["content"] for entry in slot._queue] == ["plain send"]
    expected_cancels = 1 if condition == "no_steer_backend" else 0
    assert state.sessions.cancel_owned_turn.await_count == expected_cancels
    pending.cancel()
    if slot.task is not None:
        slot.task.cancel()


@pytest.mark.asyncio
async def test_a_burst_steers_into_the_protected_answer_and_never_cancels_it(env, clock):
    """The second message of a burst joins the reply being written: it is steered
    into the protected answering turn, and an unconsumed steer there waits for the
    turn's own end instead of cancelling the answer."""
    state, slot, request, *_ = env
    wires = _steerable(slot)
    rc.begin_turn(slot, protected=True)
    receipt = await _send(request, slot, "现在啥情况", "send-burst")
    assert receipt == {"ok": True, "steered": True, "responsive": "steered"}
    assert wires == [rc.RESPONSE_GUIDANCE + "现在啥情况"]
    assert slot._queue == []
    grace = rc.turn_state(slot).grace
    await _expire(clock, grace)
    state.sessions.cancel_owned_turn.assert_not_called()
    slot.task.cancel()


@pytest.mark.asyncio
async def test_a_cancel_claimed_during_grace_is_not_repeated(env, clock):
    state, slot, request, *_ = env
    _steerable(slot)
    await _send(request, slot, "steered first")
    grace = rc.turn_state(slot).grace
    # A later send the backend refuses takes today's queue path and its cancel.
    slot._acp_client.steer = AsyncMock(return_value=False)
    fallback = await _send(request, slot, "refused steer")
    assert fallback["responsive"] == "cancel_requested"
    await _expire(clock, grace)
    assert state.sessions.cancel_owned_turn.await_count == 1
    slot.task.cancel()


@pytest.mark.asyncio
async def test_a_failed_steer_falls_back_to_the_queue_and_immediate_cancel(env, clock):
    state, slot, request, *_ = env
    _steerable(slot)
    slot._acp_client.steer = AsyncMock(return_value=False)
    receipt = await _send(request, slot, "fallback")
    assert receipt["responsive"] == "cancel_requested"
    assert [entry["content"] for entry in slot._queue] == ["fallback"]
    assert slot._pending_steers == []
    assert clock.delays == []
    state.sessions.cancel_owned_turn.assert_awaited_once()
    slot.task.cancel()


@pytest.mark.asyncio
async def test_a_harness_without_steer_queues_and_cancels_at_once_with_no_grace(env, clock):
    """The grace waits on a steer the turn can consume. A client that does not
    advertise steer support has none, so the send queues and claims the cancel
    in the same request."""
    state, slot, request, *_ = env
    wires = _steerable(slot)
    slot._acp_client.supports_steer = False
    owner = rc.turn_state(slot).owner
    receipt = await _send(request, slot, "no steer here", "send-ns")
    assert receipt["responsive"] == "cancel_requested"
    assert wires == []
    assert rc.turn_state(slot).grace is None
    assert clock.delays == []
    assert [entry["content"] for entry in slot._queue] == ["no steer here"]
    state.sessions.cancel_owned_turn.assert_awaited_once_with(
        rc.effective_session_key(slot), owner, wait_ack_timeout=rc.CANCEL_ACK_SECONDS
    )
    slot.task.cancel()


def _real_runner(tmp_path, monkeypatch, stream_events):
    from test_chat_runner_coverage import _runner_state, _slot

    state, client = _runner_state(tmp_path)
    slot = _slot("member-steer-first")
    state._slots[slot.key] = slot
    wires: list[str] = []

    async def steer(message):
        wires.append(message)
        return True

    inner = SimpleNamespace(
        pop_pending_oauth_requests=lambda: [],
        awaiting_permission=False,
        last_prompt_stats=None,
        pending_steers=[],
        supports_steer=True,
        steer=steer,
        has_active_turn=lambda: True,
    )
    client.client = inner
    client.billing_stats = MagicMock(return_value=None)
    client.has_active_turn = MagicMock(return_value=True)
    client.is_alive = MagicMock(return_value=True)
    client.supports_steer = True
    state.sessions.is_compacting = lambda key: False
    prompts: list[str] = []

    async def stream(prompt):
        prompts.append(prompt)
        async for event in stream_events(len(prompts), wires):
            yield event

    client.stream = MagicMock(side_effect=stream)
    monkeypatch.setattr(chat_runner, "subagents_hold_user_messages", lambda *a: True)
    return state, slot, prompts, wires


async def _run_original(state, slot):
    slot.append("user", "Original task CEDAR-17")
    original = asyncio.create_task(
        chat_runner._run_chat(
            state,
            slot,
            "Original task CEDAR-17",
            _responsive_human=True,
            _directive_user_origin=True,
        )
    )
    slot.task = original
    return original


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ending", ["consumed", "consumed_then_end", "natural_end", "grace_expired"]
)
async def test_real_turn_steer_first_delivers_exactly_once(tmp_path, monkeypatch, clock, ending):
    from test_chat_runner_coverage import _complete

    from kiro_crew.acp.types import EVENT_STEER_CONSUMED
    from kiro_crew.providers.base import EVENT_TEXT_CHUNK, EVENT_TOOL_CALL, LLMEvent

    entered, steered, proceed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def events(n, wires):
        if n == 1:
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Working on the original task.")
            yield LLMEvent(kind=EVENT_TOOL_CALL, tool_call_id="call-1", title="Long operation")
            entered.set()
            await asyncio.wait_for(steered.wait(), timeout=30)
            if ending.startswith("consumed"):
                yield LLMEvent(kind=EVENT_STEER_CONSUMED, text=wires[0])
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=" Answering you first.")
            await asyncio.wait_for(proceed.wait(), timeout=30)
            yield _complete("cancelled" if ending == "grace_expired" else "end_turn")
        else:
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Here is the answer.")
            yield _complete()

    state, slot, prompts, wires = _real_runner(tmp_path, monkeypatch, events)

    async def cancel(key, owner, **kwargs):
        proceed.set()
        return "acked"

    state.sessions.cancel_owned_turn = AsyncMock(side_effect=cancel)
    request = SimpleNamespace(app={"state": state})
    original = await _run_original(state, slot)
    try:
        await asyncio.wait_for(entered.wait(), timeout=30)
        receipt = json.loads(
            (
                await asyncio.wait_for(
                    rc.accept(request, slot, "What is the status?", {"sendId": "followup"}), 30
                )
            ).text
        )
        assert receipt["responsive"] == "steered"
        grace = rc.turn_state(slot).grace
        steered.set()
        if ending == "consumed":
            await asyncio.wait_for(_until(lambda: not slot._pending_steers), timeout=30)
            await _expire(clock, grace)
            proceed.set()
        elif ending == "consumed_then_end":
            await asyncio.wait_for(_until(lambda: not slot._pending_steers), timeout=30)
            proceed.set()
        elif ending == "natural_end":
            proceed.set()
        else:
            await _expire(clock, grace)
        await asyncio.wait_for(original, timeout=30)
        successor = slot.task
        if successor is not None and successor is not original:
            await asyncio.wait_for(successor, timeout=30)
    finally:
        steered.set()
        proceed.set()
        clock.release.set()
        if not original.done():
            original.cancel()
        await asyncio.wait_for(asyncio.gather(original, return_exceptions=True), timeout=30)
    assert wires == [rc.RESPONSE_GUIDANCE + "What is the status?"]
    assert [r["content"] for r in slot.messages if r["role"] == "user"] == [
        "Original task CEDAR-17",
        "What is the status?",
    ]
    if ending.startswith("consumed"):
        assert len(prompts) == 1
        state.sessions.cancel_owned_turn.assert_not_called()
    else:
        assert len(prompts) == 2
        assert prompts[1].endswith("What is the status?")
        assert rc.RESPONSE_GUIDANCE in prompts[1]
        assert state.sessions.cancel_owned_turn.await_count == (ending == "grace_expired")
    if ending in ("natural_end", "consumed_then_end"):
        # The turn's end drops a timer still waiting, with or without a successor.
        assert grace.cancelled()
    assert slot._queue == [] and slot._pending_steers == []
    state.sessions.reset.assert_not_called()


async def _until(predicate):
    while not predicate():
        await asyncio.sleep(0)
