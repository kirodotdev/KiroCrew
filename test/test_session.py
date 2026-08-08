"""Tests for session manager."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp.types import AcpPromptStats
from kiro_crew.config import KiroCrewConfig
from kiro_crew.session import (
    _BG_BLIND_RECYCLE_PROMPTS,
    BACKGROUND_KEY,
    SessionManager,
)


@pytest.fixture
def cfg():
    c = KiroCrewConfig()
    c.session.timeout_secs = 2  # short for testing
    return c


def _mock_provider_factory():
    """Return a factory that creates mock LLMProviders."""

    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        m = AsyncMock()
        m.start = AsyncMock()
        m.shutdown = AsyncMock()
        # Explicit, not AsyncMock-generated: the post-semaphore re-validate calls
        # this synchronously, and an auto-generated coroutine would read as
        # "alive" only by truthiness while leaking an un-awaited coroutine.
        m.is_process_alive = lambda: True
        m.context_usage_pct = lambda: 0.0
        m.has_active_turn = lambda: False
        return m

    return factory


def _alive_provider_factory():
    """Like _mock_provider_factory but with an explicit live process check, so
    the fast-path session-reuse branch (which gates on is_process_alive) treats
    the session as alive instead of relying on AsyncMock attribute truthiness."""

    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        m = AsyncMock()
        m.start = AsyncMock()
        m.shutdown = AsyncMock()
        m.is_process_alive = lambda: True
        m.is_alive = lambda: True
        m.context_usage_pct = lambda: 0.0
        m.has_active_turn = lambda: False
        return m

    return factory


class TestSessionManager:
    @pytest.mark.asyncio
    async def test_reinjection_flag_is_one_shot(self, cfg):
        """mark → consume returns True once, then False. If it did not clear,
        every turn after a compaction would re-pay the skills-index cost."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("thread1")
        mgr.release("thread1")

        assert mgr.consume_needs_reinjection("thread1") is False, "unset by default"
        mgr.mark_needs_reinjection("thread1")
        assert mgr.consume_needs_reinjection("thread1") is True, "first read sees it"
        assert mgr.consume_needs_reinjection("thread1") is False, "cleared on read"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_reinjection_helpers_tolerate_an_unknown_key(self, cfg):
        """A compaction callback can fire for a session that has since been
        evicted; neither helper may raise."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr.mark_needs_reinjection("never-existed")
        assert mgr.consume_needs_reinjection("never-existed") is False
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_compaction_marks_reinjection_without_any_callback(self, cfg):
        """The mark lives at the compaction chokepoint, not in one surface.

        Placing it in DashboardState._on_compacted missed every channel-born
        session (and dashboard sessions with no open tab, whose branch returns
        before the callback body). Marking here covers all surfaces and works
        even when no callback is registered at all.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("thread1")
        mgr.release("thread1")
        assert mgr._on_compacted is None, "precondition: no callback registered"

        await mgr._fire_compact_callback("thread1", 90.0, success=True)

        assert mgr.consume_needs_reinjection("thread1") is True
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_failed_compaction_does_not_mark_reinjection(self, cfg):
        """A compaction that failed did not drop the context, so there is
        nothing to re-inject."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("thread1")
        mgr.release("thread1")

        await mgr._fire_compact_callback("thread1", 90.0, success=False)

        assert mgr.consume_needs_reinjection("thread1") is False
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_recycle_does_not_mark_reinjection(self, cfg):
        """A recycle reports success=True but is NOT a compaction.

        Recycling destroys the session; its successor cold-starts and gets the
        index through the normal new-session context. The dangerous case is
        `_recycle_held`'s "entry already replaced" branch: without the guard the
        mark would land on the fresh replacement via `_sessions.get(key)`,
        making an un-compacted session re-inject a redundant index.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("thread1")
        mgr.release("thread1")
        replacement = mgr._sessions[mgr._fold_key("thread1")]

        # Stand in for the in-flight recycle of the session that was REPLACED by
        # this one -- _recycle_held holds the key in _recycling across its
        # success callback.
        mgr._recycling["thread1"] = object()  # type: ignore[assignment]
        try:
            await mgr._fire_compact_callback("thread1", 90.0, success=True)
        finally:
            mgr._recycling.pop("thread1", None)

        assert replacement.needs_context_reinjection is False, (
            "a recycle must not flag the fresh replacement session"
        )
        assert mgr.consume_needs_reinjection("thread1") is False
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_creates_session(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, is_new, _resumed = await mgr.get_or_create("thread1")

        assert is_new is True
        assert mgr.count == 1
        provider.start.assert_awaited_once()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_reuses_session(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        p1, new1, _ = await mgr.get_or_create("thread1")
        mgr.release("thread1")
        p2, new2, _ = await mgr.get_or_create("thread1")
        mgr.release("thread1")

        assert p1 is p2
        assert new1 is True
        assert new2 is False
        assert mgr.count == 1
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_separate_sessions_per_key(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("t1")
        await mgr.get_or_create("t2")

        assert mgr.count == 2
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_remove_shuts_down_client(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("thread1")
        await mgr.remove("thread1")

        assert mgr.count == 0
        provider.shutdown.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_all(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("t1")
        mgr.release("t1")
        await mgr.get_or_create("t1")  # same key
        mgr.release("t1")
        await mgr.close_all()

        assert mgr.count == 0

    @pytest.mark.asyncio
    async def test_reset_removes_session(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("thread1")
        await mgr.reset("thread1")

        assert mgr.count == 0
        provider.shutdown.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_held_session_does_not_block_other_sessions(self, cfg):
        """A SAME-KEY second acquirer parked on session A's held semaphore must
        NOT block get_or_create for a DIFFERENT session B. This is the actual
        lock-ordering freeze: the fast path acquired the per-session semaphore
        while holding the global self._lock, so a second caller for A — wedged
        on A's semaphore — pinned self._lock and froze EVERY other session.

        This test FAILS on the pre-fix code (B hangs); it only passes because
        the fast path now claims under the lock and acquires the semaphore
        after releasing it. The earlier version of this test parked the second
        caller on a *different* key, so it never pinned the lock and passed even
        on the buggy code — it did not guard the fix."""
        mgr = SessionManager(cfg, provider_factory=_alive_provider_factory())

        # Caller 1 holds A's semaphore (a turn in flight, not yet released).
        await mgr.get_or_create("A")

        # Caller 2 on the SAME key takes the fast path (A exists + alive) and
        # blocks on A's held semaphore. On the buggy code it blocks while
        # holding self._lock — the freeze.
        a2 = asyncio.create_task(mgr.get_or_create("A"))
        await asyncio.sleep(0.1)
        assert not a2.done()  # correctly waiting on A's semaphore

        # With self._lock pinned by a2 (buggy) this hangs; with the fix a2
        # released the lock before blocking, so B cold-starts freely.
        b = asyncio.create_task(mgr.get_or_create("B"))
        provider_b, is_new_b, _ = await asyncio.wait_for(b, timeout=3.0)
        assert provider_b is not None
        assert is_new_b is True

        a2.cancel()  # unwedge the parked same-key acquirer

    @pytest.mark.asyncio
    async def test_cold_start_race_loser_does_not_block_other_sessions(self, cfg):
        """The cold-start variant of the lock-ordering freeze (Concern #1).

        Two callers cold-start the SAME new key concurrently. The race loser
        hits the 'another task won the race' branch and must NOT acquire the
        winner's held semaphore while holding self._lock — doing so pins the
        global lock and freezes every other session, exactly like the fast-path
        bug but in a branch the original CR diff never touched.

        FAILS on the pre-fix cold-start path (a different key hangs while the
        loser is wedged under the lock)."""
        start_gate = asyncio.Event()

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            m = AsyncMock()

            async def _start():
                await start_gate.wait()  # park both cold-starts to widen the race

            m.start = _start
            m.shutdown = AsyncMock()
            m.is_process_alive = lambda: True
            m.is_alive = lambda: True
            m.context_usage_pct = lambda: 0.0
            return m

        mgr = SessionManager(cfg, provider_factory=factory)

        # Both pass the fast path (no existing session) and park in start().
        c1 = asyncio.create_task(mgr.get_or_create("A"))
        c2 = asyncio.create_task(mgr.get_or_create("A"))
        await asyncio.sleep(0.1)
        start_gate.set()  # release both; they serialize on the registration lock

        # Exactly one wins, registers A, and returns holding A's semaphore. The
        # loser reaches the won-race branch and parks on that held semaphore.
        done, pending = await asyncio.wait({c1, c2}, timeout=3.0)
        assert len(done) == 1  # winner returned; loser is wedged on the semaphore
        assert len(pending) == 1

        # While the loser is wedged, a DIFFERENT key must still cold-start. On
        # the buggy cold-start path the loser holds self._lock — this hangs.
        b = asyncio.create_task(mgr.get_or_create("B"))
        provider_b, is_new_b, _ = await asyncio.wait_for(b, timeout=3.0)
        assert provider_b is not None
        assert is_new_b is True

        for t in pending:
            t.cancel()

    @pytest.mark.asyncio
    async def test_same_session_still_serializes(self, cfg):
        """Sanity: the per-session semaphore still serializes the SAME key —
        a second get_or_create on a held session blocks until release."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("A")  # holds A's semaphore

        second = asyncio.create_task(mgr.get_or_create("A"))
        await asyncio.sleep(0.2)
        assert not second.done()  # blocked on A's semaphore, as intended
        mgr.release("A")  # let the first holder's turn "finish"
        provider, _, _ = await asyncio.wait_for(second, timeout=3.0)
        assert provider is not None
        mgr.release("A")

    @pytest.mark.asyncio
    async def test_stale_between_claim_and_acquire_cold_starts_and_reaps(self, cfg):
        """Covers the stale-between-claim-and-acquire branch (the riskiest new
        logic in Option A). Caller 1 holds A's semaphore; A's provider then dies.
        Caller 2 claims A (still in the dict) under the lock, blocks on the
        semaphore, and on acquire re-validates: provider dead -> must evict +
        await shutdown() on the dead provider AND cold-start a fresh one."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())

        p1, _is_new, _ = await mgr.get_or_create("A")  # caller 1 holds semaphore

        # Caller 2 must claim A while it is STILL ALIVE (so it takes the claim +
        # wait-on-semaphore path, not the in-lock dead-provider eviction), then
        # find it dead only AFTER acquiring the semaphore. Park caller 2 on the
        # semaphore first, THEN kill A's provider, THEN release.
        second = asyncio.create_task(mgr.get_or_create("A"))
        await asyncio.sleep(0.2)
        assert not second.done()  # blocked on A's semaphore behind caller 1

        # A's process dies between claim and acquire.
        p1.is_process_alive = lambda: False
        p1.is_alive = lambda: False

        mgr.release("A")  # caller 1's turn ends; caller 2 acquires + re-validates
        p2, is_new2, _ = await asyncio.wait_for(second, timeout=3.0)

        assert p2 is not p1  # cold-started a fresh provider
        assert is_new2 is True  # reported as new
        p1.shutdown.assert_awaited()  # dead provider was reaped
        mgr.release("A")


class TestWarmPool:
    """Tests for warm session pool and background session."""

    @pytest.mark.asyncio
    async def test_start_pool_creates_background(self, cfg):
        """start_pool() creates background session."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.start_pool()

        assert BACKGROUND_KEY in mgr._sessions
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_cold_start_for_new_session(self, cfg):
        """get_or_create cold-starts a new session."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.start_pool()

        provider, is_new, _resumed = await mgr.get_or_create("dashboard:chat-1")
        assert is_new is True
        assert provider is not None
        provider.start.assert_awaited_once()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_background_session_reused(self, cfg):
        """BACKGROUND_KEY returns the same provider on repeated calls."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.start_pool()

        p1, _, _ = await mgr.get_or_create(BACKGROUND_KEY)
        mgr.release(BACKGROUND_KEY)
        p2, _, _ = await mgr.get_or_create(BACKGROUND_KEY)
        mgr.release(BACKGROUND_KEY)

        assert p1 is p2
        p1.start.assert_awaited_once()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_background_session_not_expired(self, cfg):
        """Background session is never expired by idle cleanup."""
        import time

        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.start_pool()

        mgr._sessions[BACKGROUND_KEY].last_used = time.monotonic() - 9999
        await mgr._expire_idle(1)

        assert BACKGROUND_KEY in mgr._sessions
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_channel_session_not_expired_by_idle(self, cfg):
        """Channel-agent sessions survive idle expiry (managed by channel lifecycle)."""
        import time

        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.start_pool()

        key = "channel:abc123:agent1"
        mgr._sessions[key] = mgr._sessions[BACKGROUND_KEY].__class__.__new__(
            mgr._sessions[BACKGROUND_KEY].__class__
        )
        mgr._sessions[key].__dict__.update(mgr._sessions[BACKGROUND_KEY].__dict__)
        mgr._sessions[key].last_used = time.monotonic() - 9999

        await mgr._expire_idle(1)

        assert key in mgr._sessions
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_close_all_shuts_down_sessions(self, cfg):
        """close_all() shuts down all active sessions."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.start_pool()
        await mgr.get_or_create("chat-1")

        await mgr.close_all()
        assert mgr.count == 0

    @pytest.mark.asyncio
    async def test_start_pool_idempotent(self, cfg):
        """Calling start_pool() twice is a no-op."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.start_pool()

        await mgr.start_pool()  # should be no-op
        assert BACKGROUND_KEY in mgr._sessions
        await mgr.close_all()


class TestWorkflowPoolStateless:
    """Warm workflow-pool workers (``wf-pool:`` keys) must be treated as
    stateless — never persist a session_map entry and never attempt a
    ``session/load`` resume. Otherwise the pool's hard-reset fallback would
    resume the PRIOR task's transcript into the next task, leaking cross-task
    context and violating the pool's isolation guarantee."""

    def test_wf_pool_prefix_is_stateless(self):
        from kiro_crew.session import _STATELESS_PREFIXES

        assert any("wf-pool:".startswith(p) for p in _STATELESS_PREFIXES)
        assert "wf-pool:run-1:0".startswith(
            next(p for p in _STATELESS_PREFIXES if "wf-pool:".startswith(p))
        )

    @pytest.mark.asyncio
    async def test_wf_pool_key_skips_resume_lookup(self, cfg):
        """A ``wf-pool:`` key must NOT consult the session_map for a resume sid —
        stateless keys skip the lookup entirely (guarded to catch regressions if
        the prefix is dropped from _STATELESS_PREFIXES)."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        # Spy on the resume lookup: it must never be called for a stateless key.
        mgr._session_map.get = MagicMock(return_value="stale-sid")  # type: ignore[method-assign]
        await mgr.get_or_create("wf-pool:run-1:0")
        mgr._session_map.get.assert_not_called()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_non_pool_key_still_consults_resume_lookup(self, cfg):
        """Control: a normal conversational key (not stateless) DOES consult the
        session_map for a resume sid — proving the skip above is specific to the
        stateless classification, not a blanket no-op."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._session_map.get = MagicMock(return_value=None)  # type: ignore[method-assign]
        await mgr.get_or_create("dashboard:chat-9")
        mgr._session_map.get.assert_called()
        await mgr.close_all()


class TestHeartbeatStateless:
    """``_hb`` must be treated as stateless alongside ``_bg``.

    Heartbeat's published contract (``config/prompt.md``) is "fresh context
    each cycle", and every entry is re-read from ``HEARTBEAT.md`` each cycle,
    so a resumed transcript supplies nothing the next cycle depends on while
    costing input tokens on every tick. Resuming is also actively wrong: for a
    watch task the external system is the source of truth, and unrelated
    queued tasks would inherit each other's reasoning.
    """

    @pytest.mark.asyncio
    async def test_heartbeat_key_skips_resume_lookup(self, cfg):
        """``_hb`` must NOT consult the session_map for a resume sid."""
        from kiro_crew.session import HEARTBEAT_KEY

        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._session_map.get = MagicMock(return_value="stale-sid")  # type: ignore[method-assign]
        await mgr.get_or_create(HEARTBEAT_KEY)
        mgr._session_map.get.assert_not_called()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_background_key_also_stateless(self, cfg):
        """Control: ``_bg`` was already stateless — ``_hb`` now matches it."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._session_map.get = MagicMock(return_value="stale-sid")  # type: ignore[method-assign]
        await mgr.get_or_create(BACKGROUND_KEY)
        mgr._session_map.get.assert_not_called()
        await mgr.close_all()


class TestRecycleBackground:
    """Tests for background session context overflow recycling."""

    @pytest.mark.asyncio
    async def test_recycle_on_high_context(self, cfg):
        """Background session is recycled when context >= 70%."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.start_pool()

        old_provider = mgr._sessions[BACKGROUND_KEY].provider
        # Simulate high context
        old_provider.context_usage_pct = lambda: 75.0

        await mgr.recycle_background()

        # Old provider should have been shut down
        old_provider.shutdown.assert_awaited_once()
        # New session should exist
        assert BACKGROUND_KEY in mgr._sessions
        new_provider = mgr._sessions[BACKGROUND_KEY].provider
        assert new_provider is not old_provider
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_recycle_blind_fallback(self, cfg):
        """Background session is recycled after 40 prompts with no metadata."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.start_pool()

        old_provider = mgr._sessions[BACKGROUND_KEY].provider
        old_provider.context_usage_pct = lambda: 0.0  # no metadata
        mgr._sessions[BACKGROUND_KEY].prompt_count = 45

        await mgr.recycle_background()

        old_provider.shutdown.assert_awaited_once()
        assert BACKGROUND_KEY in mgr._sessions
        assert mgr._sessions[BACKGROUND_KEY].provider is not old_provider
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_no_recycle_when_low_context(self, cfg):
        """Background session is NOT recycled when context is low."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.start_pool()

        old_provider = mgr._sessions[BACKGROUND_KEY].provider
        old_provider.context_usage_pct = lambda: 30.0

        await mgr.recycle_background()

        # Should NOT have been shut down
        old_provider.shutdown.assert_not_awaited()
        assert mgr._sessions[BACKGROUND_KEY].provider is old_provider
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_recycle_no_background_session(self, cfg):
        """recycle_background() is no-op when no background session exists."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        # Don't start pool — no background session
        await mgr.recycle_background()  # should not raise
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_blind_fallback_counts_its_own_prompts(self, cfg):
        """The blind fallback must fire on its own counting.

        ``check_context_usage`` is a chat-turn hook and never runs for
        BACKGROUND_KEY, so if ``recycle_background`` does not count the turn the
        counter stays at 0 forever and the 40-prompt fallback is dead code.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.start_pool()

        provider = mgr._sessions[BACKGROUND_KEY].provider
        provider.context_usage_pct = lambda: 0.0  # backend reports no metadata
        provider.context_usage_unknown = lambda: False

        for _ in range(_BG_BLIND_RECYCLE_PROMPTS - 1):
            await mgr.recycle_background()

        assert mgr._sessions[BACKGROUND_KEY].provider is provider
        assert mgr._sessions[BACKGROUND_KEY].prompt_count == _BG_BLIND_RECYCLE_PROMPTS - 1

        await mgr.recycle_background()

        provider.shutdown.assert_awaited_once()
        assert mgr._sessions[BACKGROUND_KEY].provider is not provider
        # The replacement starts its own count.
        assert mgr._sessions[BACKGROUND_KEY].prompt_count == 0
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_just_compacted_zero_pct_does_not_suppress_recycle(self, cfg):
        """A post-compaction 0% is "unknown", not "empty".

        The backend zeroes the percentage when it compacts in place, which is
        byte-identical to a brand-new session. Reading it as empty leaves a
        session that just hit its ceiling in place to be compacted again — and
        each compaction is a billed summarization turn over the whole transcript.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.start_pool()

        provider = mgr._sessions[BACKGROUND_KEY].provider
        provider.context_usage_pct = lambda: 0.0
        provider.context_usage_unknown = lambda: True

        # One turn — far below the blind threshold, so only the unknown signal
        # can trigger the recycle.
        await mgr.recycle_background()

        provider.shutdown.assert_awaited_once()
        assert mgr._sessions[BACKGROUND_KEY].provider is not provider
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_acp_prompt_stats_flag_post_compaction_zero_as_unknown(self):
        """The provider-level signal the recycle decision rides on."""
        stats = AcpPromptStats(context_pct=88.0, context_used_tokens=170_000)
        assert stats.context_pct_unknown is False

        stats.reset_after_compaction()
        assert stats.context_pct == 0.0
        assert stats.context_pct_unknown is True

        # Survives the per-turn stats re-init...
        carried = stats.carry_over()
        assert carried.context_pct_unknown is True

        # ...and clears as soon as the backend reports a real number.
        carried.note_pct_reported()
        assert carried.context_pct_unknown is False

    @pytest.mark.asyncio
    async def test_recycle_never_kills_a_turn_that_started_after_release(self, cfg):
        """A turn taken in the release→recycle gap must not be torn down.

        Every call site releases the turn semaphore on the line before calling
        ``recycle_background``, so a waiter can start a turn in that gap. If the
        recycle decides and shuts down outside the semaphore it SIGKILLs that
        live turn.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.start_pool()

        sess = mgr._sessions[BACKGROUND_KEY]
        old_provider = sess.provider
        old_provider.context_usage_pct = lambda: 95.0
        old_provider.context_usage_unknown = lambda: False

        killed_mid_turn: list[str] = []
        turn_providers: list[object] = []
        recycle_done = asyncio.Event()

        async def _waiter_turn() -> None:
            # Mirrors _ProviderBgSession.prompt: take the turn semaphore, then
            # stream on whatever provider the session holds at that moment.
            await sess.semaphore.acquire()
            try:
                provider = sess.provider
                turn_providers.append(provider)
                # Stay in the turn until the recycle attempt finishes. Deadline
                # is a yield budget, not wall-clock, so the interleaving is
                # deterministic.
                for _ in range(200):
                    if provider.shutdown.await_count:
                        killed_mid_turn.append("provider shut down mid-turn")
                        break
                    if recycle_done.is_set():
                        break
                    await asyncio.sleep(0)
            finally:
                sess.semaphore.release()

        async def _recycle() -> None:
            try:
                await mgr.recycle_background()
            finally:
                recycle_done.set()

        # Reproduce the real call-site interleaving: a turn completes and
        # releases, a waiter wins the gap, THEN the recycle runs.
        await mgr.get_or_create(BACKGROUND_KEY)
        mgr.release(BACKGROUND_KEY)
        waiter = asyncio.create_task(_waiter_turn())
        await asyncio.sleep(0)  # let the waiter take the semaphore

        recycle = asyncio.create_task(_recycle())
        await recycle
        await waiter

        assert killed_mid_turn == []
        # The turn ran to completion on the provider it picked up...
        assert turn_providers == [old_provider]
        # ...and the recycle still happened, once the turn was done.
        assert sess.provider is not old_provider
        old_provider.shutdown.assert_awaited_once()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_turn_starting_after_the_recycle_gets_the_replacement(self, cfg):
        """The session is recycled in place, so a holder is routed to the new
        provider rather than to the torn-down one."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.start_pool()

        sess = mgr._sessions[BACKGROUND_KEY]
        old_provider = sess.provider
        old_provider.context_usage_pct = lambda: 95.0
        old_provider.context_usage_unknown = lambda: False

        await mgr.recycle_background()

        # A caller that captured the session before the recycle still finds a
        # live provider on it, and the registry entry did not go absent.
        assert mgr._sessions[BACKGROUND_KEY] is sess
        await sess.semaphore.acquire()
        try:
            assert sess.provider is not old_provider
            assert sess.provider.shutdown.await_count == 0
        finally:
            sess.semaphore.release()
        # Conversation state describing the old transcript does not carry over.
        assert sess.prompt_count == 0
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_failed_replacement_spawn_keeps_the_working_provider(self, cfg):
        """A spawn failure must not leave _bg with no provider at all."""
        calls = {"n": 0}
        base = _mock_provider_factory()

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("no capacity")
            return base(session_key, agent, channel_id, **kwargs)

        mgr = SessionManager(cfg, provider_factory=factory)
        await mgr.start_pool()

        sess = mgr._sessions[BACKGROUND_KEY]
        old_provider = sess.provider
        old_provider.context_usage_pct = lambda: 95.0
        old_provider.context_usage_unknown = lambda: False

        await mgr.recycle_background()

        assert sess.provider is old_provider
        old_provider.shutdown.assert_not_awaited()
        # The semaphore is handed back even on the failure path.
        assert not sess.semaphore.locked()
        await mgr.close_all()


class TestCancelRaceCondition:
    """Tests for process leak prevention when CancelledError fires during get_or_create."""

    @pytest.mark.asyncio
    async def test_cancel_during_start_kills_provider(self, cfg):
        """CancelledError during provider.start() kills the process synchronously."""
        mock_provider = AsyncMock()
        mock_provider.start = AsyncMock(side_effect=asyncio.CancelledError)
        mock_provider._client = AsyncMock()
        mock_provider._client._pid = 99999

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            return mock_provider

        mgr = SessionManager(cfg, provider_factory=factory)

        import kiro_crew.session as _sess_mod

        with patch.object(_sess_mod, "_sync_kill_provider") as mock_kill:
            with pytest.raises(asyncio.CancelledError):
                await mgr.get_or_create("test-cancel")

            mock_kill.assert_called_once_with(mock_provider)

        # Session must NOT be registered
        assert mgr.count == 0
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_cancel_after_start_before_registration_kills_provider(self, cfg):
        """CancelledError after start() but before _sessions[key] kills the process."""
        mock_provider = AsyncMock()
        mock_provider.start = AsyncMock()  # succeeds
        mock_provider.context_usage_pct = lambda: 0.0
        mock_provider._client = AsyncMock()
        mock_provider._client._pid = 88888
        mock_provider.is_alive.return_value = True

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            return mock_provider

        mgr = SessionManager(cfg, provider_factory=factory)
        original_lock = mgr._lock

        class CancelOnSecondLock:
            """First acquire (fast path) passes through; second (registration) cancels."""

            def __init__(self):
                self._calls = 0

            async def __aenter__(self):
                self._calls += 1
                if self._calls >= 2:
                    raise asyncio.CancelledError
                return await original_lock.__aenter__()

            async def __aexit__(self, *a):
                if self._calls < 2:
                    return await original_lock.__aexit__(*a)

        import kiro_crew.session as _sess_mod

        with patch.object(_sess_mod, "_sync_kill_provider") as mock_kill:
            mgr._lock = CancelOnSecondLock()
            with pytest.raises(asyncio.CancelledError):
                await mgr.get_or_create("test-cancel-2")

            mock_kill.assert_called_once_with(mock_provider)

        mgr._lock = original_lock
        assert mgr.count == 0
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_normal_path_unaffected(self, cfg):
        """Normal get_or_create still works after the cancel-safety changes."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, is_new, _ = await mgr.get_or_create("normal-session")

        assert is_new is True
        assert mgr.count == 1
        provider.start.assert_awaited_once()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_model_forwarded_to_factory(self, cfg):
        """model param is forwarded to factory as model_override."""
        captured = {}

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            captured.update(kwargs)
            m = AsyncMock()
            m.start = AsyncMock()
            m.context_usage_pct = lambda: 0.0
            m.is_alive.return_value = True
            return m

        mgr = SessionManager(cfg, provider_factory=factory)
        await mgr.get_or_create("test-model", model="claude-sonnet")
        assert captured["model_override"] == "claude-sonnet"
        await mgr.close_all()


class TestDeadProviderCleanup:
    """Tests for orphaned child process cleanup when a dead provider is detected."""

    @staticmethod
    def _make_provider(*, alive: bool = True):
        """Create a mock provider with sync is_alive."""
        from unittest.mock import MagicMock

        m = AsyncMock()
        m.start = AsyncMock()
        m.shutdown = AsyncMock()
        m.context_usage_pct = MagicMock(return_value=0.0)
        m.is_alive = MagicMock(return_value=alive)
        m.is_process_alive = MagicMock(return_value=alive)
        return m

    @pytest.mark.asyncio
    async def test_dead_provider_calls_shutdown(self, cfg):
        """When is_alive() returns False, shutdown() is called on the stale provider."""
        dead_provider = self._make_provider(alive=True)
        call_count = 0

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            nonlocal call_count
            call_count += 1
            return dead_provider if call_count == 1 else self._make_provider()

        mgr = SessionManager(cfg, provider_factory=factory)
        await mgr.get_or_create("sess1")
        mgr.release("sess1")

        dead_provider.is_alive.return_value = False
        dead_provider.is_process_alive.return_value = False
        _, is_new, _ = await mgr.get_or_create("sess1")
        assert is_new is True
        dead_provider.shutdown.assert_awaited_once()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_dead_provider_shutdown_exception_does_not_propagate(self, cfg):
        """If shutdown() raises on a dead provider, get_or_create still succeeds."""
        dead_provider = self._make_provider(alive=True)
        dead_provider.shutdown = AsyncMock(side_effect=OSError("kill failed"))
        call_count = 0

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            nonlocal call_count
            call_count += 1
            return dead_provider if call_count == 1 else self._make_provider()

        mgr = SessionManager(cfg, provider_factory=factory)
        await mgr.get_or_create("sess1")
        mgr.release("sess1")

        dead_provider.is_alive.return_value = False
        dead_provider.is_process_alive.return_value = False
        _, is_new, _ = await mgr.get_or_create("sess1")
        assert is_new is True
        assert mgr.count == 1
        dead_provider.shutdown.assert_awaited_once()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_dead_provider_removed_from_sessions(self, cfg):
        """Dead provider session is removed and replaced by a fresh one."""
        dead_provider = self._make_provider(alive=True)
        fresh_provider = self._make_provider()
        call_count = 0

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            nonlocal call_count
            call_count += 1
            return dead_provider if call_count == 1 else fresh_provider

        mgr = SessionManager(cfg, provider_factory=factory)
        await mgr.get_or_create("sess1")
        mgr.release("sess1")

        dead_provider.is_alive.return_value = False
        dead_provider.is_process_alive.return_value = False
        provider, is_new, _ = await mgr.get_or_create("sess1")
        assert provider is fresh_provider
        assert is_new is True
        assert mgr.count == 1
        await mgr.close_all()


class TestIsProviderAlive:
    """Tests for is_provider_alive preferring is_process_alive over is_alive."""

    @pytest.mark.asyncio
    async def test_uses_is_process_alive_when_available(self, cfg):
        provider = TestDeadProviderCleanup._make_provider(alive=True)
        provider.is_process_alive.return_value = True
        mgr = SessionManager(cfg, provider_factory=lambda *a, **kw: provider)
        await mgr.get_or_create("sess1")
        mgr.release("sess1")
        result = await mgr.is_provider_alive("sess1")
        assert result is True
        provider.is_process_alive.assert_called()
        await mgr.close_all()


class TestApprovalPolicy:
    """Tests for approval policy get/set on sessions."""

    @pytest.mark.asyncio
    async def test_set_and_get_approval_policy(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("thread1")
        mgr.release("thread1")

        mgr.set_approval_policy("thread1", "auto")
        assert mgr.get_approval_policy("thread1") == "auto"

        mgr.set_approval_policy("thread1", "")
        assert mgr.get_approval_policy("thread1") == ""
        await mgr.close_all()

    def test_get_approval_policy_missing_session(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        assert mgr.get_approval_policy("nonexistent") == ""

    def test_set_approval_policy_missing_session(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr.set_approval_policy("nonexistent", "auto")  # should not raise

    @pytest.mark.asyncio
    async def test_approval_policy_propagated_on_create(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("thread1", approval_policy="auto")
        mgr.release("thread1")
        assert mgr.get_approval_policy("thread1") == "auto"
        await mgr.close_all()


class TestGetAgent:
    """Tests for get_agent() on SessionManager."""

    @pytest.mark.asyncio
    async def test_get_agent_returns_agent_name(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("thread1", agent="my-agent")
        mgr.release("thread1")
        assert mgr.get_agent("thread1") == "my-agent"
        await mgr.close_all()

    def test_get_agent_missing_session_returns_empty(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        assert mgr.get_agent("nonexistent") == ""

    @pytest.mark.asyncio
    async def test_get_agent_no_agent_returns_empty(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("thread1")
        mgr.release("thread1")
        assert mgr.get_agent("thread1") == ""
        await mgr.close_all()


class TestOrphanedDashboardSessions:
    """Tests for orphaned dashboard session detection in _expire_idle."""

    @pytest.mark.asyncio
    async def test_expire_idle_reaps_orphaned_dashboard_session(self, cfg):
        """Dashboard session whose slot no longer exists is reaped immediately."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("dashboard:tab1")
        mgr.release("dashboard:tab1")
        # Mark tab2 as the only active slot — tab1 is orphaned
        mgr.set_active_dashboard_slots({"dashboard:tab2"})
        await mgr._expire_idle(9999)  # high timeout so idle doesn't trigger

        assert "dashboard:tab1" not in mgr._sessions
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_expire_idle_skips_uninitialized_slots(self, cfg):
        """When _active_dashboard_slots is None, no orphan reaping occurs."""
        import time

        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("dashboard:tab1")
        mgr.release("dashboard:tab1")
        # Don't call set_active_dashboard_slots — stays None
        mgr._sessions["dashboard:tab1"].last_used = time.monotonic()
        await mgr._expire_idle(9999)

        assert "dashboard:tab1" in mgr._sessions
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_expire_idle_preserves_active_dashboard_session(self, cfg):
        """Dashboard session whose slot still exists is NOT reaped."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("dashboard:tab1")
        mgr.release("dashboard:tab1")
        mgr.set_active_dashboard_slots({"dashboard:tab1"})
        await mgr._expire_idle(9999)

        assert "dashboard:tab1" in mgr._sessions
        await mgr.close_all()


class TestStopTurn:
    """Tests for stop_turn(), _eager_respawn(), and cancel_current backcompat."""

    @pytest.mark.asyncio
    async def test_stop_turn_idle_no_session(self, cfg):
        """No session for key → returns 'idle'."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        result = await mgr.stop_turn("nonexistent")
        assert result == "idle"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_stop_turn_soft_ack(self, cfg):
        """Provider returns 'acked' → stop_turn returns 'soft', on_soft called."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("key1")
        mgr.release("key1")

        provider.cancel = AsyncMock(return_value="acked")
        on_soft = AsyncMock()
        on_hard = AsyncMock()

        result = await mgr.stop_turn("key1", on_soft=on_soft, on_hard=on_hard)

        assert result == "soft"
        on_soft.assert_awaited_once()
        on_hard.assert_not_awaited()
        # Session should still exist (not reset)
        assert mgr.has_session("key1")
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_stop_turn_hard_on_timeout(self, cfg):
        """Provider returns 'timeout' → stop_turn returns 'hard', on_hard called."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("key1")
        mgr.release("key1")

        provider.cancel = AsyncMock(return_value="timeout")
        on_soft = AsyncMock()
        on_hard = AsyncMock()

        result = await mgr.stop_turn("key1", on_soft=on_soft, on_hard=on_hard)

        assert result == "hard"
        on_soft.assert_not_awaited()
        on_hard.assert_awaited_once()
        # Session should have been reset
        assert not mgr.has_session("key1")
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_stop_turn_hard_on_error(self, cfg):
        """Provider returns 'error' → stop_turn returns 'hard'."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("key1")
        mgr.release("key1")

        provider.cancel = AsyncMock(return_value="error")
        on_hard = AsyncMock()

        result = await mgr.stop_turn("key1", on_hard=on_hard)

        assert result == "hard"
        on_hard.assert_awaited_once()
        assert not mgr.has_session("key1")
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_stop_turn_force_skips_cancel(self, cfg):
        """force=True goes straight to reset without calling provider.cancel."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("key1")
        mgr.release("key1")

        provider.cancel = AsyncMock(return_value="acked")
        on_hard = AsyncMock()

        result = await mgr.stop_turn("key1", force=True, on_hard=on_hard)

        assert result == "hard"
        provider.cancel.assert_not_awaited()
        on_hard.assert_awaited_once()
        assert not mgr.has_session("key1")
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_stop_turn_clears_queue_first(self, cfg):
        """stop_turn clears the message queue regardless of outcome."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("key1")
        mgr.release("key1")

        # Populate queue
        mgr.enqueue("key1", "ts1", "msg1", force=True)
        mgr.enqueue("key1", "ts2", "msg2", force=True)

        provider.cancel = AsyncMock(return_value="acked")
        await mgr.stop_turn("key1")

        # Queue should be empty
        assert mgr.dequeue("key1") is None
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_stop_turn_idle_still_clears_queue(self, cfg):
        """Even when provider returns 'no_turn', queue is cleared."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("key1")
        mgr.release("key1")

        mgr.enqueue("key1", "ts1", "msg1", force=True)

        provider.cancel = AsyncMock(return_value="no_turn")
        result = await mgr.stop_turn("key1")

        assert result == "idle"
        assert mgr.dequeue("key1") is None
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_eager_respawn_called(self, cfg):
        """Hard path schedules _eager_respawn via asyncio.create_task."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("key1")
        mgr.release("key1")

        provider.cancel = AsyncMock(return_value="timeout")

        with patch.object(mgr, "_eager_respawn", new_callable=AsyncMock) as mock_respawn:
            await mgr.stop_turn("key1")
            # Allow the created task to run
            await asyncio.sleep(0)
            mock_respawn.assert_awaited_once_with("key1")

        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_eager_respawn_failure_logged(self, cfg, caplog):
        """_eager_respawn swallows exceptions and logs at debug."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())

        with patch.object(
            mgr, "get_or_create", new_callable=AsyncMock, side_effect=RuntimeError("boom")
        ):
            with caplog.at_level(logging.DEBUG, logger="kiro_crew.session"):
                await mgr._eager_respawn("key1")

        assert "Eager respawn failed" in caplog.text
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_eager_respawn_releases_semaphore(self, cfg):
        """_eager_respawn must release the semaphore acquired by get_or_create,
        else the next user message deadlocks waiting on it."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        # Prime the session so get_or_create takes the fast path.
        provider, _, _ = await mgr.get_or_create("key1")
        mgr.release("key1")
        sess = mgr._sessions["key1"]
        # Sanity: semaphore is full (1 permit available) before respawn.
        assert sess.semaphore.locked() is False

        await mgr._eager_respawn("key1")

        # After respawn the semaphore MUST be released, otherwise the next
        # caller of get_or_create would hang on sess.semaphore.acquire().
        assert sess.semaphore.locked() is False
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_cancel_current_backcompat_default(self, cfg):
        """Existing cancel_current(key) call with no kwargs still works."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("key1")
        mgr.release("key1")

        provider.cancel = AsyncMock(return_value="no_turn")
        result = await mgr.cancel_current("key1")

        assert result == "no_turn"
        provider.cancel.assert_awaited_once_with(wait_ack_timeout=0.0)
        await mgr.close_all()


class TestCompactCallback:
    """Tests for the compact callback wiring on SessionManager.

    Covers set_compact_callback registration, pct threading through
    check_context_usage -> _trigger_compaction -> _compact_session, and
    callback fault isolation.
    """

    @pytest.mark.asyncio
    async def test_set_compact_callback_registers_handler(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        cb = AsyncMock()

        mgr.set_compact_callback(cb)

        assert mgr._on_compacted is cb
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_set_compact_callback_none_clears(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr.set_compact_callback(AsyncMock())

        mgr.set_compact_callback(None)

        assert mgr._on_compacted is None
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_set_compact_callback_warns_on_replace(self, cfg, caplog):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr.set_compact_callback(AsyncMock())

        with caplog.at_level(logging.WARNING, logger="kiro_crew.session"):
            mgr.set_compact_callback(AsyncMock())

        assert any("Compact callback already registered" in r.message for r in caplog.records)
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_compact_session_invokes_callback_with_key_and_pct(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("dashboard:chat-1")
        mgr.release("dashboard:chat-1")
        cb = AsyncMock()
        mgr.set_compact_callback(cb)

        await mgr._compact_session("dashboard:chat-1", 92.0)

        cb.assert_awaited_once_with("dashboard:chat-1", 92.0, success=True)
        assert "dashboard:chat-1" not in mgr._sessions
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_compact_session_skips_callback_when_session_absent(self, cfg):
        """No session means no recycle happened, so the callback must not fire."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        cb = AsyncMock()
        mgr.set_compact_callback(cb)

        await mgr._compact_session("dashboard:missing", 91.0)

        cb.assert_not_awaited()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_compact_session_waits_for_inflight_turn(self, cfg):
        """kiro-cli recycle must drain the in-flight turn: while the session
        semaphore is held (turn active) it blocks instead of SIGKILL'ing."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        # get_or_create returns HOLDING the semaphore -> simulates an active turn.
        await mgr.get_or_create("dashboard:chat-1")
        cb = AsyncMock()
        mgr.set_compact_callback(cb)

        task = asyncio.create_task(mgr._compact_session("dashboard:chat-1", 92.0))
        await asyncio.sleep(0.05)
        # Still draining: not recycled, callback not fired.
        assert not task.done()
        assert "dashboard:chat-1" in mgr._sessions
        cb.assert_not_awaited()

        # Turn finishes -> semaphore released -> recycle proceeds.
        mgr.release("dashboard:chat-1")
        await asyncio.wait_for(task, timeout=2)
        assert "dashboard:chat-1" not in mgr._sessions
        cb.assert_awaited_once_with("dashboard:chat-1", 92.0, success=True)
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_compact_session_defers_when_turn_never_drains(self, cfg, caplog, monkeypatch):
        """A still-running turn (semaphore held) must NEVER be killed for
        compaction: after _COMPACT_TIMEOUT_SECS the attempt is deferred —
        session intact, no callback — and re-triggered at the next turn end."""
        monkeypatch.setattr("kiro_crew.session._COMPACT_TIMEOUT_SECS", 0.1)
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        # Hold the semaphore and never release -> simulates a long-running turn.
        provider, _, _ = await mgr.get_or_create("dashboard:chat-1")
        cb = AsyncMock()
        mgr.set_compact_callback(cb)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.session"):
            await asyncio.wait_for(mgr._compact_session("dashboard:chat-1", 92.0), timeout=2)

        # Session survives, the live turn was not killed, and nothing was
        # reported to the user (a deferral is not a failure).
        assert "dashboard:chat-1" in mgr._sessions
        provider.shutdown.assert_not_awaited()
        cb.assert_not_awaited()
        assert any("compaction deferred" in r.message for r in caplog.records)
        # _compacting cleared so the next turn-end check can re-trigger.
        assert "dashboard:chat-1" not in mgr._compacting
        mgr.release("dashboard:chat-1")
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_compact_session_callback_exception_is_logged(self, cfg, caplog):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("dashboard:chat-1")
        mgr.release("dashboard:chat-1")
        cb = AsyncMock(side_effect=RuntimeError("boom"))
        mgr.set_compact_callback(cb)

        with caplog.at_level(logging.ERROR, logger="kiro_crew.session"):
            await mgr._compact_session("dashboard:chat-1", 95.0)

        cb.assert_awaited_once()
        assert any("Compact callback failed" in r.message for r in caplog.records)
        # Session still recycled, compacting flag cleared
        assert "dashboard:chat-1" not in mgr._sessions
        assert "dashboard:chat-1" not in mgr._compacting
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_trigger_compaction_threads_pct_through(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("dashboard:chat-2")
        mgr.release("dashboard:chat-2")
        captured: list[tuple[str, float, bool]] = []

        async def cb(key, pct, *, success):
            captured.append((key, pct, success))

        mgr.set_compact_callback(cb)

        mgr._trigger_compaction("dashboard:chat-2", "context at 92%", 92.0)
        # _trigger_compaction schedules the work as a background task
        await asyncio.gather(*mgr._background_tasks, return_exceptions=True)

        assert captured == [("dashboard:chat-2", 92.0, True)]
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_check_context_usage_fires_callback_with_observed_pct(self, cfg):
        """High pct should flow from check_context_usage through to the callback."""
        cfg.session.autocompact_pct = 90.0
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("dashboard:chat-3")
        mgr.release("dashboard:chat-3")
        provider.context_usage_pct = lambda: 93.0
        captured: list[tuple[str, float, bool]] = []

        async def cb(key, pct, *, success):
            captured.append((key, pct, success))

        mgr.set_compact_callback(cb)

        pct = mgr.check_context_usage("dashboard:chat-3", provider)
        await asyncio.gather(*mgr._background_tasks, return_exceptions=True)

        assert pct == 93.0
        assert captured == [("dashboard:chat-3", 93.0, True)]
        await mgr.close_all()


class TestRecordSuccessFailure:
    """Tests for record_success and record_failure circuit breaker."""

    @pytest.mark.asyncio
    async def test_get_provider_returns_provider(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        assert mgr.get_provider("k1") is provider
        await mgr.close_all()

    def test_get_provider_missing_returns_none(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        assert mgr.get_provider("nonexistent") is None

    @pytest.mark.asyncio
    async def test_pool_size_clamping(self, cfg, caplog):
        cfg.session.pool_size = 999
        with caplog.at_level(logging.WARNING, logger="kiro_crew.session"):
            mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        assert any("exceeds max" in r.message for r in caplog.records)
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_record_success_resets_counter(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.release("k1")
        mgr._sessions["k1"].consecutive_failures = 3
        mgr.record_success("k1")
        assert mgr._sessions["k1"].consecutive_failures == 0
        await mgr.close_all()

    def test_record_success_missing_session(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr.record_success("nonexistent")  # should not raise

    @pytest.mark.asyncio
    async def test_record_failure_increments(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.release("k1")
        tripped = await mgr.record_failure("k1")
        assert tripped is False
        assert mgr._sessions["k1"].consecutive_failures == 1
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_record_failure_trips_circuit_breaker(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.release("k1")
        mgr._sessions["k1"].consecutive_failures = 4  # one below threshold
        tripped = await mgr.record_failure("k1")
        assert tripped is True
        assert not mgr.has_session("k1")
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_record_failure_missing_session(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        tripped = await mgr.record_failure("nonexistent")
        assert tripped is False


class TestMessageQueue:
    """Tests for enqueue, dequeue, cancel_queued, is_cancelled."""

    @pytest.mark.asyncio
    async def test_enqueue_when_busy(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        # semaphore is locked (acquired by get_or_create)
        queued = mgr.enqueue("k1", "ts1", "hello")
        assert queued is True
        mgr.release("k1")
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_enqueue_when_idle_returns_false(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.release("k1")
        queued = mgr.enqueue("k1", "ts1", "hello")
        assert queued is False
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_enqueue_force(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.release("k1")
        queued = mgr.enqueue("k1", "ts1", "hello", force=True)
        assert queued is True
        await mgr.close_all()

    def test_enqueue_missing_session(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        assert mgr.enqueue("nope", "ts1", "hi") is False

    @pytest.mark.asyncio
    async def test_dequeue_fifo(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.enqueue("k1", "ts1", "first")
        mgr.enqueue("k1", "ts2", "second")
        mgr.release("k1")
        result = mgr.dequeue("k1")
        assert result == ("ts1", "first", {})
        result = mgr.dequeue("k1")
        assert result == ("ts2", "second", {})
        assert mgr.dequeue("k1") is None
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_dequeue_skips_cancelled(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.enqueue("k1", "ts1", "first")
        mgr.enqueue("k1", "ts2", "second")
        mgr._sessions["k1"].cancelled.add("ts1")
        mgr.release("k1")
        result = mgr.dequeue("k1")
        assert result == ("ts2", "second", {})
        await mgr.close_all()

    def test_dequeue_missing_session(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        assert mgr.dequeue("nope") is None

    @pytest.mark.asyncio
    async def test_cancel_queued_removes_from_queue(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.enqueue("k1", "ts1", "msg1")
        mgr.enqueue("k1", "ts2", "msg2")
        mgr.release("k1")
        removed = mgr.cancel_queued("k1", "ts1")
        assert removed is True
        result = mgr.dequeue("k1")
        assert result[0] == "ts2"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_cancel_queued_marks_inflight(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        # semaphore locked = something in-flight
        removed = mgr.cancel_queued("k1", "ts_inflight")
        assert removed is False
        assert "ts_inflight" in mgr._sessions["k1"].cancelled
        mgr.release("k1")
        await mgr.close_all()

    def test_cancel_queued_missing_session(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        assert mgr.cancel_queued("nope", "ts1") is False

    @pytest.mark.asyncio
    async def test_is_cancelled_consumes(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.release("k1")
        mgr._sessions["k1"].cancelled.add("ts1")
        assert mgr.is_cancelled("k1", "ts1") is True
        # Second call returns False (consumed)
        assert mgr.is_cancelled("k1", "ts1") is False
        await mgr.close_all()

    def test_is_cancelled_missing_session(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        assert mgr.is_cancelled("nope", "ts1") is False


class TestDrainProviders:
    """Tests for drain_all_providers and drain_warm_pool."""

    @pytest.mark.asyncio
    async def test_drain_all_providers(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.release("k1")
        await mgr.get_or_create("k2")
        mgr.release("k2")
        providers = await mgr.drain_all_providers()
        assert len(providers) == 2
        assert mgr.count == 0
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_drain_all_providers_empty(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        providers = await mgr.drain_all_providers()
        assert providers == []

    @pytest.mark.asyncio
    async def test_drain_warm_pool(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        # Manually put items in the warm pool
        mock_p = AsyncMock()
        mgr._warm_pool.put_nowait((mock_p, "agent1"))
        drained = await mgr.drain_warm_pool()
        assert len(drained) == 1
        assert drained[0] is mock_p
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_drain_warm_pool_empty(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        drained = await mgr.drain_warm_pool()
        assert drained == []


class TestRelease:
    """Tests for release() with subagent cleanup."""

    @pytest.mark.asyncio
    async def test_release_normal_session(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        # Semaphore should be locked after get_or_create
        assert mgr._sessions["k1"].semaphore.locked()
        mgr.release("k1")
        assert not mgr._sessions["k1"].semaphore.locked()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_release_subagent_with_cleanup(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("subagent:abc")
        provider.session_id = "sid-123"
        provider.cleanup_session = AsyncMock()
        mgr.release("subagent:abc", cleanup=True)
        # Allow the ensure_future to run
        await asyncio.sleep(0)
        provider.cleanup_session.assert_awaited_once_with("sid-123")
        await mgr.close_all()

    def test_release_missing_session(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr.release("nonexistent")  # should not raise


class TestResetWithPid:
    """Tests for reset() PID capture and force-kill logic."""

    @pytest.mark.asyncio
    async def test_reset_no_pid_just_shuts_down(self, cfg):
        """reset() with no PID attribute just calls shutdown."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        await mgr.reset("k1")
        provider.shutdown.assert_awaited_once()
        assert not mgr.has_session("k1")

    @pytest.mark.asyncio
    async def test_reset_with_acp_pid_dead_after_shutdown(self, cfg):
        """reset() with ACP PID that dies after shutdown — no force kill."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        # Simulate ACP client with PID
        mock_client = AsyncMock()
        mock_client._pid = 12345
        mock_client._child_pids = {}
        provider._client = mock_client

        with patch("os.kill", side_effect=ProcessLookupError):
            await mgr.reset("k1")

        provider.shutdown.assert_awaited_once()
        assert not mgr.has_session("k1")

    @pytest.mark.asyncio
    async def test_reset_with_pid_survives_shutdown_force_kills(self, cfg):
        """reset() force-kills when PID survives shutdown."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        mock_client = AsyncMock()
        mock_client._pid = 12345
        mock_client._child_pids = {}
        provider._client = mock_client

        with (
            patch("os.kill", side_effect=[None, None]),
            patch("os.killpg") as mock_killpg,
            patch("os.getpgid", return_value=12345),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._get_start_time", return_value=None),
        ):
            await mgr.reset("k1")
            mock_killpg.assert_called_once()

    @pytest.mark.asyncio
    async def test_reset_with_cc_provider_proc(self, cfg):
        """reset() picks up PID from ClaudeCode _proc attribute."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        # No _client, but has _proc (CC provider style)
        provider._client = None
        mock_proc = AsyncMock()
        mock_proc.pid = 99999
        mock_proc.returncode = None
        provider._proc = mock_proc

        with (
            patch("os.kill", side_effect=ProcessLookupError),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._get_start_time", return_value=None),
        ):
            await mgr.reset("k1")

        provider.shutdown.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_reset_child_sweep(self, cfg):
        """reset() sweeps escaped children after root is dead."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        mock_client = AsyncMock()
        mock_client._pid = 12345
        mock_client._child_pids = {111: 1000, 222: 2000}
        provider._client = mock_client

        with (
            patch("os.kill", side_effect=ProcessLookupError),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[333]),
            patch("kiro_crew.acp.client._get_start_time", return_value=3000),
            patch("kiro_crew.acp.client._read_basename", return_value=b"node"),
            patch("kiro_crew.acp.client._kill_escaped_children") as mock_sweep,
        ):
            await mgr.reset("k1")
            mock_sweep.assert_called_once()
            # Should include both original children and discovered ones
            call_arg = mock_sweep.call_args[0][0]
            assert 111 in call_arg
            assert 222 in call_arg
            assert 333 in call_arg

    @pytest.mark.asyncio
    async def test_reset_nonexistent_session(self, cfg):
        """reset() on missing key is a no-op."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.reset("nonexistent")  # should not raise


class TestReloadProviderFactory:
    """Tests for reload_provider_factory."""

    @pytest.mark.asyncio
    async def test_reload_clears_sessions_and_pool(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.release("k1")
        # Put something in warm pool
        mock_pool_p = AsyncMock()
        mgr._warm_pool.put_nowait((mock_pool_p, "agent"))

        with (
            patch.object(KiroCrewConfig, "load", return_value=cfg),
            patch.object(cfg, "create_provider_factory", return_value=_mock_provider_factory()),
        ):
            await mgr.reload_provider_factory()

        # Old sessions cleared
        assert not mgr.has_session("k1")
        # Pool provider shut down
        mock_pool_p.shutdown.assert_awaited_once()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_reload_shuts_down_stale_sessions(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")

        with (
            patch.object(KiroCrewConfig, "load", return_value=cfg),
            patch.object(cfg, "create_provider_factory", return_value=_mock_provider_factory()),
        ):
            await mgr.reload_provider_factory()

        provider.shutdown.assert_awaited_once()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_reload_shutdown_exception_swallowed(self, cfg):
        """Stale session shutdown failure doesn't crash reload."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        provider.shutdown = AsyncMock(side_effect=OSError("dead"))

        with (
            patch.object(KiroCrewConfig, "load", return_value=cfg),
            patch.object(cfg, "create_provider_factory", return_value=_mock_provider_factory()),
        ):
            await mgr.reload_provider_factory()  # should not raise

        await mgr.close_all()


class TestCheckContextUsage:
    """Tests for check_context_usage thresholds and prompt counting."""

    @pytest.mark.asyncio
    async def test_increments_prompt_count(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        assert mgr._sessions["k1"].prompt_count == 0
        mgr.check_context_usage("k1", provider)
        assert mgr._sessions["k1"].prompt_count == 1
        mgr.check_context_usage("k1", provider)
        assert mgr._sessions["k1"].prompt_count == 2
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_returns_pct(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        provider.context_usage_pct = lambda: 42.5
        result = mgr.check_context_usage("k1", provider)
        assert result == 42.5
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_warning_at_70_pct(self, cfg, caplog):
        cfg.session.autocompact_pct = 90.0
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        provider.context_usage_pct = lambda: 75.0
        with caplog.at_level(logging.WARNING, logger="kiro_crew.session"):
            mgr.check_context_usage("k1", provider)
        assert any("75%" in r.message for r in caplog.records)
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_compaction_triggered_at_threshold(self, cfg):
        cfg.session.autocompact_pct = 90.0
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        provider.context_usage_pct = lambda: 92.0
        with patch.object(mgr, "_trigger_compaction") as mock_trigger:
            mgr.check_context_usage("k1", provider)
            mock_trigger.assert_called_once_with("k1", "context at 92%", 92.0)
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_no_compaction_below_threshold(self, cfg):
        cfg.session.autocompact_pct = 90.0
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        provider.context_usage_pct = lambda: 50.0
        with patch.object(mgr, "_trigger_compaction") as mock_trigger:
            mgr.check_context_usage("k1", provider)
            mock_trigger.assert_not_called()
        await mgr.close_all()

    def test_missing_session_still_returns_pct(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mock_p = AsyncMock()
        mock_p.context_usage_pct = lambda: 55.0
        result = mgr.check_context_usage("nonexistent", mock_p)
        assert result == 55.0


class TestDestroy:
    """Tests for destroy() — permanent session removal."""

    @pytest.mark.asyncio
    async def test_destroy_shuts_down_and_deletes_map(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        with patch.object(mgr._session_map, "delete") as mock_delete:
            await mgr.destroy("k1")
        provider.shutdown.assert_awaited_once()
        mock_delete.assert_called_once_with("k1")
        assert not mgr.has_session("k1")

    @pytest.mark.asyncio
    async def test_destroy_nonexistent_still_deletes_map(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        with patch.object(mgr._session_map, "delete") as mock_delete:
            await mgr.destroy("nonexistent")
        mock_delete.assert_called_once_with("nonexistent")

    @pytest.mark.asyncio
    async def test_destroy_shutdown_exception_still_deletes_map(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        provider.shutdown = AsyncMock(side_effect=RuntimeError("boom"))
        with patch.object(mgr._session_map, "delete") as mock_delete:
            with pytest.raises(RuntimeError, match="boom"):
                await mgr.destroy("k1")
        # finally block still runs
        mock_delete.assert_called_once_with("k1")


class TestContextInfo:
    """Tests for context_info() and _resolve_agent_model()."""

    @pytest.mark.asyncio
    async def test_context_info_basic(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("dashboard:slot0")
        mgr.release("dashboard:slot0")
        mgr._sessions["dashboard:slot0"].prompt_count = 5

        info = mgr.context_info()
        assert len(info) == 1
        entry = info[0]
        assert entry["key"] == "dashboard:slot0"
        assert entry["name"] == "Chat (slot0)"
        assert entry["prompts"] == 5
        assert entry["context_pct"] == 0.0
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_context_info_background_key_name(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.start_pool()
        info = mgr.context_info()
        bg_entry = next(e for e in info if e["key"] == BACKGROUND_KEY)
        assert "Background" in bg_entry["name"]
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_context_info_non_dashboard_key(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("slack:thread123")
        mgr.release("slack:thread123")
        info = mgr.context_info()
        entry = next(e for e in info if e["key"] == "slack:thread123")
        assert entry["name"] == "slack:thread123"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_context_info_with_acp_provider(self, cfg):
        """AcpProvider path extracts model and agent from client."""
        from unittest.mock import MagicMock

        from kiro_crew.providers.acp import AcpProvider
        from kiro_crew.session import _Session

        mock_provider = MagicMock(spec=AcpProvider)
        mock_provider.context_usage_pct = MagicMock(return_value=45.0)
        mock_provider.shutdown = AsyncMock()
        mock_provider.client = MagicMock()
        mock_provider.client._model = "sonnet-4"
        mock_provider.client._agent = "kirocrew"
        mock_provider.client._session_id = None

        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._sessions["k1"] = _Session(provider=mock_provider, prompt_count=3)

        info = mgr.context_info()
        entry = info[0]
        assert entry["model"] == "sonnet-4"
        assert entry["agent"] == "kirocrew"
        assert entry["context_pct"] == 45.0
        await mgr.close_all()

    def test_resolve_agent_model_cache_miss_returns_auto(self, cfg):
        # Clear cache if exists
        if hasattr(SessionManager, "_agent_model_cache"):
            SessionManager._agent_model_cache.clear()
        result = SessionManager._resolve_agent_model("nonexistent-agent-xyz")
        assert result == "auto"

    def test_resolve_agent_model_from_file(self, cfg, tmp_path):
        """Reads model from agent JSON file."""
        import json

        if hasattr(SessionManager, "_agent_model_cache"):
            SessionManager._agent_model_cache.clear()
        agent_file = tmp_path / "test-agent.json"
        agent_file.write_text(json.dumps({"name": "test-agent", "model": "opus-5"}))

        with patch("kiro_crew.agent.KIRO_AGENTS_DIR", tmp_path):
            result = SessionManager._resolve_agent_model("test-agent")
        assert result == "opus-5"

    def test_resolve_agent_model_coerces_non_string_spec(self, cfg, tmp_path):
        """A foreign spec's structured ``model`` must not escape as a dict.

        ``~/.kiro/agents`` is shared with other tools; an ACP-style
        ``{"id": ...}`` here would be CACHED and then handed to
        ``/api/sessions/context`` (the dashboard calls ``.replace()`` on it) and
        to the pooled-model comparison in ``claim_pooled``. This method is
        annotated ``-> str`` and must honour that.
        """
        import json

        if hasattr(SessionManager, "_agent_model_cache"):
            SessionManager._agent_model_cache.clear()
        agent_file = tmp_path / "foreign.json"
        agent_file.write_text(
            json.dumps({"name": "foreign", "model": {"id": "anthropic:claude-opus-4-8"}})
        )

        with patch("kiro_crew.agent.KIRO_AGENTS_DIR", tmp_path):
            result = SessionManager._resolve_agent_model("foreign")
        assert result == "auto"
        assert isinstance(result, str)


class TestWarmPoolInternals:
    """Tests for _fill_warm_pool, _claim_from_pool, _drain_and_claim."""

    @pytest.mark.asyncio
    async def test_fill_warm_pool_spawns_to_size(self, cfg):
        cfg.session.pool_size = 2
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._pool_size = 2
        await mgr._fill_warm_pool()
        assert mgr._warm_pool.qsize() == 2
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_fill_warm_pool_no_factory(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._provider_factory = None
        await mgr._fill_warm_pool()  # should not raise
        assert mgr._warm_pool.qsize() == 0

    @pytest.mark.asyncio
    async def test_fill_warm_pool_zero_size(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._pool_size = 0
        await mgr._fill_warm_pool()
        assert mgr._warm_pool.qsize() == 0

    @pytest.mark.asyncio
    async def test_fill_warm_pool_stops_on_failure(self, cfg):
        call_count = 0

        def failing_factory(session_key=None, agent=None, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise RuntimeError("spawn failed")
            m = AsyncMock()
            m.start = AsyncMock()
            return m

        mgr = SessionManager(cfg, provider_factory=failing_factory)
        mgr._pool_size = 3
        await mgr._fill_warm_pool()
        # Only 1 succeeded before failure stopped the loop
        assert mgr._warm_pool.qsize() == 1
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_claim_from_pool_matching_agent(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._pool_agent = "kirocrew"
        mock_p = AsyncMock()
        mgr._warm_pool.put_nowait((mock_p, 100.0))
        result = mgr._claim_from_pool("kirocrew")
        assert result == (mock_p, 100.0)

    @pytest.mark.asyncio
    async def test_claim_from_pool_mismatched_agent(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._pool_agent = "kirocrew"
        mock_p = AsyncMock()
        mgr._warm_pool.put_nowait((mock_p, 100.0))
        result = mgr._claim_from_pool("different-agent")
        assert result is None

    @pytest.mark.asyncio
    async def test_claim_from_pool_empty(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        result = mgr._claim_from_pool(None)
        assert result is None

    @pytest.mark.asyncio
    async def test_drain_and_claim_healthy(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._pool_agent = ""
        mock_p = AsyncMock()
        mock_p.is_process_alive = lambda: True
        mgr._warm_pool.put_nowait((mock_p, time.monotonic()))
        result = await mgr._drain_and_claim(None)
        assert result is mock_p
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_drain_and_claim_dead_provider_discarded(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._pool_agent = ""
        dead_p = AsyncMock()
        dead_p.is_process_alive = lambda: False
        dead_p.exit_code = 1
        mgr._warm_pool.put_nowait((dead_p, time.monotonic()))
        result = await mgr._drain_and_claim(None)
        assert result is None
        dead_p.shutdown.assert_awaited_once()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_drain_and_claim_stale_ttl_discarded(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._pool_agent = ""
        mgr._pool_ttl_secs = 60
        stale_p = AsyncMock()
        stale_p.is_process_alive = lambda: True

        # Spawned 120s ago — exceeds 60s TTL
        mgr._warm_pool.put_nowait((stale_p, time.monotonic() - 120))
        result = await mgr._drain_and_claim(None)
        assert result is None
        stale_p.shutdown.assert_awaited_once()
        await mgr.close_all()


class TestPoolHealthLoop:
    """Tests for _pool_health_loop periodic sweep."""

    @pytest.mark.asyncio
    async def test_health_loop_removes_dead_providers(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._pool_size = 2
        mgr._pool_ttl_secs = 0  # no TTL

        dead_p = AsyncMock()
        dead_p.is_process_alive = lambda: False
        dead_p.exit_code = 1
        dead_p.client = AsyncMock()
        dead_p.client._pid = 111

        mgr._warm_pool.put_nowait((dead_p, time.monotonic()))

        # Run one iteration then cancel
        call_count = 0
        original_sleep = asyncio.sleep

        async def one_pass_sleep(secs):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise asyncio.CancelledError
            await original_sleep(0)

        with patch("asyncio.sleep", side_effect=one_pass_sleep):
            with pytest.raises(asyncio.CancelledError):
                await mgr._pool_health_loop()

        assert mgr._warm_pool.qsize() == 0
        dead_p.shutdown.assert_awaited_once()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_health_loop_keeps_healthy_providers(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._pool_size = 2
        mgr._pool_ttl_secs = 0

        healthy_p = AsyncMock()
        healthy_p.is_process_alive = lambda: True
        healthy_p.client = AsyncMock()
        healthy_p.client._pid = 222

        mgr._warm_pool.put_nowait((healthy_p, time.monotonic()))

        call_count = 0

        async def one_pass_sleep(secs):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise asyncio.CancelledError
            # instant return for first sleep
            return

        with patch("asyncio.sleep", side_effect=one_pass_sleep):
            with pytest.raises(asyncio.CancelledError):
                await mgr._pool_health_loop()

        assert mgr._warm_pool.qsize() == 1
        healthy_p.shutdown.assert_not_awaited()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_health_loop_ttl_expiry(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._pool_size = 2
        mgr._pool_ttl_secs = 60

        stale_p = AsyncMock()
        stale_p.is_process_alive = lambda: True
        stale_p.client = AsyncMock()
        stale_p.client._pid = 333

        mgr._warm_pool.put_nowait((stale_p, time.monotonic() - 120))

        call_count = 0

        async def one_pass_sleep(secs):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise asyncio.CancelledError
            return

        with patch("asyncio.sleep", side_effect=one_pass_sleep):
            with pytest.raises(asyncio.CancelledError):
                await mgr._pool_health_loop()

        assert mgr._warm_pool.qsize() == 0
        stale_p.shutdown.assert_awaited_once()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_health_loop_empty_pool_skips(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._pool_size = 2

        call_count = 0

        async def one_pass_sleep(secs):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise asyncio.CancelledError
            return

        with patch("asyncio.sleep", side_effect=one_pass_sleep):
            with pytest.raises(asyncio.CancelledError):
                await mgr._pool_health_loop()
        # No crash, just skipped
        await mgr.close_all()


class TestCleanupLoop:
    """Tests for _cleanup_loop periodic maintenance.

    Every sweep the loop dispatches is stubbed: these tests pin the LOOP's
    wiring (which sweeps run, with what args, and when), not sweep behavior —
    each sweep has its own tests in its own module. Leaving a sweep unstubbed
    (notably ``find_orphan_mcp_candidates``, a full process-table scan, and
    ``cleanup_orphaned_session_roots``, which reads the operator's real
    ``~/.kirocrew`` PID file) made each test take ~10-20s of wall-clock and
    probe live system state — both banned by testing-conventions.md.
    """

    @pytest.mark.asyncio
    async def test_cleanup_loop_calls_expire_idle(self, cfg):
        cfg.session.timeout_secs = 120
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())

        with (
            patch.object(mgr, "_expire_idle", new_callable=AsyncMock) as mock_expire,
            patch("kiro_crew.session._cleanup_orphaned_mcp_servers", return_value=0),
            patch("kiro_crew.session.cleanup_stale_sandbox_profiles", return_value=0),
            patch("kiro_crew.session._collect_active_pids", return_value=({}, True)),
            patch("kiro_crew.session._periodic_pid_sweep", return_value=([], [])),
            patch("kiro_crew.session._kill_confirmed_and_writeback", return_value=0),
            patch("kiro_crew.session.cleanup_orphaned_session_roots", return_value=0),
            patch("kiro_crew.session.find_orphan_mcp_candidates", return_value=[]),
            patch("kiro_crew.session.shutdown_event") as mock_event,
        ):
            # First wait_for returns TimeoutError (normal wakeup), second signals shutdown
            mock_event.is_set = lambda: mock_expire.await_count >= 1
            mock_event.wait = AsyncMock(side_effect=asyncio.TimeoutError)
            await mgr._cleanup_loop()

        mock_expire.assert_awaited_once_with(120)
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_cleanup_loop_disabled_idle_sweep(self, cfg):
        cfg.session.timeout_secs = 0
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())

        with (
            patch.object(mgr, "_expire_idle", new_callable=AsyncMock) as mock_expire,
            patch("kiro_crew.session._cleanup_orphaned_mcp_servers", return_value=0),
            patch("kiro_crew.session.cleanup_stale_sandbox_profiles", return_value=0),
            patch("kiro_crew.session._collect_active_pids", return_value=({}, True)),
            patch("kiro_crew.session._periodic_pid_sweep", return_value=([], [])),
            patch("kiro_crew.session._kill_confirmed_and_writeback", return_value=0),
            patch("kiro_crew.session.cleanup_orphaned_session_roots", return_value=0),
            patch("kiro_crew.session.find_orphan_mcp_candidates", return_value=[]),
            patch("kiro_crew.session.shutdown_event") as mock_event,
        ):
            call_count = [0]

            async def one_pass(*a, **kw):
                call_count[0] += 1
                raise asyncio.TimeoutError

            mock_event.is_set = lambda: call_count[0] >= 1
            mock_event.wait = AsyncMock(side_effect=one_pass)
            await mgr._cleanup_loop()

        # idle sweep disabled — _expire_idle should NOT be called
        mock_expire.assert_not_awaited()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_cleanup_loop_clamps_low_timeout(self, cfg, caplog):
        cfg.session.timeout_secs = 30  # below 60 minimum
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())

        with (
            patch.object(mgr, "_expire_idle", new_callable=AsyncMock) as mock_expire,
            patch("kiro_crew.session._cleanup_orphaned_mcp_servers", return_value=0),
            patch("kiro_crew.session.cleanup_stale_sandbox_profiles", return_value=0),
            patch("kiro_crew.session._collect_active_pids", return_value=({}, True)),
            patch("kiro_crew.session._periodic_pid_sweep", return_value=([], [])),
            patch("kiro_crew.session._kill_confirmed_and_writeback", return_value=0),
            patch("kiro_crew.session.cleanup_orphaned_session_roots", return_value=0),
            patch("kiro_crew.session.find_orphan_mcp_candidates", return_value=[]),
            patch("kiro_crew.session.shutdown_event") as mock_event,
        ):
            mock_event.is_set = lambda: mock_expire.await_count >= 1
            mock_event.wait = AsyncMock(side_effect=asyncio.TimeoutError)
            with caplog.at_level(logging.WARNING, logger="kiro_crew.session"):
                await mgr._cleanup_loop()

        # Should clamp to 60
        mock_expire.assert_awaited_once_with(60)
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_cleanup_loop_shutdown_signal(self, cfg):
        cfg.session.timeout_secs = 120
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())

        with patch("kiro_crew.session.shutdown_event") as mock_event:
            mock_event.is_set = lambda: True
            mock_event.wait = AsyncMock(return_value=None)
            # Should return immediately since shutdown is set
            await mgr._cleanup_loop()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_cleanup_loop_runs_sandbox_sweep_via_executor(self, cfg, caplog):
        """Sandbox sweep is invoked through run_in_executor on maintenance_executor.

        Asserts the offload specifically (the blocking-call fix): the sweep
        must execute on a maintenance-executor worker thread, NOT the event
        loop thread, so its blocking os.listdir/os.kill/os.remove I/O cannot
        freeze the loop.
        """
        cfg.session.timeout_secs = 120
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())

        sweep_threads: list[str] = []

        def _fake_sweep() -> int:
            sweep_threads.append(threading.current_thread().name)
            return 3

        with (
            patch.object(mgr, "_expire_idle", new_callable=AsyncMock),
            patch("kiro_crew.session._cleanup_orphaned_mcp_servers", return_value=0),
            patch(
                "kiro_crew.session.cleanup_stale_sandbox_profiles", side_effect=_fake_sweep
            ) as mock_sweep,
            patch("kiro_crew.session._collect_active_pids", return_value=({}, True)),
            patch("kiro_crew.session._periodic_pid_sweep", return_value=([], [])),
            patch("kiro_crew.session._kill_confirmed_and_writeback", return_value=0),
            patch("kiro_crew.session.cleanup_orphaned_session_roots", return_value=0),
            patch("kiro_crew.session.find_orphan_mcp_candidates", return_value=[]),
            patch("kiro_crew.session.shutdown_event") as mock_event,
        ):
            mock_event.is_set = lambda: mock_sweep.call_count >= 1
            mock_event.wait = AsyncMock(side_effect=asyncio.TimeoutError)
            with caplog.at_level(logging.INFO, logger="kiro_crew.session"):
                await mgr._cleanup_loop()

        # Verify: sweep was called (production wiring)
        mock_sweep.assert_called_once()
        # Verify the offload: ran on a maintenance-executor worker thread,
        # not the event loop thread (run_in_executor path).
        assert sweep_threads, "sweep never executed"
        assert sweep_threads[0] != threading.main_thread().name
        assert sweep_threads[0].startswith("mc-maint")
        # Verify: non-zero return produces the info log
        assert "removed 3 stale sandbox launchers" in caplog.text
        await mgr.close_all()


class TestPoolPids:
    """Tests for _pool_pids non-destructive peek."""

    @pytest.mark.asyncio
    async def test_pool_pids_extracts_pids(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mock_p = AsyncMock()
        mock_p.client = AsyncMock()
        mock_p.client._pid = 42
        mgr._warm_pool.put_nowait((mock_p, time.monotonic()))
        pids = mgr._pool_pids()
        assert 42 in pids
        # Non-destructive — item still in pool
        assert mgr._warm_pool.qsize() == 1

    @pytest.mark.asyncio
    async def test_pool_pids_empty(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        assert mgr._pool_pids() == set()

    @pytest.mark.asyncio
    async def test_pool_pids_includes_sweep_pids(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._pool_sweep_pids.add(999)
        pids = mgr._pool_pids()
        assert 999 in pids


class TestSlackLinkHelpers:
    """Tests for set/get slack_link, thread, channel helpers."""

    @pytest.mark.asyncio
    async def test_set_and_get_slack_link(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr.set_slack_link("k1", "ts123", "C001")
        assert mgr.get_slack_link("k1") == ("ts123", "C001")

    @pytest.mark.asyncio
    async def test_get_session_for_thread(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr.set_slack_link("k1", "ts123", "C001")
        assert mgr.get_session_for_thread("ts123") == "k1"
        assert mgr.get_session_for_thread("unknown") is None

    @pytest.mark.asyncio
    async def test_set_channel_compat(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr.set_slack_link("k1", "ts123", None)
        await mgr.set_channel("k1", "C002")
        assert mgr.get_channel("k1") == "C002"

    @pytest.mark.asyncio
    async def test_set_thread_compat(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr.set_slack_link("k1", "", "C001")
        await mgr.set_thread("k1", "ts456")
        assert mgr.get_thread("k1") == "ts456"

    def test_get_channel_no_link(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        assert mgr.get_channel("nonexistent") is None

    def test_get_thread_no_link(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        assert mgr.get_thread("nonexistent") is None

    def test_find_key_by_sid(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._session_map.set("k1", "sid-abc")
        assert mgr.find_key_by_sid("sid-abc") == "k1"
        assert mgr.find_key_by_sid("unknown") is None

    def test_delete_session_map_entry(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._session_map.set("k1", "sid-abc")
        mgr.delete_session_map_entry("k1")
        assert mgr.find_key_by_sid("sid-abc") is None


class TestGetPid:
    """Tests for get_pid."""

    @pytest.mark.asyncio
    async def test_get_pid_returns_pid(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        provider.client = AsyncMock()
        provider.client._pid = 12345
        assert mgr.get_pid("k1") == 12345
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_get_pid_no_client(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        # Remove client attr
        del provider.client
        assert mgr.get_pid("k1") is None
        await mgr.close_all()

    def test_get_pid_missing_session(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        assert mgr.get_pid("nonexistent") is None


class TestIsProviderAliveFallback:
    """Test is_provider_alive fallback to is_alive when no is_process_alive."""

    @pytest.mark.asyncio
    async def test_fallback_to_is_alive(self, cfg):
        from unittest.mock import MagicMock

        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        # Remove is_process_alive so it falls back
        if hasattr(provider, "is_process_alive"):
            del provider.is_process_alive
        provider.is_alive = MagicMock(return_value=True)
        result = await mgr.is_provider_alive("k1")
        assert result is True
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_no_session_returns_none(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        result = await mgr.is_provider_alive("nonexistent")
        assert result is None


class TestSetActiveDashboardSlots:
    """Test set_active_dashboard_slots."""

    def test_sets_slots(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr.set_active_dashboard_slots({"dashboard:tab1", "dashboard:tab2"})
        assert mgr._active_dashboard_slots == {"dashboard:tab1", "dashboard:tab2"}


class TestStartPoolNonBlocking:
    """Tests for start_pool non-blocking path."""

    @pytest.mark.asyncio
    async def test_start_pool_non_blocking(self, cfg):
        cfg.session.pool_size = 1
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._pool_size = 1
        await mgr.start_pool(blocking=False)
        # Let background tasks run
        await asyncio.sleep(0.1)
        assert BACKGROUND_KEY in mgr._sessions
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_start_pool_no_factory(self, cfg):
        mgr = SessionManager(cfg, provider_factory=None)
        await mgr.start_pool()  # should be no-op
        assert mgr.count == 0

    @pytest.mark.asyncio
    async def test_ensure_background_already_exists(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.start_pool()
        # Call again — should be no-op
        await mgr._ensure_background()
        assert mgr.count == 1  # still just the one bg session
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_ensure_background_factory_failure(self, cfg):
        def failing_factory(session_key=None, **kwargs):
            raise RuntimeError("spawn failed")

        mgr = SessionManager(cfg, provider_factory=failing_factory)
        await mgr._ensure_background()
        # Should not crash, just log warning
        assert BACKGROUND_KEY not in mgr._sessions


class TestScheduleReplenish:
    """Tests for _schedule_replenish fire-and-forget pool refill."""

    @pytest.mark.asyncio
    async def test_schedule_replenish_creates_task(self, cfg):
        cfg.session.pool_size = 2
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._pool_size = 2
        mgr._schedule_replenish()
        await asyncio.sleep(0.1)  # let task run
        assert mgr._warm_pool.qsize() >= 1
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_schedule_replenish_noop_when_pool_disabled(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._pool_size = 0
        mgr._schedule_replenish()  # should not create task
        assert len(mgr._background_tasks) == 0


class TestCompaction:
    """Tests for _trigger_compaction and _compact_session."""

    @pytest.mark.asyncio
    async def test_trigger_compaction_duplicate_is_noop(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.release("k1")
        # First trigger starts compaction
        mgr._trigger_compaction("k1", "test", 92.0)
        assert "k1" in mgr._compacting
        # Second trigger on same key is a no-op (already in progress)
        mgr._trigger_compaction("k1", "test again", 95.0)
        await asyncio.sleep(0.1)
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_compact_session_calls_callback(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        callback_args: list[tuple[str, float, bool]] = []

        async def cb(key, pct, *, success):
            callback_args.append((key, pct, success))

        mgr.set_compact_callback(cb)
        await mgr._compact_session("k1", 92.0)
        provider.shutdown.assert_awaited_once()
        assert callback_args == [("k1", 92.0, True)]
        assert not mgr.has_session("k1")

    @pytest.mark.asyncio
    async def test_compact_session_missing_key_is_safe(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._compacting.add("gone")
        await mgr._compact_session("gone", 90.0)
        assert "gone" not in mgr._compacting


class TestClaudeBackendCompaction:
    """Claude-agent-acp autocompact runs /compact in place — no recycle."""

    @pytest.mark.asyncio
    async def test_compact_session_claude_runs_in_place(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        provider.compact = AsyncMock()
        callback_args: list[tuple[str, float, bool]] = []

        async def cb(key, pct, *, success):
            callback_args.append((key, pct, success))

        mgr.set_compact_callback(cb)

        with patch("kiro_crew.session._is_claude_backend", return_value=True):
            await mgr._compact_session("k1", 92.0)

        provider.compact.assert_awaited_once()
        provider.shutdown.assert_not_awaited()
        assert mgr.has_session("k1")
        assert callback_args == [("k1", 92.0, True)]
        assert "k1" not in mgr._compacting

    @pytest.mark.asyncio
    async def test_compact_session_claude_failure_keeps_session(self, cfg, caplog):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        provider.compact = AsyncMock(side_effect=RuntimeError("boom"))
        cb = AsyncMock()
        mgr.set_compact_callback(cb)

        with (
            patch("kiro_crew.session._is_claude_backend", return_value=True),
            caplog.at_level(logging.ERROR, logger="kiro_crew.session"),
        ):
            await mgr._compact_session("k1", 92.0)

        assert mgr.has_session("k1")
        provider.shutdown.assert_not_awaited()
        # Failure callback fires with success=False so the dashboard can
        # show a "compact failed" banner. (Behavior changed in the I2 fix.)
        cb.assert_awaited_once_with("k1", 92.0, success=False)
        assert "k1" not in mgr._compacting
        assert any("Compact failed" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_check_context_usage_triggers_for_claude(self, cfg):
        """Autocompact threshold must apply to claude — no longer skipped."""
        cfg.session.autocompact_pct = 20.0
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        provider.context_usage_pct = lambda: 40.0

        with (
            patch("kiro_crew.session._is_claude_backend", return_value=True),
            patch.object(mgr, "_trigger_compaction") as mock_trigger,
        ):
            mgr.check_context_usage("k1", provider)

        mock_trigger.assert_called_once_with("k1", "context at 40%", 40.0)
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_get_or_create_during_claude_compact_reuses_session(self, cfg):
        """Concurrent get_or_create while claude compact is in flight must
        return the existing session, not cold-start a duplicate provider that
        would later overwrite _sessions[key] and leak the original process."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")

        with patch("kiro_crew.session._is_claude_backend", return_value=True):
            # Simulate compact in progress.
            mgr._compacting.add("k1")
            try:
                provider2, is_new, _ = await mgr.get_or_create("k1")
            finally:
                mgr._compacting.discard("k1")

        assert provider2 is provider
        assert is_new is False
        assert mgr.count == 1
        mgr.release("k1")
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_get_or_create_during_recycle_teardown_cold_starts(self, cfg):
        """While the failure recycle is tearing the entry down (_recycling
        holds the exact object still in the map), get_or_create must not
        reuse the doomed entry — fall through to cold-start."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")

        mgr._recycling["k1"] = mgr._sessions["k1"]
        try:
            provider2, is_new, _ = await mgr.get_or_create("k1")
        finally:
            mgr._recycling.pop("k1", None)

        # New provider: we did not short-circuit to the doomed entry.
        assert provider2 is not provider
        mgr.release("k1")
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_get_or_create_reuses_healthy_replacement_during_recycle(self, cfg):
        """The _recycling marker is object-aware: when the map already holds a
        healthy REPLACEMENT for a key whose OLD session is still being torn
        down, get_or_create must reuse the replacement — not exile it and
        cold-start a duplicate provider that would overwrite and leak it."""
        from kiro_crew.session import _Session

        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        old_provider, _, _ = await mgr.get_or_create("k1")
        old_sess = mgr._sessions["k1"]
        mgr.release("k1")

        # Recycle of the OLD object is in flight; a racing cold-start has
        # already registered a fresh replacement under the same key.
        replacement_provider = AsyncMock()
        replacement_provider.shutdown = AsyncMock()
        replacement = _Session(provider=replacement_provider, is_new=False)
        mgr._sessions["k1"] = replacement
        mgr._recycling["k1"] = old_sess
        try:
            provider2, is_new, _ = await mgr.get_or_create("k1")
        finally:
            mgr._recycling.pop("k1", None)

        assert provider2 is replacement_provider
        assert is_new is False
        assert mgr._sessions["k1"] is replacement
        mgr.release("k1")
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_get_or_create_during_inplace_compact_reuses_session(self, cfg):
        """An in-place compact (kiro or claude) keeps the entry healthy:
        concurrent get_or_create must reuse it — queueing on the session
        semaphore — instead of cold-starting a duplicate provider."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")

        mgr._compacting.add("k1")  # in-place compact in flight; NOT recycling
        try:
            provider2, is_new, _ = await mgr.get_or_create("k1")
        finally:
            mgr._compacting.discard("k1")

        assert provider2 is provider
        assert is_new is False
        mgr.release("k1")
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_get_or_create_after_kiro_pop_cold_starts(self, cfg):
        """Real kiro recycle pops _sessions[key] before adding to _compacting.
        Concurrent get_or_create must cold-start fresh (is_new=True)."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        # Pop is the post-recycle reality: entry gone from _sessions.
        mgr._compacting.add("k1")
        try:
            provider, is_new, _ = await mgr.get_or_create("k1")
        finally:
            mgr._compacting.discard("k1")

        assert is_new is True
        assert provider is not None
        mgr.release("k1")
        await mgr.close_all()


class TestKiroInPlaceCompaction:
    """kiro-cli auto-compaction runs /compact IN PLACE first, so the session
    (and any queued/agentic work) continues automatically — recycle is only
    the fallback. Fix for 'session stops after auto-compaction'."""

    @staticmethod
    def _inplace_provider_factory(result: dict, *, stream_events: list | None = None):
        """Provider whose native compaction reports *result*.

        ``stream_command("/compact")`` yields *stream_events* (default: none —
        the terminal status arrives async via ``wait_for_compaction``,
        mirroring kiro-cli's post-end_turn status emission).
        """

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            m = AsyncMock()
            m.start = AsyncMock()
            m.shutdown = AsyncMock()
            m.context_usage_pct = lambda: 0.0

            async def _stream(_cmd):
                for ev in stream_events or []:
                    yield ev

            m.stream_command = MagicMock(side_effect=_stream)
            m.wait_for_compaction = AsyncMock(return_value=result)
            return m

        return factory

    @pytest.mark.asyncio
    async def test_inplace_success_keeps_session_and_process(self, cfg):
        mgr = SessionManager(
            cfg, provider_factory=self._inplace_provider_factory({"type": "completed"})
        )
        provider, _, _ = await mgr.get_or_create("dashboard:chat-1")
        mgr.release("dashboard:chat-1")
        cb = AsyncMock()
        mgr.set_compact_callback(cb)

        await mgr._compact_session("dashboard:chat-1", 92.0)

        # Session survives in place: same entry, same provider, no SIGKILL.
        assert "dashboard:chat-1" in mgr._sessions
        assert mgr._sessions["dashboard:chat-1"].provider is provider
        provider.stream_command.assert_called_once_with("/compact")
        provider.shutdown.assert_not_awaited()
        cb.assert_awaited_once_with("dashboard:chat-1", 92.0, success=True)
        # Semaphore released: the next turn can proceed immediately.
        assert not mgr._sessions["dashboard:chat-1"].semaphore.locked()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_inplace_failed_result_falls_back_to_recycle(self, cfg):
        mgr = SessionManager(
            cfg, provider_factory=self._inplace_provider_factory({"type": "failed"})
        )
        provider, _, _ = await mgr.get_or_create("dashboard:chat-1")
        mgr.release("dashboard:chat-1")
        cb = AsyncMock()
        mgr.set_compact_callback(cb)

        await mgr._compact_session("dashboard:chat-1", 92.0)

        # Fallback recycle: entry dropped, process killed, context guaranteed
        # to clear on the next (re-seeded) message.
        assert "dashboard:chat-1" not in mgr._sessions
        provider.shutdown.assert_awaited_once()
        cb.assert_awaited_once_with("dashboard:chat-1", 92.0, success=True)
        assert "dashboard:chat-1" not in mgr._recycling
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_inplace_no_native_support_falls_back_to_recycle(self, cfg):
        """Base LLMProvider.wait_for_compaction returns {'type': 'timeout'} —
        providers without native compaction must keep today's recycle path."""
        mgr = SessionManager(
            cfg, provider_factory=self._inplace_provider_factory({"type": "timeout"})
        )
        provider, _, _ = await mgr.get_or_create("dashboard:chat-1")
        mgr.release("dashboard:chat-1")
        cb = AsyncMock()
        mgr.set_compact_callback(cb)

        await mgr._compact_session("dashboard:chat-1", 92.0)

        assert "dashboard:chat-1" not in mgr._sessions
        provider.shutdown.assert_awaited_once()
        cb.assert_awaited_once_with("dashboard:chat-1", 92.0, success=True)
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_inplace_success_clears_failure_cooldown(self, cfg):
        mgr = SessionManager(
            cfg, provider_factory=self._inplace_provider_factory({"type": "completed"})
        )
        await mgr.get_or_create("dashboard:chat-1")
        mgr.release("dashboard:chat-1")
        mgr._compact_cooldown_until["dashboard:chat-1"] = time.monotonic() + 999

        await mgr._compact_session("dashboard:chat-1", 92.0)

        assert "dashboard:chat-1" not in mgr._compact_cooldown_until
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_inplace_midstream_terminal_status_skips_async_wait(self, cfg):
        """kiro-cli may emit the terminal compaction status MID-TURN (before
        end_turn). The stream watcher must latch it so the blind drain never
        eats it — otherwise wait_for_compaction would stall to timeout and
        wrongly recycle a just-compacted healthy session."""
        from kiro_crew.acp.types import EVENT_COMPACTION_STATUS, AcpEvent

        mid = AcpEvent(kind=EVENT_COMPACTION_STATUS, text="completed", title="sum")
        mgr = SessionManager(
            cfg,
            provider_factory=self._inplace_provider_factory(
                {"type": "timeout"},  # async wait would FAIL if consulted
                stream_events=[mid],
            ),
        )
        provider, _, _ = await mgr.get_or_create("dashboard:chat-1")
        mgr.release("dashboard:chat-1")
        cb = AsyncMock()
        mgr.set_compact_callback(cb)

        await mgr._compact_session("dashboard:chat-1", 92.0)

        # Mid-stream status decided the outcome; async wait never consulted.
        assert "dashboard:chat-1" in mgr._sessions
        provider.wait_for_compaction.assert_not_awaited()
        provider.shutdown.assert_not_awaited()
        cb.assert_awaited_once_with("dashboard:chat-1", 92.0, success=True)
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_inplace_never_uses_commands_execute(self, cfg):
        """Regression for the 2026-07-23 production failure: /compact sent
        via the string form of _kiro.dev/commands/execute makes kiro-cli
        2.14.0 exit rc=0. The auto-compact path must use the prompt
        transport (stream_command), never send_command."""
        mgr = SessionManager(
            cfg, provider_factory=self._inplace_provider_factory({"type": "completed"})
        )
        provider, _, _ = await mgr.get_or_create("dashboard:chat-1")
        provider.send_command = AsyncMock()
        mgr.release("dashboard:chat-1")

        await mgr._compact_session("dashboard:chat-1", 92.0)

        provider.stream_command.assert_called_once_with("/compact")
        provider.send_command.assert_not_awaited()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_inplace_holds_semaphore_so_queued_turns_wait(self, cfg):
        """While the in-place compact runs, the session semaphore is held —
        a queued turn waits behind it and then continues on the compacted
        session instead of interleaving with the compaction."""
        gate = asyncio.Event()

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            m = AsyncMock()
            m.start = AsyncMock()
            m.shutdown = AsyncMock()
            m.context_usage_pct = lambda: 0.0

            async def _stream(_cmd):
                if False:  # pragma: no cover - empty async generator
                    yield

            m.stream_command = MagicMock(side_effect=_stream)

            async def _wait(timeout=120.0):
                await gate.wait()
                return {"type": "completed"}

            m.wait_for_compaction = _wait
            return m

        mgr = SessionManager(cfg, provider_factory=factory)
        await mgr.get_or_create("dashboard:chat-1")
        mgr.release("dashboard:chat-1")
        sess = mgr._sessions["dashboard:chat-1"]

        task = asyncio.create_task(mgr._compact_session("dashboard:chat-1", 92.0))
        await asyncio.sleep(0.05)
        assert not task.done()
        assert sess.semaphore.locked()  # queued turn would wait here

        gate.set()
        await asyncio.wait_for(task, timeout=2)
        assert "dashboard:chat-1" in mgr._sessions
        assert not sess.semaphore.locked()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_failure_recycle_never_yields_semaphore_to_queued_turn(self, cfg):
        """Regression (production 2026-08-05): the failure recycle must not
        open a window in which a queued turn is dispatched into a session that
        is still compacting.

        The old code released the turn semaphore as soon as the in-place
        compact reported failure and let the CALLER re-acquire it for the
        recycle. A queued turn won that gap: it was sent to a kiro-cli that was
        still finishing its ``/compact``, its stream received the late
        ``completed`` status instead of an ``end_turn``, and the turn hung
        holding the semaphore until the 2h prompt timeout — while the recycle
        that would have rescued it gave up at its own acquire timeout. The kill
        must therefore land BEFORE any queued turn can run.
        """
        order: list[str] = []
        gate = asyncio.Event()

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            m = AsyncMock()
            m.start = AsyncMock()
            m.context_usage_pct = lambda: 0.0

            async def _stream(_cmd):
                if False:  # pragma: no cover - empty async generator
                    yield

            m.stream_command = MagicMock(side_effect=_stream)

            async def _shutdown() -> None:
                order.append("shutdown")

            m.shutdown = AsyncMock(side_effect=_shutdown)

            async def _wait(timeout=120.0):
                # Mirrors production: the async wait gives up while the
                # compaction is in fact still running on the backend.
                await gate.wait()
                return {"type": "timeout"}

            m.wait_for_compaction = _wait
            return m

        mgr = SessionManager(cfg, provider_factory=factory)
        await mgr.get_or_create("dashboard:chat-1")
        mgr.release("dashboard:chat-1")
        sess = mgr._sessions["dashboard:chat-1"]

        compact = asyncio.create_task(mgr._compact_session("dashboard:chat-1", 92.0))
        await asyncio.sleep(0.05)
        assert sess.semaphore.locked()

        # A queued turn parks on the turn semaphore, exactly as a real
        # dispatch does while a compaction holds it.
        async def _queued_turn() -> None:
            async with sess.semaphore:
                order.append("turn")

        turn = asyncio.create_task(_queued_turn())
        await asyncio.sleep(0.05)
        assert not turn.done()

        gate.set()  # compact reports timeout -> recycle, semaphore still held
        await asyncio.wait_for(compact, timeout=2)
        await asyncio.wait_for(turn, timeout=2)

        # Kill first, queued turn second: the semaphore was never handed back
        # while the backend could still have been compacting.
        assert order == ["shutdown", "turn"]
        assert "dashboard:chat-1" not in mgr._sessions
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_recycle_pop_by_identity_spares_replacement(self, cfg):
        """If a racing cold-start replaced the entry while the in-place
        compact was still running, the failure recycle kills only the OLD
        session; the fresh replacement (and its session_map entry) survives."""
        from kiro_crew.session import _Session

        gate = asyncio.Event()

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            m = AsyncMock()
            m.start = AsyncMock()
            m.shutdown = AsyncMock()
            m.context_usage_pct = lambda: 0.0

            async def _stream(_cmd):
                if False:  # pragma: no cover - empty async generator
                    yield

            m.stream_command = MagicMock(side_effect=_stream)

            async def _wait(timeout=120.0):
                await gate.wait()
                return {"type": "failed"}

            m.wait_for_compaction = _wait
            return m

        mgr = SessionManager(cfg, provider_factory=factory)
        old_provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        cb = AsyncMock()
        mgr.set_compact_callback(cb)

        task = asyncio.create_task(mgr._compact_session("k1", 92.0))
        await asyncio.sleep(0.05)
        assert not task.done()

        # A racing cold-start replaces the entry with a fresh session.
        new_provider = AsyncMock()
        new_provider.shutdown = AsyncMock()
        mgr._sessions["k1"] = _Session(provider=new_provider, is_new=False)

        gate.set()  # compact fails -> recycle pops by identity
        await asyncio.wait_for(task, timeout=2)

        # Replacement untouched; old provider reaped.
        assert mgr._sessions["k1"].provider is new_provider
        new_provider.shutdown.assert_not_awaited()
        old_provider.shutdown.assert_awaited_once()
        cb.assert_awaited_once_with("k1", 92.0, success=True)
        await mgr.close_all()


class TestCompactFailureCooldown:
    """After a compact failure, subsequent triggers within the cooldown
    window must be skipped to avoid hammering the provider on every turn."""

    @pytest.mark.asyncio
    async def test_compact_failure_sets_cooldown(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        provider.compact = AsyncMock(side_effect=RuntimeError("boom"))

        with patch("kiro_crew.session._is_claude_backend", return_value=True):
            await mgr._compact_session("k1", 92.0)

        assert mgr._compact_cooldown_until.get("k1", 0.0) > time.monotonic()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_trigger_compaction_skipped_during_cooldown(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        # Set cooldown 60s in the future.
        mgr._compact_cooldown_until["k1"] = time.monotonic() + 60.0

        mgr._trigger_compaction("k1", "context at 90%", 90.0)

        # No background task scheduled, no compacting marker set.
        assert "k1" not in mgr._compacting
        assert not mgr._background_tasks
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_compact_success_clears_cooldown(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        provider.compact = AsyncMock()
        # Pre-existing cooldown from an earlier failure.
        mgr._compact_cooldown_until["k1"] = time.monotonic() + 60.0

        with patch("kiro_crew.session._is_claude_backend", return_value=True):
            await mgr._compact_session("k1", 92.0)

        assert "k1" not in mgr._compact_cooldown_until
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_expired_cooldown_allows_retrigger(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        # Cooldown already in the past.
        mgr._compact_cooldown_until["k1"] = time.monotonic() - 1.0

        mgr._trigger_compaction("k1", "context at 90%", 90.0)

        # Background task scheduled and compacting marker set.
        assert "k1" in mgr._compacting
        assert len(mgr._background_tasks) == 1
        # Snapshot to a list — `add_done_callback` discard mutates the set
        # during await, which would break direct iteration.
        for t in list(mgr._background_tasks):
            await t
        await mgr.close_all()


class TestCompactTimeout:
    """provider.compact() must be wrapped in a timeout so a stuck compact
    cannot hold session.semaphore forever and block concurrent gets."""

    @pytest.mark.asyncio
    async def test_compact_timeout_sets_cooldown_and_fires_failure(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")

        async def _hang(*_a, **_kw):
            await asyncio.sleep(10.0)

        provider.compact = _hang
        callback_calls: list[tuple[str, float, bool]] = []

        async def cb(key, pct, *, success):
            callback_calls.append((key, pct, success))

        mgr.set_compact_callback(cb)

        with (
            patch("kiro_crew.session._is_claude_backend", return_value=True),
            patch("kiro_crew.session._COMPACT_TIMEOUT_SECS", 0.05),
        ):
            await mgr._compact_session("k1", 92.0)

        assert mgr._compact_cooldown_until.get("k1", 0.0) > time.monotonic()
        assert callback_calls == [("k1", 92.0, False)]
        assert "k1" not in mgr._compacting
        await mgr.close_all()


class TestCompactCallbackSuccessFlag:
    """The compact callback must receive ``success=False`` on failure so the
    dashboard can show a different banner."""

    @pytest.mark.asyncio
    async def test_keyword_callback_fires_with_success_true_on_success(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        provider.compact = AsyncMock()

        calls: list[tuple[str, float, bool]] = []

        async def cb(key, pct, *, success):
            calls.append((key, pct, success))

        mgr.set_compact_callback(cb)

        with patch("kiro_crew.session._is_claude_backend", return_value=True):
            await mgr._compact_session("k1", 92.0)

        assert calls == [("k1", 92.0, True)]
        await mgr.close_all()


class TestCooldownPruning:
    """`_compact_cooldown_until` entries must be cleared on session lifecycle
    events so a fresh session reusing a key never inherits a stale cooldown."""

    @pytest.mark.asyncio
    async def test_remove_clears_cooldown(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.release("k1")
        mgr._compact_cooldown_until["k1"] = time.monotonic() + 60.0

        await mgr.remove("k1")

        assert "k1" not in mgr._compact_cooldown_until
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_destroy_clears_cooldown(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.release("k1")
        mgr._compact_cooldown_until["k1"] = time.monotonic() + 60.0

        await mgr.destroy("k1")

        assert "k1" not in mgr._compact_cooldown_until
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_reset_clears_cooldown(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.release("k1")
        mgr._compact_cooldown_until["k1"] = time.monotonic() + 60.0

        await mgr.reset("k1")

        assert "k1" not in mgr._compact_cooldown_until
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_close_all_clears_cooldowns(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.release("k1")
        mgr._compact_cooldown_until["k1"] = time.monotonic() + 60.0
        mgr._compact_cooldown_until["k2"] = time.monotonic() + 60.0

        await mgr.close_all()

        assert mgr._compact_cooldown_until == {}


class TestCloseAllPersistence:
    """Tests for close_all session_map persistence."""

    @pytest.mark.asyncio
    async def test_close_all_persists_acp_session_ids(self, cfg):
        from unittest.mock import MagicMock

        from kiro_crew.providers.acp import AcpProvider
        from kiro_crew.session import _Session

        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mock_provider = MagicMock(spec=AcpProvider)
        mock_provider.shutdown = AsyncMock()
        mock_provider.context_usage_pct = MagicMock(return_value=0.0)
        # session_map persistence reads the public provider.cwd accessor
        # (AcpProvider exposes the work dir via _client._work_dir, not a bare
        # _work_dir attribute).
        mock_provider.cwd = "/tmp/test"
        mock_provider.client = MagicMock()
        mock_provider.client._session_id = "sid-persist-test"
        mock_provider.client.backend = ""  # kiro-cli backend

        mgr._sessions["dashboard:slot0"] = _Session(provider=mock_provider)
        with patch.object(mgr._session_map, "set") as mock_set:
            await mgr.close_all()
        # provider= is now persisted so the next-startup detect_provider_switch
        # doesn't see a missing label and falsely fire an acp/cc switch
        # (review round 1 #24).
        mock_set.assert_called_once_with(
            "dashboard:slot0",
            "sid-persist-test",
            provider="acp",
            cwd="/tmp/test",
        )


class TestRemove:
    """Tests for remove() — shutdown but preserve session_map."""

    @pytest.mark.asyncio
    async def test_remove_shuts_down_preserves_map(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        with patch.object(mgr._session_map, "delete") as mock_delete:
            await mgr.remove("k1")
        provider.shutdown.assert_awaited_once()
        mock_delete.assert_not_called()  # remove preserves map
        assert not mgr.has_session("k1")

    @pytest.mark.asyncio
    async def test_remove_missing_key_is_noop(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.remove("nonexistent")  # should not raise


class TestSafeCleanup:
    """Tests for _safe_cleanup best-effort session file removal."""

    @pytest.mark.asyncio
    async def test_cleanup_calls_provider(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mock_p = AsyncMock()
        mock_p.cleanup_session = AsyncMock()
        await mgr._safe_cleanup(mock_p, "sid-123")
        mock_p.cleanup_session.assert_awaited_once_with("sid-123")

    @pytest.mark.asyncio
    async def test_cleanup_swallows_exception(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mock_p = AsyncMock()
        mock_p.cleanup_session = AsyncMock(side_effect=OSError("disk full"))
        await mgr._safe_cleanup(mock_p, "sid-456")  # should not raise


class TestSetCompactCallback:
    """Tests for set_compact_callback."""

    def test_sets_callback(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        cb = AsyncMock()
        mgr.set_compact_callback(cb)
        assert mgr._on_compacted is cb

    def test_warns_on_replace(self, cfg, caplog):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr.set_compact_callback(AsyncMock())
        with caplog.at_level(logging.WARNING, logger="kiro_crew.session"):
            mgr.set_compact_callback(AsyncMock())
        assert any("already registered" in r.message for r in caplog.records)


class TestExpireIdleOrphans:
    """Tests for _expire_idle orphaned dashboard slot detection."""

    @pytest.mark.asyncio
    async def test_orphaned_dashboard_slot_expired(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("dashboard:slot5")
        mgr.release("dashboard:slot5")
        # Set active slots to NOT include slot5
        mgr.set_active_dashboard_slots({"dashboard:slot0"})
        await mgr._expire_idle(timeout_secs=9999)  # not idle, but orphaned
        assert not mgr.has_session("dashboard:slot5")

    @pytest.mark.asyncio
    async def test_active_slot_not_expired(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("dashboard:slot0")
        mgr.release("dashboard:slot0")
        mgr.set_active_dashboard_slots({"dashboard:slot0"})
        await mgr._expire_idle(timeout_secs=9999)
        assert mgr.has_session("dashboard:slot0")
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_channel_session_never_expired(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("channel:C123")
        mgr.release("channel:C123")
        # Backdate to make it idle
        async with mgr._lock:
            mgr._sessions["channel:C123"].last_used = time.monotonic() - 9999
        await mgr._expire_idle(timeout_secs=1)
        assert mgr.has_session("channel:C123")
        await mgr.close_all()


class TestGetOrCreatePoolClaim:
    """Test get_or_create claiming from warm pool."""

    @pytest.mark.asyncio
    async def test_claims_from_pool_on_new_session(self, cfg):
        from kiro_crew.providers.acp import AcpProvider

        cfg.session.pool_size = 1
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._pool_size = 1
        mgr._pool_agent = "kirocrew"

        # Pre-fill pool with a mock provider that looks like AcpProvider
        mock_pooled = AsyncMock(spec=AcpProvider)
        mock_pooled.start = AsyncMock()
        mock_pooled.shutdown = AsyncMock()
        mock_pooled.context_usage_pct = lambda: 0.0
        mock_pooled.is_process_alive = lambda: True
        mock_pooled.client = AsyncMock()
        mock_pooled.client._model = "claude-opus-4"
        mock_pooled.client._agent = "kirocrew"
        mock_pooled.client._session_id = None
        mock_pooled.client.rekey = lambda *a, **kw: None
        mock_pooled.client.resumed = False

        mgr._warm_pool.put_nowait((mock_pooled, time.monotonic()))

        provider, is_new, _ = await mgr.get_or_create("dashboard:slot1", agent="kirocrew")
        mgr.release("dashboard:slot1")
        assert provider is mock_pooled
        assert is_new is True
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_cold_start_with_resume_sid(self, cfg):
        """get_or_create with a stored session_map entry attempts resume."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        # Mock session_map to return a resume SID
        with (
            patch.object(mgr._session_map, "get", return_value="sid-resume-test"),
            patch.object(mgr._session_map, "get_cwd", return_value=None),
            patch.object(mgr._session_map, "get_provider", return_value="acp"),
        ):
            provider, is_new, _ = await mgr.get_or_create("dashboard:slot2")
            mgr.release("dashboard:slot2")
        assert is_new is True
        assert mgr.has_session("dashboard:slot2")
        await mgr.close_all()


class TestGetOrCreateDeadProvider:
    """Test get_or_create when existing session has a dead provider."""

    @pytest.mark.asyncio
    async def test_dead_provider_gets_replaced(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        # Mark provider as dead
        provider.is_alive = lambda: False
        provider.is_process_alive = lambda: False
        # Next get_or_create should detect dead provider and create new one
        new_provider, is_new, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        assert new_provider is not provider
        await mgr.close_all()


class TestSessionTimeout:
    @pytest.mark.asyncio
    async def test_session_expires_after_timeout(self, cfg):
        cfg.session.timeout_secs = 1
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        p, is_new, _ = await mgr.get_or_create("thread1")
        mgr.release("thread1")
        # Manually backdate last_used
        async with mgr._lock:
            mgr._sessions["thread1"].last_used = time.monotonic() - 10
        await mgr._expire_idle(timeout_secs=1)
        assert mgr.count == 0
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_active_session_not_expired(self, cfg):
        cfg.session.timeout_secs = 10
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        p, is_new, _ = await mgr.get_or_create("thread1")
        mgr.release("thread1")
        await mgr._expire_idle(timeout_secs=10)
        assert mgr.count == 1
        await mgr.close_all()


class TestConcurrentAccess:
    @pytest.mark.asyncio
    async def test_concurrent_get_or_create_same_key(self, cfg):
        """Second get_or_create on same key reuses existing session."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        p1, new1, _ = await mgr.get_or_create("shared")
        mgr.release("shared")
        p2, new2, _ = await mgr.get_or_create("shared")
        mgr.release("shared")
        assert p1 is p2
        assert new1 is True
        assert new2 is False
        assert mgr.count == 1
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_concurrent_different_keys(self, cfg):
        """Different keys should create independent sessions."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        results = await asyncio.gather(  # noqa: F841
            mgr.get_or_create("a"),
            mgr.get_or_create("b"),
            mgr.get_or_create("c"),
        )
        assert mgr.count == 3
        for key in ("a", "b", "c"):
            mgr.release(key)
        await mgr.close_all()


class TestCloseSession:
    @pytest.mark.asyncio
    async def test_close_removes_session(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        p, _, _ = await mgr.get_or_create("thread1")
        mgr.release("thread1")
        await mgr.destroy("thread1")
        assert not mgr.has_session("thread1")

    @pytest.mark.asyncio
    async def test_close_nonexistent_is_noop(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.destroy("nonexistent")  # should not raise

    @pytest.mark.asyncio
    async def test_close_calls_shutdown(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        p, _, _ = await mgr.get_or_create("thread1")
        mgr.release("thread1")
        await mgr.destroy("thread1")
        p.shutdown.assert_awaited_once()


class TestCloseAll:
    @pytest.mark.asyncio
    async def test_close_all_shuts_down_all(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        p1, _, _ = await mgr.get_or_create("a")
        p2, _, _ = await mgr.get_or_create("b")
        mgr.release("a")
        mgr.release("b")
        await mgr.close_all()
        p1.shutdown.assert_awaited_once()
        p2.shutdown.assert_awaited_once()
        assert mgr.count == 0


class TestSessionState:
    @pytest.mark.asyncio
    async def test_is_new_flag(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        _, is_new1, _ = await mgr.get_or_create("t1")
        mgr.release("t1")
        _, is_new2, _ = await mgr.get_or_create("t1")
        mgr.release("t1")
        assert is_new1 is True
        assert is_new2 is False
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_release_updates_last_used(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("t1")
        mgr.release("t1")
        async with mgr._lock:
            sess = mgr._sessions["t1"]
        # last_used should be recent (within last second)
        assert time.monotonic() - sess.last_used < 1.0
        await mgr.close_all()


class TestBackgroundSession:
    @pytest.mark.asyncio
    async def test_background_key_constant(self):
        assert BACKGROUND_KEY == "_bg"

    @pytest.mark.asyncio
    async def test_ensure_background_creates_session(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr._ensure_background()
        async with mgr._lock:
            assert BACKGROUND_KEY in mgr._sessions
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_ensure_background_idempotent(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr._ensure_background()
        await mgr._ensure_background()
        # Should still only have one background session
        assert mgr.count == 1
        await mgr.close_all()


class TestContextInfoBasic:
    @pytest.mark.asyncio
    async def test_returns_session_info(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("dashboard:slot0")
        mgr.release("dashboard:slot0")
        info = mgr.context_info()
        assert len(info) >= 1
        slot_info = [i for i in info if i["key"] == "dashboard:slot0"]
        assert len(slot_info) == 1
        assert slot_info[0]["context_pct"] == 0.0
        assert "Chat" in slot_info[0]["name"]
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_background_session_name(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr._ensure_background()
        info = mgr.context_info()
        bg_info = [i for i in info if i["key"] == BACKGROUND_KEY]
        assert len(bg_info) == 1
        assert "Background" in bg_info[0]["name"]


class TestCleanupLoopResilience:
    """Tests that _cleanup_loop survives _expire_idle exceptions."""

    @pytest.mark.asyncio
    async def test_cleanup_loop_continues_after_expire_idle_crash(self, cfg):
        """If _expire_idle raises, the loop keeps running."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        cfg.session.timeout_secs = 360
        call_count = 0

        # Collapse the loop's inter-sweep sleep to ~zero WITHOUT busy-spinning.
        # The loop sleeps via ``asyncio.wait_for(shutdown_event.wait(), timeout=interval)``
        # (interval >= 60s). We shrink only THAT call to a tiny real timeout so
        # the wait actually runs: it returns immediately once shutdown_event is
        # set, and otherwise times out in ~1ms. Previously this raised
        # TimeoutError WITHOUT awaiting the wait(), which turned the loop into an
        # unbounded busy-spin — if _expire_idle's shutdown_event.set() landed on
        # a cross-loop-rebound event (after an earlier asyncio test in the same
        # process), the top-of-loop is_set() check could miss it and the test
        # would hang until its own outer deadline. Letting the real wait() run
        # makes the stop deterministic regardless of prior event-loop binding.
        # The ``timeout >= 60`` discriminator keeps this from clamping the outer
        # ``wait_for(_cleanup_loop(), timeout=5)`` guard below.
        real_wait_for = asyncio.wait_for

        async def _fast_wait_for(coro, *, timeout):
            if timeout >= 60:
                return await real_wait_for(coro, timeout=0.001)
            return await real_wait_for(coro, timeout=timeout)

        async def _expire_then_stop(timeout):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("simulated crash")
            # Force loop exit on second call
            from kiro_crew import shutdown_event

            shutdown_event.set()

        import kiro_crew

        kiro_crew.shutdown_event.clear()
        with (
            patch("asyncio.wait_for", side_effect=_fast_wait_for),
            patch.object(mgr, "_expire_idle", side_effect=_expire_then_stop),
            patch("kiro_crew.session.find_orphan_mcp_candidates", return_value=[]),
        ):
            await asyncio.wait_for(mgr._cleanup_loop(), timeout=5)

        assert call_count >= 2
        kiro_crew.shutdown_event.clear()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_cleanup_loop_logs_expire_idle_exception(self, cfg, caplog):
        """Exception in _expire_idle is logged at ERROR level."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        cfg.session.timeout_secs = 360

        # Shrink only the loop's inter-sweep sleep to a tiny REAL timeout (not a
        # coro.close()+raise) so the loop observes shutdown_event deterministically
        # instead of busy-spinning — see the note in
        # test_cleanup_loop_continues_after_expire_idle_crash.
        real_wait_for = asyncio.wait_for

        async def _fast_wait_for(coro, *, timeout):
            if timeout >= 60:
                return await real_wait_for(coro, timeout=0.001)
            return await real_wait_for(coro, timeout=timeout)

        async def _crash_and_stop(timeout):
            from kiro_crew import shutdown_event

            shutdown_event.set()
            raise ValueError("boom")

        import kiro_crew

        kiro_crew.shutdown_event.clear()
        with (
            patch("asyncio.wait_for", side_effect=_fast_wait_for),
            patch.object(mgr, "_expire_idle", side_effect=_crash_and_stop),
            patch("kiro_crew.session.find_orphan_mcp_candidates", return_value=[]),
        ):
            with caplog.at_level(logging.ERROR):
                await asyncio.wait_for(mgr._cleanup_loop(), timeout=5)

        assert "_expire_idle crashed" in caplog.text
        kiro_crew.shutdown_event.clear()
        await mgr.close_all()


class TestGetBgSessionRecycle:
    """get_bg_session() recycles a healthy-but-stale _bg runtime only when it
    has zero active sessions."""

    @pytest.mark.asyncio
    async def test_recycles_stale_idle_runtime(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())

        stale = AsyncMock()
        stale.is_alive = lambda: True
        stale.has_active_sessions = lambda: False
        stale._is_stale = AsyncMock(return_value="age")
        stale.kill = AsyncMock()
        stale.pid = 111
        mgr._bg_runtime = stale

        rt2 = AsyncMock()
        rt2.spawn = AsyncMock()
        rt2.is_alive = lambda: True
        sentinel = object()
        rt2.create_session = AsyncMock(return_value=sentinel)

        with patch("kiro_crew.acp.runtime.AcpRuntime", side_effect=[rt2]):
            result = await mgr.get_bg_session()

        stale._is_stale.assert_awaited_once()
        stale.kill.assert_awaited_once()  # stale + idle → recycled
        rt2.spawn.assert_awaited_once()  # respawned
        assert result is sentinel
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_does_not_recycle_stale_runtime_with_active_sessions(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())

        stale = AsyncMock()
        stale.is_alive = lambda: True
        stale.has_active_sessions = lambda: True
        stale._stale_by_age = lambda: True  # drives the deferral log
        stale._is_stale = AsyncMock(return_value="age")  # must NOT be consulted
        stale.kill = AsyncMock()
        stale.pid = 222
        stale._session_queues = {"s": object()}
        sentinel = object()
        stale.create_session = AsyncMock(return_value=sentinel)
        mgr._bg_runtime = stale

        # A live+reused runtime must not trigger a respawn.
        with patch(
            "kiro_crew.acp.runtime.AcpRuntime",
            side_effect=AssertionError("should not respawn a live runtime"),
        ):
            result = await mgr.get_bg_session()

        stale.kill.assert_not_awaited()  # active sessions → recycle deferred
        # The active-session path uses the cheap _stale_by_age(), NOT the
        # offloaded _is_stale() probe.
        stale._is_stale.assert_not_awaited()
        assert result is sentinel
        await mgr.close_all()


def _run_runtime_factory(created_runtimes: list):
    """Factory whose providers each carry a fully-configured shared AcpRuntime.

    In production each factory call spawns its own kiro-cli process; the task
    runner must call the factory ONCE per run and reuse that runtime for every
    step. ``created_runtimes`` records each runtime so tests can assert the
    factory ran exactly once.
    """

    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        runtime = MagicMock()
        runtime.is_alive = MagicMock(return_value=True)
        runtime.pid = 4321
        runtime.create_session = AsyncMock(
            side_effect=lambda **kw: MagicMock(session_id="step-session")
        )
        runtime.terminate_session = AsyncMock()
        runtime.kill = AsyncMock()
        created_runtimes.append(runtime)

        boot_handle = MagicMock()
        boot_handle.session_id = "bootstrap-sess"
        session_provider = MagicMock()
        session_provider._runtime = runtime
        session_provider._handle = boot_handle
        session_provider._owns_runtime = True

        provider = AsyncMock()
        provider.start = AsyncMock()
        provider.shutdown = AsyncMock()
        provider._client = session_provider
        return provider

    return factory


class TestOpenTaskSession:
    """The task runner shares ONE run-scoped AcpRuntime across all its steps."""

    @pytest.mark.asyncio
    async def test_run_shares_one_runtime_across_sessions(self, cfg):
        created: list = []
        mgr = SessionManager(cfg, provider_factory=_run_runtime_factory(created))
        parent = "taskrunner:run1:runtime"

        p1, new1, res1 = await mgr.open_task_session(
            parent, "taskrunner:run1:decompose", agent="kirocrew"
        )
        p2, new2, res2 = await mgr.open_task_session(
            parent, "taskrunner:run1:task0", agent="kirocrew"
        )

        # Exactly ONE factory-built runtime, adopted + reused for both steps.
        assert len(created) == 1
        runtime = created[0]
        assert mgr._subagent_runtimes[parent] is runtime
        # Each step opened its own isolated session on the shared runtime.
        assert runtime.create_session.await_count == 2
        # The factory provider's bootstrap session was freed (runtime kept alive).
        runtime.terminate_session.assert_awaited_once_with("bootstrap-sess")
        # Fresh, never-resumed sessions.
        assert new1 is True and new2 is True
        assert res1 is False and res2 is False

        # Release frees the shared runtime exactly once.
        mgr.release("taskrunner:run1:decompose")
        mgr.release("taskrunner:run1:task0")
        await mgr.release_subagent_runtime(parent)
        runtime.kill.assert_awaited_once()
        assert parent not in mgr._subagent_runtimes
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_open_task_session_registers_under_key(self, cfg):
        created: list = []
        mgr = SessionManager(cfg, provider_factory=_run_runtime_factory(created))
        parent = "taskrunner:run2:runtime"
        key = "taskrunner:run2:task0"

        provider, is_new, _resumed = await mgr.open_task_session(parent, key, agent="kirocrew")

        # Registered under the per-step key so reset/context helpers work by key.
        assert key in mgr._sessions
        assert mgr._sessions[key].provider is provider
        mgr.release(key)
        await mgr.release_subagent_runtime(parent)
        await mgr.close_all()


class TestLoadRecoveryHistoryReplay:
    """F2 load-recovery Phase 2: when a provider signals it fell back to a FRESH
    native session (the prior session's lock never cleared), get_or_create flags
    the new slot for KiroCrew conversation_log replay on the first prompt so the
    slot is not context-free."""

    @staticmethod
    def _factory(history_replay_needed: bool):
        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            m = AsyncMock()
            m.start = AsyncMock()
            m.shutdown = AsyncMock()
            m.context_usage_pct = lambda: 0.0
            m._history_replay_needed = history_replay_needed
            return m

        return factory

    @pytest.mark.asyncio
    async def test_fresh_fallback_triggers_history_replay(self, cfg):
        mgr = SessionManager(cfg, provider_factory=self._factory(True))
        _provider, is_new, _resumed = await mgr.get_or_create("thread1")
        assert is_new is True
        sess = next(iter(mgr._sessions.values()))
        assert sess.provider_switch_replay is True
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_native_resume_does_not_trigger_replay(self, cfg):
        mgr = SessionManager(cfg, provider_factory=self._factory(False))
        await mgr.get_or_create("thread1")
        sess = next(iter(mgr._sessions.values()))
        assert sess.provider_switch_replay is False
        await mgr.close_all()
