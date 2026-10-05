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
import hashlib
import json
import logging
import os
import re
from pathlib import Path

from kiro_crew.hooks import (
    is_unc_shape,
    safe_read_file_bytes,
    safe_read_file_bytes_nolink,
    unc_probe_allowed,
)

# The path grammar and the history scrubber live in the LEAF module
# kiro_crew.image_refs for the same reason the Pillow machinery lives in
# kiro_crew.imaging: kiro_crew.context needs the scrubber and the
# agent-sdk-boundary gate forbids application code from importing
# kiro_crew.acp. The pattern names are re-exported because this module and
# its tests are where they have always been read from.
from kiro_crew.image_refs import (  # noqa: F401 -- re-exported, see comment
    _PATH_RE,
    _POSIX_PATH_RE,
    _WINDOWS_PATH_RE,
    STRIPPED_IMAGE_MARKER,
    strip_image_refs,
)

# The budget constants and Pillow machinery live in the LEAF module
# kiro_crew.imaging (shared with the gateway's tool-result rewrite, which must
# not import the ACP package). The two constants are re-exported because this
# module is where the prompt path's callers and tests import them from.
from kiro_crew.imaging import (  # noqa: F401 -- constants re-exported, see comment
    MAX_IMAGE_B64_BYTES,
    MAX_IMAGE_EDGE_PX,
    downscale_image_block,
)
from kiro_crew.messaging.raster import SNIFF_BYTES, sniff_raster_mime
from kiro_crew.pinned_fs import screen_held_file_kind

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

#: Caps on what ONE prompt inlines, whatever its per-image sizes. The smallest
#: backend request-body ceiling measured so far lies between a replayed request
#: of 30.4 MB of base64 (accepted) and one of 33.8 MB (refused as improperly
#: formed): 32 MiB. Three quarters of it is the images' share of a replayed
#: request, the rest text, tool results and framing, and one prompt may inline
#: half of that share -- 12 MiB. Twenty is where the backend's many-image
#: dimension rule begins, and each replayed image costs about 1,600 tokens on
#: every later turn. Both caps bound this prompt alone; what a conversation's
#: replayed history carries in total is not measured here. A picture past either
#: cap stays a path in the text, like one over the per-image cap.
MAX_PROMPT_IMAGE_BLOCKS = 20
MAX_PROMPT_IMAGE_B64_BYTES = 12 * 1024 * 1024

#: Written after a readable picture's path when a cap kept it out of the
#: prompt, so neither the user nor the model takes the picture as seen.
PROMPT_LIMIT_NOTE = "[image not attached: prompt image limit]"
IMAGE_SIZE_NOTE = "[image not attached: image size limit]"

#: A marker-shaped token the USER typed, in any case. Escaped before the builder
#: writes its own, so only a marker this function produced reads as an
#: attachment. The replay scrubber's "[image not carried ...]" is first-party
#: text and is left alone.
_TYPED_MARKER_RE = re.compile(r"\[image(?=:|\snot\sattached:)", re.IGNORECASE)


