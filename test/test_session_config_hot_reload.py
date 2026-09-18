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

from kiro_crew.acp.types import ACP_BACKEND_KIRO
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
        from kiro_crew import platform_compat

        mgr, _ = _make_manager()
        mgr.refresh_defaults = AsyncMock()  # type: ignore[method-assign]
        mgr.reload_provider_factory = AsyncMock()  # type: ignore[method-assign]
        mgr._invalidate_and_purge_scheduled_messages = AsyncMock()  # type: ignore[method-assign]
        new_cfg = _make_cfg()
        await mgr._on_config_change(_change(new_cfg, path))
        if path == "agent.sandbox" and platform_compat.IS_LINUX:
            mgr.reload_provider_factory.assert_awaited_once_with(
                cfg=new_cfg,
                retire_companions=True,
            )
            mgr.refresh_defaults.assert_not_awaited()
            assert mgr._invalidate_and_purge_scheduled_messages.await_count == 2
        else:
            mgr.refresh_defaults.assert_awaited_once()
            mgr.reload_provider_factory.assert_not_awaited()
            expected_purges = 1 if path == "agent.sandbox" else 0
            assert mgr._invalidate_and_purge_scheduled_messages.await_count == expected_purges


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
    async def test_security_reload_retires_detached_companion_runtimes(self) -> None:
        mgr, _ = _make_manager()
        new_cfg = _make_cfg()
        subagent_runtime = SimpleNamespace(
            kill=AsyncMock(),
            is_alive=MagicMock(return_value=False),
        )
        background_runtime = SimpleNamespace(
            kill=AsyncMock(),
            is_alive=MagicMock(return_value=False),
        )
        mgr._subagent_runtimes["dashboard:parent"] = subagent_runtime
        mgr._bg_runtime = background_runtime
        with (
            patch("kiro_crew.session.KiroCrewConfig.load") as load,
            patch("kiro_crew.session.default_project_dir", return_value="/ws"),
            patch("kiro_crew.session.build_provider_factory", side_effect=_factory_for),
            patch.object(mgr, "start_pool", AsyncMock()),
        ):
            await mgr.reload_provider_factory(cfg=new_cfg, retire_companions=True)

        load.assert_not_called()
        subagent_runtime.kill.assert_awaited_once_with(expected=True)
        background_runtime.kill.assert_awaited_once_with(expected=True)
        assert mgr._subagent_runtimes == {}
        assert mgr._bg_runtime is None
        assert mgr._draining_bg_runtimes == []

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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("initial_agent", "updated_agent"),
    [
        (
            {
                "sandbox": "auto",
                "sandbox_allow_no_isolation": False,
                "sandbox_allow_unsandboxed_exec": False,
            },
            {
                "sandbox": "off",
                "sandbox_allow_no_isolation": False,
                "sandbox_allow_unsandboxed_exec": False,
            },
        ),
        (
            {
                "sandbox": "off",
                "sandbox_allow_no_isolation": False,
                "sandbox_allow_unsandboxed_exec": False,
            },
            {
                "sandbox": "auto",
                "sandbox_allow_no_isolation": False,
                "sandbox_allow_unsandboxed_exec": False,
            },
        ),
        (
            {
                "sandbox": "auto",
                "sandbox_allow_no_isolation": False,
                "sandbox_allow_unsandboxed_exec": False,
            },
            {
                "sandbox": "auto",
                "sandbox_allow_no_isolation": True,
                "sandbox_allow_unsandboxed_exec": False,
            },
        ),
        (
            {
                "sandbox": "auto",
                "sandbox_allow_no_isolation": False,
                "sandbox_allow_unsandboxed_exec": False,
            },
            {
                "sandbox": "auto",
                "sandbox_allow_no_isolation": False,
                "sandbox_allow_unsandboxed_exec": True,
            },
        ),
    ],
)
async def test_sandbox_security_write_purges_before_factory_refresh(
    tmp_path: Path,
    monkeypatch,
    initial_agent: dict[str, object],
    updated_agent: dict[str, object],
) -> None:
    from kiro_crew import autonudge_selfarm, sandbox
    from kiro_crew.dashboard import token_secret

    cfg_file = tmp_path / "config.json"
    local = tmp_path / "config.local.json"
    _write(cfg_file, {"agent": initial_agent, "session": {"pool_size": 0}})
    autonudge_selfarm._reset_scheduled_message_confinement_epoch_for_tests()
    monkeypatch.setattr(autonudge_selfarm, "data_home", lambda: tmp_path)
    monkeypatch.setattr(autonudge_selfarm.platform_compat, "IS_LINUX", True)
    monkeypatch.setattr(sandbox, "effective_sandbox_mode", lambda mode: mode)
    monkeypatch.setattr(
        sandbox,
        "detect_backend",
        lambda *, config_mode: "namespace" if config_mode != "off" else "none",
    )
    monkeypatch.setattr(token_secret, "_SECRET", None)
    monkeypatch.setattr(token_secret, "_SECRET_PERSISTENT", None)

    with (
        patch("kiro_crew.config.loader.config_path", return_value=cfg_file),
        patch("kiro_crew.config.loader.config_local_path", return_value=local),
        patch("kiro_crew.config.live._WATCH", ConfigWatch(poll_interval_secs=0.05)),
        patch("kiro_crew.session.default_project_dir", return_value="/ws"),
        patch("kiro_crew.autonudge.get_instance", return_value=None),
    ):
        watch = live.watch()
        boot = KiroCrewConfig.load()
        epoch_was_eligible = autonudge_selfarm.establish_scheduled_message_confinement_epoch()
        record_id = "scheduled-message:config-transition"
        record = autonudge_selfarm.scheduled_message_record_path(record_id)
        if epoch_was_eligible:
            autonudge_selfarm.record_scheduled_message(
                record_id,
                "chat-1-123",
                "private deferred text",
                2_000_000_000.0,
            )
        else:
            record.parent.mkdir(parents=True)
            record.write_text("forged while unconfined", encoding="utf-8")
        assert record.exists()

        mgr = SessionManager(boot, provider_factory=MagicMock())
        provider = SimpleNamespace(
            sandbox_mode=initial_agent["sandbox"],
            shutdown=AsyncMock(),
        )
        survivor = SimpleNamespace(provider=provider)
        mgr._sessions["dashboard:survivor"] = survivor  # type: ignore[assignment]
        order: list[str] = []

        async def reload_after_purge(*, cfg, retire_companions=False) -> None:
            order.append("reload")
            assert retire_companions is True
            assert not record.exists()
            assert not autonudge_selfarm.scheduled_message_hidden_leaf_confined()
            assert mgr._sessions["dashboard:survivor"] is survivor
            await provider.shutdown()
            mgr._sessions.clear()
            mgr._cfg = cfg

        mgr.reload_provider_factory = AsyncMock(  # type: ignore[method-assign]
            side_effect=reload_after_purge
        )
        watch.prime(boot)
        _write(cfg_file, {"agent": updated_agent, "session": {"pool_size": 0}})
        change = await watch.refresh_now()

    assert change is not None
    assert order == ["reload"]
    assert "dashboard:survivor" not in mgr._sessions
    provider.shutdown.assert_awaited_once()
    assert mgr._cfg.agent.sandbox == updated_agent["sandbox"]
    assert not (tmp_path / autonudge_selfarm.SCHEDULED_MESSAGE_RECORD_NAME).exists()
    assert not autonudge_selfarm.scheduled_message_hidden_leaf_confined()
    autonudge_selfarm._reset_scheduled_message_confinement_epoch_for_tests()


