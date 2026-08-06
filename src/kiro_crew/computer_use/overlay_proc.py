"""The Cursor Motion overlay RENDERER — a separate process, run as ``python -m``.

    python -m kiro_crew.computer_use.overlay_proc

Reads newline-delimited JSON commands on stdin and draws a fake mouse cursor on
the real desktop. That is its entire job: it makes no policy decisions, reads no
config, computes no paths, and never touches the accessibility or capture
surfaces. Every "should we?" question was already answered by
:mod:`kiro_crew.computer_use.overlay` before a byte reached this process.

**Why this module owns its own ctypes, when ``macos_ffi`` is otherwise the only
module allowed to.** That invariant exists so the FFI hazards of the AX/CG
surface — the segfault-on-missing-argtypes, the CFString lifetime discipline, the
uncatchable ``CFArrayGetValueAtIndex`` abort — are audited in ONE file inside the
gateway process. This module is not in the gateway process. It is a separate
executable whose entire address space is disposable: it touches the AppKit/ObjC
runtime (a surface ``macos_ffi`` does not model at all), it needs a main-thread
run loop that the gateway's main thread cannot provide because that thread is the
asyncio loop, and if it segfaults the only consequence is that no fake cursor is
drawn. Merging this into ``macos_ffi`` would drag AppKit into the gateway's
address space to no benefit and would put a run-loop pump next to code that must
never block. The separation is the safety property, not a violation of it.

Hard-won FFI facts encoded below, each of which cost a real debugging cycle:

* **``objc_msgSend`` is a SINGLE ctypes function object.** Assigning
  ``restype``/``argtypes`` mutates it GLOBALLY, so a cached, pre-configured
  binding goes stale the instant anything else calls it — the observed symptom was
  ``TypeError: this function takes at least 4 arguments``. :func:`_msg` therefore
  re-declares the signature at every call site. It looks wasteful; it is the only
  correct pattern short of hand-rolling separate function pointers.
* **Every symbol needs BOTH ``restype`` and ``argtypes``.** A missing ``argtypes``
  makes ctypes marshal a Python int as a 32-bit C int and TRUNCATE a 64-bit
  pointer, which is a SIGSEGV rather than an exception. There is no partial
  declaration in this file.
* **``setSharingType: 0`` (NSWindowSharingNone) makes the overlay invisible to
  ``screencapture``** — A/B verified. It stays on: the agent's own decoration must
  never pollute the screenshots the agent takes, which would otherwise feed a fake
  cursor back into the model's observations as if it were part of the UI.
* **NSWindow's origin is BOTTOM-left; the rest of computer use is TOP-left.** The
  single flip lives in :func:`_place`, so no other module has to think about it.
* **``setIgnoresMouseEvents:True``** makes the window click-THROUGH. Without it a
  purely decorative window would swallow the user's own clicks — turning a
  cosmetic feature into an input-blocking bug.

Lifecycle: the process exits cleanly on stdin EOF. That is the primary guarantee
that a crashed gateway cannot leave an orphan cursor parked on the user's screen —
when the parent dies its end of the pipe closes, ``readline`` returns ``""``, and
this process orders the window out and returns. :data:`OVERLAY_IDLE_HIDE_SECS` is
the backstop for the rarer case where the pipe stays open but nobody writes.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import json
import logging
import math
import os
import sys
import threading
import time
from typing import Any, Sequence

from kiro_crew import platform_compat
from kiro_crew.computer_use.types import (
    CLICK_PULSE_DEPTH,
    CLICK_PULSE_GAP_MS,
    CLICK_PULSE_MAX_STEP,
    CLICK_PULSE_MS,
    CURSOR_GLYPH_HEIGHT,
    CURSOR_GLYPH_WIDTH,
    CURSOR_HOTSPOT_X,
    CURSOR_HOTSPOT_Y,
    FALLBACK_SCREEN_HEIGHT,
    FALLBACK_SCREEN_WIDTH,
    MAX_CLICK_COUNT,
    MAX_MOVE_DURATION_MS,
    MIN_MOVE_DURATION_MS,
    NS_ACTIVATION_POLICY_ACCESSORY,
    NS_BACKING_STORE_BUFFERED,
    NS_COLLECTION_BEHAVIOR,
    NS_IMAGE_SCALE_PROPORTIONAL,
    NS_STATUS_WINDOW_LEVEL,
    NS_WINDOW_SHARING_NONE,
    NS_WINDOW_STYLE_BORDERLESS,
    OVERLAY_CMD_CLICK,
    OVERLAY_CMD_HIDE,
    OVERLAY_CMD_KEY,
    OVERLAY_CMD_MOVE,
    OVERLAY_CMD_QUIT,
    OVERLAY_FRAME_SLICE_SECS,
    OVERLAY_IDLE_HIDE_SECS,
    OVERLAY_IDLE_SLICE_SECS,
    OVERLAY_KEY_COUNT,
    OVERLAY_KEY_MS,
    OVERLAY_KEY_POINTS,
    OVERLAY_KEY_X,
    OVERLAY_KEY_Y,
    OVERLAY_READY_LINE,
)

logger = logging.getLogger(__name__)

__all__ = ["CursorOverlayWindow", "ObjCRuntime", "main", "read_commands"]

# ── ctypes struct layouts ──
# Real ``ctypes.Structure`` types, never "two doubles": a CGPoint passed as two
# separate arguments is mis-marshalled on arm64 (the same class of bug that made
# ``CGEventCreateMouseEvent`` misbehave in the FFI probe).


class CGPoint(ctypes.Structure):
    """CoreGraphics point. Bottom-left origin when it reaches an NSWindow."""

    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class CGSize(ctypes.Structure):
    """CoreGraphics size."""

    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


class CGRect(ctypes.Structure):
    """CoreGraphics rect (origin + size)."""

    _fields_ = [("origin", CGPoint), ("size", CGSize)]


_RUNLOOP_MODE = b"kCFRunLoopDefaultMode"


class ObjCRuntime:
    """A minimal, self-contained binding to the ObjC runtime and AppKit.

    Instantiating this is what LOADS the native libraries, so nothing happens at
    import time and this module can be imported (and its pure helpers tested) on a
    Linux CI shard. Tests substitute a fake with the same three primitives —
    ``cls`` / ``sel`` / ``msg`` — which is the whole reason the window code below
    talks to this object rather than to ``ctypes`` directly.
    """

    def __init__(self) -> None:
        objc_path = ctypes.util.find_library("objc")
        if not objc_path:  # pragma: no cover - present on every macOS
            raise OSError("libobjc not found")
        self._objc = ctypes.CDLL(objc_path)
        appkit_path = ctypes.util.find_library("AppKit")
        if appkit_path:
            # Loading AppKit REALIZES the NSApplication/NSWindow classes so
            # ``objc_getClass`` can find them. The handle itself is unused.
            ctypes.CDLL(appkit_path)
        cg_path = ctypes.util.find_library("CoreGraphics")
        self._cg = ctypes.CDLL(cg_path) if cg_path else None

        # BOTH restype and argtypes on every symbol — see the module docstring.
        self._objc.objc_getClass.restype = ctypes.c_void_p
        self._objc.objc_getClass.argtypes = [ctypes.c_char_p]
        self._objc.sel_registerName.restype = ctypes.c_void_p
        self._objc.sel_registerName.argtypes = [ctypes.c_char_p]
        if self._cg is not None:
            self._cg.CGMainDisplayID.restype = ctypes.c_uint32
            self._cg.CGMainDisplayID.argtypes = []
            self._cg.CGDisplayPixelsWide.restype = ctypes.c_size_t
            self._cg.CGDisplayPixelsWide.argtypes = [ctypes.c_uint32]
            self._cg.CGDisplayPixelsHigh.restype = ctypes.c_size_t
            self._cg.CGDisplayPixelsHigh.argtypes = [ctypes.c_uint32]

    def cls(self, name: str) -> Any:
        """``objc_getClass`` — the class object for *name*, or ``None``."""
        return self._objc.objc_getClass(name.encode("utf-8"))

    def sel(self, name: str) -> Any:
        """``sel_registerName`` — the selector for *name*."""
        return self._objc.sel_registerName(name.encode("utf-8"))

    def msg(
        self, receiver: Any, selector: Any, restype: Any, argtypes: Sequence[Any], *args: Any
    ) -> Any:
        """Send *selector* to *receiver*, declaring the signature AT THE CALL.

        ``objc_msgSend`` is one shared ctypes function object, so its
        ``restype``/``argtypes`` are process-global mutable state: caching a
        configured binding and reusing it later is the bug that raised
        ``TypeError: this function takes at least 4 arguments`` in the prototype.
        Re-declaring on every send is the fix.
        """
        fn = self._objc.objc_msgSend
        fn.restype = restype
        fn.argtypes = [ctypes.c_void_p, ctypes.c_void_p] + list(argtypes)
        return fn(receiver, selector, *args)

    def screen_size(self) -> tuple[float, float]:
        """Main-display size in points, or the fallback constants.

        Only used to clamp a target point into something finite. An approximate
        clamp on an exotic multi-display setup is strictly better than refusing to
        draw, because the overlay is cosmetic.
        """
        if self._cg is None:  # pragma: no cover - CoreGraphics is always present
            return (FALLBACK_SCREEN_WIDTH, FALLBACK_SCREEN_HEIGHT)
        try:
            display = self._cg.CGMainDisplayID()
            width = float(self._cg.CGDisplayPixelsWide(display))
            height = float(self._cg.CGDisplayPixelsHigh(display))
        except Exception:
            logger.debug("overlay: display size probe failed", exc_info=True)
            return (FALLBACK_SCREEN_WIDTH, FALLBACK_SCREEN_HEIGHT)
        if width <= 0.0 or height <= 0.0:
            return (FALLBACK_SCREEN_WIDTH, FALLBACK_SCREEN_HEIGHT)
        return (width, height)


class CursorOverlayWindow:
    """The borderless, click-through, screenshot-invisible NSWindow.

    Constructed lazily by :meth:`ensure` so a process that receives only a
    ``quit`` never creates a window at all. Every method is best-effort: a failure
    to draw is logged at debug and swallowed, because this process exists purely to
    put pixels on a screen and the caller (the gateway) has already committed to
    treating a missing cursor as a non-event.
    """

    def __init__(self, runtime: ObjCRuntime) -> None:
        self._rt = runtime
        self._window: Any = None
        self._glyph_width = CURSOR_GLYPH_WIDTH
        self._glyph_height = CURSOR_GLYPH_HEIGHT
        self._hotspot_x = CURSOR_HOTSPOT_X
        self._hotspot_y = CURSOR_HOTSPOT_Y
        self._screen_width, self._screen_height = runtime.screen_size()
        self._visible = False

    # ── construction ──

    def ensure(self) -> bool:
        """Create the window if needed. Returns whether one exists."""
        if self._window is not None:
            return True
        try:
            self._build()
        except Exception:
            logger.debug("overlay: window construction failed", exc_info=True)
            self._window = None
            return False
        return self._window is not None

    def _build(self) -> None:
        """Create NSApplication + the overlay NSWindow and add the glyph view."""
        rt = self._rt
        app = rt.msg(rt.cls("NSApplication"), rt.sel("sharedApplication"), ctypes.c_void_p, [])
        # Accessory activation policy: no Dock icon, no menu bar, and — critically
        # — the overlay never becomes the active application, so it cannot steal
        # focus from the app the agent is driving.
        rt.msg(
            app,
            rt.sel("setActivationPolicy:"),
            ctypes.c_bool,
            [ctypes.c_long],
            NS_ACTIVATION_POLICY_ACCESSORY,
        )

        image, hotspot = self._load_arrow_glyph()

        window = rt.msg(rt.cls("NSWindow"), rt.sel("alloc"), ctypes.c_void_p, [])
        window = rt.msg(
            window,
            rt.sel("initWithContentRect:styleMask:backing:defer:"),
            ctypes.c_void_p,
            [CGRect, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_bool],
            CGRect(CGPoint(0.0, 0.0), CGSize(self._glyph_width, self._glyph_height)),
            NS_WINDOW_STYLE_BORDERLESS,
            NS_BACKING_STORE_BUFFERED,
            False,
        )
        if not window:
            raise OSError("NSWindow allocation returned NULL")

        clear = rt.msg(rt.cls("NSColor"), rt.sel("clearColor"), ctypes.c_void_p, [])
        rt.msg(window, rt.sel("setBackgroundColor:"), None, [ctypes.c_void_p], clear)
        rt.msg(window, rt.sel("setOpaque:"), None, [ctypes.c_bool], False)
        rt.msg(window, rt.sel("setHasShadow:"), None, [ctypes.c_bool], False)
        # CLICK-THROUGH. Without this a decorative window would swallow the user's
        # own clicks in the region it covers.
        rt.msg(window, rt.sel("setIgnoresMouseEvents:"), None, [ctypes.c_bool], True)
        rt.msg(window, rt.sel("setLevel:"), None, [ctypes.c_long], NS_STATUS_WINDOW_LEVEL)
        # Invisible to screencapture / CGWindowList — keeps the agent's own fake
        # cursor out of the screenshots the agent takes. Do not relax this.
        rt.msg(window, rt.sel("setSharingType:"), None, [ctypes.c_long], NS_WINDOW_SHARING_NONE)
        rt.msg(
            window,
            rt.sel("setCollectionBehavior:"),
            None,
            [ctypes.c_ulong],
            NS_COLLECTION_BEHAVIOR,
        )
        rt.msg(window, rt.sel("setAlphaValue:"), None, [ctypes.c_double], 1.0)

        if image:
            view = rt.msg(rt.cls("NSImageView"), rt.sel("alloc"), ctypes.c_void_p, [])
            view = rt.msg(
                view,
                rt.sel("initWithFrame:"),
                ctypes.c_void_p,
                [CGRect],
                CGRect(CGPoint(0.0, 0.0), CGSize(self._glyph_width, self._glyph_height)),
            )
            rt.msg(view, rt.sel("setImage:"), None, [ctypes.c_void_p], image)
            rt.msg(
                view,
                rt.sel("setImageScaling:"),
                None,
                [ctypes.c_ulong],
                NS_IMAGE_SCALE_PROPORTIONAL,
            )
            content = rt.msg(window, rt.sel("contentView"), ctypes.c_void_p, [])
            if content:
                rt.msg(content, rt.sel("addSubview:"), None, [ctypes.c_void_p], view)

        self._hotspot_x, self._hotspot_y = hotspot
        self._window = window

    def _load_arrow_glyph(self) -> tuple[Any, tuple[float, float]]:
        """The system arrow cursor's image, its size, and its hot spot.

        Using ``NSCursor arrowCursor`` rather than shipping artwork means the fake
        cursor matches the user's real one (including accessibility size settings),
        which is the difference between "the agent is pointing at this" and "there
        is a weird glyph on my screen". Measured 28x40 with a (5,5) hot spot on the
        probe machine; the constants in ``types`` are only the fallback.

        The hot spot is returned in TOP-LEFT glyph coordinates (AppKit reports it
        that way), and :meth:`_place` converts once.
        """
        rt = self._rt
        try:
            cursor = rt.msg(rt.cls("NSCursor"), rt.sel("arrowCursor"), ctypes.c_void_p, [])
            if not cursor:
                return (None, (self._hotspot_x, self._hotspot_y))
            image = rt.msg(cursor, rt.sel("image"), ctypes.c_void_p, [])
            if not image:
                return (None, (self._hotspot_x, self._hotspot_y))
            size = rt.msg(image, rt.sel("size"), CGSize, [])
            width = float(getattr(size, "width", 0.0) or 0.0)
            height = float(getattr(size, "height", 0.0) or 0.0)
            if width > 0.0 and height > 0.0:
                self._glyph_width, self._glyph_height = width, height
            spot = rt.msg(cursor, rt.sel("hotSpot"), CGPoint, [])
            hx = float(getattr(spot, "x", CURSOR_HOTSPOT_X) or 0.0)
            hy = float(getattr(spot, "y", CURSOR_HOTSPOT_Y) or 0.0)
            return (image, (hx, hy))
        except Exception:
            logger.debug("overlay: arrow glyph probe failed", exc_info=True)
            return (None, (self._hotspot_x, self._hotspot_y))

    # ── drawing ──

    def show(self) -> None:
        """Order the window in front without activating this application.

        ``orderFrontRegardless`` (not ``makeKeyAndOrderFront:``) is deliberate: the
        latter would activate the overlay process and pull focus away from the
        application the agent is driving, which would change that app's behaviour —
        a cosmetic feature must not do that.
        """
        if not self.ensure():
            return
        try:
            self._rt.msg(self._window, self._rt.sel("orderFrontRegardless"), None, [])
            self._rt.msg(self._window, self._rt.sel("setAlphaValue:"), None, [ctypes.c_double], 1.0)
            self._visible = True
        except Exception:
            logger.debug("overlay: show failed", exc_info=True)

    def hide(self) -> None:
        """Order the window out. Safe to call when nothing was ever created."""
        if self._window is None:
            self._visible = False
            return
        try:
            self._rt.msg(self._window, self._rt.sel("orderOut:"), None, [ctypes.c_void_p], None)
        except Exception:
            logger.debug("overlay: hide failed", exc_info=True)
        self._visible = False

    def close(self) -> None:
        """Hide and release the window — the teardown path on EOF/quit."""
        self.hide()
        if self._window is None:
            return
        try:
            self._rt.msg(self._window, self._rt.sel("close"), None, [])
        except Exception:
            logger.debug("overlay: close failed", exc_info=True)
        self._window = None

    def move_along(self, points: Sequence[tuple[float, float]], duration_ms: int) -> None:
        """Animate the tip along *points* over *duration_ms*, pumping the run loop.

        Frame pacing is by WALL CLOCK against the requested duration, not by
        "one point per pumped frame": the point list is a shape, not a schedule, so
        a slow frame skips ahead rather than stretching the animation. The tip is
        always placed at the LAST point before returning, so a skipped frame can
        never leave the cursor short of the target the caller asked for.
        """
        if not points:
            return
        if not self.ensure():
            return
        self.show()
        duration = min(max(int(duration_ms), MIN_MOVE_DURATION_MS), MAX_MOVE_DURATION_MS) / 1000.0
        started = time.monotonic()
        last = len(points) - 1
        while True:
            elapsed = time.monotonic() - started
            progress = 1.0 if duration <= 0.0 else min(max(elapsed / duration, 0.0), 1.0)
            index = min(int(round(progress * last)), last)
            self._place(points[index][0], points[index][1])
            if progress >= 1.0:
                break
            self.pump(OVERLAY_FRAME_SLICE_SECS)
        self._place(points[last][0], points[last][1])
        self.pump(OVERLAY_FRAME_SLICE_SECS)

    def pulse_click(self, x: float, y: float, count: int) -> None:
        """Draw *count* click pulses at the given TOP-LEFT point.

        One sine half-period of alpha dip per click. Alpha (rather than a scale
        transform) because scaling the window would move its content rect and
        therefore its tip anchor, so the "click" would visibly drift off the
        element it is announcing.
        """
        if not self.ensure():
            return
        self.show()
        self._place(x, y)
        pulses = min(max(int(count), 1), MAX_CLICK_COUNT)
        duration = CLICK_PULSE_MS / 1000.0
        for pulse in range(pulses):
            started = time.monotonic()
            progress = 0.0
            while True:
                if duration <= 0.0:
                    target = 1.0
                else:
                    target = min(max((time.monotonic() - started) / duration, 0.0), 1.0)
                # Follow the clock, but never let one frame jump the whole pulse:
                # alpha is `sin(progress * pi)`, which is 0 at progress 0.0 AND
                # 1.0, so a stall spanning the pulse would draw alpha 1.0 twice
                # and the dip that IS the click would never reach the screen.
                # Capping the advance keeps the peak on screen on a loaded
                # machine, at a floor of ceil(1 / CLICK_PULSE_MAX_STEP) frames.
                progress = target if duration <= 0.0 else min(
                    target, progress + CLICK_PULSE_MAX_STEP
                )
                self._set_alpha(1.0 - CLICK_PULSE_DEPTH * math.sin(progress * math.pi))
                if progress >= 1.0:
                    break
                self.pump(OVERLAY_FRAME_SLICE_SECS)
            self._set_alpha(1.0)
            if pulse < pulses - 1:
                self.pump(CLICK_PULSE_GAP_MS / 1000.0)

    def pump(self, seconds: float) -> None:
        """Run the AppKit run loop for *seconds*.

        Manual pumping rather than ``NSApp run``: that call never returns, and this
        process must stay in control of its own stdin-reading loop so the EOF exit
        (the anti-orphan-window guarantee) keeps working.
        """
        rt = self._rt
        try:
            run_loop = rt.msg(rt.cls("NSRunLoop"), rt.sel("currentRunLoop"), ctypes.c_void_p, [])
            date = rt.msg(
                rt.cls("NSDate"),
                rt.sel("dateWithTimeIntervalSinceNow:"),
                ctypes.c_void_p,
                [ctypes.c_double],
                float(max(seconds, 0.0)),
            )
            mode = rt.msg(
                rt.cls("NSString"),
                rt.sel("stringWithUTF8String:"),
                ctypes.c_void_p,
                [ctypes.c_char_p],
                _RUNLOOP_MODE,
            )
            rt.msg(
                run_loop,
                rt.sel("runMode:beforeDate:"),
                ctypes.c_bool,
                [ctypes.c_void_p, ctypes.c_void_p],
                mode,
                date,
            )
        except Exception:
            logger.debug("overlay: run-loop pump failed", exc_info=True)
            # Still yield the CPU, so a broken pump cannot turn into a spin.
            time.sleep(max(seconds, 0.0))

    # ── internals ──

    def _place(self, x_top: float, y_top: float) -> None:
        """Place the glyph so its TIP lands on the given top-left point.

        Two conversions happen here and only here:

        * TOP-LEFT to BOTTOM-LEFT (``y_bottom = screen_height - y_top``), because
          every other computer-use module speaks top-left and ``NSWindow``'s origin
          is bottom-left;
        * tip to window ORIGIN, by subtracting the glyph's hot spot. The hot spot
          arrives in top-left glyph coordinates, so the vertical component is
          measured from the glyph's top edge and the window origin sits
          ``glyph_height - hotspot_y`` below the tip.
        """
        if self._window is None:
            return
        px, py = self._clamp(x_top, y_top)
        origin_x = px - self._hotspot_x
        origin_y = (self._screen_height - py) - (self._glyph_height - self._hotspot_y)
        if not (math.isfinite(origin_x) and math.isfinite(origin_y)):
            # A NaN reaches AppKit as an un-placeable window rather than an error;
            # dropping the frame is the honest behaviour.
            return
        try:
            self._rt.msg(
                self._window,
                self._rt.sel("setFrameOrigin:"),
                None,
                [CGPoint],
                CGPoint(origin_x, origin_y),
            )
        except Exception:
            logger.debug("overlay: place failed", exc_info=True)

    def _set_alpha(self, alpha: float) -> None:
        """Set window alpha, clamped to ``[0, 1]``."""
        if self._window is None:
            return
        value = min(max(float(alpha), 0.0), 1.0)
        try:
            self._rt.msg(
                self._window, self._rt.sel("setAlphaValue:"), None, [ctypes.c_double], value
            )
        except Exception:
            logger.debug("overlay: alpha failed", exc_info=True)

    def _clamp(self, x_top: float, y_top: float) -> tuple[float, float]:
        """Keep a point on the measured main display.

        Deliberately forgiving about a non-finite input (mapped to the origin
        corner) — a cosmetic renderer must not raise on a malformed command, and
        the supervisor already validated the shape.
        """
        x = float(x_top) if math.isfinite(x_top) else 0.0
        y = float(y_top) if math.isfinite(y_top) else 0.0
        max_x = max(self._screen_width - 1.0, 0.0)
        max_y = max(self._screen_height - 1.0, 0.0)
        return (min(max(x, 0.0), max_x), min(max(y, 0.0), max_y))

    @property
    def visible(self) -> bool:
        """Whether the window is currently ordered in."""
        return self._visible


def parse_command(line: str) -> "dict[str, Any] | None":
    """Parse one NDJSON command line, or ``None`` if it is not usable.

    Tolerant by design: this process's stdin is written by our own supervisor, but
    a truncated write during a gateway crash is a real event and a malformed line
    must be skipped rather than kill the renderer (which would leave a cursor on
    screen — the exact failure the EOF exit exists to prevent).
    """
    text = line.strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        logger.debug("overlay: unparseable command line")
        return None
    if not isinstance(payload, dict):
        return None
    kind = payload.get(OVERLAY_CMD_KEY)
    if not isinstance(kind, str) or not kind:
        return None
    return payload


def _coerce_points(raw: Any) -> tuple[tuple[float, float], ...]:
    """Coerce a command's ``points`` payload into finite float pairs.

    Every non-conforming entry is DROPPED rather than defaulted: a point silently
    replaced by ``(0, 0)`` would fling the visible cursor to the corner of the
    screen mid-animation, which is worse than a shorter path.
    """
    if not isinstance(raw, list):
        return ()
    out: list[tuple[float, float]] = []
    for item in raw:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            first, second = item[0], item[1]
        elif isinstance(item, dict):
            first, second = item.get(OVERLAY_KEY_X), item.get(OVERLAY_KEY_Y)
        else:
            continue
        if isinstance(first, bool) or isinstance(second, bool):
            continue
        if not isinstance(first, (int, float)) or not isinstance(second, (int, float)):
            continue
        x, y = float(first), float(second)
        if not (math.isfinite(x) and math.isfinite(y)):
            continue
        out.append((x, y))
    return tuple(out)


def _coerce_number(raw: Any, default: float) -> float:
    """A finite float from *raw*, else *default* (``bool`` is not a number here)."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return default
    value = float(raw)
    return value if math.isfinite(value) else default


