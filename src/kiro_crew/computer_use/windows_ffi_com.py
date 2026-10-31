"""The COM and Win32 binding layer of :mod:`kiro_crew.computer_use.windows_ffi`.

The ABI constants and structures; the two declarative tables, :data:`_FN_SPECS` for the
flat Win32 functions and :data:`_VTABLE`, the one place a COM slot index may appear; the
library loader and :func:`vcall`; and the COM reference release (:func:`release`) the
GDI+ capture stream needs. The UI Automation client is gone: agent-desktop reads the
tree.

:mod:`kiro_crew.computer_use.windows_ffi` stays the import path and the patch surface for
every name defined here. The hazards the comments below cite by number ("hazard 6 in the
module docstring") are the ones its docstring lists.
"""

from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import wintypes  # type aliases only; imports cleanly on every platform
from dataclasses import dataclass
from typing import Any, Sequence

from kiro_crew import platform_compat
from kiro_crew.computer_use.types import ComputerUseUnsupported

# Logs under the facade's name, which operators and tests filter on.
logger = logging.getLogger("kiro_crew.computer_use.windows_ffi")

#: ``ctypes.get_last_error`` / ``set_last_error`` exist only in the Windows ctypes
#: stubs, and CI runs mypy on Linux. Reached through an ``Any``-typed alias — the
#: same approach ``mcp_gateway/transport.py`` takes — so a POSIX type check does not
#: flag a call site that only ever runs on Windows.
_ct: Any = ctypes

# ── COM plumbing constants ──
S_OK = 0

# ── DPI awareness contexts (SetThreadDpiAwarenessContext) ──
# Negative sentinels, passed as handles.
DPI_AWARENESS_CONTEXT_UNAWARE = -1
DPI_AWARENESS_CONTEXT_SYSTEM_AWARE = -2
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE = -3
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4

# ── Win32 misc ──
GA_ROOT = 2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class GUID(ctypes.Structure):
    """A COM GUID. Module scope: see hazard 6 in the module docstring."""

    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


@dataclass(frozen=True)
class WindowInfo:
    """One visible top-level window, as the enumerator saw it.

    ``root_hwnd`` is what confinement and identity key on: a window's DIRECT pid
    and its ``GA_ROOT`` pid legitimately differ (a Chromium child surface is
    hosted by another process), and a pid is not an app identity at all on
    Windows — one broker fronts many packaged apps.
    """

    hwnd: int
    root_hwnd: int
    pid: int
    title: str
    class_name: str
    exe_name: str
    bounds: "tuple[float, float, float, float] | None"


# ── Flat Win32 function table ──
# (library key, symbol, restype, argtypes). Every row carries BOTH, and the bind
# pass raises on a missing argtypes: see hazard 3.
_FN_SPECS: tuple[tuple[str, str, Any, list[Any]], ...] = (
    ("ole32", "CLSIDFromString", ctypes.c_long, [ctypes.c_wchar_p, ctypes.POINTER(GUID)]),
    ("user32", "EnumWindows", wintypes.BOOL, [ctypes.c_void_p, wintypes.LPARAM]),
    ("user32", "IsWindow", wintypes.BOOL, [ctypes.c_void_p]),
    ("user32", "IsWindowVisible", wintypes.BOOL, [ctypes.c_void_p]),
    ("user32", "IsIconic", wintypes.BOOL, [ctypes.c_void_p]),
    ("user32", "GetWindowTextW", ctypes.c_int, [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]),
    ("user32", "GetWindowTextLengthW", ctypes.c_int, [ctypes.c_void_p]),
    ("user32", "GetClassNameW", ctypes.c_int, [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]),
    (
        "user32",
        "GetWindowThreadProcessId",
        wintypes.DWORD,
        [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)],
    ),
    ("user32", "GetAncestor", ctypes.c_void_p, [ctypes.c_void_p, wintypes.UINT]),
    ("user32", "GetWindowRect", wintypes.BOOL, [ctypes.c_void_p, ctypes.POINTER(RECT)]),
    ("user32", "WindowFromPoint", ctypes.c_void_p, [POINT]),
    ("user32", "SetThreadDpiAwarenessContext", ctypes.c_void_p, [ctypes.c_void_p]),
    # The DPI pair below exists for ONE purpose: sizing a capture buffer. See
    # :func:`window_render_scale`.
    ("user32", "GetDpiForWindow", wintypes.UINT, [ctypes.c_void_p]),
    ("user32", "MonitorFromWindow", ctypes.c_void_p, [ctypes.c_void_p, wintypes.DWORD]),
    # DWM cloak state: the half of "is this window on screen" that IsWindowVisible
    # cannot answer. See :func:`window_is_on_screen`.
    (
        "dwmapi",
        "DwmGetWindowAttribute",
        ctypes.c_long,
        [ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD],
    ),
    (
        "shcore",
        "GetDpiForMonitor",
        ctypes.c_long,
        [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.POINTER(wintypes.UINT),
            ctypes.POINTER(wintypes.UINT),
        ],
    ),
    # SendInput: the keyboard path. ``cbSize`` MUST be sizeof(INPUT) — a wrong size
    # is rejected wholesale with ERROR_INVALID_PARAMETER rather than partially
    # applied, which is the safe failure but silent if the return value is dropped.
    ("user32", "SendInput", ctypes.c_uint, [ctypes.c_uint, ctypes.c_void_p, ctypes.c_int]),
    ("user32", "GetSystemMetrics", ctypes.c_int, [ctypes.c_int]),
    ("user32", "GetWindowLongW", ctypes.c_long, [ctypes.c_void_p, ctypes.c_int]),
    ("user32", "GetWindowDC", ctypes.c_void_p, [ctypes.c_void_p]),
    ("user32", "ReleaseDC", ctypes.c_int, [ctypes.c_void_p, ctypes.c_void_p]),
    (
        "user32",
        "PrintWindow",
        wintypes.BOOL,
        [ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT],
    ),
    (
        "kernel32",
        "OpenProcess",
        ctypes.c_void_p,
        [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD],
    ),
    ("kernel32", "CloseHandle", wintypes.BOOL, [ctypes.c_void_p]),
    # Global-memory helpers for draining the GDI+ encode stream. Declared here
    # rather than beside the encoder so every bound symbol in the package sits in
    # one table with an explicit signature.
    ("kernel32", "GlobalSize", ctypes.c_size_t, [ctypes.c_void_p]),
    ("kernel32", "GlobalLock", ctypes.c_void_p, [ctypes.c_void_p]),
    ("kernel32", "GlobalUnlock", wintypes.BOOL, [ctypes.c_void_p]),
    (
        "ole32",
        "CreateStreamOnHGlobal",
        ctypes.c_long,
        [ctypes.c_void_p, wintypes.BOOL, ctypes.POINTER(ctypes.c_void_p)],
    ),
    (
        "ole32",
        "GetHGlobalFromStream",
        ctypes.c_long,
        [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
    ),
    (
        "psapi",
        "GetModuleFileNameExW",
        wintypes.DWORD,
        [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_wchar_p, wintypes.DWORD],
    ),
)

