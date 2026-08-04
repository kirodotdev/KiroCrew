"""Build ACP ``session/prompt`` content blocks from a plain message string.

Channels hand the provider ONE string. When that string contains an absolute
path to a readable image, the image must travel as a real ACP image block --
a bare path is just text, and the model cannot see it. This module owns that
conversion so both prompt paths share one implementation:

* :meth:`kiro_crew.acp.session_handle.AcpSessionHandle.prompt` -- the live path
  for the public Kiro backend (``AcpProvider.start`` swaps ``AcpClient`` out for
  ``AcpSessionProvider``, so this is what actually reaches kiro-cli).
* :meth:`kiro_crew.acp.client.AcpClient._send_prompt` -- the direct-client path.

Keeping one builder matters: both paths need the same path-to-image
conversion, so a single implementation stops any channel from shipping a
filesystem path to the model as text.

Wire shape (per docs/kiro-cli/acp.md):

.. code-block:: json

    {"sessionId": "...", "prompt": [
        {"type": "text",  "text": "look at this [image: shot.png]"},
        {"type": "image", "data": "<base64>", "mimeType": "image/png"}
    ]}
"""

from __future__ import annotations

import base64
import io
import logging
import os
import re
from pathlib import Path

from kiro_crew.hooks import safe_read_file_bytes

try:
    from PIL import Image, ImageOps
    _HAS_PIL = True
except ImportError:  # pragma: no cover - Pillow ships via qrcode[pil]/pdfplumber
    _HAS_PIL = False

logger = logging.getLogger(__name__)

#: Raster formats kiro-cli accepts as inline vision input. SVG is deliberately
#: absent: it is scriptable XML rather than a raster image, and a vision model
#: gains nothing from it.
IMAGE_MEDIA_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}

#: Raw bytes per image, checked BEFORE base64. Encoding inflates by 4/3 and the
#: whole request is serialized as a single newline-delimited JSON frame, so an
#: unbounded image becomes an unbounded write. Matches the Slack producer cap so
#: a file that passed ingestion is not silently dropped here.
MAX_IMAGE_BYTES = 10 * 1024 * 1024

#: Longest-edge cap (px) enforced on every inlined image. Anthropic rejects the
#: ENTIRE request when a many-image conversation (more than 20 images) carries
#: any image whose width or height exceeds 2000px. Because kiro-cli replays the
#: full message history to the model every turn, a single oversized image
#: permanently wedges the session -- the offending block sits at a fixed history
#: index and re-uploading a smaller copy cannot evict it. This builder is the
#: one funnel every channel's images cross before reaching kiro-cli, so capping
#: here protects all of them (dashboard upload/paste/screenshot, Slack, Discord)
#: regardless of any best-effort client-side resize. 2000 is the hard limit
#: itself: valid even past 20 images, yet it keeps more detail than the browser's
#: 1568px pre-upload downscale, which only ever covers dashboard uploads.
MAX_IMAGE_EDGE_PX = 2000

#: mime -> Pillow save format for a re-encoded downscale. GIF collapses to a PNG
#: first frame: vision models read frame 0 only (animation is invisible to them)
#: and rescaling a palette image is lossy, so a lossless still is faithful.
_PIL_SAVE_FORMAT: dict[str, str] = {
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/webp": "WEBP",
    "image/bmp": "BMP",
    "image/gif": "PNG",
}

# Absolute paths ending in a supported raster suffix.
#
# Two properties are load-bearing, and BOTH were learned from real defects:
#
# 1. The quantifier is non-greedy. A greedy `+` swallows the separator between
#    two paths, so "/tmp/a.png and /tmp/b.png" matched as ONE span ending at the
#    final ".png" -- not a file, so every image in a multi-image message was
#    dropped.
#
# 2. The character class holds HORIZONTAL whitespace only, and a lookbehind
#    forbids starting inside a URL or another path. With `\s` (which includes
#    "\n") a leading URL chained across the newline into the appended path:
#    `slack/events.py` emits "<user text>\n<image path>", so
#
#        see https://example.com/docs\n/tmp/a.png
#
#    matched as "//example.com/docs\n/tmp/a.png" -- one nonexistent path. Any
#    Slack message containing a link therefore lost its image. The `(?<![\w:/])`
#    guard rejects the "/" inside "https://" as a start position, which also
#    stops a URL that merely ends in ".png" from being probed as a local file.
_SUFFIX_GROUP = r"(?:png|jpg|jpeg|gif|webp|bmp)"

