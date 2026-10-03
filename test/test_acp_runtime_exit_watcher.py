"""The runtime fails fast when its root process exits with stdout still open.

A lane's ACP runtime can die (an external 429 shutdown, an operator kill) while
a descendant that inherited the runtime's stdout pipe is still alive. The reader
loop waits for EOF on that pipe, which does not come while a descendant holds the
write end, so the death is invisible to the parent for the life of the survivor
-- up to idle-session expiry (~1h). An independent watcher on the process's own
exit marks the runtime dead on the exit itself, without waiting for the stream
to reach EOF.

The watcher polls ``process.returncode`` -- set by the event loop's
``_process_exited`` the instant the root is reaped -- rather than awaiting
``process.wait()``, whose waiters are woken only once every pipe disconnects and
so parks exactly as long as the reader when a descendant holds stdout open.
"""

import asyncio
import os
import signal
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
from test_update_provider import _UNALLOCATABLE_PID

from kiro_crew.acp.runtime import AcpRuntime, AcpRuntimeDead

_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="needs POSIX fork + inherited-fd semantics to hold stdout open past the root's death",
)

# A root process that forks a child inheriting its stdout, then idles. Killing
# ONLY the root leaves the child holding the stdout write end open, so the
# reader never sees EOF -- the descendant-holds-stdout shape this watcher
# exists for. The grandchild closes its own stdin so it is not wired to anything the test must feed. It sleeps far longer
# than the test to guarantee it outlives the root for the window under test; the
# test reaps it in a finally.
_ROOT_FORKS_CHILD_HOLDING_STDOUT = (
    "import os,sys,time\n"
    "pid=os.fork()\n"
    "if pid==0:\n"
    "    # child: keep inherited stdout (fd 1) open, do not write, outlive root\n"
    "    time.sleep(60)\n"
    "    os._exit(0)\n"
    "# root: print its own pid and the child's, then idle until killed\n"
    "sys.stderr.write(str(os.getpid())+' '+str(pid)+'\\n')\n"
    "sys.stderr.flush()\n"
    "time.sleep(60)\n"
)

# A root that forks a child holding stdout open, writes a FINAL response frame
# to stdout, then exits ON ITS OWN. The child keeps the pipe open so no EOF
# arrives -- the exact shape where the watcher must drain the already-written
# response before marking the runtime dead, or the completed turn is lost.
_ROOT_WRITES_FINAL_FRAME_THEN_EXITS = (
    "import os,sys,time\n"
    "pid=os.fork()\n"
    "if pid==0:\n"
    "    # child: keep inherited stdout (fd 1) open, do not write, outlive root\n"
    "    time.sleep(60)\n"
    "    os._exit(0)\n"
    "# root: announce pids on stderr, write a final response frame on stdout,\n"
    "# flush it into the pipe, then exit while the child still holds stdout.\n"
    "sys.stderr.write(str(os.getpid())+' '+str(pid)+'\\n')\n"
    "sys.stderr.flush()\n"
    'sys.stdout.write(\'{"id": 1, "result": {"ok": true}}\\n\')\n'
    "sys.stdout.flush()\n"
    "os._exit(0)\n"
)


@_POSIX_ONLY
@pytest.mark.asyncio
async def test_exit_watcher_fires_on_real_root_death_with_descendant_holding_stdout():
    """The discriminating test: a REAL subprocess that forks a child holding
    stdout, then has only its root killed.

    Against ``await process.wait()`` this hangs -- ``wait()`` is woken only once
    every pipe disconnects, and the surviving child holds stdout, so the watcher
    never fires and the pending request never fails (the test times out). The
    returncode poll observes the root's exit the moment it is reaped, regardless
    of the still-open pipe, so the pending request fails fast.
    """
    # Own process group (start_new_session) so the finally can kill the ROOT
    # and its forked child together, even if setup raises before the asserts.
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _ROOT_FORKS_CHILD_HOLDING_STDOUT,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    # start_new_session makes the root its own group leader, so its pgid equals
    # its pid; read proc.pid directly rather than os.getpgid(proc.pid), which
    # would raise if the self-exiting root is reaped before this line runs.
    pgid = proc.pid
    reader_task = None
    watch_task = None
    try:
        # Every post-spawn step is inside this try: a timeout on the stderr
        # read, the int-unpack, or the runtime construction would otherwise
        # leak the root and its 60s-sleeping child. The finally reaps the group.
        line = await asyncio.wait_for(proc.stderr.readline(), timeout=5.0)
        root_pid, _child_pid = (int(x) for x in line.split())

        rt = AcpRuntime(work_dir="/tmp")
        rt._process = proc
        rt._pid = root_pid
        rt._initialized = True
        pending = asyncio.get_running_loop().create_future()
        rt._pending_requests[1] = pending

        reader_task = asyncio.ensure_future(rt._reader_loop())
        watch_task = asyncio.ensure_future(rt._exit_watch_loop())
        await asyncio.sleep(0)

        assert rt.is_alive()
        assert not pending.done()

        # Kill ONLY the root. The child keeps stdout open, so no EOF arrives
        # and process.wait() would stay parked -- but returncode is set as soon
        # as the root is reaped.
        os.kill(root_pid, signal.SIGKILL)

        # The watcher must observe the root's exit and fail the pending request
        # well before the surviving child exits (60s). A generous ceiling that
        # still proves fail-fast relative to the ~1h idle-expiry backstop.
        await asyncio.wait_for(watch_task, timeout=10.0)

        assert rt._dead
        assert pending.done()
        assert isinstance(pending.exception(), AcpRuntimeDead)
    finally:
        for t in (reader_task, watch_task):
            if t is not None:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        # Kill the whole group so the surviving forked child cannot idle 60s.
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except (asyncio.TimeoutError, ProcessLookupError):
            pass