def build_prompt_blocks(
    message: str,
    *,
    allow_image: bool = True,
    max_image_bytes: int = MAX_IMAGE_BYTES,
    max_image_edge: int = MAX_IMAGE_EDGE_PX,
    max_image_b64_bytes: int = MAX_IMAGE_B64_BYTES,
    max_prompt_image_blocks: int = MAX_PROMPT_IMAGE_BLOCKS,
    max_prompt_image_b64_bytes: int = MAX_PROMPT_IMAGE_B64_BYTES,
) -> list[dict]:
    """Return ACP prompt blocks for *message*.

    Each readable image path found in *message* becomes an ``image`` block and is
    replaced in the text by ``[image: <name>]`` so the model still sees where the
    attachment sat in the sentence. The marker lands only where the path grammar
    matched -- never inside a URL's own path or a longer local path that merely
    contains the same characters; a local path quoted as a URL query value
    (``?src=/tmp/a.png``) is a path to the grammar and is rewritten like any
    other. A second DISTINCT file with the same basename gets
    ``[image: <name> (2)]``, and so on; the same bytes under two names are one
    block, marked both places with the first name. Past ``max_prompt_image_blocks``
    blocks or ``max_prompt_image_b64_bytes`` of base64 in one prompt, a further
    picture stays a path in the text.

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
        # Every candidate the grammar matched, as ``(start, end, raw)`` in
        # *message*, repeats included; the one-pass substitution after the loop
        # rewrites exactly these spans.
        candidates: list[tuple[int, int, str]] = []
        # raw path -> the marker written for it, for every path this call
        # inlined (or recognised as the same bytes as one it inlined).
        markers: dict[str, str] = {}
        # raw path -> the note written after it, for every readable picture a
        # cap kept out of this prompt.
        notes: dict[str, str] = {}
        # sha256 of the file's bytes -> marker: the same bytes under a second
        # name are the picture already attached, before any cap is consulted.
        raw_digests: dict[str, str] = {}
        # Basename -> how many DISTINCT files with that name were inlined, so
        # the second one's marker can be told from the first.
        marker_count: dict[str, int] = {}
        prompt_b64_bytes = 0
        past_block_cap = 0
        for match in _PATH_RE.finditer(message):
            group = match.group(1)
            raw = group.strip()
            start = match.start(1) + (len(group) - len(group.lstrip()))
            end = start + len(raw)
            candidates.append((start, end, raw))
            if raw in seen:
                continue
            seen.add(raw)
            # UNC-shaped candidates name a HOST on Windows: gate them before
            # any filesystem call, or is_file() below opens an SMB connection
            # to attacker-controlled text. POSIX has no such semantics (a
            # doubled leading slash is an ordinary local path), and _PATH_RE
            # is platform-gated anyway. See kiro_crew.hooks.unc_probe_allowed.
            if os.name == "nt" and is_unc_shape(raw) and not unc_probe_allowed(raw):
                continue
            path = Path(raw)
            suffix = path.suffix.lower()
            suffix_mime = IMAGE_MEDIA_TYPES.get(suffix)
            if suffix_mime is None:
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
            #
            # The file-kind and size checks run THROUGH the held leaf descriptor,
            # UNDER the hold: `screen_held_file_kind` refuses a link ANYWHERE in the
            # chain (ancestor OR leaf) and reports `is_regular`/`size` from `fstat`
            # on the descriptor the walk is still holding -- never a by-name
            # `is_file()`/`stat()` after the hold closes, which a junction planted in
            # that window would redirect into an SMB auth. POSIX keeps the by-name
            # probe (a symlink lookup there is a harmless local hop).
            # A long run of prose ending in an image suffix is not a path: a
            # component over 255 characters (past every common name limit) raises
            # ENAMETOOLONG, and one raise here fails the whole turn. Skip it.
            if any(len(part) > 255 for part in path.parts):
                continue
            if os.name == "nt":
                screened = screen_held_file_kind(str(path))
                if screened is None or not screened.is_regular:
                    continue
                size = screened.size
            else:
                try:
                    if not path.is_file():
                        continue
                except OSError:
                    continue
                try:
                    size = path.stat().st_size
                except OSError:
                    logger.debug("acp prompt: could not stat image %s", raw, exc_info=True)
                    continue
            if size > max_image_bytes:
                # The path stays usable text, and the note says the picture
                # was not attached, so nobody takes it as seen. The note speaks
                # about an image, so only bytes that sniff as a raster earn it:
                # a bounded read through the same gate, never the whole file.
                if _sniffs_as_raster(path):
                    notes[raw] = IMAGE_SIZE_NOTE
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
            raw_digest = hashlib.sha256(raw_bytes).hexdigest()
            if raw_digest in raw_digests:
                # The same picture under another name: one block, marked both
                # places -- a cap never turns a duplicate into a dropped picture.
                markers[raw] = raw_digests[raw_digest]
                continue
            # The suffix selects path CANDIDATES; the bytes decide what reaches
            # the wire. Require a complete sniff window so a truncated header
            # cannot become a pass-through image when Pillow is unavailable.
            mime = (
                sniff_raster_mime(raw_bytes[:SNIFF_BYTES])
                if len(raw_bytes) >= SNIFF_BYTES
                else None
            )
            if mime is None or mime not in IMAGE_MEDIA_TYPES.values():
                logger.warning(
                    "acp prompt: %s is not a supported raster by content - "
                    "sending path, not inline",
                    path.name,
                )
                continue
            if len(images) >= max_prompt_image_blocks:
                # The prompt is full: a further picture stays a path and says
                # so, and is not decoded to learn that.
                notes[raw] = PROMPT_LIMIT_NOTE
                past_block_cap += 1
                continue
            if mime != suffix_mime:
                logger.info(
                    "acp prompt: %s is %s by content, not %s by suffix; using content",
                    path.name,
                    mime,
                    suffix_mime,
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
                notes[raw] = IMAGE_SIZE_NOTE
                logger.warning(
                    "acp prompt: image %s could not be rendered within the "
                    "dimension and encoded-size caps - sending path, not inline",
                    path.name,
                )
                continue
            out_bytes, out_mime = downscaled
            data = base64.b64encode(out_bytes).decode("ascii")
            if prompt_b64_bytes + len(data) > max_prompt_image_b64_bytes:
                # Past what one prompt may carry: the path stays usable text
                # and says so, exactly as a picture over the per-image cap does.
                notes[raw] = PROMPT_LIMIT_NOTE
                logger.warning(
                    "acp prompt: image %s is past this prompt's limit of %d base64 bytes - "
                    "sending path, not inline",
                    path.name,
                    max_prompt_image_b64_bytes,
                )
                continue
            marker_count[path.name] = marker_count.get(path.name, 0) + 1
            nth = marker_count[path.name]
            marker = f"[image: {path.name}]" if nth == 1 else f"[image: {path.name} ({nth})]"
            images.append({"type": "image", "data": data, "mimeType": out_mime})
            markers[raw] = marker
            raw_digests[raw_digest] = marker
            prompt_b64_bytes += len(data)
        if past_block_cap:
            logger.warning(
                "acp prompt: %d image(s) past this prompt's limit of %d blocks - "
                "sending paths, not inline",
                past_block_cap,
                max_prompt_image_blocks,
            )
        text = _rewrite_text(message, candidates, markers, notes)
    else:
        # The path stays as written, but a typed marker must not claim an
        # attachment on a backend that takes none either.
        text = _rewrite_text(message, [], {}, {})

    return [{"type": "text", "text": text}, *images]


def _sniffs_as_raster(path: Path) -> bool:
    """Whether the first bytes of *path* are a supported raster's, read through
    the gate and bounded to the sniff window, for a file too large to read whole.

    The bounded reader refuses a hardlinked file, so such a picture earns no
    note: a missing note is the fail-safe direction, a wrong claim is not.
    """
    try:
        head = safe_read_file_bytes_nolink(str(path), max_bytes=SNIFF_BYTES, allow_truncate=True)
    except Exception:
        return False
    if head is None or len(head) < SNIFF_BYTES:
        return False
    return sniff_raster_mime(head) in IMAGE_MEDIA_TYPES.values()


#: What may follow a path inside a markdown destination: an optional closing
#: angle bracket, an optional quoted title, then the parenthesis.
_DESTINATION_TAIL_RE = re.compile(r">?(?:[ \t]+\"[^\"\n]*\")?\)")


def _markdown_destination_close(message: str, start: int, end: int) -> int | None:
    """Index just past the ``)`` closing a markdown destination that holds
    ``message[start:end]``, or ``None`` when the span is not such a destination."""
    opened = message[start - 2 : start] == "](" or message[start - 3 : start] == "](<"
    if start < 2 or not opened:
        return None
    tail = _DESTINATION_TAIL_RE.match(message, end)
    if tail is None:
        return None
    return tail.end()


def _rewrite_text(
    message: str,
    candidates: list[tuple[int, int, str]],
    markers: dict[str, str],
    notes: dict[str, str],
) -> str:
    """*message* with inlined candidates replaced by their marker, kept-out
    pictures followed by their note, and typed marker-shaped tokens escaped.

    One pass over the grammar's own match spans plus the typed-marker spans, in
    text order, so a marker lands exactly where a candidate the grammar
    recognised stood -- never inside a URL or another path that merely contains
    the same characters, which a whole-text ``str.replace`` would also rewrite.
    """
    edits: list[tuple[int, int, str]] = []
    for start, end, raw in candidates:
        marker = markers.get(raw)
        if marker is not None:
            edits.append((start, end, marker))
            continue
        note = notes.get(raw)
        if note is not None:
            close = _markdown_destination_close(message, start, end)
            if close is not None:
                # Inside a markdown destination the note would corrupt the link:
                # it follows the whole reference instead.
                edits.append((close, close, f" {note}"))
            else:
                edits.append((start, end, f"{raw} {note}"))
    edits.extend((m.start(), m.start(), "\\") for m in _TYPED_MARKER_RE.finditer(message))
    if not edits:
        return message
    out: list[str] = []
    pos = 0
    for start, end, replacement in sorted(edits):
        out.append(message[pos:start])
        out.append(replacement)
        pos = end
    out.append(message[pos:])
    return "".join(out)


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
    therefore cannot leak credentials or user data. That is a hard
    requirement -- the kiro-cli data dir is fenced precisely because it holds
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
