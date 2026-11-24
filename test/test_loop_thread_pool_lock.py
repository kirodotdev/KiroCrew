"""The loop's default executor and spawn pool take work while a pool worker starts.

CPython's ``ThreadPoolExecutor.submit`` holds ``concurrent.futures.thread.
_global_shutdown_lock`` (one lock for the whole process) while it starts a worker,
and a slow ``Thread.start()`` -- seconds on a Windows host near its commit limit --
held the gateway loop inside ``submit`` for 20 s until its watchdog exited it.
Two of the pools the loop submits to are covered here: the default executor that
``configure_default_executor`` installs (``asyncio.to_thread``) and
``spawn_executor()``, which builds every child the gateway loop starts.

These tests hold that lock on a helper thread until the test releases it, so a
start of any length is simulated without a sleep. A loop whose pool takes the lock
waits in ``submit`` where nothing can release it, and the test fails by name at the
hold bound.
"""

from __future__ import annotations

import asyncio
import concurrent.futures.thread as stdlib_thread
import contextlib
import sys
import threading
from collections.abc import Iterator

import pytest

from kiro_crew import executors
from kiro_crew import platform_compat_offloop_spawn as offloop
from kiro_crew.testing.wait import finished

#: A lost-run bound only: the tests release what they hold themselves, and a hold
#: still in place at this bound means the code under test waited for it.
_HOLD_BOUND_SECS = 10.0


class _Held:
    def __init__(self) -> None:
        self.release = threading.Event()
        self.timed_out = False


@contextlib.contextmanager
def _held_pool_lock() -> Iterator[_Held]:
    """Hold the stdlib pools' process-wide lock on a helper thread, as a slow start does."""
    held = _Held()
    holding = threading.Event()

    def hold() -> None:
        with stdlib_thread._global_shutdown_lock:
            holding.set()
            held.timed_out = not held.release.wait(_HOLD_BOUND_SECS)

    holder = threading.Thread(target=hold, name="test-held-pool-lock", daemon=True)
    holder.start()
    assert holding.wait(_HOLD_BOUND_SECS)
    try:
        yield held
    finally:
        held.release.set()
        holder.join(_HOLD_BOUND_SECS)


@pytest.mark.asyncio
async def test_to_thread_runs_while_another_thread_holds_the_pool_lock() -> None:
    executors.configure_default_executor()
    with _held_pool_lock() as held:
        assert await finished(asyncio.to_thread(lambda: "ran"), "the to_thread call") == "ran"
    assert not held.timed_out, "the loop waited for another thread's worker start"


# Each run starts its own interpreter child; the spawn tests keep them serial.
@pytest.mark.xdist_group(name="spawn_off_loop")
def test_a_gateway_loop_spawn_runs_while_another_thread_holds_the_pool_lock() -> None:
    async def body() -> tuple[int, bool]:
        with _held_pool_lock() as held:
            child = await finished(
                asyncio.create_subprocess_exec(sys.executable, "-c", "raise SystemExit(3)"),
                "the spawn",
            )
            returncode = await finished(child.wait(), "the child's exit")
        return returncode, held.timed_out

    # The Proactor loop on Windows, the Unix selector loop elsewhere.
    with asyncio.Runner(loop_factory=offloop.gateway_loop_factory()) as runner:
        returncode, timed_out = runner.run(body())
    assert not timed_out, "the loop waited for another thread's worker start"
    assert returncode == 3
