"""``SessionManager.claim_existing`` claims the session registered for a key, never a new one.

A cron sub-agent's completion continues its parent's orchestration in the parent's
own session. A ``cron:`` key is stateless, so a session started for it would hold
none of that orchestration. The verb therefore hands out the entry registered when it
is called, waiting out a busy turn, and refuses (None, no permit held, nothing
started) whenever that entry is gone, replaced, dead or fenced by the time the permit
is in hand.

These tests drive the real ``SessionManager`` lease with fake providers. Each
hand-off is an event, an awaited task or a bounded wait on the claim's reservation.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from test_subagent_injection_live_turn import _HANG_CEILING_S, _provider_factory

from kiro_crew.config import KiroCrewConfig
from kiro_crew.session import SessionBusyError, SessionManager
from kiro_crew.testing.wait import async_wait_until

_KEY = "cron:job-1:run-1"


def _manager() -> tuple[SessionManager, list[Any]]:
    """A manager whose provider factory records every provider it starts."""
    factory, started = _provider_factory(), []

    def counting_factory(*args: Any, **kwargs: Any) -> Any:
        started.append(factory(*args, **kwargs))
        return started[-1]

    return SessionManager(KiroCrewConfig(), provider_factory=counting_factory), started


async def _live(sessions: SessionManager, key: str = _KEY) -> Any:
    """Register an idle session under *key*, as a turn that ended and released it."""
    provider, _is_new, _resumed = await sessions.get_or_create(key)
    sessions.release(key)
    return provider


class _Turn:
    """A turn holding *key*'s permit until ``finish`` is set."""

    def __init__(self, sessions: SessionManager, key: str = _KEY) -> None:
        self.sessions, self.key = sessions, key
        self.holds = asyncio.Event()
        self.finish = asyncio.Event()
        self.task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        await self.sessions.get_or_create(self.key)
        self.holds.set()
        try:
            await self.finish.wait()
        finally:
            self.sessions.release(self.key)


async def _parked_claim(sessions: SessionManager, key: str = _KEY) -> asyncio.Task[Any]:
    """Start a claim of *key* and return once it is waiting for the permit."""
    claim = asyncio.create_task(sessions.claim_existing(key))
    await async_wait_until(
        lambda: claim.done() or sessions._has_allocation_reservation(key),
        describe=lambda: "the claim waiting on the permit",
    )
    assert not claim.done()
    return claim


@pytest.mark.asyncio
async def test_a_key_with_no_session_is_refused_and_nothing_is_started() -> None:
    sessions, started = _manager()
    try:
        assert await sessions.claim_existing(_KEY) is None
        assert started == []
        assert not sessions.has_session(_KEY)
        assert not sessions._has_allocation_reservation(_KEY)
    finally:
        await sessions.close_all()


@pytest.mark.asyncio
async def test_a_live_idle_session_is_claimed_and_held_like_get_or_create() -> None:
    sessions, started = _manager()
    try:
        provider = await _live(sessions)

        claim = await sessions.claim_existing(_KEY)

        assert claim is not None
        claimed, is_new, resumed = claim
        assert claimed is provider
        # Its first turn was the one that registered it.
        assert (is_new, resumed) == (False, False)
        assert sessions.turn_holder(_KEY) is asyncio.current_task()
        assert len(started) == 1
        sessions.release(_KEY)
        assert sessions.turn_holder(_KEY) is None
        assert not sessions.is_busy(_KEY)
    finally:
        await sessions.close_all()


@pytest.mark.asyncio
async def test_a_busy_session_is_claimed_once_its_turn_releases_it() -> None:
    sessions, started = _manager()
    try:
        turn = _Turn(sessions)
        await asyncio.wait_for(turn.holds.wait(), _HANG_CEILING_S)
        provider = sessions.get_provider(_KEY)
        claim = await _parked_claim(sessions)

        turn.finish.set()
        result = await asyncio.wait_for(claim, _HANG_CEILING_S)

        assert result is not None and result[0] is provider
        assert len(started) == 1
        sessions.release(_KEY)
        await asyncio.wait_for(turn.task, _HANG_CEILING_S)
    finally:
        await sessions.close_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("ends_conversation", [False, True], ids=["recycle", "ending"])
async def test_a_session_reset_under_a_waiting_claim_is_refused_without_a_successor(
    ends_conversation: bool,
) -> None:
    """The turn the claim waits on resets the session while it still holds it, as a
    failed cron turn ends its conversation before its release."""
    sessions, started = _manager()
    try:
        turn = _Turn(sessions)
        await asyncio.wait_for(turn.holds.wait(), _HANG_CEILING_S)
        claim = await _parked_claim(sessions)

        await sessions.reset(_KEY, ends_conversation=ends_conversation)
        turn.finish.set()

        assert await asyncio.wait_for(claim, _HANG_CEILING_S) is None
        await asyncio.wait_for(turn.task, _HANG_CEILING_S)
        assert len(started) == 1, "the refused claim started a session"
        assert not sessions.has_session(_KEY)
        assert not sessions._has_allocation_reservation(_KEY)
    finally:
        await sessions.close_all()


