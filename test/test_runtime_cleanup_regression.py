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
from kiro_crew.acp import client, runtime, session_mcp
from kiro_crew.acp.types import (
    METHOD_SESSION_LOAD,
    METHOD_SESSION_NEW,
    METHOD_SESSION_TERMINATE,
    METHOD_SET_MODE,
    JsonRpcMessage,
)
from kiro_crew.security import paths as security_paths
from kiro_crew.session_cleanup import SessionCleanup
from kiro_crew.testing.wait import await_until
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
    # load_session resolves its MCP array from the agent spec, which the create
    # path never reads when it is handed an explicit array. That read is not the
    # subject here and it is expensive in two ways the per-test home cannot
    # hide: it self-heals a missing managed spec by rebuilding every managed
    # agent config, and its project-checkout scan runs the work dir through
    # the sensitive-path fence, whose symlink resolution spawns the
    # process-wide resolver child pool on first use. On a loaded Windows shard
    # that cold spawn alone outlasts the 5 s lost-run bound around the resume,
    # so the [load] cases went red while the [new] cases never could. No
    # kiro-cli reads the spec here, so the resume path is answered "no spec"
    # before either cost is reached, the same shape the create path takes.
    monkeypatch.setattr(session_mcp, "_agent_spec_for", lambda *_, **__: None)

    def _no_path_resolution(expanded, *_args, **_kwargs):
        raise AssertionError(
            "this lifecycle must not reach the sensitive-path resolver pool "
            f"(a cold spawn costs the test its lost-run bound): {expanded!r}"
        )

    # The closed interface extends to the resolver children: a path that still
    # reaches the pool fails by name on every host, instead of only on the shard
    # slow enough to expose it.
    monkeypatch.setattr(security_paths, "_run_resolution_bounded", _no_path_resolution)
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
    sibling = await await_until(asyncio.ensure_future(rt.create_session(mcp_servers=[])))
    await await_until(asyncio.ensure_future(rt.wait_tree_recorded()))
    sibling_record = dict(rt._child_pids)
    assert child_lines(lifecycle) == {"1001:1000:child-1001"}

    handle = await await_until(asyncio.ensure_future(open_session(rt, resume)))
    await await_until(asyncio.ensure_future(rt.wait_tree_recorded()))

    assert resident[handle.session_id] == 1002
    assert rt._child_pids == {**sibling_record, 1002: ("child-1002", b"node")}
    assert child_lines(lifecycle) == {"1001:1000:child-1001", "1002:1000:child-1002"}
    assert rt._session_queues[handle.session_id] is handle._queue
    assert rt._session_queues[sibling.session_id] is sibling._queue
    assert lifecycle.kernel.signals == []


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True], ids=["new", "load"])
async def test_a_session_start_does_not_wait_for_its_descendant_pass(
    lifecycle, monkeypatch, resume
):
    """The session is handed back while its tree pass is still queued; the pass then lands."""
    rt, resident, _ = make_runtime(lifecycle, monkeypatch)
    # Hold the real scan lock, so the session-start pass cannot run.
    await await_until(asyncio.ensure_future(rt._descendant_scan_lock.acquire()))
    try:
        handle = await await_until(asyncio.ensure_future(open_session(rt, resume)))
        assert resident[handle.session_id] == 1001
        assert rt._child_pids == {} and child_lines(lifecycle) == set()
    finally:
        rt._descendant_scan_lock.release()
    await await_until(asyncio.ensure_future(rt.wait_tree_recorded()))
    assert rt._child_pids == {1001: ("child-1001", b"node")}
    assert child_lines(lifecycle) == {"1001:1000:child-1001"}
    assert lifecycle.kernel.signals == []


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True], ids=["new", "load"])
async def test_a_kill_waits_for_the_session_start_pass_before_it_signals(
    lifecycle, monkeypatch, resume
):
    """A child that left the group is only reachable through the record; the kill waits for it."""
    rt, _, _ = make_runtime(lifecycle, monkeypatch)
    kernel = lifecycle.kernel
    await await_until(asyncio.ensure_future(rt._descendant_scan_lock.acquire()))
    released = False
    try:
        await await_until(asyncio.ensure_future(open_session(rt, resume)))
        kernel.groups[1001] = 1001  # the session's child leads a group of its own
        waiting = asyncio.Event()
        wait_tree_recorded = rt.wait_tree_recorded

        async def observed_wait(**kwargs):
            waiting.set()
            return await wait_tree_recorded(**kwargs)

        monkeypatch.setattr(rt, "wait_tree_recorded", observed_wait)
        rt._KILL_TREE_WAIT_SECS = None  # unbounded: the wait itself is the subject
        kill = asyncio.ensure_future(rt.kill(expected=True))
        await await_until(waiting)
        assert kernel.signals == [], "the root was signalled before its tree was recorded"
        rt._descendant_scan_lock.release()
        released = True
        await await_until(kill)
    finally:
        if not released:
            rt._descendant_scan_lock.release()
    assert kernel.signals == [(1000, platform_compat.SIGTERM)]
    assert 1001 in kernel.tokens
    assert child_lines(lifecycle) == {
        "1001:1000:child-1001"
    }, "the survivor must stay recorded for the orphan sweep"