@pytest.mark.asyncio
async def test_non_linux_sandbox_edit_only_invalidates_and_purges(monkeypatch) -> None:
    """Unsupported hosts never honor provenance, so an edit must not kill live work."""
    from kiro_crew import autonudge_selfarm, platform_compat

    mgr, _ = _make_manager()
    new_cfg = _make_cfg()
    provider = SimpleNamespace(shutdown=AsyncMock())
    subagent = SimpleNamespace(kill=AsyncMock())
    background = SimpleNamespace(kill=AsyncMock())
    session = SimpleNamespace(provider=provider)
    mgr._sessions["dashboard:live"] = session  # type: ignore[assignment]
    mgr._subagent_runtimes["dashboard:parent"] = subagent
    mgr._bg_runtime = background

    async def adopt(*, cfg) -> None:
        mgr._cfg = cfg

    mgr.refresh_defaults = AsyncMock(side_effect=adopt)  # type: ignore[method-assign]
    invalidate = MagicMock()
    clear = MagicMock()
    monkeypatch.setattr(platform_compat, "IS_LINUX", False)
    monkeypatch.setattr(
        autonudge_selfarm,
        "invalidate_scheduled_message_confinement_epoch",
        invalidate,
    )
    monkeypatch.setattr(autonudge_selfarm, "clear_scheduled_message_records", clear)

    with patch("kiro_crew.autonudge.get_instance", return_value=None):
        await mgr._on_config_change(_change(new_cfg, "agent.sandbox"))

    invalidate.assert_called_once_with()
    clear.assert_called_once_with()
    mgr.refresh_defaults.assert_awaited_once_with(cfg=new_cfg)
    assert mgr._sessions["dashboard:live"] is session
    assert mgr._subagent_runtimes["dashboard:parent"] is subagent
    assert mgr._bg_runtime is background
    provider.shutdown.assert_not_awaited()
    subagent.kill.assert_not_awaited()
    background.kill.assert_not_awaited()


