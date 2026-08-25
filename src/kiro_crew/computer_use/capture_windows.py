"""Windows window capture: ``PrintWindow`` into a DIB, encoded as JPEG by GDI+.

The counterpart of :mod:`capture_macos`, and it makes the same trade: the
accessibility tree is the primary channel and the image is corroboration, so a
capture failure degrades the result rather than failing the observation.

**No new dependency.** GDI+ is used rather than WIC because ``gdiplus.dll``
exposes a FLAT C API — no COM vtable dispatch, so none of the slot-index hazards
:mod:`windows_ffi` exists to contain apply to the encoder. Pillow is declared in
neither ``setup.cfg`` nor ``pyproject.toml`` and must not be imported here, and a
subprocess encoder would need a ``test_spawn_audit.BENIGN_SPAWNS`` entry.

Three Windows-specific findings shape the capture:

* **``PW_RENDERFULLCONTENT`` at 32bpp is the only combination that works**, and the
  two halves are inseparable. The flag renders through DWM, so it needs a 32bpp
  BGRA target; against a 24bpp DIB it returns 1 and writes nothing. Measured
  across depth x flag on two windows: a WinForms window yielded 1 distinct byte
  value at 24bpp and 180 at 32bpp, and a Chromium window yielded 1 distinct value
  for every other combination and a full frame only at 32bpp with this flag. A
  24bpp buffer therefore disables the flag silently everywhere, which is not
  visible in a code reading — it looks like a pixel-format preference.
* **There is no ``BitBlt`` fallback, by design.** A window DC is a view onto the
  SCREEN, so ``BitBlt`` copies any overlapping window's pixels into a frame
  labelled as the target's. See :func:`_capture_window_bitmap` for why that is a
  disclosure rather than a degradation.
* **A capture reports SUCCESS while producing a blank frame**, so the return value
  cannot be trusted at all: a swapchain surface is simply not in the DC being
  copied. The PIXELS are validated (:func:`_has_content`) and a uniform frame is a
  failed capture, never relayed — handing the model a blank rectangle is worse than
  handing it no image, because it would reason about the blank as though it were
  the application.

The spool PATH, the ring trim and the owner-only protection are all shared with
:mod:`capture_macos` (``shots_dir`` resolves through ``tempfile.gettempdir()``,
already ``%TEMP%`` here). Both layers of that protection are needed on Windows and
neither substitutes for the other: the directory grant is inheritable
(``restrict_dir_to_owner`` emits ``(OI)(CI)``) so files created inside inherit the
owner-only DACL, but inheritance governs only what is CREATED from here on -- a
frame that already exists keeps its own DACL, and Windows grants Bypass Traverse
Checking to Everyone by default, so a permissive pre-existing file stays reachable
through a tightened parent. A file-only grant, conversely, leaves the directory
itself listable. The directory is tightened once per process and each frame is
tightened as it is written.
"""

from __future__ import annotations

import ctypes
import logging
import os
import tempfile
import threading
import time
from dataclasses import replace
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.computer_use import screencast, windows_ffi
from kiro_crew.computer_use.capture_macos import shots_dir, trim_shots_dir
from kiro_crew.computer_use.types import (
    DEFAULT_SCREENSHOT_JPEG_QUALITY,
    DEFAULT_SCREENSHOT_MAX_PX,
    MAX_SCREENSHOT_MAX_PX,
    MIN_SCREENSHOT_MAX_PX,
    SCREENSHOT_FILE_PREFIX,
    SCREENSHOT_FILE_SUFFIX,
    Snapshot,
)

logger = logging.getLogger(__name__)

#: ``PrintWindow`` flag that renders a hardware-composited window. Without it a
#: Chromium or DirectComposition surface comes back blank.
PW_RENDERFULLCONTENT = 0x00000002

# GDI / GDI+ constants.
_BI_RGB = 0
#: 32bpp, and this is NOT a size/quality preference — it is what makes
#: ``PW_RENDERFULLCONTENT`` render at all. That flag draws through DWM, whose
#: surfaces are 32bpp BGRA, and against a 24bpp DIB it returns 1 while writing
#: NOTHING. Measured on two windows: a WinForms one produced 1 distinct byte value
#: at 24bpp and 180 at 32bpp, and a Chromium one produced 1 distinct value for
#: every other flag/depth combination and a full frame only at 32bpp + this flag.
#: The alpha channel is still discarded by the JPEG encoder; the depth is here for
#: the renderer, not for the output.
_BITSPIXEL = 32
_DIB_RGB_COLORS = 0
_GDIP_OK = 0
#: GDI+ quality is an encoder parameter, passed as a LONG through an
#: ``EncoderParameters`` block.
_ENCODER_QUALITY_GUID = "{1D5BE4B5-FA4A-452D-9CDD-5DB35105E7EB}"
_ENCODER_PARAMETER_VALUE_TYPE_LONG = 4
#: ``InterpolationModeHighQualityBicubic``: the downscale a screenshot needs. A
#: nearest-neighbour scale makes small text unreadable, which defeats the point of
#: attaching the image at all.
_INTERPOLATION_HIGH_QUALITY_BICUBIC = 7
_SMOOTHING_NONE = 3
_PIXEL_OFFSET_HALF = 4