@pytest.mark.asyncio
async def test_a_turn_end_record_on_a_shared_runtime_does_not_wait_for_the_walk(
    lifecycle, monkeypatch
):
    """The turn ends at once; the walk it started still lands, and a refresh waits for it."""
    from kiro_crew import process_identity
    from kiro_crew.acp.session_provider import AcpSessionProvider

    rt, _, _ = make_runtime(lifecycle, monkeypatch)
    lifecycle.kernel.add(1001, 1000, 1000, "child-1001")  # a turn forked it
    provider = AcpSessionProvider.__new__(AcpSessionProvider)
    provider._runtime = rt
    # The group read is not this test's subject, and it would read the host.
    monkeypatch.setattr(process_identity, "_retain_verified_group", lambda source: None)
    await await_until(asyncio.ensure_future(rt._descendant_scan_lock.acquire()))
    try:
        await await_until(
            asyncio.ensure_future(process_identity.record_live_children_after_turn(provider))
        )
        assert child_lines(lifecycle) == set(), "the walk ran in front of the turn's end"
    finally:
        rt._descendant_scan_lock.release()
    await await_until(asyncio.ensure_future(rt.wait_tree_recorded()))
    assert child_lines(lifecycle) == {"1001:1000:child-1001"}
    assert lifecycle.kernel.signals == []


@pytest.mark.asyncio
async def test_a_kill_cancels_the_rescan_a_pending_spawn_pass_schedules(lifecycle, monkeypatch):
    """A spawn pass that ends empty under the kill leaves no rescan running past it."""
    rt, _, _ = make_runtime(lifecycle, monkeypatch)
    rt._DESCENDANT_RESCAN_DELAY = 3600.0  # only ever cancelled, never waited out
    await await_until(asyncio.ensure_future(rt._descendant_scan_lock.acquire()))
    released = False
    try:
        rt._record_tree_in_background(retry_when_empty=True)
        waiting = asyncio.Event()
        wait_tree_recorded = rt.wait_tree_recorded

        async def observed_wait(**kwargs):
            waiting.set()
            return await wait_tree_recorded(**kwargs)

        monkeypatch.setattr(rt, "wait_tree_recorded", observed_wait)
        rt._KILL_TREE_WAIT_SECS = None  # unbounded: the wait itself is the subject
        kill = asyncio.ensure_future(rt.kill(expected=True))
        await await_until(waiting)
        rt._descendant_scan_lock.release()
        released = True
        await await_until(kill)
    finally:
        if not released:
            rt._descendant_scan_lock.release()
    retry = rt._descendant_rescan_task
    assert retry is not None, "the empty spawn pass did not schedule its rescan"
    assert retry.cancelled(), "a rescan scheduled under the kill outlived it"


