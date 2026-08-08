"""Cross-platform compatibility shims for POSIX-only APIs.

All helpers are safe no-ops (or best-effort fallbacks) on Windows where
the underlying syscall does not exist. Callers should use these instead
of raw ``os.*`` / ``fcntl`` / ``signal`` calls for anything that is
POSIX-only.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes.util
import io
import logging
import os
import shutil
import signal
import stat
import struct
import subprocess
import sys
import time
import zlib
from ctypes import wintypes  # type aliases only; imports cleanly on every platform
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional

from kiro_crew.executors import subprocess_executor

logger = logging.getLogger(__name__)

IS_WINDOWS: bool = sys.platform == "win32"
IS_POSIX: bool = not IS_WINDOWS
IS_LINUX: bool = sys.platform == "linux"
IS_MACOS: bool = sys.platform == "darwin"

# Portable signal constants — signal.SIGKILL is undefined on Windows.
SIGKILL: int = getattr(signal, "SIGKILL", 9)

# Our own process group, captured at import time (POSIX; 0 on Windows where
# os.getpgid doesn't exist). Used by kill_process_tree's broadcast guard so
# the self-check is stable and immune to test-time os.getpgid patching.
_OWN_PGID: int = os.getpgid(0) if hasattr(os, "getpgid") else 0
SIGTERM: int = getattr(signal, "SIGTERM", 15)

# Portable subprocess creation flags — these constants exist ONLY on Windows
# (the subprocess module has no such attributes on POSIX). Referencing
# ``subprocess.CREATE_NEW_PROCESS_GROUP`` directly fails mypy's ``[attr-defined]``
# check on the Linux build fleet even when guarded by ``if IS_WINDOWS:`` (mypy
# resolves attributes statically, ignoring the runtime guard). Expose them via
# ``getattr`` so the names resolve to 0 on POSIX (where they are never used) and
# to the real flags on Windows. Mirrors the ``SIGKILL`` pattern above.
CREATE_NEW_PROCESS_GROUP: int = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
DETACHED_PROCESS: int = getattr(subprocess, "DETACHED_PROCESS", 0)
# For the short-lived helper tools this module shells out to on Windows
# (whoami / netstat / taskkill / icacls / powershell): a console-less parent
# (gateway respawned with DETACHED_PROCESS, or pythonw) would otherwise
# allocate a NEW visible console per spawn — a black-window flash on the
# user's desktop for every status poll / secret write / kill. 0 on POSIX.
_SUBPROCESS_NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Spawn process-group isolation (for clean tree-kill later): pass these TWO
# keyword args EXPLICITLY to subprocess.Popen / asyncio.create_subprocess_exec —
#     start_new_session=platform_compat.IS_POSIX,
#     creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
# Do NOT build a dict and ``**unpack`` it into the spawn call: that defeats
# mypy's Popen overload resolution on the build fleet ("no overload variant
# matches"). On POSIX start_new_session=True calls setsid (so killpg reaps the
# group) and creationflags=0 is a no-op; on Windows there is no setsid
# (start_new_session is silently ignored) and CREATE_NEW_PROCESS_GROUP makes the
# child tree taskkill /T-reapable. Add DETACHED_PROCESS to the flags for a
# fully detached, console-less child (e.g. the gateway respawn).

# ── macOS TCC-protected home subdirectories ──
# macOS gates these home subdirectories behind TCC (Transparency, Consent and
# Control). The FIRST read of any one of them by a given app triggers a modal
# "…would like to access files in your Downloads folder" prompt, and consent is
# recorded PER (app, folder) pair — so incidentally touching three of them
# during one operation produces THREE separate prompts, not one.
#
# Nothing KiroCrew does at startup needs these folders: they are only ever
# reached INCIDENTALLY, by a breadth-first walk that was rooted at $HOME as a
# catch-all fallback (the @-mention file picker's search root). Pruning them
# from such unscoped walks removes the prompts entirely, which is strictly
# better than pre-declaring NS*FolderUsageDescription strings — those change
# the prompt's wording but still prompt, once per folder.
#
# This does NOT restrict a user's EXPLICIT navigation: an operation whose root
# the user named (a project dir, or a browse request for ~/Downloads itself) is
# scoped by definition and never consults this set. macOS still shows its own
# one-time prompt for that deliberate access, which is the expected contract.
#
# Names only (no leading path): matched against a single path component so the
# same set works for both os.walk dirname pruning and scandir entry filtering.
TCC_PROTECTED_HOME_DIRS: frozenset[str] = frozenset(
    {
        "Downloads",
        "Documents",
        "Desktop",
        "Pictures",
        "Movies",
        "Music",
    }
)
# ``Library`` is deliberately absent from the set ABOVE, but it is not
# unpruned — see TCC_LIBRARY_WALKABLE_CHILDREN. It cannot be a plain member
# here because it must be *descended into* to reach the cloud-drive mounts,
# which a top-level name prune would make unreachable.

#: Children of ``~/Library`` that stay walkable; every other child is pruned
#: from a home-rooted walk. This is an ALLOWLIST on purpose.
#:
#: Much of ``~/Library`` is gated behind Full Disk Access — ``Mail``,
#: ``Messages``, ``Safari``, ``Calendars``, ``HomeKit``, ``Cookies``,
#: ``IdentityServices``, ``Suggestions``, ``PersonalizationPortrait``,
#: ``Metadata/CoreSpotlight``, ``Containers/com.apple.*`` and several
#: ``Application Support`` leaves (``AddressBook``, ``CallHistoryDB``,
#: ``MobileSync``, ``com.apple.TCC``) — and Apple keeps ADDING to that list
#: with each release. A denylist would therefore go stale and silently start
#: leaking prompts again on the next macOS version, so the rule is inverted:
#: name the two paths worth reaching and drop the rest.
#:
#: The two kept entries are the modern cloud-drive mount points, which are
#: common project homes and hold real search hits:
#: ``~/Library/CloudStorage/<Provider>/`` (OneDrive / Google Drive / Dropbox)
#: and ``~/Library/Mobile Documents/`` (iCloud Drive).
TCC_LIBRARY_WALKABLE_CHILDREN: frozenset[str] = frozenset(
    {
        "CloudStorage",
        "Mobile Documents",
    }
)

#: The single ``~/Library`` component name, kept as a constant because the walk
#: pruner compares it positionally rather than by membership.
_LIBRARY_DIR = "Library"


def tcc_protected_dirs_for_walk(root: str | os.PathLike) -> frozenset[str]:
    """Return the TCC-protected dir names to prune when walking *root*.

    Empty off macOS (no TCC), and empty unless *root* is the user's home
    directory itself: a walk the user explicitly scoped to ``~/Downloads`` (or
    to a project that happens to live under it) must still see its own
    contents. Only the incidental ``$HOME``-as-fallback walk is pruned.

    Callers MUST pass the same ``str`` they hand to :func:`os.walk` — the
    returned names are compared against ``os.walk``'s ``dirpath``, which is
    byte-identical to the ``top`` argument it was given.

    On any resolution failure this returns the empty set, i.e. it prunes
    nothing. That degrades to today's behavior (a prompt may appear) rather
    than silently hiding a directory the caller asked for.
    """
    if not IS_MACOS:
        return frozenset()
    try:
        if os.path.realpath(root) != os.path.realpath(os.path.expanduser("~")):
            return frozenset()
    except (OSError, ValueError):
        # OSError: EACCES / ELOOP / ENAMETOOLONG on an exotic path.
        # ValueError: realpath() rejects a path containing a null byte — NOT an
        # OSError subclass, so it would otherwise escape to the caller and 500
        # the /api/file-search request (same class as agent.py's guard).
        return frozenset()
    return TCC_PROTECTED_HOME_DIRS


def tcc_prune_walk_dirs(root: str, dirpath: str, dirnames: list[str]) -> list[str]:
    """Return *dirnames* minus the TCC-gated entries for this walk position.

    Single entry point for ``os.walk`` pruning: call it with the ``top`` passed
    to :func:`os.walk` plus the ``dirpath``/``dirnames`` of the current step and
    assign the result back into ``dirnames[:]``.

    Two positions prune, and only when *root* is the user's home directory
    itself (see :func:`tcc_protected_dirs_for_walk` for why an explicitly
    scoped root is never pruned):

    * at *root* — drop the gated top-level folders (``Downloads``, ``Desktop``,
      ...). ``Library`` is NOT dropped here, so the walk can reach the cloud
      mounts below it.
    * at ``<root>/Library`` — keep only TCC_LIBRARY_WALKABLE_CHILDREN and drop
      every other child, most of which is Full-Disk-Access gated.

    Every deeper position returns *dirnames* untouched. A name that merely
    matches a gated folder further down the tree (a project's own
    ``Documents/``) is not gated and stays walkable.

    Callers MUST pass the same ``str`` they hand to :func:`os.walk`: the
    positional comparisons rely on ``dirpath`` being built by joining onto
    ``top`` verbatim, which is what ``os.walk`` guarantees.
    """
    if not IS_MACOS:
        return dirnames
    # Positional gate FIRST: only two walk positions can prune, so every other
    # directory in a large tree returns without paying the realpath syscall
    # that the home check below costs.
    at_root = dirpath == root
    at_library = not at_root and dirpath == os.path.join(root, _LIBRARY_DIR)
    if not (at_root or at_library):
        return dirnames
    # Doubles as the "is root the home directory" test and inherits that
    # helper's failure handling (an unresolvable root prunes nothing).
    protected = tcc_protected_dirs_for_walk(root)
    if not protected:
        return dirnames
    if at_root:
        return [d for d in dirnames if d not in protected]
    return [d for d in dirnames if d in TCC_LIBRARY_WALKABLE_CHILDREN]


def ensure_utf8_console() -> None:
    """Make stdout/stderr UTF-8 on Windows so KiroCrew's emoji output can't crash it.

    KiroCrew prints non-ASCII glyphs throughout its CLI/gateway output. On
    Windows the default console code page is cp1252, and when stdout is a pipe
    (e.g. the gateway launched detached with redirected output, or under the
    KiroCrewHub client) Python encodes prints as cp1252 — so the FIRST non-ASCII
    print raises ``UnicodeEncodeError: 'charmap' codec can't encode character``
    and the process dies before the gateway binds. POSIX defaults to UTF-8, so
    this is a no-op there. Best-effort: reconfigure (Python 3.7+) the streams to
    UTF-8 with backslashreplace so a stray un-encodable char degrades to an
    escape instead of crashing. Idempotent and safe to call once at startup.
    """
    if not IS_WINDOWS:
        return
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:  # pythonw / fully detached — no stream to fix
            continue
        # Preferred: reconfigure in place (Python 3.7+ TextIOWrapper). Works for a
        # normal console or a redirect-to-file when the stream is a TextIOWrapper.
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
            if (getattr(stream, "encoding", "") or "").lower().startswith("utf-8"):
                continue
        except (AttributeError, ValueError, OSError):
            # reconfigure() is absent or refused (e.g. the stream got replaced
            # with a plain object somewhere up a multi-process launch chain —
            # observed in the 3-layer Windows gateway spawn: kirocrew.exe launcher
            # -> venv python stub -> base python worker, where the worker's stderr
            # is NOT a reconfigure-able TextIOWrapper, so emoji log records crash
            # the stderr StreamHandler with UnicodeEncodeError under cp1252).
            pass
        # Fallback: wrap the underlying binary buffer in a fresh UTF-8 writer so
        # the encoding is guaranteed regardless of the original stream type.
        buffer = getattr(stream, "buffer", None)
        if buffer is None:
            continue
        try:
            setattr(
                sys,
                name,
                io.TextIOWrapper(
                    buffer, encoding="utf-8", errors="backslashreplace", line_buffering=True
                ),
            )
        except (AttributeError, ValueError, OSError):
            pass


# ---------------------------------------------------------------------------
# File locking
# ---------------------------------------------------------------------------

if IS_POSIX:
    import fcntl  # noqa: F401 — re-exported
    import resource  # noqa: F401 — POSIX-only; used by the resource shims below
else:
    import msvcrt  # type: ignore[import-not-found]


# msvcrt's blocking lock codes (LK_LOCK / LK_RLCK) are NOT the equivalent of
# fcntl.flock(LOCK_EX): rather than waiting until the lock is free, they retry
# ~10 times at 1s intervals and then RAISE EDEADLOCK (errno 36). Swallowing
# that as "acquired" lets a caller run its read-modify-write with no exclusion
# and silently lose writes. So the Windows "blocking" acquire spins on the
# non-blocking code (LK_NBLCK) instead — the same idiom cron._file_lock uses.
# It is bounded rather than truly unbounded because a contended fd and a
# non-writable fd are indistinguishable on Windows (both surface as errno 13
# EACCES), so an unbounded spin would turn a permission error into a hang.
#
# Two ceilings, because on-loop and off-loop have opposite needs:
#  - OFF the loop (cron, home migration, app backends — threads/subprocesses):
#    the wait must cover a legitimately long holder. home_migration holds the
#    lock across a full copy+verify+delete of the data home, which can exceed
#    many seconds, and a waiter there must NOT give up and race it. So use a
#    generous ceiling that no real hold approaches, matching POSIX's "wait for
#    the lock" as closely as a bounded spin can.
#  - ON the loop (e.g. bridges._mcp_lock during app enable): a spin-sleep would
#    freeze chat/heartbeat, so that path never sleeps at all (single-shot).
_WIN_LOCK_POLL_SECS = 0.01
# Generous off-loop ceiling: longer than any legitimate hold (a large data-home
# migration), short enough that a truly stuck/permission-denied fd still fails.
_WIN_LOCK_TIMEOUT_SECS = 300.0


def _win_acquire_blocking(fd: int, *, timeout: float = _WIN_LOCK_TIMEOUT_SECS) -> bool:
    """Windows blocking lock acquire: spin on LK_NBLCK until free or timeout.

    Returns True if the lock was taken, False if it could not be.

    NEVER spins on the asyncio event-loop thread: ``time.sleep`` there would
    freeze chat/heartbeat for the whole wait. A few callers still take the lock
    on the loop (e.g. bridges._mcp_lock during app enable), so when a running
    loop is detected the acquire is single-shot — take it if free, else return
    False at once — and the caller fails closed rather than stalling the loop.
    Off the loop (the common case) it polls up to ``timeout`` as a real
    blocking wait, so a legitimately long holder (a data-home migration) is
    waited out rather than raced.
    """

    def _try_once() -> bool:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
            return True
        except OSError:
            return False

    try:
        asyncio.get_running_loop()
        on_loop = True
    except RuntimeError:
        on_loop = False
    if on_loop:
        # Single attempt only — a spin-sleep here blocks the event loop.
        return _try_once()

    deadline = time.monotonic() + timeout
    while True:
        if _try_once():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_WIN_LOCK_POLL_SECS)


@contextlib.contextmanager
def file_lock(
    fd: int,
    *,
    exclusive: bool = True,
    required: bool = False,
) -> Iterator[None]:
    """Acquire an advisory lock on ``fd`` for the duration of the block.

    POSIX: ``fcntl.flock(LOCK_EX|LOCK_SH)`` with ``LOCK_UN`` release.
    Windows: ``msvcrt.locking`` on the first byte, acquired by spinning on the
    non-blocking code up to ``_WIN_LOCK_TIMEOUT_SECS`` — because msvcrt's own
    "blocking" code gives up after ~10s with EDEADLOCK, which cannot be treated
    as a wait. ``msvcrt`` has no shared mode, so a shared request is satisfied
    with an exclusive lock (correctness over concurrency — readers genuinely
    serialize with the holder, but never see torn writes).

    On Windows the acquire is single-shot when called on the asyncio event-loop
    thread (a spin-sleep there would freeze chat/heartbeat) and a bounded poll
    up to the timeout otherwise; either way, if the lock cannot be taken
    ``file_lock`` FAILS CLOSED — it raises rather than entering the critical
    section unserialized, since proceeding lock-less is the exact fail-open that
    loses writes. The timeout is a safety ceiling against a stuck holder, not a
    normal wait (every in-tree critical section is a sub-second read + atomic
    rename). ``required`` is retained for call-site intent but no longer changes
    the outcome (both paths refuse to proceed without the lock). On POSIX the
    acquire blocks until the lock is free, as before.

    Note: on Windows, ``msvcrt.locking`` requires seeking to byte 0, so the
    ``fd`` must be a dedicated lock file; callers must not rely on the file
    offset being preserved across the context manager boundary.
    """
    if IS_POSIX:
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(fd, mode)
        try:
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
    else:
        # Fail CLOSED, not open: if the lock cannot be taken within the ceiling
        # (a stuck/crashed holder — never a normal sub-second hold), raise rather
        # than enter the critical section unserialized. Entering anyway is the
        # exact fail-open that loses writes; a loud error in that rare case is
        # strictly safer, and callers already run under `with`, so the fd is
        # cleaned up. `required` is retained for call-site intent but no longer
        # changes the outcome — both paths now refuse to proceed lock-less.
        if not _win_acquire_blocking(fd):
            raise OSError(
                f"could not acquire exclusive file lock within {_WIN_LOCK_TIMEOUT_SECS:g}s "
                "(a holder is stuck); refusing to proceed unserialized"
            )
        try:
            yield
        finally:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
            except OSError:
                pass


@contextlib.contextmanager
def flock_exclusive(fd: int) -> Iterator[None]:
    """Acquire an exclusive advisory lock on ``fd`` (see :func:`file_lock`)."""
    with file_lock(fd, exclusive=True):
        yield


def acquire_lock(fd: int, *, exclusive: bool = True) -> None:
    """Low-level lock acquire for the acquire-now / release-later fd-handoff
    pattern (where a context manager does not fit).

    POSIX: ``fcntl.flock`` (blocks until free). Windows: single-shot on the
    asyncio loop thread, else a bounded poll up to ``_WIN_LOCK_TIMEOUT_SECS``
    (see :func:`_win_acquire_blocking`). If the lock cannot be taken it FAILS
    CLOSED — raises rather than letting the caller proceed unserialized — since
    a stuck holder past the ceiling is an error, not a routine wait, and
    proceeding lock-less is the fail-open that loses writes. Pair every call
    with :func:`release_lock` on the same ``fd``.
    """
    if IS_POSIX:
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        return
    if not _win_acquire_blocking(fd):
        raise OSError(
            f"could not acquire file lock within {_WIN_LOCK_TIMEOUT_SECS:g}s "
            "(a holder is stuck); refusing to proceed unserialized"
        )


def release_lock(fd: int) -> None:
    """Release a lock acquired via :func:`acquire_lock` / :func:`try_acquire_lock`."""
    if IS_POSIX:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        return
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
    except OSError:
        pass


def try_acquire_lock(fd: int, *, exclusive: bool = False) -> bool:
    """Attempt a non-blocking lock acquire. Returns True iff the lock was taken.

    POSIX: ``fcntl.flock(... | LOCK_NB)``. Windows: ``msvcrt.locking`` with the
    non-blocking codes. On success, the caller must :func:`release_lock` the fd.
    """
    if IS_POSIX:
        mode = (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
        try:
            fcntl.flock(fd, mode)
            return True
        except (BlockingIOError, OSError):
            return False
    # msvcrt has no shared lock; LK_NBLCK is a non-blocking exclusive lock.
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Win32 struct layouts
# ---------------------------------------------------------------------------
# These MUST stay at module scope, never inside the functions that use them.
# ``ctypes.POINTER(T)`` memoises T -> POINTER(T) in a module-level dict inside
# ctypes and never evicts it, so a Structure subclass declared in a function
# body pins a BRAND-NEW pair of type objects on every call. The helpers below
# are polled (the dashboard's system metrics, the RSS-recycle watchdog, the
# tree-kill parent-map walk, the MCP pipe's per-connection peer check), which
# turns that into unbounded growth in a long-lived gateway. Declared once here,
# the memo holds a single entry for the process lifetime.
#
# ``wintypes`` supplies type aliases only, so these definitions import cleanly
# on POSIX; the functions below still resolve the DLLs lazily, which is what
# keeps them patchable from the non-Windows test fleet.


class _ProcessEntry32(ctypes.Structure):
    """Toolhelp ``PROCESSENTRY32`` — process-enumeration snapshot entry."""

    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_char * 260),
    ]


class _ProcessMemoryCounters(ctypes.Structure):
    """psapi ``PROCESS_MEMORY_COUNTERS`` — per-process working set."""

    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


class _MemoryStatusEx(ctypes.Structure):
    """kernel32 ``MEMORYSTATUSEX`` — system-wide physical memory."""

    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class _SidAndAttributes(ctypes.Structure):
    """advapi32 ``SID_AND_ATTRIBUTES``."""

    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class _TokenUser(ctypes.Structure):
    """advapi32 ``TOKEN_USER`` — the ``TokenUser`` information-class payload."""

    _fields_ = [("User", _SidAndAttributes)]


# ---------------------------------------------------------------------------
# Process termination / existence
# ---------------------------------------------------------------------------


def get_ppid(pid: int) -> int:
    """Return the parent PID of *pid*, or ``-1`` on failure.

    Linux: ``/proc/<pid>/status``.
    macOS: ``libproc.proc_pidinfo`` (no entitlement required).
    Windows: ``CreateToolhelp32Snapshot``.
    """
    if sys.platform == "linux":
        try:
            for ln in Path(f"/proc/{pid}/status").read_text().splitlines():
                if ln.startswith("PPid:"):
                    return int(ln.split()[1])
        except Exception:
            pass
        return -1
    if sys.platform == "darwin":
        try:
            path = ctypes.util.find_library("proc")
            if path is None:
                return -1
            lib = ctypes.CDLL(path)
            lib.proc_pidinfo.argtypes = [
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_uint64,
                ctypes.c_void_p,
                ctypes.c_int,
            ]
            lib.proc_pidinfo.restype = ctypes.c_int
            buf = ctypes.create_string_buffer(136)
            ret = lib.proc_pidinfo(pid, 3, 0, buf, 136)  # PROC_PIDTBSDINFO=3
            if ret <= 0:
                return -1
            return struct.unpack_from("<I", buf.raw, 16)[0]
        except Exception:
            return -1
    if IS_WINDOWS:
        try:

            TH32CS_SNAPPROCESS = 0x00000002  # noqa: N806 — Windows API constant
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            entry_ptr = ctypes.POINTER(_ProcessEntry32)

            kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
            kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
            kernel32.Process32First.argtypes = [wintypes.HANDLE, entry_ptr]
            kernel32.Process32First.restype = wintypes.BOOL
            kernel32.Process32Next.argtypes = [wintypes.HANDLE, entry_ptr]
            kernel32.Process32Next.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL

            snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
            if snap == wintypes.HANDLE(-1).value:
                return -1
            try:
                entry = _ProcessEntry32()
                entry.dwSize = ctypes.sizeof(_ProcessEntry32)
                if not kernel32.Process32First(snap, ctypes.byref(entry)):
                    return -1
                while True:
                    if entry.th32ProcessID == pid:
                        return entry.th32ParentProcessID
                    if not kernel32.Process32Next(snap, ctypes.byref(entry)):
                        return -1
            finally:
                kernel32.CloseHandle(snap)
        except Exception:
            return -1
    return -1


# macOS ``struct proc_bsdinfo`` (PROC_PIDTBSDINFO, 136 bytes) field offsets used
# below. Verified empirically against ``ps -o lstart=`` on darwin: ``pbi_ppid``
# at 16 (see get_ppid), ``pbi_start_tvsec`` at 120, ``pbi_start_tvusec`` at 128.
_DARWIN_BSDINFO_SIZE = 136
_DARWIN_OFF_START_TVSEC = 120
_DARWIN_OFF_START_TVUSEC = 128


def get_process_start_id(pid: int) -> str | None:
    """Return a stable per-process start-time identity string, or ``None``.

    Two processes that reuse the same PID at different times get DIFFERENT
    values, so callers can tell "still the process I spawned" from "this PID was
    recycled". The value is stable for the whole lifetime of a process and is
    safe to persist and compare from a *different* process (unlike a builtin
    ``hash()``, which is PYTHONHASHSEED-randomized per interpreter).

    Never contains ``:``, so callers may embed it in colon-delimited records.

    Implementation is deliberately **in-process and non-blocking** on every
    platform — no ``subprocess``/fork — so it is safe to call directly from the
    asyncio event loop:

    - Linux: ``/proc/<pid>/stat`` field 22 (starttime in clock ticks since boot).
    - macOS: ``libproc.proc_pidinfo`` ``pbi_start_tvsec``/``pbi_start_tvusec``
      (microsecond resolution, so processes spawned in the same second do not
      alias — unlike ``ps -o lstart=``, which is 1-second granularity).
    - Windows / any failure (including a process we may not introspect): ``None``,
      meaning "identity unknown" — callers must not treat that as a mismatch.
    """
    if sys.platform == "linux":
        try:
            stat_data = Path(f"/proc/{pid}/stat").read_text()
            # comm (field 2) may contain spaces/parens — parse after the LAST ')'
            close_paren = stat_data.rfind(")")
            if close_paren < 0:
                return None
            return stat_data[close_paren + 2 :].split()[19]
        except Exception:
            return None
    if sys.platform == "darwin":
        try:
            path = ctypes.util.find_library("proc")
            if path is None:
                return None
            lib = ctypes.CDLL(path)
            lib.proc_pidinfo.argtypes = [
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_uint64,
                ctypes.c_void_p,
                ctypes.c_int,
            ]
            lib.proc_pidinfo.restype = ctypes.c_int
            buf = ctypes.create_string_buffer(_DARWIN_BSDINFO_SIZE)
            ret = lib.proc_pidinfo(pid, 3, 0, buf, _DARWIN_BSDINFO_SIZE)  # PROC_PIDTBSDINFO=3
            if ret <= 0:
                return None
            sec = struct.unpack_from("<Q", buf.raw, _DARWIN_OFF_START_TVSEC)[0]
            usec = struct.unpack_from("<Q", buf.raw, _DARWIN_OFF_START_TVUSEC)[0]
            if sec == 0:
                return None  # implausible — treat as unknown rather than a value
            return f"{sec}.{usec:06d}"
        except Exception:
            return None
    return None


def _descendants_from_parent_map(root_pid: int, parent_map: dict[int, int]) -> list[int]:
    """Return a breadth-first descendant list from a PID -> PPID snapshot."""

    result: list[int] = []
    frontier = [root_pid]
    seen = {root_pid}
    while frontier:
        parents = set(frontier)
        frontier = []
        for child_pid, parent_pid in parent_map.items():
            if parent_pid in parents and child_pid not in seen:
                seen.add(child_pid)
                result.append(child_pid)
                frontier.append(child_pid)
    return result


_TRUSTED_SYSTEM_BIN_DIRS = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")

# Windows argv carries a bare name (``taskkill``) while the file on disk carries
# an extension (``taskkill.exe``), so a trusted lookup must try the suffixes the
# loader would rather than requiring callers to spell them.
_WINDOWS_BIN_SUFFIXES = ("", ".exe", ".com")


def _windows_system_dirs() -> tuple[str, ...]:
    """Return the Windows directories a system binary may be resolved from.

    ``GetSystemDirectoryW`` is the authoritative source and, unlike
    ``%SystemRoot%``, is not read from the process environment — which is
    precisely the input this module declines to trust. The environment variable
    and the conventional install path follow only as fallbacks for the
    unexpected case where the API call fails. PowerShell ships in a versioned
    directory beside the system binaries, not inside it, so it is appended per
    root rather than assumed to sit alongside ``taskkill``.
    """

    dirs: list[str] = []
    try:
        buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
        written = ctypes.windll.kernel32.GetSystemDirectoryW(  # type: ignore[attr-defined]
            buf, len(buf)
        )
        if 0 < written < len(buf):
            dirs.append(buf.value)
    except Exception:
        pass
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    # Case-insensitive dedupe: GetSystemDirectoryW reports the on-disk casing
    # ("C:\Windows\system32"), which names the same directory as the
    # conventionally-cased fallback and must not be probed twice.
    seen = {d.casefold() for d in dirs}
    for fallback in (os.path.join(root, "System32"), r"C:\Windows\System32"):
        if fallback.casefold() not in seen:
            seen.add(fallback.casefold())
            dirs.append(fallback)
    return tuple(dirs) + tuple(os.path.join(d, "WindowsPowerShell", "v1.0") for d in dirs)


# Names already probed for the diagnostic below, so the message costs one PATH
# scan per name per process. Only the *message* is one-shot; resolution itself
# stays uncached, so a tool that lands in a trusted directory later is still
# found on the next call.
_UNPINNED_TOOL_PROBED: set[str] = set()


def _log_tool_outside_trusted_dirs(name: str, directories: tuple[str, ...]) -> None:
    """Log once per *name* when the pin is what made a present tool unavailable.

    A host that keeps its binaries outside the FHS system directories (NixOS's
    ``/run/current-system/sw/bin``, a Homebrew or conda prefix) has a working
    ``lsof`` that this lookup still declines, and the caller's degradation is
    otherwise indistinguishable from the tool not being installed:
    ``listening_pid_tool_available()`` would tell such an operator to install a
    tool they already have, and ``kirocrew stop`` would quietly no-op. The
    ``PATH`` result is read to write the message and is never spawned, so
    reporting it does not widen what may run.
    """

    if name in _UNPINNED_TOOL_PROBED:
        return
    # Concurrent first probes of one name can duplicate the line. That is
    # cheaper than serializing a filesystem scan behind a lock for a message.
    _UNPINNED_TOOL_PROBED.add(name)
    on_path = shutil.which(name)
    if not on_path:
        return
    logger.warning(
        "%s is on PATH at %s but does not resolve under the trusted system "
        "directories (%s), so it is treated as unavailable; OS introspection "
        "that needs it degrades instead of running a PATH-chosen binary",
        name,
        on_path,
        ", ".join(directories),
    )


def trusted_system_bin(name: str) -> str | None:
    """Resolve *name* from fixed system directories, ignoring ``PATH``.

    A gateway's ``PATH`` can legitimately lead with agent-writable directories
    (a worktree venv's ``bin``, ``~/.local/bin``), so a bare argv name lets a
    planted shim run with the gateway's environment. Callers that shell out for
    OS introspection resolve through here and treat ``None`` as "unavailable".

    A miss on a host whose tools live elsewhere is a real functional
    degradation, so it is logged once per name rather than left silent. The pin
    still decides; the log only makes the decision diagnosable.

    Deliberately uncached. The lookup is a handful of ``stat`` calls on
    teardown and introspection paths, and caching the *miss* would pin "tool
    absent" for the lifetime of a long-lived gateway, so an ``lsof`` installed
    after boot would never be picked up.
    """

    if IS_WINDOWS:
        directories: tuple[str, ...] = _windows_system_dirs()
        suffixes: tuple[str, ...] = _WINDOWS_BIN_SUFFIXES
    else:
        directories = _TRUSTED_SYSTEM_BIN_DIRS
        suffixes = ("",)
    for directory in directories:
        for suffix in suffixes:
            candidate = os.path.join(directory, name + suffix)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    _log_tool_outside_trusted_dirs(name, directories)
    return None


def tool_outside_trusted_dirs(name: str) -> str | None:
    """Where ``PATH`` finds *name* when :func:`trusted_system_bin` declined it.

    ``None`` means the pin is not the reason the tool is unavailable: either it
    resolved normally, or it is not installed anywhere ``PATH`` can see. Callers
    use this to word a diagnostic — an operator on a host that keeps its
    binaries elsewhere needs to be told where theirs actually is, not to install
    a tool they already have. The path is reported and never spawned, so asking
    does not widen what may run.
    """

    if trusted_system_bin(name) is not None:
        return None
    return shutil.which(name)


def _posix_process_parent_map() -> dict[int, int]:
    """Return one ``ps`` PID -> PPID snapshot; empty when enumeration fails."""

    if IS_WINDOWS:
        return {}
    ps_bin = trusted_system_bin("ps")
    if ps_bin is None:
        return {}
    try:
        out = subprocess.check_output(
            [ps_bin, "-Ao", "pid=,ppid="], timeout=5, stderr=subprocess.DEVNULL
        ).decode(errors="replace")
    except (OSError, subprocess.SubprocessError):
        return {}
    parent_map: dict[int, int] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            parent_map[int(parts[0])] = int(parts[1])
        except ValueError:
            continue
    return parent_map


def process_descendants(pid: int) -> list[int]:
    """Return *pid*'s descendants, breadth-first, from a single OS snapshot.

    Best-effort: an unreadable process table yields an empty list rather than
    raising, so callers using this to broaden a kill still perform their
    primary kill.

    Snapshot BEFORE killing anything. A kill reparents surviving orphans to
    init, erasing the PPID links that identify them, so a post-kill snapshot
    cannot find the very processes a caller needs to clean up.
    """

    if type(pid) is not int or pid <= 1:
        return []
    try:
        parent_map = (
            _windows_process_parent_map() if IS_WINDOWS else _posix_process_parent_map()
        )
    except Exception:  # noqa: BLE001 - introspection must never break a kill path
        return []
    return _descendants_from_parent_map(pid, parent_map)


def _windows_process_parent_map() -> dict[int, int]:
    """Return one Toolhelp PID -> PPID snapshot, raising if enumeration fails."""

    if not IS_WINDOWS:
        return {}
    try:
        th32cs_snapprocess = 0x00000002
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

        entry_ptr = ctypes.POINTER(_ProcessEntry32)

        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Process32First.argtypes = [wintypes.HANDLE, entry_ptr]
        kernel32.Process32First.restype = wintypes.BOOL
        kernel32.Process32Next.argtypes = [wintypes.HANDLE, entry_ptr]
        kernel32.Process32Next.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        set_last_error = getattr(kernel32, "SetLastError", None)
        get_last_error = getattr(kernel32, "GetLastError", None)

        snapshot = kernel32.CreateToolhelp32Snapshot(th32cs_snapprocess, 0)
        if snapshot == wintypes.HANDLE(-1).value:
            raise OSError("Windows process snapshot creation failed")
        try:
            entry = _ProcessEntry32()
            entry.dwSize = ctypes.sizeof(_ProcessEntry32)
            if not kernel32.Process32First(snapshot, ctypes.byref(entry)):
                raise OSError("Windows first process enumeration failed")
            result: dict[int, int] = {}
            while True:
                result[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                if callable(set_last_error):
                    set_last_error(0)
                else:
                    ctypes_set_last_error = getattr(ctypes, "set_last_error", None)
                    if callable(ctypes_set_last_error):
                        ctypes_set_last_error(0)
                if not kernel32.Process32Next(snapshot, ctypes.byref(entry)):
                    error = (
                        int(get_last_error()) if callable(get_last_error) else _windows_last_error()
                    )
                    if error not in (0, 18):  # ERROR_NO_MORE_FILES
                        raise OSError(error, "Windows process enumeration failed")
                    return result
        finally:
            kernel32.CloseHandle(snapshot)
    except OSError:
        raise
    except Exception as exc:
        raise OSError("Windows process enumeration failed") from exc


def _open_process_termination_handle(pid: int) -> int | None:
    """Open an identity-stable Windows handle suitable for later termination."""

    if not IS_WINDOWS:
        return None
    try:
        process_terminate = 0x0001
        process_query_limited_information = 0x1000
        synchronize = 0x00100000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(
            process_terminate | process_query_limited_information | synchronize,
            False,
            pid,
        )
        return int(handle) if handle else None
    except Exception:
        return None


def duplicate_asyncio_process_handle(process: object) -> int | None:
    """Duplicate asyncio's original Windows process handle for tree anchoring."""

    if not IS_WINDOWS:
        return None
    try:
        transport = getattr(process, "_transport", None)
        get_extra_info = getattr(transport, "get_extra_info", None)
        popen = get_extra_info("subprocess") if callable(get_extra_info) else None
        source_value = int(getattr(popen, "_handle", 0))
        if source_value <= 0:
            return None

        duplicate_same_access = 0x00000002
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.DuplicateHandle.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.HANDLE),
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.DuplicateHandle.restype = wintypes.BOOL
        owner = kernel32.GetCurrentProcess()
        duplicate = wintypes.HANDLE()
        if not kernel32.DuplicateHandle(
            owner,
            wintypes.HANDLE(source_value),
            owner,
            ctypes.byref(duplicate),
            0,
            False,
            duplicate_same_access,
        ):
            return None
        return int(duplicate.value) if duplicate.value else None
    except Exception:
        return None


