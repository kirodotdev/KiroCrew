"""The Linux native layer's foundation, an owner of :mod:`kiro_crew.computer_use.linux_ffi`.

Library sonames, the pinned AT-SPI role/state ids and X11 constants, every ctypes
structure (module scope — a Structure in a function body leaks), the flat function
table with ``argtypes``/``restype`` on every row, the once-per-process loader and
binder, the X error handler, the GError/ownership helpers, and the availability
verdict with its lifecycle. **Every run-time-rebound global of the family lives here**
(``_libs``, ``_available``, ``_unavailable_reason``, ``_display``, ``_last_x_error``,
``_x_error_handler``, ``_atspi_initialised``) together with every reader of it, which
is what lets the other owners stay pure functions over the accessors ``_atspi()``,
``_display_ptr()`` and ``_lib_*()``.

:mod:`kiro_crew.computer_use.linux_ffi` stays the import path and the patch surface for every name defined
here (see :mod:`kiro_crew._facade_mirror`); an owner never imports its facade.
"""

from __future__ import annotations

import ctypes
import logging
import os
import threading
from contextlib import contextmanager
from typing import Any, Iterator, Sequence

from kiro_crew.computer_use.types import ComputerUseError

logger = logging.getLogger(__name__)

# ── Library sonames ──
# Versioned sonames, never the bare ``.so`` dev symlink: a host has the runtime
# library (``libX11.so.6``) without the ``-dev`` package that provides ``libX11.so``,
# so loading the bare name would fail on a machine where the feature actually works.
LIB_ATSPI = "libatspi.so.0"
LIB_X11 = "libX11.so.6"
LIB_XTST = "libXtst.so.6"
LIB_GDK_PIXBUF = "libgdk_pixbuf-2.0.so.0"
LIB_GOBJECT = "libgobject-2.0.so.0"
LIB_GLIB = "libglib-2.0.so.0"

# ── AT-SPI role ids (at-spi2 2.54, pinned from the installed headers) ──
ATSPI_ROLE_CHECK_BOX = 7
ATSPI_ROLE_DIALOG = 16
ATSPI_ROLE_FILLER = 20
ATSPI_ROLE_FRAME = 23
ATSPI_ROLE_LABEL = 29
ATSPI_ROLE_MENU_ITEM = 35
ATSPI_ROLE_PANEL = 39
#: THE security-critical role. GTK/Qt/Firefox all report a masked field as this,
#: and there is NO ``ATSPI_STATE_PROTECTED`` in 2.54 — so ``secure`` is role-driven
#: and ``snapshot_linux`` fails closed on an unreadable role.
ATSPI_ROLE_PASSWORD_TEXT = 40
ATSPI_ROLE_PUSH_BUTTON = 43
ATSPI_ROLE_TEXT = 61
ATSPI_ROLE_WINDOW = 69
ATSPI_ROLE_APPLICATION = 75
ATSPI_ROLE_ENTRY = 79
ATSPI_ROLE_LINK = 88

# ── AT-SPI state ids ──
ATSPI_STATE_ACTIVE = 1
ATSPI_STATE_CHECKED = 4
ATSPI_STATE_DEFUNCT = 6
ATSPI_STATE_EDITABLE = 7
ATSPI_STATE_ENABLED = 8
ATSPI_STATE_EXPANDED = 10
ATSPI_STATE_FOCUSABLE = 11
ATSPI_STATE_FOCUSED = 12
ATSPI_STATE_ICONIFIED = 15
ATSPI_STATE_MODAL = 16
ATSPI_STATE_SELECTED = 23
ATSPI_STATE_SENSITIVE = 24
ATSPI_STATE_SHOWING = 25
ATSPI_STATE_VISIBLE = 30

#: ``AtspiCoordType.ATSPI_COORD_TYPE_SCREEN`` — absolute screen coordinates, the
#: convention every frame in this package uses.
ATSPI_COORD_SCREEN = 0
#: Window-relative extents. The coordinate type every toolkit answers CORRECTLY:
#: measured on GTK 4.16, ``SCREEN`` returns ``(0, 0, w, h)`` for every widget (the
#: toplevel included) while ``WINDOW`` returns the real window-relative rect.
ATSPI_COORD_WINDOW = 1

