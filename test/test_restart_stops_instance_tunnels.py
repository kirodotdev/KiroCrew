"""The in-app restart paths run the pre-exec seam before they exec.

``SshTunnelManager.shutdown()`` runs from the aiohttp cleanup hook, which the
two exec-based restarts (the dashboard's ``_restart_gateway`` and the update
coordinator's ``_restart_after_update_claimed``) never reach. The exec keeps
the pid, so a forwarder left running still names the gateway as its parent:
the new image refuses to reclaim it, allocates around its port, and the old
``ssh -N -L`` stays alive unused for the life of the process.

Owners that supervise a child register its stop with ``kiro_crew.pre_exec_stops``
and both restarts call ``run_pre_exec_stops()``. These tests pin the seam itself
(registration, concurrency under one bound, a hung or raising stop), that both
paths call it after the sessions close and before the exec, that the instances
startup hook registers the manager's teardown and its cleanup hook drops it,
that the sleep inhibitor registers its release the same way (the stop cancels
the poll and releases the block, and a restart reaches the exec released),
that the skill-usage ledger's debounced hits reach disk before either exec,
that a real child is reaped through the seam on each path and left running when
the path skips it, that an SSM forwarder's tree kill never blocks the loop, and
that a fresh manager over the same registry (the new image) reconnects on the
recorded port.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from kiro_crew import platform_compat, pre_exec_stops
from kiro_crew.dashboard.handlers import updates
from kiro_crew.pre_exec_stops import register_pre_exec_stop, run_pre_exec_stops
from kiro_crew.slack import gateway as slack_gateway
from kiro_crew.slack.gateway import GatewayOrchestrator
from kiro_crew.testing.ids import UNALLOCATABLE_PID
from kiro_crew.testing.wait import await_until

#: Bound on reaping a test's own child after a signal. It only names a hang: a
#: child that ignores SIGTERM is killed at it, and one that outlives the kill
#: fails the test at that line instead of reaching the worker-killing timeout.
_REAP_TIMEOUT_SECS = 30.0


def _executable(tmp_path) -> str:
    """A real native executable, so the restart paths' interpreter guard passes."""
    source = next(
        (c for c in ("/bin/true", "/usr/bin/true", "/bin/cat") if os.path.exists(c)),
        None,
    )
    if source is None:  # pragma: no cover - a POSIX host without any of these
        pytest.skip("no native binary available to copy")
    target = tmp_path / "python"
    shutil.copyfile(source, target)
    platform_compat.chmod_safe(target, 0o700)
    return str(target)


def _recorder(seen: list[str], label: str) -> AsyncMock:
    async def _record(*_a, **_k):
        seen.append(label)

    return AsyncMock(side_effect=_record)


def _hang(cancelled: asyncio.Event):
    async def _stop():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    return _stop


def _dashboard_state(seen: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        _gateway_restart_in_progress=False,
        push_update_progress=Mock(),
        sessions=SimpleNamespace(close_all=_recorder(seen, "close_all")),
        _mcp_gateway_stop=_recorder(seen, "broker"),
    )


def _dashboard_exec_seams(monkeypatch, seen: list[str]) -> None:
    monkeypatch.setattr(updates, "resolve_restart_launcher", lambda: None)
    monkeypatch.setattr(updates, "reexec_launcher", Mock())
    monkeypatch.setattr(updates, "reexec_python_module", lambda *_a, **_k: seen.append("exec"))


def _orchestrator(seen: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        _pending_update_respawn=None,
        _update_apply_deferred=False,
        _UPDATE_DRAIN_TIMEOUT_SECS=GatewayOrchestrator._UPDATE_DRAIN_TIMEOUT_SECS,
        dashboard_state=SimpleNamespace(push_update_progress=Mock()),
        sessions=SimpleNamespace(
            close_all=_recorder(seen, "close_all"), fence_update_restart=Mock(return_value=True)
        ),
        _drain_update_callback_work=AsyncMock(return_value=True),
        _stop_mcp_broker=_recorder(seen, "broker"),
    )


def _update_exec_seams(monkeypatch, seen: list[str]) -> None:
    monkeypatch.setattr(slack_gateway, "resolve_restart_launcher", lambda: None)
    monkeypatch.setattr(slack_gateway, "flush_breadcrumb_writes", Mock())
    monkeypatch.setattr(slack_gateway.platform_compat, "reexec_launcher", Mock())
    monkeypatch.setattr(
        slack_gateway.platform_compat,
        "reexec_python_module",
        lambda *_a, **_k: seen.append("exec"),
    )