def _windows_last_error() -> int:
    """Return ctypes' thread-local Win32 error without POSIX stub assumptions."""

    getter = getattr(ctypes, "get_last_error", None)
    return int(getter()) if callable(getter) else 0


# Bounds for the exited-but-exit-FILETIME-unpublished window (see
# _windows_process_handle_identity). The window closes within a few tens of
# milliseconds; the ceiling is generous enough to absorb a loaded host without
# letting a genuinely unreadable handle stall a caller.
_WINDOWS_EXIT_FILETIME_TIMEOUT_SECS = 0.25
_WINDOWS_EXIT_FILETIME_POLL_SECS = 0.002


def _windows_process_handle_identity(handle: int) -> tuple[int, int, int | None] | None:
    """Return ``(pid, creation_time, exit_time)`` for an exact process handle."""

    if not IS_WINDOWS or type(handle) is not int or handle <= 0:
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.GetProcessId.argtypes = [wintypes.HANDLE]
        kernel32.GetProcessId.restype = wintypes.DWORD
        kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        process_handle = wintypes.HANDLE(handle)
        pid = int(kernel32.GetProcessId(process_handle))
        creation = wintypes.FILETIME()
        exit_ = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        exit_code = wintypes.DWORD()

        def _read_times() -> bool:
            return bool(
                kernel32.GetProcessTimes(
                    process_handle,
                    ctypes.byref(creation),
                    ctypes.byref(exit_),
                    ctypes.byref(kernel),
                    ctypes.byref(user),
                )
            )

        if (
            pid <= 1
            or not _read_times()
            or not kernel32.GetExitCodeProcess(
                process_handle,
                ctypes.byref(exit_code),
            )
        ):
            return None
        still_active = 259
        active = exit_code.value == still_active
        # The exit FILETIME is not defined for a live process. If the status
        # says the process exited, read the times again after that observation
        # so the returned exit bound belongs to the terminated object.
        if not active and not _read_times():
            return None

        def _filetime_value(value: "wintypes.FILETIME") -> int:
            return (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)

        creation_value = _filetime_value(creation)
        exit_value = _filetime_value(exit_)
        # GetExitCodeProcess reports the exit BEFORE the kernel publishes the
        # exit FILETIME, so a just-terminated process reads back as
        # exited-with-exit_time==0 for a brief window (sub-millisecond to a few
        # tens of milliseconds). Treating that window as "no identity" makes the
        # caller reject a perfectly good handle, so poll briefly for the real
        # value. The bound stays short because the only alternative to a
        # published exit time is refusing the handle.
        if not active and exit_value <= 0:
            deadline = time.monotonic() + _WINDOWS_EXIT_FILETIME_TIMEOUT_SECS
            while exit_value <= 0 and time.monotonic() < deadline:
                time.sleep(_WINDOWS_EXIT_FILETIME_POLL_SECS)
                if not _read_times():
                    return None
                exit_value = _filetime_value(exit_)
        if creation_value <= 0 or (not active and exit_value <= 0):
            return None
        return pid, creation_value, None if active else exit_value
    except Exception:
        return None


