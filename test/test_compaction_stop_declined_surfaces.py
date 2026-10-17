"""Each Stop surface leaves its work in place when a compaction declines the Stop.

Slack's ``!stop``, the shared channel stop, the dashboard Stop and interrupt routes
and the spec-builder Pause keep the queue, the pending approvals and the staged
files a declined Stop must not touch, and a Stop that goes through keeps its
discard-and-cancel contract.
"""

from __future__ import annotations

import asyncio

import pytest
from compaction_stop_declined_helpers import _clear_channel_decline_markers  # noqa: F401
from compaction_stop_declined_helpers import _dashboard_state, _run_slack_stop, _setup, _slack_orch


def test_an_ordinary_second_press_escalation_still_discards_the_session_queue(
    tmp_path, monkeypatch
):
    """Control: a hard kill that is NOT the escape from a compaction decline keeps
    its "discard everything" contract, session queue included."""
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.dashboard.chat_handlers import stop_slot_turn

    state = _dashboard_state(tmp_path)
    slot = state.get_or_create_slot("chat-14841")
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    slot._stop_state = "soft_pending"  # a cooperative cancel is already pending
    state.sessions.is_compacting = MagicMock(return_value=False)
    state.sessions.stop_turn = AsyncMock(return_value="hard")
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.sel", lambda: MagicMock())

    asyncio.run(stop_slot_turn(state, slot))

    assert state.sessions.stop_turn.await_args.kwargs["force"] is True
    assert state.sessions.stop_turn.await_args.kwargs["preserve_queue"] is False


