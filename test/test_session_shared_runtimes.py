"""SessionManager runtimes shared across keys.

Among them: ``recycle_background``'s context-overflow and blind recycle, ``_ensure_background``, the
stale-runtime recycle and backend switch of ``get_bg_session``, the retirement of a stale-backend
``_bg`` runtime, the provider-factory reload, and the run-scoped runtime ``open_task_session``
shares, including how a dead or stalled one is replaced.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from session_helpers import (  # noqa: F401 -- the fixture is requested by name
    _mock_provider_factory,
    cfg_fixture,
)

from kiro_crew.acp.runtime import AcpWorkspaceBindingError
from kiro_crew.acp.types import ACP_BACKEND_KAS, ACP_BACKEND_KIRO, AcpPromptStats
from kiro_crew.config import KiroCrewConfig
from kiro_crew.session import (
    _BG_BLIND_RECYCLE_PROMPTS,
    BACKGROUND_KEY,
    CONSOLIDATE_KEY,
    SessionClosingError,
    SessionManager,
)
from kiro_crew.start_priority import StartPriority


class TestRecycleBackground:
    """Tests for background session context overflow recycling."""

    @pytest.mark.asyncio
    async def test_consolidation_recycles_its_own_provider(self, cfg):
        factory = MagicMock(side_effect=_mock_provider_factory())
        mgr = SessionManager(cfg, provider_factory=factory)
        try:
            background, _, _ = await mgr.get_or_create(BACKGROUND_KEY)
            mgr.release(BACKGROUND_KEY)
            extraction, _, _ = await mgr.get_or_create(CONSOLIDATE_KEY, agent="kirocrew-lite")
            mgr.release(CONSOLIDATE_KEY)
            extraction.context_usage_pct = lambda: 75.0
            await mgr.recycle_background(session_key=CONSOLIDATE_KEY, agent="kirocrew-lite")
            assert mgr._sessions[CONSOLIDATE_KEY].provider is not extraction
            extraction.shutdown.assert_awaited_once()
            assert mgr._sessions[BACKGROUND_KEY].provider is background
            background.shutdown.assert_not_awaited()
            factory.assert_called_with(CONSOLIDATE_KEY, agent="kirocrew-lite")
        finally:
            await mgr.close_all()

    @pytest.mark.asyncio
    async def test_recycle_on_high_context(self, cfg):
        """Background session is recycled when context >= 70%."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.start_pool()
        await mgr._ensure_background()

        old_provider = mgr._sessions[BACKGROUND_KEY].provider
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
        await mgr._ensure_background()

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
        await mgr._ensure_background()

        old_provider = mgr._sessions[BACKGROUND_KEY].provider
        old_provider.context_usage_pct = lambda: 30.0

        await mgr.recycle_background()

        old_provider.shutdown.assert_not_awaited()
        assert mgr._sessions[BACKGROUND_KEY].provider is old_provider
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_recycle_no_background_session(self, cfg):
        """recycle_background() is no-op when no background session exists."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.recycle_background()
        assert not mgr.has_session(BACKGROUND_KEY), "recycling must not create one"
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
        await mgr._ensure_background()

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
        await mgr._ensure_background()

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
        await mgr._ensure_background()

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
                    # flake-ok: zero-length yield in the waiter's 200-pass turn loop that watches for a mid-turn shutdown; a yield budget not a wall clock; moved verbatim
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
        # flake-ok: one zero-length yield runs the waiter task so it takes the free turn semaphore before the recycle task exists; moved verbatim
        await asyncio.sleep(0)  # let the waiter take the semaphore

        recycle = asyncio.create_task(_recycle())
        # flake-ok: pre-existing unbounded await of the recycle task; the waiter frees the turn semaphore after 200 yields; moved verbatim; tracked for a de-flake pass
        await recycle
        # flake-ok: pre-existing unbounded await of the waiter task whose turn loop ends within 200 yields; moved verbatim; tracked for a de-flake pass
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
        await mgr._ensure_background()

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
        await mgr._ensure_background()

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


class TestReloadProviderFactory:
    """Tests for reload_provider_factory."""

    @pytest.mark.asyncio
    async def test_reload_clears_sessions_and_pool(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.release("k1")
        # Put something in warm pool
        mock_pool_p = AsyncMock()
        mock_pool_p.is_process_alive = lambda: False
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
        assert not mgr.has_session("k1")
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
            await mgr.reload_provider_factory()

        provider.shutdown.assert_awaited_once()
        assert not mgr.has_session("k1"), "the stale session is dropped despite the failure"
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
        assert mgr.count == 1
        await mgr.close_all()


class TestGetBgSessionRecycle:
    """get_bg_session() displaces a healthy-but-stale _bg runtime, killing it
    when idle and parking it to drain when its handles are still live."""

    @pytest.mark.asyncio
    async def test_recycles_stale_idle_runtime(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())

        stale = AsyncMock()
        stale.is_alive = lambda: True
        stale.has_active_or_initializing_sessions = lambda: False
        stale._is_stale = AsyncMock(return_value="age")
        stale.kill = AsyncMock()
        # flake-ok: pre-existing pid on the stale _bg runtime double; kill() is an AsyncMock and the recycle logs the pid; moved verbatim; tracked for a de-flake pass
        # nosemgrep: kirocrew.mock-pid-allocatable -- pre-existing literal pid on the stale _bg runtime double whose kill is an AsyncMock; no os.kill patch; moved verbatim; tracked for a de-flake pass
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
    async def test_parks_a_stale_runtime_that_still_has_active_sessions(self, cfg):
        """A runtime that never goes idle must still be bounded.

        The old policy only recycled during a zero-session window and merely
        logged otherwise, so under sustained background load the age/RSS caps
        were never enforced. Now the retiree is detached from the slot — its
        in-flight work finishes untouched — and new callers get a fresh process.
        Staleness is probed with ``_is_stale()`` (age OR RSS), not the age-only
        ``_stale_by_age()``, because RSS is the growth mode that was observed.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())

        stale = AsyncMock()
        stale.is_alive = lambda: True
        stale.has_active_or_initializing_sessions = lambda: True
        # Inside the age cap; stale by RSS.
        stale._is_stale = AsyncMock(return_value="rss")
        stale.kill = AsyncMock()
        # flake-ok: pre-existing pid on the stale busy _bg runtime double that is parked not killed; kill() is an AsyncMock; moved verbatim; tracked for a de-flake pass
        # nosemgrep: kirocrew.mock-pid-allocatable -- pre-existing literal pid on a parked _bg runtime double whose kill is an AsyncMock; no os.kill patch; moved verbatim; tracked for a de-flake pass
        stale.pid = 222
        stale.create_session = AsyncMock(return_value=object())
        mgr._bg_runtime = stale

        rt2 = AsyncMock()
        rt2.spawn = AsyncMock()
        rt2.is_alive = lambda: True
        sentinel = object()
        rt2.create_session = AsyncMock(return_value=sentinel)

        with patch("kiro_crew.acp.runtime.AcpRuntime", side_effect=[rt2]):
            result = await mgr.get_bg_session()

        stale._is_stale.assert_awaited_once()
        stale.kill.assert_not_awaited()  # live handles → parked, not killed
        stale.create_session.assert_not_awaited()  # and never serves again
        assert stale in mgr._draining_bg_runtimes
        assert result is sentinel
        mgr._draining_bg_runtimes = []
        await mgr.close_all()


