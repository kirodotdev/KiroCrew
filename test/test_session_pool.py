"""Tests for warm session pool (session.pool_size / session.pool_agent)."""

from __future__ import annotations

import asyncio
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from kiro_crew import platform_compat
from kiro_crew.acp.session_handle import WatchdogSettings
from kiro_crew.start_priority import PrioritySemaphore, StartPriority
from kiro_crew.testing.wait import default_timeout


async def _await_test(awaitable, what: str):
    try:
        async with asyncio.timeout(default_timeout()) as deadline:
            return await awaitable
    except TimeoutError as exc:
        if not deadline.expired():
            raise
        raise AssertionError(f"timed out waiting for {what}") from exc


@pytest.fixture(autouse=True)
def _isolate_config_dir(tmp_path, monkeypatch):
    """Point config_dir() at a throwaway dir so SessionManager's SessionMap
    writes to a per-test ``session_map.json`` instead of the real
    ``~/.kirocrew/session_map.json``.

    Without this, every test reuses key ``"test-key"``: a pool-claim test
    persists a ``claude_code`` session_map entry (which ``SessionMap.get``
    returns without a kiro-file existence check), and a later test reading
    the same key then sees a truthy ``resume_sid`` and bypasses the warm
    pool — making ``assert provider is pooled`` fail nondeterministically
    under xdist. Isolating config_dir also stops the suite from polluting
    the developer's real ``~/.kirocrew``.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "kirocrew_home"))


def _make_cfg(
    pool_size: int = 2, pool_agent: str = "kirocrew", pool_ttl_secs: int = 1800
) -> MagicMock:
    cfg = MagicMock()
    cfg.session.pool_size = pool_size
    cfg.session.pool_agent = pool_agent
    cfg.session.pool_ttl_secs = pool_ttl_secs
    cfg.session.timeout_secs = 3600
    cfg.agent.default_agent = ""
    cfg.agent.model = "auto"  # match real KiroCrewConfig default
    return cfg


def _make_provider() -> MagicMock:
    p = MagicMock()
    p.start = AsyncMock()
    p.shutdown = AsyncMock()
    p.is_process_alive = MagicMock(return_value=True)
    p.exit_code = None
    # session_map persistence reads provider.cwd (the LLMProvider ABC accessor);
    # a bare MagicMock returns a non-serializable Mock, so pin it to a string
    # like a real provider with no work dir.
    p.cwd = ""
    return p


def _make_manager(pool_size: int = 2, pool_agent: str = "kirocrew", pool_ttl_secs: int = 1800):
    from kiro_crew.session import SessionManager

    cfg = _make_cfg(pool_size, pool_agent, pool_ttl_secs)
    factory = MagicMock(side_effect=lambda *a, **kw: _make_provider())
    with patch(
        "kiro_crew.session.default_project_dir", return_value="/home/user/.kirocrew/workspace"
    ):
        mgr = SessionManager(cfg, provider_factory=factory)
    return mgr, factory


# ---------------------------------------------------------------------------
# _fill_warm_pool
# ---------------------------------------------------------------------------


class TestMemberContextAllocation:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "key", ["cron:review", "subagent:review", "memory-consolidation:review:unique"]
    )
    async def test_member_context_captures_native_sources_before_start_without_pool(self, key):
        from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef

        execution = ExecutionContext(
            "review", MemoryStoreRef("member-review", "review"), "member", "kirocrew"
        )
        mgr, factory = _make_manager()
        provider = _make_provider()
        factory.side_effect = None
        factory.return_value = provider
        mgr._drain_and_claim = AsyncMock()
        mgr._ensure_cleanup_task = MagicMock()

        async def start():
            assert provider.memory_mode == "persistent"

        provider.start.side_effect = start
        with patch("kiro_crew.execution_context.read_session_execution", return_value=execution):
            actual, is_new, _ = await mgr.get_or_create(key, agent="kirocrew")
            assert actual is provider and is_new
            mgr.release(key)
            reused, is_new, _ = await mgr.get_or_create(key, agent="kirocrew")
            assert reused is provider and not is_new
            mgr.release(key)
        mgr._drain_and_claim.assert_not_awaited()
        factory.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["incognito", "temporary"])
    async def test_restricted_task_uses_fresh_provider_instead_of_parent_runtime(self, mode):
        from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef

        execution = ExecutionContext(
            "review", MemoryStoreRef("member-review", "review"), "member", "kirocrew", mode
        )
        mgr, _ = _make_manager()
        expected = (_make_provider(), True, False)
        mgr.get_or_create = AsyncMock(return_value=expected)
        mgr._get_or_bootstrap_run_runtime = AsyncMock()
        with patch("kiro_crew.execution_context.read_session_execution", return_value=execution):
            result = await mgr.open_task_session(
                "task:parent", "task:child", agent="review", cwd="/work"
            )
        assert result is expected
        mgr.get_or_create.assert_awaited_once_with(
            "task:child",
            agent="review",
            approval_policy="",
            cwd="/work",
            start_priority=StartPriority.BACKGROUND,
        )
        mgr._get_or_bootstrap_run_runtime.assert_not_awaited()


class TestFillWarmPool:
    @pytest.mark.asyncio
    async def test_fills_to_pool_size(self):
        mgr, factory = _make_manager(pool_size=3)
        await mgr._fill_warm_pool()

        assert mgr._warm_pool.qsize() == 3
        assert factory.call_count == 3
        # Each provider should have been started
        for _ in range(3):
            p, spawn_time = mgr._warm_pool.get_nowait()
            p.start.assert_awaited_once()
            assert spawn_time > 0

    @pytest.mark.asyncio
    async def test_noop_when_pool_size_zero(self):
        mgr, factory = _make_manager(pool_size=0)
        await mgr._fill_warm_pool()

        assert mgr._warm_pool.qsize() == 0
        factory.assert_not_called()

    @pytest.mark.asyncio
    async def test_stops_on_spawn_failure(self):
        mgr, factory = _make_manager(pool_size=3)
        call_count = 0
        failed_providers: list = []

        def _factory(*a, **kw):
            nonlocal call_count
            call_count += 1
            p = _make_provider()
            if call_count == 2:
                p.start = AsyncMock(side_effect=RuntimeError("spawn failed"))
                failed_providers.append(p)
            return p

        factory.side_effect = _factory
        await mgr._fill_warm_pool()

        # Should have 1 successful + 1 failed (breaks loop)
        assert mgr._warm_pool.qsize() == 1
        assert len(failed_providers) == 1
        failed_providers[0].shutdown.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cancelled_error_cleans_up_via_finally(self):
        mgr, factory = _make_manager(pool_size=1)
        provider = _make_provider()
        provider.start = AsyncMock(side_effect=asyncio.CancelledError)
        # shutdown also raises CancelledError (the real scenario)
        provider.shutdown = AsyncMock(side_effect=asyncio.CancelledError)
        factory.side_effect = lambda *a, **kw: provider

        with patch("kiro_crew.session._sync_kill_provider") as mock_kill:
            with pytest.raises(asyncio.CancelledError):
                await mgr._fill_warm_pool()
            # The hard kill is dispatched fire-and-forget to the subprocess
            # executor (a cancellation handler can neither await nor block the
            # loop) — give the worker thread a beat to run it.
            for _ in range(100):
                if mock_kill.call_count:
                    break
                await asyncio.sleep(0.01)
            mock_kill.assert_called_once_with(provider)
        assert mgr._warm_pool.qsize() == 0


# ---------------------------------------------------------------------------
# Liveness drain loop
# ---------------------------------------------------------------------------


class TestLivenessDrainLoop:
    @pytest.mark.asyncio
    async def test_a_provider_queued_before_an_explicit_default_rewrite_is_discarded(self):
        """It read cli.json at its own spawn, so it would run the entry just removed."""
        mgr, _ = _make_manager(pool_agent="kirocrew")
        stale = _make_provider()
        mgr._warm_pool.put_nowait((stale, time.monotonic()))

        with mgr.fence_effort_overlay_rewrite():
            pass

        assert await mgr._drain_and_claim("kirocrew") is None
        stale.shutdown.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_provider_is_claimed_while_an_explicit_default_rewrite_is_in_progress(self):
        """The file can change at any point in the write, so no queued runtime is known fresh."""
        mgr, _ = _make_manager(pool_agent="kirocrew")
        queued = _make_provider()
        mgr._warm_pool.put_nowait((queued, time.monotonic() + 3600))

        with mgr.fence_effort_overlay_rewrite():
            assert await mgr._drain_and_claim("kirocrew") is None

        queued.shutdown.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_provider_spawned_during_an_explicit_default_rewrite_is_discarded(
        self, manual_clock, monkeypatch
    ):
        """Its kiro-cli may have read the file before the write landed."""
        from kiro_crew import session_pool

        manual_clock.install(monkeypatch, session_pool)
        mgr, _ = _make_manager(pool_agent="kirocrew")
        spawned_during = _make_provider()

        with mgr.fence_effort_overlay_rewrite():
            spawned_after_the_rewrite_began = manual_clock.advance(0.01)
            mgr._warm_pool.put_nowait((spawned_during, spawned_after_the_rewrite_began))

        assert await mgr._drain_and_claim("kirocrew") is None
        spawned_during.shutdown.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_provider_spawned_after_an_explicit_default_rewrite_is_claimed(self):
        mgr, _ = _make_manager(pool_agent="kirocrew")
        with mgr.fence_effort_overlay_rewrite():
            pass
        fresh = _make_provider()
        mgr._warm_pool.put_nowait((fresh, mgr._pool.state.effort_overlay_epoch + 0.001))

        assert await mgr._drain_and_claim("kirocrew") is fresh
        fresh.shutdown.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dead_provider_discarded_healthy_used(self):
        """Dead providers are drained; first healthy one is used."""
        mgr, _ = _make_manager(pool_agent="kirocrew")

        dead = _make_provider()
        dead.is_process_alive = MagicMock(return_value=False)
        healthy = _make_provider()
        healthy.is_process_alive = MagicMock(return_value=True)

        mgr._warm_pool.put_nowait((dead, time.monotonic()))
        mgr._warm_pool.put_nowait((healthy, time.monotonic()))

        pooled = await mgr._drain_and_claim("kirocrew")

        dead.shutdown.assert_awaited_once()
        assert pooled is healthy

    @pytest.mark.asyncio
    async def test_unanswerable_liveness_probe_is_discarded_fail_closed(self):
        """A provider whose liveness probe fails is discarded, not recycled.

        The TTL recycle reads the same process check, and an unusable answer
        is treated as dead (WARNING, discard) so a broken provider never
        reaches a session. Under the declared-ABC liveness contract a real
        provider always answers ``is_process_alive`` (the ABC defaults it to
        ``is_alive``), so the only remaining unanswerable case is a probe
        that raises — which this models.
        """
        mgr, _ = _make_manager(pool_agent="kirocrew", pool_ttl_secs=1)

        broken = _make_provider()
        broken.is_process_alive = MagicMock(side_effect=RuntimeError("probe failed"))
        healthy = _make_provider()

        mgr._warm_pool.put_nowait((broken, time.monotonic() - 10))
        mgr._warm_pool.put_nowait((healthy, time.monotonic()))

        pooled = await mgr._drain_and_claim("kirocrew")

        broken.shutdown.assert_awaited_once()
        assert pooled is healthy


# ---------------------------------------------------------------------------
# _claim_from_pool
# ---------------------------------------------------------------------------


class TestClaimFromPool:
    def test_claim_matching_agent(self):
        mgr, _ = _make_manager(pool_agent="kirocrew")
        provider = _make_provider()
        mgr._warm_pool.put_nowait((provider, time.monotonic()))

        result = mgr._claim_from_pool("kirocrew")
        assert result[0] is provider
        assert mgr._warm_pool.qsize() == 0

    def test_claim_none_agent_matches_pool_agent(self):
        """None agent means 'use default' — matches pool_agent."""
        mgr, _ = _make_manager(pool_agent="kirocrew")
        provider = _make_provider()
        mgr._warm_pool.put_nowait((provider, time.monotonic()))

        result = mgr._claim_from_pool(None)
        assert result[0] is provider
        assert mgr._warm_pool.qsize() == 0

    def test_claim_empty_agent_matches_empty_pool_agent(self):
        """Empty agent matches empty pool_agent."""
        mgr, _ = _make_manager(pool_agent="")
        provider = _make_provider()
        mgr._warm_pool.put_nowait((provider, time.monotonic()))

        result = mgr._claim_from_pool(None)
        assert result[0] is provider

    def test_claim_mismatched_agent_returns_none(self):
        mgr, _ = _make_manager(pool_agent="kirocrew")
        mgr._warm_pool.put_nowait((_make_provider(), time.monotonic()))

        result = mgr._claim_from_pool("custom-agent")
        assert result is None
        assert mgr._warm_pool.qsize() == 1  # not consumed

    def test_claim_empty_pool_returns_none(self):
        mgr, _ = _make_manager()
        result = mgr._claim_from_pool("kirocrew")
        assert result is None

    def test_claim_nonempty_agent_rejected_when_pool_agent_empty(self):
        mgr, _ = _make_manager(pool_agent="")
        mgr._warm_pool.put_nowait((_make_provider(), time.monotonic()))
        result = mgr._claim_from_pool("some-agent")
        assert result is None
        assert mgr._warm_pool.qsize() == 1  # not consumed


class TestPoolAgentResolvesLikeASession:
    """A blank / alias pool agent resolves to the kiro agent a session asks for."""

    def _manager(self, tmp_path, monkeypatch, pool_agent: str):
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.session import SessionManager

        monkeypatch.setenv("HOME", str(tmp_path))
        cfg = KiroCrewConfig.load()
        cfg.session.pool_size = 1
        cfg.session.pool_agent = pool_agent
        factory = MagicMock(side_effect=lambda *a, **kw: _make_provider())
        with patch("kiro_crew.session.default_project_dir", return_value=str(tmp_path)):
            mgr = SessionManager(cfg, provider_factory=factory)
        return mgr, factory

    @pytest.mark.parametrize("pool_agent", ["", "default"])
    def test_default_pool_is_claimed_by_the_resolved_default_agent(
        self, tmp_path, monkeypatch, pool_agent
    ):
        mgr, _ = self._manager(tmp_path, monkeypatch, pool_agent)
        provider = _make_provider()
        mgr._warm_pool.put_nowait((provider, time.monotonic()))

        result = mgr._claim_from_pool("kirocrew")

        assert result is not None and result[0] is provider

    def test_a_kiro_agent_name_the_resolver_cannot_see_is_kept(self, tmp_path, monkeypatch):
        """A name that is not a config alias is a kiro agent already, kept as written."""
        mgr, _ = self._manager(tmp_path, monkeypatch, "project-agent")
        provider = _make_provider()
        mgr._warm_pool.put_nowait((provider, time.monotonic()))

        assert mgr._claim_from_pool("kirocrew") is None
        result = mgr._claim_from_pool("project-agent")
        assert result is not None and result[0] is provider

    def test_a_different_agent_still_misses(self, tmp_path, monkeypatch):
        mgr, _ = self._manager(tmp_path, monkeypatch, "")
        mgr._warm_pool.put_nowait((_make_provider(), time.monotonic()))

        assert mgr._claim_from_pool("custom-agent") is None
        assert mgr._warm_pool.qsize() == 1

    @pytest.mark.asyncio
    async def test_default_pool_prewarms_the_resolved_agent(self, tmp_path, monkeypatch):
        """The pooled process runs the same agent the claiming session asked for."""
        mgr, factory = self._manager(tmp_path, monkeypatch, "")

        await mgr._fill_warm_pool()

        assert factory.call_args.kwargs.get("agent") == "kirocrew"


# ---------------------------------------------------------------------------
# _schedule_replenish
# ---------------------------------------------------------------------------


class TestScheduleReplenish:
    @pytest.mark.asyncio
    async def test_replenish_creates_background_task(self):
        mgr, _ = _make_manager(pool_size=1)
        mgr._schedule_replenish()

        assert len(mgr._background_tasks) == 1
        await asyncio.gather(*list(mgr._background_tasks), return_exceptions=True)
        assert mgr._warm_pool.qsize() == 1

    @pytest.mark.asyncio
    async def test_replenish_noop_when_disabled(self):
        mgr, _ = _make_manager(pool_size=0)
        mgr._schedule_replenish()
        assert len(mgr._background_tasks) == 0


# ---------------------------------------------------------------------------
# Pool drain on shutdown (close_all)
# ---------------------------------------------------------------------------


class TestPoolDrainOnShutdown:
    @pytest.mark.asyncio
    async def test_close_all_shuts_down_pool_providers(self):
        mgr, _ = _make_manager(pool_size=2)
        p1, p2 = _make_provider(), _make_provider()
        mgr._warm_pool.put_nowait((p1, time.monotonic()))
        mgr._warm_pool.put_nowait((p2, time.monotonic()))

        await mgr.close_all()

        p1.shutdown.assert_awaited_once()
        p2.shutdown.assert_awaited_once()
        assert mgr._warm_pool.qsize() == 0


# ---------------------------------------------------------------------------
# Config wiring
# ---------------------------------------------------------------------------


class TestConfigWiring:
    def test_pool_size_from_config(self):
        mgr, _ = _make_manager(pool_size=5, pool_agent="custom")
        assert mgr._pool_size == 5
        assert mgr._pool_agent == "custom"

    def test_pool_agent_falls_back_to_default_agent(self):
        from kiro_crew.session import SessionManager

        cfg = _make_cfg(pool_size=1, pool_agent="")
        cfg.agent.default_agent = "fallback-agent"
        mgr = SessionManager(cfg)
        assert mgr._pool_agent == "fallback-agent"

    def test_pool_disabled_by_default(self):
        from kiro_crew.session import SessionManager

        cfg = _make_cfg(pool_size=0)
        mgr = SessionManager(cfg)
        assert mgr._pool_size == 0

    def test_pool_size_capped_at_max(self):
        """pool_size > 10 is clamped to 10."""
        mgr, _ = _make_manager(pool_size=100)
        assert mgr._pool_size == 10


# ---------------------------------------------------------------------------
# get_or_create integration with pool
# ---------------------------------------------------------------------------


class TestGetOrCreatePoolIntegration:
    @pytest.mark.asyncio
    async def test_claims_from_pool_when_agent_matches(self):
        """get_or_create uses pooled provider, verifies rekey() called."""
        from kiro_crew.providers.acp import AcpProvider

        mgr, factory = _make_manager(pool_agent="kirocrew")
        pooled = _make_provider()
        pooled.__class__ = AcpProvider
        pooled.client = MagicMock()
        pooled.client.resumed = False
        pooled.client._session_id = "fake-sid"
        mgr._drain_and_claim = AsyncMock(return_value=pooled)
        mgr._schedule_replenish = MagicMock()

        provider, is_new, _ = await mgr.get_or_create(
            "test-key", agent="kirocrew", channel_id="ch-1"
        )

        assert provider is pooled
        # crew_agent="" — the caller supplied no canonical crew identity, and
        # the claim must still rebind (a recycled runtime never carries a
        # previous crew's watchdog windows). The watchdog snapshot is resolved
        # off-loop by the claim site and handed in as data.
        assert pooled.client.rekey.call_count == 1
        args, kwargs = pooled.client.rekey.call_args
        assert args == ("test-key", "ch-1")
        assert kwargs["crew_agent"] == ""
        assert isinstance(kwargs["watchdog"], WatchdogSettings)
        mgr._schedule_replenish.assert_called_once()
        factory.assert_not_called()

    @staticmethod
    def _warm_acp_provider():
        from kiro_crew.providers.acp import AcpProvider

        pooled = _make_provider()
        pooled.__class__ = AcpProvider
        pooled.client = MagicMock()
        pooled.client.resumed = False
        pooled.client._session_id = "fake-sid"
        return pooled

    async def _claim_waiting_at_model_resolution(self, mgr, monkeypatch):
        entered = threading.Event()
        release = threading.Event()

        def resolve_pool_model(_agent):
            entered.set()
            assert release.wait(timeout=5), "the test did not release model resolution"
            return "custom-model"

        monkeypatch.setattr(mgr, "_resolve_agent_model", resolve_pool_model)
        task = asyncio.create_task(
            mgr.get_or_create("test-key", agent="kirocrew", model="custom-model")
        )
        assert await asyncio.to_thread(
            entered.wait, 5
        ), "the warm claim never reached model resolution"
        return task, release

    @pytest.mark.asyncio
    async def test_completed_effort_rewrite_after_claim_discards_before_registration(
        self, monkeypatch
    ):
        mgr, factory = _make_manager(pool_agent="kirocrew")
        pooled = self._warm_acp_provider()
        mgr._warm_pool.put_nowait((pooled, time.monotonic()))
        mgr._schedule_replenish = MagicMock()
        discard = AsyncMock()
        mgr._discard_pool_provider = discard

        task, release = await self._claim_waiting_at_model_resolution(mgr, monkeypatch)
        with mgr.fence_effort_overlay_rewrite():
            pass
        release.set()
        provider, is_new, _ = await asyncio.wait_for(task, timeout=5)

        assert provider is not pooled and is_new
        discard.assert_awaited_once_with(pooled, "Warm pool effort registration discard")
        factory.assert_called_once()
        mgr.release("test-key")
        await mgr.reset("test-key")

    @pytest.mark.asyncio
    async def test_cancelled_effort_rewrite_discard_releases_reservation(self, monkeypatch):
        mgr, factory = _make_manager(pool_agent="kirocrew")
        pooled = self._warm_acp_provider()
        mgr._warm_pool.put_nowait((pooled, time.monotonic()))
        mgr._schedule_replenish = MagicMock()
        discard_entered = asyncio.Event()

        async def discard(provider, context):
            assert provider is pooled
            assert context == "Warm pool effort registration discard"
            discard_entered.set()
            await asyncio.Event().wait()

        mgr._discard_pool_provider = AsyncMock(side_effect=discard)

        task, release = await self._claim_waiting_at_model_resolution(mgr, monkeypatch)
        with mgr.fence_effort_overlay_rewrite():
            pass
        release.set()
        await asyncio.wait_for(discard_entered.wait(), timeout=5)
        assert mgr.effort_basis_locked("test-key") is True

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await _await_test(task, "task")

        mgr._discard_pool_provider.assert_awaited_once_with(
            pooled, "Warm pool effort registration discard"
        )
        # This is the picker's turn_in_flight gate. A leaked reservation leaves it true.
        assert mgr.effort_basis_locked("test-key") is False
        assert mgr._allocation_boundary()._allocation_reservations == {}

        provider, is_new, _ = await mgr.get_or_create("test-key", agent="kirocrew")
        assert provider is not pooled and is_new
        factory.assert_called_once()
        mgr.release("test-key")
        await mgr.reset("test-key")

    @pytest.mark.asyncio
    async def test_in_progress_effort_rewrite_after_claim_discards_before_registration(
        self, monkeypatch
    ):
        mgr, factory = _make_manager(pool_agent="kirocrew")
        pooled = self._warm_acp_provider()
        mgr._warm_pool.put_nowait((pooled, time.monotonic()))
        mgr._schedule_replenish = MagicMock()
        discard = AsyncMock()
        mgr._discard_pool_provider = discard

        task, release = await self._claim_waiting_at_model_resolution(mgr, monkeypatch)
        with mgr.fence_effort_overlay_rewrite():
            release.set()
            provider, is_new, _ = await asyncio.wait_for(task, timeout=5)

        assert provider is not pooled and is_new
        discard.assert_awaited_once_with(pooled, "Warm pool effort registration discard")
        factory.assert_called_once()
        mgr.release("test-key")
        await mgr.reset("test-key")

    @pytest.mark.asyncio
    async def test_warm_claim_without_effort_rewrite_still_registers(self, monkeypatch):
        mgr, factory = _make_manager(pool_agent="kirocrew")
        pooled = self._warm_acp_provider()
        mgr._warm_pool.put_nowait((pooled, time.monotonic()))
        mgr._schedule_replenish = MagicMock()

        task, release = await self._claim_waiting_at_model_resolution(mgr, monkeypatch)
        release.set()
        provider, is_new, _ = await asyncio.wait_for(task, timeout=5)

        assert provider is pooled and is_new
        pooled.shutdown.assert_not_awaited()
        factory.assert_not_called()
        mgr.release("test-key")
        await mgr.reset("test-key")

    @pytest.mark.asyncio
    async def test_claim_model_resolution_failure_removes_claimed_spawn_time(self, monkeypatch):
        mgr, factory = _make_manager(pool_agent="kirocrew")
        pooled = self._warm_acp_provider()
        mgr._warm_pool.put_nowait((pooled, time.monotonic()))

        with monkeypatch.context() as scoped:
            scoped.setattr(
                mgr,
                "_resolve_agent_model",
                MagicMock(side_effect=RuntimeError("model resolution failed")),
            )
            with pytest.raises(RuntimeError, match="model resolution failed"):
                await mgr.get_or_create("test-key", agent="kirocrew", model="custom-model")

        assert mgr._pool.state.claimed_spawn_times == {}
        factory.assert_not_called()

    @pytest.mark.asyncio
    async def test_claim_forwards_canonical_crew_identity_to_rekey(self):
        """The claiming session's crew_agent kwarg reaches rekey so the pooled
        handle's watchdog windows rebind to the claiming crew — the identity
        travels with the session, not the pool key."""
        from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
        from kiro_crew.providers.acp import AcpProvider

        def declared_legacy_member():
            cfg = KiroCrewConfig.load()
            # This tests a legacy pooled runtime, not private V2 allocation.
            cfg.agents["pr-reviewer"] = KiroCrewAgentConfig(
                kiro_agent="kirocrew", memory_store="default"
            )
            cfg.save()
            return cfg.agents

        members = await asyncio.to_thread(declared_legacy_member)
        mgr, factory = _make_manager(pool_agent="kirocrew")
        mgr._cfg.agents = members
        pooled = _make_provider()
        pooled.__class__ = AcpProvider
        pooled.client = MagicMock()
        pooled.client.resumed = False
        pooled.client._session_id = "fake-sid"
        mgr._drain_and_claim = AsyncMock(return_value=pooled)
        mgr._schedule_replenish = MagicMock()

        provider, _, _ = await mgr.get_or_create(
            "test-key", agent="kirocrew", channel_id="ch-1", crew_agent="pr-reviewer"
        )

        assert provider is pooled
        args, kwargs = pooled.client.rekey.call_args
        assert args == ("test-key", "ch-1")
        assert kwargs["crew_agent"] == "pr-reviewer"
        assert isinstance(kwargs["watchdog"], WatchdogSettings)

    def test_capability_preparation_refuses_an_undeclared_canonical_member(self):
        from kiro_crew.session_capabilities import CapabilityStartupError, prepare_runtime

        with pytest.raises(CapabilityStartupError, match="capability_member_missing"):
            prepare_runtime("kirocrew", "undeclared-crew", None)

    def test_corrupt_sidecar_refuses_declared_members_with_a_closed_code_only(self):
        """Enrollment lives in the one shared sidecar, so an unreadable file
        cannot prove a declared member is unenrolled: every declared member
        refuses with the closed ``capability_state_unreadable`` code, never a
        raw parser error, while a session that resolves to no crew never reads
        the sidecar and still starts."""
        from kiro_crew import agent_state
        from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
        from kiro_crew.session_capabilities import CapabilityStartupError, prepare_runtime

        cfg = KiroCrewConfig.load()
        cfg.agents["legacy-member"] = KiroCrewAgentConfig(
            kiro_agent="kirocrew", memory_store="default"
        )
        cfg.save()
        path = agent_state._state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{", encoding="utf-8")

        with pytest.raises(CapabilityStartupError, match="capability_state_unreadable"):
            prepare_runtime("kirocrew", "legacy-member", None)
        assert prepare_runtime("kirocrew", "", None).member == ""
        assert path.read_text(encoding="utf-8") == "{"

    def test_capability_refusal_names_the_member_it_belongs_to(self):
        """The chat card links to the member's Capabilities pane, so the
        refusal must say whose spec failed; the code stays unchanged."""
        from kiro_crew import session_capabilities
        from kiro_crew.agent_capabilities import CapabilityError
        from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig

        cfg = KiroCrewConfig.load()
        cfg.agents["drifted"] = KiroCrewAgentConfig(kiro_agent="kirocrew", memory_store="default")
        cfg.save()
        with (
            patch.object(
                session_capabilities.agent_state,
                "get_capabilities",
                return_value={"status": "saved"},
            ),
            patch.object(
                session_capabilities,
                "reconcile_member_capabilities",
                side_effect=CapabilityError("materialization_changed"),
            ),
            pytest.raises(CapabilityError) as caught,
        ):
            session_capabilities.prepare_runtime("kirocrew", "drifted", None)
        assert caught.value.code == "materialization_changed"
        assert caught.value.member == "drifted"

    @pytest.mark.asyncio
    async def test_skips_pool_when_resume_sid_set(self):
        """get_or_create skips pool when session has resume_sid."""
        mgr, factory = _make_manager(pool_agent="kirocrew")
        pooled = _make_provider()
        mgr._warm_pool.put_nowait((pooled, time.monotonic()))
        mgr._drain_and_claim = AsyncMock(return_value=pooled)

        # Simulate existing session in map
        mgr._session_map.get = MagicMock(return_value="existing-sid")

        provider, is_new, _ = await mgr.get_or_create("test-key", agent="kirocrew")

        # Pool should be skipped — _drain_and_claim not called
        mgr._drain_and_claim.assert_not_awaited()
        # Factory called for cold start
        factory.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_pool_when_cwd_set(self):
        """get_or_create skips pool when caller provides cwd.

        Pooled providers were spawned in the gateway's cwd and cannot be
        re-rooted; a caller requesting cwd must get a fresh cold-start
        process.  Forwarding cwd to the factory is verified separately.
        """
        mgr, factory = _make_manager(pool_agent="kirocrew")
        pooled = _make_provider()
        mgr._warm_pool.put_nowait((pooled, time.monotonic()))
        mgr._drain_and_claim = AsyncMock(return_value=pooled)

        provider, is_new, _ = await mgr.get_or_create(
            "test-key", agent="kirocrew", cwd="/Users/alice/workspace/proj"
        )

        # Pool skipped
        mgr._drain_and_claim.assert_not_awaited()
        # Factory called for cold start, cwd forwarded
        factory.assert_called_once()
        assert factory.call_args.kwargs.get("cwd") == "/Users/alice/workspace/proj"

    @pytest.mark.asyncio
    async def test_claims_pool_with_model_override_and_switches(self):
        """get_or_create claims pool even with model_override, then calls set_model."""
        from kiro_crew.providers.acp import AcpProvider

        mgr, factory = _make_manager(pool_agent="kirocrew")
        pooled = _make_provider()
        pooled.__class__ = AcpProvider
        pooled.client = MagicMock()
        pooled.client.set_model = AsyncMock()
        pooled.client.resumed = False
        pooled.client._session_id = "fake-sid"
        mgr._drain_and_claim = AsyncMock(return_value=pooled)
        mgr._schedule_replenish = MagicMock()

        with patch.object(type(mgr), "_resolve_agent_model", return_value="default-model"):
            provider, is_new, _ = await mgr.get_or_create(
                "test-key", agent="kirocrew", model="custom-model"
            )

        assert provider is pooled
        mgr._drain_and_claim.assert_awaited_once()
        factory.assert_not_called()
        pooled.client.set_model.assert_awaited_once_with("custom-model")


# ---------------------------------------------------------------------------
# TTL expiration
# ---------------------------------------------------------------------------


class TestTTLExpiration:
    @pytest.mark.asyncio
    async def test_stale_provider_discarded(self):
        """Provider older than TTL is discarded."""
        mgr, _ = _make_manager(pool_agent="kirocrew", pool_ttl_secs=60)
        stale = _make_provider()
        # Simulate provider spawned 120s ago
        mgr._warm_pool.put_nowait((stale, time.monotonic() - 120))

        result = await mgr._drain_and_claim("kirocrew")

        assert result is None
        stale.shutdown.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_fresh_provider_used(self):
        """Provider within TTL is used."""
        mgr, _ = _make_manager(pool_agent="kirocrew", pool_ttl_secs=60)
        fresh = _make_provider()
        mgr._warm_pool.put_nowait((fresh, time.monotonic()))

        result = await mgr._drain_and_claim("kirocrew")

        assert result is fresh
        fresh.shutdown.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ttl_zero_disables_check(self):
        """TTL=0 disables expiration check."""
        mgr, _ = _make_manager(pool_agent="kirocrew", pool_ttl_secs=0)
        old = _make_provider()
        # Very old provider
        mgr._warm_pool.put_nowait((old, time.monotonic() - 10000))

        result = await mgr._drain_and_claim("kirocrew")

        assert result is old
        old.shutdown.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stale_drain_triggers_replenish(self):
        """Discarding stale providers triggers pool replenish."""
        mgr, _ = _make_manager(pool_agent="kirocrew", pool_ttl_secs=60)
        stale = _make_provider()
        mgr._warm_pool.put_nowait((stale, time.monotonic() - 120))
        mgr._schedule_replenish = MagicMock()

        await mgr._drain_and_claim("kirocrew")

        mgr._schedule_replenish.assert_called_once()

    @pytest.mark.asyncio
    async def test_claim_ttl_discard_logs_info_dead_keeps_warning(self, caplog):
        """The claim path follows the same severity rule as the health
        sweep — a TTL recycle of a healthy provider is INFO, one that also died
        before aging out keeps WARNING. Both are still discarded."""
        import logging

        mgr, _ = _make_manager(pool_agent="kirocrew", pool_ttl_secs=60)
        stale = _make_provider()
        stale.is_process_alive = MagicMock(return_value=True)
        stale_dead = _make_provider()
        stale_dead.is_process_alive = MagicMock(return_value=False)
        mgr._warm_pool.put_nowait((stale, time.monotonic() - 120))
        mgr._warm_pool.put_nowait((stale_dead, time.monotonic() - 120))

        with patch("kiro_crew.session._sync_kill_provider"):
            with caplog.at_level(logging.INFO, logger="kiro_crew.session"):
                result = await mgr._drain_and_claim("kirocrew")

        assert result is None
        ttl_records = [
            r for r in caplog.records if str(r.msg).startswith("Warm pool: %.0fs old provider")
        ]
        # FIFO claim order: healthy-stale first (INFO), dead-stale second (WARNING).
        assert [r.levelname for r in ttl_records] == ["INFO", "WARNING"]
        stale.shutdown.assert_awaited_once()
        stale_dead.shutdown.assert_awaited_once()


# ---------------------------------------------------------------------------
# Model-matches-pool-default bypass (effective_model normalization)
# ---------------------------------------------------------------------------


class TestModelMatchesPoolDefault:
    """When the dashboard sends model == pool agent's default, treat as None
    so the pool isn't bypassed unnecessarily."""

    @pytest.mark.asyncio
    async def test_pool_claimed_when_model_matches_agent_default(self):
        """model='claude-opus-4.6' matching pool agent default → pool used, no set_model."""
        from kiro_crew.providers.acp import AcpProvider

        mgr, factory = _make_manager(pool_agent="kirocrew")
        pooled = _make_provider()
        pooled.__class__ = AcpProvider
        pooled.client = MagicMock()
        pooled.client.set_model = AsyncMock()
        pooled.client.resumed = False
        pooled.client._session_id = "fake-sid"
        mgr._drain_and_claim = AsyncMock(return_value=pooled)
        mgr._schedule_replenish = MagicMock()

        with patch.object(type(mgr), "_resolve_agent_model", return_value="claude-opus-4.6"):
            provider, is_new, _ = await mgr.get_or_create(
                "test-key", agent="kirocrew", model="claude-opus-4.6"
            )

        assert provider is pooled
        mgr._drain_and_claim.assert_awaited_once()
        factory.assert_not_called()
        pooled.client.set_model.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pool_claimed_when_model_differs_with_post_switch(self):
        """model='claude-sonnet-4.6' != pool default → pool claimed, set_model called."""
        from kiro_crew.providers.acp import AcpProvider

        mgr, factory = _make_manager(pool_agent="kirocrew")
        pooled = _make_provider()
        pooled.__class__ = AcpProvider
        pooled.client = MagicMock()
        pooled.client.set_model = AsyncMock()
        pooled.client.resumed = False
        pooled.client._session_id = "fake-sid"
        mgr._drain_and_claim = AsyncMock(return_value=pooled)
        mgr._schedule_replenish = MagicMock()

        with patch.object(type(mgr), "_resolve_agent_model", return_value="claude-opus-4.6"):
            provider, is_new, _ = await mgr.get_or_create(
                "test-key", agent="kirocrew", model="claude-sonnet-4.6"
            )

        assert provider is pooled
        mgr._drain_and_claim.assert_awaited_once()
        factory.assert_not_called()
        pooled.client.set_model.assert_awaited_once_with("claude-sonnet-4.6")

    @pytest.mark.asyncio
    async def test_pool_claude_backend_translates_canonical_key_on_switch(self):
        """On the claude backend, a canonical wire key (e.g. opus-4.8-1m) is
        translated to a provider id before set_model — else the adapter
        mis-resolves it. kiro/acp backends still pass the value through."""
        from kiro_crew.providers.acp import AcpProvider

        mgr, factory = _make_manager(pool_agent="kirocrew")
        pooled = _make_provider()
        pooled.__class__ = AcpProvider
        pooled.client = MagicMock()
        pooled.client.backend = "claude"  # marks this an AcpProvider(claude)
        pooled.client.set_model = AsyncMock()
        pooled.client.resumed = False
        pooled.client._session_id = "fake-sid"
        mgr._drain_and_claim = AsyncMock(return_value=pooled)
        mgr._schedule_replenish = MagicMock()

        with patch.object(type(mgr), "_resolve_agent_model", return_value="default-model"):
            await mgr.get_or_create("test-key", agent="kirocrew", model="opus-4.8-1m")

        pooled.client.set_model.assert_awaited_once_with("global.anthropic.claude-opus-4-8[1m]")

    @pytest.mark.asyncio
    async def test_pool_claude_backend_skips_redundant_switch_cross_namespace(self):
        """The short-circuit must work ACROSS namespaces: a canonical wire key
        and the pool agent's kiro model slot that resolve to the SAME provider id
        must NOT trigger a redundant set_model. Requested 'opus-4.8-1m' vs pool
        agent kiro 'claude-opus-4.6' both → the flagship provider id."""
        from kiro_crew.providers.acp import AcpProvider

        mgr, factory = _make_manager(pool_agent="kirocrew")
        pooled = _make_provider()
        pooled.__class__ = AcpProvider
        pooled.client = MagicMock()
        pooled.client.backend = "claude"
        pooled.client.set_model = AsyncMock()
        pooled.client.resumed = False
        pooled.client._session_id = "fake-sid"
        mgr._drain_and_claim = AsyncMock(return_value=pooled)
        mgr._schedule_replenish = MagicMock()

        # pool agent's kiro model 'claude-opus-4.6' translates to the SAME
        # flagship provider id as the requested canonical 'opus-4.8-1m'.
        with patch.object(type(mgr), "_resolve_agent_model", return_value="claude-opus-4.6"):
            await mgr.get_or_create("test-key", agent="kirocrew", model="opus-4.8-1m")

        pooled.client.set_model.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pool_skipped_when_model_set_but_pool_disabled(self):
        """pool_size=0 → no model comparison, straight to cold start."""
        mgr, factory = _make_manager(pool_size=0, pool_agent="kirocrew")
        mgr._drain_and_claim = AsyncMock()

        with patch.object(type(mgr), "_resolve_agent_model", return_value="claude-opus-4.6"):
            await mgr.get_or_create("test-key", agent="kirocrew", model="claude-opus-4.6")

        mgr._drain_and_claim.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_model_match_skipped_when_resume_sid_exists(self):
        """resume_sid takes priority — pool skipped even if model matches."""
        mgr, factory = _make_manager(pool_agent="kirocrew")
        mgr._drain_and_claim = AsyncMock()
        mgr._session_map.get = MagicMock(return_value="existing-sid")

        with patch.object(type(mgr), "_resolve_agent_model", return_value="claude-opus-4.6"):
            await mgr.get_or_create("test-key", agent="kirocrew", model="claude-opus-4.6")

        mgr._drain_and_claim.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_none_model_still_claims_from_pool(self):
        """model=None (no explicit model) → pool used as before."""
        from kiro_crew.providers.acp import AcpProvider

        mgr, factory = _make_manager(pool_agent="kirocrew")
        pooled = _make_provider()
        pooled.__class__ = AcpProvider
        pooled.client = MagicMock()
        pooled.client.resumed = False
        pooled.client._session_id = "fake-sid"
        mgr._drain_and_claim = AsyncMock(return_value=pooled)
        mgr._schedule_replenish = MagicMock()

        provider, is_new, _ = await mgr.get_or_create("test-key", agent="kirocrew", model=None)

        assert provider is pooled
        mgr._drain_and_claim.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_empty_pool_agent_skips_model_resolution_on_claim(self):
        """No pool_agent configured → no model resolution on post-claim check."""
        from kiro_crew.providers.acp import AcpProvider

        mgr, factory = _make_manager(pool_agent="")
        pooled = _make_provider()
        pooled.__class__ = AcpProvider
        pooled.client = MagicMock()
        pooled.client.set_model = AsyncMock()
        pooled.client.resumed = False
        pooled.client._session_id = "fake-sid"
        mgr._drain_and_claim = AsyncMock(return_value=pooled)
        mgr._schedule_replenish = MagicMock()

        with patch.object(type(mgr), "_resolve_agent_model") as mock_resolve:
            await mgr.get_or_create("test-key", agent=None, model="claude-opus-4.6")

        mock_resolve.assert_not_called()
        # model provided but no pool_agent → pool_model is None → skip set_model
        # (pool process already has whatever model kiro-cli defaults to)
        pooled.client.set_model.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unusable_model_is_withheld_not_raised_on_claim(self):
        """A stale slot model must behave the SAME warm as cold.

        The post-claim re-apply carries an INHERITED
        value, so letting AcpModelUnavailable escape here would kill the claimed
        provider — while an identical cold start quietly withholds. That makes
        the outcome depend on whether a pooled process happened to exist.
        """
        from kiro_crew.providers.acp import AcpProvider

        mgr, factory = _make_manager(pool_agent="kirocrew")
        pooled = _make_provider()
        pooled.__class__ = AcpProvider
        pooled.client = MagicMock()
        pooled.client.set_model = AsyncMock()
        pooled.client.resumed = False
        pooled.client._session_id = "fake-sid"
        # The account can only run sonnet; the slot still asks for opus.
        pooled.available_models = MagicMock(return_value=[{"modelId": "claude-sonnet-4.6"}])
        mgr._drain_and_claim = AsyncMock(return_value=pooled)
        mgr._schedule_replenish = MagicMock()

        with patch.object(type(mgr), "_resolve_agent_model", return_value="claude-sonnet-4.6"):
            provider, _is_new, _resumed = await mgr.get_or_create(
                "test-key", agent="kirocrew", model="claude-opus-4.8"
            )

        # Withheld, not sent — and the claim survives.
        pooled.client.set_model.assert_not_awaited()
        assert provider is pooled

    @pytest.mark.asyncio
    async def test_namespaced_pin_resolves_on_claim_like_a_cold_start(self):
        """A warm claim must run exactly what a cold start of the pin runs.

        The pin carries a stale `<namespace>::` qualifier while the pooled
        session advertises the bare id. The cold-start spawn resolves it via
        resolve_pin_spelling and sends the advertised spelling; withholding it
        here instead would make whether the pinned model runs depend on whether
        a pooled process happened to exist — the exact failure class the
        withhold test above guards from the other direction.
        """
        from kiro_crew.providers.acp import AcpProvider

        mgr, factory = _make_manager(pool_agent="kirocrew")
        pooled = _make_provider()
        pooled.__class__ = AcpProvider
        pooled.client = MagicMock()
        pooled.client.set_model = AsyncMock()
        pooled.client.resumed = False
        pooled.client._session_id = "fake-sid"
        pooled.available_models = MagicMock(return_value=[{"modelId": "z-ai/glm-5.3-flash"}])
        mgr._drain_and_claim = AsyncMock(return_value=pooled)
        mgr._schedule_replenish = MagicMock()

        with patch.object(type(mgr), "_resolve_agent_model", return_value="claude-sonnet-4.6"):
            provider, _is_new, _resumed = await mgr.get_or_create(
                "test-key", agent="kirocrew", model="openrouter::z-ai/glm-5.3-flash"
            )

        # Resolved to the ADVERTISED spelling and sent — not withheld, and not
        # sent under the qualified spelling the backend never advertised.
        pooled.client.set_model.assert_awaited_once_with("z-ai/glm-5.3-flash")
        assert provider is pooled


