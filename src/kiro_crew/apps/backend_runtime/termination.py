"""Stopping a backend, and draining the whole tree a spawned backend leaves.

A spawned backend leads its own process group (POSIX) or anchors its tree by its root's
creation identity (Windows), so every exit that signals one reaches the tree, root alive
or not. Nothing here signals a PID it cannot name: an adopted backend's recorded PIDs are
signalled only while their start-time identities still match, and an exited root's group
is reached only through members vouched by the spawn's instance token.
"""

from __future__ import annotations

import logging
import subprocess
import time
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.apps.backend_runtime import _FACADE, _facade
from kiro_crew.apps.backend_runtime.pidfile import (
    _forget_app_pid,
    _forget_app_pid_if,
    _proc_start_time,
)
from kiro_crew.apps.backend_runtime.ports import _allocated_ports
from kiro_crew.apps.backend_runtime.probe import _health_probe
from kiro_crew.apps.backend_runtime.tracking import (
    _LIFECYCLE_STOP,
    AppProcess,
    _advance_lifecycle_locked,
    _health_reconcile_lock,
    _lock,
    _processes,
    _restart_attempts,
)
from kiro_crew.sel import sel
from kiro_crew.session_pid import group_vouching_available, signal_orphaned_spawn_group

logger = logging.getLogger(_FACADE)


# Startup stale-reap timing (see _reap_stale_app_backends). The SIGTERM grace is
# applied PER orphan, not shared across the batch.
_REAP_SIGTERM_GRACE = 3.0  # seconds to wait for an orphan to exit after SIGTERM
_REAP_POLL_INTERVAL = 0.1  # liveness re-poll cadence during the grace window


def _signal_backend_tree(
    app_name: str, proc: subprocess.Popen, start_time: str | None, sig: int
) -> None:
    """Signal a spawned backend's whole tree, identity-pinned on Windows.

    POSIX is ``kill_process_tree`` unchanged: the backend leads its own process
    group, so ``killpg`` reaches every member whether or not the root is still
    running. Windows has no group to signal. ``taskkill /T /PID <root>`` walks the
    tree FROM the root, so once the root has exited it reaches nothing -- which is
    how a launcher that forks its real server and returns leaves that server
    running with no way to stop it. With the root's creation identity
    recorded at spawn, ``kill_process_tree_pinned`` opens the exact process object
    instead and drains the descendants it still anchors, confirming their exit.

    Falls back to the numeric ``taskkill`` -- the behaviour before the pinned path
    existed, so a stop is never weaker than it was -- when no identity was
    recorded, when the identity cannot be pinned, when cleanup capacity refuses
    the tree, or when the exact-handle drain raises (its pinned handles stay
    registered for maintenance either way). Exceptions from the fallback
    propagate exactly as ``kill_process_tree``'s do.
    """
    if platform_compat.IS_WINDOWS and start_time is not None:
        try:
            if platform_compat.kill_process_tree_pinned(proc.pid, start_time, sig):
                return
            logger.info(
                "App %s: pid %d identity could not be pinned for the tree drain; "
                "falling back to taskkill",
                app_name,
                proc.pid,
            )
        except platform_compat.WindowsCleanupCapacityError as exc:
            logger.warning(
                "App %s: Windows cleanup capacity refused the pid %d tree drain (%s); "
                "falling back to taskkill",
                app_name,
                proc.pid,
                exc,
            )
        except (ProcessLookupError, OSError) as exc:
            logger.warning(
                "App %s: exact-handle drain of pid %d did not complete (%s); "
                "falling back to taskkill",
                app_name,
                proc.pid,
                exc,
            )
    platform_compat.kill_process_tree(proc.pid, sig)


