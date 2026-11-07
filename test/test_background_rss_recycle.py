"""The background session is bounded by resident memory growth, not only by context.

Context percent and the prompt-count backstop are both reached by a busy
session, not by a fat one: background turns are tiny text prompts, so a
transcript of oversized records grows the runtime by
``session.watchdog_rss_max_mb`` long before either criterion fires. The RSS
sweep in ``session_cleanup`` skips persistent keys and the background key is
one, so ``recycle_background`` is the only place that reading is taken. The
limit applies to growth past the provider's baseline: the MCP servers a runtime
launches can put a healthy tree above the limit, and they keep starting after
the provider is up. These cases pin it: the growth threshold, the first reading
as baseline, the settle window that raises it, the re-baseline after a recycle,
the ``0`` fallback, and an unreadable tree falling through to the other criteria.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import session_background
from kiro_crew.config import KiroCrewConfig
from kiro_crew.session import _BG_BLIND_RECYCLE_PROMPTS, BACKGROUND_KEY, SessionManager
from kiro_crew.session_background import BACKGROUND_RSS_FALLBACK_MB

_CEILING_MB = 1536
_BG_PID = 4242
#: A fresh runtime's first reading: the baseline growth is measured from.
_START_MB = 400
#: A runtime that launches many MCP servers, already above the limit at rest.
_MCP_HEAVY_START_MB = 3700
#: Clock steps on either side of the settle window, chosen so these cases do not
#: depend on its exact length.
_INSIDE_THE_SETTLE_WINDOW_SECS = 1.0
_PAST_THE_SETTLE_WINDOW_SECS = 3600.0


class _Clock:
    """The ``time.monotonic`` the background runtime reads, moved by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def settle(self) -> None:
        """Move past the window in which readings only raise the baseline."""
        self.now += _PAST_THE_SETTLE_WINDOW_SECS


@pytest.fixture(autouse=True)
def clock():
    fake = _Clock()
    with patch("kiro_crew.session_background.time", SimpleNamespace(monotonic=fake.monotonic)):
        yield fake


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
        # there, which is neither a pid nor None. Every provider, the recycle's
        # replacement included, reports the same pid.
        m.client._pid = _BG_PID
        return m

    return factory


async def _started_manager(cfg):
    mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
    await mgr.start_pool()
    # The background session starts on first use; these tests act on a live one.
    await mgr._ensure_background()
    return mgr


async def _turn_with_reading(mgr, mb):
    """One background turn's recycle check, with the tree reading *mb* MiB."""
    with patch("kiro_crew.session.get_session_rss_mb", return_value=mb):
        await mgr.recycle_background()


async def _settled_baseline(mgr, clock, mb=_START_MB):
    """Record *mb* as the provider's baseline and fix it with a reading past the
    settle window, so growth counts from the next reading. Two turns."""
    await _turn_with_reading(mgr, mb)
    clock.settle()
    await _turn_with_reading(mgr, mb)


class TestTreeRssGrowthRecyclesTheBackgroundSession:
    """Growth past the baseline is compared against ``session.watchdog_rss_max_mb``."""

    @pytest.mark.asyncio
    async def test_growth_at_the_limit_recycles_and_names_the_rss_reason(self, cfg, clock, caplog):
        """At the limit, not only above it: 1536 of a 1536 budget is spent."""
        mgr = await _started_manager(cfg)
        provider = mgr._sessions[BACKGROUND_KEY].provider
        await _settled_baseline(mgr, clock)
        provider.shutdown.assert_not_awaited()

        with patch(
            "kiro_crew.session.get_session_rss_mb", return_value=_START_MB + _CEILING_MB
        ) as measure:
            with caplog.at_level("INFO", logger="kiro_crew.session"):
                await mgr.recycle_background()

        measure.assert_called_once_with(_BG_PID)
        provider.shutdown.assert_awaited_once()
        assert mgr._sessions[BACKGROUND_KEY].provider is not provider
        assert mgr._sessions[BACKGROUND_KEY].prompt_count == 0
        assert (
            f"tree rss={_START_MB + _CEILING_MB}MB grew {_CEILING_MB}MB past its baseline "
            f"of {_START_MB}MB (limit {_CEILING_MB}MB)"
        ) in caplog.text
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_growth_far_over_the_limit_recycles(self, cfg, clock, caplog):
        """A transcript that grew the tree from 400 MB to 1.9 GB."""
        mgr = await _started_manager(cfg)
        provider = mgr._sessions[BACKGROUND_KEY].provider
        await _settled_baseline(mgr, clock)

        with caplog.at_level("INFO", logger="kiro_crew.session"):
            await _turn_with_reading(mgr, 1990)

        provider.shutdown.assert_awaited_once()
        assert f"grew 1590MB past its baseline of {_START_MB}MB" in caplog.text
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_growth_below_the_limit_keeps_the_session(self, cfg, clock):
        """Control: a healthy runtime under every criterion is left alone."""
        mgr = await _started_manager(cfg)
        provider = mgr._sessions[BACKGROUND_KEY].provider
        await _settled_baseline(mgr, clock)

        await _turn_with_reading(mgr, _START_MB + _CEILING_MB - 1)

        provider.shutdown.assert_not_awaited()
        assert mgr._sessions[BACKGROUND_KEY].provider is provider
        assert mgr._sessions[BACKGROUND_KEY].prompt_count == 3
        await mgr.close_all()