# ── AT-SPI key-synth types (AtspiKeySynthType) ──
KEYSYNTH_PRESSRELEASE = 2
KEYSYNTH_SYM = 3
KEYSYNTH_STRING = 4

# ── X11 button numbers ──
Button1 = 1
Button2 = 2
Button3 = 3
Button4 = 4
Button5 = 5

# ── X11 modifier masks ──
ShiftMask = 1 << 0
ControlMask = 1 << 2
Mod1Mask = 1 << 3  # Alt
Mod4Mask = 1 << 6  # Super

# ── X11 misc constants ──
_ZPixmap = 2
_AllPlanes = 0xFFFFFFFF
_IsViewable = 2  # XWindowAttributes.map_state
_CurrentTime = 0
_ButtonPress = 4
_ButtonRelease = 5
_MotionNotify = 6
_KeyPress = 2
_KeyRelease = 3
_ButtonPressMask = 1 << 2
_ButtonReleaseMask = 1 << 3
_ButtonMotionMask = 1 << 13
_PropModeReplace = 0
_AnyPropertyType = 0
_Success = 0
#: ``GdkInterpType.GDK_INTERP_BILINEAR`` — the downscale filter for the capture.
_GDK_INTERP_BILINEAR = 2


# ── X11 / GLib structures (module scope: a Structure in a function body leaks,
#    because ``ctypes.POINTER`` memoises the type in an un-evicted module dict) ──