def _drain_exited_root_tree(
    app_name: str,
    proc: subprocess.Popen,
    root_start_time: str | None,
    spawn_instance: str | None,
) -> bool | None:
    """Terminate whatever a launcher that has already exited left behind. Never raises.

    The root has exited -- that is why the caller is here -- but the processes it
    forked have not necessarily: a launcher that starts the real server and returns
    0 is the shape this guards. Three callers: the startup-survival failure branch,
    where no ``AppProcess`` and no pidfile row exist yet, so nothing later would
    ever name those survivors; ``stop_app_backend`` for a tracked record whose root
    exited after startup while a child kept serving; and the health supervisor's
    restart, which drops that same record before spawning a replacement. In each
    the record is being dropped, so this is the last exit that can still reach the
    tree.

    Returns what the caller may CONCLUDE about the tree, because a stop under a
    withdrawn trust ceiling has to refuse rather than report a success it cannot
    support: ``True`` when the tree is positively gone, ``False`` when a member
    positively survives the drain, ``None`` when nothing can be concluded (no
    identity or token to vouch by, a host that cannot read the vouch, a cleanup
    capacity refusal). Absence is never inferred from an empty census.

    Windows: the exact-handle drain, keyed on the root identity the caller read
    BEFORE the survival check (an exited root's creation time cannot be probed
    afresh once its ``Popen`` handle goes). It confirms exit or raises, so its
    ``True`` is the conclusion. A capacity refusal or a drain that raises leaves
    the tree to the cleanup registry's maintenance, as the stale reaper does; a
    numeric ``taskkill`` fallback is pointless here because the root it would walk
    from is the exited one.

    POSIX: the group outlives its leader and its id is the leader's pid, but
    ``kill_process_tree`` cannot be used -- ``getpgid`` raises for a reaped pid --
    and signalling the bare group NUMBER is refused for the reason
    ``_reap_orphaned_backend_group`` gives. So it takes the same route as that
    reaper: members vouched by this spawn's instance token, each signalled pinned
    to its own identity, then a SIGKILL pass after the reap's grace that is ALSO
    the final census. That pass runs unconditionally, exactly as the reaper's does:
    the opening census cannot contain a member the SIGTERM itself caused to be
    forked (a server whose handler forks a replacement into the same group), so
    the conclusion is read off the final reading -- no live vouched member AND
    ``pgroup_exists`` False -- never off the signalled set or the opening snapshot.
    Off Linux the vouch cannot be read, nothing is signalled and the decline is
    logged -- the same trade the reaper makes.
    """
    if platform_compat.IS_WINDOWS:
        if root_start_time is None:
            logger.warning(
                "App %s: root pid %d exited and its identity was not captured; any "
                "surviving descendants are left running",
                app_name,
                proc.pid,
            )
            return None
        try:
            drained = platform_compat.kill_process_tree_pinned(
                proc.pid, root_start_time, platform_compat.SIGTERM
            )
        except platform_compat.WindowsCleanupCapacityError as exc:
            logger.warning(
                "App %s: Windows cleanup capacity refused the tree drain of exited root "
                "pid %d (%s); its survivors are left to maintenance",
                app_name,
                proc.pid,
                exc,
            )
            return None
        except (ProcessLookupError, OSError) as exc:
            logger.warning(
                "App %s: exact-handle drain of exited root pid %d did not complete (%s); "
                "retained for maintenance",
                app_name,
                proc.pid,
                exc,
            )
            return False
        if drained:
            logger.info(
                "App %s: drained the process tree of exited root pid %d", app_name, proc.pid
            )
            return True
        logger.info(
            "App %s: exited root pid %d identity could not be pinned; nothing signalled",
            app_name,
            proc.pid,
        )
        return None
    if spawn_instance is None:
        logger.info(
            "App %s: exited root pid %d carries no spawn instance to vouch its group by; "
            "any surviving descendants are left running",
            app_name,
            proc.pid,
        )
        return None
    if not group_vouching_available():
        logger.info(
            "App %s: cannot vouch exited root pid %d's group on this platform; any "
            "surviving descendants are left running",
            app_name,
            proc.pid,
        )
        return None
    try:
        vouched, signalled = signal_orphaned_spawn_group(
            proc.pid, platform_compat.SIGTERM, spawn_instance
        )
        if vouched:
            logger.info(
                "App %s: SIGTERM %d of %d surviving member(s) of exited root pid %d's group",
                app_name,
                len(signalled),
                len(vouched),
                proc.pid,
            )
        deadline = time.monotonic() + _REAP_SIGTERM_GRACE
        while any(_facade()._pid_alive(m) for m in signalled) and time.monotonic() < deadline:
            time.sleep(_REAP_POLL_INTERVAL)
        # Final census AND escalation in one reading. ``expected`` keeps the SIGKILL
        # to the members that took the SIGTERM; a member first seen now is observed
        # (it decides the conclusion) but never signalled -- it owes no grace, and it
        # is what a fresh occupant of a recycled group number would look like.
        final_vouched, killed = signal_orphaned_spawn_group(
            proc.pid, platform_compat.SIGKILL, spawn_instance, expected=signalled
        )
        if killed:
            logger.info(
                "App %s: SIGKILL %d surviving member(s) of exited root pid %d's group",
                app_name,
                len(killed),
                proc.pid,
            )
        alive = [m for m in final_vouched if _facade()._pid_alive(m)]
        if alive:
            logger.warning(
                "App %s: %d member(s) of exited root pid %d's group still alive after the "
                "drain: %s",
                app_name,
                len(alive),
                proc.pid,
                sorted(alive),
            )
            return False
        # An empty final reading is fail-open (every /proc read swallows OSError),
        # so absence is confirmed by the probe that cannot fail open.
        return not platform_compat.pgroup_exists(proc.pid)
    except Exception as exc:  # noqa: BLE001 — a failed drain must not crash the caller
        logger.warning(
            "App %s: draining exited root pid %d's group failed: %s", app_name, proc.pid, exc
        )
        return None


