"""Build ACP ``session/prompt`` content blocks from a message and its attachments.

Channels hand the provider ONE string plus a STRUCTURED attachment list
(:class:`kiro_crew.prompt_attachments.PromptAttachment`). Every image block
this module emits comes from that list; the text is never scanned for image
paths. A path in the text is a mention -- the session ledger's
``artifact <name>: <path>.png`` snapshot line, a nudge body, an injected
envelope, an agent's own ``![shot](...png)`` reply quoted back, a consolidation
prompt -- and a mention must not become an upload: the backend replays every
stored image block, so one file re-inlined on every automation cycle grew the
request by its full encoded size per turn until the backend rejected the body.
Only the channel that received a file knows the user attached it, so only the
channel's list says so. This module owns the conversion so both prompt paths
share one implementation:

* :meth:`kiro_crew.acp.session_handle.AcpSessionHandle.prompt` -- the live path
  for the public Kiro backend (``AcpProvider.start`` swaps ``AcpClient`` out for
  ``AcpSessionProvider``, so this is what actually reaches kiro-cli).
* :meth:`kiro_crew.acp.client.AcpClient._send_prompt` -- the direct-client path.

Keeping one builder matters: both paths need the same list-to-image conversion,
so a single implementation stops any channel from shipping an attachment the
model never sees -- or one it was never given.

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
import re
from collections.abc import Sequence

# The history scrubber lives in the LEAF module kiro_crew.image_refs for the
# same reason the Pillow machinery lives in kiro_crew.imaging: kiro_crew.context
# needs the scrubber and the agent-sdk-boundary gate forbids application code
# from importing kiro_crew.acp. The name is re-exported because this module is
# where its callers have always read it from. The path grammar beside it is NOT
# imported here any more: this builder reads no path out of the text.
from kiro_crew.image_refs import STRIPPED_IMAGE_MARKER, strip_image_refs  # noqa: F401

# The budget constants and Pillow machinery live in the LEAF module
# kiro_crew.imaging (shared with the gateway's tool-result rewrite, which must
# not import the ACP package). The two constants are re-exported because this
# module is where the prompt path's callers and tests import them from.
from kiro_crew.imaging import (  # noqa: F401 -- constants re-exported, see comment
    MAX_IMAGE_B64_BYTES,
    MAX_IMAGE_EDGE_PX,
    downscale_image_block,
)
from kiro_crew.prompt_attachments import (  # noqa: F401 -- the constants are this module's declared surface
    IMAGE_MEDIA_TYPES,
    IMAGE_SIZE_NOTE,
    MAX_IMAGE_BYTES,
    PROMPT_LIMIT_NOTE,
    PromptAttachment,
    path_spans,
    read_image_attachment,
)

logger = logging.getLogger(__name__)

#: Caps on what ONE prompt inlines, whatever its per-image sizes. The smallest
#: backend request-body ceiling measured so far lies between a replayed request
#: of 30.4 MB of base64 (accepted) and one of 33.8 MB (refused as improperly
#: formed): 32 MiB. Three quarters of it is the images' share of a replayed
#: request, the rest text, tool results and framing, and one prompt may inline
#: half of that share -- 12 MiB. Twenty is where the backend's many-image
#: dimension rule begins, and each replayed image costs about 1,600 tokens on
#: every later turn. Both caps bound this prompt alone; what a conversation's
#: replayed history carries in total is not measured here. A picture past either
#: cap stays text, like one over the per-image cap.
MAX_PROMPT_IMAGE_BLOCKS = 20
MAX_PROMPT_IMAGE_B64_BYTES = 12 * 1024 * 1024

#: A marker-shaped token the USER typed, in any case. Escaped before the builder
#: writes its own, so only a marker this function produced reads as an
#: attachment. The replay scrubber's "[image not carried ...]" is first-party
#: text and is left alone.
_TYPED_MARKER_RE = re.compile(r"\[image(?=:|\snot\sattached:)", re.IGNORECASE)


def build_prompt_blocks(
    message: str,
    *,
    attachments: Sequence[PromptAttachment] | None = None,
    allow_image: bool = True,
    max_image_bytes: int = MAX_IMAGE_BYTES,
    max_image_edge: int = MAX_IMAGE_EDGE_PX,
    max_image_b64_bytes: int = MAX_IMAGE_B64_BYTES,
    max_prompt_image_blocks: int = MAX_PROMPT_IMAGE_BLOCKS,
    max_prompt_image_b64_bytes: int = MAX_PROMPT_IMAGE_B64_BYTES,
) -> list[dict]:
    """Return ACP prompt blocks for *message* and its *attachments*.

    Every readable image in *attachments* -- the structured list the receiving
    channel supplied -- becomes an ``image`` block. The text is NEVER scanned for
    image paths: a path that only appears in *message* is a mention, and stays
    text. For each inlined attachment the model is told what it was given by a
    ``[image: <name>]`` marker: the attachment's path is rewritten to the marker
    where the channel also wrote it into the text (Slack appends it as a bare
    line, the dashboard renders it as ``![image](path)``), and the marker is
    appended on its own line when the text never named it. A second DISTINCT
    file with the same name gets ``[image: <name> (2)]``, and so on; the same
    bytes under two names are one block, marked both places with the first
    name. Past ``max_prompt_image_blocks`` blocks or ``max_prompt_image_b64_bytes``
    of base64 in one prompt, a further picture stays text and says so: its path
    is followed by ``[image not attached: prompt image limit]`` where the text
    names it (after the closing parenthesis of a markdown destination, so the
    link stays intact), or its name and the note are appended on their own line,
    exactly as a picture over the per-image size cap, or one with no rendition
    within the size caps, is followed by ``[image not attached: image size
    limit]``. A ``[image: ...]`` or ``[image not attached: ...]`` token the user
    typed, in any case, is escaped with a backslash, so only a marker this
    builder wrote reads as an attachment.

    ``allow_image=False`` (the agent did not advertise
    ``promptCapabilities.image``) emits no image block and leaves every path in
    the text as written: the file is still on disk and the channel's own path
    text still names it, so a tool-capable agent can open it, which is a
    strictly better fallback than dropping the reference. A typed marker is
    still escaped, since the text still reaches the model. The result is always
    at least one text block, so a caller can pass it straight to
    ``session/prompt``.

    Inlined images are downscaled so their longest edge is at most
    ``max_image_edge`` px -- the server-side backstop for Anthropic's many-image
    dimension limit, applied for EVERY channel here regardless of any
    client-side resize that was skipped or bypassed -- and then shrunk further if
    needed so the base64 payload stays within ``max_image_b64_bytes``, the
    backend's per-image byte ceiling.
    """
    images: list[dict] = []
    # raw path -> the marker written for it, for every attachment this call
    # inlined (or recognised as the same bytes as one it inlined); and
    # raw path -> the note written for it, for every readable picture a cap
    # kept out of this prompt. Insertion order is the list's order.
    markers: dict[str, str] = {}
    notes: dict[str, str] = {}
    # Marker or note -> the name the never-named fallback line announces.
    names: dict[str, str] = {}

    if allow_image and attachments:
        seen: set[str] = set()
        # sha256 of the file's bytes -> marker: the same bytes under a second
        # name are the picture already attached, before any cap is consulted.
        raw_digests: dict[str, str] = {}
        # Every marker written so far, so a second DISTINCT file with the same
        # name is told from the first and no two blocks share a label.
        used_labels: set[str] = set()
        prompt_b64_bytes = 0
        past_block_cap = 0
        for attachment in attachments:
            raw = (attachment.path or "").strip()
            if not raw or raw in seen:
                continue
            seen.add(raw)
            names[raw] = attachment.display_name
            read = read_image_attachment(attachment, max_image_bytes=max_image_bytes)
            if read.raw is None or read.mime is None:
                if read.note is not None:
                    notes[raw] = read.note
                continue
            raw_digest = hashlib.sha256(read.raw).hexdigest()
            if raw_digest in raw_digests:
                # The same picture under another name: one block, marked both
                # places -- a cap never turns a duplicate into a dropped picture.
                markers[raw] = raw_digests[raw_digest]
                continue
            if len(images) >= max_prompt_image_blocks:
                # The prompt is full: a further picture stays text and says
                # so, and is not decoded to learn that.
                notes[raw] = PROMPT_LIMIT_NOTE
                past_block_cap += 1
                continue
            downscaled = downscale_image_block(
                read.raw, read.mime, max_edge=max_image_edge, max_b64_bytes=max_image_b64_bytes
            )
            if downscaled is None:
                # No compliant rendition (decompression-bomb / undecodable /
                # truncated / over the decode-pixel ceiling / still over the
                # encoded ceiling at the minimum edge): the text keeps the
                # channel's reference rather than an inline payload the backend
                # rejects on this and every later turn. A tool-capable agent
                # can still open it.
                notes[raw] = IMAGE_SIZE_NOTE
                logger.warning(
                    "acp prompt: image %s could not be rendered within the "
                    "dimension and encoded-size caps - sending path, not inline",
                    names[raw],
                )
                continue
            out_bytes, out_mime = downscaled
            data = base64.b64encode(out_bytes).decode("ascii")
            if prompt_b64_bytes + len(data) > max_prompt_image_b64_bytes:
                # Past what one prompt may carry: the picture stays text and
                # says so, exactly as one over the per-image cap does.
                notes[raw] = PROMPT_LIMIT_NOTE
                logger.warning(
                    "acp prompt: image %s is past this prompt's limit of %d base64 bytes - "
                    "sending path, not inline",
                    names[raw],
                    max_prompt_image_b64_bytes,
                )
                continue
            marker = _marker_label(names[raw], used_labels)
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

    text = _rewrite_text(message, markers, notes, names)
    return [{"type": "text", "text": text}, *images]


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


#: What is escaped inside a marker: the two characters that bound it, and the
#: backslash that escapes them. A sender's filename is written inside marker
#: syntax, so a bracket in it must not close the marker early and open a
#: second one, and a backslash it already carries must not pair with the
#: escape and leave the bracket live.
_MARKER_SYNTAX_RE = re.compile(r"[\\\[\]]")


def _marker_name(name: str) -> str:
    return _MARKER_SYNTAX_RE.sub(r"\\\g<0>", name)


def _marker_label(name: str, used: set[str]) -> str:
    """The ``[image: <name>]`` marker for a picture called *name*, unique in
    this prompt: a second distinct file with the same name is ``(2)``, and a
    name that already spells a label another picture holds moves on too."""
    base = f"[image: {_marker_name(name)}"
    label = f"{base}]"
    nth = 1
    while label in used:
        nth += 1
        label = f"{base} ({nth})]"
    used.add(label)
    return label


def _rewrite_text(
    message: str,
    markers: dict[str, str],
    notes: dict[str, str],
    names: dict[str, str],
) -> str:
    """*message* with each inlined attachment's path replaced by its marker,
    each kept-out picture's path followed by its note, a never-named picture
    announced on its own line, and typed marker-shaped tokens escaped.

    A substitution of KNOWN strings, not a scan: each path is one the channel's
    list named, and it is rewritten in every spelling the channel could have
    written it in (:func:`~kiro_crew.prompt_attachments.path_spans`: the path
    itself, its forward-slash form for a Windows path, and the escaped or
    ``<...>``-wrapped destination the dashboard composer emits inside
    ``![image](...)``) -- but only where it stands delimited, so ``/tmp/a.png.bak``
    beside an attached ``/tmp/a.png`` is another file and stays as written, and
    never inside a URL that merely contains the same characters. Every listed
    path claims its spans first, the longest span winning where two overlap,
    whether or not that path is rewritten: a path the list named is the only
    reference the model has to that file, so a shorter attachment's marker
    never lands inside it. The rewrite is one pass over the original text, in
    text order, so no edit can create or hide a span for another; a picture
    that claimed no span is announced on its own line like one the text never
    named.
    """
    # Every listed path's spans, longest first at a tie, so the span merge
    # below prefers the longer path; the owner rides along.
    claims: list[tuple[int, int, str]] = []
    for raw in names:
        claims.extend((start, end, raw) for start, end in path_spans(raw, message))
    claims.sort(key=lambda claim: (claim[0], claim[0] - claim[1]))
    owned: list[tuple[int, int, str]] = []
    claimed_end = -1
    for start, end, raw in claims:
        if start < claimed_end:
            continue
        owned.append((start, end, raw))
        claimed_end = end
    # (start, end, replacement); a kept-out picture's note is an insertion
    # after its span, a typed-marker escape an insertion before the token.
    edits: list[tuple[int, int, str]] = []
    landed: set[str] = set()
    for start, end, raw in owned:
        marker = markers.get(raw)
        note = notes.get(raw)
        if marker is not None:
            edits.append((start, end, marker))
        elif note is not None:
            close = _markdown_destination_close(message, start, end)
            # Inside a markdown destination the note would corrupt the link:
            # it follows the whole reference instead.
            at = close if close is not None else end
            edits.append((at, at, f" {note}"))
        else:
            continue
        landed.add(raw)
    # A marker-shaped token inside a listed path's own spelling is part of a
    # file reference, not a typed marker: escaping it would corrupt the one
    # reference the model has to a picture that stayed text.
    edits.extend(
        (m.start(), m.start(), "\\")
        for m in _TYPED_MARKER_RE.finditer(message)
        if not any(start <= m.start() < end for start, end, _ in owned)
    )
    if edits:
        out: list[str] = []
        pos = 0
        # An insertion at a position goes before a replacement starting there.
        for start, end, replacement in sorted(edits, key=lambda e: (e[0], e[0] != e[1])):
            if start < pos:
                continue
            out.append(message[pos:start])
            out.append(replacement)
            pos = end
        out.append(message[pos:])
        text = "".join(out)
    else:
        text = message
    # A marker the text now carries, in place or on its own line: the same
    # bytes under two names share one marker, and one picture is announced once.
    written = {markers[raw] for raw in landed if raw in markers}
    for raw, name in names.items():
        if raw in landed:
            continue
        marker = markers.get(raw)
        note = notes.get(raw)
        if marker is not None:
            if marker in written:
                continue
            written.add(marker)
            line = marker
        elif note is not None:
            line = f"{_marker_name(name)} {note}"
        else:
            continue
        text = f"{text}\n{line}" if text else line
    return text


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