# ---------------------------------------------------------------------------
# Stateless sessions must not claim from pool
# ---------------------------------------------------------------------------


class TestStatelessSkipsPool:
    @pytest.mark.asyncio
    async def test_bg_session_skips_pool(self):
        """get_or_create for _bg must not claim from warm pool."""
        mgr, factory = _make_manager(pool_agent="kirocrew")
        pooled = _make_provider()
        mgr._warm_pool.put_nowait((pooled, time.monotonic()))
        mgr._drain_and_claim = AsyncMock(return_value=pooled)

        provider, is_new, _ = await mgr.get_or_create("_bg", agent=None)

        mgr._drain_and_claim.assert_not_awaited()
        factory.assert_called_once()

    @pytest.mark.asyncio
    async def test_stateless_prefix_skips_pool(self):
        """Stateless-prefixed keys (cron:, subagent:, etc.) skip pool."""
        for prefix in ("cron:job1", "subagent:abc", "taskrunner:step1"):
            mgr, factory = _make_manager(pool_agent="kirocrew")
            mgr._drain_and_claim = AsyncMock(return_value=_make_provider())

            await mgr.get_or_create(prefix, agent=None)

            mgr._drain_and_claim.assert_not_awaited()


# ---------------------------------------------------------------------------
# pool_size=0 must not attempt pool claim
# ---------------------------------------------------------------------------


