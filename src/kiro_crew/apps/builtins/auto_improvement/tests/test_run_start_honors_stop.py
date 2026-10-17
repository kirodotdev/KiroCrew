"""A stop or disable that lands while a run is still being started must hold.

``RunSupervisor.start`` builds the driver (git and provider probes, seconds) before it
creates the worker thread. A ``stop`` arriving in that window has no thread to signal,
so it must still prevent the run that is being started from launching. The disable
hook stops the run through that same ``stop``, so the same rule covers disabling the
app. These tests park ``_build_driver`` on an event so the window is held open.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest
from aiohttp import web

from kiro_crew.apps import teardown
from kiro_crew.apps.builtins.auto_improvement.backend import routes as R
from kiro_crew.apps.builtins.auto_improvement.backend import runner, store


class _Driver:
    """Parks in ``run`` until stopped, so a launched run stays visibly RUNNING."""

    def __init__(self) -> None:
        self._release = threading.Event()

    def run(self, **_kw: Any) -> Any:
        self._release.wait(timeout=10.0)
        return _Stats()

    def request_stop(self) -> None:
        self._release.set()


class _Stats:
    cycles = 0
    discovered = 0
    deduped = 0
    gated_out = 0
    not_kept = 0
    kept = 0
    filed = 0
    errors = 0
    cost_usd = 0.0


class _SlowBuild:
    """A ``_build_driver`` stand-in that waits until the test lets it finish."""

    def __init__(self, driver: _Driver) -> None:
        self.driver = driver
        self.entered = threading.Event()
        self.proceed = threading.Event()

    def __call__(self, _cfg: dict) -> _Driver:
        self.entered.set()
        assert self.proceed.wait(timeout=10.0)
        return self.driver


@pytest.fixture
def sup(monkeypatch: pytest.MonkeyPatch) -> runner.RunSupervisor:
    fresh = runner.RunSupervisor()
    monkeypatch.setattr(runner, "_SUPERVISOR", fresh)
    return fresh


@pytest.fixture(autouse=True)
def _clean_disable_registry():
    teardown.unregister_app_disable_hook(store.APP_NAME)
    yield
    teardown.unregister_app_disable_hook(store.APP_NAME)


def _start_in_thread(sup: runner.RunSupervisor) -> tuple[threading.Thread, dict]:
    outcome: dict = {}

    def _go() -> None:
        try:
            outcome["result"] = sup.start({"clone": "/does/not/matter"})
        except Exception as exc:  # noqa: BLE001 - recorded for the assertion
            outcome["error"] = exc

    t = threading.Thread(target=_go, daemon=True)
    t.start()
    return t, outcome


def _cleanup(sup: runner.RunSupervisor, driver: _Driver) -> None:
    driver.request_stop()
    sup.stop()


def test_stop_while_the_driver_is_being_built_keeps_the_run_from_starting(
    sup: runner.RunSupervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _Driver()
    build = _SlowBuild(driver)
    monkeypatch.setattr(sup, "_build_driver", build)

    t, outcome = _start_in_thread(sup)
    try:
        assert build.entered.wait(timeout=5.0)
        sup.stop()
        build.proceed.set()
        t.join(timeout=5.0)
        assert not t.is_alive()

        assert isinstance(outcome.get("error"), RuntimeError), outcome
        assert sup.status()["status"] != runner.STATUS_RUNNING
    finally:
        build.proceed.set()
        _cleanup(sup, driver)


def test_a_start_after_the_stop_is_admitted(
    sup: runner.RunSupervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stop withdraws only the start it overlapped, not every later one."""
    driver = _Driver()
    monkeypatch.setattr(sup, "_build_driver", lambda _cfg: driver)
    sup.stop()
    try:
        assert sup.start({"clone": "/does/not/matter"})["status"] == runner.STATUS_RUNNING
    finally:
        _cleanup(sup, driver)


@pytest.mark.asyncio
async def test_disabling_the_app_while_a_run_is_being_started_leaves_it_stopped(
    sup: runner.RunSupervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _Driver()
    build = _SlowBuild(driver)
    monkeypatch.setattr(sup, "_build_driver", build)

    app = web.Application()
    R.register_routes(app)

    t, outcome = _start_in_thread(sup)
    try:
        assert await asyncio.to_thread(build.entered.wait, 5.0)
        await teardown.notify_app_disabled(store.APP_NAME)
        build.proceed.set()
        await asyncio.to_thread(t.join, 5.0)
        assert not t.is_alive()

        assert "result" not in outcome, outcome
        assert sup.status()["status"] != runner.STATUS_RUNNING
    finally:
        build.proceed.set()
        _cleanup(sup, driver)
