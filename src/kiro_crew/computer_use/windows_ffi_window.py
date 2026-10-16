"""Top-level windows in :mod:`kiro_crew.computer_use.windows_ffi`: DPI scope, enumeration, hit test.

:func:`dpi_awareness_scope`, the thread-scoped awareness that every measurement and click
runs under; the per-window reads (title, class, pid, root, bounds, liveness, on-screen and
minimized state, :func:`window_render_scale`); :func:`quantize_point`, the one rounding the
hit test and the click share; :func:`root_window_at_point`; and :func:`window_list`.

:mod:`kiro_crew.computer_use.windows_ffi` stays the import path and the patch surface for
every name defined here.
"""

from __future__ import annotations

import ctypes
import math
from contextlib import contextmanager
from ctypes import wintypes  # type aliases only; imports cleanly on every platform
from typing import Any, Iterator

from kiro_crew.computer_use.types import ComputerUseUnsupported
from kiro_crew.computer_use.windows_ffi_com import (
    DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2,
    GA_ROOT,
    POINT,
    PROCESS_QUERY_LIMITED_INFORMATION,
    RECT,
    S_OK,
    WindowInfo,
    _ct,
    _libraries,
    logger,
)

# ── DPI ──


@contextmanager
def dpi_awareness_scope() -> Iterator[None]:
    """Run a block per-monitor-DPI-aware, restoring the previous context.

    ONE awareness scope must cover the tree walk, the capture, the confinement
    lookup and the click. UIA rectangles follow the CALLER's awareness rather
    than always being physical pixels, and a mismatch does not merely offset a
    point: a physical coordinate fed to ``WindowFromPoint`` from an unaware
    thread resolved to a DIFFERENT APPLICATION than the same numbers from an
    aware thread. So the confinement check and the click can agree with each
    other while both pointing at the wrong window.

    Thread-scoped rather than process-wide: the process-wide setter is
    irreversible and would change behaviour for the whole gateway, including
    surfaces that have nothing to do with computer use.

    **A FAILED set raises rather than running the block unaware.** The setter
    returns NULL on failure, which is the same falsy value a successfully-restored
    default context can marshal as, so "the set failed" and "there is nothing to
    restore" cannot be told apart by the return value alone — and silently
    proceeding degrades into exactly the unaware state this scope exists to
    prevent, where the confinement check and the click agree with each other about
    the wrong window. ``GetLastError`` distinguishes them, and an unset awareness is
    a refusal (a caller sees ``ComputerUseUnsupported``) rather than a wrong answer.

    The restore is unconditional on a successful set, so this thread's awareness is
    never left changed. That matters because the pooled worker is shared with chat,
    terminal and browse work, and a leaked PER_MONITOR_AWARE_V2 would make later
    ``GetWindowRect`` / ``GetCursorPos`` calls from unrelated code read physical
    rather than virtualized coordinates.
    """
    libs = _libraries()
    setter = getattr(libs["user32"], "SetThreadDpiAwarenessContext", None)
    if setter is None:
        # Pre-1607 / 8.1: the API does not exist, so there is nothing to set and
        # nothing to restore. Reachable only if the symbol is absent while the
        # library bound successfully.
        yield
        return
    _ct.set_last_error(0)
    previous = setter(ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2))
    if not previous:
        error = _ct.get_last_error()
        if error:
            raise ComputerUseUnsupported(
                f"SetThreadDpiAwarenessContext failed (GetLastError={error}); refusing to "
                "measure or click under an unknown DPI awareness"
            )
        # NULL with no error: the previous context marshalled falsy. The set
        # SUCCEEDED, so the block runs aware; there is simply nothing to restore.
        yield
        return
    try:
        yield
    finally:
        try:
            setter(previous)
        except Exception:
            logger.debug("restoring DPI awareness context failed", exc_info=True)


#: ``MONITOR_DEFAULTTONEAREST`` — a window is always on SOME monitor, so the
#: nearest one is the right answer even for a rect parked partly off-screen.
MONITOR_DEFAULTTONEAREST = 2
#: ``MONITOR_DPI_TYPE.MDT_EFFECTIVE_DPI`` — the scaling actually in effect,
#: which is what a rendered size has to be compared against.
MDT_EFFECTIVE_DPI = 0