class TestPoolDisabledSkipsClaim:
    @pytest.mark.asyncio
    async def test_pool_size_zero_skips_drain_and_claim(self):
        """pool_size=0 with no resume/model/stateless must still skip pool."""
        mgr, factory = _make_manager(pool_size=0, pool_agent="kirocrew")
        mgr._drain_and_claim = AsyncMock()

        await mgr.get_or_create("test-key", agent="kirocrew")

        mgr._drain_and_claim.assert_not_awaited()
        factory.assert_called_once()


# ---------------------------------------------------------------------------
# _pool_health_loop
# ---------------------------------------------------------------------------


class TestPoolHealthLoop:
    @pytest.mark.asyncio
    async def test_removes_dead_provider_and_replenishes(self):
        """Dead provider is removed during health sweep, replenish triggered."""
        mgr, _ = _make_manager(pool_agent="kirocrew")
        dead = _make_provider()
        dead.is_process_alive.return_value = False
        dead.exit_code = 1
        mgr._warm_pool.put_nowait((dead, time.monotonic()))
        mgr._schedule_replenish = MagicMock()

        # Run one iteration by patching sleep to raise after first call
        call_count = 0

        async def _sleep_once(secs):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise asyncio.CancelledError

        with patch("asyncio.sleep", side_effect=_sleep_once):
            with pytest.raises(asyncio.CancelledError):
                await mgr._pool_health_loop()

        assert mgr._warm_pool.empty()
        dead.shutdown.assert_awaited_once()
        mgr._schedule_replenish.assert_called_once()

    @pytest.mark.asyncio
    async def test_removes_expired_provider(self):
        """TTL-expired provider is removed during health sweep."""
        mgr, _ = _make_manager(pool_agent="kirocrew", pool_ttl_secs=60)
        stale = _make_provider()
        mgr._warm_pool.put_nowait((stale, time.monotonic() - 120))
        mgr._schedule_replenish = MagicMock()

        call_count = 0

        async def _sleep_once(secs):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise asyncio.CancelledError

        with patch("asyncio.sleep", side_effect=_sleep_once):
            with pytest.raises(asyncio.CancelledError):
                await mgr._pool_health_loop()

        assert mgr._warm_pool.empty()
        stale.shutdown.assert_awaited_once()
        mgr._schedule_replenish.assert_called_once()

    @pytest.mark.asyncio
    async def test_keeps_healthy_provider(self):
        """Healthy provider at target survives health sweep with no churn."""
        mgr, _ = _make_manager(pool_size=1, pool_agent="kirocrew")
        healthy = _make_provider()
        mgr._warm_pool.put_nowait((healthy, time.monotonic()))
        mgr._schedule_replenish = MagicMock()

        call_count = 0

        async def _sleep_once(secs):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise asyncio.CancelledError

        with patch("asyncio.sleep", side_effect=_sleep_once):
            with pytest.raises(asyncio.CancelledError):
                await mgr._pool_health_loop()

        assert mgr._warm_pool.qsize() == 1
        healthy.shutdown.assert_not_awaited()
        mgr._schedule_replenish.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_pool_schedules_replenish(self):
        """Empty pool self-heals: sweep schedules a refill instead of skipping."""
        mgr, _ = _make_manager(pool_agent="kirocrew")
        mgr._schedule_replenish = MagicMock()

        call_count = 0

        async def _sleep_once(secs):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise asyncio.CancelledError

        with patch("asyncio.sleep", side_effect=_sleep_once):
            with pytest.raises(asyncio.CancelledError):
                await mgr._pool_health_loop()

        mgr._schedule_replenish.assert_called_once()

    @pytest.mark.asyncio
    async def test_under_target_pool_schedules_replenish(self):
        """A short-but-healthy pool is a deficit: sweep schedules a refill."""
        mgr, _ = _make_manager(pool_size=3, pool_agent="kirocrew")
        healthy = _make_provider()
        mgr._warm_pool.put_nowait((healthy, time.monotonic()))
        mgr._schedule_replenish = MagicMock()

        await mgr._sweep_warm_pool_once()

        assert mgr._warm_pool.qsize() == 1
        healthy.shutdown.assert_not_awaited()
        mgr._schedule_replenish.assert_called_once()

    @pytest.mark.asyncio
    async def test_at_target_pool_does_not_replenish(self):
        """A full healthy pool has no deficit: sweep schedules nothing."""
        mgr, _ = _make_manager(pool_size=2, pool_agent="kirocrew")
        for _ in range(2):
            mgr._warm_pool.put_nowait((_make_provider(), time.monotonic()))
        mgr._schedule_replenish = MagicMock()

        await mgr._sweep_warm_pool_once()

        assert mgr._warm_pool.qsize() == 2
        mgr._schedule_replenish.assert_not_called()

    @pytest.mark.asyncio
    async def test_disabled_pool_sweep_is_noop(self):
        """pool_size=0 keeps the sweep a no-op: no refill for a disabled pool."""
        mgr, _ = _make_manager(pool_size=0)
        mgr._schedule_replenish = MagicMock()

        await mgr._sweep_warm_pool_once()

        mgr._schedule_replenish.assert_not_called()

    @pytest.mark.asyncio
    async def test_mixed_healthy_and_dead(self):
        """Only dead providers removed; healthy ones re-enqueued in order."""
        mgr, _ = _make_manager(pool_agent="kirocrew")
        healthy1 = _make_provider()
        dead = _make_provider()
        dead.is_process_alive.return_value = False
        healthy2 = _make_provider()
        mgr._warm_pool.put_nowait((healthy1, time.monotonic()))
        mgr._warm_pool.put_nowait((dead, time.monotonic()))
        mgr._warm_pool.put_nowait((healthy2, time.monotonic()))
        mgr._schedule_replenish = MagicMock()

        call_count = 0

        async def _sleep_once(secs):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise asyncio.CancelledError

        with patch("asyncio.sleep", side_effect=_sleep_once):
            with pytest.raises(asyncio.CancelledError):
                await mgr._pool_health_loop()

        assert mgr._warm_pool.qsize() == 2
        dead.shutdown.assert_awaited_once()
        healthy1.shutdown.assert_not_awaited()
        healthy2.shutdown.assert_not_awaited()
        mgr._schedule_replenish.assert_called_once()