# ── the seam itself ───────────────────────────────────────────────────────


class TestTheSeam:
    @pytest.mark.asyncio
    async def test_an_empty_registry_is_a_no_op(self):
        assert pre_exec_stops._stops == {}
        await run_pre_exec_stops()

    @pytest.mark.asyncio
    async def test_a_registered_stop_runs_until_it_is_unregistered(self):
        seen: list[str] = []
        unregister = register_pre_exec_stop("A", _recorder(seen, "A"))

        await run_pre_exec_stops()
        unregister()
        await run_pre_exec_stops()

        assert seen == ["A"]
        assert pre_exec_stops._stops == {}

    @pytest.mark.asyncio
    async def test_an_older_unregister_keeps_a_newer_stop_under_the_same_name(self):
        seen: list[str] = []
        old_unregister = register_pre_exec_stop("A", _recorder(seen, "old"))
        register_pre_exec_stop("A", _recorder(seen, "new"))

        old_unregister()
        await run_pre_exec_stops()

        assert seen == ["new"]

    @pytest.mark.asyncio
    async def test_the_stops_run_concurrently(self):
        """Each stop waits for the other to start, so a serial run would time out."""
        a_started, b_started = asyncio.Event(), asyncio.Event()
        finished: list[str] = []

        async def _a():
            a_started.set()
            await b_started.wait()
            finished.append("A")

        async def _b():
            b_started.set()
            await a_started.wait()
            finished.append("B")

        register_pre_exec_stop("A", _a)
        register_pre_exec_stop("B", _b)

        await run_pre_exec_stops(timeout=30)

        assert sorted(finished) == ["A", "B"]

    @pytest.mark.asyncio
    async def test_a_hung_stop_is_capped_and_the_others_still_finish(self, caplog):
        cancelled = asyncio.Event()
        seen: list[str] = []
        register_pre_exec_stop("Hung", _hang(cancelled))
        register_pre_exec_stop("Quick", _recorder(seen, "quick"))

        with caplog.at_level(logging.WARNING, logger="kiro_crew.pre_exec_stops"):
            await run_pre_exec_stops(timeout=0.05)

        assert cancelled.is_set(), "the hung stop was not cancelled at the bound"
        assert seen == ["quick"]
        assert "Hung stop before restart did not finish" in caplog.text

    @pytest.mark.asyncio
    async def test_two_hung_stops_share_the_one_bound(self):
        first, second = asyncio.Event(), asyncio.Event()
        register_pre_exec_stop("First", _hang(first))
        register_pre_exec_stop("Second", _hang(second))

        await run_pre_exec_stops(timeout=0.05)

        assert first.is_set() and second.is_set()

    @pytest.mark.asyncio
    async def test_a_raising_stop_is_swallowed_and_logged(self, caplog):
        seen: list[str] = []

        def _sync_raise():
            raise ValueError("raised before any await")

        register_pre_exec_stop("Async", AsyncMock(side_effect=RuntimeError("wedged")))
        register_pre_exec_stop("Sync", _sync_raise)
        register_pre_exec_stop("Fine", _recorder(seen, "fine"))

        with caplog.at_level(logging.WARNING, logger="kiro_crew.pre_exec_stops"):
            await run_pre_exec_stops()  # must not raise

        assert seen == ["fine"]
        assert "Async stop before restart failed" in caplog.text
        assert "Sync stop before restart failed" in caplog.text


# ── both exec paths call the seam ─────────────────────────────────────────


class TestTheDashboardRestart:
    @pytest.mark.asyncio
    async def test_runs_the_seam_after_the_sessions_and_before_the_exec(
        self, monkeypatch, tmp_path
    ):
        seen: list[str] = []
        _dashboard_exec_seams(monkeypatch, seen)
        register_pre_exec_stop("Child", _recorder(seen, "child"))

        restarted = await updates._restart_gateway(
            _dashboard_state(seen), resolver=lambda: _executable(tmp_path)
        )

        assert restarted is True
        assert seen == ["close_all", "broker", "child", "exec"]

    @pytest.mark.asyncio
    async def test_a_refused_restart_leaves_the_children_up(self, monkeypatch, tmp_path):
        """The interpreter guard refuses before the drain; the gateway keeps serving."""
        seen: list[str] = []
        _dashboard_exec_seams(monkeypatch, seen)
        register_pre_exec_stop("Child", _recorder(seen, "child"))

        restarted = await updates._restart_gateway(
            _dashboard_state(seen), resolver=lambda: str(tmp_path / "pruned")
        )

        assert restarted is False
        assert "child" not in seen

    @pytest.mark.asyncio
    async def test_a_stop_that_raises_still_reaches_the_exec(self, monkeypatch, tmp_path):
        seen: list[str] = []
        _dashboard_exec_seams(monkeypatch, seen)
        register_pre_exec_stop("Child", AsyncMock(side_effect=RuntimeError("ssh child wedged")))

        restarted = await updates._restart_gateway(
            _dashboard_state(seen), resolver=lambda: _executable(tmp_path)
        )

        assert restarted is True
        assert seen == ["close_all", "broker", "exec"]


