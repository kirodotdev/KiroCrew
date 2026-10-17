"""Ending processes.

Signalling a pid, a process group or a tree (``taskkill`` on Windows), the pinned
variants that act only while the target's identity is held open, and kill-and-reap
and terminate-and-reap for children this process spawned.

Every public kill or reap helper of the family lives here, and so does every call to
``os.killpg``, ``kill_pid`` or ``kill_process_tree`` (pinned or not), the signal-0
group probe ``pgroup_exists`` included: ``test/test_kill_chokepoint_ratchet.py``
exempts this one file, so such a call in another owner counts as a new kill site.

One of the owners ``kiro_crew.platform_compat`` is split into: every name here stays
readable and patchable as ``kiro_crew.platform_compat.<name>``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import subprocess
import time
from typing import Any, Coroutine

from kiro_crew.executors import subprocess_executor
from kiro_crew.platform_compat_identity import _windows_process_handle_identity
from kiro_crew.platform_compat_liveness import pgroup_of, pid_exists
from kiro_crew.platform_compat_native import (
    _OWN_PGID,
    _SUBPROCESS_NO_WINDOW,
    _WINDOWS_PID_MAX,
    IS_POSIX,
    IS_WINDOWS,
    SIGKILL,
    SIGTERM,
    _close_process_handle,
    _open_process_query_handle,
)
from kiro_crew.platform_compat_trust import trusted_system_bin
from kiro_crew.platform_compat_winproc import open_process_termination_handle
from kiro_crew.platform_compat_wintree import (
    _PENDING_WINDOWS_TREE_CLEANUPS,
    _PENDING_WINDOWS_TREE_CLEANUPS_LOCK,
    _advance_owned_windows_tree,
    release_windows_tree_reservation,
    reserve_windows_tree_cleanup,
)

# The whole compatibility layer logs under one name, which operator log filters and
# the tests' caplog scopes key on.
logger = logging.getLogger("kiro_crew.platform_compat")


def pgroup_exists(pgid: int) -> bool:
    """Return True iff any member of process GROUP ``pgid`` is alive (best-effort).

    The tree-faithful liveness probe for a child spawned with
    ``start_new_session=True``: the launcher's pid doubles as the group id and
    ordinary descendants keep it after the launcher exits, so the group
    outlives the launcher exactly as long as any member does. A descendant
    that ``setsid()``s out of the group evades this probe precisely as it
    evades ``kill_process_tree`` -- callers that must catch those use the
    escaped-children reapers, not this.

    POSIX: ``os.killpg(pgid, 0)`` -- conservative on EPERM and on an integer
    outside the native ``pid_t`` range (both read as alive/unknown). Windows:
    process groups in this sense do not exist and ``kill_process_tree`` already
    walks the whole child tree via ``taskkill /T``, so the group id (== the
    launcher pid) is probed as a plain pid via :func:`pid_exists`. A value wider
    than the Win32 DWORD PID boundary also reads as alive/unknown rather than
    being truncated into a different identity.
    """
    if pgid <= 0:
        return False
    if not IS_POSIX:
        if pgid > _WINDOWS_PID_MAX:
            return True
        return pid_exists(pgid)
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except OverflowError:
        # Persisted owner markers can be syntactically numeric without fitting
        # pid_t. Unknown identity is not proof that cleanup is safe.
        return True
    except OSError:
        return True  # exists but we can't signal it
    return True


def _raise_taskkill_error(pid: int, rc: int, stderr: bytes) -> None:
    """Translate a Windows taskkill non-zero rc into ProcessLookupError /
    PermissionError / OSError so callers' POSIX-style ``except`` guards
    (``except (ProcessLookupError, OSError)``, ``except PermissionError``)
    fire on Windows too, matching the POSIX raise semantics.

    taskkill exit codes: 128 = process not found (rebadge as
    ProcessLookupError, POSIX analog of ESRCH); 1/5 = access denied
    (PermissionError, POSIX analog of EPERM); anything else = generic
    OSError with the stderr blob.
    """
    msg = (stderr or b"").decode("utf-8", "replace").strip() or f"taskkill rc={rc}"
    if rc == 128:
        raise ProcessLookupError(f"[taskkill rc=128] {msg}")
    if rc in (1, 5):
        raise PermissionError(f"[taskkill rc={rc}] {msg}")
    raise OSError(f"[taskkill rc={rc}] {msg}")


def kill_pid(pid: int, sig: int = SIGTERM) -> bool:
    """Send *sig* to *pid*. Returns True on success.

    POSIX: delegates to ``os.kill`` and **lets exceptions propagate**
    (``ProcessLookupError``, ``PermissionError``, ``OSError``) so callers
    can branch on them.
    Windows: uses ``taskkill /F`` and raises the same exception types on
    non-zero rc (mapped from taskkill's exit codes — see
    :func:`_raise_taskkill_error`) so ``except (ProcessLookupError,
    OSError)`` handlers written for POSIX fire uniformly. Returns True
    on success on both platforms.
    """
    if IS_POSIX:
        os.kill(pid, sig)
        return True
    taskkill_bin = trusted_system_bin("taskkill")
    if taskkill_bin is None:
        raise OSError("taskkill not found in the trusted system directories")
    try:
        r = subprocess.run(
            [taskkill_bin, "/F", "/PID", str(pid)],
            check=False,
            capture_output=True,
            timeout=5,
            creationflags=_SUBPROCESS_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OSError(f"taskkill invocation failed: {exc}") from exc
    if r.returncode != 0:
        _raise_taskkill_error(pid, r.returncode, r.stderr or r.stdout)
    return True


def kill_process_group(pgid: int, sig: int = SIGTERM) -> bool:
    """Signal the POSIX process group *pgid* -- an id the CALLER captured and verified.

    The group-addressed sibling of :func:`kill_process_tree`, for a caller that
    holds a group id it read while the group's leader was alive and identity-
    checked (:func:`kiro_crew.process_identity.isolated_group_of`) and must not
    resolve anything from a pid at signal time: ``os.getpgid(pid)`` of a pid the
    kernel has since handed to another process names that process's group.
    ``os.killpg(pgid, sig)`` in-process, **letting exceptions propagate**
    (``ProcessLookupError`` when the group has emptied, ``PermissionError``
    when a member is unsignalable).

    Carries the same broadcast guard as :func:`kill_process_tree`, refusing
    with ``ValueError`` instead of degrading: ``killpg(1, sig)`` is ``kill(-1,
    sig)`` in libc -- a signal to every process this uid owns -- so a non-int
    id, an id <= 1, or our own group is never signalled, and there is no pid to
    fall back to here. POSIX only: Windows has no process groups in this sense
    (``OSError``); its trees are terminated through pinned handles
    (:func:`kill_process_tree_pinned`).
    """
    if not IS_POSIX:
        raise OSError("kill_process_group: no POSIX process groups on this platform")
    if type(pgid) is not int or pgid <= 1 or pgid == _OWN_PGID:
        raise ValueError(f"kill_process_group: refusing broadcast/self process group {pgid!r}")
    os.killpg(pgid, sig)
    return True


def kill_process_tree(pid: int, sig: int = SIGTERM) -> bool:
    """Kill *pid* and all descendants. Returns True on success.

    POSIX: ``os.killpg(os.getpgid(pid), sig)``; **lets exceptions
    propagate** (``ProcessLookupError`` if already dead, etc.).
    Windows: ``taskkill /T /F`` and raises the same exception types on
    non-zero rc (via :func:`_raise_taskkill_error`) so ``except
    (ProcessLookupError, OSError)`` handlers written for POSIX fire
    uniformly and callers' fallback / escalation branches execute on a
    genuine Windows failure (protected descendant, transient
    access-denied) instead of the shim silently returning False.
    Returns True on success on both platforms.

    POSIX broadcast guard: ``killpg(1, sig)`` is ``kill(-1, sig)`` in
    libc — a signal to EVERY process this uid owns (systemd --user
    manager, SSH, the gateway itself). A non-int pid (e.g. a mocked
    ``Popen``'s ``MagicMock`` pid coerces to 1 via ``__index__``),
    pid <= 1, pgid <= 1, or our own process group is therefore refused
    for the *group* signal and degrades to a pid-scoped ``os.kill``
    (or raises ``ValueError`` for a non-int pid).
    """
    if IS_POSIX:
        if type(pid) is not int or pid <= 1:
            raise ValueError(f"kill_process_tree: refusing non-int/reserved pid {pid!r}")
        pgid = os.getpgid(pid)
        if pgid <= 1 or pgid == _OWN_PGID:
            logger.error(
                "kill_process_tree: refusing broadcast/self pgid %d for pid %d; "
                "falling back to pid-scoped kill",
                pgid,
                pid,
            )
            os.kill(pid, sig)
            return True
        os.killpg(pgid, sig)
        return True
    taskkill_bin = trusted_system_bin("taskkill")
    if taskkill_bin is None:
        raise OSError("taskkill not found in the trusted system directories")
    try:
        r = subprocess.run(
            [taskkill_bin, "/T", "/F", "/PID", str(pid)],
            check=False,
            capture_output=True,
            timeout=5,
            creationflags=_SUBPROCESS_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OSError(f"taskkill /T invocation failed: {exc}") from exc
    if r.returncode != 0:
        _raise_taskkill_error(pid, r.returncode, r.stderr or r.stdout)
    return True


def kill_process_tree_pinned(
    pid: int, expected_start_time: str, sig: int = SIGTERM, *, app_tracking: bool = False
) -> bool:
    """Drain *pid*'s Windows tree only after pinning its creation identity.

    Returns False without signalling when the original object cannot be opened
    or its creation time does not match. On Windows an exited root may still
    anchor surviving descendants; the exact-handle drain verifies their full
    lifetime chains and confirms exit, raising on unknown or incomplete cleanup.
    SIGTERM and SIGKILL both use Windows hard termination, as with taskkill /F.
    POSIX continues to delegate to kill_process_tree unchanged.
    """
    if not IS_WINDOWS:
        return kill_process_tree(pid, sig)
    try:
        key = (pid, int(expected_start_time))
    except (TypeError, ValueError):
        return False
    state = reserve_windows_tree_cleanup(key)
    with state.lock:
        if state.retired:
            return state.root_pid > 0
        if not state.handles:
            try:
                handle = open_process_termination_handle(pid, expected_start_time)
            except BaseException:
                release_windows_tree_reservation(state)
                raise
            if handle is None:
                release_windows_tree_reservation(state)
                return False
            state.root_pid = pid
            state.handles[pid] = handle
        state.retire_app_tracking |= app_tracking
        with _PENDING_WINDOWS_TREE_CLEANUPS_LOCK:
            _PENDING_WINDOWS_TREE_CLEANUPS[key] = state
    drained, _ = _advance_owned_windows_tree(state)
    return drained


def kill_pid_pinned(pid: int, expected_start_time: str, sig: int = SIGTERM) -> bool:
    """Kill *pid* only while its verified identity is PINNED OPEN.

    Single-process variant of :func:`kill_process_tree_pinned` — same Windows
    guarantee (the query handle that verified the creation time stays open
    across the terminate, so the PID ``taskkill`` resolves cannot have been
    recycled between the check and the signal), delegating to :func:`kill_pid`
    instead of tearing down the tree. Returns ``False`` — without signalling —
    when the handle cannot be opened or the identity does not match; callers
    treat that as "identity unconfirmed, do not kill". On a match it delegates
    to :func:`kill_pid` and propagates its exceptions unchanged.

    POSIX delegates straight through: ``os.kill`` is issued in-process by the
    same interpreter that did the check and there is no handle to hold; the
    residual probe-to-signal window there is the pre-existing one callers
    mitigate by re-confirming identity before destructive escalation.
    """
    if not IS_WINDOWS:
        return kill_pid(pid, sig)
    handle = _open_process_query_handle(pid)
    if handle is None:
        return False
    try:
        identity = _windows_process_handle_identity(handle)
        # (pid, creation_time, exit_time) -- the creation half is the identity.
        if identity is None or str(identity[1]) != expected_start_time:
            return False
        # The handle stays open for the whole call: taskkill resolves the PID
        # while this process object is still referenced, so the PID cannot have
        # been recycled onto a different process in between.
        return kill_pid(pid, sig)
    finally:
        _close_process_handle(handle)


async def kill_pid_async(pid: int, sig: int = SIGTERM) -> bool:
    """Async variant of :func:`kill_pid` — offloads Windows ``taskkill`` off the loop.

    Windows kills spawn a ``taskkill.exe`` subprocess (``subprocess.run`` with a
    5s timeout) — a blocking spawn that stalls the asyncio event loop when
    called from an ``async def`` coroutine. Offload to
    :func:`kiro_crew.executors.subprocess_executor` (the same bounded pool the
    ACP client already uses for its ``ps``/``pgrep`` + ``os.close`` teardown
    work) so the loop keeps running while ``taskkill`` waits for the target to
    exit. POSIX ``os.kill`` is a non-blocking syscall — we still call
    :func:`kill_pid` inline (no executor hop) so the async signature is
    consistent across platforms AND existing test suites that monkeypatch
    :func:`kill_pid` continue to intercept the call. Raises the same exception
    types as :func:`kill_pid` (``ProcessLookupError`` / ``PermissionError`` /
    ``OSError``).
    """
    if IS_POSIX:
        # Inline dispatch to sync kill_pid: POSIX os.kill is non-blocking, and
        # keeping this in-process (rather than a to-thread hop) preserves the
        # exception frame + lets existing tests that patch kill_pid observe it.
        return kill_pid(pid, sig)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(subprocess_executor(), kill_pid, pid, sig)


async def kill_process_tree_async(pid: int, sig: int = SIGTERM) -> bool:
    """Async variant of :func:`kill_process_tree` — offloads Windows ``taskkill /T``.

    See :func:`kill_pid_async` for the offload rationale. POSIX
    ``os.killpg`` is non-blocking so this dispatches inline to
    :func:`kill_process_tree`; the Windows branch spawns ``taskkill /T /F``
    off the loop via :func:`kiro_crew.executors.subprocess_executor`. Raises
    the same exceptions as :func:`kill_process_tree`.
    """
    if IS_POSIX:
        return kill_process_tree(pid, sig)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(subprocess_executor(), kill_process_tree, pid, sig)


#: Ceiling on waiting for a killed process tree. A descendant that ignores the
#: signal must not turn cleanup into a hang while the caller is already
#: handling a timeout or a cancellation — often on the shutdown path, where an
#: unbounded reap would wedge the whole teardown.
REAP_TIMEOUT_SECS: float = 10


def _shares_own_process_group(pid: int) -> bool:
    """True when *pid* runs in the gateway's OWN process group.

    Such a child was spawned without ``start_new_session``, so it has no tree
    of its own to signal — see :func:`kill_and_reap`, whose group kill this
    gates. Fail-closed (``False``) on every probe failure: the pid may be gone
    or unreadable, and the tree kill it guards is itself best-effort and
    protected by :func:`kill_process_tree`'s own broadcast/self-group guard.

    This is a named seam on purpose. The probe reads the LIVE process table,
    so a test handing :func:`kill_and_reap` a synthetic pid was at the mercy
    of whichever real process happened to own that pid: when it landed inside
    the runner's own group the skip fired and the expected tree kill never
    happened. The rootdir ``conftest`` pins this one function instead of the
    shared ``_OWN_PGID`` — pinning that would also disarm
    :func:`kill_process_tree`'s self-group refusal, the guard that keeps a
    test from broadcasting a signal to the whole pytest run.
    """

    if not IS_POSIX:
        return False
    try:
        return os.getpgid(pid) == _OWN_PGID
    except Exception:
        return False


def kill_popen_tree(proc: subprocess.Popen[Any]) -> None:
    """Kill a ``Popen`` child and its descendants; never raises.

    The synchronous counterpart of :func:`kill_and_reap`'s kill step, for a child
    started in its own process group (``start_new_session`` on POSIX, a new
    process group on Windows). The group is signalled only while *proc* is
    unreaped: its pid (also its group id) cannot be reused before ``waitpid``,
    so the signal cannot land on an unrelated group. The direct ``kill``
    afterwards covers a platform where the tree walk failed. That ``kill`` polls
    first (``Popen.send_signal``), so it may reap a child that already exited:
    the caller collects through ``Popen.wait()``/``poll()`` (which then report the
    status) and must tolerate ``ChildProcessError`` from any lower-level wait.
    """
    if proc.returncode is None:
        if IS_POSIX:
            with contextlib.suppress(OSError, ValueError):
                kill_process_group(proc.pid, SIGKILL)
        else:
            with contextlib.suppress(Exception):
                kill_process_tree(proc.pid)
    with contextlib.suppress(OSError):
        proc.kill()


async def kill_and_reap(proc: asyncio.subprocess.Process, *, timeout: float | None = None) -> None:
    """Kill *proc* AND its descendants, then wait for it under a bound.

    The shared cleanup for a PIPE-stdio child whose ``communicate()`` was
    abandoned by ``asyncio.wait_for`` — used on BOTH the timeout and the
    cancellation path. Cancellation matters as much as timeout: a gateway
    shutdown cancels the owning task, and without this the child keeps
    running after the process that started it is gone.

    The whole TREE is signalled, not just the direct child. A spawned command
    is often a shell line (``curl … | sh``, ``pip … | tee log``), so killing
    only the shell leaves the pipeline members running and can leave
    ``communicate()`` waiting on pipes those survivors still hold. A child
    sharing the caller's own process group (spawned without
    ``start_new_session``) has no tree of its own to signal — the group kill
    is skipped for it and the pid-scoped ``kill()`` below covers it, instead
    of tripping :func:`kill_process_tree`'s broadcast guard on every routine
    timeout. Likewise, a child that asyncio has already reaped
    (``proc.returncode is not None``) is skipped: its pid may have been
    recycled onto a different process, and the pid-scoped ``kill()`` below is
    harmless because the handle refers to a child that has already exited.

    The reap goes through ``communicate()`` rather than ``wait()`` so the
    pipes are drained: ``wait_for`` already cancelled the original
    ``communicate()``, and a killed child blocked writing into a full pipe
    would make a bare ``wait()`` hang the calling task forever. The reap is
    bounded by *timeout* (default :data:`REAP_TIMEOUT_SECS`). Both the kill
    and the reap are best-effort, since the caller is already handling a
    timeout or a cancellation and must not have it masked by a cleanup error.

    The whole sequence runs in a shielded inner task (see
    :func:`_run_cleanup_shielded`). A child whose own TERM handling matters
    uses :func:`terminate_and_reap` instead.
    """

    async def _cleanup() -> None:
        # Bare-name lookup so a test can pin the probe (see
        # ``_shares_own_process_group``) without reaching into ``os``.
        #
        # ``proc.returncode is None`` guards against a recycled pid: once
        # asyncio has recorded the child's exit, the OS may hand that pid to
        # an unrelated process (even another of our own gateway children, whose
        # parent pid would also be ours), so a pid-addressed group SIGKILL could
        # reach a stranger's tree. A child that has not been reaped yet still
        # owns its pid. This is the same ``reaped=proc.returncode is not None``
        # test ``_isolated_group_of_live_child`` / ``terminate_and_reap`` use.
        if proc.returncode is None and not _shares_own_process_group(proc.pid):
            # Bare-name lookup resolves through this module's namespace at
            # call time, where a patch of ``kiro_crew.platform_compat.
            # kill_process_tree_async`` lands too (``kiro_crew._facade_mirror``),
            # so tests still intercept the tree kill.
            with contextlib.suppress(Exception):
                await kill_process_tree_async(proc.pid, SIGKILL)
        with contextlib.suppress(Exception):
            proc.kill()
        with contextlib.suppress(Exception, asyncio.TimeoutError):
            await asyncio.wait_for(
                proc.communicate(),
                timeout=REAP_TIMEOUT_SECS if timeout is None else timeout,
            )

    await _run_cleanup_shielded(_cleanup())


async def _run_cleanup_shielded(coro: Coroutine[Any, Any, None]) -> None:
    """Run a kill-and-reap *coro* to completion even if the caller is cancelled.

    A (repeat) cancellation of the caller landing mid-cleanup must not abandon
    the kill or leave the child un-reaped: the cancellation is absorbed until
    the cleanup finishes and then re-delivered once.
    """
    cleanup = asyncio.ensure_future(coro)
    cancelled = False
    while True:
        try:
            await asyncio.shield(cleanup)
            break
        except asyncio.CancelledError:
            # Either the CALLER was (re-)cancelled while the shield kept the
            # cleanup running, or the cleanup itself ended cancelled. Absorb
            # until the cleanup has genuinely finished, then re-deliver.
            cancelled = True
            if cleanup.done():
                break
    if cancelled:
        raise asyncio.CancelledError


def _isolated_group_of_live_child(pid: int, *, reaped: bool) -> int | None:
    """The process group a child leads, or ``None`` when it cannot be signalled safely.

    Read only while the caller still holds the child un-reaped (*reaped* is
    False), so its pid cannot have been handed to another process yet, and
    kept only for an isolated leader (``pgid == pid``, which is what
    ``start_new_session=True`` makes it; a child is never init or the leader
    of our own group, so that also rules both out).
    """
    if not IS_POSIX or type(pid) is not int or reaped:
        return None
    try:
        pgid = pgroup_of(pid)
    except (OverflowError, ValueError):
        return None
    return pgid if pgid == pid else None


async def _discard_output_until_exit(proc: asyncio.subprocess.Process) -> None:
    """Drain *proc*'s pipes to EOF, keeping nothing, then wait for it.

    ``communicate()`` would buffer everything a still-running child writes in
    this process's memory; a stopping installer can be chatty.
    """

    async def _discard(stream: asyncio.StreamReader | None) -> None:
        if stream is None:
            return
        while await stream.read(65536):
            pass

    await asyncio.gather(_discard(proc.stdout), _discard(proc.stderr))
    await proc.wait()


async def terminate_and_reap(
    proc: asyncio.subprocess.Process, *, grace: float, reap_timeout: float | None = None
) -> None:
    """Stop *proc*'s process group gracefully: SIGTERM, up to *grace*, then SIGKILL.

    For a child whose own cleanup matters: an installer that moved the install
    aside before rebuilding it restores it from a TERM trap, and the SIGKILL
    :func:`kill_and_reap` sends first skips that trap and strands the install.

    POSIX, for a live child that leads its own group (see
    :func:`_isolated_group_of_live_child`): the group gets SIGTERM, its pipes
    are drained (and discarded), and the GROUP is waited on until it empties or
    *grace* runs out. Pipe EOF alone is not the end of the trap: a member that
    holds neither pipe (``cmd >log 2>&1``, ``cmd | tee``) can still be rolling
    back. Whatever is left then gets SIGKILL, addressed to the group id read
    at the start, which cannot name another group while any member of this
    one is alive. The leader is then reaped without resolving anything from
    its pid again, bounded by *reap_timeout* (default
    :data:`REAP_TIMEOUT_SECS`). A descendant that ``setsid()``-ed into a session
    of its own is outside the group and is not signalled; the trap of the
    process that started it is what stops it.

    Otherwise (Windows, a child already reaped, or one that does not lead its
    own group) this is :func:`kill_and_reap`. Shielded like it: a cancellation
    of the caller is re-delivered after the stop has finished.
    """
    reap = REAP_TIMEOUT_SECS if reap_timeout is None else reap_timeout

    async def _cleanup() -> None:
        pgid = _isolated_group_of_live_child(proc.pid, reaped=proc.returncode is not None)
        if pgid is None:
            await kill_and_reap(proc, timeout=reap)
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + grace
        with contextlib.suppress(Exception):
            kill_process_group(pgid, SIGTERM)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(_discard_output_until_exit(proc), timeout=grace)
        while pgroup_exists(pgid) and loop.time() < deadline:
            await asyncio.sleep(0.05)
        if pgroup_exists(pgid):
            with contextlib.suppress(Exception):
                kill_process_group(pgid, SIGKILL)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(_discard_output_until_exit(proc), timeout=reap)

    await _run_cleanup_shielded(_cleanup())


def terminate_and_reap_sync(
    proc: subprocess.Popen, *, grace: float, reap_timeout: float | None = None
) -> None:
    """Blocking sibling of :func:`terminate_and_reap`, for a ``Popen`` child.

    Same policy: on POSIX, for a live child that leads its own group
    (``start_new_session=True``), SIGTERM the group, wait for the GROUP to
    empty for up to *grace* seconds, SIGKILL whatever is left (the leader by
    pid too, should the group signal be refused), then reap the leader within
    *reap_timeout* (default :data:`REAP_TIMEOUT_SECS`). For a child with PIPE
    stdio use the async helper, which drains them; this one is for a child
    writing to the caller's own terminal. Otherwise (Windows, a child already
    reaped, or one that does not lead its own group) it is killed outright the
    way :func:`kill_and_reap` kills: its tree, unless it shares our group or
    has already been reaped -- a reaped child's pid may belong to another
    process by now.

    A Ctrl-C landing mid-stop does not abandon it: KeyboardInterrupt is held
    until the stop has finished, then re-raised once.
    """
    reap = REAP_TIMEOUT_SECS if reap_timeout is None else reap_timeout
    interrupted = False

    def _wait(seconds: float) -> None:
        nonlocal interrupted
        deadline = time.monotonic() + seconds
        while True:
            try:
                proc.wait(timeout=max(0.0, deadline - time.monotonic()))
                return
            except subprocess.TimeoutExpired:
                return
            except KeyboardInterrupt:
                interrupted = True

    pgid = _isolated_group_of_live_child(proc.pid, reaped=proc.poll() is not None)
    if pgid is not None:
        deadline = time.monotonic() + grace
        with contextlib.suppress(Exception):
            kill_process_group(pgid, SIGTERM)
        _wait(grace)
        while pgroup_exists(pgid) and time.monotonic() < deadline:
            try:
                time.sleep(0.05)
            except KeyboardInterrupt:
                interrupted = True
        if pgroup_exists(pgid):
            with contextlib.suppress(Exception):
                kill_process_group(pgid, SIGKILL)
    else:
        # ``poll()`` reaps an exited child, which frees its pid for reuse, so a
        # pid-addressed tree kill is only safe while it still answers ``None``:
        # a child exiting after that stays an unreaped zombie holding its pid.
        if (
            type(proc.pid) is int
            and proc.poll() is None
            and not _shares_own_process_group(proc.pid)
        ):
            with contextlib.suppress(Exception):
                kill_process_tree(proc.pid, SIGKILL)
    if proc.poll() is None:
        with contextlib.suppress(Exception):
            proc.kill()
    _wait(reap)
    if interrupted:
        raise KeyboardInterrupt