@pytest.mark.asyncio
async def test_a_hung_start_pass_cannot_hold_the_kill_past_its_bound(lifecycle, monkeypatch):
    """A walk that never returns is cancelled at the bound; the kill goes on, on the earlier record."""
    rt, _, _ = make_runtime(lifecycle, monkeypatch)
    kernel = lifecycle.kernel
    kernel.add(1001, 1000, 1000, "child-1001")
    hung = asyncio.Event()

    async def hung_pass(**_kwargs):
        hung.set()
        await asyncio.Event().wait()  # a walk that never returns

    monkeypatch.setattr(rt, "_snapshot_descendants", hung_pass)
    rt._KILL_TREE_WAIT_SECS = 0.0  # the bound itself, not its length, is the subject
    rt._record_tree_in_background()
    await await_until(hung)
    (pending,) = rt._start_tree_scans

    await await_until(asyncio.ensure_future(rt.kill(expected=True)))

    assert pending.cancelled(), "the hung pass outlived the kill"
    assert kernel.signals == [(1000, platform_compat.SIGTERM)]
    assert 1000 not in kernel.tokens and 1001 not in kernel.tokens
    assert rt._child_pids == {} and child_lines(lifecycle) == set()


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
        await await_until(asyncio.ensure_future(rt._snapshot_descendants()))
        assert len(rt._child_pids) == 4
        # Three leave the group. One is then recycled; another moves under a
        # live foreign parent. Only the exact recorded orphan may be signalled.
        for child in (escapee, recycled, foreign):
            kernel.groups[child] = child
        kernel.tokens[recycled] = "unrelated-incarnation"
        kernel.parents[foreign] = 900
        before = len(kernel.signals)
        await await_until(asyncio.ensure_future(rt.kill(expected=True)))
        assert rt._process is None and rt._dead and rt._child_pids == {}
        assert root not in session_pid._PROTECTED_PIDS
        assert (lifecycle.home / "kiro_session_pids.txt").read_text(encoding="utf-8") == ""
        assert child_lines(lifecycle) == {
            f"{child}:{root}:child-{child}" for child in (escapee, recycled, foreign)
        }, "teardown must retain every survivor's original recovery identity"
        assert ordinary not in kernel.tokens
        assert kernel.signals[before:] == [(root, platform_compat.SIGTERM)]

        await await_until(asyncio.ensure_future(watchdog.tick()))
        assert escapee not in kernel.tokens, "periodic hook must recover the recorded escapee"
        assert kernel.tokens[recycled] == "unrelated-incarnation"
        assert kernel.tokens[foreign] == f"child-{foreign}"
        assert kernel.signals[before:] == [
            (root, platform_compat.SIGTERM),
            (escapee, platform_compat.SIGKILL),
        ]
        assert child_lines(lifecycle) == set()
        # Retrying teardown and ticking an empty file cannot signal twice.
        await await_until(asyncio.ensure_future(rt.kill(expected=True)))
        await await_until(asyncio.ensure_future(watchdog.tick()))
        assert len(kernel.signals) == before + 2


@pytest.mark.asyncio
async def test_kill_cancels_the_pending_spawn_rescan_and_still_ends_the_tree(
    lifecycle, monkeypatch
):
    """A spawn scan that saw no descendant leaves one retry waiting; the kill ends it.

    The retry must not record a tree the kill is ending, and the kill itself is
    the same one: the root's group is signalled once and every member is gone.
    """
    rt, _, _ = make_runtime(lifecycle, monkeypatch)
    kernel = lifecycle.kernel
    # Far beyond the test: the retry is only ever cancelled, never waited out.
    rt._DESCENDANT_RESCAN_DELAY = 3600.0
    await await_until(asyncio.ensure_future(rt._snapshot_descendants(retry_when_empty=True)))
    retry = rt._descendant_rescan_task
    assert retry is not None and not retry.done()
    assert rt._child_pids == {} and child_lines(lifecycle) == set()
    # The root forks after the scan; the group kill reaches the child unrecorded.
    kernel.add(1001, 1000, 1000, "child-1001")

    await await_until(asyncio.ensure_future(rt.kill(expected=True)))

    assert retry.cancelled()
    assert kernel.signals == [(1000, platform_compat.SIGTERM)]
    assert 1000 not in kernel.tokens and 1001 not in kernel.tokens
    assert rt._child_pids == {} and child_lines(lifecycle) == set()
