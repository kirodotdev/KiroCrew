"""Whether a pid is alive, and which process group it is in.

The three-way liveness probe, thread-group leaders, zombies, process-group
membership and the thread count, answered without sending a signal that could act
on the target. Whether a whole process group is alive (``pgroup_exists``) is
answered by ``platform_compat_kill``, which holds every ``os.killpg`` call.

One of the owners ``kiro_crew.platform_compat`` is split into: every name here stays
readable and patchable as ``kiro_crew.platform_compat.<name>``.
"""

from __future__ import annotations

import ctypes.util
import os
import sys
from ctypes import wintypes

from kiro_crew.platform_compat_native import IS_LINUX, IS_POSIX
from kiro_crew.platform_compat_proctable import (
    _LINUX_EXITED_STATES,
    darwin_pid_is_zombie,
    read_proc_stat,
    read_proc_status_int,
)

# Tri-state liveness results for pid_liveness().
PID_DEAD = "dead"  # confirmed not running -> safe to prune
PID_ALIVE = "alive"  # confirmed running
PID_UNSIGNALABLE = "unsignalable"  # exists but we cannot signal it (POSIX EPERM)


def live_thread_group_leaders() -> frozenset[int] | None:
    """Every pid on the host that is a PROCESS, or ``None`` when unknowable.

    Linux numbers threads from the same space as processes and exposes
    ``/proc/<tid>`` for them, and POSIX permits signalling a tid — so a tid
    satisfies both :func:`pid_exists` and :func:`pid_liveness` while naming no
    process at all. A caller holding a recorded pid therefore cannot tell "my
    process is still alive" from "that number now belongs to some unrelated
    process's thread", which matters once the pid counter wraps
    (``/proc/sys/kernel/pid_max`` is commonly 4194304 and a busy host cycles it
    in hours).

    The discriminator is ``/proc`` itself: its top-level listing enumerates
    ONLY thread-group leaders. A non-leader tid is absent from that listing
    even though ``/proc/<tid>`` stays directly openable — which is exactly why
    the cheaper per-pid probes cannot see the difference.

    Deliberately ONE directory read for the whole host rather than a read per
    pid. A sweep over N recorded mappings costs a single ``os.listdir`` instead
    of N opens of ``/proc/<pid>/status`` (measured on Linux: 1.5 ms once versus
    7.8 ms across 233 mappings), so no per-entry synchronous file read happens
    on the caller's thread at all. Also cheaper than ``process_matches``, which
    shells out to ``ps`` on macOS and so cannot be used per entry in a sweep.

    Returns ``None`` — never an empty set — whenever the answer is not knowable
    (non-Linux, unreadable ``/proc``, or a listing with no numeric entries).
    Callers use this to *narrow* a liveness check, so an inconclusive result
    must never be the thing that decides a pid is stale: treat ``None`` as
    "retain everything".
    """
    if not IS_LINUX:
        return None
    try:
        entries = os.listdir("/proc")
    except OSError:
        return None
    leaders = {int(name) for name in entries if name.isdigit()}
    if not leaders:
        return None
    return frozenset(leaders)


def is_thread_group_leader(pid: int) -> bool | None:
    """Whether ``pid`` names a PROCESS right now, or ``None`` when unknowable.

    The per-pid counterpart to :func:`live_thread_group_leaders`, for the one
    question a host-wide snapshot cannot answer. A snapshot is a reading taken at
    an instant, so a pid recycled AFTER it was taken is absent from it while
    naming a live process. A caller about to act destructively on "absent from
    the snapshot" therefore needs a reading taken now, for that pid alone.

    ``/proc/<pid>/status`` carries ``Tgid``, the pid of the thread group's
    leader, so ``Tgid == pid`` is a process while a non-leader tid reports its
    leader's pid instead. One file read is the cheap way to ask about one pid,
    where the top-level ``/proc`` listing is the cheap way to ask about all of
    them -- which is why this narrows the snapshot rather than replacing it.

    Returns ``None`` -- never ``False`` -- whenever the answer is not knowable
    (non-Linux, the pid is gone, an unreadable or malformed ``status``), so an
    inconclusive read can never be the thing that licenses a destructive action.
    """
    if not IS_LINUX:
        return None
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.startswith("Tgid:"):
                    return int(line.split()[1]) == pid
    except (OSError, ValueError, IndexError):
        return None
    return None


