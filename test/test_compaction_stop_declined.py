"""A user Stop during an automatic compaction neither fails it nor restarts the session.

The scenario: a long-running dashboard session looks stalled, the user presses Stop
while an automatic ``/compact`` turn holds it. Without these guarantees the Stop
cancels that turn, the failure arm recycles the process, and the dashboard answers
"Compaction didn't succeed, so the session was restarted instead" for a Stop the
user pressed. Four things pin the behaviour:

1. ``stop_turn`` DECLINES a cooperative Stop while the key is compacting and does
   not record it as a Stop the turn saw; a force stop still goes through.
2. When a ``/compact`` turn IS ended by a Stop (a force stop, or the race the
   pre-check cannot close), the compaction settles as ``cancelled`` -- cooldown armed,
   provider NOT shut down, notice says so -- instead of recycling.
3. The compacting set is observable: an observer is told on enter and leave, and the
   dashboard slot payload carries ``compacting`` so the composer can show it.
4. The restart notices name the transcript excerpt the successor starts from.

Fakes only: a mock provider whose ``/compact`` blocks until released or raises, no
real harness.
"""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.config import KiroCrewConfig
from kiro_crew.session import SessionManager
from kiro_crew.session_compaction import (
    COMPACT_OUTCOME_CANCELLED,
    COMPACT_OUTCOME_RECYCLED,
)

KEY = "dashboard:chat-14841"


@pytest.fixture(autouse=True)
def _clear_channel_decline_markers():
    from kiro_crew import session_lifecycle as sl

    sl._stop_declined_markers.clear()
    sl._parked_queue.clear()
    yield
    sl._stop_declined_markers.clear()
    sl._parked_queue.clear()


class _Compact:
    """A ``/compact`` turn the test controls: it blocks until released or is failed."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.fail_with: BaseException | None = None
        self.started = asyncio.Event()

    async def stream(self, _command: str):
        self.started.set()
        await self.release.wait()
        if self.fail_with is not None:
            raise self.fail_with
        if False:  # pragma: no cover - makes this an async generator
            yield None


def _factory(compact: _Compact, order: list[str]):
    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        m = AsyncMock()
        m.cwd = ""
        m.disown_work_dir = MagicMock()
        m.memory_mode = "persistent"
        m.is_process_alive = lambda: True
        m.context_usage_pct = lambda: 90.0
        m.context_usage_unknown = lambda: False
        m.context_window_tokens = lambda: 0
        m.has_active_turn = lambda: True
        m.runtime_info = lambda: (None, None)
        m.stream_command = MagicMock(side_effect=compact.stream)
        m.wait_for_compaction = AsyncMock(return_value={"type": "failed"})

        # A cooperative cancel is what a soft Stop does to a live turn; here it
        # ends the /compact turn the way the harness would: the stream raises.
        async def _cancel(*, wait_ack_timeout: float = 0.0):
            compact.fail_with = RuntimeError("compaction reported no result")
            compact.release.set()
            return "acked"

        m.cancel = AsyncMock(side_effect=_cancel)
        m.shutdown = AsyncMock(side_effect=lambda: order.append("shutdown"))
        return m

    return factory


async def _setup():
    order: list[str] = []
    compact = _Compact()
    mgr = SessionManager(KiroCrewConfig(), provider_factory=_factory(compact, order))
    await mgr.get_or_create(KEY)
    key = mgr._fold_key(KEY)
    mgr.release(key)
    mgr._compaction._deps = dataclasses.replace(
        mgr._compaction._deps,
        compact_wait_timeout_secs=lambda: 5.0,
        compact_result_wait_secs=lambda _elapsed, _budget: 0.05,
        compact_failure_cooldown_secs=123.0,
    )
    notices: list[tuple[bool, str]] = []

    async def _cb(key, pct, *, success, outcome="compacted"):
        notices.append((success, outcome))

    mgr.set_compact_callback(_cb)
    return mgr, key, compact, order, notices


async def _settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


# -- 1. a cooperative Stop is declined while compacting --


@pytest.mark.asyncio
async def test_a_cooperative_stop_during_compaction_is_declined_and_not_recorded():
    mgr, key, compact, order, notices = await _setup()
    session = mgr._sessions[key]
    before = mgr.stop_generation(key)

    task = asyncio.ensure_future(mgr._compact_in_place(key, session, 90.0))
    await asyncio.wait_for(compact.started.wait(), timeout=2)
    # The trigger paths add the key; _compact_in_place alone does not. Mirror
    # the state a real threshold trigger leaves.
    mgr._compacting.add(key)
    try:
        assert mgr.is_compacting(KEY) is True
        outcome = await mgr.stop_turn(KEY, force=False)
        assert outcome == "compacting"
        # Not recorded: a declined Stop is not a Stop the turn saw, and recording
        # it would make the compaction read its own later failure as cancelled.
        assert mgr.stop_generation(key) == before
        session.provider.cancel.assert_not_called()
        assert not task.done()
    finally:
        mgr._compacting.discard(key)
        compact.release.set()
        await asyncio.wait_for(task, timeout=5)
    # The compaction ran to its own (failed) end and recycled -- the ordinary
    # failure arm, untouched by the declined Stop.
    assert order == ["shutdown"]
    assert notices == [(True, COMPACT_OUTCOME_RECYCLED)]
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_force_stop_during_compaction_is_never_declined():
    """The escape hatch stays open: ``force`` is the user's second press."""
    mgr, key, compact, order, notices = await _setup()
    session = mgr._sessions[key]
    task = asyncio.ensure_future(mgr._compact_in_place(key, session, 90.0))
    await asyncio.wait_for(compact.started.wait(), timeout=2)
    mgr._compacting.add(key)
    try:
        outcome = await mgr.stop_turn(KEY, force=True)
    finally:
        mgr._compacting.discard(key)
        compact.fail_with = RuntimeError("compaction reported no result")
        compact.release.set()
    assert outcome == "hard"
    result = await asyncio.wait_for(task, timeout=5)
    # The force stop reset the session; the compaction that was running on it
    # settles as CANCELLED rather than recycling a provider the reset already
    # replaced and telling the user compaction "didn't succeed".
    assert result == "cancelled"
    assert notices[-1] == (False, COMPACT_OUTCOME_CANCELLED)
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_force_stop_hands_the_permit_to_the_waiter_and_the_compaction_does_not_reclaim_it():
    """The hard Stop pops the session and releases the compaction's permit to wake
    a parked claimant. That claimant now OWNS the permit; the compaction's own
    cleanup must not release it a second time under the claimant's feet."""
    mgr, key, compact, order, notices = await _setup()
    session = mgr._sessions[key]
    task = asyncio.ensure_future(mgr._compact_in_place(key, session, 90.0))
    await asyncio.wait_for(compact.started.wait(), timeout=2)

    # A claimant parked on the held permit, as ``_reacquire_and_validate`` does.
    async def _claimant():
        await session.semaphore.acquire()
        await asyncio.sleep(0.05)  # holds it while the compaction's finally runs
        session.semaphore.release()  # must not raise
        return "released-cleanly"

    claimant = asyncio.ensure_future(_claimant())
    await _settle()
    assert not claimant.done()

    mgr._compacting.add(key)
    try:
        assert await mgr.stop_turn(KEY, force=True) == "hard"
    finally:
        mgr._compacting.discard(key)
        compact.fail_with = RuntimeError("compaction reported no result")
        compact.release.set()
    assert await asyncio.wait_for(task, timeout=5) == "cancelled"
    assert await asyncio.wait_for(claimant, timeout=5) == "released-cleanly"
    await mgr.close_all()


@pytest.mark.asyncio
async def test_stop_turn_is_unchanged_when_nothing_is_compacting():
    mgr, key, compact, order, notices = await _setup()
    assert mgr.is_compacting(KEY) is False
    # No compaction and a mock provider that acks: the ordinary soft path.
    assert await mgr.stop_turn(KEY, force=False) == "soft"
    assert mgr.stop_generation(key) == 1
    await mgr.close_all()


# -- 2. a Stop that ends the /compact turn settles as cancelled, not recycled --


