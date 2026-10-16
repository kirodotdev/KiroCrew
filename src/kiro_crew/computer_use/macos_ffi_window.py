"""CoreGraphics windows in :mod:`kiro_crew.computer_use.macos_ffi`.

The on-screen window list (the package's one app and pid resolution), a pid's executable
path through ``proc_pidpath``, and the in-process capture of one window: the CoreGraphics
image, the ImageIO JPEG encode, the JPEG size check and the default spool directory.

:mod:`kiro_crew.computer_use.macos_ffi` stays the import path and the patch surface for
every name defined here.
"""

from __future__ import annotations

import ctypes
import os
import struct
import tempfile
from ctypes import c_double, c_int32, c_uint32, c_void_p
from typing import Any

from kiro_crew.computer_use.macos_ffi_bind import (
    CG_BOUNDS_H,
    CG_BOUNDS_W,
    CG_BOUNDS_X,
    CG_BOUNDS_Y,
    CG_WINDOW_BOUNDS,
    CG_WINDOW_LAYER,
    CG_WINDOW_NAME,
    CG_WINDOW_NUMBER,
    CG_WINDOW_OWNER_NAME,
    CG_WINDOW_OWNER_PID,
    IO_KEY_LOSSY_QUALITY,
    IO_KEY_MAX_PIXEL_SIZE,
    K_CF_NUMBER_DOUBLE,
    K_CF_NUMBER_SINT32,
    K_CG_NULL_WINDOW_ID,
    K_CG_WINDOW_IMAGE_BOUNDS_IGNORE_FRAMING,
    K_CG_WINDOW_LIST_EXCLUDE_DESKTOP,
    K_CG_WINDOW_LIST_INCLUDING_WINDOW,
    K_CG_WINDOW_LIST_ON_SCREEN_ONLY,
    PROC_PIDPATH_MAX,
    UTI_JPEG,
    CGPoint,
    CGRect,
    CGSize,
    WindowInfo,
    _frameworks,
    logger,
    type_ids,
)
from kiro_crew.computer_use.macos_ffi_cf import (
    _cf_type_dict,
    cf_array_items,
    cf_is,
    cf_number_double,
    cf_number_int,
    cf_string,
    cf_string_value,
)
from kiro_crew.computer_use.types import SCREENSHOT_DIR_NAME

# ── CoreGraphics: window list ──


def window_list() -> list[WindowInfo]:
    """Enumerate on-screen windows, excluding desktop elements.

    This list is the ONLY app/pid resolution mechanism in the package. A
    process-name search returns short-lived helper processes: ``pgrep -n
    "Google Chrome"`` answered 47492 (a helper that answered ``-25204`` to every
    attribute read and then exited) while the real browser was 637, and Slack's
    helper 1614 shadowed the real 942. The pid that owns a visible, layer-0
    window is the one whose accessibility tree is populated.
    """
    libs = _frameworks()
    info = c_void_p(
        libs.cg.CGWindowListCopyWindowInfo(
            K_CG_WINDOW_LIST_ON_SCREEN_ONLY | K_CG_WINDOW_LIST_EXCLUDE_DESKTOP,
            K_CG_NULL_WINDOW_ID,
        )
    )
    if not info:
        return []
    try:
        out: list[WindowInfo] = []
        dict_type = type_ids().dictionary
        for entry in cf_array_items(info):
            if not cf_is(entry, dict_type):
                continue
            window_id = _dict_int(entry, CG_WINDOW_NUMBER)
            pid = _dict_int(entry, CG_WINDOW_OWNER_PID)
            if window_id is None or pid is None:
                continue
            out.append(
                WindowInfo(
                    window_id=window_id,
                    pid=pid,
                    owner_name=_dict_str(entry, CG_WINDOW_OWNER_NAME),
                    title=_dict_str(entry, CG_WINDOW_NAME),
                    layer=_dict_int(entry, CG_WINDOW_LAYER) or 0,
                    bounds=_dict_bounds(entry),
                )
            )
        return out
    finally:
        # The window list is a "Copy" API: we own the array and must release it,
        # even if a malformed entry raised on the way through.
        libs.cf.CFRelease(info)


def _dict_int(entry: Any, key: str) -> "int | None":
    """Read an int out of a window-list dictionary."""
    with cf_string(key) as ref:
        if not ref:
            return None
        value = _frameworks().cf.CFDictionaryGetValue(entry, ref)
    return cf_number_int(value)


def _dict_str(entry: Any, key: str) -> str:
    """Read a string out of a window-list dictionary."""
    with cf_string(key) as ref:
        if not ref:
            return ""
        value = _frameworks().cf.CFDictionaryGetValue(entry, ref)
    if not cf_is(value, type_ids().string):
        return ""
    return cf_string_value(value)