# ── COM vtable table: the ONLY place a slot index may appear ──
# (interface, method, slot, restype, argtypes-after-the-this-pointer). IUnknown only,
# for the stream the GDI+ capture encodes into.
_VTABLE: dict[tuple[str, str], tuple[int, Any, list[Any]]] = {
    ("IUnknown", "Release"): (2, ctypes.c_ulong, []),
}

_LIB_NAMES = ("ole32", "user32", "kernel32", "psapi", "shcore", "dwmapi")

_libs: "dict[str, Any] | None" = None
_libs_lock = threading.Lock()


def _bind(lib: Any, symbol: str, restype: Any, argtypes: "Sequence[Any] | None") -> None:
    """Bind one flat function, refusing an unspecified signature.

    Raises rather than accepting ``argtypes=None``: the ctypes default marshals a
    Python int as a 32-bit C int, which truncates every handle and pointer.
    """
    if argtypes is None:
        raise ComputerUseUnsupported(f"{symbol} declared without argtypes")
    fn = getattr(lib, symbol)
    fn.argtypes = list(argtypes)
    fn.restype = restype


def _libraries() -> "dict[str, Any]":
    """Load and bind the Win32 libraries, once per process.

    The load lives here rather than at module scope so importing this module on
    macOS or Linux costs nothing and cannot fail — which is what keeps the whole
    package importable on the CI fleet.
    """
    global _libs
    with _libs_lock:
        if _libs is not None:
            return _libs
        if not platform_compat.IS_WINDOWS:
            raise ComputerUseUnsupported("the Windows computer-use helper needs Windows")
        loaded: dict[str, Any] = {}
        for name in _LIB_NAMES:
            try:
                # The ignore is required because CI runs mypy on Linux, where
                # ctypes has no WinDLL attribute at all.
                loaded[name] = ctypes.WinDLL(name, use_last_error=True)  # type: ignore[attr-defined]
            except OSError as exc:
                raise ComputerUseUnsupported(f"could not load {name}: {exc}") from exc
        for lib_key, symbol, restype, argtypes in _FN_SPECS:
            try:
                _bind(loaded[lib_key], symbol, restype, argtypes)
            except AttributeError as exc:
                raise ComputerUseUnsupported(f"{lib_key}!{symbol} is unavailable") from exc
        _libs = loaded
        return _libs


def libraries() -> "dict[str, Any]":
    """The bound libraries, raising :class:`ComputerUseUnsupported` off Windows."""
    return _libraries()


def reset_libraries() -> None:
    """Drop the cached handles. For tests and a backend swap."""
    global _libs
    with _libs_lock:
        _libs = None


def available() -> bool:
    """Whether the Windows native surface can be used here. Never raises."""
    if not platform_compat.IS_WINDOWS:
        return False
    try:
        _libraries()
        return True
    except Exception:
        logger.debug("Windows computer-use libraries unavailable", exc_info=True)
        return False


def _guid(text: str) -> GUID:
    libs = _libraries()
    out = GUID()
    if libs["ole32"].CLSIDFromString(text, ctypes.byref(out)) != S_OK:
        raise ComputerUseUnsupported(f"malformed GUID {text}")
    return out


def vcall(ptr: Any, interface: str, method: str) -> Any:
    """Build a callable for *interface*'s *method* on the COM object at *ptr*.

    Resolution is by NAME against :data:`_VTABLE`, never by a literal index at
    the call site. That indirection is the whole point: a mistyped index is an
    access violation with no traceback, and two of the slots in this package's
    working set have a neighbour that returns ``S_OK`` while doing something
    else entirely.
    """
    try:
        slot, restype, argtypes = _VTABLE[(interface, method)]
    except KeyError:
        raise ComputerUseUnsupported(f"no vtable entry for {interface}::{method}") from None
    vtable = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p))[0]
    fn_ptr = ctypes.cast(vtable, ctypes.POINTER(ctypes.c_void_p))[slot]
    proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)  # type: ignore[attr-defined]
    return proto(fn_ptr)


def release(ptr: Any) -> None:
    """Drop a reference, tolerating a NULL and never raising.

    A failure to release leaks one object; raising here would turn a capture's
    cleanup into a failed call.
    """
    if not ptr:
        return
    try:
        vcall(ptr, "IUnknown", "Release")(ptr)
    except Exception:
        logger.debug("COM Release failed", exc_info=True)