def _windows_lineage_matches_lifetimes(
    child_pid: int,
    root_pid: int,
    parent_map: Mapping[int, int],
    identities: Mapping[int, tuple[int, int, int | None]],
) -> bool:
    """Bind numeric Toolhelp ancestry to the exact handles' lifetimes."""

    current = child_pid
    seen: set[int] = set()
    while current != root_pid:
        if current in seen:
            return False
        seen.add(current)
        parent_pid = parent_map.get(current)
        child_identity = identities.get(current)
        parent_identity = identities.get(parent_pid) if parent_pid is not None else None
        if (
            parent_pid is None
            or child_identity is None
            or parent_identity is None
            or child_identity[0] != current
            or parent_identity[0] != parent_pid
        ):
            return False
        child_created = child_identity[1]
        parent_created = parent_identity[1]
        parent_exited = parent_identity[2]
        if child_created < parent_created:
            return False
        if parent_exited is not None and child_created >= parent_exited:
            return False
        current = parent_pid
    return True


def descendant_termination_handles(
    pid: int,
    retained_handles: Mapping[int, int] | None = None,
    root_handle: int | None = None,
) -> dict[int, int]:
    """Return exact Windows process handles for newly observed descendants.

    Toolhelp exposes numeric parent PIDs, which can be recycled as soon as a
    process exits. Every edge is therefore checked against creation/exit times
    from exact root, retained-parent, and newly-opened child handles in two
    snapshots. This admits a genuine child created before an immediate launcher
    exit while rejecting a tree attached to a recycled root or intermediate PID.
    """

    if type(pid) is not int or pid <= 1:
        raise ValueError(f"descendant_termination_handles: refusing non-int/reserved pid {pid!r}")
    if not IS_WINDOWS:
        return {}
    if type(root_handle) is not int or root_handle <= 0:
        raise ValueError("descendant_termination_handles: exact root handle required")
    retained = dict(retained_handles or {})
    root_identity = _windows_process_handle_identity(root_handle)
    if root_identity is None or root_identity[0] != pid:
        raise ValueError("descendant_termination_handles: root handle identity mismatch")

    first_map = _windows_process_parent_map()
    first = set(_descendants_from_parent_map(pid, first_map))
    opened: dict[int, int] = {}
    for child_pid in sorted(first - set(retained)):
        handle = _open_process_termination_handle(child_pid)
        if handle is not None:
            opened[child_pid] = handle
    if not opened:
        return {}

    try:
        handles = {**retained, **opened, pid: root_handle}
        first_identities = {
            process_pid: identity
            for process_pid, handle in handles.items()
            if (identity := _windows_process_handle_identity(handle)) is not None
        }
        eligible = {
            child_pid
            for child_pid in opened
            if child_pid in first
            and _windows_lineage_matches_lifetimes(
                child_pid,
                pid,
                first_map,
                first_identities,
            )
        }

        second_map = _windows_process_parent_map()
        still_descendants = set(_descendants_from_parent_map(pid, second_map))
        second_identities = {
            process_pid: identity
            for process_pid, handle in handles.items()
            if (identity := _windows_process_handle_identity(handle)) is not None
        }
        for child_pid in tuple(opened):
            if (
                child_pid not in eligible
                or child_pid not in still_descendants
                or not _windows_lineage_matches_lifetimes(
                    child_pid,
                    pid,
                    second_map,
                    second_identities,
                )
            ):
                close_process_handle(opened.pop(child_pid))
        return opened
    except Exception:
        for handle in opened.values():
            close_process_handle(handle)
        raise


