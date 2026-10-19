"""How much a process, or the host, uses.

Resident and peak memory, heap trimming and the malloc arena cap, open descriptors,
CPU time, one-pass process-subtree samples, and the host's total and available
memory, CPU load and memory pressure.

One of the owners ``kiro_crew.platform_compat`` is split into: every name here stays
readable and patchable as ``kiro_crew.platform_compat.<name>``.
"""

from __future__ import annotations

import asyncio
import ctypes.util
import logging
import os
import sys
import time
from ctypes import wintypes
from pathlib import Path
from typing import Callable, Mapping, NamedTuple, Optional

from kiro_crew.platform_compat_identity import _windows_process_handle_identity, process_matches
from kiro_crew.platform_compat_liveness import process_thread_count
from kiro_crew.platform_compat_native import (
    _PROCESS_QUERY_LIMITED_INFORMATION,
    IS_LINUX,
    IS_MACOS,
    IS_POSIX,
    IS_WINDOWS,
    _close_process_handle,
    _MemoryStatusEx,
    _open_process_query_handle,
    _ProcessMemoryCounters,
)
from kiro_crew.platform_compat_proctable import (
    _darwin_process_cpu_nanos,
    _darwin_sysctl_handle,
    _proc_children,
    _proc_cpu_jiffies,
    _proc_status_rss_kb,
)
from kiro_crew.platform_compat_winproc import (
    _open_process_termination_handle,
    close_process_handle,
    descendant_termination_handles,
)

if IS_POSIX:
    import resource

# The whole compatibility layer logs under one name, which operator log filters and
# the tests' caplog scopes key on.
logger = logging.getLogger("kiro_crew.platform_compat")


# ---------------------------------------------------------------------------
# Resource limits
# ---------------------------------------------------------------------------

#: ``task_info`` flavor selector for ``mach_task_basic_info``
#: (``<mach/task_info.h>``). Chosen over the legacy ``TASK_BASIC_INFO`` because
#: its sizes are 64-bit, so a footprint above 4 GiB is not truncated.
_MACH_TASK_BASIC_INFO = 20


class _MachTimeValue(ctypes.Structure):
    """``time_value_t`` (``<mach/time_value.h>``).

    Never read; present only so the fields after it in
    :class:`_MachTaskBasicInfo` land at the offsets the kernel writes them to.
    """

    _fields_ = [("seconds", ctypes.c_int32), ("microseconds", ctypes.c_int32)]


class _MachTaskBasicInfo(ctypes.Structure):
    """``mach_task_basic_info`` (``<mach/task_info.h>``), in kernel order.

    ``resident_size`` is the task's CURRENT resident footprint in bytes and
    falls when pages are released; ``resident_size_max`` is the high-water mark
    that never falls. Reading the wrong one of the two is exactly the bug this
    layout exists to avoid, so both are named rather than indexed.

    Module scope is load-bearing: ``ctypes.POINTER(T)`` memoises T in a
    module-level dict inside ctypes that is never evicted, so declaring this
    inside the probe would pin a fresh pair of type objects on every call — and
    this probe is polled by the dashboard's system-metrics endpoint.
    """

    _fields_ = [
        ("virtual_size", ctypes.c_uint64),
        ("resident_size", ctypes.c_uint64),
        ("resident_size_max", ctypes.c_uint64),
        ("user_time", _MachTimeValue),
        ("system_time", _MachTimeValue),
        ("policy", ctypes.c_int),
        ("suspend_count", ctypes.c_int),
    ]


#: ``task_info`` takes and returns a count in ``natural_t``-sized elements
#: (``MACH_TASK_BASIC_INFO_COUNT``). Derived from the layout so it cannot go
#: stale if a field is added above.
_MACH_TASK_BASIC_INFO_COUNT = ctypes.sizeof(_MachTaskBasicInfo) // ctypes.sizeof(ctypes.c_int)


def _scale_ru_maxrss(ru_maxrss: int) -> int:
    """``ru_maxrss`` -> bytes. macOS reports bytes; Linux and other POSIX KiB.

    The unit differs by platform with nothing in the value to tell them apart,
    so every reader of ``ru_maxrss`` goes through here.
    """
    return ru_maxrss if sys.platform == "darwin" else ru_maxrss * 1024