def _companion_start_case(mgr, kind, monkeypatch, started, proceed):
    alive = False

    async def spawn() -> None:
        nonlocal alive
        started.set()
        await proceed.wait()
        alive = True

    async def kill(*, expected: bool = False, reason: str = "") -> None:
        nonlocal alive
        alive = False

    runtime = SimpleNamespace(
        acp_backend=ACP_BACKEND_KIRO,
        pid=4242,
        spawn=AsyncMock(side_effect=spawn),
        kill=AsyncMock(side_effect=kill),
        is_alive=lambda: alive,
        _is_stale=AsyncMock(return_value=None),
        create_session=AsyncMock(return_value=object()),
        terminate_session=AsyncMock(),
    )
    bootstrap_factory = None
    if kind == "bootstrap":
        session_provider = SimpleNamespace(
            _runtime=runtime,
            _owns_runtime=True,
            _handle=SimpleNamespace(session_id="bootstrap-session"),
        )
        provider = SimpleNamespace(
            _client=session_provider,
            start=AsyncMock(side_effect=spawn),
            shutdown=AsyncMock(),
        )
        bootstrap_factory = MagicMock(return_value=provider)
        mgr._provider_factory = bootstrap_factory

        async def start():
            return await mgr._get_or_bootstrap_run_runtime("dashboard:admission-parent")

        expected = runtime
    else:
        monkeypatch.setattr("kiro_crew.acp.runtime.AcpRuntime", MagicMock(return_value=runtime))
        if kind == "subagent":

            async def start():
                return await mgr.get_subagent_runtime("dashboard:admission-parent")

            expected = runtime
        else:
            mgr._cfg.agent.acp_backend = ACP_BACKEND_KIRO

            async def start():
                return await mgr.get_bg_session()

            expected = runtime.create_session.return_value
    return runtime, start, expected, bootstrap_factory


async def _wait_until_all_start_permits_are_claimed(mgr) -> None:
    for _ in range(100):
        if mgr._start_sem._value == 0:
            return
        await asyncio.sleep(0)
    raise AssertionError("sandbox transition did not claim every start permit")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["subagent", "bootstrap", "background"])
async def test_companion_start_registers_before_transition_snapshot(
    kind: str,
    monkeypatch,
) -> None:
    """An admitted old-generation start is registered before retirement snapshots."""
    mgr, _ = _make_manager()
    started = asyncio.Event()
    proceed = asyncio.Event()
    runtime, start, expected, _ = _companion_start_case(
        mgr,
        kind,
        monkeypatch,
        started,
        proceed,
    )
    new_cfg = _make_cfg()
    new_cfg.agent.acp_backend = ACP_BACKEND_KIRO
    new_factory = MagicMock(name="new_factory")
    mgr._invalidate_and_purge_scheduled_messages = AsyncMock()  # type: ignore[method-assign]

    with (
        patch("kiro_crew.session.default_project_dir", return_value="/ws"),
        patch("kiro_crew.session.build_provider_factory", return_value=new_factory),
        patch.object(mgr, "start_pool", AsyncMock()),
    ):
        start_task = asyncio.create_task(start())
        await asyncio.wait_for(started.wait(), timeout=1.0)
        transition = asyncio.create_task(
            mgr._apply_linux_sandbox_security_change(_change(new_cfg, "agent.sandbox"))
        )
        await _wait_until_all_start_permits_are_claimed(mgr)
        assert not transition.done()

        proceed.set()
        assert await start_task is expected
        await transition

    runtime.kill.assert_awaited_once_with(expected=True)
    assert mgr._subagent_runtimes == {}
    assert mgr._bg_runtime is None
    assert mgr._sandbox_retirement_quarantine == {}
    assert mgr._provider_factory is new_factory
    assert mgr._start_sem._value == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["subagent", "bootstrap", "background"])