class TestTheUpdateRestart:
    @pytest.mark.asyncio
    async def test_runs_the_seam_after_the_sessions_and_before_the_exec(
        self, monkeypatch, tmp_path
    ):
        seen: list[str] = []
        _update_exec_seams(monkeypatch, seen)
        register_pre_exec_stop("Child", _recorder(seen, "child"))

        await GatewayOrchestrator._restart_after_update(
            _orchestrator(seen), lambda: _executable(tmp_path)
        )

        assert seen == ["close_all", "broker", "child", "exec"]

    @pytest.mark.asyncio
    async def test_a_deferred_restart_leaves_the_children_up(self, monkeypatch, tmp_path):
        """A pre-fence drain that does not finish defers with every session open."""
        seen: list[str] = []
        _update_exec_seams(monkeypatch, seen)
        register_pre_exec_stop("Child", _recorder(seen, "child"))
        orch = _orchestrator(seen)
        orch._UPDATE_DRAIN_TIMEOUT_SECS = 0.01
        orch._drain_update_callback_work = AsyncMock(return_value=False)

        await GatewayOrchestrator._restart_after_update(orch, lambda: _executable(tmp_path))

        assert orch._update_apply_deferred is True
        assert seen == []

    @pytest.mark.asyncio
    async def test_a_stop_that_hangs_is_abandoned_and_the_exec_still_runs(
        self, monkeypatch, tmp_path
    ):
        """Bounded: the hung stop is cancelled at the ceiling, never awaited out."""
        seen: list[str] = []
        _update_exec_seams(monkeypatch, seen)
        monkeypatch.setattr(pre_exec_stops, "PRE_EXEC_STOP_TIMEOUT_SECS", 0.05)
        cancelled = asyncio.Event()
        register_pre_exec_stop("Child", _hang(cancelled))

        await GatewayOrchestrator._restart_after_update(
            _orchestrator(seen), lambda: _executable(tmp_path)
        )

        assert cancelled.is_set()
        assert seen == ["close_all", "broker", "exec"]


# ── the skill-usage ledger flushes before the exec ───────────────────────


class TestTheSkillUsageLedgerJoinsTheSeam:
    """``os.execv`` kills the ledger's daemon flush thread, like ``os._exit``.

    A hit ``record()`` accepted inside the 60 s debounce window must be on disk
    before either in-place restart replaces the process image.
    """

    @staticmethod
    def _debounced_ledger(monkeypatch, tmp_path):
        import weakref

        from kiro_crew import skill_usage

        monkeypatch.setattr(skill_usage, "_live_ledgers", weakref.WeakSet())
        path = tmp_path / "skill-usage.json"
        ledger = skill_usage.SkillUsageLedger(path)
        ledger._last_flush = time.time() + 3600  # inside the window: no write yet
        ledger.record("alpha")
        assert not path.exists(), "precondition: the hit is only in memory"
        register_pre_exec_stop("Skill-usage ledger", skill_usage.flush_all_before_exec)
        return skill_usage, path, ledger  # held, as the loader holds it

    @pytest.mark.asyncio
    async def test_the_dashboard_restart_persists_the_window(self, monkeypatch, tmp_path):
        seen: list[str] = []
        _dashboard_exec_seams(monkeypatch, seen)
        skill_usage, path, ledger = self._debounced_ledger(monkeypatch, tmp_path)

        restarted = await updates._restart_gateway(
            _dashboard_state(seen), resolver=lambda: _executable(tmp_path)
        )

        assert restarted is True
        assert seen[-1] == "exec"
        assert skill_usage.SkillUsageLedger(path).score("alpha")[0] == 1.0
        assert not ledger.dirty

    @pytest.mark.asyncio
    async def test_the_update_restart_persists_the_window(self, monkeypatch, tmp_path):
        seen: list[str] = []
        _update_exec_seams(monkeypatch, seen)
        skill_usage, path, ledger = self._debounced_ledger(monkeypatch, tmp_path)

        await GatewayOrchestrator._restart_after_update(
            _orchestrator(seen), lambda: _executable(tmp_path)
        )

        assert seen[-1] == "exec"
        assert skill_usage.SkillUsageLedger(path).score("alpha")[0] == 1.0
        assert not ledger.dirty

    @pytest.mark.asyncio
    async def test_a_failed_flush_is_logged_and_the_exec_still_runs(
        self, monkeypatch, tmp_path, caplog
    ):
        from kiro_crew import skill_usage

        seen: list[str] = []
        _dashboard_exec_seams(monkeypatch, seen)
        monkeypatch.setattr(skill_usage, "flush_all_for_shutdown", lambda: False)
        register_pre_exec_stop("Skill-usage ledger", skill_usage.flush_all_before_exec)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.skill_usage"):
            restarted = await updates._restart_gateway(
                _dashboard_state(seen), resolver=lambda: _executable(tmp_path)
            )

        assert restarted is True
        assert seen[-1] == "exec"
        assert "did not fully flush before restart" in caplog.text


