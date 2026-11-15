"""The gateway's exit path stops what a skipped dashboard cleanup hook would have.

A graceful shutdown that runs out of its budget never reaches the dashboard's
``on_cleanup`` hooks, or cancels one mid-stop, and ``_shutdown_and_exit`` then
ends the process with ``os._exit``. The supervised children those hooks stop (the
browser harness daemon, the instance tunnels) register the same stop with
:mod:`kiro_crew.pre_exec_stops`, so the exit path runs them there. These tests
drive the real startup and cleanup hooks with the supervised work replaced, and
replace ``os._exit`` so the test process never exits.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

from kiro_crew import cli as cli_mod
from kiro_crew import eventlog_hooks, pre_exec_stops
from kiro_crew import session as session_mod
from kiro_crew import skill_usage
from kiro_crew.dashboard import server
from kiro_crew.dashboard.handlers import browser_harness_status
from kiro_crew.slack import gateway as gw
from kiro_crew.testing.wait import await_until, finished


class _NoExitOs:
    """The facade's ``os`` with ``_exit`` recorded instead of ending the process."""

    def __init__(self) -> None:
        self._exit = MagicMock(name="os._exit")

    def __getattr__(self, name: str) -> Any:
        import os

        return getattr(os, name)


@pytest.fixture
def no_exit(monkeypatch: pytest.MonkeyPatch) -> _NoExitOs:
    """Every exit-path effect outside the pre-exec seam replaced; the exit recorded."""
    found = _NoExitOs()
    monkeypatch.setattr(gw, "os", found)
    monkeypatch.setattr(gw, "drain_sign_in_refresher", AsyncMock())
    monkeypatch.setattr(gw, "flush_audit_queue_before_hard_exit", AsyncMock())
    monkeypatch.setattr(cli_mod, "drain_log_queue_before_hard_exit", AsyncMock())
    monkeypatch.setattr(session_mod, "cleanup_orphaned_sessions", MagicMock())
    monkeypatch.setattr(eventlog_hooks, "drain_for_shutdown", MagicMock(return_value=True))
    monkeypatch.setattr(skill_usage, "flush_all_for_shutdown", MagicMock(return_value=True))
    from kiro_crew.crew_log import emit as crew_log_emit
    from kiro_crew.dashboard import crash_dump_store
    from kiro_crew.shared_crew import admission_gate

    monkeypatch.setattr(crew_log_emit, "drain_for_shutdown", MagicMock(return_value=True))
    monkeypatch.setattr(admission_gate, "shutdown_cleanup", AsyncMock(return_value=0))
    monkeypatch.setattr(crash_dump_store, "mark_clean_exit", AsyncMock())
    return found


def _orchestrator(shutdown: AsyncMock) -> Any:
    orch = gw.GatewayOrchestrator.__new__(gw.GatewayOrchestrator)
    orch.dashboard_state = None
    orch._dashboard_port = 0
    orch._sign_in_refresher_task = None
    orch._shutdown = shutdown
    return orch


def _hook(hooks: Any, name: str) -> Any:
    """The one hook called *name* in an app signal list (aiohttp adds its own)."""
    (found,) = [hook for hook in hooks if getattr(hook, "__name__", "") == name]
    return found


class _Supervisor:
    """The harness supervisor: records each stop, and can hold one until released."""

    def __init__(self, exits: MagicMock) -> None:
        self._exits = exits
        self.stops: list[bool] = []
        self.hold = False
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def stop(self) -> None:
        # Whether the hard exit had already happened when this stop ran.
        self.stops.append(self._exits.called)
        if self.hold:
            self.hold = False
            self.entered.set()
            await self.release.wait()


async def _start_harness_lifecycle(
    monkeypatch: pytest.MonkeyPatch, supervisor: _Supervisor
) -> web.Application:
    """Register and run the real harness startup hook; return the app."""

    async def _auto_setup(_state: Any, **_kw: Any) -> str:
        return "ready"

    monkeypatch.setattr(
        "kiro_crew.browser_harness.supervisor.get_supervisor", lambda _home: supervisor
    )
    monkeypatch.setattr(browser_harness_status, "auto_setup_browser_harness", _auto_setup)
    monkeypatch.setattr(server.handlers, "stop_browser_harness_install", AsyncMock())
    state = SimpleNamespace(_background_tasks=set())
    app = web.Application()
    app["state"] = state  # the key the hook reads
    server._register_browser_harness_lifecycle(app, state)  # type: ignore[arg-type]
    await _hook(app.on_startup, "_harness_startup")(app)
    # The startup reclaim: the one stop that runs before any shutdown.
    await await_until(asyncio.gather(*list(state._background_tasks)), what="the startup reclaim")
    assert supervisor.stops == [False]
    supervisor.stops.clear()
    return app


