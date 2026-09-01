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

Wire shape (per docs/reference/kiro-cli/acp.md):

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

from kiro_crew.hooks import is_unc_shape, safe_read_file_bytes, unc_probe_allowed

# The budget constants and downscale machinery live in the LEAF module
# kiro_crew.imaging (shared with the gateway's tool-result rewrite, which must
# not import the ACP package). The two constants are re-exported because this
# module is where the prompt path's callers and tests historically found them.
from kiro_crew.imaging import (  # noqa: F401 -- constants re-exported, see comment
    MAX_IMAGE_B64_BYTES,
    MAX_IMAGE_EDGE_PX,
    downscale_image_block,
)

try:
    from PIL import Image

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

#: Pillow format name -> wire mime, for the formats this builder inlines. Pillow
#: reports the format it actually DECODED, which is the authoritative answer to
#: "what is this file"; the suffix is only ever a claim made by whoever named it.
_PIL_FORMAT_MEDIA_TYPE: dict[str, str] = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "GIF": "image/gif",
    "WEBP": "image/webp",
    "BMP": "image/bmp",
}

#: Magic-byte signatures for the same set, as ``(offset, prefix)`` pairs plus an
#: optional second window that must also match. This is the no-Pillow answer:
#: without it a hand-stripped install falls back to trusting the suffix, which is
#: exactly the property this sink is supposed to stop having. Signature checks
#: need no dependency and no decode, so they also give the Pillow path a cheap
#: cross-check on formats Pillow reports under a different name.
_IMAGE_SIGNATURES: tuple[tuple[str, int, bytes, int, bytes], ...] = (
    ("image/png", 0, b"\x89PNG\r\n\x1a\n", 0, b""),
    ("image/jpeg", 0, b"\xff\xd8\xff", 0, b""),
    ("image/gif", 0, b"GIF87a", 0, b""),
    ("image/gif", 0, b"GIF89a", 0, b""),
    # RIFF container: bytes 0-3 are "RIFF", 8-11 name the form. Only WEBP is ours.
    ("image/webp", 0, b"RIFF", 8, b"WEBP"),
    ("image/bmp", 0, b"BM", 0, b""),
)

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
#
# The UNC alternative accepts both separators after the leading pair
# (``\\host\share\...`` and ``//host/share/...``): the dashboard composer
# serializes image attachments with forward slashes (a markdown destination
# cannot carry raw backslashes -- CommonMark eats ``\`` before punctuation),
# and Windows file APIs accept the forward-slash form verbatim. The leading
# pair likewise accepts ``//``; ``(?<![\w:/])`` guards it from matching inside
# a URL's ``://``.
_WINDOWS_PATH_CHARS = r"[\w\\/.@ \t()\-]"
_WINDOWS_PATH_RE = re.compile(
    rf"(?<![\w:])(?:(?<![\w:/]))((?:[A-Za-z]:[\\/]|[\\/]{{2}}[^\\/:*?\"<>|\r\n]+[\\/])"
    rf"{_WINDOWS_PATH_CHARS}+?\.{_SUFFIX_GROUP})",
    re.IGNORECASE,
)

_PATH_RE = _WINDOWS_PATH_RE if os.name == "nt" else _POSIX_PATH_RE


def _sniff_media_type(raw_bytes: bytes) -> str | None:
    """Return the media type *raw_bytes* actually is, or ``None`` if it is not
    one of the raster formats this builder inlines.

    The DECODED identity, never the filename. A path suffix is a claim made by
    whoever wrote the name -- an upload handler that kept a client-supplied
    filename, a channel that renamed an attachment, a screenshot tool with its
    own convention -- and every producer feeding this sink would otherwise need
    its own guard to keep that claim honest. Deciding it once, here, is what
    makes the wire metadata authoritative for all of them.

    Pillow's report wins when Pillow is installed, because it comes from a real
    decode. The signature table is the fallback for a hand-stripped install, and
    it matters: a suffix-only fallback is the exact behaviour this function
    exists to remove, so degrading into it would leave the sink lying whenever
    Pillow is absent.
    """
    if _HAS_PIL:
        try:
            with Image.open(io.BytesIO(raw_bytes)) as img:
                # Header read only -- `format` is populated by ``open`` itself,
                # so this costs no decompression.
                return _PIL_FORMAT_MEDIA_TYPE.get((img.format or "").upper())
        except Exception:
            # Undecodable: not an image we can inline, whatever it is named.
            return None
    for mime, offset, prefix, second_at, second in _IMAGE_SIGNATURES:
        if not raw_bytes.startswith(prefix, offset):
            continue
        if second and not raw_bytes.startswith(second, second_at):
            continue
        return mime
    return None