# ── the restart claim covers every moment a connect could slip in ────────


class TestTheRestartClaimSpansTheWholeSequence:
    """``_gateway_restart_in_progress`` is what the connect route and the startup
    revive read, so it must be set from before the seam to the exec on both paths."""

    @pytest.mark.asyncio
    async def test_the_dashboard_restart_holds_it_through_the_seam_and_the_exec(
        self, monkeypatch, tmp_path
    ):
        seen: list[str] = []
        state = _dashboard_state(seen)
        readings: list[tuple[str, bool]] = []

        def _note(label: str) -> None:
            readings.append((label, state._gateway_restart_in_progress))

        _dashboard_exec_seams(monkeypatch, seen)
        monkeypatch.setattr(updates, "reexec_python_module", lambda *_a, **_k: _note("exec"))

        async def _stop():
            _note("seam")

        register_pre_exec_stop("Child", _stop)

        await updates._restart_gateway(state, resolver=lambda: _executable(tmp_path))

        assert readings == [("seam", True), ("exec", True)]
        assert state._gateway_restart_in_progress is False

    @pytest.mark.asyncio
    async def test_the_update_restart_holds_it_through_every_drain_the_seam_and_the_exec(
        self, monkeypatch, tmp_path
    ):
        seen: list[str] = []
        orch = _orchestrator(seen)
        state = orch.dashboard_state
        readings: list[tuple[str, bool]] = []

        def _note(label: str) -> None:
            readings.append((label, getattr(state, "_gateway_restart_in_progress", False)))

        async def _drain(*_a, **_k):
            _note("drain")
            return True

        orch._drain_update_callback_work = AsyncMock(side_effect=_drain)
        _update_exec_seams(monkeypatch, seen)
        monkeypatch.setattr(
            slack_gateway.platform_compat, "reexec_python_module", lambda *_a, **_k: _note("exec")
        )

        async def _stop():
            _note("seam")

        register_pre_exec_stop("Child", _stop)

        await GatewayOrchestrator._restart_after_update(orch, lambda: _executable(tmp_path))

        assert [label for label, _ in readings] == ["drain", "seam", "drain", "exec"]
        assert all(held for _, held in readings), readings
        assert state._gateway_restart_in_progress is False


# ── the sleep inhibitor is the seam's second owner ────────────────────────


class _Inhibitor:
    """Stand-in for ``power.SleepInhibitor`` that records the states asked of it."""

    def __init__(self) -> None:
        self.states: list[bool] = []

    def set_active(self, active: bool) -> None:
        self.states.append(active)