def _ru_maxrss_bytes() -> int | None:
    """Peak (high-water) RSS in bytes from ``getrusage``, or None on failure.

    POSIX only, and NOT the Linux reader: there ``execve`` folds the pre-exec
    image's high-water mark into the new process's ``ru_maxrss`` (``fs/exec.c``
    ``exec_mmap`` -> ``setmax_mm_hiwater_rss``), so a gateway started from a
    large parent -- a launcher, a test runner, a bloated shell -- reports that
    parent's peak as its own for life. :func:`_linux_peak_rss_bytes` is the
    Linux source; :func:`_posix_peak_rss_bytes` picks. This is a **peak**, not
    a live reading: ``ru_maxrss`` never decreases for the life of the process.
    """
    try:
        return _scale_ru_maxrss(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    except (ImportError, OSError, ValueError, AttributeError):
        return None


#: Where Linux reports THIS process's own peak resident size: ``VmHWM`` in
#: ``/proc/self/status`` is the current ``mm``'s high-water mark, which a fresh
#: image starts from zero, unlike ``ru_maxrss`` (see :func:`_ru_maxrss_bytes`).
_LINUX_STATUS_PATH = Path("/proc/self/status")
_LINUX_PEAK_RSS_FIELD = "VmHWM:"
#: The highest ``VmHWM`` this process has read. The kernel answers ``VmHWM``
#: with ``max(hiwater_rss, live RSS)`` but folds the live figure into
#: ``hiwater_rss`` only at unmap/exit, from per-thread counters it syncs in
#: batches, so a reading taken while a mapping is live can sit a few hundred KiB
#: above what the next reading, after the unmap, reports (measured 136-376 KiB
#: on a 128 MiB mapping). A peak that never decreases is the contract, so the
#: reader keeps its own floor.
_LINUX_PEAK_RSS_FLOOR = 0


def _peak_rss_from_status(status: str) -> int | None:
    """Parse ``VmHWM`` out of a ``/proc/<pid>/status`` text, in bytes.

    The kernel prints the field as ``VmHWM:\\t   11432 kB`` -- always kB, so any
    other shape (a missing field, a unit that is not kB, a non-numeric value) is
    unreadable rather than a guess. ``kiro_crew.pdf_extract_child`` carries the
    same parser by design: that module keeps its imports minimal because it runs
    under a capped address space, so it does not import this one.
    """
    for line in status.splitlines():
        if not line.startswith(_LINUX_PEAK_RSS_FIELD):
            continue
        parts = line.split()
        if len(parts) == 3 and parts[2] == "kB" and parts[1].isdigit():
            return int(parts[1]) * 1024
        return None
    return None


def _linux_peak_rss_bytes() -> int | None:
    """This process's OWN peak RSS in bytes from ``VmHWM``, or None if unreadable.

    Monotonic across calls (see :data:`_LINUX_PEAK_RSS_FLOOR`); an unreadable
    status file is None even when a floor exists, so a failure reads as one.
    """
    global _LINUX_PEAK_RSS_FLOOR
    try:
        # ``errors="replace"``: the ``Name:`` line is this process's raw comm.
        peak = _peak_rss_from_status(
            _LINUX_STATUS_PATH.read_text(encoding="utf-8", errors="replace")
        )
    except (OSError, ValueError):
        return None
    if peak is None:
        return None
    if peak > _LINUX_PEAK_RSS_FLOOR:
        _LINUX_PEAK_RSS_FLOOR = peak
    return _LINUX_PEAK_RSS_FLOOR


def _posix_peak_rss_bytes() -> int | None:
    """Peak RSS in bytes for THIS process, or None where it cannot be read.

    Linux reads its own ``VmHWM``; an unreadable ``/proc`` is None, never the
    inherited ``ru_maxrss`` -- a wrong number is worse than a missing one in a
    figure an operator uses to size a host. Every other POSIX platform reads
    ``ru_maxrss`` in its unit.
    """
    if sys.platform.startswith("linux"):
        return _linux_peak_rss_bytes()
    return _ru_maxrss_bytes()


def _linux_current_rss_bytes() -> int | None:
    """Current RSS in bytes from ``/proc/self/statm``, or None if unreadable.

    Field 1 of ``statm`` is the resident page count — the same quantity
    ``/proc/self/status``'s ``VmRSS`` and ``ps -o rss=`` report, so the
    dashboard's figure reconciles with what an operator measures by hand.
    """
    try:
        fields = Path("/proc/self/statm").read_text().split()
        return int(fields[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return None


def _macos_current_rss_bytes() -> int | None:
    """Current RSS in bytes via Mach ``task_info``, or None on any failure.

    ``proc_rss_bytes_for_pid`` has no ctypes-only route for an ARBITRARY pid
    (it needs a task port it cannot obtain), but ``mach_task_self()`` hands out
    a port for THIS task unconditionally, so the self-only reading below is
    always available — no subprocess, which matters because the macOS app
    sandbox can deny spawning ``ps``.

    Returns ``resident_size`` (what ``ps -o rss=`` reports), not
    ``phys_footprint``: every other platform branch here reports RSS, and the
    payload field it feeds is named for RSS. Activity Monitor's "Memory" column
    is the phys_footprint variant and will read somewhat differently; that is a
    separate accounting question from the peak-vs-current bug.
    """
    try:
        libc = ctypes.CDLL("/usr/lib/libSystem.dylib", use_errno=True)
    except OSError:
        return None  # not macOS / libSystem unavailable
    try:
        libc.mach_task_self.restype = ctypes.c_uint
        libc.task_info.restype = ctypes.c_int
        libc.task_info.argtypes = [
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.POINTER(_MachTaskBasicInfo),
            ctypes.POINTER(ctypes.c_uint),
        ]
        info = _MachTaskBasicInfo()
        count = ctypes.c_uint(_MACH_TASK_BASIC_INFO_COUNT)
        # mach_task_self() returns a port name owned by the task itself, not a
        # fresh send right, so unlike mach_host_self() it must NOT be deallocated.
        kern_return = libc.task_info(
            libc.mach_task_self(),
            _MACH_TASK_BASIC_INFO,
            ctypes.byref(info),
            ctypes.byref(count),
        )
    except (AttributeError, OSError, ValueError):
        return None
    if kern_return != 0:  # non-zero kern_return_t -> failure
        return None
    return int(info.resident_size)


def _windows_memory_counters() -> "_ProcessMemoryCounters | None":
    """psapi ``PROCESS_MEMORY_COUNTERS`` for this process, or None on failure."""
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
            return counters
        return None
    except Exception:
        return None


def proc_rss_bytes() -> int:
    """Return this process's CURRENT resident set size in bytes, or 0 on failure.

    "Current" is the contract, not an implementation detail: this feeds an
    operator-facing live memory figure, so it must FALL when the gateway
    releases memory and must reconcile with ``ps -o rss=``.

    - Linux: ``/proc/self/statm`` resident pages.
    - macOS: Mach ``task_info(MACH_TASK_BASIC_INFO).resident_size``.
    - Windows: ``GetProcessMemoryInfo().WorkingSetSize``.
    - Last resort on POSIX only: the process's own peak (``VmHWM`` on Linux,
      ``getrusage(RUSAGE_SELF).ru_maxrss`` elsewhere), which never decreases.
      It is here so an unavailable ``libSystem`` or a ``statm`` that will not
      parse still yields an order-of-magnitude number rather than 0, and it
      over-reports by construction — see :func:`proc_peak_rss_bytes` for the
      peak as a deliberate reading.
    """
    if IS_POSIX:
        current = (
            _macos_current_rss_bytes() if sys.platform == "darwin" else _linux_current_rss_bytes()
        )
        if current is not None:
            return current
        return _posix_peak_rss_bytes() or 0
    counters = _windows_memory_counters()
    return 0 if counters is None else int(counters.WorkingSetSize)


HEAP_TRIM_INTERVAL_SECONDS = 10 * 60.0
HEAP_TRIM_RSS_THRESHOLD_BYTES = 1536 * 1024 * 1024
HEAP_TRIM_LOG_THRESHOLD_BYTES = 16 * 1024 * 1024
# Must remain below the heartbeat interval.  A queued or wedged default-executor
# worker forfeits this maintenance pass instead of starving the liveness beat.
HEAP_TRIM_TIMEOUT_SECONDS = 2.0


def _malloc_trim() -> bool:
    """Ask glibc to return wholly-free heap pages, or no-op elsewhere."""
    try:
        libc = ctypes.CDLL(None)
        getattr(libc, "gnu_get_libc_version")
        trim = libc.malloc_trim
        trim.argtypes = [ctypes.c_size_t]
        trim.restype = ctypes.c_int
        return bool(trim(0))
    except (AttributeError, OSError):
        return False


# glibc's ``M_ARENA_MAX`` mallopt parameter (malloc.h). Glibc otherwise creates
# up to 8 x cores per-thread arenas, each reserving 64 MiB heaps that keep freed
# memory per thread: on a 32-core host a pod gateway under dashboard load grew
# ~90 such heaps holding ~2.2 GB. A/B on paired seeded pods:
# a cap of 4 cut peak RSS ~4.0 -> ~3.1 GB and heap RSS ~2.2 -> ~1.1 GB with no
# throughput or p95 regression; a cap of 2 saved slightly more but cost ~3%
# throughput and +5-7% p95 on /api/sessions under saturation, so 4 is the default.
_M_ARENA_MAX = -8
GATEWAY_MALLOC_ARENA_MAX = 4
#: Operator override for the gateway's arena cap: a positive integer replaces the
#: default, ``0`` (or any non-positive value) leaves glibc's own default alone.
MALLOC_ARENA_MAX_ENV = "KIROCREW_MALLOC_ARENA_MAX"


def _glibc_mallopt() -> Callable[[int, int], int] | None:
    """glibc's ``mallopt``, or ``None`` on musl / non-glibc / no libc."""
    try:
        libc = ctypes.CDLL(None)
        libc.gnu_get_libc_version  # noqa: B018 - glibc probe; musl lacks it
        fn = libc.mallopt
        fn.argtypes = [ctypes.c_int, ctypes.c_int]
        fn.restype = ctypes.c_int
        return fn
    except (AttributeError, OSError):
        return None


class MallocArenaCap(NamedTuple):
    """What :func:`limit_malloc_arenas` did.

    ``applied`` is the cap known to be in force, else 0. ``requested`` is the
    cap handed to ``mallopt`` (0 when none was), and ``threads`` the OS thread
    count just before that call. ``requested`` without ``applied`` means glibc
    accepted the value but it may not hold: see :func:`limit_malloc_arenas`.
    """

    applied: int = 0
    requested: int = 0
    threads: int | None = None


def limit_malloc_arenas(
    *,
    environ: Mapping[str, str] | None = None,
    mallopt: Callable[[int, int], int] | None = None,
    thread_count: Callable[[], int | None] | None = None,
) -> MallocArenaCap:
    """Cap glibc malloc arenas for THIS process.

    Called first thing in the gateway command, so it holds for every launcher
    (systemd unit, pod, detached start, Dev Fleet re-exec) without depending on
    each one exporting ``MALLOC_ARENA_MAX`` before exec. ``mallopt`` is used
    rather than the env var because glibc reads that variable once, at its first
    allocation -- long before any Python code runs.

    glibc fixes its arena limit for good the first time a thread needs a new
    arena while more than 8 exist; ``mallopt(M_ARENA_MAX)`` after that still
    returns success but changes nothing. A single-threaded process has only the
    main arena, so the cap is reported as ``applied`` only when this process
    had exactly one OS thread at the call. With more threads (or an unreadable
    count) the value is still handed to glibc, but reported as ``requested``
    only, because whether it holds cannot be seen.

    An operator's own ``MALLOC_ARENA_MAX`` or ``glibc.malloc.arena_max`` tunable
    already took effect at process start and wins: nothing is changed. No-op on
    every non-Linux platform and on a non-glibc libc. Never raises.
    """
    if not IS_LINUX:
        return MallocArenaCap()
    env = os.environ if environ is None else environ
    if env.get("MALLOC_ARENA_MAX", "").strip():
        return MallocArenaCap()
    if "glibc.malloc.arena_max" in env.get("GLIBC_TUNABLES", ""):
        return MallocArenaCap()
    value = GATEWAY_MALLOC_ARENA_MAX
    raw = env.get(MALLOC_ARENA_MAX_ENV, "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = GATEWAY_MALLOC_ARENA_MAX
    if value <= 0:
        return MallocArenaCap()
    try:
        fn = mallopt or _glibc_mallopt()
        if fn is None:
            return MallocArenaCap()
        threads = thread_count() if thread_count else process_thread_count(os.getpid())
        if fn(_M_ARENA_MAX, value) != 1:
            return MallocArenaCap(threads=threads)
        return MallocArenaCap(value if threads == 1 else 0, value, threads)
    except Exception:  # noqa: BLE001 - an allocator hint must never stop the boot
        return MallocArenaCap()


def log_malloc_arena_cap(result: MallocArenaCap) -> None:
    """Log *result* once logging is configured; silent when nothing was asked."""
    if result.applied:
        logger.info("glibc malloc arenas capped at %d", result.applied)
    elif result.requested:
        logger.warning(
            "glibc malloc arena cap %d may not hold: %s OS threads were already "
            "running when it was set",
            result.requested,
            "an unknown number of" if result.threads is None else result.threads,
        )


def trim_heap_if_needed(
    *,
    rss_reader: Callable[[], int | None] | None = None,
    trimmer: Callable[[], bool] | None = None,
) -> int:
    """Return bytes released from a large Linux gateway heap.

    A high threshold avoids allocator-wide work on healthy gateways. This must
    use Linux's current RSS directly: :func:`proc_rss_bytes` deliberately falls
    back to peak RSS when procfs is unavailable, which would turn one historic
    spike into repeated trim attempts. Unsupported libc and probe failures are
    harmless because reclamation is optional.
    """
    if not IS_LINUX:
        return 0
    try:
        read_rss = rss_reader or _linux_current_rss_bytes
        before = read_rss()
        if before is None:
            return 0
        if before < HEAP_TRIM_RSS_THRESHOLD_BYTES:
            return 0
        if not (trimmer or _malloc_trim)():
            return 0
        after = read_rss()
        if after is None:
            return 0
    except Exception:  # noqa: BLE001 - optional maintenance must not stop the heartbeat
        return 0
    return max(0, before - after)


class HeapTrimMaintainer:
    """Self-gate bounded, best-effort heap reclamation for the gateway.

    The heartbeat calls :meth:`maybe_trim` on every tick. Cadence and in-flight
    ownership live here so the heartbeat stays cadence-free. A timed-out worker
    may continue running because Python cannot cancel native work already in a
    thread; ``_inflight`` prevents another from being submitted until that
    worker returns. If cancellation wins before the worker starts, maintenance
    remains disabled for this object, which is the safe failure mode.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        trim: Callable[[], int] = trim_heap_if_needed,
    ) -> None:
        self._clock = clock
        self._trim = trim
        self._next_trim = clock() + HEAP_TRIM_INTERVAL_SECONDS
        self._inflight = False

    async def maybe_trim(self) -> int:
        """Return bytes released, or zero when skipped, timed out, or failed."""
        try:
            if not IS_LINUX:
                return 0
            now = self._clock()
            if now < self._next_trim or self._inflight:
                return 0
            self._next_trim = now + HEAP_TRIM_INTERVAL_SECONDS
            self._inflight = True
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(self._run_trim),
                    timeout=HEAP_TRIM_TIMEOUT_SECONDS,
                )
            except (asyncio.TimeoutError, TimeoutError):
                logger.debug("gateway heap trim timed out; maintenance pass skipped")
                return 0
        except Exception:  # noqa: BLE001 - maintenance must not stop the heartbeat
            logger.debug("gateway heap trim failed", exc_info=True)
            return 0

    def _run_trim(self) -> int:
        """Worker-thread wrapper that releases the single in-flight slot."""
        try:
            return self._trim()
        finally:
            self._inflight = False


def proc_peak_rss_bytes() -> int:
    """Return this process's PEAK resident set size in bytes, or 0 on failure.

    The high-water mark since the process started: it never decreases, which is
    what makes it useful for diagnosing a transient spike that a live reading
    has already forgotten — and useless as the live reading itself. Linux reads
    this process's own ``/proc/self/status`` ``VmHWM`` (``ru_maxrss`` there is
    inherited across ``execve`` from the parent, see :func:`_ru_maxrss_bytes`);
    other POSIX reads ``getrusage(RUSAGE_SELF).ru_maxrss``; Windows reads
    ``GetProcessMemoryInfo().PeakWorkingSetSize``.
    """
    if IS_POSIX:
        return _posix_peak_rss_bytes() or 0
    counters = _windows_memory_counters()
    return 0 if counters is None else int(counters.PeakWorkingSetSize)


# Per-process fd directories, in preference order: /proc/self/fd (Linux),
# /dev/fd (macOS/BSD; also present on Linux as a symlink to the former).
_FD_DIRS = ("/proc/self/fd", "/dev/fd")


def count_open_fds() -> int | None:
    """Return this process's open file descriptor count, or None if unavailable.

    The one shared probe behind both the ``kirocrew.process.open_fds`` gauge
    (``metrics/process_gauges.py``) and gatewayd's zombie-diagnostic
    ``fd_count`` snapshot field, so the two figures cannot drift apart.

    - POSIX: entry count of ``/proc/self/fd`` (Linux) or ``/dev/fd``
      (macOS/BSD), minus one because enumerating the directory opens one fd
      itself (the directory handle) — callers want the steady state.
    - Windows: ``GetProcessHandleCount`` — kernel HANDLEs, not fds, so the
      semantics are platform-dependent (callers document this). Returned raw:
      the query opens no extra handle, so no correction applies.

    Returns None when no probe works; each caller maps its own sentinel.
    """
    for fd_dir in _FD_DIRS:
        try:
            return max(0, len(os.listdir(fd_dir)) - 1)
        except OSError:
            continue
    if not IS_WINDOWS:
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        # argtypes/restype are load-bearing on 64-bit: without them ctypes
        # defaults GetCurrentProcess's return to a 32-bit int and TRUNCATES the
        # pseudo-handle (see _windows_memory_counters).
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetProcessHandleCount.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.GetProcessHandleCount.restype = wintypes.BOOL
        handle_count = wintypes.DWORD()
        if kernel32.GetProcessHandleCount(kernel32.GetCurrentProcess(), ctypes.byref(handle_count)):
            return int(handle_count.value)
        return None
    except Exception:
        return None


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


# --- /proc process-subtree sampling ----------------------------------------
#
# ONE walk for its two callers, ``mcp_gateway.pool`` and ``subagent``: a
# per-caller BFS over ``/proc/<pid>/task/<tid>/children`` with its own ``256``
# ceiling would let a fix to either policy reach only one surface.
# :func:`proc_subtree_sample` is the
# single entry point for BOTH, built from the per-process ``/proc`` reads in
# ``kiro_crew.platform_compat_proctable`` -- private to the walk, except
# :func:`read_proc_stat`, the stat reader for callers elsewhere. Pure stdlib: on
# a host without ``/proc`` every
# access raises ``OSError`` and each reading degrades to its own sentinel.
#
# NOT the only way this repository walks a process tree, and deliberately so.
# ``session_pid._build_child_map`` sums a session's tree from a full ``/proc``
# scan of every process's ``stat`` ``PPid`` field, precisely because the
# ``children`` file this walk reads needs ``CONFIG_PROC_CHILDREN`` and is
# documented as reliable only for frozen tasks -- for a live task it can return
# an incomplete child set and silently drop a descendant subtree from the sum.
# That trade is the right one there (an under-counted tree would make the RSS
# watchdog no-op) and the wrong one here: these two callers sample per backend
# and per live agent on a timer, where a whole-machine scan per sample is the
# larger cost, and an under-count degrades a displayed number rather than
# disabling a protection. Reconsidering the method for these two surfaces is a
# behaviour change to figures users already read, not part of this
# consolidation -- but it is now ONE place to reconsider instead of two.

#: Upper bound on processes walked in one subtree sample. A real tree is tiny
#: (a launcher plus a handful of workers); the cap only guards against a
#: pathological or looping ``/proc`` graph.
#:
#: It bounds THIS WALK's work; it is not a display ceiling for a count, and a
#: surface that already enumerates its own tree does not adopt it. Truncating a
#: displayed count at this number would hand the card a plain integer no
#: consumer can tell from a complete one, where ``procs``/``matched`` reserve
#: ``None`` for "unmeasurable". A walk that needs bounding elsewhere wants a
#: budget that yields ``None``, not a silent truncation.
_SUBTREE_MAX_PROCS = 256


def proc_cpu_nanos_for_pid(pid: int) -> int | None:
    """Total (user+system) CPU time of *pid* in NANOSECONDS, or None.

    The per-pid, cross-platform counterpart to :func:`proc_cpu_seconds`, which
    can only measure the CALLING process. A caller samples this twice and
    consumes the DELTA as evidence that a process is doing work; ``None`` means
    no per-pid counter is readable on this host, which is evidence of neither
    work nor death.

    - Linux: ``_proc_cpu_jiffies`` scaled by ``SC_CLK_TCK``. Never None — an
      unreadable pid reads 0 there, which a delta correctly sees as flat.
    - macOS: ``libproc.proc_pidinfo(PROC_PIDTASKINFO)``, already nanoseconds.
    - Windows: ``GetProcessTimes`` kernel+user (100-ns units) over a query-only
      handle.
    - Any other platform: None.

    Root pid ONLY — deliberately no subtree walk here: this probe runs on a read
    loop's cadence and Windows has no child enumeration cheap enough for that. A
    caller that needs the subtree total uses :func:`proc_subtree_sample` on Linux,
    or walks :func:`darwin_child_pids` and sums this per pid on macOS (the
    liveness oracle's darwin backend does exactly that).
    """
    if type(pid) is not int or pid <= 0:
        return None
    if IS_LINUX:
        try:
            ticks = os.sysconf("SC_CLK_TCK") or 100
        except (AttributeError, OSError, ValueError):
            ticks = 100
        return _proc_cpu_jiffies(pid) * (1_000_000_000 // ticks)
    if IS_MACOS:
        return _darwin_process_cpu_nanos(pid)
    if not IS_WINDOWS:
        return None
    handle = _open_process_query_handle(pid)
    if handle is None:
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        creation = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(
            wintypes.HANDLE(handle),
            ctypes.byref(creation),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return None

        def _hundred_ns(value: "wintypes.FILETIME") -> int:
            return (value.dwHighDateTime << 32) | value.dwLowDateTime

        return (_hundred_ns(kernel) + _hundred_ns(user)) * 100
    except Exception:
        return None
    finally:
        _close_process_handle(handle)


class SubtreeSample(NamedTuple):
    """Every reading one subtree walk can produce, from ONE frontier.

    Each field keeps its own sentinel, because the readings are unmeasurable in
    different ways and collapsing any of them into zero is a bug class in its own
    right:

    * ``rss_kb`` — summed KiB, or ``-1`` when the root pid's own status is
      unreadable (it is gone, or the host has no ``/proc``).
    * ``jiffies`` — summed utime+stime clock ticks; an unreadable pid contributes
      0, since a *delta* of jiffies is what a caller consumes.
    * ``procs`` / ``matched`` — how many processes the subtree carries, and how
      many of their command lines contain one of the caller's ``needles``.
      ``None`` means UNMEASURABLE, never zero: rendering "0 processes" for a live
      tree would be a lie, so a surface renders ``None`` as an em dash instead.
    * ``pss_kb`` — summed proportional set size (``smaps_rollup``), or ``-1``
      when not requested or the root's is unreadable (non-Linux included).
    """

    rss_kb: int
    jiffies: int
    procs: Optional[int]
    matched: Optional[int]
    pss_kb: int = -1


def _proc_pss_kb(pid: int) -> int:
    """Proportional set size (KiB) of one *pid* from ``smaps_rollup``, or -1."""
    try:
        with open(f"/proc/{pid}/smaps_rollup", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("Pss:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return -1


def proc_subtree_sample(
    pid: Optional[int],
    *,
    rss: bool = True,
    jiffies: bool = True,
    counts: bool = False,
    needles: tuple[str, ...] = (),
    pss: bool = False,
) -> SubtreeSample:
    """Walk *pid*'s process subtree ONCE and return every requested reading.

    A tracked process is frequently a thin launcher whose real memory lives in a
    child, so every reading describes the whole subtree rather than the root pid
    alone.

    The point of one pass is not only the fewer ``/proc`` reads: separate readers
    run at separate instants, so a process that exits between them is counted by
    one and missed by another. Reading every metric off a single frontier is what
    makes "the same set of processes" true of the *result* and not merely of the
    walk rules.

    ``rss`` / ``jiffies`` / ``counts`` let a caller skip the per-process reads it
    does not want, so sharing this walk costs each caller what its own walk cost:
    a CPU-only caller pays no ``status`` read, and an RSS-only caller pays no
    ``stat`` read. A skipped reading comes back as its own sentinel. When nothing
    remains to accumulate — RSS unreadable at the root, no jiffies, nothing
    countable — the descendants are not walked at all.

    ``counts`` is Linux-only (it matches command lines via
    :func:`process_matches`) and yields ``(None, None)`` elsewhere. ``pss`` is
    Linux-only too (``smaps_rollup``, kernel 4.14+): summed PSS charges each
    shared page to its sharers proportionally, so it is what the tree costs the
    host once, where summed VmRSS counts the code pages a tree of node /
    MCP-server processes shares once per process. ``smaps_rollup`` walks page
    tables, so it costs far more than a ``status`` read: for an occasional
    one-off reading, never a periodic sweep.

    Coverage caveat for a new caller: the walk reads
    ``/proc/<pid>/task/<tid>/children``, which needs ``CONFIG_PROC_CHILDREN`` and
    is documented as reliable only for frozen tasks, so a live tree can come back
    short. That is acceptable for the periodic per-process figures the two
    callers display; a reading that must not under-count (the RSS watchdog's
    recycle decision) uses ``session_pid._build_child_map`` instead, which pays a
    full ``/proc`` scan for completeness.

    Blocking: reads a handful of ``/proc`` entries per process in the subtree, so
    it belongs on an executor thread, never on the event loop.
    """
    if not pid:
        return SubtreeSample(-1, 0, None, None)
    # The counts share RSS's liveness probe: a root pid whose own status cannot
    # be read has nothing to attribute, so there is nothing to count either.
    own_rss = _proc_status_rss_kb(pid) if (rss or counts) else -1
    countable = counts and IS_LINUX and own_rss >= 0
    rss_total = own_rss if (rss and own_rss >= 0) else -1
    total_jiffies = _proc_cpu_jiffies(pid) if jiffies else 0
    procs = 1
    matched = 1 if countable and process_matches(pid, needles) else 0
    pss_total = _proc_pss_kb(pid) if (pss and IS_LINUX) else -1
    if rss_total < 0 and not jiffies and not countable and pss_total < 0:
        # Nothing a descendant could add — do not pay for the walk.
        return SubtreeSample(-1, total_jiffies, None, None)
    seen = {pid}
    frontier = [pid]
    while frontier and len(seen) < _SUBTREE_MAX_PROCS:
        nxt: list[int] = []
        for parent in frontier:
            for child in _proc_children(parent):
                if child in seen:
                    continue
                seen.add(child)
                if rss_total >= 0:
                    kb = _proc_status_rss_kb(child)
                    if kb > 0:
                        rss_total += kb
                if jiffies:
                    total_jiffies += _proc_cpu_jiffies(child)
                if pss_total >= 0:
                    kb = _proc_pss_kb(child)
                    if kb > 0:
                        pss_total += kb
                if countable:
                    procs += 1
                    if process_matches(child, needles):
                        matched += 1
                nxt.append(child)
        frontier = nxt
    if not countable:
        return SubtreeSample(rss_total, total_jiffies, None, None, pss_total)
    return SubtreeSample(rss_total, total_jiffies, procs, matched, pss_total)


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
# Available physical memory, on every platform
#
# "How much RAM can a new process take without pushing this machine into swap"
# has a different answer, and a different interface, on each OS: Linux publishes
# the number outright, macOS publishes page counters and leaves the composition
# to the caller, Windows has a Win32 call. A caller that reads only one of them
# does not get a conservative answer on the others -- it gets NO answer, which
# is why this lives here rather than at each call site.
# ---------------------------------------------------------------------------

_MIB_BYTES = 1024 * 1024

#: ``natural_t`` is 32-bit on macOS, including on Apple silicon.
_NATURAL_T = ctypes.c_uint

#: ``host_statistics64`` flavor selector for ``vm_statistics64_data_t``.
_HOST_VM_INFO64 = 4


class _VMStatistics64(ctypes.Structure):
    """``vm_statistics64_data_t`` (``<mach/vm_statistics.h>``), in kernel order.

    Declared in full even though few fields are read, so the element count handed
    to ``host_statistics64`` is exact and the trailing fields land at the offsets
    the kernel writes them to.

    Module scope is load-bearing: ``ctypes.POINTER(T)`` memoises T in a
    module-level dict inside ctypes that is never evicted, so declaring this
    inside the probe would pin a fresh pair of type objects on every call.
    """

    _fields_ = [
        ("free_count", _NATURAL_T),
        ("active_count", _NATURAL_T),
        ("inactive_count", _NATURAL_T),
        ("wire_count", _NATURAL_T),
        ("zero_fill_count", ctypes.c_uint64),
        ("reactivations", ctypes.c_uint64),
        ("pageins", ctypes.c_uint64),
        ("pageouts", ctypes.c_uint64),
        ("faults", ctypes.c_uint64),
        ("cow_faults", ctypes.c_uint64),
        ("lookups", ctypes.c_uint64),
        ("hits", ctypes.c_uint64),
        ("purges", ctypes.c_uint64),
        ("purgeable_count", _NATURAL_T),
        ("speculative_count", _NATURAL_T),
        ("decompressions", ctypes.c_uint64),
        ("compressions", ctypes.c_uint64),
        ("swapins", ctypes.c_uint64),
        ("swapouts", ctypes.c_uint64),
        ("compressor_page_count", _NATURAL_T),
        ("throttled_count", _NATURAL_T),
        ("external_page_count", _NATURAL_T),
        ("internal_page_count", _NATURAL_T),
        ("total_uncompressed_pages_in_compressor", ctypes.c_uint64),
    ]


#: How many ``natural_t``-sized elements the kernel must report having filled
#: before ``external_page_count`` holds anything. ``host_statistics64`` writes
#: the count back, and an older kernel that predates the field leaves it zero --
#: indistinguishable from "no file-backed pages" unless the count is checked.
#: Derived from the layout so it cannot go stale if a field is added above.
_EXTERNAL_PAGE_COUNT_ELEMENTS = (
    _VMStatistics64.external_page_count.offset + _VMStatistics64.external_page_count.size
) // ctypes.sizeof(ctypes.c_int)


def macos_vm_statistics() -> "tuple[_VMStatistics64, int] | None":
    """Mach ``host_statistics64(HOST_VM_INFO64)``, or ``None`` on any failure.

    Returns the filled struct and the element count the kernel wrote back, which
    a caller needs to know whether the trailing (later-revision) fields are
    meaningful. macOS-only; returns ``None`` everywhere else.

    Reads in-process through ``ctypes``/``libSystem`` -- **no subprocess**. That
    is not merely faster: the macOS app sandbox can deny spawning ``vm_stat`` or
    ``sysctl``, and this probe runs on the gateway event loop.
    """
    try:
        libc = ctypes.CDLL("/usr/lib/libSystem.dylib", use_errno=True)
    except OSError:
        return None  # not macOS / libSystem unavailable

    try:
        libc.mach_host_self.restype = ctypes.c_uint
        libc.mach_task_self.restype = ctypes.c_uint
        libc.mach_port_deallocate.argtypes = [ctypes.c_uint, ctypes.c_uint]
        libc.host_statistics64.restype = ctypes.c_int
        libc.host_statistics64.argtypes = [
            ctypes.c_uint,
            ctypes.c_int,
            ctypes.POINTER(_VMStatistics64),
            ctypes.POINTER(ctypes.c_uint),
        ]
        host_port = libc.mach_host_self()
        try:
            stats = _VMStatistics64()
            count = ctypes.c_uint(ctypes.sizeof(_VMStatistics64) // ctypes.sizeof(ctypes.c_int))
            kern_return = libc.host_statistics64(
                host_port,
                _HOST_VM_INFO64,
                ctypes.byref(stats),
                ctypes.byref(count),
            )
        finally:
            # Release the send right from mach_host_self so the port reference is
            # not leaked per probe. Guarded so a missing symbol still returns
            # None cleanly below rather than raising out of a memory reading.
            try:
                libc.mach_port_deallocate(libc.mach_task_self(), host_port)
            except (AttributeError, OSError, ValueError):
                pass
    except (AttributeError, OSError, ValueError):
        return None
    if kern_return != 0:  # non-zero kern_return_t -> failure
        return None
    return stats, int(count.value)


def _macos_available_mib() -> int:
    """RAM in MiB a new process can take on macOS without swapping, or 0.

    macOS publishes no ``MemAvailable``; it publishes page counters, and which
    ones count as available is a decision. Each term here is one:

    * ``free_count`` ALREADY INCLUDES ``speculative_count`` -- Darwin's own
      ``vm_stat`` prints ``free_count - speculative_count`` as its "Pages free"
      line. Adding speculative on top double-counts it, which inflates the
      reading on exactly the loaded machine where it must not.
    * ``purgeable_count`` is volatile memory the kernel may drop outright, with
      no I/O, so it is genuinely available.
    * ``inactive_count`` is NOT all reclaimable: it mixes clean file-backed pages
      with DIRTY ANONYMOUS pages that cannot be handed over without compressing
      or swapping them. ``HOST_VM_INFO64`` publishes no inactive-AND-file
      counter, so the intersection is not computable -- but
      ``min(inactive, external_page_count)`` is an upper bound on the file-backed
      share, and it is strictly tighter than ``inactive``. That tightening is
      what stops a browser's gigabytes of inactive anonymous memory reading as
      free.

    Compressed pages are occupied, so the compressor counts are excluded.

    ``0`` means UNKNOWN, and callers skip an unknown reading rather than treating
    it as zero memory. A read that SUCCEEDED but computed nothing therefore
    returns 0 too: a host with no free, purgeable or file-backed pages at all is
    not a reading anyone should act on.
    """
    probe = macos_vm_statistics()
    if probe is None:
        return 0
    stats, filled = probe
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, OSError, ValueError):
        return 0
    if page_size <= 0:
        return 0
    inactive = int(stats.inactive_count)
    if filled >= _EXTERNAL_PAGE_COUNT_ELEMENTS:
        inactive = min(inactive, int(stats.external_page_count))
    pages = int(stats.free_count) + int(stats.purgeable_count) + inactive
    if pages <= 0:
        return 0
    # max(1, ...) only after the >0 check above, so a real but sub-MiB reading
    # stays distinguishable from "unknown".
    return max(1, pages * page_size // _MIB_BYTES)


def _linux_available_mib() -> int:
    """``MemAvailable`` in MiB, or 0 when ``/proc/meminfo`` cannot be read.

    The kernel's own estimate of what a new allocation can use without swapping.
    It counts reclaimable page cache, which ``MemFree`` and ``SC_AVPHYS_PAGES``
    both omit -- on a host that has read any files those understate badly (they
    match ``MemFree`` exactly, measured 43,574 MiB against ``MemAvailable``'s
    74,768 MiB on the same idle host).
    """
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if not line.startswith("MemAvailable:"):
                    continue
                # "MemAvailable:   107374182 kB" -- the unit is always kB.
                return int(line.split()[1]) * 1024 // _MIB_BYTES
    except (OSError, IndexError, ValueError):
        return 0
    return 0


def host_total_mib() -> int:
    """Total physical RAM in MiB, or 0 when it cannot be determined.

    POSIX ``sysconf`` first, then the Win32 reading -- the same order
    ``sandbox._default_max_memory_mb`` uses, and for the same reason: ``os.sysconf``
    does not EXIST on Windows, so a probe written against it alone does not return a
    conservative number there, it returns nothing. Paired with
    :func:`host_available_mib` so both halves of a memory budget answer on every
    platform; a budget with only one of them silently stops bounding anything.
    """
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        if pages > 0 and page_size > 0:
            return pages * page_size // _MIB_BYTES
    except (AttributeError, OSError, ValueError):
        pass
    mem = system_memory()  # GlobalMemoryStatusEx: (total, available)
    return (mem[0] // _MIB_BYTES) if mem else 0


def host_available_mib() -> int:
    """RAM in MiB actually free for a new process right now, or 0 when unknown.

    **MiB, not GiB, and the unit is load-bearing.** In GiB every reading under
    1 GiB truncates to ``0``, which is also this function's "could not
    determine" answer -- so on the starved host the reading exists to protect,
    860 MiB free would read as "unknown" and the bound built on it would vanish.

    ``0`` is returned only when the platform genuinely cannot be read, so a
    caller can distinguish "no headroom" from "no reading" and fail open on the
    latter.
    """
    if IS_LINUX:
        return _linux_available_mib()
    if IS_MACOS:
        return _macos_available_mib()
    if IS_WINDOWS:
        mem = system_memory()  # GlobalMemoryStatusEx: (total, available)
        return (mem[1] // _MIB_BYTES) if mem else 0
    return 0


#: The values ``kern.memorystatus_vm_pressure_level`` answers in. XNU's handler
#: (``bsd/kern/kern_memorystatus_notify.c``) converts its internal level through
#: ``convert_internal_pressure_level_to_dispatch_level`` before answering, so
#: the reading is ``NOTE_MEMORYSTATUS_PRESSURE_*`` from ``sys/event_private.h``.
#: Those are the same numbers as libdispatch's public
#: ``DISPATCH_MEMORYPRESSURE_NORMAL`` / ``_WARN`` / ``_CRITICAL``. The internal
#: "urgent" level reports as WARN.
MEMORY_PRESSURE_NORMAL = 1
MEMORY_PRESSURE_WARN = 2
MEMORY_PRESSURE_CRITICAL = 4
_MEMORY_PRESSURE_NAMES = {
    MEMORY_PRESSURE_NORMAL: "NORMAL",
    MEMORY_PRESSURE_WARN: "WARN",
    MEMORY_PRESSURE_CRITICAL: "CRITICAL",
}
_MEMORY_PRESSURE_SYSCTL = b"kern.memorystatus_vm_pressure_level"


def memory_pressure_level() -> int | None:
    """The macOS kernel's memory-pressure level, or ``None`` when unknown.

    Answers one of :data:`MEMORY_PRESSURE_NORMAL`, :data:`MEMORY_PRESSURE_WARN`
    or :data:`MEMORY_PRESSURE_CRITICAL`, the level Activity Monitor's
    memory-pressure graph shows. It is the kernel's own verdict, and it LAGS
    (docs/system-specs/modules/subagent.md gives the measurement), so it is no
    measure of available memory and cannot replace a figure such as
    :func:`host_available_mib`. A caller uses it as a backstop
    beside such a figure: when the kernel does say WARN, the host is short
    whatever the page counters add up to.

    ``None`` everywhere other than macOS, and on any failure: no ``libc``, a
    failed call, a wrong-size answer, or a value that is not one of the three
    levels. A caller treats ``None`` as "no reading" and fails
    open, the same contract as ``host_available_mib``'s 0.

    Reads in-process through the cached :func:`_darwin_sysctl_handle`, with no
    ``sysctl`` subprocess, for the reason given in :func:`macos_vm_statistics`.
    On macOS the sysctl needs no privilege; XNU checks one only on other Apple
    platforms.
    """
    if not IS_MACOS:
        return None
    libc = _darwin_sysctl_handle()
    if libc is None:
        return None
    level = ctypes.c_uint32(0)
    size = ctypes.c_size_t(ctypes.sizeof(level))
    try:
        result = libc.sysctlbyname(
            _MEMORY_PRESSURE_SYSCTL, ctypes.byref(level), ctypes.byref(size), None, 0
        )
    except (OSError, ValueError, ctypes.ArgumentError):
        return None
    if result != 0 or size.value != ctypes.sizeof(level):
        return None
    value = int(level.value)
    return value if value in _MEMORY_PRESSURE_NAMES else None


def memory_pressure_name(level: int | None) -> str:
    """``"NORMAL"`` / ``"WARN"`` / ``"CRITICAL"`` for *level*, ``""`` when unknown."""
    return _MEMORY_PRESSURE_NAMES.get(level, "") if level is not None else ""
