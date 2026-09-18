"""Tests for warm session pool (session.pool_size / session.pool_agent).

This module pins how the pool fills and which session may claim a warm provider: the
fill and its liveness drain, the agent, TTL and model matches a claim needs, the config
that sizes the pool, and the sessions (resumed, stateless, side read-only, or on a
disabled pool) that never claim. How a provider leaves the pool and its process dies
lives in ``test_session_pool_reaping.py``; how the pool refills after a factory reload
or a defaults refresh, in which workspace, and around the fill lock in
``test_session_pool_refill.py``; the shared fixture and stand-ins in
``session_pool_helpers.py``.
"""

from __future__ import annotations

import asyncio
import os
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from session_pool_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _isolate_config_dir,
    _make_cfg,
    _make_manager,
    _make_provider,
)

from kiro_crew.acp.session_handle import WatchdogSettings
from kiro_crew.config.paths import CWD_CLEARED, resolved_cwd
from kiro_crew.start_priority import StartPriority
from kiro_crew.testing.wait import await_until

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

        # The cancellation handler can neither await nor block the loop, so it
        # dispatches the hard kill fire-and-forget. Production sends it to
        # `subprocess_executor()`, a process-wide 8-worker singleton shared by
        # every test in this xdist worker, and a started run_in_executor future
        # cannot be cancelled -- so sibling tests holding those threads (a real
        # `taskkill` or descendant scan on Windows, a wedged PTY close) make
        # this kill queue behind them for as long as they run. A private
        # executor owns the capacity this assertion depends on; draining it with
        # shutdown(wait=True) also guarantees the worker thread has returned and
        # call_args is recorded before the assertion reads it. unittest.mock
        # increments call_count before writing call_args, so waiting on
        # call_count alone is a data race on any scheduler.
        with (
            ThreadPoolExecutor(max_workers=1) as private_executor,
            patch("kiro_crew.session._sync_kill_provider") as mock_kill,
            patch("kiro_crew.session.subprocess_executor", return_value=private_executor),
        ):
            with pytest.raises(asyncio.CancelledError):
                await mgr._fill_warm_pool()
            await asyncio.to_thread(private_executor.shutdown, wait=True)
            mock_kill.assert_called_once_with(provider)
        assert mgr._warm_pool.qsize() == 0


# ---------------------------------------------------------------------------
# Liveness drain loop
# ---------------------------------------------------------------------------


