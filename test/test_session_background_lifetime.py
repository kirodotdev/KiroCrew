"""Lifetime bounds on the two background execution shapes.

Both shapes are long-lived by design and both had a hole that made their only
lifetime bound unreachable in practice:

* ``recycle_background()`` gated its 40-prompt blind backstop on
  ``pct == 0.0``, so a backend that reports any positive percentage disabled
  the backstop forever — and background turns are tiny text prompts that never
  approach the 70% threshold.
* ``get_bg_session()`` only recycled a stale multiplexed ``_bg`` runtime during
  a zero-session window, and merely LOGGED when the runtime was busy. A runtime
  that never goes idle was therefore never bounded by age or RSS.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.config import KiroCrewConfig
from kiro_crew.session import _BG_BLIND_RECYCLE_PROMPTS, BACKGROUND_KEY, SessionManager


@pytest.fixture
def cfg():
    c = KiroCrewConfig()
    c.session.timeout_secs = 2
    return c


async def _empty_provider_stream(_command: str):
    """An empty async iterator for provider methods consumed by ``async for``."""
    if False:  # pragma: no cover - establishes the async-generator protocol
        yield None


def _mock_provider_factory():
    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        m = AsyncMock()
        m.start = AsyncMock()
        m.shutdown = AsyncMock()
        m.is_process_alive = lambda: True
        m.context_usage_pct = lambda: 0.0
        m.context_usage_unknown = lambda: False
        m.context_window_tokens = lambda: 0
        m.has_active_turn = lambda: False
        m.runtime_info = lambda: (None, None)
        m.stream_command = MagicMock(side_effect=_empty_provider_stream)
        return m

    return factory


class TestBlindRecycleBackstopIsNotPctGated:
    """The prompt-count backstop must not be gated on ``pct == 0.0``."""

    @pytest.mark.asyncio
    async def test_backstop_fires_at_a_low_but_positive_reported_pct(self, cfg):
        """A truthfully reported 3% must not disable the only lifetime bound.

        Background prompts are tiny, so the 70% threshold is never reached; if
        the backstop is skipped whenever the backend reports a real number, the
        agent process lives for the whole gateway uptime.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.start_pool()

        provider = mgr._sessions[BACKGROUND_KEY].provider
        provider.context_usage_pct = lambda: 3.0
        provider.context_usage_unknown = lambda: False
        mgr._sessions[BACKGROUND_KEY].prompt_count = _BG_BLIND_RECYCLE_PROMPTS - 1

        await mgr.recycle_background()

        provider.shutdown.assert_awaited_once()
        assert mgr._sessions[BACKGROUND_KEY].provider is not provider
        assert mgr._sessions[BACKGROUND_KEY].prompt_count == 0
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_low_pct_below_the_backstop_still_keeps_the_session(self, cfg):
        """Control: the backstop is a prompt count, not "recycle every turn"."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.start_pool()

        provider = mgr._sessions[BACKGROUND_KEY].provider
        provider.context_usage_pct = lambda: 3.0
        provider.context_usage_unknown = lambda: False

        await mgr.recycle_background()

        provider.shutdown.assert_not_awaited()
        assert mgr._sessions[BACKGROUND_KEY].provider is provider
        assert mgr._sessions[BACKGROUND_KEY].prompt_count == 1
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_the_recycle_reason_names_the_backstop_not_the_percentage(self, cfg, caplog):
        """An operator reading "context at 3%" would not find the real trigger."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.start_pool()

        provider = mgr._sessions[BACKGROUND_KEY].provider
        provider.context_usage_pct = lambda: 3.0
        provider.context_usage_unknown = lambda: False
        mgr._sessions[BACKGROUND_KEY].prompt_count = _BG_BLIND_RECYCLE_PROMPTS - 1

        with caplog.at_level("INFO", logger="kiro_crew.session"):
            await mgr.recycle_background()

        assert f"blind ({_BG_BLIND_RECYCLE_PROMPTS} prompts" in caplog.text
        await mgr.close_all()


