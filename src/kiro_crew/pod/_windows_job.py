"""Pod-only Windows lifetime containment; no resource-limit policy.

The caller assigns its original Popen handle BEFORE resuming a CREATE_SUSPENDED
child. Assignment cannot retroactively contain descendants of a running process.
A named job survives its publisher while members remain; opening never creates
an empty replacement. A successful drain covers job members, not outside launch
brokers, and authorizes reclamation only after the caller retires the publisher
and validates the durable run identity. The windows backend and _windows_run
own that publication and retirement protocol; resource ceilings stay separate.
"""

from __future__ import annotations

import ctypes as C
import math
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

from kiro_crew import platform_compat as pc

# Task Scheduler and the CLI can occupy different Windows sessions. Global job
# names cross that boundary; the owner-only DACL, not the namespace, grants access.
_NAME_PREFIX = "Global\\KiroCrew.Pod."
_NAME_RE = re.compile(re.escape(_NAME_PREFIX) + r"[0-9a-f]{32}\Z")
_JOB_QUERY = 0x0004
_JOB_TERMINATE = 0x0008
_JOB_ALL_ACCESS = 0x001F003F
_ERROR_ALREADY_EXISTS = 183
_EXTENDED_LIMITS = 9
_BASIC_ACCOUNTING = 1
# Neither breakaway nor last-handle closure may change lifetime containment.
_FORBIDDEN_LIMITS = 0x0800 | 0x1000 | 0x2000
_POLL_SECONDS = 0.05
_DWORD = C.c_uint32
_BOOL = C.c_int32
_HANDLE = C.c_void_p


class _SecurityAttributes(C.Structure):
    _fields_ = [("length", _DWORD), ("descriptor", C.c_void_p), ("inherit", _BOOL)]


class _Accounting(C.Structure):
    _fields_ = [
        ("total_user_time", C.c_int64),
        ("total_kernel_time", C.c_int64),
        ("period_user_time", C.c_int64),
        ("period_kernel_time", C.c_int64),
        ("page_faults", _DWORD),
        ("total_processes", _DWORD),
        ("active_processes", _DWORD),
        ("terminated_processes", _DWORD),
    ]


def _error(operation: str) -> OSError:
    getter = getattr(C, "get_last_error", lambda: 0)
    return OSError(int(getter()), f"Windows pod job: {operation} failed")


def _last_error() -> int:
    return int(getattr(C, "get_last_error", lambda: 0)())


def _load() -> tuple[Any, Any]:
    if not pc.IS_WINDOWS:
        raise OSError("Windows pod jobs require Windows")
    loader = getattr(C, "WinDLL")
    kernel = loader("kernel32", use_last_error=True)
    advapi = loader("advapi32", use_last_error=True)
    signatures = {
        "CreateJobObjectW": ([C.POINTER(_SecurityAttributes), C.c_wchar_p], _HANDLE),
        "OpenJobObjectW": ([_DWORD, _BOOL, C.c_wchar_p], _HANDLE),
        "AssignProcessToJobObject": ([_HANDLE, _HANDLE], _BOOL),
        "IsProcessInJob": ([_HANDLE, _HANDLE, C.POINTER(_BOOL)], _BOOL),
        "SetInformationJobObject": ([_HANDLE, C.c_int, C.c_void_p, _DWORD], _BOOL),
        "QueryInformationJobObject": (
            [_HANDLE, C.c_int, C.c_void_p, _DWORD, C.POINTER(_DWORD)],
            _BOOL,
        ),
        "TerminateJobObject": ([_HANDLE, _DWORD], _BOOL),
        "OpenProcess": ([_DWORD, _BOOL, _DWORD], _HANDLE),
        "WaitForSingleObject": ([_HANDLE, _DWORD], _DWORD),
        "TerminateProcess": ([_HANDLE, _DWORD], _BOOL),
        "CloseHandle": ([_HANDLE], _BOOL),
        "LocalFree": ([C.c_void_p], C.c_void_p),
    }
    for name, (args, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = args, result
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        C.c_wchar_p,
        _DWORD,
        C.POINTER(C.c_void_p),
        C.POINTER(_DWORD),
    ]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = _BOOL
    return kernel, advapi