async def test_companion_start_waits_behind_transition_barrier(
    kind: str,
    monkeypatch,
) -> None:
    """A new companion cannot spawn until the sandbox transition releases admission."""
    mgr, _ = _make_manager()
    started = asyncio.Event()
    proceed = asyncio.Event()
    proceed.set()
    runtime, start, expected, bootstrap_factory = _companion_start_case(
        mgr,
        kind,
        monkeypatch,
        started,
        proceed,
    )
    old_factory = MagicMock(name="old_factory")
    if kind == "bootstrap":
        mgr._provider_factory = old_factory
    new_cfg = _make_cfg()
    new_cfg.agent.acp_backend = ACP_BACKEND_KIRO
    new_factory = bootstrap_factory or MagicMock(name="new_factory")
    transition_holds_admission = asyncio.Event()
    release_transition = asyncio.Event()
    purge_calls = 0

    async def controlled_purge() -> None:
        nonlocal purge_calls
        purge_calls += 1
        if purge_calls == 1:
            transition_holds_admission.set()
            await release_transition.wait()

    mgr._invalidate_and_purge_scheduled_messages = AsyncMock(  # type: ignore[method-assign]
        side_effect=controlled_purge
    )
    with (
        patch("kiro_crew.session.default_project_dir", return_value="/ws"),
        patch("kiro_crew.session.build_provider_factory", return_value=new_factory),
        patch.object(mgr, "start_pool", AsyncMock()),
    ):
        transition = asyncio.create_task(
            mgr._apply_linux_sandbox_security_change(_change(new_cfg, "agent.sandbox"))
        )
        await asyncio.wait_for(transition_holds_admission.wait(), timeout=1.0)
        assert mgr._start_sem._value == 0

        start_task = asyncio.create_task(start())
        await asyncio.sleep(0)
        assert not started.is_set()
        assert "dashboard:admission-parent" not in mgr._subagent_runtimes
        assert mgr._bg_runtime is None

        release_transition.set()
        await transition
        assert await start_task is expected

    assert started.is_set()
    runtime.kill.assert_not_awaited()
    if kind == "background":
        assert mgr._bg_runtime is runtime
    else:
        assert mgr._subagent_runtimes["dashboard:admission-parent"] is runtime
    if kind == "bootstrap":
        old_factory.assert_not_called()
        assert bootstrap_factory is not None
        bootstrap_factory.assert_called_once()
    assert mgr._start_sem._value == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["subagent", "bootstrap", "background"])
async def test_live_companion_runtime_bypasses_start_admission(
    kind: str,
    monkeypatch,
) -> None:
    """A canonical live runtime remains available while cold starts are fenced."""
    mgr, _ = _make_manager()
    started = asyncio.Event()
    proceed = asyncio.Event()
    proceed.set()
    runtime, start, expected, _ = _companion_start_case(
        mgr,
        kind,
        monkeypatch,
        started,
        proceed,
    )
    await runtime.spawn()
    if kind == "background":
        mgr._cfg.agent.acp_backend = ACP_BACKEND_KIRO
        mgr._bg_runtime = runtime
    else:
        mgr._subagent_runtimes["dashboard:admission-parent"] = runtime

    for _ in range(4):
        await mgr._start_sem.acquire()
    try:
        assert await asyncio.wait_for(start(), timeout=1.0) is expected
    finally:
        for _ in range(4):
            mgr._start_sem.release()

    assert runtime.spawn.await_count == 1
    runtime.kill.assert_not_awaited()
    assert mgr._start_sem._value == 4