#: ``DWMWA_CLOAKED``. A NON-ZERO value means the window is composited but not shown:
#: a suspended packaged app, or a window on another virtual desktop. Distinct from
#: minimized, which ``IsIconic`` reports, and from hidden, which ``IsWindowVisible``
#: reports — so all three have to be asked separately.
DWMWA_CLOAKED = 14


# ── Window enumeration ──


def _exe_name(pid: int) -> str:
    """Best-effort image name for *pid*, or ``""``.

    ``PROCESS_QUERY_LIMITED_INFORMATION`` rather than a fuller right: this is the
    least privilege that answers the question, and it succeeds against processes
    a broader open would be denied on.
    """
    libs = _libraries()
    handle = libs["kernel32"].OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(512)
        written = libs["psapi"].GetModuleFileNameExW(handle, None, buf, 512)
        if not written:
            return ""
        path = buf.value or ""
        return path.replace("/", "\\").rsplit("\\", 1)[-1]
    finally:
        libs["kernel32"].CloseHandle(handle)


def window_text(hwnd: int) -> str:
    """The window's title, or ``""``."""
    libs = _libraries()
    handle = ctypes.c_void_p(int(hwnd))
    length = int(libs["user32"].GetWindowTextLengthW(handle))
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    libs["user32"].GetWindowTextW(handle, buf, length + 1)
    return buf.value or ""


def window_class(hwnd: int) -> str:
    """The window's class name, or ``""``."""
    libs = _libraries()
    buf = ctypes.create_unicode_buffer(256)
    if not libs["user32"].GetClassNameW(ctypes.c_void_p(int(hwnd)), buf, 256):
        return ""
    return buf.value or ""


def window_pid(hwnd: int) -> int:
    """The pid owning *hwnd*, or 0."""
    libs = _libraries()
    pid = wintypes.DWORD()
    libs["user32"].GetWindowThreadProcessId(ctypes.c_void_p(int(hwnd)), ctypes.byref(pid))
    return int(pid.value)


def window_root(hwnd: int) -> int:
    """The top-level ancestor of *hwnd*, or *hwnd* itself."""
    libs = _libraries()
    root = libs["user32"].GetAncestor(ctypes.c_void_p(int(hwnd)), GA_ROOT)
    return int(root or int(hwnd))


def window_bounds(hwnd: int) -> "tuple[float, float, float, float] | None":
    """``(left, top, width, height)`` for *hwnd*, or ``None``.

    Top-left convention, matching every other coordinate in this package.
    """
    libs = _libraries()
    rect = RECT()
    if not libs["user32"].GetWindowRect(ctypes.c_void_p(int(hwnd)), ctypes.byref(rect)):
        return None
    return (
        float(rect.left),
        float(rect.top),
        float(rect.right - rect.left),
        float(rect.bottom - rect.top),
    )


def window_is_live(hwnd: int) -> bool:
    """Whether *hwnd* still exists and is visible."""
    libs = _libraries()
    handle = ctypes.c_void_p(int(hwnd))
    return bool(libs["user32"].IsWindow(handle)) and bool(libs["user32"].IsWindowVisible(handle))


def window_is_on_screen(hwnd: int) -> bool:
    """Whether *hwnd* is actually visible to the operator right now.

    Three states pass ``IsWindowVisible`` while being invisible, and each produces a
    window the model can read and photograph but the operator cannot see:

    * **minimized** (``IsIconic``) — the rect is parked at ``-32000, -32000``;
    * **cloaked by DWM** (``DWMWA_CLOAKED``) — a suspended packaged app, or a window
      belonging to another virtual desktop;
    * a zero-area rect, which no click can land inside.

    Fails OPEN on an unreadable cloak attribute: ``DwmGetWindowAttribute`` is absent on
    a host without DWM composition, and treating "cannot ask" as "not on screen" would
    empty the whole application list. The ordinary reads above already exclude the
    common cases, and every downstream gesture is still confined by
    ``apps_windows.hwnd_owns_point``.
    """
    libs = _libraries()
    handle = ctypes.c_void_p(int(hwnd))
    if libs["user32"].IsIconic(handle):
        return False
    cloaked = wintypes.DWORD(0)
    try:
        hr = libs["dwmapi"].DwmGetWindowAttribute(
            handle,
            DWMWA_CLOAKED,
            ctypes.byref(cloaked),
            ctypes.sizeof(cloaked),
        )
    except OSError:
        return True
    if hr == S_OK and cloaked.value:
        return False
    bounds = window_bounds(int(hwnd))
    if bounds is None or bounds[2] <= 0 or bounds[3] <= 0:
        return False
    return True