def test_a_stale_decline_does_not_turn_a_later_first_press_into_a_force_stop(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.dashboard.chat_handlers import stop_slot_turn
    from kiro_crew.dashboard.slot_projection import STOP_DECLINED_ESCALATION_SECS

    state = _dashboard_state(tmp_path)
    slot = state.get_or_create_slot("chat-14841")
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    state.sessions.is_compacting = MagicMock(return_value=False)
    state.sessions.stop_turn = AsyncMock(return_value="soft")
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.sel", lambda: MagicMock())
    import time as _time

    # flake-ok: pre-existing stamp backdated past the escalation window on the real monotonic clock, moved verbatim; tracked for a de-flake pass
    slot._stop_declined_at = _time.monotonic() - STOP_DECLINED_ESCALATION_SECS - 1
    assert slot.to_dict()["stop_declined"] is False

    asyncio.run(stop_slot_turn(state, slot))

    assert state.sessions.stop_turn.await_args.kwargs["force"] is False


def test_the_shared_channel_stop_keeps_the_queue_while_compacting():
    """Discord/Telegram/Teams stop through ``stop_running_turn``, which cancels the
    provider itself and clears the queue; a declined Stop must do neither."""
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.messaging.commands import STOP_REPLY_COMPACTING, stop_running_turn

    sessions = MagicMock()
    sessions.is_compacting = MagicMock(return_value=True)
    sessions.is_busy = MagicMock(return_value=True)
    sessions.get_provider = MagicMock(return_value=MagicMock(cancel=AsyncMock()))
    queue = MagicMock()
    queue.lock = asyncio.Lock()
    queue.finish_cancelled_locked = AsyncMock()

    reply = asyncio.run(
        stop_running_turn(
            sessions,
            "telegram:1",
            queue=queue,
            surface=MagicMock(label="telegram"),
            owner="u1",
            deliver=AsyncMock(),
        )
    )

    assert reply == STOP_REPLY_COMPACTING
    sessions.note_stop.assert_not_called()
    sessions.clear_queue.assert_not_called()
    sessions.get_provider.return_value.cancel.assert_not_awaited()
    queue.finish_cancelled_locked.assert_not_awaited()


def _callers(ts):
    """A queue entry the presser of ``_run_slack_stop`` queued, tagged as Slack tags it."""
    from kiro_crew.slack import events as ev

    return (ts, "text", {"paths": [], **ev._queue_tags("U_OWNER", "D1")})


def test_slack_stop_declined_by_the_precheck_touches_nothing():
    from unittest.mock import AsyncMock, MagicMock

    orch, task = _slack_orch()
    orch.sessions.is_compacting = MagicMock(return_value=True)
    orch.sessions.stop_turn = AsyncMock()
    unlink = _run_slack_stop(orch)
    orch.sessions.stop_turn.assert_not_awaited()
    orch.sessions.note_stop.assert_not_called()
    orch.sessions.clear_queue.assert_not_called()
    unlink.assert_not_called()
    assert orch._session_tasks == {"100.0": task}
    assert "100.0" in orch._pending_queue
    task.cancel.assert_not_called()


def test_slack_stop_declined_by_stop_turn_keeps_queue_pending_files_and_task():
    """The race the pre-check cannot close: a compaction commits during the
    ephemeral post, and ``stop_turn`` declines. Nothing queued may be lost."""
    from unittest.mock import AsyncMock, MagicMock

    orch, task = _slack_orch()
    orch.sessions.is_compacting = MagicMock(return_value=False)
    orch.sessions.stop_turn = AsyncMock(return_value="compacting")
    unlink = _run_slack_stop(orch)
    orch.sessions.stop_turn.assert_awaited_once()
    orch.sessions.clear_queue.assert_not_called()
    unlink.assert_not_called()
    assert orch._session_tasks == {"100.0": task}
    assert "100.0" in orch._pending_queue
    task.cancel.assert_not_called()


def test_slack_stop_leaves_a_successor_started_from_a_newer_message_alone():
    """A message admitted during the stop's awaits is newer intent. If it is
    already running as the tracked task, the stop neither pops nor cancels it."""
    from unittest.mock import AsyncMock, MagicMock

    orch, task = _slack_orch()
    orch.sessions.is_compacting = MagicMock(return_value=False)
    successor = MagicMock()
    successor.done.return_value = False

    async def _stop(*_a, **_k):
        orch._session_tasks["100.0"] = successor  # dispatched mid-await
        return "soft"

    orch.sessions.stop_turn = AsyncMock(side_effect=_stop)
    _run_slack_stop(orch)
    assert orch._session_tasks == {"100.0": successor}
    successor.cancel.assert_not_called()
    task.cancel.assert_called_once()


def test_slack_stop_detaches_at_the_press_and_drops_only_that():
    """What was queued at the press is taken out of the live queue BEFORE the first
    await (so the cancelled turn's drain cannot start it), then dropped once the
    stop went through. A message admitted mid-stop is newer intent and stays."""
    from unittest.mock import AsyncMock, MagicMock, patch

    orch, task = _slack_orch()
    orch.sessions.is_compacting = MagicMock(return_value=False)
    at_press = (_callers("q-at-press"),)
    orch.sessions.detach_queue = MagicMock(return_value=at_press)
    orch._pending_queue = {"100.0": [_callers("ts")]}
    late = ("ts-late", "sent during the stop", {"paths": []})

    async def _stop(*_a, **_k):
        # By now the press-time pending entry is detached; a later one arrives.
        assert "100.0" not in orch._pending_queue
        orch._pending_queue["100.0"] = [late]
        return "soft"

    orch.sessions.stop_turn = AsyncMock(side_effect=_stop)
    dispatched = AsyncMock()
    with patch("kiro_crew.slack.events._dispatch_queued", new=dispatched):
        unlink = _run_slack_stop(orch)
    orch.sessions.detach_queue.assert_called_once_with("100.0")
    orch.sessions.clear_queue.assert_called_once_with("100.0", only=at_press)
    orch.sessions.restore_queue.assert_not_called()
    # Kept, and the next thing dispatched once the stop is done.
    assert [call.args[2] for call in dispatched.await_args_list] == ["ts-late"]
    assert unlink.call_count == 1  # the press-time pending entry's files


def test_slack_stop_tells_stop_turn_to_keep_the_queue_it_did_not_detach():
    """What the Stop drops was detached at the press and is cleared by identity
    after the outcome. ``stop_turn``'s own whole-queue clear would take a message
    admitted between the detach and the cancel: newer intent, never this Stop's."""
    from unittest.mock import AsyncMock, MagicMock

    orch, task = _slack_orch()
    orch.sessions.is_compacting = MagicMock(return_value=False)
    at_press = (_callers("q-at-press"),)
    orch.sessions.detach_queue = MagicMock(return_value=at_press)
    orch.sessions.stop_turn = AsyncMock(return_value="soft")
    _run_slack_stop(orch)
    assert orch.sessions.stop_turn.await_args.kwargs["preserve_queue"] is True
    orch.sessions.clear_queue.assert_called_once_with("100.0", only=at_press)


def test_slack_stop_declined_in_the_race_restores_what_it_detached():
    from unittest.mock import AsyncMock, MagicMock

    orch, task = _slack_orch()
    orch.sessions.is_compacting = MagicMock(return_value=False)
    at_press = ("q-at-press",)
    orch.sessions.detach_queue = MagicMock(return_value=at_press)
    pending_at_press = list(orch._pending_queue["100.0"])
    late = ("ts-late", "sent during the stop", {"paths": []})

    async def _stop(*_a, **_k):
        orch._pending_queue["100.0"] = [late]
        return "compacting"

    orch.sessions.stop_turn = AsyncMock(side_effect=_stop)
    unlink = _run_slack_stop(orch)
    orch.sessions.restore_queue.assert_called_once_with("100.0", at_press)
    orch.sessions.clear_queue.assert_not_called()
    unlink.assert_not_called()
    # Press-time pending entries go back AHEAD of the later arrival.
    assert orch._pending_queue["100.0"] == pending_at_press + [late]
    assert orch._session_tasks == {"100.0": task}
    # The reply promised that a repeat forces, so the race decline armed the
    # marker for this presser, the same as the pre-check decline does.
    from kiro_crew.session_lifecycle import consume_stop_declined

    # flake-ok: pre-existing decline marker armed and read back to back inside the real-clock escalation window, moved verbatim; tracked for a de-flake pass
    assert consume_stop_declined("100.0", "U_OWNER") is True


def test_slack_stop_that_goes_through_still_clears_and_cancels():
    """The control: an ordinary soft stop keeps the destructive half."""
    from unittest.mock import AsyncMock, MagicMock

    orch, task = _slack_orch()
    orch.sessions.is_compacting = MagicMock(return_value=False)
    orch.sessions.stop_turn = AsyncMock(return_value="soft")
    unlink = _run_slack_stop(orch)
    orch.sessions.clear_queue.assert_called_once()
    unlink.assert_called_once()
    assert orch._session_tasks == {}
    assert "100.0" not in orch._pending_queue
    task.cancel.assert_called_once()


def _interrupt_state():
    """The interrupt route's state double, as ``test_chat_slot_interrupt`` builds it."""
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.dashboard.state import DashboardState, _ChatSlot

    slot = _ChatSlot("test")
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    slot.queue_append("msg")
    fut = asyncio.get_event_loop_policy().new_event_loop().create_future()
    slot._approval_futures["req-1"] = fut
    state = MagicMock(spec=DashboardState)
    state._slots = {"test": slot}
    state.push_slots_update = MagicMock()
    state.sessions = MagicMock()
    state.sessions.stop_turn = AsyncMock(return_value="soft")
    state.broadcast_ws = MagicMock()
    return state, slot, fut


async def _post_interrupt(state):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.chat import api_chat_slot_interrupt

    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/interrupt", api_chat_slot_interrupt)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/chat/slots/test/interrupt", json={})
        return resp.status, await resp.json()