class TestStaleBusyBgRuntimeIsDisplaced:
    """A stale ``_bg`` runtime is detached even while its handles are live."""

    @staticmethod
    def _fresh_runtime():
        rt = AsyncMock()
        rt.spawn = AsyncMock()
        rt.is_alive = lambda: True
        rt.create_session = AsyncMock(return_value=object())
        return rt

    @staticmethod
    def _busy_stale_runtime(reason: str = "rss"):
        rt = AsyncMock()
        rt.is_alive = lambda: True
        rt.has_active_or_initializing_sessions = lambda: True
        rt._is_stale = AsyncMock(return_value=reason)
        rt.kill = AsyncMock()
        rt.pid = 4242
        rt.create_session = AsyncMock(return_value=object())
        return rt

    @pytest.mark.asyncio
    async def test_a_busy_stale_runtime_is_parked_and_never_serves_a_new_caller(self, cfg):
        """The never-idle gap: the retiree keeps its handles, callers move on.

        The fake is stale by RSS and inside the age cap, because RSS is the
        growth mode that was observed; staleness is probed with ``_is_stale()``
        (age OR RSS), never an age-only predicate.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        stale = self._busy_stale_runtime()
        mgr._bg_runtime = stale

        fresh = self._fresh_runtime()
        with patch("kiro_crew.acp.runtime.AcpRuntime", return_value=fresh) as ctor:
            result = await mgr.get_bg_session()

        assert ctor.call_count == 1
        # The busy path must ask the full RSS+age probe: an age-only predicate
        # cannot see a runtime that balloons to multi-GB inside the 6h age cap.
        stale._is_stale.assert_awaited_once()
        assert result is fresh.create_session.return_value
        stale.create_session.assert_not_awaited()
        # In-flight background work finishes untouched...
        stale.kill.assert_not_awaited()
        # ...but the retiree is parked, so it is reaped once it drains.
        assert stale in mgr._draining_bg_runtimes
        assert mgr._bg_runtime is fresh

        mgr._draining_bg_runtimes = []
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_parked_retiree_is_reaped_once_its_handles_drain(self, cfg):
        """The park is a deferral, not a leak."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        stale = self._busy_stale_runtime()
        mgr._bg_runtime = stale

        fresh = self._fresh_runtime()
        with patch("kiro_crew.acp.runtime.AcpRuntime", return_value=fresh):
            await mgr.get_bg_session()

        # The retiree's last handle unregisters.
        stale.has_active_or_initializing_sessions = lambda: False
        async with mgr._bg_runtime_lock:
            await mgr._reap_drained_bg_runtimes_locked()

        stale.kill.assert_awaited_once()
        assert mgr._draining_bg_runtimes == []
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_healthy_busy_runtime_is_still_reused(self, cfg):
        """Control: displacement is staleness-driven, not load-driven."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        healthy = self._busy_stale_runtime(reason="")
        healthy._is_stale = AsyncMock(return_value=None)
        mgr._bg_runtime = healthy

        with patch(
            "kiro_crew.acp.runtime.AcpRuntime",
            side_effect=AssertionError("must not respawn a healthy runtime"),
        ):
            result = await mgr.get_bg_session()

        assert result is healthy.create_session.return_value
        assert mgr._draining_bg_runtimes == []
        assert mgr._bg_runtime is healthy
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_the_displacement_log_names_staleness_not_a_backend_switch(self, cfg, caplog):
        """Misattributing an RSS recycle as a backend flap misroutes triage."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        stale = self._busy_stale_runtime(reason="rss")
        mgr._bg_runtime = stale

        fresh = self._fresh_runtime()
        with (
            caplog.at_level("INFO", logger="kiro_crew.session"),
            patch("kiro_crew.acp.runtime.AcpRuntime", return_value=fresh),
        ):
            await mgr.get_bg_session()

        assert "stale by rss" in caplog.text
        # Routing staleness through the backend-switch adapter would send an
        # operator debugging RSS growth after a backend flap that never happened.
        assert "backend switched" not in caplog.text

        mgr._draining_bg_runtimes = []
        await mgr.close_all()
