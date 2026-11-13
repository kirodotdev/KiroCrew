"""The completion-timeout reset clears a stuck injection, never the parent's own turn.

When a completion's delivery outlives ``_ON_DONE_TIMEOUT``, the terminal report
cancels it and resets the parent session. The reset exists for an injection
whose prompt is stuck on the parent's provider. The cron route waits for the
parent's lease, though, so the same timeout also fires while the parent's own
long turn is simply still running, and that turn must not be killed.

These drive the real ``SessionManager`` lease under the real terminal report. The
cancelled delivery releases any lease it held before the reset runs, so a turn
still holding the session at reset time is the parent's own. A permit that
release handed to a queued sibling names no holder yet, and the reset still runs.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from test_subagent_injection_live_turn import _gateway, _provider_factory

from kiro_crew.config import KiroCrewConfig
from kiro_crew.session import SessionManager
from kiro_crew.subagent import SubagentInfo, SubagentManager

_KEY = "cron:job-1:run-1"
_TIMEOUT_S = 0.2
_CEILING_S = 30


class _ParentTurn:
    """The parent's own turn: it holds the key's lease until told to finish."""

    def __init__(self, sessions: SessionManager) -> None:
        self._sessions = sessions
        self.owns_lease = asyncio.Event()
        self.finish = asyncio.Event()
        self.task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        await self._sessions.get_or_create(_KEY)
        self.owns_lease.set()
        try:
            await self.finish.wait()
        finally:
            self._sessions.release(_KEY)


async def _report(sessions: SessionManager, on_done: Any) -> bool:
    """Run the terminal report for a finished child of ``_KEY`` delivered by *on_done*."""
    manager = SubagentManager(sessions=sessions, ctx_builder=MagicMock(), on_done=on_done)
    info = SubagentInfo(id="child-1", task="work", parent_session_key=_KEY)
    info.result, info.done = "done", True
    with patch("kiro_crew.subagent._ON_DONE_TIMEOUT", _TIMEOUT_S):
        return await asyncio.wait_for(
            manager._terminal._report_terminal_guarded_impl(
                info,
                source="test",
                injection_timeout_reason="timed out",  # flake-ok: failure text, not a duration
                mark_delivered_on_success=False,
            ),
            _CEILING_S,
        )


async def _report_with_hanging_injection(sessions: SessionManager) -> bool:
    """Run the terminal report through the real cron route, whose injection never returns."""
    gateway = _gateway(sessions)

    async def inject_hangs(*_args: Any) -> str:
        await asyncio.Event().wait()
        return ""

    gateway._inject_with_retry = inject_hangs

    async def on_done(info: SubagentInfo) -> None:
        await gateway._finish_subagent_cron_or_notify(
            info,
            announce="child done",
            body="b",
            title="t",
            carried=(info.id,),
            parent_key=_KEY,
        )

    return await _report(sessions, on_done)


@pytest.mark.asyncio
async def test_a_stuck_injection_prompt_is_still_reset() -> None:
    sessions = SessionManager(KiroCrewConfig(), provider_factory=_provider_factory())
    try:
        provider, _is_new, _resumed = await sessions.get_or_create(_KEY)
        sessions.release(_KEY)
        assert await _report_with_hanging_injection(sessions) is False
        assert _KEY not in sessions._sessions, "the stuck injection's session was not reset"
        provider.shutdown.assert_awaited()
    finally:
        await sessions.close_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("lifecycle_lease", [False, True], ids=["turn-lease", "lifecycle-lease"])
async def test_a_parent_busy_with_its_own_turn_is_left_running(lifecycle_lease: bool) -> None:
    """A lifecycle holder (a listening channel member) keeps its lease for life.

    The injection then never acquires it, so no injection prompt can be stuck on
    that provider, and the reset must not tear down the provider the member holds.
    """
    sessions = SessionManager(KiroCrewConfig(), provider_factory=_provider_factory())
    parent = _ParentTurn(sessions)
    try:
        await asyncio.wait_for(parent.owns_lease.wait(), _CEILING_S)
        if lifecycle_lease:
            assert sessions.mark_lifecycle_lease(_KEY)
        provider = sessions._sessions[_KEY].provider
        assert await _report_with_hanging_injection(sessions) is False
        assert _KEY in sessions._sessions, "the parent's own turn was reset"
        assert sessions._sessions[_KEY].provider is provider
        assert sessions._sessions[_KEY].turn_owner is parent.task
        provider.shutdown.assert_not_awaited()
        assert not parent.task.done()
    finally:
        parent.finish.set()
        await asyncio.wait_for(parent.task, _CEILING_S)
        await sessions.close_all()


@pytest.mark.asyncio
async def test_a_stuck_injection_is_reset_while_a_sibling_waits_for_the_parent() -> None:
    """The cancelled claim's release hands its permit to a queued sibling delivery.

    The lease is then still held, by a claimant that has not run yet, so the reset
    must not read it as the parent's turn: it pops the stuck provider, and the
    sibling validates onto a fresh one instead of prompting the stuck one.
    """
    sessions = SessionManager(KiroCrewConfig(), provider_factory=_provider_factory())
    try:
        # The stuck delivery's claim, taken in this task: the report runs here too,
        # as the route runs in the report's task in production.
        provider, _is_new, _resumed = await sessions.get_or_create(_KEY)
        semaphore = sessions._sessions[_KEY].semaphore
        real_acquire = semaphore.acquire
        parked = asyncio.Event()

        async def acquire_marking_the_wait() -> bool:
            parked.set()
            return await real_acquire()

        async def delivery_hangs_holding_the_lease(_info: SubagentInfo) -> None:
            try:
                await asyncio.Event().wait()
            finally:
                sessions.release(_KEY)

        with patch.object(semaphore, "acquire", acquire_marking_the_wait):
            sibling = asyncio.create_task(sessions.get_or_create(_KEY))
            await asyncio.wait_for(parked.wait(), _CEILING_S)
            assert await _report(sessions, delivery_hangs_holding_the_lease) is False
        provider.shutdown.assert_awaited()
        sibling_provider, _is_new, _resumed = await asyncio.wait_for(sibling, _CEILING_S)
        assert sibling_provider is not provider, "the sibling was handed the stuck provider"
        sessions.release(_KEY)
    finally:
        await sessions.close_all()
