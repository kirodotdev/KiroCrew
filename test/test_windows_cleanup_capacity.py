"""Cleanup capacity is reserved before spawn and overflow cannot lose lineage."""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kiro_crew import platform_compat as pc


@pytest.fixture
def kernel(monkeypatch):
    snapshot = pc._windows_process_parent_map
    identities: dict[int, tuple[int, int, int | None]] = {}
    parents: dict[int, int] = {}
    closed: list[int] = []
    opened = []
    denied = set()
    clock = [0.0]
    monkeypatch.setattr(pc, "IS_WINDOWS", True)
    monkeypatch.setattr(pc, "_PENDING_WINDOWS_TREE_CLEANUPS", {})
    monkeypatch.setattr(pc, "_WINDOWS_TREE_ADMISSIONS", set(), raising=False)
    monkeypatch.setattr(pc, "_WINDOWS_CLEANUP_ROOT_LIMIT", 2, raising=False)
    monkeypatch.setattr(pc, "_WINDOWS_CLEANUP_IDENTITY_LIMIT", 3, raising=False)
    monkeypatch.setattr(pc, "_windows_process_parent_map", lambda: dict(parents))
    monkeypatch.setattr(pc, "_windows_process_handle_identity", identities.get)
    monkeypatch.setattr(pc, "close_process_handle", closed.append)
    monkeypatch.setattr(pc, "ctypes", SimpleNamespace())
    monkeypatch.setattr(
        pc,
        "time",
        SimpleNamespace(
            monotonic=lambda: clock[0], sleep=lambda _: clock.__setitem__(0, clock[0] + 0.01)
        ),
    )

    def add(pid, parent=1, created=10):
        identities[pid * 10] = (pid, created, None)
        parents[pid] = parent
        return pid * 10

    def exit_(pid, when=200):
        identities[pid * 10] = (pid, identities[pid * 10][1], when)
        parents.pop(pid, None)

    def terminate(handle):
        if handle in denied:
            raise OSError("fixture denied")
        exit_(identities[handle][0])
        return True

    def open_(pid, **kwargs):
        assert pid * 10 in identities
        opened.append(pid)
        return pid * 10

    monkeypatch.setattr(pc, "_open_process_termination_handle", open_)
    monkeypatch.setattr(pc, "terminate_process_handle", terminate)
    return SimpleNamespace(**locals())


def test_last_slot_is_atomic_and_refund_is_idempotent(kernel):
    first = pc.reserve_windows_tree_cleanup()

    def acquire(_):
        try:
            return pc.reserve_windows_tree_cleanup()
        except pc.WindowsCleanupCapacityError:
            return None

    with ThreadPoolExecutor(max_workers=8) as workers:
        answers = list(workers.map(acquire, range(16)))
    winners = [answer for answer in answers if answer is not None]
    assert len(winners) == 1
    assert len(pc._WINDOWS_TREE_ADMISSIONS) == 2
    pc.release_windows_tree_reservation(first)
    pc.release_windows_tree_reservation(first)
    assert len(pc._WINDOWS_TREE_ADMISSIONS) == 1
    pc.release_windows_tree_reservation(winners[0])
    assert not pc._WINDOWS_TREE_ADMISSIONS


def test_capacity_refuses_import_before_open_and_does_not_close_owned_handle(kernel):
    for _ in range(2):
        pc.reserve_windows_tree_cleanup()
    handle = kernel.add(100)
    with pytest.raises(pc.WindowsCleanupCapacityError):
        pc.kill_process_tree_pinned(100, "10")
    with pytest.raises(pc.WindowsCleanupCapacityError):
        pc.terminate_windows_process_tree_owned(handle)
    assert kernel.opened == []
    assert kernel.closed == []
    assert kernel.identities[handle][2] is None