class TestTheSleepInhibitorJoinsTheSeam:
    """``caffeinate -w`` / ``systemd-inhibit`` watch the gateway pid, which an
    in-place exec keeps, so the helper has to be released before the exec.

    The poll's predicate sets an event once the block is engaged, so each test
    starts from an engaged block on a real signal. Every wait on the poll task
    is bounded, so a regression fails at its line.
    """

    @staticmethod
    @contextlib.asynccontextmanager
    async def _armed(monkeypatch):
        from kiro_crew.dashboard import server

        inhibitor = _Inhibitor()
        engaged = asyncio.Event()

        async def _should(_state, _port) -> bool:
            if inhibitor.states and inhibitor.states[-1] is True:
                engaged.set()
            return True

        monkeypatch.setattr(server, "SleepInhibitor", lambda: inhibitor)
        monkeypatch.setattr(server, "_should_prevent_sleep", _should)
        monkeypatch.setattr(server, "_PREVENT_SLEEP_POLL_INTERVAL_SECS", 0)
        state = SimpleNamespace(_background_tasks=set())
        server._arm_prevent_sleep_poll(state, 0)
        task = state._prevent_sleep_task
        try:
            await await_until(engaged, what="the poll engaging the block")
            yield server, state, inhibitor
        finally:
            task.cancel()
            await await_until(
                asyncio.gather(task, return_exceptions=True), what="the poll unwinding"
            )
            state._prevent_sleep_unregister()

    @pytest.mark.asyncio
    async def test_arming_registers_the_stop_and_cleanup_drops_it(self, monkeypatch):
        from aiohttp import web

        async with self._armed(monkeypatch) as (server, state, _):
            app = web.Application()
            server._register_prevent_sleep_shutdown(app, state)
            assert list(pre_exec_stops._stops) == ["Sleep inhibitor"]

            for hook in list(app.on_cleanup):
                await hook(app)

            assert pre_exec_stops._stops == {}

    @pytest.mark.asyncio
    async def test_the_seam_cancels_the_poll_before_it_can_engage_again(self, monkeypatch):
        async with self._armed(monkeypatch) as (_, state, _inhibitor):
            task = state._prevent_sleep_task

            await run_pre_exec_stops()

            assert task.cancelling() or task.done(), "the seam left the poll running"
            await await_until(
                asyncio.gather(task, return_exceptions=True), what="the poll unwinding"
            )
            assert task.cancelled()

    @pytest.mark.asyncio
    async def test_the_seam_releases_a_block_the_poll_no_longer_owns(self, monkeypatch):
        """A poll that already ended cannot release on its way out, so the stop must."""
        async with self._armed(monkeypatch) as (_, state, inhibitor):
            task = state._prevent_sleep_task
            task.cancel()
            await await_until(
                asyncio.gather(task, return_exceptions=True), what="the poll unwinding"
            )
            inhibitor.set_active(True)

            await run_pre_exec_stops()

            assert inhibitor.states[-1] is False

    @pytest.mark.asyncio
    async def test_a_restart_releases_the_block_before_the_exec(self, monkeypatch, tmp_path):
        async with self._armed(monkeypatch) as (_, _state, inhibitor):
            seen: list[str] = []
            _dashboard_exec_seams(monkeypatch, seen)
            monkeypatch.setattr(
                updates,
                "reexec_python_module",
                lambda *_a, **_k: seen.append(f"exec:{inhibitor.states[-1]}"),
            )

            await updates._restart_gateway(
                _dashboard_state(seen), resolver=lambda: _executable(tmp_path)
            )

        assert seen[-1] == "exec:False", "the block was held, or engaged again, at the exec"


# ── the browser view is the seam's third owner ────────────────────────────