class TestTheBaseline:
    """A runtime's MCP servers are not growth, including those still starting."""

    @pytest.mark.asyncio
    async def test_a_tree_that_starts_above_the_limit_survives_its_turns(self, cfg, clock):
        """The reported case: a 3.7 GB tree at rest against a 1536 MB limit is
        kept turn after turn while it does not grow by the limit."""
        mgr = await _started_manager(cfg)
        provider = mgr._sessions[BACKGROUND_KEY].provider
        await _settled_baseline(mgr, clock, _MCP_HEAVY_START_MB)

        for mb in (_MCP_HEAVY_START_MB + 200, _MCP_HEAVY_START_MB + 50):
            await _turn_with_reading(mgr, mb)

        provider.shutdown.assert_not_awaited()
        assert mgr._sessions[BACKGROUND_KEY].provider is provider
        assert mgr._sessions[BACKGROUND_KEY].prompt_count == 4
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_tree_that_starts_above_the_limit_still_recycles_on_growth(
        self, cfg, clock, caplog
    ):
        mgr = await _started_manager(cfg)
        provider = mgr._sessions[BACKGROUND_KEY].provider
        await _settled_baseline(mgr, clock, _MCP_HEAVY_START_MB)

        with caplog.at_level("INFO", logger="kiro_crew.session"):
            await _turn_with_reading(mgr, _MCP_HEAVY_START_MB + _CEILING_MB)

        provider.shutdown.assert_awaited_once()
        assert f"past its baseline of {_MCP_HEAVY_START_MB}MB" in caplog.text
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_servers_still_starting_after_a_short_first_turn_raise_the_baseline(
        self, cfg, clock
    ):
        """A first turn that ends a few seconds after spawn reads a tree whose MCP
        servers are still starting. Readings inside the settle window raise the
        baseline, so the servers that come up after it are not growth."""
        mgr = await _started_manager(cfg)
        provider = mgr._sessions[BACKGROUND_KEY].provider
        await _turn_with_reading(mgr, 1000)
        clock.now += _INSIDE_THE_SETTLE_WINDOW_SECS
        await _turn_with_reading(mgr, 3800)
        clock.settle()

        await _turn_with_reading(mgr, 3900)

        provider.shutdown.assert_not_awaited()
        assert mgr._sessions[BACKGROUND_KEY].provider is provider
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_start_up_reading_followed_by_a_gap_fixes_the_baseline(self, cfg, clock):
        """A burst of turns, then a quiet gap: the only reading inside the window
        was taken while MCP servers were still starting, and the next one comes
        long after. That later reading fixes the baseline instead of counting
        the servers' start-up as growth."""
        mgr = await _started_manager(cfg)
        provider = mgr._sessions[BACKGROUND_KEY].provider
        await _turn_with_reading(mgr, 1000)
        clock.settle()

        await _turn_with_reading(mgr, 3800)
        await _turn_with_reading(mgr, 3900)

        provider.shutdown.assert_not_awaited()
        assert mgr._sessions[BACKGROUND_KEY].provider is provider
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_the_window_counts_from_the_first_reading(self, cfg, clock):
        """A raise inside the window does not restart it: the first reading at
        least the window's length after the FIRST reading fixes the baseline,
        so growth past it recycles on the very next turn."""
        window = session_background.BACKGROUND_RSS_SETTLE_SECS
        mgr = await _started_manager(cfg)
        provider = mgr._sessions[BACKGROUND_KEY].provider
        await _turn_with_reading(mgr, 1000)
        clock.now += window * 0.9
        await _turn_with_reading(mgr, 3800)
        clock.now += window * 0.1
        await _turn_with_reading(mgr, 3800)
        provider.shutdown.assert_not_awaited()

        await _turn_with_reading(mgr, 3800 + _CEILING_MB)

        provider.shutdown.assert_awaited_once()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_the_replacement_measures_growth_from_its_own_baseline(self, cfg, clock):
        """After a recycle the next reading is the replacement's baseline, even
        though it reports the same pid, so the swap is not recycled again."""
        mgr = await _started_manager(cfg)
        await _settled_baseline(mgr, clock)
        await _turn_with_reading(mgr, _START_MB + _CEILING_MB)
        replacement = mgr._sessions[BACKGROUND_KEY].provider

        await _settled_baseline(mgr, clock, _START_MB + _CEILING_MB)
        await _turn_with_reading(mgr, _START_MB + _CEILING_MB + 100)

        replacement.shutdown.assert_not_awaited()
        assert mgr._sessions[BACKGROUND_KEY].provider is replacement
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_zero_reading_is_not_a_baseline(self, cfg, clock):
        """A host that cannot measure the tree reports 0; the first real reading
        after it is the baseline, so the whole MCP-heavy tree is not growth."""
        mgr = await _started_manager(cfg)
        provider = mgr._sessions[BACKGROUND_KEY].provider
        await _turn_with_reading(mgr, 0)
        clock.settle()
        await _settled_baseline(mgr, clock, _MCP_HEAVY_START_MB)

        await _turn_with_reading(mgr, _MCP_HEAVY_START_MB + 100)

        provider.shutdown.assert_not_awaited()
        assert mgr._sessions[BACKGROUND_KEY].provider is provider
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_new_pid_is_a_new_baseline(self, cfg, clock):
        """A provider started by another path (a respawn after death) reports a
        new pid; its first reading is not compared with the old provider's."""
        mgr = await _started_manager(cfg)
        provider = mgr._sessions[BACKGROUND_KEY].provider
        await _settled_baseline(mgr, clock)
        provider.client._pid = _BG_PID + 1

        await _turn_with_reading(mgr, _START_MB + _CEILING_MB)

        provider.shutdown.assert_not_awaited()
        assert mgr._sessions[BACKGROUND_KEY].provider is provider
        await mgr.close_all()