def test_overflow_is_sticky_after_unpinned_intermediary_exits(kernel):
    root = kernel.add(100)
    for child in (201, 202, 203):
        kernel.add(child, 100, 20)
    with pytest.raises(pc.WindowsCleanupCapacityError):
        pc.terminate_windows_process_tree_owned(root)
    state = pc._PENDING_WINDOWS_TREE_CLEANUPS[(100, 10)]
    assert state.manual_required
    assert state.handles == {100: root}
    assert kernel.opened == []
    kernel.add(300, 201, 30)
    for child in (201, 202, 203):
        kernel.exit_(child, 50)
    with pytest.raises(pc.WindowsCleanupCapacityError, match="manual handling"):
        pc.terminate_windows_process_tree_owned(root)
    assert pc.retry_pending_windows_process_trees() == ()
    assert kernel.identities[3000][2] is None
    assert kernel.closed == []
    assert len(pc._WINDOWS_TREE_ADMISSIONS) == 1
    with pytest.raises(pc.WindowsCleanupCapacityError) as refusal:
        pc.reserve_windows_tree_cleanup()
    # The refusal outlives the log line that named the cause, so it has to name
    # the quarantined root and the remedy by itself.
    assert "100" in str(refusal.value)
    assert "by hand" in str(refusal.value)
    assert "restarts" in str(refusal.value)


def test_growth_keeps_previous_pins_and_all_nested_stores_bounded(kernel):
    root = kernel.add(100)
    kernel.add(201, 100, 20)
    kernel.denied.add(root)
    with pytest.raises(OSError, match="fixture denied"):
        pc.terminate_windows_process_tree_owned(root)
    state = pc._PENDING_WINDOWS_TREE_CLEANUPS[(100, 10)]
    before = dict(state.handles)
    kernel.add(301, 201, 30)
    kernel.add(302, 201, 31)
    assert pc.retry_pending_windows_process_trees() == ()
    assert state.manual_required
    assert state.handles == before
    assert kernel.opened == [201]
    assert kernel.closed == []
    for name in ("handles", "signalled", "terminally_scanned"):
        assert len(getattr(state, name)) <= pc._WINDOWS_CLEANUP_IDENTITY_LIMIT


def test_transient_failure_duplicate_and_metadata_retry_refund_once(kernel, monkeypatch):
    root = kernel.add(100)
    kernel.denied.add(root)
    with pytest.raises(OSError):
        pc.terminate_windows_process_tree_owned(root)
    kernel.identities[1001] = kernel.identities[root]
    with pytest.raises(OSError):
        pc.terminate_windows_process_tree_owned(1001)
    assert kernel.closed == [1001]
    assert len(pc._WINDOWS_TREE_ADMISSIONS) == 1
    kernel.denied.clear()

    # Fail the drain's OWN retirement rather than a caller-supplied stand-in, so
    # the retained-receipt path is exercised where production runs it.
    from kiro_crew import session_pid

    original_retire = session_pid.retire_windows_tree_tracking

    def failed_metadata(pid):
        assert root not in kernel.closed
        raise OSError("metadata not written")

    monkeypatch.setattr(session_pid, "retire_windows_tree_tracking", failed_metadata)
    assert pc.retry_pending_windows_process_trees() == ()
    assert kernel.closed == [1001]
    assert len(pc._WINDOWS_TREE_ADMISSIONS) == 1
    monkeypatch.setattr(session_pid, "retire_windows_tree_tracking", original_retire)
    assert pc.retry_pending_windows_process_trees() == (100,)
    assert not pc._WINDOWS_TREE_ADMISSIONS
    assert kernel.closed == [1001, root]
    assert pc.retry_pending_windows_process_trees() == ()


@pytest.mark.asyncio
async def test_spawn_full_never_calls_factory_and_failure_refunds(kernel):
    factory = AsyncMock(side_effect=OSError("launch failed"))
    with pytest.raises(OSError, match="launch failed"):
        await pc.create_windows_cleanup_owned_process(factory)
    assert not pc._WINDOWS_TREE_ADMISSIONS
    for _ in range(2):
        pc.reserve_windows_tree_cleanup()
    factory.reset_mock()
    with pytest.raises(pc.WindowsCleanupCapacityError):
        await pc.create_windows_cleanup_owned_process(factory)
    factory.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_launch_waits_for_result_then_drains(kernel, monkeypatch):
    entered = asyncio.Event()
    finish = asyncio.Event()
    root = kernel.add(100)
    process = SimpleNamespace(pid=100, wait=AsyncMock(return_value=0))
    monkeypatch.setattr(pc, "duplicate_asyncio_process_handle", lambda p: root)

    async def factory(**kwargs):
        entered.set()
        await finish.wait()
        return process

    task = asyncio.create_task(pc.create_windows_cleanup_owned_process(factory))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert len(pc._WINDOWS_TREE_ADMISSIONS) == 1
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert kernel.identities[root][2] is not None
        assert not pc._WINDOWS_TREE_ADMISSIONS
    finally:
        finish.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)


