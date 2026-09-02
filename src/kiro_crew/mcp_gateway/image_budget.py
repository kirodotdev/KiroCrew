"""Inline-image budget for MCP tool-result image content blocks.

The gateway relay is the one seam every brokered MCP server's ``tools/call``
response crosses before kiro-cli stores it in the session's conversation
history. Images that arrive there as ``{"type": "image", "data": <base64>,
"mimeType": ...}`` content blocks bypass the prompt-path downscale in
:mod:`kiro_crew.acp.prompt_blocks` entirely -- and because kiro-cli replays the
full history to the model on every subsequent turn, a single block over the
backend's per-image limits (2000 px longest edge for many-image requests, 5 MiB
base64 payload) wedges the session permanently: the offending block sits at a
fixed history index and no later message can evict it.

This module applies the SAME budget the prompt path enforces
(:func:`kiro_crew.imaging.downscale_image_block`) to every image block in a
relayed tool result:

* an image already inside both caps and carrying canonical base64 passes
  through byte-identical; a compliant image with wrapped/whitespace base64
  keeps its bytes but is re-emitted canonically, because the byte ceiling is
  measured on the canonical form;
* an oversized image is downscaled in place (data and mimeType replaced);
* an image whose compliance cannot be established -- undecodable bytes, a
  Pillow decompression-bomb refusal, a source over the decode-pixel ceiling,
  still over the encoded ceiling at the minimum edge, or Pillow absent
  entirely -- is replaced by a short text block saying so. That replacement
  fails CLOSED on purpose: forwarding an unverified image risks the permanent
  history wedge, while a dropped image costs exactly one tool result.

Structural failures (a frame that is not JSON, has no ``result.content`` list,
or an unexpected exception while rewriting) forward the original line
unmodified, mirroring :mod:`kiro_crew.mcp_gateway.spill`: those frames carry no
image this module can verify, and breaking the relay for them would fail every
co-pooled tenant's call, not just the offending one. The fail-closed rule is
per image block, where "oversized but unshrinkable" is actually decided.

Deliberately OUT of scope: an MCP embedded resource
(``{"type": "resource", "resource": {"mimeType": "image/...", "blob": ...}}``)
is not rewritten. No brokered path in this codebase renders resource blobs to
the model, so rewriting them would be speculative; if that ever changes, this
module is the place to extend. This carve-out is documented alongside the
kiro-cli-builtin-tools one in the Layer 3 section of
``docs/architecture/design-notes/mcp-gateway-oversize-response.md``.

Pillow work happens here synchronously; the caller (the backend stdout pump)
offloads the whole call to the dedicated image executor so neither the shared
event loop nor the maintenance sweeps ever pay for a decode.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from typing import Any

from kiro_crew import imaging
from kiro_crew.imaging import (
    MAX_IMAGE_B64_BYTES,
    MAX_IMAGE_EDGE_PX,
    downscale_image_block,
    image_dimensions,
    pil_available,
)

logger = logging.getLogger(__name__)

#: Byte-level pre-filter half 1: any MCP image content block whose ``type``
#: value is written as plain text serializes these exact bytes, whatever the
#: producer's whitespace style.
IMAGE_BLOCK_PROBE: bytes = b'"image"'

#: Byte-level pre-filter half 2: the only JSON escape that can produce a
#: letter is ``\uXXXX``, so a string that PARSES to ``image`` without the raw
#: bytes containing ``"image"`` must contain at least one ``\u`` escape.
#: Checking for these two bytes therefore makes the pre-filter's negative
#: answer PROVABLE rather than heuristic. Lines carrying escaped non-ASCII
#: text also match; they merely pay one JSON parse off the event loop.
_ESCAPE_PROBE: bytes = b"\\u"


def line_may_carry_image_block(line: bytes) -> bool:
    """Whether *line* could serialize an ``{"type": "image"}`` content block.

    A ``False`` is sound (the line provably cannot parse to one), so the pump
    may skip the executor hop entirely. A ``True`` is cheap to be wrong about:
    the line pays one JSON parse in the image executor and is forwarded
    unchanged.
    """
    return IMAGE_BLOCK_PROBE in line or _ESCAPE_PROBE in line


#: Ceiling on the cumulative SOURCE pixels decoded per tools/call response.
#: Each block's decode cost scales with its pixel area (bounded per block by
#: the source-pixel ceiling), and one frame's blocks are processed
#: sequentially on one image-pool worker -- so without a per-frame budget, a
#: single valid response stuffed with maximum-cost images monopolizes the
#: worker for the whole frame and stalls co-pooled calls behind it. A PIXEL
#: budget rather than a block count deliberately lets legitimate many-image
#: results through (ten small PDF-page renders spend almost nothing) while
#: bounding the adversarial case at four maximum-size decodes, a few seconds
#: of worker time. Blocks past the budget are replaced by the omission text
#: WITHOUT being decoded, so the excess costs one header read each.
MAX_FRAME_SOURCE_PIXELS = 4 * imaging.MAX_IMAGE_SOURCE_PIXELS

#: Replacement text for an image block that could not be made compliant.
#: Told to the model (this lands in the conversation history), so it explains
#: what happened and why retrying with the same tool will not help.
_OMITTED_TEMPLATE = (
    "[Kiro Crew: image omitted from this tool result -- it could not be "
    "rendered within the inline-image budget ({max_edge} px longest edge, "
    "{max_mib} MiB base64). Have the tool save the image to a file and "
    "return the path instead.]"
)

#: Ceiling on individual omission-text notes emitted per response. Each note
#: is ~10x the size of the smallest rejectable image block, so replacing every
#: block one-for-one in a many-block frame AMPLIFIES it -- hundreds of
#: thousands of empty image blocks in a few-MiB frame would expand past the
#: 64 MiB relay read limit and the whole response would be dropped. The first
#: few rejected blocks get the explanatory note; everything after is dropped
#: and counted into one trailing summary block, bounding the rewrite's output
#: at roughly the input size plus ~1.5 KiB.
_MAX_OMISSION_NOTES = 4

#: Trailing summary for notes suppressed past :data:`_MAX_OMISSION_NOTES`.
_SUMMARY_TEMPLATE = (
    "[Kiro Crew: {count} additional image blocks were omitted from this "
    "tool result for the same reasons as above.]"
)

#: Replacement text for image blocks past the per-response decode budget.
#: Distinct from the budget text: the image itself may be fine; the frame as a
#: whole asked for more decode work than one relay slot will spend.
_EXCESS_TEMPLATE = (
    "[Kiro Crew: image omitted -- this tool result asked for more image "
    "processing than a single response is allotted; later images were "
    "dropped. Have the tool save extra images to files and return the "
    "paths instead.]"
)


def _omitted_text_block() -> dict[str, str]:
    """The fail-closed replacement block for a non-compliant image."""
    return {
        "type": "text",
        "text": _OMITTED_TEMPLATE.format(
            max_edge=MAX_IMAGE_EDGE_PX,
            max_mib=MAX_IMAGE_B64_BYTES // (1024 * 1024),
        ),
    }


def _rewrite_image_item(item: dict[str, Any]) -> dict[str, Any] | None:
    """Rewrite one ``type=="image"`` content block to fit the budget.

    Returns ``None`` when the block is already compliant (caller keeps the
    original object untouched), or the replacement block: the downscaled image,
    or the fail-closed text block when compliance cannot be established.
    """
    if not pil_available():
        # Without Pillow the dimensions are unverifiable, and an unverified
        # image is exactly the payload that wedges the session. The prompt
        # path's no-Pillow pass-through serves a user inlining their OWN file;
        # this seam serves an unattended multi-tenant relay, so it fails
        # closed instead.
        return _omitted_text_block()
    data = item.get("data")
    if not isinstance(data, str) or not data:
        return _omitted_text_block()
    try:
        # Whitespace-tolerant WITHOUT split()/join(): base64.encodebytes-style
        # producers wrap at 76 columns and must keep relaying, but splitting a
        # 64 MiB whitespace-dense payload materializes millions of small
        # string objects (a transient multi-GiB allocation an adversarial
        # server can trigger at will). The default non-validating decode
        # discards non-alphabet bytes in C with no per-token Python objects;
        # junk that survives it still fails the Pillow gate and falls closed.
        raw = base64.b64decode(data)
    except (binascii.Error, ValueError):
        return _omitted_text_block()
    mime = item.get("mimeType")
    if not isinstance(mime, str) or not mime:
        mime = "image/png"
    fitted = downscale_image_block(raw, mime)
    if fitted is None:
        return _omitted_text_block()
    out_bytes, out_mime = fitted
    # The byte ceiling was measured on the CANONICAL encoding of out_bytes,
    # so canonical is what must go on the wire: passing the original string
    # through would forward whitespace-inflated base64 (encodebytes wraps at
    # 76 columns, ~1.3% larger) that can exceed the ceiling its canonical
    # form just passed. Untouched only when the block already carries the
    # exact canonical form -- then the whole frame can skip re-serialization.
    canonical = base64.b64encode(out_bytes).decode("ascii")
    if canonical == data and out_mime == mime:
        return None  # already within budget and canonically encoded
    return {
        **item,
        "data": canonical,
        "mimeType": out_mime,
    }


def parse_image_bearing_frame(line: bytes) -> dict[str, Any] | None:
    """Parse *line* and return the message ONLY when it carries image blocks.

    The cheap confirmation stage between the byte probe and the Pillow work:
    a JSON parse plus a structural scan, no image decoding. Runs on the
    maintenance pool (short, like the spill rewrite), so frames that merely
    carry escaped non-ASCII text -- which match the probe's ``\\u`` half --
    never occupy an image-pool worker behind seconds-long decodes.

    Returns ``None`` for anything that is not a ``tools/call`` result with at
    least one ``{"type": "image"}`` content block.
    """
    try:
        msg = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(msg, dict):
        return None
    result = msg.get("result")
    if not isinstance(result, dict):
        return None
    content = result.get("content")
    if not isinstance(content, list):
        return None
    for item in content:
        if isinstance(item, dict) and item.get("type") == "image":
            return msg
    return None


def rewrite_image_frame(msg: dict[str, Any], line: bytes, server_name: str) -> bytes:
    """Rewrite the image blocks of a parsed frame to fit the budget.

    *msg* is the dict :func:`parse_image_bearing_frame` returned for *line*;
    *line* is returned unchanged when no block needed rewriting (or the frame
    cannot be re-serialized). The Pillow work happens here, so this stage is
    what the pump runs on the image executor.

    Cumulative decode work per frame is bounded by
    :data:`MAX_FRAME_SOURCE_PIXELS`: each block's header-read pixel area is
    charged against the budget BEFORE its decode, and blocks past it are
    replaced by the excess text without ever being decoded.
    """
    result = msg["result"]
    content = result["content"]
    changed = False
    pixels_spent = 0
    notes_emitted = 0
    notes_suppressed = 0
    rewritten: list[Any] = []

    def _note(block: dict[str, str]) -> None:
        """Append an omission note, or count it once past the note ceiling.

        One-for-one replacement would amplify a many-block frame past the
        relay read limit (each note is ~10x an empty image block), so notes
        past the ceiling are suppressed here and reported by count in a
        single trailing summary block instead.
        """
        nonlocal notes_emitted, notes_suppressed
        if notes_emitted < _MAX_OMISSION_NOTES:
            notes_emitted += 1
            rewritten.append(block)
        else:
            notes_suppressed += 1

    for item in content:
        if isinstance(item, dict) and item.get("type") == "image":
            try:
                charge = _source_pixel_charge(item)
                if pixels_spent + charge > MAX_FRAME_SOURCE_PIXELS:
                    changed = True
                    _note({"type": "text", "text": _EXCESS_TEMPLATE})
                    continue
                pixels_spent += charge
                replacement = _rewrite_image_item(item)
            except Exception:
                # Unexpected rewrite failure on a block that decoded: fail
                # closed for THIS block -- its compliance is unproven and
                # forwarding it risks the permanent history wedge.
                logger.warning(
                    "image budget: rewrite failed for an image block from %s; "
                    "replacing with text",
                    server_name,
                    exc_info=True,
                )
                replacement = _omitted_text_block()
            if replacement is None:
                rewritten.append(item)
            elif replacement.get("type") == "text":
                changed = True
                _note(replacement)
            else:
                changed = True
                rewritten.append(replacement)
        else:
            rewritten.append(item)

    if notes_suppressed:
        rewritten.append(
            {"type": "text", "text": _SUMMARY_TEMPLATE.format(count=notes_suppressed)}
        )

    if not changed:
        return line

    try:
        msg["result"] = {**result, "content": rewritten}
        out = json.dumps(msg, separators=(",", ":")).encode("utf-8") + b"\n"
    except (TypeError, ValueError):
        # The frame cannot be re-serialized (non-JSON-safe values elsewhere in
        # the message). Forward the original: mangling the frame would fail the
        # call outright, and this shape never occurs for a real MCP response.
        logger.warning(
            "image budget: could not re-serialize rewritten response from %s; "
            "forwarding original",
            server_name,
        )
        return line
    logger.info(
        "image budget: rewrote %s tool result (%d -> %d bytes)",
        server_name,
        len(line),
        len(out),
    )
    return out


def _source_pixel_charge(item: dict[str, Any]) -> int:
    """The pixel area this block will charge against the frame decode budget.

    A header-only read (no decode). Blocks that will never reach a decoder
    charge NOTHING, because the budget accounts decode work, not image size:

    * unreadable/undecodable data takes the fail-closed omission path in
      :func:`_rewrite_image_item` on a header read alone;
    * a source over the per-image ceiling (:data:`MAX_IMAGE_SOURCE_PIXELS`)
      is refused by :func:`downscale_image_block` on its own header read --
      charging its full area would let a run of rejected oversize images
      exhaust the frame budget and starve a later legitimate image that
      would actually have been decoded.
    """
    data = item.get("data")
    if not isinstance(data, str) or not data:
        return 0
    try:
        # Same C-level tolerant decode as _rewrite_image_item, same rationale.
        raw = base64.b64decode(data)
    except (binascii.Error, ValueError):
        return 0
    dims = image_dimensions(raw)
    if dims is None:
        return 0
    area = dims[0] * dims[1]
    # Module-attribute read on purpose: :func:`downscale_image_block` resolves
    # the ceiling from ``imaging``'s live global at call time, and the charge
    # decision must agree with the refusal decision under any runtime (or
    # test-patched) value -- a name import here would freeze a copy.
    if area > imaging.MAX_IMAGE_SOURCE_PIXELS:
        return 0
    return area
