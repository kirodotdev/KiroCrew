"""Who a process is.

Its parent, image and command line, its start identity and start time (this
process's own too), the user that owns it, its Linux namespaces and cgroups, its
macOS sandbox, and whether its argv matches an expected one.

One of the owners ``kiro_crew.platform_compat`` is split into: every name here stays
readable and patchable as ``kiro_crew.platform_compat.<name>``.
"""

from __future__ import annotations

import ctypes.util
import enum
import hashlib
import os
import re
import struct
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path
from typing import Sequence

from kiro_crew.platform_compat_native import (
    _SUBPROCESS_NO_WINDOW,
    IS_POSIX,
    IS_WINDOWS,
    _close_process_handle,
    _open_process_query_handle,
    _ProcessEntry32,
)
from kiro_crew.platform_compat_proctable import (
    ProcessStartIdentity,
    _darwin_libproc_start_id,
    _darwin_process_cwd,
    _darwin_process_start_identity,
    _darwin_process_start_microtime,
    _DarwinLibprocUnavailable,
    darwin_kinfo_proc,
    darwin_process_argv,
    darwin_process_path,
    read_proc_stat,
    read_proc_status_int,
)
from kiro_crew.platform_compat_trust import trusted_system_bin
from kiro_crew.subprocess_utf8 import UTF8_TEXT


def process_cwd(pid: int) -> str | None:
    """Current working directory of *pid*, or None when no source can answer.

    Never spawns a subprocess. Callers poll this per open terminal, where a
    fork+exec of the whole gateway costs orders of magnitude more than the
    answer is worth. ``/proc`` serves Linux; macOS goes to ``libproc``. Windows
    and any host with neither source get None, leaving the caller to decide
    whether a costlier fallback is warranted.
    """
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        pass
    if sys.platform == "darwin":
        return _darwin_process_cwd(pid)
    return None


def get_ppid(pid: int) -> int:
    """Return the parent PID of *pid*, or ``-1`` on failure.

    Linux: ``PPid:`` of ``/proc/<pid>/status``, through :func:`read_proc_status_int`.
    macOS: ``libproc.proc_pidinfo`` (no entitlement required).
    Windows: ``CreateToolhelp32Snapshot``.
    """
    if sys.platform == "linux":
        ppid = read_proc_status_int(pid, "PPid")
        return -1 if ppid is None else ppid
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


# Bounds the parent walk in is_exec_supervisor_of_this_process. Each in-app
# restart through a supervising launcher nests one more supervisor above the
# gateway, so the bound is generous; the walk normally ends at init long before.
_ANCESTRY_MAX_DEPTH = 64

# A Python interpreter's file name: python, python3, python3.12, python3.13t,
# python.exe, and the macOS framework build's ``Python``.
_PYTHON_INTERPRETER_NAME = re.compile(r"python(\d+(\.\d+)*)?[a-z]?(\.exe)?", re.IGNORECASE)


def process_executable_path(pid: int) -> str | None:
    """The executable *pid* is running now, or None when the host will not say.

    Linux: ``/proc/<pid>/exe``. macOS: :func:`darwin_process_path`
    (``proc_pidpath``). Windows and every failure: None. The path is the image
    after the most recent exec, which is what tells a process that exec-ed into
    a launcher apart from a Python program.
    """
    if type(pid) is not int or pid <= 0:
        return None
    try:
        if sys.platform == "linux":
            target = os.readlink(f"/proc/{pid}/exe")
            return target.removesuffix(" (deleted)") or None
        if sys.platform == "darwin":
            return darwin_process_path(pid)
    except Exception:
        return None
    return None


def _is_python_interpreter_path(path: str) -> bool:
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    return _PYTHON_INTERPRETER_NAME.fullmatch(name) is not None


