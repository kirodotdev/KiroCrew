"""SessionManager live config: the session/agent/watchdog fields follow config.json.

``SessionManager`` subscribes itself on the process config watcher. These tests
pin each applier: the manager adopts the reloaded config, a factory-bound
default goes through ``refresh_defaults`` (which now re-derives the warm-pool
shape without a second load), the cleanup loop re-reads its idle timeout and
RSS ceiling every tick, a ``watchdog.*`` change re-clamps live handles through
``_load_watchdog_settings``, the runtime's session-start budget and the
context builder's ``{bot_name}`` read the live snapshot, and a file write
dispatched through ``ConfigWatch`` reaches the manager end to end.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from _hot_reload_helpers import change as _change
from _hot_reload_helpers import write_config as _write

from kiro_crew.config import live
from kiro_crew.config.live import ConfigWatch
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.session import SessionManager, _watchdog_handle_of
from kiro_crew.session_cleanup import SessionCleanup


def _make_cfg(
    pool_size: int = 2,
    pool_agent: str = "kirocrew",
    pool_ttl_secs: int = 1800,
    timeout_secs: int = 3600,
    rss_max_mb: int = 0,
) -> MagicMock:
    cfg = MagicMock()
    cfg.session.pool_size = pool_size
    cfg.session.pool_agent = pool_agent
    cfg.session.pool_ttl_secs = pool_ttl_secs
    cfg.session.timeout_secs = timeout_secs
    cfg.session.watchdog_rss_max_mb = rss_max_mb
    cfg.agent.default_agent = ""
    cfg.agent.model = "auto"
    cfg.agent.reasoning_effort = ""
    # A clean document; a test that wants a torn one sets this to its sections.
    cfg.degraded_sections = frozenset()
    return cfg


def _make_provider() -> MagicMock:
    p = MagicMock()
    p.start = AsyncMock()
    p.shutdown = AsyncMock()
    p.is_process_alive = MagicMock(return_value=True)
    p.exit_code = None
    p.cwd = ""
    # A MagicMock vivifies any attribute, so pin the two the watchdog resolver
    # probes: a fake provider carries no live ACP handle to re-clamp.
    p._handle = None
    p._client = None
    return p


def _factory_for(cfg) -> MagicMock:
    """Stand in for ``build_provider_factory``.

    The real one composes the platform context, which loads config itself --
    noise for a test that pins whether the APPLIER re-reads the file.
    """
    return MagicMock(side_effect=lambda *a, **kw: _make_provider())


def _make_manager(**cfg_kwargs) -> tuple[SessionManager, MagicMock]:
    cfg = _make_cfg(**cfg_kwargs)
    factory = MagicMock(side_effect=lambda *a, **kw: _make_provider())
    with patch("kiro_crew.session.default_project_dir", return_value="/ws"):
        mgr = SessionManager(cfg, provider_factory=factory)
    return mgr, factory


class TestSubscription:
    def test_manager_registers_on_the_process_watcher(self) -> None:
        mgr, _ = _make_manager()
        subs = [s for s in live.watch().subscriptions() if s.name == "SessionManager"]
        assert len(subs) == 1
        assert subs[0].prefixes == (
            "session",
            "agent",
            "watchdog",
            "agents",
            "workspaces",
            "default_workspace",
        )
        assert mgr._config_sub is subs[0]

    def test_factory_paths_are_under_the_subscribed_prefixes(self) -> None:
        prefixes = ("agent.", "session.", "workspaces", "default_workspace")
        for path in SessionManager._FACTORY_CONFIG_PATHS:
            assert path.startswith(prefixes)

    def test_the_pool_cwd_sources_are_factory_paths(self) -> None:
        """``WarmPoolState.cwd`` is ``default_project_dir()``, resolved from
        ``default_workspace`` and ``workspaces``; a workspace edit that only
        swapped ``_cfg`` would leave cwd-less subagents in the old directory."""
        assert "workspaces" in SessionManager._FACTORY_CONFIG_PATHS
        assert "default_workspace" in SessionManager._FACTORY_CONFIG_PATHS


class TestAdoptOnChange:
    @pytest.mark.asyncio
    async def test_a_torn_watched_section_defers_instead_of_adopting_defaults(self) -> None:
        """A malformed ``agent`` section loads as defaults; adopting it would
        rebuild the factory on the default model and backend and drain the warm
        pool. The applier raises ``ConfigDeferred`` and keeps what is in force,
        like the owned appliers; the watcher retries each tick."""
        from kiro_crew.config.live import ConfigDeferred

        mgr, _ = _make_manager()
        mgr.refresh_defaults = AsyncMock()  # type: ignore[method-assign]
        before = mgr._cfg
        for degraded in ({"agent"}, {"session"}):
            torn = _make_cfg(timeout_secs=120)
            torn.degraded_sections = frozenset(degraded)
            with pytest.raises(ConfigDeferred) as info:
                await mgr._on_config_change(_change(torn, "agent.model"))
            assert info.value.paths == frozenset({"agent.model"})
        assert mgr._cfg is before
        mgr.refresh_defaults.assert_not_awaited()
        # A degraded section this manager does not watch does not gate it, and
        # neither does the whole-config flag on its own: the watcher never
        # dispatches a document that is torn NOW, so on a dispatched change that
        # flag is the loader's process-long memory of a since-repaired tear.
        for degraded in ({"slack"}, {"*", "*config.json"}):
            fine = _make_cfg(timeout_secs=120)
            fine.degraded_sections = frozenset(degraded)
            await mgr._on_config_change(_change(fine, "session.timeout_secs"))
            assert mgr._cfg is fine

    @pytest.mark.asyncio
    async def test_point_of_use_field_adopts_the_config_without_a_refresh(self) -> None:
        mgr, _ = _make_manager()
        mgr.refresh_defaults = AsyncMock()  # type: ignore[method-assign]
        new_cfg = _make_cfg(timeout_secs=120)
        await mgr._on_config_change(_change(new_cfg, "session.timeout_secs"))
        assert mgr._cfg is new_cfg
        mgr.refresh_defaults.assert_not_awaited()

    def test_the_warm_pool_agent_source_is_a_factory_path(self) -> None:
        """``WarmPoolState.agent`` is ``session.pool_agent or agent.default_agent``,
        captured once; with ``pool_agent`` empty, a live ``agent.default_agent``
        change must re-derive the pool, not just swap ``_cfg``."""
        assert "agent.default_agent" in SessionManager._FACTORY_CONFIG_PATHS
        assert "session.pool_agent" in SessionManager._FACTORY_CONFIG_PATHS

    @pytest.mark.asyncio
    async def test_factory_bound_default_goes_through_refresh_defaults(self) -> None:
        mgr, _ = _make_manager()
        mgr.refresh_defaults = AsyncMock()  # type: ignore[method-assign]
        new_cfg = _make_cfg()
        await mgr._on_config_change(_change(new_cfg, "agent.model"))
        mgr.refresh_defaults.assert_awaited_once_with(cfg=new_cfg)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "path",
        [
            "agent.reasoning_effort",
            "agent.acp_backend",
            "agent.role_efforts.background",
            "agent.tool_search",
            "agent.sandbox",
            "session.pool_size",
            "session.pool_agent",
            "session.pool_ttl_secs",
        ],
    )
    async def test_each_factory_path_triggers_a_refresh(self, path: str) -> None:
        mgr, _ = _make_manager()
        mgr.refresh_defaults = AsyncMock()  # type: ignore[method-assign]
        await mgr._on_config_change(_change(_make_cfg(), path))
        mgr.refresh_defaults.assert_awaited_once()


class TestRefreshDefaultsRederivesThePool:
    @pytest.mark.asyncio
    async def test_pool_shape_follows_the_handed_in_config_without_a_load(self) -> None:
        mgr, old_factory = _make_manager(pool_size=2, pool_agent="kirocrew", pool_ttl_secs=1800)
        new_cfg = _make_cfg(pool_size=3, pool_agent="reviewer", pool_ttl_secs=60)
        with (
            patch("kiro_crew.session.KiroCrewConfig.load") as load,
            patch("kiro_crew.session.default_project_dir", return_value="/new-ws"),
            patch("kiro_crew.session.build_provider_factory", side_effect=_factory_for) as build,
            patch.object(mgr, "start_pool", AsyncMock()),
            patch.object(mgr, "_retire_stale_backend_bg_runtime", AsyncMock()),
        ):
            await mgr.refresh_defaults(cfg=new_cfg)
        load.assert_not_called()
        # The factory is rebuilt from the handed-in config, not from a re-read.
        build.assert_called_once_with(new_cfg)
        assert mgr._cfg is new_cfg
        assert mgr._provider_factory is not old_factory
        assert mgr._pool_size == 3
        assert mgr._pool_agent == "reviewer"
        assert mgr._pool_ttl_secs == 60
        assert mgr._pool_cwd == "/new-ws"

    @pytest.mark.asyncio
    async def test_reload_provider_factory_adopts_the_same_pool_fields(self) -> None:
        """The reset handler's path re-adopts the pool shape too, TTL included: a
        disk-edited ``pool_ttl_secs`` it loads must not keep evicting the warm pool
        at the old TTL until the watcher's next ``refresh_defaults``."""
        mgr, old_factory = _make_manager(pool_size=2, pool_agent="kirocrew", pool_ttl_secs=1800)
        new_cfg = _make_cfg(pool_size=3, pool_agent="reviewer", pool_ttl_secs=60)
        with (
            patch("kiro_crew.session.KiroCrewConfig.load") as load,
            patch("kiro_crew.session.default_project_dir", return_value="/new-ws"),
            patch("kiro_crew.session.build_provider_factory", side_effect=_factory_for),
            patch.object(mgr, "start_pool", AsyncMock()),
            patch.object(mgr, "_retire_stale_backend_bg_runtime", AsyncMock()),
        ):
            await mgr.reload_provider_factory(cfg=new_cfg)
        load.assert_not_called()
        assert mgr._provider_factory is not old_factory
        assert (mgr._pool_size, mgr._pool_agent, mgr._pool_ttl_secs, mgr._pool_cwd) == (
            3,
            "reviewer",
            60,
            "/new-ws",
        )

    @pytest.mark.asyncio
    async def test_a_workspace_edit_rederives_the_pool_cwd(self) -> None:
        mgr, _ = _make_manager()
        assert mgr._pool_cwd == "/ws"
        with (
            patch("kiro_crew.session.default_project_dir", return_value="/moved"),
            patch("kiro_crew.session.build_provider_factory", side_effect=_factory_for),
            patch.object(mgr, "start_pool", AsyncMock()),
            patch.object(mgr, "_retire_stale_backend_bg_runtime", AsyncMock()),
        ):
            await mgr._on_config_change(_change(_make_cfg(), "default_workspace"))
        assert mgr._pool_cwd == "/moved"

    @pytest.mark.asyncio
    async def test_pool_cwd_is_resolved_off_the_event_loop(self) -> None:
        """``default_project_dir()`` reads the config file and stats the
        workspace, so the refresh resolves it in a worker thread and never while
        holding ``_lock``."""
        import threading

        loop_thread = threading.get_ident()
        seen: list[tuple[int, bool]] = []
        mgr, _ = _make_manager()

        def resolve() -> str:
            seen.append((threading.get_ident(), mgr._lock.locked()))
            return "/threaded"

        with (
            patch("kiro_crew.session.default_project_dir", side_effect=resolve),
            patch("kiro_crew.session.build_provider_factory", side_effect=_factory_for),
            patch.object(mgr, "start_pool", AsyncMock()),
            patch.object(mgr, "_retire_stale_backend_bg_runtime", AsyncMock()),
        ):
            await mgr.refresh_defaults(cfg=_make_cfg())
        assert seen and all(tid != loop_thread and not locked for tid, locked in seen)
        assert mgr._pool_cwd == "/threaded"

    @pytest.mark.asyncio
    async def test_pool_size_is_clamped_and_ttl_floored_like_the_constructor(self) -> None:
        mgr, _ = _make_manager(pool_size=1)
        new_cfg = _make_cfg(pool_size=10_000, pool_agent="", pool_ttl_secs=-5)
        new_cfg.agent.default_agent = "fallback"
        with (
            patch("kiro_crew.session.build_provider_factory", side_effect=_factory_for),
            patch.object(mgr, "start_pool", AsyncMock()),
            patch.object(mgr, "_retire_stale_backend_bg_runtime", AsyncMock()),
        ):
            await mgr.refresh_defaults(cfg=new_cfg)
        constants = mgr._lifecycle_boundary()._deps.constants()
        assert mgr._pool_size == constants.max_pool
        assert mgr._pool_agent == "fallback"
        assert mgr._pool_ttl_secs == 0

    @pytest.mark.asyncio
    async def test_no_cfg_still_loads_off_loop(self) -> None:
        mgr, _ = _make_manager(pool_size=0)
        new_cfg = _make_cfg(pool_size=0, pool_ttl_secs=7)
        with (
            patch("kiro_crew.session.KiroCrewConfig.load", return_value=new_cfg) as load,
            # The pool cwd resolves through the workspace table, which reads config
            # on its own; pinning it keeps the count below the lifecycle's one read.
            patch("kiro_crew.session.default_project_dir", return_value="/ws"),
            patch("kiro_crew.session.build_provider_factory", side_effect=_factory_for),
            patch.object(mgr, "start_pool", AsyncMock()),
            patch.object(mgr, "_retire_stale_backend_bg_runtime", AsyncMock()),
        ):
            await mgr.refresh_defaults()
        load.assert_called_once()
        assert mgr._pool_ttl_secs == 7

    @pytest.mark.asyncio
    async def test_a_raising_factory_builder_leaves_cfg_and_factory_untouched(self) -> None:
        """``build_provider_factory`` raising must not leave ``_cfg`` pointing at
        the new config while ``_provider_factory`` still reflects the old one --
        a raising builder must leave the previous (cfg, factory) pair intact so
        a watcher retry re-enters against a consistent state, not a half-swapped
        one."""
        mgr, old_factory = _make_manager(pool_size=2, pool_agent="kirocrew", pool_ttl_secs=1800)
        old_cfg = mgr._cfg
        new_cfg = _make_cfg(pool_size=3, pool_agent="reviewer", pool_ttl_secs=60)

        def boom(cfg):
            raise RuntimeError("factory build failed")

        with (
            patch("kiro_crew.session.default_project_dir", return_value="/new-ws"),
            patch("kiro_crew.session.build_provider_factory", side_effect=boom),
            patch.object(mgr, "start_pool", AsyncMock()),
            patch.object(mgr, "_retire_stale_backend_bg_runtime", AsyncMock()),
        ):
            with pytest.raises(RuntimeError, match="factory build failed"):
                await mgr.refresh_defaults(cfg=new_cfg)

        assert mgr._cfg is old_cfg
        assert mgr._provider_factory is old_factory
        assert mgr._pool_size == 2
        assert mgr._pool_agent == "kirocrew"
        assert mgr._pool_ttl_secs == 1800


