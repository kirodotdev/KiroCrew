"""Synchronous teardown of a leaked provider's process tree, and of an orphaned spawn group.

:func:`_sync_kill_provider` resolves the provider's root, consults the runtime-ownership
gate, commits the teardown, then TERMs and KILLs the isolated group and every recorded
descendant, each signal pinned to the identity captured before the first one;
:func:`signal_orphaned_spawn_group` and the runtime teardown signal the members of a
reaped leader's group that the per-spawn instance marker vouches for; and
:func:`_tracked_child_has_runtime_identity`, the argv check a vouched member of the ACP
runtime's group signalling must pass, and so must a tracked ``kiro_pids.txt`` child
reparented to a ``systemd`` subreaper before the facade's ``_cleanup_orphaned_mcp_servers``
signals it.

Split out of ``kiro_crew.session_pid``, which stays the import path and the patch target:
every name here is reached as ``kiro_crew.session_pid.<name>``, and a patch through that
module reaches this one (``kiro_crew._facade_mirror``). Nothing here imports it.
"""

from __future__ import annotations

import errno
import logging
import os
import signal
import sys
import time
from collections.abc import Mapping
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.runtime_ownership import (
    authorize_runtime_kill,
    commit_runtime_teardown,
    release_runtime_teardown,
    tenancy_epoch,
)
from kiro_crew.session_pid_harness import _is_managed_agent_process, _pid_cmdline
from kiro_crew.session_pid_orphan_scan import (
    _env_has_kirocrew_marker,
    _env_spawn_instance,
    _is_marked_mcp_launcher,
    _is_orphan_mcp,
)

#: The family logs as ``kiro_crew.session_pid``, the name callers, log filters and
#: ``caplog`` assertions use.
logger = logging.getLogger("kiro_crew.session_pid")


# Grace given to a TERMed provider tree before the group SIGKILL. Mirrors the
# 3.0 s budget ``AcpClient._kill_process`` allows on the async teardown path, so
# a runtime gets the same chance to flush and exit through its own shutdown
# whichever path reaps it.
_PROVIDER_TERM_GRACE_SECONDS = 3.0


# Poll interval while waiting out that grace.
_PROVIDER_TERM_POLL_SECONDS = 0.05


# ``(start_id, argv0 basename)`` -- as the ACP layer records a descendant at spawn
# time. Only the start id is verified before signalling (see
# :func:`_signal_provider_descendants`); the basename rides along unread. Spelled
# here rather than imported from the ACP layer, which this module must carry no
# knowledge of: ``scripts/check_agent_sdk_boundary.py`` counts even a type-only
# import as such knowledge, and ``test_agent_lifecycle_cycle.py`` pins the absence.
_ProviderChildRecord = tuple[str | None, bytes | None]


def _isolated_provider_group(pid: int) -> int | None:
    """The process GROUP to signal for a provider rooted at *pid*, or ``None``.

    ``None`` means a pid-scoped kill is the only safe option, so the caller must
    not reach for ``killpg``.

    A group id is returned only when *pid* is an isolated group leader
    (``pgid == pid``, not our own group, not init's), which is what a provider
    spawned with ``start_new_session=True`` is. Under that predicate the group
    holds this provider's tree and nothing else, so a group signal can never
    reach a foreign process -- the same guard
    :func:`_kill_orphan_browser_daemon` carries for the same reason.

    Callers resolve this while the root is still alive and reuse the number for
    the escalation: the group outlives its leader, so the id read here still
    names the survivors after a TERM reaps the root, and reading it up front
    keeps a recycled pid out of the answer.

    POSIX only. Windows has no process groups in this sense; teardown there goes
    through ``kill_process_tree`` (``taskkill /T``), which walks the child tree
    itself.
    """
    if platform_compat.IS_WINDOWS:
        return None
    pgid = platform_compat.pgroup_of(pid)
    if pgid is None:
        return None
    if pgid == pid and pgid > 1 and pgid != os.getpgrp():
        return pgid
    return None


def _root_identity_holds(pid: int, recorded_start: str | None, *, gated: bool) -> bool:
    """Whether *pid* is still the process whose identity was recorded.

    Called again before EVERY signal, not once up front: the identity checked at
    entry can go stale inside the SIGTERM grace, because this teardown is not the
    only reaper. ``asyncio``'s child watcher can reap the leader zombie on its own
    (``_reap_provider_root`` says as much), which frees the pid, and under pid-space
    wraparound a new process can take it before the SIGKILL lands.

    ``gated=False`` is the ``_proc`` / ``_active_proc`` shape, whose pid comes from
    a live handle this process owns: there is no recorded id to compare and the
    handle already proves the child is unreaped.
    """
    if not gated:
        return True
    if recorded_start is None:
        return False
    return platform_compat.get_process_start_id(pid) == recorded_start


def _pgroup_still_ours(
    pgid: int,
    root_pid: int,
    recorded_start: str | None,
    records: dict[int, _ProviderChildRecord],
    *,
    gated: bool,
) -> bool:
    """Whether an identity-verified process still owns process group *pgid*.

    A pgid is only as trustworthy as its members. Once every process in the group
    is gone the number is free, and a group signal aimed at it lands on whichever
    tree takes it next -- so the escalation may only signal the group while it can
    still name a member it has verified: the root itself, or one of the descendants
    recorded at spawn. An unverifiable group is not signalled; the caller falls
    back to the verified descendants it can still name individually.

    A held zombie counts, and deliberately so: it is still a group member and its
    identity is still readable, which is the whole point of not reaping the root
    until the last signal is out. Membership is read through
    :func:`_pid_in_pgroup` for that reason -- see there for why ``getpgid`` alone
    would drop the zombie root on macOS. The ungated ``_proc`` shape has no
    recorded start id, so its owned live handle supplies a fresh identity read for
    the listing comparison.
    """
    if platform_compat.IS_WINDOWS:
        return False
    if _root_identity_holds(root_pid, recorded_start, gated=gated):
        root_start = (
            recorded_start
            if recorded_start is not None
            else platform_compat.get_process_start_id(root_pid)
        )
        if _pid_in_pgroup(root_pid, pgid, root_start):
            return True
    for cpid, (start, _basename) in records.items():
        if start is None:
            continue
        if platform_compat.get_process_start_id(cpid) != start:
            continue
        if _pid_in_pgroup(cpid, pgid, start):
            return True
    return False


def _pid_in_pgroup(pid: int, pgid: int, start_id: str | None) -> bool:
    """Whether *pid* is a member of process group *pgid*, zombie or not.

    ``getpgid`` answers for a live member on every POSIX host, and on Linux for a
    zombie too. macOS refuses it for a zombie (``ESRCH``: the kernel looks the
    pid up among running processes only), which is exactly the member
    :func:`_pgroup_still_ours` needs to see -- the exited-but-unreaped root is
    the one verified member left in the group once its children have been
    SIGTERMed, and losing it there suppresses the group SIGKILL, so a child that
    ignored SIGTERM outlives the teardown. ``sysctl KERN_PROC_PGRP`` lists the
    group's zombies alongside its live members, so it settles the question
    ``getpgid`` cannot.

    The listing is consulted ONLY when ``getpgid`` had no answer. A definite
    answer naming another group is final: the pid has left the group, or the
    number has moved on to a stranger's tree, and a second oracle must not be
    allowed to overrule that verdict in the direction of signalling. Unreadable
    on both is "not a member": the caller must not signal a group it cannot
    prove it owns.

    A listed member counts only when its ``start_id`` matches *start_id* as well
    as its pid. The caller verified the identity before asking, but the listing
    is a separate read: a zombie collected by another reaper in between frees the
    pid, and under wraparound a stranger's new group leader can hold it by the
    time the listing runs. Matching the start instant refuses that recycled pid;
    a caller with no identity to offer (``None``) gets "not a member" for the
    same reason.
    """
    answer = platform_compat.pgroup_of(pid)
    if answer is not None:
        return answer == pgid
    if sys.platform != "darwin" or start_id is None:
        return False
    members = platform_compat.darwin_pgroup_members(pgid)
    if members is None:
        return False
    return any(m.pid == pid and m.start_id == start_id for m in members)


