"""Lifecycle composition against a simulated kernel, never host process IDs.

The RPC and kernel tables are fake; descendant walking/capture, PID-file writes,
runtime teardown and the periodic orphan hook are production implementations.
Native signal delivery belongs to the existing platform/process-tree tests.
"""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from kiro_crew import platform_compat, session_pid
from kiro_crew.acp import client, runtime
from kiro_crew.acp.types import (
    METHOD_SESSION_LOAD,
    METHOD_SESSION_NEW,
    METHOD_SESSION_TERMINATE,
    METHOD_SET_MODE,
    JsonRpcMessage,
)
from kiro_crew.session_cleanup import SessionCleanup
from kiro_crew.watchdog import CleanupHook, SessionWatchdog


class SimulatedKernel:
    """Only explicitly seeded processes exist; no operation falls back to the OS."""

    def __init__(self):
        self.tokens = {}
        self.parents = {}
        self.groups = {}
        self.signals = []

    def add(self, pid, parent, group, token):
        self.tokens[pid] = token
        self.parents[pid] = parent
        self.groups[pid] = group

    def exit(self, pid):
        self.tokens.pop(pid, None)
        self.parents.pop(pid, None)
        self.groups.pop(pid, None)
        for child, parent in list(self.parents.items()):
            if parent == pid:
                self.parents[child] = 1

    def children(self, pid):
        return [child for child, parent in self.parents.items() if parent == pid]

    def kill_tree(self, pid, sig):
        # Simulate POSIX killpg: a setsid escapee is NOT reached.
        self.signals.append((pid, sig))
        for child, group in list(self.groups.items()):
            if group == pid:
                self.exit(child)

    def kill_pid(self, pid, sig):
        assert pid in self.tokens, "signal must target a live simulated process"
        self.signals.append((pid, sig))
        self.exit(pid)