class TestTheBrowserViewJoinsTheSeam:
    """`playwright-cli show` is spawned detached (``start_new_session``), so an
    in-place restart's exec would leave it running parented to the new image,
    which then starts a second tree on the next view request. The on_cleanup
    hook that reaps it is skipped by the exec, so the startup hook registers the
    same reap with the seam and the cleanup hook drops it."""

    @pytest.mark.asyncio
    async def test_startup_registers_the_view_stop_and_cleanup_drops_it(self, monkeypatch):
        from aiohttp import web

        from kiro_crew.dashboard import server

        monkeypatch.setattr(server.browser_cli_view, "stop", Mock())
        monkeypatch.setattr(server.browser_cli_launcher, "reclaim_stranded", Mock())
        monkeypatch.setattr(server.browser_cli_launcher, "close_all", Mock())
        monkeypatch.setattr(server.handlers, "close_relay_client", AsyncMock())
        state = SimpleNamespace(_background_tasks=set())
        app = web.Application()
        server._register_browser_view_cleanup(app, state)
        app.freeze()

        await app.on_startup.send(app)
        for task in list(state._background_tasks):
            await task
        assert list(pre_exec_stops._stops) == ["Browser view"]

        await app.on_cleanup.send(app)
        assert pre_exec_stops._stops == {}

    @pytest.mark.asyncio
    async def test_the_seam_reaps_the_view_child(self, monkeypatch):
        """The registered stop calls view.stop(), the only reaper of the child."""
        from aiohttp import web

        from kiro_crew.dashboard import server

        view_stop = Mock()
        monkeypatch.setattr(server.browser_cli_view, "stop", view_stop)
        monkeypatch.setattr(server.browser_cli_launcher, "reclaim_stranded", Mock())
        state = SimpleNamespace(_background_tasks=set())
        app = web.Application()
        server._register_browser_view_cleanup(app, state)
        app.freeze()
        await app.on_startup.send(app)
        for task in list(state._background_tasks):
            await task

        await run_pre_exec_stops()

        view_stop.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_a_restart_reaps_the_view_before_the_exec(self, monkeypatch, tmp_path):
        from aiohttp import web

        from kiro_crew.dashboard import server

        order: list[str] = []
        monkeypatch.setattr(server.browser_cli_view, "stop", lambda: order.append("view"))
        monkeypatch.setattr(server.browser_cli_launcher, "reclaim_stranded", Mock())
        state = SimpleNamespace(_background_tasks=set())
        app = web.Application()
        server._register_browser_view_cleanup(app, state)
        app.freeze()
        await app.on_startup.send(app)
        for task in list(state._background_tasks):
            await task

        _dashboard_exec_seams(monkeypatch, order)
        monkeypatch.setattr(updates, "reexec_python_module", lambda *_a, **_k: order.append("exec"))

        await updates._restart_gateway(
            _dashboard_state(order), resolver=lambda: _executable(tmp_path)
        )

        assert order == ["close_all", "broker", "view", "exec"]


# ── the instances hooks register and drop the manager's teardown ──────────


class TestTheInstancesHooksRegisterTheTeardown:
    @pytest.mark.asyncio
    async def test_startup_registers_shutdown_and_cleanup_drops_it(self, tmp_path, monkeypatch):
        from aiohttp import web

        from kiro_crew.config import loader
        from kiro_crew.dashboard import server
        from kiro_crew.dashboard.state import DashboardState

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        (tmp_path / "config.json").write_text('{"instances": {"enabled": true}}', encoding="utf-8")
        loader._invalidate_config_cache()

        async def _no_revive(_registry, _manager, _state=None):
            return None

        monkeypatch.setattr(server, "_retake_hops_then_revive", _no_revive)
        state = DashboardState(sessions=Mock(), crons=Mock(), lessons=Mock(), start_time=0.0)
        app = web.Application()
        server._register_instances_hooks(app, state, port=7777)
        app.freeze()

        await app.on_startup.send(app)
        for task in list(state._background_tasks):
            await task

        manager = state.instances_manager
        assert manager is not None
        assert pre_exec_stops._stops == {"Instance tunnels": manager.shutdown}

        await app.on_cleanup.send(app)

        assert pre_exec_stops._stops == {}

    @pytest.mark.asyncio
    async def test_disabled_instances_register_nothing(self, tmp_path, monkeypatch):
        from aiohttp import web

        from kiro_crew.config import loader
        from kiro_crew.dashboard import server
        from kiro_crew.dashboard.state import DashboardState

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        (tmp_path / "config.json").write_text('{"instances": {"enabled": false}}', encoding="utf-8")
        loader._invalidate_config_cache()
        state = DashboardState(sessions=Mock(), crons=Mock(), lessons=Mock(), start_time=0.0)
        app = web.Application()
        server._register_instances_hooks(app, state, port=7777)
        app.freeze()

        await app.on_startup.send(app)
        assert pre_exec_stops._stops == {}
        await app.on_cleanup.send(app)


# ── a real forwarder child, through the real manager and both paths ───────


def _pin_port_probe(monkeypatch) -> None:
    """Loopback probes answer "free" in both namespaces that probe."""
    import kiro_crew.instances.port_allocator as pa
    import kiro_crew.instances.ssh_tunnel_manager as stm

    monkeypatch.setattr(stm, "_is_port_free", lambda port, host="127.0.0.1": True)
    monkeypatch.setattr(pa, "_is_port_free", lambda port, host="127.0.0.1": True)


