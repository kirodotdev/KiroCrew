"""What this host is, and the native layouts the compatibility layer reads.

The platform flags, the portable signal numbers and Windows process-creation
flags, the Win32 struct layouts (declared once, at module scope, so ``ctypes``
memoises one pointer type for each), and the query-only Windows process handle the
Windows probes open.

One of the owners ``kiro_crew.platform_compat`` is split into: every name here stays
readable and patchable as ``kiro_crew.platform_compat.<name>``.
"""

from __future__ import annotations

import ctypes.util
import logging
import os
import signal
import subprocess
import sys
from ctypes import wintypes

# The whole compatibility layer logs under one name, which operator log filters and
# the tests' caplog scopes key on.
logger = logging.getLogger("kiro_crew.platform_compat")


IS_WINDOWS: bool = sys.platform == "win32"
IS_POSIX: bool = not IS_WINDOWS
IS_LINUX: bool = sys.platform == "linux"
IS_MACOS: bool = sys.platform == "darwin"

# Win32 process identifiers cross the API boundary as a fixed-width DWORD.
# ctypes otherwise truncates a larger Python integer before OpenProcess sees it.
_WINDOWS_PID_MAX = (1 << 32) - 1


# Portable signal constants — signal.SIGKILL is undefined on Windows.
SIGKILL: int = getattr(signal, "SIGKILL", 9)

# Our own process group, captured at import time (POSIX; 0 on Windows where
# os.getpgid doesn't exist). Used by kill_process_tree's broadcast guard so
# the self-check is stable and immune to test-time os.getpgid patching.
_OWN_PGID: int = os.getpgid(0) if hasattr(os, "getpgid") else 0
SIGTERM: int = getattr(signal, "SIGTERM", 15)
# The hangup a vanished controlling terminal delivers. Undefined on Windows, where
# the ConPTY backend tears a console down by handle rather than by signal.
SIGHUP: int = getattr(signal, "SIGHUP", 1)

# Portable subprocess creation flags — these constants exist ONLY on Windows
# (the subprocess module has no such attributes on POSIX). Referencing
# ``subprocess.CREATE_NEW_PROCESS_GROUP`` directly fails mypy's ``[attr-defined]``
# check on the Linux build fleet even when guarded by ``if IS_WINDOWS:`` (mypy
# resolves attributes statically, ignoring the runtime guard). Expose them via
# ``getattr`` so the names resolve to 0 on POSIX (where they are never used) and
# to the real flags on Windows. Mirrors the ``SIGKILL`` pattern above.
CREATE_NEW_PROCESS_GROUP: int = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
DETACHED_PROCESS: int = getattr(subprocess, "DETACHED_PROCESS", 0)
# CREATE_SUSPENDED has no ``subprocess`` alias to getattr from (that module
# re-exports only a subset of the Win32 creation flags), so the value is spelled
# out. It is the load-bearing half of race-free Job object assignment: a process
# created suspended has not executed a single instruction, so it provably has no
# descendants yet and none can escape the job. See :func:`apply_job_limits` and
# :func:`resume_process_main_thread`. 0 on POSIX, where it is never used, so a
# caller can OR it into ``creationflags`` unconditionally.
CREATE_SUSPENDED: int = 0x00000004 if os.name == "nt" else 0
# For the short-lived helper tools this module shells out to on Windows
# (whoami / netstat / taskkill / powershell): a console-less parent
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


# ---------------------------------------------------------------------------
# Win32 struct layouts
# ---------------------------------------------------------------------------
# These MUST stay at module scope, never inside the functions that use them.
# ``ctypes.POINTER(T)`` memoises T -> POINTER(T) in a module-level dict inside
# ctypes and never evicts it, so a Structure subclass declared in a function
# body pins a BRAND-NEW pair of type objects on every call. The helpers that
# use them are polled (the dashboard's system metrics, the RSS-recycle
# watchdog, the tree-kill parent-map walk, the MCP pipe's per-connection peer
# check), which turns that into unbounded growth in a long-lived gateway.
# Declared once here, the memo holds a single entry for the process lifetime.
#
# ``wintypes`` supplies type aliases only, so these definitions import cleanly
# on POSIX; the functions that use them still resolve the DLLs lazily, which is
# what keeps them patchable from the non-Windows test fleet.


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


class _IoCounters(ctypes.Structure):
    """kernel32 ``IO_COUNTERS`` — the I/O accounting block inside a job's limits.

    Never read; present only so the extended-limit layout below has the correct
    size and field offsets.
    """

    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JobObjectBasicLimitInformation(ctypes.Structure):
    """kernel32 ``JOBOBJECT_BASIC_LIMIT_INFORMATION``."""

    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    """kernel32 ``JOBOBJECT_EXTENDED_LIMIT_INFORMATION`` — the ceiling payload.

    ``ActiveProcessLimit`` (in the basic block) bounds the process count where
    ``TasksMax`` bounds tasks, and ``JobMemoryLimit`` is the ``MemoryMax``
    equivalent. See :func:`apply_job_limits` for why the process row is not a
    one-for-one mapping.
    """

    _fields_ = [
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _ThreadEntry32(ctypes.Structure):
    """Toolhelp ``THREADENTRY32`` — thread-enumeration snapshot entry.

    Used by :func:`resume_process_main_thread`, which takes
    ``ctypes.POINTER(_ThreadEntry32)`` for the ``Thread32First`` /
    ``Thread32Next`` argtypes — so this layout in particular MUST stay at module
    scope: it is pointed at, which is exactly what pins a type in ctypes'
    unbounded memo, and the helper runs once per agent spawn.
    """

    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ThreadID", wintypes.DWORD),
        ("th32OwnerProcessID", wintypes.DWORD),
        ("tpBasePri", wintypes.LONG),
        ("tpDeltaPri", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
    ]


def _windows_last_error() -> int:
    """Return ctypes' thread-local Win32 error without POSIX stub assumptions."""

    getter = getattr(ctypes, "get_last_error", None)
    return int(getter()) if callable(getter) else 0


def _open_process_query_handle(pid: int) -> int | None:
    """Open a QUERY-ONLY Windows handle to *pid*, or ``None``.

    Terminate rights are deliberately NOT requested: the callers use this handle
    to decide whether a kill may happen at all, so demanding the right to kill in
    order to read the identity would refuse the guard for exactly the processes a
    caller must be most careful about.

    Returns ``None`` on every non-Windows platform, and on any failure -- an
    unopenable process is one whose identity cannot be confirmed, which every
    caller must treat as "do not kill".
    """
    if not IS_WINDOWS:
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    except Exception:
        return None
    return int(handle) if handle else None


def _close_process_handle(handle: int) -> None:
    """Release a handle from :func:`_open_process_query_handle`. Never raises."""
    if not IS_WINDOWS or type(handle) is not int or handle <= 0:
        return
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(wintypes.HANDLE(handle))
    except Exception:
        logger.debug("CloseHandle failed for process handle %d", handle, exc_info=True)


_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

#: Whether ``os.access`` accepts ``effective_ids`` here — it needs ``faccessat``. Resolved
#: once, at import: it is a property of the platform, not of a call, and computing it from
#: ``os.access``'s own identity per call would silently disable the ACL arm for any caller
#: that had substituted the function.
_ACCESS_HONOURS_EFFECTIVE_IDS = os.access in os.supports_effective_ids