@pytest.mark.asyncio
async def test_interrupt_declined_by_the_precheck_leaves_the_turn_untouched(monkeypatch):
    """Pending approvals stay pending, no cancel is sent."""
    from unittest.mock import MagicMock

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.sel", lambda: MagicMock())
    state, slot, fut = _interrupt_state()
    state.sessions.is_compacting = MagicMock(return_value=True)

    status, data = await _post_interrupt(state)

    assert status == 200 and data["outcome"] == "compacting"
    state.sessions.stop_turn.assert_not_awaited()
    assert not fut.done(), "a declined interrupt must not reject the turn's approvals"
    assert slot._stop_state == "idle"
    assert slot._stop_declined_at == 0.0, "an interrupt is not a Stop and arms no hard kill"
    cards = [m for m in slot.messages if '"kind": "stop_event"' in (m.get("cls") or "")]
    assert len(cards) == 1 and '"state": "stop_declined_compacting"' in cards[0]["cls"]


@pytest.mark.asyncio
async def test_interrupt_declined_after_the_body_read_keeps_the_pending_approval(monkeypatch):
    """The pre-check passes, then a compaction commits during the request-body
    await. The second probe, right before the pending waits would be rejected,
    declines: the approval stays pending and the claim is released."""
    from unittest.mock import MagicMock

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.sel", lambda: MagicMock())
    state, slot, fut = _interrupt_state()
    # First probe (pre-check) says no; every later probe says a compaction holds it.
    state.sessions.is_compacting = MagicMock(side_effect=[False, True, True, True])

    status, data = await _post_interrupt(state)

    assert status == 200 and data["outcome"] == "compacting"
    state.sessions.stop_turn.assert_not_awaited()
    assert not fut.done(), "the approval must not be rejected by a Stop that ended nothing"
    assert slot._stop_state == "idle"
    assert slot._stop_event_id is None
    cards = [m for m in slot.messages if '"kind": "stop_event"' in (m.get("cls") or "")]
    assert len(cards) == 1 and '"state": "stop_declined_compacting"' in cards[0]["cls"]