@pytest.mark.asyncio
async def test_a_stop_that_ends_the_compact_turn_does_not_recycle():
    """The race the pre-check cannot close: the Stop lands on the /compact turn.

    Driven by noting the Stop directly, which is what every channel stop path
    that cancels the provider itself does, and then failing the turn the way the
    harness reports a cancelled prompt.
    """
    mgr, key, compact, order, notices = await _setup()
    session = mgr._sessions[key]
    task = asyncio.ensure_future(mgr._compact_in_place(key, session, 90.0))
    await asyncio.wait_for(compact.started.wait(), timeout=2)

    assert mgr.note_stop(KEY) is True
    compact.fail_with = RuntimeError("compaction reported no result")
    compact.release.set()

    assert await asyncio.wait_for(task, timeout=5) == "cancelled"
    # No recycle: the provider is still the session's provider and was not shut down.
    assert order == []
    assert mgr._sessions[key] is session
    assert notices == [(False, COMPACT_OUTCOME_CANCELLED)]
    # The cooldown is armed so the next threshold reading retries later rather
    # than immediately re-entering the compaction the user just stopped.
    assert key in mgr._compact_cooldown_until
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_stop_recorded_during_the_recycle_window_does_not_strand_a_claimant():
    """A Stop recorded (but no reset taken) during the cotenant wait ends the wait
    as cancelled. The permit is still this task's, so the finally must release
    it, or a claimant parked on the permit never wakes."""
    mgr, key, compact, order, notices = await _setup()
    session = mgr._sessions[key]
    # Hold the recycle open: cotenant wait spins while a sub-agent looks live.
    gate = asyncio.Event()

    class _Runs:
        child = SimpleNamespace(
            id="a1", parent_session_key=key, conversation_key="", _stop_origin=""
        )

        @property
        def running(self):
            return [] if gate.is_set() else [self.child]

        def has_live_shared_session(self, _k):
            return not gate.is_set()

        def snapshot_teardown_children(self, _p):
            return ()

        async def cancel(self, _i):
            gate.set()
            return True

        async def cancel_for_teardown(self, ids, *, parent_session_key, verb=""):
            return len(ids)

    mgr.set_child_teardown_handler(_Runs())
    mgr._compaction._deps = dataclasses.replace(
        mgr._compaction._deps, cotenant_wait_secs=30.0, cotenant_poll_secs=0.01
    )
    task = asyncio.ensure_future(mgr._compact_in_place(key, session, 90.0))
    await asyncio.wait_for(compact.started.wait(), timeout=2)
    compact.fail_with = RuntimeError("compaction reported no result")
    compact.release.set()
    await asyncio.sleep(0.05)  # inside _await_cotenants now

    woke = asyncio.Event()

    async def _claimant():
        await session.semaphore.acquire()
        woke.set()
        session.semaphore.release()

    claimant = asyncio.ensure_future(_claimant())
    await _settle()
    # A Stop recorded during the recycle window (what a force Stop does first).
    # The wait ends on it and the compaction settles cancelled rather than
    # recycling; the permit this task still owns is released, so the parked
    # claimant wakes.
    assert mgr.note_stop(KEY) is True
    assert await asyncio.wait_for(task, timeout=5) == "cancelled"
    assert not gate.is_set(), "the sub-agent was not cancelled"
    await asyncio.wait_for(woke.wait(), timeout=2)
    gate.set()
    await claimant
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_hard_stop_during_the_cotenant_wait_settles_cancelled_and_spares_the_subagents():
    """The failure arm waits for sub-agents for seconds before it pops the session.
    A force Stop landing inside that wait resets the session and hands the permit
    to a parked claimant; the compaction's cleanup must not release it again
    under that claimant, and its stale restart must neither cancel the live
    sub-agents nor recycle again."""
    mgr, key, compact, order, notices = await _setup()
    session = mgr._sessions[key]
    gate = asyncio.Event()

    class _Runs:
        child = SimpleNamespace(
            id="a1", parent_session_key=key, conversation_key="", _stop_origin=""
        )

        @property
        def running(self):
            return [] if gate.is_set() else [self.child]

        def has_live_shared_session(self, _k):
            return not gate.is_set()

        def snapshot_teardown_children(self, _p):
            return ()

        async def cancel(self, _i):
            gate.set()
            return True

        async def cancel_for_teardown(self, ids, *, parent_session_key, verb=""):
            return len(ids)

    mgr.set_child_teardown_handler(_Runs())
    mgr._compaction._deps = dataclasses.replace(
        mgr._compaction._deps, cotenant_wait_secs=30.0, cotenant_poll_secs=0.01
    )
    task = asyncio.ensure_future(mgr._compact_in_place(key, session, 90.0))
    await asyncio.wait_for(compact.started.wait(), timeout=2)
    compact.fail_with = RuntimeError("compaction reported no result")
    compact.release.set()
    await asyncio.sleep(0.05)  # inside _await_cotenants now

    async def _claimant():
        await session.semaphore.acquire()
        await asyncio.sleep(0.05)  # holds it while the compaction's finally runs
        session.semaphore.release()  # must not raise
        return "released-cleanly"

    claimant = asyncio.ensure_future(_claimant())
    await _settle()
    assert not claimant.done()

    # The real thing: a force Stop resets the session mid-wait.
    assert await mgr.stop_turn(KEY, force=True) == "hard"
    assert mgr._sessions.get(key) is not session
    # The Stop already reset the parent and respawned its successor; this
    # compaction's restart is stale, so it settles as cancelled WITHOUT waiting
    # out the cotenants or cancelling them: the sub-agent stays live.
    assert await asyncio.wait_for(task, timeout=5) == "cancelled"
    assert not gate.is_set(), "the live sub-agent was not cancelled"
    assert notices[-1] == (False, COMPACT_OUTCOME_CANCELLED)
    assert await asyncio.wait_for(claimant, timeout=5) == "released-cleanly"
    gate.set()
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_stop_before_the_semaphore_is_held_is_not_this_compactions_cancel():
    """A Stop that ended the PREVIOUS turn must not be read as cancelling this compaction."""
    mgr, key, compact, order, notices = await _setup()
    session = mgr._sessions[key]
    # The Stop happened earlier, on some other turn.
    assert mgr.note_stop(KEY) is True
    task = asyncio.ensure_future(mgr._compact_in_place(key, session, 90.0))
    await asyncio.wait_for(compact.started.wait(), timeout=2)
    compact.fail_with = RuntimeError("compaction reported no result")
    compact.release.set()
    # Genuine failure: the ordinary recycle arm.
    assert await asyncio.wait_for(task, timeout=5) == "recycled"
    assert order == ["shutdown"]
    assert notices == [(True, COMPACT_OUTCOME_RECYCLED)]
    await mgr.close_all()


# -- 3. the compacting set is observable --


@pytest.mark.asyncio
async def test_channel_decline_markers_die_with_the_compaction_that_armed_them():
    """Compaction A declines a channel Stop and ends; compaction B starts inside
    the window. B's first press must be a first press, not A's forcing second."""
    from kiro_crew import session_lifecycle as sl

    mgr, key, compact, order, notices = await _setup()
    provider = mgr._sessions[key].provider
    assert mgr._compaction._trigger_compaction(key, "test", 90.0, provider) is None
    await asyncio.wait_for(compact.started.wait(), timeout=2)
    # Declined during A, under the live key and under an alias a channel may
    # press with, which the probe folds onto the live key.
    sl.note_stop_declined(key, "bob")
    sl.note_stop_declined("alias-of-" + key, "alice")
    sl.note_stop_declined("other-session", "alice")
    real_fold = mgr._fold_key
    mgr._fold_key = lambda k: key if k == "alias-of-" + key else real_fold(k)
    compact.fail_with = RuntimeError("compaction reported no result")
    compact.release.set()
    for _ in range(200):
        if not mgr.is_compacting(KEY):
            break
        await asyncio.sleep(0.01)
    mgr._fold_key = real_fold
    assert sl.consume_stop_declined("alias-of-" + key, "alice") is False, "alias died with A"
    assert sl.consume_stop_declined(key, "bob") is False
    assert sl.consume_stop_declined("other-session", "alice") is True, "other keys kept"
    await mgr.close_all()


def test_clear_stop_declined_drops_only_that_keys_markers():
    from kiro_crew import session_lifecycle as sl

    sl.note_stop_declined("k", "a", now=100.0)
    sl.note_stop_declined("k", "b", now=100.0)
    sl.note_stop_declined("j", "a", now=100.0)
    assert sl.clear_stop_declined("k") == 2
    assert sl.clear_stop_declined("k") == 0
    assert list(sl._stop_declined_markers) == [("j", "a")]
    sl.note_stop_declined("alias", "a", now=100.0)
    assert sl.clear_stop_declined("j", fold=lambda k: "j" if k == "alias" else k) == 2
    assert sl._stop_declined_markers == {}


