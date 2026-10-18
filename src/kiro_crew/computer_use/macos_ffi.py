"""The macOS native layer: its import path, and its CoreGraphics event synthesis.

Everything native lives here: CoreFoundation memory discipline, the
Accessibility (AX) attribute/action calls, CoreGraphics window enumeration and
event synthesis, and the in-process ImageIO encode, across this module and its
owners: the framework binding in :mod:`~kiro_crew.computer_use.macos_ffi_bind`, the
CoreFoundation helpers in :mod:`~kiro_crew.computer_use.macos_ffi_cf`, the AX calls in
:mod:`~kiro_crew.computer_use.macos_ffi_ax`, window enumeration and capture in
:mod:`~kiro_crew.computer_use.macos_ffi_window`, and the event synthesis below. Every
other computer-use module talks to macOS through these helpers, read and patched as
``macos_ffi.<name>`` (see :mod:`kiro_crew._facade_mirror`), so the FFI hazards are audited
in one module family instead of scattered across the five modules that use them.

``import ctypes`` is a top-level import statement (AUTOSDE ``top-level-imports``
is about statements, not about *loading* a library), but **no ``CDLL`` or
``find_library`` runs at module scope**. The frameworks load inside
:func:`_frameworks`, cached in a module global, and raise
:class:`ComputerUseUnsupported` off macOS — so this family imports cleanly on the
Linux and Windows CI shards and a test can exercise the binding logic with a fake
``CDLL``.

Six hazards this family exists to contain. Each was reproduced live on a real Mac
(darwin-arm64, Darwin 25.5.0); the failure mode of the first three is a **process
abort**, not a Python exception, so none of them can be handled by a caller.

1. **A missing ``argtypes`` truncates a 64-bit pointer to 32 bits and
   SEGFAULTS.** One omitted ``argtypes`` produced a real ``EXIT=139``. Hence the
   declarative :data:`_FN_SPECS` table and a single bind pass: no function in
   this family can be reached un-bound, and :func:`_bind` RAISES on
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
count, so a stray global post anywhere else in the family fails CI.

Every AX read is also type-checked with ``CFGetTypeID`` *before* the value is
used: AX attributes are polymorphic and a wrong-type read is another segfault
rather than an exception.
"""

from __future__ import annotations

import importlib as _importlib
import sys as _sys
import time
from ctypes import c_bool, c_int32, c_int64, c_long, c_uint16, c_uint32, c_uint64, c_void_p
from typing import TYPE_CHECKING, Sequence

from kiro_crew import platform_compat  # noqa: F401
from kiro_crew import _facade_mirror
from kiro_crew.computer_use.macos_ffi_bind import (
    CLICK_PAIR_DELAY_SECS,
    DRAG_STEP_DELAY_SECS,
    DRAG_STEPS,
    K_CG_HID_EVENT_TAP,
    K_CG_MOUSE_EVENT_CLICK_STATE,
    K_CG_SCROLL_DELTA_AXIS_1,
    K_CG_SCROLL_DELTA_AXIS_2,
    K_CG_SCROLL_EVENT_UNIT_LINE,
    MOUSE_EVENT_TYPES,
    CGPoint,
    Libs,
    _frameworks,
    event_source,
    logger,
)
from kiro_crew.computer_use.types import (  # noqa: F401
    AX_PARENT,
    AX_POSITION,
    AX_SIZE,
    MOUSE_BUTTON_LEFT,
    MOUSE_BUTTON_MIDDLE,
    MOUSE_BUTTON_RIGHT,
    SCREENSHOT_DIR_NAME,
    ComputerUseUnsupported,
)


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


