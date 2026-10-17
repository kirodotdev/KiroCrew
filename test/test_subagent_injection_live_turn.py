"""A sub-agent completion never cancels its parent's live turn.

The cron and channel injection routes claim the parent's session, prompt it,
and on the way out cancel "their" prompt. When an attempt times out, the route
resets the parent first. That reset pops the session and wakes the claimants
parked on its lease, so by the time the route's own cleanup runs the key can
already hold a successor running the parent's NEXT turn (the next cron fire, a
new channel message). A cancel addressed by key alone lands on that turn.

These tests drive the real ``SessionManager`` lease. The parent's next turn is
already waiting for the key while the injection holds it, and the reset wrapper
only lets the injection's cleanup run once that turn owns the successor --
the window a slow provider shutdown inside ``reset`` opens in production.
No sleeps: every hand-off is an event or an awaited task.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.config import KiroCrewConfig
from kiro_crew.session import SessionManager
from kiro_crew.subagent import SubagentInfo

#: Ceiling for a wait that only fails on a hang, so a regression fails by name.
_HANG_CEILING_S = 30


def _provider_factory() -> Any:
    def factory(session_key: Any = None, agent: Any = None, channel_id: Any = None, **kwargs: Any):
        m = AsyncMock()
        m.start = AsyncMock()
        m.memory_mode = kwargs.get("memory_mode", "persistent")
        m.shutdown = AsyncMock()
        m.is_process_alive = lambda: True
        m.disown_work_dir = MagicMock()
        m.context_usage_pct = lambda: 0.0
        m.context_window_tokens = lambda: 0
        m.has_active_turn = lambda: False
        m.runtime_abort_target = lambda: None
        m.spawn_identity = ""
        m._runtime = None
        m.cancel = AsyncMock(return_value="acked")
        return m

    return factory


def _gateway(sessions: SessionManager) -> Any:
    from kiro_crew.slack.gateway import GatewayOrchestrator

    gateway = GatewayOrchestrator.__new__(GatewayOrchestrator)
    gateway.sessions = sessions
    gateway.ctx_builder = None
    gateway.conv_log = None
    gateway.dashboard_state = None
    gateway.slack = None
    gateway._owner_id = ""
    gateway._cron_injecting = {}
    gateway.cron_svc = None
    gateway.subagent_mgr = MagicMock()
    gateway.subagent_mgr.running = []
    gateway.subagent_mgr.queued_count_for_async = AsyncMock(return_value=0)
    gateway._channel_reply_link = MagicMock(return_value=None)
    gateway._deliver_channel_reply = AsyncMock()
    return gateway


class _ParentNextTurn:
    """The parent's next turn: parked on the key, then holding the successor."""

    def __init__(self, sessions: SessionManager, key: str) -> None:
        self._sessions = sessions
        self._key = key
        self.owns_lease = asyncio.Event()
        self.finish = asyncio.Event()
        self.task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self.task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        await self._sessions.get_or_create(self._key)
        self.owns_lease.set()
        try:
            await self.finish.wait()
        finally:
            self._sessions.release(self._key)


async def _drive(route: str, key: str) -> tuple[Any, Any, _ParentNextTurn, SessionManager]:
    """Time an injection out under *route* while the parent's next turn waits."""
    sessions = SessionManager(KiroCrewConfig(), provider_factory=_provider_factory())
    gateway = _gateway(sessions)
    parent_turn = _ParentNextTurn(sessions, key)
    injected_into: list[Any] = []

    async def inject_times_out(info, client, msg, parent_key, label):
        # The injection holds the parent's lease here. The parent's next turn
        # arrives now and parks on it, as a cron fire or channel message does.
        injected_into.append(client)
        parent_turn.start()
        raise asyncio.TimeoutError

    real_reset = sessions.reset

    async def reset_until_successor_is_live(parent_key, **kwargs):
        result = await real_reset(parent_key, **kwargs)
        await asyncio.wait_for(parent_turn.owns_lease.wait(), _HANG_CEILING_S)
        return result

    gateway._inject_with_retry = inject_times_out
    info = SubagentInfo(id="child-1", task="work", parent_session_key=key)
    info.result = "done"
    info.done = True
    with (
        patch.object(sessions, "reset", side_effect=reset_until_successor_is_live),
        patch("kiro_crew.slack.gateway._MAX_INJECT_ATTEMPTS", 1),
    ):
        if route == "cron":
            # A sibling still running keeps the route's after-injection
            # session reset out of this test; that reset is not a cancel.
            gateway.subagent_mgr.running = [
                SubagentInfo(id="child-2", task="more", parent_session_key=key)
            ]
            await asyncio.wait_for(
                gateway._finish_subagent_cron_or_notify(
                    info, announce="child done", body="b", title="t", parent_key=key
                ),
                _HANG_CEILING_S,
            )
        else:
            await asyncio.wait_for(
                gateway._inject_subagent_into_channel_parent(
                    info, announce="child done", body="b", title="t", parent_key=key
                ),
                _HANG_CEILING_S,
            )
    assert len(injected_into) == 1
    return injected_into[0], sessions._sessions[key], parent_turn, sessions


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "key"),
    [("cron", "cron:job-1"), ("channel", "telegram:chat-1")],
)
async def test_a_timed_out_injection_leaves_the_parents_next_turn_running(route, key):
    injected_provider, successor, parent_turn, sessions = await _drive(route, key)
    try:
        assert successor.provider is not injected_provider, "the reset made a successor"
        assert successor.turn_owner is parent_turn.task
        successor.provider.cancel.assert_not_awaited()
        # The injection's release was owed to the popped session, so the
        # parent's turn still holds its own lease.
        assert successor.semaphore.locked()
        assert not parent_turn.task.done()
    finally:
        parent_turn.finish.set()
        await asyncio.wait_for(parent_turn.task, _HANG_CEILING_S)
        await sessions.close_all()


@pytest.mark.asyncio
async def test_the_owner_fenced_cancel_still_reaches_the_callers_own_turn():
    """The fence narrows the cancel to the caller; it does not disable it."""
    sessions = SessionManager(KiroCrewConfig(), provider_factory=_provider_factory())
    try:
        provider, _is_new, _resumed = await sessions.get_or_create("cron:job-2")
        outcome = await sessions.cancel_current("cron:job-2", owner_task=asyncio.current_task())
        assert outcome == "acked"
        provider.cancel.assert_awaited_once()
        sessions.release("cron:job-2")
    finally:
        await sessions.close_all()