class XErrorEvent(ctypes.Structure):
    """The event passed to an ``XErrorHandler``. Only ``error_code`` is read."""

    _fields_ = [
        ("type", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("resourceid", ctypes.c_ulong),
        ("serial", ctypes.c_ulong),
        ("error_code", ctypes.c_ubyte),
        ("request_code", ctypes.c_ubyte),
        ("minor_code", ctypes.c_ubyte),
    ]


class XWindowAttributes(ctypes.Structure):
    """A prefix of ``XWindowAttributes`` long enough for the fields read.

    Over-sized rather than exact, with ``_pad`` covering the tail: ``XGetWindowAttributes``
    writes the WHOLE struct, so a short declaration lets the callee write past the end
    of the object. ``map_state`` and the geometry are what this module reads.
    """

    _fields_ = [
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
        ("border_width", ctypes.c_int),
        ("depth", ctypes.c_int),
        ("visual", ctypes.c_void_p),
        ("root", ctypes.c_ulong),
        ("class_", ctypes.c_int),
        ("bit_gravity", ctypes.c_int),
        ("win_gravity", ctypes.c_int),
        ("backing_store", ctypes.c_int),
        ("backing_planes", ctypes.c_ulong),
        ("backing_pixel", ctypes.c_ulong),
        ("save_under", ctypes.c_int),
        ("colormap", ctypes.c_ulong),
        ("map_installed", ctypes.c_int),
        ("map_state", ctypes.c_int),
        ("all_event_masks", ctypes.c_long),
        ("your_event_mask", ctypes.c_long),
        ("do_not_propagate_mask", ctypes.c_long),
        ("override_redirect", ctypes.c_int),
        ("_pad", ctypes.c_byte * 64),
    ]


class XImage(ctypes.Structure):
    """``XImage`` through the function-pointer table, which this module does not use.

    Only the header fields are read — the pixel buffer is reached through ``data``
    with the image's own ``bytes_per_line`` / ``bits_per_pixel`` / masks, never
    through the per-image ``f.get_pixel`` function table — so the trailing ``funcs``
    block is declared as opaque padding. ``XGetImage`` allocates and owns the whole
    struct; :func:`x_get_image_rgb` copies the pixels out and ``XDestroyImage`` frees
    it.
    """

    _fields_ = [
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
        ("xoffset", ctypes.c_int),
        ("format", ctypes.c_int),
        ("data", ctypes.c_void_p),
        ("byte_order", ctypes.c_int),
        ("bitmap_unit", ctypes.c_int),
        ("bitmap_bit_order", ctypes.c_int),
        ("bitmap_pad", ctypes.c_int),
        ("depth", ctypes.c_int),
        ("bytes_per_line", ctypes.c_int),
        ("bits_per_pixel", ctypes.c_int),
        ("red_mask", ctypes.c_ulong),
        ("green_mask", ctypes.c_ulong),
        ("blue_mask", ctypes.c_ulong),
        ("obdata", ctypes.c_void_p),
        ("funcs", ctypes.c_byte * 128),
    ]


class GArray(ctypes.Structure):
    """GLib ``GArray``: a ``gchar*`` data pointer and a ``guint`` length.

    ``atspi_state_set_get_states`` returns one of these holding ``gint`` state ids.
    The caller frees it with ``g_array_free(arr, TRUE)``.
    """

    _fields_ = [("data", ctypes.c_void_p), ("len", ctypes.c_uint)]


class AtspiRect(ctypes.Structure):
    """``AtspiRect``: four ``gint`` fields. ``atspi_component_get_extents`` returns a
    HEAP pointer to one, which the caller must ``g_free`` after reading."""

    _fields_ = [
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
    ]


# ── Flat function table: (library key, symbol, restype, argtypes) ──
# Every row carries BOTH, and ``_bind`` raises on a missing ``argtypes``: a bare
# Python int handed to an un-typed function is marshalled as a 32-bit C int and
# TRUNCATES the 64-bit handle, so the callee dereferences garbage. An AT-SPI object
# handle, an XID and a Display are all pointer-width and all cross as ``c_void_p``
# (an XID is a ``c_ulong`` where it is a VALUE argument/return, ``c_void_p`` where it
# is the Display). ``restype`` matters as much: a returned pointer read back as a
# 32-bit int is a silently corrupt handle that faults later.
_GECARG = ctypes.c_void_p  # a ``GError**`` out-param, passed as ``byref(c_void_p)``

_FN_SPECS: tuple[tuple[str, str, Any, list[Any]], ...] = (
    # ── libatspi: lifecycle ──
    (LIB_ATSPI, "atspi_init", ctypes.c_int, []),
    (LIB_ATSPI, "atspi_exit", ctypes.c_int, []),
    (LIB_ATSPI, "atspi_get_desktop", ctypes.c_void_p, [ctypes.c_int]),
    # ── libatspi: tree ──
    (LIB_ATSPI, "atspi_accessible_get_child_count", ctypes.c_int, [ctypes.c_void_p, _GECARG]),
    (
        LIB_ATSPI,
        "atspi_accessible_get_child_at_index",
        ctypes.c_void_p,
        [ctypes.c_void_p, ctypes.c_int, _GECARG],
    ),
    (LIB_ATSPI, "atspi_accessible_get_parent", ctypes.c_void_p, [ctypes.c_void_p, _GECARG]),
    (LIB_ATSPI, "atspi_accessible_get_name", ctypes.c_void_p, [ctypes.c_void_p, _GECARG]),
    (LIB_ATSPI, "atspi_accessible_get_description", ctypes.c_void_p, [ctypes.c_void_p, _GECARG]),
    (LIB_ATSPI, "atspi_accessible_get_role", ctypes.c_int, [ctypes.c_void_p, _GECARG]),
    (LIB_ATSPI, "atspi_accessible_get_role_name", ctypes.c_void_p, [ctypes.c_void_p, _GECARG]),
    (LIB_ATSPI, "atspi_accessible_get_process_id", ctypes.c_int, [ctypes.c_void_p, _GECARG]),
    (LIB_ATSPI, "atspi_accessible_get_toolkit_name", ctypes.c_void_p, [ctypes.c_void_p, _GECARG]),
    # ── libatspi: states ──
    (LIB_ATSPI, "atspi_accessible_get_state_set", ctypes.c_void_p, [ctypes.c_void_p]),
    (LIB_ATSPI, "atspi_state_set_get_states", ctypes.POINTER(GArray), [ctypes.c_void_p]),
    (LIB_ATSPI, "atspi_state_set_contains", ctypes.c_int, [ctypes.c_void_p, ctypes.c_int]),
    # ── libatspi: component (geometry + focus) ──
    (LIB_ATSPI, "atspi_accessible_get_component_iface", ctypes.c_void_p, [ctypes.c_void_p]),
    (
        LIB_ATSPI,
        "atspi_component_get_extents",
        ctypes.POINTER(AtspiRect),
        [ctypes.c_void_p, ctypes.c_int, _GECARG],
    ),
    (LIB_ATSPI, "atspi_component_grab_focus", ctypes.c_int, [ctypes.c_void_p, _GECARG]),
    # ── libatspi: action ──
    (LIB_ATSPI, "atspi_accessible_get_action_iface", ctypes.c_void_p, [ctypes.c_void_p]),
    (LIB_ATSPI, "atspi_action_get_n_actions", ctypes.c_int, [ctypes.c_void_p, _GECARG]),
    (
        LIB_ATSPI,
        "atspi_action_get_action_name",
        ctypes.c_void_p,
        [ctypes.c_void_p, ctypes.c_int, _GECARG],
    ),
    (
        LIB_ATSPI,
        "atspi_action_do_action",
        ctypes.c_int,
        [ctypes.c_void_p, ctypes.c_int, _GECARG],
    ),
    # ── libatspi: text ──
    (LIB_ATSPI, "atspi_accessible_get_text_iface", ctypes.c_void_p, [ctypes.c_void_p]),
    (LIB_ATSPI, "atspi_text_get_character_count", ctypes.c_int, [ctypes.c_void_p, _GECARG]),
    (
        LIB_ATSPI,
        "atspi_text_get_text",
        ctypes.c_void_p,
        [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, _GECARG],
    ),
    # ── libatspi: value ──
    (LIB_ATSPI, "atspi_accessible_get_value_iface", ctypes.c_void_p, [ctypes.c_void_p]),
    (LIB_ATSPI, "atspi_value_get_current_value", ctypes.c_double, [ctypes.c_void_p, _GECARG]),
    # ── libatspi: editable text ──
    (LIB_ATSPI, "atspi_accessible_get_editable_text_iface", ctypes.c_void_p, [ctypes.c_void_p]),
    (
        LIB_ATSPI,
        "atspi_editable_text_set_text_contents",
        ctypes.c_int,
        [ctypes.c_void_p, ctypes.c_char_p, _GECARG],
    ),
    (
        LIB_ATSPI,
        "atspi_editable_text_insert_text",
        ctypes.c_int,
        [ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int, _GECARG],
    ),
    # ── libatspi: GLOBAL input (XTest under the hood) ──
    (
        LIB_ATSPI,
        "atspi_generate_keyboard_event",
        ctypes.c_int,
        [ctypes.c_long, ctypes.c_char_p, ctypes.c_int, _GECARG],
    ),
    (
        LIB_ATSPI,
        "atspi_generate_mouse_event",
        ctypes.c_int,
        [ctypes.c_long, ctypes.c_long, ctypes.c_char_p, _GECARG],
    ),
    # ── libX11: display lifecycle + error handler ──
    (LIB_X11, "XOpenDisplay", ctypes.c_void_p, [ctypes.c_char_p]),
    (LIB_X11, "XCloseDisplay", ctypes.c_int, [ctypes.c_void_p]),
    (LIB_X11, "XSetErrorHandler", ctypes.c_void_p, [ctypes.c_void_p]),
    (LIB_X11, "XDefaultRootWindow", ctypes.c_ulong, [ctypes.c_void_p]),
    (LIB_X11, "XFlush", ctypes.c_int, [ctypes.c_void_p]),
    (LIB_X11, "XSync", ctypes.c_int, [ctypes.c_void_p, ctypes.c_int]),
    (LIB_X11, "XFree", ctypes.c_int, [ctypes.c_void_p]),
    # ── libX11: screen size ──
    (LIB_X11, "XDisplayWidth", ctypes.c_int, [ctypes.c_void_p, ctypes.c_int]),
    (LIB_X11, "XDisplayHeight", ctypes.c_int, [ctypes.c_void_p, ctypes.c_int]),
    (LIB_X11, "XDefaultScreen", ctypes.c_int, [ctypes.c_void_p]),
    # ── libX11: window properties (EWMH) ──
    (LIB_X11, "XInternAtom", ctypes.c_ulong, [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]),
    (
        LIB_X11,
        "XGetWindowProperty",
        ctypes.c_int,
        [
            ctypes.c_void_p,  # display
            ctypes.c_ulong,  # window
            ctypes.c_ulong,  # property atom
            ctypes.c_long,  # long_offset
            ctypes.c_long,  # long_length
            ctypes.c_int,  # delete
            ctypes.c_ulong,  # req_type
            ctypes.POINTER(ctypes.c_ulong),  # actual_type_return
            ctypes.POINTER(ctypes.c_int),  # actual_format_return
            ctypes.POINTER(ctypes.c_ulong),  # nitems_return
            ctypes.POINTER(ctypes.c_ulong),  # bytes_after_return
            ctypes.POINTER(ctypes.c_void_p),  # prop_return
        ],
    ),
    # ── libX11: geometry ──
    (
        LIB_X11,
        "XGetWindowAttributes",
        ctypes.c_int,
        [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(XWindowAttributes)],
    ),
    (
        LIB_X11,
        "XTranslateCoordinates",
        ctypes.c_int,
        # display, src_window, dest_window, src_x, src_y,
        # dest_x_return*, dest_y_return*, child_return*
        [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_ulong),
        ],
    ),
    # ── libX11: capture ──
    (
        LIB_X11,
        "XGetImage",
        ctypes.POINTER(XImage),
        # display, drawable, x, y, width, height, plane_mask, format
        [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.c_ulong,
            ctypes.c_int,
        ],
    ),
    (LIB_X11, "XDestroyImage", ctypes.c_int, [ctypes.POINTER(XImage)]),
    # ── libX11: synthetic events ──
    (
        LIB_X11,
        "XSendEvent",
        ctypes.c_int,
        [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_long, ctypes.c_void_p],
    ),
    # ── libX11: keymap ──
    (LIB_X11, "XKeysymToKeycode", ctypes.c_ubyte, [ctypes.c_void_p, ctypes.c_ulong]),
    (LIB_X11, "XStringToKeysym", ctypes.c_ulong, [ctypes.c_char_p]),
    (
        LIB_X11,
        "XkbKeycodeToKeysym",
        ctypes.c_ulong,
        [ctypes.c_void_p, ctypes.c_ubyte, ctypes.c_int, ctypes.c_int],
    ),
    # ── libXtst: GLOBAL input ──
    (
        LIB_XTST,
        "XTestQueryExtension",
        ctypes.c_int,
        [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
        ],
    ),
    (
        LIB_XTST,
        "XTestFakeMotionEvent",
        ctypes.c_int,
        [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_ulong],
    ),
    (
        LIB_XTST,
        "XTestFakeButtonEvent",
        ctypes.c_int,
        [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong],
    ),
    (
        LIB_XTST,
        "XTestFakeKeyEvent",
        ctypes.c_int,
        [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong],
    ),
    # ── libgdk_pixbuf: JPEG encoding ──
    (
        LIB_GDK_PIXBUF,
        "gdk_pixbuf_new_from_data",
        ctypes.c_void_p,
        # data, colorspace(RGB=0), has_alpha, bits/sample, width, height, rowstride,
        # destroy_fn, destroy_fn_data
        [ctypes.c_char_p] + [ctypes.c_int] * 6 + [ctypes.c_void_p, ctypes.c_void_p],
    ),
    (
        LIB_GDK_PIXBUF,
        "gdk_pixbuf_scale_simple",
        ctypes.c_void_p,
        [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int],
    ),
    (LIB_GDK_PIXBUF, "gdk_pixbuf_get_width", ctypes.c_int, [ctypes.c_void_p]),
    (LIB_GDK_PIXBUF, "gdk_pixbuf_get_height", ctypes.c_int, [ctypes.c_void_p]),
    (
        LIB_GDK_PIXBUF,
        "gdk_pixbuf_save_to_bufferv",
        ctypes.c_int,
        # pixbuf, buffer*, size*, type("jpeg"), option keys, option values, GError**
        [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_char_p),
            ctypes.POINTER(ctypes.c_char_p),
            _GECARG,
        ],
    ),
    # ── libgobject / libglib: ownership ──
    (LIB_GOBJECT, "g_object_unref", None, [ctypes.c_void_p]),
    (LIB_GLIB, "g_free", None, [ctypes.c_void_p]),
    (LIB_GLIB, "g_error_free", None, [ctypes.c_void_p]),
    (LIB_GLIB, "g_array_free", ctypes.c_void_p, [ctypes.c_void_p, ctypes.c_int]),
)