def _drag_plan(
    start: tuple[float, float],
    end: tuple[float, float],
    down_type: int,
    up_type: int,
    dragged_type: int,
    steps: int,
    points: "Sequence[tuple[float, float]] | None",
) -> "list[tuple[int, float, float]]":
    """The event plan both drag functions post: down, the motion, up.

    Shared so the two paths cannot disagree about the shape of a gesture — the only
    thing that differs between them is WHERE each event is delivered, and a plan
    built twice is a plan that drifts.

    *points* is a caller-supplied path INCLUDING both endpoints (from
    ``cursor_motion.drag_points``); when it is ``None`` the endpoints are interpolated
    into *steps* straight segments, which is what a slider sweep or a range
    selection wants.

    The endpoints come from *start* and *end* rather than from the path's own ends:
    those two points are what the confinement check authorized for the press and the
    release, and a release lands a drop.
    """
    x0, y0 = float(start[0]), float(start[1])
    x1, y1 = float(end[0]), float(end[1])
    if points:
        motion = [(float(px), float(py)) for px, py in points[1:-1]]
    else:
        total = max(1, int(steps))
        motion = [
            (x0 + (x1 - x0) * (index / total), y0 + (y1 - y0) * (index / total))
            for index in range(1, total)
        ]
    plan: list[tuple[int, float, float]] = [(down_type, x0, y0)]
    plan.extend((dragged_type, px, py) for px, py in motion)
    plan.append((up_type, x1, y1))
    return plan


def post_mouse_drag(
    pid: int,
    start: tuple[float, float],
    end: tuple[float, float],
    *,
    button: str = MOUSE_BUTTON_LEFT,
    steps: int = DRAG_STEPS,
    points: "Sequence[tuple[float, float]] | None" = None,
) -> None:
    """Drag from *start* to *end* inside *pid*. **The pointer does NOT move.**

    Down at the start, interpolated ``MouseDragged`` events, up at the end — all
    app-targeted. The intermediate events are NOT padding: a bare down/up pair is
    not a drag to most applications, because the gesture is recognized from the
    motion between the endpoints (a canvas records two isolated points and a text
    view selects nothing). Verified against TextEdit, which needed the interpolation
    before it registered a selection.

    *points* supplies the whole path when the caller wants a specific SHAPE (a curved
    stroke); otherwise :data:`DRAG_STEPS` straight segments are interpolated, which is
    what a slider sweep and a range selection need.

    The per-step sleep matters for the same reason the intermediate events do:
    identical timestamps let AppKit's recognizer coalesce the whole sequence into one
    motion.
    """
    libs = _frameworks()
    button_number, down_type, up_type, dragged_type = mouse_button_codes(button)
    plan = _drag_plan(start, end, down_type, up_type, dragged_type, steps, points)
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
    points: "Sequence[tuple[float, float]] | None" = None,
) -> None:
    """MOVE THE OPERATOR'S REAL POINTER along a drag from *start* to *end*.

    The pointer-moving counterpart of :func:`post_mouse_drag`, subject to the
    identical two permits (see :func:`post_mouse_global`) and taking the same
    optional *points* path. The cursor is warped before EVERY event, not only the
    first: a global drag is only coherent if the physical cursor tracks the
    synthesized motion, and warping once would leave the OS delivering the
    intermediate events to whatever sat under the start point.
    """
    libs = _frameworks()
    button_number, down_type, up_type, dragged_type = mouse_button_codes(button)
    plan = _drag_plan(start, end, down_type, up_type, dragged_type, steps, points)
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


