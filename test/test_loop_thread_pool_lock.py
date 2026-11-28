"""The loop's default executor and spawn pool take work while a pool worker starts.

CPython's ``ThreadPoolExecutor.submit`` holds ``concurrent.futures.thread.
_global_shutdown_lock`` (one lock for the whole process) while it starts a worker,
and a slow ``Thread.start()`` -- seconds on a Windows host near its commit limit --
held the gateway loop inside ``submit`` for 20 s until its watchdog exited it.
The pools the loop submits to are covered here: the default executor that
``configure_default_executor`` installs (``asyncio.to_thread``), ``spawn_executor()``,
which builds every child the gateway loop starts, every pool
``kiro_crew.executors`` builds (``run_in_executor``) and the task store's writer
(``TaskStore.run``), which ``open_default_store`` starts off the loop.

These tests hold that lock on a helper thread until the test releases it, so a
start of any length is simulated without a sleep. A loop whose pool takes the lock
waits in ``submit`` where nothing can release it, and the test fails by name at the
hold bound.
"""

from __future__ import annotations

import asyncio
import concurrent.futures.thread as stdlib_thread
import contextlib
import inspect
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from kiro_crew import enqueue_only_pool, executors
from kiro_crew import platform_compat_offloop_spawn as offloop
from kiro_crew import taskq
from kiro_crew.taskq.store import TaskStore
from kiro_crew.testing.wait import finished

#: A lost-run bound only: the tests release what they hold themselves, and a hold
#: still in place at this bound means the code under test waited for it.
_HOLD_BOUND_SECS = 10.0

#: Every thread-pool getter in ``kiro_crew.executors`` but the default executor's; the
#: path resolver's pool is a ``SubprocessPoolExecutor``, not a thread pool.
_POOL_GETTERS = sorted(
    name
    for name, value in vars(executors).items()
    if name.endswith("_executor")
    and inspect.isfunction(value)
    and name
    not in {"configure_default_executor", "path_resolve_executor", "shutdown_maintenance_executor"}
)


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


@pytest.mark.asyncio
@pytest.mark.parametrize("getter", _POOL_GETTERS)
async def test_run_in_executor_on_each_pool_runs_while_another_thread_holds_the_pool_lock(
    getter: str,
) -> None:
    pool = getattr(executors, getter)()
    loop = asyncio.get_running_loop()
    try:
        with _held_pool_lock() as held:
            worker = await finished(
                loop.run_in_executor(pool, threading.current_thread), f"the {getter} call"
            )
        assert not held.timed_out, "the loop waited for another thread's worker start"
        assert worker.name.startswith(pool._thread_name_prefix)
    finally:
        if getter == "memory_preparation_executor":  # a new pool per call, owned here
            pool.shutdown(wait=True)


@pytest.mark.asyncio
async def test_the_task_store_writer_runs_while_another_thread_holds_the_pool_lock(
    tmp_path: Path,
) -> None:
    store = TaskStore(tmp_path / "taskq.db")
    try:
        with _held_pool_lock() as held:
            # The first call builds the writer pool on the loop thread, the second
            # submits to the built one.
            for call in ("first", "second"):
                worker = await finished(store.run(threading.current_thread), f"the {call} call")
                assert worker.name.startswith("taskq-writer")
        assert not held.timed_out, "the loop waited for another thread's worker start"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_the_booted_task_store_runs_its_first_call_without_starting_a_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``open_default_store`` runs off the loop and starts the writer's worker there."""
    store: TaskStore | None = None
    writer_starts: list[str] = []
    real_start = threading.Thread.start

    def recording_start(thread: threading.Thread) -> None:
        # Only the writer's own threads: other code may start threads meanwhile.
        if thread.name.startswith("taskq-writer"):
            writer_starts.append(thread.name)
        real_start(thread)

    try:
        store = taskq.open_default_store(tmp_path, import_legacy_records=False)
        monkeypatch.setattr(enqueue_only_pool.threading.Thread, "start", recording_start)
        worker = await finished(store.run(threading.current_thread), "the first call")
        assert worker.name.startswith("taskq-writer")
        assert writer_starts == [], "the loop's first call started a writer thread"
    finally:
        if store is not None:
            store.close()


def _refuse_writer_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    real_start = threading.Thread.start

    def refusing_start(thread: threading.Thread) -> None:
        if thread.name.startswith("taskq-writer"):
            raise RuntimeError("can't start new thread")
        real_start(thread)

    monkeypatch.setattr(enqueue_only_pool.threading.Thread, "start", refusing_start)


def test_a_writer_start_refused_at_boot_makes_the_store_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The opener's off-loop refusal and retry take it, never the loop's first call."""
    _refuse_writer_starts(monkeypatch)
    with pytest.raises(taskq.TaskStoreUnavailable, match="writer thread could not start"):
        taskq.open_default_store(tmp_path, import_legacy_records=False)


@pytest.mark.parametrize("refused", [False, True])
def test_the_admission_gates_store_starts_its_writer_where_it_is_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refused: bool
) -> None:
    from kiro_crew.config import paths
    from kiro_crew.shared_crew import admission_gate

    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    if refused:
        _refuse_writer_starts(monkeypatch)
    work = admission_gate._open_default_store()
    try:
        if refused:
            assert work is None
        else:
            assert work is not None and work.tasks._executor is not None
    finally:
        if work is not None:
            work.tasks.close()


@pytest.mark.parametrize(
    "getter", [name for name in _POOL_GETTERS if name != "memory_preparation_executor"]
)
def test_a_built_pools_getter_returns_while_another_thread_holds_the_getters_lock(
    getter: str,
) -> None:
    """A first pool build holds ``executors._lock`` across its worker's and grower's starts."""
    pool = getattr(executors, getter)()
    got: list[object] = []
    caller = threading.Thread(target=lambda: got.append(getattr(executors, getter)()), daemon=True)
    with executors._lock:
        caller.start()
        caller.join(_HOLD_BOUND_SECS)
        waited = caller.is_alive()
    caller.join(_HOLD_BOUND_SECS)
    assert not waited, f"{getter}() waited on the lock another pool's build holds"
    assert got == [pool]


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
