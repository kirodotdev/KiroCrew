"""The Claude compaction arm and the uncompactable restart honour a Stop like ``/compact``.

A Stop from an earlier turn leaves a genuine failure as failed, a Stop on the arm's
own turn settles cancelled, a cooperative Stop is declined once the restart holds
the permit, and a force Stop hands that permit to the parked claimant exactly once.
"""

from __future__ import annotations

import asyncio
import dataclasses
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from compaction_stop_declined_helpers import _clear_channel_decline_markers  # noqa: F401
from compaction_stop_declined_helpers import KEY, _settle, _setup

from kiro_crew.acp_backends import ACP_BACKEND_DEEPSEEK
from kiro_crew.session_compaction import (
    COMPACT_OUTCOME_CANCELLED,
    COMPACT_OUTCOME_RESTARTED_UNCOMPACTABLE,
    COMPACT_OUTCOME_WAITING_FOR_SUBAGENTS,
)
from kiro_crew.session_lifecycle import compaction_in_flight
from kiro_crew.testing.wait import async_wait_until, await_until, until_parked


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
    # flake-ok: pre-existing acquire of a permit no other task holds, so it returns at once; moved verbatim
    await session.semaphore.acquire()
    task = asyncio.ensure_future(mgr._compaction._compact_session(key, 90.0))
    # flake-ok: pre-existing yield barrier that lets the scheduled task reach its wait, moved verbatim; tracked for a de-flake pass
    await _settle()
    assert mgr.note_stop(KEY) is True
    session.semaphore.release()
    result = await await_until(task, what="the compaction settling")
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
        # flake-ok: pre-existing 50 ms compaction wait budget the test lets run out on purpose: the timeout is the behaviour under test, moved verbatim
        compact_wait_timeout_secs=lambda: 0.05,
    )
    assert mgr.note_stop(KEY) is True  # an earlier Stop, long settled
    # flake-ok: pre-existing acquire of a permit no other task holds, so it returns at once; moved verbatim
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
        # flake-ok: pre-existing unbounded wait inside the stub until the test releases it, moved verbatim; tracked for a de-flake pass
        await release.wait()
        raise RuntimeError("cancelled by the harness")

    session.provider.compact = AsyncMock(side_effect=_compact)
    mgr._compaction._deps = dataclasses.replace(
        mgr._compaction._deps, is_claude_backend=lambda _p: True
    )
    task = asyncio.ensure_future(mgr._compaction._compact_session(key, 90.0))
    await await_until(started, what="the compaction starting")

    async def _claimant():
        # flake-ok: pre-existing acquire inside a claimant task the test awaits under a bounded await_until, moved verbatim; tracked for a de-flake pass
        await session.semaphore.acquire()
        # flake-ok: pre-existing 50 ms hold inside the claimant task before it releases the permit, moved verbatim; tracked for a de-flake pass
        # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing sleep barrier, moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(0.05)
        session.semaphore.release()  # must not raise
        return "released-cleanly"

    claimant = asyncio.ensure_future(_claimant())
    # flake-ok: pre-existing yield barrier that lets the scheduled task reach its wait, moved verbatim; tracked for a de-flake pass
    await _settle()
    assert not claimant.done()
    mgr._compacting.add(key)
    try:
        assert await mgr.stop_turn(KEY, force=True) == "hard"
    finally:
        mgr._compacting.discard(key)
        release.set()
    assert await await_until(task, what="the compaction settling") == "cancelled"
    assert (
        await await_until(claimant, what="the claimant releasing the permit") == "released-cleanly"
    )
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


# -- the uncompactable restart (no /compact to run) honours a Stop the same way --


class _SharedSubagent:
    """One sub-agent live on the parent's process until ``gate`` is set: the slice
    of ``SubagentManager`` the restart's cotenant wait reads and ends."""

    def __init__(self, key: str, gate: asyncio.Event) -> None:
        self.gate = gate
        self.child = SimpleNamespace(
            id="a1", parent_session_key=key, conversation_key="", _stop_origin=""
        )

    @property
    def running(self):
        return [] if self.gate.is_set() else [self.child]

    def has_live_shared_session(self, _k):
        return not self.gate.is_set()

    def snapshot_teardown_children(self, _p):
        return ()

    async def cancel(self, _i):
        self.gate.set()
        return True

    async def cancel_for_teardown(self, ids, *, parent_session_key, verb=""):
        return len(ids)


async def _uncompactable_restart(wait_secs: float):
    """Start ``_recycle_unmanaged`` with one live shared sub-agent; return once it
    holds the permit and waits in ``_await_cotenants``."""
    mgr, key, _compact, order, notices = await _setup()
    session = mgr._sessions[key]
    gate = asyncio.Event()
    mgr.set_child_teardown_handler(_SharedSubagent(key, gate))
    mgr._compaction._deps = dataclasses.replace(
        mgr._compaction._deps, cotenant_wait_secs=wait_secs, cotenant_poll_secs=0.01
    )
    task = asyncio.ensure_future(mgr._compaction._recycle_unmanaged(key, session, 95.0))
    # The wait announces itself before its first poll, with the permit held.
    await async_wait_until(
        lambda: (False, COMPACT_OUTCOME_WAITING_FOR_SUBAGENTS) in notices,
        describe=lambda: f"notices={notices!r}",
    )
    return mgr, key, session, gate, task, order, notices


async def _parked_on(session) -> None:
    """Return once a claimant is parked on *session*'s turn permit."""
    await until_parked(session.semaphore)