@pytest.mark.asyncio
async def test_admitted_live_trees_keep_capacity_when_they_fail_together(kernel, monkeypatch):
    import gc
    import weakref

    class Process:
        def __init__(self, pid):
            self.pid = pid

        async def wait(self):
            return 0

    monkeypatch.setattr(pc, "duplicate_asyncio_process_handle", lambda p: p.pid * 10)

    async def exercise():
        owners = []
        for pid in (100, 200):
            kernel.add(pid)

            async def factory(pid=pid, **kwargs):
                return Process(pid)

            owners.append(await pc.create_windows_cleanup_owned_process(factory))
        assert len(pc._WINDOWS_TREE_ADMISSIONS) == 2
        kernel.denied.update((1000, 2000))

        # Consume exceptions inside each task so no traceback-bearing gathered
        # result or assertion temporary keeps the owners alive in the test.
        async def failed_cleanup(owner):
            try:
                await pc.terminate_windows_asyncio_tree(owner)
            except OSError:
                return True
            return False

        assert all(await asyncio.gather(*(failed_cleanup(p) for p in owners)))
        return [weakref.ref(p) for p in owners]

    pool = ThreadPoolExecutor(max_workers=2)
    try:
        monkeypatch.setattr(pc, "subprocess_executor", lambda: pool)
        refs = await exercise()
    finally:
        # A published exception can reach the waiter before the worker drops
        # its traceback. Join our workers before testing registry-only ownership.
        await asyncio.to_thread(pool.shutdown, wait=True)
    await asyncio.sleep(0)
    gc.collect()
    assert all(ref() is None for ref in refs)
    assert len(pc._PENDING_WINDOWS_TREE_CLEANUPS) == 2
    assert len(pc._WINDOWS_TREE_ADMISSIONS) == 2
    kernel.denied.clear()
    assert set(pc.retry_pending_windows_process_trees()) == {100, 200}
    assert not pc._WINDOWS_TREE_ADMISSIONS


@pytest.mark.asyncio
async def test_cancelled_failed_factory_refunds_without_a_child(kernel):
    entered, finish = asyncio.Event(), asyncio.Event()

    async def factory(**kwargs):
        entered.set()
        await finish.wait()
        raise OSError("spawn never created child")

    task = asyncio.create_task(pc.create_windows_cleanup_owned_process(factory))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        finish.set()
        with pytest.raises(OSError, match="never created"):
            await asyncio.wait_for(task, 5)
        assert not pc._WINDOWS_TREE_ADMISSIONS
    finally:
        finish.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)


@pytest.mark.asyncio
async def test_cancelled_spawn_failed_cleanup_keeps_capacity(kernel, monkeypatch):
    entered, finish = asyncio.Event(), asyncio.Event()
    root = kernel.add(100)
    kernel.denied.add(root)
    monkeypatch.setattr(pc, "duplicate_asyncio_process_handle", lambda p: root)

    async def factory(**kwargs):
        entered.set()
        await finish.wait()
        return SimpleNamespace(pid=100, wait=AsyncMock(return_value=0))

    task = asyncio.create_task(pc.create_windows_cleanup_owned_process(factory))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert len(pc._WINDOWS_TREE_ADMISSIONS) == 1
        assert pc._PENDING_WINDOWS_TREE_CLEANUPS[(100, 10)].handles == {100: root}
        assert kernel.closed == []
    finally:
        finish.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("module_name", ["client", "runtime"])
