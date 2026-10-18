"""The appliers the dashboard server registers on the live config watcher.

``_register_config_watch`` owns every applier whose holder is the dashboard; each is driven
the way the dispatcher drives a subscriber, with a hand-built ``ConfigChange``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from _hot_reload_helpers import write_config as _write
from config_live_helpers import (  # noqa: F401 -- fixtures are used by name
    cfg_file_fixture,
)

from kiro_crew.config import live
from kiro_crew.config.live import ConfigChange
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.testing.wait import await_until

# ── the dashboard server's own appliers ───────────────────────────────────


class TestServerAppliers:
    """``_register_config_watch`` owns the appliers whose holder is the dashboard.

    They are driven the way a dispatcher's subscriber actually is -- a hand-built
    :class:`ConfigChange` handed straight to the registered callback -- so the test
    covers the applier's own decision (does it fire, what does it push) without
    booting an aiohttp server.
    """

    def _register(self, state) -> dict[str, Any]:
        from aiohttp import web

        from kiro_crew.dashboard.server import _register_config_watch

        app = web.Application()
        state.set_dynamic_cards_enabled = MagicMock()
        _register_config_watch(app, state, None)
        return {s.name: s for s in app["config_watch_subscriptions"]}

    def _cfg(self, **agent_kw) -> KiroCrewConfig:
        cfg = KiroCrewConfig()
        for key, value in agent_kw.items():
            setattr(cfg.agent, key, value)
        return cfg

    def test_the_dashboard_registers_no_applier_for_owner_bound_leaves(self) -> None:
        """The workflow ceiling and the channel caps are bound by their OWNERS'
        constructors (``live.bind``), so the dashboard registers nothing for them."""
        subs = self._register(SimpleNamespace(workflow_service=None, channel_manager=None))
        assert not any("workflow_run_timeout" in n or "channel" in n for n in subs)

    def test_the_chat_default_model_has_an_applier(self) -> None:
        """Regression: the list carried ``agent.role_models.background`` but not
        ``agent.model``.

        Both keys are baked into a kiro agent spec at agent-build time, so both
        need the same rebuild to take effect. With no applier for the chat
        default, a change reached ``config.json`` and the provider factory
        (``refresh_defaults``) but never ``~/.kiro/agents/kirocrew.json`` -- which
        is the file kiro-cli reads at ``--agent`` startup -- so every newly created
        session kept inheriting the PREVIOUS model until the gateway restarted.
        """
        subs = self._register(SimpleNamespace(workflow_service=None, channel_manager=None))
        assert "agent.model" in subs

    @pytest.mark.asyncio
    async def test_the_chat_default_model_applier_rebuilds_only_when_touched(self) -> None:
        """It fires on its own leaf and stays out of every other change.

        The rebuild is not free -- it rewrites the spec files -- so an unrelated
        config write must not pay for one, the same discrimination
        ``_apply_background_model`` makes with its ``change.touched`` guard.
        """
        state = SimpleNamespace(
            workflow_service=None,
            channel_manager=None,
            push_refresh=MagicMock(),
        )
        apply = self._register(state)["agent.model"].callback()
        rebuilds: list[int] = []
        cfg = KiroCrewConfig()

        def _rebuild() -> tuple[Path, bool]:
            rebuilds.append(1)
            return (Path("/tmp/kirocrew.json"), True)

        with patch("kiro_crew.agent.rebuild_agent_config_reporting", _rebuild):
            await apply(
                ConfigChange(old=KiroCrewConfig(), new=cfg, changed=frozenset({"agent.model"}))
            )
            assert rebuilds == [1], "a touched agent.model rebuilds the spec"
            state.push_refresh.assert_called_once_with("agents")
            await apply(ConfigChange(old=cfg, new=cfg, changed=frozenset({"agent.log_level"})))
            assert rebuilds == [1], "an untouched agent.model rebuilds nothing"
            state.push_refresh.assert_called_once_with("agents")

    @pytest.mark.asyncio
    async def test_a_successful_rebuild_reconciles_the_warm_pool(self) -> None:
        """The rebuild must re-drain the warm pool to close the ordering race.

        SessionManager subscribes to ``agent.model`` before this applier, so it
        runs ``refresh_defaults`` first and re-fills the warm pool from the spec
        as it stood BEFORE the rebuild. A provider minted in that window pins the
        old model. After the spec is rebuilt this applier re-runs the same
        idempotent refresh so any raced provider is discarded and re-spawned from
        the correct spec. Passing ``change.new`` keeps it off disk on the loop.
        """
        sessions = SimpleNamespace(refresh_defaults=AsyncMock())
        state = SimpleNamespace(
            workflow_service=None,
            channel_manager=None,
            push_refresh=MagicMock(),
            sessions=sessions,
        )
        apply = self._register(state)["agent.model"].callback()
        new_cfg = KiroCrewConfig()
        change = ConfigChange(old=KiroCrewConfig(), new=new_cfg, changed=frozenset({"agent.model"}))
        with patch(
            "kiro_crew.agent.rebuild_agent_config_reporting",
            lambda: (Path("/tmp/kirocrew.json"), True),
        ):
            await apply(change)
        sessions.refresh_defaults.assert_awaited_once_with(cfg=new_cfg)
        state.push_refresh.assert_called_once_with("agents")

    @pytest.mark.asyncio
    async def test_a_failed_rebuild_notifies_and_defers_for_retry(self) -> None:
        """A durable config write must not silently claim the stale spec is live.

        The dashboard receives an actionable error, no refresh advertises the
        unapplied value, and the exception reaches ConfigWatch so it records the
        applier as stale and retries it on later ticks.
        """
        state = SimpleNamespace(
            workflow_service=None,
            channel_manager=None,
            push_refresh=MagicMock(),
            notify=MagicMock(),
        )
        apply = self._register(state)["agent.model"].callback()

        def _boom() -> tuple[Path, bool]:
            raise OSError("spec directory is read-only")

        with (
            patch("kiro_crew.agent.rebuild_agent_config_reporting", _boom),
            pytest.raises(OSError, match="spec directory is read-only"),
        ):
            await apply(
                ConfigChange(
                    old=KiroCrewConfig(),
                    new=KiroCrewConfig(),
                    changed=frozenset({"agent.model"}),
                )
            )
        state.push_refresh.assert_not_called()
        state.notify.assert_called_once()
        assert state.notify.call_args.args[:2] == (
            "agent",
            "Default model could not be applied",
        )
        assert state.notify.call_args.args[2] == (
            "The setting was saved but new sessions will keep using the previous model. "
            "Kiro Crew retries automatically; check the gateway logs if this persists."
        )

    @pytest.mark.asyncio
    async def test_a_refused_rebuild_is_not_reported_as_applied(self) -> None:
        """A shared-home refusal returns ``wrote=False`` WITHOUT writing the spec.

        ``rebuild_agent_config_reporting`` returns the spec path even when the
        shared-agent-home guard declines to rewrite it, so the installed
        ``kirocrew.json`` keeps its old model pin. That is a no-op, not a
        success: the applier must take the SAME failure path as a raised error --
        notify once, push no refresh, and re-raise so ConfigWatch defers it for
        retry -- instead of clearing the failure state and broadcasting success.
        """
        state = SimpleNamespace(
            workflow_service=None,
            channel_manager=None,
            push_refresh=MagicMock(),
            notify=MagicMock(),
        )
        apply = self._register(state)["agent.model"].callback()

        def _refused() -> tuple[Path, bool]:
            return (Path("/home/x/.kiro/agents/kirocrew.json"), False)

        with (
            patch("kiro_crew.agent.rebuild_agent_config_reporting", _refused),
            pytest.raises(RuntimeError, match="refused"),
        ):
            await apply(
                ConfigChange(
                    old=KiroCrewConfig(),
                    new=KiroCrewConfig(),
                    changed=frozenset({"agent.model"}),
                )
            )
        state.push_refresh.assert_not_called()
        state.notify.assert_called_once()
        assert state.notify.call_args.args[:2] == (
            "agent",
            "Default model could not be applied",
        )

    @pytest.mark.asyncio
    async def test_successful_retry_notifies_that_the_saved_model_is_active(self) -> None:
        """Recovery must resolve the operator-visible failure state."""
        state = SimpleNamespace(
            workflow_service=None,
            channel_manager=None,
            push_refresh=MagicMock(),
            notify=MagicMock(),
        )
        apply = self._register(state)["agent.model"].callback()
        attempts = 0

        def _rebuild() -> tuple[Path, bool]:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("spec directory is read-only")
            return (Path("/tmp/kirocrew.json"), True)

        change = ConfigChange(
            old=KiroCrewConfig(),
            new=KiroCrewConfig(),
            changed=frozenset({"agent.model"}),
        )
        with patch("kiro_crew.agent.rebuild_agent_config_reporting", _rebuild):
            with pytest.raises(OSError, match="spec directory is read-only"):
                await apply(change)
            await apply(change)

        state.push_refresh.assert_called_once_with("agents")
        assert [call.args[:2] for call in state.notify.call_args_list] == [
            ("agent", "Default model could not be applied"),
            ("agent", "Default model applied"),
        ]
        assert state.notify.call_args_list[-1].args[2] == (
            "The saved default model is now active. New sessions will use it."
        )

    @pytest.mark.asyncio
    async def test_the_channel_manager_binds_its_caps_in_its_constructor(
        self, tmp_path: Path
    ) -> None:
        from kiro_crew.channel import ChannelManager

        live.reset_for_tests()
        mgr = ChannelManager(
            broadcast_fn=lambda *_a, **_k: None,
            max_channels=2,
            max_agents=3,
            channels_dir=str(tmp_path),
        )
        names = {s.name for s in live.watch().subscriptions()}
        assert {"bind[agent.max_channels]", "bind[agent.max_channel_agents]"} <= names
        await live.watch()._dispatch(
            ConfigChange(
                old=None,
                new=self._cfg(max_channels=7, max_channel_agents=9),
                changed=frozenset({"agent.max_channels"}),
            )
        )
        assert (mgr._max_channels, mgr._max_agents) == (7, 3), "only the touched leaf moves"
        await live.watch()._dispatch(
            ConfigChange(
                old=None,
                new=self._cfg(max_channels=7, max_channel_agents=9),
                changed=frozenset({"agent.max_channel_agents"}),
            )
        )
        assert (mgr._max_channels, mgr._max_agents) == (7, 9)

    @pytest.mark.asyncio
    async def test_the_workflow_service_binds_its_ceiling_in_its_constructor(self) -> None:
        from kiro_crew.workflows.service import WorkflowService

        live.reset_for_tests()
        svc = WorkflowService(sessions=SimpleNamespace(), timeout_secs=600)
        assert "bind[agent.workflow_run_timeout_secs]" in {
            s.name for s in live.watch().subscriptions()
        }
        await live.watch()._dispatch(
            ConfigChange(
                old=None,
                new=self._cfg(workflow_run_timeout_secs=1800),
                changed=frozenset({"agent.workflow_run_timeout_secs"}),
            )
        )
        assert svc.timeout_secs == 1800
        await live.watch()._dispatch(
            ConfigChange(
                old=None,
                new=self._cfg(workflow_run_timeout_secs=900),
                changed=frozenset({"agent.max_channels"}),
            )
        )
        assert svc.timeout_secs == 1800, "an untouched leaf leaves the ceiling alone"

    def test_log_level_applier_is_registered_on_its_own_path(self) -> None:
        from kiro_crew.dashboard.handlers.updates import apply_log_level_from_config

        subs = self._register(SimpleNamespace(workflow_service=None, channel_manager=None))
        sub = subs["agent.log_level"]
        assert sub.prefixes == ("agent.log_level",)
        assert sub.callback() is apply_log_level_from_config

    @pytest.mark.asyncio
    async def test_the_provider_switch_runs_off_the_watcher_cycle_exactly_once(self) -> None:
        """``reload_provider_factory`` clears the session registry and then shuts
        the retired providers down one at a time, which can outlast the applier
        bound. Awaited inside the applier, a timeout would cancel it mid-shutdown
        and the retry would clear the sessions created with the new provider in
        between. So the applier schedules the switch as a tracked task and
        returns at once; the switch itself runs once, with the changed config."""
        release = asyncio.Event()
        entered = asyncio.Event()
        calls: list[Any] = []

        async def reload_provider_factory(*, cfg: Any) -> None:
            calls.append(cfg)
            entered.set()
            # flake-ok: pre-existing unbounded rendezvous inside the stubbed reload, moved verbatim; the test sets release once its bounded asserts pass
            await release.wait()

        slot = SimpleNamespace(model="claude-x", _model_pick_gen=0)
        pushed: list[bool] = []
        state = SimpleNamespace(
            workflow_service=None,
            channel_manager=None,
            sessions=SimpleNamespace(reload_provider_factory=reload_provider_factory),
            _slots={"s1": slot},
            push_slots_update=lambda: pushed.append(True),
            _background_tasks=set(),
        )
        apply = self._register(state)["agent.provider"].callback()
        cfg = KiroCrewConfig()
        with patch("kiro_crew.agent.rebuild_agent_config", lambda: None):
            await await_until(
                asyncio.ensure_future(
                    apply(
                        ConfigChange(
                            old=KiroCrewConfig(), new=cfg, changed=frozenset({"agent.provider"})
                        )
                    )
                )
            )
            assert len(state._background_tasks) == 1, "the switch is a tracked task"
            await await_until(entered)
            assert calls == [cfg], "the switch runs once, with the changed config"
            assert slot.model == "claude-x", "slot models clear only after the reload"
            release.set()
            await asyncio.gather(*state._background_tasks)
        assert calls == [cfg]
        assert slot.model == "" and slot._model_pick_gen == 1 and pushed == [True]
        # Stamped as the runtime's own write, so a reader that treats a moved
        # generation as a person's pick (a crew's triage switch) skips it.
        assert slot._model_runtime_pick_gen == 1
        assert state._background_tasks == set(), "the task drops out of the registry when done"
        # An unrelated leaf schedules nothing.
        await apply(ConfigChange(old=cfg, new=cfg, changed=frozenset({"agent.log_level"})))
        assert state._background_tasks == set()

    @pytest.mark.asyncio
    async def test_the_provider_switch_installs_the_snapshot_current_at_install_time(
        self,
    ) -> None:
        """The task runs off the cycle, so by the time it installs, a later change
        may already be in force; installing the document it was scheduled with
        would revert that. It installs the watcher's current snapshot instead --
        unless that snapshot is torn, which holds defaults."""
        from dataclasses import replace

        calls: list[Any] = []

        async def reload_provider_factory(*, cfg: Any) -> None:
            calls.append(cfg)

        state = SimpleNamespace(
            workflow_service=None,
            channel_manager=None,
            sessions=SimpleNamespace(reload_provider_factory=reload_provider_factory),
            _slots={},
            push_slots_update=lambda: None,
            _background_tasks=set(),
        )
        apply = self._register(state)["agent.provider"].callback()
        scheduled = KiroCrewConfig()
        newer = replace(KiroCrewConfig(), agent=replace(KiroCrewConfig().agent, model="newer"))
        torn = replace(KiroCrewConfig(), _degraded_sections=frozenset({"agent"}))
        change = ConfigChange(
            old=KiroCrewConfig(), new=scheduled, changed=frozenset({"agent.provider"})
        )
        with patch("kiro_crew.agent.rebuild_agent_config", lambda: None):
            with patch("kiro_crew.config.live.snapshot", return_value=newer):
                await apply(change)
                await asyncio.gather(*state._background_tasks)
            with patch("kiro_crew.config.live.snapshot", return_value=torn):
                await apply(change)
                await asyncio.gather(*state._background_tasks)
        assert calls == [newer, scheduled]

    def test_every_applier_is_registered_and_scoped(self) -> None:
        subs = self._register(SimpleNamespace(workflow_service=None, channel_manager=None))
        assert set(subs) == {
            "agent.provider",
            "agent.model",
            "agent.role_models.background",
            "agent.log_level",
        }
        # None of them is a catch-all: an unrelated write must dispatch nothing.
        for sub in subs.values():
            assert sub.prefixes, f"{sub.name} would fire on every reload"

    def test_the_watcher_is_not_an_on_startup_hook_but_is_stopped_on_cleanup(self) -> None:
        """``no-new-work-on-gateway-boot-path``: ``on_startup`` runs before the socket
        binds, so the watcher is started post-bind by ``_kick_config_watch``; only the
        cleanup hook is registered at build time."""
        from aiohttp import web

        from kiro_crew.dashboard.server import _register_config_watch

        app = web.Application()
        _register_config_watch(
            app, SimpleNamespace(workflow_service=None, set_dynamic_cards_enabled=MagicMock()), None
        )
        assert not any("config_watch" in cb.__name__ for cb in app.on_startup)
        assert any(cb.__name__ == "_config_watch_shutdown" for cb in app.on_cleanup)

    @pytest.mark.asyncio
    async def test_the_post_bind_kick_starts_the_watcher_as_a_tracked_task(
        self, cfg_file: Path
    ) -> None:
        from aiohttp import web

        from kiro_crew.dashboard.server import _kick_config_watch, _register_config_watch

        app = web.Application()
        initial = KiroCrewConfig.load()
        state = SimpleNamespace(
            workflow_service=None, _background_tasks=set(), set_dynamic_cards_enabled=MagicMock()
        )
        _register_config_watch(app, state, initial)
        _kick_config_watch(app, state)
        assert len(state._background_tasks) == 1
        await asyncio.gather(*state._background_tasks)
        try:
            assert live.watch().started
            assert live.snapshot() is not None
        finally:
            await live.watch().stop()

    @pytest.mark.asyncio
    async def test_the_kick_primes_the_snapshot_before_yielding(self, cfg_file: Path) -> None:
        """A transport started right after the kick, before the background task has
        run, must already see the boot config: the per-turn readers fall back to a
        disk load on a ``None`` snapshot, and that fallback must be unreachable in a
        running gateway."""
        from aiohttp import web

        from kiro_crew.dashboard.server import _kick_config_watch, _register_config_watch

        app = web.Application()
        initial = KiroCrewConfig.load()
        state = SimpleNamespace(
            workflow_service=None, _background_tasks=set(), set_dynamic_cards_enabled=MagicMock()
        )
        _register_config_watch(app, state, initial)
        _kick_config_watch(app, state)
        try:
            # No await between the kick and this read: the task has not run yet.
            assert not live.watch().started
            assert live.snapshot() is initial
            await asyncio.gather(*state._background_tasks)
            # The deferred start re-primes the same config; the first cycle's
            # reload-and-diff is unchanged.
            assert live.watch().started
            assert live.snapshot() is initial
        finally:
            await live.watch().stop()

    @pytest.mark.asyncio
    async def test_a_file_write_reaches_the_workflow_ceiling(self, cfg_file: Path) -> None:
        """End to end: config.json -> ConfigWatch -> the WorkflowService's own bind."""
        from kiro_crew.workflows.service import WorkflowService

        svc = WorkflowService(sessions=SimpleNamespace(), timeout_secs=600)
        w = live.watch()  # the constructor bound on the singleton
        w.prime(KiroCrewConfig.load())
        assert "bind[agent.workflow_run_timeout_secs]" in {s.name for s in w.subscriptions()}
        _write(cfg_file, {"agent": {"model": "model-a", "workflow_run_timeout_secs": 2400}})
        change = await w.refresh_now()
        assert change is not None and change.touched("agent.workflow_run_timeout_secs")
        assert svc.timeout_secs == 2400