class TestCleanupLoopRereadsPolicy:
    def test_adopt_reads_timeout_and_rss_from_the_current_config(self) -> None:
        mgr, _ = _make_manager(timeout_secs=3600, rss_max_mb=0)
        cleanup = mgr._cleanup_boundary()
        assert cleanup._adopt_idle_policy() == 600.0
        assert cleanup.state.idle_sweep_enabled is True
        assert cleanup.state.idle_timeout == 3600
        assert cleanup.state.rss_max_mb == 0

        mgr._cfg = _make_cfg(timeout_secs=600, rss_max_mb=2048)
        assert cleanup._adopt_idle_policy() == 100.0
        assert cleanup.state.idle_timeout == 600
        assert cleanup.state.rss_max_mb == 2048
        assert mgr._rss_max_mb == 2048

    def test_adopt_keeps_the_loader_clamps(self) -> None:
        mgr, _ = _make_manager(timeout_secs=30)
        cleanup = mgr._cleanup_boundary()
        assert cleanup._adopt_idle_policy() == 60.0
        assert cleanup.state.idle_timeout == 60

        mgr._cfg = _make_cfg(timeout_secs=0)
        assert cleanup._adopt_idle_policy() == 300.0
        assert cleanup.state.idle_sweep_enabled is False

        mgr._cfg = _make_cfg(rss_max_mb=-4)
        cleanup._adopt_idle_policy()
        assert cleanup.state.rss_max_mb == 0

        mgr._cfg = _make_cfg()
        mgr._cfg.session.watchdog_rss_max_mb = "big"
        cleanup._adopt_idle_policy()
        assert cleanup.state.rss_max_mb == 0

    def test_transitions_are_logged_once_not_per_tick(self) -> None:
        mgr, _ = _make_manager(timeout_secs=30)
        cleanup = mgr._cleanup_boundary()
        with patch.object(cleanup._deps.logger, "warning") as warn:
            cleanup._adopt_idle_policy()
            cleanup._adopt_idle_policy()
            cleanup._adopt_idle_policy()
        assert warn.call_count == 1

    @pytest.mark.asyncio
    async def test_tick_loop_re_adopts_before_every_sleep(self) -> None:
        mgr, _ = _make_manager()
        cleanup = mgr._cleanup_boundary()
        signal = asyncio.Event()
        calls = 0

        def adopt() -> float:
            nonlocal calls
            calls += 1
            if calls >= 3:
                signal.set()
            return 0.01

        quiet = {
            name: AsyncMock()
            for name in (
                "_sweep_session_roots",
                "_sweep_sandbox_artifacts",
                "_maybe_prune_pycache",
                "_sweep_periodic_pids",
                "_sweep_untracked_mcps",
            )
        }
        mgr._watchdog = MagicMock(tick=AsyncMock())
        with (
            patch("kiro_crew.session.shutdown_event", signal),
            patch.object(SessionCleanup, "_adopt_idle_policy", side_effect=adopt),
            patch.multiple(SessionCleanup, **quiet),
        ):
            await asyncio.wait_for(cleanup._run_cleanup_ticks(0.01), timeout=5)
        assert calls >= 3