def _group_from_witnessed_descendant(
    pid: int,
    recorded_start: str | None,
    records: dict[int, _ProviderChildRecord],
) -> int | None:
    """The group led by *pid*, once :func:`_pgroup_still_ours` vouches for it.

    Companion to :func:`_isolated_provider_group` for the case it cannot serve: a
    reaped leader. ``pgroup_of`` needs the leader in ``/proc``, so once it is gone
    the group has no readable id and the escalation signals nothing -- while the
    members left in that group keep running.

    ``pgroup_of_leader`` resolves the id for a reaped leader, but from the pid
    ALONE, and an unverified pid is what a RECYCLED one looks like: killpg on it
    takes a stranger's tree. So this reads the number and then hands it to the same
    ownership check the escalation itself uses, rather than carrying a second copy
    of that loop.

    What is added here is only the leader predicate: ``candidate == pid`` ties the
    number to the leader we recorded, since a group a descendant ``setsid``-ed into
    is not the one this pid led.

    ``None`` -- no group signal, exactly as before -- when no verified member
    vouches, when the group is not the one ``pid`` leads, and when signalling is
    denied. POSIX only, like :func:`_isolated_provider_group`.
    """
    if platform_compat.IS_WINDOWS:
        return None
    candidate = platform_compat.pgroup_of_leader(pid)
    if candidate is None or candidate != pid or candidate <= 1 or candidate == os.getpgrp():
        return None
    if not _pgroup_still_ours(candidate, pid, recorded_start, records, gated=True):
        return None
    return candidate


def _provider_descendant_records(
    provider: object,
    pid: int,
    *,
    include_live_walk: bool = True,
    recorded_start: str | None = None,
    gated: bool = False,
) -> dict[int, _ProviderChildRecord]:
    """Snapshot a provider's descendants as ``pid -> (start_time, basename)``.

    Merges the runtime's own spawn-time snapshot (``client._child_pids``, which
    holds descendants that have already reparented away from the root and are
    therefore invisible to a live walk) with a fresh recursive scan of the
    process tree, and records each pid's identity so
    :func:`_signal_provider_descendants` can refuse a recycled one.

    Taken BEFORE any signal: once the root exits its children reparent to init
    and no walk can find them from the root again.

    Reads the machine through ``platform_compat`` alone -- ``process_descendants``
    for the walk, ``get_process_start_id`` for the identity -- so this module
    borrows nothing from the agent layer to do it. That is possible because the
    ACP layer records the SAME neutral start id at spawn time, so a stored record
    and a fresh one are directly comparable.

    ``include_live_walk=False`` drops the fresh walk and keeps ONLY the spawn-time
    snapshot. Pass it when the root's own identity could not be verified: a walk
    from an unverified pid enumerates whoever holds it now, and because those pids
    would have their identity captured here and re-read moments later they would
    MATCH and be signalled -- turning a recycled root into a licence to kill a
    stranger's children. The stored snapshot cannot do that: its entries were
    recorded when the tree was provably ours.

    The walk is BRACKETED by the same identity check for the same reason. A root
    verified just before this call can exit while the walk runs, and the pid can be
    reused inside that window, so a walk that started on our tree can finish on a
    replacement's. Re-reading the identity afterwards and discarding the fresh
    entries on a mismatch is what keeps a stale verification from authorising the
    sweep; the spawn-time snapshot is unaffected and is still returned.
    """
    records: dict[int, _ProviderChildRecord] = {}
    client = getattr(provider, "_client", None)
    stored = getattr(client, "_child_pids", None) if client is not None else None
    if isinstance(stored, dict):
        for cpid, record in stored.items():
            if isinstance(cpid, int) and cpid > 1:
                records[cpid] = record if isinstance(record, tuple) else (record, None)
    if not include_live_walk:
        return records
    if not _root_identity_holds(pid, recorded_start, gated=gated):
        logger.warning(
            "_sync_kill_provider: skipping the live descendant walk for pid %d -- "
            "identity went stale before it started",
            pid,
        )
        return records
    try:
        fresh = [p for p in platform_compat.process_descendants(pid) if p > 1 and p not in records]
    except Exception:
        logger.debug("_sync_kill_provider: descendant scan failed for %d", pid, exc_info=True)
        fresh = []
    if not _root_identity_holds(pid, recorded_start, gated=gated):
        logger.warning(
            "_sync_kill_provider: discarding %d freshly walked descendant(s) of pid %d -- "
            "the root's identity changed while the walk ran, so they are not ours",
            len(fresh),
            pid,
        )
        return records
    # Capture each identity, THEN re-confirm ancestry from a second snapshot. The
    # enumeration above and the capture below are separate reads, so a descendant
    # can exit and its pid be reused in between, handing us a stranger's start id
    # that then verifies at signal time.
    #
    # The second snapshot is bracketed by the root check as well, and it is NOT
    # enough to argue that an intersection can only shrink the set. Membership is
    # not the only thing these two reads decide: if the ROOT pid is itself reused
    # by a foreign process F while this runs, the second snapshot enumerates F's
    # children, so a first-walk pid that F's child Y now holds is confirmed by the
    # filter and carries Y's captured id -- which the signal-time re-read then
    # agrees with instead of catching. Re-reading the root identity here is what
    # separates "this pid is still in OUR tree" from "this pid is in whatever tree
    # holds that number now".
    captured = {cpid: platform_compat.get_process_start_id(cpid) for cpid in fresh}
    try:
        still_ours = set(platform_compat.process_descendants(pid))
    except Exception:
        logger.debug("_sync_kill_provider: re-scan failed for %d", pid, exc_info=True)
        still_ours = set()
    if not _root_identity_holds(pid, recorded_start, gated=gated):
        logger.warning(
            "_sync_kill_provider: discarding %d freshly walked descendant(s) of pid %d -- "
            "the root's identity changes across the re-scan, so the tree scanned is "
            "not ours",
            len(captured),
            pid,
        )
        return records
    for cpid, start_id in captured.items():
        if cpid in still_ours:
            records[cpid] = (start_id, None)
        else:
            logger.warning(
                "_sync_kill_provider: dropping pid %d -- it stops being a descendant of "
                "%d between the walk and its identity capture, so the id may be a "
                "stranger's",
                cpid,
                pid,
            )
    return records


