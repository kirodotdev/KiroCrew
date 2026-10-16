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

This module pins the in-place ``/compact`` arm, the dashboard Stop, the shared
channel stop's forcing repeat and its per-presser decline markers, and the queue a
forced Stop carries to the successor. How the other Stop surfaces leave their work
in place on a decline is pinned in ``test_compaction_stop_declined_surfaces.py``, the
marker and parked-queue bounds, arming only after delivery and the restart notices
in ``test_compaction_stop_declined_escalation.py``, and the Claude arm and the
uncompactable restart in ``test_compaction_stop_declined_claude_and_restart.py``.
The fakes they share live in ``compaction_stop_declined_helpers.py``.
"""

from __future__ import annotations

import asyncio
import dataclasses
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from compaction_stop_declined_helpers import _clear_channel_decline_markers  # noqa: F401
from compaction_stop_declined_helpers import (
    KEY,
    _Compact,
    _dashboard_state,
    _factory,
    _settle,
    _setup,
)

from kiro_crew.config import KiroCrewConfig
from kiro_crew.session import SessionManager
from kiro_crew.session_compaction import COMPACT_OUTCOME_CANCELLED, COMPACT_OUTCOME_RECYCLED
from kiro_crew.session_lifecycle import compaction_in_flight
from kiro_crew.testing.wait import async_wait_until, await_until

# -- 1. a cooperative Stop is declined while compacting --


@pytest.mark.asyncio
async def test_a_cooperative_stop_during_compaction_is_declined_and_not_recorded():
    mgr, key, compact, order, notices = await _setup()
    session = mgr._sessions[key]
    before = mgr.stop_generation(key)

    task = asyncio.ensure_future(mgr._compact_in_place(key, session, 90.0))
    await await_until(compact.started, what="the compaction starting")
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
        await await_until(task, what="the compaction settling")
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
    await await_until(compact.started, what="the compaction starting")
    mgr._compacting.add(key)
    try:
        outcome = await mgr.stop_turn(KEY, force=True)
    finally:
        mgr._compacting.discard(key)
        compact.fail_with = RuntimeError("compaction reported no result")
        compact.release.set()
    assert outcome == "hard"
    result = await await_until(task, what="the compaction settling")
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
    await await_until(compact.started, what="the compaction starting")

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
    assert await await_until(task, what="the compaction settling") == "cancelled"
    assert (
        await await_until(claimant, what="the claimant releasing the permit") == "released-cleanly"
    )
    await mgr.close_all()


@pytest.mark.asyncio
async def test_stop_turn_is_unchanged_when_nothing_is_compacting():
    mgr, key, compact, order, notices = await _setup()
    assert mgr.is_compacting(KEY) is False
    # No compaction and a mock provider that acks: the ordinary soft path.
    assert await mgr.stop_turn(KEY, force=False) == "soft"
    assert mgr.stop_generation(key) == 1
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_stop_on_the_turn_a_compaction_waits_behind_is_a_soft_cancel():
    """The threshold trigger commits a compaction while the turn it was triggered
    from still holds the turn permit, and the compaction then waits for it. Until
    it has the permit that turn owns the session, so a cooperative Stop cancels it
    as usual, and the probe every channel runs first agrees."""
    mgr, key, compact, order, notices = await _setup()
    await mgr.get_or_create(KEY)  # the user's turn takes the permit
    session = mgr._sessions[key]
    session.provider.context_usage_pct = lambda: 95.0
    session.provider.cancel = AsyncMock(return_value="acked")
    before = mgr.stop_generation(key)

    mgr.check_context_usage(key, session.provider)
    async with asyncio.timeout(30):
        while not session.semaphore._waiters:  # the compaction parks on the permit
            await asyncio.sleep(0)
    assert mgr.is_compacting(KEY) is True
    assert not compact.started.is_set(), "premise: /compact has not started"

    # What a channel reads first, then the Stop itself.
    probe = compaction_in_flight(mgr, KEY)
    outcome = await mgr.stop_turn(KEY, force=False)
    observed = (probe, outcome, session.provider.cancel.await_count)
    assert observed == (False, "soft", 1), (
        "a cooperative Stop on the turn the compaction waits behind was declined: "
        f"(compaction_in_flight, stop_turn, cancels) = {observed}"
    )
    assert mgr.stop_generation(key) == before + 1

    mgr.release(key)
    compact.release.set()
    await async_wait_until(
        lambda: not mgr.is_compacting(KEY),
        describe=lambda: f"is_compacting({KEY!r}) is still true",
    )
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_stop_once_the_triggered_compaction_holds_the_session_is_still_declined():
    """Control: once the triggered compaction has the permit and runs ``/compact``,
    a cooperative Stop is declined and not recorded, as before."""
    mgr, key, compact, order, notices = await _setup()
    session = mgr._sessions[key]
    session.provider.context_usage_pct = lambda: 95.0
    before = mgr.stop_generation(key)

    mgr.check_context_usage(key, session.provider)
    await asyncio.wait_for(compact.started.wait(), timeout=30)
    assert compaction_in_flight(mgr, KEY) is True

    assert await mgr.stop_turn(KEY, force=False) == "compacting"
    session.provider.cancel.assert_not_called()
    assert mgr.stop_generation(key) == before

    compact.release.set()
    await async_wait_until(
        lambda: not mgr.is_compacting(KEY),
        describe=lambda: f"is_compacting({KEY!r}) is still true",
    )
    await mgr.close_all()


class _ParkSignal(asyncio.BoundedSemaphore):
    """A session turn permit that sets ``parked`` when a claimant has to wait on it.

    The event is set in the claimant's own step, just before it parks, so a test
    awaiting it resumes only after the claimant is queued on the permit."""

    def __init__(self) -> None:
        super().__init__(1)
        self.parked = asyncio.Event()

    async def acquire(self) -> bool:
        if self.locked():
            self.parked.set()
        return await super().acquire()


@pytest.mark.asyncio
async def test_a_stop_while_the_turn_hands_the_permit_to_the_compaction_is_declined():
    """The turn the compaction waits behind releases the permit, and the semaphore
    hands it to the parked compaction, whose task has not run yet. The permit is the
    compaction's from that release on, although the turn's task is still alive, so
    a cooperative Stop in the hand-off is declined and not recorded, and the probe
    every channel runs first agrees."""
    mgr, key, compact, order, notices = await _setup()
    session = mgr._sessions[key]
    permit = _ParkSignal()
    session.semaphore = permit  # idle: _setup released it
    await mgr.get_or_create(KEY)  # this task's turn takes the permit
    session.provider.context_usage_pct = lambda: 95.0
    before = mgr.stop_generation(key)

    mgr.check_context_usage(key, session.provider)
    task = mgr._compaction.state.compaction_tasks[key]
    try:
        await asyncio.wait_for(permit.parked.wait(), timeout=30)
        assert compaction_in_flight(mgr, KEY) is False, "premise: the turn holds the session"

        # The turn releases and this task runs on. From here to the asserts
        # nothing awaits, so the compaction's task cannot run in between.
        mgr.release(key)
        assert permit.locked() and not task.done(), "premise: the hand-off is pending"
        probe = compaction_in_flight(mgr, KEY)
        outcome = await mgr.stop_turn(KEY, force=False)
        observed = (probe, outcome, session.provider.cancel.await_count)
        assert not compact.started.is_set(), "premise: the Stop ran inside the hand-off"
        assert observed == (True, "compacting", 0), (
            "a cooperative Stop during the permit hand-off to the compaction was not "
            f"declined: (compaction_in_flight, stop_turn, cancels) = {observed}"
        )
        assert mgr.stop_generation(key) == before
    finally:
        compact.release.set()
        await asyncio.wait_for(task, timeout=30)
        await mgr.close_all()


@pytest.mark.asyncio
async def test_a_stop_after_the_turn_releases_before_the_compaction_ran_is_declined():
    """Control: the turn releases before the triggered compaction's task has run at
    all, so nobody holds the permit and no turn is left for the Stop to reach. The
    decline applies, as it does once the compaction holds the session."""
    mgr, key, compact, order, notices = await _setup()
    await mgr.get_or_create(KEY)  # this task's turn takes the permit
    session = mgr._sessions[key]
    session.provider.context_usage_pct = lambda: 95.0
    before = mgr.stop_generation(key)

    mgr.check_context_usage(key, session.provider)
    task = mgr._compaction.state.compaction_tasks[key]
    mgr.release(key)  # no await since the trigger: the compaction's task has not run
    try:
        assert not session.semaphore.locked(), "premise: nobody holds the permit"
        probe = compaction_in_flight(mgr, KEY)
        outcome = await mgr.stop_turn(KEY, force=False)
        observed = (probe, outcome, session.provider.cancel.await_count)
        assert observed == (True, "compacting", 0), (
            "a cooperative Stop with the permit free and the compaction committed was "
            f"not declined: (compaction_in_flight, stop_turn, cancels) = {observed}"
        )
        assert mgr.stop_generation(key) == before
    finally:
        compact.release.set()
        await asyncio.wait_for(task, timeout=30)
        await mgr.close_all()


@pytest.mark.asyncio
async def test_a_release_leaves_no_holder_named_on_the_session():
    """The record ends with the holding: a turn's release clears ``turn_owner``, and
    so does an awaited compaction, which holds the permit in its caller's task, a
    task that keeps running after the compaction."""
    mgr, key, compact, order, notices = await _setup()
    session = mgr._sessions[key]
    await mgr.get_or_create(KEY)
    assert session.turn_owner is asyncio.current_task(), "premise: the turn named itself"
    mgr.release(key)
    assert session.turn_owner is None, "the turn's release left its record"

    levels = [95.0]
    provider = session.provider
    provider.context_usage_pct = lambda: levels[0]

    def _completed(*_args, **_kwargs):
        levels[0] = 10.0
        return {"type": "completed"}

    provider.wait_for_compaction = AsyncMock(side_effect=_completed)
    compact.release.set()
    assert await asyncio.wait_for(mgr.compact_if_needed(KEY), timeout=30) == "ok"
    assert mgr._sessions.get(key) is session, "premise: compacted in place"
    assert session.turn_owner is None, "the compaction's release left its record"
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
    await await_until(compact.started, what="the compaction starting")

    assert mgr.note_stop(KEY) is True
    compact.fail_with = RuntimeError("compaction reported no result")
    compact.release.set()

    assert await await_until(task, what="the compaction settling") == "cancelled"
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
    await await_until(compact.started, what="the compaction starting")
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
    assert await await_until(task, what="the compaction settling") == "cancelled"
    assert not gate.is_set(), "the sub-agent was not cancelled"
    await await_until(woke, what="the parked claimant waking")
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
    await await_until(compact.started, what="the compaction starting")
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
    assert await await_until(task, what="the compaction settling") == "cancelled"
    assert not gate.is_set(), "the live sub-agent was not cancelled"
    assert notices[-1] == (False, COMPACT_OUTCOME_CANCELLED)
    assert (
        await await_until(claimant, what="the claimant releasing the permit") == "released-cleanly"
    )
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
    await await_until(compact.started, what="the compaction starting")
    compact.fail_with = RuntimeError("compaction reported no result")
    compact.release.set()
    # Genuine failure: the ordinary recycle arm.
    assert await await_until(task, what="the compaction settling") == "recycled"
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
    await await_until(compact.started, what="the compaction starting")
    # Declined during A, under the live key and under an alias a channel may
    # press with, which the probe folds onto the live key.
    sl.note_stop_declined(key, "bob")
    sl.note_stop_declined("alias-of-" + key, "alice")
    sl.note_stop_declined("other-session", "alice")
    real_fold = mgr._fold_key
    mgr._fold_key = lambda k: key if k == "alias-of-" + key else real_fold(k)
    compact.fail_with = RuntimeError("compaction reported no result")
    compact.release.set()
    await async_wait_until(
        lambda: not mgr.is_compacting(KEY), describe=lambda: f"compacting={mgr._compacting!r}"
    )
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
    await await_until(compact.started, what="the compaction starting")
    assert seen == [(key, True)]
    assert mgr.is_compacting(KEY) is True
    compact.fail_with = RuntimeError("compaction reported no result")
    compact.release.set()
    await async_wait_until(
        lambda: not mgr.is_compacting(KEY), describe=lambda: f"compacting={mgr._compacting!r}"
    )
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
    await await_until(compact.started, what="the compaction starting")
    assert mgr.is_compacting(KEY) is True
    compact.release.set()  # completes with no status -> failed -> recycled
    compact.fail_with = RuntimeError("compaction reported no result")
    await async_wait_until(
        lambda: not mgr.is_compacting(KEY), describe=lambda: f"compacting={mgr._compacting!r}"
    )
    assert mgr.is_compacting(KEY) is False
    assert notices == [(True, COMPACT_OUTCOME_RECYCLED)]
    await mgr.close_all()


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
    # And a fresh registration adopts too: no session under the key at all. The
    # adopted entry is dropped first, or the reset would park it and respawn.
    mgr._sessions[key].queue.clear()
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


async def _until_registered(mgr, key, old) -> object:
    """Wait for a successor of *old* to be registered under *key*, and return it."""

    def _succeeded() -> bool:
        current = mgr._sessions.get(key)
        return current is not None and current is not old

    await async_wait_until(
        _succeeded, describe=lambda: f"no successor registered under the key: {mgr._sessions!r}"
    )
    return mgr._sessions[key]


@pytest.mark.asyncio
async def test_a_failure_path_reset_carries_queued_follow_ups_to_a_successor(tmp_path):
    """A turn fails (``AcpPromptBusy``) while follow-ups wait on its queue; the
    handler's plain ``reset`` pops the session. The follow-ups and their staged
    files must reach a successor started for them, ahead of nothing, in order;
    an entry a mid-turn cancel marked is still dropped and its file unlinked."""
    from kiro_crew import session_lifecycle as sl

    mgr, key, compact, order, notices = await _setup()
    files = {name: tmp_path / f"{name}.png" for name in ("m2", "gone", "m3")}
    for path in files.values():
        path.write_bytes(b"x")
    for ts, name in (("ts-2", "m2"), ("ts-gone", "gone"), ("ts-3", "m3")):
        assert mgr.enqueue(key, ts, name, force=True, image_temp_paths=[str(files[name])])
    old = mgr._sessions[key]
    old.cancelled.add("ts-gone")

    assert await mgr.reset(KEY) is True

    new = await _until_registered(mgr, key, old)
    assert [e[0] for e in new.queue] == ["ts-2", "ts-3"], "the follow-ups reached the successor"
    assert files["m2"].exists() and files["m3"].exists(), "their staged files were kept"
    assert not files["gone"].exists(), "the cancelled entry's file was unlinked"
    assert key not in sl._parked_queue
    await _settle()
    assert not new.semaphore.locked(), "the respawn released its lease for the drain"
    await mgr.close_all()


@pytest.mark.asyncio
async def test_the_reset_successor_keeps_the_popped_sessions_identity():
    """A later claim takes a live session as-is, so a successor the reset started
    under default arguments would answer the follow-ups -- and every message after
    them -- as the default agent. It must start as the session it replaces."""
    order: list[str] = []
    calls: list[dict] = []
    inner = _factory(_Compact(), order)

    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        calls.append({"agent": agent, "channel_id": channel_id, **kwargs})
        return inner(session_key=session_key, agent=agent, channel_id=channel_id, **kwargs)

    mgr = SessionManager(KiroCrewConfig(), provider_factory=factory)
    await mgr.get_or_create(KEY, agent="research-agent", approval_policy="auto")
    key = mgr._fold_key(KEY)
    mgr.release(key)
    await mgr.set_channel(key, "C-team")
    assert mgr.enqueue(key, "ts-2", "m2", force=True)
    old = mgr._sessions[key]

    assert await mgr.reset(KEY) is True

    new = await _until_registered(mgr, key, old)
    assert new.agent == "research-agent"
    assert new.approval_policy == "auto"
    assert calls[-1]["agent"] == "research-agent"
    assert calls[-1]["channel_id"] == "C-team"
    assert [e[0] for e in new.queue] == ["ts-2"]
    await _settle()
    # The first queued turn, not the respawn, owns the first-turn observation:
    # without it a failed native resume would answer the follow-up with no history.
    _provider, is_new, resumed = await mgr.get_or_create(KEY)
    mgr.release(key)
    assert (is_new, resumed) == (True, False), "the queued turn sees a fresh start"
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_conversation_ending_reset_still_drops_the_queue(tmp_path):
    from kiro_crew import session_lifecycle as sl

    mgr, key, compact, order, notices = await _setup()
    staged = tmp_path / "m2.png"
    staged.write_bytes(b"x")
    assert mgr.enqueue(key, "ts-2", "m2", force=True, image_temp_paths=[str(staged)])

    assert await mgr.reset(KEY, ends_conversation=True) is True
    await _settle()

    assert key not in mgr._sessions, "an ending reset starts no successor"
    assert key not in sl._parked_queue
    assert not staged.exists()
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