class _FakeTunnel:
    """A forwarder that starts and stops without spawning anything."""

    def __init__(self, iid, ssh_host, lp, rp, *, transport="ssh", **_kw):
        from kiro_crew.instances.ssh_tunnel_manager import TunnelStatus

        self.pid = None
        self.transport = transport
        self.stopped = False
        self.status = TunnelStatus(instance_id=iid, local_port=lp, remote_port=rp)

    async def start(self):
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        self.status.state = TunnelState.CONNECTED
        return True

    async def stop(self):
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        self.stopped = True
        self.status.state = TunnelState.STOPPED


async def _mint(host, **_kw):
    return "TOKEN"


def _child_tunnel_factory(cwd):
    """A forwarder whose ``start`` spawns a real long-lived child process."""

    class _ChildTunnel(_FakeTunnel):
        proc: asyncio.subprocess.Process | None = None

        async def start(self):
            self.proc = await asyncio.create_subprocess_exec(
                sys.executable, "-c", "import time; time.sleep(300)", cwd=str(cwd)
            )
            self.pid = self.proc.pid
            return await super().start()

        async def stop(self):
            if self.proc is not None and self.proc.returncode is None:
                self.proc.terminate()
                try:
                    await asyncio.wait_for(self.proc.wait(), timeout=_REAP_TIMEOUT_SECS)
                except asyncio.TimeoutError:
                    self.proc.kill()
                    await asyncio.wait_for(self.proc.wait(), timeout=_REAP_TIMEOUT_SECS)
            await super().stop()

    return _ChildTunnel


class TestARealForwarderChildIsReaped:
    """The real ``SshTunnelManager.shutdown`` reaches a real child via the seam.

    Each path's mutation pin replaces that path's ``run_pre_exec_stops`` with a
    no-op, the shape of the bug: the exec is reached and the child still runs.
    """

    @pytest.fixture(autouse=True)
    def _free_ports(self, _floor_monkeypatch):
        _pin_port_probe(_floor_monkeypatch)

    async def _connected_child(self, tmp_path):
        from kiro_crew.instances.registry import InstancesRegistry
        from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

        reg = InstancesRegistry(path=tmp_path / "instances.json")
        reg.add(name="A", ssh_host="a-alias", instance_id="a")
        manager = SshTunnelManager(
            reg,
            base_port=53700,
            mint_token=_mint,
            tunnel_factory=_child_tunnel_factory(tmp_path),
        )
        await manager.connect("a")
        proc = manager._tunnels["a"].proc
        assert proc is not None and proc.returncode is None
        register_pre_exec_stop("Instance tunnels", manager.shutdown)
        return manager, proc

    async def _restart(self, path, monkeypatch, tmp_path, seen):
        if path == "dashboard":
            _dashboard_exec_seams(monkeypatch, seen)
            await updates._restart_gateway(
                _dashboard_state(seen), resolver=lambda: _executable(tmp_path)
            )
        else:
            _update_exec_seams(monkeypatch, seen)
            await GatewayOrchestrator._restart_after_update(
                _orchestrator(seen), lambda: _executable(tmp_path)
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["dashboard", "update"])
    async def test_the_restart_reaps_the_child_before_the_exec(self, path, monkeypatch, tmp_path):
        manager, proc = await self._connected_child(tmp_path)
        seen: list[str] = []
        try:
            await self._restart(path, monkeypatch, tmp_path, seen)

            assert seen[-1] == "exec"
            assert proc.returncode is not None, "the forwarder child outlived the restart"
        finally:
            await manager.shutdown()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["dashboard", "update"])
    async def test_a_path_that_skips_the_seam_leaves_the_child_running(
        self, path, monkeypatch, tmp_path
    ):
        manager, proc = await self._connected_child(tmp_path)
        seen: list[str] = []

        async def _skipped(*_a, **_k):
            return None

        owner = updates if path == "dashboard" else slack_gateway
        monkeypatch.setattr(owner, "run_pre_exec_stops", _skipped)
        try:
            await self._restart(path, monkeypatch, tmp_path, seen)

            assert seen[-1] == "exec"
            assert proc.returncode is None, "control: without the seam the child survives"
        finally:
            await manager.shutdown()
        assert proc.returncode is not None


# ── the SSM tree kill runs off the loop ───────────────────────────────────