@pytest.mark.parametrize("manual", [False, True])
async def test_both_physical_spawn_expressions_obey_admission(kernel, module_name, manual):
    """Execute each production physical-spawn expression, without its IO prelude."""
    import ast
    import importlib
    import inspect
    import textwrap

    mod = importlib.import_module(f"kiro_crew.acp.{module_name}")
    cls = mod.AcpClient if module_name == "client" else mod.AcpRuntime
    method = cls._spawn if module_name == "client" else cls._spawn_admitted
    tree = ast.parse(textwrap.dedent(inspect.getsource(method)))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Attribute) and target.attr == "_process"
            for target in node.targets
        )
    ]
    assert len(calls) == 1
    assignment = calls[0]
    state = pc.reserve_windows_tree_cleanup()
    if manual:
        pc._manual_windows_tree(state)
    else:
        pc.reserve_windows_tree_cleanup()
    factory = AsyncMock()
    namespace = dict(
        vars(mod),
        create_subprocess_limited=factory,
        self=SimpleNamespace(_spawn_work_dir="unused", _bound_workspace_fd=None),
        argv=["never-launched"],
        env={},
    )
    function = ast.AsyncFunctionDef(
        name="physical_spawn",
        args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
        body=[assignment],
        decorator_list=[],
        type_params=[],
    )
    code = compile(
        ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])),
        "<production physical spawn>",
        "exec",
    )
    exec(code, namespace)  # nosemgrep: python.lang.security.audit.exec-detected.exec-detected -- runs the PRODUCTION spawn statement lifted out of this repo's own source by AST, never external input; a hand-copied duplicate is exactly what this test exists to rule out  # noqa: E501  # fmt: skip
    with pytest.raises(pc.WindowsCleanupCapacityError):
        await namespace["physical_spawn"]()
    factory.assert_not_awaited()