_LIB_NAMES: tuple[str, ...] = (
    LIB_ATSPI,
    LIB_X11,
    LIB_XTST,
    LIB_GDK_PIXBUF,
    LIB_GOBJECT,
    LIB_GLIB,
)


# ── Load / bind, once per process ──

_libs_lock = threading.RLock()
_libs: "dict[str, Any] | None" = None
#: The AT-SPI/X11 availability verdict, computed once (it opens the display and the
#: AT-SPI bus, which is not free) and cached for the process. ``None`` = not yet
#: computed.
_available: "bool | None" = None
_unavailable_reason: str = ""
#: The process-wide X ``Display*``, opened lazily and held for the process lifetime.
_display: "Any | None" = None
_atspi_initialised = False
#: The installed ``XErrorHandler`` callback. Held in a module global so Python does
#: not collect it while Xlib still holds the pointer — ctypes cannot raise out of a
#: C callback, so a collected handler is a fault, not an exception.
_x_error_handler: "Any | None" = None
#: The error code of the most recent X error the handler recorded, read and cleared
#: by :func:`_take_x_error`. Guarded by ``_libs_lock``.
_last_x_error: int = 0


def _bind(lib: Any, symbol: str, restype: Any, argtypes: "Sequence[Any] | None") -> None:
    """Bind one symbol, refusing an unspecified ``argtypes``.

    Raises rather than accepting ``argtypes=None``: the ctypes default marshals a
    Python int as a 32-bit C int, which truncates every handle and XID.
    """
    if argtypes is None:
        raise ComputerUseError(f"{symbol} declared without argtypes")
    fn = getattr(lib, symbol)
    fn.argtypes = list(argtypes)
    fn.restype = restype