# ---------------------------------------------------------------------------
# _pool_pids
# ---------------------------------------------------------------------------


class TestPoolPids:
    def test_returns_pids_from_pool(self):
        """Extracts PIDs from pooled providers."""
        mgr, _ = _make_manager(pool_agent="kirocrew")
        p1 = _make_provider()
        p1.client = MagicMock()
        p1.client._pid = 1234
        p2 = _make_provider()
        p2.client = MagicMock()
        p2.client._pid = 5678
        mgr._warm_pool.put_nowait((p1, time.monotonic()))
        mgr._warm_pool.put_nowait((p2, time.monotonic()))

        pids = mgr._pool_pids()

        assert pids == {1234, 5678}
        # Non-destructive: queue still has both entries
        assert mgr._warm_pool.qsize() == 2

    def test_empty_pool_returns_empty_set(self):
        mgr, _ = _make_manager(pool_agent="kirocrew")

        assert mgr._pool_pids() == set()

    def test_skips_provider_without_client(self):
        """Provider with no client attr is skipped, not crashed."""
        mgr, _ = _make_manager(pool_agent="kirocrew")
        p = _make_provider()
        del p.client  # no client attribute
        mgr._warm_pool.put_nowait((p, time.monotonic()))

        pids = mgr._pool_pids()

        assert pids == set()
        assert mgr._warm_pool.qsize() == 1

    def test_skips_non_int_pid(self):
        """Provider with non-int PID is skipped."""
        mgr, _ = _make_manager(pool_agent="kirocrew")
        p = _make_provider()
        p.client = MagicMock()
        p.client._pid = None
        mgr._warm_pool.put_nowait((p, time.monotonic()))

        pids = mgr._pool_pids()

        assert pids == set()
        assert mgr._warm_pool.qsize() == 1

    def test_includes_sweep_pids_during_health_check(self):
        """PIDs temporarily out of queue during health sweep are still visible."""
        mgr, _ = _make_manager(pool_agent="kirocrew")
        # Simulate health loop having drained providers
        mgr._pool_sweep_pids = {1111, 2222}

        pids = mgr._pool_pids()

        assert {1111, 2222} <= pids


# ---------------------------------------------------------------------------
# reload_provider_factory resets pool
# ---------------------------------------------------------------------------


class TestReloadProviderFactoryRefillsPool:
    @pytest.mark.asyncio
    async def test_reload_resets_pool_started_and_refills(self):
        """After reload_provider_factory, warm pool is replenished with new provider type."""
        mgr, factory = _make_manager(pool_size=1)
        # Simulate initial start_pool having run
        mgr._pool_started = True
        old_provider = _make_provider()
        mgr._warm_pool.put_nowait((old_provider, time.monotonic()))

        with patch("kiro_crew.session.KiroCrewConfig.load") as mock_load:
            new_cfg = _make_cfg(pool_size=1)
            new_factory = MagicMock(side_effect=lambda *a, **kw: _make_provider())
            new_cfg.create_provider_factory = MagicMock(return_value=new_factory)
            new_cfg.agent.provider = "claude_code"
            mock_load.return_value = new_cfg

            await mgr.reload_provider_factory()

        # Old provider was shut down
        old_provider.shutdown.assert_awaited_once()
        # Pool started was reset and start_pool ran (non-blocking task created)
        assert mgr._pool_started is True  # re-set by start_pool
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_reload_cancels_old_health_task(self):
        """Health loop task is cancelled on reload so a fresh one starts."""
        mgr, _ = _make_manager(pool_size=1)
        mgr._pool_started = True
        fake_task = MagicMock()
        fake_task.done.return_value = False
        fake_task.cancel = MagicMock()
        mgr._pool_health_task = fake_task

        with patch("kiro_crew.session.KiroCrewConfig.load") as mock_load:
            new_cfg = _make_cfg(pool_size=1)
            new_cfg.create_provider_factory = MagicMock(
                return_value=MagicMock(side_effect=lambda *a, **kw: _make_provider())
            )
            new_cfg.agent.provider = "acp"
            mock_load.return_value = new_cfg

            await mgr.reload_provider_factory()

        fake_task.cancel.assert_called_once()
        await mgr.close_all()


# ---------------------------------------------------------------------------
# refresh_defaults adopts new defaults WITHOUT tearing down live sessions
# ---------------------------------------------------------------------------


class TestRefreshDefaultsSparesLiveSessions:
    """``agent.model`` / ``agent.reasoning_effort`` are defaults: they apply to
    the NEXT session. Adopting them must not shut down providers that are
    mid-turn, which is what reload_provider_factory() does."""

    @pytest.mark.asyncio
    async def test_live_sessions_are_not_cleared_or_shut_down(self):
        mgr, _ = _make_manager(pool_size=0)
        live_provider = _make_provider()
        mgr._sessions["dashboard:1"] = SimpleNamespace(provider=live_provider)

        with patch("kiro_crew.session.KiroCrewConfig.load") as mock_load:
            new_cfg = _make_cfg(pool_size=0)
            new_cfg.create_provider_factory = MagicMock(
                return_value=MagicMock(side_effect=lambda *a, **kw: _make_provider())
            )
            new_cfg.agent.model = "claude-opus-4.8"
            new_cfg.agent.reasoning_effort = "xhigh"
            mock_load.return_value = new_cfg

            await mgr.refresh_defaults()

        assert "dashboard:1" in mgr._sessions, "live session was evicted"
        live_provider.shutdown.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_adopts_the_new_config_and_factory(self):
        # Identity-level only: proves the refresh swapped in the reloaded cfg and
        # a freshly built factory (the two things a stale default would come
        # from). The resulting model/effort values are asserted against the real
        # factory in test_effort.py::TestFactoryDefaultEffortFallback.
        mgr, old_factory = _make_manager(pool_size=0)

        with patch("kiro_crew.session.KiroCrewConfig.load") as mock_load:
            new_cfg = _make_cfg(pool_size=0)
            new_factory = MagicMock(side_effect=lambda *a, **kw: _make_provider())
            new_cfg.create_provider_factory = MagicMock(return_value=new_factory)
            new_cfg.agent.model = "claude-sonnet-4.5"
            new_cfg.agent.reasoning_effort = "high"
            mock_load.return_value = new_cfg

            await mgr.refresh_defaults()

        assert mgr._cfg is new_cfg
        assert mgr._provider_factory is not old_factory

    @pytest.mark.asyncio
    async def test_warm_pool_is_drained(self):
        # A pooled provider was built by the OLD factory and would hand the
        # stale default to the very next session; unlike a live session it has
        # no conversation to lose, so draining it is safe and necessary.
        # NOTE: this asserts the drain only. That a NEW session then actually
        # receives the refreshed default is covered end-to-end by
        # test_effort.py::TestFactoryDefaultEffortFallback, and the pool being
        # re-armed after the drain by test_pool_is_restarted_after_the_drain —
        # an empty pool alone would satisfy this test even while broken.
        mgr, _ = _make_manager(pool_size=1)
        mgr._pool_started = True
        stale_pooled = _make_provider()
        mgr._warm_pool.put_nowait((stale_pooled, time.monotonic()))

        with patch("kiro_crew.session.KiroCrewConfig.load") as mock_load:
            new_cfg = _make_cfg(pool_size=1)
            new_cfg.create_provider_factory = MagicMock(
                return_value=MagicMock(side_effect=lambda *a, **kw: _make_provider())
            )
            new_cfg.agent.model = "claude-opus-4.8"
            new_cfg.agent.reasoning_effort = ""
            mock_load.return_value = new_cfg

            await mgr.refresh_defaults()

        stale_pooled.shutdown.assert_awaited()
        assert mgr._warm_pool.empty()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_pool_is_restarted_after_the_drain(self):
        # The health sweep returns early on an empty pool, so a drain that does
        # not re-arm start_pool would leave a configured warm pool permanently
        # empty until the next gateway restart.
        mgr, _ = _make_manager(pool_size=1)
        mgr._pool_started = True
        stale_task = MagicMock()
        stale_task.done.return_value = False
        stale_task.cancel = MagicMock()
        mgr._pool_health_task = stale_task
        mgr._warm_pool.put_nowait((_make_provider(), time.monotonic()))

        with patch("kiro_crew.session.KiroCrewConfig.load") as mock_load:
            new_cfg = _make_cfg(pool_size=1)
            new_cfg.create_provider_factory = MagicMock(
                return_value=MagicMock(side_effect=lambda *a, **kw: _make_provider())
            )
            new_cfg.agent.model = "claude-opus-4.8"
            new_cfg.agent.reasoning_effort = "high"
            mock_load.return_value = new_cfg

            await mgr.refresh_defaults()

        stale_task.cancel.assert_called_once()
        assert mgr._pool_started is True, "start_pool never re-armed after the drain"
        await mgr.close_all()