@_POSIX_ONLY
@pytest.mark.asyncio
async def test_exit_watcher_drains_a_real_final_frame_before_marking_dead():
    """End to end: a real root writes a final response frame then exits with a
    descendant holding stdout, and that frame is routed, not lost.

    The root flushes a completed turn's response into the pipe and exits on its
    own; the forked child keeps stdout open so no EOF arrives. The watcher's poll
    sees the exit, but it must let the reader drain the already-written frame
    before ``_mark_dead`` fails the pending request. The request resolves with
    its RESULT rather than ``AcpRuntimeDead``.
    """
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _ROOT_WRITES_FINAL_FRAME_THEN_EXITS,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    # start_new_session makes the root its own group leader, so its pgid equals
    # its pid; read proc.pid directly rather than os.getpgid(proc.pid), which
    # would raise if the self-exiting root is reaped before this line runs.
    pgid = proc.pid
    reader_task = None
    watch_task = None
    try:
        line = await asyncio.wait_for(proc.stderr.readline(), timeout=5.0)
        root_pid, _child_pid = (int(x) for x in line.split())

        rt = AcpRuntime(work_dir="/tmp")
        rt._process = proc
        rt._pid = root_pid
        rt._initialized = True
        pending = asyncio.get_running_loop().create_future()
        rt._pending_requests[1] = pending

        reader_task = asyncio.ensure_future(rt._reader_loop())
        watch_task = asyncio.ensure_future(rt._exit_watch_loop())

        # The watcher observes the root's self-exit; the drain window lets the
        # reader pull the final frame the root flushed before dying.
        await asyncio.wait_for(watch_task, timeout=10.0)

        assert rt._dead
        assert pending.done()
        # The final response was drained and routed: result, not a dead error.
        assert pending.exception() is None
        assert pending.result() == {"ok": True}
    finally:
        for t in (reader_task, watch_task):
            if t is not None:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except (asyncio.TimeoutError, ProcessLookupError):
            pass


def _runtime_with_blocking_stdout():
    """A fast unit harness: stdout never reaches EOF (mirroring a descendant
    holding the dead root's inherited write end), and the root's exit is driven
    by setting ``process.returncode`` directly -- the SAME attribute the event
    loop's ``_process_exited`` sets and the watcher polls, so the mock exercises
    the real observation path rather than a hand-made ``wait()`` future that
    would bypass it.
    """
    rt = AcpRuntime(work_dir="/tmp")
    reader = asyncio.StreamReader()  # fed nothing, never EOF -> readuntil blocks

    proc = MagicMock()
    proc.stdout = reader
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.returncode = None
    proc.pid = _UNALLOCATABLE_PID
    # wait() must never be the thing that reports the exit here -- the watcher
    # polls returncode. A wait() that blocks forever proves the watcher does not
    # depend on it; if the watcher ever regressed to awaiting wait(), it would
    # hang and the test's wait_for would time out.
    proc.wait = AsyncMock(side_effect=lambda: asyncio.get_running_loop().create_future())
    rt._process = proc
    rt._pid = _UNALLOCATABLE_PID
    rt._initialized = True
    return rt, proc


@pytest.mark.asyncio
async def test_exit_watcher_marks_dead_when_root_exits_with_stdout_open():
    rt, proc = _runtime_with_blocking_stdout()
    pending = asyncio.get_running_loop().create_future()
    rt._pending_requests[1] = pending

    reader_task = asyncio.ensure_future(rt._reader_loop())
    watch_task = asyncio.ensure_future(rt._exit_watch_loop())
    await asyncio.sleep(0)  # let both reach their first await

    # The reader is blocked on EOF that will never arrive; nothing is dead yet.
    assert rt.is_alive()
    assert not pending.done()

    # The root process exits: _process_exited sets returncode. The stream stays
    # open (a descendant holds it), so only the returncode poll can notice.
    proc.returncode = 1

    # The watcher must observe the exit and fail the pending request promptly,
    # WITHOUT waiting for the stream to reach EOF.
    await asyncio.wait_for(watch_task, timeout=2.0)

    assert rt._dead
    assert pending.done()
    assert isinstance(pending.exception(), AcpRuntimeDead)
    # The real exit code is recorded, not the <not reaped> placeholder.
    assert "returncode=1" in (rt.death_summary() or "")

    reader_task.cancel()
    try:
        await reader_task
    except (asyncio.CancelledError, Exception):
        pass


