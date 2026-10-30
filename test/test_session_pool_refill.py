"""How the warm pool refills when its provider factory, defaults or workspace change.

``reload_provider_factory`` resets the pool and refills it with the new provider
type; ``refresh_defaults`` adopts new defaults without clearing or shutting down
a live session, draining and restarting only the pool. Pool providers start in
the workspace ``default_project_dir`` resolves, and a session cwd other than
the pool's bypasses the pool. The fill lock is held only around the queue
mutations, so a refresh interleaves between starts, while a second refill is a
no-op as long as one is live; a provider started under a replaced factory or
before a new identity epoch is discarded, never enqueued. The shared fixture and
stand-ins live in ``session_pool_helpers.py``.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from session_pool_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _isolate_config_dir,
    _make_cfg,
    _make_manager,
    _make_provider,
)

from kiro_crew.start_priority import PrioritySemaphore
from kiro_crew.testing.wait import await_until, finished

# ---------------------------------------------------------------------------
# reload_provider_factory resets pool
# ---------------------------------------------------------------------------


class TestReloadProviderFactoryRefillsPool:
    @pytest.mark.asyncio
    async def test_reload_resets_pool_started_and_refills(self):
        """After reload_provider_factory, warm pool is replenished with new provider type."""
        mgr, factory = _make_manager(pool_size=1)
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
        assert mgr._pool_health_task is not fake_task, "a fresh health loop replaced it"
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
    @pytest.mark.parametrize(
        "project_dir, expected",
        [
            pytest.param(
                "/custom/workspace", "/custom/workspace", id="pool_cwd_set_from_default_project_dir"
            ),
            pytest.param("", "", id="pool_cwd_empty_when_no_workspace"),
        ],
    )
    def test_pool_cwd_follows_default_project_dir(self, project_dir, expected):
        from kiro_crew.session import SessionManager

        cfg = _make_cfg()
        with patch("kiro_crew.session.default_project_dir", return_value=project_dir):
            mgr = SessionManager(cfg)
        assert mgr._pool_cwd == expected

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
        assert provider is not pooled
        assert mgr._warm_pool.qsize() == 1

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


_MID_REFILL_LOST_RUN_CEILING_SECS = 30.0


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
            # flake-ok: pre-existing gated start stub, moved verbatim; the finally block sets the event and every outer await is bounded
            await release_start.wait()

        def _factory(*a, **kw):
            p = _make_provider()
            p.start = AsyncMock(side_effect=_slow_start)
            return p

        mgr._provider_factory = MagicMock(side_effect=_factory)

        # Lost-run ceiling, not a speed check. The starts never finish until
        # ``release_start`` is set in ``finally``, so a refresh that waits on
        # the whole refill blocks until this ceiling however long it is.
        # Measured: the call phase takes 0.03 s on Linux (``--durations``);
        # hosted macOS job 115414883668 ran past 2 s in the off-loop config
        # load and was cut off there, so its full length is unknown. 30 s is
        # 15x that cut-off and a quarter of ``--timeout=120``.
        bound = _MID_REFILL_LOST_RUN_CEILING_SECS

        fill = asyncio.create_task(mgr._fill_warm_pool())
        try:
            # The refill is now parked inside the first provider's start(), with
            # the fill lock released.
            await asyncio.wait_for(first_started.wait(), timeout=bound)

            with patch("kiro_crew.session.KiroCrewConfig.load") as mock_load:
                new_cfg = _make_cfg(pool_size=0)
                new_cfg.create_provider_factory = MagicMock(
                    return_value=MagicMock(side_effect=lambda *a, **kw: _make_provider())
                )
                mock_load.return_value = new_cfg
                # With the lock held across the whole refill this waits for all
                # three starts; released around the start it returns at once.
                await asyncio.wait_for(mgr.refresh_defaults(), timeout=bound)

            assert mgr._cfg is new_cfg
        finally:
            release_start.set()
            await asyncio.wait_for(fill, timeout=bound)

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
            # flake-ok: pre-existing gated start stub, moved verbatim; the finally block sets the event and every outer await is bounded
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
            await await_until(in_start, what="the refill enters start()")
            # The ``fill_active`` guard makes a second refill a no-op while the
            # first is live, so this returns at once without starting a
            # provider of its own. Delete the guard and this second fill
            # instead runs a real refill that blocks inside its own start() on
            # ``release_start`` -- the bounded wait then fails by name (the hang
            # the guard prevents), not as a passing no-op.
            await finished(mgr._fill_warm_pool(), "the second fill returns as a no-op")
            assert started == 1
        finally:
            release_start.set()
            await await_until(first, what="the first fill finishes")

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
            # flake-ok: pre-existing gated start stub, moved verbatim; the finally block sets the event and every outer await is bounded
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
            await await_until(in_start, what="the refill enters start()")
            # Swap the factory while the refill is parked inside start();
            # pool_size is left at 1 so there is still room for a provider -- the
            # stale one must be dropped purely because its factory is retired.
            new_factory = MagicMock(side_effect=_fresh_factory)
            mgr._provider_factory = new_factory
        finally:
            release_start.set()
            await await_until(fill, what="the refill finishes")

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
            # flake-ok: pre-existing gated start stub, moved verbatim; the finally block sets the event and every outer await is bounded
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
            await await_until(in_start, what="the refill enters start()")
            # The identity sweep only marks the epoch -- pool_size stays 1, so
            # the size check still has room for a provider. The ONLY reason the
            # pre-epoch provider is refused is the identity-epoch check; delete
            # that check and the provider would be enqueued and this test fail.
            mgr._pool.mark_identity_epoch()
        finally:
            release_start.set()
            await await_until(fill, what="the refill finishes")

        # Pool still has room (size 1) yet nothing is enqueued: the pre-epoch
        # provider was discarded, not seeded behind the drain.
        assert mgr._warm_pool.qsize() == 0
        # The epoch mismatch stops the fill, so the loop never re-spawns a
        # second provider against the retired identity.
        assert len(providers) == 1
        providers[0].shutdown.assert_awaited_once()


class TestMcpConfigFence:
    """An MCP write during the released-lock window of a fill is handed to the
    provider that fill holds. The dashboard's live-apply gate marks the providers
    it can see (registered sessions, the queued pool); a provider between its
    ``start()`` and its enqueue is in neither snapshot, so without this fence it
    joins the pool projected from the spec as it stood before the write."""

    @staticmethod
    def _gated_factory(providers: list, in_start: asyncio.Event, release: asyncio.Event):
        async def _slow_start() -> None:
            in_start.set()
            # flake-ok: gated start stub as the sibling tests use; the finally block sets the event and every outer await is bounded
            await release.wait()

        def _factory(*a, **kw):
            p = _make_provider()
            p.start = AsyncMock(side_effect=_slow_start)
            p.mark_mcp_config_changed = MagicMock()
            providers.append(p)
            return p

        return _factory

    @pytest.mark.asyncio
    async def test_a_write_during_the_start_marks_the_provider_at_enqueue(self):
        mgr, _ = _make_manager(pool_size=1)
        mgr._start_sem = PrioritySemaphore(1)
        in_start = asyncio.Event()
        release_start = asyncio.Event()
        providers: list = []
        mgr._provider_factory = MagicMock(
            side_effect=self._gated_factory(providers, in_start, release_start)
        )

        fill = asyncio.create_task(mgr._fill_warm_pool())
        try:
            await await_until(in_start, what="the refill enters start()")
            # The write lands while the provider is parked inside start(): the
            # gate's snapshots cannot include it, so the manager-level fence is
            # the only thing that can reach it.
            mgr.mark_mcp_config_changed()
        finally:
            release_start.set()
            await await_until(fill, what="the refill finishes")

        assert len(providers) == 1
        assert mgr._warm_pool.qsize() == 1
        providers[0].mark_mcp_config_changed.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_no_write_leaves_the_provider_unmarked(self):
        mgr, _ = _make_manager(pool_size=1)
        mgr._start_sem = PrioritySemaphore(1)
        in_start = asyncio.Event()
        release_start = asyncio.Event()
        providers: list = []
        mgr._provider_factory = MagicMock(
            side_effect=self._gated_factory(providers, in_start, release_start)
        )

        fill = asyncio.create_task(mgr._fill_warm_pool())
        try:
            await await_until(in_start, what="the refill enters start()")
        finally:
            release_start.set()
            await await_until(fill, what="the refill finishes")

        assert mgr._warm_pool.qsize() == 1
        providers[0].mark_mcp_config_changed.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_write_before_the_start_is_not_a_fence_crossing(self):
        """The provider built after the write already projects from the new
        spec; marking it would cost a needless close and reload at its first turn."""
        mgr, _ = _make_manager(pool_size=1)
        mgr._start_sem = PrioritySemaphore(1)
        in_start = asyncio.Event()
        release_start = asyncio.Event()
        providers: list = []
        mgr._provider_factory = MagicMock(
            side_effect=self._gated_factory(providers, in_start, release_start)
        )
        mgr.mark_mcp_config_changed()

        fill = asyncio.create_task(mgr._fill_warm_pool())
        try:
            await await_until(in_start, what="the refill enters start()")
        finally:
            release_start.set()
            await await_until(fill, what="the refill finishes")

        assert mgr._warm_pool.qsize() == 1
        providers[0].mark_mcp_config_changed.assert_not_called()