def _dict_float(entry: Any, key: str) -> "float | None":
    """Read a float out of a CFDictionary (the bounds sub-dictionary)."""
    with cf_string(key) as ref:
        if not ref:
            return None
        value = _frameworks().cf.CFDictionaryGetValue(entry, ref)
    return cf_number_double(value)


def _dict_bounds(entry: Any) -> "tuple[float, float, float, float] | None":
    """Read ``kCGWindowBounds`` as ``(x, y, w, h)`` in TOP-LEFT coordinates.

    Returns ``None`` — never a partial or zero rect — when the key is missing or
    any component is unreadable. A caller uses this to decide whether a screen
    point belongs to an authorized application, so "unknown" must be
    distinguishable from "a rect at the origin": the latter would silently claim
    the top-left corner of the display.
    """
    with cf_string(CG_WINDOW_BOUNDS) as ref:
        if not ref:
            return None
        raw = _frameworks().cf.CFDictionaryGetValue(entry, ref)
    if not cf_is(raw, type_ids().dictionary):
        return None
    x = _dict_float(raw, CG_BOUNDS_X)
    y = _dict_float(raw, CG_BOUNDS_Y)
    width = _dict_float(raw, CG_BOUNDS_W)
    height = _dict_float(raw, CG_BOUNDS_H)
    # Read into four names rather than a comprehension so the None-checks actually
    # narrow the types (a list comprehension keeps them ``float | None`` no matter
    # what the ``any(... is None)`` guard proved).
    if x is None or y is None or width is None or height is None:
        return None
    if width <= 0.0 or height <= 0.0:
        return None
    return (x, y, width, height)


def executable_path(pid: int) -> str:
    """Absolute executable path of *pid* via ``proc_pidpath``, or ``""``.

    ``proc_pidpath`` rather than reading ``/proc`` (which macOS does not have) or
    shelling out to ``ps``: it is a single syscall, needs no subprocess, and
    measured under 1ms. Returns ``""`` for a dead pid or a process we cannot
    inspect — never raises, because this feeds identity resolution, and a failure
    there must produce "identity unknown" (which the governance gate DENIES), not
    an exception.
    """
    libs = _frameworks()
    buf = ctypes.create_string_buffer(PROC_PIDPATH_MAX)
    written = int(libs.proc.proc_pidpath(int(pid), buf, PROC_PIDPATH_MAX))
    if written <= 0:
        return ""
    return buf.value.decode("utf-8", "replace")


# ── CoreGraphics + ImageIO: in-process window capture ──


def preflight_screen_capture() -> bool:
    """``CGPreflightScreenCaptureAccess()`` — ADVISORY. See :mod:`permissions`.

    The request variant is deliberately not bound: it pops a system dialog from
    the calling process, which from a background sidecar is an unattributable
    prompt.
    """
    return bool(_frameworks().cg.CGPreflightScreenCaptureAccess())