def is_exec_supervisor_of_this_process(pid: int) -> bool:
    """True when *pid* is a live ancestor whose image is not a Python interpreter.

    An in-app restart ``os.execv``s the launcher, and a launcher that runs the
    gateway as a supervised CHILD (``toolbox-exec``) leaves the old pid alive as
    the new gateway's ancestor, start time unchanged. Ownership checks use this to
    tell that supervisor -- this gateway's own previous image -- from another
    gateway.

    Ancestry alone is not that proof: a gateway started from a shell a LIVE
    gateway spawned has that gateway as an ancestor too. What the exec changed is
    the image, and every gateway image is a Python interpreter, so an ancestor
    still running one is treated as a gateway. The same holds for every process
    BETWEEN this one and *pid*: a live Python process there may be the gateway
    *pid* supervises, which started this one, so the walk refuses as soon as it
    passes one -- or one whose image it cannot read. False also for pid <= 1
    (init is every process's ancestor), for an executable the host will not
    report (Windows, an unreadable process), and when the parent walk fails --
    each keeps the caller's pre-existing verdict.
    """
    current = os.getppid()
    seen: set[int] = set()
    for _ in range(_ANCESTRY_MAX_DEPTH):
        # Checked before the match, so pid 1 (and lower) is never accepted.
        if current <= 1 or current in seen:
            return False
        exe = process_executable_path(current)
        if exe is None or _is_python_interpreter_path(exe):
            # *pid* itself must be a non-Python image, and so must everything
            # between: a Python process in between may be a live gateway.
            return False
        if current == pid:
            return True
        seen.add(current)
        current = get_ppid(current)
    return False


