"""The completion-timeout reset clears a stuck injection, never the parent's own turn.

When a completion's delivery outlives ``_ON_DONE_TIMEOUT``, the terminal report
cancels it and resets the parent session. The reset exists for an injection
whose prompt is stuck on the parent's provider. The cron route waits for the
parent's lease, though, so the same timeout also fires while the parent's own
long turn is simply still running, and that turn must not be killed.

Both cases drive the real ``SessionManager`` lease and the real cron route under
the real terminal report. The cancelled route releases any lease it held before
the reset runs, so a lease still held at reset time is the parent's own.
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
_CEILING_S = 10


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


async def _report_with_hanging_injection(sessions: SessionManager) -> tuple[bool, Any]:
    """Run the terminal report for a child of ``_KEY`` whose injection never returns."""
    gateway = _gateway(sessions)

    async def inject_hangs(*_args: Any) -> str:
        await asyncio.Event().wait()
        return ""

    gateway._inject_with_retry = inject_hangs
    gateway.subagent_mgr.running = [SubagentInfo(id="sibling", task="t", parent_session_key=_KEY)]

    async def on_done(info: SubagentInfo) -> None:
        await gateway._finish_subagent_cron_or_notify(
            info, announce="child done", body="b", title="t", parent_key=_KEY
        )

    manager = SubagentManager(sessions=sessions, ctx_builder=MagicMock(), on_done=on_done)
    info = SubagentInfo(id="child-1", task="work", parent_session_key=_KEY)
    info.result, info.done = "done", True
    with patch("kiro_crew.subagent._ON_DONE_TIMEOUT", _TIMEOUT_S):
        delivered = await asyncio.wait_for(
            manager._terminal._report_terminal_guarded_impl(
                info,
                source="test",
                injection_timeout_reason="timed out",
                mark_delivered_on_success=False,
            ),
            _CEILING_S,
        )
    return delivered, gateway


@pytest.mark.asyncio
async def test_a_stuck_injection_prompt_is_still_reset() -> None:
    sessions = SessionManager(KiroCrewConfig(), provider_factory=_provider_factory())
    try:
        provider, _is_new, _resumed = await sessions.get_or_create(_KEY)
        sessions.release(_KEY)
        delivered, _gateway_ = await _report_with_hanging_injection(sessions)
        assert delivered is False
        assert _KEY not in sessions._sessions, "the stuck injection's session was not reset"
        provider.shutdown.assert_awaited()
    finally:
        await sessions.close_all()


@pytest.mark.asyncio
async def test_a_parent_busy_with_its_own_turn_is_left_running() -> None:
    sessions = SessionManager(KiroCrewConfig(), provider_factory=_provider_factory())
    parent = _ParentTurn(sessions)
    try:
        await asyncio.wait_for(parent.owns_lease.wait(), _CEILING_S)
        provider = sessions._sessions[_KEY].provider
        delivered, _gateway_ = await _report_with_hanging_injection(sessions)
        assert delivered is False
        assert _KEY in sessions._sessions, "the parent's own turn was reset"
        assert sessions._sessions[_KEY].provider is provider
        assert sessions._sessions[_KEY].turn_owner is parent.task
        provider.shutdown.assert_not_awaited()
        assert not parent.task.done()
    finally:
        parent.finish.set()
        await asyncio.wait_for(parent.task, _CEILING_S)
        await sessions.close_all()