def terminate_process_handle(handle: int) -> bool:
    """Terminate the exact Windows process object referenced by *handle*."""

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
    process_handle = wintypes.HANDLE(handle)
    exit_code = wintypes.DWORD()
    still_active = 259
    if not kernel32.GetExitCodeProcess(process_handle, ctypes.byref(exit_code)):
        raise OSError(_windows_last_error(), "GetExitCodeProcess failed")
    if exit_code.value != still_active:
        return False
    if not kernel32.TerminateProcess(process_handle, 1):
        raise OSError(_windows_last_error(), "TerminateProcess failed")
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


def close_process_handle(handle: int) -> None:
    """Close a handle returned by :func:`descendant_termination_handles`."""

    if not IS_WINDOWS or type(handle) is not int or handle <= 0:
        return
    with contextlib.suppress(Exception):
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(wintypes.HANDLE(handle))


def process_matches(pid: int, needles: tuple[str, ...]) -> bool:
    """Return True iff *pid*'s command line / image name contains any *needle*.

    Used to guard against PID recycling before killing a tracked process.
    Linux: ``/proc/<pid>/cmdline``. macOS: ``ps -o command=``.
    Windows: the image name from ``CreateToolhelp32Snapshot`` (full command
    line is not cheaply available; the ``.exe`` name suffices for matching
    ``kiro-cli`` / ``claude``). Returns False on any failure.
    """
    try:
        if sys.platform == "linux":
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
            return any(n.encode() in cmdline for n in needles)
        if sys.platform == "darwin":
            ps_bin = trusted_system_bin("ps")
            if ps_bin is None:
                return False
            out = subprocess.check_output(
                [ps_bin, "-o", "command=", "-p", str(pid)],
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
            return any(n.encode() in out for n in needles)
        if IS_WINDOWS:
            name = _win_process_image_name(pid)
            if name is None:
                return False
            low = name.lower()
            return any(n.lower() in low for n in needles)
    except Exception:
        return False
    return False


def _win_process_image_name(pid: int) -> str | None:
    """Return the image (exe) name for *pid* on Windows, or None on failure."""
    try:

        TH32CS_SNAPPROCESS = 0x00000002  # noqa: N806 — Windows API constant
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        entry_ptr = ctypes.POINTER(_ProcessEntry32)

        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Process32First.argtypes = [wintypes.HANDLE, entry_ptr]
        kernel32.Process32First.restype = wintypes.BOOL
        kernel32.Process32Next.argtypes = [wintypes.HANDLE, entry_ptr]
        kernel32.Process32Next.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap == wintypes.HANDLE(-1).value:
            return None
        try:
            entry = _ProcessEntry32()
            entry.dwSize = ctypes.sizeof(_ProcessEntry32)
            if not kernel32.Process32First(snap, ctypes.byref(entry)):
                return None
            while True:
                if entry.th32ProcessID == pid:
                    return entry.szExeFile.decode(errors="replace")
                if not kernel32.Process32Next(snap, ctypes.byref(entry)):
                    return None
        finally:
            kernel32.CloseHandle(snap)
    except Exception:
        return None


def listening_pid_tool() -> str:
    """Return the external tool find_listening_pids relies on: 'lsof' / 'netstat'."""
    return "netstat" if IS_WINDOWS else "lsof"


def listening_pid_tool_available() -> bool:
    """Whether the port->PID lookup tool (lsof on POSIX / netstat on Windows) is
    resolvable. Lets callers distinguish "tool absent" from "no listener found",
    which find_listening_pids() alone collapses into an empty list — without that
    a genuinely-running gateway reads as stopped when lsof is missing.

    Resolves through :func:`trusted_system_bin`, the same lookup
    :func:`find_listening_pids` performs. Probing ``PATH`` here instead would
    let the two disagree: a shim on ``PATH`` would answer "available" for a tool
    the pinned lookup refuses to run, turning a live gateway into one that reads
    as stopped — the exact failure this probe exists to prevent. A tool that is
    installed but outside those directories therefore reads as absent, which
    :func:`trusted_system_bin` logs so the answer can be explained.
    """
    return trusted_system_bin(listening_pid_tool()) is not None


def find_listening_pids(port: int) -> list[int]:
    """Return PIDs with a LISTEN socket on TCP *port* (best-effort, deduped).

    POSIX: ``lsof -ti TCP:<port> -sTCP:LISTEN``.
    Windows: parse ``netstat -ano`` (no lsof; netstat ships in-box). Matches
    rows whose local address ends in ``:<port>`` and whose state is LISTENING,
    taking the PID from the last column. Returns ``[]`` on any failure (caller
    treats "no PID found" as "nothing to stop"; use listening_pid_tool_available()
    to tell a genuine empty result apart from the tool being absent).
    """
    if IS_POSIX:
        lsof_bin = trusted_system_bin("lsof")
        if lsof_bin is None:
            return []
        try:
            out = subprocess.check_output(
                [lsof_bin, "-ti", f"TCP:{port}", "-sTCP:LISTEN"],
                text=True,
                stderr=subprocess.DEVNULL,
            )
        except (FileNotFoundError, subprocess.CalledProcessError, OSError):
            return []
        return list(dict.fromkeys(int(p) for p in out.split() if p.strip().isdigit()))
    # Windows: netstat -ano. Lines look like:
    #   TCP    127.0.0.1:7777         0.0.0.0:0    LISTENING    17152   (IPv4)
    #   TCP    [::1]:7777             [::]:0       LISTENING    17152   (IPv6)
    # No `-p tcp`: that flag restricts output to IPv4 TCP on Windows and
    # silently drops IPv6 listeners entirely, so `kirocrew stop` /
    # `kirocrew restart` no-op on a dual-stack or `[::]`-bound gateway
    # Windows netstat labels IPv6 rows with proto column
    # "TCP" too — the bracketed address form is what distinguishes v4 vs
    # v6, not the proto token — so once `-p tcp` is dropped the existing
    # port suffix match already handles both families uniformly.
    netstat_bin = trusted_system_bin("netstat")
    if netstat_bin is None:
        return []
    try:
        out = subprocess.check_output(
            [netstat_bin, "-ano"],
            # encoding="oem" (Windows-only pseudo-codec): netstat emits the
            # console OEM codepage when piped; text=True would decode with the
            # ANSI codepage and can raise UnicodeDecodeError on non-Western
            # locales — which, as a ValueError, would escape a
            # (SubprocessError, OSError) net and crash stop/status instead of
            # degrading to []. errors="replace" belts the rest.
            encoding="oem",
            errors="replace",
            stderr=subprocess.DEVNULL,
            timeout=10,
            creationflags=_SUBPROCESS_NO_WINDOW,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError, ValueError):
        return []
    suffix = f":{port}"
    pids: list[int] = []
    for line in out.splitlines():
        parts = line.split()
        # Expect: proto local foreign state pid.
        # startswith("TCP") not `== "TCP"` for defensive future-proofing:
        # today Windows netstat prints plain "TCP" for both families, but a
        # future Windows build could relabel IPv6 rows as "TCP6" (the netstat
        # -p flag already accepts "tcpv6"). UDP rows never carry a LISTEN
        # state, so the listener check below is the load-bearing filter for
        # the non-TCP case, but keeping the proto guard avoids feeding
        # malformed ICMPv6 / RAW lines into the port match.
        if len(parts) < 5 or not parts[0].upper().startswith("TCP"):
            continue
        # Listener detection via the FOREIGN address, not the state column:
        # netstat localizes state names ("ABHÖREN" on German Windows,
        # Cyrillic on Russian), so matching the English "LISTENING" literal
        # returns [] on any non-English locale and stop/restart silently
        # no-op. A TCP row whose foreign endpoint is the wildcard 0.0.0.0:0 /
        # [::]:0 is in LISTEN state by definition, locale-independently.
        # Accept the English literal too as a defensive second signal.
        if parts[2] not in ("0.0.0.0:0", "[::]:0") and parts[3].upper() != "LISTENING":
            continue
        local = parts[1]
        # Suffix-match the local-address port. Formats: A.B.C.D:port for
        # TCP4 and [::]:port / [::1]:port / [fe80::...]:port for TCP6.
        # ']' is never a digit, so no false-positive against a bracketed
        # suffix (e.g. [::1234]:7 would not endswith ":234").
        if not local.endswith(suffix):
            continue
        pid_str = parts[-1]
        if pid_str.isdigit():
            pids.append(int(pid_str))
    # Dedup: a dual-stack listener appears in BOTH a TCP4 row (0.0.0.0:port)
    # and a TCP6 row ([::]:port) under the same PID — collapse to one entry.
    return list(dict.fromkeys(pids))


def process_command_line(pid: int) -> str:
    """Return the full command line of *pid*, or ``""`` on failure (best-effort).

    Linux: ``/proc/<pid>/cmdline`` (NUL-joined → spaces).
    macOS: ``ps -o command= -p <pid>``.
    Windows: ``Win32_Process.CommandLine`` via WMI (PowerShell ``Get-CimInstance``).
    Used to confirm a listener PID is actually a KiroCrew gateway when the image
    name alone is ambiguous (the venv ``kirocrew.exe`` re-execs ``python.exe``).
    """
    try:
        if sys.platform == "linux":
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
            return raw.replace(b"\x00", b" ").decode(errors="replace").strip()
        if sys.platform == "darwin":
            ps_bin = trusted_system_bin("ps")
            if ps_bin is None:
                return ""
            out = subprocess.check_output(
                [ps_bin, "-o", "command=", "-p", str(pid)],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
            return out.strip()
        if IS_WINDOWS:
            # Query WMI for the exact PID's command line. PowerShell is always
            # present on supported Windows; -NoProfile keeps it fast.
            powershell_bin = trusted_system_bin("powershell")
            if powershell_bin is None:
                return ""
            out = subprocess.check_output(
                [
                    powershell_bin,
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}')"
                    ".CommandLine",
                ],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=10,
                creationflags=_SUBPROCESS_NO_WINDOW,
            )
            return out.strip()
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""
    return ""


def process_owner_uid(pid: int) -> int | None:
    """Return the uid owning *pid*, or ``None`` when it cannot be determined.

    Linux: ``os.stat("/proc/<pid>").st_uid``.
    macOS: ``ps -o uid= -p <pid>``.
    Windows: ``None`` — there is no uid concept, and a WMI ``GetOwner`` round
    trip costs a PowerShell spawn per call; callers that need an ownership gate
    must decide what to do with ``None`` explicitly rather than assume a match.

    Used to confirm that a pid a client is about to trust belongs to the calling
    user (see ``cli_server._gateway_owns_port``), which is what makes pid
    recycling into a *foreign* user's process non-exploitable.
    """
    try:
        if sys.platform == "linux":
            return os.stat(f"/proc/{int(pid)}").st_uid
        if sys.platform == "darwin":
            # An unresolvable ``ps`` yields None, which ``_gateway_owns_port``
            # treats as "ownership unproven" and denies on — the same direction
            # as every other failure in that gate.
            ps_bin = trusted_system_bin("ps")
            if ps_bin is None:
                return None
            out = subprocess.check_output(
                [ps_bin, "-o", "uid=", "-p", str(int(pid))],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
            raw = out.strip()
            return int(raw) if raw.isdigit() else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return None


# Tri-state liveness results for pid_liveness().
PID_DEAD = "dead"  # confirmed not running -> safe to prune
PID_ALIVE = "alive"  # confirmed running
PID_UNSIGNALABLE = "unsignalable"  # exists but we cannot signal it (POSIX EPERM)


def pid_liveness(pid: int) -> str:
    """Three-way liveness probe: PID_DEAD / PID_ALIVE / PID_UNSIGNALABLE.

    Unlike :func:`pid_exists` (which collapses ALIVE and UNSIGNALABLE into
    ``True``), this distinguishes "exists but owned by another user / can't be
    signalled" (POSIX ``EPERM``) from "running and ours". Callers that must
    LEAVE an unsignalable PID untouched (the orphan sweep — pruning or killing
    a PID we merely can't signal is wrong) branch on ``PID_UNSIGNALABLE``.

    POSIX: ``os.kill(pid, 0)`` — ``ProcessLookupError`` -> DEAD,
    ``PermissionError`` -> UNSIGNALABLE, success -> ALIVE.
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
        except OSError:
            # Unknown errno — be conservative and treat as unsignalable
            # (leave it alone) rather than risk pruning/killing a live PID.
            return PID_UNSIGNALABLE
    return PID_ALIVE if pid_exists(pid) else PID_DEAD


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
        except (PermissionError, OSError):
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


def process_thread_count(pid: int) -> int | None:
    """Thread count of *pid*, or ``None`` when it cannot be determined.

    Used to tell a *running* gateway (dozens of threads: the event loop, the
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
    try:
        for ln in Path(f"/proc/{pid}/status").read_text().splitlines():
            if ln.startswith("Threads:"):
                return int(ln.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def flock_owner_pid(path: str | os.PathLike) -> int | None:
    """PID recorded against an ``flock`` on *path*, via ``/proc/locks``.

    This is the pid that ACQUIRED the lock, which is not always a live process:
    an ``flock`` belongs to the open file description, so when the acquirer dies
    and a forked child still holds the inherited fd, the kernel keeps reporting
    the DEAD acquirer here. Verified on Linux 5.x -- an orphaned lock listed
    ``FLOCK ADVISORY WRITE <dead parent pid>`` while the surviving child held the
    fd. So a returned pid that is dead is positive evidence of an inherited fd,
    and no ``/proc`` surface names the inheritor: use
    :func:`pids_holding_file` for candidates and do not present them as owners.

    Returns ``None`` on non-Linux, when ``/proc/locks`` is unreadable, or when no
    ``FLOCK`` entry matches the file. Blocked waiters (``->`` rows) are skipped.

    Matching is on the full ``major:minor:inode`` triple that ``/proc/locks``
    prints, because inode numbers are only unique WITHIN a filesystem -- an
    unrelated flock on another device can share this inode's number, and
    accepting it would name a completely unrelated process. The device halves are
    hex (``%02x``), the inode decimal (``%lu``). On filesystems whose ``s_dev``
    differs from the ``st_dev`` that ``stat`` reports (btrfs subvolumes and
    overlayfs use anonymous devices) the triple will not match and this returns
    ``None``. That is the safe direction to fail: the caller degrades to its
    "holder could not be identified" wording instead of naming a wrong process.
    """
    if sys.platform != "linux":
        return None
    try:
        info = os.stat(path)
    except OSError:
        return None
    want = (os.major(info.st_dev), os.minor(info.st_dev), info.st_ino)
    try:
        # Streamed, not slurped: /proc/locks is unbounded (one row per lock
        # system-wide) and this runs on a startup failure path.
        with open("/proc/locks", encoding="utf-8") as handle:
            for row in handle:
                fields = row.split()
                # "23: FLOCK ADVISORY WRITE 39542 103:01:146872 0 EOF"; a blocked
                # waiter is "23: -> FLOCK ..." and owns nothing.
                if len(fields) < 6 or "->" in fields[:2] or fields[1] != "FLOCK":
                    continue
                try:
                    pid = int(fields[4])
                    major, minor, ino = fields[5].split(":")
                    found = (int(major, 16), int(minor, 16), int(ino))
                except (ValueError, IndexError):
                    continue
                if found == want:
                    return pid
    except OSError:
        return None
    return None


def parent_pid(pid: int) -> int | None:
    """PPID of *pid* from ``/proc/<pid>/stat``, or ``None`` if unknowable.

    Used to corroborate that a candidate process really is orphaned: a child
    reparented to init after its parent died reports ``1``. ``None`` means
    "unknown", which callers must not read as either answer.
    """
    if sys.platform != "linux":
        return None
    try:
        stat_data = Path(f"/proc/{pid}/stat").read_text()
        # comm (field 2) may contain spaces/parens -- parse after the LAST ')'
        close_paren = stat_data.rfind(")")
        if close_paren < 0:
            return None
        return int(stat_data[close_paren + 2 :].split()[1])
    except Exception:
        return None


def pids_holding_file(path: str | os.PathLike) -> list[int] | None:
    """PIDs with an open fd on *path*, matched by inode via ``/proc/*/fd``.

    These are CANDIDATE OPENERS, not lock owners. Any process may open the file
    without locking it, and an inherited ``flock`` has no live owner to identify
    (see :func:`flock_owner_pid`), so callers must not present a pid from here as
    the holder. Answering "who has this file open" is still the only way to reach
    the process that inherited a dead acquirer's descriptor.

    Matching is by ``(st_dev, st_ino)`` rather than by link target so a bind
    mount (a jailed gateway sees a different path for the same inode) still
    resolves. PIDs whose ``/proc`` entry we cannot read are skipped: the result
    is a best-effort lower bound.

    Returns ``None`` on non-Linux, where there is no ``/proc`` to walk.
    """
    if sys.platform != "linux":
        return None
    try:
        target = os.stat(path)
    except OSError:
        return None
    key = (target.st_dev, target.st_ino)
    holders: list[int] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return None
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        fd_dir = f"/proc/{entry}/fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            # Dead between listdir and here, or another user's process.
            continue
        for fd_name in fds:
            try:
                st = os.stat(f"{fd_dir}/{fd_name}")
            except OSError:
                continue
            if (st.st_dev, st.st_ino) == key:
                holders.append(pid)
                break
    return holders


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


async def descendant_termination_handles_async(
    pid: int,
    retained_handles: Mapping[int, int] | None = None,
    root_handle: int | None = None,
) -> dict[int, int]:
    """Async variant of :func:`descendant_termination_handles`."""

    if not IS_WINDOWS:
        return {}
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        subprocess_executor(),
        descendant_termination_handles,
        pid,
        dict(retained_handles or {}),
        root_handle,
    )


# ---------------------------------------------------------------------------
# File permissions
# ---------------------------------------------------------------------------


def fchmod_safe(fd: int, mode: int) -> None:
    """Apply ``mode`` to ``fd``. Logs warning on failure.
    No-op on Windows (no POSIX perms).
    """
    if IS_POSIX:
        try:
            os.fchmod(fd, mode)
        except OSError:
            logger.warning("Cannot set permissions on fd %d", fd)


def chmod_safe(path: str | os.PathLike, mode: int) -> None:
    """Apply ``mode`` to ``path``. Logs warning on failure.
    No-op on Windows.
    """
    if IS_POSIX:
        try:
            os.chmod(path, mode)
        except OSError:
            logger.warning("Cannot set permissions on %s", path)


def _clear_readonly_and_retry(func: Any, path: str, _exc: BaseException) -> None:
    """``shutil.rmtree`` error hook: drop the read-only bit, then retry once.

    Windows checks the read-only ATTRIBUTE on the file being deleted, whereas
    POSIX consults the parent directory's write bit. So a mode-``444`` file — of
    which a git checkout is full, since loose objects are written read-only —
    cannot be unlinked on Windows even when its directory is writable.
    """
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        logger.warning("Cannot remove %s", path)


def rmtree_force(path: str | os.PathLike) -> bool:
    """Remove a directory tree, defeating Windows read-only files. Never raises.

    Returns True when *path* is gone afterwards.

    ``shutil.rmtree(..., ignore_errors=True)`` is the usual spelling and is WRONG
    for any tree that may contain a git checkout: on Windows the read-only loose
    objects under ``.git/objects`` refuse to unlink, ``ignore_errors`` swallows
    every one of those failures, and the caller reports success over a tree that
    is still on disk — so the project name stays taken and the next create
    answers 409.

    The return value is what lets a caller tell a real deletion from a partial
    one; the boolean is derived from the filesystem rather than from the hook,
    because a surviving file is the only thing that actually matters.
    """
    # `onexc` replaced `onerror` in 3.12 and the old name warns; this project
    # still supports 3.9+, so pick by capability rather than by version number.
    kwarg = "onexc" if sys.version_info >= (3, 12) else "onerror"
    if kwarg == "onerror":  # pragma: no cover - exercised on Python < 3.12

        def _legacy(func: Any, target: str, exc_info: Any) -> None:
            _clear_readonly_and_retry(func, target, exc_info[1])

        shutil.rmtree(path, onerror=_legacy)
    else:
        shutil.rmtree(path, onexc=_clear_readonly_and_retry)  # type: ignore[call-arg]
    return not os.path.exists(path)


def symlink_or_junction(target: str | os.PathLike, link: str | os.PathLike) -> None:
    """Create a directory link at *link* pointing to *target*.

    POSIX: a plain ``os.symlink``.

    Windows: ``os.symlink`` needs SeCreateSymbolicLinkPrivilege — held only by
    an elevated process or one running with Developer Mode on — so it raises
    ``OSError WinError 1314`` for the ordinary non-admin user, silently breaking
    every feature that links a directory into place (app skills, etc.). A
    directory JUNCTION needs no privilege, is followed transparently by reads
    and by ``os.path.realpath`` / ``Path.resolve()`` (so app-root containment
    checks still hold), and is the standard no-elevation substitute. Fall back
    to it, and only if the symlink attempt fails, so the POSIX-identical path is
    unchanged where symlinks are permitted.

    ``target`` must be an existing directory on Windows (junctions are
    directory-only). Raises if neither a symlink nor a junction can be made.
    """
    if IS_POSIX:
        os.symlink(str(target), str(link))
        return
    try:
        # target_is_directory=True is required on Windows: a directory link made
        # without it is a FILE-type symlink pointing at a directory, which is not
        # traversable. Ignored on POSIX. This helper only ever links directories.
        os.symlink(str(target), str(link), target_is_directory=True)
    except OSError:
        # No symlink privilege (the common non-admin case) — use a junction,
        # which requires none. _winapi.CreateJunction exists on all supported
        # CPython builds on Windows.
        import _winapi

        # _winapi.CreateJunction is Windows-only; typeshed omits it on the POSIX
        # stub, so ignore the attr error mypy raises when checking on Linux.
        _winapi.CreateJunction(str(target), str(link))  # type: ignore[attr-defined]


# os.path.isjunction is 3.12+; fall back to the reparse-tag check below on the
# 3.10/3.11 interpreters this project still supports, or the junction guard is a
# silent no-op there. Constants mirror CPython's own isjunction.
_ISJUNCTION = getattr(os.path, "isjunction", None)
_FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003


def _is_junction_fallback(path: str | os.PathLike) -> bool:
    """``os.path.isjunction`` for Python 3.10/3.11, which lack it.

    A junction is a reparse point (``FILE_ATTRIBUTE_REPARSE_POINT``) whose tag is
    ``IO_REPARSE_TAG_MOUNT_POINT``. Both fields are Windows-only additions to
    ``os.stat_result``, so their absence off Windows makes this False — correct,
    since junctions do not exist there. ``follow_symlinks=False``: the question
    is what THIS name is, not what it points at.
    """
    try:
        info = os.stat(path, follow_symlinks=False)
    except (OSError, ValueError, TypeError):
        return False
    attrs = getattr(info, "st_file_attributes", 0)
    if not attrs & _FILE_ATTRIBUTE_REPARSE_POINT:
        return False
    return getattr(info, "st_reparse_tag", 0) == _IO_REPARSE_TAG_MOUNT_POINT


def is_link_or_junction(path: str | os.PathLike) -> bool:
    """True if *path* is a symlink OR (on Windows) a directory junction.

    ``os.path.islink`` returns False for a junction, so a caller that only
    checks ``islink`` would treat a junction as a real directory and
    ``rmtree`` THROUGH it, destroying the target's contents. Pair with
    :func:`unlink_link_or_junction` to remove one safely.
    """
    if os.path.islink(path):
        return True
    if _ISJUNCTION is not None:
        try:
            return bool(_ISJUNCTION(path))
        except (OSError, ValueError):
            return False
    return _is_junction_fallback(path)


def unlink_link_or_junction(path: str | os.PathLike) -> None:
    """Remove a symlink or directory junction WITHOUT touching its target.

    A symlink is removed with ``unlink``; a Windows junction is a directory
    reparse point removed with ``rmdir`` (which unlinks the junction itself,
    never the target it points at).
    """
    if os.path.islink(path):
        os.unlink(path)
        return
    is_junction = _ISJUNCTION(path) if _ISJUNCTION is not None else _is_junction_fallback(path)
    if is_junction:
        os.rmdir(path)
        return
    # Neither — let the caller's own logic handle a real file/dir.
    os.unlink(path)


# Well-known SID for the file's *owner* (implicit). Under a self-relative DACL
# with inheritance stripped, S-1-3-4 grants access to whoever currently owns
# the file. See:
# https://learn.microsoft.com/en-us/windows/win32/secauthz/well-known-sids
_OWNER_RIGHTS_SID = "*S-1-3-4"


# Success-only memo for _current_user_sid. NOT functools.lru_cache: lru_cache
# would memoize a *failure* (None) permanently, so one transient whoami
# timeout at first call (AV scan, system pressure) would poison every later
# restrict_to_owner for the process lifetime. A None result must stay
# retryable; only a resolved SID is cached.
_USER_SID_CACHE: list[str] = []


_TOKEN_QUERY = 0x0008
_TOKEN_USER_CLASS = 1  # TOKEN_INFORMATION_CLASS.TokenUser
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _process_token_sid() -> str | None:
    """The invoking user's SID read from this process's own access token.

    Preferred over ``whoami`` because it spawns nothing: the SID is already in
    the process token, so this cannot time out under load, cannot be defeated
    by a stripped PATH or a locked-down host, and is safe to call on the event
    loop. Returns ``None`` on any failure so the caller can fall back.
    """
    if IS_POSIX:
        return None
    try:
        return _process_token_sid_unguarded()
    except Exception:  # noqa: BLE001 - best-effort: the caller falls back
        logger.debug("_process_token_sid failed", exc_info=True)
        return None


def _process_token_sid_unguarded(pid: int | None = None) -> str | None:
    """Body of :func:`_process_token_sid`; may raise.

    ``pid`` selects whose token to read: ``None`` means this process (via the
    ``GetCurrentProcess`` pseudo-handle), any other value opens that process
    with ``PROCESS_QUERY_LIMITED_INFORMATION`` -- the least right that still
    permits ``OpenProcessToken``, and one a user always holds over their own
    processes without elevation.
    """

    try:
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    except (OSError, AttributeError):
        # AttributeError: ctypes has no WinDLL off Windows. Reachable because
        # tests exercise the Windows branch from Linux by patching IS_POSIX.
        return None

    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    # Every prototype is declared and every argument below is passed as a
    # ctypes instance rather than a Python int. Leaving either to the default
    # lets ctypes convert a pointer-sized value through a C int, which either
    # truncates it silently or raises OverflowError depending on the call --
    # both observed on Windows, neither reproducible on Linux.
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL

    # Hand the pseudo-handle over as a HANDLE instance rather than the int
    # ctypes hands back for a c_void_p restype: converting that int through the
    # declared argtype raises OverflowError on Windows because the value is
    # pointer-sized and unsigned.
    #
    # own_handle tracks whether this is a real handle we must close. The
    # GetCurrentProcess pseudo-handle must NOT be closed.
    own_handle = pid is not None
    if pid is None:
        process = wintypes.HANDLE(kernel32.GetCurrentProcess())
    else:
        process = wintypes.HANDLE(
            kernel32.OpenProcess(
                wintypes.DWORD(_PROCESS_QUERY_LIMITED_INFORMATION),
                wintypes.BOOL(False),
                wintypes.DWORD(int(pid)),
            )
        )
        if not process.value:
            return None
    try:
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(
            process, wintypes.DWORD(_TOKEN_QUERY), ctypes.byref(token)
        ):
            return None
        try:
            size = wintypes.DWORD()
            # First call sizes the buffer and is expected to fail.
            advapi32.GetTokenInformation(
                token,
                ctypes.c_int(_TOKEN_USER_CLASS),
                None,
                wintypes.DWORD(0),
                ctypes.byref(size),
            )
            if size.value == 0:
                return None
            buf = (ctypes.c_byte * size.value)()
            if not advapi32.GetTokenInformation(
                token,
                ctypes.c_int(_TOKEN_USER_CLASS),
                ctypes.cast(buf, ctypes.c_void_p),
                size,
                ctypes.byref(size),
            ):
                return None
            user = ctypes.cast(buf, ctypes.POINTER(_TokenUser)).contents
            out = wintypes.LPWSTR()
            if not advapi32.ConvertSidToStringSidW(
                ctypes.c_void_p(user.User.Sid), ctypes.byref(out)
            ):
                return None
            try:
                sid = out.value
            finally:
                kernel32.LocalFree(out)
        finally:
            kernel32.CloseHandle(token)
    finally:
        if own_handle:
            kernel32.CloseHandle(process)
    if not sid or not sid.startswith("S-1-"):
        return None
    return sid


def process_owner_sid(pid: int) -> str | None:
    """The SID of the user owning *pid*, as a string, or ``None``.

    Windows' answer to :func:`process_owner_uid`, which returns ``None`` there
    because Windows has no uid. Reads the target process's access token
    directly -- no subprocess, no WMI round trip -- so it is safe to call on the
    event loop.

    This is what lets a Windows peer-principal check work at *connect* time.
    The obvious alternative, ``ImpersonateNamedPipeClient``, cannot:
    per Microsoft's documentation it impersonates "the security context of the
    last message read from the pipe", so before the first read there is no
    context to adopt and the call fails (or yields an anonymous token that
    ``OpenThreadToken`` then refuses). Reading the peer process's own token has
    no such ordering requirement, and it never borrows the peer's token onto one
    of our threads.

    PID reuse is not exploitable here. The window between learning the peer PID
    and opening it is tiny, and either outcome is safe: if the PID has been
    recycled to a process owned by *another* user the comparison reports a
    mismatch and the caller denies; if it was recycled to another process owned
    by *us* then the principal genuinely is us, which is the only question this
    function answers. Ownership -- not process identity -- is the assertion.

    Returns ``None`` on POSIX and on any failure, so callers must treat ``None``
    as "unverifiable" rather than as a match.
    """
    if IS_POSIX:
        return None
    try:
        return _process_token_sid_unguarded(int(pid))
    except Exception:  # noqa: BLE001 - best-effort: the caller fails closed
        logger.debug("process_owner_sid(%s) failed", pid, exc_info=True)
        return None


def _current_user_sid() -> str | None:
    """Return the current user's SID, ``*``-prefixed for icacls, or None.

    Resolved from this process's access token when possible, falling back to
    ``whoami /user /fo csv``.

    Used by :func:`restrict_to_owner` on Windows to grant the invoking user
    Full Control even when the file is owned by a different principal (e.g.
    the file was created by a prior elevated / SYSTEM run, or restored from a
    backup that preserved another owner). Granting only S-1-3-4 (Owner Rights)
    in that scenario locks the current user out entirely on next read.
    Best-effort: on any failure returns None (uncached, so the next call
    retries) and the caller refuses the DACL write.

    Success is cached at module scope: the user's SID is constant for the
    process lifetime, so one lookup covers every restrict_to_owner call (six
    secret-write sites). That matters for the whoami fallback specifically --
    this is called on the event loop under token_secret._SECRET_LOCK's
    first-token-verify path, so the memo bounds that stall to one spawn even
    when the token read is unavailable.
    """
    if IS_POSIX:
        return None
    if _USER_SID_CACHE:
        return _USER_SID_CACHE[0]
    # Own access token first: no spawn, so it survives a stripped PATH, a
    # hermetic test harness and load that would time the whoami call out.
    from_token = _process_token_sid()
    if from_token:
        result = "*" + from_token
        _USER_SID_CACHE.append(result)
        return result
    whoami = shutil.which("whoami") or r"C:\Windows\System32\whoami.exe"
    try:
        r = subprocess.run(
            [whoami, "/user", "/fo", "csv", "/nh"],
            check=False,
            capture_output=True,
            timeout=5,
            creationflags=_SUBPROCESS_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    # csv row: "DOMAIN\\user","S-1-5-21-..."
    text = r.stdout.decode("utf-8", "replace").strip().strip('"')
    parts = text.split('","')
    if len(parts) < 2:
        return None
    sid = parts[-1].strip('"').strip()
    if not sid.startswith("S-1-"):
        return None
    result = "*" + sid
    _USER_SID_CACHE.append(result)
    return result


#: Memo for :func:`current_user_sid`. Separate from ``_USER_SID_CACHE``, which
#: holds the ``*``-prefixed icacls form and is populated by a path that may
#: spawn; this one only ever holds a token-derived bare SID.
_TOKEN_SID_CACHE: list[str] = []


def current_user_sid() -> str | None:
    """Return the invoking user's bare SID (``S-1-5-...``), or ``None``.

    Read from this process's own access token and nothing else. Deliberately
    NOT :func:`_current_user_sid`, which falls back to a ``whoami`` subprocess
    with a 5 s timeout: every caller of this function runs on the event loop --
    the gatewayd admission check, the client-side server check, and the pipe
    DACL builder, which runs once per pipe instance and therefore sits on the
    accept path. A token-lookup failure there would stall accepts for seconds
    at a time, repeatedly. :func:`restrict_to_owner` keeps the fallback because
    it is always invoked through ``asyncio.to_thread``.

    Failing closed is correct for every caller: each treats ``None`` as
    "principal unverifiable" and refuses the connection, which degrades to a
    per-session MCP server rather than admitting an unattributable peer.

    SDDL and the Win32 security APIs want the bare SID, so the ``*`` prefix the
    icacls form carries is stripped here rather than in each consumer. Memoised:
    the SID is constant for the process lifetime and this is on a hot path.
    Returns ``None`` on POSIX and on any lookup failure.
    """
    if _TOKEN_SID_CACHE:
        return _TOKEN_SID_CACHE[0]
    raw = _process_token_sid()
    if not raw:
        return None
    sid = raw.lstrip("*") or None
    if sid:
        _TOKEN_SID_CACHE.append(sid)
    return sid


def make_owner_only_dir(path: str | os.PathLike) -> None:
    """Create *path* (with parents) and make it readable only by this user.

    ``mkdir(mode=...)`` alone is not enough on either platform: POSIX masks the
    mode with the umask and ignores it entirely for a directory that already
    exists, and Windows derives access from the DACL rather than the mode bits,
    so the mode argument is inert there. Both cases matter for the same reason --
    a directory created before the owner-only guarantee existed, or created on
    Windows at all, would silently stay readable.

    ``0o700`` and not :func:`restrict_to_owner` on POSIX: that helper applies
    ``0o600``, correct for a secret-bearing file and wrong for a directory,
    which needs the execute bit to be traversable at all. Windows has no such
    split (``icacls ... :F`` grants traverse with everything else), and there the
    DACL is the only carrier of access, so the fail-loud helper is right.

    Best-effort on the tightening step: the directory is still created, and the
    caller decides whether an un-tightened directory is fatal.
    """
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        if IS_WINDOWS:
            restrict_to_owner(p)
        else:
            p.chmod(0o700)
    except OSError:
        logger.warning("could not restrict directory %s to owner-only", p, exc_info=True)


def local_user_id() -> int:
    """A stable integer identifying the invoking user on this host.

    POSIX: ``os.getuid()``. Windows has no uid, so this is a CRC-32 of the
    user's SID -- an arbitrary but stable and collision-resistant-enough
    integer for the one thing the value is used for: partitioning a cache or a
    pool so two different users can never share an entry.

    An integer rather than the SID string because the consumer
    (``mcp_gateway.PoolKey``) type-checks this dimension strictly and refuses to
    coerce -- ``bool("false")`` is ``True`` and ``int()`` on a bool passes
    silently, so a wire value of the wrong type could land a peer in the wrong
    trust partition. Keeping the type identical across platforms keeps that
    check meaningful.

    Returns ``0`` when the SID cannot be resolved. That is a partition
    collapse, not a privilege change: the gateway's endpoint is already
    per-user (owner-only DACL) and its daemon runs per user, so two users
    cannot reach the same pool regardless.
    """
    if IS_POSIX:
        return os.getuid()
    sid = current_user_sid()
    if not sid:
        return 0
    return zlib.crc32(sid.encode("utf-8"))


def restrict_to_owner(path: str | os.PathLike) -> None:
    """Fail-loud owner-only lockdown of a secret-bearing file.

    POSIX: ``os.chmod(path, 0o600)`` — identical semantics to a raw call,
    including the fail-loud ``OSError`` propagation the security-sensitive
    callers rely on to reach their warn-and-continue handlers.

    Windows: strip inheritance and apply an owner-only DACL via
    ``icacls <path> /inheritance:r /grant:r "*S-1-3-4:F" /grant:r "*<user-sid>:F"``.
    S-1-3-4 (Owner Rights) covers the file's current owner; the invoking-user
    grant covers the caller by explicit SID, so a file created by another
    principal (elevated first-run, backup restore, SYSTEM-context service)
    remains readable by the caller that is trying to lock it down — otherwise
    the current user would be denied their own token signing key on the next
    read and every issued auth cookie / refresh token would be invalidated on
    each restart. When ``_current_user_sid()`` cannot resolve the SID
    (whoami missing / fails / unparseable), we raise ``OSError`` BEFORE
    invoking icacls: an Owner-Rights-only DACL would recreate the exact
    ownership-lockout regression the dual grant exists to prevent, so we
    refuse to apply a half-configured lockdown. Any failure raises
    ``OSError`` so callers hit the same warn-and-continue path they use on
    POSIX.
    """
    if IS_POSIX:
        os.chmod(path, 0o600)
        return
    icacls = shutil.which("icacls") or r"C:\Windows\System32\icacls.exe"
    # Resolve the invoking user's SID BEFORE building the argv. If whoami is
    # unavailable (missing from PATH under a stripped-down profile, subprocess
    # exception, unparseable output), we CANNOT safely apply the DACL: the
    # Owner-Rights-only fallback (S-1-3-4 alone) would lock the current user
    # out of their own file when the file was created by a different principal
    # (elevated first-run, SYSTEM-context service, backup-restored tarball
    # preserving foreign ownership — the exact scenarios the dual-grant
    # exists to prevent). Fail loud (raise OSError, same shape callers see on
    # icacls non-zero rc) so the security-warning handler fires instead of
    # silently re-introducing the ownership-lockout regression.
    user_sid = _current_user_sid()
    if user_sid is None:
        raise OSError(
            "restrict_to_owner: cannot resolve current user SID via whoami; "
            "refusing to apply Owner-Rights-only DACL (would lock non-owner "
            f"users out of {path!s} — see _current_user_sid docstring)."
        )
    argv: list[str] = [
        icacls,
        os.fspath(path),
        "/inheritance:r",
        "/grant:r",
        f"{_OWNER_RIGHTS_SID}:F",
    ]
    if user_sid != _OWNER_RIGHTS_SID:
        argv += ["/grant:r", f"{user_sid}:F"]
    try:
        r = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            timeout=10,
            creationflags=_SUBPROCESS_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OSError(f"icacls invocation failed for {path}: {exc}") from exc
    if r.returncode != 0:
        raise OSError(
            f"icacls failed for {path} (rc={r.returncode}): "
            f"{(r.stderr or r.stdout).decode('utf-8', 'replace').strip()}"
        )


# Hook-script extensions treated as runnable on Windows (where there is no
# POSIX execute bit). A hook is a small script KiroCrew shells out to; on
# Windows its runnability is decided by extension + interpreter at exec time,
# not a filesystem bit.
_WINDOWS_RUNNABLE_HOOK_SUFFIXES = (".sh", ".ps1", ".cmd", ".bat", ".py", ".exe")


def is_executable_file(
    path: str | os.PathLike,
    *,
    platform_name: str | None = None,
) -> bool:
    """Should this file be treated as a runnable hook/script for *this* platform?

    POSIX: the file must carry an execute bit (``os.access(X_OK)``) — unchanged
    behavior, so a ``chmod -x`` still disables a hook.

    Windows: there is NO execute bit (every file reports the same mode), so a
    POSIX X_OK check would reject EVERY hook and silently disable the whole
    kiro-hooks autoimport (observed: no preToolUse hook ever registered). On
    Windows we instead accept a regular file whose extension is a known script
    type (``.sh``/``.ps1``/``.cmd``/``.bat``/``.py``/``.exe``) — runnability is
    determined when KiroCrew actually invokes it, not by a meaningless bit.
    ``platform_name`` lets cross-platform discovery apply the target platform's
    rules instead of the host's. A Windows host cannot represent POSIX execute
    bits, so an existing regular file is accepted for an explicit POSIX target.
    """
    try:
        if not os.path.isfile(path):
            return False
        target_is_windows = IS_WINDOWS if platform_name is None else platform_name == "win32"
        if target_is_windows:
            suffix = os.path.splitext(str(path))[1].lower()
            return suffix in _WINDOWS_RUNNABLE_HOOK_SUFFIXES
        return not IS_POSIX or os.access(path, os.X_OK)
    except OSError:
        return False


def _is_windows_store_python_stub(path: str) -> bool:
    """True if *path* is the Microsoft Store ``python`` App Execution Alias stub.

    On Windows, ``shutil.which("python"/"python3")`` resolves a 0-byte reparse
    point under ``%LOCALAPPDATA%\\Microsoft\\WindowsApps`` when no real CPython
    is installed/on PATH. Spawning it does NOT run Python — it prints "Python was
    not found; run without arguments to install from the Microsoft Store…" and
    exits 9009. Detect it by its WindowsApps location so callers never execute
    it (which otherwise floods logs on every probe). Mirrors install.ps1's
    Find-RealPython, which rejects the same stub.
    """
    if not IS_WINDOWS:
        return False
    norm = path.replace("/", "\\").lower()
    return "\\microsoft\\windowsapps\\" in norm


def find_python_interpreter(reject: Optional[Callable[[str], bool]] = None) -> str | None:
    """Resolve a real CPython >= 3.10 interpreter, or None.

    Single source of truth for "where is a usable system python" on every
    platform. Prefers versioned names (3.12/3.11), then bare ``python``/
    ``python3``, with free-threaded-prone ``python3.13`` LAST so a usable
    3.12/3.11/3.10 wins first. Rejects
    Brazil-path/build interpreters and — critically on Windows — the Microsoft
    Store alias stub (see :func:`_is_windows_store_python_stub`): running that
    stub is what emits the "Python was not found" nag, so we must never spawn it.

    ``reject`` is an optional predicate run against each >= 3.10 candidate path;
    return True to skip it and FALL THROUGH to the next candidate (not abort).
    Callers with extra constraints the shared resolver can't express — e.g. the
    STT prereq probe needs pip and a non-free-threaded build — pass it here so a
    single unusable interpreter does not short-circuit the whole search.

    Returns the interpreter path, or None when none is usable. Callers that just
    need *an* interpreter to re-exec KiroCrew itself should prefer
    ``sys.executable``; this is for finding a SEPARATE system python (e.g. STT /
    whisper installs that must not land in the gateway's venv).
    """
    names = (
        ("python3.12", "python3.11", "python3.10", "python", "python3")
        if IS_WINDOWS
        else ("python3.12", "python3.11", "python3.10", "python3", "python3.13")
    )
    for name in names:
        p = shutil.which(name)
        if not p or "brazil-path" in p or "build/private" in p:
            continue
        if _is_windows_store_python_stub(p):
            continue
        try:
            out = subprocess.check_output(
                [p, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
                timeout=5,
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
            major, _, minor = out.partition(".")
            if not (int(major) == 3 and int(minor) >= 10):
                continue
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
        # >= 3.10 and resolvable. Let the caller veto it (e.g. free-threaded /
        # no pip) and keep searching the remaining candidates.
        if reject is not None and reject(p):
            continue
        return p
    return None


# ---------------------------------------------------------------------------
# Resource limits
# ---------------------------------------------------------------------------


def proc_rss_bytes() -> int:
    """Return this process's resident set size in bytes, or 0 on failure.

    POSIX: ``resource.getrusage(RUSAGE_SELF).ru_maxrss`` (KiB on Linux, bytes
    on macOS). Windows: ``GetProcessMemoryInfo().WorkingSetSize``.
    """
    if IS_POSIX:
        try:

            ru_maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            # Linux reports KiB; macOS reports bytes.
            return ru_maxrss if sys.platform == "darwin" else ru_maxrss * 1024
        except (ImportError, OSError, ValueError):
            return 0
    try:

        psapi = ctypes.WinDLL("psapi", use_last_error=True)  # type: ignore[attr-defined]
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        # argtypes/restype are load-bearing on 64-bit: without them ctypes
        # defaults GetCurrentProcess's return to a 32-bit int and TRUNCATES the
        # pseudo-handle, so GetProcessMemoryInfo fails and this returned 0 for
        # every process — silently disabling the watchdog's RSS-recycle ceiling.
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ProcessMemoryCounters),
            wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        counters = _ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(_ProcessMemoryCounters)
        if psapi.GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        ):
            return int(counters.WorkingSetSize)
        return 0
    except Exception:
        return 0


def proc_rss_bytes_for_pid(pid: int) -> int | None:
    """Resident set size (bytes) of an ARBITRARY *pid*, or None if unavailable.

    Unlike :func:`proc_rss_bytes` (self only), this measures another process so
    the watchdog can sum a spawned agent's whole tree. Linux reads
    ``/proc/<pid>/statm``; Windows opens the PID and calls
    ``GetProcessMemoryInfo``; macOS has no ctypes-only per-pid path, so it
    returns None and the caller keeps its ``ps`` route.
    """

    if sys.platform == "linux":
        try:
            fields = Path(f"/proc/{pid}/statm").read_text().split()
            # statm resident pages * page size.
            return int(fields[1]) * os.sysconf("SC_PAGE_SIZE")
        except (OSError, ValueError, IndexError):
            return None
    if not IS_WINDOWS:
        return None
    try:

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        psapi = ctypes.WinDLL("psapi", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ProcessMemoryCounters),
            wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            counters = _ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(_ProcessMemoryCounters)
            if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                return int(counters.WorkingSetSize)
            return None
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


def proc_rss_tree_mb_for_pid(pid: int) -> float | None:
    """Sum RSS (MiB) of *pid* and its LINEAGE-VALIDATED Windows descendants.

    Windows-only; returns None on other platforms (callers keep their /proc or
    ps route). The naive way to sum a Windows tree — walk Toolhelp's
    ``th32ParentProcessID`` map — is unsafe for a kill/health decision: that
    field is never cleared when a parent dies and Windows recycles PIDs
    aggressively, so a raw walk sums unrelated subtrees rooted at a recycled
    PID. This reuses :func:`descendant_termination_handles`, which validates
    every parent->child edge against exact creation/exit times across two
    snapshots, so only genuine descendants are counted. RSS that cannot be read
    for a given descendant (another session / higher integrity) is skipped, but
    the root itself always contributes, so the result is never a phantom-low
    tree total attached to a recycled root.

    Returns None if even the root's RSS is unavailable, matching the "unknown,
    do not judge" contract the RSS staleness probe relies on.
    """

    if not IS_WINDOWS:
        return None
    if type(pid) is not int or pid <= 1:
        return None
    root_handle = _open_process_termination_handle(pid)
    if root_handle is None:
        # Cannot even anchor the root — fall back to the single-process read so a
        # readable self still yields a number rather than a spurious None.
        rss = proc_rss_bytes_for_pid(pid)
        return None if rss is None else rss / (1024 * 1024)
    descendants: dict[int, int] = {}
    try:
        identity = _windows_process_handle_identity(root_handle)
        if identity is None or identity[0] != pid:
            rss = proc_rss_bytes_for_pid(pid)
            return None if rss is None else rss / (1024 * 1024)
        try:
            descendants = descendant_termination_handles(pid, root_handle=root_handle)
        except Exception:
            # Enumeration failed (transient snapshot race): measure the root
            # alone rather than an unvalidated tree.
            descendants = {}
        total_bytes = 0
        found = False
        for member in (pid, *descendants):
            member_rss = proc_rss_bytes_for_pid(member)
            if member_rss is not None:
                total_bytes += member_rss
                found = True
        return total_bytes / (1024 * 1024) if found else None
    finally:
        for handle in descendants.values():
            close_process_handle(handle)
        close_process_handle(root_handle)


def proc_cpu_seconds() -> float:
    """Return total (user+system) CPU seconds consumed by this process, or 0.0.

    POSIX: ``resource.getrusage`` user+system times.
    Windows: ``GetProcessTimes`` kernel+user times (100-ns units).
    """
    if IS_POSIX:
        try:

            ru = resource.getrusage(resource.RUSAGE_SELF)
            return ru.ru_utime + ru.ru_stime
        except (ImportError, OSError, ValueError):
            return 0.0
    try:

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        # argtypes/restype are load-bearing on 64-bit: without them ctypes
        # defaults GetCurrentProcess's return to a 32-bit int and truncates the
        # pseudo-handle, so GetProcessTimes fails and this reads 0.0 (mirrors the
        # proc_rss_bytes fix — same truncation, same cause).
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        creation = wintypes.FILETIME()
        exit_ = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(
            kernel32.GetCurrentProcess(),
            ctypes.byref(creation),
            ctypes.byref(exit_),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return 0.0

        def _to_secs(ft: "wintypes.FILETIME") -> float:
            return ((ft.dwHighDateTime << 32) | ft.dwLowDateTime) / 1e7

        return _to_secs(kernel) + _to_secs(user)
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# System-wide metrics (Windows). POSIX callers read /proc or sysctl directly;
# Windows has neither, so route through Win32 via ctypes.
# ---------------------------------------------------------------------------

# Prev-sample state for the Windows system-CPU delta (GetSystemTimes).
_prev_win_sys_cpu: dict[str, float] = {"idle": 0.0, "total": 0.0}


def system_memory() -> "tuple[int, int] | None":
    """Return (total_bytes, available_bytes) of physical RAM on Windows.

    Uses ``GlobalMemoryStatusEx``. Returns ``None`` on non-Windows (POSIX
    callers read ``/proc/meminfo`` or ``sysctl hw.memsize`` themselves) or on
    any failure, so the caller can fall back.
    """
    if not IS_WINDOWS:
        return None
    try:

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        stat = _MemoryStatusEx()
        stat.dwLength = ctypes.sizeof(_MemoryStatusEx)
        if kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            return int(stat.ullTotalPhys), int(stat.ullAvailPhys)
        return None
    except Exception:
        return None


def system_cpu_percent() -> "float | None":
    """Return system-wide CPU utilization percent since the previous call.

    Windows only, via ``GetSystemTimes`` (idle/kernel/user FILETIMEs; the
    kernel time INCLUDES idle). Returns ``None`` on non-Windows, the first
    (pre-delta) sample, or failure. Stateful — keeps the previous sample in a
    module global, so callers should poll it periodically.
    """
    if not IS_WINDOWS:
        return None
    try:

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        idle = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
        ):
            return None

        def _ticks(ft: "wintypes.FILETIME") -> float:
            return float((ft.dwHighDateTime << 32) | ft.dwLowDateTime)

        idle_t = _ticks(idle)
        # kernel already includes idle, so kernel+user is the full busy+idle.
        total_t = _ticks(kernel) + _ticks(user)
        prev_idle = _prev_win_sys_cpu["idle"]
        prev_total = _prev_win_sys_cpu["total"]
        _prev_win_sys_cpu["idle"] = idle_t
        _prev_win_sys_cpu["total"] = total_t
        if prev_total <= 0:
            return None  # first sample, no delta yet
        dtotal = total_t - prev_total
        if dtotal <= 0:
            return None
        busy = dtotal - (idle_t - prev_idle)
        return min(100.0, max(0.0, round(busy / dtotal * 100.0, 1)))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# strftime portability
# ---------------------------------------------------------------------------


def strftime(dt: "object", fmt: str) -> str:
    """``dt.strftime(fmt)`` with the GNU/BSD no-pad directives made portable.

    ``%-I`` / ``%-d`` / ``%-m`` etc. (strip leading zero) are glibc/BSD
    extensions that raise ``ValueError`` on Windows' MSVCRT strftime, which
    spells the same thing ``%#I``. Translate the POSIX form to the Windows
    form on win32 so format strings written for macOS/Linux keep working.
    """
    if IS_WINDOWS:
        out = []
        i = 0
        while i < len(fmt):
            if fmt[i] == "%" and i + 1 < len(fmt):
                nxt = fmt[i + 1]
                if nxt == "-" and i + 2 < len(fmt):
                    out.append("%#" + fmt[i + 2])
                    i += 3
                    continue
                out.append(fmt[i : i + 2])
                i += 2
                continue
            out.append(fmt[i])
            i += 1
        fmt = "".join(out)
    return dt.strftime(fmt)  # type: ignore[attr-defined]


def raise_nofile_soft_limit(target: int) -> None:
    """Best-effort raise of the open-file soft limit toward ``target``.

    POSIX: ``resource.setrlimit(RLIMIT_NOFILE)`` capped at the hard limit.
    Windows: no-op — there is no per-process descriptor rlimit; the C runtime
    uses a fixed handle table sized via ``_setmaxstdio`` which does not apply
    to sockets, so nothing to do.
    """
    if not IS_POSIX:
        return
    try:

        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft < target:
            resource.setrlimit(resource.RLIMIT_NOFILE, (min(target, hard), hard))
    except (ValueError, OSError, ImportError):
        logger.debug("Could not raise RLIMIT_NOFILE", exc_info=True)
