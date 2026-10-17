"""A gateway stopped mid-update leaves no build child of the update behind.

The real gateway boots in-process with its update coordinator running ONE
unattended managed-venv apply (the real ``_auto_apply_wheel_update`` and the
real ``wheel_apply.run_wheel_apply``), whose engine stand-in runs a REAL build
child through the engine's own ``wheel_engine._run``: a process that starts a
grandchild in its group and sleeps far past the test. The harness then stops
the gateway the way SIGTERM does (it sets ``shutdown_event``, so ``run()``
walks its own exit path to ``os._exit``, which the harness intercepts). When
the boot block returns, the process would have exited, so that is where the
build child and its grandchild must already be dead.

Repeated, because what this pins is a race: the stop must kill the child before
the exit on every run, not on most of them.
"""

from __future__ import annotations

import asyncio
import sys

import pytest
from spawn_test_helpers import LONG_CHILD_WITH_GRANDCHILD, await_gone, await_pids, kill_leftovers
from wheel_update_test_helpers import wire_wheel_apply

from kiro_crew.platform import wheel_engine
from kiro_crew.slack.gateway import GatewayOrchestrator

#: Boots in this test. Each is a full boot (~2s here).
ITERATIONS = 10

pytestmark = [pytest.mark.integration, pytest.mark.timeout(ITERATIONS * 60)]


@pytest.mark.asyncio
async def test_a_stop_mid_build_kills_the_build_child_before_the_exit(
    gateway_boot, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    pidfile = tmp_path / "build-child.pids"

    def engine(**kwargs: object) -> object:
        # The real build-child seam, honouring the apply's real cancel.
        wheel_engine._run(
            [sys.executable, "-c", LONG_CHILD_WITH_GRANDCHILD, str(pidfile)],
            600,
            "pip install into the shadow venv",
            ctx=wheel_engine._BuildContext(cancel=kwargs["cancel"]),  # type: ignore[arg-type]
        )
        raise wheel_engine.WheelUpdateError("the stand-in build child exited")

    wire_wheel_apply(monkeypatch, apply=engine)

    async def one_apply(self: GatewayOrchestrator) -> None:
        await self._auto_apply_wheel_update("stable", "9.9.9")

    monkeypatch.setattr(GatewayOrchestrator, "_run_update_checks", one_apply)

    for run in range(ITERATIONS):
        pidfile.unlink(missing_ok=True)
        pids: tuple[int, ...] = ()
        try:
            async with gateway_boot():
                pids = await asyncio.to_thread(await_pids, pidfile, 60.0)
            # Out of the block: the gateway's exit path ran to os._exit.
            for pid in pids:
                assert await asyncio.to_thread(
                    await_gone, pid, 0.5
                ), f"run {run}: build child {pid} outlived the gateway's exit"
        finally:
            kill_leftovers(pids)