def window_is_minimized(hwnd: int) -> bool:
    """Whether *hwnd* is minimized, and therefore has no pixels to capture.

    A minimized window is still ``IsWindow`` and still ``IsWindowVisible`` — the
    "visible" flag means "not explicitly hidden", not "on screen" — so liveness
    cannot answer this. Its rect is also parked off-screen (measured at
    ``-32000, -32000``), so a capture succeeds and returns a uniform buffer, which
    the blank-frame gate then correctly rejects with no reason a caller can report.
    """
    libs = _libraries()
    return bool(libs["user32"].IsIconic(ctypes.c_void_p(int(hwnd))))


def window_render_scale(hwnd: int) -> float:
    """How much SMALLER *hwnd* draws itself than its DPI-aware rect. 1.0 when equal.

    **MUST be called inside :func:`dpi_awareness_scope`**, and both DPI reads have
    to happen in the same scope: ``GetDpiForMonitor`` is itself virtualized, so
    reading it unaware reports the window's DPI back and the ratio collapses to 1.0
    — the exact bug this function exists to correct, silently.

    ``PrintWindow`` asks a window to draw itself in ITS OWN coordinate space, not
    the caller's. A DPI-unaware window therefore renders at its logical size into
    whatever buffer it is given, and an aware-sized buffer is left with a black
    margin on two sides — an image that does not map linearly onto the window rect
    the element frames are expressed in. Measured on a 125% monitor: a WinForms
    window (``GetDpiForWindow`` 96, monitor 120) drew 620x392 into both a 620x400
    buffer and a 775x500 one, while a Chromium window (both 120) filled each buffer
    it was handed. So the ratio is the window's DPI over the monitor's, and it is
    1.0 for every per-monitor-aware application.
    """
    libs = _libraries()
    handle = ctypes.c_void_p(int(hwnd))
    window_dpi = int(libs["user32"].GetDpiForWindow(handle))
    monitor = libs["user32"].MonitorFromWindow(handle, MONITOR_DEFAULTTONEAREST)
    if not window_dpi or not monitor:
        # Either read failing means the ratio is unknown; 1.0 keeps the capture at
        # the caller's size, which is correct for an aware window and no worse than
        # the unscaled behaviour for any other.
        return 1.0
    dpi_x = wintypes.UINT(0)
    dpi_y = wintypes.UINT(0)
    hr = libs["shcore"].GetDpiForMonitor(
        ctypes.c_void_p(monitor),
        MDT_EFFECTIVE_DPI,
        ctypes.byref(dpi_x),
        ctypes.byref(dpi_y),
    )
    if hr != S_OK or not dpi_x.value:
        return 1.0
    return window_dpi / float(dpi_x.value)


def quantize_point(x: float, y: float) -> "tuple[int, int]":
    """The integer pixel a float coordinate refers to. **The single source of truth.**

    Confinement and delivery MUST agree on which pixel they mean, and they did not:
    ``WindowFromPoint`` took ``int(x)`` (truncation toward zero) while normalization
    scaled the raw float. For a negative coordinate — a monitor left of or above the
    primary — those disagree: ``int(-100.5)`` is -100 while the float scales toward
    -101. So the check authorized one pixel and the event landed on another, which at a
    window edge is a different application.

    ``floor``, not ``int``: truncation rounds toward zero, so it is asymmetric about the
    origin and only correct for positive coordinates. Flooring means "the pixel
    containing this point" on both sides.
    """
    return math.floor(x), math.floor(y)


def root_window_at_point(x: float, y: float) -> int:
    """The top-level window under the screen point, or 0.

    The confinement primitive. Runs inside :func:`dpi_awareness_scope` so the
    point is interpreted in the same space the element rects were measured in;
    without that, this resolves a different window than the one the model was
    shown.
    """
    libs = _libraries()
    with dpi_awareness_scope():
        px, py = quantize_point(x, y)
        hwnd = libs["user32"].WindowFromPoint(POINT(px, py))
        if not hwnd:
            return 0
        return int(libs["user32"].GetAncestor(ctypes.c_void_p(hwnd), GA_ROOT) or hwnd)