def get_process_start_identity(
    pid: int, *, proc_root: Path | None = None
) -> ProcessStartIdentity | None:
    """Return ``(start_id, ppid)`` from one per-pid kernel record.

    Linux reads both fields from one ``/proc/<pid>/stat`` value. macOS reads
    both from one ``PROC_PIDTBSDINFO`` value. Other platforms return ``None``;
    callers that can tolerate the coarser ``ps`` fallback must bracket it
    separately so process recycling cannot join parentage from one process to
    the identity of another.
    """
    if pid <= 0:
        return None
    if sys.platform == "linux":
        stat = read_proc_stat(pid, proc_root=proc_root)
        if stat is None or stat.ppid is None or stat.start_ticks is None:
            return None
        return ProcessStartIdentity(str(stat.start_ticks), stat.ppid)
    if sys.platform == "darwin":
        return _darwin_process_start_identity(pid)
    return None


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
    - Windows: creation FILETIME from a query-only process handle (100 ns).
    - Any failure: ``None``, meaning "identity unknown"; identity-sensitive
      callers must refuse authorization when they cannot confirm it.
    """
    if pid <= 0:
        return None
    if sys.platform == "win32":
        return process_start_time(pid)
    if sys.platform == "linux":
        identity = get_process_start_identity(pid)
        return identity.start_id if identity is not None else None
    if sys.platform == "darwin":
        try:
            start = _darwin_libproc_start_id(pid)
        except _DarwinLibprocUnavailable:
            # No libproc at all is an unrecognised host, not a zombie: the
            # identity stays unknown, and identity-sensitive callers refuse.
            return None
        if start is not None:
            return start
        # libproc refuses a zombie outright, yet a zombie still owns its pid and
        # must stay identifiable: the teardown compares this value before it
        # waits on the exited leader, and an unreadable identity would leave
        # that zombie unreaped for good. sysctl reads the same ``p_start`` from
        # the kernel's zombie list, in the same format.
        facts = darwin_kinfo_proc(pid)
        return facts.start_id if facts is not None else None
    return None


def process_namespaces_match(pid: int, reference_pid: int) -> bool | None:
    """Compare live Linux user AND mount namespaces; unknown is never a match.

    These kernel identities survive reparenting and cannot be replaced by a
    descendant with identities from its parent's user namespace. Comparing both
    prevents a new mount view in the same user namespace from borrowing host
    authority. Process incarnation checks bracket the reads to reject PID reuse.
    """
    if sys.platform != "linux" or any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 1
        for value in (pid, reference_pid)
    ):
        return None
    try:
        starts = [get_process_start_id(value) for value in (pid, reference_pid)]
        if not all(starts):
            return None
        identities = []
        for value in (pid, reference_pid):
            identity = []
            for namespace in ("user", "mnt"):
                info = Path(f"/proc/{value}/ns/{namespace}").stat()
                identity.append((info.st_dev, info.st_ino))
            identities.append(identity)
        if starts != [get_process_start_id(value) for value in (pid, reference_pid)]:
            return None
        return identities[0] == identities[1]
    except (OSError, ValueError):
        return None


def process_is_sandboxed(pid: int) -> bool | None:
    """Read inherited macOS Seatbelt state without applying a policy.

    The null-operation sandbox_check query reports whether the process has a
    sandbox, including after its original parent exits. Missing SPI, errors and
    process recycling are unknown; callers must not treat them as unsandboxed.
    """
    if sys.platform != "darwin" or isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        return None
    try:
        start = get_process_start_id(pid)
        if not start:
            return None
        library = ctypes.CDLL("/usr/lib/libsandbox.dylib", use_errno=True)
        check = library.sandbox_check
        check.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        check.restype = ctypes.c_int
        result = check(pid, None, 0)
        if get_process_start_id(pid) != start or result not in (0, 1):
            return None
        return result == 1
    except (AttributeError, OSError, ValueError):
        return None


def process_can_read_under_sandbox(pid: int, path: Path) -> bool | None:
    """Query a live Darwin process's Seatbelt read permission without reading.

    A sandboxed V1 runtime can read Global memory; a private member cannot.
    The sandbox-presence bit alone cannot distinguish those policies. Callers
    supply a trusted absolute path and accept only an explicit True result.
    """
    if sys.platform != "darwin" or isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        return None
    try:
        if not path.is_absolute():
            return None
        start = get_process_start_id(pid)
        if not start:
            return None
        library = ctypes.CDLL("/usr/lib/libsandbox.dylib", use_errno=True)
        check = library.sandbox_check
        # sandbox_check is variadic. Declare only its three fixed arguments;
        # Apple ARM64 passes the fourth (path) argument using the varargs ABI.
        check.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        check.restype = ctypes.c_int
        no_report = ctypes.c_int.in_dll(library, "SANDBOX_CHECK_NO_REPORT").value
        # SANDBOX_FILTER_PATH is 1 in Apple's SandboxSPI.h declaration.
        result = check(pid, b"file-read-data", 1 | no_report, ctypes.c_char_p(os.fsencode(path)))
        if get_process_start_id(pid) != start or result not in (0, 1):
            return None
        return result == 0
    except (AttributeError, OSError, TypeError, ValueError):
        return None


class ProcessIdentitySource(enum.Enum):
    """The encoding and reader that produced a process-start identity."""

    ATOMIC = "atomic"
    WINDOWS = "windows"
    LSTART = "lstart"


# Named by the path callers import it from, so a traceback, a pickle or any
# ``module.qualname`` rendering of the class says ``kiro_crew.platform_compat``.
ProcessIdentitySource.__module__ = "kiro_crew.platform_compat"


# Bounds for the exited-but-exit-FILETIME-unpublished window (see
# _windows_process_handle_identity). The window closes within a few tens of
# milliseconds; the ceiling is generous enough to absorb a loaded host without
# letting a genuinely unreadable handle stall a caller.
_WINDOWS_EXIT_FILETIME_TIMEOUT_SECS = 0.25
_WINDOWS_EXIT_FILETIME_POLL_SECS = 0.002


def _windows_process_handle_identity(
    handle: int, *, await_exit_time: bool = True
) -> tuple[int, int, int | None] | None:
    """Return ``(pid, creation_time, exit_time)`` for an exact process handle.

    ``await_exit_time=False`` answers with the creation half alone: liveness is
    not decided, the exit FILETIME is neither awaited nor reported, and the third
    element is ``None`` by construction. That mode performs no wait and no sleep,
    so it is safe to call from the asyncio event loop; a caller that must certify
    a process has exited needs the exit bound and keeps the default.
    """

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

        def _filetime_value(value: "wintypes.FILETIME") -> int:
            return (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)

        if not await_exit_time:
            # Creation-only: the caller asks which process object this pid names,
            # and the creation FILETIME answers that on its own. Skipping the exit
            # half keeps this read free of the wait and the poll below, which is
            # what lets an event-loop caller use it directly.
            creation_only = _filetime_value(creation)
            return (pid, creation_only, None) if creation_only > 0 else None
        # A process may exit with code 259, the value STILL_ACTIVE reserves, so
        # GetExitCodeProcess alone cannot decide liveness: such a child reads back
        # as running for as long as a handle to it is held, and a drain waiting for
        # its exit never finishes while its reservation stays charged. A zero-timeout
        # wait answers from the object's signal state, which carries no collision:
        # a signalled process object is a terminated one. Handles opened for query
        # alone lack SYNCHRONIZE and cannot be waited on; those callers read identity
        # without draining anything, so they keep the exit-code reading rather than
        # losing the answer, and every handle a drain retains is opened to wait.
        still_active = 259
        wait_object_0 = 0x00000000
        wait_timeout = 0x00000102
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        waited = int(kernel32.WaitForSingleObject(process_handle, 0))
        if waited == wait_object_0:
            active = False
        elif waited == wait_timeout:
            active = True
        else:
            active = exit_code.value == still_active
        # The exit FILETIME is not defined for a live process. If the status
        # says the process exited, read the times again after that observation
        # so the returned exit bound belongs to the terminated object.
        if not active and not _read_times():
            return None

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


def process_matches(pid: int, needles: tuple[str, ...]) -> bool:
    """Return True iff *pid*'s command line / image name contains any *needle*.

    It guards against PID recycling before a tracked process is killed.
    Linux: ``/proc/<pid>/cmdline``. macOS: ``ps -o command=``.
    Windows: the image name from ``CreateToolhelp32Snapshot`` (the full command
    line is not cheaply available). A harness hosted by an interpreter reads as
    that interpreter's image there — a Node-hosted ACP adapter is ``node.exe`` —
    so a needle naming the adapter itself cannot match on Windows, and a caller
    that needs a per-harness answer reads a recorded start identity instead
    (``get_process_start_id``). Returns False on any failure.
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


