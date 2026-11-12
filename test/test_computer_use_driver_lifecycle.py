"""The driver child is stopped on gateway cleanup and before an in-place restart."""

from __future__ import annotations

import asyncio

import pytest

from kiro_crew import pre_exec_stops
from kiro_crew.computer_use.driver_runtime import lifecycle
from kiro_crew.computer_use.driver_runtime import supervisor as sup_mod
from kiro_crew.testing.wait import default_timeout


class _App:
    def __init__(self) -> None:
        self.on_startup: list = []
        self.on_cleanup: list = []


def test_stop_is_registered_for_pre_exec_and_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    stops: list[str] = []
    monkeypatch.setattr(sup_mod, "stop_shared", lambda: stops.append("stop"))
    app = _App()
    lifecycle.register(app)

    async def run() -> None:
        for hook in app.on_startup:
            await hook(app)
        assert lifecycle.PRE_EXEC_NAME in pre_exec_stops._stops
        await pre_exec_stops.run_pre_exec_stops(timeout=default_timeout())
        for hook in app.on_cleanup:
            await hook(app)
        assert lifecycle.PRE_EXEC_NAME not in pre_exec_stops._stops

    asyncio.run(run())
    assert stops == ["stop", "stop"]
