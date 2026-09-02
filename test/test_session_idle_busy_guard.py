"""Tests for the idle sweep's busy guard and the keepalive's ``last_used`` bump.

The defect these pin: ``_expire_idle`` called ``reset()`` with ``skip_if_busy``
left at its default of ``False``, so the periodic sweep could tear down a session
with a turn in flight. Its sibling ``_rss_threshold_check`` already skipped busy
sessions — the guard was simply omitted here.

Why it reached a long turn: ``last_used`` is only bumped by ``get_or_create()``,
which a dashboard turn calls exactly once, so a turn running longer than
``timeout_secs`` looks idle to the arithmetic. The orphaned-dashboard-slot branch
is worse — it ignores the clock entirely, so a closed tab could reap a live turn
immediately.

The ``wait`` tool's keepalive did not help: it refreshed only the ACP runtime's
activity clock (which feeds ``is_responsive()``), never the session's
``last_used`` that the sweep actually reads.

The user-visible symptom was "⟳ Connection lost — retrying…" partway through a
long turn, never "your session was reaped for idleness".
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.config import KiroCrewConfig
from kiro_crew.session import SessionManager


@pytest.fixture
def cfg():
    c = KiroCrewConfig()
    c.session.timeout_secs = 2
    return c


def _mock_provider_factory():
    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        m = AsyncMock()
        m.start = AsyncMock()
        m.shutdown = AsyncMock()
        m.context_usage_pct = lambda: 0.0
        m.has_active_turn = lambda: False
        return m

    return factory


class TestIdleSweepBusyGuard:
    @pytest.mark.asyncio
    async def test_busy_session_is_not_reaped_when_it_looks_idle(self, cfg) -> None:
        """The regression: a long turn must survive the sweep.

        The permit is still held (no release()), so the session has a turn in
        flight even though ``last_used`` is backdated well past the timeout.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("dashboard:tab1")  # holds the permit
        async with mgr._lock:
            mgr._sessions["dashboard:tab1"].last_used = time.monotonic() - 10_000

        await mgr._expire_idle(timeout_secs=1)

        assert "dashboard:tab1" in mgr._sessions, "a session mid-turn was reaped as idle"
        mgr.release("dashboard:tab1")
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_busy_session_is_not_reaped_as_an_orphaned_tab(self, cfg) -> None:
        """The orphan branch ignores the clock, so it needs the guard too."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("dashboard:tab1")  # holds the permit
        mgr.set_active_dashboard_slots({"dashboard:tab2"})  # tab1 looks orphaned

        await mgr._expire_idle(9999)

        assert "dashboard:tab1" in mgr._sessions, "a live turn was reaped as an orphan"
        mgr.release("dashboard:tab1")
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_genuinely_idle_session_is_still_reaped(self, cfg) -> None:
        """The guard must not disable the sweep it protects."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("thread1")
        mgr.release("thread1")
        async with mgr._lock:
            mgr._sessions["thread1"].last_used = time.monotonic() - 10

        await mgr._expire_idle(timeout_secs=1)

        assert mgr.count == 0
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_reset_is_asked_to_skip_busy_sessions(self, cfg) -> None:
        """Closes the collect→reset race: a turn may start after the lock drops.

        Pinned as a call-shape assertion so a later refactor cannot silently
        drop the flag while the behavioural tests still pass.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("thread1")
        mgr.release("thread1")
        async with mgr._lock:
            mgr._sessions["thread1"].last_used = time.monotonic() - 10
        mgr.reset = AsyncMock(return_value=True)  # type: ignore[method-assign]

        await mgr._expire_idle(timeout_secs=1)

        mgr.reset.assert_awaited_once()
        assert mgr.reset.await_args.kwargs["skip_if_busy"] is True
        await mgr.close_all()


class TestTouchLastUsed:
    @pytest.mark.asyncio
    async def test_touch_advances_last_used(self, cfg) -> None:
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("thread1")
        mgr.release("thread1")
        async with mgr._lock:
            mgr._sessions["thread1"].last_used = time.monotonic() - 10_000

        assert mgr.touch("thread1") is True

        await mgr._expire_idle(timeout_secs=1)
        assert "thread1" in mgr._sessions, "touch() did not protect the session"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_touch_reports_a_missing_session(self, cfg) -> None:
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        assert mgr.touch("nope") is False
        await mgr.close_all()


class TestKeepaliveHandler:
    @pytest.mark.asyncio
    async def test_keepalive_touches_last_used_as_well_as_the_acp_clock(self) -> None:
        """Without this, a session blocking in `wait` still ages toward reaping."""
        from kiro_crew.dashboard.handlers.sessions import api_session_keepalive

        provider = MagicMock()
        sessions = MagicMock()
        sessions.get_provider = MagicMock(return_value=provider)
        sessions.touch = MagicMock(return_value=True)
        request = MagicMock()
        request.app = {"state": MagicMock(sessions=sessions)}
        request.headers = {"X-Session-Key": "dashboard:tab1"}

        resp = await api_session_keepalive(request)

        assert resp.status == 200
        provider.touch_activity.assert_called_once()
        sessions.touch.assert_called_once_with("dashboard:tab1")

    @pytest.mark.asyncio
    async def test_keepalive_still_succeeds_when_touch_is_absent(self) -> None:
        """Older session managers / test stubs must not 500 the keepalive."""
        from kiro_crew.dashboard.handlers.sessions import api_session_keepalive

        sessions = MagicMock(spec=["get_provider"])
        sessions.get_provider = MagicMock(return_value=MagicMock())
        request = MagicMock()
        request.app = {"state": MagicMock(sessions=sessions)}
        request.headers = {"X-Session-Key": "dashboard:tab1"}

        resp = await api_session_keepalive(request)

        assert resp.status == 200
