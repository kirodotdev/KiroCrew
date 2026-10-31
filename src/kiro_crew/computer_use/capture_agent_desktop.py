"""Window screenshots through agent-desktop's own ``screenshot`` command.

Used where no native capture exists (Linux). agent-desktop returns the window as a
base64 PNG; this module re-encodes it as the downscaled JPEG every other capture
path produces and persists it through ``capture_macos.persist_jpeg``, which is
platform-free (it writes into ``tempfile.gettempdir()``) and owns the owner-only
file mode and the ring trim. Reusing it keeps one spool, one mode and one trim
rule on every platform.

The floors are the native capture's, in the same order: no capture when the walk
found a secure element, when it was cut short (``truncated`` or
``depth_truncated``, so "none seen" is not mistaken for "none present"), or when
the window id is unknown. Every failure returns the snapshot unchanged, because
the tree is the primary channel and a missing image must not fail the call.
"""

from __future__ import annotations

import base64
import binascii
import io
import logging
from dataclasses import replace
from typing import Callable

from kiro_crew.computer_use.types import (
    MAX_SCREENSHOT_MAX_PX,
    MIN_SCREENSHOT_MAX_PX,
    Snapshot,
)

logger = logging.getLogger(__name__)

#: A decoded PNG larger than this is refused rather than handed to Pillow. A 4K
#: window is about 25 MB of RGBA; this leaves room for that and no more.
_MAX_PNG_BYTES = 64 * 1024 * 1024

#: Returns agent-desktop's ``screenshot`` ``data`` for the snapshot's window.
Shooter = Callable[[], object]


def capture_snapshot_image(
    snap: Snapshot, shoot: Shooter, *, max_px: int, quality: int
) -> Snapshot:
    """Attach a JPEG of *snap*'s window taken through *shoot*; never raises."""
    if snap.has_secure or snap.truncated or snap.depth_truncated:
        return snap
    if snap.app.window_id <= 0:
        return snap
    try:
        png = _png_bytes(shoot())
        if not png:
            return snap
        raw, width, height = _to_jpeg(png, max_px=max_px, quality=quality)
    except Exception:
        logger.debug("agent-desktop capture failed for %s", snap.app.label, exc_info=True)
        return snap
    if not raw:
        return snap
    from kiro_crew.computer_use import capture_macos, screencast

    path = capture_macos.persist_jpeg(raw)
    if not path:
        return snap
    captured = replace(
        snap, image_jpeg=raw, image_path=path, image_width=width, image_height=height
    )
    try:
        screencast.emit_snapshot_frame(captured)
    except Exception:
        logger.debug("computer-use live-view relay failed", exc_info=True)
    return captured


def _png_bytes(data: object) -> bytes:
    """The PNG bytes in a ``screenshot`` envelope's ``data``, or ``b""``."""
    if not isinstance(data, dict) or data.get("format") != "png":
        return b""
    encoded = data.get("data")
    if not isinstance(encoded, str) or len(encoded) > _MAX_PNG_BYTES * 4 // 3 + 4:
        return b""
    try:
        return base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return b""


def _to_jpeg(png: bytes, *, max_px: int, quality: int) -> "tuple[bytes, int, int]":
    """Downscale *png* to a *max_px* long edge and encode it as JPEG."""
    from PIL import Image

    edge = max(MIN_SCREENSHOT_MAX_PX, min(int(max_px), MAX_SCREENSHOT_MAX_PX))
    with Image.open(io.BytesIO(png)) as image:
        image.load()
        rgb = image.convert("RGB")
    rgb.thumbnail((edge, edge))
    out = io.BytesIO()
    rgb.save(out, format="JPEG", quality=max(1, min(int(quality), 100)))
    return out.getvalue(), rgb.width, rgb.height


__all__ = ["capture_snapshot_image"]
