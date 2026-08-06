"""Tests for warm session pool (session.pool_size / session.pool_agent)."""

from __future__ import annotations

import asyncio
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


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
    async def test_provider_without_is_alive_discarded(self):
        """Provider missing is_alive attribute is treated as dead."""
        mgr, _ = _make_manager(pool_agent="kirocrew")

        no_alive = _make_provider()
        del no_alive.is_process_alive
        healthy = _make_provider()

        mgr._warm_pool.put_nowait((no_alive, time.monotonic()))
        mgr._warm_pool.put_nowait((healthy, time.monotonic()))

        pooled = await mgr._drain_and_claim("kirocrew")

        no_alive.shutdown.assert_awaited_once()
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
        pooled.client.rekey.assert_called_once_with("test-key", "ch-1")
        mgr._schedule_replenish.assert_called_once()
        factory.assert_not_called()

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

        Design Review on #1596: the post-claim re-apply carries an INHERITED
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
        """Healthy provider survives health sweep."""
        mgr, _ = _make_manager(pool_agent="kirocrew")
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
    async def test_skips_when_pool_empty(self):
        """No crash when pool is empty during sweep."""
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
        with patch("kiro_crew.config.loader.workspace_dir_for", return_value=ws), patch(
            "kiro_crew.security.is_sensitive_path", return_value=True
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

        with patch("kiro_crew.session._sync_kill_provider") as mock_kill:
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
            provider._client = SimpleNamespace(_pid=proc.pid)
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
            # Mimic an ACP provider: the tracked PID lives at provider._client._pid
            provider._client = SimpleNamespace(_pid=proc.pid)
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
        with patch("kiro_crew.session.subprocess_executor", return_value=dead_executor), \
                patch("kiro_crew.session._sync_kill_provider", side_effect=_record_thread):
            SessionManager._dispatch_hard_kill(provider)

        assert done.wait(timeout=5), "fallback kill was never dispatched"
        assert called_on[0] is not threading.main_thread(), (
            "fallback kill ran inline on the event-loop thread"
        )

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

        assert first in attempted and second in attempted, (
            "a failing hard kill aborted the batch and leaked later providers"
        )
        assert mgr._warm_pool.qsize() == 0