def _process_handle(value: int) -> _HANDLE:
    if type(value) is not int or value <= 0 or value == C.c_void_p(-1).value:
        raise ValueError("an exact, caller-owned process handle is required")
    return _HANDLE(value)


@dataclass
class PodJob:
    """Own one non-inheritable job handle; close explicitly or via a with block.

    Process handles are BORROWED, never opened by PID or closed here. Assignment
    requires SET_QUOTA | TERMINATE; membership requires QUERY_INFORMATION or
    QUERY_LIMITED_INFORMATION. Popen's original Windows handle supplies these.
    The job name is a locator, not authentication or proof of the pod's identity.
    """

    name: str
    _handle: int
    _kernel: Any

    @classmethod
    def create(cls) -> PodJob:
        """Create a unique owner-only job, refusing collisions and ACL failures."""
        kernel, advapi = _load()
        sid = pc.current_user_sid()
        if not sid or re.fullmatch(r"S-1-\d+(?:-\d+)+", sid) is None:
            raise OSError("Windows pod job owner SID is unavailable")
        descriptor = C.c_void_p()
        sddl = f"O:{sid}D:P(A;;0x{_JOB_ALL_ACCESS:08x};;;{sid})"
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, C.byref(descriptor), None
        ):
            raise _error("owner-only security descriptor")
        try:
            attributes = _SecurityAttributes(C.sizeof(_SecurityAttributes), descriptor, False)
            name = _NAME_PREFIX + uuid.uuid4().hex
            handle = kernel.CreateJobObjectW(C.byref(attributes), name)
            error = _last_error()  # Capture before LocalFree or any other native call.
        finally:
            kernel.LocalFree(descriptor)
        if not handle:
            raise OSError(error, "Windows pod job creation failed")
        job = cls(name, int(handle), kernel)
        try:
            if error == _ERROR_ALREADY_EXISTS:
                raise OSError(error, "Windows pod job name already exists")
            limits = pc._JobObjectExtendedLimitInformation()
            if not kernel.SetInformationJobObject(
                job._native_handle(), _EXTENDED_LIMITS, C.byref(limits), C.sizeof(limits)
            ):
                raise _error("non-breakaway policy")
            job._check_limits()
            return job
        except BaseException:
            job.close()
            raise

    @classmethod
    def open_existing(cls, name: str) -> PodJob:
        """Open a recorded job with query/terminate rights only; absence raises."""
        if not isinstance(name, str) or _NAME_RE.fullmatch(name) is None:
            raise ValueError("invalid Windows pod job name")
        kernel, _advapi = _load()
        handle = kernel.OpenJobObjectW(_JOB_QUERY | _JOB_TERMINATE, False, name)
        if not handle:
            raise _error("open existing job")
        job = cls(name, int(handle), kernel)
        try:
            job._check_limits()
            return job
        except BaseException:
            job.close()
            raise

    def _native_handle(self) -> _HANDLE:
        if not self._handle:
            raise ValueError("Windows pod job handle is closed")
        return _HANDLE(self._handle)

    def _query(self, kind: int, result: C.Structure) -> None:
        returned = _DWORD()
        if not self._kernel.QueryInformationJobObject(
            self._native_handle(), kind, C.byref(result), C.sizeof(result), C.byref(returned)
        ):
            raise _error("query job")
        if returned.value != C.sizeof(result):
            raise OSError("Windows pod job query returned incomplete information")

    def _check_limits(self) -> None:
        limits = pc._JobObjectExtendedLimitInformation()
        self._query(_EXTENDED_LIMITS, limits)
        if limits.BasicLimitInformation.LimitFlags & _FORBIDDEN_LIMITS:
            raise OSError("Windows pod job permits breakaway or kill-on-close")

    def assign_suspended(self, process_handle: int) -> None:
        """Assign a caller-owned, never-resumed child; suspension is a precondition.

        This API does not suspend or resume and cannot prove past execution from
        a process handle. The spawning caller must enforce CREATE_SUSPENDED.
        """
        process = _process_handle(process_handle)
        self._check_limits()
        if not self._kernel.AssignProcessToJobObject(self._native_handle(), process):
            raise _error("assign suspended process")
        if not self.contains(process_handle):
            raise OSError("Windows pod job assignment did not establish membership")

    def contains(self, process_handle: int) -> bool:
        """Query exact membership. An API error is unknown, never False."""
        process = _process_handle(process_handle)
        member = _BOOL()
        if not self._kernel.IsProcessInJob(process, self._native_handle(), C.byref(member)):
            raise _error("query process membership")
        return bool(member.value)

    def active_count(self) -> int:
        """Read kernel accounting, including descendants of exited intermediaries."""
        result = _Accounting()
        self._query(_BASIC_ACCOUNTING, result)
        return int(result.active_processes)

    def terminate_and_wait(self, *, timeout: float) -> None:
        """Terminate members and require a successful zero count within timeout.

        The caller must retire all external producers before spending this proof.
        No completion notification, PID scan, or process-query failure means zero.
        """
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("job drain timeout must be finite and non-negative")
        deadline = time.monotonic() + timeout
        if not self._kernel.TerminateJobObject(self._native_handle(), 1):
            raise _error("terminate job")
        while self.active_count():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Windows pod job still has active processes")
            time.sleep(min(_POLL_SECONDS, remaining))

    def close(self) -> None:
        """Close once; failure raises and keeps ownership available for retry."""
        if self._handle:
            if not self._kernel.CloseHandle(self._native_handle()):
                raise _error("close job")
            self._handle = 0

    def __enter__(self) -> PodJob:
        self._native_handle()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def open_identity(pid: int, token: str) -> int | None:
    """Pin an identity or prove it gone/replaced; access and query errors raise."""
    if type(pid) is not int or pid <= 1 or not token.isascii() or not token.isdigit():
        raise ValueError("invalid Windows process identity")
    kernel, _advapi = _load()
    # TERMINATE | QUERY_LIMITED_INFORMATION | SYNCHRONIZE, never SET_QUOTA.
    handle = kernel.OpenProcess(0x00101001, False, pid)
    if not handle:
        error = _last_error()
        if error == 87:  # ERROR_INVALID_PARAMETER: this valid PID does not exist.
            return None
        raise OSError(error, "Windows pod process identity could not be opened")
    handle = int(handle)
    try:
        identity = pc._windows_process_handle_identity(handle)
        if identity is None:
            raise OSError("Windows pod process identity could not be queried")
        if identity[:2] == (pid, int(token)):
            return handle
    except BaseException:
        close_identity(handle)
        raise
    close_identity(handle)
    return None  # The pinned object positively proves PID reuse; never signal it.


def retire_identity(handle: int, *, timeout: float) -> None:
    """End the exact publisher and wait for its signaled process object."""
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError("publisher timeout must be finite and non-negative")
    kernel, _advapi = _load()
    native = _process_handle(handle)
    result = kernel.WaitForSingleObject(native, 0)
    if result == 0:
        return
    if result != 258:  # WAIT_TIMEOUT is the only positive evidence of a live object.
        raise _error("query publisher exit")
    if not kernel.TerminateProcess(native, 1):
        # It can exit between wait and terminate. Only a signaled object clears that error.
        if kernel.WaitForSingleObject(native, 0) != 0:
            raise _error("terminate publisher")
    result = kernel.WaitForSingleObject(native, min(int(timeout * 1000), 0xFFFFFFFE))
    if result == 258:
        raise TimeoutError("Windows pod publisher did not retire")
    if result != 0:
        raise _error("wait for publisher retirement")


def close_identity(handle: int) -> None:
    kernel, _advapi = _load()
    if not kernel.CloseHandle(_process_handle(handle)):
        raise _error("close process identity")
