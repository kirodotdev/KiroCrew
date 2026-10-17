"""Windows process trees the gateway owns.

The cleanup reservation charged before a physical spawn, the owned spawn itself,
terminating an exact handle and probing whether it still runs, and the bounded
drains that retire a tree only once every exact handle in it has exited, retrying
the ones that outlive a pass.

One of the owners ``kiro_crew.platform_compat`` is split into: every name here stays
readable and patchable as ``kiro_crew.platform_compat.<name>``.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes.util
import logging
import threading
import time
from ctypes import wintypes
from typing import Any

from kiro_crew import platform_lock_compat
from kiro_crew.executors import subprocess_executor
from kiro_crew.platform_compat_descendants import (
    _WINDOWS_CLEANUP_ROOT_LIMIT,
    WindowsCleanupCapacityError,
    _WindowsTreeOverflow,
)
from kiro_crew.platform_compat_identity import _windows_process_handle_identity
from kiro_crew.platform_compat_native import IS_WINDOWS, _windows_last_error
from kiro_crew.platform_compat_winproc import (
    close_process_handle,
    descendant_termination_handles,
    duplicate_asyncio_process_handle,
)

# The whole compatibility layer logs under one name, which operator log filters and
# the tests' caplog scopes key on.
logger = logging.getLogger("kiro_crew.platform_compat")


#: How long :func:`terminate_process_handle` waits for a refused process object to
#: signal when its exit code cannot settle the refusal: a process whose exit code is
#: 259, or one that is genuinely refused. Off the event loop only. A wait that ends
#: unsignalled keeps the refusal an error, which the drain retains and retries.
_WINDOWS_TERMINATE_REFUSAL_WAIT_MS = 250


def terminate_process_handle(handle: int) -> bool:
    """Terminate the exact Windows process object referenced by *handle*.

    Returns ``True`` when this call terminated a live process and ``False`` when
    the process had already exited, including one whose own exit begins between
    the liveness read and the terminate.
    """

    if type(handle) is not int or handle <= 0:
        raise ValueError(f"terminate_process_handle: refusing invalid handle {handle!r}")
    if not IS_WINDOWS:
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    process_handle = wintypes.HANDLE(handle)
    exit_code = wintypes.DWORD()
    still_active = 259
    error_access_denied = 5
    wait_object_0 = 0x00000000
    if not kernel32.GetExitCodeProcess(process_handle, ctypes.byref(exit_code)):
        raise OSError(_windows_last_error(), "GetExitCodeProcess failed")
    if exit_code.value != still_active:
        return False
    if not kernel32.TerminateProcess(process_handle, 1):
        error = _windows_last_error()
        # The kernel answers a terminate aimed at a process whose exit has begun
        # with ERROR_ACCESS_DENIED, the same code a genuine refusal carries. A
        # process that starts exiting between the read above and the call lands
        # here -- a console host leaving once its last client is gone does this
        # inside a drain. Its exit publishes the exit code, then runs the process
        # down (from which point the terminate is refused), and only then signals
        # the object, so the refusal can arrive while the object is unsignalled.
        # An exit code other than STILL_ACTIVE proves the exit, since a running
        # process always reads STILL_ACTIVE. While it still reads STILL_ACTIVE (a
        # process whose exit code IS 259, or a live one), the signal decides,
        # within a bounded wait -- a zero-time look on the event loop, which must
        # never wait. A refusal the signal does not settle, including one on a
        # handle that cannot be waited on, stays an error, as does any other
        # error.
        if error == error_access_denied:
            if (
                kernel32.GetExitCodeProcess(process_handle, ctypes.byref(exit_code))
                and exit_code.value != still_active
            ):
                return False
            wait_ms = (
                0 if platform_lock_compat._on_event_loop() else _WINDOWS_TERMINATE_REFUSAL_WAIT_MS
            )
            if int(kernel32.WaitForSingleObject(process_handle, wait_ms)) == wait_object_0:
                return False
        raise OSError(error, "TerminateProcess failed")
    return True


def process_handle_active(handle: int) -> bool:
    """Return whether an identity-stable Windows process handle is still live."""

    if type(handle) is not int or handle <= 0:
        raise ValueError(f"process_handle_active: refusing invalid handle {handle!r}")
    if not IS_WINDOWS:
        return False
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        exit_code = wintypes.DWORD()
        return bool(
            kernel32.GetExitCodeProcess(
                wintypes.HANDLE(handle),
                ctypes.byref(exit_code),
            )
            and exit_code.value == 259
        )
    except Exception:
        return False


_WINDOWS_TREE_REAP_TIMEOUT_SECS = 5.0
_WINDOWS_TREE_REAP_POLL_SECS = 0.01
_PENDING_WINDOWS_TREE_RETRY_LIMIT = 8


# The interpreters the capture bindings are measured against, quoted verbatim in
# the refusals so the operator is told which ones work rather than only that
# theirs does not.
_WINDOWS_CAPTURE_MEASURED_VERSIONS = "CPython 3.12, 3.13 or 3.14"


class WindowsTreeDrainPending(OSError):
    """An owned tree outlived one bounded drain pass and stays pinned for the sweep.

    Not a refusal and not a lost tree. Every exact handle stays in
    ``_PENDING_WINDOWS_TREE_CLEANUPS``, including members discovered too late in
    the pass to be signalled yet, and
    :func:`retry_pending_windows_process_trees` resumes the drain from the
    members this pass already confirmed. A slow host meets it routinely: each
    member's pass is two identity reads and two Toolhelp snapshots, so a large
    agent tree on a loaded machine can spend the whole budget observing exits
    that are already under way. It is an ``OSError`` so every caller that treats
    an unconfirmed drain as a failed kill keeps doing so; a caller that only
    reports the outcome can name it in one line instead of a traceback.
    """

    def __init__(self, *, root_pid: int, pending: int) -> None:
        super().__init__(
            "Windows process tree did not drain before the deadline "
            f"(root pid {root_pid}, {pending} member(s) pending cleanup; "
            "retained for the cleanup sweep)"
        )
        self.pending = pending


# Named by the path callers import it from, so a traceback, a pickle or any
# ``module.qualname`` rendering of the class says ``kiro_crew.platform_compat``.
WindowsTreeDrainPending.__module__ = "kiro_crew.platform_compat"


class _PendingWindowsTreeCleanup:
    """One reservation, from before physical spawn through verified retirement."""

    __slots__ = (
        "handles",
        "key",
        "lock",
        "retired",
        "root_pid",
        "signalled",
        "terminally_scanned",
        "manual_required",
        "raw_root_pin",
        "retire_app_tracking",
    )

    def __init__(
        self, root_handle: int = 0, identity: tuple[int, int, int | None] = (0, 0, None)
    ) -> None:
        self.root_pid = identity[0]
        self.key = identity[:2]
        self.handles = {self.root_pid: root_handle} if root_handle else {}
        self.terminally_scanned: set[int] = set()
        self.signalled: set[int] = set()
        self.lock = threading.Lock()
        self.retired = False
        self.manual_required = False
        # Only a subprocess.Handle (an int with Close/__del__), NEVER Popen or
        # its transport. Keeps the original pin on duplicate/identity failure.
        self.raw_root_pin: int | None = None
        self.retire_app_tracking = False


_PENDING_WINDOWS_TREE_CLEANUPS: dict[tuple[int, int], _PendingWindowsTreeCleanup] = {}
_PENDING_WINDOWS_TREE_CLEANUPS_LOCK = threading.RLock()
_WINDOWS_TREE_ADMISSIONS: set[_PendingWindowsTreeCleanup] = set()


def reserve_windows_tree_cleanup(
    key: tuple[int, int] | None = None,
) -> _PendingWindowsTreeCleanup:
    """Atomically admit a physical tree, or reuse its already charged identity."""
    with _PENDING_WINDOWS_TREE_CLEANUPS_LOCK:
        if key is not None:
            for state in _WINDOWS_TREE_ADMISSIONS:
                if state.key == key:
                    return state
                if state.root_pid == key[0] and state.key[1] == 0:
                    raise OSError("Windows original root identity is pending verification")
        manual = sorted(
            state.root_pid for state in _WINDOWS_TREE_ADMISSIONS if state.manual_required
        )
        if manual:
            # The refusal outlives the tree that caused it, so the text has to
            # carry the cause and the remedy: an operator meeting it hours later
            # sees only a failed session start, and the log line that named the
            # root has scrolled away. Naming every quarantined root is bounded by
            # the same root admission limit that bounds the registry.
            roots = ", ".join(str(pid) for pid in manual)
            raise WindowsCleanupCapacityError(
                "Windows session start refused: cleanup of the process tree rooted at "
                f"pid {roots} could not be accounted for, so its surviving processes must "
                "be ended by hand; starts resume after this gateway restarts"
            )
        if len(_WINDOWS_TREE_ADMISSIONS) >= _WINDOWS_CLEANUP_ROOT_LIMIT:
            raise WindowsCleanupCapacityError("Windows cleanup root capacity exhausted")
        state = _PendingWindowsTreeCleanup()
        if key is not None:
            state.key = key
        _WINDOWS_TREE_ADMISSIONS.add(state)
        return state


def release_windows_tree_reservation(state: _PendingWindowsTreeCleanup) -> None:
    """Refund only an empty reservation. Pins/manual debt cannot be discarded."""
    with _PENDING_WINDOWS_TREE_CLEANUPS_LOCK:
        if state.handles or state.raw_root_pin is not None or state.manual_required:
            return
        _WINDOWS_TREE_ADMISSIONS.discard(state)
        state.retired = True


def _manual_windows_tree(state: _PendingWindowsTreeCleanup) -> None:
    with _PENDING_WINDOWS_TREE_CLEANUPS_LOCK:
        if not state.manual_required:
            state.manual_required = True
            logger.error(
                "Windows cleanup manual-handling-required: root=%d retained=%d; "
                "new physical starts refused; this is not OS isolation",
                state.root_pid,
                len(state.handles),
            )


async def _bind_windows_cleanup_process(
    state: _PendingWindowsTreeCleanup, process: asyncio.subprocess.Process
) -> None:
    """Capture the original suspended child before any cancellable resume work.

    The exact handle is pinned into ``state.handles`` synchronously, before the
    single await, so a cancellation that lands while the identity is being read
    still leaves this child retained for maintenance rather than dropped.

    The identity read is the one step that goes off the loop. A child that has
    already exited reads back as exited-with-no-exit-FILETIME until the kernel
    publishes that value, and ``_windows_process_handle_identity`` polls for it
    for up to ``_WINDOWS_EXIT_FILETIME_TIMEOUT_SECS``. This runs during session
    start on a loop that is serving every other session, so a child that dies
    the instant it resumes must not stall them: the wait belongs on
    :func:`kiro_crew.executors.subprocess_executor`, the same pool the rest of
    this module's spawn-adjacent waits use.
    """
    state.root_pid = process.pid
    state.key = (process.pid, 0)
    transport = getattr(process, "_transport", None)
    popen = transport.get_extra_info("subprocess") if transport is not None else None
    raw = getattr(popen, "_handle", None)
    if isinstance(raw, int):
        state.raw_root_pin = raw
    handle = duplicate_asyncio_process_handle(process)
    if handle is None and isinstance(raw, int):
        # Borrow the original reference-counted Handle if duplication failed.
        # It must never be explicitly closed while Popen can still use it.
        handle = int(raw)
    elif handle is not None:
        state.raw_root_pin = None
    setattr(process, "_windows_cleanup_state", state)
    if handle is not None:
        state.handles[process.pid] = handle
        identity = await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(), _windows_process_handle_identity, handle
        )
        if identity is not None and identity[0] == process.pid:
            with _PENDING_WINDOWS_TREE_CLEANUPS_LOCK:
                state.key = identity[:2]
            return
    else:
        # An unsupported transport with no original pin has no retry authority.
        _manual_windows_tree(state)
    with _PENDING_WINDOWS_TREE_CLEANUPS_LOCK:
        _PENDING_WINDOWS_TREE_CLEANUPS[state.key] = state
    raise OSError("Windows original child pin/identity unavailable")


async def create_windows_cleanup_owned_process(factory: Any) -> asyncio.subprocess.Process:
    """Charge before spawning; cancellation waits for the child's ownership handoff.

    The factory is invoked only after reservation and is never cancelled by its
    waiter. An OS-spawn failure has no returned child; a returned child is pinned
    before the factory task settles, even when the caller has been cancelled.
    POSIX invokes the factory directly and has no cleanup admission policy.
    """
    if not IS_WINDOWS:
        return await factory()
    state = reserve_windows_tree_cleanup()

    async def launch() -> asyncio.subprocess.Process:
        process = await factory(windows_cleanup_owner=state)
        await _bind_windows_cleanup_process(state, process)
        return process

    creation = launch()
    try:
        task = asyncio.ensure_future(creation)
    except BaseException:
        creation.close()
        release_windows_tree_reservation(state)
        raise
    cancelled = False
    try:
        while True:
            try:
                process = await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                cancelled = True
                if task.done():
                    process = task.result()
                    break
    except BaseException:
        if state.handles:
            # Creation capture precedes transport initialization. No returned
            # Process is needed to retain this exact child for maintenance.
            with _PENDING_WINDOWS_TREE_CLEANUPS_LOCK:
                _PENDING_WINDOWS_TREE_CLEANUPS[state.key] = state
        release_windows_tree_reservation(state)
        raise
    if cancelled:
        # This child never reaches its caller; cleanup owns it independently.
        with contextlib.suppress(OSError):
            await terminate_windows_asyncio_tree(process)
        raise asyncio.CancelledError
    return process


async def finish_windows_cleanup_owned_spawn(factory: Any) -> Any:
    """Drain the resume worker before cancellation can race tree retirement."""
    if not IS_WINDOWS:
        return await factory()
    task = asyncio.ensure_future(factory())
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            cancelled = True
            if task.done():
                result = task.result()
                break
    if cancelled:
        raise asyncio.CancelledError
    return result


def _drain_windows_process_tree(state: _PendingWindowsTreeCleanup) -> bool:
    """Advance one exact-handle tree; retain every handle when the pass fails."""

    if state.manual_required:
        raise WindowsCleanupCapacityError("Windows tree requires manual handling")
    if state.key[1] == 0:
        identity = _windows_process_handle_identity(state.handles[state.root_pid])
        if identity is None or identity[0] != state.root_pid:
            raise OSError("Windows process-tree root identity unavailable")
        with _PENDING_WINDOWS_TREE_CLEANUPS_LOCK:
            if _PENDING_WINDOWS_TREE_CLEANUPS.get(state.key) is state:
                del _PENDING_WINDOWS_TREE_CLEANUPS[state.key]
            state.key = identity[:2]
            _PENDING_WINDOWS_TREE_CLEANUPS[state.key] = state
    deadline = time.monotonic() + _WINDOWS_TREE_REAP_TIMEOUT_SECS
    while True:
        for pid, handle in tuple(state.handles.items()):
            if pid in state.terminally_scanned:
                continue
            if time.monotonic() >= deadline:
                raise WindowsTreeDrainPending(
                    root_pid=state.root_pid,
                    pending=len(set(state.handles) - state.terminally_scanned),
                )
            before = _windows_process_handle_identity(handle)
            if before is None or before[0] != pid:
                raise OSError(f"Windows process-tree identity unreadable: {pid}")
            state.handles.update(descendant_termination_handles(pid, state.handles, handle))
            after = _windows_process_handle_identity(handle)
            if after is None or after[:2] != before[:2]:
                raise OSError(f"Windows process-tree identity changed or unreadable: {pid}")
            if before[2] is not None and after[2] is not None:
                state.terminally_scanned.add(pid)
            elif after[2] is None and pid not in state.signalled:
                terminate_process_handle(handle)
                state.signalled.add(pid)
        if state.terminally_scanned == set(state.handles):
            return True
        time.sleep(_WINDOWS_TREE_REAP_POLL_SECS)


def _advance_owned_windows_tree(
    state: _PendingWindowsTreeCleanup,
    *,
    try_only: bool = False,
) -> tuple[bool, bool]:
    """Return ``(drained, completed_here)`` and retire a verified handle set.

    A completed drain retires the tree's PID-file records and protected-PID
    shield BEFORE any handle is closed and while ``state.lock`` is still held, so
    the retirement runs while the root handle still pins the old incarnation — a
    recycled pid cannot slip in between the close and the untrack. A retirement
    that raises leaves the state un-retired and its handles open, so the receipt
    survives for the next tick rather than being lost with the handles.

    ``try_only`` acquires ``state.lock`` non-blockingly: a state already draining
    on another caller returns ``(False, False)`` untouched so a maintenance sweep
    rotates it behind its peers instead of blocking the whole pass on one busy
    tree. Caller-initiated cleanup leaves it False and keeps its serialization.
    """

    handles_to_close: tuple[int, ...] = ()
    completed_here = False
    if try_only:
        acquired = state.lock.acquire(blocking=False)
        if not acquired:
            return False, False
    else:
        state.lock.acquire()
    try:
        if state.retired:
            return True, False
        try:
            result = _drain_windows_process_tree(state)
        except _WindowsTreeOverflow:
            _manual_windows_tree(state)
            raise
        if result:
            try:
                # circular import: session_pid imports kiro_crew.platform_compat, and
                # with it this module, at its own top,
                # so the tracking retirement it owns can only be reached in-function.
                from kiro_crew.session_pid import retire_windows_tree_tracking

                retire_windows_tree_tracking(state.root_pid)
                if state.retire_app_tracking:
                    # circular import: apps.backend also imports kiro_crew.platform_compat
                    # at its top.
                    from kiro_crew.apps.backend import retire_windows_app_tracking

                    retire_windows_app_tracking(*state.key)
            except Exception:
                logger.warning(
                    "Windows exact-handle tree retirement failed for PID %d; "
                    "retaining pinned handles for retry",
                    state.root_pid,
                    exc_info=True,
                )
                return False, False
            state.retired = True
            completed_here = True
            handles_to_close = tuple(state.handles.values())
            # The state remains locked and charged until all pins close.
            for handle in handles_to_close:
                if state.raw_root_pin is None or handle != int(state.raw_root_pin):
                    close_process_handle(handle)
            state.handles.clear()
            state.signalled.clear()
            state.terminally_scanned.clear()
            state.raw_root_pin = None
            with _PENDING_WINDOWS_TREE_CLEANUPS_LOCK:
                if _PENDING_WINDOWS_TREE_CLEANUPS.get(state.key) is state:
                    del _PENDING_WINDOWS_TREE_CLEANUPS[state.key]
                release_windows_tree_reservation(state)
    finally:
        state.lock.release()
    return result, completed_here


def terminate_windows_process_tree_owned(
    root_handle: int,
    *,
    reservation: _PendingWindowsTreeCleanup | None = None,
) -> bool:
    """Drain an exact handle, retaining failed ownership for maintenance.

    An unadmitted caller at capacity retains ownership of its input handle;
    capacity refusal never closes it. PID-based callers reserve before opening.
    """
    if not IS_WINDOWS:
        close_process_handle(root_handle)
        raise ValueError("Windows process-tree termination requires Windows")
    identity = _windows_process_handle_identity(root_handle)
    if identity is None:
        # Even an unidentifiable imported handle must not be consumed at full
        # capacity. Its caller keeps ownership when admission is refused.
        empty = reserve_windows_tree_cleanup() if reservation is None else reservation
        if empty.handles:
            raise OSError("Windows process-tree root identity unavailable")
        close_process_handle(root_handle)
        release_windows_tree_reservation(empty)
        raise OSError("Windows process-tree root identity unavailable")
    key = identity[:2]
    with _PENDING_WINDOWS_TREE_CLEANUPS_LOCK:
        existing = next((item for item in _WINDOWS_TREE_ADMISSIONS if item.key == key), None)
        if existing is not None:
            if reservation is not None and reservation is not existing:
                release_windows_tree_reservation(reservation)
            state = existing
        else:
            state = reserve_windows_tree_cleanup(key) if reservation is None else reservation
        if state.key == (0, 0):
            state.key = key
    with state.lock:
        if state.retired:
            close_process_handle(root_handle)
            return state.root_pid > 0
        if not state.handles:
            state.root_pid = identity[0]
            state.key = key
            state.handles[state.root_pid] = root_handle
        elif state.key != key:
            raise ValueError("Windows cleanup reservation identity mismatch")
        elif state.handles[state.root_pid] != root_handle:
            close_process_handle(root_handle)
        with _PENDING_WINDOWS_TREE_CLEANUPS_LOCK:
            _PENDING_WINDOWS_TREE_CLEANUPS[key] = state
    drained, _ = _advance_owned_windows_tree(state)
    return drained


def retry_pending_windows_process_trees(
    limit: int = _PENDING_WINDOWS_TREE_RETRY_LIMIT,
) -> tuple[int, ...]:
    """Advance a finite, fair snapshot of process-owned failed Windows drains.

    Each selected tree gets one attempt. Failures remain visible and move behind
    their peers, so a permanently denied tree cannot starve a healthy one. A tree
    already draining on another caller is skipped non-blockingly and rotated
    behind its peers, so one busy entry cannot hold up the whole sweep. This is
    same-process maintenance, not crash recovery; it acquires no PID authority.
    Returns the exact root PIDs whose handle-verified drains completed AND whose
    tracking retirement succeeded.
    """
    if not IS_WINDOWS or type(limit) is not int or limit <= 0:
        return ()
    with _PENDING_WINDOWS_TREE_CLEANUPS_LOCK:
        selected = [
            (key, state)
            for key, state in _PENDING_WINDOWS_TREE_CLEANUPS.items()
            if not state.manual_required
        ][:limit]
    completed: list[int] = []
    for key, state in selected:
        try:
            _, completed_here = _advance_owned_windows_tree(state, try_only=True)
            if completed_here:
                completed.append(state.root_pid)
            else:
                # Busy trees and failed metadata writes both yield to peers.
                # Fully retired entries have already left the registry.
                with _PENDING_WINDOWS_TREE_CLEANUPS_LOCK:
                    if _PENDING_WINDOWS_TREE_CLEANUPS.get(key) is state:
                        del _PENDING_WINDOWS_TREE_CLEANUPS[key]
                        _PENDING_WINDOWS_TREE_CLEANUPS[key] = state
        except Exception as exc:
            logger.warning(
                "Windows exact-handle tree cleanup remains pending for PID %d (%s)",
                state.root_pid,
                exc,
            )
            with _PENDING_WINDOWS_TREE_CLEANUPS_LOCK:
                if _PENDING_WINDOWS_TREE_CLEANUPS.get(key) is state:
                    del _PENDING_WINDOWS_TREE_CLEANUPS[key]
                    _PENDING_WINDOWS_TREE_CLEANUPS[key] = state
    return tuple(completed)


async def terminate_windows_asyncio_tree(process: asyncio.subprocess.Process) -> bool:
    """Drain the original asyncio Windows process tree even after root exit.

    asyncio's transport retains the original Popen in its extra-info mapping.
    Duplicate that handle and transfer its ownership to process-local cleanup
    state before draining. The exact root and every observed intermediary then
    outlive provider/client GC after a failed pass. Repeated caller cancellation
    is re-delivered only after the current cleanup attempt settles.
    """
    candidate = getattr(process, "_windows_cleanup_state", None)
    admitted = isinstance(candidate, _PendingWindowsTreeCleanup)
    state: _PendingWindowsTreeCleanup
    handle: int | None
    if isinstance(candidate, _PendingWindowsTreeCleanup):
        state = candidate
        handle = None
        # Publish cleanup debt before executor submission: a rejected worker
        # must still leave the original pins reachable by maintenance after GC.
        with _PENDING_WINDOWS_TREE_CLEANUPS_LOCK:
            if not state.retired:
                _PENDING_WINDOWS_TREE_CLEANUPS[state.key] = state
    else:
        state = reserve_windows_tree_cleanup()
        handle = duplicate_asyncio_process_handle(process)
        if handle is None:
            release_windows_tree_reservation(state)
            raise OSError("Cannot retain the original Windows runtime process handle")

    def drain() -> bool:
        if admitted:
            result, _ = _advance_owned_windows_tree(state)
        else:
            assert handle is not None
            result = terminate_windows_process_tree_owned(handle, reservation=state)
        if not result:
            raise OSError("Windows tree tracking retirement did not complete")
        return result

    async def cleanup() -> bool:
        loop = asyncio.get_running_loop()
        try:
            worker = loop.run_in_executor(subprocess_executor(), drain)
        except BaseException:
            if handle is not None:
                # Executor submission never transferred this duplicate.
                close_process_handle(handle)
                release_windows_tree_reservation(state)
            raise
        result = await asyncio.shield(worker)
        await asyncio.wait_for(process.wait(), timeout=_WINDOWS_TREE_REAP_TIMEOUT_SECS)
        return result

    task = asyncio.ensure_future(cleanup())
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            cancelled = True
            if task.done():
                result = task.result()
                break
    if cancelled:
        raise asyncio.CancelledError
    return result