@pytest.fixture
def lifecycle(monkeypatch, tmp_path):
    kernel = SimulatedKernel()
    # A closed interface: an unexpected process operation fails rather than
    # reaching the host. Only native file locking remains real on each OS.
    backend = SimpleNamespace(
        IS_WINDOWS=False,
        SIGTERM=platform_compat.SIGTERM,
        SIGKILL=platform_compat.SIGKILL,
        PID_DEAD=platform_compat.PID_DEAD,
        pid_exists=lambda pid: pid in kernel.tokens,
        pid_liveness=lambda pid: (
            platform_compat.PID_ALIVE if pid in kernel.tokens else platform_compat.PID_DEAD
        ),
        get_process_start_id=kernel.tokens.get,
        get_ppid=kernel.parents.get,
        kill_process_tree=kernel.kill_tree,
        # The teardown pins the root's identity across the terminate, so the
        # stand-in has to answer the pinned call as well; it delegates to the same
        # fake kernel once the recorded token still matches, and refuses when it
        # does not, which is the real function's contract.
        kill_process_tree_pinned=lambda pid, start, sig: (
            # Delegates for the side effect and reports success, which is the real
            # function's contract: True once the signal went out, False -- without
            # signalling -- when the identity could not be confirmed.
            (kernel.kill_tree(pid, sig), True)[1]
            if kernel.tokens.get(pid) == start
            else False
        ),
        kill_pid=kernel.kill_pid,
        file_lock=platform_compat.file_lock,
        open_lock_file=platform_compat.open_lock_file,
    )
    for module in (runtime, client, session_pid):
        monkeypatch.setattr(module, "platform_compat", backend)
    monkeypatch.setattr(client, "_direct_children", kernel.children)
    monkeypatch.setattr(client, "_read_basename", lambda pid: b"node")
    monkeypatch.setattr(session_pid, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(session_pid, "os", SimpleNamespace(getpid=lambda: 900))
    monkeypatch.setattr(session_pid, "_accepted_subreaper_pids", lambda: {1})
    monkeypatch.setattr(session_pid, "_PROTECTED_PIDS", set())
    monkeypatch.setattr(runtime, "pooled_session_servers", lambda *_, **__: [])
    kernel.add(900, 1, 900, "gateway")
    with ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(runtime, "subprocess_executor", lambda: pool)
        yield SimpleNamespace(kernel=kernel, home=tmp_path, pool=pool)


def make_runtime(lifecycle, monkeypatch, root=1000):
    kernel = lifecycle.kernel
    kernel.add(root, 900, root, f"root-{root}")
    rt = runtime.AcpRuntime(work_dir=lifecycle.home, expect_mcp_reports=False)
    rt._pid = root
    rt._start_time = kernel.tokens[root]
    rt._initialized = True
    rt._can_load_session = True
    proc = SimpleNamespace(pid=root, returncode=None)

    async def wait():
        assert root not in kernel.tokens
        proc.returncode = -platform_compat.SIGTERM
        return proc.returncode

    proc.wait = wait
    rt._process = proc
    resident = {}
    terminated = []

    async def rpc(method, params, timeout=None):
        if method in (METHOD_SESSION_NEW, METHOD_SESSION_LOAD):
            sid = params.get("sessionId", f"session-{len(resident)}")
            child = root + 1 + len(resident)
            kernel.add(child, root, root, f"child-{child}")
            resident[sid] = child
            return {"sessionId": sid, "modes": {}}
        if method == METHOD_SESSION_TERMINATE:
            sid = params["sessionId"]
            terminated.append(sid)
            kernel.exit(resident.pop(sid))
            return {}
        if method == METHOD_SET_MODE:
            assert params["sessionId"] in resident
            return {}
        raise AssertionError(f"Unexpected RPC: {method}")

    async def send_request(method, params):
        response = await rpc(method, params)
        request_id = rt._next_id
        rt._next_id += 1
        rt._session_queues[params["sessionId"]].put_nowait(
            JsonRpcMessage(id=request_id, result=response)
        )
        return request_id

    monkeypatch.setattr(rt, "_send_and_await", rpc)
    monkeypatch.setattr(rt, "send_request", send_request)
    return rt, resident, terminated


async def open_session(rt, resume):
    if resume:
        return await rt.load_session("", "resumed")
    return await rt.create_session(mcp_servers=[])


def child_lines(lifecycle):
    path = lifecycle.home / "kiro_pids.txt"
    return set(path.read_text(encoding="utf-8").splitlines()) if path.exists() else set()


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True], ids=["new", "load"])
async def test_session_start_persists_new_descendants(lifecycle, monkeypatch, resume):
    rt, resident, _ = make_runtime(lifecycle, monkeypatch)
    sibling = await asyncio.wait_for(rt.create_session(mcp_servers=[]), 5)
    sibling_record = dict(rt._child_pids)
    assert child_lines(lifecycle) == {"1001:1000:child-1001"}

    handle = await asyncio.wait_for(open_session(rt, resume), 5)

    assert resident[handle.session_id] == 1002
    assert rt._child_pids == {**sibling_record, 1002: ("child-1002", b"node")}
    assert child_lines(lifecycle) == {"1001:1000:child-1001", "1002:1000:child-1002"}
    assert rt._session_queues[handle.session_id] is handle._queue
    assert rt._session_queues[sibling.session_id] is sibling._queue
    assert lifecycle.kernel.signals == []


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True], ids=["new", "load"])
async def test_cancelled_descendant_scan_terminates_only_failed_session(
    lifecycle, monkeypatch, resume
):
    rt, resident, terminated = make_runtime(lifecycle, monkeypatch)
    sibling = await asyncio.wait_for(rt.create_session(mcp_servers=[]), 5)
    entered = asyncio.Event()
    snapshot = rt._snapshot_descendants

    async def observed_snapshot(**kwargs):
        entered.set()
        await snapshot(**kwargs)

    monkeypatch.setattr(rt, "_snapshot_descendants", observed_snapshot)
    # Hold the real scan lock. The wrapper signals before delegating, and the
    # task cannot yield back until the production scan waits on this lock.
    await asyncio.wait_for(rt._descendant_scan_lock.acquire(), 5)
    task = asyncio.create_task(open_session(rt, resume))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        failed_sid = "resumed" if resume else "session-1"
        assert resident[failed_sid] == 1002
        assert failed_sid in rt._session_queues
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert terminated == [failed_sid]
        assert resident == {sibling.session_id: 1001}
        assert 1002 not in lifecycle.kernel.tokens
        assert rt._session_queues == {sibling.session_id: sibling._queue}
        assert child_lines(lifecycle) == {"1001:1000:child-1001"}
        assert rt.is_alive()
        # Exercise a real handle operation after the failed co-tenant's unwind.
        await asyncio.wait_for(sibling.set_mode("kirocrew"), 5)
        assert lifecycle.kernel.signals == []
    finally:
        rt._descendant_scan_lock.release()
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)