# ---------------------------------------------------------------------------
# default_project_dir
# ---------------------------------------------------------------------------


class TestDefaultProjectDir:
    def test_returns_realpath_of_workspace_dir(self, tmp_path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        with patch("kiro_crew.config.loader.workspace_dir_for", return_value=ws):
            from kiro_crew.config.loader import default_project_dir

            result = default_project_dir("default")
        assert result == str(ws.resolve())

    def test_returns_empty_when_dir_missing(self, tmp_path):
        missing = tmp_path / "nonexistent"
        with patch("kiro_crew.config.loader.workspace_dir_for", return_value=missing):
            from kiro_crew.config.loader import default_project_dir

            result = default_project_dir("default")
        assert result == ""

    def test_returns_empty_when_sensitive(self, tmp_path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        with (
            patch("kiro_crew.config.loader.workspace_dir_for", return_value=ws),
            patch("kiro_crew.security.is_sensitive_path", return_value=True),
        ):
            from kiro_crew.config.loader import default_project_dir

            result = default_project_dir("default")
        assert result == ""

    def test_returns_empty_on_exception(self):
        with patch("kiro_crew.config.loader.workspace_dir_for", side_effect=RuntimeError("boom")):
            from kiro_crew.config.loader import default_project_dir

            result = default_project_dir("default")
        assert result == ""


# ---------------------------------------------------------------------------
# _pool_cwd initialization and bypass logic
# ---------------------------------------------------------------------------


class TestPoolCwd:
    def test_pool_cwd_set_from_default_project_dir(self):
        from kiro_crew.session import SessionManager

        cfg = _make_cfg()
        with patch("kiro_crew.session.default_project_dir", return_value="/custom/workspace"):
            mgr = SessionManager(cfg)
        assert mgr._pool_cwd == "/custom/workspace"

    def test_pool_cwd_empty_when_no_workspace(self):
        from kiro_crew.session import SessionManager

        cfg = _make_cfg()
        with patch("kiro_crew.session.default_project_dir", return_value=""):
            mgr = SessionManager(cfg)
        assert mgr._pool_cwd == ""

    @pytest.mark.asyncio
    async def test_pool_claimed_when_cwd_matches_pool_cwd(self):
        """cwd == _pool_cwd should NOT bypass pool."""
        from kiro_crew.providers.acp import AcpProvider

        mgr, factory = _make_manager(pool_agent="kirocrew")
        pooled = _make_provider()
        pooled.__class__ = AcpProvider
        pooled.client = MagicMock()
        pooled.client.resumed = False
        pooled.client._session_id = "sid"
        pooled.client._profile = MagicMock()
        pooled.client._profile.name = "acp"
        mgr._drain_and_claim = AsyncMock(return_value=pooled)
        mgr._schedule_replenish = MagicMock()

        provider, is_new, _ = await mgr.get_or_create(
            "test-key",
            agent="kirocrew",
            cwd="/home/user/.kirocrew/workspace",  # same as _pool_cwd
        )

        assert provider is pooled
        mgr._drain_and_claim.assert_awaited_once()
        factory.assert_not_called()

    @pytest.mark.asyncio
    async def test_pool_bypassed_when_pool_cwd_empty_and_cwd_set(self):
        """If _pool_cwd is empty, any cwd bypasses pool."""
        from kiro_crew.session import SessionManager

        cfg = _make_cfg()
        factory = MagicMock(side_effect=lambda *a, **kw: _make_provider())
        with patch("kiro_crew.session.default_project_dir", return_value=""):
            mgr = SessionManager(cfg, provider_factory=factory)

        pooled = _make_provider()
        mgr._warm_pool.put_nowait((pooled, time.monotonic()))
        mgr._drain_and_claim = AsyncMock(return_value=pooled)

        provider, is_new, _ = await mgr.get_or_create(
            "test-key",
            agent="kirocrew",
            cwd="/some/project",
        )

        mgr._drain_and_claim.assert_not_awaited()
        factory.assert_called_once()

    @pytest.mark.asyncio
    async def test_fill_warm_pool_passes_pool_cwd(self):
        """Pool processes are spawned with _pool_cwd."""
        mgr, factory = _make_manager(pool_size=1)
        await mgr._fill_warm_pool()

        factory.assert_called_once()
        assert factory.call_args.kwargs.get("cwd") == "/home/user/.kirocrew/workspace"

    @pytest.mark.asyncio
    async def test_fill_warm_pool_passes_none_when_pool_cwd_empty(self):
        """Pool processes get cwd=None when _pool_cwd is empty."""
        from kiro_crew.session import SessionManager

        cfg = _make_cfg(pool_size=1)
        factory = MagicMock(side_effect=lambda *a, **kw: _make_provider())
        with patch("kiro_crew.session.default_project_dir", return_value=""):
            mgr = SessionManager(cfg, provider_factory=factory)

        await mgr._fill_warm_pool()

        factory.assert_called_once()
        assert factory.call_args.kwargs.get("cwd") is None


# ---------------------------------------------------------------------------
# Discard reaping — a discarded provider's OS process must actually die
# ---------------------------------------------------------------------------


class TestDiscardReaping:
    """A discard removes the provider from all pool bookkeeping, so the
    discard path is the last chance to signal the process. These tests pin
    the escalation contract: bounded graceful shutdown, hard-kill fallback
    on failure, and post-shutdown liveness verification.
    """

    @staticmethod
    def _expired_entry(mgr, provider):
        mgr._warm_pool.put_nowait((provider, time.monotonic() - 10_000))

    @pytest.mark.asyncio
    async def test_survivor_after_noop_shutdown_is_hard_killed(self):
        """Graceful shutdown returning cleanly is not proof the process died —
        a still-alive process must be hard-killed."""
        mgr, _ = _make_manager(pool_agent="kirocrew", pool_ttl_secs=1)
        survivor = _make_provider()
        survivor.is_process_alive = MagicMock(return_value=True)
        self._expired_entry(mgr, survivor)

        with patch("kiro_crew.session._sync_kill_provider") as mock_kill:
            pooled = await mgr._drain_and_claim("kirocrew")

        assert pooled is None
        survivor.shutdown.assert_awaited_once()
        mock_kill.assert_called_once_with(survivor)

    @pytest.mark.asyncio
    async def test_exited_process_is_not_hard_killed(self):
        """No hard kill when the process actually exited — its PID may already
        be recycled by an unrelated process."""
        mgr, _ = _make_manager(pool_agent="kirocrew", pool_ttl_secs=1)
        clean = _make_provider()
        clean.is_process_alive = MagicMock(return_value=False)
        self._expired_entry(mgr, clean)

        with patch("kiro_crew.session._sync_kill_provider") as mock_kill:
            await mgr._drain_and_claim("kirocrew")

        clean.shutdown.assert_awaited_once()
        mock_kill.assert_not_called()

    @pytest.mark.asyncio
    async def test_shutdown_failure_falls_back_to_hard_kill(self):
        """A raising shutdown must not be swallowed into a leak."""
        mgr, _ = _make_manager(pool_agent="kirocrew", pool_ttl_secs=1)
        broken = _make_provider()
        broken.is_process_alive = MagicMock(return_value=True)
        broken.shutdown = AsyncMock(side_effect=RuntimeError("protocol close failed"))
        self._expired_entry(mgr, broken)

        with patch("kiro_crew.session._sync_kill_provider") as mock_kill:
            pooled = await mgr._drain_and_claim("kirocrew")

        assert pooled is None
        mock_kill.assert_called_once_with(broken)

    @pytest.mark.asyncio
    async def test_wedged_shutdown_is_bounded_and_hard_killed(self, monkeypatch):
        """A shutdown that never returns must not stall the discard path
        (the health sweep is a single task — a wedge would disable TTL
        enforcement for the whole pool)."""
        from kiro_crew.session import SessionManager

        monkeypatch.setattr(SessionManager, "_POOL_DISCARD_TIMEOUT", 0.05)
        mgr, _ = _make_manager(pool_agent="kirocrew", pool_ttl_secs=1)
        wedged = _make_provider()
        wedged.is_process_alive = MagicMock(return_value=True)

        async def _never_returns() -> None:
            await asyncio.sleep(3600)

        wedged.shutdown = _never_returns
        self._expired_entry(mgr, wedged)

        # Give the hard-kill offload a PRIVATE executor. Production dispatches it
        # to `subprocess_executor()`, a process-wide 8-worker singleton shared by
        # every test in this xdist worker, and a started run_in_executor future
        # cannot be cancelled — so a sibling test holding those threads (a wedged
        # PTY close, a real `taskkill` on Windows) makes this offload queue behind
        # them. That queue wait is unbounded and is NOT covered by
        # `_POOL_DISCARD_TIMEOUT`, so it could consume the 5s budget below and
        # fail with a TimeoutError naming the wedged shutdown — the one thing this
        # test had already bounded, to 0.05s. A dedicated executor keeps the
        # assertion about escalation ordering instead of about the shared pool's
        # spare capacity.
        with (
            ThreadPoolExecutor(max_workers=1) as private_executor,
            patch("kiro_crew.session._sync_kill_provider") as mock_kill,
            patch("kiro_crew.session.subprocess_executor", return_value=private_executor),
        ):
            pooled = await asyncio.wait_for(mgr._drain_and_claim("kirocrew"), timeout=5)

        assert pooled is None
        mock_kill.assert_called_once_with(wedged)

    @pytest.mark.asyncio
    async def test_health_sweep_hard_kills_expired_survivor(self):
        """The periodic sweep applies the same escalation as the claim path."""
        mgr, _ = _make_manager(pool_agent="kirocrew", pool_ttl_secs=1)
        survivor = _make_provider()
        survivor.is_process_alive = MagicMock(return_value=True)
        self._expired_entry(mgr, survivor)

        with patch("kiro_crew.session._sync_kill_provider") as mock_kill:
            await mgr._sweep_warm_pool_once()

        survivor.shutdown.assert_awaited_once()
        mock_kill.assert_called_once_with(survivor)
        assert mgr._warm_pool.qsize() == 0

    @pytest.mark.asyncio
    async def test_sweep_ttl_discard_logs_info_dead_provider_stays_warning(self, caplog):
        """A scheduled TTL recycle of a healthy provider is the pool
        working as designed, so its discard line is INFO. Both anomalies keep
        WARNING: a provider that died before aging out (TTL line, dead process)
        and the dead-provider branch below. All three are still reaped."""
        import logging

        mgr, _ = _make_manager(pool_agent="kirocrew", pool_ttl_secs=60)
        stale = _make_provider()
        stale.is_process_alive = MagicMock(return_value=True)
        stale_dead = _make_provider()
        stale_dead.is_process_alive = MagicMock(return_value=False)
        dead = _make_provider()
        dead.is_process_alive = MagicMock(return_value=False)
        dead.exit_code = 9
        mgr._warm_pool.put_nowait((stale, time.monotonic() - 120))
        mgr._warm_pool.put_nowait((stale_dead, time.monotonic() - 120))
        mgr._warm_pool.put_nowait((dead, time.monotonic()))

        with patch("kiro_crew.session._sync_kill_provider"):
            with caplog.at_level(logging.INFO, logger="kiro_crew.session"):
                await mgr._sweep_warm_pool_once()

        ttl_records = [
            r for r in caplog.records if str(r.msg).startswith("Pool health: %.0fs old provider")
        ]
        dead_records = [
            r for r in caplog.records if str(r.msg).startswith("Pool health: dead provider")
        ]
        # FIFO drain order: healthy-stale first (INFO), dead-stale second (WARNING).
        assert [r.levelname for r in ttl_records] == ["INFO", "WARNING"]
        assert [r.levelname for r in dead_records] == ["WARNING"]
        # Severity-only: all three entries were still discarded and shut down.
        stale.shutdown.assert_awaited_once()
        stale_dead.shutdown.assert_awaited_once()
        dead.shutdown.assert_awaited_once()
        assert mgr._warm_pool.qsize() == 0

    @pytest.mark.asyncio
    async def test_hard_kill_never_signals_mock_or_sentinel_pids(self):
        """A provider stand-in whose pid resolves to a non-int (Mock coerces
        to 1 via __index__) or to pid<=1 must never be signaled — an unguarded
        kill would SIGTERM init / the CI container entrypoint."""
        from kiro_crew.session_pid import _sync_kill_provider

        mock_provider = _make_provider()  # _client._pid auto-resolves to a Mock
        pid_one = _make_provider()
        pid_one._client = SimpleNamespace(_pid=1)

        with patch("kiro_crew.session_pid.platform_compat.kill_pid") as mock_kill:
            _sync_kill_provider(mock_provider)
            _sync_kill_provider(pid_one)

        mock_kill.assert_not_called()

    @pytest.mark.skipif(os.name != "posix", reason="POSIX signal semantics")
    @pytest.mark.asyncio
    async def test_survivor_reaped_even_when_provider_bookkeeping_says_dead(self):
        """The ACP provider's is_process_alive() self-reports dead once its
        kill path has run — even when signal delivery silently failed and the
        OS process survived. Verification must probe the OS via the tracked
        PID, not trust provider bookkeeping."""
        proc = subprocess.Popen(["sleep", "300"])
        try:
            provider = _make_provider()
            # Model a real ACP provider: the tracked PID lives at
            # provider._client._pid AND the client records the pid's start
            # identity, exactly as AcpClient does after start
            # (client.py: self._start_time = get_process_start_id(self._pid)).
            # _sync_kill_provider verifies that recorded id against the live one
            # before signalling the root; a stand-in without it is refused as
            # unverifiable and the survivor would leak, which is not the
            # production shape this test exists to exercise.
            provider._client = SimpleNamespace(
                _pid=proc.pid, _start_time=platform_compat.get_process_start_id(proc.pid)
            )
            # Bookkeeping lies: claims dead while the OS process is alive
            provider.is_process_alive = MagicMock(return_value=False)
            provider.shutdown = AsyncMock()  # "ran" but killed nothing

            mgr, _ = _make_manager(pool_agent="kirocrew", pool_ttl_secs=1)
            self._expired_entry(mgr, provider)

            await mgr._drain_and_claim("kirocrew")

            deadline = time.monotonic() + 5
            while proc.poll() is None and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            assert proc.poll() is not None, "survivor leaked behind lying bookkeeping"
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()

    @pytest.mark.skipif(os.name != "posix", reason="POSIX signal semantics")
    @pytest.mark.asyncio
    async def test_discard_kills_real_process_when_graceful_shutdown_is_noop(self):
        """End-to-end: the OS process behind a discarded provider is gone even
        when the graceful shutdown does nothing (child ignores the protocol
        close). Exercises the real hard-kill fallback, not a mock."""
        proc = subprocess.Popen(["sleep", "300"])
        try:
            provider = _make_provider()
            # Mimic an ACP provider: the tracked PID lives at
            # provider._client._pid, and the client records that pid's start
            # identity the way AcpClient does after start
            # (client.py: self._start_time = get_process_start_id(self._pid)).
            # _sync_kill_provider re-verifies it before signalling the root.
            provider._client = SimpleNamespace(
                _pid=proc.pid, _start_time=platform_compat.get_process_start_id(proc.pid)
            )
            provider.is_process_alive = MagicMock(side_effect=lambda: proc.poll() is None)
            provider.shutdown = AsyncMock()  # graceful close that kills nothing

            mgr, _ = _make_manager(pool_agent="kirocrew", pool_ttl_secs=1)
            self._expired_entry(mgr, provider)

            pooled = await mgr._drain_and_claim("kirocrew")
            assert pooled is None

            deadline = time.monotonic() + 5
            while proc.poll() is None and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            assert proc.poll() is not None, "discarded provider process leaked"
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()

    @pytest.mark.asyncio
    async def test_dispatch_hard_kill_never_runs_inline_when_executor_down(self):
        """When the subprocess executor is already shut down (gateway
        teardown), the fallback must carry the kill on a dedicated thread —
        never inline on the event loop, where ``_sync_kill_provider`` blocks
        (``os.waitpid`` / ``taskkill``) and can trip the loop watchdog."""
        from kiro_crew.session import SessionManager

        dead_executor = ThreadPoolExecutor(max_workers=1)
        dead_executor.shutdown(wait=True)

        called_on: list[threading.Thread] = []
        done = threading.Event()

        def _record_thread(_provider) -> None:
            called_on.append(threading.current_thread())
            done.set()

        provider = _make_provider()
        with (
            patch("kiro_crew.session.subprocess_executor", return_value=dead_executor),
            patch("kiro_crew.session._sync_kill_provider", side_effect=_record_thread),
        ):
            SessionManager._dispatch_hard_kill(provider)

        assert done.wait(timeout=5), "fallback kill was never dispatched"
        assert (
            called_on[0] is not threading.main_thread()
        ), "fallback kill ran inline on the event-loop thread"

    @pytest.mark.asyncio
    async def test_one_failing_hard_kill_does_not_abort_batch_discard(self):
        """The health sweep discards several providers in one pass — a
        hard-kill failure for one provider must not escape and skip the
        discard of the remaining providers (that would re-leak them)."""
        mgr, _ = _make_manager(pool_agent="kirocrew", pool_ttl_secs=1)
        first = _make_provider()
        first.is_process_alive = MagicMock(return_value=True)
        second = _make_provider()
        second.is_process_alive = MagicMock(return_value=True)
        self._expired_entry(mgr, first)
        self._expired_entry(mgr, second)

        attempted: list[object] = []

        def _kill(provider) -> None:
            attempted.append(provider)
            if provider is first:
                raise RuntimeError("kill blew up")

        with patch("kiro_crew.session._sync_kill_provider", side_effect=_kill):
            await mgr._sweep_warm_pool_once()

        assert (
            first in attempted and second in attempted
        ), "a failing hard kill aborted the batch and leaked later providers"
        assert mgr._warm_pool.qsize() == 0


class TestFillLockReleasedAcrossStart:
    """``_fill_warm_pool`` holds ``_pool_fill_lock`` only around the queue
    mutations, releasing it across each per-iteration start-permit wait.

    A caller that waits on the lock -- ``refresh_defaults`` /
    ``reload_provider_factory`` from a config apply, the identity sweep's
    ``_retire_kiro_warm_pool`` -- interleaves between refill iterations and
    waits at most one start, not the whole refill.
    """

    @pytest.mark.asyncio
    async def test_refresh_defaults_returns_mid_refill(self):
        # Serialize starts so the refill blocks one provider at a time, the way a
        # busy ``_start_sem`` does under a stream of foreground starts.
        mgr, _ = _make_manager(pool_size=3)
        mgr._start_sem = PrioritySemaphore(1)

        first_started = asyncio.Event()
        release_start = asyncio.Event()
        started = 0

        async def _slow_start() -> None:
            nonlocal started
            started += 1
            if started == 1:
                first_started.set()
            await release_start.wait()

        def _factory(*a, **kw):
            p = _make_provider()
            p.start = AsyncMock(side_effect=_slow_start)
            return p

        mgr._provider_factory = MagicMock(side_effect=_factory)

        fill = asyncio.create_task(mgr._fill_warm_pool())
        try:
            # The refill is now parked inside the first provider's start(), with
            # the fill lock released.
            await asyncio.wait_for(first_started.wait(), timeout=2.0)

            with patch("kiro_crew.session.KiroCrewConfig.load") as mock_load:
                new_cfg = _make_cfg(pool_size=0)
                new_cfg.create_provider_factory = MagicMock(
                    return_value=MagicMock(side_effect=lambda *a, **kw: _make_provider())
                )
                mock_load.return_value = new_cfg
                # With the lock held across the whole refill this waits for all
                # three starts; released around the start it returns at once.
                await asyncio.wait_for(mgr.refresh_defaults(), timeout=2.0)

            assert mgr._cfg is new_cfg
        finally:
            release_start.set()
            await asyncio.wait_for(fill, timeout=2.0)

    @pytest.mark.asyncio
    async def test_concurrent_refills_do_not_overfill(self):
        # The fill lock guards the queue mutations only, not whole refills, so
        # the single-fill guard (``fill_active``) is what keeps a replenish
        # racing the startup fill from overshooting size.
        #
        # ``_start_sem`` is sized at 2 -- larger than the pool -- precisely so
        # the second fill is NOT blocked on the semaphore. The only thing that
        # stops it from running a real second refill is the ``fill_active``
        # guard: with the guard the second fill is an immediate no-op; without
        # it the second fill runs and parks inside its own start() on
        # ``release_start``, so the wrapped call below times out.
        mgr, _ = _make_manager(pool_size=2)
        mgr._start_sem = PrioritySemaphore(2)

        release_start = asyncio.Event()
        in_start = asyncio.Event()
        started = 0

        async def _slow_start() -> None:
            nonlocal started
            started += 1
            in_start.set()
            await release_start.wait()

        def _factory(*a, **kw):
            p = _make_provider()
            p.start = AsyncMock(side_effect=_slow_start)
            return p

        mgr._provider_factory = MagicMock(side_effect=_factory)

        first = asyncio.create_task(mgr._fill_warm_pool())
        try:
            # The first fill is parked inside a start() with the fill lock
            # released and a free start permit -- a second fill could run were
            # it not for the guard.
            await asyncio.wait_for(in_start.wait(), timeout=2.0)
            # The ``fill_active`` guard makes a second refill a no-op while the
            # first is live, so this returns at once without starting a
            # provider of its own. Delete the guard and this second fill
            # instead runs a real refill that blocks inside its own start() on
            # ``release_start`` -- the ``wait_for`` then fails with a timeout
            # (the hang the guard prevents), not a passing no-op.
            await asyncio.wait_for(mgr._fill_warm_pool(), timeout=2.0)
            assert started == 1
        finally:
            release_start.set()
            await asyncio.wait_for(first, timeout=2.0)

        assert mgr._warm_pool.qsize() == 2
        assert started == 2

    @pytest.mark.asyncio
    async def test_factory_swap_mid_start_discards_the_stale_provider(self):
        # The lock is released across the start, so a config apply can swap the
        # factory in that window. The provider built from the retired factory
        # must be discarded under the enqueue lock, never seeded into the pool
        # the apply just drained.
        #
        # ``_pool_size`` stays at 1 throughout, so the factory-identity check --
        # not a size check -- is the sole reason the stale provider is dropped:
        # the next iteration builds from the new factory and fills the one slot.
        mgr, old_factory = _make_manager(pool_size=1)
        mgr._start_sem = PrioritySemaphore(1)

        in_start = asyncio.Event()
        release_start = asyncio.Event()
        stale_providers: list = []
        fresh_providers: list = []

        async def _slow_start() -> None:
            in_start.set()
            await release_start.wait()

        def _stale_factory(*a, **kw):
            p = _make_provider()
            p.start = AsyncMock(side_effect=_slow_start)
            stale_providers.append(p)
            return p

        def _fresh_factory(*a, **kw):
            p = _make_provider()
            fresh_providers.append(p)
            return p

        mgr._provider_factory = MagicMock(side_effect=_stale_factory)

        fill = asyncio.create_task(mgr._fill_warm_pool())
        try:
            await asyncio.wait_for(in_start.wait(), timeout=2.0)
            # Swap the factory while the refill is parked inside start();
            # pool_size is left at 1 so there is still room for a provider -- the
            # stale one must be dropped purely because its factory is retired.
            new_factory = MagicMock(side_effect=_fresh_factory)
            mgr._provider_factory = new_factory
        finally:
            release_start.set()
            await asyncio.wait_for(fill, timeout=2.0)

        # The stale provider was discarded; the one queued slot holds a provider
        # built from the new factory.
        assert len(stale_providers) == 1
        stale_providers[0].shutdown.assert_awaited_once()
        assert mgr._warm_pool.qsize() == 1
        assert len(fresh_providers) == 1
        queued, _spawn_time = mgr._warm_pool.get_nowait()
        assert queued is fresh_providers[0]
        assert mgr._provider_factory is new_factory

    @pytest.mark.asyncio
    async def test_pre_epoch_provider_discarded_not_enqueued(self):
        # ``_retire_kiro_warm_pool`` can mark the identity epoch and drain the
        # queue during the released-lock window of a fill parked inside start().
        # The pre-epoch provider that fill holds must be discarded under the
        # enqueue lock, not seeded behind the drain -- the health sweep checks
        # TTL only, so an enqueued stale-identity provider would run until it
        # ages out (up to the TTL) holding a pool slot.
        mgr, _ = _make_manager(pool_size=1)
        mgr._start_sem = PrioritySemaphore(1)

        in_start = asyncio.Event()
        release_start = asyncio.Event()
        providers: list = []

        async def _slow_start() -> None:
            in_start.set()
            await release_start.wait()

        def _factory(*a, **kw):
            p = _make_provider()
            p.start = AsyncMock(side_effect=_slow_start)
            # The identity predicate reads this capability the object declares;
            # a pre-epoch provider that authenticates from the retired store
            # must be refused.
            p.uses_kiro_identity_store = True
            providers.append(p)
            return p

        mgr._provider_factory = MagicMock(side_effect=_factory)

        fill = asyncio.create_task(mgr._fill_warm_pool())
        try:
            # The provider is building/starting before any epoch is marked, so
            # its spawn_time predates the epoch set below.
            await asyncio.wait_for(in_start.wait(), timeout=2.0)
            # The identity sweep only marks the epoch -- pool_size stays 1, so
            # the size check still has room for a provider. The ONLY reason the
            # pre-epoch provider is refused is the identity-epoch check; delete
            # that check and the provider would be enqueued and this test fail.
            mgr._pool.mark_identity_epoch()
        finally:
            release_start.set()
            await asyncio.wait_for(fill, timeout=2.0)

        # Pool still has room (size 1) yet nothing is enqueued: the pre-epoch
        # provider was discarded, not seeded behind the drain.
        assert mgr._warm_pool.qsize() == 0
        # The epoch mismatch stops the fill, so the loop never re-spawns a
        # second provider against the retired identity.
        assert len(providers) == 1
        providers[0].shutdown.assert_awaited_once()


class TestExplicitEffortDefaultAllocation:
    """A pending explicit Default stays durable until a start applies its projection."""

    KEY = "test-key"

    @staticmethod
    def _cold_manager(applied: bool, start: AsyncMock | None = None):
        mgr, factory = _make_manager()
        mgr._drain_and_claim = AsyncMock(return_value=_make_provider())
        mgr._schedule_replenish = MagicMock()
        mgr._record_pool_decision = MagicMock()
        provider = _make_provider()
        provider.explicit_effort_default_applied = applied
        if start is not None:
            provider.start = start
        factory.side_effect = None
        factory.return_value = provider
        return mgr, factory, provider

    @pytest.mark.asyncio
    async def test_a_pending_default_skips_the_pool_and_arms_the_provider(self):
        mgr, _factory, provider = self._cold_manager(applied=True)
        mgr.set_explicit_effort_default(self.KEY, True)

        created, _is_new, _ = await mgr.get_or_create(self.KEY, agent="kirocrew")

        # A warm process read cli.json at its own spawn and Default cannot be
        # pushed live, so only a cold start can apply the intent.
        assert created is provider
        mgr._drain_and_claim.assert_not_awaited()
        mgr._record_pool_decision.assert_called_once_with("bypass_effort", self.KEY)
        provider.arm_explicit_effort_default.assert_called_once_with(
            mgr.fence_effort_overlay_rewrite
        )
        assert mgr.explicit_effort_default_pending(self.KEY) is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("applied", [True, False])
    async def test_the_key_is_reserved_from_the_basis_read_through_registration(self, applied):
        # The span the effort handler's refusal rests on: the allocation
        # reservation is held before the start reads the one-shot Default and
        # is still held when the session is registered, so no pick can change
        # the basis a start publishes on. ``effort_basis_locked`` is that
        # reservation, read as the handler reads it.
        mgr, _factory, provider = self._cold_manager(applied=applied)
        mgr.set_explicit_effort_default(self.KEY, True)
        seen: list[tuple[str, bool]] = []
        real_pending = mgr.explicit_effort_default_pending

        def pending(key: str) -> bool:
            seen.append(("read", mgr.effort_basis_locked(key)))
            return real_pending(key)

        async def start() -> None:
            seen.append(("start", mgr.effort_basis_locked(self.KEY)))

        class Registry(dict[str, object]):
            def __setitem__(self, key: str, session: object) -> None:
                seen.append(("register", mgr.effort_basis_locked(key)))
                super().__setitem__(key, session)

        mgr.explicit_effort_default_pending = pending
        provider.start = AsyncMock(side_effect=start)
        mgr._allocation_boundary()._sessions = Registry()
        assert mgr.effort_basis_locked(self.KEY) is False

        created, _is_new, _ = await mgr.get_or_create(self.KEY, agent="kirocrew")

        assert created is provider
        assert seen[0] == ("read", True)
        assert ("start", True) in seen
        assert seen[-1] == ("register", True)
        assert all(locked for _step, locked in seen)
        # Released once the allocation returned: the next pick lands.
        assert mgr.effort_basis_locked(self.KEY) is False

    # -- a pick saving its flag when the start begins settles before the read --
    #
    # The handler's check passes before the start takes the key's reservation,
    # so the pick's in-memory write is already there when the start begins and
    # its save is still in flight. The start waits for that write to settle
    # before its single basis read, so it reads the saved value or the one a
    # failed save put back, never an in-memory value no save keeps.

    async def _a_default_pick_saving_as_the_start_begins(
        self,
        mgr,
        outcome: str,
        pick_key: str | None = None,
        start_key: str | None = None,
        alias_folds: list[tuple[str, str]] | None = None,
    ):
        """Race a Default pick's save against a key's cold start.

        The pick is the effort handler's write as ``record_default_intent``
        makes it: the ``effort_basis_locked`` check passes (no reservation yet),
        the flag is written in memory under the key's intent-write count, and
        the save runs until the start has reached its basis read. ``outcome``
        is what the save then does: "saved", "raised" or "cancelled". Returns
        the pick's task and the start's reads as ``(value, save_settled)``.
        """
        pick_key = self.KEY if pick_key is None else pick_key
        start_key = self.KEY if start_key is None else start_key
        save_started = asyncio.Event()
        save_release = asyncio.Event()
        start_at_read = asyncio.Event()
        save_settled = False
        flushes = 0

        async def aflush() -> None:
            nonlocal flushes, save_settled
            flushes += 1
            if flushes > 1:
                # The pick's put-back save and the start's post-application clear save.
                return
            save_started.set()
            await _await_test(save_release.wait(), "save_release")
            save_settled = True
            if outcome == "raised":
                raise OSError("disk full")
            if outcome == "cancelled":
                raise asyncio.CancelledError()

        real_wait = mgr.wait_for_effort_intent_writes
        real_pending = mgr.explicit_effort_default_pending
        reads: list[tuple[bool, bool]] = []

        async def wait_for_writes(key: str) -> None:
            # The start announces it is at its read; the wait right before it
            # is where it stops while the save is in flight.
            if alias_folds is not None:
                alias_folds.append((mgr._fold_key(pick_key), mgr._fold_key(start_key)))
            start_at_read.set()
            await real_wait(key)

        def pending(key: str) -> bool:
            # Announced here too, for a start that reaches the read without
            # waiting: the save is then released only after the read landed.
            start_at_read.set()
            value = real_pending(key)
            reads.append((value, save_settled))
            return value

        async def pick() -> bool:
            assert mgr.effort_basis_locked(pick_key) is False
            with mgr.effort_intent_write(pick_key):
                prior = real_pending(pick_key)
                assert mgr.set_explicit_effort_default(pick_key, True) is True
                try:
                    await mgr.aflush()
                except asyncio.CancelledError:
                    mgr.set_explicit_effort_default(pick_key, prior)
                    await mgr.aflush()
                    raise
                except Exception:
                    mgr.set_explicit_effort_default(pick_key, prior)
                    return False
            return True

        mgr.aflush = aflush
        mgr.wait_for_effort_intent_writes = wait_for_writes
        mgr.explicit_effort_default_pending = pending
        alias_seed = None
        if alias_folds is not None:
            alias_seed = object()
            mgr._allocation_boundary()._sessions[start_key] = alias_seed
        pick_task = asyncio.create_task(pick())
        start_task: asyncio.Task | None = None
        try:
            await _await_test(save_started.wait(), "default save starting")
            if alias_seed is not None:
                assert mgr._allocation_boundary()._sessions.pop(start_key) is alias_seed
            start_task = asyncio.create_task(mgr.get_or_create(start_key, agent="kirocrew"))
            await _await_test(start_at_read.wait(), "cold start reaching its basis read")
            save_release.set()
            created, _is_new, _ = await _await_test(start_task, "cold start completion")
        except BaseException:
            save_release.set()
            participants = [pick_task]
            if start_task is not None:
                participants.append(start_task)
            for participant in participants:
                if not participant.done():
                    participant.cancel()
            await _await_test(
                asyncio.gather(*participants, return_exceptions=True),
                "allocation-race cleanup",
            )
            raise
        return pick_task, created, reads

    @pytest.mark.asyncio
    async def test_a_default_whose_save_raises_under_a_cold_start_is_not_read_by_it(self):
        mgr, _factory, provider = self._cold_manager(applied=True)

        pick_task, created, reads = await self._a_default_pick_saving_as_the_start_begins(
            mgr, "raised"
        )

        # The pick is refused (the handler answers 503) and put the flag back;
        # the start read that restored value, after the save had settled, so it
        # took the warm pool as a start with no Default pending does and armed
        # nothing.
        assert await _await_test(pick_task, "pick_task") is False
        assert reads[0] == (False, True)
        assert created is not provider
        mgr._drain_and_claim.assert_awaited_once()
        provider.arm_explicit_effort_default.assert_not_called()
        assert mgr.explicit_effort_default_pending(self.KEY) is False
        assert self.KEY not in mgr._effort_intent_writes
        assert mgr.effort_basis_locked(self.KEY) is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("pick_key", "start_key"),
        [
            ("slack:1700000000.000100", "1700000000.000100"),
            ("1700000000.000100", "slack:1700000000.000100"),
        ],
    )
    async def test_a_start_under_an_alias_waits_for_a_failed_default_save(
        self, pick_key, start_key
    ):
        mgr, _factory, provider = self._cold_manager(applied=True)
        alias_folds: list[tuple[str, str]] = []

        pick_task, created, reads = await self._a_default_pick_saving_as_the_start_begins(
            mgr,
            "raised",
            pick_key=pick_key,
            start_key=start_key,
            alias_folds=alias_folds,
        )

        assert alias_folds[0][0] == alias_folds[0][1]
        assert await _await_test(pick_task, "pick_task") is False
        assert reads[0] == (False, True)
        assert created is not provider
        mgr._drain_and_claim.assert_awaited_once()
        provider.arm_explicit_effort_default.assert_not_called()
        assert mgr.explicit_effort_default_pending(start_key) is False
        assert "slack:1700000000.000100" not in mgr._effort_intent_writes

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("pick_key", "start_key"),
        [
            ("slack:1700000000.000100", "1700000000.000100"),
            ("1700000000.000100", "slack:1700000000.000100"),
        ],
    )
    async def test_a_cold_start_under_an_alias_waits_for_a_failed_default_save(
        self, pick_key, start_key
    ):
        mgr, _factory, provider = self._cold_manager(applied=True)

        pick_task, created, reads = await self._a_default_pick_saving_as_the_start_begins(
            mgr,
            "raised",
            pick_key=pick_key,
            start_key=start_key,
        )

        assert await _await_test(pick_task, "pick_task") is False
        assert reads[0] == (False, True)
        assert created is not provider
        mgr._drain_and_claim.assert_awaited_once()
        provider.arm_explicit_effort_default.assert_not_called()
        assert mgr.explicit_effort_default_pending(start_key) is False

    @pytest.mark.asyncio
    async def test_a_default_whose_save_lands_under_a_cold_start_is_applied_and_cleared(self):
        mgr, _factory, provider = self._cold_manager(applied=True)

        pick_task, created, reads = await self._a_default_pick_saving_as_the_start_begins(
            mgr, "saved"
        )

        # The start read the saved value once the save had settled, skipped the
        # pool for it, applied it, and cleared the durable flag afterward.
        assert await _await_test(pick_task, "pick_task") is True
        assert reads[0] == (True, True)
        assert created is provider
        mgr._drain_and_claim.assert_not_awaited()
        provider.arm_explicit_effort_default.assert_called_once_with(
            mgr.fence_effort_overlay_rewrite
        )
        assert mgr.explicit_effort_default_pending(self.KEY) is False
        assert self.KEY not in mgr._effort_intent_writes

    @pytest.mark.asyncio
    async def test_a_default_whose_save_is_cancelled_under_a_cold_start_is_not_read_by_it(self):
        mgr, _factory, provider = self._cold_manager(applied=True)

        pick_task, created, reads = await self._a_default_pick_saving_as_the_start_begins(
            mgr, "cancelled"
        )

        with pytest.raises(asyncio.CancelledError):
            await _await_test(pick_task, "pick_task")
        assert reads[0] == (False, True)
        assert created is not provider
        mgr._drain_and_claim.assert_awaited_once()
        provider.arm_explicit_effort_default.assert_not_called()
        assert mgr.explicit_effort_default_pending(self.KEY) is False
        assert self.KEY not in mgr._effort_intent_writes

    @pytest.mark.asyncio
    async def test_the_keys_write_count_is_gone_once_its_last_write_has_exited(self):
        # Nothing outlives a write: the entry is removed when the count reaches
        # zero, however the write ended, and a waiter wakes at that moment.
        mgr, _factory, _provider = self._cold_manager(applied=True)

        with mgr.effort_intent_write(self.KEY):
            assert mgr._effort_intent_writes[self.KEY].count == 1
        assert self.KEY not in mgr._effort_intent_writes

        with pytest.raises(OSError):
            with mgr.effort_intent_write(self.KEY):
                raise OSError("disk full")
        assert self.KEY not in mgr._effort_intent_writes

        with pytest.raises(asyncio.CancelledError):
            with mgr.effort_intent_write(self.KEY):
                raise asyncio.CancelledError()
        assert self.KEY not in mgr._effort_intent_writes

        # Two picks under one key: the entry stays until the last one exits,
        # and a waiter is released only then.
        waiting = asyncio.Event()

        async def waiter() -> None:
            waiting.set()
            await mgr.wait_for_effort_intent_writes(self.KEY)

        with mgr.effort_intent_write(self.KEY):
            with mgr.effort_intent_write(self.KEY):
                entry = mgr._effort_intent_writes[self.KEY]
                assert entry.count == 2
                waiter_task = asyncio.create_task(waiter())
                await _await_test(waiting.wait(), "waiting")
                assert not waiter_task.done()
            assert entry.count == 1
            assert not entry.settled.is_set()
            assert not waiter_task.done()
        assert entry.settled.is_set()
        await _await_test(waiter_task, "waiter_task")
        assert mgr._effort_intent_writes == {}

    @pytest.mark.asyncio
    async def test_with_no_write_in_flight_the_basis_read_is_not_delayed(self):
        mgr, _factory, provider = self._cold_manager(applied=True)
        mgr.set_explicit_effort_default(self.KEY, True)
        assert getattr(mgr, "_effort_intent_writes", {}) == {}

        # The wait completes on its first step: nothing to wait for, no
        # suspension, and no write was ever entered.
        wait = mgr.wait_for_effort_intent_writes(self.KEY)
        with pytest.raises(StopIteration):
            wait.send(None)

        created, _is_new, _ = await mgr.get_or_create(self.KEY, agent="kirocrew")

        assert created is provider
        provider.arm_explicit_effort_default.assert_called_once_with(
            mgr.fence_effort_overlay_rewrite
        )
        assert mgr.explicit_effort_default_pending(self.KEY) is False
        assert getattr(mgr, "_effort_intent_writes", {}) == {}

    @pytest.mark.asyncio
    async def test_a_start_that_did_not_apply_it_keeps_the_intent(self):
        mgr, _factory, _provider = self._cold_manager(applied=False)
        mgr.set_explicit_effort_default(self.KEY, True)

        await mgr.get_or_create(self.KEY, agent="kirocrew")

        # The projection could not rewrite the file (locked, a link, past its
        # ceiling), so the next cold start tries again.
        assert mgr.explicit_effort_default_pending(self.KEY) is True

    @pytest.mark.asyncio
    async def test_the_start_rewrites_inside_the_warm_pools_fence(self):
        inside: list[int] = []

        async def start() -> None:
            (fence,) = provider.arm_explicit_effort_default.call_args.args
            with fence():
                inside.append(mgr._pool.state.effort_overlay_rewrites)

        mgr, _factory, provider = self._cold_manager(
            applied=True, start=AsyncMock(side_effect=start)
        )
        mgr.set_explicit_effort_default(self.KEY, True)
        before = time.monotonic()

        await mgr.get_or_create(self.KEY, agent="kirocrew")

        # The fence the start rewrites inside is the pool's: claims are refused
        # while it is up, and runtimes queued before it came down are discarded.
        assert inside == [1]
        assert mgr._pool.state.effort_overlay_rewrites == 0
        assert mgr._pool.state.effort_overlay_epoch >= before

    @pytest.mark.asyncio
    @pytest.mark.parametrize("error", [RuntimeError("spawn failed"), asyncio.CancelledError()])
    @pytest.mark.parametrize("applied", [True, False])
    async def test_a_start_that_fails_arms_it_again_only_when_it_did_not_apply(
        self, applied, error
    ):
        mgr, _factory, _provider = self._cold_manager(
            applied=applied, start=AsyncMock(side_effect=error)
        )
        mgr.set_explicit_effort_default(self.KEY, True)

        with pytest.raises(type(error)):
            await mgr.get_or_create(self.KEY, agent="kirocrew")

        # A projection that ran is cleared in memory for the deferred save. A
        # failure before projection leaves the durable intent untouched.
        assert mgr.explicit_effort_default_pending(self.KEY) is (not applied)
        assert mgr._allocation_state.explicit_effort_default_reservations == set()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("start_fails", [False, True])
    async def test_a_default_revoked_while_the_start_spawns_stays_revoked(
        self, monkeypatch, start_fails
    ):
        from kiro_crew import session_allocation

        async def pre_spawn_identity(*_args, **_kwargs):
            # A second allocation under the same key clears the flag between
            # this start's read of it and its own reservation: both read it
            # pending, and only the one that still sees the flag may reserve and
            # arm the replacement. (A pick cannot write here: the effort handler
            # refuses one while the key's allocation reservation is held.)
            assert mgr.set_explicit_effort_default(self.KEY, False) is True
            return ""

        monkeypatch.setattr(session_allocation, "pre_spawn_identity", pre_spawn_identity)
        start = AsyncMock(side_effect=RuntimeError("spawn failed")) if start_fails else None
        mgr, _factory, provider = self._cold_manager(applied=False, start=start)
        assert mgr.set_explicit_effort_default(self.KEY, True) is True

        if start_fails:
            with pytest.raises(RuntimeError, match="spawn failed"):
                await mgr.get_or_create(self.KEY, agent="kirocrew")
        else:
            await mgr.get_or_create(self.KEY, agent="kirocrew")

        # The pending Default was the authorization to remove an entry Kiro
        # Crew did not write. Once another allocation clears it, this start arms
        # no replacement and a later failure does not restore that old intent.
        provider.arm_explicit_effort_default.assert_not_called()
        assert mgr.explicit_effort_default_pending(self.KEY) is False

    @pytest.mark.asyncio
    async def test_the_flag_stays_on_disk_until_the_projection_applies(self, tmp_path):
        from kiro_crew.session_map import SessionMap

        work_dir = tmp_path / "work"
        settings = work_dir / ".kiro" / "settings"
        settings.mkdir(parents=True)
        cli_json = settings / "cli.json"
        cli_json.write_text(
            '{"chat":{"modelDefaults":{"gpt-5.6":{"reasoning":{"effort":"max"}}}}}',
            encoding="utf-8",
        )
        start_entered = asyncio.Event()
        apply_projection = asyncio.Event()

        async def start() -> None:
            start_entered.set()
            await _await_test(apply_projection.wait(), "projection release")
            cli_json.write_text('{"chat":{"modelDefaults":{}}}', encoding="utf-8")
            provider.explicit_effort_default_applied = True

        mgr, factory = _make_manager()
        provider = _make_provider()
        provider.explicit_effort_default_applied = False
        provider.start = AsyncMock(side_effect=start)
        factory.side_effect = None
        factory.return_value = provider
        mgr._drain_and_claim = AsyncMock(return_value=None)
        mgr._schedule_replenish = MagicMock()
        mgr._record_pool_decision = MagicMock()
        mgr.set_explicit_effort_default(self.KEY, True)
        await mgr.aflush()

        allocation = asyncio.create_task(
            mgr.get_or_create(self.KEY, agent="kirocrew", cwd=str(work_dir))
        )
        await _await_test(start_entered.wait(), "provider start")

        # This fresh reader sees the file, not the manager's in-memory map. A
        # kill here must leave the next process enough intent to replay Default.
        assert SessionMap().get_flag(self.KEY, "explicit_effort_default") is True
        assert '"effort":"max"' in cli_json.read_text(encoding="utf-8")

        apply_projection.set()
        created, _is_new, _ = await _await_test(allocation, "allocation")
        assert created is provider
        assert SessionMap().get_flag(self.KEY, "explicit_effort_default") is False
        assert '"effort":"max"' not in cli_json.read_text(encoding="utf-8")

    @pytest.mark.asyncio
    async def test_a_kill_before_projection_leaves_the_next_manager_armed(self):
        from kiro_crew.session_map import SessionMap

        mgr, _factory, _provider = self._cold_manager(applied=False)
        mgr.set_explicit_effort_default(self.KEY, True)
        await mgr.aflush()
        allocation = mgr._allocation_boundary()
        with mgr.hold_explicit_effort_default(self.KEY):
            assert allocation._reserve_explicit_effort_default(self.KEY) is True

        # Simulate process death: no provider exception arm and no manager cleanup.
        del allocation
        del mgr
        assert SessionMap().get_flag(self.KEY, "explicit_effort_default") is True

        restarted, _factory, provider = self._cold_manager(applied=True)
        created, _is_new, _ = await restarted.get_or_create(self.KEY, agent="kirocrew")

        assert created is provider
        provider.arm_explicit_effort_default.assert_called_once_with(
            restarted.fence_effort_overlay_rewrite
        )
        assert restarted.explicit_effort_default_pending(self.KEY) is False

    @pytest.mark.asyncio
    async def test_a_cancelled_clear_keeps_the_applied_default_cleared_in_memory(self):
        mgr, _factory, provider = self._cold_manager(applied=True)
        mgr.set_explicit_effort_default(self.KEY, True)
        mgr.aflush = AsyncMock(side_effect=asyncio.CancelledError())

        with pytest.raises(asyncio.CancelledError):
            await mgr.get_or_create(self.KEY, agent="kirocrew")

        provider.start.assert_awaited_once()
        assert mgr.explicit_effort_default_pending(self.KEY) is False
        assert mgr._allocation_state.explicit_effort_default_reservations == set()

    @pytest.mark.asyncio
    async def test_the_clear_is_saved_only_after_the_projection_applies(self):
        mgr, _factory, provider = self._cold_manager(applied=True)
        mgr.set_explicit_effort_default(self.KEY, True)
        seen: list[tuple[bool, int, int]] = []

        async def aflush():
            seen.append(
                (
                    mgr.explicit_effort_default_pending(self.KEY),
                    provider.arm_explicit_effort_default.call_count,
                    provider.start.await_count,
                )
            )

        mgr.aflush = aflush

        await mgr.get_or_create(self.KEY, agent="kirocrew")

        assert seen == [(False, 1, 1)]
        provider.arm_explicit_effort_default.assert_called_once_with(
            mgr.fence_effort_overlay_rewrite
        )
        assert mgr.explicit_effort_default_pending(self.KEY) is False

    @pytest.mark.asyncio
    async def test_a_clear_save_failure_does_not_fail_the_start(self, caplog):
        mgr, _factory, provider = self._cold_manager(applied=True)
        mgr.set_explicit_effort_default(self.KEY, True)
        real_aflush = mgr.aflush
        attempts = 0

        async def fail_once() -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("disk full")
            await real_aflush()

        mgr.aflush = fail_once

        created, _is_new, _ = await mgr.get_or_create(self.KEY, agent="kirocrew")

        assert created is provider
        assert self.KEY in mgr._sessions
        assert mgr.explicit_effort_default_pending(self.KEY) is False
        assert "the Default already ran" in caplog.text

        await mgr.aflush()
        from kiro_crew.session_map import SessionMap

        assert SessionMap().get_flag(self.KEY, "explicit_effort_default") is False

    def test_two_allocations_of_one_canonical_key_only_one_arms(self):
        mgr, _factory, _provider = self._cold_manager(applied=False)
        service = mgr._allocation_boundary()
        slack_key = "slack:1700000000.000100"
        bare_key = "1700000000.000100"
        assert mgr.set_explicit_effort_default(slack_key, True) is True
        providers = [_make_provider(), _make_provider()]

        for key, provider in zip((slack_key, bare_key), providers, strict=True):
            with mgr.hold_explicit_effort_default(key):
                if service._reserve_explicit_effort_default(key):
                    provider.arm_explicit_effort_default(mgr.fence_effort_overlay_rewrite)

        assert providers[0].arm_explicit_effort_default.call_count == 1
        providers[1].arm_explicit_effort_default.assert_not_called()
        service._release_explicit_effort_default(slack_key)
        assert service.state.explicit_effort_default_reservations == set()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["unapplied", "raised"])
    async def test_another_chat_cannot_take_the_row_while_the_flag_is_armed(
        self, monkeypatch, path
    ):
        from kiro_crew import session_map as session_map_module

        monkeypatch.setattr(session_map_module, "EXPLICIT_EFFORT_DEFAULT_ROW_CAP", 1)
        other = "dashboard:another-chat"
        admitted = []

        def another_chat_picks_default():
            # Another chat's Default pick while this key's flag keeps the row counted.
            if not admitted:
                admitted.append(mgr.set_explicit_effort_default(other, True))

        async def start():
            another_chat_picks_default()
            if path == "raised":
                raise RuntimeError("spawn failed")

        mgr, _factory, _provider = self._cold_manager(
            applied=False, start=AsyncMock(side_effect=start)
        )
        assert mgr.set_explicit_effort_default(self.KEY, True) is True

        if path == "raised":
            with pytest.raises(RuntimeError, match="spawn failed"):
                await mgr.get_or_create(self.KEY, agent="kirocrew")
        else:
            await mgr.get_or_create(self.KEY, agent="kirocrew")

        # The armed flag keeps this key's row counted, so the other pick is
        # refused and the row bound remains intact.
        assert admitted == [False]
        assert mgr.explicit_effort_default_pending(self.KEY) is True
        assert mgr.explicit_effort_default_pending(other) is False

    @pytest.mark.asyncio
    async def test_the_row_is_free_again_once_an_applied_start_clears_the_flag(self, monkeypatch):
        from kiro_crew import session_map as session_map_module

        monkeypatch.setattr(session_map_module, "EXPLICIT_EFFORT_DEFAULT_ROW_CAP", 1)
        other = "dashboard:another-chat"
        during_start = []

        async def start():
            during_start.append(mgr.set_explicit_effort_default(other, True))

        mgr, _factory, _provider = self._cold_manager(
            applied=True, start=AsyncMock(side_effect=start)
        )
        assert mgr.set_explicit_effort_default(self.KEY, True) is True

        await mgr.get_or_create(self.KEY, agent="kirocrew")

        # Refused while the flag keeps this key's row counted; free once the
        # replacement runs and clears it.
        assert during_start == [False]
        assert mgr.explicit_effort_default_pending(self.KEY) is False
        assert mgr.set_explicit_effort_default(other, True) is True

    @pytest.mark.asyncio
    async def test_the_applied_clear_frees_the_row_before_the_session_is_registered(
        self, monkeypatch
    ):
        from kiro_crew import session_allocation
        from kiro_crew import session_map as session_map_module

        monkeypatch.setattr(session_map_module, "EXPLICIT_EFFORT_DEFAULT_ROW_CAP", 1)
        other = "dashboard:another-chat"
        after_start = []

        async def stamp_spawn_identity(*_args, **_kwargs):
            after_start.append(mgr.set_explicit_effort_default(other, True))

        monkeypatch.setattr(session_allocation, "stamp_spawn_identity", stamp_spawn_identity)
        mgr, _factory, _provider = self._cold_manager(applied=True)
        assert mgr.set_explicit_effort_default(self.KEY, True) is True

        await mgr.get_or_create(self.KEY, agent="kirocrew")

        # The applied clear frees the row before the allocation registers the session.
        assert after_start == [True]

    @pytest.mark.asyncio
    async def test_a_level_start_leaves_the_row_to_the_default_it_does_not_apply(self, monkeypatch):
        # A level start does not clear a Default it does not apply, so it never
        # writes the flag: the pending flag itself keeps the row counted, and
        # another chat's pick is refused during the start and after it.
        from kiro_crew import session_map as session_map_module

        monkeypatch.setattr(session_map_module, "EXPLICIT_EFFORT_DEFAULT_ROW_CAP", 1)
        other = "dashboard:another-chat"
        during_start = []
        writes_of_this_key = []

        async def start():
            during_start.append(mgr.set_explicit_effort_default(other, True))

        mgr, _factory, _provider = self._cold_manager(
            applied=True, start=AsyncMock(side_effect=start)
        )
        mgr._drain_and_claim = AsyncMock(return_value=None)
        assert mgr.set_explicit_effort_default(self.KEY, True) is True
        real_set_explicit_effort_default = mgr.set_explicit_effort_default

        def record_writes(key: str, pending: bool) -> bool:
            if mgr._fold_key(key) == mgr._fold_key(self.KEY):
                writes_of_this_key.append(pending)
            return real_set_explicit_effort_default(key, pending)

        mgr.set_explicit_effort_default = record_writes

        await mgr.get_or_create(self.KEY, agent="kirocrew", reasoning_effort_override="high")

        assert writes_of_this_key == []
        assert during_start == [False]
        assert mgr.explicit_effort_default_pending(self.KEY) is True
        assert mgr.explicit_effort_default_pending(other) is False
        assert mgr.set_explicit_effort_default(other, True) is False

    @pytest.mark.asyncio
    async def test_a_level_the_caller_starts_on_leaves_a_pending_default_in_place(self):
        # A start that does not apply the Default must not clear it: the level
        # the caller chose is not a newer effort action on the key (a level pick
        # through the effort handler, or a level session_set_model commits at
        # the turn start, clears the flag itself), so the Default is left for
        # the next start that takes no override.
        mgr, _factory, provider = self._cold_manager(applied=True)
        mgr._drain_and_claim = AsyncMock(return_value=None)
        mgr.set_explicit_effort_default(self.KEY, True)

        await mgr.get_or_create(self.KEY, agent="kirocrew", reasoning_effort_override="high")

        # A stale Default never authorizes replacing an entry for a level start,
        # and the level start leaves it pending.
        provider.arm_explicit_effort_default.assert_not_called()
        assert mgr.explicit_effort_default_pending(self.KEY) is True

    @pytest.mark.asyncio
    async def test_a_committed_session_set_model_level_spends_the_pending_default(
        self, tmp_path, monkeypatch
    ):
        from chat_test_helpers import _make_state

        from kiro_crew.dashboard import session_control as session_control_module
        from kiro_crew.dashboard.chat_utils import effective_session_key, slot_history_key

        mgr, _factory, provider = self._cold_manager(applied=True)
        mgr._drain_and_claim = AsyncMock(return_value=None)
        state = _make_state(tmp_path)
        caller = state.get_or_create_slot("chat-1")
        target = state.get_or_create_slot("chat-2")
        state.sessions = mgr
        monkeypatch.setattr(session_control_module, "session_control_enabled", lambda: True)
        key = effective_session_key(target)
        mgr.set_explicit_effort_default(key, True)
        settings = tmp_path / ".kiro" / "settings" / "cli.json"
        settings.parent.mkdir(parents=True)
        original = b'{"chat":{"modelDefaults":{"sonnet":{"output_config":{"effort":"max"}}}}}'
        settings.write_bytes(original)

        await session_control_module.set_model_target(
            state,
            caller_session_key=slot_history_key(caller),
            target="chat-2",
            reasoning_effort="high",
        )
        assert session_control_module.apply_pending_model_pick(state, target) is True
        assert mgr.explicit_effort_default_pending(key) is False

        created, _is_new, _ = await mgr.get_or_create(key, agent="kirocrew", cwd=str(tmp_path))

        assert created is provider
        provider.arm_explicit_effort_default.assert_not_called()
        assert settings.read_bytes() == original

    @pytest.mark.asyncio
    async def test_a_pending_default_survives_a_level_start_and_the_next_start_applies_it(
        self,
    ):
        """A start carrying an override never clears a Default it does not apply.

        The override is a level the caller chose: a slot's level read before the
        Default pick landed, an alias slot's own level, or a caller's pin. That
        start runs the level, arms nothing, and leaves the flag pending; the
        key's next start without an override skips the pool for it, arms the
        provider and clears it after the projection. Before the fix the level
        start cleared the flag at its basis read, so the next start found nothing
        to apply.
        """
        mgr, factory, level_provider = self._cold_manager(applied=True)
        mgr._drain_and_claim = AsyncMock(return_value=None)
        assert mgr.set_explicit_effort_default(self.KEY, True) is True

        created, _is_new, _ = await mgr.get_or_create(
            self.KEY, agent="kirocrew", reasoning_effort_override="high"
        )

        # The level start ran the caller's level and took the pool path (a miss
        # here), arming no Default and leaving the flag as it found it.
        assert created is level_provider
        assert factory.call_args.kwargs["reasoning_effort_override"] == "high"
        level_provider.arm_explicit_effort_default.assert_not_called()
        assert mgr.explicit_effort_default_pending(self.KEY) is True
        assert mgr._record_pool_decision.call_args_list == [call("miss_empty", self.KEY)]

        mgr.release(self.KEY)
        await mgr.reset(self.KEY)
        default_provider = _make_provider()
        default_provider.explicit_effort_default_applied = True
        factory.return_value = default_provider

        created, _is_new, _ = await mgr.get_or_create(self.KEY, agent="kirocrew")

        # The next start without an override applies the Default and clears it.
        assert created is default_provider
        assert "reasoning_effort_override" not in factory.call_args.kwargs
        default_provider.arm_explicit_effort_default.assert_called_once_with(
            mgr.fence_effort_overlay_rewrite
        )
        assert mgr.explicit_effort_default_pending(self.KEY) is False
        assert mgr._record_pool_decision.call_args_list[-1] == call("bypass_effort", self.KEY)
        mgr._drain_and_claim.assert_awaited_once()

    def test_a_provider_that_reads_no_overlay_has_nothing_to_arm(self):
        from kiro_crew.providers.base import LLMProvider

        # Declared on the base so the session layer calls and reads them without
        # a probe; a provider with no workspace overlay clears an applied Default.
        assert LLMProvider.arm_explicit_effort_default(object(), nullcontext) is None
        assert LLMProvider.explicit_effort_default_applied.fget(object()) is True

    @pytest.mark.asyncio
    async def test_without_a_pending_default_nothing_is_armed(self):
        mgr, _factory, provider = self._cold_manager(applied=False)
        mgr._drain_and_claim = AsyncMock(return_value=None)

        await mgr.get_or_create(self.KEY, agent="kirocrew")

        mgr._drain_and_claim.assert_awaited_once()
        provider.arm_explicit_effort_default.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_reset_keeps_the_intent_for_the_next_cold_start(self):
        # The handler records the intent and then resets the session; the reset
        # recycles the process and keeps the map entry the intent lives on.
        mgr, _factory, _provider = self._cold_manager(applied=True)
        await mgr.get_or_create(self.KEY, agent="kirocrew")
        mgr.set_explicit_effort_default(self.KEY, True)

        await mgr.reset(self.KEY)

        assert mgr.explicit_effort_default_pending(self.KEY) is True
