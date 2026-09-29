"""The background session is bounded by resident memory, not only by context.

Context percent and the prompt-count backstop are both reached by a busy
session, not by a fat one: background turns are tiny text prompts, so a
transcript of oversized records drives the runtime past
``session.watchdog_rss_max_mb`` long before either criterion fires. The RSS
sweep in ``session_cleanup`` skips persistent keys and the background key is
one, so ``recycle_background`` is the only place that reading is taken. These
cases pin it: the threshold, the ``0`` disable, and an unreadable tree falling
through to the other two criteria.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.config import KiroCrewConfig
from kiro_crew.session import _BG_BLIND_RECYCLE_PROMPTS, BACKGROUND_KEY, SessionManager

_CEILING_MB = 1536
_BG_PID = 4242


@pytest.fixture
def cfg():
    c = KiroCrewConfig()
    c.session.timeout_secs = 2
    c.session.watchdog_rss_max_mb = _CEILING_MB
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
        # Well under the 70% threshold: these cases are about the RSS criterion,
        # so the two existing ones stay silent unless a case arms them.
        m.context_usage_pct = lambda: 3.0
        m.context_usage_unknown = lambda: False
        m.context_window_tokens = lambda: 0
        m.has_active_turn = lambda: False
        m.runtime_info = lambda: (None, None)
        m.stream_command = MagicMock(side_effect=_empty_provider_stream)
        # get_pid reads provider.client._pid; an AsyncMock hands back a Mock
        # there, which is neither a pid nor None.
        m.client._pid = _BG_PID
        return m

    return factory


async def _started_manager(cfg):
    mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
    await mgr.start_pool()
    return mgr


class TestTreeRssRecyclesTheBackgroundSession:
    """The tree reading is compared against ``session.watchdog_rss_max_mb``."""

    @pytest.mark.asyncio
    async def test_tree_at_the_ceiling_recycles_and_names_the_rss_reason(self, cfg, caplog):
        """At the ceiling, not only above it: 1536 of a 1536 budget is spent."""
        mgr = await _started_manager(cfg)
        provider = mgr._sessions[BACKGROUND_KEY].provider

        with patch("kiro_crew.session.get_session_rss_mb", return_value=_CEILING_MB) as measure:
            with caplog.at_level("INFO", logger="kiro_crew.session"):
                await mgr.recycle_background()

        measure.assert_called_once_with(_BG_PID)
        provider.shutdown.assert_awaited_once()
        assert mgr._sessions[BACKGROUND_KEY].provider is not provider
        assert mgr._sessions[BACKGROUND_KEY].prompt_count == 0
        assert f"tree rss={_CEILING_MB}MB exceeds {_CEILING_MB}MB" in caplog.text
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_tree_far_over_the_ceiling_recycles(self, cfg, caplog):
        """The 1.9 GB reading from the report, against the 1536 MB default."""
        mgr = await _started_manager(cfg)
        provider = mgr._sessions[BACKGROUND_KEY].provider

        with patch("kiro_crew.session.get_session_rss_mb", return_value=1890):
            with caplog.at_level("INFO", logger="kiro_crew.session"):
                await mgr.recycle_background()

        provider.shutdown.assert_awaited_once()
        assert f"tree rss=1890MB exceeds {_CEILING_MB}MB" in caplog.text
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_tree_below_the_ceiling_keeps_the_session(self, cfg):
        """Control: a healthy runtime under every criterion is left alone."""
        mgr = await _started_manager(cfg)
        provider = mgr._sessions[BACKGROUND_KEY].provider

        with patch("kiro_crew.session.get_session_rss_mb", return_value=_CEILING_MB - 1):
            await mgr.recycle_background()

        provider.shutdown.assert_not_awaited()
        assert mgr._sessions[BACKGROUND_KEY].provider is provider
        assert mgr._sessions[BACKGROUND_KEY].prompt_count == 1
        await mgr.close_all()


class TestTheCeilingCanBeDisabled:
    """``session.watchdog_rss_max_mb = 0`` disables this check, as in the sweep."""

    @pytest.mark.asyncio
    async def test_zero_never_recycles_on_rss_and_reads_no_tree(self, cfg):
        cfg.session.watchdog_rss_max_mb = 0
        mgr = await _started_manager(cfg)
        provider = mgr._sessions[BACKGROUND_KEY].provider

        with patch("kiro_crew.session.get_session_rss_mb", return_value=99_999) as measure:
            await mgr.recycle_background()

        measure.assert_not_called()
        provider.shutdown.assert_not_awaited()
        assert mgr._sessions[BACKGROUND_KEY].provider is provider
        await mgr.close_all()


class TestAnUnreadableTreeFallsThrough:
    """No reading is not a reading of zero, and it is not an error either."""

    @pytest.mark.asyncio
    async def test_none_leaves_the_session_alone_without_raising(self, cfg):
        mgr = await _started_manager(cfg)
        provider = mgr._sessions[BACKGROUND_KEY].provider

        with patch("kiro_crew.session.get_session_rss_mb", return_value=None):
            await mgr.recycle_background()

        provider.shutdown.assert_not_awaited()
        assert mgr._sessions[BACKGROUND_KEY].provider is provider
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_none_still_lets_the_prompt_backstop_recycle(self, cfg, caplog):
        """The existing criteria decide the turn on their own."""
        mgr = await _started_manager(cfg)
        provider = mgr._sessions[BACKGROUND_KEY].provider
        mgr._sessions[BACKGROUND_KEY].prompt_count = _BG_BLIND_RECYCLE_PROMPTS - 1

        with patch("kiro_crew.session.get_session_rss_mb", return_value=None):
            with caplog.at_level("INFO", logger="kiro_crew.session"):
                await mgr.recycle_background()

        provider.shutdown.assert_awaited_once()
        assert f"blind ({_BG_BLIND_RECYCLE_PROMPTS} prompts" in caplog.text
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_raising_reader_leaves_the_session_alone(self, cfg):
        """A /proc read that throws must not take the background turn down."""
        mgr = await _started_manager(cfg)
        provider = mgr._sessions[BACKGROUND_KEY].provider

        with patch("kiro_crew.session.get_session_rss_mb", side_effect=OSError("boom")):
            await mgr.recycle_background()

        provider.shutdown.assert_not_awaited()
        assert mgr._sessions[BACKGROUND_KEY].provider is provider
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_no_pid_reads_no_tree(self, cfg):
        """A provider with no host pid has no tree to measure."""
        mgr = await _started_manager(cfg)
        provider = mgr._sessions[BACKGROUND_KEY].provider

        with patch.object(SessionManager, "get_pid", return_value=None):
            with patch("kiro_crew.session.get_session_rss_mb", return_value=99_999) as measure:
                await mgr.recycle_background()

        measure.assert_not_called()
        provider.shutdown.assert_not_awaited()
        await mgr.close_all()