def test_app_capacity_refusal_and_manual_dead_root_preserve_rows(kernel, monkeypatch, tmp_path):
    from kiro_crew.apps import backend

    path = tmp_path / "apps.json"
    monkeypatch.setattr(backend, "_pidfile_path", lambda: path)
    rows = {
        "owned": {"pid": 100, "start_time": "10"},
        "unrelated": {"pid": 900, "start_time": "90"},
    }
    backend._write_pidfile(rows)
    monkeypatch.setattr(pc, "pid_liveness", lambda pid: pc.PID_ALIVE)
    monkeypatch.setattr(backend, "_proc_start_time", lambda pid: str(pid // 10))
    for _ in range(2):
        pc.reserve_windows_tree_cleanup()
    assert backend._reap_stale_app_backends() == 0
    assert backend._read_pidfile() == rows
    assert kernel.opened == []
    # A subsequent sweep must not interpret a quarantined root's exit as cleanup.
    state = next(iter(pc._WINDOWS_TREE_ADMISSIONS))
    state.root_pid, state.key = 100, (100, 10)
    state.handles = {100: kernel.add(100)}
    pc._PENDING_WINDOWS_TREE_CLEANUPS[state.key] = state
    pc._manual_windows_tree(state)
    monkeypatch.setattr(pc, "pid_liveness", lambda pid: pc.PID_DEAD if pid == 100 else pc.PID_ALIVE)
    assert backend._reap_stale_app_backends() == 0
    assert backend._read_pidfile() == rows


def test_app_metadata_failure_keeps_pin_and_capacity_for_maintenance(kernel, monkeypatch, tmp_path):
    from kiro_crew.apps import backend

    path = tmp_path / "apps.json"
    monkeypatch.setattr(backend, "_pidfile_path", lambda: path)
    rows = {
        "owned": {"pid": 100, "start_time": "10"},
        "unrelated": {"pid": 900, "start_time": "90"},
    }
    backend._write_pidfile(rows)
    root = kernel.add(100)
    original_write = backend.atomic_write

    def fail(*args, **kwargs):
        assert root not in kernel.closed
        raise OSError("metadata disk failure")

    monkeypatch.setattr(backend, "atomic_write", fail)
    assert pc.kill_process_tree_pinned(100, "10", app_tracking=True) is False
    assert kernel.closed == []
    assert len(pc._WINDOWS_TREE_ADMISSIONS) == 1
    assert backend._read_pidfile() == rows
    monkeypatch.setattr(backend, "atomic_write", original_write)
    assert pc.retry_pending_windows_process_trees() == (100,)
    assert backend._read_pidfile() == {"unrelated": rows["unrelated"]}
    assert kernel.closed == [root]
    assert not pc._WINDOWS_TREE_ADMISSIONS


@pytest.mark.asyncio
async def test_resume_worker_settles_before_repeated_cancellation(kernel):
    entered, finish = asyncio.Event(), asyncio.Event()

    async def worker():
        entered.set()
        await finish.wait()
        return True

    task = asyncio.create_task(pc.finish_windows_cleanup_owned_spawn(worker))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
    finally:
        finish.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)


@pytest.mark.parametrize("shape", ["_client", "_proc", "_active_proc"])
def test_sync_provider_shapes_cannot_bypass_capacity(kernel, monkeypatch, shape):
    from kiro_crew import session_pid

    root = kernel.add(100)
    for _ in range(2):
        pc.reserve_windows_tree_cleanup()
    monkeypatch.setattr(pc, "get_process_start_id", lambda pid: "10")
    monkeypatch.setattr(pc, "kill_pid", lambda *a: pytest.fail("root-only bypass"))
    monkeypatch.setattr(pc, "kill_process_tree", lambda *a: pytest.fail("unadmitted tree bypass"))
    obj = (
        SimpleNamespace(_pid=100, _start_time="10")
        if shape == "_client"
        else SimpleNamespace(pid=100, returncode=None)
    )
    provider = SimpleNamespace(**{shape: obj})
    session_pid._sync_kill_provider(provider)
    assert kernel.opened == []
    assert kernel.closed == []
    assert kernel.identities[root][2] is None


def test_duplicate_sync_admission_at_capacity_and_wrong_identity(kernel):
    root = kernel.add(100)
    kernel.denied.add(root)
    with pytest.raises(OSError, match="fixture denied"):
        pc.kill_process_tree_pinned(100, "10")
    pc.reserve_windows_tree_cleanup()
    with pytest.raises(OSError, match="fixture denied"):
        pc.kill_process_tree_pinned(100, "10")
    assert kernel.opened == [100]
    assert len(pc._WINDOWS_TREE_ADMISSIONS) == 2
    with pytest.raises(pc.WindowsCleanupCapacityError):
        pc.kill_process_tree_pinned(100, "11")
    assert kernel.closed == []
    kernel.denied.clear()
    assert pc.kill_process_tree_pinned(100, "10") is True
    assert len(pc._WINDOWS_TREE_ADMISSIONS) == 1


def test_breadth_scan_refuses_before_retaining_an_excess_identity(kernel):
    parents = {200 + n: 100 for n in range(10)}
    with pytest.raises(pc.WindowsCleanupCapacityError):
        pc._descendants_from_parent_map(100, parents, limit=3)
    assert kernel.opened == []


@pytest.mark.asyncio
async def test_posix_factory_and_finish_are_unchanged(kernel, monkeypatch):
    monkeypatch.setattr(pc, "IS_WINDOWS", False)
    factory = AsyncMock(return_value="original result")
    assert await pc.create_windows_cleanup_owned_process(factory) == "original result"
    assert await pc.finish_windows_cleanup_owned_spawn(factory) == "original result"
    assert factory.await_count == 2
    assert not pc._WINDOWS_TREE_ADMISSIONS


@pytest.mark.asyncio
async def test_competing_physical_starts_only_launch_the_last_slot(kernel, monkeypatch):
    monkeypatch.setattr(pc, "_WINDOWS_CLEANUP_ROOT_LIMIT", 1)
    root = kernel.add(100)
    monkeypatch.setattr(pc, "duplicate_asyncio_process_handle", lambda p: root)
    entered, finish = asyncio.Event(), asyncio.Event()
    calls = []

    async def factory(**kwargs):
        calls.append(True)
        entered.set()
        await finish.wait()
        return SimpleNamespace(pid=100, wait=AsyncMock(return_value=0))

    tasks = [
        asyncio.create_task(pc.create_windows_cleanup_owned_process(factory)) for _ in range(8)
    ]
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert calls == [True]
        assert sum(task.done() for task in tasks) == 7
        assert len(pc._WINDOWS_TREE_ADMISSIONS) == 1
        finish.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
        processes = [result for result in results if not isinstance(result, BaseException)]
        assert len(processes) == 1
        assert sum(isinstance(result, pc.WindowsCleanupCapacityError) for result in results) == 7
        await pc.terminate_windows_asyncio_tree(processes[0])
        assert not pc._WINDOWS_TREE_ADMISSIONS
    finally:
        finish.set()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)