def read_commands(stream: Any) -> "Any":
    """Yield parsed commands from *stream* until EOF.

    Written as an explicit ``readline`` loop rather than ``for line in stream``:
    file iteration buffers, so a command could sit unread until the buffer filled —
    which for an animation means arriving after it was relevant. ``readline``
    returning ``""`` is EOF, and EOF is the signal that the parent is gone.
    """
    while True:
        try:
            line = stream.readline()
        except (OSError, ValueError):
            # ValueError: the stream was closed under us during shutdown.
            return
        if not line:
            return
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="replace")
        command = parse_command(line)
        if command is not None:
            yield command


def _handle(window: CursorOverlayWindow, command: "dict[str, Any]") -> bool:
    """Apply one command. Returns False when the renderer should exit."""
    kind = command.get(OVERLAY_CMD_KEY)
    if kind == OVERLAY_CMD_QUIT:
        return False
    if kind == OVERLAY_CMD_HIDE:
        window.hide()
        return True
    if kind == OVERLAY_CMD_MOVE:
        points = _coerce_points(command.get(OVERLAY_KEY_POINTS))
        if not points:
            # A bare {"type":"move","x":..,"y":..} is a legal one-point move: the
            # supervisor uses it to park the cursor with no animation.
            x = command.get(OVERLAY_KEY_X)
            y = command.get(OVERLAY_KEY_Y)
            if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                if not isinstance(x, bool) and not isinstance(y, bool):
                    points = _coerce_points([[x, y]])
        if points:
            duration = int(_coerce_number(command.get(OVERLAY_KEY_MS), MIN_MOVE_DURATION_MS))
            window.move_along(points, duration)
        return True
    if kind == OVERLAY_CMD_CLICK:
        x = _coerce_number(command.get(OVERLAY_KEY_X), math.nan)
        y = _coerce_number(command.get(OVERLAY_KEY_Y), math.nan)
        count = int(_coerce_number(command.get(OVERLAY_KEY_COUNT), 1.0))
        if math.isfinite(x) and math.isfinite(y):
            window.pulse_click(x, y, count)
        return True
    logger.debug("overlay: ignoring unknown command %r", kind)
    return True