class _FakeHandle:
    def __init__(self, crew: str) -> None:
        self._crew_agent = crew
        self.rebinds: list[tuple[str, object]] = []

    def rebind_watchdog(self, crew_agent: str, settings=None) -> None:
        self.rebinds.append((crew_agent, settings))


class TestWatchdogFanOut:
    def test_handle_resolver_covers_both_provider_shapes(self) -> None:
        direct = SimpleNamespace(_handle=_FakeHandle(""))
        wrapped = SimpleNamespace(_client=SimpleNamespace(_handle=_FakeHandle("")))
        assert _watchdog_handle_of(direct) is direct._handle
        assert _watchdog_handle_of(wrapped) is wrapped._client._handle
        assert _watchdog_handle_of(SimpleNamespace()) is None
        assert _watchdog_handle_of(SimpleNamespace(_handle=object())) is None

    @pytest.mark.asyncio
    async def test_watchdog_change_rebinds_every_live_handle_with_its_own_crew(self) -> None:
        mgr, _ = _make_manager()
        a, b = _FakeHandle(""), _FakeHandle("reviewer")
        mgr._sessions["dashboard:1"] = SimpleNamespace(provider=SimpleNamespace(_handle=a))
        mgr._sessions["slack:2"] = SimpleNamespace(
            provider=SimpleNamespace(_client=SimpleNamespace(_handle=b))
        )
        mgr._sessions["fake:3"] = SimpleNamespace(provider=_make_provider())
        new_cfg = KiroCrewConfig()
        new_cfg.watchdog.stale_window_secs = 111.0
        settings_for = {}

        def fake_load(crew: str = "", cfg=None):  # the module seam takes cfg positionally
            settings_for[crew] = cfg
            return f"settings:{crew}"

        with patch("kiro_crew.session._load_allocation_watchdog_settings", side_effect=fake_load):
            await mgr._on_config_change(_change(new_cfg, "watchdog.stale_window_secs"))

        assert a.rebinds == [("", "settings:")]
        assert b.rebinds == [("reviewer", "settings:reviewer")]
        # The re-clamp reads the config the watcher loaded, never the disk.
        assert settings_for == {"": new_cfg, "reviewer": new_cfg}

    @pytest.mark.asyncio
    async def test_per_crew_watchdog_override_and_ceiling_also_fan_out(self) -> None:
        mgr, _ = _make_manager()
        h = _FakeHandle("reviewer")
        mgr._sessions["k"] = SimpleNamespace(provider=SimpleNamespace(_handle=h))
        with patch("kiro_crew.session._load_allocation_watchdog_settings", return_value="s"):
            await mgr._on_config_change(
                _change(_make_cfg(), "agents.reviewer.watchdog_tool_stall_suspect_secs")
            )
            await mgr._on_config_change(_change(_make_cfg(), "agent.chat_turn_timeout_secs"))
            await mgr._on_config_change(_change(_make_cfg(), "agents.reviewer.model"))
        assert len(h.rebinds) == 2

    @pytest.mark.asyncio
    async def test_real_settings_loader_reclamps_from_the_given_config(self) -> None:
        from kiro_crew.acp.session_handle import _load_watchdog_settings

        cfg = KiroCrewConfig()
        cfg.watchdog.stale_window_secs = 123.0
        with patch("kiro_crew.config.loader.KiroCrewConfig.load") as load:
            settings = _load_watchdog_settings("", cfg=cfg)
        load.assert_not_called()
        assert settings.stale_window_secs == 123.0