@pytest.mark.asyncio
async def test_verified_warm_provider_retirement_allows_publish() -> None:
    """A drained warm provider leaves quarantine only after a dead liveness verdict."""
    mgr, old_factory = _make_manager()
    new_cfg = _make_cfg()
    new_factory = MagicMock(name="new_factory")
    provider = SimpleNamespace(is_process_alive=MagicMock(return_value=False))
    mgr._warm_pool.put_nowait((provider, 0.0))

    async def discard(candidate, context: str) -> None:
        assert candidate is provider
        assert context == "Sandbox posture drain"

    with (
        patch("kiro_crew.session.default_project_dir", return_value="/ws"),
        patch("kiro_crew.session.build_provider_factory", return_value=new_factory),
        patch.object(mgr, "_discard_pool_provider", AsyncMock(side_effect=discard)) as shutdown,
        patch.object(mgr, "start_pool", AsyncMock()),
    ):
        await mgr.reload_provider_factory(cfg=new_cfg, retire_companions=True)

    shutdown.assert_awaited_once_with(provider, "Sandbox posture drain")
    assert mgr._warm_pool.empty()
    assert mgr._sandbox_retirement_quarantine == {}
    assert mgr._provider_factory is new_factory
    assert mgr._provider_factory is not old_factory


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", ["kill", "alive", "missing", "probe"])
async def test_unretired_warm_provider_retries_same_identity_before_publish(
    failure_mode: str,
) -> None:
    """Every uncertain warm-provider outcome retains the exact object for retry."""
    mgr, old_factory = _make_manager()
    old_cfg = mgr._cfg
    new_cfg = _make_cfg()
    new_factory = MagicMock(name="new_factory")
    alive = True
    attempts: list[object] = []
    provider = SimpleNamespace()

    if failure_mode != "missing":

        def is_process_alive() -> bool:
            if failure_mode == "probe" and len(attempts) == 1:
                raise RuntimeError("probe failed")
            return alive

        provider.is_process_alive = is_process_alive

    async def discard(candidate, context: str) -> None:
        nonlocal alive
        assert candidate is provider
        assert context == "Sandbox posture drain"
        attempts.append(candidate)
        if len(attempts) == 1 and failure_mode == "kill":
            raise RuntimeError("hard kill failed")
        if len(attempts) > 1:
            alive = False
            provider.is_process_alive = lambda: False

    mgr._warm_pool.put_nowait((provider, 0.0))
    with (
        patch("kiro_crew.session.default_project_dir", return_value="/ws"),
        patch("kiro_crew.session.build_provider_factory", return_value=new_factory) as build,
        patch.object(mgr, "_discard_pool_provider", AsyncMock(side_effect=discard)),
        patch.object(mgr, "start_pool", AsyncMock()),
    ):
        with pytest.raises(RuntimeError, match="warm-pool"):
            await mgr.reload_provider_factory(cfg=new_cfg, retire_companions=True)
        assert mgr._cfg is old_cfg
        assert mgr._provider_factory is old_factory
        assert mgr._warm_pool.empty()
        assert mgr._sandbox_retirement_quarantine[id(provider)][1] is provider
        build.assert_not_called()

        await mgr.reload_provider_factory(cfg=new_cfg, retire_companions=True)

    assert attempts == [provider, provider]
    assert mgr._sandbox_retirement_quarantine == {}
    assert mgr._cfg is new_cfg
    assert mgr._provider_factory is new_factory
    build.assert_called_once_with(new_cfg)


@pytest.mark.asyncio
async def test_warm_provider_arriving_before_publish_is_verified_first() -> None:
    """A provider found at the publication barrier joins the retirement loop."""
    mgr, old_factory = _make_manager()
    new_cfg = _make_cfg()
    new_factory = MagicMock(name="new_factory")
    late_provider = SimpleNamespace(is_process_alive=MagicMock(return_value=False))
    registered_provider = SimpleNamespace(
        shutdown=AsyncMock(),
        is_process_alive=MagicMock(return_value=False),
    )
    mgr._sessions["dashboard:live"] = SimpleNamespace(  # type: ignore[assignment]
        provider=registered_provider
    )

    async def register_late_provider() -> None:
        mgr._warm_pool.put_nowait((late_provider, 0.0))

    registered_provider.shutdown.side_effect = register_late_provider
    with (
        patch("kiro_crew.session.default_project_dir", return_value="/ws"),
        patch("kiro_crew.session.build_provider_factory", return_value=new_factory),
        patch.object(mgr, "_discard_pool_provider", AsyncMock()) as shutdown,
        patch.object(mgr, "start_pool", AsyncMock()),
    ):
        await mgr.reload_provider_factory(cfg=new_cfg, retire_companions=True)

    registered_provider.shutdown.assert_awaited_once_with()
    shutdown.assert_awaited_once_with(late_provider, "Sandbox posture drain")
    assert mgr._warm_pool.empty()
    assert mgr._sandbox_retirement_quarantine == {}
    assert mgr._provider_factory is new_factory
    assert mgr._provider_factory is not old_factory