def pid_liveness(pid: int) -> str:
    """Three-way liveness probe: PID_DEAD / PID_ALIVE / PID_UNSIGNALABLE.

    Unlike :func:`pid_exists` (which collapses ALIVE and UNSIGNALABLE into
    ``True``), this distinguishes "exists but owned by another user / can't be
    signalled" (POSIX ``EPERM``) from "running and ours". Callers that must
    LEAVE an unsignalable PID untouched (the orphan sweep — pruning or killing
    a PID we merely can't signal is wrong) branch on ``PID_UNSIGNALABLE``.

    POSIX: ``os.kill(pid, 0)`` — ``ProcessLookupError`` -> DEAD,
    ``PermissionError`` or an out-of-range PID -> UNSIGNALABLE, success ->
    ALIVE.
    Windows: no EPERM distinction for our processes; map ``pid_exists`` onto
    DEAD/ALIVE (UNSIGNALABLE never returned).
    """
    if IS_POSIX:
        try:
            os.kill(pid, 0)
            return PID_ALIVE
        except ProcessLookupError:
            return PID_DEAD
        except PermissionError:
            return PID_UNSIGNALABLE
        except OverflowError:
            # The PID may have come from corrupt persistent state.  It is not
            # evidence that the named process is dead, so preserve fail-closed
            # callers by classifying it as unknown/unsignalable.
            return PID_UNSIGNALABLE
        except OSError:
            # Unknown errno — be conservative and treat as unsignalable
            # (leave it alone) rather than risk pruning/killing a live PID.
            return PID_UNSIGNALABLE
    return PID_ALIVE if pid_exists(pid) else PID_DEAD


def pgroup_of(pid: int) -> int | None:
    """The process GROUP *pid* belongs to, or ``None`` when it cannot be read.

    Distinct from :func:`pgroup_of_leader`, which answers "what group does this
    LEADER name" and falls back to the pid itself so a reaped leader's group can
    still be signalled. This one asks a plain membership question about a pid that
    may be any descendant, so there is no leader contract to fall back on and an
    unreadable answer is ``None`` rather than a guess.

    Callers use it to tell an in-group descendant (a group kill covers it) from one
    that has ``setsid``'d out of the group (it has to be signalled on its own), so
    a wrong guess here either signals a stranger or leaves a live writer behind --
    which is why the failure answers "unknown".

    POSIX ONLY, deliberately unguarded: ``os.getpgid`` does not exist on Windows,
    and a caller reaching here on that platform has the wrong primitive.
    """
    try:
        return os.getpgid(pid)
    except OSError:
        return None


def pgroup_of_leader(pid: int) -> int | None:
    """Resolve the process GROUP to signal for a group leader ``pid``.

    Companion to :func:`pgroup_exists` for the teardown side: a caller that must
    signal a group needs its id even when the leader itself has been reaped,
    because the group outlives its leader and the children left in it keep
    running. ``os.getpgid`` cannot name such a group -- it raises
    ``ProcessLookupError`` once the leader is gone -- and reading that as "the
    group is gone" signals nothing at all while the tree survives.

    So a reaped leader resolves to ``pid`` itself: for a child spawned with
    ``start_new_session=True`` the leader's pid IS the group id (the same
    identity :func:`pgroup_exists` documents), so that number still names the
    group. ``killpg`` on a group that really is empty raises
    ``ProcessLookupError``, which callers already absorb, so the fallback costs
    nothing when the group is gone and is the whole teardown when it is not.

    Returns:
        The group id, or ``None`` when the group cannot be resolved because
        signalling it is denied (pid recycled to another user, or reduced
        privilege) -- a caller must not proceed to signal on ``None``.

    POSIX ONLY, deliberately unguarded: ``os.getpgid`` does not exist on
    Windows, so a caller reaching here on Windows has the wrong teardown
    primitive and gets an ``AttributeError`` saying so rather than a silent
    no-op. Windows teardown goes through ``kill_process_tree``.
    """
    try:
        return os.getpgid(pid)
    except ProcessLookupError:
        return pid
    except PermissionError:
        return None