# The owner modules, lowest layer first: the first one that binds a name answers a read
# of it. ``kiro_crew.computer_use.macos_ffi.<name>`` stays the import path and the patch
# surface for every name they define: a patch through this module reaches every owner that
# binds the name (see :mod:`kiro_crew._facade_mirror`).
_OWNERS = (
    "kiro_crew.computer_use.macos_ffi_bind",
    "kiro_crew.computer_use.macos_ffi_cf",
    "kiro_crew.computer_use.macos_ffi_ax",
    "kiro_crew.computer_use.macos_ffi_window",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name this module serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule

if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.computer_use.macos_ffi_ax import (  # noqa: F401
        MAX_ACTION_NAME_LEN,
        _ax_value_point,
        _ax_value_size,
        _sanitize_action,
        _unbox_ax_value,
        ax_actions,
        ax_app_element,
        ax_application,
        ax_attr,
        ax_attr_error,
        ax_bool,
        ax_bool_opt,
        ax_children,
        ax_frame,
        ax_is_process_trusted,
        ax_is_settable,
        ax_owned_attr,
        ax_parent,
        ax_perform,
        ax_retained_elements,
        ax_set_str,
        ax_set_true,
        ax_str,
        ax_window_id,
        release_all,
        retain,
        same_element,
    )
    from kiro_crew.computer_use.macos_ffi_bind import (  # noqa: F401
        _CF_TYPE_ID,
        _FN_SPECS,
        _FRAMEWORK_NAMES,
        _LIBPROC_FALLBACK_PATH,
        _OPTIONAL_FN_SPECS,
        _VOID,
        AX_ERR_ACTION_UNSUPPORTED,
        AX_ERR_ATTRIBUTE_UNSUPPORTED,
        AX_ERR_CANNOT_COMPLETE,
        AX_ERR_NO_VALUE,
        AX_ERR_SUCCESS,
        AX_MANUAL_ACCESSIBILITY,
        AX_MESSAGING_TIMEOUT_SECS,
        CG_BOUNDS_H,
        CG_BOUNDS_W,
        CG_BOUNDS_X,
        CG_BOUNDS_Y,
        CG_SCROLL_LINES_PER_PAGE,
        CG_WINDOW_BOUNDS,
        CG_WINDOW_LAYER,
        CG_WINDOW_LAYER_NORMAL,
        CG_WINDOW_NAME,
        CG_WINDOW_NUMBER,
        CG_WINDOW_OWNER_NAME,
        CG_WINDOW_OWNER_PID,
        ELECTRON_OPT_IN_POLL_SECS,
        ELECTRON_OPT_IN_WAIT_SECS,
        IO_KEY_LOSSY_QUALITY,
        IO_KEY_MAX_PIXEL_SIZE,
        K_AX_VALUE_CG_POINT,
        K_AX_VALUE_CG_SIZE,
        K_CF_NUMBER_DOUBLE,
        K_CF_NUMBER_SINT32,
        K_CF_STRING_ENCODING_UTF8,
        K_CG_EVENT_LEFT_MOUSE_DOWN,
        K_CG_EVENT_LEFT_MOUSE_DRAGGED,
        K_CG_EVENT_LEFT_MOUSE_UP,
        K_CG_EVENT_MOUSE_MOVED,
        K_CG_EVENT_OTHER_MOUSE_DOWN,
        K_CG_EVENT_OTHER_MOUSE_DRAGGED,
        K_CG_EVENT_OTHER_MOUSE_UP,
        K_CG_EVENT_RIGHT_MOUSE_DOWN,
        K_CG_EVENT_RIGHT_MOUSE_DRAGGED,
        K_CG_EVENT_RIGHT_MOUSE_UP,
        K_CG_EVENT_SOURCE_STATE_PRIVATE,
        K_CG_MOUSE_BUTTON_CENTER,
        K_CG_MOUSE_BUTTON_LEFT,
        K_CG_MOUSE_BUTTON_RIGHT,
        K_CG_NULL_WINDOW_ID,
        K_CG_WINDOW_IMAGE_BOUNDS_IGNORE_FRAMING,
        K_CG_WINDOW_LIST_EXCLUDE_DESKTOP,
        K_CG_WINDOW_LIST_INCLUDING_WINDOW,
        K_CG_WINDOW_LIST_ON_SCREEN_ONLY,
        LIB_AX,
        LIB_CF,
        LIB_CG,
        LIB_IO,
        LIB_PROC,
        PROC_PIDPATH_MAX,
        UTI_JPEG,
        CGRect,
        CGSize,
        TypeIds,
        WindowInfo,
        _bind,
        _event_source,
        _init_lock,
        _libs,
        _load_library,
        _type_ids,
        available,
        frameworks,
        reset_frameworks,
        type_ids,
    )
    from kiro_crew.computer_use.macos_ffi_cf import (  # noqa: F401
        _cf_type_dict,
        cf_array_items,
        cf_bool_value,
        cf_is,
        cf_number_double,
        cf_number_int,
        cf_string,
        cf_string_value,
        cf_true,
    )
    from kiro_crew.computer_use.macos_ffi_window import (  # noqa: F401
        _JPEG_EOI,
        _JPEG_MARKER,
        _JPEG_RST_FIRST,
        _JPEG_RST_LAST,
        _JPEG_SOF_DIMENSION_OFFSET,
        _JPEG_SOF_MARKERS,
        _JPEG_SOI,
        _dict_bounds,
        _dict_float,
        _dict_int,
        _dict_str,
        capture_window_jpeg,
        executable_path,
        jpeg_dimensions,
        preflight_screen_capture,
        shots_dir_default,
        window_list,
    )