def window_list() -> list[WindowInfo]:
    """Every ON-SCREEN, titled top-level window, front to back.

    The pid comes from the WINDOW rather than from a process-name search, for the
    same reason the macOS driver reads it from the window list: a name search
    returns short-lived helper processes whose accessibility trees are empty.

    **``IsWindowVisible`` is not "on screen"** — it means "not explicitly hidden", so
    a minimized window (parked at -32000,-32000) and a DWM-CLOAKED one (a packaged app
    suspended in the background, or a window on another virtual desktop) both pass it.
    Measured on one desktop: 4 of 12 windows that passed the old filter were not
    visible to the operator. That is not cosmetic — ``render`` and the tool
    descriptions both say "on-screen windows", ``build_snapshot`` succeeds on a cloaked
    window and publishes an origin that OVERLAPS other applications' real pixels, and
    the capture returns a full bitmap of a window the operator cannot see. Confinement
    then fails closed correctly, but its refusal tells the model to re-read bounds that
    never change, so the retry cannot terminate.

    ``IsIconic`` plus ``DWMWA_CLOAKED`` is the Windows equivalent of the macOS list's
    ``kCGWindowListOptionOnScreenOnly``, which excludes other Spaces the same way.
    """
    libs = _libraries()
    found: list[WindowInfo] = []
    # CPython cannot raise out of a ctypes callback: it prints "Exception ignored on
    # calling ctypes callback function" to stderr and returns 0 to the caller, so an
    # unguarded failure inside ``_collect`` would drop that window AND every window
    # after it while ``EnumWindows`` still reported success. Measured: raising in
    # every second callback yielded 135 of 270 windows with no error reaching Python
    # — a short list indistinguishable from a genuinely small desktop. The failure
    # is recorded here instead and re-raised by the caller, which is what
    # ``apps_windows.list_apps`` relies on: it must never report "no applications on
    # screen" for a read it could not perform.
    failures: list[BaseException] = []

    # WINFUNCTYPE, not CFUNCTYPE: EnumWindows' callback is stdcall. The callback
    # object is held in a local for the duration of the call so it cannot be
    # collected while native code still holds the pointer.
    @ctypes.WINFUNCTYPE(wintypes.BOOL, ctypes.c_void_p, wintypes.LPARAM)  # type: ignore[attr-defined]
    def _collect(hwnd: Any, _param: Any) -> bool:
        try:
            handle = int(hwnd or 0)
            if not handle or not libs["user32"].IsWindowVisible(ctypes.c_void_p(handle)):
                return True
            title = window_text(handle)
            # An untitled top-level window is shell furniture, not an application
            # window: the desktop's own thumbnail helpers, the tray, and the XAML
            # islands all pass IsWindowVisible. Dropping them here mirrors how
            # ``apps_macos`` keeps only ``kCGWindowLayer == 0`` entries, and it means
            # the caller never has to know which class names are noise.
            if not title.strip():
                return True
            if not window_is_on_screen(handle):
                return True
            root = window_root(handle)
            pid = window_pid(handle)
            found.append(
                WindowInfo(
                    hwnd=handle,
                    root_hwnd=root,
                    pid=pid,
                    title=title,
                    class_name=window_class(handle),
                    exe_name=_exe_name(pid),
                    bounds=window_bounds(handle),
                )
            )
        except BaseException as exc:  # noqa: BLE001 - see the comment above
            failures.append(exc)
        # Enumeration CONTINUES after a failure: one unreadable window (an elevated
        # app under UIPI, a window dying mid-enumeration) should not decide the fate
        # of the rest. The recorded failure is what makes the partial list visible.
        return True

    with dpi_awareness_scope():
        libs["user32"].EnumWindows(_collect, 0)
    if failures:
        raise ComputerUseUnsupported(
            f"enumerating windows failed on {len(failures)} of {len(found) + len(failures)} "
            f"window(s): {failures[0]}"
        ) from failures[0]
    return found
