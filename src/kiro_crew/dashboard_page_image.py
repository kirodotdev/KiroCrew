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

The page's text
---------------
Right after the picture the run also reads the page's rendered text, the whole page and
not the first screen only (:func:`read_text`): ``document.body.innerText`` once each
field the frame marked "not current" or "may be out of date" has that mark written after
it as text, since the frame draws the marks with CSS, which ``innerText`` leaves out. The
document is the run's throwaway copy and the picture is already taken, so the write
changes neither the picture nor a person's tab. At most :data:`MAX_TEXT_CHARS` characters
are kept.

Nothing here talks to the browser itself: :func:`capture` is handed the call that sends
``Page.captureScreenshot`` and :func:`read_text` the one that sends ``Runtime.evaluate``,
each returning its result, so the run keeps its one pipe, its deadline and its error
handling.
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
    "MAX_TEXT_CHARS",
    "PNG_SIGNATURE",
    "PageImage",
    "PageText",
    "TEXT_EXPRESSION",
    "capture",
    "png_size",
    "read_text",
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

#: The most characters of the page's text kept: a page's worth of values and labels,
#: a bounded cost to the agent whatever the page holds. Characters are code points on
#: both sides (``Array.from`` in the page, ``len`` here), so an emoji counts once.
MAX_TEXT_CHARS: Final[int] = 6000

#: The one expression :func:`read_text` evaluates in the page. It writes each frame mark
#: after its field as text (a field marked missing is never also marked out of date), then
#: returns the body's rendered text cut to the cap and its full length.
TEXT_EXPRESSION: Final[str] = (
    "(() => {"
    "for (const node of document.querySelectorAll("
    '\'[data-dashboard-missing="true"], [data-dashboard-out-of-date="true"]\')) {'
    "const label = node.getAttribute('data-dashboard-missing') === 'true'"
    " ? node.getAttribute('data-dashboard-missing-label')"
    " : node.getAttribute('data-dashboard-out-of-date-label');"
    "node.appendChild(document.createTextNode(' [' + (label || '') + ']'));"
    "}"
    "const chars = Array.from(document.body ? document.body.innerText : '');"
    f"return {{text: chars.slice(0, {MAX_TEXT_CHARS}).join(''), length: chars.length}};"
    "})()"
)

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


@dataclasses.dataclass(frozen=True)
class PageText:
    """The page's rendered text, at most :data:`MAX_TEXT_CHARS` characters of it, and the
    full text's length in characters (over :data:`MAX_TEXT_CHARS` exactly when it was cut)."""

    text: str
    length: int


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


def read_text(call: Callable[[Mapping[str, Any]], Mapping[str, Any]]) -> PageText | None:
    """The page's rendered text with the frame's marks, or ``None`` when the page gave none.

    *call* sends ``Runtime.evaluate`` with the parameters it is given and returns the
    reply's ``result``; its exceptions are the caller's, as for :func:`capture`. The page's
    script ran before this and can have replaced anything the expression uses, so the
    reply is checked here and not trusted: cut to the cap again, a ``length`` that is not
    a whole number at least as large as the text replaced by the text's own, and lone
    surrogates, which no UTF-8 reader takes, dropped.
    """
    reply = call({"expression": TEXT_EXPRESSION, "returnByValue": True})
    if reply.get("exceptionDetails"):
        return None
    result = reply.get("result")
    value = result.get("value") if isinstance(result, Mapping) else None
    if not isinstance(value, Mapping) or not isinstance(value.get("text"), str):
        return None
    text = value["text"][:MAX_TEXT_CHARS].encode("utf-8", "ignore").decode("utf-8")
    length = value.get("length")
    if not isinstance(length, int) or isinstance(length, bool) or length < len(text):
        length = len(text)
    return PageText(text, length)