class TestSessionStartBudgetFollowsTheSnapshot:
    @pytest.mark.asyncio
    async def test_snapshot_wins_over_the_runtime_memo(self) -> None:
        from kiro_crew.acp import runtime as rt

        runtime = rt.AcpRuntime.__new__(rt.AcpRuntime)
        runtime._session_start_timeout = 5.0
        cfg = KiroCrewConfig()
        cfg.agent.session_start_timeout_secs = 900
        live.watch().prime(cfg)
        assert await runtime._session_start_budget() == 900.0

    @pytest.mark.asyncio
    async def test_snapshot_keeps_the_builtin_floor(self) -> None:
        from kiro_crew.acp import runtime as rt

        runtime = rt.AcpRuntime.__new__(rt.AcpRuntime)
        runtime._session_start_timeout = None
        cfg = KiroCrewConfig()
        cfg.agent.session_start_timeout_secs = 1
        live.watch().prime(cfg)
        assert await runtime._session_start_budget() == rt._SESSION_NEW_TIMEOUT

    @pytest.mark.asyncio
    async def test_without_a_snapshot_the_memo_is_used(self) -> None:
        from kiro_crew.acp import runtime as rt

        runtime = rt.AcpRuntime.__new__(rt.AcpRuntime)
        runtime._session_start_timeout = None
        with patch.object(rt, "_resolve_session_start_timeout", return_value=77.0) as resolve:
            assert await runtime._session_start_budget() == 77.0
            assert await runtime._session_start_budget() == 77.0
        resolve.assert_called_once()


