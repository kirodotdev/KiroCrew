"""The runtime's process tree and its teardown.

Proving the root process is still the one this runtime spawned, recording its
descendants, signalling the whole tree, and ``kill`` with its reap and the release of
the resources the process held.

Each function here whose first parameter is ``self`` is an ``AcpRuntime`` method: the class
binds it by name, so it reads and writes the runtime's own state on the instance, and a patch
of the method on the class or on an instance reaches every caller.

Instance fields written: ``_bound_workspace_fd``, ``_child_pids``, ``_process``,
``_process_instance``, ``_process_tree_confirmed_dead``, ``_scope_unit``,
``_spawn_work_dir``.

Instance fields read: ``_close_remote_workspace``, ``_descendant_scan_lock``,
``_exit_watch_task``, ``_pid``, ``_reader_task``, ``_spawn_start_token``, ``_start_time``, ``_stderr_task``, ``_work_dir``.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import TYPE_CHECKING

from kiro_crew import platform_compat
from kiro_crew.acp import runtime_process_tree
from kiro_crew.acp.runtime_process_tree import ChildRecord, _capture_child_records, _get_child_pids
from kiro_crew.executors import subprocess_executor
from kiro_crew.runtime_ownership import authorize_runtime_kill
from kiro_crew.sandbox import release_bound_agent_workspace
from kiro_crew.session_pid import (
    _pgroup_has_member_besides,
    _pid_gone_or_unmanaged,
    _replace_child_pids,
    _signal_orphaned_runtime_group,
    _untrack_child_pids,
    _untrack_pid,
    _untrack_pid_if_dead,
    _untrack_root_by_identity,
    _untrack_session_pid,
    group_vouching_available,
    unregister_protected_pid,
)

if TYPE_CHECKING:
    from kiro_crew.acp.runtime import AcpRuntime

#: The runtime's logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.acp.runtime")


def _escapee_is_still_ours(pid: int, record: ChildRecord) -> bool:
    """Whether a descendant outside the walk is still the process we recorded.

    Liveness alone cannot answer this, and answering it with liveness is how a
    stranger gets killed: a descendant that exited and had its number taken
    reads as alive, and carrying its record forward would publish the number as
    ours. The sweep then finds live identity and recorded identity in agreement
    -- both the stranger's -- and signals it.

    So compare the identity instead. A pid that is gone reads ``None`` and fails
    the same comparison, which is the answer we want for it too. An unreadable
    identity (a live process we may not introspect) also fails: we cannot vouch
    for it, and a record we cannot vouch for must not be written.
    """
    recorded = record[0] if isinstance(record, tuple) and record else None
    if recorded is None:
        return False
    return platform_compat.get_process_start_id(pid) == recorded


def _prune_dead_descendants(saved: dict[int, ChildRecord]) -> list[int]:
    """Untrack the descendants that are gone; return the ones still alive.

    A descendant's entry is pruned by ITS OWN liveness, never by the root's
    fate. One that escaped the group kill (it called setsid, so killpg never
    reached it) must keep its entry: that entry is the only handle the periodic
    sweep and the next startup cleanup have on it, and dropping it is precisely
    the leak this tracking exists to close.

    A synchronous unit so the caller can hand the whole thing to a worker
    thread: the liveness probes read ``/proc`` and the untrack takes the PID
    file's exclusive lock, neither of which may run on the event loop.
    """
    dead = {pid: rec for pid, rec in saved.items() if _pid_gone_or_unmanaged(pid)}
    if dead:
        try:
            _untrack_child_pids(dead)
        except Exception:
            logger.debug(
                "AcpRuntime: untracking descendant PIDs %s failed", list(dead), exc_info=True
            )
    return [pid for pid in saved if pid not in dead]


async def _discard_bound_workspace(self: AcpRuntime) -> None:
    """Close the parent copy of a macOS workspace identity off-loop."""
    descriptor = getattr(self, "_bound_workspace_fd", None)
    self._bound_workspace_fd = None
    work_dir = getattr(self, "_work_dir", None)
    if work_dir is not None:
        self._spawn_work_dir = str(work_dir)
    if descriptor is not None:
        await release_bound_agent_workspace(descriptor)


def _root_identity(self: AcpRuntime) -> str:
    """``"holds"``, ``"mismatch"`` or ``"unknown"`` for this runtime's root.

    ``_start_time`` is read once, at spawn (``get_process_start_id``), and a
    pid plus a start instant name one process for good: two processes on the
    same number at different times cannot share it.

    The three answers are not two. ``"mismatch"`` is a MEASUREMENT: both
    identities were read and they differ, so the number is provably someone
    else's. ``"unknown"`` is the absence of one -- nothing was recorded at
    spawn, or the live read failed, which
    :func:`platform_compat.get_process_start_id` also returns for a pid that
    is simply gone. Both refuse authorization, and callers must treat them
    alike when deciding, but only the first may DESCRIBE the root: saying "no
    longer the process spawned" about a read that never happened sends a
    diagnostic after the wrong cause.
    """
    pid = self._pid
    recorded = self._start_time
    if pid is None or recorded is None:
        return "unknown"
    live = platform_compat.get_process_start_id(pid)
    if live is None:
        return "unknown"
    return "holds" if live == recorded else "mismatch"


def _root_identity_holds(self: AcpRuntime) -> bool:
    """Whether the root's identity is PROVEN to still be ours.

    Refuses on ``"unknown"`` as firmly as on ``"mismatch"`` -- recording or
    signalling a tree we cannot prove is ours is how a stranger gets
    signalled -- and logs only what it measured.
    """
    verdict = self._root_identity()
    if verdict == "mismatch":
        logger.warning(
            "AcpRuntime: root PID %d is no longer the process spawned for this "
            "runtime -- recording nothing",
            self._pid,
        )
    elif verdict == "unknown":
        logger.debug(
            "AcpRuntime: root PID %s identity is unreadable -- treated as not "
            "ours, so nothing is recorded or resolved from its number",
            self._pid,
        )
    return verdict == "holds"


async def refresh_process_tree(self: AcpRuntime) -> None:
    """Re-walk this runtime's tree before a teardown takes its kill handle.

    The snapshot otherwise runs only at spawn, create and resume, so a child
    a turn started is missing from the handle, and only a recorded member may
    vouch for a leaderless group (``process_identity._group_member_vouches``).
    Same guarded pass as every other snapshot (:meth:`_snapshot_descendants`).
    """
    await self._snapshot_descendants()


async def _snapshot_descendants(self: AcpRuntime, *, retry_when_empty: bool = False) -> None:
    """Record this runtime's descendant PIDs in the tracking file.

    The PID this runtime registers is the sandbox launcher, not the agent:
    the tree is ``launcher -> agent -> agent chat process -> MCP servers``,
    and every one of those below the root held no entry in either PID file.
    A root that died before its subtree -- a teardown race, a crash mid-init
    -- therefore left a multi-hundred-MB subtree reparented to init that no
    reaper could act on, because every sweep keys off those files.

    Every pass re-enumerates the tree and writes the whole answer, because
    nothing tells this process when a descendant exits: they are its
    grandchildren, so there is no ``SIGCHLD`` and no wait to reap. Looking is
    the only way to learn, and the record has to be corrected in both
    directions -- a pid that left keeps or loses its line by its own
    liveness, and a pid still there gets the identity read on THIS pass, not
    the one recorded earlier. A stale identity is what would make the
    teardown sweep read a live descendant as recycled and skip it.

    A pid the walk cannot reach but which is still alive keeps its record:
    that is the child that left the process group, and its record is the only
    handle the teardown and the sweeps have on it. One confirmed gone is
    dropped from both the record and the file.

    The identity captured is re-confirmed against a second walk before it is
    persisted. A pid released between the enumeration and the capture can be
    held by an unrelated process by the time its identity is read, and that
    identity would then be recorded as OURS -- self-consistent, so every
    later ownership check passes and the teardown signals a stranger. A pid
    absent from the second walk is not a descendant of this root and is
    dropped, and so is one whose start id now reads back as something other
    than the one captured. The window is not closed, but crossing it now needs a pid to
    become a stranger and then become our descendant again under the
    identity it had before.

    The walk itself is :func:`runtime_process_tree.guarded_tree_pass` in
    replace mode, the same pass ``AcpClient.refresh_process_tree`` runs
    add-only.

    The file is written BEFORE the in-memory record is replaced, and a failed
    write publishes nothing: the next pass re-reads the tree and tries again.

    Never raises on failure. A failed scan must not fail the spawn or the
    session start that called it -- the cost is a leak the sweep still
    reports, not a broken session -- but it logs at WARNING, because that
    report is then the only signal left. A CANCELLATION is not a failure and
    is deliberately NOT swallowed: it reaches the caller's cleanup guard,
    which owns the half-built runtime or session it must tear down.

    POSIX-shaped: ``_get_child_pids`` short-circuits on Windows, where the
    tree kill walks descendants itself through ``taskkill /T``.
    """
    pid = self._pid
    if pid is None:
        return
    try:
        async with self._descendant_scan_lock:
            # The root's own number can be reused too, and a walk from a
            # recycled root enumerates a stranger's whole tree, so the pass
            # brackets the walk with the root's identity. No recorded
            # identity means it cannot be verified at all: nothing is
            # recorded. The blocking reads ride the subprocess executor.
            result = await runtime_process_tree.guarded_tree_pass(
                pid,
                self._child_pids,
                probes=runtime_process_tree.TreeProbes(
                    walk=_get_child_pids,
                    capture=_capture_child_records,
                    start_id=platform_compat.get_process_start_id,
                    write=_replace_child_pids,
                ),
                executor=subprocess_executor(),
                root_holds=self._root_identity_holds,
                replace=True,
                keep_unwalked=_escapee_is_still_ours,
                retry_delay=self._DESCENDANT_RESCAN_DELAY if retry_when_empty else None,
            )
            if result.outcome == "write_failed":
                logger.warning(
                    "AcpRuntime: could not write the descendant PIDs of root %d -- "
                    "retrying on the next scan",
                    pid,
                )
                return
            if result.outcome != "written":
                return
            self._child_pids = result.record
        logger.info(
            "AcpRuntime: tracking %d descendant PID(s) of root %d (%d outside the tree)",
            len(result.record),
            pid,
            len(result.kept),
        )
    except Exception:
        logger.warning(
            "AcpRuntime: descendant PID snapshot failed for root %s -- a tree "
            "that outlives this runtime would be invisible to every reaper",
            pid,
            exc_info=True,
        )


async def _signal_tree(
    self: AcpRuntime,
    pid: int,
    sig: int,
    *,
    instance: str,
    expected: dict[int, str | None] | None = None,
) -> dict[int, str | None]:
    """Signal this runtime's process tree; return the orphans it reached.

    ``kill_process_tree`` is ``killpg(getpgid(pid))``, and ``getpgid`` raises
    once the root has exited. Read as "already dead", that leaves every
    process still in the group -- the launcher's children, the agent, its
    chat process -- unsignalled, reparented to init and holding their
    memory. A root that dies a few seconds into its life, before any
    descendant was recorded, is exactly the tree nothing else can find.

    So a reaped root is not the end of the teardown. The root was spawned as
    a session leader, so its pid IS the group id, and
    :func:`_signal_orphaned_runtime_group` signals that group once a live
    member vouches for it by identity -- and by *instance*: the per-spawn
    token this runtime put in its child's environment, which is what tells
    the root's own tree from a fresh runtime that took the root's recycled
    pid and leads a group that vouches just as well. The members it returns are empty for
    a tree that really is gone, which is what the caller needs to decide
    whether the grace-and-escalate that ``wait()`` would otherwise have
    driven is still owed -- and, when it is, they are what the escalation
    hands back as *expected*, so a group SIGKILL after the grace lands only
    on the group the SIGTERM did, never on a fresh runtime that took the
    root's number in between.

    Windows has no process groups, so this ladder is the POSIX half of the
    teardown and a Windows runtime does not reach it: ``_kill_inner`` drains
    the tree through its owned handles first and returns. That drain is what
    closes the reaped-root gap described above on Windows -- the descendants
    are pinned when the tree is SPAWNED, so a root that dies before any of
    them was recorded is still reachable, where ``kill_process_tree``'s
    ``taskkill /T`` walk would find nothing to tear down. The vouched-group
    path below is therefore guarded by ``IS_POSIX``, not merely
    platform-agnostic code that happens to no-op.
    Every call is off-loop: ``taskkill`` is a blocking spawn, and the group
    walk reads ``/proc``.

    The kill is attributed once, in ``kill``, not here. ``_kill_inner`` is
    this method's only caller, so the whole ladder -- SIGTERM, the grace, the
    SIGKILL escalation, the vouched-group arm -- is the mechanics of that one
    decision, and noting it per signal would write the same shot three times.
    """
    loop = asyncio.get_running_loop()
    # kill_process_tree resolves the group FROM the root's number, so it may
    # run only while that number is provably still ours, and the one proof
    # is the live start id matching the one recorded at spawn. ``returncode``
    # is NOT that proof: asyncio's child watcher does the waitpid in the
    # background and propagates the code to the Process object a callback
    # later, so a root can be reaped -- its number free for a fresh session
    # leader whose getpgid SUCCEEDS -- while ``returncode`` still reads None.
    # A root whose identity cannot be read is treated as gone: the vouched
    # path below reaches its members where it can, and where it cannot the
    # cost is a leak the sweep reports, never a signal to a stranger. The
    # same reasoning bars re-resolving on the escalation (``expected`` set).
    identity = self._root_identity()
    if expected is None and identity == "holds":
        recorded = self._start_time
        assert recorded is not None  # implied by identity == "holds"
        try:
            # PINNED, not merely checked. The tree kill is deferred to an
            # executor, so the identity verified here has to stay pinned
            # across that hop: a check that ends with its handle closed
            # leaves the root free to exit and the number free to be
            # recycled in between, and a kill that re-resolves the pid would
            # then tear down whatever holds it now.
            # kill_process_tree_pinned keeps the identity PINNED across the
            # terminate, which is what makes the number still mean this
            # process. On Windows it re-resolves nothing from the number at
            # all: it drains the tree through process handles opened against
            # this exact creation identity and held open until every member's
            # exit is confirmed. POSIX delegates straight through, where
            # os.killpg is issued in-process by this same interpreter.
            pinned = await loop.run_in_executor(
                subprocess_executor(),
                functools.partial(platform_compat.kill_process_tree_pinned, pid, recorded, sig),
            )
            if pinned:
                return {}
            # False is "identity unconfirmed" and NO signal was sent. Treat it
            # exactly as an identity that does not hold: fall through to the
            # vouched path, which names members by an inherited token instead
            # of by the root's number.
            logger.warning(
                "AcpRuntime kill: not resolving the tree of root PID %d from its "
                "number -- its identity could not be pinned across the terminate",
                pid,
            )
        except ProcessLookupError:
            pass
        except OSError:
            return {}
    reached: dict[int, str | None] = {}
    if platform_compat.IS_POSIX:
        reached = await loop.run_in_executor(
            subprocess_executor(),
            functools.partial(
                _signal_orphaned_runtime_group, pid, sig, instance, expected=expected
            ),
        )
    if reached:
        logger.warning(
            "AcpRuntime kill: root PID %d was already gone; signalled %d orphaned "
            "member(s) of its process group with signal %d",
            pid,
            len(reached),
            sig,
        )
    else:
        # Nothing was reached. The tree is leaked to the orphan sweep -- the
        # deliberate trade, a leak over a signal to a stranger -- but a silent
        # return made that trade invisible in the field, where it reads as a
        # teardown that worked.
        #
        # The guard is the GROUP, not the platform. A host that cannot read the
        # token never reaches a member, and so does a Linux host whose members
        # are there but do not vouch -- a sandbox that scrubbed the token, an
        # unreadable environ, a missing argv identity. The second is the
        # reported leak's own shape, so the line must cover it too; keying this
        # on the platform hid exactly that case on the one platform where the
        # vouched path runs.
        #
        # Still only when something is plausibly there: a root that exited
        # cleanly before a routine kill() leaves an empty group, and a line on
        # that path is noise on the ordinary teardown.
        # _pgroup_has_member_besides answers on every platform and is
        # conservative on a failed read, so an unreadable group still speaks. It
        # scans /proc or sysctl, hence the executor. Reporting only, never
        # routing: the attempt above is unconditional on POSIX and
        # _marked_group_members owns the platform answer.
        leaked = await loop.run_in_executor(
            subprocess_executor(),
            functools.partial(_pgroup_has_member_besides, pid, pid),
        )
        if leaked:
            logger.warning(
                "AcpRuntime kill: root PID %d could not be reached with signal %d -- "
                "%s, and %s, so its tree is left to the orphan sweep",
                pid,
                sig,
                self._ROOT_UNREACHED_REASON_BY_VERDICT[identity],
                (
                    "no member of its group vouched for this spawn's incarnation"
                    if group_vouching_available()
                    else "this host cannot vouch a process group by incarnation token"
                ),
            )
    return reached


async def kill(self: AcpRuntime, *, expected: bool = False, reason: str = "") -> None:
    """Kill the subprocess and release spawn resources even when cancelled.

    ``reason`` names the caller's intent ("warm mint teardown", "failed
    session setup cleanup", ...) and flows into the death log line and
    ``death_summary()``. Unattributed kills proved undiagnosable in the
    field: a runtime killed under a live turn surfaces to the turn only
    as a bare "process died during prompt", and a log line that says
    "killed" without saying WHO killed leaves nothing to correlate.

    The attribution line is written HERE, before any signal, rather than
    where the process is reaped -- a line saying a process died cannot say
    who decided it should.

    A REFUSED kill returns without signalling and without discarding
    anything. The sandbox and the bound workspace below belong to the
    *process*, not to this handle: releasing them under a runtime another
    lease is still using would strand a live agent on a deleted workspace,
    which is worse than the leak the kill was meant to prevent.
    """
    if not authorize_runtime_kill(self, reason=reason or "runtime kill", caller="AcpRuntime.kill"):
        return
    self._process_tree_confirmed_dead = False
    try:
        await self._kill_inner(expected=expected, reason=reason)
    finally:
        self._discard_sandbox_cleanup()
        await self._discard_bound_workspace()
        await self._close_remote_workspace()


async def _kill_inner(self: AcpRuntime, *, expected: bool = False, reason: str = "") -> None:
    """Kill the subprocess and clean up all state.

    ``expected`` changes log severity only: a deliberate teardown of a
    healthy runtime (pool TTL recycle, session shutdown, logout) passes
    ``expected=True`` to log the death at INFO. The default is False —
    matching ``_mark_dead`` — so every cleanup kill on a failure path
    (``initialize()``'s failed-spawn cleanup, a failed session setup) and
    any future call site stays a WARNING without having to opt in.
    ``_mark_dead`` additionally refuses to downgrade when the process
    already exited on its own, so a reap-after-death can never log INFO.
    """
    # Fail pending futures + poison session queues FIRST. _mark_dead sets
    # self._dead internally; doing it up front (before teardown) ensures any
    # waiters learn the runtime died. Calling it after setting _dead=True
    # would hit its early-return guard and skip all cleanup.
    self._mark_dead(f"killed ({reason})" if reason else "killed", expected=expected)

    if self._reader_task and not self._reader_task.done():
        self._reader_task.cancel()
        try:
            await self._reader_task
        except (asyncio.CancelledError, Exception):
            pass

    if self._exit_watch_task and not self._exit_watch_task.done():
        self._exit_watch_task.cancel()
        try:
            await self._exit_watch_task
        except (asyncio.CancelledError, Exception):
            pass

    if self._stderr_task and not self._stderr_task.done():
        self._stderr_task.cancel()
        try:
            await self._stderr_task
        except (asyncio.CancelledError, Exception):
            pass

    if self._process and platform_compat.IS_WINDOWS:
        # The Windows completion of the same teardown ``_signal_tree`` drives
        # on POSIX, and it runs INSTEAD of that ladder rather than inside it.
        # Both close the one failure: a root that exits while its agent and
        # MCP descendants keep running and holding their memory. They cannot
        # share a seam, because the evidence arrives at different times. POSIX
        # can name the survivors AFTER the fact -- the root was a session
        # leader, so its pid is still the group id and a live member vouches
        # for the group by an inherited token. Windows has no group and no
        # such token, and a reaped root leaves ``taskkill /T`` nothing to
        # walk, so the tree is instead pinned by handle when it is SPAWNED
        # and drained from those handles here. That also makes the ladder
        # below vestigial on Windows and not merely unused: the drain returns
        # only once every member's exit is CONFIRMED, so there is no grace
        # left to serve and no escalation owed. A drain that cannot confirm
        # raises, keeping the pins and the tracking for maintenance to retry,
        # which is why this must not be softened into a best-effort call.
        process = self._process
        pid = process.pid
        try:
            await platform_compat.terminate_windows_asyncio_tree(process)
        except platform_compat.WindowsTreeDrainPending as exc:
            # Pending, not lost: every pin is in the pending registry and the
            # cleanup sweep resumes this drain. One line, because a traceback
            # here reads as the crash and buries the failure that asked for
            # this kill. Still raised, so the caller retains.
            logger.warning(
                "AcpRuntime Windows tree for PID %s did not finish one bounded drain "
                "pass (%d member(s) pending cleanup); its handles stay pinned and the "
                "cleanup sweep keeps reaping it",
                pid,
                exc.pending,
            )
            raise
        except (OSError, asyncio.TimeoutError):
            logger.warning(
                "AcpRuntime Windows tree cleanup incomplete for PID %s; retaining process",
                pid,
                exc_info=True,
            )
            raise
        # Before the handle is dropped, and on this branch rather than at the
        # ladder's own amendment below: the death line was written pre-signal
        # and says returncode=<not reaped>, and the drain above returns only
        # once every member's exit is CONFIRMED, so the status is knowable at
        # exactly this point. The drain's failure path raises instead, keeping
        # the placeholder true for a tree it could not confirm.
        self._note_reaped_after_kill(process.returncode)
        self._process_tree_confirmed_dead = process.returncode is not None
        self._process = None
        self._process_instance = ""
        self._scope_unit = ""
        # Tracking was retired by the shared drain under the original pin.
        return

    if self._process:
        pid = self._process.pid
        # platform_compat.kill_process_tree: killpg on POSIX (the spawn
        # sets start_new_session=IS_POSIX, so the group is the tree);
        # taskkill /T on Windows, where os.getpgid/os.killpg do not exist
        # (a raw call raises AttributeError, which the OSError guard here
        # would NOT catch — the kiro-cli tree then leaks on every session
        # recycle). Offloaded to the subprocess executor: on Windows the
        # shim shells out to taskkill (a blocking subprocess.run), which
        # must not run on the event loop (no blocking call on the event
        # loop).
        # Read before the kill clears it: the group fallback needs the
        # incarnation this process was spawned as, not the empty successor.
        instance = self._process_instance
        orphaned_group = await self._signal_tree(pid, platform_compat.SIGTERM, instance=instance)
        escalated = False
        try:
            await asyncio.wait_for(self._process.wait(), timeout=self._KILL_TERM_TIMEOUT)
        except asyncio.TimeoutError:
            escalated = True
            await self._signal_tree(pid, platform_compat.SIGKILL, instance=instance)
            # Reap the child so a delivered SIGKILL doesn't leave a zombie
            # that the liveness probe below would misread as a survivor.
            try:
                await asyncio.wait_for(self._process.wait(), timeout=self._KILL_REAP_TIMEOUT)
            except asyncio.TimeoutError:
                pass
        # Only when the wait did NOT time out. The block below exists because
        # a root that was already gone makes wait() return at once, so the
        # escalation never ran; if it DID run, repeating it here would pay a
        # second grace and send the members a duplicate SIGKILL.
        if orphaned_group and not escalated:
            # The root was already gone, so wait() above returned at once and
            # the escalation never ran for the members left in the group.
            # Give them the same grace a live tree gets, then escalate to
            # the group -- aimed by the members the SIGTERM vouched, not by
            # the root's number, which a fresh runtime can hold by now.
            escalate = functools.partial(
                self._signal_tree,
                pid,
                platform_compat.SIGKILL,
                instance=instance,
                expected=orphaned_group,
            )
            try:
                await asyncio.sleep(self._KILL_TERM_TIMEOUT)
            except asyncio.CancelledError:
                # A shutdown that cancels this teardown inside the grace must
                # not leave SIGTERM-ignoring members alive: they were vouched
                # and signalled, and the SIGKILL is the only thing still
                # owed. Shielded so THIS cancellation cannot cut it short; a
                # further cancel raises at the await and leaves it running
                # unawaited, which is the bound this gives, not immunity. It
                # is one identity re-check per member and a signal each.
                await asyncio.shield(escalate())
                raise
            await escalate()
        # Before the handle is dropped: the death line above was
        # written pre-signal and says returncode=<not reaped>.
        root_confirmed_dead = self._process.returncode is not None
        self._note_reaped_after_kill(self._process.returncode)
        self._process = None
        # The id names the process that just ended; the next spawn mints its
        # own, and nothing may answer with this one in between.
        self._process_instance = ""
        self._scope_unit = ""
        if platform_compat.pid_exists(pid):
            # Both kill_process_tree calls above swallow OSError by design
            # (racing a normal exit), which makes a signal-delivery failure
            # (EPERM through a launcher wrapper, pgid drift) look identical
            # to success. Verify instead of assuming: a survivor must stay
            # PID-tracked so the startup/periodic sweeps keep a handle on
            # it — untracking here would leak the process until reboot.
            logger.warning(
                "AcpRuntime kill: PID %d survived SIGTERM/SIGKILL escalation; "
                "leaving PID tracked for sweep",
                pid,
            )
        else:
            logger.info("AcpRuntime killed (PID %d)", pid)

            # Untrack the PID so the orphan sweep doesn't chase a dead entry
            # (mirrors AcpClient._reset_state). Best-effort — a leftover entry
            # is only pruned lazily otherwise.
            #
            # By IDENTITY, the same way an observed death retires. The reap
            # above proved THIS process dead, not that its number is still
            # ours: the kernel can hand the number to a root this gateway
            # spawns next before this line runs, and a prefix-matched untrack
            # would then take the successor's lines with it. The recorded
            # start token names the line that is ours; the bare line is
            # removed only while the number is dead at that moment. A root
            # whose identity could not be read at spawn has no token to
            # compare, so it keeps the prefix-matched untrack it always had.
            try:
                if self._spawn_start_token:
                    if not _untrack_root_by_identity(pid, self._spawn_start_token):
                        # No session line of ours to retire (spawn's append
                        # failed, or a successor already replaced it): the
                        # bare line still goes, but only while the number
                        # is dead -- never by number alone.
                        _untrack_pid_if_dead(pid)
                else:
                    _untrack_pid(pid)
                    _untrack_session_pid(pid)
                unregister_protected_pid(pid)
            except Exception:
                logger.debug("AcpRuntime: PID untracking failed for %s", pid, exc_info=True)

        # Runs on BOTH branches above: whether the root died or survived says
        # nothing about a descendant that left the process group, and the
        # entry of one still running is what the sweep needs to reap it.
        saved_children = dict(self._child_pids)
        self._child_pids = {}
        survivors: list[int] = []
        if saved_children:
            survivors = await asyncio.to_thread(_prune_dead_descendants, saved_children)
            if survivors:
                logger.warning(
                    "AcpRuntime: retained tracking for %d descendant PID(s) that "
                    "survived teardown; the orphan sweep will reap them: %s",
                    len(survivors),
                    survivors,
                )
        self._process_tree_confirmed_dead = root_confirmed_dead and not survivors
