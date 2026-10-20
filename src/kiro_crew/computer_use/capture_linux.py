"""Linux window capture: X11 root pixels for one window, encoded as JPEG.

The counterpart of :mod:`capture_windows` / :mod:`capture_macos`, and it makes
the same trade: the accessibility tree is the primary channel and the image is
corroboration, so a capture failure degrades the result rather than failing the
observation.

**No new dependency and no child process.** ``XGetImage`` on the root grabs the
window's rect and ``gdk-pixbuf`` (``gdk_pixbuf_save_to_buffer``) encodes it to
JPEG — the Linux analogue of GDI+ on Windows and ImageIO on macOS. Pillow is
declared in neither ``setup.cfg`` nor ``pyproject.toml`` and must not be imported;
spawning an external encoder (``import`` / ``scrot``) would need a spawn-audit
entry and a runtime dependency to degrade around. All native work goes through
:mod:`linux_ffi`.

**Honest limitation, stated because a reviewer will find it.** ``XGetImage`` reads
the ROOT window over the target's rectangle, so an OVERLAPPING window shows
through — the captured frame contains whatever pixels are actually on screen in
that rect, which may belong to a window on top. Windows' ``PrintWindow`` renders
the target's own backing store and does not have this property; the macOS
SkyLight path captures the window directly. This first cut ACCEPTS the limitation:
the tree is the primary channel, the capture is per-window (never full-screen),
and a window holding any secure field is not captured at all. It is not claimed to
be closed. (The alternative — compositing the window's own pixmap via the
Composite extension — is a later cut, and requires the window to be redirected,
which it is not by default under this session.)

The spool PATH, the owner-only directory, the ring trim and the per-file
owner-only tightening are all SHARED with :mod:`capture_macos` — ``shots_dir``,
``persist_jpeg`` and ``trim_shots_dir`` there are platform-free (``shots_dir``
resolves through ``tempfile.gettempdir()``, which is ``$TMPDIR`` on this host),
so this module reuses them rather than forking a second copy that could drift in
how a frame of the operator's screen is protected.
"""

from __future__ import annotations

import logging
from dataclasses import replace

from kiro_crew.computer_use import apps_linux, linux_ffi, screencast
from kiro_crew.computer_use.capture_macos import persist_jpeg, shots_dir, trim_shots_dir
from kiro_crew.computer_use.types import (
    DEFAULT_SCREENSHOT_JPEG_QUALITY,
    DEFAULT_SCREENSHOT_MAX_PX,
    MAX_SCREENSHOT_MAX_PX,
    MIN_SCREENSHOT_MAX_PX,
    Snapshot,
)

logger = logging.getLogger(__name__)

#: A frame whose pixels are all identical is a failed capture, not a screenshot.
#: ``XGetImage`` of a withdrawn or just-unmapped window can hand back a uniform
#: buffer, and relaying it would give the model a blank rectangle it would then
#: reason about as if it were the window — the same "worse than no image" hazard
#: the Windows and macOS captures guard against. Mirrors
#: ``capture_windows._MIN_DISTINCT_PIXELS``.
_MIN_DISTINCT_PIXELS = 2


def ensure_shots_dir() -> str:
    """Create the screenshot directory owner-only, reusing the macOS primitive.

    ``capture_macos.ensure_shots_dir`` makes the dir ``0o700`` and re-asserts the
    mode through ``chmod_safe`` — correct on POSIX, where this driver runs, so
    there is nothing Linux-specific to add. Imported lazily inside the function so
    the module still imports with no native libraries present.
    """
    from kiro_crew.computer_use.capture_macos import ensure_shots_dir as _ensure

    return _ensure()