#: Space and tab only -- NEVER `\s`. See note 2 above.
_PATH_CHARS = r"[\w./@~ \t()\-]"

#: Must not begin mid-token: rules out "https://host/..." and a "/" that is
#: already part of a longer path.
_NOT_MID_TOKEN = r"(?<![\w:/])"

_POSIX_PATH_RE = re.compile(
    rf"{_NOT_MID_TOKEN}(/{_PATH_CHARS}+?\.{_SUFFIX_GROUP})",
    re.IGNORECASE,
)

# Windows absolute paths: a drive letter ("C:\...", "C:/...") or a UNC share
# ("\\\\host\\share\\..."). Temp attachments land in %LOCALAPPDATA%\Temp and
# dashboard uploads in %USERPROFILE%\.kiro\crew\uploads, so on Windows the
# POSIX grammar matched NOTHING and every image stayed prose -- then the temp
# file was deleted at end of turn, leaving a dead reference.
#
# Platform-gated rather than merged into one pattern: backslash and ":" are
# legal in POSIX filenames, so accepting Windows shapes everywhere makes prose
# like `the path C:\docs\logo.png is an example` a candidate -- and on Linux a
# file with that literal name can exist in the CWD, which would inline a file
# the user only mentioned. Matching the host's own grammar keeps that impossible.
_WINDOWS_PATH_CHARS = r"[\w\\/.@ \t()\-]"
_WINDOWS_PATH_RE = re.compile(
    rf"(?<![\w:])((?:[A-Za-z]:[\\/]|\\\\[^\\/:*?\"<>|\r\n]+[\\/])"
    rf"{_WINDOWS_PATH_CHARS}+?\.{_SUFFIX_GROUP})",
    re.IGNORECASE,
)

_PATH_RE = _WINDOWS_PATH_RE if os.name == "nt" else _POSIX_PATH_RE


def _downscale_within_limits(raw_bytes: bytes, mime: str, max_edge: int) -> tuple[bytes, str] | None:
    """Downscale *raw_bytes* so its longest edge is <= *max_edge*, keeping aspect.

    Returns ``(bytes, mime)`` -- the ORIGINAL pair when the image is already
    within the cap, the downscaled pair otherwise. The mime can change
    (``image/gif`` -> ``image/png``) because the re-encode picks a still format.

    Returns ``None`` when the image is oversized (or its size is unknowable) but
    a compliant copy could NOT be produced -- an undecodable file, or a Pillow
    decompression-bomb rejection on a huge-but-small-bytes raster. The caller
    must then NOT inline it: shipping the un-shrunk original is precisely the
    >2000px payload that poisons the session, so a failure fails CLOSED (leave
    the path as text) rather than open. Pillow is a guaranteed runtime dep; the
    ``_HAS_PIL`` guard only keeps image support alive on a hand-stripped install
    where dimensions cannot be verified at all.
    """
    if not _HAS_PIL or max_edge <= 0:
        return raw_bytes, mime
    try:
        with Image.open(io.BytesIO(raw_bytes)) as img:
            # Header read only -- no decompression -- so a within-cap image never
            # pays for a decode and rides through byte-identical.
            if max(img.width, img.height) <= max_edge:
                return raw_bytes, mime
            # Bake EXIF orientation into the pixels BEFORE resizing: the re-encode
            # (PNG in particular) drops the orientation tag, so a phone photo
            # would otherwise reach the model rotated.
            oriented = ImageOps.exif_transpose(img) or img
            # Palette frames upsample poorly; promote to RGBA so the resample
            # interpolates real channels rather than palette indices.
            src = oriented.convert("RGBA") if oriented.mode in ("P", "PA") else oriented
            long_edge = max(src.width, src.height)
            scale = max_edge / long_edge
            new_size = (max(1, round(src.width * scale)), max(1, round(src.height * scale)))
            resample = getattr(Image, "LANCZOS", getattr(Image, "ANTIALIAS", 1))
            resized = src.resize(new_size, resample)
            fmt = _PIL_SAVE_FORMAT.get(mime, "PNG")
            if fmt in ("JPEG", "BMP") and resized.mode not in ("RGB", "L"):
                # JPEG/BMP carry no alpha channel; flatten before encoding.
                resized = resized.convert("RGB")
            buf = io.BytesIO()
            if fmt == "JPEG":
                resized.save(buf, format="JPEG", quality=90)
            elif fmt == "WEBP":
                # A source WEBP may be lossless; PIL defaults to lossy q80, so
                # stay lossless to avoid re-encode artifacts on the downscale.
                resized.save(buf, format="WEBP", lossless=True)
            else:
                resized.save(buf, format=fmt)
            out_mime = "image/png" if mime == "image/gif" else mime
            return buf.getvalue(), out_mime
    except Exception:
        # Fail CLOSED: an oversized image we could not shrink must not be inlined,
        # or the original >2000px payload reaches the model and poisons the session.
        logger.warning(
            "acp prompt: could not downscale oversized image (mime=%s); leaving as path",
            mime,
            exc_info=True,
        )
        return None