@pytest.mark.asyncio
async def test_interrupt_race_outcome_leaves_the_turn_and_settles_idle(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.sel", lambda: MagicMock())
    state, slot, _fut = _interrupt_state()
    state.sessions.is_compacting = MagicMock(return_value=False)
    state.sessions.stop_turn = AsyncMock(return_value="compacting")

    status, data = await _post_interrupt(state)

    assert status == 200 and data["outcome"] == "compacting"
    assert slot._stop_state == "idle"
    assert slot._stop_declined_at == 0.0
    assert slot._stop_event_id is None


class _SpecSlot:
    """The spec-builder worker slot as ``_halt_active_turn`` reads it."""

    key = "spec-builder-live"
    _app = "spec-builder"
    running = True

    def __init__(self):
        from unittest.mock import MagicMock

        self.task = MagicMock()
        self.task.done.return_value = False
        self._queue = [{"id": "q1", "content": "next prompt"}]
        self._pending_steers = ["steer-1"]
        self._pending_synthesis = True


def _spec_state(slot, *, compacting: bool, stop_outcome: str):
    from unittest.mock import AsyncMock, MagicMock

    state = MagicMock()
    state.get_slot = lambda key: slot if key == slot.key else None
    state.sessions.is_compacting = MagicMock(return_value=compacting)
    state.sessions.stop_turn = AsyncMock(return_value=stop_outcome)
    return state


@pytest.mark.asyncio
async def test_spec_builder_pause_while_compacting_skips_the_stop_but_still_halts_the_worker():
    """The cooperative stop would be declined, so it is not sent; the WORKER task
    (this app's own run loop, not the compaction turn) is still cancelled, so a
    Pause the route reports as halted has halted, and its queued work is dropped
    as on any Pause."""
    from kiro_crew.apps.builtins.spec_builder.backend import runtime

    slot = _SpecSlot()
    state = _spec_state(slot, compacting=True, stop_outcome="soft")
    assert await runtime._halt_active_turn(state, "live") is True
    state.sessions.stop_turn.assert_not_awaited()
    slot.task.cancel.assert_called_once()
    assert slot._queue == [] and slot._pending_steers == []
    assert slot._pending_synthesis is False


@pytest.mark.asyncio
async def test_spec_builder_pause_declined_in_the_race_still_halts_the_worker():
    """The race: the probe passed, the compaction committed, ``stop_turn`` declined.
    The worker task is ours to cancel regardless, so the Pause still halts it."""
    from kiro_crew.apps.builtins.spec_builder.backend import runtime

    slot = _SpecSlot()
    state = _spec_state(slot, compacting=False, stop_outcome="compacting")
    assert await runtime._halt_active_turn(state, "live") is True
    state.sessions.stop_turn.assert_awaited_once()
    slot.task.cancel.assert_called_once()
    assert slot._queue == [] and slot._pending_steers == []


@pytest.mark.asyncio
async def test_spec_builder_pause_that_goes_through_still_discards_and_cancels():
    from kiro_crew.apps.builtins.spec_builder.backend import runtime

    slot = _SpecSlot()
    state = _spec_state(slot, compacting=False, stop_outcome="soft")
    assert await runtime._halt_active_turn(state, "live") is True
    slot.task.cancel.assert_called_once()
    assert slot._queue == [] and slot._pending_steers == []
    assert slot._pending_synthesis is False


@pytest.mark.asyncio
async def test_detach_then_restore_puts_press_time_entries_ahead_of_newer_ones():
    mgr, key, compact, order, notices = await _setup()
    assert mgr.enqueue(key, "ts-1", "at press", force=True)
    taken = mgr.detach_queue(key)
    assert [e[0] for e in mgr._sessions[key].queue] == []
    assert mgr.enqueue(key, "ts-2", "admitted later", force=True)
    mgr.restore_queue(key, taken)
    assert [e[0] for e in mgr._sessions[key].queue] == ["ts-1", "ts-2"]
    await mgr.close_all()


@pytest.mark.asyncio
async def test_detach_then_clear_only_drops_the_detached_entries_and_keeps_newer():
    mgr, key, compact, order, notices = await _setup()
    assert mgr.enqueue(key, "ts-1", "at press", force=True)
    taken = mgr.detach_queue(key)
    assert mgr.enqueue(key, "ts-2", "admitted later", force=True)
    mgr.clear_queue(key, only=taken)
    assert [e[0] for e in mgr._sessions[key].queue] == ["ts-2"]
    await mgr.close_all()


def test_force_true_on_an_idle_stop_state_reaches_the_hard_stop_not_the_decline(
    tmp_path, monkeypatch
):
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.dashboard.chat_handlers import stop_slot_turn

    state = _dashboard_state(tmp_path)
    slot = state.get_or_create_slot("chat-14841")
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    state.sessions.is_compacting = MagicMock(return_value=True)
    state.sessions.stop_turn = AsyncMock(return_value="hard")
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.sel", lambda: MagicMock())

    asyncio.run(stop_slot_turn(state, slot, force=True))

    state.sessions.stop_turn.assert_awaited_once()
    assert state.sessions.stop_turn.await_args.kwargs["force"] is True
    # The escape from a compaction decline keeps the SESSION queue (a linked
    # channel's, on a linked slot); the slot's own queue is still discarded.
    assert state.sessions.stop_turn.await_args.kwargs["preserve_queue"] is True


def test_force_true_on_an_idle_state_with_no_compaction_stays_cooperative(tmp_path, monkeypatch):
    """A retried first press whose original was lost on the wire arrives as
    ``force=True`` on an idle state. Without a compaction there is nothing to
    escape from, so it keeps the cooperative path and the queue."""
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.dashboard.chat_handlers import stop_slot_turn

    state = _dashboard_state(tmp_path)
    slot = state.get_or_create_slot("chat-14841")
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    slot._queue.append({"id": "q1", "content": "keep me"})
    state.sessions.is_compacting = MagicMock(return_value=False)
    state.sessions.stop_turn = AsyncMock(return_value="soft")
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.sel", lambda: MagicMock())

    asyncio.run(stop_slot_turn(state, slot, force=True))

    state.sessions.stop_turn.assert_awaited_once()
    assert state.sessions.stop_turn.await_args.kwargs["force"] is False
    assert list(slot._queue) == [{"id": "q1", "content": "keep me"}]


def test_slack_stop_restores_detached_work_when_the_ephemeral_post_raises():
    """The detached work lives only in locals across the awaits; a Slack error
    must not leave the queue emptied with no stop performed."""
    from unittest.mock import AsyncMock, MagicMock

    orch, task = _slack_orch()
    orch.sessions.is_compacting = MagicMock(return_value=False)
    at_press = ("q-at-press",)
    orch.sessions.detach_queue = MagicMock(return_value=at_press)
    pending_at_press = list(orch._pending_queue["100.0"])
    orch.slack.post_ephemeral = AsyncMock(side_effect=RuntimeError("ratelimited"))
    orch.sessions.stop_turn = AsyncMock(return_value="soft")

    with pytest.raises(RuntimeError):
        _run_slack_stop(orch)

    orch.sessions.restore_queue.assert_called_once_with("100.0", at_press)
    assert orch._pending_queue["100.0"] == pending_at_press
    orch.sessions.clear_queue.assert_not_called()
    orch.sessions.stop_turn.assert_not_awaited()


def test_slack_stop_hands_the_detached_queue_on_when_the_raising_stop_already_popped_the_session():
    """A forced stop whose reset raised after the pop: no session to restore onto,
    so the detached entries go to the successor (or are parked) instead of being
    dropped -- they may be co-tenants' messages."""
    from unittest.mock import AsyncMock, MagicMock, patch

    orch, task = _slack_orch()
    orch.sessions.is_compacting = MagicMock(return_value=False)
    at_press = ("q-at-press",)
    orch.sessions.detach_queue = MagicMock(return_value=at_press)

    async def _stop(*_a, **_k):
        orch.sessions.has_session = MagicMock(return_value=False)  # the pop happened
        raise RuntimeError("shutdown raised after the pop")

    orch.sessions.stop_turn = AsyncMock(side_effect=_stop)
    with patch("kiro_crew.slack.events.hand_queue_to_successor", new=AsyncMock()) as hand:
        with pytest.raises(RuntimeError):
            _run_slack_stop(orch)
    orch.sessions.restore_queue.assert_not_called()
    orch.sessions.clear_queue.assert_not_called()
    hand.assert_awaited_once_with(orch.sessions, "100.0", at_press)


def test_slack_forced_repeat_carries_the_detached_queue_to_the_successor():
    """On the forced repeat (hard reset) everything detached at the press goes to
    the successor instead of being dropped, the presser's own included: the Stop
    was aimed at the compaction, not at the shared queue. The pending stash goes
    back too, and is the next thing the stop's drain dispatches."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from kiro_crew.session_lifecycle import note_stop_declined

    orch, task = _slack_orch()
    orch.sessions.is_compacting = MagicMock(return_value=True)
    at_press = ("q-at-press",)
    orch.sessions.detach_queue = MagicMock(return_value=at_press)
    pending_at_press = list(orch._pending_queue["100.0"])
    orch.sessions.stop_turn = AsyncMock(return_value="hard")
    # flake-ok: pre-existing decline marker armed and read back to back inside the real-clock escalation window, moved verbatim; tracked for a de-flake pass
    note_stop_declined("100.0", "U_OWNER")  # the first press was declined
    dispatched = AsyncMock()
    with patch("kiro_crew.slack.events.hand_queue_to_successor", new=AsyncMock()) as hand:
        with patch("kiro_crew.slack.events._dispatch_queued", new=dispatched):
            unlink = _run_slack_stop(orch)
    assert orch.sessions.stop_turn.await_args.kwargs["force"] is True
    hand.assert_awaited_once_with(orch.sessions, "100.0", at_press)
    orch.sessions.clear_queue.assert_not_called()
    unlink.assert_not_called()
    assert [call.args[2:] for call in dispatched.await_args_list] == [tuple(pending_at_press[0])]