class TestGetBgSessionBackendSwitch:
    """The _bg runtime spawns under the CONFIGURED ``agent.acp_backend``, and a
    cached runtime spawned under a different backend is recycled once idle —
    otherwise background work (chat titles, suggestions, consolidation) keeps
    running the previous backend indefinitely."""

    @staticmethod
    def _fresh_runtime():
        rt = AsyncMock()
        rt.spawn = AsyncMock()
        rt.is_alive = lambda: True
        rt.create_session = AsyncMock(return_value=object())
        return rt

    @pytest.mark.asyncio
    @pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_KAS])
    async def test_runtime_spawns_under_the_configured_backend(self, cfg, backend):
        cfg.agent.acp_backend = backend
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        rt = self._fresh_runtime()

        with patch("kiro_crew.acp.runtime.AcpRuntime", return_value=rt) as ctor:
            result = await mgr.get_bg_session()

        assert ctor.call_args.kwargs["acp_backend"] == backend
        assert result is rt.create_session.return_value
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_recycles_an_idle_runtime_spawned_under_a_different_backend(self, cfg):
        cfg.agent.acp_backend = ACP_BACKEND_KAS
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())

        stranded = AsyncMock()
        stranded.is_alive = lambda: True
        stranded.has_active_sessions = lambda: False
        stranded.has_active_or_initializing_sessions = lambda: False
        stranded.acp_backend = ACP_BACKEND_KIRO  # spawned before the switch
        stranded._is_stale = AsyncMock(return_value=None)  # must NOT be consulted
        stranded.kill = AsyncMock()
        # flake-ok: pre-existing pid on the old-backend runtime double; kill() is an AsyncMock and the recycle logs the pid; moved verbatim; tracked for a de-flake pass
        # nosemgrep: kirocrew.mock-pid-allocatable -- pre-existing literal pid on a recycled _bg runtime double whose kill is an AsyncMock; no os.kill patch; moved verbatim; tracked for a de-flake pass
        stranded.pid = 333
        mgr._bg_runtime = stranded

        rt2 = self._fresh_runtime()
        with patch("kiro_crew.acp.runtime.AcpRuntime", return_value=rt2) as ctor:
            result = await mgr.get_bg_session()

        stranded.kill.assert_awaited_once()  # mismatched + idle → recycled
        stranded._is_stale.assert_not_awaited()  # mismatch outranks staleness
        assert ctor.call_args.kwargs["acp_backend"] == ACP_BACKEND_KAS
        assert result is rt2.create_session.return_value
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_busy_mismatched_runtime_is_parked_and_never_serves_a_new_caller(self, cfg):
        """A post-switch caller must never create_session() on the old-backend
        runtime — under sustained load a busy runtime never reaches a
        zero-session window, so waiting for one would let the switch never
        take effect. Its in-flight handles are not killed either."""
        cfg.agent.acp_backend = ACP_BACKEND_KAS
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())

        busy = AsyncMock()
        busy.is_alive = lambda: True
        busy.has_active_sessions = lambda: True
        busy.has_active_or_initializing_sessions = lambda: True
        busy.acp_backend = ACP_BACKEND_KIRO  # spawned before the switch
        busy.kill = AsyncMock()
        busy.create_session = AsyncMock()
        # flake-ok: pre-existing pid on the busy old-backend runtime double that is parked; kill() is an AsyncMock; moved verbatim; tracked for a de-flake pass
        # nosemgrep: kirocrew.mock-pid-allocatable -- pre-existing literal pid on a parked _bg runtime double whose kill is an AsyncMock; no os.kill patch; moved verbatim; tracked for a de-flake pass
        busy.pid = 335
        mgr._bg_runtime = busy

        rt2 = self._fresh_runtime()
        with patch("kiro_crew.acp.runtime.AcpRuntime", return_value=rt2) as ctor:
            result = await mgr.get_bg_session()

        busy.kill.assert_not_awaited()  # live handles are never killed
        busy.create_session.assert_not_awaited()  # new work goes to the new runtime
        assert busy in mgr._draining_bg_runtimes
        assert ctor.call_args.kwargs["acp_backend"] == ACP_BACKEND_KAS
        assert result is rt2.create_session.return_value
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_parked_runtime_is_reaped_once_its_handles_drain(self, cfg):
        cfg.agent.acp_backend = ACP_BACKEND_KAS
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())

        drained = AsyncMock()
        drained.is_alive = lambda: True
        drained.has_active_or_initializing_sessions = lambda: False
        drained.kill = AsyncMock()
        mgr._draining_bg_runtimes = [drained]

        still_busy = AsyncMock()
        still_busy.is_alive = lambda: True
        still_busy.has_active_or_initializing_sessions = lambda: True
        still_busy.kill = AsyncMock()
        mgr._draining_bg_runtimes.append(still_busy)

        rt = self._fresh_runtime()
        with patch("kiro_crew.acp.runtime.AcpRuntime", return_value=rt):
            await mgr.get_bg_session()

        drained.kill.assert_awaited_once()  # drained → reaped
        still_busy.kill.assert_not_awaited()  # busy → stays parked
        assert mgr._draining_bg_runtimes == [still_busy]
        mgr._draining_bg_runtimes = []
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_failed_reap_keeps_the_runtime_parked(self, cfg):
        """Dropping a parked runtime whose kill failed would orphan a possibly
        live process outside every sweep; keeping it parked retries the kill
        on the next pass."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())

        stuck = AsyncMock()
        stuck.is_alive = lambda: True
        stuck.has_active_or_initializing_sessions = lambda: False
        stuck.kill = AsyncMock(side_effect=RuntimeError("boom"))
        mgr._draining_bg_runtimes = [stuck]

        async with mgr._bg_runtime_lock:
            await mgr._reap_drained_bg_runtimes_locked()

        assert mgr._draining_bg_runtimes == [stuck]
        mgr._draining_bg_runtimes = []
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_matching_backend_is_reused_not_recycled(self, cfg):
        cfg.agent.acp_backend = ACP_BACKEND_KAS
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())

        cached = AsyncMock()
        cached.is_alive = lambda: True
        cached.has_active_sessions = lambda: False
        cached.acp_backend = ACP_BACKEND_KAS
        cached._is_stale = AsyncMock(return_value=None)
        cached.kill = AsyncMock()
        # flake-ok: pre-existing pid on the matching-backend _bg runtime double that is reused; kill() is an AsyncMock; moved verbatim; tracked for a de-flake pass
        # nosemgrep: kirocrew.mock-pid-allocatable -- pre-existing literal pid on a reused _bg runtime double whose kill is an AsyncMock; no os.kill patch; moved verbatim; tracked for a de-flake pass
        cached.pid = 334
        sentinel = object()
        cached.create_session = AsyncMock(return_value=sentinel)
        mgr._bg_runtime = cached

        with patch(
            "kiro_crew.acp.runtime.AcpRuntime",
            side_effect=AssertionError("should not respawn a matching runtime"),
        ):
            result = await mgr.get_bg_session()

        cached.kill.assert_not_awaited()
        assert result is sentinel
        await mgr.close_all()


class TestRetireStaleBackendBgRuntime:
    """A backend switch retires the cached _bg runtime only once its live
    handles drain — killing it mid-turn would abort an in-flight title
    generation belonging to a caller unrelated to the switch."""

    @staticmethod
    def _runtime(*, backend, busy):
        rt = AsyncMock()
        rt.acp_backend = backend
        rt.has_active_or_initializing_sessions = lambda: busy
        rt.kill = AsyncMock()
        # flake-ok: pre-existing pid on the retire tests' runtime double; kill() is an AsyncMock and the retire logs the pid; moved verbatim; tracked for a de-flake pass
        # nosemgrep: kirocrew.mock-pid-allocatable -- pre-existing literal pid on the retire tests' runtime double whose kill is an AsyncMock; no os.kill patch; moved verbatim; tracked for a de-flake pass
        rt.pid = 444
        return rt

    @pytest.mark.asyncio
    async def test_idle_mismatched_runtime_is_retired(self, cfg):
        cfg.agent.acp_backend = ACP_BACKEND_KAS
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        rt = self._runtime(backend=ACP_BACKEND_KIRO, busy=False)
        mgr._bg_runtime = rt

        await mgr._retire_stale_backend_bg_runtime()

        rt.kill.assert_awaited_once()
        assert mgr._bg_runtime is None
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_live_handle_is_never_killed_by_a_backend_switch(self, cfg):
        """The busy runtime is parked to drain — its slot is freed so new work
        runs under the configured backend, but its in-flight handles finish."""
        cfg.agent.acp_backend = ACP_BACKEND_KAS
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        rt = self._runtime(backend=ACP_BACKEND_KIRO, busy=True)
        mgr._bg_runtime = rt

        await mgr._retire_stale_backend_bg_runtime()

        rt.kill.assert_not_awaited()
        assert mgr._bg_runtime is None
        assert rt in mgr._draining_bg_runtimes
        mgr._draining_bg_runtimes = []
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_matching_backend_is_left_alone(self, cfg):
        cfg.agent.acp_backend = ACP_BACKEND_KAS
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        rt = self._runtime(backend=ACP_BACKEND_KAS, busy=False)
        mgr._bg_runtime = rt

        await mgr._retire_stale_backend_bg_runtime()

        rt.kill.assert_not_awaited()
        assert mgr._bg_runtime is rt
        mgr._bg_runtime = None
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_fails_closed_on_a_holder_without_a_string_backend(self, cfg):
        """A holder that does not declare a string acp_backend (a test double,
        a future holder) is left running rather than recycled on a backend it
        may never have had."""
        cfg.agent.acp_backend = ACP_BACKEND_KAS
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        rt = self._runtime(backend=object(), busy=False)
        mgr._bg_runtime = rt

        await mgr._retire_stale_backend_bg_runtime()

        rt.kill.assert_not_awaited()
        assert mgr._bg_runtime is rt
        mgr._bg_runtime = None
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_failed_kill_parks_the_runtime_for_the_reaper(self, cfg):
        """Dropping the reference after a failed kill would orphan a live
        process outside every sweep; parking it retries the kill later while
        keeping its PID shielded."""
        cfg.agent.acp_backend = ACP_BACKEND_KAS
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        rt = self._runtime(backend=ACP_BACKEND_KIRO, busy=False)
        rt.kill = AsyncMock(side_effect=RuntimeError("boom"))
        mgr._bg_runtime = rt

        await mgr._retire_stale_backend_bg_runtime()

        assert mgr._bg_runtime is None
        assert rt in mgr._draining_bg_runtimes
        mgr._draining_bg_runtimes = []
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_refresh_defaults_triggers_the_retirement(self, cfg):
        """refresh_defaults() re-reads config, so any invocation of it (and any
        future agent.acp_backend edit surface routed through it) must also run
        the retirement check."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())

        with (
            patch.object(mgr, "start_pool", AsyncMock()),
            patch.object(mgr, "_retire_stale_backend_bg_runtime", AsyncMock()) as retire,
            patch("kiro_crew.session.build_provider_factory", return_value=MagicMock()),
            patch("kiro_crew.session.KiroCrewConfig.load", return_value=cfg),
        ):
            await mgr.refresh_defaults()

        retire.assert_awaited_once()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_identity_sweep_incomplete_while_a_parked_runtime_drains(self, cfg):
        """A parked runtime still runs under the previous account, so the
        identity baseline must not advance past it (advancing would record the
        switch as handled and no later turn would re-sweep); once its handles
        drain it is reaped and completeness is restored."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        parked = AsyncMock()
        parked.uses_kiro_identity_store = True
        parked.is_alive = lambda: True
        parked.has_active_or_initializing_sessions = lambda: True
        parked.kill = AsyncMock()
        mgr._draining_bg_runtimes = [parked]

        assert await mgr._retire_kiro_bg_runtime() is False
        parked.kill.assert_not_awaited()

        parked.has_active_or_initializing_sessions = lambda: False
        assert await mgr._retire_kiro_bg_runtime() is True
        parked.kill.assert_awaited_once()
        assert mgr._draining_bg_runtimes == []
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_periodic_hook_reaps_a_drained_parked_runtime(self, cfg):
        """The watchdog hook is the backstop for an idle gateway where no
        background call, refresh, or identity sweep ever runs the other reap
        triggers — without it a drained parked runtime sits shielded from the
        orphan sweep indefinitely."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        assert any(h.name == "bg_drain_reap" for h in mgr._watchdog._hooks)

        drained = AsyncMock()
        drained.is_alive = lambda: True
        drained.has_active_or_initializing_sessions = lambda: False
        drained.kill = AsyncMock()
        mgr._draining_bg_runtimes = [drained]

        await mgr._bg_drain_reap_hook()

        drained.kill.assert_awaited_once()
        assert mgr._draining_bg_runtimes == []
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_close_all_kills_the_slot_and_every_parked_runtime(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        slot = AsyncMock()
        slot.kill = AsyncMock()
        parked = AsyncMock()
        parked.kill = AsyncMock()
        mgr._bg_runtime = slot
        mgr._draining_bg_runtimes = [parked]

        await mgr.close_all()

        slot.kill.assert_awaited_once()
        parked.kill.assert_awaited_once()
        assert mgr._bg_runtime is None
        assert mgr._draining_bg_runtimes == []

    @pytest.mark.asyncio
    async def test_get_bg_session_refuses_while_closing(self, cfg):
        """A runtime spawned or parked after close_all's locked detach would
        leak until the next-startup orphan reaper. The error is the typed
        SessionClosingError so shutdown-aware handlers classify it."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._closing = True

        with pytest.raises(SessionClosingError):
            await mgr.get_bg_session()

        mgr._closing = False
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_retire_helper_does_not_park_while_closing(self, cfg):
        """refresh_defaults (or the provider path) racing close_all must not
        append to a draining list the shutdown sweep has already cleared."""
        cfg.agent.acp_backend = ACP_BACKEND_KAS
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        rt = self._runtime(backend=ACP_BACKEND_KIRO, busy=True)
        mgr._bg_runtime = rt
        mgr._closing = True

        await mgr._retire_stale_backend_bg_runtime()

        assert mgr._draining_bg_runtimes == []
        assert mgr._bg_runtime is rt  # left for close_all's own detach
        mgr._closing = False
        mgr._bg_runtime = None
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_an_unreadable_backend_never_displaces_a_cached_runtime(self, cfg):
        """An unreadable probe must not assert a backend it did not read: a
        correctly-configured KAS runtime survives a config edge instead of
        being invisibly recycled onto kiro."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())

        class _Boom:
            @property
            def acp_backend(self):
                raise RuntimeError("config exploded")

        from types import SimpleNamespace

        mgr._cfg = SimpleNamespace(agent=_Boom())
        rt = self._runtime(backend=ACP_BACKEND_KAS, busy=False)
        mgr._bg_runtime = rt

        await mgr._retire_stale_backend_bg_runtime()

        rt.kill.assert_not_awaited()
        assert mgr._bg_runtime is rt
        assert mgr._draining_bg_runtimes == []
        mgr._bg_runtime = None
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_backend_moving_under_the_lock_falls_back_to_the_provider_path(self, cfg):
        """If the config moves to a backend the runtime cannot serve between
        dispatch and the lock, the caller must not get a runtime constructed
        under a backend it cannot classify — it is served through the
        provider-backed path instead."""
        cfg.agent.acp_backend = "claude"  # non-runtime; assigned directly, loader normalizes
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider_handle = object()

        with (
            # Dispatch saw a runtime-capable backend...
            patch.object(mgr, "_bg_backend_supports_runtime", lambda: True),
            # ...but the in-lock revalidation reads the moved config and must
            # divert to the provider path without constructing a runtime.
            patch.object(
                mgr, "_provider_backed_bg_session", AsyncMock(return_value=provider_handle)
            ),
            patch(
                "kiro_crew.acp.runtime.AcpRuntime",
                side_effect=AssertionError("must not construct a runtime for a moved backend"),
            ),
        ):
            result = await mgr.get_bg_session()

        assert result is provider_handle
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_ensure_background_does_not_register_a_provider_while_closing(self, cfg):
        """A provider whose start spans close_all's session snapshot must be
        torn down, not registered — a session registered after the snapshot
        escapes graceful cleanup."""
        started = AsyncMock()

        def _factory(*a, **k):
            return started

        mgr = SessionManager(cfg, provider_factory=_factory)

        real_start = started.start

        async def _start_then_close():
            mgr._closing = True
            await real_start()

        started.start = _start_then_close

        await mgr._ensure_background()

        assert BACKGROUND_KEY not in mgr._sessions
        started.shutdown.assert_awaited_once()
        mgr._closing = False
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
        # flake-ok: pre-existing pid on the task-run runtime double; kill() is an AsyncMock and nothing patches os.kill; moved verbatim; tracked for a de-flake pass
        # nosemgrep: kirocrew.mock-pid-allocatable -- pre-existing literal pid on the task-run runtime double; no os.kill or process-table patch; moved verbatim; tracked for a de-flake pass
        runtime.pid = 4321
        runtime.create_session = AsyncMock(
            side_effect=lambda **kw: MagicMock(
                session_id="step-session", memory_mode=kw.get("memory_mode", "persistent")
            )
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

    @pytest.mark.asyncio
    async def test_open_task_session_reserves_key_while_creating(self, cfg):
        created: list = []
        mgr = SessionManager(cfg, provider_factory=_run_runtime_factory(created))
        parent = "taskrunner:run3:runtime"
        first = "taskrunner:run3:decompose"
        key = "taskrunner:run3:task0"
        await mgr.open_task_session(parent, first, agent="kirocrew")
        runtime = created[0]
        inner = runtime.create_session.side_effect
        seen: dict[str, object] = {}

        def observe(**kw):
            if kw.get("session_key") == key:
                seen["reserved"] = mgr._has_allocation_reservation(key)
                seen["listed"] = key in mgr.session_keys()
                seen["generation"] = mgr.session_generation(key)
            return inner(**kw)

        runtime.create_session.side_effect = observe
        before = mgr.session_generation(key)
        await mgr.open_task_session(parent, key, agent="kirocrew")

        # The create window is visible to the registry, and the reservation is
        # dropped once the session is registered.
        assert seen["reserved"] is True
        assert seen["listed"] is True
        assert seen["generation"] > before
        assert not mgr._has_allocation_reservation(key)
        assert key in mgr._sessions
        mgr.release(key)
        mgr.release(first)
        await mgr.release_subagent_runtime(parent)
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_open_task_session_drops_reservation_when_create_fails(self, cfg):
        created: list = []
        mgr = SessionManager(cfg, provider_factory=_run_runtime_factory(created))
        parent = "taskrunner:run4:runtime"
        first = "taskrunner:run4:decompose"
        key = "taskrunner:run4:task0"
        await mgr.open_task_session(parent, first, agent="kirocrew")
        created[0].create_session.side_effect = RuntimeError("create failed")

        with pytest.raises(RuntimeError, match="create failed"):
            await mgr.open_task_session(parent, key, agent="kirocrew")

        assert not mgr._has_allocation_reservation(key)
        assert key not in mgr.session_keys()
        mgr.release(first)
        await mgr.release_subagent_runtime(parent)
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_macos_workspace_mismatch_uses_dedicated_provider(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        runtime = MagicMock()
        runtime.create_session = AsyncMock(
            side_effect=AcpWorkspaceBindingError("exact workspace required")
        )
        mgr._get_or_bootstrap_run_runtime = AsyncMock(return_value=runtime)
        dedicated = MagicMock()
        mgr.get_or_create = AsyncMock(return_value=(dedicated, True, False))

        result = await mgr.open_task_session(
            "taskrunner:run3:runtime",
            "taskrunner:run3:task0",
            agent="kirocrew",
            cwd="/repo/packages/app",
            approval_policy="auto",
        )

        assert result == (dedicated, True, False)
        mgr.get_or_create.assert_awaited_once_with(
            "taskrunner:run3:task0",
            agent="kirocrew",
            approval_policy="auto",
            cwd="/repo/packages/app",
            start_priority=StartPriority.BACKGROUND,
        )


@pytest.mark.asyncio
async def test_a_dead_run_runtime_is_killed_before_its_replacement_starts(cfg) -> None:
    """A task-run runtime marked dead on a stdin stall still has a live child
    that may read the frames left in its pipe, so the next step's bootstrap
    kills it BEFORE starting the replacement, never beside it."""
    order: list[str] = []
    base = _mock_provider_factory()

    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        order.append("spawn")
        return base(session_key, agent, channel_id, **kwargs)

    mgr = SessionManager(cfg, provider_factory=factory)
    dead = MagicMock()
    dead.is_alive = lambda: False

    async def _kill(**_kw):
        order.append("kill")

    dead.kill = _kill
    mgr._subagent_runtimes["taskrunner:run"] = dead
    mgr.get_subagent_runtime = AsyncMock(return_value=MagicMock())  # type: ignore[method-assign]

    await mgr._get_or_bootstrap_run_runtime("taskrunner:run")

    assert order[:2] == ["kill", "spawn"], order


@pytest.mark.parametrize("confirmed", [False, True])
@pytest.mark.asyncio
async def test_a_stalled_run_runtime_is_replaced_only_once_confirmed_dead(cfg, confirmed) -> None:
    """A runtime that died of a stdin stall keeps a child that may still read the
    stalled frames, so its replacement starts only once no process that can read
    that pipe survives; otherwise the step's retry ladder gets an error and runs
    the kill again."""
    spawned: list[str] = []
    base = _mock_provider_factory()

    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        spawned.append("spawn")
        return base(session_key, agent, channel_id, **kwargs)

    mgr = SessionManager(cfg, provider_factory=factory)
    dead = MagicMock()
    dead.is_alive = lambda: False
    dead.stdin_stall_death = True
    dead.stdin_reader_may_live = lambda: not confirmed
    dead.kill = AsyncMock()
    mgr._subagent_runtimes["taskrunner:run"] = dead
    mgr.get_subagent_runtime = AsyncMock(return_value=MagicMock())  # type: ignore[method-assign]

    if confirmed:
        await mgr._get_or_bootstrap_run_runtime("taskrunner:run")
        assert spawned == ["spawn"]
    else:
        with pytest.raises(RuntimeError, match="could not be confirmed dead"):
            await mgr._get_or_bootstrap_run_runtime("taskrunner:run")
        assert spawned == []
    dead.kill.assert_awaited_once()