def _terminate_retired_spawn(app_name: str, proc: subprocess.Popen, log_fh: Any) -> None:
    """Terminate a child whose caller does not own the STARTING placeholder."""
    pid_start_time = _proc_start_time(proc.pid)
    try:
        _signal_backend_tree(app_name, proc, pid_start_time, platform_compat.SIGTERM)
    except (ProcessLookupError, OSError):
        pass
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            platform_compat.kill_process_tree(proc.pid, platform_compat.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
    _forget_app_pid_if(app_name, proc.pid, pid_start_time)
    try:
        log_fh.close()
    except OSError:
        pass


def _wait_for_pids(pids: list[int], timeout: float = 2.0) -> None:
    """Poll until all PIDs have exited or timeout is reached.

    Uses short sleeps (0.1s) to avoid blocking the thread for the full
    timeout duration when processes exit quickly.

    Uses pid_liveness (tri-state), NOT pid_exists (which collapses EPERM to
    True): an adopted app-backend PID can be recycled between
    kill_pid_pinned(SIGTERM) and this poll to a different user's process. pid_exists would keep it in
    still_alive for the whole 2.0s deadline; pid_liveness returns UNSIGNALABLE
    for the not-ours case and we treat that as done, so a recycled PID returns
    fast instead of holding the deadline. Never raw ``os.kill(pid, 0)`` — that
    TERMINATES the target on Windows.
    """
    deadline = time.monotonic() + timeout
    remaining = list(pids)
    while remaining and time.monotonic() < deadline:
        still_alive: list[int] = []
        for pid in remaining:
            if platform_compat.pid_liveness(pid) == platform_compat.PID_ALIVE:
                still_alive.append(pid)
        remaining = still_alive
        if remaining:
            time.sleep(0.1)


def stop_app_backend(
    app_name: str,
    *,
    _expected: AppProcess | None = None,
    _retry_if_serving: str | None = None,
) -> bool:
    """Stop an app's backend process.

    ``_retry_if_serving`` is a health path, and passing one asks for the stricter
    reading of ONE ambiguous case: an adopted backend none of whose recorded PIDs
    still match their adoption identity. By default that reads as "the backend
    exited and its PID was recycled", so the stop reports success. With a health
    path the port is probed, and one that still answers reports failure with
    tracking restored instead, so the caller can retry. Only a caller enforcing a
    withdrawn trust ceiling needs that, and it pays for the probe.
    """
    # Teardown participates in the health serialization, so the pop cannot land in the
    # middle of a reconcile. Without this, a watcher that had already passed its identity
    # check could still be inside `_gate_mcp_registration` when the caller's subsequent
    # `deregister_app` scrubs — and its write would land AFTER, restoring the dead url
    # this whole gate exists to keep out of mcp.json. Holding it across the pop makes the
    # two mutually exclusive: either the reconcile completes and this pop follows it (the
    # caller's scrub then wins), or this pop lands first and the reconcile's identity
    # check fails. Lock order matches `_set_backend_health`: reconcile lock, then `_lock`.
    with _health_reconcile_lock:
        with _lock:
            if _expected is not None and _processes.get(app_name) is not _expected:
                return False
            _advance_lifecycle_locked(app_name, _LIFECYCLE_STOP)
            ap = _processes.pop(app_name, None)
            _allocated_ports.pop(app_name, None)
            _restart_attempts.pop(app_name, None)
        # Keep cleanup inside the lifecycle transition's serialization. A later explicit
        # start cannot record its successor between the pop and this identity check.
        if ap is not None and ap.proc is not None:
            _forget_app_pid_if(app_name, ap.pid, ap.pid_start_time)
        else:
            _forget_app_pid(app_name)

    if not ap:
        return False

    if ap.proc and ap.proc.poll() is None:
        try:
            sel().log_api_access(
                caller="gateway",
                operation="app_backend_stop",
                outcome="sigterm",
                resources=f"{app_name} pid={ap.proc.pid}",
            )
        except Exception as exc:
            logger.debug("SEL audit failed for app_backend_stop %s: %s", app_name, exc)
        try:
            # killpg(getpgid) on POSIX; on Windows the exact-handle drain pinned to the
            # identity recorded at spawn, so descendants of an exited root are reached.
            _signal_backend_tree(app_name, ap.proc, ap.pid_start_time, platform_compat.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
        try:
            ap.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                platform_compat.kill_process_tree(ap.proc.pid, platform_compat.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            try:
                sel().log_api_access(
                    caller="gateway",
                    operation="app_backend_stop",
                    outcome="sigkill_escalation",
                    resources=f"{app_name} pid={ap.proc.pid}",
                )
            except Exception as exc:
                logger.debug("SEL audit failed for sigkill_escalation %s: %s", app_name, exc)
        if (
            _retry_if_serving is not None
            and ap.port
            and _health_probe(ap.port, _retry_if_serving).healthy
        ):
            # A DESCENDANT outlived its root, which the root's exit status cannot show.
            #
            # The signal above goes to the process GROUP, but the wait watches only
            # ``ap.proc``, so a root that exits promptly on SIGTERM skips the escalation
            # entirely. App code is free to ignore SIGTERM in a child it forked, or to
            # leave the group with ``setsid`` before binding, and either way the port
            # keeps being served while this returns True and the record is popped. For
            # an ordinary stop that is tolerable. Under a WITHDRAWN ceiling it is the
            # whole failure: un-trusted code still serving, with nothing tracked left to
            # retry against.
            #
            # NOT escalated with another signal here, deliberately. The root has been
            # reaped by the wait above, so the OS may already have reused its pid, and
            # ``kill_process_tree`` would resolve that number to whatever group owns it
            # now. The descendant is an UNKNOWN process -- the gateway recorded no
            # identity for it -- which is the same position as an adopted record with no
            # recorded PIDs, and that branch refuses for the same reason rather than
            # signalling blind.
            #
            # So this reports the refusal instead of a success it cannot support:
            # tracking is restored, the ceiling stays engaged on the next sweep, the MCP
            # entry the revocation scrubbed stays scrubbed, and the operator gets a row
            # and a warning naming the port rather than a silent claim that the app was
            # stopped.
            logger.warning(
                "App %s: something is still answering port %d after its backend was "
                "stopped; a descendant outlived the process we spawned, so the stop is "
                "reported as refused rather than successful",
                app_name,
                ap.port,
            )
            try:
                sel().log_api_access(
                    caller="gateway",
                    operation="app_backend_stop",
                    outcome="rejected_descendant_serving",
                    resources=f"{app_name} port={ap.port}",
                )
            except Exception as exc:
                logger.debug(
                    "SEL audit failed for rejected_descendant_serving %s: %s",
                    app_name,
                    exc,
                )
            with _lock:
                _processes.setdefault(app_name, ap)
                if ap.port:
                    _allocated_ports.setdefault(app_name, ap.port)
            return False
    elif ap.proc is not None:
        # The tracked ROOT has already exited, but the record is only now being
        # dropped -- a launcher that died after startup while the server it forked
        # kept serving, and a stop (disable, uninstall, ceiling revocation, gateway
        # shutdown) arriving before the health supervisor replaced the record. The
        # group signal above needs a live root to resolve from, so without this the
        # stop dropped the record and left that server running. Same drain as the
        # startup-failure branch: the identity recorded at spawn on Windows, the
        # spawn-instance vouch on POSIX, nothing aimed at a bare number.
        gone = _drain_exited_root_tree(app_name, ap.proc, ap.pid_start_time, ap.spawn_instance)
        # The same refusal the live-root branch makes above, on the same grounds and
        # under the same condition: a WITHDRAWN ceiling cannot be told the app was
        # stopped while un-trusted code may still be serving. Positive survivors
        # refuse outright; an inconclusive drain (nothing to vouch by, or a host
        # that cannot read the vouch) is settled by the same port probe the live
        # branch uses. An ordinary stop tolerates a survivor, as it does above,
        # and says so.
        if gone is not True:
            refuse = _retry_if_serving is not None and (
                gone is False or bool(ap.port and _health_probe(ap.port, _retry_if_serving).healthy)
            )
            if refuse:
                logger.warning(
                    "App %s: descendants of its exited backend root (pid %d) may still be "
                    "serving on port %d, so the stop is reported as refused rather than "
                    "successful",
                    app_name,
                    ap.proc.pid,
                    ap.port,
                )
                try:
                    sel().log_api_access(
                        caller="gateway",
                        operation="app_backend_stop",
                        outcome="rejected_descendant_serving",
                        resources=f"{app_name} port={ap.port}",
                    )
                except Exception as exc:
                    logger.debug(
                        "SEL audit failed for rejected_descendant_serving %s: %s",
                        app_name,
                        exc,
                    )
                with _lock:
                    _processes.setdefault(app_name, ap)
                    if ap.port:
                        _allocated_ports.setdefault(app_name, ap.port)
                return False
            if gone is False:
                logger.warning(
                    "App %s: a descendant outlived its exited backend root (pid %d) and "
                    "survived the drain; the stop is reported as successful, as an "
                    "ordinary stop tolerates",
                    app_name,
                    ap.proc.pid,
                )
    elif not ap.proc and ap.port:
        # Adopted process (proc=None) — kill only PIDs we recorded at adoption
        if not ap.adopted_pids:
            logger.warning(
                "Cannot stop adopted backend for %s on port %s: no recorded PIDs — "
                "refusing to kill unknown processes",
                app_name,
                ap.port,
            )
            try:
                sel().log_api_access(
                    caller="gateway",
                    operation="app_backend_stop_adopted",
                    outcome="rejected_no_pids",
                    resources=f"{app_name} port={ap.port}",
                )
            except Exception as exc:
                logger.debug("SEL audit failed for rejected_no_pids %s: %s", app_name, exc)
            # Restore tracking so a retry is possible after re-adoption
            with _lock:
                _processes.setdefault(app_name, ap)
                if ap.port:
                    _allocated_ports.setdefault(app_name, ap.port)
            return False
        try:
            # PID-reuse guard: signal a recorded PID only when its live
            # start-time identity still POSITIVELY matches the token captured
            # at adoption (same convention as the spawned-backend reap). This
            # is process identity, not a port/address heuristic, so every
            # recycling shape — same address, another local address, a
            # v6-only wildcard, or a non-listener — fails the match and is
            # never signalled. A PID with no recorded token (identity was
            # unreadable at adoption) or an unreadable live value reads as
            # "identity unconfirmed" and is skipped, per the
            # process_start_time contract: do not kill what you cannot name.
            target_pids: set[int] = set()
            unconfirmed: list[int] = []
            for pid in ap.adopted_pids:
                recorded_st = ap.adopted_start_times.get(pid)
                if recorded_st is not None and _proc_start_time(pid) == recorded_st:
                    target_pids.add(pid)
                elif platform_compat.pid_exists(pid):
                    unconfirmed.append(pid)
            if unconfirmed:
                logger.warning(
                    "Adopted backend for %s on port %s: skipping live PIDs %s — "
                    "start-time identity does not match the adoption record "
                    "(recycled PID or unreadable identity); not signalling them",
                    app_name,
                    ap.port,
                    unconfirmed,
                )

            pids: list[int] = []
            for pid in target_pids:
                if pid <= 0:
                    continue
                try:
                    # Identity-PINNED (kill_pid_pinned): on Windows the handle
                    # that re-verifies the start time stays open across the
                    # terminate, so the PID taskkill resolves cannot have been
                    # recycled between the identity check above and the signal.
                    # False means the pin could not be established (the process
                    # exited since the check) — nothing to stop, skip it.
                    # POSIX delegates straight through to os.kill.
                    if (
                        platform_compat.kill_pid_pinned(
                            pid, ap.adopted_start_times[pid], platform_compat.SIGTERM
                        )
                        is False
                    ):
                        logger.info(
                            "Adopted backend for %s: pid %d exited before the "
                            "pinned SIGTERM — nothing to signal",
                            app_name,
                            pid,
                        )
                        continue
                    pids.append(pid)
                except (ProcessLookupError, OSError):
                    pass
            try:
                sel().log_api_access(
                    caller="gateway",
                    operation="app_backend_stop_adopted",
                    outcome="sigterm",
                    resources=f"{app_name} port={ap.port} pids={pids}",
                )
            except Exception as exc:
                logger.debug("SEL log_api_access failed for app_backend_stop_adopted: %s", exc)
            # Wait for graceful shutdown (non-blocking poll)
            _wait_for_pids(pids, timeout=2.0)
            # Escalate to SIGKILL if still alive
            escalated: list[int] = []
            for pid in pids:
                # pid_exists (not os.kill(pid,0), which terminates on Windows).
                # The graceful-shutdown wait above is exactly the window in
                # which the backend can exit and its PID be recycled, and
                # SIGKILL is the destructive half — so the escalation re-reads
                # the start-time identity here (this is what covers POSIX,
                # where kill_pid_pinned delegates straight through) and the
                # pinned kill then holds the Windows handle across the signal.
                if (
                    platform_compat.pid_exists(pid)
                    and _proc_start_time(pid) == ap.adopted_start_times[pid]
                ):
                    try:
                        if (
                            platform_compat.kill_pid_pinned(
                                pid,
                                ap.adopted_start_times[pid],
                                platform_compat.SIGKILL,
                            )
                            is not False
                        ):
                            escalated.append(pid)
                    except (ProcessLookupError, OSError):
                        pass
            if escalated:
                try:
                    sel().log_api_access(
                        caller="gateway",
                        operation="app_backend_stop_adopted",
                        outcome="sigkill_escalation",
                        resources=f"{app_name} port={ap.port} pids={escalated}",
                    )
                except Exception as exc:
                    logger.debug(
                        "SEL log_api_access failed for app_backend_stop_adopted sigkill: %s", exc
                    )
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            logger.warning(
                "Failed to stop adopted backend for %s on port %s: %s",
                app_name,
                ap.port,
                exc,
            )
            # Restore tracking so a retry is possible
            with _lock:
                _processes.setdefault(app_name, ap)
                if ap.port:
                    _allocated_ports.setdefault(app_name, ap.port)
            return False
        if _retry_if_serving is not None and _health_probe(ap.port, _retry_if_serving).healthy:
            # AMBIGUOUS observation, resolved by the caller who cares.
            #
            # An adopted backend can reach here two ways: nothing was signalled
            # because no recorded PID matched its adoption identity, or the recorded
            # PIDs were signalled and its supervisor started a replacement. Both read
            # the same from here, and the DEFAULT reading covers both: the app's
            # process is gone, so the stop succeeded (see TestStopAdoptedBackend,
            # which pins that an identity mismatch means the PID was recycled).
            #
            # A caller enforcing a WITHDRAWN trust ceiling cannot accept that reading
            # on faith: if something is still answering the port, reporting success
            # hands it un-trusted code that is serving, with tracking popped and
            # nothing left to retry against. Such a caller passes a health path and
            # pays for one probe, and a port that still answers becomes the same
            # refusal shape as the two branches above: tracking restored, False
            # returned. Only a silent port ends the sequence.
            logger.warning(
                "Adopted backend for %s is answering on port %s again after the "
                "stop; restoring tracking so the stop can be retried",
                app_name,
                ap.port,
            )
            try:
                sel().log_api_access(
                    caller="gateway",
                    operation="app_backend_stop_adopted",
                    outcome="rejected_replacement_serving",
                    resources=f"{app_name} port={ap.port}",
                )
            except Exception as exc:
                logger.debug(
                    "SEL audit failed for rejected_replacement_serving %s: %s",
                    app_name,
                    exc,
                )
            with _lock:
                _processes.setdefault(app_name, ap)
                if ap.port:
                    _allocated_ports.setdefault(app_name, ap.port)
            return False

    if ap.proc:
        logger.info("Stopped app %s backend (pid %d)", app_name, ap.pid)
    else:
        logger.info("Stopped adopted app %s backend on port %s", app_name, ap.port)
    if ap.log_fh:
        try:
            ap.log_fh.close()
        except OSError:
            pass
    return True