def process_image_name(pid: int) -> str:
    """The process image (exe) name for *pid* on Windows, ``""`` anywhere else.

    Exposed because an image name is the only per-process identity Windows offers
    cheaply, and a caller that wants to compare it EXACTLY cannot use
    :func:`process_matches`, whose Windows arm is a substring test. Off Windows the
    answer is ``""`` rather than a POSIX equivalent: the platforms that have a real
    command line should read that instead of a name that would drop the interpreter
    distinction.
    """
    if not IS_WINDOWS:
        return ""
    return _win_process_image_name(pid) or ""


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


def process_argv_matches_exact(pid: int, expected_argv: Sequence[str]) -> bool:
    """Return True iff *pid*'s FULL command line is exactly *expected_argv*.

    The strict identity check behind reclaiming a child this process's own
    lineage spawned and then lost (a recorded pid surviving a supervisor
    hard-kill): a recorded pid may have been recycled onto an unrelated
    process, and :func:`process_matches`-style substring needles cannot tell
    the two apart — partial argv matching against the process table is exactly
    what once killed forwards operators had started themselves. So the whole
    argv must match, element for element, and every failure answers False.

    For a DESTRUCTIVE decision this check must be paired with a
    :func:`process_start_time` pin recorded when the child was spawned: argv
    equality alone cannot rule out a recycled pid running an identical
    command line, and on macOS the comparison basis below makes equality
    necessary but not sufficient for vector equality. The pair fails toward
    "do not signal" on either mismatch.

    Linux: ``/proc/<pid>/cmdline`` NUL-split and compared element-wise (an
    empty cmdline — a zombie — never matches). macOS: ``ps -ww -o command=``
    reports the argv space-joined, so the comparison is against
    ``" ".join(expected_argv)``; exact only when no expected element contains
    a space, which holds for the argv shapes this guards (option tokens and
    validated host/target strings).

    Windows: a process has no argv vector, only the one command-line string
    ``CreateProcess`` was given. ``subprocess`` builds that string from a list
    with :func:`subprocess.list2cmdline`, so the check is that the live
    ``Win32_Process.CommandLine`` (read by :func:`process_command_line`, whose
    only interpolated value is the int pid) equals
    ``list2cmdline(expected_argv)`` character for character. This proves the
    string, not the vector: two argv lists that quote to the same string are
    indistinguishable, and a process may rewrite its own command line after
    start. An unreadable command line (access denied, process gone, WMI
    failure) answers False. A target launched through a ``.cmd``/``.bat`` shim
    runs under ``cmd.exe`` with a different command line, so it never matches
    and is never signalled.
    """
    if type(pid) is not int or pid <= 1 or not expected_argv:
        return False
    if IS_WINDOWS:
        try:
            expected = subprocess.list2cmdline([str(a) for a in expected_argv])
            actual = process_command_line(pid)
        except Exception:
            return False
        return bool(actual) and actual == expected
    try:
        if sys.platform == "linux":
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
            if not raw:
                return False  # zombie / kernel thread: no argv to confirm
            parts = raw.split(b"\0")
            if parts and parts[-1] == b"":
                parts.pop()  # trailing NUL terminator
            return parts == [a.encode() for a in expected_argv]
        if sys.platform == "darwin":
            ps_bin = trusted_system_bin("ps")
            if ps_bin is None:
                return False
            out = subprocess.check_output(
                [ps_bin, "-ww", "-o", "command=", "-p", str(pid)],
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
            return out.decode(errors="replace").strip() == " ".join(expected_argv)
    except Exception:
        return False
    return False


def process_argv_fingerprint(pid: int) -> str | None:
    """Return a sha256 hex fingerprint of *pid*'s live argv, or None if unreadable.

    The point is ONE basis for "recorded" and "observed": the caller takes the
    fingerprint of its own child right after spawning it, and later takes it
    again from whatever runs at the recorded pid. Equal fingerprints mean the
    kernel reports the same command line both times, whatever the kernel did to
    it (a shebang ``aws`` entrypoint shows up as ``<interpreter> /path/aws ...``
    in both reads). Comparing a stored fingerprint never depends on rebuilding
    the command line from settings that may have changed since.

    Linux: ``/proc/<pid>/cmdline`` as the NUL-separated vector. macOS: the
    ``KERN_PROCARGS2`` vector from :func:`darwin_process_argv`, NUL-joined.
    Windows answers None, so a Windows record keeps the exact command-line
    check, which never matches a ``.cmd``/``.bat`` shim. An empty, unreadable or
    unsupported argv answers None, never a fingerprint of nothing.
    """
    if IS_WINDOWS or type(pid) is not int or pid <= 1:
        return None
    try:
        if sys.platform == "linux":
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
            if raw.endswith(b"\0"):
                raw = raw[:-1]  # trailing NUL terminator
        elif sys.platform == "darwin":
            argv = darwin_process_argv(pid)
            if not argv:
                return None
            raw = b"\0".join(a.encode("utf-8", errors="surrogatepass") for a in argv)
        else:
            return None
    except Exception:
        return None
    if not raw:
        return None  # zombie / kernel thread: no argv to fingerprint
    return hashlib.sha256(raw).hexdigest()


def linux_process_name(pid: int, *, proc_root: Path | None = None) -> str | None:
    """Return Linux ``comm`` for *pid*, or ``None`` when it is unproven.

    The kernel caps ``comm`` and exposes it independently of environ, which
    makes it suitable only for conservative process-class decisions. *proc_root*
    is a test seam for fixture-owned process tables.
    """
    if sys.platform != "linux":
        return None
    root = proc_root if proc_root is not None else Path("/proc")
    try:
        name = (
            (root / str(pid) / "comm")
            .read_bytes()
            .decode("utf-8", errors="surrogateescape")
            .strip()
        )
    except OSError:
        return None
    return name or None


def process_cgroups_match(
    pid: int,
    reference_pid: int,
    *,
    proc_root: Path | None = None,
) -> bool | None:
    """Compare two Linux processes' stable unified-cgroup memberships.

    Returns ``True`` for equal cgroup v2 memberships, ``False`` for proven
    different memberships, and ``None`` off Linux, on cgroup v1, or when either
    read is unavailable or changes during the probe. Reading both files twice
    prevents a concurrent cgroup move from licensing a destructive action.
    *proc_root* is a test seam for fixture-owned process tables.
    """
    if sys.platform != "linux":
        return None
    root = proc_root if proc_root is not None else Path("/proc")

    def read_membership(process_pid: int) -> str | None:
        raw = (
            (root / str(process_pid) / "cgroup")
            .read_bytes()
            .decode("utf-8", errors="surrogateescape")
        )
        lines = tuple(line for line in raw.splitlines() if line)
        if len(lines) != 1 or not lines[0].startswith("0::/"):
            return None
        return lines[0]

    try:
        pid_first = read_membership(pid)
        reference_first = read_membership(reference_pid)
        pid_second = read_membership(pid)
        reference_second = read_membership(reference_pid)
    except OSError:
        return None
    if (
        pid_first is None
        or reference_first is None
        or pid_first != pid_second
        or reference_first != reference_second
    ):
        return None
    return pid_first == reference_first


def process_command_line(pid: int) -> str:
    """Return the full command line of *pid*, or ``""`` on failure (best-effort).

    Linux: ``/proc/<pid>/cmdline`` (NUL-joined → spaces).
    macOS: ``ps -o command= -p <pid>``.
    Windows: ``Win32_Process.CommandLine`` via WMI (PowerShell ``Get-CimInstance``).
    It confirms a listener PID is actually a Kiro Crew gateway when the image
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
                text=True,  # subprocess-encoding: locale -- darwin ps writes argv in the locale
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
            # A command line is arbitrary user text (an install path under a
            # non-ASCII account name is the common case), and PowerShell is one
            # of the console-encoding children ``subprocess_utf8`` says NOT to
            # decode as UTF-8: it writes stdout in ``[Console]::OutputEncoding``,
            # so pinning UTF-8 on this end alone raises UnicodeDecodeError on a
            # legacy code page. Set the child's output encoding instead, which
            # makes the encoding KNOWN and so brings the site inside that
            # module's own precondition. UTF8Encoding(false) rather than
            # ``[Text.Encoding]::UTF8`` so no host can prepend a BOM to the
            # first field. Without this the code page silently best-fits an
            # unrepresentable character away (cp950 turns "é" into "e"), and the
            # callers below compare the result to decide whether a listening PID
            # is our own gateway -- a corrupted string fails that check quietly.
            out = subprocess.check_output(
                [
                    powershell_bin,
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
                    f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}')"
                    ".CommandLine",
                ],
                stderr=subprocess.DEVNULL,
                timeout=10,
                creationflags=_SUBPROCESS_NO_WINDOW,
                **UTF8_TEXT,
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

    It confirms that a pid a client is about to trust belongs to the calling
    user (see ``port_resolution._gateway_owns_port``), which is what makes pid
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
                text=True,  # subprocess-encoding: locale -- darwin ps output follows the locale
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
            raw = out.strip()
            return int(raw) if raw.isdigit() else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return None


def effective_uid() -> int | None:
    """Return this process's effective uid, or ``None`` where there is no uid concept.

    Windows has no ``os.geteuid``; a caller that formats a per-user path from the
    uid (``/run/user/<uid>``) must treat ``None`` as "no such path on this
    platform" rather than guess a number.
    """
    if not IS_POSIX:
        return None
    return os.geteuid()


#: Seconds before the ``ps`` start-time probe is abandoned. Only the BSD leg
#: spawns anything; Linux reads /proc and Windows calls the kernel directly.
_START_TIME_PS_TIMEOUT = 2


def _process_lstart(pid: int) -> str | None:
    """Read the one-second ``ps -o lstart=`` identity encoding."""
    ps_bin = trusted_system_bin("ps")
    if ps_bin is None:
        return None
    try:
        out = subprocess.check_output(
            [ps_bin, "-o", "lstart=", "-p", str(pid)],
            stderr=subprocess.DEVNULL,
            timeout=_START_TIME_PS_TIMEOUT,
        )
        # STRICT decode. A lossy one can alias two unreadable identities.
        return out.decode().strip() or None
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return None


def process_start_time(pid: int) -> str | None:
    """Stable identity for WHEN *pid* started, or ``None`` when unreadable.

    An opaque token whose only contract is that it compares equal across gateway
    generations on the same host while the PID still names the same process
    object, and unequal once that PID has been recycled onto another. Units
    differ per platform and are deliberately not normalised -- nothing ever
    compares one host's value against another's. One caller parses the
    Windows token: ``session_pid._pid_age_seconds`` turns that FILETIME into a
    process age, so a change to the Windows encoding must change that parser too.

    Callers use it as a PID-reuse guard before signalling, so an unreadable
    value must fail SAFE: ``None`` means "identity unconfirmed", which every
    caller treats as "do not kill".

    * **Linux** -- ``/proc/<pid>/stat`` field 22 (start time in clock ticks
      since boot): monotonic, locale-independent, and far finer than 1s, so
      same-second reuse cannot alias.
    * **Windows** -- the process creation ``FILETIME`` (100-ns units), read
      through a QUERY-ONLY handle. Terminate rights are deliberately NOT
      requested: this value is what decides whether a kill may happen at all, so
      demanding the right to kill in order to read it would refuse the guard for
      exactly the processes a caller must be most careful about.
    * **macOS / other POSIX** -- ``ps -o lstart=`` (1s resolution, locale/TZ
      formatted). Coarser, so a format or resolution drift can only make the
      guard decline to act, never act on the wrong process.
    """
    if sys.platform == "linux":
        stat = read_proc_stat(pid)
        if stat is None or stat.start_ticks is None:
            return None
        return str(stat.start_ticks)
    if IS_WINDOWS:
        # Opened and closed through the shared seams so this READ and the
        # identity-pinned TERMINATE below cannot drift in how they acquire or
        # release the handle -- the difference between the two is the handle's
        # LIFETIME, and that is easier to reason about with one acquisition site.
        handle = _open_process_query_handle(pid)
        if handle is None:
            return None
        try:
            identity = _windows_process_handle_identity(handle, await_exit_time=False)
        finally:
            _close_process_handle(handle)
        # (pid, creation_time, exit_time) -- only the creation half is an
        # identity; exit_time moves as the process dies. Asking for the creation
        # half alone keeps this read non-blocking, which is the contract
        # :func:`get_process_start_id` publishes to its event-loop callers.
        return str(identity[1]) if identity is not None else None
    return _process_lstart(pid)


def process_start_id_for_source(pid: int, source: ProcessIdentitySource) -> str | None:
    """Re-read *pid* with the same identity source used at capture time.

    There is deliberately no fallback between sources. ``WINDOWS`` reads
    process creation time through the query-only process handle; ``ATOMIC`` uses
    the native high-resolution start-ID reader, and ``LSTART`` uses ``ps``. If
    that source is now unavailable, the identity is inconclusive rather than
    compared against a token with a different encoding or resolution.
    """
    if source is ProcessIdentitySource.ATOMIC:
        return get_process_start_id(pid)
    if source is ProcessIdentitySource.WINDOWS:
        return process_start_time(pid)
    if source is ProcessIdentitySource.LSTART:
        return _process_lstart(pid)
    return None


#: (pid, token) cache for :func:`own_process_start_time`. Keyed by PID rather
#: than a bare value so a forked child re-reads its OWN identity instead of
#: inheriting the parent's — the OTEL SDK re-installs metric exporters in fork
#: children via ``os.register_at_fork``, so children genuinely export under
#: this token. Two threads racing the first read is benign: both compute the
#: same immutable tuple for the same process.
_OWN_START_TIME: tuple[int, str | None] | None = None


def _linux_boot_id() -> str | None:
    """The kernel's per-boot UUID, or ``None`` when unreadable."""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip() or None
    except OSError:
        return None


def _own_identity_token(pid: int) -> str | None:
    """Reboot-unique start-time token for THIS process, or ``None``.

    The aggregator DISABLES its value-drop reset heuristic for any stream that
    carries a token, trusting one token = one OS process. A token that cannot
    honor that contract is therefore worse than no token — an aliased coarse
    token would merge two lifetimes AND mute the heuristic that catches the
    merge — so every degraded read returns ``None`` (no identity field, legacy
    heuristic applies) rather than a best-effort value:

    * **Linux** — ``/proc`` start ticks count from BOOT, so a post-reboot
      process can repeat an earlier boot's (PID, ticks) pair; metric shards
      outlive boots. The kernel's per-boot UUID makes the pair reboot-unique;
      without it, no token.
    * **macOS** — ``proc_pidinfo`` reports the absolute start instant at
      microsecond resolution, so a recycled PID cannot alias within the 1s
      window the ``ps -o lstart=`` probe cannot see past. Without ``libproc``,
      no token — the 1s probe is exactly such an aliasable coarse source.
    * **Windows** — the creation ``FILETIME`` (100ns units since 1601) is
      absolute, already reboot-unique and alias-proof.
    * **Other POSIX** — only the 1s ``ps`` probe exists: no token.
    """
    if sys.platform == "linux":
        ticks = process_start_time(pid)
        boot = _linux_boot_id()
        return f"{ticks}:{boot}" if ticks and boot else None
    if sys.platform == "darwin":
        return _darwin_process_start_microtime(pid)
    if IS_WINDOWS:
        return process_start_time(pid)
    return None


def process_incarnation_id(pid: int) -> str | None:
    """Read a reboot-unique process token without the caller-lifetime cache."""
    return _own_identity_token(pid)


def own_process_start_time() -> str | None:
    """This process's own start-time identity, read once and cached.

    A module-scope cache of :func:`_own_identity_token` for the calling
    process. The cache is the contract, not an optimisation: every reader
    inside one process must observe the SAME token for the process lifetime,
    so a metrics provider rebuilt in-process (telemetry off/on) stamps records
    that stitch into one stream with those written before the rebuild — and a
    read that degrades mid-process (a ``libproc`` load failing on one call)
    must not flip the process between stamped and unstamped forms.

    Fail soft: an unreadable or alias-prone platform answer is cached as
    ``None`` for the process lifetime, so a consumer emits no identity at all
    rather than an identity that flaps between absent and present.
    """
    global _OWN_START_TIME
    pid = os.getpid()
    if _OWN_START_TIME is None or _OWN_START_TIME[0] != pid:
        _OWN_START_TIME = (pid, _own_identity_token(pid))
    return _OWN_START_TIME[1]