def test_process_snapshot_is_bounded_during_enumeration(kernel, monkeypatch):
    import ctypes

    next_pid = [100]
    closed = []

    def first(handle, entry):
        entry._obj.th32ProcessID = next_pid[0]
        entry._obj.th32ParentProcessID = 1
        return True

    def next_(handle, entry):
        next_pid[0] += 1
        return first(handle, entry)

    api = SimpleNamespace(
        CreateToolhelp32Snapshot=lambda *args: 777,
        Process32First=first,
        Process32Next=next_,
        CloseHandle=lambda handle: closed.append(handle),
        SetLastError=lambda error: None,
        GetLastError=lambda: 0,
    )
    monkeypatch.setattr(
        pc,
        "ctypes",
        SimpleNamespace(
            windll=SimpleNamespace(kernel32=api),
            POINTER=ctypes.POINTER,
            byref=ctypes.byref,
            sizeof=ctypes.sizeof,
        ),
    )
    monkeypatch.setattr(pc, "_WINDOWS_CLEANUP_SNAPSHOT_LIMIT", 3)
    with pytest.raises(pc.WindowsCleanupCapacityError) as caught:
        kernel.snapshot()
    frames = list(caught.traceback)
    storage = frames[-1].frame.f_locals["result"]
    assert len(storage) == 3
    assert next_pid == [103]
    assert closed == [777]
    assert kernel.opened == []


def test_snapshot_overflow_after_child_open_keeps_old_pins_and_stays_manual(kernel, monkeypatch):
    root = kernel.add(100)
    kernel.add(201, 100, 20)
    kernel.denied.add(root)
    with pytest.raises(OSError, match="fixture denied"):
        pc.terminate_windows_process_tree_owned(root)
    state = pc._PENDING_WINDOWS_TREE_CLEANUPS[(100, 10)]
    owned = dict(state.handles)
    kernel.add(301, 201, 30)
    calls = [0]

    def snapshot():
        calls[0] += 1
        if calls[0] == 2:
            raise pc._WindowsTreeOverflow("fixture second snapshot overflow")
        return dict(kernel.parents)

    monkeypatch.setattr(pc, "_windows_process_parent_map", snapshot)
    assert pc.retry_pending_windows_process_trees() == ()
    assert state.manual_required
    assert state.handles == owned
    assert kernel.closed == [3010], "only the newly opened, unvalidated candidate may close"
    assert pc.retry_pending_windows_process_trees() == ()
    assert calls == [2], "a manually unresolved tree must not be rescanned"
    assert len(pc._WINDOWS_TREE_ADMISSIONS) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["duplicate", "identity"])