@pytest.mark.asyncio
async def test_repeated_kill_retains_escapees_for_periodic_cleanup(lifecycle, monkeypatch):
    kernel = lifecycle.kernel
    # Drive the production watchdog + orphan hook only, not unrelated host
    # sweeps. The hook's dependency port uses a fixture-owned, joined executor.
    hook_owner = SimpleNamespace(
        _deps=SimpleNamespace(
            get_maintenance_executor=lambda: lifecycle.pool,
            cleanup_orphaned_mcp_servers=session_pid._cleanup_orphaned_mcp_servers,
            logger=logging.getLogger(__name__),
        )
    )
    watchdog = SessionWatchdog(
        [CleanupHook("orphan_mcp", lambda: SessionCleanup._orphan_mcp_hook(hook_owner))]
    )
    for root in (2000, 3000, 4000):
        rt, _, _ = make_runtime(lifecycle, monkeypatch, root)
        escapee, recycled, foreign, ordinary = range(root + 1, root + 5)
        for child in (escapee, recycled, foreign, ordinary):
            kernel.add(child, root, root, f"child-{child}")
        await asyncio.to_thread(session_pid._track_pid, root)
        await asyncio.to_thread(session_pid._track_session_pid, root)
        session_pid.register_protected_pid(root)
        await asyncio.wait_for(rt._snapshot_descendants(), 5)
        assert len(rt._child_pids) == 4
        # Three leave the group. One is then recycled; another moves under a
        # live foreign parent. Only the exact recorded orphan may be signalled.
        for child in (escapee, recycled, foreign):
            kernel.groups[child] = child
        kernel.tokens[recycled] = "unrelated-incarnation"
        kernel.parents[foreign] = 900
        before = len(kernel.signals)
        await asyncio.wait_for(rt.kill(expected=True), 5)
        assert rt._process is None and rt._dead and rt._child_pids == {}
        assert root not in session_pid._PROTECTED_PIDS
        assert (lifecycle.home / "kiro_session_pids.txt").read_text(encoding="utf-8") == ""
        assert child_lines(lifecycle) == {
            f"{child}:{root}:child-{child}" for child in (escapee, recycled, foreign)
        }, "teardown must retain every survivor's original recovery identity"
        assert ordinary not in kernel.tokens
        assert kernel.signals[before:] == [(root, platform_compat.SIGTERM)]

        await asyncio.wait_for(watchdog.tick(), 5)
        assert escapee not in kernel.tokens, "periodic hook must recover the recorded escapee"
        assert kernel.tokens[recycled] == "unrelated-incarnation"
        assert kernel.tokens[foreign] == f"child-{foreign}"
        assert kernel.signals[before:] == [
            (root, platform_compat.SIGTERM),
            (escapee, platform_compat.SIGKILL),
        ]
        assert child_lines(lifecycle) == set()
        # Retrying teardown and ticking an empty file cannot signal twice.
        await asyncio.wait_for(rt.kill(expected=True), 5)
        await asyncio.wait_for(watchdog.tick(), 5)
        assert len(kernel.signals) == before + 2
