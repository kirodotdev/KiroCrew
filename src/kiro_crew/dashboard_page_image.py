"""A picture of an agent-written dashboard page, taken by the offline page run.

The page run in :mod:`kiro_crew.dashboard_verify` already loads a crewmate's page in a
headless browser to collect its script errors. A crewmate has no other way to look at the
page it wrote, so the same run can also hand back a PNG of the first paint: what the
browser drew over the run's synthetic copy of the record, at the run's own viewport.

What bounds it
--------------
* Its size in pixels is the run's viewport (1280 by 800 at one device pixel per CSS pixel),
  captured without the area below the fold, so the image costs the agent the same number of
  tokens whatever the page holds.
* Its size in bytes is :data:`MAX_IMAGE_BYTES`. A busier picture is taken again at half
  scale, once; one still over the cap is not kept, and the reason says so.
* The DevTools reply that carries it may be larger than the run's ordinary message cap, up
  to :data:`MAX_REPLY_BYTES`, which is above the largest PNG a 1280 by 800 viewport can
  produce once it is base64-encoded.

Nothing here talks to the browser itself: :func:`capture` is handed the call that sends
``Page.captureScreenshot`` and returns its result, so the run keeps its one pipe, its
deadline and its error handling.
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
from collections.abc import Callable, Mapping
from typing import Any, Final

__all__ = [
    "MAX_IMAGE_BYTES",
    "MAX_REPLY_BYTES",
    "PNG_SIGNATURE",
    "PageImage",
    "capture",
    "png_size",
]

#: The largest PNG kept, in bytes. A 1280 by 800 page of flat colour and text is a few
#: hundred KiB; a page covered in noise or photographs can be several MiB.
MAX_IMAGE_BYTES: Final[int] = 2 * 1024 * 1024

#: The largest DevTools reply the capture accepts. A 1280 by 800 PNG is at most about
#: 4 MB (four bytes a pixel and a little framing), 5.5 MB once base64-encoded.
MAX_REPLY_BYTES: Final[int] = 8 * 1024 * 1024

#: The scale of the second picture, taken when the first is over :data:`MAX_IMAGE_BYTES`.
SMALLER_SCALE: Final[float] = 0.5

#: The eight bytes every PNG file starts with.
PNG_SIGNATURE: Final[bytes] = b"\x89PNG\r\n\x1a\n"

#: Base64 characters that decode to at most :data:`MAX_IMAGE_BYTES`, so a reply that
#: cannot fit is turned away before it is decoded.
_MAX_ENCODED_CHARS: Final[int] = (MAX_IMAGE_BYTES + 2) // 3 * 4


@dataclasses.dataclass(frozen=True)
class PageImage:
    """A picture of the page, or why there is none.

    ``png`` is empty exactly when ``reason`` is set.
    """

    png: bytes = b""
    width: int = 0
    height: int = 0
    reason: str = ""


def png_size(data: bytes) -> tuple[int, int] | None:
    """``(width, height)`` from a PNG's header, or ``None`` when *data* is not a PNG.

    Read from the IHDR chunk, which the format requires to come first, so nothing is
    decoded.
    """
    if len(data) < 24 or not data.startswith(PNG_SIGNATURE) or data[12:16] != b"IHDR":
        return None
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    if width <= 0 or height <= 0:
        return None
    return width, height


def capture(
    call: Callable[[Mapping[str, Any]], Mapping[str, Any]], viewport: tuple[int, int]
) -> PageImage:
    """Take a PNG of the viewport, at full scale and if needed once more at half.

    *call* sends ``Page.captureScreenshot`` with the parameters it is given and returns the
    reply's ``result``. The exceptions it raises are the caller's to answer: they say the
    browser went away or the run ran out of time, which only the caller can word.
    """
    width, height = viewport
    for scale in (1.0, SMALLER_SCALE):
        params: dict[str, Any] = {
            "format": "png",
            "fromSurface": True,
            "captureBeyondViewport": False,
        }
        if scale != 1.0:
            params["clip"] = {"x": 0, "y": 0, "width": width, "height": height, "scale": scale}
        data = call(params).get("data")
        if not isinstance(data, str) or not data:
            return PageImage(reason="the browser returned no picture of the page")
        if len(data) > _MAX_ENCODED_CHARS:
            continue
        try:
            png = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            return PageImage(reason="the browser returned a picture that could not be read")
        size = png_size(png)
        if size is None:
            return PageImage(reason="the browser returned a picture that is not a PNG")
        if size[0] > width or size[1] > height:
            # Larger than the viewport it was asked for: not the picture that was asked
            # for, and not one whose cost is known.
            return PageImage(reason="the browser returned a picture larger than the page view")
        if len(png) > MAX_IMAGE_BYTES:
            continue
        return PageImage(png, size[0], size[1])
    return PageImage(
        reason=(
            f"the picture was over {MAX_IMAGE_BYTES // (1024 * 1024)} MiB even at half size, "
            "so it was not kept"
        )
    )