def _load_libraries() -> "dict[str, Any]":
    """Load and bind the native libraries, once per process.

    Deferred out of module scope so importing this module costs nothing and cannot
    fail on a host without the libraries — which is what keeps the whole package
    importable on the headless CI fleet.
    """
    global _libs
    with _libs_lock:
        if _libs is not None:
            return _libs
        loaded: dict[str, Any] = {}
        for name in _LIB_NAMES:
            try:
                loaded[name] = ctypes.CDLL(name)
            except OSError as exc:
                raise OSError(f"{name} not found: {exc}") from exc
        for lib_key, symbol, restype, argtypes in _FN_SPECS:
            try:
                _bind(loaded[lib_key], symbol, restype, argtypes)
            except AttributeError as exc:
                raise OSError(f"{lib_key}!{symbol} is unavailable") from exc
        _libs = loaded
        return _libs


def _lib(name: str) -> Any:
    """One bound library, loading the set on first use. Raises ``OSError`` when absent."""
    return _load_libraries()[name]


def _lib_atspi() -> Any:
    """The AT-SPI library for the lifecycle functions ONLY (``available``/``shutdown``).

    Every tree/action/text helper goes through :func:`_atspi` instead, which refuses
    before the bus is initialised. ``atspi_init`` itself has to be callable before
    availability is known, which is why this un-guarded accessor exists at all.
    """
    return _lib(LIB_ATSPI)