#: A frame whose pixels are all identical is a failed capture, not a screenshot.
#: ``PrintWindow`` reports success while producing one, and relaying it would hand
#: the model a blank rectangle it would then reason about as if it were the window.
_MIN_DISTINCT_PIXELS = 2

#: GDI+ ``PixelFormat32bppARGB``. Named rather than left as a magic literal: it is
#: the format the scaled bitmap is allocated in before the downscale draw.
_PIXELFORMAT_32BPP_ARGB = 0x00021808

_dir_lock = threading.Lock()
_dir_ready = False
#: Guards the one-time gdi32/gdiplus load. Driver calls run on a pooled executor, so
#: this init is genuinely concurrent — see :func:`_gdi_libraries`.
_gdi_lock = threading.Lock()


class BITMAPINFOHEADER(ctypes.Structure):
    """Module scope: a Structure in a function body leaks through POINTER's memo."""

    _fields_ = [
        ("biSize", ctypes.c_uint32),
        ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32),
        ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16),
        ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32),
        ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", ctypes.c_uint32 * 3)]


class GdiplusStartupInput(ctypes.Structure):
    _fields_ = [
        ("GdiplusVersion", ctypes.c_uint32),
        ("DebugEventCallback", ctypes.c_void_p),
        ("SuppressBackgroundThread", ctypes.c_int32),
        ("SuppressExternalCodecs", ctypes.c_int32),
    ]


class EncoderParameter(ctypes.Structure):
    _fields_ = [
        ("Guid", windows_ffi.GUID),
        ("NumberOfValues", ctypes.c_uint32),
        ("Type", ctypes.c_uint32),
        ("Value", ctypes.c_void_p),
    ]


class EncoderParameters(ctypes.Structure):
    _fields_ = [("Count", ctypes.c_uint32), ("Parameter", EncoderParameter * 1)]


class ImageCodecInfo(ctypes.Structure):
    """GDI+ codec descriptor. Only ``Clsid`` and ``MimeType`` are read."""

    _fields_ = [
        ("Clsid", windows_ffi.GUID),
        ("FormatID", windows_ffi.GUID),
        ("CodecName", ctypes.c_wchar_p),
        ("DllName", ctypes.c_wchar_p),
        ("FormatDescription", ctypes.c_wchar_p),
        ("FilenameExtension", ctypes.c_wchar_p),
        ("MimeType", ctypes.c_wchar_p),
        ("Flags", ctypes.c_uint32),
        ("Version", ctypes.c_uint32),
        ("SigCount", ctypes.c_uint32),
        ("SigSize", ctypes.c_uint32),
        ("SigPattern", ctypes.c_void_p),
        ("SigMask", ctypes.c_void_p),
    ]


# gdi32 / gdiplus signatures. Every HDC, HBITMAP and GpImage crosses as
# c_void_p, never a bare Python int: a handle above 2^31 marshalled as the ctypes
# default C int truncates and the call lands on GDI with a corrupt handle, which
# per windows_ffi's hazard 3 is an access violation with no traceback. This table
# is the gdi counterpart of windows_ffi._FN_SPECS, and _bind_gdi raises on a
# missing argtypes for the same reason.
_HDC = ctypes.c_void_p
_HBITMAP = ctypes.c_void_p
_GP = ctypes.c_void_p  # a GDI+ object pointer (GpImage / GpBitmap / GpGraphics)
_GDI_FN_SPECS: tuple[tuple[str, str, Any, list[Any]], ...] = (
    ("gdi32", "CreateCompatibleDC", _HDC, [_HDC]),
    ("gdi32", "DeleteDC", ctypes.c_int, [_HDC]),
    (
        "gdi32",
        "CreateDIBSection",
        _HBITMAP,
        [
            _HDC,
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_void_p,
            ctypes.c_uint,
        ],
    ),
    ("gdi32", "SelectObject", ctypes.c_void_p, [_HDC, ctypes.c_void_p]),
    ("gdi32", "DeleteObject", ctypes.c_int, [ctypes.c_void_p]),
    (
        "gdi32",
        "BitBlt",
        ctypes.c_int,
        [
            _HDC,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            _HDC,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint,
        ],
    ),
    (
        "gdiplus",
        "GdiplusStartup",
        ctypes.c_int,
        [ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.c_void_p],
    ),
    ("gdiplus", "GdiplusShutdown", None, [ctypes.c_void_p]),
    (
        "gdiplus",
        "GdipCreateBitmapFromHBITMAP",
        ctypes.c_int,
        [_HBITMAP, ctypes.c_void_p, ctypes.POINTER(_GP)],
    ),
    ("gdiplus", "GdipDisposeImage", ctypes.c_int, [_GP]),
    (
        "gdiplus",
        "GdipGetImageEncodersSize",
        ctypes.c_int,
        [ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32)],
    ),
    (
        "gdiplus",
        "GdipGetImageEncoders",
        ctypes.c_int,
        [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p],
    ),
    (
        "gdiplus",
        "GdipCreateBitmapFromScan0",
        ctypes.c_int,
        [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.POINTER(_GP),
        ],
    ),
    ("gdiplus", "GdipGetImageGraphicsContext", ctypes.c_int, [_GP, ctypes.POINTER(_GP)]),
    ("gdiplus", "GdipSetInterpolationMode", ctypes.c_int, [_GP, ctypes.c_int]),
    ("gdiplus", "GdipSetPixelOffsetMode", ctypes.c_int, [_GP, ctypes.c_int]),
    (
        "gdiplus",
        "GdipDrawImageRectI",
        ctypes.c_int,
        [_GP, _GP, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int],
    ),
    ("gdiplus", "GdipDeleteGraphics", ctypes.c_int, [_GP]),
    (
        "gdiplus",
        "GdipSaveImageToStream",
        ctypes.c_int,
        [_GP, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p],
    ),
)