def _signal_provider_descendants(records: dict[int, _ProviderChildRecord], sig: int) -> None:
    """Send *sig* to each snapshotted descendant still alive, leaf-first.

    Covers the descendant a group signal cannot reach: one that ``setsid``-ed
    out of the group, and one already reparented before the snapshot. Ownership
    is re-verified per pid against the recorded start id, so a pid recycled
    inside the teardown window is skipped rather than signalled (deny-by-default).
    Leaf-first so a parent cannot fork a replacement while its own children are
    being reaped.

    The start id alone decides this, through ``platform_compat`` rather than the
    agent layer's own check: it is microsecond libproc on macOS, 100 ns creation
    time on Windows and stat field 22 on Linux, so two processes on the same pid
    at different times cannot share one. The recorded basename is deliberately
    NOT compared, because reading a live pid's basename would mean borrowing the
    ACP layer's reader and this module is held to no knowledge of that layer
    (``scripts/check_agent_sdk_boundary.py``). Basename was only ever a second
    opinion on top of the start id, and it cannot rescue a case the start id
    misses -- a matching start id already means it is the same process.
    """
    if platform_compat.IS_WINDOWS:
        return
    for cpid in reversed(list(records)):
        try:
            if not platform_compat.pid_exists(cpid):
                continue
            expected_start, _expected_basename = records[cpid]
            actual_start = platform_compat.get_process_start_id(cpid)
            if expected_start is None or actual_start is None or actual_start != expected_start:
                logger.debug("_sync_kill_provider: skipping pid %d -- not ours (recycled?)", cpid)
                continue
            platform_compat.kill_pid(cpid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def _pid_exited_but_unreaped(pid: int) -> bool:
    """True when *pid* has finished running: a zombie, or already gone.

    Lets the teardown observe the root's exit WITHOUT reaping it. That matters
    because a zombie still owns its pid, and for a group leader that pid IS the
    process group id -- reaping it frees the number, after which the pgid may be
    handed to an unrelated new leader. So the escalation has to be able to say
    "the root has exited" without also making its pgid ambiguous.

    Conservative on an unreadable state: returns False, i.e. "still running", so
    a caller waits out its grace rather than exiting early on a guess. On macOS
    the state comes from ``sysctl KERN_PROC_PID``, which lists zombies where
    libproc refuses them; a liveness probe would not do, because a zombie
    answers ``kill(pid, 0)`` as present and the root would read as running until
    someone else reaped it -- which this teardown deliberately does not, until
    the last group signal is sent. On other non-Linux platforms there is no
    zombie state to read, so this falls back to plain liveness: an
    exited-but-unreaped root reads as alive and the caller waits out the grace.
    """
    if sys.platform == "darwin":
        zombie = platform_compat.darwin_pid_is_zombie(pid)
        return bool(zombie) if zombie is not None else False
    if sys.platform != "linux":
        return not platform_compat.pid_exists(pid)
    zombie = platform_compat.pid_is_zombie(pid)
    if zombie is None:
        # Gone, or present but unreadable: only a pid that has gone has exited.
        return not platform_compat.pid_exists(pid)
    return zombie


def _pgroup_has_member_besides(pgid: int, root_pid: int, *, proc_root: Path | None = None) -> bool:
    """True when some process other than *root_pid* is still in group *pgid*.

    ``pgroup_exists`` cannot answer this: a retained zombie leader is itself a
    group member, so ``killpg(pgid, 0)`` keeps succeeding after everything real
    has died (measured). Without this distinction, holding the zombie for pgid
    safety would cost every teardown its full SIGTERM grace.

    One ``/proc`` pass through ``platform_compat.linux_pgroup_members``, which
    reads every stat as bytes. Callers gate it behind the cheap probes, so the
    common path runs it once. *proc_root* substitutes a fixture process table.

    Conservative on a scan failure: returns True, i.e. "assume the group still
    holds something", so the caller escalates rather than declaring the tree
    gone on unread evidence.

    macOS lists the group with ``sysctl KERN_PROC_PGRP`` and applies the same
    rule -- a zombie member is not holding the group open. Other non-Linux
    platforms can only ask whether the group exists, which a retained zombie
    leader keeps answering yes to, so there the caller waits out its grace.
    """
    if sys.platform == "darwin":
        members = platform_compat.darwin_pgroup_members(pgid)
        if members is None:
            return True
        return any(m.pid != root_pid and not m.zombie for m in members)
    if sys.platform != "linux":
        return platform_compat.pgroup_exists(pgid)
    group = platform_compat.linux_pgroup_members(pgid, proc_root=proc_root)
    if group is None:
        logger.debug("_sync_kill_provider: /proc scan failed for pgid %d", pgid)
        return True
    return any(member != root_pid for member in group)


def group_vouching_available() -> bool:
    """Whether the instance-vouched group read can reach a tree on this host.

    The vouch reads ``/proc/<pid>/environ`` for the per-spawn incarnation token,
    which exists on Linux alone. Everywhere else :func:`_marked_group_members`
    answers ``{}`` and a teardown whose root identity is not proven reaches
    NOTHING -- a bounded leak the orphan sweep reports, never a signal to a
    stranger. Callers need to be able to SAY that, so the platform test lives
    here once rather than being re-derived at each reader.
    """
    return sys.platform == "linux"


def _marked_group_members(
    pgid: int,
    instance: str,
    *,
    require_runtime_identity: bool = True,
    proc_root: Path | None = None,
) -> dict[int, str | None]:
    """Live members of process group *pgid* spawned as incarnation *instance*.

    Returned as ``pid -> start id`` so a caller that signals the group twice can
    prove on the second pass that it is still the SAME group: a pid and a start
    instant name one process for good, where the group number alone does not
    (see :func:`_signal_orphaned_runtime_group`).

    *instance* is the per-spawn ``KIROCREW_SPAWN_INSTANCE`` the runtime put on
    its root's environment, and a member counts only if it carries that exact
    value. The generic ``KIROCREW_SPAWNED`` marker proves a group is SOME Kiro
    Crew runtime's; it cannot prove it is this one's, because every runtime here
    is a marked session leader and a root pid released to a fresh spawn names a
    group that carries the marker just as well. The instance is what tells them
    apart. An empty *instance* matches nothing.

    The proof a teardown needs once the group's LEADER is gone. A leader spawned
    with ``start_new_session=True`` names its group by its own pid, so the number
    survives the leader -- but a bare number is what a recycled pid looks like
    too, and ``killpg`` on it would take a stranger's tree. A member that carries
    ``KIROCREW_SPAWNED`` in its exec-time environment is the positive identity
    that a group is one Kiro Crew spawned; a detached survivor that merely
    inherited the marker is excluded by the argv identity check, the same pair
    the tracked sweep's systemd arm requires.

    The start id is the one ``platform_compat.linux_pgroup_members`` read
    alongside the group and state, so it names the process that was admitted;
    a member whose start cannot be read is left out. *proc_root* substitutes a
    fixture process table.

    Linux only. The environ read is Linux-only and fail-closed everywhere else,
    so the answer is ``{}`` on macOS and Windows and the caller signals nothing
    -- a missed reap there, never a wrong kill. Zombies are skipped: they hold
    no memory and cannot be signalled into exiting.

    *require_runtime_identity* is the ARGV gate
    (:func:`_tracked_child_has_runtime_identity`), on by default because it is the
    shape the ACP runtime's own group has. A caller whose tree is not an agent
    runtime turns it OFF -- an app backend's members are whatever its manifest runs
    (a uvicorn worker, a build subprocess), so requiring the runtime shape excludes
    every one of them and the reap reaches nothing. Turning it off is not "no
    identity": the group and instance checks above still apply and are already
    conclusive, because the instance is a fresh value per spawn and cannot be
    inherited from an earlier or later incarnation of the same group number. The
    ACP path's reason for the extra gate -- an intentional survivor that merely
    inherited the TREE-WIDE ``KIROCREW_SPAWNED`` marker -- does not reach a member
    of THIS group: a process that deliberately detaches calls ``setsid`` and
    thereby leaves the group. See :func:`signal_orphaned_spawn_group`.
    """
    if not group_vouching_available() or pgid <= 1 or not instance:
        return {}
    group = platform_compat.linux_pgroup_members(pgid, proc_root=proc_root)
    members: dict[int, str | None] = {}
    for member, start_ticks in (group or {}).items():
        # The start id comes from the read that admitted the member: one read
        # later, the pid may already name another process.
        if start_ticks is None:
            continue
        if (
            _env_spawn_instance(member, proc_root) == instance
            and _env_has_kirocrew_marker(member, proc_root)
            and (not require_runtime_identity or _tracked_child_has_runtime_identity(member))
        ):
            members[member] = str(start_ticks)
    return members


#: Errnos that mean "this kernel does not have the pidfd syscall", as opposed to
#: "it has it and refused this call". Only the first may use the numeric path.
_PIDFD_ABSENT_ERRNOS = frozenset({errno.ENOSYS, errno.EPERM})


def _signal_pid_by_identity(pid: int, sig: int, start: str) -> bool:
    """Signal *pid* only while it is still the process whose start id is *start*.

    Returns True when the signal was delivered, False when the identity fails to
    match, and raises the delivery error otherwise.

    Re-reading the start id and then calling ``os.kill`` leaves a window between
    the two: the number can be recycled in it, and the signal lands on whatever
    holds it now. Narrowing that window cannot close it, because a pid is not a
    handle -- it is a key the kernel may reissue. A pidfd IS a handle: it refers
    to one process for as long as it is open, so once the identity is confirmed
    AFTER opening it, ``pidfd_send_signal`` cannot reach a later occupant of the
    number no matter how long the descriptor is held. The order is open, then
    verify, then signal -- opening first is what makes the verification binding.

    ``pidfd_open`` needs Linux 5.3 and this whole path is Linux-only, but a
    kernel without it must still be reachable, so there the re-verified
    ``os.kill`` stands: the same narrow window as before, never a wider one.
    """

    def _by_number() -> bool:
        """The identity-verified numeric signal, for a host with no pidfd."""
        if platform_compat.get_process_start_id(pid) != start:
            return False
        os.kill(pid, sig)
        return True

    opener = getattr(os, "pidfd_open", None)
    sender = getattr(signal, "pidfd_send_signal", None)
    if opener is None or sender is None:
        return _by_number()
    try:
        fd = opener(pid)
    except ProcessLookupError:
        return False
    except OSError as exc:
        if exc.errno in _PIDFD_ABSENT_ERRNOS:
            # The attribute exists on any Linux build, but the SYSCALL answers
            # ENOSYS before 5.3 (and a seccomp filter can hide it with EPERM).
            # The capability is absent on this host, which is precisely the case
            # the numeric path is for.
            return _by_number()
        # The call exists and refused this open (EMFILE, ENOMEM): no handle, so no
        # atomic signal. Falling back to the number here would reopen the window
        # the handle closes, so fail closed and leave the member to the orphan
        # sweep -- a leak over a signal to a stranger, the trade the rest of this
        # path makes. A fallback is for a capability that is missing, never for one
        # that is present and said no.
        logger.warning(
            "_signal_pid_by_identity: no pidfd for pid %d, so signal %d is not sent; "
            "leaving it to the orphan sweep",
            pid,
            sig,
            exc_info=True,
        )
        return False
    try:
        # AFTER the open: the descriptor already pins whichever process answered,
        # so a match here proves the pinned process is the one that vouched.
        if platform_compat.get_process_start_id(pid) != start:
            return False
        try:
            sender(fd, sig)
        except OSError as exc:
            # pidfd_send_signal landed in 5.1 and pidfd_open in 5.3, so the two
            # can disagree; the same split applies.
            if exc.errno in _PIDFD_ABSENT_ERRNOS:
                return _by_number()
            raise
        return True
    finally:
        os.close(fd)


def _vouch_and_signal_orphaned_group(
    pgid: int,
    sig: int,
    instance: str,
    *,
    expected: Mapping[int, str | None] | None = None,
    require_runtime_identity: bool = True,
) -> tuple[dict[int, str | None], dict[int, str | None]]:
    """Signal a runtime's group members after its leader has been reaped, if ours.

    The kill path that signals a live tree is ``killpg(getpgid(root))``, and it
    has a hole exactly where the leak lives: ``getpgid`` raises once the root has
    exited, the caller reads that as "already dead", and the launcher, agent and
    chat processes left in the group are never signalled. They reparent to init
    holding their memory, and if the root died before any descendant was
    recorded there is no tracking entry to find them by either.

    So resolve the group from the contract instead of from the dead pid -- the
    root was a session leader, so ``pgid == root pid`` -- and find its members
    through :func:`_marked_group_members`, which vouches each one by *instance*:
    the per-spawn token the runtime put on its root's environment, which its
    whole tree inherited. Then signal THOSE MEMBERS, each re-verified by start id
    at the instant of the signal -- not the group number. The number is the
    reaped root's pid and can be handed to a fresh session leader at any moment,
    including between the vouch and the signal; a member's pid plus its start
    instant cannot be. No vouching member means no signal: declining to act
    costs a leak the sweep still reports, where acting costs an unrelated
    process. ``pgid`` is still refused for 0, 1 and our own group, since a
    member listing keyed on one of those would be a listing of the wrong thing.

    The instance is the incarnation pin, and it is what the generic marker
    cannot supply. Every runtime here is a marked session leader, so a root pid
    released and handed to a fresh spawn names a group that carries the marker
    just as well -- and a signal aimed by the number and the marker alone would
    terminate that fresh runtime's live session. A fresh spawn carries a
    different instance, so it does not vouch for this one's group.

    An escalation additionally passes the members the first pass vouched, as
    *expected*, and signals only those of them still alive under the same start
    id -- nothing found for the first time now: the instance proves the
    incarnation, the start id proves the pass is looking at the same processes,
    and a process that was not signalled on the first pass owes no escalation.

    Returns ``(vouched, signalled)``: the live members the vouch FOUND, and the
    subset a signal was actually delivered to -- the latter being the value a
    caller hands back as *expected*. A member that exited between the vouch and the
    signal, or whose identity stops reading as vouched, is skipped; so is one
    whose signal the kernel REFUSED, which is why the two maps are reported
    separately. A teardown that only has to clear its own state can ignore the
    census (see :func:`_signal_orphaned_runtime_group`); a caller deciding whether
    to keep an orphan's only record cannot, because an empty ``signalled`` alone
    cannot say whether the group is gone or merely unreachable right now.

    *require_runtime_identity* is passed through to :func:`_marked_group_members`;
    a caller whose tree is not an agent runtime turns it off. See
    :func:`signal_orphaned_spawn_group`, the public entry point for those.
    """
    if platform_compat.IS_WINDOWS or pgid <= 1 or pgid == os.getpgrp() or not instance:
        return {}, {}
    vouched = _marked_group_members(
        pgid, instance, require_runtime_identity=require_runtime_identity
    )
    if not vouched:
        return {}, {}
    members = vouched
    if expected is not None:
        still_ours: dict[int, str | None] = {
            p: start
            for p, start in members.items()
            if p in expected and start is not None and start == expected[p]
        }
        if not still_ours:
            logger.info(
                "_vouch_and_signal_orphaned_group: none of the %d member(s) vouched for "
                "group %d are still alive; not re-signalling a group that may be a "
                "newer runtime's",
                len(expected),
                pgid,
            )
            return vouched, {}
        # The escalation's target set is the FIRST pass's, not a fresh census: a
        # member found only now was not signalled then, owes no grace, and is
        # exactly what a newer incarnation of the number would look like.
        members = still_ours
    # Signal the MEMBERS, each re-verified by start id at the instant of the
    # signal, never the group number. ``killpg(pgid)`` is aimed at a number, and
    # the number is the reaped root's pid, which the kernel can hand to a fresh
    # session leader between the vouch above and the signal; a pid plus a start
    # instant names one process for good, so a member whose identity still reads
    # as vouched is the process that vouched. Highest pid first as a cheap
    # leaf-first order, so a parent cannot fork a replacement while its own
    # children are being signalled.
    signalled: dict[int, str | None] = {}
    for member in sorted(members, reverse=True):
        start = members[member]
        if start is None:
            continue
        try:
            if not _signal_pid_by_identity(member, sig, start):
                continue
        except ProcessLookupError:
            continue
        except OSError:
            logger.warning(
                "_vouch_and_signal_orphaned_group: signal %d to pid %d (group %d) refused; "
                "leaving the member alive; the caller decides what a refusal means",
                sig,
                member,
                pgid,
                exc_info=True,
            )
            continue
        signalled[member] = start
    return vouched, signalled


def _signal_orphaned_runtime_group(
    pgid: int,
    sig: int,
    instance: str,
    *,
    expected: Mapping[int, str | None] | None = None,
) -> dict[int, str | None]:
    """The ACP teardown's view of :func:`_vouch_and_signal_orphaned_group`.

    Returns only what was SIGNALLED, which is all this caller acts on: an ACP
    teardown clears its own state and prunes its PID entries whatever the kernel
    answered, so a refused signal changes nothing it does next. A caller that must
    tell "the group is empty" apart from "every signal was refused" -- because it
    is deciding whether to keep a record that is an orphan's only handle -- needs
    the vouched census too and calls the implementation through
    :func:`signal_orphaned_spawn_group`.
    """
    return _vouch_and_signal_orphaned_group(pgid, sig, instance, expected=expected)[1]


def signal_orphaned_spawn_group(
    pgid: int,
    sig: int,
    instance: str,
    *,
    expected: Mapping[int, str | None] | None = None,
) -> tuple[dict[int, str | None], dict[int, str | None]]:
    """Signal a NON-agent-runtime spawn's group members after its leader is gone.

    Public entry point onto :func:`_vouch_and_signal_orphaned_group` for a tree
    that Kiro Crew spawned as its own session leader but which is not an ACP
    runtime -- today an app backend (``kiro_crew.apps.backend``), whose members are
    whatever the app's manifest runs. Read that function's docstring for the
    reasoning; the single difference in the signalling is that the per-member ARGV
    gate is off, because an app backend's tree never has the runtime shape and
    would otherwise vouch for nothing.

    Every other guarantee is the one the ACP path makes and is deliberately not
    re-implemented here: the group is resolved from the session-leader contract
    rather than from the dead pid, each member is vouched by the caller's exact
    per-spawn instance token, each signal is pinned to a pid plus its start
    instant (never aimed at the group NUMBER, which the kernel may have reissued),
    an escalation signals only members the first pass vouched, and a host that
    cannot read the vouch signals nothing at all.

    Returns ``(vouched, signalled)``: the live members the vouch FOUND, and the
    subset a signal was actually delivered to. The ACP path discards the census
    because a refused signal changes nothing it does next, but this caller is
    deciding whether to keep a record that is an orphan's only handle, and for that
    the two must be told apart: an empty ``signalled`` with a non-empty ``vouched``
    means the members are alive and the kernel refused (a later attempt can
    succeed), while both empty means the group is genuinely gone. Collapsing them
    into one count is how a refusal comes to look like a completed reap.

    Callers must stamp a fresh ``KIROCREW_SPAWN_INSTANCE`` on the tree's root at
    spawn time and persist it with the pid they will later reap by; without the
    token there is no identity to vouch with and both maps come back empty. Check
    :func:`group_vouching_available` to report a host where the vouch cannot be
    read as the leak it is, rather than as a completed reap.
    """
    return _vouch_and_signal_orphaned_group(
        pgid,
        sig,
        instance,
        expected=expected,
        require_runtime_identity=False,
    )


def _provider_tree_gone(
    pid: int, pgid: int | None, records: dict[int, _ProviderChildRecord]
) -> bool:
    """True when nothing of the provider's tree is alive any more.

    Probes the GROUP when one was resolved -- a group outlives its leader, so a
    dead root proves nothing about the launcher and agent processes left in it,
    which is the whole failure this teardown exists to close. The root is judged
    by :func:`_pid_exited_but_unreaped` rather than by liveness, because the
    escalation deliberately holds its zombie to keep the pgid unambiguous, and a
    zombie answers every liveness probe as present.

    The cheap record probe runs first so the ``/proc`` group scan is reached only
    once the recorded descendants are all gone.
    """
    if any(platform_compat.pid_exists(cpid) for cpid in records):
        return False
    if not _pid_exited_but_unreaped(pid):
        return False
    if pgid is not None:
        return not _pgroup_has_member_besides(pgid, pid)
    return True


def _reap_provider_root(pid: int, recorded_start: str | None, *, gated: bool) -> None:
    """Reap *pid* if it is still the process whose identity was recorded.

    A zombie answers every liveness probe as present until someone waits on it.
    ``ChildProcessError`` means the process is not ours to reap (or asyncio's
    child watcher got there first), which is not a failure here.

    Identity is re-checked immediately before the wait, for the same reason every
    signal re-checks it: that watcher can reap the leader zombie on its own, which
    frees the pid, and ``waitpid`` on a recycled pid consumes an UNRELATED child's
    exit status. That loss is silent and nothing detects or repairs it, so this
    reads the identity here rather than trusting the one taken at entry.

    WHY THE RE-READ IS ENOUGH, on every platform. Linux keeps ``/proc/<pid>/stat``
    readable for a zombie. macOS ``proc_pidinfo`` refuses one, so
    :func:`platform_compat.get_process_start_id` falls back to ``sysctl``, which
    reads the same start instant from the kernel's zombie list. Either way the
    identity of the state this teardown CREATES -- a killed root held unreaped to
    keep its pgid unambiguous -- survives the exit and the check passes for the case
    this exists to handle.

    Where an identity cannot be read at all, refusing is deny-by-default working as
    designed, not a gap to route around. An unreadable identity is exactly the case
    where a recycled pid cannot be told from our own zombie: a freed pid can be taken
    by another child of THIS process that is itself an unreaped zombie, and that
    occupant reads as unreadable too, so a wait would steal its exit status and its
    own watcher would report a code that never happened.

    What such a refusal leaves behind is a ZOMBIE, which has already released its
    memory: one process-table entry and an exit status, bounded by pid space and
    gone when this process exits. That is not the leak this teardown exists to
    close -- that one is a RUNNING descendant holding hundreds of megabytes with no
    tracking entry left to find it by. And the entry is not necessarily permanent:
    asyncio's child watcher may still reap it afterwards, since the root is this
    process's child. A benign bounded entry is the cheaper side of the trade.
    """
    if not _root_identity_holds(pid, recorded_start, gated=gated):
        logger.warning(
            "_sync_kill_provider: NOT reaping pid %d -- identity does not hold, so a "
            "wait would consume an unrelated child's status",
            pid,
        )
        return
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass


def _release_client_seed_registrations(client: object) -> None:
    """Run the client's synchronous settings-seed hand-back, if it has one.

    ``AcpClient.release_settings_seed_claim`` withdraws the owner claim or reader
    lease the client holds on its ``settings.local.json`` seed; a torn-down client
    whose ``shutdown`` never runs otherwise keeps that registration for the life of
    the gateway, and every later session on the same work dir reads the path as a
    live sibling's. Resolved by name rather than by type, as every client
    read in this leaf is: ``session_pid`` imports nothing from the ACP layer. A
    client without the method -- a test stand-in, a provider shape that never
    seeds -- costs nothing, and one that holds no registration performs no I/O.
    Never lets an exception out: the teardown that follows is the process's only
    remaining end, and a hand-back that did not land is the recorded-orphan shape
    a later session already repairs.
    """
    hand_back = getattr(client, "release_settings_seed_claim", None)
    if not callable(hand_back):
        return
    try:
        hand_back()
    except Exception:
        logger.debug(
            "_sync_kill_provider: the client's settings-seed hand-back failed", exc_info=True
        )


def _root_identity_refuted(pid: int, recorded_start: str | None, live_start: str | None) -> bool:
    """Whether an unverified recorded root is KNOWN to be gone or recycled.

    Decides the settings-seed hand-back for a root whose identity did not hold.
    True when the pid names no process at all (the runtime is gone) or when a live
    start identity was read and differs from the recorded one (the pid was handed to
    a stranger): in both, the process that read the seed is not the one behind this
    pid, so handing the claim back lets the next session on the work dir adopt the seed.
    False when nothing can be read -- the pid exists but its start identity is
    unavailable, or no identity was recorded at spawn -- because an unreadable
    identity cannot tell our live runtime from a stranger's, and a successor must not
    re-seed the permission file under a runtime that may still be ours.
    """
    if not platform_compat.pid_exists(pid):
        return True
    return (
        isinstance(recorded_start, str) and live_start is not None and live_start != recorded_start
    )


def _sync_kill_provider(provider: object) -> None:
    """Synchronously kill a provider's whole process tree.

    Used during CancelledError handling where async shutdown is unreliable
    (asyncio.shield + await raises CancelledError immediately, leaving
    shutdown fire-and-forget).  Escalates SIGTERM to SIGKILL after a bounded
    grace.

    Signals the process GROUP, not the pid. An agent runtime is a tree -- a
    sandbox launcher, the agent binary, its own chat subprocess and a handful of
    MCP stub children -- and the pid recorded for a provider is the tree's group
    leader (spawned ``start_new_session=True``). A pid-scoped signal reaps that
    leader alone; everything below it survives, reparents to init, and holds its
    memory for the life of the machine, with no tracking entry left to find it
    by. Descendants outside the group are swept individually.

    ``provider`` is deliberately ``object`` rather than ``LLMProvider``.  Every
    read below goes through ``getattr(..., None)`` against a PRIVATE attribute
    that the provider ABC does not declare, so the ABC never described this
    parameter -- and importing it here for the annotation alone closed a cycle:
    session_pid -> providers.base -> acp.types -> acp/__init__ -> acp.runtime ->
    session_pid.  That cycle was fatal, not cosmetic: importing ``session_pid``
    first raised ``ImportError`` on ``_track_pid``.  It is why sibling
    leaves carry ``LLMProvider = Any`` runtime stubs and why ``session_pid``
    reaches acp.client through function-local imports (``_kill_pid_tree``,
    ``_kill_orphan_work_tree``).  ``test_agent_lifecycle_cycle.py``
    pins the absence; keep this leaf ignorant of the agent layer.

    """
    # ACP provider: long-lived process via client._pid
    client = getattr(provider, "_client", None)
    # The provider is being abandoned: nothing will call its client's shutdown, so
    # the registrations that client took on its settings seed are handed back by
    # this funnel -- but only once the gates below have said what stands behind the
    # pid. A seed is written before the spawn publishes a pid and the claim belongs
    # to the client OBJECT, so with no pid at all there is no runtime to protect and
    # the claim is handed back at once. With a pid, the hand-back follows the gate's
    # verdict: released when the teardown is authorized (before the first signal, so
    # it lands ahead of the grace), when the recorded root is known to be gone or
    # recycled, and otherwise KEPT -- while a lease or tenancy still holds the
    # runtime, and while the root's identity cannot be read -- because a successor
    # adopting the seed would re-write ``permissions.defaultMode`` under a runtime
    # that may still be live. Duck-typed like every other client read in this leaf.
    pid = getattr(client, "_pid", None) if client else None
    # Whether the pid is a RECORDED number (staleness-prone, so identity-gated
    # below) or one read from a live handle this process owns.
    pid_from_client = pid is not None
    # CC provider: long-lived process via _proc.pid or ephemeral via _active_proc.pid
    if pid is None:
        proc = getattr(provider, "_proc", None)
        if proc is not None and proc.returncode is None:
            pid = proc.pid
    if pid is None:
        proc = getattr(provider, "_active_proc", None)
        if proc is not None and proc.returncode is None:
            pid = proc.pid
    if pid is None:
        _release_client_seed_registrations(client)
        return
    # Only ever signal a real, positive, non-init PID. Test stand-ins are the
    # sharp edge: a Mock attribute passes the None check and coerces to 1 via
    # __index__, so an unguarded os.kill would SIGTERM init / the container
    # entrypoint (observed as a CI sandbox dying with exit 143). pid <= 1 also
    # excludes the kill(0)/kill(-n) process-group semantics outright.
    if not isinstance(pid, int) or pid <= 1:
        logger.debug("_sync_kill_provider: refusing to signal invalid pid %r", pid)
        # No process stands behind an invalid pid, so the seed claim is handed back
        # exactly as for no pid.
        _release_client_seed_registrations(client)
        return
    # Asked ONCE, here, before any signal. Everything below is one careful
    # escalation -- a group resolved while the leader was alive, a SIGTERM grace,
    # a recorded descendant sweep -- and asking per signal would both write the
    # same shot three times and leave a window where the answer changed
    # mid-escalation.
    #
    # This is the ONE gate every hard kill of a provider passes: all three
    # ``_dispatch_hard_kill`` implementations (the facade's static seam, the
    # allocator's, and the warm pool's) resolve their killer through
    # ``get_sync_kill_provider()``, and the dashboard's reset-all fallback calls
    # this function directly.
    #
    # A caller legitimately ending this runtime releases its lease first. The
    # owning provider does so as the first statement of its shutdown's owning
    # branch -- a plain statement, not a ``finally``, so a shutdown cancelled
    # BEFORE it has no release and this gate would refuse the cleanup that
    # follows. The two paths where that is reachable release explicitly:
    # ``session_allocation``'s handler that spans registration, and the
    # dashboard's reset-all timeout arm.
    #
    # At ``cap=1`` a refusal therefore does NOT mean a second tenant exists --
    # one runtime has one owning session. It means a lease outlived the session
    # that took it, so read a ``REFUSED`` line as a release site that did not
    # run, not as a near miss the gate handled.
    if not authorize_runtime_kill(
        pid,
        reason="leaked provider teardown",
        caller="session_pid._sync_kill_provider",
    ):
        # The runtime is still leased or in use, so its settings seed stays
        # claimed: a successor must not re-seed the permission file under a
        # process another party is still mid-flight on. The claim is handed back
        # only by a later teardown of this runtime, or when this process exits
        # and the persisted holder reads as stale.
        logger.debug(
            "_sync_kill_provider: keeping the settings-seed claim of pid %d; the "
            "runtime is still leased or in use",
            pid,
        )
        return
    # The verdict above is a statement about the past, and this function is where
    # that matters most: it runs on an EXECUTOR thread while a session-sharing
    # subagent takes its turn's tenancy on the event loop, and between here and the
    # first signal sit a start-id read, a group resolution and an unbounded
    # descendant walk. A claim landing in that window is invisible to the verdict,
    # so the epoch is captured here and re-checked immediately before every signal
    # below -- the only place the check is worth anything, since narrowing the gap
    # is the entire point.
    tenancy_token = tenancy_epoch(pid)
    # Deny-by-default on the ROOT's own identity -- but only where the pid can go
    # stale. ``_client._pid`` is a RECORDED number that outlives a failed start, so
    # it can name a process the OS has since handed to someone else; and for a group
    # leader that pid IS the pgid, so an unverified ``killpg`` takes a stranger's
    # whole process tree. The pid-scoped fallback is the same hazard one process
    # wide, so an unproven root is not signalled AT ALL rather than signalled
    # narrowly. Recorded descendants are still swept: each carries its own
    # spawn-time identity and is verified against it, which is exactly the check
    # the root was missing.
    #
    # The ``_proc`` / ``_active_proc`` pids are NOT gated: they come from a live
    # handle this process owns whose ``returncode`` is None, so the child is
    # unreaped and its pid cannot have been recycled -- the handle is already
    # better evidence than a recorded id would be, and there is no recorded id
    # for that shape to compare against.
    #
    # Same source on both sides or the comparison is meaningless: the ACP layer
    # records ``_start_time`` with ``platform_compat.get_process_start_id`` (both
    # its client and its runtime do), which is what this reads back. It is also
    # what makes this work on Windows, where there are no process groups but a
    # recycled pid would still send ``taskkill /T`` down a foreign tree.
    root_verified = True
    recorded_start: str | None = None
    # Set once the seed claim has been handed back on this path, so the authorized
    # arms below do not hand it back a second time.
    seed_claim_handed_back = False
    if pid_from_client:
        recorded_start = getattr(client, "_start_time", None)
        live_start = platform_compat.get_process_start_id(pid)
        root_verified = (
            isinstance(recorded_start, str)
            and live_start is not None
            and live_start == recorded_start
        )
        if not root_verified:
            logger.warning(
                "_sync_kill_provider: NOT signalling root pid %d -- identity unproven "
                "(recorded=%r, live=%r); sweeping only descendants recorded at spawn",
                pid,
                recorded_start,
                live_start,
            )
            # The seed claim follows what the identity read established. A root
            # that is gone or recycled has none of our processes behind its pid,
            # so the claim is handed back here (the Windows
            # arm below returns without signalling, and the POSIX arm may still
            # sweep recorded descendants). An identity that could not be read is
            # kept: it cannot tell our live runtime from a stranger's.
            if _root_identity_refuted(pid, recorded_start, live_start):
                _release_client_seed_registrations(client)
                seed_claim_handed_back = True
            else:
                logger.debug(
                    "_sync_kill_provider: keeping the settings-seed claim of pid %d; the "
                    "root's identity could not be read",
                    pid,
                )
    # On Windows there is no SIGTERM/SIGKILL distinction (taskkill /F is a hard
    # kill) and no os.waitpid for non-child PIDs, so a single kill suffices.
    if platform_compat.IS_WINDOWS:
        if not root_verified:
            # Nothing further to do here: the descendant sweep is a POSIX-only
            # arm (no process groups to escape on Windows, and ``taskkill /T``
            # is what normally covers the tree), so refusing the root refuses
            # the whole kill rather than narrowing it.
            return
        # Every Windows shape uses exact-tree cleanup and the same capacity
        # admission. Refusal preserves the whole tree; no root-only fallback.
        #
        # Committed HERE, before the one hard kill this platform sends: taskkill /F
        # has no grace to re-check inside, so the barrier is the only thing standing
        # between the verdict and a claim taken while the tree comes down.
        if not _commit_teardown(pid, tenancy_token):
            return
        # Authorized and committed: the runtime is coming down, so its seed claim
        # is handed back now, ahead of the kill, and the replacement spawn on the
        # same work dir finds the slot free when it seeds.
        if not seed_claim_handed_back:
            _release_client_seed_registrations(client)
            seed_claim_handed_back = True
        try:
            if pid_from_client:
                # PINNED: the query handle that verified this identity is held
                # open across taskkill, and Windows reserves a pid while any
                # handle to the process object exists -- so the pid still means
                # the same process when taskkill resolves it. Reading the start
                # id and then calling the plain variant releases that handle
                # first, which is the window taskkill /T would tear a stranger's
                # whole tree down through. False means identity unconfirmed, and
                # is a refusal to reap rather than a failure to report.
                assert recorded_start is not None  # implied by root_verified
                if not platform_compat.kill_process_tree_pinned(
                    pid, recorded_start, platform_compat.SIGKILL
                ):
                    logger.warning(
                        "_sync_kill_provider: NOT killing tree of pid %d -- Windows "
                        "identity could not be pinned across the terminate",
                        pid,
                    )
                    return
            else:
                # An owned Popen still participates in the same cleanup budget.
                # Its handle pins the source while the identity is read; an
                # unreadable identity is not permission for a numeric fallback.
                start = platform_compat.get_process_start_id(pid)
                if start is None or not platform_compat.kill_process_tree_pinned(
                    pid, start, platform_compat.SIGKILL
                ):
                    return
        except (ProcessLookupError, PermissionError, OSError) as exc:
            logger.warning(
                "_sync_kill_provider: Windows tree cleanup incomplete for PID %d (%s)",
                pid,
                exc,
            )
            return
        finally:
            # Every exit above returns, so the barrier is dropped here or not at
            # all: a pid left committed is one no tenant can ever claim again.
            release_runtime_teardown(pid)
        logger.warning("_sync_kill_provider: killed PID %d for leaked provider", pid)
        return
    # Resolved once, while the root is alive. The root's zombie is then held
    # unreaped until every group signal has been sent (see below), so this id
    # keeps naming OUR group for the whole escalation. Read BEFORE the descendant
    # scan: that scan is unbounded in the width of the tree, and the watcher can
    # reap the leader while it runs, which would cost a verified root its group.
    pgid = _isolated_provider_group(pid) if root_verified else None
    records = _provider_descendant_records(
        provider,
        pid,
        include_live_walk=root_verified,
        recorded_start=recorded_start,
        gated=pid_from_client,
    )
    if pgid is None and pid_from_client:
        # No group id from the root. Usually that root is a RECYCLED pid, and
        # reading a group off it would name whatever group holds that pid now. But
        # it is also the shape a REAPED leader presents -- no /proc entry to verify
        # against -- and that root's group is still full of our running members.
        # Telling the two apart needs evidence the pid cannot give, so the group is
        # derived from a spawn-recorded descendant still in it; failing that this
        # stays None and nothing is signalled, as before.
        #
        # Only for the RECORDED-pid shape. A `_proc`/`_active_proc` root came from a
        # live handle whose returncode was None, so it was alive moments ago and
        # `_isolated_provider_group` already had its answer -- there is no reaped
        # leader to recover. That shape also passes `gated=False`, which makes every
        # `_root_identity_holds` check pass on trust, including the ones bracketing
        # the live descendant walk: a pid recycled to a foreign root would have that
        # root's children walked into `records` and verified against identities read
        # from the same stranger, so a witness drawn from them proves nothing. The
        # recorded shape cannot reach that state -- an unverified root turns the walk
        # off (`include_live_walk=root_verified`), leaving only the spawn snapshot.
        pgid = _group_from_witnessed_descendant(pid, recorded_start, records)
    # Committed ONCE here, immediately before the first signal, and held for the
    # whole escalation. A per-round re-read cannot serve: by the second round the
    # SIGTERM has already been delivered and its grace -- seconds, by this
    # function's own comment ample time for a shared turn to start -- has passed,
    # so abandoning the SIGKILL leaves the turn that claimed in the grace dying
    # from a signal no table can recall. Closing the pid to new tenants is the only
    # answer that holds for the duration of a kill that cannot be taken back.
    if not _commit_teardown(pid, tenancy_token):
        return
    # Authorized and committed: the runtime is coming down, so its seed claim is
    # handed back here, before the first signal. Ahead of the grace on purpose --
    # the replacement spawn on the same work dir runs beside this teardown and
    # reads the live slot when it seeds; a hand-back that waited out the grace
    # would land after that read and the replacement would decline its own seed
    # as a live sibling's. Verified roots only: an unverified root is never
    # signalled below (only its recorded descendants are swept), so a root whose
    # identity could not be read may still be running and keeps its claim.
    if root_verified and not seed_claim_handed_back:
        _release_client_seed_registrations(client)
        seed_claim_handed_back = True
    try:
        for sig in (platform_compat.SIGTERM, platform_compat.SIGKILL):
            # killpg is authorized by GROUP OWNERSHIP, not by the root still being
            # alive. `_pgroup_still_ours` proves an identity-verified member of our
            # tree owns this pgid -- the root if it is still there, otherwise a
            # spawn-recorded descendant -- and that is exactly the property killpg
            # needs. Demanding the root's identity on top of it suppresses the
            # escalation in the one case it matters: asyncio's watcher collects the
            # leader zombie during the grace, so the SIGKILL round is skipped, and a
            # descendant that forked into the group AFTER the snapshot is reached by
            # neither the group signal nor the recorded sweep. It survives the
            # teardown, which is the leak this whole change exists to stop.
            #
            # The pid-scoped fallback below is different: it names the root itself,
            # so it keeps the root identity check.
            group_ok = pgid is not None and _pgroup_still_ours(
                pgid, pid, recorded_start, records, gated=pid_from_client
            )
            root_ok = root_verified and _root_identity_holds(
                pid, recorded_start, gated=pid_from_client
            )
            if group_ok or root_ok:
                try:
                    if group_ok:
                        os.killpg(pgid, sig)  # type: ignore[arg-type]
                    elif pgid is None:
                        platform_compat.kill_pid(pid, sig)
                    else:
                        logger.warning(
                            "_sync_kill_provider: pgid %d holds no verified member "
                            "of pid %d's tree; signalling recorded descendants only",
                            pgid,
                            pid,
                        )
                except ProcessLookupError:
                    # The root (or its whole group) is gone. Descendants that escaped
                    # it can still be alive, so sweep before deciding this teardown
                    # is done.
                    pass
                except OSError:
                    pass
            _signal_provider_descendants(records, sig)
            if sig == platform_compat.SIGTERM:
                deadline = time.monotonic() + _PROVIDER_TERM_GRACE_SECONDS
                while True:
                    # Deliberately NOT reaping here. A zombie owns its pid, and for a
                    # group leader that pid IS the pgid, so reaping the root mid-grace
                    # frees the number while the SIGKILL escalation below still aims
                    # at it -- a pid recycled into a new group leader would take that
                    # SIGKILL. The root's exit is read from its zombie state instead,
                    # and it is reaped only once no further group signal can be sent.
                    if _provider_tree_gone(pid, pgid, records):
                        _reap_provider_root(pid, recorded_start, gated=pid_from_client)
                        logger.warning(
                            "_sync_kill_provider: killed PID %d for leaked provider "
                            "(SIGTERM, scope=%s, descendants=%d)",
                            pid,
                            "pgid" if pgid is not None else "pid",
                            len(records),
                        )
                        return
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(_PROVIDER_TERM_POLL_SECONDS)
        _reap_provider_root(pid, recorded_start, gated=pid_from_client)
        logger.warning(
            "_sync_kill_provider: killed PID %d for leaked provider "
            "(SIGKILL, scope=%s, descendants=%d)",
            pid,
            "pgid" if pgid is not None else "pid",
            len(records),
        )
    finally:
        # The grace's early return is inside this block, so the barrier is dropped
        # here for every path out of the escalation. Left standing it would refuse
        # this pid's tenancies for the life of the gateway -- the leak that made a
        # reservation released at each early exit the wrong shape.
        release_runtime_teardown(pid)


def _tracked_child_has_runtime_identity(child_pid: int) -> bool:
    """Positive argv identity for the tracked sweep's systemd kill arm.

    True only when the live process looks like something Kiro Crew tracks in
    ``kiro_pids.txt``: a managed agent runtime (:data:`_MANAGED_AGENT_MARKERS`),
    an MCP entrypoint (:func:`_is_orphan_mcp`), or a fingerprint-less MCP
    launcher shape (:func:`_is_marked_mcp_launcher` -- callers pair this arm
    with the environ marker). FAIL-CLOSED: unreadable argv is inconclusive and
    returns ``False``, so the sweep prunes without killing. This keeps an
    intentional survivor (a detached process that merely inherited the
    tree-wide ``KIROCREW_SPAWNED`` marker, e.g. a preview server) out of the
    systemd arm's kill authority even if a tracking entry names its PID.
    """
    if _is_managed_agent_process(child_pid):
        return True
    cmdline = _pid_cmdline(child_pid)
    if not cmdline:
        return False
    return _is_orphan_mcp(cmdline) or _is_marked_mcp_launcher(cmdline)


def _commit_teardown(pid: int, token: int) -> bool:
    """Close *pid* to new tenants and answer whether the signal may be delivered.

    The gate's verdict is computed before a start-id read, a group resolution and
    an unbounded descendant walk, on a thread that does not own the tenancy table.
    This is where that verdict is made current AND the window is shut: re-reading
    before each signal, which this replaces, could only abandon the LATER SIGKILL,
    because the SIGTERM was already out and its grace is by design long enough for a
    shared turn to begin. A claim arriving in that grace would have been granted a
    defence against a signal already delivered.

    True obliges the caller to call :func:`release_runtime_teardown` on every exit
    path; a barrier left standing refuses that pid's tenancies for the life of the
    gateway.

    Logged at WARNING when it declines, because an abandoned kill leaves a process
    alive that a drain decided to end: the next drain revisits it, and until then
    this line is the only record of why the teardown did not happen.
    """
    if commit_runtime_teardown(pid, token):
        return True
    logger.warning(
        "_sync_kill_provider: ABANDONING the authorized kill of pid %d -- a tenant "
        "claimed this process after the gate allowed it, so the signal would land "
        "on a live turn; the next drain revisits it",
        pid,
    )
    return False