class TestLivenessDrainLoop:
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

    def test_a_blank_pool_agent_is_the_default_template_not_a_same_named_alias(self):
        """A plain session runs ``default_template``; when a crewmate alias spells
        the same name but binds another template, the pool must prewarm what the
        session runs, or every claim misses."""
        from kiro_crew.config.loader import KiroCrewAgentConfig
        from kiro_crew.session import SessionManager

        cfg = _make_cfg(pool_size=1, pool_agent="")
        cfg.agent.default_agent = "atlas"
        cfg.agents["atlas"] = KiroCrewAgentConfig(
            kiro_agent="other-template", workspace="default", memory_store="default"
        )
        mgr = SessionManager(cfg)
        assert mgr._pool_agent == "atlas"

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
    async def test_explicit_clear_skips_pool_while_no_preference_still_claims(self):
        """An explicitly CLEARED project cold-starts; ``cwd=None`` still reaches the pool."""
        mgr, factory = _make_manager(pool_agent="kirocrew")
        mgr._drain_and_claim = AsyncMock(return_value=None)

        await mgr.get_or_create("cleared-key", agent="kirocrew", cwd=CWD_CLEARED)
        mgr._drain_and_claim.assert_not_awaited()

        await mgr.get_or_create("no-pref-key", agent="kirocrew")
        mgr._drain_and_claim.assert_awaited()

    @pytest.mark.asyncio
    async def test_an_explicit_clear_refuses_the_stored_resume_sid(self):
        """A cleared project must COLD-START, not ``session/load`` the conversation it dropped."""
        mgr, factory = _make_manager(pool_agent="kirocrew")
        mgr._drain_and_claim = AsyncMock(return_value=None)

        with patch.object(mgr._session_map, "get", return_value="sid-abc"):
            with patch.object(mgr._session_map, "clear_sid") as cleared:
                await mgr.get_or_create("cleared-key", agent="kirocrew", cwd=CWD_CLEARED)
            cleared.assert_called_once_with("cleared-key")
            assert factory.call_args.kwargs.get("cwd") == CWD_CLEARED

            with patch.object(mgr._session_map, "clear_sid") as kept:
                await mgr.get_or_create("no-pref-key", agent="kirocrew")
            kept.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_cleared_claim_resumes_the_sid_stored_in_the_default_workspace(self):
        """Only a SID stored for ANOTHER directory is dropped; the replacement is resumed."""
        mgr, factory = _make_manager(pool_agent="kirocrew")
        mgr._drain_and_claim = AsyncMock(return_value=None)
        default = resolved_cwd(CWD_CLEARED, mgr._fold_key("cleared-key"))

        with patch.object(mgr._session_map, "get", return_value="sid-new"):
            with patch.object(mgr._session_map, "get_cwd", return_value=default):
                with patch.object(mgr._session_map, "clear_sid") as kept:
                    await mgr.get_or_create("cleared-key", agent="kirocrew", cwd=CWD_CLEARED)
            kept.assert_not_called()

            with patch.object(mgr._session_map, "get_cwd", return_value="/Users/alice/old"):
                with patch.object(mgr._session_map, "clear_sid") as dropped:
                    await mgr.get_or_create("cleared-key-2", agent="kirocrew", cwd=CWD_CLEARED)
            dropped.assert_called_once_with("cleared-key-2")

    @pytest.mark.asyncio
    async def test_a_fresh_start_bound_where_the_arm_points_spends_it(self):
        """A start bound where the arm points spends it; one stating its own directory replaces it."""
        mgr, factory = _make_manager(pool_agent="kirocrew")
        arms = mgr._allocation_boundary()
        folded = mgr._fold_key("armed-key")

        def _bound(cwd):
            def _factory(*_a, **_kw):
                provider = _make_provider()
                provider.cwd = cwd
                return provider

            return _factory

        arms.mark_retire_on_next_claim("armed-key", "/Users/alice/proj")
        factory.side_effect = _bound("/Users/alice/other")
        await mgr.get_or_create("armed-key", agent="kirocrew", cwd="/Users/alice/other")
        assert folded not in arms.state.retire_arms
        await mgr.reset("armed-key")

        arms.mark_retire_on_next_claim("armed-key", "/Users/alice/proj")
        factory.side_effect = _bound("/Users/alice/proj")
        await mgr.get_or_create("armed-key", agent="kirocrew")
        assert folded not in arms.state.retire_arms

    @pytest.mark.asyncio
    async def test_a_cleared_member_claim_resumes_the_sid_stored_in_its_own_workspace(self):
        """An enrolled member binds its own workspace on a clear, so that SID is the replacement."""
        mgr, factory = _make_manager(pool_agent="kirocrew")
        mgr._drain_and_claim = AsyncMock(return_value=None)
        member_dir = "/Users/alice/member-ws"

        with (
            patch("kiro_crew.session_capabilities.cleared_claim_dir", return_value=member_dir),
            patch.object(mgr._session_map, "get", return_value="sid-member"),
            patch.object(mgr._session_map, "get_cwd", return_value=member_dir),
            patch.object(mgr._session_map, "clear_sid") as kept,
        ):
            await mgr.get_or_create("member-key", agent="kirocrew", cwd=CWD_CLEARED)
        kept.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_member_cleared_claim_reuses_its_own_workspace_binding(self):
        """A member's cleared claim binds the member workspace, not the per-session default."""
        mgr, factory = _make_manager(pool_agent="kirocrew")

        def _member(*_a, **_kw):
            provider = _make_provider()
            provider.cwd = "/members/ada/workspace"
            return provider

        factory.side_effect = _member
        default = await mgr.resolve_arm_cwd("member-key", CWD_CLEARED)
        assert default != "/members/ada/workspace", "the binding must differ, or this is vacuous"
        mgr.mark_retire_on_next_claim("member-key", default)

        _p, is_new, _ = await mgr.get_or_create("member-key", agent="kirocrew", cwd=CWD_CLEARED)
        assert is_new
        mgr.release("member-key")
        _p, is_new, _ = await mgr.get_or_create("member-key", agent="kirocrew", cwd=CWD_CLEARED)
        assert not is_new, "the member's own cleared claim evicted its session"
        assert factory.call_count == 1

    @staticmethod
    def _bound_where_asked(*_a, **kw):
        provider = _make_provider()
        provider.cwd = kw.get("cwd") or ""
        return provider

    @pytest.mark.asyncio
    async def test_a_claim_stating_no_cwd_binds_the_armed_directory_and_then_reuses(self, tmp_path):
        mgr, factory = _make_manager(pool_agent="kirocrew")
        mgr._drain_and_claim = AsyncMock(return_value=None)
        factory.side_effect = self._bound_where_asked
        armed = tmp_path / "armed"
        armed.mkdir()
        mgr.mark_retire_on_next_claim("armed-key", str(armed))
        folded = mgr._fold_key("armed-key")
        arms = mgr._allocation_boundary().state.retire_arms

        _p, is_new, _ = await mgr.get_or_create("armed-key", agent="kirocrew")
        assert is_new
        mgr._drain_and_claim.assert_not_awaited()
        assert factory.call_args.kwargs.get("cwd") == resolved_cwd(str(armed))
        assert folded not in arms
        mgr.release("armed-key")

        _p, is_new, _ = await mgr.get_or_create("armed-key", agent="kirocrew")
        assert not is_new, "a later claim stating no cwd cold-started again"
        assert factory.call_count == 1

    @pytest.mark.asyncio
    async def test_a_cwd_less_claim_of_an_armed_key_ignores_the_stored_cwd(self, tmp_path):
        mgr, factory = _make_manager(pool_agent="kirocrew")
        old, armed = tmp_path / "old", tmp_path / "armed"
        old.mkdir()
        armed.mkdir()
        mgr.mark_retire_on_next_claim("armed-key", str(armed))
        with (
            patch.object(mgr._session_map, "get", return_value="sid-old"),
            patch.object(mgr._session_map, "get_cwd", return_value=str(old)),
        ):
            await mgr.get_or_create("armed-key", agent="kirocrew")
        assert factory.call_args.kwargs.get("cwd") == resolved_cwd(str(armed))

    @pytest.mark.asyncio
    async def test_a_cwd_less_claim_of_a_cleared_arm_is_a_clear(self, tmp_path):
        """An arm naming the default workspace makes a cwd-less claim drop the old SID too."""
        mgr, factory = _make_manager(pool_agent="kirocrew")
        old = tmp_path / "old"
        old.mkdir()
        default = resolved_cwd(CWD_CLEARED, mgr._fold_key("cleared-key"))
        mgr.mark_retire_on_next_claim("cleared-key", default)
        with (
            patch.object(mgr._session_map, "get", return_value="sid-old"),
            patch.object(mgr._session_map, "get_cwd", return_value=str(old)),
            patch.object(mgr._session_map, "clear_sid") as dropped,
        ):
            await mgr.get_or_create("cleared-key", agent="kirocrew")
        dropped.assert_called_once_with("cleared-key")
        assert factory.call_args.kwargs.get("cwd") == CWD_CLEARED

    @pytest.mark.asyncio
    async def test_a_clear_outlives_a_restart_for_a_cwd_less_claim(self, tmp_path):
        mgr, factory = _make_manager(pool_agent="kirocrew")
        mgr._drain_and_claim = AsyncMock(return_value=None)
        old = tmp_path / "old"
        old.mkdir()
        smap, key = mgr._session_map, mgr._fold_key("restart-key")
        smap.set(key, "sid-old", cwd=str(old))
        default = await mgr.resolve_arm_cwd(key, CWD_CLEARED)
        mgr.mark_retire_on_next_claim(key, default)
        await mgr.drop_resume_outside(key, default)
        mgr._allocation_boundary().discard_all_retire_arms()

        with patch.object(smap, "get", side_effect=lambda k: smap._data[k]["sid"] or None):
            await mgr.get_or_create(key, agent="kirocrew")
        assert factory.call_args.kwargs.get("cwd") != str(old)
        assert smap._data[key]["sid"] == ""

    @pytest.mark.asyncio
    async def test_the_stale_session_retry_of_a_cwd_less_claim_honours_the_arm(self, tmp_path):
        mgr, factory = _make_manager(pool_agent="kirocrew")
        mgr._drain_and_claim = AsyncMock(return_value=None)
        factory.side_effect = self._bound_where_asked
        old, armed = tmp_path / "old", tmp_path / "armed"
        old.mkdir()
        armed.mkdir()
        await mgr.get_or_create("armed-key", agent="kirocrew", cwd=str(old))
        mgr.release("armed-key")
        mgr.mark_retire_on_next_claim("armed-key", str(armed))

        _p, is_new, _ = await mgr.get_or_create("armed-key", agent="kirocrew")
        assert is_new
        assert factory.call_count == 2
        assert factory.call_args.kwargs.get("cwd") == resolved_cwd(str(armed))
        assert mgr._fold_key("armed-key") not in mgr._allocation_boundary().state.retire_arms

    @pytest.mark.asyncio
    async def test_a_queued_cwd_less_claim_follows_a_clear_that_lands_while_it_waits(
        self, tmp_path
    ):
        mgr, factory = _make_manager(pool_agent="kirocrew")
        mgr._drain_and_claim = AsyncMock(return_value=None)
        factory.side_effect = self._bound_where_asked
        old, b = tmp_path / "old", tmp_path / "b"
        old.mkdir()
        b.mkdir()
        key = mgr._fold_key("queued-key")
        await mgr.get_or_create(key, agent="kirocrew", cwd=str(old))
        mgr.mark_retire_on_next_claim(key, str(b))
        semaphore = mgr._allocation_boundary().state.sessions[key].semaphore
        parked, acquire = asyncio.Event(), semaphore.acquire

        async def park_then_acquire():
            parked.set()
            return await acquire()

        semaphore.acquire = park_then_acquire
        queued = asyncio.create_task(mgr.get_or_create(key, agent="kirocrew"))
        await await_until(parked, what="the queued claim parks on the session")
        assert semaphore.locked() and not queued.done()
        mgr.mark_retire_on_next_claim(key, await mgr.resolve_arm_cwd(key, CWD_CLEARED))
        mgr.release(key)
        await await_until(queued, what="the queued claim allocates")
        assert factory.call_args.kwargs.get("cwd") is CWD_CLEARED

    @pytest.mark.asyncio
    async def test_a_cwd_less_start_refuses_an_arm_that_moved_while_it_started(self, tmp_path):
        mgr, factory = _make_manager(pool_agent="kirocrew")
        mgr._drain_and_claim = AsyncMock(return_value=None)
        armed = tmp_path / "armed"
        armed.mkdir()
        key = mgr._fold_key("moved-key")
        mgr.mark_retire_on_next_claim(key, str(armed))
        default = await mgr.resolve_arm_cwd(key, CWD_CLEARED)
        first = _make_provider()

        def clear_during_the_first_start(*_a, **kw):
            if factory.call_count == 1:
                mgr.mark_retire_on_next_claim(key, default)
                first.cwd = kw.get("cwd")
                return first
            return self._bound_where_asked(**kw)

        factory.side_effect = clear_during_the_first_start
        provider, is_new, _ = await mgr.get_or_create(key, agent="kirocrew")
        assert is_new and provider is not first
        first.shutdown.assert_awaited()
        assert factory.call_args.kwargs.get("cwd") is CWD_CLEARED

    @pytest.mark.asyncio
    async def test_a_claim_stating_its_own_directory_replaces_an_older_arm(self, tmp_path):
        mgr, factory = _make_manager(pool_agent="kirocrew")
        mgr._drain_and_claim = AsyncMock(return_value=None)
        factory.side_effect = self._bound_where_asked
        stale, new = tmp_path / "stale", tmp_path / "new"
        stale.mkdir()
        new.mkdir()
        mgr.mark_retire_on_next_claim("switch-key", str(stale))

        await mgr.get_or_create("switch-key", agent="kirocrew", cwd=str(new))
        assert factory.call_args.kwargs.get("cwd") == str(new)
        mgr.release("switch-key")
        _p, is_new, _ = await mgr.get_or_create("switch-key", agent="kirocrew", cwd=str(new))
        assert not is_new, "a stale arm evicted the session the slot's own claim bound"

    @pytest.mark.asyncio
    async def test_an_empty_string_claim_restores_like_no_claim(self, tmp_path):
        mgr, factory = _make_manager(pool_agent="kirocrew")
        old = tmp_path / "old"
        old.mkdir()
        with (
            patch.object(mgr._session_map, "get", return_value="sid-old"),
            patch.object(mgr._session_map, "get_cwd", return_value=str(old)),
            patch.object(mgr._session_map, "clear_sid") as dropped,
        ):
            await mgr.get_or_create("empty-key", agent="kirocrew", cwd="")
        dropped.assert_not_called()
        assert factory.call_args.kwargs.get("cwd") == str(old)

    @pytest.mark.asyncio
    async def test_a_clear_with_no_sid_outlives_a_restart_and_skips_the_pool(self, tmp_path):
        mgr, factory = _make_manager(pool_agent="kirocrew")
        mgr._drain_and_claim = AsyncMock(return_value=None)
        key = mgr._fold_key("pool-key")
        default = await mgr.resolve_arm_cwd(key, CWD_CLEARED)
        mgr.mark_retire_on_next_claim(key, default)
        await mgr.drop_resume_outside(key, default)
        mgr._allocation_boundary().discard_all_retire_arms()
        mgr._session_map.prune()

        await mgr.get_or_create(key, agent="kirocrew")
        mgr._drain_and_claim.assert_not_awaited()
        assert factory.call_args.kwargs.get("cwd") is CWD_CLEARED
        mgr.mark_retire_on_next_claim(key, str(tmp_path))
        assert not mgr._session_map.get_flag(key, "project_cleared")

    @pytest.mark.asyncio
    async def test_a_never_cleared_key_in_its_default_workspace_keeps_the_pool(self, tmp_path):
        mgr, factory = _make_manager(pool_agent="kirocrew")
        mgr._drain_and_claim = AsyncMock(return_value=None)
        key = mgr._fold_key("slack:never-cleared")
        default = await mgr.resolve_arm_cwd(key, CWD_CLEARED)
        mgr._session_map.set(key, "", cwd=default)

        await mgr.get_or_create(key, agent="kirocrew")
        mgr._drain_and_claim.assert_awaited()

    @pytest.mark.asyncio
    async def test_a_retiring_claude_session_does_not_rewrite_a_recorded_clear(self, tmp_path):
        import kiro_crew.session as session_module
        from kiro_crew.session_allocation_parts.errors import SessionBusyError

        mgr, factory = _make_manager(pool_agent="kirocrew")
        old = tmp_path / "old"
        old.mkdir()

        def claude(*_a, **_kw):
            provider = _make_provider()
            provider.session_id, provider.cwd = "sid-old", str(old)
            return provider

        factory.side_effect = claude
        key = mgr._fold_key("claude-key")
        with patch.object(session_module, "_is_claude_provider", lambda _p: True):
            await mgr.get_or_create(key, agent="kirocrew", cwd=str(old))
            default = await mgr.resolve_arm_cwd(key, CWD_CLEARED)
            mgr.mark_retire_on_next_claim(key, default)
            await mgr.drop_resume_outside(key, default)
            with pytest.raises(SessionBusyError):
                await mgr.get_or_create(key, agent="kirocrew", wait_if_busy=False)
        assert mgr._session_map.get_cwd(key) == default
        assert mgr._session_map.get_flag(key, "project_cleared")

    @pytest.mark.asyncio
    async def test_a_claim_for_another_project_does_not_reuse_a_session_bound_to_the_first(
        self, tmp_path
    ):
        mgr, factory = _make_manager(pool_agent="kirocrew")
        factory.side_effect = self._bound_where_asked
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        await mgr.get_or_create("ab-key", agent="kirocrew", cwd=str(a))
        mgr.release("ab-key")
        _p, is_new, _ = await mgr.get_or_create("ab-key", agent="kirocrew", cwd=str(b))
        assert is_new, "a project-B claim was served the session bound to project A"
        assert factory.call_args.kwargs.get("cwd") == str(b)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("caller", ["cron", "subagent", "home-relative"])
    async def test_a_callers_repeated_claim_reuses_its_session(self, tmp_path, monkeypatch, caller):
        monkeypatch.setenv("HOME", str(tmp_path))
        real = tmp_path / "real"
        real.mkdir()
        (tmp_path / "link").symlink_to(real)
        claim = {
            "cron": str(tmp_path / "link"),
            "subagent": f"{tmp_path}/link/",
            "home-relative": "~/link",
        }[caller]
        mgr, factory = _make_manager(pool_agent="kirocrew")

        def reports_the_realpath(*_a, **kw):
            provider = _make_provider()
            provider.cwd = os.path.realpath(os.path.expanduser(kw["cwd"]))
            return provider

        factory.side_effect = reports_the_realpath
        for _turn in range(2):
            await mgr.get_or_create(f"{caller}:job", agent="kirocrew", cwd=claim)
            mgr.release(f"{caller}:job")
        assert factory.call_count == 1, "a repeated claim evicted its own session"

    @pytest.mark.asyncio
    async def test_skips_pool_for_a_side_read_only_session(self):
        """A pooled child was started without the Side Chat read-only shape. A
        side key is stateless, so the session cold-starts and the factory
        receives the flag."""
        mgr, factory = _make_manager(pool_agent="kirocrew")
        pooled = _make_provider()
        mgr._warm_pool.put_nowait((pooled, time.monotonic()))
        mgr._drain_and_claim = AsyncMock(return_value=pooled)

        await mgr.get_or_create("side:parent:1", agent="kirocrew", side_read_only=True)

        mgr._drain_and_claim.assert_not_awaited()
        factory.assert_called_once()
        assert factory.call_args.kwargs.get("side_read_only") is True

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
    async def test_empty_pool_agent_is_the_default_template(self):
        """No pool_agent configured → the pool is the default TEMPLATE.

        A plain session asks the pool for the default template, so that is
        what the pool prewarms and what the post-claim model check reads.
        """
        from kiro_crew.providers.acp import AcpProvider

        mgr, factory = _make_manager(pool_agent="")
        assert mgr._pool_agent == "kirocrew"
        pooled = _make_provider()
        pooled.__class__ = AcpProvider
        pooled.client = MagicMock()
        pooled.client.set_model = AsyncMock()
        pooled.client.resumed = False
        pooled.client._session_id = "fake-sid"
        mgr._drain_and_claim = AsyncMock(return_value=pooled)
        mgr._schedule_replenish = MagicMock()

        with patch.object(type(mgr), "_resolve_agent_model", return_value="") as mock_resolve:
            await mgr.get_or_create("test-key", agent=None, model="claude-opus-4.6")

        mock_resolve.assert_called_once_with("kirocrew")
        # The template pins no model → pool_model is "" → skip set_model (the
        # pool process already has whatever model kiro-cli defaults to).
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

        def _persistent_provider(*_a, **_kw):
            # A real provider reports its privacy mode; the turn after creation
            # checks it against the session's.
            p = _make_provider()
            p.memory_mode = "persistent"
            return p

        factory.side_effect = _persistent_provider
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