class TestTheTreeKillDoesNotBlockTheLoop:
    """``_terminate`` awaits the async tree kill, never the synchronous one.

    On Windows the synchronous form is a ``taskkill /T`` subprocess with its own
    timeout, run on the loop: no heartbeat runs and the seam's ``wait_for``
    cannot fire until it returns. The fakes stand in for both forms: the async
    one parks without blocking, and the synchronous one records that it was
    called, which is the regression.
    """

    @pytest.mark.asyncio
    async def test_the_bound_fires_and_the_loop_keeps_running(self, monkeypatch):
        from kiro_crew.instances.ssh_tunnel_manager import _SshTunnel

        sync_calls: list[int] = []
        kill_started = asyncio.Event()
        kill_cancelled = asyncio.Event()

        def _sync_tree_kill(pid, sig):
            sync_calls.append(sig)
            return True

        async def _slow_async_tree_kill(pid, sig):
            kill_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                kill_cancelled.set()
                raise
            return True

        monkeypatch.setattr(platform_compat, "kill_process_tree", _sync_tree_kill)
        monkeypatch.setattr(platform_compat, "kill_process_tree_async", _slow_async_tree_kill)

        class _LiveProc:
            pid = UNALLOCATABLE_PID
            returncode = None

            def terminate(self):
                raise AssertionError("the tree kill was delivered; no single kill")

            def kill(self):
                raise AssertionError("the tree kill was delivered; no single kill")

            async def wait(self):
                await asyncio.Event().wait()

        tunnel = _SshTunnel(
            "i-1", "", 53998, 7777, transport="ssm", ssm_target="i-0123456789abcdef0"
        )
        tunnel._proc = _LiveProc()  # type: ignore[assignment]

        beats = 0

        async def _heartbeat():
            nonlocal beats
            await kill_started.wait()
            while not kill_cancelled.is_set():
                beats += 1
                await asyncio.sleep(0)

        heart = asyncio.create_task(_heartbeat())
        register_pre_exec_stop("Instance tunnels", tunnel._terminate)
        await run_pre_exec_stops(timeout=0.05)
        if not heart.done():  # the regression: the async kill never started
            heart.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heart

        assert sync_calls == [], "the loop ran the synchronous tree kill"
        assert kill_cancelled.is_set(), "the seam's bound never fired"
        assert beats > 0, "no other task ran while the tree kill was pending"


# ── the new image reconnects on the recorded port ─────────────────────────


class TestTheNewImageReconnectsOnTheRecordedPort:
    @pytest.fixture(autouse=True)
    def _free_ports(self, _floor_monkeypatch):
        _pin_port_probe(_floor_monkeypatch)

    def _manager(self, reg, base_port):
        from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

        return SshTunnelManager(
            reg, base_port=base_port, mint_token=_mint, tunnel_factory=_FakeTunnel
        )

    @pytest.mark.asyncio
    async def test_revive_after_the_pre_exec_teardown_reuses_the_port(self, tmp_path):
        from kiro_crew.instances.registry import InstancesRegistry
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        reg = InstancesRegistry(path=tmp_path / "instances.json")
        reg.add(name="A", ssh_host="a-alias", instance_id="a")
        reg.add(name="B", ssh_host="b-alias", instance_id="b")
        old = self._manager(reg, 53600)
        base = old._allocator.base_port
        assert (await old.connect("a")).local_port == base
        assert (await old.connect("b")).local_port == base + 1
        tunnel_b = old._tunnels["b"]
        # A chained credential this gateway minted still routes to a hop port.
        hop = base + 2
        reg.lend_hop(hop, time.time() + 3600)

        register_pre_exec_stop("Instance tunnels", old.shutdown)
        await run_pre_exec_stops()

        assert tunnel_b.stopped, "the forwarder must be stopped before the exec"
        assert old.status_all() == {}
        assert reg.get("b").was_connected is True
        assert reg.get("b").local_port == base + 1
        assert hop in reg.live_hop_leases(), "the teardown must not drop a live lease"

        # The new image: a fresh manager over the same registry. B first, the
        # order that would hand it A's lower port under a first-free allocator.
        new = self._manager(reg, base)
        st_b = await new.connect("b")
        assert st_b.state == TunnelState.CONNECTED
        assert st_b.local_port == base + 1
        assert (await new.connect("a")).local_port == base
        # A crew with no recorded port takes the next free one, never the leased
        # hop port that first-free would otherwise hand it.
        reg.add(name="C", ssh_host="c-alias", instance_id="c")
        st_c = await new.connect("c")
        assert st_c.state == TunnelState.CONNECTED
        assert st_c.local_port == base + 3
        assert hop in reg.live_hop_leases()
        await new.shutdown()