def _require_available() -> None:
    """Refuse — with the user-facing reason — unless :func:`available` is True.

    The floor under every native call. Without it, a handle helper reached before
    ``available()`` ran (or after it reported False) would hand ``libatspi`` an
    un-initialised bus or ``Xlib`` a NULL ``Display*`` — and Xlib does not return an
    error for a NULL display, it dereferences it. Measured on the merged tree: a
    ``drag`` on a host with no ``DISPLAY`` reached ``XDefaultRootWindow(NULL)`` through
    ``apps_linux.window_owns_point`` and took the whole process down with SIGSEGV,
    past every ``_guarded`` in the driver. A ``ComputerUseError`` is what the seam
    can convert into a refusal; a segfault is not.
    """
    if not available():
        raise ComputerUseError(
            unavailable_reason() or "the Linux computer-use driver is not available"
        )


def _atspi() -> Any:
    """The AT-SPI library, after :func:`_require_available`. Use this for every handle call."""
    _require_available()
    return _lib(LIB_ATSPI)


def _lib_x11() -> Any:
    return _lib(LIB_X11)


def _lib_xtst() -> Any:
    return _lib(LIB_XTST)


def _lib_gdk_pixbuf() -> Any:
    return _lib(LIB_GDK_PIXBUF)


def _lib_gobject() -> Any:
    return _lib(LIB_GOBJECT)


def _lib_glib() -> Any:
    return _lib(LIB_GLIB)