class TestTheReuseComparisonSurvivesASymlinkedRoot:
    """`_reacquire_and_validate` compares the claim against `provider.cwd` on EVERY reuse."""

    @pytest.mark.asyncio
    async def test_restating_the_same_symlinked_directory_does_not_evict(self, tmp_path):
        from kiro_crew.session_allocation_parts.get_or_create import cwd_moved_for_reuse

        real = tmp_path / "real-root"
        real.mkdir()
        link = tmp_path / "linked-root"
        link.symlink_to(real, target_is_directory=True)
        assert link.is_symlink(), "the fixture is not a symlink, so this proves nothing"

        bound = str(link)
        default = str(tmp_path / "per-session-default")

        for spelling in (str(link), str(link) + "/", str(link) + "//"):
            assert cwd_moved_for_reuse(bound, spelling, default) is False, spelling

        for reported in (str(link) + "/", str(link) + "//", str(link) + "/."):
            assert cwd_moved_for_reuse(reported, str(link), default) is False, reported

        assert cwd_moved_for_reuse(bound, None, default) is False

        assert cwd_moved_for_reuse(bound, str(tmp_path / "elsewhere"), default) is True
        assert cwd_moved_for_reuse(bound, str(real), default) is True
        assert cwd_moved_for_reuse("", str(link), default) is False
        assert cwd_moved_for_reuse(None, str(link), default) is False

    @pytest.mark.asyncio
    async def test_a_backend_reporting_the_realpath_of_a_symlinked_claim_is_reused(self, tmp_path):
        real = tmp_path / "real-root"
        real.mkdir()
        link = tmp_path / "linked-root"
        link.symlink_to(real, target_is_directory=True)
        mgr, factory = _make_manager(pool_agent="kirocrew")
        mgr._drain_and_claim = AsyncMock(return_value=None)

        def _bound_at_realpath(*_a, **kw):
            provider = _make_provider()
            provider.cwd = str(real.resolve())
            return provider

        factory.side_effect = _bound_at_realpath
        await mgr.get_or_create("link-key", agent="kirocrew", cwd=str(link))
        mgr.release("link-key")
        # Restated in another spelling, so the start's own claimed_cwd does not answer it.
        _p, is_new, _ = await mgr.get_or_create("link-key", agent="kirocrew", cwd=f"{link}/")
        assert not is_new, "a realpath-reporting backend was evicted on reuse"
        assert factory.call_count == 1