def build_prompt_blocks(
    message: str,
    *,
    allow_image: bool = True,
    max_image_bytes: int = MAX_IMAGE_BYTES,
    max_image_edge: int = MAX_IMAGE_EDGE_PX,
) -> list[dict]:
    """Return ACP prompt blocks for *message*.

    Each readable image path found in *message* becomes an ``image`` block and is
    replaced in the text by ``[image: <name>]`` so the model still sees where the
    attachment sat in the sentence.

    ``allow_image=False`` (the agent did not advertise
    ``promptCapabilities.image``) leaves the path in the text untouched: the file
    is still on disk, so a tool-capable agent can open it, which is a strictly
    better fallback than dropping the reference. The result is always at least
    one text block, so a caller can pass it straight to ``session/prompt``.

    Inlined images are downscaled so their longest edge is at most
    ``max_image_edge`` px -- the server-side backstop for Anthropic's many-image
    dimension limit, applied for EVERY channel here regardless of any
    client-side resize that was skipped or bypassed.
    """
    text = message
    images: list[dict] = []

    if allow_image:
        seen: set[str] = set()
        for match in _PATH_RE.finditer(message):
            raw = match.group(1).strip()
            if raw in seen:
                continue
            path = Path(raw)
            suffix = path.suffix.lower()
            mime = IMAGE_MEDIA_TYPES.get(suffix)
            if mime is None or not path.is_file():
                continue
            try:
                size = path.stat().st_size
            except OSError:
                logger.debug("acp prompt: could not stat image %s", raw, exc_info=True)
                continue
            if size > max_image_bytes:
                # Leave the path in the text: the turn still carries a usable
                # reference instead of silently losing the attachment.
                logger.warning(
                    "acp prompt: image %s is %d bytes (cap %d) - sending path, not inline",
                    path.name,
                    size,
                    max_image_bytes,
                )
                continue
            try:
                raw_bytes = safe_read_file_bytes(str(path))
            except Exception:
                logger.debug("acp prompt: could not read image %s", raw, exc_info=True)
                continue
            if raw_bytes is None:
                # Refused by the sensitive-path gate (or unreadable). The path
                # stays in the text; it is NOT inlined.
                logger.warning("acp prompt: image read refused for %s", path.name)
                continue
            downscaled = _downscale_within_limits(raw_bytes, mime, max_image_edge)
            if downscaled is None:
                # Oversized and un-shrinkable (decompression-bomb / undecodable):
                # leave the path as text rather than inline a >2000px payload that
                # would poison the session. A tool-capable agent can still open it.
                logger.warning(
                    "acp prompt: image %s exceeds the dimension cap and could not be "
                    "downscaled - sending path, not inline",
                    path.name,
                )
                continue
            out_bytes, out_mime = downscaled
            data = base64.b64encode(out_bytes).decode("ascii")
            seen.add(raw)
            images.append({"type": "image", "data": data, "mimeType": out_mime})
            text = text.replace(raw, f"[image: {path.name}]")

    return [{"type": "text", "text": text}, *images]