def build_prompt_blocks(
    message: str,
    *,
    allow_image: bool = True,
    max_image_bytes: int = MAX_IMAGE_BYTES,
    max_image_edge: int = MAX_IMAGE_EDGE_PX,
    max_image_b64_bytes: int = MAX_IMAGE_B64_BYTES,
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
    client-side resize that was skipped or bypassed -- and then shrunk further if
    needed so the base64 payload stays within ``max_image_b64_bytes``, the
    backend's per-image byte ceiling.
    """
    text = message
    images: list[dict] = []

    if allow_image:
        seen: set[str] = set()
        for match in _PATH_RE.finditer(message):
            raw = match.group(1).strip()
            if raw in seen:
                continue
            # UNC-shaped candidates name a HOST on Windows: gate them before
            # any filesystem call, or is_file() below opens an SMB connection
            # to attacker-controlled text. POSIX has no such semantics (a
            # doubled leading slash is an ordinary local path), and _PATH_RE
            # is platform-gated anyway. See kiro_crew.hooks.unc_probe_allowed.
            if os.name == "nt" and is_unc_shape(raw) and not unc_probe_allowed(raw):
                seen.add(raw)
                continue
            path = Path(raw)
            suffix = path.suffix.lower()
            # The suffix decides only which paths are CANDIDATES -- it is what
            # `_PATH_RE` matched on, and re-checking it here keeps a `.txt` from
            # costing a stat. What the file IS gets decided from its bytes below.
            if suffix not in IMAGE_MEDIA_TYPES or not path.is_file():
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
            mime = _sniff_media_type(raw_bytes)
            if mime is None:
                # Named like an image, not one (or not a format we inline). Leave
                # the path as text and fail CLOSED rather than shipping bytes under
                # a mimeType taken from the filename: the backend decodes by the
                # declared type, and a block it rejects sits at a fixed history
                # index that kiro-cli replays on every later turn.
                logger.warning(
                    "acp prompt: %s is not a supported image by content - "
                    "sending path, not inline",
                    path.name,
                )
                continue
            if mime != IMAGE_MEDIA_TYPES[suffix]:
                # Not an error: the bytes are authoritative and the block is built
                # from them. Logged because a mismatch usually means a producer
                # renamed an attachment, which is worth seeing.
                logger.info(
                    "acp prompt: %s is %s by content, not %s by suffix - using content",
                    path.name,
                    mime,
                    IMAGE_MEDIA_TYPES[suffix],
                )
            downscaled = downscale_image_block(
                raw_bytes, mime, max_edge=max_image_edge, max_b64_bytes=max_image_b64_bytes
            )
            if downscaled is None:
                # No compliant rendition (decompression-bomb / undecodable /
                # truncated / over the decode-pixel ceiling / still over the
                # encoded ceiling at the minimum edge): leave the path as text
                # rather than inline a payload the backend rejects on this and
                # every later turn. A tool-capable agent can still open it.
                logger.warning(
                    "acp prompt: image %s could not be rendered within the "
                    "dimension and encoded-size caps - sending path, not inline",
                    path.name,
                )
                continue
            out_bytes, out_mime = downscaled
            data = base64.b64encode(out_bytes).decode("ascii")
            seen.add(raw)
            images.append({"type": "image", "data": data, "mimeType": out_mime})
            text = text.replace(raw, f"[image: {path.name}]")

    return [{"type": "text", "text": text}, *images]
