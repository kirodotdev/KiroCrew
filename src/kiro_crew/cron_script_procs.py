"""The registry of running cron children, and how a cancel kills one mid-run.

A script or command cron runs as a blocking subprocess in the cron thread executor,
so cancelling its asyncio task cannot stop it. Each child is registered here by job
id while it is spawned and while it runs, so ``kill_running_process`` can kill its
whole process group, and a cancel that arrives during the spawn is kept until the
spawn settles.

Part of :mod:`kiro_crew.cron_script`, the import path callers, tests and docs use: it
reaches every name here, and a patch through it reaches this module too
(:mod:`kiro_crew._facade_mirror`). Logs under the ``kiro_crew.cron_script`` logger.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
import time

from kiro_crew import platform_compat

logger = logging.getLogger("kiro_crew.cron_script")


# ── Running-subprocess registry (user-initiated cancellation) ──
#
# Script/command crons run as blocking ``subprocess`` calls inside the cron
# thread executor — cancelling the owning asyncio task cannot interrupt them.
# Each sandboxed child is registered here (keyed by job id) so that
# ``CronService.cancel()`` can SIGTERM the whole process group mid-run.
_PROCS_LOCK = threading.Lock()

_RUNNING_PROCS: dict[str, subprocess.Popen] = {}

_CANCELLED_PROC_JOBS: set[str] = set()

#: Jobs whose sandboxed child is being SPAWNED right now but is not yet in
#: ``_RUNNING_PROCS``. Without this, ``kill_running_process`` sees no registered
#: child, returns False and records NOTHING -- so a cancel arriving inside
#: ``popen_limited``'s interpreter-ENOENT backoff is discarded rather than
#: delayed, and the retry then runs work the user cancelled while the run still
#: reports ``ok``. Membership makes that window cancellable.
_SPAWNING_JOBS: set[str] = set()

_KILL_ESCALATION_GRACE_SECS = 5.0


def _begin_spawn(job_id: str | None) -> bool:
    """Claim the spawn slot for *job_id*. False means REFUSED, do not proceed.

    Refused when this job is already spawning or already registered, because
    every cancellation surface here is keyed on the job id ALONE:
    ``kill_running_process`` takes only a job id, and ``_RUNNING_PROCS`` holds one
    child per job. Two concurrent runs of one job therefore make "cancel this
    job" ambiguous by construction, and the flag is consumed by whichever run
    finishes its spawn first -- so a cancel aimed at a run still in its ENOENT
    backoff could be eaten by a rerun that was never cancelled. That rerun then
    kills its own child and reports cancelled, while the run the user actually
    cancelled sees no flag and executes.

    Refusing the overlap makes the per-job cancellation contract well defined.
    It also closes a standing hazard: a second concurrent run would overwrite
    the ``_RUNNING_PROCS`` entry, orphaning the first child from cancellation
    entirely.

    An unidentified run (``job_id is None``) is never registered or cancellable,
    so it is always allowed and claims nothing.
    """
    if job_id is None:
        return True
    with _PROCS_LOCK:
        if job_id in _SPAWNING_JOBS or job_id in _RUNNING_PROCS:
            return False
        _SPAWNING_JOBS.add(job_id)
        return True


def _finish_spawn(job_id: str | None, proc: subprocess.Popen) -> bool:
    """Atomically move *job_id* from spawning to registered.

    Both mutations happen under a single hold of ``_PROCS_LOCK``, so
    ``kill_running_process`` -- which takes the same lock -- can never observe a
    moment where the job is NEITHER spawning nor registered. Doing this as two
    separate calls left exactly that gap, and a cancel landing in it was dropped.

    Returns True when a cancel was recorded while the spawn was in flight. The
    child launched regardless (the cancel raced the successful ``Popen``), so
    nothing else will ever signal it and the caller MUST kill it. The flag is
    CONSUMED here: leaving it set would make ``_unregister_proc`` attribute this
    cancellation to a later run of the same job.
    """
    if job_id is None:
        return False
    with _PROCS_LOCK:
        _SPAWNING_JOBS.discard(job_id)
        if job_id in _CANCELLED_PROC_JOBS:
            _CANCELLED_PROC_JOBS.discard(job_id)
            return True
        _RUNNING_PROCS[job_id] = proc
        return False


def _abandon_spawn(job_id: str | None) -> bool:
    """Clear spawn state after a spawn that produced NO child.

    Returns True when a cancel had been recorded, so the caller reports the run
    cancelled instead of raising. Either way the cancellation flag is cleared:
    there is no child for it to signal, and leaving it set would make the NEXT
    run of the same job report itself cancelled.
    """
    if job_id is None:
        return False
    with _PROCS_LOCK:
        _SPAWNING_JOBS.discard(job_id)
        cancelled = job_id in _CANCELLED_PROC_JOBS
        _CANCELLED_PROC_JOBS.discard(job_id)
        return cancelled


def _spawn_cancelled(job_id: str | None) -> bool:
    """True when a cancel landed while this job's spawn was in flight.

    A PEEK, not a take, because this is the ``abort_retry`` hook: it may be
    consulted several times across the backoff. The flag is consumed by
    :func:`_finish_spawn` or :func:`_abandon_spawn`, whichever ends the spawn.
    """
    if job_id is None:
        return False
    with _PROCS_LOCK:
        return job_id in _CANCELLED_PROC_JOBS


def _unregister_proc(job_id: str, proc: subprocess.Popen) -> bool:
    """Remove the registry entry; return True if this run was cancelled."""
    with _PROCS_LOCK:
        if _RUNNING_PROCS.get(job_id) is proc:
            _RUNNING_PROCS.pop(job_id, None)
        cancelled = job_id in _CANCELLED_PROC_JOBS
        _CANCELLED_PROC_JOBS.discard(job_id)
        return cancelled


def _resolve_safe_pgid(proc: subprocess.Popen) -> int | None:
    """Resolve *proc*'s process group id with broadcast protection.

    Returns None (caller must fall back to the direct Popen handle) unless
    every check passes:

    - ``proc.pid`` must be a real ``int`` > 1. A ``MagicMock`` pid coerces to
      1 via ``__index__``, and ``os.killpg(1, sig)`` is ``kill(-1, sig)`` in
      libc — a signal broadcast to EVERY process this uid can reach, which
      SIGKILLed the whole login session (systemd --user manager included).
    - The resolved pgid must be > 1 (same ``kill(-1)`` footgun) and must not
      be our own process group (suicide / killing the gateway tree).
    """
    if not platform_compat.IS_POSIX:
        # Windows has no process groups (os.getpgid/os.killpg don't exist);
        # callers fall back to platform_compat.kill_process_tree (taskkill /T).
        return None
    pid = getattr(proc, "pid", None)
    if type(pid) is not int or pid <= 1:
        logger.error("kill guard: refusing non-int/reserved pid %r", pid)
        return None
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, PermissionError, OSError):
        return None
    if pgid <= 1 or pgid == os.getpgid(0):
        logger.error("kill guard: refusing broadcast/self pgid %d for pid %d", pgid, pid)
        return None
    return pgid


def kill_running_process(job_id: str) -> bool:
    """SIGTERM the sandboxed subprocess group for a running script/command cron.

    Escalates to SIGKILL after a grace period from a daemon thread so the
    caller (the async cancel path) never blocks. Returns True when a live
    subprocess was found and signalled.
    """
    with _PROCS_LOCK:
        maybe_proc = _RUNNING_PROCS.get(job_id)
        if maybe_proc is None or maybe_proc.poll() is not None:
            # No live child to signal. If a spawn is in flight the cancellation
            # must still be RECORDED, or it is lost: the spawn's retry backoff
            # would finish, launch the command, and the run would report ok
            # having done the work the caller cancelled. The spawner consults
            # this flag before spawning and the run is reported cancelled.
            if job_id in _SPAWNING_JOBS:
                _CANCELLED_PROC_JOBS.add(job_id)
                return True
            return False
        proc: subprocess.Popen = maybe_proc
        _CANCELLED_PROC_JOBS.add(job_id)
    pgid = _resolve_safe_pgid(proc)
    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pgid = None
    if pgid is None:
        # Already gone (or unsignallable) on POSIX; on Windows there are no
        # process groups, so reap the whole tree via taskkill /T
        # (platform_compat) before falling back to a single-process terminate.
        killed_tree = False
        if not platform_compat.IS_POSIX:
            try:
                platform_compat.kill_process_tree(proc.pid, platform_compat.SIGTERM)
                killed_tree = True
            except (OSError, ProcessLookupError):
                killed_tree = False
        if not killed_tree:
            try:
                proc.terminate()
            except Exception:
                # Signal never delivered: clear the cancelled flag so a natural
                # completion is not misreported as a cancellation.
                with _PROCS_LOCK:
                    _CANCELLED_PROC_JOBS.discard(job_id)
                return False

    def _escalate() -> None:
        time.sleep(_KILL_ESCALATION_GRACE_SECS)
        if proc.poll() is None:
            _kill_proc_group(proc)

    threading.Thread(target=_escalate, name=f"cron-cancel-{job_id}", daemon=True).start()
    logger.info("Cancel: sent SIGTERM to subprocess group of cron %s (pid %d)", job_id, proc.pid)
    return True


def _kill_proc_group(proc: subprocess.Popen) -> None:
    """Best-effort SIGKILL of a subprocess and its whole process group."""
    pgid = _resolve_safe_pgid(proc)
    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    # Windows (pgid is always None there): reap the tree via taskkill /T before
    # the single-process fallback so children don't orphan.
    if not platform_compat.IS_POSIX:
        try:
            platform_compat.kill_process_tree(proc.pid, platform_compat.SIGKILL)
            return
        except (OSError, ProcessLookupError):
            pass
    try:
        proc.kill()
    except Exception:
        pass


def _drain_after_kill(proc: subprocess.Popen, job_id: str | None) -> None:
    """Reap a SIGKILLed child's pipes without leaking fds or hijacking the result.

    ``communicate(timeout=5)`` can ITSELF raise ``TimeoutExpired``: the child
    outlived the group kill (uninterruptible I/O, or no pgid resolved so only
    ``proc.kill()`` was tried), or another process inherited the write end of
    the pipe and holds it open, so EOF never arrives. Waiting longer cannot help
    once SIGKILL has been sent, and the caller has already decided the outcome —
    so swallow that one exception rather than letting it displace the caller's
    ``raise`` / ``return``. Closing the pipes has to happen either way:
    ``Popen._communicate`` closes them as a side effect of reaching EOF, which
    is exactly the path not taken here, and nothing else ever closes them.
    """
    try:
        proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        logger.warning(
            "Post-kill drain timed out (5s) for cron %s (pid %s); the child "
            "outlived SIGKILL or another process holds the pipe — closing pipes",
            job_id,
            proc.pid,
        )
    finally:
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass
