"""Windows tree-drain contracts with a closed kernel boundary, on every host."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kiro_crew import platform_compat as pc


@pytest.fixture(autouse=True)
def cleanup_admissions(monkeypatch):
    monkeypatch.setattr(pc, "_WINDOWS_TREE_ADMISSIONS", set())


@pytest.fixture
def kernel(monkeypatch):
    identities = {1000: (100, 10, None), 2000: (200, 20, None)}
    children = {100: {200: 2000}}
    scanned = []
    killed = []
    closed = []
    clock = [0.0]

    def snapshot(pid, retained, handle):
        assert identities[handle][0] == pid
        scanned.append((pid, identities[handle][2]))
        return {p: h for p, h in children.get(pid, {}).items() if p not in retained}

    def terminate(handle):
        killed.append(handle)
        pid, created, _ = identities[handle]
        identities[handle] = (pid, created, created + 100)
        return True

    monkeypatch.setattr(pc, "IS_WINDOWS", True)
    monkeypatch.setattr(pc, "_PENDING_WINDOWS_TREE_CLEANUPS", {})
    monkeypatch.setattr(pc, "_windows_process_handle_identity", identities.get)
    monkeypatch.setattr(pc, "descendant_termination_handles", snapshot)
    monkeypatch.setattr(pc, "terminate_process_handle", terminate)
    monkeypatch.setattr(pc, "close_process_handle", closed.append)
    monkeypatch.setattr(
        pc,
        "time",
        SimpleNamespace(
            monotonic=lambda: clock[0], sleep=lambda _: clock.__setitem__(0, clock[0] + 1)
        ),
    )
    return SimpleNamespace(
        identities=identities,
        children=children,
        scanned=scanned,
        killed=killed,
        closed=closed,
        clock=clock,
        terminate=terminate,
    )


def test_an_exited_root_still_anchors_live_descendants(kernel):
    kernel.identities[1000] = (100, 10, 40)
    assert pc.terminate_windows_process_tree_owned(1000) is True
    assert kernel.killed == [2000]
    assert sorted(kernel.closed) == [1000, 2000]
    assert (100, 40) in kernel.scanned
    assert (200, 120) in kernel.scanned


def test_every_parent_is_rescanned_after_exit_for_late_children(kernel, monkeypatch):
    def terminate(handle):
        if handle == 2000:
            # Born before the parent's published exit, but after its first scan.
            kernel.identities[3000] = (300, 30, None)
            kernel.children[200] = {300: 3000}
        return kernel.terminate(handle)

    monkeypatch.setattr(pc, "terminate_process_handle", terminate)
    assert pc.terminate_windows_process_tree_owned(1000) is True
    assert kernel.killed == [1000, 2000, 3000]
    assert (200, 120) in kernel.scanned
    assert (300, 130) in kernel.scanned
    assert sorted(kernel.closed) == [1000, 2000, 3000]


def test_unknown_root_is_not_success_or_permission_to_signal(kernel):
    kernel.identities.pop(1000)
    with pytest.raises(OSError, match="root identity unavailable"):
        pc.terminate_windows_process_tree_owned(1000)
    assert kernel.killed == []
    assert kernel.closed == [1000]
    assert pc._PENDING_WINDOWS_TREE_CLEANUPS == {}


def test_descendant_identity_failure_is_not_an_empty_tree(kernel, monkeypatch):
    read = pc._windows_process_handle_identity

    def identity(handle):
        return None if handle == 2000 else read(handle)

    monkeypatch.setattr(pc, "_windows_process_handle_identity", identity)
    with pytest.raises(OSError, match="identity unreadable"):
        pc.terminate_windows_process_tree_owned(1000)
    assert kernel.killed == [1000]
    assert kernel.closed == []
    assert pc._PENDING_WINDOWS_TREE_CLEANUPS[(100, 10)].handles == {100: 1000, 200: 2000}


def test_a_successful_terminate_return_is_not_an_exit_proof(kernel, monkeypatch):
    monkeypatch.setattr(
        pc, "terminate_process_handle", lambda handle: kernel.killed.append(handle) or True
    )
    with pytest.raises(OSError, match="did not drain"):
        pc.terminate_windows_process_tree_owned(1000)
    assert kernel.killed == [1000, 2000]
    assert kernel.closed == []
    assert pc._PENDING_WINDOWS_TREE_CLEANUPS[(100, 10)].handles == {100: 1000, 200: 2000}


def test_scan_error_preserves_failure_and_retains_owned_descendant_handles(kernel, monkeypatch):
    scan = pc.descendant_termination_handles

    def snapshot(pid, retained, handle):
        if pid == 200:
            raise OSError("unreadable child ancestry")
        return scan(pid, retained, handle)

    monkeypatch.setattr(pc, "descendant_termination_handles", snapshot)
    with pytest.raises(OSError, match="unreadable child ancestry"):
        pc.terminate_windows_process_tree_owned(1000)
    assert kernel.closed == []
    assert 2000 not in kernel.killed
    assert pc._PENDING_WINDOWS_TREE_CLEANUPS[(100, 10)].handles == {100: 1000, 200: 2000}


@pytest.mark.asyncio
async def test_failed_owner_cleanup_survives_gc_and_retries_from_maintenance(
    kernel, monkeypatch, tmp_path
):
    """Failed exact handles outlive owners and are retried without PID authority."""
    from kiro_crew import session_pid

    process_type = type(
        "OwnedProcess",
        (),
        {"wait": lambda self: asyncio.sleep(0, result=0)},
    )
    process = process_type()
    owner = SimpleNamespace(process=process)
    owner_ref = __import__("weakref").ref(process)
    monkeypatch.setattr(pc, "duplicate_asyncio_process_handle", lambda _: 1000)
    monkeypatch.setattr(session_pid, "config_dir", lambda: tmp_path)

    scan = pc.descendant_termination_handles
    refused = [True]
    retained_seen = []

    def temporarily_refused(pid, retained, handle):
        retained_seen.append(set(retained))
        if pid == 200 and refused[0]:
            raise OSError("temporary descendant discovery refusal")
        return scan(pid, retained, handle)

    monkeypatch.setattr(pc, "descendant_termination_handles", temporarily_refused)

    try:
        await pc.terminate_windows_asyncio_tree(owner.process)
    except OSError as exc:
        assert "temporary descendant discovery refusal" in str(exc)
    else:
        pytest.fail("temporary descendant discovery refusal was treated as success")
    assert session_pid.cleanup_orphaned_session_roots() == 0

    owner.process = None
    del process
    await asyncio.sleep(0)
    __import__("gc").collect()
    assert owner_ref() is None, "pending cleanup retained the process owner graph"
    assert retained_seen[1] == {100, 200}, "retry lost the pinned intermediary"
    assert kernel.closed == [], "failed retries released exact cleanup authority"

    kernel.identities[9000] = (900, 90, None)
    refused[0] = False
    assert session_pid.cleanup_orphaned_session_roots() == 1

    assert kernel.identities[1000][2] is not None
    assert kernel.identities[2000][2] is not None
    assert kernel.identities[9000][2] is None, "maintenance touched an unrelated process"
    assert sorted(kernel.closed) == [1000, 2000]
    assert session_pid.cleanup_orphaned_session_roots() == 0
    assert sorted(kernel.closed) == [1000, 2000], "retired handles closed more than once"


def test_owned_cleanup_deduplicates_and_denied_tree_does_not_starve_peer(monkeypatch):
    roots = {10: 100, 11: 100, 20: 200}
    active = {100: True, 200: True}
    denied = {100, 200}
    closed = []
    clock = [0.0]

    def identity(handle):
        pid = roots[handle]
        return pid, pid * 10, None if active[pid] else pid * 10 + 5

    def descendants(pid, retained, handle):
        assert retained[pid] in roots
        if pid in denied:
            raise OSError(f"denied {pid}")
        return {}

    def terminate(handle):
        active[roots[handle]] = False
        return True

    monkeypatch.setattr(pc, "IS_WINDOWS", True)
    monkeypatch.setattr(pc, "_PENDING_WINDOWS_TREE_CLEANUPS", {})
    monkeypatch.setattr(pc, "_windows_process_handle_identity", identity)
    monkeypatch.setattr(pc, "descendant_termination_handles", descendants)
    monkeypatch.setattr(pc, "terminate_process_handle", terminate)
    monkeypatch.setattr(pc, "close_process_handle", closed.append)
    monkeypatch.setattr(
        pc,
        "time",
        SimpleNamespace(
            monotonic=lambda: clock[0], sleep=lambda _: clock.__setitem__(0, clock[0] + 1)
        ),
    )

    with pytest.raises(OSError, match="denied 100"):
        pc.terminate_windows_process_tree_owned(10)
    with pytest.raises(OSError, match="denied 100"):
        pc.terminate_windows_process_tree_owned(11)
    with pytest.raises(OSError, match="denied 200"):
        pc.terminate_windows_process_tree_owned(20)
    assert len(pc._PENDING_WINDOWS_TREE_CLEANUPS) == 2
    assert closed == [11], "only the duplicate root handle should be released"

    denied.remove(200)
    assert pc.retry_pending_windows_process_trees(limit=2) == (200,)
    assert active == {100: True, 200: False}
    assert closed == [11, 20], "the denied first tree starved its healthy peer"

    denied.clear()
    assert pc.retry_pending_windows_process_trees(limit=2) == (100,)
    assert active == {100: False, 200: False}
    assert closed == [11, 20, 10]
    assert pc.retry_pending_windows_process_trees(limit=2) == ()
    assert closed == [11, 20, 10], "retired handles closed more than once"


def test_metadata_write_refusal_keeps_the_pin_until_a_successful_retry(
    kernel, monkeypatch, tmp_path
):
    from kiro_crew import session_pid

    monkeypatch.setattr(session_pid, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(session_pid, "_PROTECTED_PIDS", {100})
    (tmp_path / "kiro_pids.txt").write_text("100\n", encoding="utf-8")
    original_rewrite = session_pid._rewrite_pid_file
    monkeypatch.setattr(session_pid, "_rewrite_pid_file", lambda *args: False)
    state = pc._PendingWindowsTreeCleanup(1000, kernel.identities[1000])
    pc._PENDING_WINDOWS_TREE_CLEANUPS[state.key] = state

    assert session_pid.cleanup_orphaned_session_roots() == 0
    assert kernel.closed == []
    assert session_pid._PROTECTED_PIDS == {100}
    assert pc._PENDING_WINDOWS_TREE_CLEANUPS[state.key] is state

    monkeypatch.setattr(session_pid, "_rewrite_pid_file", original_rewrite)
    assert session_pid.cleanup_orphaned_session_roots() == 1
    assert (tmp_path / "kiro_pids.txt").read_text(encoding="utf-8") == ""
    assert sorted(kernel.closed) == [1000, 2000]
    assert session_pid._PROTECTED_PIDS == set()
    assert pc._PENDING_WINDOWS_TREE_CLEANUPS == {}


def test_maintenance_busy_first_entry_does_not_starve_a_healthy_later_one(kernel, monkeypatch):
    kernel.identities[3000] = (300, 30, None)
    kernel.children[300] = {}

    scan = pc.descendant_termination_handles
    refuse_scan = [True]

    def gated_scan(pid, retained, handle):
        if refuse_scan[0]:
            raise OSError("temporary discovery refusal")
        return scan(pid, retained, handle)

    monkeypatch.setattr(pc, "descendant_termination_handles", gated_scan)

    # Both trees fail their first drain and stay pending, first inserted first.
    with pytest.raises(OSError, match="temporary discovery refusal"):
        pc.terminate_windows_process_tree_owned(1000)
    with pytest.raises(OSError, match="temporary discovery refusal"):
        pc.terminate_windows_process_tree_owned(3000)
    refuse_scan[0] = False
    assert list(pc._PENDING_WINDOWS_TREE_CLEANUPS) == [(100, 10), (300, 30)]

    first = pc._PENDING_WINDOWS_TREE_CLEANUPS[(100, 10)]
    holder_has_lock = threading.Event()
    holder_may_release = threading.Event()

    # Hold the FIRST entry's lock from another thread — model a caller-initiated
    # drain still in flight — so maintenance must non-blockingly skip it.
    def hold_busy():
        with first.lock:
            holder_has_lock.set()
            holder_may_release.wait(10)

    holder = threading.Thread(target=hold_busy)
    holder.start()
    try:
        # The holder signals once it actually owns the lock — a real handshake,
        # not a probe spin. The bounded wait is a hang guard; nothing asserts on
        # elapsed time.
        assert holder_has_lock.wait(10), "the holder never took the busy lock"

        # Maintenance must complete the HEALTHY later entry without waiting on the
        # busy first one — proven by the completed set, not by elapsed time.
        completed = pc.retry_pending_windows_process_trees()
    finally:
        holder_may_release.set()
        holder.join(10)

    assert not holder.is_alive()
    assert completed == (300,), "a busy first entry starved the healthy later one"
    # The busy entry was rotated behind its peer and remains pending for retry.
    assert list(pc._PENDING_WINDOWS_TREE_CLEANUPS) == [(100, 10)]

    # Once the holder is gone the next tick finishes the rotated entry too.
    assert pc.retry_pending_windows_process_trees() == (100,)
    assert pc._PENDING_WINDOWS_TREE_CLEANUPS == {}


@pytest.mark.asyncio
async def test_async_cleanup_keeps_the_pin_until_repeated_cancellation_settles(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    closed = []
    process = SimpleNamespace(wait=AsyncMock(return_value=0))
    monkeypatch.setattr(
        pc, "duplicate_asyncio_process_handle", lambda p: 1234 if p is process else None
    )
    monkeypatch.setattr(pc, "close_process_handle", closed.append)

    def drain(handle, *, reservation):
        assert handle == 1234
        entered.set()
        assert release.wait(10), "test did not release the drain worker"
        assert closed == []
        pc.close_process_handle(handle)
        return True

    monkeypatch.setattr(pc, "terminate_windows_process_tree_owned", drain)
    task = asyncio.create_task(pc.terminate_windows_asyncio_tree(process))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert closed == []
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert closed == [1234]
        process.wait.assert_awaited_once()
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)


@pytest.mark.asyncio
async def test_async_cleanup_refuses_a_missing_original_handle(monkeypatch):
    process = SimpleNamespace(wait=AsyncMock())
    close = []
    monkeypatch.setattr(pc, "duplicate_asyncio_process_handle", lambda p: None)
    monkeypatch.setattr(pc, "close_process_handle", close.append)
    with pytest.raises(OSError, match="original Windows runtime process handle"):
        await pc.terminate_windows_asyncio_tree(process)
    assert close == []
    process.wait.assert_not_awaited()


@pytest.mark.asyncio
async def test_client_retry_cannot_discard_an_incompletely_drained_windows_tree(
    monkeypatch, tmp_path
):
    from kiro_crew.acp.client import AcpClient

    client = AcpClient(work_dir=tmp_path)
    original = SimpleNamespace(returncode=0)
    client._process = original
    client._pid = 123
    client._windows_tree_cleanup_failed = True
    monkeypatch.setattr(pc, "IS_WINDOWS", True)
    monkeypatch.setattr(client, "_kill_process", AsyncMock(side_effect=OSError("tree unresolved")))
    spawn = AsyncMock()
    monkeypatch.setattr(client, "_spawn", spawn)

    client._reset_state()
    assert client._process is original
    assert client._pid == 123
    with pytest.raises(OSError, match="tree unresolved"):
        await client.ensure_ready()
    assert client._process is original
    spawn.assert_not_awaited()