@pytest.mark.asyncio
async def test_a_claim_never_follows_its_entry_into_a_successor() -> None:
    """The entry the claim waited on is torn down and another turn registers and holds
    a successor under the key: the claim refuses, and the successor's turn keeps its
    permit."""
    sessions, started = _manager()
    try:
        turn = _Turn(sessions)
        await asyncio.wait_for(turn.holds.wait(), _HANG_CEILING_S)
        claim = await _parked_claim(sessions)
        await sessions.reset(_KEY)
        turn.finish.set()
        await asyncio.wait_for(turn.task, _HANG_CEILING_S)
        successor = _Turn(sessions)
        await asyncio.wait_for(successor.holds.wait(), _HANG_CEILING_S)

        assert await asyncio.wait_for(claim, _HANG_CEILING_S) is None
        assert sessions.turn_holder(_KEY) is successor.task
        assert len(started) == 2, "only the successor's own turn started a session"
        successor.finish.set()
        await asyncio.wait_for(successor.task, _HANG_CEILING_S)
    finally:
        await sessions.close_all()


@pytest.mark.asyncio
async def test_a_dead_process_is_refused_and_nothing_is_started_in_its_place() -> None:
    sessions, started = _manager()
    try:
        provider = await _live(sessions)
        provider.is_process_alive = lambda: False

        assert await sessions.claim_existing(_KEY) is None
        assert len(started) == 1
        assert not sessions.is_busy(_KEY)
    finally:
        await sessions.close_all()


@pytest.mark.asyncio
async def test_a_process_that_died_during_the_wait_refuses_the_claim() -> None:
    """The process exits while the claim waits out a turn: when the turn releases the
    permit the claim refuses, gives the permit back and starts nothing."""
    sessions, started = _manager()
    try:
        turn = _Turn(sessions)
        await asyncio.wait_for(turn.holds.wait(), _HANG_CEILING_S)
        claim = await _parked_claim(sessions)
        assert len(started) == 1
        started[0].is_process_alive = lambda: False
        turn.finish.set()

        assert await asyncio.wait_for(claim, _HANG_CEILING_S) is None
        await asyncio.wait_for(turn.task, _HANG_CEILING_S)
        assert len(started) == 1
        assert not sessions.is_busy(_KEY), "the refused claim kept the permit"
        assert not sessions._has_allocation_reservation(_KEY)
    finally:
        await sessions.close_all()


@pytest.mark.asyncio
async def test_a_fenced_key_is_refused_at_once_rather_than_held() -> None:
    sessions, started = _manager()
    try:
        await _live(sessions)
        with sessions.ending_key(_KEY):
            assert await sessions.claim_existing(_KEY) is None
        assert len(started) == 1
        assert not sessions.is_busy(_KEY)
    finally:
        await sessions.close_all()


@pytest.mark.asyncio
async def test_a_fence_that_rose_and_lifted_during_the_wait_refuses_the_claim() -> None:
    """The reaper fences the key while the claim waits, writes its record and lifts the
    fence, all before the claim gets the permit: the claim still refuses."""
    sessions, started = _manager()
    try:
        turn = _Turn(sessions)
        await asyncio.wait_for(turn.holds.wait(), _HANG_CEILING_S)
        claim = await _parked_claim(sessions)
        with sessions.ending_key(_KEY):
            pass
        turn.finish.set()

        assert await asyncio.wait_for(claim, _HANG_CEILING_S) is None
        await asyncio.wait_for(turn.task, _HANG_CEILING_S)
        assert len(started) == 1
        assert not sessions.is_busy(_KEY), "the refused claim kept the permit"
        assert not sessions._has_allocation_reservation(_KEY)
    finally:
        await sessions.close_all()


@pytest.mark.asyncio
async def test_a_manager_that_began_closing_during_the_wait_refuses_the_claim() -> None:
    """The gateway starts shutting down while the claim waits out a turn: the session
    is still registered and alive when the turn releases it, and the claim still
    refuses, giving the permit back."""
    sessions, _started = _manager()
    try:
        turn = _Turn(sessions)
        await asyncio.wait_for(turn.holds.wait(), _HANG_CEILING_S)
        claim = await _parked_claim(sessions)
        sessions._allocation_boundary()._closing = True
        turn.finish.set()

        assert await asyncio.wait_for(claim, _HANG_CEILING_S) is None
        await asyncio.wait_for(turn.task, _HANG_CEILING_S)
        assert sessions.has_session(_KEY)
        assert not sessions.is_busy(_KEY), "the refused claim kept the permit"
    finally:
        sessions._allocation_boundary()._closing = False
        await sessions.close_all()


@pytest.mark.asyncio
async def test_a_cancelled_waiting_claim_leaves_no_reservation_and_no_permit() -> None:
    sessions, _started = _manager()
    try:
        turn = _Turn(sessions)
        await asyncio.wait_for(turn.holds.wait(), _HANG_CEILING_S)
        claim = await _parked_claim(sessions)

        claim.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(claim, _HANG_CEILING_S)

        assert not sessions._has_allocation_reservation(_KEY)
        turn.finish.set()
        await asyncio.wait_for(turn.task, _HANG_CEILING_S)
        assert not sessions.is_busy(_KEY)
    finally:
        await sessions.close_all()


@pytest.mark.asyncio
async def test_a_busy_session_refuses_a_claim_that_will_not_wait() -> None:
    sessions, _started = _manager()
    try:
        turn = _Turn(sessions)
        await asyncio.wait_for(turn.holds.wait(), _HANG_CEILING_S)

        with pytest.raises(SessionBusyError):
            await sessions.claim_existing(_KEY, wait_if_busy=False)

        assert not sessions._has_allocation_reservation(_KEY)
        turn.finish.set()
        await asyncio.wait_for(turn.task, _HANG_CEILING_S)
    finally:
        await sessions.close_all()
