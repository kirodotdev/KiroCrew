"""pptx_maker's unawaited provisioning runs are held until they finish.

The provisioning routes start the work and answer 202 at once, so nothing awaits
the task. The event loop keeps only a weak reference to a running task, so a run
parked on an await could be collected mid-run and leave the provisioning state
stuck. ``_run_in_background`` holds each task in a module set until it is done.
"""

from __future__ import annotations

import asyncio
import gc
import weakref

import pytest

from kiro_crew.apps.builtins.pptx_maker.backend import routes
from kiro_crew.testing.wait import async_wait_until


@pytest.mark.asyncio
async def test_a_parked_background_run_survives_a_collection() -> None:
    release = asyncio.Event()
    finished: list[bool] = []

    async def provision() -> None:
        await release.wait()
        finished.append(True)

    start = getattr(routes, "_run_in_background", asyncio.ensure_future)
    held = getattr(routes, "_BACKGROUND_TASKS", set())
    ref = weakref.ref(start(provision()))
    await asyncio.sleep(0)  # let the task start and park on the event
    gc.collect()
    assert ref() is not None, "a parked provisioning run was collected mid-run"
    assert ref() in held, "nothing holds the parked provisioning run"
    release.set()
    await async_wait_until(lambda: finished == [True], describe=lambda: f"finished: {finished}")
    await async_wait_until(lambda: not held, describe=lambda: f"held runs: {len(held)}")


def test_both_provisioning_routes_start_their_run_through_the_holder() -> None:
    import inspect

    for handler in (routes._handle_engine_provision, routes._handle_assets_provision):
        source = inspect.getsource(inspect.unwrap(handler))
        assert "_run_in_background(" in source
        assert "asyncio.ensure_future(" not in source
