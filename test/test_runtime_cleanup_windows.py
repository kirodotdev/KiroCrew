"""Native Windows runtime cleanup, without an agent login or a live gateway.

Python workers stand in for runtime/agent/MCP processes. Kernel tree termination,
creation FILETIME, PID-file IO and provider teardown remain real. These are
process-lifecycle integration tests, not real Kiro CLI or model-turn acceptance.
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import sys
import textwrap
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from kiro_crew import platform_compat as pc
from kiro_crew import session_pid
from kiro_crew.acp import runtime
from kiro_crew.acp.session_provider import AcpSessionProvider

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="native Windows process trees")


@pytest.fixture(autouse=True)
def _isolate_pending_windows_tree_cleanup(monkeypatch):
    pending = {}
    monkeypatch.setattr(pc, "_WINDOWS_TREE_ADMISSIONS", set())
    monkeypatch.setattr(pc, "_PENDING_WINDOWS_TREE_CLEANUPS", pending)
    yield
    # These handles are the only thing that can still reach a retained tree, so
    # closing them is an abandonment unless the tree is gone first. Drain each
    # one and CONFIRM exit before closing, then fail loudly on a survivor: a
    # test-owned process must never outlive the test that created it. Handles
    # are closed and the registry cleared either way, so one stuck tree cannot
    # leak pins into the next test.
    survivors: list[int] = []
    try:
        for state in set(pending.values()) | pc._WINDOWS_TREE_ADMISSIONS:
            handles = tuple(state.handles.values())
            for handle in handles:
                observed = pc._windows_process_handle_identity(handle)
                if observed is not None and observed[2] is None:
                    pc.terminate_process_handle(handle)
            deadline = time.monotonic() + 10
            while True:
                alive = [
                    observed[0]
                    for observed in (pc._windows_process_handle_identity(h) for h in handles)
                    if observed is not None and observed[2] is None
                ]
                if not alive or time.monotonic() >= deadline:
                    survivors.extend(alive)
                    break
                time.sleep(0.02)
            for handle in handles:
                pc.close_process_handle(handle)
            state.handles.clear()
            state.retired = True
    finally:
        pending.clear()
    assert not survivors, f"retained owned processes outlived teardown: {survivors}"


# Every worker has an independent, bounded escape hatch. Cleanup does not rely on
# the production tree-kill function whose regression these tests must catch.
_WORKER = textwrap.dedent("""\
    import json
    import os
    import pathlib
    import subprocess
    import sys
    import time

    directory = pathlib.Path(sys.argv[1])
    depth = int(sys.argv[2])
    child = None
    if depth:
        child = subprocess.Popen(
            [sys.executable, '-I', '-S', '-B', __file__, str(directory), str(depth - 1)],
            cwd=directory,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW,
        )
    retained = bytearray(1024 * 1024)
    retained[0] = 1
    (directory / f'{depth}.json').write_text(
        json.dumps({'pid': os.getpid(), 'parent': os.getppid()}), encoding='utf-8'
    )
    deadline = time.monotonic() + 60
    while not (directory / 'finish').exists() and time.monotonic() < deadline:
        if depth == 2 and (directory / 'root-exit').exists():
            sys.exit(0)
        time.sleep(0.02)
    if child is not None:
        child.wait(timeout=10)
    """)


def _identity(pid, token, handle):
    observed = pc._windows_process_handle_identity(handle)
    assert observed is not None, f"cannot read owned process identity: {pid}"
    assert observed[:2] == (pid, int(token)), "owned process identity changed"
    return observed


async def _assert_exited(identities):
    deadline = time.monotonic() + 10
    while True:
        alive = [
            pid for pid, token, handle in identities if _identity(pid, token, handle)[2] is None
        ]
        if not alive:
            return
        assert time.monotonic() < deadline, f"owned processes still alive: {alive}"
        await asyncio.sleep(0.02)


@asynccontextmanager
async def _owned_tree(directory, *, depth=2, admitted=False):
    directory.mkdir()
    script = directory / "worker.py"
    script.write_text(_WORKER, encoding="utf-8")
    python = getattr(sys, "_base_executable", sys.executable)
    from kiro_crew.sandbox import create_subprocess_limited

    spawn = functools.partial(
        create_subprocess_limited if admitted else asyncio.create_subprocess_exec,
        python,
        "-I",
        "-S",
        "-B",
        str(script),
        str(directory),
        str(depth),
        cwd=directory,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        creationflags=pc.CREATE_NEW_PROCESS_GROUP | pc._SUBPROCESS_NO_WINDOW,
    )
    process = await pc.create_windows_cleanup_owned_process(spawn) if admitted else await spawn()
    identities = []
    try:
        # Capture the directly spawned object before observing any descendant.
        token = pc.get_process_start_id(process.pid)
        assert token is not None
        handle = pc.open_process_termination_handle(process.pid, token)
        assert handle is not None
        identities.append((process.pid, token, handle))
        parent = process.pid
        deadline = time.monotonic() + 15
        for level in range(depth - 1, -1, -1):
            while True:
                try:
                    row = json.loads((directory / f"{level}.json").read_text(encoding="utf-8"))
                    break
                except (FileNotFoundError, json.JSONDecodeError):
                    assert process.returncode is None, "fixture root exited before ready"
                    assert time.monotonic() < deadline, "owned process tree did not start"
                    await asyncio.sleep(0.02)
            assert row["parent"] == parent
            child = row["pid"]
            child_token = pc.get_process_start_id(child)
            assert child_token is not None
            child_handle = pc.open_process_termination_handle(child, child_token)
            assert child_handle is not None
            identities.append((child, child_token, child_handle))
            assert int(child_token) > int(identities[-2][1])
            assert pc.get_ppid(child) == parent
            assert all(_identity(*item)[2] is None for item in identities)
            parent = child
        assert all(_identity(*item)[2] is None for item in identities)
        yield SimpleNamespace(process=process, identities=identities, directory=directory)
    finally:
        (directory / "finish").touch()
        try:
            try:
                # A partial startup still owns children we may not have observed.
                # The worker joins its own child on this cooperative exit path.
                await asyncio.wait_for(process.wait(), 10)
            except asyncio.TimeoutError:
                for pid, token, handle in reversed(identities):
                    if _identity(pid, token, handle)[2] is None:
                        pc.terminate_process_handle(handle)
                if process.returncode is None:
                    process.kill()
                await asyncio.wait_for(process.wait(), 10)
            await _assert_exited(identities)
        finally:
            for _, _, handle in identities:
                pc.close_process_handle(handle)


def _attach_runtime(tree, home):
    rt = runtime.AcpRuntime(work_dir=home, expect_mcp_reports=False)
    rt._process = tree.process
    rt._pid, rt._start_time, _ = tree.identities[0]
    rt._initialized = True
    return rt


def _persistent_handle():
    # ``AcpSessionProvider.shutdown`` reads ``handle.memory_mode`` before it
    # kills an owned runtime; a persistent handle takes the direct
    # ``runtime.kill`` path these tests exercise, so a missing attribute cannot
    # be swallowed by shutdown's best-effort guard and read as a reclaim failure.
    return SimpleNamespace(memory_mode="persistent")


def _register(pid):
    session_pid._track_pid(pid)
    session_pid._track_session_pid(pid)
    session_pid.register_protected_pid(pid)


def _ledger_lines(home, name):
    path = home / name
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


@pytest.mark.asyncio
async def test_repeated_windows_provider_shutdown_reclaims_entire_tree(tmp_path, monkeypatch):
    """Three owner lifetimes drain all generations, not just the root PID."""
    monkeypatch.setattr(session_pid, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(session_pid, "_PROTECTED_PIDS", set())
    with ThreadPoolExecutor(max_workers=1) as executor:
        monkeypatch.setattr(runtime, "subprocess_executor", lambda: executor)
        async with _owned_tree(tmp_path / "unrelated", depth=0) as unrelated:
            for cycle in range(3):
                async with _owned_tree(tmp_path / f"cycle-{cycle}") as tree:
                    rt = _attach_runtime(tree, tmp_path)
                    provider = AcpSessionProvider(_persistent_handle(), rt, owns_runtime=True)
                    await asyncio.to_thread(_register, rt.pid)
                    assert _ledger_lines(tmp_path, "kiro_pids.txt") == [str(rt.pid)]
                    assert _ledger_lines(tmp_path, "kiro_session_pids.txt") == [
                        f"{os.getpid()}:{rt.pid}:{rt._start_time}"
                    ]
                    assert all(pc.proc_rss_bytes_for_pid(pid) > 0 for pid, _, _ in tree.identities)

                    await asyncio.wait_for(provider.shutdown(), 15)
                    await _assert_exited(tree.identities)
                    assert rt._dead and rt._process is None
                    assert rt.pid not in session_pid._PROTECTED_PIDS
                    assert _ledger_lines(tmp_path, "kiro_pids.txt") == []
                    assert _ledger_lines(tmp_path, "kiro_session_pids.txt") == []
                    assert _identity(*unrelated.identities[0])[2] is None
                    await asyncio.wait_for(provider.shutdown(), 5)
                    assert _identity(*unrelated.identities[0])[2] is None


@pytest.mark.asyncio
async def test_windows_sync_provider_fallback_reclaims_grandchildren(tmp_path):
    async with _owned_tree(tmp_path / "unrelated", depth=0) as unrelated:
        async with _owned_tree(tmp_path / "runtime") as tree:
            pid, start, _ = tree.identities[0]
            provider = SimpleNamespace(_client=SimpleNamespace(_pid=pid, _start_time=start))
            await asyncio.wait_for(asyncio.to_thread(session_pid._sync_kill_provider, provider), 15)
            await _assert_exited(tree.identities)
            assert _identity(*unrelated.identities[0])[2] is None


@pytest.mark.asyncio
async def test_windows_failed_tree_kill_keeps_live_pid_records(tmp_path, monkeypatch):
    """A returning kill helper does not prove that the native process exited."""
    monkeypatch.setattr(session_pid, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(session_pid, "_PROTECTED_PIDS", set())
    clock = [0.0]
    monkeypatch.setattr(
        pc,
        "time",
        SimpleNamespace(
            monotonic=lambda: clock[0],
            sleep=lambda _: clock.__setitem__(0, clock[0] + 10.0),
        ),
    )
    calls = []
    monkeypatch.setattr(pc, "terminate_process_handle", lambda handle: calls.append(handle))
    with ThreadPoolExecutor(max_workers=1) as executor:
        monkeypatch.setattr(runtime, "subprocess_executor", lambda: executor)
        async with _owned_tree(tmp_path / "runtime") as tree:
            rt = _attach_runtime(tree, tmp_path)
            await asyncio.to_thread(_register, rt.pid)
            before = [
                _ledger_lines(tmp_path, name) for name in ("kiro_pids.txt", "kiro_session_pids.txt")
            ]

            with pytest.raises(OSError, match="did not drain"):
                await asyncio.wait_for(rt.kill(expected=True), 5)

            assert calls
            assert rt._process is tree.process
            assert all(_identity(*item)[2] is None for item in tree.identities)
            assert [
                _ledger_lines(tmp_path, name) for name in ("kiro_pids.txt", "kiro_session_pids.txt")
            ] == before


@pytest.mark.asyncio
@pytest.mark.parametrize("identity_state", ["mismatch", "unreadable"])
async def test_windows_sync_cleanup_preserves_unverified_tree(
    tmp_path, monkeypatch, identity_state
):
    async with _owned_tree(tmp_path / "runtime") as tree:
        pid, start, _ = tree.identities[0]
        recorded = str(int(start) + 1) if identity_state == "mismatch" else start
        provider = SimpleNamespace(_client=SimpleNamespace(_pid=pid, _start_time=recorded))
        if identity_state == "unreadable":
            monkeypatch.setattr(pc, "get_process_start_id", lambda _: None)

        await asyncio.wait_for(asyncio.to_thread(session_pid._sync_kill_provider, provider), 5)

        assert all(_identity(*item)[2] is None for item in tree.identities)


@pytest.mark.asyncio
async def test_windows_owner_shutdown_reclaims_children_after_root_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(session_pid, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(session_pid, "_PROTECTED_PIDS", set())
    with ThreadPoolExecutor(max_workers=1) as executor:
        monkeypatch.setattr(runtime, "subprocess_executor", lambda: executor)
        async with _owned_tree(tmp_path / "runtime") as tree:
            rt = _attach_runtime(tree, tmp_path)
            provider = AcpSessionProvider(_persistent_handle(), rt, owns_runtime=True)
            await asyncio.to_thread(_register, rt.pid)
            (tree.directory / "root-exit").touch()
            assert await asyncio.wait_for(tree.process.wait(), 10) == 0
            assert _identity(*tree.identities[0])[2] is not None
            assert all(_identity(*item)[2] is None for item in tree.identities[1:])

            await asyncio.wait_for(provider.shutdown(), 15)

            await _assert_exited(tree.identities)
            assert _ledger_lines(tmp_path, "kiro_pids.txt") == []
            assert _ledger_lines(tmp_path, "kiro_session_pids.txt") == []


@pytest.mark.asyncio
async def test_windows_direct_client_shutdown_reclaims_children_after_root_exit(
    tmp_path, monkeypatch
):
    from kiro_crew.acp.client import AcpClient

    monkeypatch.setattr(session_pid, "config_dir", lambda: tmp_path)
    async with _owned_tree(tmp_path / "client") as tree:
        client = AcpClient(work_dir=tmp_path)
        client._process = tree.process
        client._pid, client._start_time, _ = tree.identities[0]
        await asyncio.to_thread(_register, client._pid)
        (tree.directory / "root-exit").touch()
        assert await asyncio.wait_for(tree.process.wait(), 10) == 0
        assert all(_identity(*item)[2] is None for item in tree.identities[1:])

        await asyncio.wait_for(client.shutdown(), 15)

        await _assert_exited(tree.identities)
        assert client._process is None
        assert _ledger_lines(tmp_path, "kiro_pids.txt") == []
        assert _ledger_lines(tmp_path, "kiro_session_pids.txt") == []


@pytest.mark.asyncio
async def test_windows_failed_owner_drain_recovers_after_owner_drop(tmp_path, monkeypatch):
    """The maintenance tick finishes a refused exact tree after its owners vanish."""

    monkeypatch.setattr(session_pid, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(session_pid, "_PROTECTED_PIDS", set())
    with ThreadPoolExecutor(max_workers=1) as executor:
        monkeypatch.setattr(runtime, "subprocess_executor", lambda: executor)
        async with _owned_tree(tmp_path / "unrelated", depth=0) as unrelated:
            async with _owned_tree(tmp_path / "runtime") as tree:
                rt = _attach_runtime(tree, tmp_path)
                provider = AcpSessionProvider(_persistent_handle(), rt, owns_runtime=True)
                await asyncio.to_thread(_register, rt.pid)

                discover = pc.descendant_termination_handles
                close_handle = pc.close_process_handle
                closed_identities = {}
                calls = [0]
                refused = [True]

                def close_with_identity_receipt(handle):
                    identity = pc._windows_process_handle_identity(handle)
                    if identity is not None:
                        closed_identities[identity[0]] = identity
                    close_handle(handle)

                def transient_discovery_failure(pid, retained, root_handle):
                    calls[0] += 1
                    if refused[0] and calls[0] == 2:
                        raise OSError("temporary native discovery refusal")
                    return discover(pid, retained, root_handle)

                monkeypatch.setattr(pc, "close_process_handle", close_with_identity_receipt)
                monkeypatch.setattr(
                    pc, "descendant_termination_handles", transient_discovery_failure
                )
                async with asyncio.timeout(15):
                    await provider.shutdown()

                assert len(pc._PENDING_WINDOWS_TREE_CLEANUPS) == 1
                pending = next(iter(pc._PENDING_WINDOWS_TREE_CLEANUPS.values()))
                pending_pids = set(pending.handles)
                assert {pid for pid, _, _ in tree.identities} <= pending_pids
                assert _identity(*tree.identities[0])[2] is not None
                assert all(_identity(*item)[2] is None for item in tree.identities[1:])
                assert all(
                    getattr(pending, slot) is not provider and getattr(pending, slot) is not rt
                    for slot in pending.__slots__
                ), "pending state retained the provider/runtime graph"

                del provider
                del rt
                refused[0] = False
                assert await asyncio.to_thread(session_pid.cleanup_orphaned_session_roots) == 1
                assert pending_pids <= set(closed_identities)
                assert all(closed_identities[pid][2] is not None for pid in pending_pids)
                await _assert_exited(tree.identities)
                assert pc._PENDING_WINDOWS_TREE_CLEANUPS == {}
                assert _ledger_lines(tmp_path, "kiro_pids.txt") == []
                assert _ledger_lines(tmp_path, "kiro_session_pids.txt") == []
                assert tree.identities[0][0] not in session_pid._PROTECTED_PIDS
                assert _identity(*unrelated.identities[0])[2] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("direct_client", [False, True])
async def test_native_admission_and_owner_shutdown_refund_after_verified_drain(
    tmp_path, monkeypatch, direct_client
):
    from kiro_crew.acp.client import AcpClient

    monkeypatch.setattr(session_pid, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(session_pid, "_PROTECTED_PIDS", set())
    async with _owned_tree(tmp_path / "unrelated", depth=0) as unrelated:
        async with _owned_tree(tmp_path / "admitted", admitted=True) as tree:
            assert len(pc._WINDOWS_TREE_ADMISSIONS) == 1
            assert pc._PENDING_WINDOWS_TREE_CLEANUPS == {}
            if direct_client:
                owner = AcpClient(work_dir=tmp_path)
                owner._process = tree.process
                owner._pid, owner._start_time, _ = tree.identities[0]
            else:
                owner = _attach_runtime(tree, tmp_path)
            await asyncio.to_thread(_register, tree.process.pid)
            if direct_client:
                await asyncio.wait_for(owner.shutdown(), 15)
            else:
                await asyncio.wait_for(owner.kill(expected=True), 15)
            # Product cleanup must finish BEFORE the independent fixture finalizer.
            await _assert_exited(tree.identities)
            assert not pc._WINDOWS_TREE_ADMISSIONS
            assert not pc._PENDING_WINDOWS_TREE_CLEANUPS
            assert _ledger_lines(tmp_path, "kiro_pids.txt") == []
            assert _ledger_lines(tmp_path, "kiro_session_pids.txt") == []
            assert _identity(*unrelated.identities[0])[2] is None


@pytest.fixture
def creation_kernel(monkeypatch, event_loop):
    """Keep CPython Popen/transport code; replace only native process/pipe APIs."""
    import subprocess
    from asyncio import windows_utils

    alive = [False]
    closed = []
    kills = []
    created = []
    mode = ["registration"]
    popen_refs = []

    class Handle(int):
        closed = False

        def Close(self):
            if not self.closed:
                self.closed = True
                closed.append(int(self))

        __del__ = Close

    class Popen(windows_utils.Popen):
        def __init__(self, *args, **kwargs):
            popen_refs.append(__import__("weakref").ref(self))
            super().__init__(*args, **kwargs)

        def _get_handles(self, *args):
            return (-1,) * 6

        def _close_pipe_fds(self, *args):
            self._closed_child_pipe_fds = True
            if mode[0] == "pipe_fds":
                raise OSError("fixture pipe-fd close failed")

        def _internal_poll(self, *args, **kwargs):
            return None if alive[0] else 1

        def kill(self):
            kills.append(True)
            raise PermissionError("fixture termination denied")

    def create(*args):
        if mode[0] == "pre_create":
            raise OSError("fixture creation failed")
        alive[0] = True
        created.append(True)
        return 987654, 987655, 123456, 123457

    def registration(handle):
        assert handle == 987654
        if mode[0] == "registration":
            raise OSError("fixture proactor registration failed")
        return asyncio.get_running_loop().create_future()

    monkeypatch.setattr(windows_utils, "Popen", Popen)
    monkeypatch.setattr(subprocess, "Handle", Handle)
    monkeypatch.setattr(subprocess._winapi, "CreateProcess", create)
    monkeypatch.setattr(subprocess._winapi, "CloseHandle", lambda h: closed.append(h))
    monkeypatch.setattr(event_loop._proactor, "wait_for_handle", registration)
    monkeypatch.setattr(
        pc, "close_process_handle", lambda h: pytest.fail("borrowed pin explicitly closed")
    )
    monkeypatch.setattr(
        pc, "_windows_process_handle_identity", lambda h: (123456, 10, None if alive[0] else 20)
    )
    monkeypatch.setattr(pc, "descendant_termination_handles", lambda *args: {})
    monkeypatch.setattr(pc, "terminate_process_handle", lambda h: alive.__setitem__(0, False))
    return SimpleNamespace(**locals())


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["registration", "pipe_fds", "pipe_setup"])
async def test_creation_failure_before_process_return_keeps_cleanup_ownership(
    monkeypatch, creation_kernel, failure
):
    """Actual factory/CPython transport; a denied kill cannot certify absence."""
    from asyncio import base_subprocess

    from kiro_crew import sandbox

    k = creation_kernel
    k.mode[0] = failure
    if failure == "pipe_setup":

        async def fail_pipes(self, waiter):
            waiter.set_exception(OSError("fixture async pipe setup failed"))

        monkeypatch.setattr(base_subprocess.BaseSubprocessTransport, "_connect_pipes", fail_pipes)

    async def factory(**kwargs):
        return await sandbox.create_subprocess_limited(
            "never-executed",
            stdin=None,
            stdout=None,
            stderr=None,
            creationflags=pc.CREATE_SUSPENDED,
            **kwargs,
        )

    with pytest.raises(OSError, match="fixture"):
        await pc.create_windows_cleanup_owned_process(factory)
    assert k.alive == [True]
    assert k.kills == ([] if failure == "pipe_fds" else [True])
    assert len(pc._WINDOWS_TREE_ADMISSIONS) == 1, "a surviving child lost its reservation"
    state = next(iter(pc._WINDOWS_TREE_ADMISSIONS))
    assert state.handles == {123456: 987654}, "the original cleanup pin was lost"
    assert 987654 not in k.closed
    await asyncio.sleep(0)  # settle shield callbacks holding the failed task
    __import__("gc").collect()
    assert all(ref() is None for ref in k.popen_refs), "cleanup retained the Popen/transport graph"
    assert 987654 not in k.closed
    assert pc.retry_pending_windows_process_trees() == (123456,)
    assert k.alive == [False]
    assert not pc._WINDOWS_TREE_ADMISSIONS
    __import__("gc").collect()
    assert k.closed.count(987654) == 1


@pytest.mark.asyncio
async def test_precreation_failure_refunds_captured_factory(creation_kernel):
    from kiro_crew import sandbox

    creation_kernel.mode[0] = "pre_create"

    async def factory(**kwargs):
        return await sandbox.create_subprocess_limited(
            "never-executed", stdin=None, stdout=None, stderr=None, **kwargs
        )

    with pytest.raises(OSError, match="creation failed"):
        await pc.create_windows_cleanup_owned_process(factory)
    assert creation_kernel.alive == [False]
    assert creation_kernel.created == []
    assert not pc._WINDOWS_TREE_ADMISSIONS
    assert not pc._PENDING_WINDOWS_TREE_CLEANUPS


@pytest.mark.asyncio
async def test_repeated_cancel_during_pipe_setup_keeps_creation_pin(monkeypatch, creation_kernel):
    from asyncio import base_subprocess

    from kiro_crew import sandbox

    entered, release = asyncio.Event(), asyncio.Event()
    creation_kernel.mode[0] = "pipe_setup"

    async def fail_pipes(self, waiter):
        entered.set()
        await release.wait()
        waiter.set_exception(OSError("fixture pipe setup failed"))

    monkeypatch.setattr(base_subprocess.BaseSubprocessTransport, "_connect_pipes", fail_pipes)

    async def factory(**kwargs):
        return await sandbox.create_subprocess_limited(
            "never-executed", stdin=None, stdout=None, stderr=None, **kwargs
        )

    task = asyncio.create_task(pc.create_windows_cleanup_owned_process(factory))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert len(pc._WINDOWS_TREE_ADMISSIONS) == 1
        assert 987654 not in creation_kernel.closed
        release.set()
        with pytest.raises(PermissionError, match="termination denied"):
            await asyncio.wait_for(task, 5)
        assert len(pc._PENDING_WINDOWS_TREE_CLEANUPS) == 1
        assert creation_kernel.alive == [True]
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
    await asyncio.sleep(0)
    assert pc.retry_pending_windows_process_trees() == (123456,)
    assert not pc._WINDOWS_TREE_ADMISSIONS


@pytest.mark.asyncio
async def test_owned_factory_does_not_intercept_untracked_native_spawns(tmp_path):
    import subprocess
    from asyncio import windows_utils

    loop = asyncio.get_running_loop()
    originals = (
        asyncio.create_subprocess_exec,
        windows_utils.Popen,
        subprocess.Popen,
        loop._make_subprocess_transport.__func__,
    )
    async with _owned_tree(tmp_path / "untracked", depth=0) as other:
        async with _owned_tree(tmp_path / "tracked", admitted=True) as tracked:
            assert not hasattr(other.process, "_windows_cleanup_state")
            assert len(pc._WINDOWS_TREE_ADMISSIONS) == 1
            assert originals == (
                asyncio.create_subprocess_exec,
                windows_utils.Popen,
                subprocess.Popen,
                loop._make_subprocess_transport.__func__,
            )
            await asyncio.wait_for(pc.terminate_windows_asyncio_tree(tracked.process), 15)
            await _assert_exited(tracked.identities)
            assert _identity(*other.identities[0])[2] is None
            assert not pc._WINDOWS_TREE_ADMISSIONS


@pytest.mark.asyncio
async def test_a_child_exiting_259_reads_as_exited_and_its_drain_finishes(tmp_path):
    """259 is both the value STILL_ACTIVE reserves and an ordinary exit code.

    An exit status alone cannot tell the two apart, so a child that picks 259
    reads back as running for as long as its handle is held: the drain signals it
    once, never reaches a terminal scan, raises at its deadline, and leaves the
    reservation charged until the gateway restarts.
    """

    python = getattr(sys, "_base_executable", sys.executable)
    process = await asyncio.create_subprocess_exec(
        python,
        "-I",
        "-S",
        "-B",
        "-c",
        "raise SystemExit(259)",
        cwd=tmp_path,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        creationflags=pc.CREATE_NEW_PROCESS_GROUP | pc._SUBPROCESS_NO_WINDOW,
    )
    token = pc.get_process_start_id(process.pid)
    assert token is not None
    handle = pc.open_process_termination_handle(process.pid, token)
    assert handle is not None
    try:
        assert await asyncio.wait_for(process.wait(), 15) == 259, "fixture chose another status"
        # The kernel publishes the exit FILETIME just after the status, so read
        # within a bound rather than demanding the first observation carry it.
        deadline = time.monotonic() + 10
        while True:
            observed = pc._windows_process_handle_identity(handle)
            assert observed is not None and observed[0] == process.pid
            if observed[2] is not None:
                break
            assert time.monotonic() < deadline, "an exit status of 259 reads back as running"
            await asyncio.sleep(0.02)

        # The harm at the seam that suffers it: this member must reach a terminal
        # scan, so the drain returns instead of raising with capacity held.
        state = pc._PendingWindowsTreeCleanup(handle, observed)
        drained = await asyncio.get_running_loop().run_in_executor(
            None, pc._drain_windows_process_tree, state
        )
        assert drained is True
        assert state.terminally_scanned == {process.pid}
    finally:
        pc.close_process_handle(handle)