def _has_content(rgb: bytes) -> bool:
    """Whether a captured RGB buffer holds more than one distinct pixel.

    ``x_get_image_rgb`` returns packed 24-bit RGB (3 bytes per pixel), so a pixel
    boundary is every third byte and there is no alpha lane or scanline padding to
    exclude — simpler than the Windows DIB case. A uniform buffer is a failed
    capture (a withdrawn window, an unmapped surface) and must not be relayed.

    The sample is spread ACROSS the whole buffer with ~4096 evenly-spaced probes,
    not taken from the head, so a solid title bar over varied content does not read
    as blank and a present caption over an absent body does not read as content.
    """
    if len(rgb) < 6:
        return False
    total_pixels = len(rgb) // 3
    step = max(1, total_pixels // 4096)
    seen = set()
    for n in range(0, total_pixels, step):
        offset = n * 3
        seen.add(rgb[offset : offset + 3])
        if len(seen) >= _MIN_DISTINCT_PIXELS:
            return True
    return False


def _clip_to_screen(
    geom: "tuple[int, int, int, int]", screen: "tuple[int, int]"
) -> "tuple[int, int, int, int] | None":
    """Clip a window rect to the virtual screen bounds, or ``None`` if off-screen.

    ``XGetImage`` of a rect that extends past the root pixmap raises a ``BadMatch``,
    so a window partly off the virtual screen (routine on a multi-output desktop)
    must be clipped to the visible region before the grab. A rect entirely outside
    the screen yields ``None`` — there is nothing to capture.
    """
    x, y, w, h = geom
    sw, sh = screen
    left = max(0, x)
    top = max(0, y)
    right = min(sw, x + w)
    bottom = min(sh, y + h)
    if right <= left or bottom <= top:
        return None
    return (left, top, right - left, bottom - top)


def capture_snapshot_image(
    snap: Snapshot,
    *,
    max_px: int = DEFAULT_SCREENSHOT_MAX_PX,
    quality: int = DEFAULT_SCREENSHOT_JPEG_QUALITY,
) -> Snapshot:
    """Capture *snap*'s window, persist the JPEG, and return an updated snapshot.

    Returns the snapshot UNCHANGED (no image) when any of these holds, in the SAME
    ORDER as ``capture_windows`` / ``capture_macos`` — the first two are the
    always-on floors, not failure handling:

    * **any element is secure** — a password field's rendered glyphs are a
      credential even though the tree redacted its value, and there is no reliable
      way to blank a sub-rectangle of an encoded JPEG, so suppression is
      whole-window;
    * **the walk was truncated or depth-truncated** — ``has_secure`` then means
      "none seen", NOT "none present", so a password field beyond the cut would
      leave rendered credentials capturable;
    * the window id is unknown, the window is not viewable, geometry is gone, the
      capture produced no usable pixels, the encode failed, or persisting failed.

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
        if not linux_ffi.x_window_is_viewable(snap.app.window_id):
            # A minimized/unmapped window has no on-screen pixels. The tree is
            # still returned (an unmapped window's AT-SPI tree reads fine); the
            # debug log names the actual cause so a blank-frame rejection with no
            # reason cannot happen downstream.
            logger.debug(
                "computer-use capture skipped: window 0x%X is not viewable",
                snap.app.window_id,
            )
            return snap
    except Exception:
        # Never fatal: an unreadable window state degrades to attempting the
        # capture rather than failing the read.
        logger.debug("viewable probe failed", exc_info=True)

    try:
        # The CONTENT rect (CSD shadow margins removed), the same rect the walk
        # published as ``window_bounds`` and measured element frames against — so a
        # pixel in the image and a window-local frame coordinate are the same number.
        # Falls back to the raw X geometry only when the walk did not publish bounds.
        geom = snap.window_bounds or apps_linux.window_bounds(snap.app)
        if geom is None:
            return snap
        screen = linux_ffi.x_screen_size()
        clipped = _clip_to_screen((int(geom[0]), int(geom[1]), int(geom[2]), int(geom[3])), screen)
        if clipped is None:
            return snap
        cx, cy, cw, ch = clipped
        grabbed = linux_ffi.x_get_image_rgb(cx, cy, cw, ch)
        if grabbed is None:
            return snap
        rgb, width, height = grabbed
        if not _has_content(rgb):
            logger.debug(
                "computer-use capture of window 0x%X produced a blank frame",
                snap.app.window_id,
            )
            return snap
        clamped_px = max(MIN_SCREENSHOT_MAX_PX, min(int(max_px), MAX_SCREENSHOT_MAX_PX))
        clamped_quality = max(1, min(int(quality), 100))
        encoded = linux_ffi.encode_jpeg_rgb(
            rgb, width, height, max_px=clamped_px, quality=clamped_quality
        )
        if encoded is None:
            return snap
        raw, out_w, out_h = encoded
    except Exception:
        logger.debug("computer-use window capture failed", exc_info=True)
        return snap
    if not raw:
        return snap
    path = persist_jpeg(raw)
    if not path:
        return snap
    # ``out_w``/``out_h`` are the ACTUAL encoded dimensions (gdk-pixbuf reports what
    # it scaled to), so a model never reads a downscaled frame as full-size.
    # ``dataclasses.replace`` rather than a field-by-field rebuild, for the reason
    # ``capture_macos`` gives: enumerating fields silently drops every field added
    # to ``Snapshot`` later, and only on responses that also carry an image.
    attached = replace(
        snap,
        image_jpeg=raw,
        image_path=path,
        image_width=out_w,
        image_height=out_h,
    )
    # Mirror to the dashboard live view, as the other two captures do — the relay
    # owns all three suppressions and relays these exact already-downscaled bytes
    # rather than capturing again. Wrapped even though the relay is itself
    # contracted never to raise: THIS function's contract is "never raises", and a
    # decorative mirror must not turn a successful observation into a failed call.
    try:
        screencast.emit_snapshot_frame(attached)
    except Exception:
        logger.debug("computer-use live-view relay failed", exc_info=True)
    return attached


__all__ = [
    "capture_snapshot_image",
    "ensure_shots_dir",
    "persist_jpeg",
    "shots_dir",
    "trim_shots_dir",
]