@pytest.mark.asyncio
async def test_registered_runtime_retry_reuses_quarantine_before_publish(
    tmp_path: Path, monkeypatch
) -> None:
    """ConfigWatch retries the exact survivor and publishes only after it is dead."""
    from kiro_crew import autonudge_selfarm, platform_compat

    cfg_file = tmp_path / "config.json"
    local = tmp_path / "config.local.json"
    initial_agent = {
        "sandbox": "auto",
        "sandbox_allow_no_isolation": False,
        "sandbox_allow_unsandboxed_exec": False,
    }
    updated_agent = {**initial_agent, "sandbox_allow_no_isolation": True}
    _write(cfg_file, {"agent": initial_agent, "session": {"pool_size": 0}})
    monkeypatch.setattr(platform_compat, "IS_LINUX", True)
    monkeypatch.setattr(autonudge_selfarm.platform_compat, "IS_LINUX", True)

    alive = True
    attempts: list[object] = []
    barrier_values: list[int] = []

    async def shutdown() -> None:
        nonlocal alive
        attempts.append(provider)
        barrier_values.append(mgr._start_sem._value)
        if len(attempts) == 1:
            raise RuntimeError("first shutdown failed")
        alive = False

    provider = SimpleNamespace(
        shutdown=AsyncMock(side_effect=shutdown),
        is_process_alive=lambda: alive,
    )
    old_factory = MagicMock(name="old_factory")
    new_factory = MagicMock(name="new_factory")

    async def purge() -> None:
        barrier_values.append(mgr._start_sem._value)

    service = SimpleNamespace(
        purge_scheduled_messages_for_confinement_change=AsyncMock(side_effect=purge)
    )

    with (
        patch("kiro_crew.config.loader.config_path", return_value=cfg_file),
        patch("kiro_crew.config.loader.config_local_path", return_value=local),
        patch("kiro_crew.config.live._WATCH", ConfigWatch(poll_interval_secs=0.05)),
        patch("kiro_crew.session.default_project_dir", return_value="/ws"),
        patch("kiro_crew.session.build_provider_factory", return_value=new_factory) as build,
        patch(
            "kiro_crew.session_lifecycle.record_sessions_ended",
            AsyncMock(),
        ) as record_ended,
        patch("kiro_crew.autonudge.get_instance", return_value=service),
    ):
        watch = live.watch()
        boot = KiroCrewConfig.load()
        mgr = SessionManager(boot, provider_factory=old_factory)
        mgr.start_pool = AsyncMock()  # type: ignore[method-assign]
        mgr._sessions["dashboard:survivor"] = SimpleNamespace(  # type: ignore[assignment]
            provider=provider
        )
        watch.prime(boot)
        _write(cfg_file, {"agent": updated_agent, "session": {"pool_size": 0}})

        first = await watch.refresh_now()
        assert first is not None
        assert mgr._cfg is boot
        assert mgr._provider_factory is old_factory
        assert mgr._sandbox_retirement_quarantine[id(provider)] == (
            "session:dashboard:survivor",
            provider,
        )
        assert service.purge_scheduled_messages_for_confinement_change.await_count == 1
        mgr.start_pool.assert_not_awaited()
        build.assert_not_called()

        # No file change: this is ConfigWatch's stale-applier retry, not a new
        # transition. It must reattempt the quarantined object by identity.
        assert await watch.refresh_now() is None

    assert attempts == [provider, provider]
    record_ended.assert_awaited_once_with(
        ["dashboard:survivor"],
        end_reason="retired",
    )
    assert mgr._sandbox_retirement_quarantine == {}
    assert mgr._cfg.agent.sandbox_allow_no_isolation is True
    assert mgr._provider_factory is new_factory
    build.assert_called_once_with(mgr._cfg)
    mgr.start_pool.assert_awaited_once_with(blocking=False)
    # Initial purge on each attempt plus the final purge only after success.
    assert service.purge_scheduled_messages_for_confinement_change.await_count == 3
    assert barrier_values and set(barrier_values) == {0}
    assert mgr._start_sem._value == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["subagent", "background"])