_gdiplus: "Any | None" = None
_gdiplus_token: "ctypes.c_void_p | None" = None
_gdi32: "Any | None" = None


def _bind_gdi(lib: Any, symbol: str, restype: Any, argtypes: "list[Any] | None") -> None:
    """Bind one flat GDI function, refusing an unspecified signature.

    Raises on ``argtypes=None`` for the same reason ``windows_ffi._bind`` does: the
    ctypes default marshals a Python int as a 32-bit C int, which truncates every
    HDC and HBITMAP. The check is what makes the table above load-bearing rather
    than decorative — a row that forgot its argtypes would otherwise ship green.
    """
    if argtypes is None:
        raise OSError(f"{symbol} declared without argtypes")
    fn = getattr(lib, symbol)
    fn.argtypes = list(argtypes)
    fn.restype = restype


def _gdi_libraries() -> "tuple[Any, Any, Any]":
    """``(user32, gdi32, gdiplus)``, each with argtypes bound, GDI+ started once.

    The load lives in a function for the same reason as in :mod:`windows_ffi`: a
    module-scope ``WinDLL`` would break importing this package on the Linux CI
    fleet. Every function used from gdi32/gdiplus is bound here from
    :data:`_GDI_FN_SPECS` so no call site marshals a handle as a bare int.

    Guarded by a lock, because driver calls run on a POOLED EXECUTOR and this is
    one-time process state. Two threads arriving together is not a benign
    double-init: ``WinDLL("gdi32")`` returns a DISTINCT object per call, so each
    thread binds its own, and the loser's ``_gdi32`` can be published while the
    winner is still mid-bind — handing a caller a function whose ``argtypes`` are
    unset, which is the pointer-truncation hazard the table exists to prevent. It
    also leaked a GDI+ token per thread while ``_gdiplus_token`` kept only the last.
    """
    global _gdiplus, _gdiplus_token, _gdi32
    libs = windows_ffi.libraries()
    with _gdi_lock:
        if _gdiplus is None or _gdi32 is None:
            gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)  # type: ignore[attr-defined]
            gdiplus = ctypes.WinDLL("gdiplus", use_last_error=True)  # type: ignore[attr-defined]
            loaded = {"gdi32": gdi32, "gdiplus": gdiplus}
            for lib_key, symbol, restype, argtypes in _GDI_FN_SPECS:
                _bind_gdi(loaded[lib_key], symbol, restype, argtypes)
            startup = GdiplusStartupInput(1, None, 0, 0)
            token = ctypes.c_void_p()
            status = gdiplus.GdiplusStartup(ctypes.byref(token), ctypes.byref(startup), None)
            if status != _GDIP_OK:
                raise OSError(f"GdiplusStartup failed ({status})")
            # Published only after every bind and the startup have SUCCEEDED, so no
            # other thread can observe a half-bound library.
            _gdi32, _gdiplus, _gdiplus_token = gdi32, gdiplus, token
    return libs["user32"], _gdi32, _gdiplus