def reset_libraries() -> None:
    """Drop every cached handle and verdict. For tests and a backend swap."""
    global _libs, _available, _unavailable_reason, _display, _atspi_initialised
    global _x_error_handler, _last_x_error, _SYMBOLS_BY_LIB
    with _libs_lock:
        _libs = None
        _available = None
        _unavailable_reason = ""
        _display = None
        _atspi_initialised = False
        _x_error_handler = None
        _last_x_error = 0


# ── X error handler ──
# Xlib's DEFAULT handler calls ``exit()``. A BadWindow on a window that vanished
# between enumeration and a geometry read would therefore abort the whole gateway,
# so the first time the display opens we install a handler that RECORDS the error
# for the next checked call to read.

_XErrorHandlerType = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(XErrorEvent))


def _install_x_error_handler() -> None:
    """Replace Xlib's exit-calling default handler with a recording one.

    Idempotent and called under ``_libs_lock``. The handler object is stored in a
    module global for the process lifetime: ctypes cannot raise out of a C callback,
    and a handler collected while Xlib still holds its pointer is a fault.
    """
    global _x_error_handler
    if _x_error_handler is not None:
        return

    def handler(_display: Any, event: Any) -> int:
        global _last_x_error
        try:
            _last_x_error = int(event.contents.error_code)
        except Exception:
            _last_x_error = 1  # BadRequest-ish; the exact code is advisory
        return 0  # the return value is ignored by Xlib, but 0 is the convention

    cb = _XErrorHandlerType(handler)
    _lib_x11().XSetErrorHandler(ctypes.cast(cb, ctypes.c_void_p))
    _x_error_handler = cb


def _take_x_error() -> int:
    """Return and clear the last recorded X error code (0 when none)."""
    global _last_x_error
    with _libs_lock:
        code = _last_x_error
        _last_x_error = 0
        return code


# ── GError + ownership helpers ──


def _gerror(err: "ctypes.c_void_p") -> None:
    """Raise + free a set ``GError``; a NULL out-param is a no-op.

    The error is owned by the caller the moment AT-SPI sets it, so it is ALWAYS
    freed. The message is not read out of the ``GError`` struct — doing so means
    another cross-ABI struct read for prose a caller rarely surfaces — so the raise
    is generic and the pointer is discharged.
    """
    if not err.value:
        return
    _lib_glib().g_error_free(err)
    raise ComputerUseError("AT-SPI call returned a GError")


def unref(obj: int) -> None:
    """Drop one AT-SPI object reference, tolerating a NULL and never raising.

    A failure to unref leaks one object; raising would abort a tree walk partway
    through and leak every reference it had taken.
    """
    if not obj:
        return
    try:
        _lib_gobject().g_object_unref(ctypes.c_void_p(obj))
    except Exception:
        logger.debug("g_object_unref failed", exc_info=True)


@contextmanager
def owned(obj: int) -> Iterator[int]:
    """Own an AT-SPI reference for a block, always unref-ing it.

    A tree walk mints thousands of element references; leaking them is a real RSS
    bug. The twin of ``windows_ffi.owned`` / ``macos_ffi.ax_owned_attr``.
    """
    try:
        yield obj
    finally:
        unref(obj)


def _gchar_to_str(ptr: int) -> str:
    """Copy a ``gchar*`` into a Python ``str`` and ``g_free`` it. ``""`` for NULL.

    AT-SPI's string accessors return heap allocated with ``g_malloc``; the Create
    rule makes the caller own them, so the copy-then-free is mandatory — a dropped
    free is a per-node leak on the hot walk path.
    """
    if not ptr:
        return ""
    try:
        value = ctypes.cast(ptr, ctypes.c_char_p).value
        return value.decode("utf-8", "replace") if value else ""
    finally:
        _lib_glib().g_free(ctypes.c_void_p(ptr))


# ── Availability / lifecycle ──


def display_name() -> "str | None":
    """The ``DISPLAY`` environment variable, or ``None``."""
    return os.environ.get("DISPLAY")


def _open_display() -> int:
    """Open (once) the process-wide X display and install the error handler.

    Returns the ``Display*`` as an int, or 0 when the open failed. ``XOpenDisplay``
    takes ``NULL`` to mean "use ``$DISPLAY``", which is exactly what we want — but
    the handler is installed only after a successful open, because installing it
    needs the library and a failed open is the one case where no display exists.
    """
    global _display
    with _libs_lock:
        if _display is not None:
            return int(_display or 0)
        name = display_name()
        encoded = name.encode("utf-8") if name else None
        disp = _lib_x11().XOpenDisplay(encoded)
        _display = disp or 0
        if _display:
            _install_x_error_handler()
        return int(_display or 0)


