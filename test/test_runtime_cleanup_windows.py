"""Native Windows runtime cleanup, without an agent login or a live gateway.

Python workers stand in for runtime/agent/MCP processes. Kernel tree termination,
creation FILETIME, PID-file IO and provider teardown remain real. These are
process-lifecycle integration tests, not real Kiro CLI or model-turn acceptance.
"""

from __future__ import annotations

import asyncio
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
    monkeypatch.setattr(pc, "_PENDING_WINDOWS_TREE_CLEANUPS", pending)
    yield
    for state in pending.values():
        for handle in state.handles.values():
            pc.close_process_handle(handle)
        state.handles.clear()
        state.retired = True
    pending.clear()


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
async def _owned_tree(directory, *, depth=2):
    directory.mkdir()
    script = directory / "worker.py"
    script.write_text(_WORKER, encoding="utf-8")
    python = getattr(sys, "_base_executable", sys.executable)
    process = await asyncio.create_subprocess_exec(
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
                    provider = AcpSessionProvider(SimpleNamespace(), rt, owns_runtime=True)
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
            provider = AcpSessionProvider(SimpleNamespace(), rt, owns_runtime=True)
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
                provider = AcpSessionProvider(SimpleNamespace(), rt, owns_runtime=True)
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
