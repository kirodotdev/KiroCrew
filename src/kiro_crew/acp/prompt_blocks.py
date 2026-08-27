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
import json
import logging
import os
import re
from pathlib import Path

from kiro_crew.hooks import is_unc_shape, safe_read_file_bytes, unc_probe_allowed

# The budget constants and Pillow machinery live in the LEAF module
# kiro_crew.imaging (shared with the gateway's tool-result rewrite, which must
# not import the ACP package). The two constants are re-exported because this
# module is where the prompt path's callers and tests historically found them.
from kiro_crew.imaging import (  # noqa: F401 -- constants re-exported, see comment
    MAX_IMAGE_B64_BYTES,
    MAX_IMAGE_EDGE_PX,
    downscale_image_block,
)
from kiro_crew.platform_compat import first_linked_ancestor, is_link_or_junction

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
            mime = IMAGE_MEDIA_TYPES.get(suffix)
            if mime is None:
                # Unreachable for regex-produced candidates today (_PATH_RE's
                # suffix group and IMAGE_MEDIA_TYPES share one key set), kept
                # as the lexical backstop should the two ever drift.
                continue
            # A linked ANCESTOR defeats the lexical UNC screen above: the
            # candidate is not itself UNC-shaped -- only the link's target is
            # -- and is_file()/stat() below resolve every ancestor, so the
            # probe itself would traverse the link and open the SMB
            # connection. Windows-only for the same reason as the UNC gate:
            # on POSIX stat-ing through a symlink is harmless. Reference
            # wiring: dashboard/handlers/themes.py::_resolve_local_source.
            if os.name == "nt" and first_linked_ancestor(path) is not None:
                seen.add(raw)
                continue
            # The LEAF gets the junction-aware check the walk deliberately
            # excludes: is_file() below FOLLOWS a final-component link, so a
            # leaf symlink/junction targeting a UNC share is the same probe.
            # lstat-based, so the link itself is never followed.
            if os.name == "nt" and is_link_or_junction(path):
                seen.add(raw)
                continue
            if not path.is_file():
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


#: Block ``type`` values that get a dedicated counter in the structure summary.
#: Anything else is folded into ``other`` so an unfamiliar shape still counts
#: toward the total without ever being named or copied.
_SUMMARY_KNOWN_TYPES = ("text", "image", "tool_use", "tool_result")


def summarize_prompt_structure(blocks: object) -> dict:
    """Return a CONTENT-FREE structural summary of an ACP prompt block list.

    The returned dict reports ONLY shape metrics -- never any message text,
    image bytes, tool arguments, or other content:

    * ``block_count`` -- total number of blocks.
    * ``type_counts`` -- a count per block ``type`` (``text`` / ``image`` /
      ``tool_use`` / ``tool_result`` / ``other`` for any unrecognised or
      typeless shape).
    * ``empty_text_blocks`` -- text blocks whose ``text`` is missing, blank, or
      whitespace-only (a structurally suspicious payload). A text block with no
      ``text`` key at all is as suspect as one whose ``text`` is a blank
      string, so both fold into this count.
    * ``tool_use`` / ``tool_result`` -- the two tool-block counts surfaced at
      the top level so a pairing imbalance (a ``tool_result`` with no matching
      ``tool_use``, or vice versa) is visible at a glance.
    * ``total_bytes`` -- length of ``json.dumps`` of the NORMALISED block list
      (``[]`` when the argument is not a list or tuple), the approximate
      serialized wire size of the outbound request. Measuring the normalised
      list keeps the size coherent with the counts: a non-list argument reports
      ``block_count: 0`` alongside ``total_bytes: 2`` (an empty ``[]``) rather
      than a size describing a payload the counts claim is empty.

    This summary is deliberately safe to log: it carries no content and
    therefore cannot leak credentials or user data. That is a hard requirement
    (issue #6022) -- the kiro-cli data dir is fenced precisely because it holds
    SSO tokens, so the outbound-request diagnostics must expose counts, types,
    and sizes ONLY, never the bytes themselves.

    Defensive by contract: this is a diagnostics helper on the live prompt
    path, so it never raises. A malformed ``blocks`` argument (not a list,
    ``None`` entries, non-dict entries, unserialisable content) yields a
    partial/minimal summary instead of propagating an exception into the turn.
    """
    summary: dict = {
        "block_count": 0,
        "type_counts": {},
        "empty_text_blocks": 0,
        "tool_use": 0,
        "tool_result": 0,
        "total_bytes": 0,
    }
    try:
        block_list = list(blocks) if isinstance(blocks, (list, tuple)) else []
        summary["block_count"] = len(block_list)

        type_counts: dict[str, int] = {}
        empty_text = 0
        for block in block_list:
            if isinstance(block, dict):
                btype = block.get("type")
                key = btype if btype in _SUMMARY_KNOWN_TYPES else "other"
                if btype == "text":
                    text = block.get("text")
                    # A missing (or non-string) text key is as structurally
                    # suspect as a present-but-blank one, so fold both into the
                    # empty count.
                    if not isinstance(text, str) or not text.strip():
                        empty_text += 1
            else:
                key = "other"
            type_counts[key] = type_counts.get(key, 0) + 1

        summary["type_counts"] = type_counts
        summary["empty_text_blocks"] = empty_text
        summary["tool_use"] = type_counts.get("tool_use", 0)
        summary["tool_result"] = type_counts.get("tool_result", 0)

        try:
            summary["total_bytes"] = len(json.dumps(block_list, default=str))
        except (TypeError, ValueError):
            # Unserialisable content must not sink the whole summary: keep the
            # structural counts and report an unknown size rather than raising.
            summary["total_bytes"] = -1
    except Exception:
        logger.debug("acp prompt: structure summary failed", exc_info=True)

    return summary
