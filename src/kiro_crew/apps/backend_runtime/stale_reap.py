"""The startup stale-reap of backends a prior gateway generation left running.

Runs at gateway startup, BEFORE the new generation spawns. A recorded leader is
terminated only while its live start time POSITIVELY matches the recorded one; a dead
leader's group is reaped through members vouched by the spawn's instance token; and a
pidfile row, the orphan's only handle, is dropped only when a census taken at decision
time proves the group gone.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.apps.backend_runtime import _FACADE, _facade
from kiro_crew.apps.backend_runtime.pidfile import (
    _pidfile_lock,
    _proc_start_time,
    _read_pidfile,
    _write_pidfile,
)
from kiro_crew.apps.backend_runtime.termination import _REAP_POLL_INTERVAL, _REAP_SIGTERM_GRACE
from kiro_crew.sel import sel
from kiro_crew.session_pid import group_vouching_available, signal_orphaned_spawn_group

logger = logging.getLogger(_FACADE)


def _reap_orphaned_backend_group(
    app_name: str, pid: int, entry: dict[str, Any]
) -> tuple[dict[int, str | None], dict[int, str | None], bool]:
    """SIGTERM the members a DEAD backend leader left behind in its group.

    The gap this closes: the leader is the only thing the pidfile names, and the
    live-leader branch reaches its whole tree because ``kill_process_tree``
    resolves the group through ``getpgid(pid)``. Once the leader has exited there
    is no pid to resolve the group from -- ``getpgid`` raises -- yet the group
    itself outlives it and its members keep the app's PORT bound. Dropping the
    row there leaks exactly the orphan that makes the next generation's spawn
    collide and serve 502s.

    The group number is recoverable from the contract rather than from the dead
    pid: a backend is spawned with ``start_new_session=True``, so its pgid IS its
    leader's pid. What is NOT safe is signalling that number -- the kernel may
    have reissued it to an unrelated session leader, and ``killpg`` would take a
    stranger's tree. So this hands the number to
    :func:`session_pid.signal_orphaned_spawn_group`, which lists the group's live
    members, keeps only those whose ``/proc/<pid>/environ`` carries THIS spawn's
    instance token, and signals each of them pinned to its own pid + start
    instant. No vouching member means no signal.

    Returns ``(vouched, signalled, keep_row)``.

    *vouched* is every live member this census FOUND, and it answers exactly one
    question: is there a group here worth escalating at all. It is NOT the basis for
    retention -- it is a snapshot taken BEFORE the SIGTERM, so it cannot contain a
    member the SIGTERM itself caused to be forked, and the caller re-reads the group
    at decision time instead. *signalled* is the subset a signal actually reached,
    and is ONLY the escalation's target set: a member that never took a SIGTERM owes
    no grace and no SIGKILL. Keeping the two apart is the whole point -- a signal can
    fail on one member and land on another (``pidfd_open`` answering EMFILE, or
    EPERM), so the signalled set is an INCOMPLETE census and anything that treats it
    as the membership will call a group gone while a live member holds the port.

    *keep_row* is consulted only when *vouched* is empty, and answers whether the
    pidfile row -- this orphan's only handle -- should survive a start that took no
    census at all: a scan that raised tells us nothing, so the row stays for a later
    attempt, while the two declines detected BEFORE any signal (no instance token,
    a host that cannot read the vouch) are permanent on this host and drop it
    rather than growing the pidfile forever. When *vouched* is non-empty the caller
    decides retention from that census's liveness instead. Never raises: a failed
    reap must not abort the rest of the startup sweep.
    """
    instance = entry.get("spawn_instance")
    if not isinstance(instance, str) or not instance:
        # Written by a build that did not stamp the token (or hand-edited). There
        # is nothing to vouch the group with, and a signal aimed at the bare
        # number could hit a recycled leader's tree, so decline. The row goes: a
        # token is never added to an existing row, so every later start would
        # decline identically. Self-healing forward -- the next spawn records one.
        logger.info(
            "Not reaping %s's orphaned group (pid %d): no spawn instance recorded", app_name, pid
        )
        return {}, {}, False
    if not group_vouching_available():
        # The vouch reads /proc/<pid>/environ, which exists on Linux alone. Say so
        # rather than reporting a reap that did not happen. Nothing else picks
        # these up: the periodic orphan sweep's positive-identity paths are an
        # agent runtime, an MCP entrypoint, a gatewayd, a browser daemon and a
        # TEST-RUNNER argv, and an app backend's worker is none of those -- so
        # these survivors are neither reaped NOR reported anywhere, and this log
        # line is the only record they exist. A leak we can name beats a signal to
        # a stranger; the operator's recourse is to kill the process holding the
        # port by hand. The row goes: the platform is the same on the next start.
        logger.info(
            "Cannot vouch %s's orphaned group (pid %d) on this platform; its members are "
            "left running and are not covered by the periodic orphan sweep",
            app_name,
            pid,
        )
        return {}, {}, False
    try:
        vouched, signalled = signal_orphaned_spawn_group(pid, platform_compat.SIGTERM, instance)
    except Exception as exc:  # noqa: BLE001 — one app's reap must not end the sweep
        # A /proc scan that raised mid-listing: no census, so KEEP the row and let
        # a later start look again.
        logger.warning("Orphaned-group reap of %s (pid %d) failed: %s", app_name, pid, exc)
        return {}, {}, True
    if vouched:
        logger.info(
            "Startup stale-reap: SIGTERM %d of %s's %d orphaned group member(s) (group %d)",
            len(signalled),
            app_name,
            len(vouched),
            pid,
        )
    # An EMPTY census is not evidence of an empty group. Every read the vouch makes
    # is fail-OPEN: ``_marked_group_members`` swallows OSError on the /proc scan and
    # on each stat, and ``_env_spawn_instance`` returns None when it cannot read a
    # member's environ -- so under fd exhaustion (EMFILE/ENFILE) every member is
    # silently dropped and the census comes back empty WITHOUT raising, which means
    # the caller's ``except`` above never sees it. Keying the drop on the census
    # would then discard the orphan's only handle at precisely the moment the host
    # is under pressure. So absence has to be confirmed POSITIVELY, by a probe that
    # cannot fail open: ``pgroup_exists`` is ``killpg(pgid, 0)``, which answers False
    # only on ESRCH and reads an unsignalable group as alive. A group that still
    # exists keeps its row even when the census could not name anything in it.
    return vouched, signalled, platform_compat.pgroup_exists(pid)


def _reap_stale_app_backends() -> int:
    """Reap app backends left running by a prior gateway generation.

    Runs at gateway startup BEFORE the new generation spawns (off the event loop
    — see start_enabled_app_backends' caller). A recorded pid is terminated only
    when it is still alive AND its current start_time POSITIVELY matches the
    recorded one (PID-reuse guard); if identity cannot be confirmed the pid is
    left alone — declining to reap leaks a recoverable orphan, whereas killing an
    unverifiable pid could signal an unrelated recycled process group. Returns
    the count terminated.

    A leader that is already DEAD is not the end of the story: its process group
    outlives it and its members can still hold the app's port, so that branch
    hands the group to :func:`_reap_orphaned_backend_group` instead of merely
    dropping the row. The row is that orphan's ONLY handle, so retention follows a
    census taken at DECISION time rather than the signals or the opening snapshot:
    the row is dropped once that final reading finds no live member, kept while it
    finds any (one no signal could reach, or one forked after the first census), and
    a declined reap drops it only when no later start could do better -- no instance
    token, or a host that cannot read the vouch. That retention is bounded by the
    spawn path: a successful respawn of the same app re-records the row, so the
    handle survives to a later start only while the app stays down. Group members
    are reported separately from leaders in the log and are not counted in the
    return value, which stays "leaders terminated" for the callers that read it.
    """
    with _pidfile_lock:
        data = _read_pidfile()
    if not data:
        return 0
    # ``handled`` = entries we either terminated or confirmed already-gone; they
    # are removed from the pidfile at the end. Entries left out of ``handled``
    # (identity unconfirmed but still alive) are KEPT for a later attempt so a
    # transient ps failure does not permanently abandon a real orphan.
    # ``handled`` maps each handled app_name -> the exact pidfile entry we acted
    # on. The final merge drops an entry ONLY if it is still identical: a
    # concurrent enable that re-recorded the app with a NEW pid mid-scan writes a
    # different entry, which must survive (clobbering it would re-introduce the
    # orphan leak this feature prevents).
    handled: dict[str, Any] = {}
    reaped: list[tuple[str, int, Any]] = []
    # Groups whose opening census found live members, as ``(app, pgid, instance,
    # signalled, entry)``. Only the SIGNALLED set is carried: it is the escalation's
    # target set, and it is the one thing a later pass cannot re-derive. The opening
    # census is deliberately NOT carried -- retention re-reads the group at decision
    # time, because a snapshot taken before the SIGTERM cannot contain a member the
    # SIGTERM caused to be forked. The entry rides along because the final merge
    # drops a row only when it still equals the exact entry we acted on.
    # Escalated in the same second pass as the leaders, for the same reason: the
    # grace window is seconds long and must not be paid serially inside the scan.
    group_reaped: list[tuple[str, int, str, dict[int, str | None], dict[str, Any]]] = []
    for app_name, entry in data.items():
        try:
            pid = int(entry.get("pid", 0))
        except (TypeError, ValueError):
            handled[app_name] = entry  # malformed entry — drop
            continue
        if pid <= 0:
            handled[app_name] = entry
            continue
        if platform_compat.windows_tree_cleanup_pending(pid, entry.get("start_time")):
            # Maintenance owns the pins and mandatory metadata retirement. Even
            # a dead root cannot retire an unresolved descendant tree's record.
            continue
        # NEVER raw ``os.kill(pid, 0)`` — that TERMINATES the process on Windows.
        # ``pid_liveness`` returns DEAD/ALIVE/UNSIGNALABLE (uid-owned-by-other on
        # POSIX; unknown errno also maps to UNSIGNALABLE). Preserve the original
        # three-way policy: drop-dead, skip-unsignalable, proceed-alive.
        liveness = platform_compat.pid_liveness(pid)
        if liveness == platform_compat.PID_DEAD:
            # The leader is gone, but its GROUP may not be: the leader was a
            # session leader, so the group survives it holding the app's port.
            # This is the leak that made the next spawn collide; see
            # _reap_orphaned_backend_group.
            #
            # The row is this orphan's ONLY handle, so it is NOT dropped up front:
            # a signalled group's retention is decided after the escalation, by
            # whether its members actually died, and an outcome a later start
            # could do better on keeps the row for that retry. Only an outcome
            # nothing can improve on drops it.
            group_vouched, group_signalled, keep_row = _reap_orphaned_backend_group(
                app_name, pid, entry
            )
            if group_vouched:
                # A non-empty census proves the row carried a usable instance —
                # the helper returns {} otherwise — so this read cannot be None.
                group_reaped.append(
                    (app_name, pid, str(entry["spawn_instance"]), group_signalled, entry)
                )
            elif not keep_row:
                handled[app_name] = entry
            continue
        if liveness == platform_compat.PID_UNSIGNALABLE:
            handled[app_name] = entry
            logger.info("Skipping stale-reap of %s pid %d: not owned by gateway", app_name, pid)
            continue
        recorded_st = entry.get("start_time")
        live_st = _proc_start_time(pid)
        if not recorded_st or live_st is None or live_st != recorded_st:
            # Identity unconfirmed: no baseline captured, ps failed now, or the
            # pid was recycled. Do NOT kill, and KEEP the entry (omit from
            # ``handled``) so a future start can retry once ps recovers.
            logger.info(
                "Skipping stale-reap of %s pid %d: start_time unconfirmed (recycled or unreadable)",
                app_name,
                pid,
            )
            continue
        try:
            # Identity-PINNED: on Windows the handle that re-verifies the start
            # time stays open across the terminate, so the pid taskkill resolves
            # cannot have been recycled between the check above and the signal.
            # False means the identity could not be pinned -- keep the entry
            # (omit from ``handled``) and retry on a later start, exactly as the
            # unconfirmed-start_time branch above does. POSIX delegates straight
            # through and is unchanged.
            signalled = platform_compat.kill_process_tree_pinned(
                pid,
                recorded_st,
                platform_compat.SIGTERM,
                **({"app_tracking": True} if platform_compat.IS_WINDOWS else {}),
            )
        except platform_compat.WindowsCleanupCapacityError:
            logger.warning(
                "Windows cleanup capacity refused stale backend %s; keeping tracking", app_name
            )
            continue
        except (ProcessLookupError, OSError):
            if platform_compat.IS_WINDOWS:
                # A failed exact-handle drain is not proof of absence.
                continue
            handled[app_name] = entry  # gone between the probe and the signal
            continue
        if not signalled:
            logger.info(
                "Skipping stale-reap of %s pid %d: identity could not be pinned for the kill",
                app_name,
                pid,
            )
            continue
        handled[app_name] = entry
        # Carry recorded_st so the delayed SIGKILL can re-confirm identity before
        # signalling (PID-reuse guard, below).
        reaped.append((app_name, pid, recorded_st))
        try:
            sel().log_api_access(
                caller="gateway",
                operation="app_backend_stale_reap",
                outcome="sigterm",
                resources=f"{app_name} pid={pid}",
            )
        except Exception as exc:
            logger.debug("SEL audit failed for app_backend_stale_reap %s: %s", app_name, exc)
    # Escalate to SIGKILL for any matched orphan that ignored SIGTERM. Each pid
    # gets its OWN grace window — a shared deadline would let the first slow
    # exiter consume the whole budget and SIGKILL the rest instantly. No lock is
    # held here: the kill/poll touches no shared file and can sleep for seconds.
    for app_name, pid, recorded_st in reaped:
        deadline = time.monotonic() + _REAP_SIGTERM_GRACE
        while _facade()._pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(_REAP_POLL_INTERVAL)
        if not _facade()._pid_alive(pid):
            continue
        # Re-confirm identity before the destructive SIGKILL. The pid may have
        # exited and been recycled to an unrelated process during the grace
        # window (macOS's ~99998 PID space makes reuse materially likely within
        # _REAP_SIGTERM_GRACE); without this, os.killpg below could signal an
        # innocent recycled process group. Same PID-reuse guard the SIGTERM path
        # applies — skip the kill on mismatch (leak-not-mis-kill).
        if _proc_start_time(pid) != recorded_st:
            logger.info(
                "Skipping stale-reap SIGKILL of %s pid %d: start_time changed (PID recycled)",
                app_name,
                pid,
            )
            continue
        try:
            # Same pinning as the SIGTERM path, and it matters more here: this is
            # the destructive escalation, and the grace window above is exactly
            # the interval in which the pid can be recycled.
            if platform_compat.IS_WINDOWS:
                # The first exact-handle call already drained the whole tree.
                # Never re-open a numeric PID for a Windows escalation.
                continue
            if not platform_compat.kill_process_tree_pinned(
                pid, recorded_st, platform_compat.SIGKILL
            ):
                logger.info(
                    "Skipping stale-reap SIGKILL of %s pid %d: identity could not be pinned",
                    app_name,
                    pid,
                )
                continue
        except (ProcessLookupError, OSError):
            continue
        try:
            sel().log_api_access(
                caller="gateway",
                operation="app_backend_stale_reap",
                outcome="sigkill",
                resources=f"{app_name} pid={pid}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "SEL audit failed for app_backend_stale_reap sigkill %s: %s", app_name, exc
            )
    # Escalate the orphaned GROUP members that ignored SIGTERM. Same per-group
    # grace as the leaders above, and the same re-verification discipline: the
    # escalation passes back the exact members the first pass vouched, so
    # signal_orphaned_spawn_group signals only those still alive under the SAME
    # start id. A member seen for the first time now is not escalated — it owes no
    # grace, and it is what a fresh occupant of the recycled group number would
    # look like.
    for app_name, pgid, instance, group_signalled, entry in group_reaped:
        # The grace is owed to the members that actually TOOK a SIGTERM, so it is
        # waited out over ``group_signalled``; a member the signal never reached has
        # nothing to respond to, and waiting on it would just spend the window.
        deadline = time.monotonic() + _REAP_SIGTERM_GRACE
        while any(_facade()._pid_alive(m) for m in group_signalled) and time.monotonic() < deadline:
            time.sleep(_REAP_POLL_INTERVAL)
        # One call does both remaining jobs, and it must be made even when nothing
        # is left to kill: it re-censuses the group, and THAT fresh reading -- not
        # the pre-SIGTERM snapshot -- is what retention is allowed to trust. A
        # backend whose SIGTERM handler forks a replacement into the same session
        # group (a supervisor/worker server does) produces a live member that the first
        # census could not have seen, and deciding on the snapshot would drop the
        # row while that replacement holds the port, with nothing left naming it.
        # ``expected`` still restricts the SIGNAL to the members the first pass
        # vouched, so the newcomer is observed but never signalled -- it owes no
        # grace, and it is indistinguishable from a fresh occupant of a recycled
        # group number. The next start reaps it with a census of its own.
        try:
            final_vouched, killed = signal_orphaned_spawn_group(
                pgid, platform_compat.SIGKILL, instance, expected=group_signalled
            )
        except Exception as exc:  # noqa: BLE001 — one app's reap must not end the sweep
            # No final reading, so nothing may be concluded: KEEP the row.
            logger.warning(
                "Orphaned-group SIGKILL of %s (group %d) failed: %s", app_name, pgid, exc
            )
            continue
        if killed:
            logger.info(
                "Startup stale-reap: SIGKILL %d orphaned member(s) of %s's group %d",
                len(killed),
                app_name,
                pgid,
            )
        # Retention reads the FINAL census, never the subset a signal reached and
        # never the opening snapshot. A member the signal could not reach
        # (``pidfd_open`` answering EMFILE, or EPERM) and a member forked after the
        # first census are both alive and both still holding the port.
        alive = [m for m in final_vouched if _facade()._pid_alive(m)]
        # The final reading is fail-open in the same way the opening one is, so an
        # empty ``alive`` is only half the question. The row is dropped only once the
        # GROUP is positively gone -- ``pgroup_exists`` answers False on ESRCH alone,
        # so an unreadable or unsignalable group keeps its handle. The cost of being
        # wrong this way is one retained pidfile row that the app's next successful
        # spawn replaces; the cost of being wrong the other way is a port held
        # forever by a process nothing names.
        if not alive and not platform_compat.pgroup_exists(pgid):
            handled[app_name] = entry
            continue
        # Row deliberately KEPT (omitted from ``handled``) so a later start reaps
        # this group again. The retention is real but not unconditional: a
        # successful respawn of this app re-records the row under the same app name
        # (_record_app_pid), so the handle survives to a later start only while the
        # app does not come back up -- disabled, failing to spawn, or not restarted.
        logger.warning(
            "Orphaned group %d of %s still has %d live member(s) after the kill pass; keeping "
            "its pidfile record, though a successful respawn of this app replaces that row",
            pgid,
            app_name,
            len(alive),
        )
    # Drop only the entries we handled, re-reading under the lock so a concurrent
    # enable/disable that wrote during the scan is merged, not clobbered. Drop an
    # entry ONLY if it still equals what we handled: a mid-scan re-record (new
    # pid) yields a different entry that must be kept.
    with _pidfile_lock:
        current = _read_pidfile()
        for app_name, handled_entry in handled.items():
            if current.get(
                app_name
            ) == handled_entry and not platform_compat.windows_tree_cleanup_pending(
                handled_entry.get("pid"), handled_entry.get("start_time")
            ):
                current.pop(app_name, None)
        _write_pidfile(current)
    if reaped:
        logger.info("Startup stale-reap: terminated %d orphaned app backend(s)", len(reaped))
    if group_reaped:
        # Reported separately because the count this function RETURNS is leaders
        # terminated, and a group reap has no live leader to count. A start that
        # reaps only groups would otherwise log nothing at all, which is exactly
        # the case a port-collision investigation needs to see.
        logger.info(
            "Startup stale-reap: signalled orphaned group members for %d app backend(s) "
            "whose leader was already gone",
            len(group_reaped),
        )
    return len(reaped)