async def test_detached_runtime_retry_kills_same_survivor(kind: str) -> None:
    """Detached runtimes remain retryable after their first kill fails."""
    mgr, old_factory = _make_manager()
    old_cfg = mgr._cfg
    new_cfg = _make_cfg()
    new_factory = MagicMock(name="new_factory")
    alive = True
    attempts: list[object] = []

    async def kill(*, expected: bool) -> None:
        nonlocal alive
        assert expected is True
        attempts.append(runtime)
        if len(attempts) == 1:
            raise RuntimeError("first kill failed")
        alive = False

    runtime = SimpleNamespace(
        kill=AsyncMock(side_effect=kill),
        is_alive=lambda: alive,
    )
    if kind == "subagent":
        mgr._subagent_runtimes["dashboard:parent"] = runtime
    else:
        mgr._bg_runtime = runtime

    with (
        patch("kiro_crew.session.default_project_dir", return_value="/ws"),
        patch("kiro_crew.session.build_provider_factory", return_value=new_factory) as build,
        patch.object(mgr, "start_pool", AsyncMock()),
    ):
        with pytest.raises(RuntimeError, match="could not retire"):
            await mgr.reload_provider_factory(cfg=new_cfg, retire_companions=True)
        assert mgr._cfg is old_cfg
        assert mgr._provider_factory is old_factory
        assert mgr._sandbox_retirement_quarantine[id(runtime)][1] is runtime
        build.assert_not_called()

        await mgr.reload_provider_factory(cfg=new_cfg, retire_companions=True)

    assert attempts == [runtime, runtime]
    assert mgr._sandbox_retirement_quarantine == {}
    assert mgr._cfg is new_cfg
    assert mgr._provider_factory is new_factory
    build.assert_called_once_with(new_cfg)
    if kind == "subagent":
        assert mgr._subagent_runtimes == {}
    else:
        assert mgr._bg_runtime is None
        assert mgr._draining_bg_runtimes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("probe_mode", ["missing", "raises"])
async def test_unverifiable_registered_runtime_stays_quarantined_until_dead(
    probe_mode: str,
) -> None:
    """Missing and raising probes fail closed without losing the runtime reference."""
    mgr, old_factory = _make_manager()
    old_cfg = mgr._cfg
    new_cfg = _make_cfg()
    provider = SimpleNamespace(shutdown=AsyncMock())
    if probe_mode == "raises":
        provider.is_process_alive = MagicMock(side_effect=RuntimeError("probe failed"))
    mgr._sessions["dashboard:unverifiable"] = SimpleNamespace(  # type: ignore[assignment]
        provider=provider
    )
    new_factory = MagicMock(name="new_factory")

    with (
        patch("kiro_crew.session.default_project_dir", return_value="/ws"),
        patch("kiro_crew.session.build_provider_factory", return_value=new_factory) as build,
        patch.object(mgr, "start_pool", AsyncMock()),
    ):
        with pytest.raises(RuntimeError, match="session:dashboard:unverifiable"):
            await mgr.reload_provider_factory(cfg=new_cfg, retire_companions=True)
        assert mgr._cfg is old_cfg
        assert mgr._provider_factory is old_factory
        assert mgr._sandbox_retirement_quarantine[id(provider)][1] is provider
        build.assert_not_called()

        # Once liveness becomes provable, the same retained object converges.
        provider.is_process_alive = MagicMock(return_value=False)
        await mgr.reload_provider_factory(cfg=new_cfg, retire_companions=True)

    assert provider.shutdown.await_count == 2
    assert mgr._sandbox_retirement_quarantine == {}
    assert mgr._cfg is new_cfg
    assert mgr._provider_factory is new_factory