def _jpeg_encoder_clsid(gdiplus: Any) -> "windows_ffi.GUID | None":
    """The JPEG encoder's CLSID, looked up rather than hardcoded.

    A hardcoded GUID would be one more magic constant that fails silently on a
    host whose codec set differs; asking GDI+ makes an absent encoder a clean
    ``None``.
    """
    count = ctypes.c_uint32()
    size = ctypes.c_uint32()
    if gdiplus.GdipGetImageEncodersSize(ctypes.byref(count), ctypes.byref(size)) != _GDIP_OK:
        return None
    if not count.value or not size.value:
        return None
    buffer = (ctypes.c_byte * size.value)()
    if gdiplus.GdipGetImageEncoders(count.value, size.value, buffer) != _GDIP_OK:
        return None
    codecs = ctypes.cast(buffer, ctypes.POINTER(ImageCodecInfo))
    for i in range(count.value):
        if (codecs[i].MimeType or "").lower() == "image/jpeg":
            return codecs[i].Clsid
    return None


def ensure_shots_dir() -> str:
    """Create the screenshot directory owner-only, tightening its ACL ONCE.

    ``platform_compat.make_owner_only_dir`` rather than a bare ``makedirs``: the
    mode argument is inert on Windows, where access comes from the DACL, and it is
    masked by the umask on POSIX — so the declared ``0o700`` alone protects
    nothing. That helper applies the mode AND the DACL, and it is the primitive
    every other owner-only directory in the tree uses.

    Tightening runs at most once per process (guarded by ``_dir_ready``) because
    ``restrict_to_owner`` shells out to ``icacls`` on Windows, and a subprocess per
    screenshot would block the pooled worker that also serves chat. A failure is
    logged and tolerated, the posture ``capture_macos`` takes: the files still land
    under a per-user ``%TEMP%``.
    """
    global _dir_ready
    path = shots_dir()
    with _dir_lock:
        if _dir_ready:
            os.makedirs(path, exist_ok=True)
            return path
        # Creates with the mode and applies the DACL; best-effort on the tightening
        # half, which it logs itself.
        platform_compat.make_owner_only_dir(path)
        _dir_ready = True
    return path


def persist_jpeg(raw: bytes) -> str:
    """Write *raw* into the screenshot dir and return its path, or ``""``.

    The Windows counterpart of ``capture_macos.persist_jpeg``, and it keeps that
    function's per-file ``restrict_to_owner``. The directory ACL is NOT sufficient
    on its own even now that it is inheritable: ``make_owner_only_dir`` routes
    through ``restrict_dir_to_owner``, whose ``(OI)(CI)`` grants cover files
    CREATED from that point on, but Windows grants Bypass Traverse Checking to
    Everyone by default, so a frame that already exists keeps its own DACL and
    stays reachable through the tightened parent. A frame can contain anything
    that was on screen, so the file itself is locked down.

    The spawn is on the capture path, not the observation path: it runs only when a
    screenshot was actually produced, which is already the expensive branch, and
    only ``want_image`` requests reach here.

    The atomic ``mkstemp`` naming and the ring trim are kept verbatim — ``mkstemp``
    prevents two captures in the same millisecond from colliding on one path (a
    cross-capture pixel leak), and the timestamp prefix keeps the trim's lexical
    ordering chronological.
    """
    if not raw:
        return ""
    try:
        directory = ensure_shots_dir()
        prefix = f"{SCREENSHOT_FILE_PREFIX}{int(time.time() * 1000)}-"
        handle_fd, path = tempfile.mkstemp(
            prefix=prefix, suffix=SCREENSHOT_FILE_SUFFIX, dir=directory
        )
    except OSError:
        # Allocation itself failed — ``mkstemp`` raised, so no file exists and
        # there is nothing to remove.
        logger.warning("could not persist computer-use screenshot", exc_info=True)
        return ""
    try:
        handle = os.fdopen(handle_fd, "wb")
    except OSError:
        logger.warning(
            "could not write computer-use screenshot; removing the partial frame",
            exc_info=True,
        )
        # ``fdopen`` raised before adopting the descriptor (fd pressure), so
        # nothing will ever close it — close it here, or the unlink below fails
        # on Windows, which refuses to remove a file with an open handle. This
        # is the ONLY branch that may close the raw fd: once ``fdopen`` returns,
        # the file object owns it, and a second ``os.close`` on the number could
        # hit an unrelated descriptor another executor thread was just handed.
        try:
            os.close(handle_fd)
        except OSError:
            pass
        try:
            os.unlink(path)
        except OSError:
            pass
        return ""
    try:
        with handle:
            handle.write(raw)
    except OSError:
        logger.warning(
            "could not write computer-use screenshot; removing the partial frame",
            exc_info=True,
        )
        # The ``mkstemp`` above succeeded, so this invocation owns *path* and no
        # caller will ever receive it — without the unlink the partially-written
        # frame (which can hold sensitive screen pixels) sits in the spool as an
        # orphan until the ring trim happens to reach it. Best-effort: cleanup
        # must never mask the original failure. The ``with`` has already closed
        # the descriptor; do NOT close the fd number again (see above).
        try:
            os.unlink(path)
        except OSError:
            pass
        return ""
    try:
        platform_compat.restrict_to_owner(path)
    except OSError:
        # Same warn-and-continue as ``capture_macos``: the frame is already
        # written, and deleting it would lose the observation over a defence-in-depth
        # step that the per-user %TEMP% already partly covers.
        logger.warning("could not restrict computer-use screenshot %s to owner-only", path)
    trim_shots_dir()
    return path