async def test_unverifiable_spawn_keeps_original_pin_and_can_recover(kernel, monkeypatch, missing):
    root = kernel.add(100)
    original_pin = 1001  # inert stand-in for CPython's reference-counted Handle
    process = SimpleNamespace(
        pid=100,
        _transport=SimpleNamespace(
            get_extra_info=lambda name: SimpleNamespace(_handle=original_pin)
        ),
    )
    monkeypatch.setattr(
        pc, "duplicate_asyncio_process_handle", lambda p: None if missing == "duplicate" else root
    )
    if missing == "identity":
        monkeypatch.setattr(pc, "_windows_process_handle_identity", lambda h: None)
    with pytest.raises(OSError, match="original child pin"):
        await pc.create_windows_cleanup_owned_process(AsyncMock(return_value=process))
    state = process._windows_cleanup_state
    assert state.raw_root_pin == (original_pin if missing == "duplicate" else None)
    assert not state.manual_required, "a transient read failure is not overflow"
    assert len(pc._WINDOWS_TREE_ADMISSIONS) == 1
    assert kernel.closed == []
    assert pc.retry_pending_windows_process_trees() == ()
    assert kernel.identities[root][2] is None
    extra = pc.reserve_windows_tree_cleanup()
    pc.release_windows_tree_reservation(extra)
    with pytest.raises(OSError, match="pending verification"):
        pc.kill_process_tree_pinned(100, "10")
    # The same pinned object becomes readable; no PID lookup supplies authority.
    monkeypatch.setattr(pc, "_windows_process_handle_identity", kernel.identities.get)
    kernel.identities[root] = kernel.identities[original_pin] = (100, 10, 200)
    assert pc.retry_pending_windows_process_trees() == (100,)
    assert not pc._WINDOWS_TREE_ADMISSIONS
    assert kernel.closed == ([] if missing == "duplicate" else [root])


@pytest.mark.asyncio
async def test_failed_executor_submission_preserves_admitted_pin_for_maintenance(
    kernel, monkeypatch
):
    root = kernel.add(100)
    monkeypatch.setattr(pc, "duplicate_asyncio_process_handle", lambda p: root)
    process = SimpleNamespace(pid=100, wait=AsyncMock(return_value=0))
    owned = await pc.create_windows_cleanup_owned_process(AsyncMock(return_value=process))

    def unavailable():
        raise RuntimeError("executor unavailable")

    monkeypatch.setattr(pc, "subprocess_executor", unavailable)
    with pytest.raises(RuntimeError, match="executor unavailable"):
        await pc.terminate_windows_asyncio_tree(owned)
    assert pc._PENDING_WINDOWS_TREE_CLEANUPS[(100, 10)].handles == {100: root}
    assert len(pc._WINDOWS_TREE_ADMISSIONS) == 1
    assert kernel.closed == []
    assert pc.retry_pending_windows_process_trees() == (100,)
    assert not pc._WINDOWS_TREE_ADMISSIONS


def test_pending_root_tracking_survives_sweep_and_delayed_writeback(kernel, monkeypatch, tmp_path):
    from kiro_crew import session_pid

    root = kernel.add(100)
    kernel.denied.add(root)
    with pytest.raises(OSError, match="fixture denied"):
        pc.terminate_windows_process_tree_owned(root)
    state = pc._PENDING_WINDOWS_TREE_CLEANUPS[(100, 10)]
    pc._manual_windows_tree(state)
    kernel.exit_(100)
    monkeypatch.setattr(pc, "pid_liveness", lambda pid: pytest.fail("pending tree liveness bypass"))
    lines = ["42:100:10", "42:100", "100"]
    assert session_pid._sweep_pid_entries(
        lines, should_skip_tagged=lambda *a: False, should_skip_bare=lambda *a: False
    ) == (0, set(), [])
    monkeypatch.setattr(session_pid, "config_dir", lambda: tmp_path)
    path = session_pid._session_pid_file_path()
    path.write_text("42:100:10\n42:900:90\n", encoding="utf-8")
    session_pid._write_back_pid_file({"42:100:10", "42:900:90"})
    assert path.read_text(encoding="utf-8") == "42:100:10\n"
    assert len(pc._WINDOWS_TREE_ADMISSIONS) == 1
    assert kernel.closed == []