def _compute_availability() -> "tuple[bool, str]":
    """Check the session, then load libs, open the display, init AT-SPI. ``(ok, reason)``; never raises.

    The SESSION check comes first and loads nothing: ``DISPLAY`` unset is the shape
    the whole upstream Linux CI fleet is in, and the spec's hard requirement is that
    ``status()`` reports it without mapping a single native library (a runner with
    no ``libatspi`` would otherwise report the missing soname instead of the
    actionable no-display reason, and the mapped libraries would be the first
    native code that process ever ran). A Wayland-only session — ``WAYLAND_DISPLAY``
    or ``XDG_SESSION_TYPE=wayland`` with no ``DISPLAY`` — is named as such, because
    that is the limitation the packaged doc and Settings pane promise to show; a
    Wayland session WITH XWayland has ``DISPLAY`` set and proceeds as X11.

    Then the libraries (a missing one names itself), the X server, and the AT-SPI
    bus (an unreachable ``org.a11y.Bus`` or a non-accessibility session). The first
    failing step wins.
    """
    global _atspi_initialised
    if display_name() is None:
        if (
            os.environ.get("WAYLAND_DISPLAY")
            or os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"
        ):
            return False, (
                "Wayland session without an X display: Wayland has no unprivileged window "
                "capture or global input, so computer use needs an X11 (or XWayland) DISPLAY"
            )
        return False, "no X display (DISPLAY unset)"
    try:
        _load_libraries()
    except OSError as exc:
        return False, str(exc)
    if not _open_display():
        return False, f"could not open X display {display_name()!r}"
    try:
        rc = int(_lib_atspi().atspi_init())
    except Exception as exc:  # noqa: BLE001 - a bus failure must degrade, not raise
        return False, f"AT-SPI bus not reachable: {exc}"
    if rc not in (0, 1):
        # 0 = initialised now, 1 = already initialised. Anything else is a failure.
        return False, f"atspi_init() returned {rc}"
    _atspi_initialised = True
    return True, ""


def available() -> bool:
    """Whether the Linux native surface can be used here. Cached; never raises.

    True only when every library loads, ``XOpenDisplay`` succeeds, AND
    ``atspi_init()`` reports success — the three things a working driver needs. The
    verdict is cached because computing it opens the display and the AT-SPI bus.
    """
    global _available, _unavailable_reason
    with _libs_lock:
        if _available is None:
            _available, _unavailable_reason = _compute_availability()
        return _available


def unavailable_reason() -> str:
    """The first failing availability step, or ``""`` when available.

    Names the step a user can act on: a missing library by soname, ``DISPLAY``
    unset, an unreachable X server, or an unreachable AT-SPI bus (the common
    "accessibility bridge not enabled" failure).
    """
    with _libs_lock:
        if _available is None:
            available()
        return _unavailable_reason


def shutdown() -> None:
    """Close the X display and exit AT-SPI if they were started. Idempotent."""
    global _display, _atspi_initialised, _available, _unavailable_reason
    with _libs_lock:
        if _display:
            try:
                _lib_x11().XCloseDisplay(ctypes.c_void_p(int(_display)))
            except Exception:
                logger.debug("XCloseDisplay failed", exc_info=True)
        _display = None
        if _atspi_initialised:
            try:
                _lib_atspi().atspi_exit()
            except Exception:
                logger.debug("atspi_exit failed", exc_info=True)
        _atspi_initialised = False
        _available = None
        _unavailable_reason = ""


def _display_ptr() -> Any:
    """The process-wide ``Display*`` as a ``c_void_p``, refusing when there is none.

    Guarded by :func:`_require_available` and then by the open itself: Xlib
    dereferences a NULL ``Display*`` rather than reporting it, so returning ``0``
    here would turn the next ``XDefaultRootWindow`` into a segfault.
    """
    _require_available()
    disp = _open_display()
    if not disp:
        raise ComputerUseError(unavailable_reason() or "no X display")
    return ctypes.c_void_p(disp)