def _reset_dir_guard_for_test() -> None:
    """Forget the one-time directory-ACL guard. For tests only."""
    global _dir_ready
    with _dir_lock:
        _dir_ready = False


def _has_content(pixels: bytes, *, width: int = 0, bits_per_pixel: int = _BITSPIXEL) -> bool:
    """Whether a captured buffer holds more than one distinct PIXEL.

    ``PrintWindow`` returns TRUE while producing a uniform bitmap for a swapchain
    surface, so success cannot be read from the return value.

    **Whole pixels, not individual bytes.** A byte-wise distinct count is defeated by
    a uniform OPAQUE frame: 32bpp BGRA grey is ``20 20 20 FF``, two distinct byte
    values from one repeated pixel, so a blank window passes a two-value threshold on
    its alpha lane alone. Comparing pixels also makes the channel question disappear —
    every channel is part of the compared value by construction, rather than depending
    on a probe step landing on the one that differs.

    The alpha lane is EXCLUDED from the comparison. GDI leaves it undefined on the
    ``PrintWindow`` path (a fresh DIB section is zeroed, and what DWM writes there is
    not specified), so including it makes the verdict depend on a byte no renderer
    promises — in either direction: uniform alpha over varied colour is real content,
    and varying alpha over uniform colour is not.

    The sample is spread ACROSS the whole buffer, not taken from its head. The DIB
    is top-down, so the first bytes are the top scanlines — a fixed 64 KiB prefix
    on a large window is only the title bar (~11 rows of 1158), which produced two
    real failures: a window with a solid caption bar over varied content read as
    blank, and WindowsTerminal's present caption over an absent swapchain body read
    as content and relayed a blank frame. Striding over the whole buffer inspects
    the body those both hinge on.

    Scanline padding is not a pixel either, and *width* is what excludes it. Rows are
    DWORD-aligned; a 32bpp row is inherently aligned so there is nothing to skip
    today, but the exclusion stays because it is what makes the function correct for
    any ``bits_per_pixel`` rather than only the current one. It mattered concretely at
    24bpp, where pad bytes on a blank frame supplied the second distinct value all by
    themselves — measured on 694 of the 1245 padded widths in 300..1959, each relaying
    a blank frame as content.

    Falls back to a byte-wise comparison when *width* is unknown, which keeps the
    function total: without a width there is no way to locate a pixel boundary. Every
    in-tree caller passes one.
    """
    if not pixels:
        return False
    bytes_per_pixel = max(1, bits_per_pixel // 8)
    if width <= 0:
        # No pixel grid to walk: fall back to distinct bytes rather than raising.
        return len({pixels[i] for i in range(0, len(pixels), max(1, len(pixels) // 4096))}) >= (
            _MIN_DISTINCT_PIXELS
        )
    stride = ((width * bits_per_pixel + 31) // 32) * 4
    # Compare the colour channels only; see the alpha note above. At 24bpp there is
    # no alpha lane, so all three are compared.
    colour_bytes = 3 if bytes_per_pixel >= 4 else bytes_per_pixel
    height = len(pixels) // stride if stride else 0
    total_pixels = width * height
    if total_pixels <= 0:
        return False
    # ~4096 evenly-spaced probes, enough to catch a body that differs from a uniform
    # caption without scanning megabytes each capture. Counted in PIXELS, so the step
    # can no longer alias onto a byte lane.
    step = max(1, total_pixels // 4096)
    seen = set()
    for n in range(0, total_pixels, step):
        row, col = divmod(n, width)
        offset = row * stride + col * bytes_per_pixel
        seen.add(pixels[offset : offset + colour_bytes])
        if len(seen) >= _MIN_DISTINCT_PIXELS:
            return True
    return False


def _capture_window_bitmap(hwnd: int) -> "tuple[Any, int, int] | None":
    """Capture *hwnd* into a GDI+ bitmap. Returns ``(bitmap, width, height)``.

    ``PrintWindow`` with ``PW_RENDERFULLCONTENT`` is the ONLY path, and the PIXELS
    decide rather than the return value.

    There is deliberately no ``BitBlt`` fallback. A window DC is a view onto the
    SCREEN, not onto the window's own backing store, so ``BitBlt`` from it copies
    whatever is on those pixels — including an overlapping window belonging to an
    application the caller was never authorized for. That breaks the same
    confinement ``apps_windows.hwnd_owns_point`` enforces for the pointer, and it
    breaks it on the OBSERVATION path, where the secure-field floor cannot help:
    that floor walks the TARGET's tree, so a password box belonging to the window
    on top is not something it can see, let alone suppress. A blank frame is a
    failed capture and degrades to a tree-only snapshot; a frame containing another
    application is a disclosure, so the two are not interchangeable fallbacks.

    The buffer is sized from the window's rect **as the TARGET reports it**, which
    is not the same number the caller's DPI-aware context reads for a DPI-UNAWARE
    window. ``PrintWindow`` asks the window to draw itself in its own coordinate
    space, so a legacy Win32 window renders at its logical size no matter what the
    caller's awareness is: measured 620x392 drawn into both a 620x400 buffer and a
    775x500 one, leaving the aware-sized buffer with a black L-shaped margin and an
    image that no longer maps linearly onto the window rect the element frames use.
    A DPI-aware window fills whichever buffer it is given (1296 unaware, 1620
    aware). ``windows_ffi.window_render_scale`` supplies the ratio, so the buffer is
    exactly the region the window draws into and carries no margin.
    """
    user32, gdi32, gdiplus = _gdi_libraries()
    bounds = windows_ffi.window_bounds(hwnd)
    if bounds is None:
        return None
    scale = windows_ffi.window_render_scale(hwnd)
    width, height = int(bounds[2] * scale), int(bounds[3] * scale)
    if width <= 0 or height <= 0:
        return None

    window_dc = user32.GetWindowDC(ctypes.c_void_p(hwnd))
    if not window_dc:
        return None
    mem_dc = None
    bitmap = None
    try:
        mem_dc = gdi32.CreateCompatibleDC(window_dc)
        if not mem_dc:
            return None
        info = BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.bmiHeader.biWidth = width
        # NEGATIVE height requests a top-down DIB, matching the top-left
        # coordinate convention this package uses everywhere else. A bottom-up
        # DIB would deliver a vertically mirrored screenshot.
        info.bmiHeader.biHeight = -height
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = _BITSPIXEL
        info.bmiHeader.biCompression = _BI_RGB
        bits = ctypes.c_void_p()
        bitmap = gdi32.CreateDIBSection(
            window_dc, ctypes.byref(info), _DIB_RGB_COLORS, ctypes.byref(bits), None, 0
        )
        if not bitmap or not bits:
            return None
        previous = gdi32.SelectObject(mem_dc, bitmap)
        stride = ((width * _BITSPIXEL + 31) // 32) * 4
        buffer_size = stride * height

        user32.PrintWindow(ctypes.c_void_p(hwnd), mem_dc, PW_RENDERFULLCONTENT)
        pixels = ctypes.string_at(bits, buffer_size)
        # ``width`` is required: without it the row padding counts as a second
        # distinct byte value and every blank frame passes.
        if not _has_content(pixels, width=width):
            logger.debug("computer-use capture of hwnd %#x produced a blank frame", hwnd)
            gdi32.SelectObject(mem_dc, previous)
            return None
        gdi_bitmap = ctypes.c_void_p()
        status = gdiplus.GdipCreateBitmapFromHBITMAP(
            ctypes.c_void_p(bitmap), None, ctypes.byref(gdi_bitmap)
        )
        # Deselected on BOTH outcomes: leaving the DIB selected into the memory DC
        # while ``finally`` deletes it relies on GDI tolerating a
        # delete-while-selected, which is documented as failing rather than
        # something to depend on.
        gdi32.SelectObject(mem_dc, previous)
        if status != _GDIP_OK or not gdi_bitmap:
            return None
        return gdi_bitmap, width, height
    finally:
        if bitmap:
            gdi32.DeleteObject(bitmap)
        if mem_dc:
            gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(ctypes.c_void_p(hwnd), window_dc)


def _encode_jpeg(
    bitmap: Any, width: int, height: int, *, max_px: int, quality: int
) -> "tuple[bytes, int, int]":
    """Downscale to *max_px* on the long edge and encode as JPEG.

    Returns ``(bytes, encoded_width, encoded_height)`` — the dimensions are
    reported back rather than recomputed by the caller, because the encode does
    not always scale: if allocating the scaled bitmap or its graphics context
    fails, it falls back to encoding the ORIGINAL, and a caller that assumed the
    requested size would then mislabel a full-size image as downscaled. Empty
    bytes on any failure.

    Compression defaults are computer use's own (1280px / q55, not browse's
    1920/q70) because the tree is the primary channel and the image is
    corroboration.

    *max_px* and *quality* are CLAMPED here rather than trusted, exactly as
    ``capture_macos._encode_window`` clamps them: the MCP schemas validate agent
    input, but this path is also reachable from config, where a zero or negative
    ``max_px`` yields a degenerate image and an unbounded one yields a frame far
    larger than any consumer expects.
    """
    _user32, _gdi32, gdiplus = _gdi_libraries()
    clsid = _jpeg_encoder_clsid(gdiplus)
    if clsid is None:
        return b"", 0, 0
    max_px = max(MIN_SCREENSHOT_MAX_PX, min(int(max_px), MAX_SCREENSHOT_MAX_PX))
    quality = max(1, min(int(quality), 100))

    # Start from the original; the scale block below reassigns these together, so
    # `source` is always bound and `out_w`/`out_h` always describe what `source`
    # actually is — the two cannot drift.
    source: Any = bitmap
    out_w, out_h = width, height
    scaled = None
    graphics = None
    stream = None
    try:
        longest = max(width, height)
        if max_px > 0 and longest > max_px:
            ratio = max_px / float(longest)
            target_w, target_h = max(1, int(width * ratio)), max(1, int(height * ratio))
            scaled = ctypes.c_void_p()
            if (
                gdiplus.GdipCreateBitmapFromScan0(
                    target_w, target_h, 0, _PIXELFORMAT_32BPP_ARGB, None, ctypes.byref(scaled)
                )
                != _GDIP_OK
            ):
                scaled = None
            else:
                graphics = ctypes.c_void_p()
                if gdiplus.GdipGetImageGraphicsContext(scaled, ctypes.byref(graphics)) == _GDIP_OK:
                    gdiplus.GdipSetInterpolationMode(graphics, _INTERPOLATION_HIGH_QUALITY_BICUBIC)
                    gdiplus.GdipSetPixelOffsetMode(graphics, _PIXEL_OFFSET_HALF)
                    drawn = gdiplus.GdipDrawImageRectI(graphics, bitmap, 0, 0, target_w, target_h)
                    gdiplus.GdipDeleteGraphics(graphics)
                    graphics = None
                    # The DRAW's status decides, not just the context's. A freshly
                    # allocated ``Scan0`` bitmap is fully transparent, so committing to
                    # it after a failed draw encodes a BLANK image and reports it as a
                    # successful screenshot — the same "worse than no image" failure
                    # the blank-frame gate exists to prevent, arriving after that gate
                    # has already run. Falling back to the unscaled original is the
                    # right degradation: it is the real window, merely larger.
                    if drawn == _GDIP_OK:
                        source, out_w, out_h = scaled, target_w, target_h
                    else:
                        logger.debug(
                            "computer-use downscale draw failed (%s); encoding the "
                            "original at %dx%d",
                            drawn,
                            width,
                            height,
                        )

        # An IStream over global memory, created through the shell helper so no
        # COM vtable work is needed to allocate it.
        ole32 = windows_ffi.libraries()["ole32"]
        stream = ctypes.c_void_p()
        if ole32.CreateStreamOnHGlobal(None, True, ctypes.byref(stream)) != windows_ffi.S_OK:
            return b"", 0, 0

        value = ctypes.c_long(max(0, min(100, quality)))
        params = EncoderParameters()
        params.Count = 1
        params.Parameter[0].Guid = windows_ffi._guid(_ENCODER_QUALITY_GUID)
        params.Parameter[0].NumberOfValues = 1
        params.Parameter[0].Type = _ENCODER_PARAMETER_VALUE_TYPE_LONG
        # ``pointer(value)`` and not ``byref(value)``: casting a ``byref`` into a
        # ``c_void_p`` field stores a bare ADDRESS and records no ctypes ownership, so
        # the quality LONG would be kept alive only by ``value`` outliving the call —
        # true here by accident of scoping, and the kind of thing a later refactor
        # breaks silently (the field then reads back a garbage quality). A real
        # pointer object registers the dependency in ``params._objects``, tying the
        # LONG's lifetime to the structure that points at it.
        params.Parameter[0].Value = ctypes.cast(ctypes.pointer(value), ctypes.c_void_p)

        if (
            gdiplus.GdipSaveImageToStream(source, stream, ctypes.byref(clsid), ctypes.byref(params))
            != _GDIP_OK
        ):
            return b"", 0, 0
        return _read_stream(stream), out_w, out_h
    finally:
        if graphics:
            gdiplus.GdipDeleteGraphics(graphics)
        if scaled:
            gdiplus.GdipDisposeImage(scaled)
        if stream:
            windows_ffi.release(stream)


def _read_stream(stream: Any) -> bytes:
    """Drain an ``IStream`` into bytes via ``GetHGlobalFromStream``.

    ``ole32``'s helper avoids an ``IStream::Read`` vtable call, keeping every COM
    slot index in this package inside :mod:`windows_ffi`'s one table.
    """
    ole32 = windows_ffi.libraries()["ole32"]
    kernel32 = windows_ffi.libraries()["kernel32"]
    handle = ctypes.c_void_p()
    if ole32.GetHGlobalFromStream(stream, ctypes.byref(handle)) != windows_ffi.S_OK:
        return b""
    size = kernel32.GlobalSize(handle)
    if not size:
        return b""
    address = kernel32.GlobalLock(handle)
    if not address:
        return b""
    try:
        return ctypes.string_at(address, size)
    finally:
        kernel32.GlobalUnlock(handle)


def capture_snapshot_image(
    snap: Snapshot,
    *,
    max_px: int = DEFAULT_SCREENSHOT_MAX_PX,
    quality: int = DEFAULT_SCREENSHOT_JPEG_QUALITY,
) -> Snapshot:
    """Capture *snap*'s window, persist the JPEG, and return an updated snapshot.

    Returns the snapshot UNCHANGED (no image) when any of these holds, and the
    first two are the always-on floors rather than failure handling:

    * **any element is secure** — a password field's rendered glyphs are a
      credential even though the tree redacted its value, and there is no reliable
      way to blank a sub-rectangle of an already-encoded JPEG, so suppression is
      whole-window;
    * **the walk was truncated or depth-truncated** — ``has_secure`` then means
      "none seen", NOT "none present", so a password field beyond the cut would
      leave rendered credentials capturable;
    * the window handle is unknown, the capture produced no usable pixels, the
      encode failed, or persisting failed.

    Never raises. The tree is the primary channel, so a capture failure degrades
    the result rather than failing the observation.
    """
    if snap.has_secure:
        return snap
    if snap.truncated or snap.depth_truncated:
        return snap
    if not snap.app.window_id:
        return snap
    try:
        if windows_ffi.window_is_minimized(snap.app.window_id):
            # A minimized window has no pixels. It is still ``IsWindow`` and still
            # ``IsWindowVisible`` — that flag means "not explicitly hidden" — and its
            # rect is parked off-screen (measured at -32000, -32000), so the capture
            # SUCCEEDS over a uniform buffer and the blank-frame gate rejects it with
            # no reason anyone can report. Checked up front so the tree is still
            # returned (the UIA tree of a minimized window is perfectly readable) and
            # the debug log names the actual cause.
            logger.debug(
                "computer-use capture skipped: window 0x%X is minimized",
                snap.app.window_id,
            )
            return snap
    except Exception:
        # Never fatal: the tree is the primary channel, so an unreadable window
        # state degrades to attempting the capture rather than failing the read.
        logger.debug("minimized-window probe failed", exc_info=True)
    try:
        # The SAME awareness scope the walk ran in: a capture taken under a
        # different DPI context would be a crop of a differently-measured
        # rectangle than the frames the model was shown.
        with windows_ffi.dpi_awareness_scope():
            captured = _capture_window_bitmap(snap.app.window_id)
            if captured is None:
                return snap
            bitmap, width, height = captured
            try:
                raw, out_w, out_h = _encode_jpeg(
                    bitmap, width, height, max_px=max_px, quality=quality
                )
            finally:
                _gdi_libraries()[2].GdipDisposeImage(bitmap)
    except Exception:
        logger.debug("computer-use window capture failed", exc_info=True)
        return snap
    if not raw:
        return snap
    path = persist_jpeg(raw)
    if not path:
        return snap
    # out_w/out_h are the ACTUAL encoded dimensions (see _encode_jpeg): the encode
    # may have declined to scale, so recomputing from max_px here could mislabel a
    # full-size image as downscaled.
    # A DISTINCT name from ``captured`` above, which is the ``_capture_window_bitmap``
    # tuple: reusing it type-checked as "bitmap tuple" for the rest of the function and
    # made the relay call below look like it was handed a bitmap rather than a Snapshot.
    attached = replace(
        snap,
        image_jpeg=raw,
        image_path=path,
        image_width=out_w,
        image_height=out_h,
    )
    # Mirror to the dashboard live view, as ``capture_macos`` does — without this the
    # panel is permanently blank on Windows. The RELAY owns all three suppressions (no
    # published surface scope, a secure window, a withheld screenshot channel), so
    # nothing is re-derived here, and it relays these exact already-downscaled bytes
    # rather than capturing again. It does not block: the POST runs on a daemon thread,
    # so a dead gateway cannot slow the observation the model asked for.
    #
    # Wrapped anyway, even though the relay is itself contracted never to raise: THIS
    # function's contract is "never raises", and a decorative mirror must not be able
    # to turn a successful observation into a failed tool call if that inner contract
    # is ever broken.
    try:
        screencast.emit_snapshot_frame(attached)
    except Exception:
        logger.debug("computer-use live-view relay failed", exc_info=True)
    return attached


__all__ = [
    "PW_RENDERFULLCONTENT",
    "capture_snapshot_image",
    "ensure_shots_dir",
    "shots_dir",
]