@pytest.mark.asyncio
async def test_a_hard_stop_during_the_uncompactable_restart_wait_settles_cancelled():
    """The restart for a backend with no ``/compact`` holds the permit while it
    waits for sub-agents. A force Stop there resets the session and hands the
    permit to a parked claimant; the restart must settle cancelled, spare the
    live sub-agent, and not release the claimant's permit a second time."""
    mgr, key, session, gate, task, order, notices = await _uncompactable_restart(30.0)
    assert not task.done()
    holds = asyncio.Event()
    let_go = asyncio.Event()

    async def _claimant() -> str:
        # flake-ok: pre-existing acquire inside a claimant task the test awaits under a bounded await_until, moved verbatim; tracked for a de-flake pass
        await session.semaphore.acquire()
        holds.set()
        # holds the permit while the restart's finally runs
        await await_until(let_go, what="the test letting the permit go")
        try:
            session.semaphore.release()
        except ValueError as exc:
            return f"ValueError: {exc}"
        return "released-cleanly"

    claimant = asyncio.ensure_future(_claimant())
    await _parked_on(session)
    assert not claimant.done()

    assert await mgr.stop_turn(KEY, force=True) == "hard"
    assert mgr._sessions.get(key) is not session
    await await_until(holds, what="the claimant holding the permit")
    await await_until(task, what="the restart finishing after the Stop")
    let_go.set()

    assert task.result() == "cancelled"
    assert not gate.is_set(), "the live sub-agent was cancelled"
    assert (
        await await_until(claimant, what="the claimant releasing the permit") == "released-cleanly"
    )
    assert notices[-1] == (False, COMPACT_OUTCOME_CANCELLED)
    gate.set()
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_stop_recorded_during_the_uncompactable_restart_wait_releases_the_permit():
    """A Stop recorded with no reset taken ends the wait as cancelled. The permit is
    still this task's, so the restart releases it and a parked claimant wakes."""
    mgr, key, session, gate, task, order, notices = await _uncompactable_restart(30.0)
    assert not task.done()
    woke = asyncio.Event()

    async def _claimant():
        # flake-ok: pre-existing acquire inside a claimant task whose wake the test waits for under a bounded await_until, moved verbatim; tracked for a de-flake pass
        await session.semaphore.acquire()
        woke.set()
        session.semaphore.release()

    claimant = asyncio.ensure_future(_claimant())
    await _parked_on(session)
    assert mgr.note_stop(KEY) is True
    await await_until(task, what="the restart finishing after the Stop")

    assert task.result() == "cancelled"
    assert not gate.is_set(), "the sub-agent was cancelled"
    assert order == [], "the session was shut down"
    await await_until(woke, what="the parked claimant waking")
    # flake-ok: pre-existing await of a claimant task already seen to wake, moved verbatim; tracked for a de-flake pass
    await claimant
    gate.set()
    await mgr.close_all()


@pytest.mark.asyncio
async def test_an_unstopped_uncompactable_restart_still_stops_the_subagent_and_recycles():
    """Control: with no Stop the wait runs out, stops the sub-agent and recycles."""
    # flake-ok: pre-existing 0.5 s cotenant wait the control lets run out on purpose: the deadline is the behaviour under test, moved verbatim
    mgr, key, session, gate, task, order, notices = await _uncompactable_restart(0.5)
    assert await await_until(task, what="the compaction settling") == "recycled"
    assert gate.is_set(), "the sub-agent was not stopped at the deadline"
    assert order == ["shutdown"]
    assert notices[-1] == (True, COMPACT_OUTCOME_RESTARTED_UNCOMPACTABLE)
    assert not session.semaphore.locked(), "the permit was not released"
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_cooperative_stop_during_the_triggered_uncompactable_restart_is_declined():
    """The threshold trigger on a backend with no ``/compact`` starts the restart,
    which waits for the permit of the turn that triggered it. That turn releases
    the permit and its task keeps running its end-of-turn work. Once the restart
    holds the permit it owns the session, so a cooperative Stop is declined as
    ``compacting`` and not recorded, the probe every channel runs first agrees,
    and the restart goes on to its recycle."""
    mgr, key, _compact, order, notices = await _setup()
    await mgr.get_or_create(KEY)  # the user's turn takes the permit
    session = mgr._sessions[key]
    provider = session.provider
    provider.context_usage_pct = lambda: 95.0
    provider.manual_compact_unsupported_backend = ACP_BACKEND_DEEPSEEK
    provider.compaction_unmanaged_backend = ACP_BACKEND_DEEPSEEK
    gate = asyncio.Event()
    mgr.set_child_teardown_handler(_SharedSubagent(key, gate))
    mgr._compaction._deps = dataclasses.replace(
        mgr._compaction._deps, cotenant_wait_secs=30.0, cotenant_poll_secs=0.01
    )
    before = mgr.stop_generation(key)

    mgr.check_context_usage(key, provider)
    task = mgr._compaction.state.compaction_tasks[key]
    await _parked_on(session)  # the restart waits behind this turn
    # The turn's prompt is over and it releases, but its task (this one) runs on.
    mgr.release(key)
    async with asyncio.timeout(30):
        while (False, COMPACT_OUTCOME_WAITING_FOR_SUBAGENTS) not in notices:
            await asyncio.sleep(0)
    assert session.semaphore.locked(), "premise: the restart holds the permit"

    try:
        probe = compaction_in_flight(mgr, KEY)
        outcome = await mgr.stop_turn(KEY, force=False)
        observed = (probe, outcome, provider.cancel.await_count)
        assert observed == (True, "compacting", 0), (
            "a cooperative Stop during the uncompactable restart was not declined: "
            f"(compaction_in_flight, stop_turn, cancels) = {observed}"
        )
        assert mgr.stop_generation(key) == before
        assert not task.done()
    finally:
        gate.set()
        result = await asyncio.wait_for(task, timeout=30)
    # The declined Stop did not end the restart: it ran on to its recycle.
    assert result == "recycled"
    assert order == ["shutdown"]
    await mgr.close_all()