@pytest.mark.asyncio
async def test_a_timed_out_shutdown_still_stops_the_browser_harness(monkeypatch, no_exit) -> None:
    supervisor = _Supervisor(no_exit._exit)
    await _start_harness_lifecycle(monkeypatch, supervisor)

    # The budget ran out before the web server reached any cleanup hook.
    await _orchestrator(AsyncMock(side_effect=asyncio.TimeoutError()))._shutdown_and_exit()

    assert supervisor.stops == [False], "the harness was not stopped before the hard exit"
    no_exit._exit.assert_called_once_with(0)


@pytest.mark.asyncio
async def test_a_cleanup_hook_cut_mid_stop_leaves_the_stop_to_the_exit_path(
    monkeypatch, no_exit
) -> None:
    supervisor = _Supervisor(no_exit._exit)
    app = await _start_harness_lifecycle(monkeypatch, supervisor)
    cleanup = _hook(app.on_cleanup, "_harness_shutdown")
    supervisor.hold = True
    hook = asyncio.ensure_future(cleanup(app))
    await await_until(supervisor.entered, what="the harness stop starting")
    # The graceful bound cancels the shutdown, and the hook with it, mid-stop.
    hook.cancel()
    with pytest.raises(asyncio.CancelledError):
        await hook

    await _orchestrator(AsyncMock(side_effect=asyncio.TimeoutError()))._shutdown_and_exit()

    assert supervisor.stops == [False, False], "the cut stop was not run again before the exit"
    no_exit._exit.assert_called_once_with(0)


@pytest.mark.asyncio
async def test_a_cleanup_hook_that_finished_leaves_nothing_to_stop_again(
    monkeypatch, no_exit
) -> None:
    supervisor = _Supervisor(no_exit._exit)
    app = await _start_harness_lifecycle(monkeypatch, supervisor)
    cleanup = _hook(app.on_cleanup, "_harness_shutdown")
    await finished(cleanup(app), "the harness cleanup hook")
    assert supervisor.stops == [False]

    await _orchestrator(AsyncMock())._shutdown_and_exit()

    assert supervisor.stops == [False], "a stop the hook already ran was run again"
    assert "Browser Harness" not in pre_exec_stops._stops
    no_exit._exit.assert_called_once_with(0)


@pytest.mark.asyncio
async def test_an_instance_teardown_cut_mid_stop_leaves_it_to_the_exit_path(
    monkeypatch, no_exit
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    shutdowns: list[bool] = []

    class _Manager:
        def __init__(self, *_a: Any, **_kw: Any) -> None:
            pass

        async def shutdown(self) -> None:
            shutdowns.append(no_exit._exit.called)
            if len(shutdowns) == 1:
                entered.set()
                await release.wait()

    async def _no_revive(*_a: Any, **_kw: Any) -> None:
        return None

    # The shipped defaults with the opt-in turned on.
    config = server.KiroCrewConfig()
    config.instances.enabled = True
    monkeypatch.setattr(server, "KiroCrewConfig", SimpleNamespace(load=lambda: config))
    monkeypatch.setattr(server, "InstancesRegistry", lambda: object())
    monkeypatch.setattr(server, "SshTunnelManager", _Manager)
    monkeypatch.setattr(server, "_retake_hops_then_revive", _no_revive)
    state = SimpleNamespace(_background_tasks=set())
    app = web.Application()
    server._register_instances_hooks(app, state, 0)  # type: ignore[arg-type]
    await _hook(app.on_startup, "_instances_startup")(app)
    await await_until(asyncio.gather(*list(state._background_tasks)), what="the instances startup")
    instances_shutdown = _hook(app.on_cleanup, "_instances_shutdown")
    hook = asyncio.ensure_future(instances_shutdown(app))
    await await_until(entered, what="the instance teardown starting")
    hook.cancel()
    with pytest.raises(asyncio.CancelledError):
        await hook

    await _orchestrator(AsyncMock(side_effect=asyncio.TimeoutError()))._shutdown_and_exit()

    assert shutdowns == [False, False], "the cut teardown was not run again before the exit"
    no_exit._exit.assert_called_once_with(0)


@pytest.mark.asyncio
async def test_a_failing_stop_is_logged_as_before_the_exit_and_the_exit_still_runs(
    monkeypatch, no_exit, caplog
) -> None:
    async def _fails() -> None:
        raise OSError("daemon socket gone")

    pre_exec_stops.register_pre_exec_stop("Probe", _fails)

    with caplog.at_level("WARNING", logger="kiro_crew.pre_exec_stops"):
        await _orchestrator(AsyncMock(side_effect=asyncio.TimeoutError()))._shutdown_and_exit()

    assert "Probe stop before exit failed" in caplog.text
    assert "before restart" not in caplog.text
    no_exit._exit.assert_called_once_with(0)