@pytest.mark.asyncio
async def test_exit_watcher_is_a_noop_when_reader_already_marked_dead():
    """EOF-driven death stays the primary path: if the reader marks the runtime
    dead first, the watcher must not re-announce or relabel the death REASON.

    The confirmed exit still amends the retained summary with the real
    returncode -- the reap measures the status ``_mark_dead`` could not know --
    but the reason itself (``process exited (rc=0)``) is not re-announced.
    """
    rt, proc = _runtime_with_blocking_stdout()

    watch_task = asyncio.ensure_future(rt._exit_watch_loop())
    await asyncio.sleep(0)

    # The reader loop's own death path runs first.
    rt._mark_dead("process exited (rc=0)")
    assert rt._dead

    # The process then reaps: returncode is set. The watcher's poll wakes, keeps
    # the already-announced reason, and only fills in the real returncode.
    proc.returncode = 1
    await asyncio.wait_for(watch_task, timeout=2.0)

    summary = rt.death_summary() or ""
    assert summary.startswith("process exited (rc=0)")  # reason not relabelled
    assert "returncode=1" in summary  # reap amended the retained code
    assert "<not reaped>" not in summary


@pytest.mark.asyncio
async def test_exit_watcher_retires_tracking_even_when_already_marked_dead():
    """A confirmed exit always owes the registry retirement.

    A broken-pipe write, a reader crash or a write stall marks the runtime dead
    WITHOUT retiring its PID-ledger entries. When the root then exits for real,
    the watcher must still run the retirement -- the ``_dead`` flag gates only
    the re-announcement, never the cleanup -- or a stale ``kiro_session_pids.txt``
    / ``kiro_pids.txt`` line naming the dead root is left standing for the sweep.
    """
    rt, proc = _runtime_with_blocking_stdout()
    rt._retire_tracking_after_exit = AsyncMock()

    watch_task = asyncio.ensure_future(rt._exit_watch_loop())
    await asyncio.sleep(0)

    # A non-retiring death path (broken pipe / reader crash / write stall) set
    # the flag before the exit, so the re-announce guard is already satisfied.
    rt._mark_dead("broken pipe on write")
    assert rt._dead

    # The root exits for real (returncode set). The retirement is still owed.
    proc.returncode = 0
    await asyncio.wait_for(watch_task, timeout=2.0)

    rt._retire_tracking_after_exit.assert_awaited_once()


@pytest.mark.asyncio
async def test_exit_watcher_drains_a_final_readable_frame_before_marking_dead():
    """A final response readable on stdout at exit is routed, not dropped.

    The backend writes a completed turn's response and then exits. ``_mark_dead``
    fails every pending request and poisons the session queues, so marking before
    that frame is routed turns a completed turn into an ``AcpRuntimeDead`` and
    drops the response. The reader is parked on ``readuntil`` when the frame
    becomes readable; a single pre-mark yield resumes the watcher before the
    reader's data callback runs, so only a drain window that keeps yielding while
    the reader makes progress routes the frame first. The pending request then
    resolves with its RESULT, not ``AcpRuntimeDead``.
    """
    rt, proc = _runtime_with_blocking_stdout()
    pending = asyncio.get_running_loop().create_future()
    rt._pending_requests[1] = pending

    reader_task = asyncio.ensure_future(rt._reader_loop())
    watch_task = asyncio.ensure_future(rt._exit_watch_loop())
    await asyncio.sleep(0)  # let both reach their first await (reader parked on readuntil)

    assert rt.is_alive()
    assert not pending.done()

    # The backend's final response becomes readable AFTER the reader has parked,
    # then the root exits (returncode set). The stream never reaches EOF (no
    # feed_eof) -- a descendant holds it open. The reader's data callback is
    # scheduled behind the watcher's resumption, so a single pre-mark yield would
    # mark dead first and lose this frame; the drain window must let the reader
    # route it before the mark.
    rt._process.stdout.feed_data(b'{"id": 1, "result": {"ok": true}}\n')
    proc.returncode = 0

    await asyncio.wait_for(watch_task, timeout=2.0)

    # The final frame was routed before the mark: the request carries its result,
    # not the dead-runtime error.
    assert pending.done()
    assert pending.exception() is None
    assert pending.result() == {"ok": True}
    assert rt._dead  # still marked dead after draining

    reader_task.cancel()
    try:
        await reader_task
    except (asyncio.CancelledError, Exception):
        pass