def test_the_dashboard_decline_marker_dies_when_the_compaction_leaves(tmp_path):
    """No press lands between A ending and B starting, so nothing consumes the
    stale marker on a press; the compacting observer drops it at A's end."""
    state = _dashboard_state(tmp_path)
    slot = state.get_or_create_slot("chat-14841")
    import time as _time

    slot._compacting = True
    slot._stop_declined_at = _time.monotonic()
    state.wire_session_compact_callback()
    cb = state.sessions.set_compacting_callback.call_args.args[0]
    cb("dashboard:chat-14841", False)
    assert slot._compacting is False
    assert slot._stop_declined_at == 0.0
    assert slot.to_dict()["stop_declined"] is False


@pytest.mark.asyncio
async def test_the_compacting_observer_sees_enter_and_leave_once_each():
    mgr, key, compact, order, notices = await _setup()
    seen: list[tuple[str, bool]] = []
    mgr.set_compacting_callback(lambda k, on: seen.append((k, on)))
    # A failing observer must not fail the compaction.
    session = mgr._sessions[key]
    provider = session.provider
    decline = mgr._compaction._trigger_compaction(key, "test", 90.0, provider)
    assert decline is None, decline
    await asyncio.wait_for(compact.started.wait(), timeout=2)
    assert seen == [(key, True)]
    assert mgr.is_compacting(KEY) is True
    compact.fail_with = RuntimeError("compaction reported no result")
    compact.release.set()
    for _ in range(200):
        if not mgr.is_compacting(KEY):
            break
        await asyncio.sleep(0.01)
    assert seen == [(key, True), (key, False)]
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_raising_observer_does_not_fail_the_compaction():
    mgr, key, compact, order, notices = await _setup()

    def _boom(_k, _on):
        raise RuntimeError("observer broke")

    mgr.set_compacting_callback(_boom)
    session = mgr._sessions[key]
    decline = mgr._compaction._trigger_compaction(key, "test", 90.0, session.provider)
    assert decline is None
    await asyncio.wait_for(compact.started.wait(), timeout=2)
    assert mgr.is_compacting(KEY) is True
    compact.release.set()  # completes with no status -> failed -> recycled
    compact.fail_with = RuntimeError("compaction reported no result")
    for _ in range(200):
        if not mgr.is_compacting(KEY):
            break
        await asyncio.sleep(0.01)
    assert mgr.is_compacting(KEY) is False
    assert notices == [(True, COMPACT_OUTCOME_RECYCLED)]
    await mgr.close_all()


_TEST_DIR = str(Path(__file__).resolve().parent)


def _dashboard_state(tmp_path):
    import sys

    if _TEST_DIR not in sys.path:
        sys.path.insert(0, _TEST_DIR)
    from chat_test_helpers import _make_state

    return _make_state(tmp_path)


def test_the_slot_payload_carries_compacting_beside_running(tmp_path):
    """The composer reads ``compacting`` off the slot, separate from ``running``."""
    state = _dashboard_state(tmp_path)
    slot = state.get_or_create_slot("chat-14841")
    assert slot.to_dict()["compacting"] is False
    slot._compacting = True
    payload = slot.to_dict()
    assert payload["compacting"] is True
    assert payload["running"] is False, "a compaction is not a dashboard turn"


def test_the_dashboard_stop_is_declined_while_the_session_compacts(tmp_path, monkeypatch):
    """The route-level pre-check: no cancel is sent and the press still leaves a card."""
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.dashboard.chat_handlers import stop_slot_turn

    state = _dashboard_state(tmp_path)
    slot = state.get_or_create_slot("chat-14841")
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    state.sessions.stop_turn = AsyncMock(return_value="soft")
    state.sessions.is_compacting = MagicMock(return_value=True)
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.sel", lambda: MagicMock())

    reply = asyncio.run(stop_slot_turn(state, slot))

    assert reply == {"ok": True, "info": "compacting", "compacting": True}
    state.sessions.stop_turn.assert_not_awaited()
    # ``_stop_state`` stays idle -- the queue drain reads that machine as "a stop
    # is in progress" and would persist a false "Session reset" row -- while
    # the separate decline marker arms the next press as the force stop.
    assert slot._stop_state == "idle"
    assert slot._stop_declined_at > 0.0
    assert slot.to_dict()["stop_declined"] is True
    cards = [m for m in slot.messages if '"kind": "stop_event"' in (m.get("cls") or "")]
    assert len(cards) == 1
    assert '"state": "stop_declined_compacting"' in cards[0]["cls"]


def test_a_mock_shaped_manager_does_not_decline_every_stop(tmp_path, monkeypatch):
    """The probe is ``is True``: a truthy Mock answer must read as not compacting."""
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.dashboard.chat_handlers import stop_slot_turn

    state = _dashboard_state(tmp_path)
    slot = state.get_or_create_slot("chat-14841")
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    state.sessions.stop_turn = AsyncMock(return_value="soft")
    assert callable(state.sessions.is_compacting)  # a bare MagicMock attribute
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.sel", lambda: MagicMock())

    asyncio.run(stop_slot_turn(state, slot))

    state.sessions.stop_turn.assert_awaited_once()


def test_the_race_outcome_settles_the_card_and_undoes_the_soft_stop(tmp_path, monkeypatch):
    """``stop_turn`` answering ``compacting`` after the pre-check passed."""
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.dashboard.chat_handlers import stop_slot_turn

    state = _dashboard_state(tmp_path)
    slot = state.get_or_create_slot("chat-14841")
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    state.sessions.is_compacting = MagicMock(return_value=False)
    state.sessions.stop_turn = AsyncMock(return_value="compacting")
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.sel", lambda: MagicMock())

    reply = asyncio.run(stop_slot_turn(state, slot))

    assert reply["compacting"] is True
    assert slot._stop_state == "idle"
    assert slot._stop_declined_at > 0.0
    assert slot._stop_event_id is None
    cards = [m for m in slot.messages if '"kind": "stop_event"' in (m.get("cls") or "")]
    assert len(cards) == 1
    assert '"state": "stop_declined_compacting"' in cards[0]["cls"]


def test_a_second_press_during_compaction_escalates_to_the_force_stop(tmp_path, monkeypatch):
    """The escape hatch: the decline arms ``soft_pending``, so press #2 hard-stops."""
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

    asyncio.run(stop_slot_turn(state, slot))
    state.sessions.stop_turn.assert_not_awaited()
    assert slot._stop_state == "idle", "a declined Stop is not a stop in progress"
    asyncio.run(stop_slot_turn(state, slot))

    state.sessions.stop_turn.assert_awaited_once()
    assert state.sessions.stop_turn.await_args.kwargs["force"] is True
    # The escape from a compaction decline keeps the SESSION queue (a linked
    # channel's, on a linked slot); the slot's own queue is still discarded.
    assert state.sessions.stop_turn.await_args.kwargs["preserve_queue"] is True
    assert slot._stop_declined_at == 0.0, "the marker is consumed by the press it armed"


def test_the_second_press_after_a_decline_gets_its_own_stop_card(tmp_path, monkeypatch):
    """The decline settled its card; the hard kill that follows needs a row of its
    own, or the last stop row reads "nothing was stopped" for a reset session."""
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

    asyncio.run(stop_slot_turn(state, slot))
    asyncio.run(stop_slot_turn(state, slot))

    cards = [m for m in slot.messages if '"kind": "stop_event"' in (m.get("cls") or "")]
    assert len(cards) == 2, "one settled decline row, one row for the hard kill"
    assert slot._stop_event_id is not None
    assert slot._stop_escalated_card_id == slot._stop_event_id


def test_a_caller_that_withholds_escalation_is_not_hard_killed_by_a_decline_marker(
    tmp_path, monkeypatch
):
    """``escalate=False`` says "this call may be a retry"; the marker must not
    turn it into a hard kill that clears the queue and pending steers."""
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.dashboard.chat_handlers import stop_slot_turn

    state = _dashboard_state(tmp_path)
    slot = state.get_or_create_slot("chat-14841")
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    slot._queue.append({"id": "q1", "content": "keep me"})
    # Compaction still in flight on the second call: the marker WOULD escalate a
    # real second press, and must not escalate this one.
    state.sessions.is_compacting = MagicMock(return_value=True)
    state.sessions.stop_turn = AsyncMock(return_value="soft")
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.sel", lambda: MagicMock())

    asyncio.run(stop_slot_turn(state, slot))  # declined, marker armed
    asyncio.run(stop_slot_turn(state, slot, escalate=False))

    # Declined again (compacting), cooperatively: no cancel, queue intact.
    state.sessions.stop_turn.assert_not_awaited()
    assert list(slot._queue) == [{"id": "q1", "content": "keep me"}]
    assert slot._stop_state == "idle"


