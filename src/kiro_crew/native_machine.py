"""WMI-free ``platform.machine()`` for Windows (issue #21632).

CPython 3.11+ answers ``platform.machine()`` by querying WMI
(``platform._wmi_query`` -> ``Win32_Processor.Architecture``). The query runs
on a helper thread with a short fixed timeout; when WMI is slow -- a dozen
processes asking at once at gateway start is enough -- the stdlib takes the
env-var fallback and abandons the helper thread, which then lives for the life
of the process (python/cpython#125315; the crash half was fixed in 3.12.8, the
abandoned thread was not). Long-lived KiroCrew processes burned two cores each
idle (issue #21632: the hot threads are inside ``wbemprox.dll``, not the
interpreter).

This module installs, once at ``kiro_crew`` import time, a
``platform._wmi_query`` that answers the one query the stdlib makes for the
machine string -- ``('CPU', 'Architecture')`` -- from ``IsWow64Process2``
(kernel32, Windows 10 1511+). That call is microseconds, needs no WMI service,
and reports the real chip under emulation, which is the one thing WMI gave
over ``PROCESSOR_ARCHITECTURE``. Every other query raises ``OSError`` -- the
exact failure the stdlib already handles at each call site (the OS-version
query falls through to its own ``sys.getwindowsversion`` path). Off Windows
the install is a no-op.
"""

from __future__ import annotations

import os
import sys

#: IMAGE_FILE_MACHINE_* values IsWow64Process2 reports, spelled the way
#: ``platform.machine()`` spells them on Windows.
_IMAGE_FILE_MACHINE = {0x014C: "x86", 0x01C4: "ARM", 0x8664: "AMD64", 0xAA64: "ARM64"}

#: Win32_Processor.Architecture codes the stdlib maps back to those names.
_WMI_ARCH_CODE = {"x86": 0, "ARM": 5, "AMD64": 9, "ARM64": 12}


def native_machine() -> str:
    """Return what ``platform.machine()`` would, without WMI."""
    if sys.platform != "win32":
        import platform

        return platform.machine()
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # GetCurrentProcess() returns a 64-bit HANDLE: without these
        # declarations ctypes truncates it to a 32-bit int and raises
        # OverflowError, which would break every platform.machine() caller.
        get_current_process = k32.GetCurrentProcess
        get_current_process.restype = wintypes.HANDLE
        get_current_process.argtypes = []
        is_wow64_process2 = k32.IsWow64Process2
        is_wow64_process2.restype = wintypes.BOOL
        is_wow64_process2.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.USHORT),
            ctypes.POINTER(wintypes.USHORT),
        ]
        process_machine = wintypes.USHORT(0)
        native = wintypes.USHORT(0)
        if is_wow64_process2(
            get_current_process(), ctypes.byref(process_machine), ctypes.byref(native)
        ):
            name = _IMAGE_FILE_MACHINE.get(native.value)
            if name:
                return name
    except Exception:
        # Pre-1511 kernel32 without IsWow64Process2, or any ctypes fault:
        # fall through to the env vars rather than surface to the caller.
        pass
    return (
        os.environ.get("PROCESSOR_ARCHITEW6432", "")
        or os.environ.get("PROCESSOR_ARCHITECTURE", "")
        or "unknown"
    )


def _wmi_query_without_wmi(table: str, *keys: str) -> list[str]:
    if table == "CPU" and tuple(keys) == ("Architecture",):
        code = _WMI_ARCH_CODE.get(native_machine())
        if code is not None:
            return [str(code)]
    # platform.py catches OSError at every _wmi_query call site and falls back
    # to its non-WMI sources (sys.getwindowsversion for the OS version, the
    # PROCESSOR_* env vars for the machine), so raising is the honest answer
    # for everything the fast path above does not serve.
    raise OSError("WMI disabled by kiro_crew.native_machine")


def install_platform_override() -> bool:
    """Patch ``platform._wmi_query`` on Windows; a no-op elsewhere.

    Idempotent. Returns True when the override is (already) installed, False
    when this stdlib has no ``_wmi_query`` to patch (Python < 3.11, where
    ``machine()`` never touched WMI in the first place).
    """
    if sys.platform != "win32":
        return False
    import platform

    if getattr(platform, "_wmi_query", None) is _wmi_query_without_wmi:
        return True
    if not hasattr(platform, "_wmi_query"):
        return False
    setattr(platform, "_wmi_query", _wmi_query_without_wmi)
    return True
