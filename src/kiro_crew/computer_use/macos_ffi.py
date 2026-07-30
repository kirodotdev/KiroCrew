"""The ONLY module in this package that touches ctypes.

Everything native lives here: CoreFoundation memory discipline, the
Accessibility (AX) attribute/action calls, CoreGraphics window enumeration and
event synthesis, and the in-process ImageIO encode. Every other computer-use
module talks to macOS through the helpers below, so the FFI hazards are audited
in one place instead of scattered across five files.

``import ctypes`` is a top-level import statement (AUTOSDE ``top-level-imports``
is about statements, not about *loading* a library), but **no ``CDLL`` or
``find_library`` runs at module scope**. The frameworks load inside
:func:`_frameworks`, cached in a module global, and raise
:class:`ComputerUseUnsupported` off macOS — so this module imports cleanly on the
Linux and Windows CI shards and a test can exercise the binding logic with a fake
``CDLL``.

Six hazards this module exists to contain. Each was reproduced live on a real Mac
(darwin-arm64, Darwin 25.5.0); the failure mode of the first three is a **process
abort**, not a Python exception, so none of them can be handled by a caller.

1. **A missing ``argtypes`` truncates a 64-bit pointer to 32 bits and
   SEGFAULTS.** One omitted ``argtypes`` produced a real ``EXIT=139``. Hence the
   declarative :data:`_FN_SPECS` table and a single bind pass: no function in
   this module can be reached un-bound, and :func:`_bind` RAISES on
   ``argtypes=None`` rather than accepting the ctypes default.
2. **``CFArrayGetValueAtIndex`` past the end raises an UNCATCHABLE ObjC
   ``NSRangeException``** — verified ``EXIT=134``, ``libc++abi: terminating``, on
   an empty ``CFArray``. ctypes cannot translate an ObjC exception into a Python
   one, so every array read goes through :func:`cf_array_items`, which reads the
   count FIRST and never indexes out of range.
3. **``CGEventCreateScrollWheelEvent`` is VARIADIC and ctypes marshals its
   variadic tail incorrectly on arm64.** Asking for ``(axis1=-3, axis2=0)``
   through the 5-argument form produced ``axis2=30416`` — garbage that would
   scroll a live window sideways by an arbitrary amount. Only the fixed
   (``wheelCount=1``) prototype is declared, and the second axis is set
   afterwards through the non-variadic
   ``CGEventSetIntegerValueField`` (verified exact for every input).
4. **``kCFTypeDictionaryKeyCallBacks`` is a STRUCT, not a pointer.**
   ``c_void_p.in_dll(...).value`` reads its first word — ``None`` — and passing
   that means "no callbacks", i.e. CF neither retains nor releases the dictionary
   contents. :func:`_cf_type_dict` passes ``ctypes.addressof`` instead.
5. **A synthesized key event inherits the user's LIVE modifier state.** Typing
   ``abc`` produced ``' I Abc'`` until the events were built from a *private*
   event source with flags set EXPLICITLY on every event. See :func:`post_key`.
6. **A CFString leak is a real RSS bug.** One tree walk creates thousands of
   them, so :func:`cf_string` is a context manager that always ``CFRelease``s.
7. **``CGEventCreateMouseEvent`` takes a ``CGPoint`` BY VALUE**, and there is no
   generic "mouse down" event type — the type is PER-BUTTON. Passing two bare
   doubles mis-marshals the call under the AArch64 convention, and posting a
   right-click as ``kCGEventLeftMouseDown`` with ``button=1`` is delivered as a
   LEFT click. Both are handled by :data:`MOUSE_EVENT_TYPES` +
   :func:`_mouse_event`. A multi-click is also NOT repeated pairs: it is a pair
   carrying ``kCGMouseEventClickState``, which is where AppKit reads
   ``NSEvent.clickCount`` from.

**One deliberate exception to the "``CGEventPostToPid`` only" rule.**
``CGEventPost`` (the global HID tap) and ``CGWarpMouseCursorPosition`` are bound
and are called from :func:`post_mouse_global` / :func:`post_mouse_drag_global`
and nowhere else. Those are the ``click_method: "global"`` path, which
deliberately takes over the operator's physical mouse and is therefore reachable
only when the model NAMED that method — ``auto`` never resolves onto it, and the
resolution happens upstream at the dispatch chokepoint. A test pins the call-site
count, so a stray global post anywhere else in the module fails CI.

Every AX read is also type-checked with ``CFGetTypeID`` *before* the value is
used: AX attributes are polymorphic and a wrong-type read is another segfault
rather than an exception.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import struct
import tempfile
import threading
import time
from contextlib import contextmanager
from ctypes import (
    POINTER,
    c_bool,
    c_char_p,
    c_double,
    c_float,
    c_int,
    c_int32,
    c_int64,
    c_long,
    c_size_t,
    c_ubyte,
    c_uint16,
    c_uint32,
    c_uint64,
    c_void_p,
)
from dataclasses import dataclass
from typing import Any, Iterator, Sequence

from kiro_crew import platform_compat
from kiro_crew.computer_use.types import (
    AX_PARENT,
    AX_POSITION,
    AX_SIZE,
    MOUSE_BUTTON_LEFT,
    MOUSE_BUTTON_MIDDLE,
    MOUSE_BUTTON_RIGHT,
    SCREENSHOT_DIR_NAME,
    ComputerUseUnsupported,
)

logger = logging.getLogger(__name__)

# ── Library keys (indexes into the bound handle bundle) ──
LIB_CF = "cf"
LIB_AX = "ax"
LIB_CG = "cg"
LIB_IO = "io"
LIB_PROC = "proc"

# Framework names passed to ``ctypes.util.find_library``. ``proc`` resolves to
# ``/usr/lib/libproc.dylib``; the explicit path is the documented fallback for a
# stripped environment where ``find_library`` returns None.
_FRAMEWORK_NAMES: dict[str, str] = {
    LIB_CF: "CoreFoundation",
    LIB_AX: "ApplicationServices",
    LIB_CG: "CoreGraphics",
    LIB_IO: "ImageIO",
    LIB_PROC: "proc",
}
_LIBPROC_FALLBACK_PATH = "/usr/lib/libproc.dylib"

# ── CoreFoundation ABI constants ──
# Stable published values, not derived at runtime. They live here (rather than in
# the platform-free ``types.py``) for the same reason ``keymap.py`` holds the
# CoreGraphics flag masks: they are ABI facts about *this* FFI surface, and a
# reader auditing a call needs the number next to the call.
K_CF_STRING_ENCODING_UTF8 = 0x08000100
# kCFNumberSInt32Type. Deliberately the sized variant rather than
# kCFNumberIntType(9): both were verified to work for the ImageIO options, and
# the sized name states the C type of the buffer we actually pass.
K_CF_NUMBER_SINT32 = 3
K_CF_NUMBER_DOUBLE = 13

# ``AXValueType`` discriminants (``AXValue.h``). An ``AXValue`` is an opaque box:
# ``AXValueGetValue`` will only unbox it into a buffer of the type it actually
# holds, so the type has to be named at the call. Reading ``AXPosition`` (a
# ``CGPoint``) into a ``CGSize`` buffer would be a silent field transposition —
# ``y`` read as ``width`` — so the discriminant is passed explicitly and the call
# is checked rather than assumed.
K_AX_VALUE_CG_POINT = 1
K_AX_VALUE_CG_SIZE = 2

# ── Accessibility error codes ──
AX_ERR_SUCCESS = 0
# kAXErrorCannotComplete. Electron/Chromium apps answer this to EVERY attribute
# read until ``AXManualAccessibility`` is set. Not a fatal error — a retry signal.
AX_ERR_CANNOT_COMPLETE = -25204
# kAXErrorActionUnsupported. Returned by an element that ADVERTISED the action:
# a real ``AXScrollArea`` listed ``AXScrollDownByPage`` and then refused it. The
# advertised action list is a hint, never a contract.
#
# The two codes below are DISTINCT and were both confirmed empirically on this
# macOS rather than copied from a header (they had been transposed onto the same
# value, which made the scroll fallback's "was it the action that was refused?"
# test answer for the wrong error):
#   AXUIElementPerformAction(app, "AXNotARealAction")      -> -25206
#   AXUIElementCopyAttributeValue(app, "AXNotAnAttribute") -> -25212
AX_ERR_ACTION_UNSUPPORTED = -25206
# kAXErrorAttributeUnsupported. Extremely common and completely benign — window
# elements answer it for ``AXValue``/``AXEnabled``, and every application element
# on this macOS answers it for ``AXBundleIdentifier`` (see ``apps_macos``).
AX_ERR_ATTRIBUTE_UNSUPPORTED = -25205
AX_ERR_NO_VALUE = -25212

# The Electron/Chromium accessibility opt-in, plus its bounded wait. Measured:
# Obsidian exposed a 13-node stub before the opt-in and 574 nodes after it;
# Chrome went from a hard ``-25204`` to 1,431 nodes.
AX_MANUAL_ACCESSIBILITY = "AXManualAccessibility"
ELECTRON_OPT_IN_WAIT_SECS = 2.0
ELECTRON_OPT_IN_POLL_SECS = 0.25
# MANDATORY, immediately after creating the application element. ctypes releases
# the GIL around the C call, so an unresponsive target app would otherwise park
# the calling worker thread indefinitely with no way to interrupt it.
AX_MESSAGING_TIMEOUT_SECS = 2.0

# ── CoreGraphics window-list ABI constants ──
K_CG_WINDOW_LIST_ON_SCREEN_ONLY = 1
K_CG_WINDOW_LIST_EXCLUDE_DESKTOP = 16
K_CG_WINDOW_LIST_INCLUDING_WINDOW = 8
K_CG_WINDOW_IMAGE_BOUNDS_IGNORE_FRAMING = 1
K_CG_NULL_WINDOW_ID = 0

# Window-list dictionary keys. Verified present on this macOS: the dict carries
# exactly {Alpha, Bounds, IsOnscreen, Layer, MemoryUsage, Name, Number,
# OwnerName, OwnerPID, SharingState, StoreType} — note there is NO bundle-id key,
# which is why ``apps_macos`` resolves the bundle id from the executable path.
CG_WINDOW_NUMBER = "kCGWindowNumber"
CG_WINDOW_OWNER_PID = "kCGWindowOwnerPID"
CG_WINDOW_OWNER_NAME = "kCGWindowOwnerName"
CG_WINDOW_NAME = "kCGWindowName"
CG_WINDOW_LAYER = "kCGWindowLayer"
# The window rect, as a CFDictionary of {X, Y, Width, Height} in TOP-LEFT screen
# coordinates (CoreGraphics' own convention for this key).
CG_WINDOW_BOUNDS = "kCGWindowBounds"
CG_BOUNDS_X = "X"
CG_BOUNDS_Y = "Y"
CG_BOUNDS_W = "Width"
CG_BOUNDS_H = "Height"
# Only layer 0 is a real application window; menu bars, the Dock, tooltips and
# shadows live on other layers and their owning pid is often a system agent.
CG_WINDOW_LAYER_NORMAL = 0

# ── CoreGraphics event ABI constants ──
# ``kCGEventSourceStatePrivate``. NEVER pass NULL: an event built from the default
# (HID) source inherits the user's live modifier state, which is the measured
# ``"a","b","c"`` -> ``' I Abc'`` bug this source exists to prevent.
#
# The value is **-1**, and it is NOT interchangeable with 1.
# ``CGEventTypes.h`` declares ``CGEventSourceStateID`` as
# ``{kCGEventSourceStatePrivate = -1, kCGEventSourceStateCombinedSessionState = 0,
# kCGEventSourceStateHIDSystemState = 1}`` — so the previous ``1`` selected the
# SHARED HID table by name while claiming to be private. Verified live on
# darwin-arm64: ``CGEventSourceGetSourceStateID`` reports an opaque id for -1 and
# literally ``1`` for 1. The symptom was masked only because every posting path
# (``post_key`` / ``post_text`` / ``post_scroll`` / ``_mouse_event``) also calls
# ``CGEventSetFlags(event, 0)``; a future path that omits that would reintroduce
# the modifier-inheritance bug, and ``macos_skylight``'s deliberate "HID source
# unlike every other path in this package" contrast was vacuous while these were
# the same table. Signed because the enum is an ``int32_t``, so ``_FN_SPECS`` binds
# ``CGEventSourceCreate`` with ``c_int32`` rather than a uint.
K_CG_EVENT_SOURCE_STATE_PRIVATE = -1
# kCGScrollEventUnitLine. The enum has exactly TWO members — ``Pixel`` (0) and
# ``Line`` (1); there is no page unit, so a "page" of scrolling is expressed as a
# line count in the delta, not by the unit. Named accurately here because the old
# spelling (``…_UNIT_PAGE``) read as though the OS were doing the page conversion
# and invited a future caller to pass a page COUNT as the delta.
K_CG_SCROLL_EVENT_UNIT_LINE = 1
#: Lines of scrolling treated as one "page" for the wheel-event fallback. The AX
#: page actions are tried first (``macos_driver.scroll``); this is only the
#: synthesized approximation for elements that refuse them.
CG_SCROLL_LINES_PER_PAGE = 10
# kCGScrollWheelEventDeltaAxis1 (vertical) / Axis2 (horizontal). Set through
# ``CGEventSetIntegerValueField`` because the multi-axis constructor is variadic.
K_CG_SCROLL_DELTA_AXIS_1 = 11
K_CG_SCROLL_DELTA_AXIS_2 = 12

# ── CoreGraphics mouse-event ABI constants ──
# ``CGEventType`` members. There is NO generic "mouse down" — the type is
# PER-BUTTON, and a right-click posted as ``kCGEventLeftMouseDown`` with
# ``button=1`` is silently delivered as a LEFT click by AppKit. Hence the explicit
# per-button triples in :data:`MOUSE_EVENT_TYPES` rather than one type plus a
# button number.
K_CG_EVENT_LEFT_MOUSE_DOWN = 1
K_CG_EVENT_LEFT_MOUSE_UP = 2
K_CG_EVENT_RIGHT_MOUSE_DOWN = 3
K_CG_EVENT_RIGHT_MOUSE_UP = 4
K_CG_EVENT_MOUSE_MOVED = 5
K_CG_EVENT_LEFT_MOUSE_DRAGGED = 6
K_CG_EVENT_RIGHT_MOUSE_DRAGGED = 7
K_CG_EVENT_OTHER_MOUSE_DOWN = 25
K_CG_EVENT_OTHER_MOUSE_UP = 26
K_CG_EVENT_OTHER_MOUSE_DRAGGED = 27

# ``CGMouseButton`` members, paired with the event types above.
K_CG_MOUSE_BUTTON_LEFT = 0
K_CG_MOUSE_BUTTON_RIGHT = 1
K_CG_MOUSE_BUTTON_CENTER = 2

#: ``button_name -> (button_number, down_type, up_type, dragged_type)``.
#: Keyed by the SAME strings ``types.MOUSE_BUTTONS`` publishes, so the wire
#: vocabulary and the ABI mapping meet in exactly one place. Middle-click uses the
#: ``OtherMouse`` family, which is what "center" means in ``CGMouseButton``.
MOUSE_EVENT_TYPES: dict[str, tuple[int, int, int, int]] = {
    MOUSE_BUTTON_LEFT: (
        K_CG_MOUSE_BUTTON_LEFT,
        K_CG_EVENT_LEFT_MOUSE_DOWN,
        K_CG_EVENT_LEFT_MOUSE_UP,
        K_CG_EVENT_LEFT_MOUSE_DRAGGED,
    ),
    MOUSE_BUTTON_RIGHT: (
        K_CG_MOUSE_BUTTON_RIGHT,
        K_CG_EVENT_RIGHT_MOUSE_DOWN,
        K_CG_EVENT_RIGHT_MOUSE_UP,
        K_CG_EVENT_RIGHT_MOUSE_DRAGGED,
    ),
    MOUSE_BUTTON_MIDDLE: (
        K_CG_MOUSE_BUTTON_CENTER,
        K_CG_EVENT_OTHER_MOUSE_DOWN,
        K_CG_EVENT_OTHER_MOUSE_UP,
        K_CG_EVENT_OTHER_MOUSE_DRAGGED,
    ),
}

# kCGMouseEventClickState. A double click is NOT two down/up pairs — it is a pair
# whose click state is 2, and AppKit ignores the repetition otherwise (verified
# behaviour of every Cocoa control: ``NSEvent.clickCount`` is read from this
# field). So the count is written onto each event rather than expressed by
# posting more of them.
K_CG_MOUSE_EVENT_CLICK_STATE = 1

# kCGHIDEventTap — the destination ``CGEventPost`` delivers to. Bound (and used)
# ONLY on the ``global`` click path, which is the one path that deliberately
# targets the system-wide tap because it is simulating a physical mouse; every
# other path uses ``CGEventPostToPid``. See :func:`post_mouse_global`.
K_CG_HID_EVENT_TAP = 0

#: Intermediate ``MouseDragged`` events synthesized between a drag's endpoints.
#: A bare down-at-A / up-at-B pair is NOT a drag to most apps: a canvas records
#: two isolated points and a text view selects nothing, because the gesture is
#: recognized from the intermediate motion. 6 steps is what the live prototype
#: needed for TextEdit to register a selection.
DRAG_STEPS = 6
#: Pause between synthesized drag events. Without it every event carries an
#: identical timestamp and AppKit's gesture recognizer coalesces them into one
#: motion, which is the same silent no-op as sending no steps at all.
DRAG_STEP_DELAY_SECS = 0.02
#: Pause between the two halves of a click pair, and between repeated clicks of a
#: multi-click. Below the OS double-click interval so a ``click_count=2`` is
#: recognized as a double click rather than as two separate clicks.
CLICK_PAIR_DELAY_SECS = 0.01

# ── ImageIO ──
UTI_JPEG = "public.jpeg"
IO_KEY_MAX_PIXEL_SIZE = "kCGImageDestinationImageMaxPixelSize"
IO_KEY_LOSSY_QUALITY = "kCGImageDestinationLossyCompressionQuality"

# ``proc_pidpath`` buffer size (PROC_PIDPATHINFO_MAXSIZE = 4 * MAXPATHLEN).
PROC_PIDPATH_MAX = 4096

# Sentinel for "this symbol returns void". ``restype = None`` is *meaningful* to
# ctypes (it means "no return value"), so it cannot double as "unspecified" —
# without a distinct sentinel a typo'd table row would silently bind a void
# restype onto a pointer-returning function and truncate the result.
_VOID = object()

# ``CFTypeID`` is a ``unsigned long`` (64-bit on every supported Mac). Aliased so
# the table below states the CF type name rather than repeating a width, and so a
# future 32-bit target is one edit.
_CF_TYPE_ID = c_uint64


class CGPoint(ctypes.Structure):
    """CoreGraphics ``CGPoint``. Declared as a struct, never as two doubles."""

    _fields_ = [("x", c_double), ("y", c_double)]


class CGSize(ctypes.Structure):
    """CoreGraphics ``CGSize``."""

    _fields_ = [("width", c_double), ("height", c_double)]


class CGRect(ctypes.Structure):
    """CoreGraphics ``CGRect``.

    Passing four bare doubles where a nested struct is expected mis-marshals the
    call on both arm64 and x86_64 — the register/stack layout of a struct
    argument is not the layout of its flattened members.
    """

    _fields_ = [("origin", CGPoint), ("size", CGSize)]


# ``(lib_key, symbol, restype, argtypes)``. ``argtypes`` is MANDATORY on every
# row; :func:`_bind` raises when it is None. Bound in one pass at first
# :func:`_frameworks` call so no symbol can be reached un-bound.
#
# ``CGEventPostToPid`` is the default and the ONLY delivery for every keyboard,
# scroll and app-scoped mouse path: it targets the app the model addressed rather
# than whatever window the user is actually working in. ``CGEventPost`` (the
# global HID tap) is ALSO bound, but reached from exactly one function —
# :func:`post_mouse_global` — because the ``global`` click method deliberately
# simulates a physical mouse and there is no per-pid form of that. A test pins
# that call-site count at one, so a stray ``CGEventPost`` anywhere else fails CI.
# ``CGWarpMouseCursorPosition`` is in the same category: bound, and called from
# that one function only.
_FN_SPECS: tuple[tuple[str, str, Any, list[Any]], ...] = (
    # ── CoreFoundation: memory + type identity ──
    (LIB_CF, "CFRelease", _VOID, [c_void_p]),
    (LIB_CF, "CFRetain", c_void_p, [c_void_p]),
    (LIB_CF, "CFGetTypeID", _CF_TYPE_ID, [c_void_p]),
    # Identity, not pointer equality: two DIFFERENT ``AXUIElement`` refs can name
    # the same UI element (``AXFocusedUIElement`` and the same node reached by
    # walking ``AXChildren`` are distinct allocations), and comparing the raw
    # addresses would answer "not the same" for the element the caret is in.
    (LIB_CF, "CFEqual", c_bool, [c_void_p, c_void_p]),
    # ── CoreFoundation: strings ──
    (LIB_CF, "CFStringCreateWithCString", c_void_p, [c_void_p, c_char_p, c_uint32]),
    # CFIndex is a signed long (64-bit here), NOT an int32 — a 32-bit restype
    # would truncate the length of a large AXValue and undersize the buffer.
    (LIB_CF, "CFStringGetLength", c_long, [c_void_p]),
    (LIB_CF, "CFStringGetCString", c_bool, [c_void_p, c_char_p, c_long, c_uint32]),
    (LIB_CF, "CFStringGetTypeID", _CF_TYPE_ID, []),
    # ── CoreFoundation: arrays ──
    (LIB_CF, "CFArrayGetTypeID", _CF_TYPE_ID, []),
    (LIB_CF, "CFArrayGetCount", c_long, [c_void_p]),
    (LIB_CF, "CFArrayGetValueAtIndex", c_void_p, [c_void_p, c_long]),
    # ── CoreFoundation: dictionaries ──
    (LIB_CF, "CFDictionaryGetTypeID", _CF_TYPE_ID, []),
    (LIB_CF, "CFDictionaryGetValue", c_void_p, [c_void_p, c_void_p]),
    (LIB_CF, "CFDictionaryCreateMutable", c_void_p, [c_void_p, c_long, c_void_p, c_void_p]),
    (LIB_CF, "CFDictionarySetValue", _VOID, [c_void_p, c_void_p, c_void_p]),
    # ── CoreFoundation: numbers + booleans ──
    (LIB_CF, "CFNumberGetTypeID", _CF_TYPE_ID, []),
    (LIB_CF, "CFNumberCreate", c_void_p, [c_void_p, c_int, c_void_p]),
    (LIB_CF, "CFNumberGetValue", c_bool, [c_void_p, c_int, c_void_p]),
    (LIB_CF, "CFBooleanGetTypeID", _CF_TYPE_ID, []),
    (LIB_CF, "CFBooleanGetValue", c_bool, [c_void_p]),
    # ── CoreFoundation: data (the ImageIO encode sink) ──
    (LIB_CF, "CFDataCreateMutable", c_void_p, [c_void_p, c_long]),
    (LIB_CF, "CFDataGetLength", c_long, [c_void_p]),
    (LIB_CF, "CFDataGetBytePtr", POINTER(c_ubyte), [c_void_p]),
    # ── ApplicationServices (Accessibility) ──
    (LIB_AX, "AXIsProcessTrusted", c_bool, []),
    (LIB_AX, "AXUIElementGetTypeID", _CF_TYPE_ID, []),
    (LIB_AX, "AXUIElementCreateApplication", c_void_p, [c_int32]),
    (LIB_AX, "AXUIElementCopyAttributeValue", c_int32, [c_void_p, c_void_p, POINTER(c_void_p)]),
    (LIB_AX, "AXUIElementSetAttributeValue", c_int32, [c_void_p, c_void_p, c_void_p]),
    (LIB_AX, "AXUIElementCopyActionNames", c_int32, [c_void_p, POINTER(c_void_p)]),
    (LIB_AX, "AXUIElementPerformAction", c_int32, [c_void_p, c_void_p]),
    # ``AXValue`` unboxing, for ``AXPosition``/``AXSize``. ``AXValueGetType``
    # is checked BEFORE ``AXValueGetValue`` so a mistyped box is refused rather
    # than transposed into the wrong struct fields.
    (LIB_AX, "AXValueGetTypeID", _CF_TYPE_ID, []),
    (LIB_AX, "AXValueGetType", c_int32, [c_void_p]),
    (LIB_AX, "AXValueGetValue", c_bool, [c_void_p, c_int32, c_void_p]),
    # Settability, the only way to tell a read-only text field from an editable
    # one — an ``AXTextField`` the app has disabled for input still reports
    # ``AXEnabled=true`` and still advertises a value.
    (LIB_AX, "AXUIElementIsAttributeSettable", c_int32, [c_void_p, c_void_p, POINTER(c_bool)]),
    # Takes a C ``float``, not a double — a c_double here passes the value in the
    # wrong register class and the timeout is never applied.
    (LIB_AX, "AXUIElementSetMessagingTimeout", c_int32, [c_void_p, c_float]),
    # ── CoreGraphics: window list + capture ──
    (LIB_CG, "CGWindowListCopyWindowInfo", c_void_p, [c_uint32, c_uint32]),
    (LIB_CG, "CGWindowListCreateImage", c_void_p, [CGRect, c_uint32, c_uint32, c_uint32]),
    (LIB_CG, "CGImageGetWidth", c_size_t, [c_void_p]),
    (LIB_CG, "CGImageGetHeight", c_size_t, [c_void_p]),
    (LIB_CG, "CGImageRelease", _VOID, [c_void_p]),
    # Preflight ONLY. ``CGRequestScreenCaptureAccess`` pops a system dialog from
    # whatever process asks, which from a background sidecar is an unexplained
    # prompt the operator cannot attribute — never called anywhere.
    (LIB_CG, "CGPreflightScreenCaptureAccess", c_bool, []),
    # ── CoreGraphics: event synthesis ──
    (LIB_CG, "CGEventSourceCreate", c_void_p, [c_int32]),
    (LIB_CG, "CGEventCreateKeyboardEvent", c_void_p, [c_void_p, c_uint16, c_bool]),
    (LIB_CG, "CGEventKeyboardSetUnicodeString", _VOID, [c_void_p, c_long, POINTER(c_uint16)]),
    # The FIXED-arity prototype only: ``wheelCount`` is pinned to 1 by every
    # caller and the second axis is applied through CGEventSetIntegerValueField.
    # Declaring the variadic tail produced garbage on arm64 (hazard 3 above).
    (LIB_CG, "CGEventCreateScrollWheelEvent", c_void_p, [c_void_p, c_uint32, c_uint32, c_int32]),
    (LIB_CG, "CGEventSetIntegerValueField", _VOID, [c_void_p, c_uint32, c_int64]),
    (LIB_CG, "CGEventSetFlags", _VOID, [c_void_p, c_uint64]),
    (LIB_CG, "CGEventPostToPid", _VOID, [c_int32, c_void_p]),
    # ── CoreGraphics: mouse-event synthesis ──
    # ``CGPoint`` BY VALUE. Declaring the location as two bare doubles
    # mis-marshals the whole call under the AArch64 calling convention — the
    # button argument lands in the wrong register and the event is created for an
    # arbitrary button at an arbitrary point. Verified in the live prototype: the
    # struct argtype is mandatory, not documentation.
    (LIB_CG, "CGEventCreateMouseEvent", c_void_p, [c_void_p, c_uint32, CGPoint, c_uint32]),
    # The GLOBAL HID tap. Reached ONLY from ``post_mouse_global`` — the
    # ``click_method: "global"`` path, which the model must name explicitly. There
    # is no per-pid form of "simulate a physical mouse", which is exactly why that
    # method has to be asked for by name and every other path does not.
    (LIB_CG, "CGEventPost", _VOID, [c_uint32, c_void_p]),
    # Warps the operator's PHYSICAL cursor. Same one call site, same rule.
    # ``CGPoint`` by value again.
    (LIB_CG, "CGWarpMouseCursorPosition", c_int32, [CGPoint]),
    # ── ImageIO ──
    (
        LIB_IO,
        "CGImageDestinationCreateWithData",
        c_void_p,
        [c_void_p, c_void_p, c_size_t, c_void_p],
    ),
    (LIB_IO, "CGImageDestinationAddImage", _VOID, [c_void_p, c_void_p, c_void_p]),
    (LIB_IO, "CGImageDestinationFinalize", c_bool, [c_void_p]),
    # ── libproc ──
    (LIB_PROC, "proc_pidpath", c_int, [c_int, c_void_p, c_uint32]),
)

# Symbols that are bound if present and simply absent otherwise. Same
# ``argtypes``-mandatory discipline; the only difference is that a missing symbol
# is not a failure.
#
# ``_AXUIElementGetWindow`` is the only way to learn which CoreGraphics window an
# ``AXUIElement`` corresponds to, and correlating the two is a correctness
# requirement, not a nicety: ``AXWindows[0]`` and the CoreGraphics window list are
# INDEPENDENTLY ordered. Verified live on Notes — ``AXWindows[0]`` was an
# ``AXDialog`` with an empty title (3 nodes) while the frontmost CG window was the
# real 103-node document window, so walking ``AXWindows[0]`` and capturing the CG
# frontmost window would have returned a tree and an image of DIFFERENT WINDOWS.
# The leading underscore marks it private-but-stable (it has shipped for a decade
# and every accessibility client uses it); it is optional here so a future macOS
# that removes it degrades to the title/AXMain fallback in ``snapshot_macos``
# rather than breaking the feature.
_OPTIONAL_FN_SPECS: tuple[tuple[str, str, Any, list[Any]], ...] = (
    (LIB_AX, "_AXUIElementGetWindow", c_int32, [c_void_p, POINTER(c_uint32)]),
)


@dataclass(frozen=True)
class Libs:
    """The five bound framework handles.

    Frozen and built exactly once: rebinding on every call would re-run 50
    ``getattr`` + attribute assignments per tree walk, and a partially bound
    handle is the hazard the single bind pass exists to prevent.
    """

    cf: Any
    ax: Any
    cg: Any
    io: Any
    proc: Any


@dataclass(frozen=True)
class TypeIds:
    """Cached ``CFTypeID`` values for the types we accept from AX and CF.

    Cached because every attribute read compares against them; each is a C call
    and a tree walk performs thousands of reads.
    """

    string: int
    array: int
    dictionary: int
    number: int
    boolean: int
    ax_element: int
    # ``AXValue`` — the opaque box ``AXPosition``/``AXSize`` come wrapped in. A
    # separate id from ``ax_element``: handing an ``AXValue`` to an
    # element-expecting call (or the reverse) is a segfault, not a type error.
    ax_value: int


@dataclass(frozen=True)
class WindowInfo:
    """One entry of the CoreGraphics on-screen window list."""

    window_id: int
    pid: int
    owner_name: str
    title: str
    layer: int
    # Screen rect in TOP-LEFT coordinates, or ``None`` when the entry carried no
    # usable ``kCGWindowBounds``. Needed to answer "which app owns this pixel?",
    # which is what confines a real-pointer click to the authorized application.
    bounds: "tuple[float, float, float, float] | None" = None


_libs: "Libs | None" = None
_type_ids: "TypeIds | None" = None
_event_source: "c_void_p | None" = None
# One lock for the lazy singletons. The MCP dispatch layer is single-threaded
# today, but the gateway offloads snapshots to an executor, so two threads can
# reach first-use concurrently and a double bind would leak an event source.
_init_lock = threading.Lock()


def _bind(lib: Any, symbol: str, restype: Any, argtypes: "Sequence[Any] | None") -> None:
    """Set BOTH ``restype`` and ``argtypes`` on one symbol, or raise.

    ``argtypes is None`` is a programming error, not a permissive default: a
    Python int passed to an un-declared parameter is marshalled as a 32-bit C
    ``int``, which TRUNCATES a 64-bit pointer and segfaults the process. That is
    not a theoretical concern — it produced a real ``EXIT=139`` during
    prototyping, and a segfault inside ctypes cannot be caught, logged or
    retried.

    ``restype`` uses the :data:`_VOID` sentinel for void-returning functions so
    "returns nothing" is distinguishable from "row is incomplete".
    """
    if argtypes is None:
        raise ComputerUseUnsupported(
            f"computer-use FFI table is invalid: {symbol} has no argtypes. Every "
            "bound symbol MUST declare argtypes — an undeclared pointer argument "
            "is marshalled as a 32-bit int and segfaults the process."
        )
    fn = getattr(lib, symbol)
    fn.restype = None if restype is _VOID else restype
    fn.argtypes = list(argtypes)


def _load_library(key: str) -> Any:
    """Load one framework by key, raising :class:`ComputerUseUnsupported`.

    ``find_library`` is called here — inside a function — and never at module
    scope: at module scope it would run on the Linux CI fleet at import time and
    break collection of every test that transitively imports this package.
    """
    name = _FRAMEWORK_NAMES[key]
    path = ctypes.util.find_library(name)
    if path is None and key == LIB_PROC:
        # libproc is always present on macOS but ``find_library`` can miss it in
        # a stripped environment; the absolute path is the documented location.
        path = _LIBPROC_FALLBACK_PATH
    if path is None:
        raise ComputerUseUnsupported(f"macOS framework {name} could not be located")
    try:
        return ctypes.CDLL(path)
    except OSError as exc:
        raise ComputerUseUnsupported(f"macOS framework {name} could not be loaded: {exc}") from exc


def _frameworks() -> Libs:
    """Load + bind the five frameworks once, then return the cached bundle.

    NOT at module scope. A module-level ``CDLL`` would raise ``OSError`` on a
    non-macOS runner during import and take down test collection for the whole
    package; here the failure is a typed :class:`ComputerUseUnsupported` that the
    backend seam converts into a refusal.
    """
    global _libs, _type_ids
    if _libs is not None:
        return _libs
    with _init_lock:
        if _libs is not None:  # pragma: no cover - double-checked under the lock
            return _libs
        if not platform_compat.IS_MACOS:
            raise ComputerUseUnsupported(
                "the macOS computer-use driver requires macOS (ApplicationServices "
                "and CoreGraphics do not exist on this platform)"
            )
        handles = {key: _load_library(key) for key in _FRAMEWORK_NAMES}
        # ONE bind pass over the whole table before anything is callable, so a
        # missing symbol or a missing argtypes row fails here rather than at the
        # first call from deep inside a tree walk.
        for lib_key, symbol, restype, argtypes in _FN_SPECS:
            _bind(handles[lib_key], symbol, restype, argtypes)
        for lib_key, symbol, restype, argtypes in _OPTIONAL_FN_SPECS:
            try:
                _bind(handles[lib_key], symbol, restype, argtypes)
            except AttributeError:
                # Absent on this macOS: the caller has a documented fallback.
                logger.debug("optional computer-use symbol %s is unavailable", symbol)
        libs = Libs(
            cf=handles[LIB_CF],
            ax=handles[LIB_AX],
            cg=handles[LIB_CG],
            io=handles[LIB_IO],
            proc=handles[LIB_PROC],
        )
        _type_ids = TypeIds(
            string=int(libs.cf.CFStringGetTypeID()),
            array=int(libs.cf.CFArrayGetTypeID()),
            dictionary=int(libs.cf.CFDictionaryGetTypeID()),
            number=int(libs.cf.CFNumberGetTypeID()),
            boolean=int(libs.cf.CFBooleanGetTypeID()),
            ax_element=int(libs.ax.AXUIElementGetTypeID()),
            ax_value=int(libs.ax.AXValueGetTypeID()),
        )
        _libs = libs
        return libs


def frameworks() -> Libs:
    """Public accessor for the bound framework bundle (see :func:`_frameworks`)."""
    return _frameworks()


def type_ids() -> TypeIds:
    """Cached ``CFTypeID`` values; loads the frameworks on first use."""
    _frameworks()
    assert _type_ids is not None  # established by _frameworks()
    return _type_ids


def reset_frameworks() -> None:
    """Drop the cached handles, type ids and event source.

    For the swap seam and for tests that install a fake ``CDLL``: without this a
    test's fake would never be bound because the real handles are already cached.
    Releases the event source rather than leaking it.
    """
    global _libs, _type_ids, _event_source
    with _init_lock:
        if _event_source is not None and _libs is not None:
            try:
                _libs.cf.CFRelease(_event_source)
            except Exception:
                logger.debug("event source release failed", exc_info=True)
        _event_source = None
        _libs = None
        _type_ids = None


def available() -> bool:
    """True when the frameworks load here. Never raises."""
    try:
        _frameworks()
        return True
    except Exception:
        return False


# ── CoreFoundation helpers ──


@contextmanager
def cf_string(text: str) -> Iterator[c_void_p]:
    """Create a CFString for *text* and ALWAYS ``CFRelease`` it on exit.

    A context manager rather than a plain factory because a single tree walk
    creates thousands of these (one per attribute name per node): leaking them is
    a genuine RSS bug the session watchdog would eventually recycle the process
    over, and a bare ``try/finally`` at every call site would be forgotten once.

    Yields a NULL ``c_void_p`` when creation fails, which every caller treats as
    "attribute unavailable" — CF calls tolerate NULL, so a failed allocation
    degrades to a missing value rather than a crash.
    """
    libs = _frameworks()
    ref = c_void_p(
        libs.cf.CFStringCreateWithCString(None, text.encode("utf-8"), K_CF_STRING_ENCODING_UTF8)
    )
    try:
        yield ref
    finally:
        if ref:
            libs.cf.CFRelease(ref)


def cf_string_value(ref: Any) -> str:
    """Decode a CFString into ``str``, or ``""``.

    Sizes the buffer from ``CFStringGetLength`` * 4 + 1: the length is in UTF-16
    code units and a code unit can expand to at most 4 UTF-8 bytes (surrogate
    pairs count as two units, so this is a safe over-estimate). Undersizing makes
    ``CFStringGetCString`` fail, which would silently drop the value.

    **The type check is a crash guard, not a nicety, and it lives HERE rather than
    at each call site.** ``CFStringGetLength`` on a non-CFString raises an ObjC
    ``NSInvalidArgumentException`` (verified on darwin-arm64:
    ``-[__NSCFNumber length]: unrecognized selector`` -> ``libc++abi: terminating``,
    exit 134). ctypes cannot translate an ObjC exception into a Python one, so it
    is an UNCATCHABLE process abort — ``macos_driver._guarded`` cannot stop it and
    the whole gateway dies with every session on it. The contents of an AX array are
    arbitrary app-controlled data (``AXActionNames`` really does carry odd payloads;
    see ``_sanitize_action``), so a caller that forgets the check is a
    remote-crash primitive. Centralising it means no future reader can reintroduce
    the hazard; the redundant ``cf_is`` checks at the typed readers stay as cheap
    short-circuits.
    """
    if not ref:
        return ""
    libs = _frameworks()
    if not cf_is(ref, type_ids().string):
        return ""
    size = int(libs.cf.CFStringGetLength(ref)) * 4 + 1
    if size <= 1:
        return ""
    buf = ctypes.create_string_buffer(size)
    if not libs.cf.CFStringGetCString(ref, buf, size, K_CF_STRING_ENCODING_UTF8):
        return ""
    return buf.value.decode("utf-8", "replace")


def cf_is(ref: Any, type_id: int) -> bool:
    """True when *ref* is non-NULL and its ``CFTypeID`` equals *type_id*.

    Called before EVERY use of an AX-returned value. AX attributes are
    polymorphic (``AXValue`` can be a string, a number, an ``AXValue`` struct or
    an element) and handing a CFNumber to ``CFStringGetCString`` is a segfault,
    not a ``TypeError``.
    """
    if not ref:
        return False
    return int(_frameworks().cf.CFGetTypeID(ref)) == type_id


def cf_array_items(ref: Any, *, limit: int = 0) -> list[Any]:
    """Return a CFArray's elements as a list, or ``[]`` for a non-array.

    **The count is read first and never exceeded.** Indexing a CFArray past its
    end raises an ObjC ``NSRangeException``, which ctypes cannot translate: the
    process aborts with ``libc++abi: terminating due to uncaught exception``
    (reproduced live, ``EXIT=134``, on an empty array). Since a caller cannot
    recover from that, no caller is allowed to index an array directly.

    *limit* caps how many entries are materialised, so a pathological child list
    cannot blow memory before the walk's own node budget notices.
    """
    if not cf_is(ref, type_ids().array):
        return []
    libs = _frameworks()
    count = int(libs.cf.CFArrayGetCount(ref))
    if count <= 0:
        return []
    if limit > 0:
        count = min(count, limit)
    return [libs.cf.CFArrayGetValueAtIndex(ref, i) for i in range(count)]


def cf_number_int(ref: Any) -> "int | None":
    """Read a CFNumber as a 32-bit int, or ``None`` for a non-number."""
    if not cf_is(ref, type_ids().number):
        return None
    out = c_int32()
    if not _frameworks().cf.CFNumberGetValue(ref, K_CF_NUMBER_SINT32, ctypes.byref(out)):
        return None
    return int(out.value)


def cf_number_double(ref: Any) -> "float | None":
    """Read a CFNumber as a double, or ``None`` for a non-number.

    Separate from :func:`cf_number_int` because the window-list ``kCGWindowBounds``
    components are CGFloats: reading them through the SInt32 path would truncate a
    fractional origin (Retina windows routinely sit on half-pixel boundaries) and
    could place a rect edge a pixel away from where it really is — which matters
    when the rect is being used to decide whether a click is inside an authorized
    application's window.
    """
    if not cf_is(ref, type_ids().number):
        return None
    out = c_double()
    if not _frameworks().cf.CFNumberGetValue(ref, K_CF_NUMBER_DOUBLE, ctypes.byref(out)):
        return None
    return float(out.value)


def cf_bool_value(ref: Any) -> "bool | None":
    """Read a CFBoolean, or ``None`` for a non-boolean."""
    if not cf_is(ref, type_ids().boolean):
        return None
    return bool(_frameworks().cf.CFBooleanGetValue(ref))


def cf_true() -> c_void_p:
    """``kCFBooleanTrue``.

    Read through ``c_void_p.in_dll``: the symbol IS a pointer to the shared
    singleton, so ``.value`` is the right read here — unlike the dictionary
    callback STRUCTS, which need :func:`_cf_type_dict`'s ``addressof``.
    """
    return c_void_p.in_dll(_frameworks().cf, "kCFBooleanTrue")


def _cf_type_dict() -> c_void_p:
    """Create an empty mutable CFDictionary with the standard CFType callbacks.

    ``kCFTypeDictionaryKeyCallBacks`` is a ``CFDictionaryKeyCallBacks`` STRUCT,
    not a pointer to one. ``c_void_p.in_dll(...).value`` therefore reads the
    struct's first word — verified ``None`` — and passing that means "no
    callbacks": CF would neither retain the keys/values we insert nor release
    them, so a CFNumber we release becomes a dangling entry. ``addressof`` on the
    ``in_dll`` view yields the address of the framework's own static storage,
    which is what the API actually wants and stays valid for the process
    lifetime.
    """
    libs = _frameworks()
    key_cb = c_void_p.in_dll(libs.cf, "kCFTypeDictionaryKeyCallBacks")
    value_cb = c_void_p.in_dll(libs.cf, "kCFTypeDictionaryValueCallBacks")
    return c_void_p(
        libs.cf.CFDictionaryCreateMutable(
            None, 0, ctypes.addressof(key_cb), ctypes.addressof(value_cb)
        )
    )


# ── Accessibility helpers ──


def ax_app_element(pid: int) -> c_void_p:
    """Create the application-level ``AXUIElement`` for *pid*. **CALLER OWNS it.**

    ``AXUIElementCreateApplication`` is a Create-Rule call (+1, verified retain
    count 1), so the caller must ``CFRelease``. Prefer :func:`ax_application`,
    which does it.

    The messaging timeout is set here — immediately after creation and before any
    attribute read — because that is the ONLY place it can be guaranteed. Every
    subsequent AX call inherits it, and without it a hung target app parks the
    calling thread with no interruption path (ctypes releases the GIL for the
    duration of the C call, so not even a signal handler runs).
    """
    libs = _frameworks()
    elem = c_void_p(libs.ax.AXUIElementCreateApplication(int(pid)))
    if elem:
        libs.ax.AXUIElementSetMessagingTimeout(elem, c_float(AX_MESSAGING_TIMEOUT_SECS))
    return elem


@contextmanager
def ax_application(pid: int) -> Iterator[c_void_p]:
    """:func:`ax_app_element` with the mandatory release.

    Measured leak without it: 1.4MB per 30,000 creations. One per snapshot is not
    dramatic on its own, but a long-lived sidecar taking a snapshot every turn
    accumulates it forever, and the context-manager form makes the obligation
    impossible to forget.
    """
    libs = _frameworks()
    elem = ax_app_element(pid)
    try:
        yield elem
    finally:
        if elem:
            libs.cf.CFRelease(elem)


def ax_attr(elem: Any, name: str) -> "tuple[Any, int]":
    """Read one AX attribute: ``(value_or_None, ax_error)``. **CALLER OWNS the value.**

    ``AXUIElementCopyAttributeValue`` follows CoreFoundation's **Create Rule**: the
    name contains "Copy", so the returned reference is +1 and the caller MUST
    ``CFRelease`` it. Verified: the returned CFString had a retain count of 1, and
    30,000 reads without a release grew RSS by 9.4MB while the same 30,000 with a
    release grew it by 0.0MB.

    This is a genuine leak, not a theoretical one — a single 1,200-node walk
    performs ~5,000 of these reads, which measured at ~400KB of permanent growth
    per walk before the fix. A long session would have been recycled by the
    watchdog on RSS.

    Prefer :func:`ax_owned_attr` (a context manager that releases) or the typed
    helpers built on it. This raw form exists for the two callers that need the
    reference beyond one expression; each releases explicitly.

    Returns the raw CF reference WITHOUT interpreting it — callers type-check via
    :func:`cf_is` before use. A non-zero error yields ``(None, err)`` and the
    out-parameter is never dereferenced: on failure AX leaves it untouched, so
    reading it would dereference uninitialised stack memory.
    """
    if not elem:
        return None, AX_ERR_CANNOT_COMPLETE
    libs = _frameworks()
    out = c_void_p()
    with cf_string(name) as key:
        if not key:
            return None, AX_ERR_CANNOT_COMPLETE
        err = int(libs.ax.AXUIElementCopyAttributeValue(elem, key, ctypes.byref(out)))
    if err != AX_ERR_SUCCESS:
        return None, err
    return out.value, err


@contextmanager
def ax_owned_attr(elem: Any, name: str) -> Iterator["tuple[Any, int]"]:
    """:func:`ax_attr` with the mandatory ``CFRelease`` — the form to use.

    A context manager for the same reason :func:`cf_string` is one: the Create Rule
    makes the release obligatory, a tree walk performs thousands of these reads, and
    a ``try/finally`` at every call site is a rule that gets forgotten exactly once
    and then leaks ~400KB per walk (measured, before this existed).

    Note what is NOT safe to do with the yielded value: any CF object derived from
    it (an array's elements, a nested element) is only valid while it is alive, so a
    caller that needs to keep such a derivative must ``CFRetain`` it — which is what
    :func:`ax_retained_elements` does.
    """
    value, err = ax_attr(elem, name)
    try:
        yield value, err
    finally:
        if value:
            _frameworks().cf.CFRelease(value)


def ax_str(elem: Any, name: str) -> str:
    """Read an AX attribute as ``str``, or ``""``.

    Returns ``""`` for a missing attribute AND for a present-but-not-a-string
    one. Both are ordinary: a window answers ``-25205`` for ``AXValue``, and
    ``AXValue`` on a slider is a CFNumber.

    The value is decoded into a Python ``str`` and released before returning, so no
    CF reference escapes. This is THE hot path of a tree walk (~5 calls per node).
    """
    with ax_owned_attr(elem, name) as (value, err):
        if err != AX_ERR_SUCCESS or value is None:
            return ""
        if not cf_is(value, type_ids().string):
            return ""
        return cf_string_value(value)


def ax_bool(elem: Any, name: str, *, default: bool = True) -> bool:
    """Read an AX attribute as ``bool``, falling back to *default*.

    *default* matters: ``AXEnabled`` is unsupported on many elements (a window
    answers ``-25205``), and rendering every such node as "(disabled)" would be
    both wrong and noisy. Absent means unknown, and unknown reads as enabled.
    """
    with ax_owned_attr(elem, name) as (value, err):
        if err != AX_ERR_SUCCESS or value is None:
            return default
        parsed = cf_bool_value(value)
        return default if parsed is None else parsed


def ax_bool_opt(elem: Any, name: str) -> "bool | None":
    """Read a boolean AX attribute, distinguishing ABSENT (``None``) from ``False``.

    :func:`ax_bool` collapses "unsupported" into a caller-chosen default, which is
    right for ``AXEnabled`` (absent means enabled) and wrong for the trait
    attributes: ``AXSelected`` absent means "this element has no notion of
    selection", while ``AXSelected=false`` means "selectable, not selected". A
    renderer that printed "(not selected)" for every node lacking the attribute
    would bury the tree in noise, so the tri-state has to survive the read.
    """
    with ax_owned_attr(elem, name) as (value, err):
        if err != AX_ERR_SUCCESS or value is None:
            return None
        return cf_bool_value(value)


def ax_is_settable(elem: Any, name: str) -> bool:
    """True when *name* can be WRITTEN on *elem* (``AXUIElementIsAttributeSettable``).

    The only reliable "is this input editable?" signal. ``AXEnabled`` is about
    interactivity, not writability: a read-only text field (a disabled form input,
    a log pane, a computed cell) reports ``AXEnabled=true`` and a readable
    ``AXValue``, and typing into it silently does nothing. Surfacing settability
    lets the model pick the editable field on the first try instead of discovering
    the read-only one by failing against it.

    Fails CLOSED (``False``) on any error: an unknowable attribute is treated as
    not settable, so the trait is never advertised on speculation.
    """
    if not elem:
        return False
    libs = _frameworks()
    out = c_bool(False)
    with cf_string(name) as cf_name:
        if not cf_name:
            return False
        err = int(libs.ax.AXUIElementIsAttributeSettable(elem, cf_name, ctypes.byref(out)))
    return err == AX_ERR_SUCCESS and bool(out.value)


def ax_parent(elem: Any) -> Any:
    """The element's parent as a RETAINED reference, or ``None``.

    +1 like :func:`ax_retained_elements`, and for the same reason: the value comes
    from a ``Copy`` call whose result the caller must own, and ``ax_owned_attr``
    releases it on scope exit — using it afterwards without the retain is a
    use-after-free in another process's accessibility client.

    ``None`` for the root and for any element whose app does not answer
    ``AXParent`` (ordinary), so callers treat it as "stop walking up".
    """
    if not elem:
        return None
    with ax_owned_attr(elem, AX_PARENT) as (value, err):
        if err != AX_ERR_SUCCESS or value is None:
            return None
        if not cf_is(value, type_ids().ax_element):
            return None
        return retain(value)


def same_element(a: Any, b: Any) -> bool:
    """True when *a* and *b* name the SAME accessibility element.

    ``CFEqual``, not ``==``. ``AXFocusedUIElement`` returns a freshly created
    reference, so the pointer it yields differs from the one the same node got
    while being walked from ``AXChildren`` even though both address one UI element.
    Comparing addresses would therefore report "not focused" for every element —
    the focus marker would simply never appear, which is a silent no-op rather
    than a visible failure.

    Both NULL is ``False``, not ``True``: "no element" is not identity with "no
    element", and the one caller (the focus marker) must not light up every record
    when nothing is focused.
    """
    if not a or not b:
        return False
    return bool(_frameworks().cf.CFEqual(a, b))


def ax_frame(elem: Any) -> "tuple[float, float, float, float] | None":
    """``(x, y, width, height)`` of *elem* in TOP-LEFT SCREEN coordinates, or ``None``.

    ``AXPosition`` and ``AXSize`` come back as ``AXValue`` boxes rather than plain
    numbers, so each is type-checked (``AXValueGetType``) before it is unboxed into
    the matching struct — reading a ``CGPoint`` box into a ``CGSize`` buffer would
    transpose ``y`` into ``width`` and produce a plausible-looking wrong rect,
    which is the worst failure mode for a coordinate a click is aimed at.

    Returns ``None`` unless BOTH reads succeed. A partial frame is worse than no
    frame: the caller would have to invent the missing half, and every consumer
    (rendering, hit-testing, the click ladder) would then be reasoning about a
    rectangle that does not exist. Many elements legitimately have no geometry
    (a menu bar item of a background app, an off-screen row), so ``None`` is an
    ordinary answer and not an error.

    Top-left screen coordinates: this is the AX convention AND the convention this
    whole package uses, so no flip happens here. The window-relative conversion is
    the caller's job, because only the caller knows which window the element
    belongs to.
    """
    if not elem:
        return None
    point = _ax_value_point(elem, AX_POSITION)
    if point is None:
        return None
    size = _ax_value_size(elem, AX_SIZE)
    if size is None:
        return None
    return (point[0], point[1], size[0], size[1])


def _ax_value_point(elem: Any, name: str) -> "tuple[float, float] | None":
    """Unbox a ``CGPoint``-typed ``AXValue`` attribute, or ``None``."""
    out = CGPoint()
    if not _unbox_ax_value(elem, name, K_AX_VALUE_CG_POINT, out):
        return None
    return (float(out.x), float(out.y))


def _ax_value_size(elem: Any, name: str) -> "tuple[float, float] | None":
    """Unbox a ``CGSize``-typed ``AXValue`` attribute, or ``None``."""
    out = CGSize()
    if not _unbox_ax_value(elem, name, K_AX_VALUE_CG_SIZE, out):
        return None
    return (float(out.width), float(out.height))


def _unbox_ax_value(elem: Any, name: str, want_type: int, out: Any) -> bool:
    """Read *name* off *elem* and unbox it into *out*, iff it really is *want_type*.

    Three checks, none of them optional:

    * the attribute read succeeded and returned something;
    * the something is an ``AXValue`` (``cf_is`` — an app may answer with a
      CFNumber or a string, and ``AXValueGetType`` on a non-``AXValue`` is
      undefined behaviour, not an error return);
    * the box's own type matches *want_type*, so the bytes land in the right
      struct fields.

    ``AXValueGetValue`` also returns false when the type disagrees, but relying on
    that alone would mean calling it on a pointer that may not be an ``AXValue``
    at all — the same class of crash ``cf_string_value``'s type guard exists to
    prevent.
    """
    with ax_owned_attr(elem, name) as (value, err):
        if err != AX_ERR_SUCCESS or value is None:
            return False
        if not cf_is(value, type_ids().ax_value):
            return False
        libs = _frameworks()
        if int(libs.ax.AXValueGetType(value)) != want_type:
            return False
        return bool(libs.ax.AXValueGetValue(value, want_type, ctypes.byref(out)))


def ax_children(elem: Any, *, limit: int = 0) -> list[Any]:
    """The element's children as RETAINED references — caller must release.

    See :func:`ax_retained_elements` for why retaining is required rather than
    optional. Every caller passes the result to :func:`release_all` when done; a
    tree walk does so as it pops each node.
    """
    return ax_retained_elements(elem, "AXChildren", limit=limit)


def ax_retained_elements(elem: Any, name: str, *, limit: int = 0) -> list[Any]:
    """Read an element-array attribute (``AXChildren``, ``AXWindows``) as +1 refs.

    Two ownership rules collide here and both have to be honoured:

    * the ARRAY comes from a ``Copy`` call, so we own it and must release it;
    * its ELEMENTS are owned by the array, so they die with it.

    A caller that wants to keep a child after the array is gone must therefore
    ``CFRetain`` it. Verified live: a child read back its ``AXRole`` correctly after
    the parent array was released *because it had been retained* — without the
    retain that is a use-after-free in another process's accessibility client, which
    is a crash rather than an exception.

    So this returns +1 references and the caller MUST call :func:`release_all`.
    Each entry is also type-checked to be an ``AXUIElement``: a malformed app could
    answer with an array of strings, and performing an action on a CFString pointer
    is a segfault.
    """
    libs = _frameworks()
    with ax_owned_attr(elem, name) as (value, err):
        if err != AX_ERR_SUCCESS:
            return []
        element_type = type_ids().ax_element
        return [
            libs.cf.CFRetain(item)
            for item in cf_array_items(value, limit=limit)
            if cf_is(item, element_type)
        ]


def retain(ref: Any) -> Any:
    """``CFRetain(ref)`` and return it — for a reference that must outlive its owner.

    Used when the walk hands one element back to its caller: the element belongs to
    a parent array the walk is about to release, and using it afterwards without a
    retain is a use-after-free in another process's accessibility client (a crash,
    not an exception).
    """
    if not ref:
        return ref
    return _frameworks().cf.CFRetain(ref)


def ax_attr_error(elem: Any, name: str) -> "tuple[bool, int]":
    """``(present, ax_error)`` for an attribute, WITHOUT leaking the value.

    For the caller that only needs to distinguish *why* a read came back empty —
    ``-25204`` ("Electron has not opted in, retry") versus a genuinely absent
    attribute. Releasing here means the diagnostic path cannot become a leak of its
    own.
    """
    with ax_owned_attr(elem, name) as (value, err):
        return bool(value), err


def release_all(refs: "Sequence[Any]") -> None:
    """``CFRelease`` every non-NULL reference in *refs*.

    The counterpart to :func:`ax_retained_elements`. Kept as a named helper so the
    release side of the walk reads as deliberately as the retain side.
    """
    libs = _frameworks()
    for ref in refs:
        if ref:
            libs.cf.CFRelease(ref)


def ax_actions(elem: Any, *, limit: int = 0) -> tuple[str, ...]:
    """The element's advertised action names, SANITIZED.

    Advisory only. A real ``AXScrollArea`` advertised ``AXScrollDownByPage`` and
    then returned ``-25205`` when asked to perform it, so every caller must try
    the action, check the error and fall back.

    Sanitized at this boundary — see :func:`_sanitize_action` — because an action
    name is arbitrary app-controlled text, not the ``AX*`` identifier one expects.
    """
    if not elem:
        return ()
    libs = _frameworks()
    out = c_void_p()
    err = int(libs.ax.AXUIElementCopyActionNames(elem, ctypes.byref(out)))
    if err != AX_ERR_SUCCESS:
        return ()
    try:
        names = [
            _sanitize_action(cf_string_value(item))
            for item in cf_array_items(out.value, limit=limit)
        ]
    finally:
        # ``Copy`` in the name => Create Rule => we own the array. The name strings
        # are decoded into Python above and belong to the array, so releasing it
        # here is the complete cleanup.
        if out.value:
            libs.cf.CFRelease(out.value)
    return tuple(name for name in names if name)


# Bounds for a sanitized action name. Real identifiers are short (``AXPress``,
# ``AXShowMenu``); the cap only bites on the app-generated descriptions below.
MAX_ACTION_NAME_LEN = 48


def _sanitize_action(name: str) -> str:
    """Collapse whitespace in an action name and clip it.

    **A live-data finding, and a real injection vector.** Action names are NOT
    guaranteed to be ``AX*`` identifiers — macOS Notes advertises
    ``'Name:remove pin\\nTarget:0x0\\nSelector:(null)'`` on its table rows, i.e. an
    app-generated description containing EMBEDDED NEWLINES.

    The rendered tree's structure IS its indentation, and the renderer joins action
    names into the element's line. An embedded newline therefore lets app content
    forge additional tree lines — fabricating elements with indices the model would
    then try to address. Values and titles are already collapsed when rendered;
    actions were not, and the safest place to fix it is here at the boundary that
    produces the data, so no downstream renderer has to remember.

    Clipped as well as collapsed: a 200-character action description crowds out
    the rest of the line for no informational gain.
    """
    flat = " ".join(name.split())
    return flat[:MAX_ACTION_NAME_LEN] if len(flat) > MAX_ACTION_NAME_LEN else flat


def ax_perform(elem: Any, action: str) -> int:
    """Perform a named accessibility action. Returns the raw AX error code.

    The RAW code is returned rather than a bool because the callers branch on it:
    ``-25205`` means "advertised but unsupported — use the fallback", while other
    codes are genuine failures whose number belongs in the error message so a
    support thread is diagnosable.
    """
    if not elem:
        return AX_ERR_CANNOT_COMPLETE
    with cf_string(action) as name:
        if not name:
            return AX_ERR_CANNOT_COMPLETE
        return int(_frameworks().ax.AXUIElementPerformAction(elem, name))


def ax_set_str(elem: Any, name: str, value: str) -> int:
    """Set a string-valued AX attribute (the ``set_value`` primitive)."""
    if not elem:
        return AX_ERR_CANNOT_COMPLETE
    with cf_string(name) as key:
        if not key:
            return AX_ERR_CANNOT_COMPLETE
        with cf_string(value) as payload:
            if not payload:
                return AX_ERR_CANNOT_COMPLETE
            return int(_frameworks().ax.AXUIElementSetAttributeValue(elem, key, payload))


def ax_set_true(elem: Any, name: str) -> int:
    """Set a boolean AX attribute to ``kCFBooleanTrue``.

    Used for ``AXManualAccessibility`` (the Electron opt-in) and ``AXFocused``
    (so typed keystrokes land in the addressed field rather than wherever the
    target app last had focus).
    """
    if not elem:
        return AX_ERR_CANNOT_COMPLETE
    with cf_string(name) as key:
        if not key:
            return AX_ERR_CANNOT_COMPLETE
        return int(_frameworks().ax.AXUIElementSetAttributeValue(elem, key, cf_true()))


def ax_is_process_trusted() -> bool:
    """``AXIsProcessTrusted()`` — ADVISORY. See :mod:`permissions`."""
    return bool(_frameworks().ax.AXIsProcessTrusted())


def ax_window_id(window: Any) -> int:
    """The CoreGraphics window id for an AX window element, or ``0``.

    Correlating the two window namespaces is a CORRECTNESS requirement.
    ``AXWindows`` and the CoreGraphics window list are independently ordered:
    verified live on Notes, ``AXWindows[0]`` was an ``AXDialog`` with an empty
    title exposing 3 nodes, while the frontmost CG window was the real 103-node
    document window. Walking one and capturing the other would hand the model a
    tree and a screenshot of DIFFERENT WINDOWS — a silent, confidently-wrong
    observation, which is worse than an error.

    Returns ``0`` when the private symbol is unavailable or the element is not a
    window (AX answers ``-25201`` for the application element, verified). Callers
    fall back to matching on title/``AXMain``.
    """
    libs = _frameworks()
    getter = getattr(libs.ax, "_AXUIElementGetWindow", None)
    if getter is None or not window:
        return 0
    out = c_uint32(0)
    if int(getter(window, ctypes.byref(out))) != AX_ERR_SUCCESS:
        return 0
    return int(out.value)


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


# ── CoreGraphics: event synthesis ──


def event_source() -> c_void_p:
    """The cached PRIVATE ``CGEventSource``.

    ``kCGEventSourceStatePrivate`` is load-bearing, not a preference. Events
    built from the default (HID) source inherit the user's LIVE modifier state:
    asking a live prototype to type ``abc`` produced ``' I Abc'`` because the
    user happened to be holding keys. A private source starts from a clean
    modifier state that only :func:`post_key`'s explicit flags populate.

    Cached process-wide: creating one per keystroke would be both wasteful and a
    leak risk in the middle of a loop.
    """
    global _event_source
    libs = _frameworks()
    if _event_source is not None:
        return _event_source
    with _init_lock:
        if _event_source is None:
            _event_source = c_void_p(libs.cg.CGEventSourceCreate(K_CG_EVENT_SOURCE_STATE_PRIVATE))
        return _event_source


def post_key(pid: int, keycode: int, flags: int = 0) -> None:
    """Post one key-down/key-up pair for *keycode* to *pid*.

    Three rules, all verified load-bearing and all violated by the obvious
    implementation:

    1. the event source is PRIVATE (:func:`event_source`);
    2. ``CGEventSetFlags`` is called on EVERY event **including when flags is
       0** — that explicit zero is what clears the inherited modifier state, so
       skipping it "because there are no modifiers" reintroduces the bug;
    3. delivery is ``CGEventPostToPid``, never ``CGEventPost``. The latter goes
       to the global event tap, i.e. to whatever window the user is actually
       typing in — a keystroke aimed at a background app would land in their
       editor.
    """
    libs = _frameworks()
    source = event_source()
    for is_down in (True, False):
        event = c_void_p(
            libs.cg.CGEventCreateKeyboardEvent(source, c_uint16(int(keycode)), c_bool(is_down))
        )
        if not event:
            continue
        try:
            libs.cg.CGEventSetFlags(event, c_uint64(int(flags)))
            libs.cg.CGEventPostToPid(int(pid), event)
        finally:
            libs.cf.CFRelease(event)


def post_text(pid: int, text: str) -> None:
    """Type *text* into *pid* as unicode key events.

    Uses ``CGEventKeyboardSetUnicodeString`` with a keycode of 0 rather than a
    per-character keycode lookup: that makes the path layout-independent and able
    to emit characters the US layout cannot reach in one keystroke (verified: a
    round-trip of ``"Hello, KiroCrew! aA$ 123"`` came back byte-identical). Flags
    are still set explicitly to zero on every event — the unicode payload
    replaces the *keystroke*, not the modifier hygiene.

    Encoded per character as UTF-16LE so an astral character is delivered as its
    full surrogate pair in one event rather than as two lone surrogates.

    **The whole string is encoded BEFORE the first keystroke is posted.** Encoding
    inside the loop meant an unencodable character — a lone surrogate, which
    ``str`` permits and UTF-16 cannot represent — raised ``UnicodeEncodeError``
    *after* every preceding character had already been typed into a live
    application. The caller then saw a failure while the target held a partial
    string, and a model told "typing failed" would retry and double the prefix.
    Encoding up front makes the call all-or-nothing: it either types the whole
    string or refuses having typed nothing, which is the contract the driver's
    failed ``DriverResult`` claims. ``surrogatepass`` is deliberately NOT used —
    delivering a lone surrogate to AppKit is not a meaningful keystroke.
    """
    libs = _frameworks()
    try:
        encoded = [(char, char.encode("utf-16-le")) for char in text]
    except UnicodeEncodeError as exc:
        raise ComputerUseUnsupported(
            f"the text contains a character that cannot be typed ({exc.reason}); "
            "nothing was sent"
        ) from exc
    source = event_source()
    for _char, units in encoded:
        count = len(units) // 2
        if count <= 0:
            continue
        buf = (c_uint16 * count).from_buffer_copy(units)
        for is_down in (True, False):
            event = c_void_p(
                libs.cg.CGEventCreateKeyboardEvent(source, c_uint16(0), c_bool(is_down))
            )
            if not event:
                continue
            try:
                libs.cg.CGEventSetFlags(event, c_uint64(0))
                libs.cg.CGEventKeyboardSetUnicodeString(event, c_long(count), buf)
                libs.cg.CGEventPostToPid(int(pid), event)
            finally:
                libs.cf.CFRelease(event)


def post_scroll(pid: int, delta_y: int, delta_x: int) -> None:
    """Post one scroll-wheel event to *pid*, in LINE units.

    **Never uses the multi-axis constructor.**
    ``CGEventCreateScrollWheelEvent`` is variadic, and ctypes marshals a variadic
    tail incorrectly on arm64: declaring the 5-argument form and asking for
    ``(axis1=-3, axis2=0)`` produced ``axis2=30416`` — an arbitrary sideways
    scroll of a live window. So the event is built with ``wheelCount=1`` through
    the FIXED prototype and both axes are then written with the non-variadic
    ``CGEventSetIntegerValueField``, which was verified exact for every input.
    """
    libs = _frameworks()
    source = event_source()
    event = c_void_p(
        libs.cg.CGEventCreateScrollWheelEvent(source, K_CG_SCROLL_EVENT_UNIT_LINE, 1, c_int32(0))
    )
    if not event:
        return
    try:
        libs.cg.CGEventSetIntegerValueField(event, K_CG_SCROLL_DELTA_AXIS_1, c_int64(int(delta_y)))
        libs.cg.CGEventSetIntegerValueField(event, K_CG_SCROLL_DELTA_AXIS_2, c_int64(int(delta_x)))
        libs.cg.CGEventSetFlags(event, c_uint64(0))
        libs.cg.CGEventPostToPid(int(pid), event)
    finally:
        libs.cf.CFRelease(event)


def mouse_button_codes(button: str) -> tuple[int, int, int, int]:
    """``(button_number, down_type, up_type, dragged_type)`` for *button*.

    Raises :class:`ComputerUseUnsupported` for an unknown name rather than
    defaulting to left: silently turning an unrecognised ``mouse_button`` into a
    left click would send a DIFFERENT gesture than the caller asked for into a live
    application, which is the same class of defect as
    :func:`~kiro_crew.computer_use.keymap.parse_key` refusing an unknown modifier.
    The dispatch layer validates the enum first, so this is the belt for the
    in-process entry point.
    """
    codes = MOUSE_EVENT_TYPES.get(button)
    if codes is None:
        raise ComputerUseUnsupported(f"unknown mouse button {button!r}")
    return codes


def _mouse_event(
    libs: Libs,
    event_type: int,
    x: float,
    y: float,
    button_number: int,
    *,
    click_state: int = 0,
) -> "c_void_p | None":
    """Create one mouse event, or ``None``. **Caller must ``CFRelease``.**

    Built from the PRIVATE event source for the same reason every keyboard event
    is (:func:`event_source`): the default HID source inherits the user's live
    modifier state, so a synthesized click would arrive as cmd-click if they
    happened to be holding a key. Flags are set to an explicit zero here too —
    the modifier hygiene is about the SOURCE of the event, not about whether it is
    a keystroke.

    ``click_state`` is written only when non-zero; a double click is a pair whose
    ``kCGMouseEventClickState`` is 2, NOT two separate pairs (AppKit reads
    ``NSEvent.clickCount`` from that field).
    """
    event = c_void_p(
        libs.cg.CGEventCreateMouseEvent(
            event_source(),
            c_uint32(int(event_type)),
            CGPoint(float(x), float(y)),
            c_uint32(int(button_number)),
        )
    )
    if not event:
        return None
    libs.cg.CGEventSetFlags(event, c_uint64(0))
    if click_state:
        libs.cg.CGEventSetIntegerValueField(
            event, K_CG_MOUSE_EVENT_CLICK_STATE, c_int64(int(click_state))
        )
    return event


def post_mouse_click(
    pid: int,
    x: float,
    y: float,
    *,
    button: str = MOUSE_BUTTON_LEFT,
    count: int = 1,
) -> None:
    """Post *count* clicks at ``(x, y)`` to *pid*. **The pointer does NOT move.**

    This is the ``app_post`` click method: the event carries a location and is
    delivered with ``CGEventPostToPid``, so the target application sees a click at
    that point while the operator's physical cursor stays exactly where they left
    it (verified live — the prototype's ``mouse_pos()`` was identical before and
    after). That property is the whole reason this method exists and is the
    default for a coordinate click; :func:`post_mouse_global` is the opt-in
    alternative that does move it.

    ``count`` is expressed as the events' CLICK STATE, not as repeated pairs — see
    :func:`_mouse_event`. Each pair is still posted so an app that tracks
    down/up transitions sees them, with a sub-double-click-interval pause between
    them so the OS recognizes the repetition.
    """
    libs = _frameworks()
    button_number, down_type, up_type, _dragged = mouse_button_codes(button)
    for click_index in range(max(1, int(count))):
        click_state = click_index + 1
        for event_type in (down_type, up_type):
            event = _mouse_event(libs, event_type, x, y, button_number, click_state=click_state)
            if event is None:
                continue
            try:
                libs.cg.CGEventPostToPid(int(pid), event)
            finally:
                libs.cf.CFRelease(event)
            time.sleep(CLICK_PAIR_DELAY_SECS)


def post_mouse_drag(
    pid: int,
    start: tuple[float, float],
    end: tuple[float, float],
    *,
    button: str = MOUSE_BUTTON_LEFT,
    steps: int = DRAG_STEPS,
) -> None:
    """Drag from *start* to *end* inside *pid*. **The pointer does NOT move.**

    Down at the start, :data:`DRAG_STEPS` interpolated ``MouseDragged`` events, up
    at the end — all app-targeted. The intermediate events are NOT padding: a bare
    down/up pair is not a drag to most applications, because the gesture is
    recognized from the motion between the endpoints (a canvas records two
    isolated points and a text view selects nothing). Verified against TextEdit,
    which needed the interpolation before it registered a selection.

    The per-step sleep matters for the same reason: identical timestamps let
    AppKit's recognizer coalesce the whole sequence into one motion.
    """
    libs = _frameworks()
    button_number, down_type, up_type, dragged_type = mouse_button_codes(button)
    x0, y0 = float(start[0]), float(start[1])
    x1, y1 = float(end[0]), float(end[1])
    total = max(1, int(steps))
    plan: list[tuple[int, float, float]] = [(down_type, x0, y0)]
    for index in range(1, total):
        ratio = index / total
        plan.append((dragged_type, x0 + (x1 - x0) * ratio, y0 + (y1 - y0) * ratio))
    plan.append((up_type, x1, y1))
    for event_type, px, py in plan:
        event = _mouse_event(libs, event_type, px, py, button_number)
        if event is None:
            continue
        try:
            libs.cg.CGEventPostToPid(int(pid), event)
        finally:
            libs.cf.CFRelease(event)
        time.sleep(DRAG_STEP_DELAY_SECS)


def post_mouse_global(
    x: float,
    y: float,
    *,
    button: str = MOUSE_BUTTON_LEFT,
    count: int = 1,
) -> None:
    """MOVE THE OPERATOR'S REAL POINTER to ``(x, y)`` and click it there.

    **The one function in this module that takes over the physical mouse**, and
    the only caller of ``CGWarpMouseCursorPosition`` and ``CGEventPost``. Every
    other input path is app-scoped and leaves the cursor untouched.

    Reachable only through ``click_method: "global"``, which the model must NAME —
    ``policy.resolve_click_method`` never resolves ``auto`` onto it. Do not add a
    second caller: that resolution happens at the dispatch chokepoint, so a call
    site that bypasses the chokepoint bypasses the naming requirement too.

    Why it exists at all, given the app-scoped path works: some UI is only
    reachable by a real physical click — a Dock item, a menu-bar extra, a window
    belonging to a process whose event queue refuses posted events, a Space
    switcher. The warp is what makes the subsequent global click land where the
    caller asked, because a global event is delivered to whatever is under the
    cursor rather than to a pid.

    ``CGEventPost`` to ``kCGHIDEventTap`` rather than ``CGEventPostToPid``: there
    is no per-pid form of "simulate a physical mouse", and delivering to a pid
    would defeat the point (the app-scoped path already does that, better).
    """
    libs = _frameworks()
    button_number, down_type, up_type, _dragged = mouse_button_codes(button)
    # Warp FIRST: a global click is delivered to whatever sits under the cursor, so
    # posting before the warp would click wherever the operator happened to leave
    # it. The return code is advisory — a refused warp still leaves a coherent
    # click at the (unchanged) cursor, and refusing the whole action would be worse
    # than a click the caller can verify from the refreshed tree.
    warped = int(libs.cg.CGWarpMouseCursorPosition(CGPoint(float(x), float(y))))
    if warped != 0:
        logger.debug("CGWarpMouseCursorPosition returned %s for (%s, %s)", warped, x, y)
    for click_index in range(max(1, int(count))):
        click_state = click_index + 1
        for event_type in (down_type, up_type):
            event = _mouse_event(libs, event_type, x, y, button_number, click_state=click_state)
            if event is None:
                continue
            try:
                libs.cg.CGEventPost(c_uint32(K_CG_HID_EVENT_TAP), event)
            finally:
                libs.cf.CFRelease(event)
            time.sleep(CLICK_PAIR_DELAY_SECS)


def post_mouse_drag_global(
    start: tuple[float, float],
    end: tuple[float, float],
    *,
    button: str = MOUSE_BUTTON_LEFT,
    steps: int = DRAG_STEPS,
) -> None:
    """MOVE THE OPERATOR'S REAL POINTER along a drag from *start* to *end*.

    The pointer-moving counterpart of :func:`post_mouse_drag`, subject to the
    identical two permits (see :func:`post_mouse_global`). The cursor is warped
    before EVERY event, not only the first: a global drag is only coherent if the
    physical cursor tracks the synthesized motion, and warping once would leave
    the OS delivering the intermediate events to whatever sat under the start
    point.
    """
    libs = _frameworks()
    button_number, down_type, up_type, dragged_type = mouse_button_codes(button)
    x0, y0 = float(start[0]), float(start[1])
    x1, y1 = float(end[0]), float(end[1])
    total = max(1, int(steps))
    plan: list[tuple[int, float, float]] = [(down_type, x0, y0)]
    for index in range(1, total):
        ratio = index / total
        plan.append((dragged_type, x0 + (x1 - x0) * ratio, y0 + (y1 - y0) * ratio))
    plan.append((up_type, x1, y1))
    for event_type, px, py in plan:
        libs.cg.CGWarpMouseCursorPosition(CGPoint(px, py))
        event = _mouse_event(libs, event_type, px, py, button_number)
        if event is None:
            continue
        try:
            libs.cg.CGEventPost(c_uint32(K_CG_HID_EVENT_TAP), event)
        finally:
            libs.cf.CFRelease(event)
        time.sleep(DRAG_STEP_DELAY_SECS)


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


__all__ = [
    "AX_ERR_ACTION_UNSUPPORTED",
    "AX_ERR_ATTRIBUTE_UNSUPPORTED",
    "AX_ERR_CANNOT_COMPLETE",
    "AX_ERR_NO_VALUE",
    "AX_ERR_SUCCESS",
    "AX_MANUAL_ACCESSIBILITY",
    "AX_MESSAGING_TIMEOUT_SECS",
    "CGPoint",
    "CGRect",
    "CGSize",
    "CLICK_PAIR_DELAY_SECS",
    "DRAG_STEPS",
    "DRAG_STEP_DELAY_SECS",
    "K_CG_HID_EVENT_TAP",
    "K_CG_MOUSE_EVENT_CLICK_STATE",
    "MOUSE_EVENT_TYPES",
    "ELECTRON_OPT_IN_POLL_SECS",
    "ELECTRON_OPT_IN_WAIT_SECS",
    "CG_WINDOW_LAYER_NORMAL",
    "Libs",
    "TypeIds",
    "WindowInfo",
    "available",
    "ax_actions",
    "ax_app_element",
    "ax_application",
    "ax_attr",
    "ax_attr_error",
    "ax_bool",
    "ax_children",
    "ax_is_process_trusted",
    "ax_owned_attr",
    "ax_perform",
    "ax_retained_elements",
    "ax_set_str",
    "ax_set_true",
    "ax_str",
    "ax_window_id",
    "capture_window_jpeg",
    "cf_array_items",
    "cf_bool_value",
    "cf_is",
    "cf_number_int",
    "cf_string",
    "cf_string_value",
    "cf_true",
    "event_source",
    "executable_path",
    "frameworks",
    "jpeg_dimensions",
    "mouse_button_codes",
    "post_key",
    "post_mouse_click",
    "post_mouse_drag",
    "post_mouse_drag_global",
    "post_mouse_global",
    "post_scroll",
    "post_text",
    "preflight_screen_capture",
    "release_all",
    "reset_frameworks",
    "retain",
    "shots_dir_default",
    "type_ids",
    "window_list",
]