def test_a_decline_marker_is_dropped_once_the_compaction_has_ended(tmp_path, monkeypatch):
    """The marker remembers a refusal; it must not escalate a press made after the
    refusal stopped applying."""
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.dashboard.chat_handlers import stop_slot_turn

    state = _dashboard_state(tmp_path)
    slot = state.get_or_create_slot("chat-14841")
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    slot._queue.append({"id": "q1", "content": "keep me"})
    # Declined once, then the compaction finished before the second press.
    state.sessions.is_compacting = MagicMock(side_effect=[True, False, False])
    state.sessions.stop_turn = AsyncMock(return_value="soft")
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.sel", lambda: MagicMock())

    asyncio.run(stop_slot_turn(state, slot))
    assert slot._stop_declined_at > 0.0
    asyncio.run(stop_slot_turn(state, slot))

    state.sessions.stop_turn.assert_awaited_once()
    assert state.sessions.stop_turn.await_args.kwargs["force"] is False
    assert list(slot._queue) == [{"id": "q1", "content": "keep me"}]
    assert slot._stop_declined_at == 0.0


def test_a_non_escalating_decline_does_not_arm_the_marker(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.dashboard.chat_handlers import stop_slot_turn

    state = _dashboard_state(tmp_path)
    slot = state.get_or_create_slot("chat-14841")
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    state.sessions.is_compacting = MagicMock(return_value=True)
    state.sessions.stop_turn = AsyncMock()
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.sel", lambda: MagicMock())

    asyncio.run(stop_slot_turn(state, slot, escalate=False))

    assert slot._stop_declined_at == 0.0
    assert slot.to_dict()["stop_declined"] is False


def test_a_non_escalating_press_does_not_spend_the_users_armed_hatch(tmp_path, monkeypatch):
    """A retry or a containment stop (``escalate=False``) arriving between the
    decline and the user's second press must leave the marker for that press."""
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

    asyncio.run(stop_slot_turn(state, slot))  # declined, armed
    armed_at = slot._stop_declined_at
    asyncio.run(stop_slot_turn(state, slot, escalate=False))  # a retry
    assert slot._stop_declined_at == armed_at, "the retry did not spend the hatch"
    asyncio.run(stop_slot_turn(state, slot))  # the user's real second press

    state.sessions.stop_turn.assert_awaited_once()
    assert state.sessions.stop_turn.await_args.kwargs["force"] is True
    # The escape from a compaction decline keeps the SESSION queue (a linked
    # channel's, on a linked slot); the slot's own queue is still discarded.
    assert state.sessions.stop_turn.await_args.kwargs["preserve_queue"] is True


def test_the_shared_channel_stop_forces_on_a_repeat_within_the_window():
    """Channels have no force button: a second /stop within the window while the
    compaction still holds the session is the escape hatch."""
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.messaging.commands import STOP_REPLY_COMPACTING, stop_running_turn

    sessions = MagicMock()
    sessions.is_compacting = MagicMock(return_value=True)
    sessions.is_busy = MagicMock(return_value=True)
    sessions.stop_turn = AsyncMock(return_value="hard")
    queue = MagicMock()
    queue.lock = asyncio.Lock()
    queue.finish_cancelled_locked = AsyncMock()
    surface = MagicMock(label="telegram")

    first = asyncio.run(
        stop_running_turn(
            sessions,
            "telegram:1",
            queue=queue,
            surface=surface,
            owner="u1",
            deliver=AsyncMock(),
        )
    )
    assert first == STOP_REPLY_COMPACTING
    sessions.stop_turn.assert_not_awaited()

    second = asyncio.run(
        stop_running_turn(
            sessions,
            "telegram:1",
            queue=queue,
            surface=surface,
            owner="u1",
            deliver=AsyncMock(),
        )
    )
    sessions.stop_turn.assert_awaited_once()
    assert sessions.stop_turn.await_args.kwargs["force"] is True
    assert second != STOP_REPLY_COMPACTING
    sessions.note_stop.assert_called_once()


def test_a_stale_channel_decline_is_dropped_not_forced():
    from kiro_crew.session_lifecycle import consume_stop_declined, note_stop_declined

    note_stop_declined("k", "u1", now=100.0)
    assert consume_stop_declined("k", "u1", now=200.0) is False
    assert consume_stop_declined("k", "u1", now=200.0) is False, "consumed either way"
    note_stop_declined("k", "u1", now=100.0)
    assert consume_stop_declined("k", "u1", now=130.0) is True


def test_one_pressers_decline_does_not_arm_another_pressers_first_stop():
    """A group route and a unified DM scope share one session key between people.
    The marker is theirs, not the session's: B's first press stays a first press."""
    from kiro_crew.session_lifecycle import consume_stop_declined, note_stop_declined

    note_stop_declined("unified:agent", "alice", now=100.0)
    assert consume_stop_declined("unified:agent", "bob", now=110.0) is False
    assert consume_stop_declined("unified:agent", "alice", now=120.0) is True, "still hers"
    assert consume_stop_declined("unified:agent", "alice", now=121.0) is False, "spent"


def test_the_shared_channel_stop_is_not_forced_by_another_persons_decline():
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.messaging.commands import STOP_REPLY_COMPACTING, stop_running_turn

    sessions = MagicMock()
    sessions.is_compacting = MagicMock(return_value=True)
    sessions.stop_turn = AsyncMock(return_value="hard")
    queue = MagicMock()
    queue.lock = asyncio.Lock()
    queue.finish_cancelled_locked = AsyncMock()
    surface = MagicMock(label="telegram")

    kw = dict(queue=queue, surface=surface, deliver=AsyncMock())
    assert asyncio.run(stop_running_turn(sessions, "unified:a", owner="alice", **kw)) == (
        STOP_REPLY_COMPACTING
    )
    assert asyncio.run(stop_running_turn(sessions, "unified:a", owner="bob", **kw)) == (
        STOP_REPLY_COMPACTING
    ), "bob's first press is a first press"
    sessions.stop_turn.assert_not_awaited()
    sessions.note_stop.assert_not_called()
    asyncio.run(stop_running_turn(sessions, "unified:a", owner="alice", **kw))
    sessions.stop_turn.assert_awaited_once()


def test_the_shared_channel_force_stop_goes_through_the_queue_keeping_helper():
    """The force reset is the caller's; the queue is everyone's. The shared stop
    detaches the queue before the hard stop and hands the other people's entries
    to the successor (``force_stop_keeping_others``); the owner-scoped clear after
    it is the ordinary one."""
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.messaging.commands import stop_running_turn
    from kiro_crew.messaging.queue_drain import QUEUED_OWNER_KEY

    sessions = MagicMock()
    sessions.is_compacting = MagicMock(return_value=True)
    sessions.stop_turn = AsyncMock(return_value="hard")
    bob = ("ts-b", "bob's", {QUEUED_OWNER_KEY: "bob"})
    alice = ("ts-a", "alice's", {QUEUED_OWNER_KEY: "alice"})
    sessions.detach_queue = MagicMock(return_value=(bob, alice))
    # What was queued at the press, read before the goal pause's awaited write.
    sessions.peek_queue = MagicMock(return_value=(bob, alice))
    sessions.get_or_create = AsyncMock(return_value=(MagicMock(), False, False))
    queue = MagicMock()
    queue.lock = asyncio.Lock()
    queue.finish_cancelled_locked = AsyncMock()
    surface = MagicMock(label="telegram")
    kw = dict(queue=queue, surface=surface, owner="alice", deliver=AsyncMock())

    asyncio.run(stop_running_turn(sessions, "unified:a", **kw))
    asyncio.run(stop_running_turn(sessions, "unified:a", **kw))
    sessions.stop_turn.assert_awaited_once_with("unified:a", force=True, preserve_queue=True)
    sessions.detach_queue.assert_called_once_with("unified:a")
    sessions.restore_queue.assert_called_once_with("unified:a", (bob,))
    sessions.release.assert_called_once_with("unified:a")
    only_calls = [c for c in sessions.clear_queue.call_args_list if "only" in c.kwargs]
    assert [c.kwargs["only"] for c in only_calls] == [(alice,)], "only alice's handles dropped"
    # No second, owner-scoped clear after the force: it would take a message
    # alice sent during the stop's awaits, which the Stop was never aimed at.
    assert all("only" in c.kwargs for c in sessions.clear_queue.call_args_list)


@pytest.mark.asyncio
async def test_force_stop_keeping_others_carries_other_peoples_entries_to_the_successor(tmp_path):
    """Against the real manager: the hard stop pops the session and its queue;
    bob's entry (and its staged file) must reach the successor, alice's must be
    dropped and its file unlinked."""
    from kiro_crew.messaging.queue_drain import QUEUED_OWNER_KEY, entries_queued_by
    from kiro_crew.session_lifecycle import force_stop_keeping_others

    mgr, key, compact, order, notices = await _setup()
    tmp_a = tmp_path / "a.png"
    tmp_b = tmp_path / "b.png"
    tmp_a.write_bytes(b"a")
    tmp_b.write_bytes(b"b")
    assert mgr.enqueue(
        key,
        "ts-a",
        "alice's",
        force=True,
        **{QUEUED_OWNER_KEY: "alice"},
        image_temp_paths=[str(tmp_a)],
    )
    assert mgr.enqueue(
        key, "ts-b", "bob's", force=True, **{QUEUED_OWNER_KEY: "bob"}, image_temp_paths=[str(tmp_b)]
    )
    old = mgr._sessions[key]

    ended = await force_stop_keeping_others(mgr, KEY, entries_queued_by("alice"))

    assert ended is True
    new = mgr._sessions.get(key)
    assert new is not None and new is not old, "a successor holds the key"
    assert [e[0] for e in new.queue] == ["ts-b"], "bob's entry carried over"
    assert not tmp_a.exists(), "alice's staged file unlinked"
    assert tmp_b.exists(), "bob's staged file kept"
    assert not new.semaphore.locked(), "the successor's lease was released"
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_failed_successor_start_parks_other_peoples_entries_for_the_retry(tmp_path):
    """The successor's provider does not start: bob's entry and its file are kept,
    parked, and delivered by the next start that succeeds (the hard stop's own
    respawn path), not deleted."""
    from kiro_crew import session_lifecycle as sl
    from kiro_crew.messaging.queue_drain import QUEUED_OWNER_KEY

    mgr, key, compact, order, notices = await _setup()
    tmp_b = tmp_path / "b.png"
    tmp_b.write_bytes(b"b")
    bob = ("ts-b", "bob's", {QUEUED_OWNER_KEY: "bob", "image_temp_paths": [str(tmp_b)]})
    real = mgr.get_or_create
    calls = {"n": 0}

    async def flaky(k, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("provider did not start")
        return await real(k, **kw)

    mgr.get_or_create = flaky  # type: ignore[method-assign]
    assert await sl.hand_queue_to_successor(mgr, KEY, (bob,)) is False
    assert tmp_b.exists(), "nothing unlinked on a failed start"
    assert [e[0] for e in sl._parked_queue[mgr._fold_key(KEY)]] == ["ts-b"]

    # The respawn path retries and drains the parked entries.
    await mgr._lifecycle_boundary()._eager_respawn(KEY)
    assert sl._parked_queue.get(mgr._fold_key(KEY)) in (None, [])
    assert [e[0] for e in mgr._sessions[key].queue] == ["ts-b"]
    assert tmp_b.exists()
    await mgr.close_all()


def test_the_parked_queue_is_count_bounded_and_the_overflow_is_logged(caplog):
    from unittest.mock import MagicMock

    from kiro_crew import session_lifecycle as sl

    sessions = MagicMock()
    entries = tuple((f"ts-{i}", "x", {}) for i in range(sl.PARKED_QUEUE_MAX + 2))
    with caplog.at_level("WARNING", logger=sl.__name__):
        sl._park_queue(sessions, "k", entries)
    assert len(sl._parked_queue["k"]) == sl.PARKED_QUEUE_MAX
    assert sl._parked_queue["k"][0][0] == "ts-2", "the two oldest were dropped"
    assert sessions.clear_queue.call_count == 2, "their files were unlinked"
    assert any("per-key bound" in r.message for r in caplog.records)
    assert "2 dropped so far" in caplog.records[-1].message


def test_the_parked_queue_is_bounded_across_keys_and_refuses_an_oversized_key(caplog):
    from unittest.mock import MagicMock

    from kiro_crew import session_lifecycle as sl

    sessions = MagicMock()
    per_key = sl.PARKED_QUEUE_MAX
    keys = sl.PARKED_QUEUE_TOTAL_MAX // per_key + 1  # one key past the total
    with caplog.at_level("WARNING", logger=sl.__name__):
        for i in range(keys):
            sl._park_queue(
                sessions, f"k{i}", tuple((f"ts-{i}-{j}", "x", {}) for j in range(per_key))
            )
    assert sum(len(v) for v in sl._parked_queue.values()) == sl.PARKED_QUEUE_TOTAL_MAX
    assert "k0" not in sl._parked_queue, "the oldest key's entries were evicted first"
    assert sessions.clear_queue.call_count == per_key
    assert any("total bound" in r.message for r in caplog.records)
    sessions.clear_queue.reset_mock()
    sl._park_queue(sessions, "k" * (sl.STOP_DECLINED_KEY_MAX_CHARS + 1), (("t", "x", {}),))
    assert sessions.clear_queue.call_count == 1, "refused entries have their files unlinked"
    assert any("string bound" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_a_message_admitted_after_the_detach_is_carried_to_the_successor_too(tmp_path):
    """Between the caller's detach and the hard reset's pop, a message can still
    land on the old session. ``stop_turn(preserve_queue=True)`` parks it right
    before the pop, so the successor gets it instead of the reset unlinking it."""
    from kiro_crew.messaging.queue_drain import QUEUED_OWNER_KEY, entries_queued_by
    from kiro_crew.session_lifecycle import force_stop_keeping_others

    mgr, key, compact, order, notices = await _setup()
    tmp_late = tmp_path / "late.png"
    tmp_late.write_bytes(b"l")
    real_abort = mgr._lifecycle_boundary()._send_abort_for_session

    async def _abort_then_admit(k, session):
        # The window: the caller detached already, the reset has not popped yet.
        assert mgr.enqueue(
            key,
            "ts-late",
            "carol's, sent during the stop",
            force=True,
            **{QUEUED_OWNER_KEY: "carol"},
            image_temp_paths=[str(tmp_late)],
        )
        await real_abort(k, session)

    mgr._lifecycle_boundary()._send_abort_for_session = _abort_then_admit  # type: ignore[method-assign]
    old = mgr._sessions[key]
    assert await force_stop_keeping_others(mgr, KEY, entries_queued_by("alice")) is True
    new = mgr._sessions.get(key)
    assert new is not None and new is not old
    assert [e[0] for e in new.queue] == ["ts-late"], "the late admission reached the successor"
    assert tmp_late.exists()
    await mgr.close_all()


@pytest.mark.asyncio
async def test_parked_entries_are_adopted_by_the_next_ordinary_allocation(tmp_path):
    """Both respawn attempts fail; the parked entry must not strand. The next
    ordinary ``get_or_create`` that registers a session under the key adopts it."""
    from kiro_crew import session_lifecycle as sl

    mgr, key, compact, order, notices = await _setup()
    # A session already lives under the key (registered before anything was
    # parked), so the next get_or_create CLAIMS it rather than registering.
    bob = ("ts-b", "bob's", {"queued_owner": "bob"})
    sl._park_queue(mgr, key, (bob,))
    await mgr.get_or_create(KEY)
    try:
        assert [e[0] for e in mgr._sessions[key].queue] == ["ts-b"]
        assert key not in sl._parked_queue
    finally:
        mgr.release(key)
    # And a fresh registration adopts too: no session under the key at all.
    await mgr.reset(KEY)
    await _settle()
    assert key not in mgr._sessions
    sl._park_queue(mgr, key, (bob,))
    await mgr.get_or_create(KEY)
    try:
        assert [e[0] for e in mgr._sessions[key].queue] == ["ts-b"]
    finally:
        mgr.release(key)
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_registration_cancelled_before_it_commits_keeps_the_parked_entries():
    """The start record is the one suspension point between registering and
    returning; a cancellation there rolls the registration back. The parked
    entries must not have been adopted by the session that rollback removes."""
    from kiro_crew import session_allocation as sa
    from kiro_crew import session_lifecycle as sl

    mgr, key, compact, order, notices = await _setup()
    await mgr.reset(KEY)
    await _settle()
    assert key not in mgr._sessions
    bob = ("ts-b", "bob's", {"queued_owner": "bob"})
    sl._park_queue(mgr, key, (bob,))

    async def _boom(_key):
        raise asyncio.CancelledError()

    # Scoped: the stub must be gone before the second get_or_create below, and
    # ``monkeypatch.undo()`` would also undo every other patch on the fixture.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sa, "record_session_started", _boom)
        with pytest.raises(asyncio.CancelledError):
            await mgr.get_or_create(KEY)
    assert key not in mgr._sessions, "the registration rolled back"
    assert sl._parked_queue.get(key) == [bob], "still parked for the next start"
    await mgr.get_or_create(KEY)
    try:
        assert [e[0] for e in mgr._sessions[key].queue] == ["ts-b"]
    finally:
        mgr.release(key)
    await mgr.close_all()


@pytest.mark.asyncio
async def test_the_release_after_an_adopting_claim_wakes_the_adopted_entries_drains(monkeypatch):
    """Adoption inside get_or_create lands the entries on the queue; nothing
    starts them until a turn ends, so the release that frees the lease wakes
    their channels' drains."""
    from kiro_crew import session_allocation as sa
    from kiro_crew import session_lifecycle as sl

    mgr, key, compact, order, notices = await _setup()
    woke: list[tuple[str, list[str]]] = []

    async def _wake(*, waker, session_key, channels):
        woke.append((session_key, list(channels)))

    monkeypatch.setattr(sa, "wake_other_drains", _wake)
    bob = ("ts-b", "bob's", {"queued_owner": "bob", "queued_channel": "telegram"})
    sl._park_queue(mgr, key, (bob,))
    await mgr.get_or_create(KEY)  # claims the live session and adopts
    assert [e[0] for e in mgr._sessions[key].queue] == ["ts-b"]
    assert woke == [], "not before the lease is free"
    mgr.release(key)
    await _settle()
    assert woke == [(key, ["telegram"])]
    mgr.release(key) if mgr._sessions[key].semaphore.locked() else None
    await mgr.close_all()


def test_a_parked_entry_past_the_field_bounds_is_refused_not_retained(caplog):
    from unittest.mock import MagicMock

    from kiro_crew import session_lifecycle as sl

    sessions = MagicMock()
    sessions._fold_key = lambda k: k
    big_text = ("t1", "x" * (sl.PARKED_ENTRY_TEXT_MAX_CHARS + 1), {})
    big_kwargs = ("t2", "ok", {"blob": "y" * (sl.PARKED_ENTRY_KWARGS_MAX_CHARS + 1)})
    big_ts = ("t" * (sl.PARKED_ENTRY_TS_MAX_CHARS + 1), "ok", {})
    fine = ("t3", "ok", {})
    with caplog.at_level("WARNING", logger=sl.__name__):
        sl._park_queue(sessions, "k", (big_text, big_kwargs, big_ts, fine))
    assert sl._parked_queue["k"] == [fine]
    assert sessions.clear_queue.call_count == 3, "refused entries have their files unlinked"
    assert sum("field bounds" in r.message for r in caplog.records) == 3


@pytest.mark.asyncio
async def test_a_reset_that_raises_after_the_pop_parks_the_detached_queue():
    """The restore has no session to land on once the reset popped it; the
    detached entries are parked for the next start instead of unwinding away."""
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew import session_lifecycle as sl

    sessions = MagicMock()
    bob = ("ts-b", "bob's", {})
    sessions.detach_queue = MagicMock(return_value=(bob,))
    sessions.stop_turn = AsyncMock(side_effect=RuntimeError("shutdown raised after the pop"))
    sessions.has_session = MagicMock(return_value=False)
    with pytest.raises(RuntimeError):
        await sl.force_stop_keeping_others(sessions, "k", lambda _kw: False)
    sessions.restore_queue.assert_not_called()
    assert sl._parked_queue["k"] == [bob]
    # With the session still there, the ordinary restore is taken instead.
    sl._parked_queue.clear()
    sessions.has_session = MagicMock(return_value=True)
    with pytest.raises(RuntimeError):
        await sl.force_stop_keeping_others(sessions, "k", lambda _kw: False)
    sessions.restore_queue.assert_called_once_with("k", (bob,))
    assert "k" not in sl._parked_queue


@pytest.mark.asyncio
async def test_clear_queue_only_unlinks_the_handles_even_when_the_session_is_gone(tmp_path):
    """A hard stop pops the session between the detach and the clear; the
    detached entries were never on the popped queue, so this is the one place
    their files can be unlinked."""
    mgr, key, compact, order, notices = await _setup()
    tmp = tmp_path / "gone.png"
    tmp.write_bytes(b"x")
    assert mgr.enqueue(key, "ts-1", "at press", force=True, image_temp_paths=[str(tmp)])
    taken = mgr.detach_queue(key)
    assert await mgr.stop_turn(KEY, force=True) == "hard"
    mgr.clear_queue(key, only=taken)
    assert not tmp.exists()
    await mgr.close_all()


def test_decline_markers_are_count_bounded_and_the_overflow_is_counted(caplog):
    from kiro_crew import session_lifecycle as sl

    with caplog.at_level("WARNING", logger=sl.__name__):
        for i in range(sl.STOP_DECLINED_MARKERS_MAX + 3):
            sl.note_stop_declined(f"k{i}", "u", now=100.0)
    assert len(sl._stop_declined_markers) == sl.STOP_DECLINED_MARKERS_MAX
    # Oldest evicted, newest kept; the evicted presser's repeat is declined again.
    assert sl.consume_stop_declined("k0", "u", now=110.0) is False
    assert sl.consume_stop_declined(f"k{sl.STOP_DECLINED_MARKERS_MAX + 2}", "u", now=110.0)
    assert sum("evicted" in r.message for r in caplog.records) == 3
    assert "3 dropped so far" in caplog.records[-1].message


def test_expired_decline_markers_are_swept_on_the_next_write():
    from kiro_crew import session_lifecycle as sl

    for i in range(10):
        sl.note_stop_declined(f"k{i}", "u", now=100.0)
    sl.note_stop_declined("fresh", "u", now=100.0 + sl.STOP_DECLINED_ESCALATION_SECS)
    assert list(sl._stop_declined_markers) == [("fresh", "u")]


def test_a_decline_marker_key_past_the_string_bound_is_refused_not_retained(caplog):
    from kiro_crew import session_lifecycle as sl

    long_key = "k" * (sl.STOP_DECLINED_KEY_MAX_CHARS + 1)
    with caplog.at_level("WARNING", logger=sl.__name__):
        sl.note_stop_declined(long_key, "u", now=100.0)
        sl.note_stop_declined("k", "u" * (sl.STOP_DECLINED_KEY_MAX_CHARS + 1), now=100.0)
    assert sl._stop_declined_markers == {}
    assert sl.consume_stop_declined(long_key, "u", now=101.0) is False
    assert sum("refused" in r.message for r in caplog.records) == 2


def test_the_escalation_window_is_one_constant_for_dashboard_and_channels():
    from kiro_crew import session_lifecycle as sl
    from kiro_crew.dashboard import slot_projection as sp

    assert sp.STOP_DECLINED_ESCALATION_SECS is sl.STOP_DECLINED_ESCALATION_SECS


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


@pytest.mark.asyncio
async def test_the_claude_arm_ignores_a_stop_that_ended_the_previous_turn():
    """The Claude compaction waits for the semaphore; a Stop recorded BEFORE it is
    held ended that earlier turn and must settle a genuine failure as failed, not
    cancelled."""
    mgr, key, compact, order, notices = await _setup()
    session = mgr._sessions[key]
    session.provider.compact = AsyncMock(side_effect=RuntimeError("provider failed"))
    mgr._compaction._deps = dataclasses.replace(
        mgr._compaction._deps, is_claude_backend=lambda _p: True
    )
    # A turn holds the session; the compaction queues behind it. The Stop lands
    # on THAT turn, after the compaction task started but before it holds the
    # permit -- the window a counter read before the wait mistakes for its own.
    await session.semaphore.acquire()
    task = asyncio.ensure_future(mgr._compaction._compact_session(key, 90.0))
    await _settle()
    assert mgr.note_stop(KEY) is True
    session.semaphore.release()
    result = await asyncio.wait_for(task, timeout=5)
    assert result == "failed"
    assert notices == [(False, "compacted")]
    await mgr.close_all()


@pytest.mark.asyncio
async def test_the_claude_arm_times_out_behind_a_live_turn_as_a_failure_not_a_cancel():
    """A session with an old Stop in its history (the counter is never popped)
    whose compaction waits out its budget behind a live turn: a timeout, not
    "Stop ended the compaction"."""
    mgr, key, compact, order, notices = await _setup()
    session = mgr._sessions[key]
    mgr._compaction._deps = dataclasses.replace(
        mgr._compaction._deps,
        is_claude_backend=lambda _p: True,
        compact_wait_timeout_secs=lambda: 0.05,
    )
    assert mgr.note_stop(KEY) is True  # an earlier Stop, long settled
    await session.semaphore.acquire()  # a live turn the compaction parks behind
    try:
        result = await mgr._compaction._compact_session(key, 90.0)
    finally:
        session.semaphore.release()
    assert result == "failed"
    assert notices == [(False, "compacted")]
    await mgr.close_all()


@pytest.mark.asyncio
async def test_the_claude_arm_hands_the_permit_to_the_waiter_on_a_force_stop():
    """Same contract as the in-place arm: a hard Stop pops the session and hands
    the permit to a woken claimant; the compaction must not release it again."""
    mgr, key, compact, order, notices = await _setup()
    session = mgr._sessions[key]
    started = asyncio.Event()
    release = asyncio.Event()

    async def _compact():
        started.set()
        await release.wait()
        raise RuntimeError("cancelled by the harness")

    session.provider.compact = AsyncMock(side_effect=_compact)
    mgr._compaction._deps = dataclasses.replace(
        mgr._compaction._deps, is_claude_backend=lambda _p: True
    )
    task = asyncio.ensure_future(mgr._compaction._compact_session(key, 90.0))
    await asyncio.wait_for(started.wait(), timeout=2)

    async def _claimant():
        await session.semaphore.acquire()
        await asyncio.sleep(0.05)
        session.semaphore.release()  # must not raise
        return "released-cleanly"

    claimant = asyncio.ensure_future(_claimant())
    await _settle()
    assert not claimant.done()
    mgr._compacting.add(key)
    try:
        assert await mgr.stop_turn(KEY, force=True) == "hard"
    finally:
        mgr._compacting.discard(key)
        release.set()
    assert await asyncio.wait_for(task, timeout=5) == "cancelled"
    assert await asyncio.wait_for(claimant, timeout=5) == "released-cleanly"
    await mgr.close_all()


@pytest.mark.asyncio
async def test_the_claude_arm_settles_cancelled_when_the_stop_lands_on_its_turn():
    mgr, key, compact, order, notices = await _setup()
    session = mgr._sessions[key]

    async def _compact_then_stopped():
        mgr.note_stop(KEY)
        raise RuntimeError("cancelled by the harness")

    session.provider.compact = AsyncMock(side_effect=_compact_then_stopped)
    mgr._compaction._deps = dataclasses.replace(
        mgr._compaction._deps, is_claude_backend=lambda _p: True
    )
    result = await mgr._compaction._compact_session(key, 90.0)
    assert result == "cancelled"
    assert notices == [(False, COMPACT_OUTCOME_CANCELLED)]
    await mgr.close_all()


def _slack_orch():
    """The Slack orchestrator double the events suite uses, with a live !stop target."""
    import sys
    from unittest.mock import AsyncMock, MagicMock

    if _TEST_DIR not in sys.path:
        sys.path.insert(0, _TEST_DIR)
    from test_slack_events_coverage import _make_orch

    orch = _make_orch()
    orch.sessions.has_session = MagicMock(return_value=True)
    orch.sessions.get_session_for_thread = MagicMock(return_value=None)
    orch.sessions.note_stop = MagicMock(return_value=True)
    orch.sessions.clear_queue = MagicMock()
    orch.sessions.detach_queue = MagicMock(return_value=())
    orch.sessions.restore_queue = MagicMock()
    task = MagicMock()
    task.done.return_value = False
    orch._session_tasks = {"100.0": task}
    orch._pending_queue = {"100.0": [("ts", "text", {"paths": []})]}
    orch.slack.post_ephemeral = AsyncMock()
    return orch, task


def _run_slack_stop(orch):
    from unittest.mock import patch

    from kiro_crew.slack import events as ev

    with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
        with patch("kiro_crew.slack.events.is_owner", return_value=True):
            with patch("kiro_crew.slack.events.unlink_queued_temp_paths") as unlink:
                from test_slack_events_coverage import _event

                asyncio.run(ev._route_message(orch, _event(text="!stop"), ev.SeenCache()))
    return unlink


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
    from unittest.mock import AsyncMock, MagicMock

    orch, task = _slack_orch()
    orch.sessions.is_compacting = MagicMock(return_value=False)
    at_press = ("q-at-press",)
    orch.sessions.detach_queue = MagicMock(return_value=at_press)
    late = ("ts-late", "sent during the stop", {"paths": []})

    async def _stop(*_a, **_k):
        # By now the press-time pending entry is detached; a later one arrives.
        assert "100.0" not in orch._pending_queue
        orch._pending_queue["100.0"] = [late]
        return "soft"

    orch.sessions.stop_turn = AsyncMock(side_effect=_stop)
    unlink = _run_slack_stop(orch)
    orch.sessions.detach_queue.assert_called_once_with("100.0")
    orch.sessions.clear_queue.assert_called_once_with("100.0", only=at_press)
    orch.sessions.restore_queue.assert_not_called()
    assert orch._pending_queue["100.0"] == [late]
    assert unlink.call_count == 1  # the press-time pending entry's files


def test_slack_stop_tells_stop_turn_to_keep_the_queue_it_did_not_detach():
    """What the Stop drops was detached at the press and is cleared by identity
    after the outcome. ``stop_turn``'s own whole-queue clear would take a message
    admitted between the detach and the cancel: newer intent, never this Stop's."""
    from unittest.mock import AsyncMock, MagicMock

    orch, task = _slack_orch()
    orch.sessions.is_compacting = MagicMock(return_value=False)
    at_press = ("q-at-press",)
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
    """A Slack entry does not say who sent it, so on the forced repeat (hard reset)
    everything detached at the press goes to the successor instead of being
    dropped: the Stop was aimed at the compaction, not at the shared queue."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from kiro_crew.session_lifecycle import note_stop_declined

    orch, task = _slack_orch()
    orch.sessions.is_compacting = MagicMock(return_value=True)
    at_press = ("q-at-press",)
    orch.sessions.detach_queue = MagicMock(return_value=at_press)
    pending_at_press = list(orch._pending_queue["100.0"])
    orch.sessions.stop_turn = AsyncMock(return_value="hard")
    note_stop_declined("100.0", "U_OWNER")  # the first press was declined
    with patch("kiro_crew.slack.events.hand_queue_to_successor", new=AsyncMock()) as hand:
        unlink = _run_slack_stop(orch)
    assert orch.sessions.stop_turn.await_args.kwargs["force"] is True
    hand.assert_awaited_once_with(orch.sessions, "100.0", at_press)
    orch.sessions.clear_queue.assert_not_called()
    unlink.assert_not_called()
    assert orch._pending_queue["100.0"] == pending_at_press


@pytest.mark.asyncio
async def test_a_cancelled_successor_handoff_parks_the_entries_before_unwinding():
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew import session_lifecycle as sl

    sessions = MagicMock()
    sessions._fold_key = lambda k: k
    sessions.get_or_create = AsyncMock(side_effect=asyncio.CancelledError())
    bob = ("ts-b", "bob's", {})
    with pytest.raises(asyncio.CancelledError):
        await sl.hand_queue_to_successor(sessions, "k", (bob,))
    assert sl._parked_queue["k"] == [bob]


@pytest.mark.asyncio
async def test_clear_queue_only_drops_the_detached_entries():
    mgr, key, compact, order, notices = await _setup()
    assert mgr.enqueue(key, "ts-1", "at press", force=True)
    taken = mgr.detach_queue(key)
    assert mgr.enqueue(key, "ts-2", "admitted later", force=True)
    mgr.restore_queue(key, taken)
    assert [e[0] for e in mgr._sessions[key].queue] == ["ts-1", "ts-2"], "restored ahead"
    mgr.clear_queue(key, only=taken)
    left = [e[0] for e in mgr._sessions[key].queue]
    assert left == ["ts-2"]
    await mgr.close_all()


# -- 4. the notices --


def test_the_cancelled_notice_names_the_users_stop_as_the_cause_of_the_restart():
    from kiro_crew.dashboard.chat_compaction_notice import notice_text
    from kiro_crew.dashboard.state import (
        _AUTO_COMPACT_CANCELLED_NOTICE,
        _AUTO_COMPACT_FAILED_NOTICE,
        _AUTO_RECYCLE_NOTICE,
    )

    dashboard = _AUTO_COMPACT_CANCELLED_NOTICE.format(pct=87)
    assert "Stop" in dashboard
    assert "87% of the context limit" in dashboard, "the percentage names its referent"
    # The only Stop that reaches a compacting session is the forced one, and a
    # forced stop resets the session: the notice says so, and says what the
    # next reply is built from.
    assert dashboard.startswith("⏹ Your forced Stop"), "leads with the actor"
    assert "was restarted" in dashboard, "one verb with the restart notices"
    assert "reset" not in dashboard
    assert "kept running" not in dashboard
    assert "recent excerpt" in dashboard
    assert "didn't succeed" not in dashboard, "nothing failed; the user ended it"
    assert dashboard != _AUTO_COMPACT_FAILED_NOTICE.format(pct=87)
    assert dashboard != _AUTO_RECYCLE_NOTICE.format(pct=87)

    channel = notice_text("slack", 87.0, success=False, outcome=COMPACT_OUTCOME_CANCELLED)
    assert "stop" in channel.lower()
    assert "87% of the context limit" in channel
    assert channel.startswith("Your forced stop"), "leads with the actor"
    assert "was restarted" in channel
    assert "reset" not in channel
    assert "kept running" not in channel
    assert "`!new`" in channel, "the channel's own start-fresh command"
    assert "{" not in channel, "every placeholder filled"
    assert channel != notice_text("slack", 87.0, success=False, outcome="compacted")


# ── the decline is delivered before the escalation is armed ──────────────────


def test_decline_stop_arms_only_after_the_warning_is_delivered():
    """The marker is what makes the next press a hard reset; the sentence is what
    makes that press informed. Send first, arm second."""
    import kiro_crew.session_lifecycle as sl

    order: list[str] = []

    async def _deliver() -> bool:
        order.append("sent")
        assert sl._stop_declined_markers == {}, "nothing armed while the send is in flight"
        return True

    assert asyncio.run(sl.decline_stop("k", "u", _deliver, now=100.0)) is True
    assert order == ["sent"]
    assert sl.consume_stop_declined("k", "u", now=101.0) is True


def test_decline_stop_arms_nothing_when_the_send_raises():
    """A transport error is ordinary, and it makes the retry MORE likely: a user
    who saw no answer presses again within seconds. That retry must be a first
    press, not a silent session reset."""
    import kiro_crew.session_lifecycle as sl

    async def _deliver() -> bool:
        raise RuntimeError("transport down")

    with pytest.raises(RuntimeError):
        asyncio.run(sl.decline_stop("k", "u", _deliver, now=100.0))
    assert sl._stop_declined_markers == {}
    assert sl.consume_stop_declined("k", "u", now=101.0) is False


def test_decline_stop_arms_nothing_when_the_send_reports_it_could_not_run():
    """Some sends are conditional on a client or a message id rather than on an
    exception, and some swallow their own transport error. Those report False."""
    import kiro_crew.session_lifecycle as sl

    async def _no_client() -> bool:
        return False

    assert asyncio.run(sl.decline_stop("k", "u", _no_client, now=100.0)) is False
    assert sl._stop_declined_markers == {}
    assert sl.consume_stop_declined("k", "u", now=101.0) is False


@pytest.mark.parametrize("reported", [None, "", 0, "ts-1", object()])
def test_decline_stop_arms_nothing_unless_delivery_is_reported_as_true(reported):
    """Only ``True`` arms. ``None`` is what a send that swallows its own error
    returns when nothing landed, so reading an unknown value as success would
    reset the session of a user who was never warned; anything that is not the
    explicit yes leaves the next press a first press."""
    import kiro_crew.session_lifecycle as sl

    async def _sent():
        return reported

    assert asyncio.run(sl.decline_stop("k", "u", _sent, now=100.0)) is False
    assert sl._stop_declined_markers == {}
    assert sl.consume_stop_declined("k", "u", now=101.0) is False


def test_the_shared_channel_decline_sends_before_it_arms():
    """``stop_running_turn`` owns the send for exactly this reason: a channel
    that posted the text after the arm could leave the escalation live."""
    from unittest.mock import AsyncMock, MagicMock

    import kiro_crew.session_lifecycle as sl
    from kiro_crew.messaging.commands import STOP_REPLY_COMPACTING, stop_running_turn

    sessions = MagicMock()
    sessions.is_compacting = MagicMock(return_value=True)
    queue = MagicMock()
    queue.lock = asyncio.Lock()
    queue.finish_cancelled_locked = AsyncMock()

    sent: list[str] = []

    async def _deliver(text: str) -> int:
        sent.append(text)
        assert sl._stop_declined_markers == {}, "armed before the user was told"
        return 4242  # what a transport hands back: the id of the message it sent

    reply = asyncio.run(
        stop_running_turn(
            sessions,
            "telegram:1",
            queue=queue,
            surface=MagicMock(label="telegram"),
            owner="u1",
            deliver=_deliver,
        )
    )
    assert reply == STOP_REPLY_COMPACTING
    assert sent == [STOP_REPLY_COMPACTING]
    assert sl.consume_stop_declined("telegram:1", "u1") is True


def test_the_shared_channel_decline_arms_nothing_when_its_send_fails():
    from unittest.mock import AsyncMock, MagicMock

    import kiro_crew.session_lifecycle as sl
    from kiro_crew.messaging.commands import stop_running_turn

    sessions = MagicMock()
    sessions.is_compacting = MagicMock(return_value=True)
    queue = MagicMock()
    queue.lock = asyncio.Lock()
    queue.finish_cancelled_locked = AsyncMock()

    async def _deliver(_text: str) -> int:
        raise RuntimeError("telegram 502")

    with pytest.raises(RuntimeError):
        asyncio.run(
            stop_running_turn(
                sessions,
                "telegram:1",
                queue=queue,
                surface=MagicMock(label="telegram"),
                owner="u1",
                deliver=_deliver,
            )
        )
    assert sl._stop_declined_markers == {}, "the retry is a first press again"
    assert sessions.note_stop.call_count == 0
    sessions.clear_queue.assert_not_called()


def test_the_shared_channel_stop_delivers_every_reply_itself():
    """The non-decline replies go through the same send, so a channel has one
    path and cannot answer a Stop twice."""
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.messaging.commands import STOP_REPLY_CANCELLED, stop_running_turn

    sessions = MagicMock()
    sessions.is_compacting = MagicMock(return_value=False)
    sessions.is_busy = MagicMock(return_value=True)
    sessions.get_provider = MagicMock(return_value=MagicMock(cancel=AsyncMock()))
    queue = MagicMock()
    queue.lock = asyncio.Lock()
    queue.finish_cancelled_locked = AsyncMock()
    deliver = AsyncMock()

    reply = asyncio.run(
        stop_running_turn(
            sessions,
            "telegram:1",
            queue=queue,
            surface=MagicMock(label="telegram"),
            owner="u1",
            deliver=deliver,
        )
    )
    assert reply == STOP_REPLY_CANCELLED
    deliver.assert_awaited_once_with(STOP_REPLY_CANCELLED)


def test_slack_stop_arms_nothing_when_the_decline_post_never_lands():
    """``post_message`` hands back the ts of what landed. A falsy one is a warning
    this member never saw, so their retry stays a first press rather than the
    forcing second press the unseen warning would have promised."""
    from unittest.mock import AsyncMock, MagicMock

    import kiro_crew.session_lifecycle as sl

    sl._stop_declined_markers.clear()
    orch, _task = _slack_orch()
    orch.sessions.is_compacting = MagicMock(return_value=True)
    orch.sessions.stop_turn = AsyncMock()
    orch.slack.post_message = AsyncMock(return_value=None)
    _run_slack_stop(orch)
    orch.slack.post_message.assert_awaited()
    assert sl._stop_declined_markers == {}, "the retry is a first press again"
    assert sl.consume_stop_declined("100.0", "U123") is False
    sl._stop_declined_markers.clear()


def test_the_shared_channel_decline_arms_nothing_when_the_send_reports_nothing_landed():
    """A transport whose send swallows its own error hands back no message id. That
    is not a delivered warning, so the escalation stays unarmed."""
    from unittest.mock import AsyncMock, MagicMock

    import kiro_crew.session_lifecycle as sl
    from kiro_crew.messaging.commands import STOP_REPLY_COMPACTING, stop_running_turn

    sl._stop_declined_markers.clear()
    sessions = MagicMock()
    sessions.is_compacting = MagicMock(return_value=True)
    queue = MagicMock()
    queue.lock = asyncio.Lock()
    queue.finish_cancelled_locked = AsyncMock()
    sent: list[str] = []

    async def _deliver(text: str) -> None:
        sent.append(text)  # logged its own failure and returned nothing

    reply = asyncio.run(
        stop_running_turn(
            sessions,
            "telegram:2",
            queue=queue,
            surface=MagicMock(label="telegram"),
            owner="u1",
            deliver=_deliver,
        )
    )
    assert reply == STOP_REPLY_COMPACTING
    assert sent == [STOP_REPLY_COMPACTING]
    assert sl._stop_declined_markers == {}
    assert sl.consume_stop_declined("telegram:2", "u1") is False
    sessions.clear_queue.assert_not_called()
