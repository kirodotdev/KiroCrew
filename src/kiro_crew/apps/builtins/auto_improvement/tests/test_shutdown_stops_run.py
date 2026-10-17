"""Gateway shutdown must stop an in-flight run, not orphan it.

The supervisor's worker thread is a daemon, so it does not block interpreter exit —
but the agent/measurer SUBPROCESS it spawns is not a daemon and can outlive the
gateway, holding the clone lock and continuing to spend budget after the process
that owned it is gone. ``RunSupervisor.stop`` exists to wind a run down cleanly and
bounded, but nothing invoked it on shutdown: ``register_routes`` wired an
``on_cleanup`` hook for the PR watchers and none for the run supervisor.

This test drives the real ``register_routes`` on a bare aiohttp application, starts a
run that parks in ``driver.run`` (so the supervisor genuinely owns a live thread),
then fires the app's ``on_cleanup`` signal exactly as ``aiohttp`` does at shutdown.
The run must be stopped afterwards. It fails before the lifecycle hook is added.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest
from aiohttp import web

from kiro_crew.apps.builtins.auto_improvement.backend import routes as R
from kiro_crew.apps.builtins.auto_improvement.backend import runner


class _BlockingDriver:
    """Parks in ``run`` until stopped, so the supervisor has a genuinely live thread."""

    def __init__(self) -> None:
        self._release = threading.Event()

    def run(self, **_kw: Any) -> Any:
        self._release.wait(timeout=10.0)
        return _FakeStats()

    def request_stop(self) -> None:
        self._release.set()


class _FakeStats:
    cycles = 0
    discovered = 0
    deduped = 0
    gated_out = 0
    not_kept = 0
    kept = 0
    filed = 0
    errors = 0
    cost_usd = 0.0


@pytest.fixture
def _fresh_singleton(monkeypatch: pytest.MonkeyPatch) -> runner.RunSupervisor:
    """A fresh process-wide supervisor for the duration of the test.

    The cleanup hook resolves the supervisor through ``runner.get_supervisor()``, so
    the test must drive that same singleton — but a run leaked into the real module
    singleton would make unrelated tests' "already active" refusal fire. Swapping the
    module global for a fresh instance keeps both true.
    """
    sup = runner.RunSupervisor()
    monkeypatch.setattr(runner, "_SUPERVISOR", sup)
    return sup


@pytest.mark.asyncio
async def test_gateway_shutdown_stops_an_active_run(
    _fresh_singleton: runner.RunSupervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _BlockingDriver()
    monkeypatch.setattr(_fresh_singleton, "_build_driver", lambda _cfg: driver)

    app = web.Application()
    R.register_routes(app)
    # aiohttp freezes the signal lists during startup; a signal can only be sent once
    # frozen, so mirror that here before firing shutdown.
    app.freeze()

    started = _fresh_singleton.start({"clone": "/does/not/matter"})
    try:
        assert started["status"] == runner.STATUS_RUNNING
        assert _fresh_singleton.status()["status"] == runner.STATUS_RUNNING

        # Fire the shutdown signals exactly as aiohttp does when the gateway stops.
        await app.on_cleanup.send(app)

        # The load-bearing assertion: before the fix `request_stop` is never called,
        # so this is False and the run is orphaned.
        assert (
            driver._release.is_set()
        ), "shutdown did not ask the driver to stop — the run was orphaned"
        # The released driver returns at once, so the join inside `stop` observes a
        # finished thread and the terminal status is DONE. (STOPPING would be the
        # honest answer only if a real measurement outran the join timeout.)
        assert (
            _fresh_singleton.status()["status"] == runner.STATUS_DONE
        ), "the run supervisor was not stopped on gateway shutdown"
    finally:
        driver.request_stop()
        _fresh_singleton.stop()


def test_register_routes_wires_a_supervisor_cleanup_hook() -> None:
    """A structural guard: the run-supervisor stop must be an on_cleanup hook, so a
    future edit that drops it fails here rather than silently orphaning runs."""
    app = web.Application()
    R.register_routes(app)
    names = {getattr(h, "__name__", "") for h in app.on_cleanup}
    assert "_stop_run" in names, sorted(names)