class TestZeroFallsBackToTheBackgroundLimit:
    """``session.watchdog_rss_max_mb = 0`` turns the chat-session sweep off, but
    the background runtime keeps ``BACKGROUND_RSS_FALLBACK_MB``."""

    @pytest.mark.asyncio
    async def test_zero_still_recycles_growth_over_the_fallback(self, cfg, clock, caplog):
        cfg.session.watchdog_rss_max_mb = 0
        mgr = await _started_manager(cfg)
        provider = mgr._sessions[BACKGROUND_KEY].provider
        await _settled_baseline(mgr, clock)

        with caplog.at_level("INFO", logger="kiro_crew.session"):
            await _turn_with_reading(mgr, _START_MB + BACKGROUND_RSS_FALLBACK_MB)

        provider.shutdown.assert_awaited_once()
        assert f"(limit {BACKGROUND_RSS_FALLBACK_MB}MB)" in caplog.text
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_zero_leaves_growth_under_the_fallback_alone(self, cfg, clock):
        cfg.session.watchdog_rss_max_mb = 0
        mgr = await _started_manager(cfg)
        provider = mgr._sessions[BACKGROUND_KEY].provider
        await _settled_baseline(mgr, clock)

        await _turn_with_reading(mgr, _START_MB + BACKGROUND_RSS_FALLBACK_MB - 1)

        provider.shutdown.assert_not_awaited()
        assert mgr._sessions[BACKGROUND_KEY].provider is provider
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_the_shipped_default_keeps_the_lower_background_limit(self, clock, caplog):
        """The chat-session default sits above the background fallback; the
        background runtime still recycles at the fallback's growth."""
        cfg = KiroCrewConfig()
        cfg.session.timeout_secs = 2
        assert cfg.session.watchdog_rss_max_mb > BACKGROUND_RSS_FALLBACK_MB
        mgr = await _started_manager(cfg)
        provider = mgr._sessions[BACKGROUND_KEY].provider
        await _settled_baseline(mgr, clock)

        with caplog.at_level("INFO", logger="kiro_crew.session"):
            await _turn_with_reading(mgr, _START_MB + BACKGROUND_RSS_FALLBACK_MB)

        provider.shutdown.assert_awaited_once()
        assert f"(limit {BACKGROUND_RSS_FALLBACK_MB}MB)" in caplog.text
        await mgr.close_all()


class TestAnUnreadableTreeFallsThrough:
    """``None`` and ``0`` are no reading, and neither is an error."""

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

        assert mgr._sessions[BACKGROUND_KEY].provider is provider
        measure.assert_not_called()
        provider.shutdown.assert_not_awaited()
        await mgr.close_all()


@pytest.mark.parametrize("verb", ["reset", "remove"])
@pytest.mark.asyncio
async def test_a_teardown_while_the_recycle_holds_the_permit_does_not_over_release_it(
    cfg, clock, verb
):
    """A teardown that pops the background session while ``recycle_background``
    holds its permit records the recycle as the holder and hands the permit on.
    The recycle's own release must then be absorbed, not raise
    ``ValueError: BoundedSemaphore released too many times``."""
    mgr = await _started_manager(cfg)
    popped = mgr._sessions[BACKGROUND_KEY]
    base_factory = mgr._provider_factory

    def factory(*args, **kwargs):
        replacement = base_factory(*args, **kwargs)

        async def _start() -> None:
            # The recycle holds the permit here; tear the entry down under it.
            await getattr(mgr, verb)(BACKGROUND_KEY)

        replacement.start = AsyncMock(side_effect=_start)
        return replacement

    await _settled_baseline(mgr, clock)
    mgr._provider_factory = factory
    try:
        await _turn_with_reading(mgr, _START_MB + _CEILING_MB)
        assert BACKGROUND_KEY not in mgr._sessions
        assert not popped.semaphore.locked()
    finally:
        await mgr.close_all()
