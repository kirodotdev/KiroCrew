"""The framework binding of :mod:`kiro_crew.computer_use.macos_ffi`.

The library keys and the CoreFoundation, Accessibility, CoreGraphics and ImageIO ABI
constants; the ``CGPoint``/``CGSize``/``CGRect`` structures; the declarative
:data:`_FN_SPECS` tables and the one bind pass (:func:`_bind`, :func:`_frameworks`); and the
process-wide native state built from them: the bound framework handles, the cached
``CFTypeID`` values and the private event source (:func:`event_source`), which
:func:`reset_frameworks` drops together.

:mod:`kiro_crew.computer_use.macos_ffi` stays the import path and the patch surface for
every name defined here. The hazards the comments below cite by number are the ones its
docstring lists.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import threading
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
from typing import Any, Sequence

from kiro_crew import platform_compat
from kiro_crew.computer_use.types import (
    MOUSE_BUTTON_LEFT,
    MOUSE_BUTTON_MIDDLE,
    MOUSE_BUTTON_RIGHT,
    ComputerUseUnsupported,
)

# Logs under the facade's name, which operators and tests filter on.
logger = logging.getLogger("kiro_crew.computer_use.macos_ffi")

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
    # Declaring the variadic tail produced garbage on arm64 (macos_ffi hazard 3).
    (LIB_CG, "CGEventCreateScrollWheelEvent", c_void_p, [c_void_p, c_uint32, c_uint32, c_int32]),
    (LIB_CG, "CGEventSetIntegerValueField", _VOID, [c_void_p, c_uint32, c_int64]),
    (LIB_CG, "CGEventSetFlags", _VOID, [c_void_p, c_uint64]),
    (LIB_CG, "CGEventPostToPid", _VOID, [c_int32, c_void_p]),
    # Where a posted wheel event lands. A new wheel event carries the operator's
    # pointer position, so without this a scroll aimed at a background window goes
    # to whatever the pointer is over and the target app drops it. ``CGPoint``
    # by value, for the same AArch64 reason as CGEventCreateMouseEvent below.
    (LIB_CG, "CGEventSetLocation", _VOID, [c_void_p, CGPoint]),
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