def capture_window_jpeg(window_id: int, *, max_px: int, quality: float) -> tuple[bytes, int, int]:
    """Capture one window's pixels and JPEG-encode them, entirely in-process.

    Returns ``(jpeg_bytes, width, height)``, or ``(b"", 0, 0)`` when the window
    cannot be captured (a closed or invalid window id yields a NULL image —
    verified, not a crash). ImageIO performs the downscale itself via
    ``kCGImageDestinationImageMaxPixelSize``, so there is **no subprocess and no
    image library**: no ``screencapture``, no Pillow, nothing for the spawn audit
    to account for and no optional dependency to degrade around.

    A null ``CGRect`` (all zeros) means "the window's own bounds", which is what
    keeps the capture scoped to ONE window rather than to the screen.

    Every CF/CG object is released in a ``finally`` — **including when
    ``Finalize`` returns False.** That branch is the easy one to get wrong: an
    early return on a failed encode is exactly when four objects leak, and it is
    also the branch a test exercises.
    """
    libs = _frameworks()
    image = c_void_p(
        libs.cg.CGWindowListCreateImage(
            CGRect(CGPoint(0.0, 0.0), CGSize(0.0, 0.0)),
            K_CG_WINDOW_LIST_INCLUDING_WINDOW,
            c_uint32(int(window_id)),
            K_CG_WINDOW_IMAGE_BOUNDS_IGNORE_FRAMING,
        )
    )
    if not image:
        return b"", 0, 0

    data: "c_void_p | None" = None
    dest: "c_void_p | None" = None
    options: "c_void_p | None" = None
    num_max: "c_void_p | None" = None
    num_quality: "c_void_p | None" = None
    try:
        data = c_void_p(libs.cf.CFDataCreateMutable(None, 0))
        if not data:
            return b"", 0, 0
        with cf_string(UTI_JPEG) as uti:
            if not uti:
                return b"", 0, 0
            dest = c_void_p(libs.io.CGImageDestinationCreateWithData(data, uti, 1, None))
        if not dest:
            return b"", 0, 0

        options = _cf_type_dict()
        if not options:
            return b"", 0, 0
        max_value = c_int32(int(max_px))
        num_max = c_void_p(
            libs.cf.CFNumberCreate(None, K_CF_NUMBER_SINT32, ctypes.byref(max_value))
        )
        quality_value = c_double(float(quality))
        num_quality = c_void_p(
            libs.cf.CFNumberCreate(None, K_CF_NUMBER_DOUBLE, ctypes.byref(quality_value))
        )
        max_key = c_void_p.in_dll(libs.io, IO_KEY_MAX_PIXEL_SIZE)
        quality_key = c_void_p.in_dll(libs.io, IO_KEY_LOSSY_QUALITY)
        if num_max:
            libs.cf.CFDictionarySetValue(options, max_key, num_max)
        if num_quality:
            libs.cf.CFDictionarySetValue(options, quality_key, num_quality)

        libs.io.CGImageDestinationAddImage(dest, image, options)
        if not libs.io.CGImageDestinationFinalize(dest):
            # Degrade to "no image": the accessibility tree is the primary
            # channel, so a failed encode must not fail the whole observation.
            logger.debug("ImageIO finalize failed for window %s", window_id)
            return b"", 0, 0
        length = int(libs.cf.CFDataGetLength(data))
        if length <= 0:
            return b"", 0, 0
        raw = ctypes.string_at(libs.cf.CFDataGetBytePtr(data), length)
        width, height = jpeg_dimensions(raw)
        return raw, width, height
    finally:
        for obj in (num_max, num_quality, options, dest, data):
            if obj:
                libs.cf.CFRelease(obj)
        libs.cg.CGImageRelease(image)


# JPEG marker bytes for :func:`jpeg_dimensions`.
_JPEG_SOI = 0xD8
_JPEG_EOI = 0xD9
_JPEG_MARKER = 0xFF
_JPEG_SOF_MARKERS = frozenset({0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB})
_JPEG_RST_FIRST = 0xD0
_JPEG_RST_LAST = 0xD7
_JPEG_SOF_DIMENSION_OFFSET = 5


def jpeg_dimensions(raw: bytes) -> tuple[int, int]:
    """Parse ``(width, height)` out of JPEG bytes, or ``(0, 0)``.

    The ENCODED dimensions are what we report, not the source image's:
    ``kCGImageDestinationImageMaxPixelSize`` downscales inside ImageIO, so
    ``CGImageGetWidth`` on the input would over-report by the scale factor and
    the size line shown to the model would be wrong (verified: a 1676x1320 window
    encoded to 1280x1008).

    A ~25-line stdlib scan rather than an image library: this is the only reason
    a dependency would be needed at all, and reporting ``(0, 0)`` on an
    unparseable stream is a cosmetic degradation, never a failure.
    """
    if len(raw) < 4 or raw[0] != _JPEG_MARKER or raw[1] != _JPEG_SOI:
        return 0, 0
    pos = 2
    while pos + 3 < len(raw):
        if raw[pos] != _JPEG_MARKER:
            pos += 1
            continue
        marker = raw[pos + 1]
        if marker in (_JPEG_MARKER, _JPEG_SOI, _JPEG_EOI) or (
            _JPEG_RST_FIRST <= marker <= _JPEG_RST_LAST
        ):
            pos += 2
            continue
        if marker in _JPEG_SOF_MARKERS:
            start = pos + _JPEG_SOF_DIMENSION_OFFSET
            if start + 4 > len(raw):
                return 0, 0
            height, width = struct.unpack(">HH", raw[start : start + 4])
            return int(width), int(height)
        segment = struct.unpack(">H", raw[pos + 2 : pos + 4])[0]
        if segment < 2:
            return 0, 0
        pos += 2 + segment
    return 0, 0


def shots_dir_default() -> str:
    """Default screenshot directory path (no side effects).

    ``tempfile.gettempdir()`` rather than a hardcoded ``/tmp``: it honours
    ``$TMPDIR`` on POSIX and resolves to ``%TEMP%`` on Windows, where ``/tmp``
    does not exist. Kept here beside the capture primitive so the path and the
    encoder cannot drift apart; :mod:`capture_macos` owns creating it with the
    right mode.
    """
    return os.path.join(tempfile.gettempdir(), SCREENSHOT_DIR_NAME)