def test_pending_root_tracking_survives_the_tracked_child_sweep(kernel, monkeypatch, tmp_path):
    from kiro_crew import session_pid

    root = kernel.add(100)
    kernel.denied.add(root)
    with pytest.raises(OSError, match="fixture denied"):
        pc.terminate_windows_process_tree_owned(root)
    pc._manual_windows_tree(pc._PENDING_WINDOWS_TREE_CLEANUPS[(100, 10)])
    kernel.exit_(100)
    monkeypatch.setattr(session_pid, "config_dir", lambda: tmp_path)
    # A row for the pending tree must not even be PROBED: every arm the probe
    # leads to ends in retiring the row, including the arm reached when the kill
    # itself raises. 43:900:90 is an ordinary dead child and still gets pruned,
    # so the sweep is guarded rather than disabled.
    monkeypatch.setattr(
        pc,
        "pid_exists",
        lambda pid: pytest.fail(f"pending tree row probed: {pid}") if pid in (42, 100) else False,
    )
    monkeypatch.setattr(pc, "kill_pid", lambda *a: pytest.fail("pending tree killed by bare pid"))
    path = session_pid._pid_file_path()
    path.write_text("42:100:10\n100\n43:900:90\n", encoding="utf-8")
    assert session_pid._cleanup_orphaned_mcp_servers() == 0
    assert path.read_text(encoding="utf-8") == "42:100:10\n100\n"
    assert len(pc._WINDOWS_TREE_ADMISSIONS) == 1
    assert kernel.closed == []


@pytest.mark.asyncio
async def test_creation_task_scheduling_failure_refunds_without_calling_factory(kernel):
    factory = AsyncMock()

    def refuse(coro):
        raise RuntimeError("fixture task scheduling refused")

    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(asyncio, "ensure_future", refuse)
        with pytest.raises(RuntimeError, match="task scheduling refused"):
            await pc.create_windows_cleanup_owned_process(factory)
    factory.assert_not_called()
    assert not pc._WINDOWS_TREE_ADMISSIONS


@pytest.mark.asyncio
async def test_slow_identity_read_leaves_the_event_loop_serving(kernel, monkeypatch):
    """A child that dies the instant it resumes must not freeze session start.

    ``_windows_process_handle_identity`` polls for an exit FILETIME the kernel
    has not published yet, so on an already-exited child the read takes tenths
    of a second. The bind runs during session start on a loop that is serving
    every other session, so that wait belongs on the subprocess executor. This
    pins both halves of the contract: an unrelated task keeps running WHILE the
    read is in flight, and the exact handle is already pinned when it starts, so
    a cancellation landing in the read cannot lose the child.
    """
    blocked_secs = 0.3
    root = kernel.add(100)
    monkeypatch.setattr(pc, "duplicate_asyncio_process_handle", lambda p: root)
    process = SimpleNamespace(pid=100, _transport=None)
    identity_of = kernel.identities.get
    window: dict[str, float] = {}
    pinned_during_read: list[dict[int, int]] = []

    def slow_identity(handle):
        # Real sleep: the fixture's fake ``pc.time`` must not shorten the block,
        # and this must occupy whichever thread actually performs the read.
        window["started"] = time.monotonic()
        pinned_during_read.append(dict(process._windows_cleanup_state.handles))
        time.sleep(blocked_secs)
        window["finished"] = time.monotonic()
        return identity_of(handle)

    monkeypatch.setattr(pc, "_windows_process_handle_identity", slow_identity)

    served: list[float] = []
    serving = True

    async def other_session_work() -> None:
        while serving:
            served.append(time.monotonic())
            await asyncio.sleep(0.01)

    heartbeat = asyncio.create_task(other_session_work())
    owned = await asyncio.wait_for(
        pc.create_windows_cleanup_owned_process(AsyncMock(return_value=process)), 10
    )
    serving = False
    await asyncio.wait_for(heartbeat, 5)

    assert owned is process
    # Windows' monotonic clock is coarse enough to report a 0.3s sleep as 0.296s,
    # so allow granularity slack; the margin still dwarfs the 0.01s tick below.
    assert window["finished"] - window["started"] >= blocked_secs * 0.8, "the read really blocked"
    assert [tick for tick in served if window["started"] < tick < window["finished"]], (
        "the loop served nothing while the identity read was in flight: the read "
        "is running ON the loop instead of the subprocess executor"
    )
    # Pinned BEFORE the read, so a cancellation inside it still retains the child.
    assert pinned_during_read == [{100: root}]
    assert process._windows_cleanup_state.key == (100, 10)
