"""Disabling the app must stop an in-flight run, not just refuse its routes.

Gateway shutdown is covered by ``test_shutdown_stops_run``. Disabling the app is a
different path with the same exposure and none of the same wiring: ``on_cleanup``
fires only when the gateway stops, so switching the app off leaves
``_require_enabled`` answering 403 on every route while the supervisor thread keeps
the clone lock and keeps spending budget. The operator sees the app off; the work
carries on.

``apps.teardown`` has a purpose-built seam for exactly this —
``register_app_disable_hook`` / ``notify_app_disabled`` — fired INSIDE the disable
request and before the ``enabled`` flag is written, which is what makes it an
off-switch rather than a sweep. Its own docstring names the reason: a worker holding
something time-bounded "can act once more in the gap between the operator's click and
the next poll, which is a whole turn's worth of authority handed out after permission
was withdrawn." This app registered nothing there.

These tests drive the real ``register_routes``, start a run that parks in
``driver.run`` so the supervisor genuinely owns a live thread, then fire
``notify_app_disabled`` exactly as the disable request does.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest
from aiohttp import web

from kiro_crew.apps import teardown
from kiro_crew.apps.builtins.auto_improvement.backend import pr_watchers
from kiro_crew.apps.builtins.auto_improvement.backend import routes as R
from kiro_crew.apps.builtins.auto_improvement.backend import runner, store


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

    The hook resolves the supervisor through ``runner.get_supervisor()``, so the test
    must drive that same singleton — but a run leaked into the real module singleton
    would make unrelated tests' "already active" refusal fire.
    """
    sup = runner.RunSupervisor()
    monkeypatch.setattr(runner, "_SUPERVISOR", sup)
    return sup


@pytest.fixture(autouse=True)
def _clean_disable_registry():
    """The disable registry is process memory shared by every test in the worker, so
    a hook registered here must not outlive the test that registered it."""
    teardown.unregister_app_disable_hook(store.APP_NAME)
    yield
    teardown.unregister_app_disable_hook(store.APP_NAME)


@pytest.mark.asyncio
async def test_disabling_the_app_stops_an_active_run(
    _fresh_singleton: runner.RunSupervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _BlockingDriver()
    monkeypatch.setattr(_fresh_singleton, "_build_driver", lambda _cfg: driver)

    app = web.Application()
    R.register_routes(app)

    started = _fresh_singleton.start({"clone": "/does/not/matter"})
    try:
        assert started["status"] == runner.STATUS_RUNNING
        assert _fresh_singleton.status()["status"] == runner.STATUS_RUNNING

        # Exactly what the disable request does, at the point it does it.
        await teardown.notify_app_disabled(store.APP_NAME)

        # The load-bearing assertion: with no hook registered, `notify_app_disabled`
        # returns immediately having found nothing, `request_stop` is never called,
        # and the run keeps the clone lock and the budget.
        assert (
            driver._release.is_set()
        ), "disabling the app did not ask the driver to stop — the run kept going"
        # The released driver returns at once, so the join inside `stop` observes a
        # finished thread and the terminal status is DONE. (STOPPING would be the
        # honest answer only if a real measurement outran the join timeout.)
        assert (
            _fresh_singleton.status()["status"] == runner.STATUS_DONE
        ), "the run supervisor was not stopped when the app was disabled"
    finally:
        driver.request_stop()
        _fresh_singleton.stop()


@pytest.mark.asyncio
async def test_disabling_while_idle_is_a_no_op(
    _fresh_singleton: runner.RunSupervisor,
) -> None:
    """``stop`` is idempotent, and the disable path must stay safe to fire at any
    time — an operator may disable an app that is doing nothing, repeatedly."""
    app = web.Application()
    R.register_routes(app)

    await teardown.notify_app_disabled(store.APP_NAME)
    await teardown.notify_app_disabled(store.APP_NAME)

    assert _fresh_singleton.status()["status"] != runner.STATUS_RUNNING


@pytest.mark.asyncio
async def test_disabling_the_app_also_stops_the_pr_watchers(
    _fresh_singleton: runner.RunSupervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run is not the only worker a disable has to reach.

    A watcher runs an agent turn inside a per-PR clone on a timer, so leaving it
    going after the operator switches the app off keeps acting on their
    repositories with a permission that was withdrawn — the same harm the run
    poses, by a different worker. Gateway shutdown already signals both
    (``_stop_watchers`` beside ``_stop_run``); disable must not stop only one.
    """
    stopped: list[bool] = []

    def record_stop_all() -> int:
        stopped.append(True)
        return 0

    monkeypatch.setattr(pr_watchers.get_registry(), "stop_all", record_stop_all)

    app = web.Application()
    R.register_routes(app)

    await teardown.notify_app_disabled(store.APP_NAME)

    assert stopped, "disabling the app left the PR watchers running"


@pytest.mark.asyncio
async def test_a_failing_watcher_stop_still_stops_the_run(
    _fresh_singleton: runner.RunSupervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two signals are contained separately.

    ``on_cleanup`` gets that for free by holding two hooks, so a raising watcher
    stop cannot skip the run stop there. This hook is one function and has to
    reproduce it deliberately.
    """
    driver = _BlockingDriver()
    monkeypatch.setattr(_fresh_singleton, "_build_driver", lambda _cfg: driver)

    def boom() -> int:
        raise RuntimeError("registry exploded")

    monkeypatch.setattr(pr_watchers.get_registry(), "stop_all", boom)

    app = web.Application()
    R.register_routes(app)

    _fresh_singleton.start({"clone": "/does/not/matter"})
    try:
        await teardown.notify_app_disabled(store.APP_NAME)
        assert driver._release.is_set(), "a failing watcher stop swallowed the run stop"
    finally:
        driver.request_stop()
        _fresh_singleton.stop()


def test_register_routes_wires_an_app_disable_hook() -> None:
    """A structural guard, matching the one the shutdown hook carries: the disable
    seam must be wired under this app's own name, so a future edit that drops it
    fails here instead of silently letting a disabled app keep running."""
    app = web.Application()
    R.register_routes(app)
    assert teardown._APP_DISABLE_HOOKS.get(store.APP_NAME) is not None