def _idle_pump(
    window: CursorOverlayWindow, stop: threading.Event, last_seen: "list[float]"
) -> None:
    """Keep the run loop alive and auto-hide after an idle period.

    Runs on the READER thread's counterpart: the main thread owns AppKit, so this
    helper is called FROM the main thread between commands. ``last_seen`` is a
    one-element list rather than a scalar so the caller and this function share one
    mutable cell without a class wrapper for two lines of state.
    """
    if stop.is_set():
        return
    window.pump(OVERLAY_IDLE_SLICE_SECS)
    if window.visible and (time.monotonic() - last_seen[0]) >= OVERLAY_IDLE_HIDE_SECS:
        # Backstop against a parent that stopped writing without closing the pipe.
        # The primary anti-orphan guarantee is the EOF exit in ``main``.
        window.hide()


def main(argv: "Sequence[str] | None" = None) -> int:
    """Entry point: pump AppKit on the main thread, read stdin on a worker.

    The thread split is forced by AppKit: the run loop MUST be on the main thread,
    and ``readline`` blocks. So stdin is read on a daemon thread that hands parsed
    commands to the main thread through a lock-guarded list, and the main thread
    alternates between draining that list and pumping the run loop.

    Returns 0 on every path — including "not macOS" and "AppKit unavailable". A
    non-zero exit would make the supervisor log a child failure for a cosmetic
    subsystem that correctly declined to run.
    """
    del argv  # No options: every parameter arrives on stdin.
    if not platform_compat.IS_MACOS:
        # Not an error: the supervisor already declines to spawn off macOS, and a
        # user running the module by hand deserves a plain answer.
        sys.stderr.write("cursor overlay is macOS-only; nothing to do\n")
        return 0
    try:
        runtime = ObjCRuntime()
    except Exception:
        logger.debug("overlay: ObjC runtime unavailable", exc_info=True)
        sys.stderr.write("cursor overlay: AppKit unavailable\n")
        return 0

    window = CursorOverlayWindow(runtime)
    pending: list[dict[str, Any]] = []
    lock = threading.Lock()
    stop = threading.Event()

    def _reader() -> None:
        try:
            for command in read_commands(sys.stdin):
                with lock:
                    pending.append(command)
        finally:
            # EOF (or a closed stream) means the parent is gone — THE anti-orphan
            # guarantee. Setting the event is what makes the main loop tear the
            # window down and return.
            stop.set()

    thread = threading.Thread(target=_reader, name="overlay-stdin", daemon=True)
    thread.start()

    # Announce readiness only after the window exists, so the supervisor can
    # distinguish "spawned" from "actually able to draw".
    ready = window.ensure()
    try:
        sys.stdout.write(f"{OVERLAY_READY_LINE} {1 if ready else 0}\n")
        sys.stdout.flush()
    except (OSError, ValueError):
        logger.debug("overlay: ready line write failed", exc_info=True)

    last_seen = [time.monotonic()]
    try:
        while True:
            with lock:
                batch = pending[:]
                del pending[:]
            if batch:
                last_seen[0] = time.monotonic()
            for command in batch:
                if not _handle(window, command):
                    return 0
            if not batch:
                if stop.is_set():
                    return 0
                _idle_pump(window, stop, last_seen)
    finally:
        # Always tear the window down: an orphan cursor on the user's screen is the
        # single worst failure this process can produce.
        window.close()
        stop.set()


if __name__ == "__main__":  # pragma: no cover - process entry point
    logging.basicConfig(level=os.environ.get("KIROCREW_LOG_LEVEL", "WARNING"))
    sys.exit(main(sys.argv[1:]))