class TestBotNameFollowsTheSnapshot:
    def _builder(self, bot_name: str):
        from kiro_crew.context import ContextBuilder

        b = ContextBuilder.__new__(ContextBuilder)
        b._bot_name = bot_name
        return b

    def test_snapshot_name_is_substituted(self) -> None:
        cfg = KiroCrewConfig()
        cfg.agent.bot_name = "Hermes"
        live.watch().prime(cfg)
        assert self._builder("Kiro")._substitute_bot_name("I am {bot_name}.") == "I am Hermes."

    def test_empty_snapshot_name_falls_back_to_the_captured_one(self) -> None:
        live.watch().prime(KiroCrewConfig())
        assert self._builder("Kiro")._substitute_bot_name("{bot_name}") == "Kiro"

    def test_no_snapshot_falls_back_to_the_captured_one(self) -> None:
        assert self._builder("Kiro")._substitute_bot_name("{bot_name}") == "Kiro"


class TestEndToEndThroughConfigWatch:
    @pytest.mark.asyncio
    async def test_a_file_write_reaches_the_manager_and_its_cleanup_loop(
        self, tmp_path: Path
    ) -> None:
        cfg_file = tmp_path / "config.json"
        local = tmp_path / "config.local.json"
        _write(cfg_file, {"session": {"timeout_secs": 3600, "pool_size": 0}})
        with (
            patch("kiro_crew.config.loader.config_path", return_value=cfg_file),
            patch("kiro_crew.config.loader.config_local_path", return_value=local),
            patch("kiro_crew.config.live._WATCH", ConfigWatch(poll_interval_secs=0.05)),
            patch("kiro_crew.session.default_project_dir", return_value="/ws"),
        ):
            watch = live.watch()
            boot = KiroCrewConfig.load()
            mgr = SessionManager(boot, provider_factory=MagicMock())
            watch.prime(boot)
            _write(
                cfg_file,
                {"session": {"timeout_secs": 600, "pool_size": 0, "watchdog_rss_max_mb": 512}},
            )
            change = await watch.refresh_now()

        assert change is not None
        assert {"session.timeout_secs", "session.watchdog_rss_max_mb"} <= change.changed
        assert mgr._cfg is change.new
        assert mgr._cfg.session.timeout_secs == 600
        cleanup = mgr._cleanup_boundary()
        assert cleanup._adopt_idle_policy() == 100.0
        assert cleanup.state.idle_timeout == 600
        assert cleanup.state.rss_max_mb == 512