def pid_exists(pid: int) -> bool:
    """Return True iff ``pid`` currently exists (best-effort).

    POSIX: ``os.kill(pid, 0)``.
    Windows: ``OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)``.
    """
    if IS_POSIX:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except (PermissionError, OSError, OverflowError):
            return True  # exists but we can't signal it
    try:
        _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000  # noqa: N806 — Windows API constant
        _STILL_ACTIVE = 259  # noqa: N806 — Windows STILL_ACTIVE exit code
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            # OpenProcess SUCCEEDS for an EXITED process as long as any handle to
            # the kernel process object is still open — and asyncio's Proactor
            # transport keeps its duplicated handle open until GC, so a
            # just-killed child we awaited would read back as "exists". Confirm
            # with GetExitCodeProcess: STILL_ACTIVE means genuinely running;
            # any other code means it has exited (a defunct handle to a dead
            # PID), so report not-exists. Without this every Windows session
            # recycle logged a false "PID survived kill" and left dead PIDs in
            # the tracker until the periodic sweep.
            try:
                code = wintypes.DWORD()
                got = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
                if got and code.value != _STILL_ACTIVE:
                    return False
                return True
            finally:
                kernel32.CloseHandle(handle)
        return getattr(ctypes, "get_last_error", lambda: 0)() == 5  # ERROR_ACCESS_DENIED → exists
    except Exception:
        return False


def pid_is_zombie(pid: int) -> bool | None:
    """Whether *pid* has exited and only waits to be reaped: True / False, None when unreadable.

    ``pid_exists`` answers True for a zombie (``os.kill(pid, 0)`` reaches it),
    so a caller asking "is this process still RUNNING" -- a survivor check
    after a signal, where the signalled process sits in the zombie state until
    its parent, or init, collects it -- needs this beside it. Linux reads the
    state field of ``/proc/<pid>/stat`` (``Z``, or ``X`` for one being torn
    down); macOS asks the kernel (:func:`darwin_pid_is_zombie`); elsewhere, and
    for a process that cannot be read, None -- the caller decides what
    "unknown" means for it. Never signals anything.
    """
    if pid <= 0:
        return None
    if sys.platform == "linux":
        stat = read_proc_stat(pid)
        if stat is None or stat.state is None:
            return None
        return stat.state in _LINUX_EXITED_STATES
    if sys.platform == "darwin":
        return darwin_pid_is_zombie(pid)
    return None


def process_thread_count(pid: int) -> int | None:
    """Thread count of *pid*, or ``None`` when it cannot be determined.

    It tells a *running* gateway (dozens of threads: the event loop, the
    executor pool, watchdogs, MCP stdio readers) apart from a **wedged fork** of
    one -- a child forked before ``exec`` inherits exactly one thread, the one
    that called ``fork()``, and never gains another. That single-thread signature
    is the decidable half of "this holder is an orphan, not a gateway".

    Linux: ``Threads:`` in ``/proc/<pid>/status``. Everywhere else: ``None``, so
    every caller must already handle "unknown" and degrade to a claim it can
    support. Deliberately no ``ps`` fallback for macOS -- shelling out from this
    low-level module would add an unrouted subprocess spawn (see
    ``test_spawn_audit``) to a purely diagnostic path.
    """
    if sys.platform != "linux":
        return None
    return read_proc_status_int(pid, "Threads")