@pytest.mark.asyncio
async def test_off_auto_transition_purges_forgery_before_confined_restart(
    tmp_path: Path, monkeypatch
) -> None:
    import hashlib
    import hmac
    import json
    import time

    from kiro_crew import autonudge_selfarm, sandbox
    from kiro_crew.autonudge import AutoNudgeService, is_scheduled_message
    from kiro_crew.dashboard import token_secret

    cfg_file = tmp_path / "config.json"
    local = tmp_path / "config.local.json"
    off_agent = {
        "sandbox": "off",
        "sandbox_allow_no_isolation": False,
        "sandbox_allow_unsandboxed_exec": False,
    }
    auto_agent = {**off_agent, "sandbox": "auto"}
    _write(cfg_file, {"agent": off_agent, "session": {"pool_size": 0}})
    monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")
    monkeypatch.setattr(autonudge_selfarm, "data_home", lambda: tmp_path)
    monkeypatch.setattr(autonudge_selfarm.platform_compat, "IS_LINUX", True)
    monkeypatch.setattr(sandbox, "effective_sandbox_mode", lambda mode: mode)
    monkeypatch.setattr(
        sandbox,
        "detect_backend",
        lambda *, config_mode: "namespace" if config_mode != "off" else "none",
    )
    signing_key = b"k" * token_secret._MIN_KEY_BYTES
    monkeypatch.setattr(token_secret, "_SECRET", signing_key)
    monkeypatch.setattr(token_secret, "_SECRET_PERSISTENT", True)
    autonudge_selfarm._reset_scheduled_message_confinement_epoch_for_tests()

    loop_id = "forged-off-generation"
    trust_id = f"scheduled-message:{loop_id}"
    record = autonudge_selfarm.scheduled_message_record_path(trust_id)
    due = 2_000_000_000.0

    def plant_forged_pair() -> None:
        payload = autonudge_selfarm._scheduled_message_payload(
            "chat-1-123", "forged user turn", due, completed=False
        )
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        mac = hmac.new(
            signing_key,
            autonudge_selfarm._SCHEDULED_PROVENANCE_DOMAIN + canonical.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(
            json.dumps({**payload, "armed_ts": time.time(), "provenance": mac}),
            encoding="utf-8",
        )
        (tmp_path / "autonudge.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "loops": [
                        {
                            "id": loop_id,
                            "slot_key": "chat-1-123",
                            "message": "forged user turn",
                            "scheduled_message": True,
                            "scheduled_at": due,
                            "next_due_ts": due,
                            "max_cycles": 1,
                            "active": True,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    with (
        patch("kiro_crew.config.loader.config_path", return_value=cfg_file),
        patch("kiro_crew.config.loader.config_local_path", return_value=local),
        patch("kiro_crew.config.live._WATCH", ConfigWatch(poll_interval_secs=0.05)),
        patch("kiro_crew.session.default_project_dir", return_value="/ws"),
        patch("kiro_crew.autonudge.get_instance", return_value=None),
    ):
        watch = live.watch()
        boot = KiroCrewConfig.load()
        assert not autonudge_selfarm.establish_scheduled_message_confinement_epoch()
        mgr = SessionManager(boot, provider_factory=MagicMock())
        provider = SimpleNamespace(shutdown=AsyncMock())
        mgr._sessions["dashboard:old-off-process"] = SimpleNamespace(  # type: ignore[assignment]
            provider=provider
        )

        async def retire_old_generation(*, cfg, retire_companions=False) -> None:
            assert retire_companions is True
            plant_forged_pair()
            assert record.exists()
            await provider.shutdown()
            mgr._sessions.clear()
            mgr._cfg = cfg

        mgr.reload_provider_factory = AsyncMock(  # type: ignore[method-assign]
            side_effect=retire_old_generation
        )
        watch.prime(boot)
        _write(cfg_file, {"agent": auto_agent, "session": {"pool_size": 0}})
        await watch.refresh_now()

        assert provider.shutdown.await_count == 1
        assert not record.exists()
        assert (tmp_path / "autonudge.json").exists()

        # A fresh confined process may establish a new epoch, but the old
        # process's signed pair has already been removed after its retirement.
        autonudge_selfarm._reset_scheduled_message_confinement_epoch_for_tests()
        restarted = AutoNudgeService(base_dir=tmp_path)
        await restarted.start()
        try:
            assert autonudge_selfarm.scheduled_message_hidden_leaf_confined()
            assert autonudge_selfarm.read_scheduled_message(trust_id, "chat-1-123") is None
            restored = [loop for loop in restarted.list_all() if is_scheduled_message(loop)]
            assert restored
            assert all(loop.message == "" for loop in restored)
        finally:
            restarted.stop()
            autonudge_selfarm._reset_scheduled_message_confinement_epoch_for_tests()
