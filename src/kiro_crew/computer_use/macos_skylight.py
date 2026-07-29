"""``click_method: "sky_click"`` — the PRIVATE SkyLight background-window click.

**This module is quarantine.** It is the ONLY file in the package allowed to touch
undocumented Apple ABI, and it exists as its own module rather than living in
``macos_ffi.py`` so that:

* ``macos_ffi.py`` keeps its "public frameworks only" property, which is what makes
  it reviewable against Apple's documentation;
* every future macOS-compatibility break has ONE review boundary — if a point
  release changes a byte offset or drops a symbol, the blast radius is this file;
* the private surface can be disabled wholesale (see :func:`available`) without
  touching any other code path.

Other implementations of this technique isolate it the same way; at least one ships
it in a separate signed helper process rather than inside the main binary, which is
the same instinct one level further. Worth revisiting if this surface grows.

**What it buys.** ``sky_click`` clicks a window that is BEHIND other windows,
without raising it and without moving the operator's pointer. Neither public path
can do that: ``accessibility`` needs an addressable element (and many canvases have
none), and ``app_post`` delivers to the app but is ignored by renderers that
hit-test against the window server's idea of which window is in front — the
browser-engine-backed apps, in practice. That
gap is real and was hit in practice: a Freeform canvas behind a Zoom annotation
overlay could not be drawn on by any public method.

**Where the ABI comes from.** The symbol declarations and the event-field recipe are
derived from prior permissively-licensed open-source work that reverse-engineered
this path (attributed in ``NOTICE``). None of it is published by
Apple: the byte layout of the activation record and the numeric event fields are
observed behaviour, and ``WindowServer`` does not document the accepted width of the
click-group field. Treat every constant here as observed, not specified.

**Fail-closed, always.** :func:`available` reports which symbols are missing and
every entry point raises a typed, model-readable refusal when the SPI is not
usable. A future macOS that removes ``SLEventPostToPid`` must degrade to "this
method is unavailable, use app_post" — never to a crash, and never to a silently
mis-delivered click.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import threading
import time
from ctypes import (
    POINTER,
    c_double,
    c_int32,
    c_int64,
    c_uint8,
    c_uint32,
    c_void_p,
)
from dataclasses import dataclass, field
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.computer_use import macos_ffi
from kiro_crew.computer_use.types import ERR_SKY_CLICK_BUTTON
from kiro_crew.computer_use.types import MOUSE_BUTTON_LEFT as _LEFT_BUTTON
from kiro_crew.computer_use.types import ComputerUseError

logger = logging.getLogger(__name__)

# ── The private frameworks ──
# Absolute paths, not ``find_library``: these are PrivateFrameworks and are not on
# the standard search path. ``dlopen`` of an absent path simply fails, which is the
# degradation we want.
_SKYLIGHT_PATH = "/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight"
_APP_SERVICES_PATH = (
    "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
)

# ── The private symbols ──
# ``CGEventSetWindowLocation`` lives in SkyLight despite the CG prefix, and
# ``GetProcessForPID`` is a deprecated-but-present Carbon call in
# ApplicationServices. Named as constants so the "which symbol is missing"
# diagnostic can quote them back to the operator.
_SYM_POST_TO_PID = "SLEventPostToPid"
_SYM_SET_INT_FIELD = "SLEventSetIntegerValueField"
_SYM_SET_WINDOW_LOCATION = "CGEventSetWindowLocation"
_SYM_POST_EVENT_RECORD = "SLPSPostEventRecordTo"
_SYM_GET_PROCESS_FOR_PID = "GetProcessForPID"

# ── The activation record ──
# A 0xF8-byte event record that tells the window server to treat one window of one
# process as synthetically focused. Every offset below is OBSERVED behaviour copied
# from the reference; none is documented. Apple can change any of it.
_RECORD_SIZE = 0xF8
_RECORD_OFF_SIZE = 0x04  # carries the record's own length
_RECORD_OFF_KIND = 0x08  # 0x0D = the activation/deactivation kind
_RECORD_OFF_WINDOW_ID = 0x3C  # 4 bytes, little-endian
_RECORD_OFF_FOCUSED = 0x8A  # 0x01 = activate, 0x02 = deactivate
_RECORD_KIND_ACTIVATE = 0x0D
_RECORD_FOCUSED_YES = 0x01
_RECORD_FOCUSED_NO = 0x02
# A ProcessSerialNumber is two 32-bit halves; ``GetProcessForPID`` fills both.
_PSN_SIZE = 8

# ── The private event fields ──
# Raw ``CGEventField`` numbers the window server reads when routing a click to a
# specific window. The public enum does not name them.
_FIELD_GESTURE_PHASE = 0
_FIELD_CLICK_STATE = 1
_FIELD_BUTTON_NUMBER = 3
_FIELD_SUBTYPE = 7
_FIELD_TARGET_PID = 40
_FIELD_WINDOW_NUMBER = 51
_FIELD_CLICK_GROUP_ID = 58
_FIELD_WINDOW_UNDER_POINTER = 91
_FIELD_HANDLING_WINDOW_UNDER_POINTER = 92
# Observed constant: subtype 3 is what a real windowed mouse event carries.
_EVENT_SUBTYPE_WINDOWED = 3

# ── The click recipe ──
# A "primer" down/up pair at (-1, -1) precedes the real click. It is not a retry:
# it is what makes the window server accept the following pair as a click on the
# TARGET window rather than on whatever is frontmost. Removing it makes the real
# click land on the front window, which is the exact bug this method exists to
# avoid.
_PRIMER_POINT = (-1.0, -1.0)
_PHASE_MOVED = 2
_PHASE_PRIMER_DOWN = 1
_PHASE_PRIMER_UP = 2
_PHASE_TARGET = 3
# Sleeps, in seconds. Tuned live against real applications. They are
# the cost of the method: ~0.12s for a single click.
_DELAY_AFTER_MOVE = 0.015
_DELAY_AFTER_PRIMER_DOWN = 0.001
_DELAY_AFTER_PRIMER_UP = 0.100
_DELAY_AFTER_TARGET_DOWN = 0.001
_DELAY_BETWEEN_CLICK_PAIRS = 0.080
# Synthetic focus needs time to be observed by the target, and the final mouse-up
# needs to be consumed BEFORE focus is dropped, or a browser-engine renderer that
# does its own hit-testing misses it.
_DELAY_FOCUS_SETTLE = 0.040
_DELAY_BEFORE_FOCUS_RELEASE = 0.100
# ``kCGEventSourceStateHIDSystemState``. Declared HERE rather than imported from
# ``macos_ffi``: that module only ever builds PRIVATE-source events (modifier
# hygiene), so it has no HID constant to borrow, and adding one there would invite
# a public path to use it. See ``_post_recipe`` for why this method needs it.
_K_CG_EVENT_SOURCE_STATE_HID = 1

# Field 58's accepted width is unpublished, so the group id is kept in the range
# observed to be accepted (a nanosecond component, always < 1e9).
_CLICK_GROUP_MODULUS = 1_000_000_000

#: ``sky_click`` supports single and double clicks only. A triple click is not in
#: the observed recipe, and inventing a third pair would be guessing at ABI.
MAX_SKY_CLICK_COUNT = 2
MIN_SKY_CLICK_COUNT = 1

_REFUSAL_UNAVAILABLE = (
    "click_method 'sky_click' is unavailable on this system ({reason}). Use "
    "click_method 'app_post' instead — it delivers to the same application through "
    "a public API, and only fails to reach renderers that hit-test against the "
    "window server"
)
_REFUSAL_COUNT = (
    "click_method 'sky_click' supports click_count 1 or 2 (got {count}). Use "
    "click_method 'app_post' for a triple click"
)
#: ``sky_click`` is a LEFT-button recipe only — see ``types.ERR_SKY_CLICK_BUTTON``
#: for the wording and ``policy.check_method_button`` for the enforcing gate. The
#: observed private event sequence (the primer pair at (-1,-1), the focus-flag
#: record, the nine private fields) was reverse-engineered for a left click; the
#: button number is one field among those, and there is no evidence the rest of the
#: recipe is button-agnostic.
_REFUSAL_BUTTON = ERR_SKY_CLICK_BUTTON
_REFUSAL_STALE_WINDOW = (
    "the sky_click target window is no longer on screen or no longer belongs to "
    "that application. Call computer_get_state again to re-resolve it"
)
_REFUSAL_NO_WINDOW_ID = (
    "click_method 'sky_click' needs a resolved window id, and the driver did not "
    "report one for this application. Use click_method 'app_post' instead"
)
_REFUSAL_OUTSIDE_WINDOW = (
    "the sky_click point is outside the target window's bounds. Call "
    "computer_get_state again — the window has probably moved or resized"
)
_REFUSAL_PSN = (
    "click_method 'sky_click' could not resolve pid {pid} to a process serial "
    "number (OSStatus {status}). Use click_method 'app_post' instead"
)
_REFUSAL_FOCUS = (
    "click_method 'sky_click' could not synthesize target-window focus (OSStatus "
    "{status}). Use click_method 'app_post' instead"
)
_REFUSAL_EVENT = "click_method 'sky_click' could not build a mouse event"

# ONE click at a time, process-wide. The method mutates global window-server focus
# state and restores it afterwards; two interleaved sequences would restore each
# other's state and leave a window synthetically focused.
_dispatch_lock = threading.Lock()
_init_lock = threading.Lock()
_spi: "_SkyLightSPI | None" = None


@dataclass(frozen=True)
class SkyLightCapability:
    """Whether the private SPI is usable here, and why not when it is not."""

    missing_symbols: tuple[str, ...] = field(default_factory=tuple)
    load_error: str = ""

    @property
    def is_available(self) -> bool:
        return not self.missing_symbols and not self.load_error

    @property
    def reason(self) -> str:
        """A model-readable explanation. Empty when available."""
        if self.load_error:
            return self.load_error
        if self.missing_symbols:
            return "missing private symbols: " + ", ".join(self.missing_symbols)
        return ""


class _SkyLightSPI:
    """The bound private symbols, or a capability report explaining the absence.

    Constructed once. Unlike ``macos_ffi``'s bind pass this NEVER raises on a
    missing symbol: an unavailable private API is an expected state (a future
    macOS, a stripped system), not an error, and the caller turns it into a
    refusal that names the public alternative.
    """

    def __init__(self) -> None:
        # ``Any``, not ``Callable | None``: these are ctypes function pointers, and
        # every caller is reached only after ``capability.is_available`` proved they
        # are bound. Narrowing them to Optional would force an ignore at each of the
        # five call sites, which reads as noise rather than as a real invariant.
        self.post_to_pid: Any = None
        self.set_int_field: Any = None
        self.set_window_location: Any = None
        self.post_event_record: Any = None
        self.get_process_for_pid: Any = None

        if not platform_compat.IS_MACOS:
            self.capability = SkyLightCapability(
                load_error="sky_click requires macOS (the SkyLight framework is Apple-only)"
            )
            return

        sky = _dlopen(_SKYLIGHT_PATH)
        app_services = _dlopen(_APP_SERVICES_PATH)
        if sky is None:
            self.capability = SkyLightCapability(
                load_error="the private SkyLight framework could not be loaded"
            )
            return

        missing: list[str] = []
        self.post_to_pid = _bind(
            sky, _SYM_POST_TO_PID, None, [c_int32, c_void_p], missing
        )
        self.set_int_field = _bind(
            sky, _SYM_SET_INT_FIELD, None, [c_void_p, c_uint32, c_int64], missing
        )
        # Scalar doubles, NOT a CGPoint by value: the reference models this private
        # ABI as (event, double, double) and notes that relying on the aggregate
        # calling convention is what breaks. Keep the scalar form.
        self.set_window_location = _bind(
            sky, _SYM_SET_WINDOW_LOCATION, None, [c_void_p, c_double, c_double], missing
        )
        self.post_event_record = _bind(
            sky, _SYM_POST_EVENT_RECORD, c_int32, [c_void_p, POINTER(c_uint8)], missing
        )
        if app_services is None:
            missing.append(_SYM_GET_PROCESS_FOR_PID)
        else:
            self.get_process_for_pid = _bind(
                app_services,
                _SYM_GET_PROCESS_FOR_PID,
                c_int32,
                [c_int32, c_void_p],
                missing,
            )
        self.capability = SkyLightCapability(missing_symbols=tuple(missing))


def _dlopen(path: str) -> "ctypes.CDLL | None":
    """Load a framework by absolute path, or ``None``. Never raises."""
    try:
        return ctypes.CDLL(path)
    except OSError:
        logger.debug("sky_click: could not load %s", path, exc_info=True)
        return None


def _bind(lib: Any, symbol: str, restype: Any, argtypes: Any, missing: list) -> Any:
    """Bind one symbol with explicit argtypes, recording it when absent.

    ``argtypes`` is mandatory here for the same reason it is in ``macos_ffi``: with
    it unset ctypes marshals a Python int as a 32-bit C int and TRUNCATES, which
    for a window id or a pid means addressing the wrong window.
    """
    try:
        fn = getattr(lib, symbol)
    except AttributeError:
        missing.append(symbol)
        return None
    fn.restype = restype
    fn.argtypes = argtypes
    return fn


def _get_spi() -> _SkyLightSPI:
    global _spi
    if _spi is not None:
        return _spi
    with _init_lock:
        if _spi is None:
            _spi = _SkyLightSPI()
    return _spi


def available() -> SkyLightCapability:
    """Whether ``sky_click`` can run here. Cheap after the first call.

    Read by the dashboard's config payload and by :func:`sky_click` itself, so the
    Settings panel can say "unavailable on this macOS" rather than letting the
    operator select a method that always refuses.
    """
    return _get_spi().capability


def activation_record(window_id: int, *, focused: bool) -> bytes:
    """Build the 0xF8-byte window-activation record.

    Pure, and separated from the posting so the byte layout is unit-testable
    without a window server. That matters more here than anywhere else in the
    package: this is undocumented ABI, so the ONLY way to notice a future edit
    silently changing an offset is to pin the bytes.
    """
    record = bytearray(_RECORD_SIZE)
    record[_RECORD_OFF_SIZE] = _RECORD_SIZE
    record[_RECORD_OFF_KIND] = _RECORD_KIND_ACTIVATE
    wid = int(window_id) & 0xFFFFFFFF
    record[_RECORD_OFF_WINDOW_ID : _RECORD_OFF_WINDOW_ID + 4] = wid.to_bytes(4, "little")
    record[_RECORD_OFF_FOCUSED] = _RECORD_FOCUSED_YES if focused else _RECORD_FOCUSED_NO
    return bytes(record)


@dataclass(frozen=True)
class SkyClickStep:
    """One event in the click recipe. See :func:`click_recipe`."""

    event_type: int
    at_target: bool
    click_state: int
    phase: int
    delay_after: float


def click_recipe(click_count: int, button: str = _LEFT_BUTTON) -> tuple[SkyClickStep, ...]:
    """The ordered event sequence for a ``sky_click``.

    Pure and separately tested, because the ORDER and the primer pair are the
    load-bearing parts: a future edit that drops the primer, or reorders the
    move-before-down, produces a click on the FRONT window instead of the target —
    a silent mis-delivery rather than a failure.

    *button* is accepted only to be REFUSED when it is not the left one. It is a
    parameter rather than an unstated assumption because the previous signature took
    no button at all and built the recipe with the left-button codes regardless of
    what the caller had asked for — so a right-click request became a left click
    silently, on a background window the operator cannot see. Naming the argument is
    what makes the constraint checkable instead of implicit; see
    ``ERR_SKY_CLICK_BUTTON`` for why refusing beats downgrading.

    Raises :class:`ComputerUseError` for an unsupported count OR button; the
    dispatcher turns either into a refusal that names ``app_post``.
    """
    if not MIN_SKY_CLICK_COUNT <= int(click_count) <= MAX_SKY_CLICK_COUNT:
        raise ComputerUseError(_REFUSAL_COUNT.format(count=click_count))
    if button != _LEFT_BUTTON:
        raise ComputerUseError(_REFUSAL_BUTTON.format(button=button))
    _, down_type, up_type, _dragged = macos_ffi.mouse_button_codes(_LEFT_BUTTON)
    moved_type = macos_ffi.K_CG_EVENT_MOUSE_MOVED
    steps = [
        SkyClickStep(moved_type, True, 0, _PHASE_MOVED, _DELAY_AFTER_MOVE),
        SkyClickStep(down_type, False, 1, _PHASE_PRIMER_DOWN, _DELAY_AFTER_PRIMER_DOWN),
        SkyClickStep(up_type, False, 1, _PHASE_PRIMER_UP, _DELAY_AFTER_PRIMER_UP),
    ]
    for pair in range(1, int(click_count) + 1):
        last = pair == int(click_count)
        steps.append(
            SkyClickStep(down_type, True, pair, _PHASE_TARGET, _DELAY_AFTER_TARGET_DOWN)
        )
        steps.append(
            SkyClickStep(
                up_type,
                True,
                pair,
                _PHASE_TARGET,
                0.0 if last else _DELAY_BETWEEN_CLICK_PAIRS,
            )
        )
    return tuple(steps)


def _window_is_current(window_id: int, pid: int) -> bool:
    """Is *window_id* still on screen AND still owned by *pid*?

    Re-checked immediately before clicking rather than trusted from the snapshot.
    A window id is recycled by the window server, so a stale id can name a
    DIFFERENT window — and this method's whole purpose is clicking something the
    operator cannot see, where a mis-aimed click is invisible to them.
    """
    try:
        for info in macos_ffi.window_list():
            if info.window_id == int(window_id):
                return info.pid == int(pid)
    except Exception:
        logger.debug("sky_click: window re-check failed", exc_info=True)
        return False
    return False


def sky_click(
    *,
    pid: int,
    window_id: int,
    screen_x: float,
    screen_y: float,
    window_x: float,
    window_y: float,
    window_width: float,
    window_height: float,
    click_count: int = 1,
    button: str = _LEFT_BUTTON,
) -> None:
    """Click ``(screen_x, screen_y)`` in a BACKGROUND window, without raising it.

    *window_x* / *window_y* are the same point expressed in the window's own
    top-left coordinates; the window server routes by the window-local point, so
    both are required and the caller (the driver) computes the conversion from the
    snapshot's bounds.

    Contracted to raise :class:`ComputerUseError` — never a ctypes error and never
    an ``OSError`` — so ``macos_driver``'s ``_guarded`` seam turns every failure
    into a model-readable refusal that names ``app_post`` as the alternative.

    **Does not move the operator's pointer** and does not change which app is
    frontmost. It DOES briefly mark the target window synthetically focused, and
    restores that afterwards; the restore runs even when the click sequence fails.
    """
    spi = _get_spi()
    capability = spi.capability
    if not capability.is_available:
        raise ComputerUseError(_REFUSAL_UNAVAILABLE.format(reason=capability.reason))
    if not int(window_id):
        raise ComputerUseError(_REFUSAL_NO_WINDOW_ID)
    for value in (screen_x, screen_y, window_x, window_y):
        if not _finite(value):
            raise ComputerUseError(_REFUSAL_OUTSIDE_WINDOW)
    if window_width <= 0 or window_height <= 0:
        raise ComputerUseError(_REFUSAL_OUTSIDE_WINDOW)
    if not (0 <= window_x <= window_width and 0 <= window_y <= window_height):
        raise ComputerUseError(_REFUSAL_OUTSIDE_WINDOW)

    recipe = click_recipe(click_count, button)
    # Serialized: the synthetic-focus flip is global window-server state.
    with _dispatch_lock:
        if not _window_is_current(window_id, pid):
            raise ComputerUseError(_REFUSAL_STALE_WINDOW)
        focused = _begin_synthetic_focus(spi, pid=pid, window_id=window_id)
        try:
            _post_recipe(
                spi,
                recipe,
                pid=pid,
                window_id=window_id,
                screen=(screen_x, screen_y),
                window=(window_x, window_y),
            )
        finally:
            if focused:
                # SkyLight delivery is asynchronous: drop focus too early and a
                # renderer that hit-tests independently never sees the final
                # mouse-up, so the click
                # registers as a press with no release.
                time.sleep(_DELAY_BEFORE_FOCUS_RELEASE)
                _end_synthetic_focus(spi, pid=pid, window_id=window_id)


def _finite(value: float) -> bool:
    return value == value and value not in (float("inf"), float("-inf"))


def _post_recipe(
    spi: _SkyLightSPI,
    recipe: "tuple[SkyClickStep, ...]",
    *,
    pid: int,
    window_id: int,
    screen: "tuple[float, float]",
    window: "tuple[float, float]",
) -> None:
    """Build, stamp and post every step of *recipe*. Releases each event."""
    libs = macos_ffi._frameworks()
    # The HID source, NOT the private source every other path in this package
    # uses. The window server's routing of these private fields is only observed
    # to work with a HID-state source; a private-source event is delivered but
    # ignored. The usual modifier-inheritance hazard does not apply because the
    # flags are explicitly zeroed on every event below.
    source = c_void_p(libs.cg.CGEventSourceCreate(_K_CG_EVENT_SOURCE_STATE_HID))
    if not source:
        raise ComputerUseError(_REFUSAL_EVENT)
    click_group = int(time.monotonic_ns() % _CLICK_GROUP_MODULUS)
    button_number, _down, _up, _dragged = macos_ffi.mouse_button_codes(
        _LEFT_BUTTON
    )
    try:
        for step in recipe:
            point = screen if step.at_target else _PRIMER_POINT
            local = window if step.at_target else _PRIMER_POINT
            event = c_void_p(
                libs.cg.CGEventCreateMouseEvent(
                    source,
                    c_uint32(int(step.event_type)),
                    macos_ffi.CGPoint(float(point[0]), float(point[1])),
                    c_uint32(int(button_number)),
                )
            )
            if not event:
                raise ComputerUseError(_REFUSAL_EVENT)
            try:
                libs.cg.CGEventSetFlags(event, ctypes.c_uint64(0))
                _stamp(
                    spi,
                    event,
                    pid=pid,
                    window_id=window_id,
                    local=local,
                    click_state=step.click_state,
                    phase=step.phase,
                    click_group=click_group,
                )
                # BOTH channels, deliberately, and not as a retry: the SkyLight
                # post is what reaches a hit-testing renderer, while the public
                # per-pid post preserves plain-AppKit compatibility. Dropping
                # either makes one class of app stop responding.
                spi.post_to_pid(int(pid), event)
                libs.cg.CGEventPostToPid(c_int32(int(pid)), event)
            finally:
                libs.cf.CFRelease(event)
            if step.delay_after:
                time.sleep(step.delay_after)
    finally:
        libs.cf.CFRelease(source)


def _stamp(
    spi: _SkyLightSPI,
    event: c_void_p,
    *,
    pid: int,
    window_id: int,
    local: "tuple[float, float]",
    click_state: int,
    phase: int,
    click_group: int,
) -> None:
    """Write the private routing fields onto one event."""
    wid = int(window_id)
    fields = (
        (_FIELD_GESTURE_PHASE, int(phase)),
        (_FIELD_CLICK_STATE, int(click_state)),
        (_FIELD_BUTTON_NUMBER, 0),
        (_FIELD_SUBTYPE, _EVENT_SUBTYPE_WINDOWED),
        (_FIELD_TARGET_PID, int(pid)),
        (_FIELD_WINDOW_NUMBER, wid),
        (_FIELD_CLICK_GROUP_ID, int(click_group)),
        # Both "what is under the pointer" fields name the TARGET window: that is
        # the lie that makes the window server route to a background window.
        (_FIELD_WINDOW_UNDER_POINTER, wid),
        (_FIELD_HANDLING_WINDOW_UNDER_POINTER, wid),
    )
    for field_id, value in fields:
        spi.set_int_field(event, c_uint32(field_id), c_int64(value))
    spi.set_window_location(event, c_double(local[0]), c_double(local[1]))


def _process_serial_number(spi: _SkyLightSPI, pid: int) -> bytes:
    """Resolve *pid* to a ProcessSerialNumber, or raise a refusal."""
    buffer = (c_uint8 * _PSN_SIZE)()
    status = int(spi.get_process_for_pid(c_int32(int(pid)), buffer))
    if status != 0:
        raise ComputerUseError(_REFUSAL_PSN.format(pid=pid, status=status))
    return bytes(buffer)


def _post_activation(spi: _SkyLightSPI, psn: bytes, record: bytes) -> None:
    psn_buf = (c_uint8 * len(psn)).from_buffer_copy(psn)
    rec_buf = (c_uint8 * len(record)).from_buffer_copy(record)
    status = int(
        spi.post_event_record(
            ctypes.cast(psn_buf, c_void_p),
            ctypes.cast(rec_buf, POINTER(c_uint8)),
        )
    )
    if status != 0:
        raise ComputerUseError(_REFUSAL_FOCUS.format(status=status))


def _begin_synthetic_focus(spi: _SkyLightSPI, *, pid: int, window_id: int) -> bool:
    """Mark the target window synthetically focused. Returns whether to undo it.

    Skipped when the target is ALREADY frontmost: flipping focus it already has
    would be a no-op followed by a deactivate that takes real focus away.
    """
    if _is_frontmost(pid):
        return False
    psn = _process_serial_number(spi, pid)
    _post_activation(spi, psn, activation_record(window_id, focused=True))
    time.sleep(_DELAY_FOCUS_SETTLE)
    return True


def _end_synthetic_focus(spi: _SkyLightSPI, *, pid: int, window_id: int) -> None:
    """Undo :func:`_begin_synthetic_focus`. Best-effort; never raises.

    Swallowing here is deliberate: the click already happened, so raising would
    replace a successful action with an error. A failed restore leaves the target
    marked focused, which the next real click corrects.
    """
    try:
        psn = _process_serial_number(spi, pid)
        _post_activation(spi, psn, activation_record(window_id, focused=False))
        time.sleep(_DELAY_FOCUS_SETTLE)
    except Exception:
        logger.debug("sky_click: releasing synthetic focus failed", exc_info=True)


def _is_frontmost(pid: int) -> bool:
    """Is *pid* the owner of the frontmost normal window?

    Read from the CG window list rather than ``NSWorkspace`` (which the Swift
    reference uses) because this package deliberately links no AppKit: the front
    entry of the on-screen layer-0 list is the same answer without a second
    framework.
    """
    try:
        for info in macos_ffi.window_list():
            if info.layer != macos_ffi.CG_WINDOW_LAYER_NORMAL:
                continue
            return info.pid == int(pid)
    except Exception:
        logger.debug("sky_click: frontmost probe failed", exc_info=True)
    return False


__all__ = [
    "MAX_SKY_CLICK_COUNT",
    "MIN_SKY_CLICK_COUNT",
    "SkyClickStep",
    "SkyLightCapability",
    "activation_record",
    "available",
    "click_recipe",
    "sky_click",
]
