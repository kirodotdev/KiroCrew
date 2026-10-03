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
import json
import logging
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
from kiro_crew.prompt_attachments import (  # noqa: F401 -- the two constants are this module's declared surface
    IMAGE_MEDIA_TYPES,
    MAX_IMAGE_BYTES,
    PromptAttachment,
    inline_image_payload,
    path_spans,
)

logger = logging.getLogger(__name__)


def build_prompt_blocks(
    message: str,
    *,
    attachments: Sequence[PromptAttachment] | None = None,
    allow_image: bool = True,
    max_image_bytes: int = MAX_IMAGE_BYTES,
    max_image_edge: int = MAX_IMAGE_EDGE_PX,
    max_image_b64_bytes: int = MAX_IMAGE_B64_BYTES,
) -> list[dict]:
    """Return ACP prompt blocks for *message* and its *attachments*.

    Every readable image in *attachments* -- the structured list the receiving
    channel supplied -- becomes an ``image`` block. The text is NEVER scanned for
    image paths: a path that only appears in *message* is a mention, and stays
    text. For each inlined attachment the model is told what it was given by a
    ``[image: <name>]`` marker: the attachment's path is rewritten to the marker
    where the channel also wrote it into the text (Slack appends it as a bare
    line, the dashboard renders it as ``![image](path)``), and the marker is
    appended on its own line when the text never named it.

    ``allow_image=False`` (the agent did not advertise
    ``promptCapabilities.image``) emits no image block and leaves the text
    untouched: the file is still on disk and the channel's own path text still
    names it, so a tool-capable agent can open it, which is a strictly better
    fallback than dropping the reference. The result is always at least one
    text block, so a caller can pass it straight to ``session/prompt``.

    Inlined images are downscaled so their longest edge is at most
    ``max_image_edge`` px -- the server-side backstop for Anthropic's many-image
    dimension limit, applied for EVERY channel here regardless of any
    client-side resize that was skipped or bypassed -- and then shrunk further if
    needed so the base64 payload stays within ``max_image_b64_bytes``, the
    backend's per-image byte ceiling.
    """
    text = message
    images: list[dict] = []

    if allow_image and attachments:
        seen: set[str] = set()
        for attachment in attachments:
            raw = (attachment.path or "").strip()
            if not raw or raw in seen:
                continue
            seen.add(raw)
            downscaled = inline_image_payload(
                attachment,
                max_image_bytes=max_image_bytes,
                max_image_edge=max_image_edge,
                max_image_b64_bytes=max_image_b64_bytes,
            )
            if downscaled is None:
                continue
            out_bytes, out_mime = downscaled
            data = base64.b64encode(out_bytes).decode("ascii")
            images.append({"type": "image", "data": data, "mimeType": out_mime})
            text = _mark_attachment(text, raw, attachment.display_name)

    return [{"type": "text", "text": text}, *images]


def _mark_attachment(text: str, raw_path: str, name: str) -> str:
    """*text* with the inlined attachment marked as ``[image: <name>]``.

    A substitution of a KNOWN string, not a scan: the path is the one the
    channel's list named, and every spelling the channel could have written it
    in is replaced (:func:`~kiro_crew.prompt_attachments.path_spans`: the
    path itself, its forward-slash form for a Windows path, and the escaped or
    ``<...>``-wrapped destination the dashboard composer emits inside
    ``![image](...)``) -- but only where it stands delimited, so
    ``/tmp/a.png.bak`` beside an attached ``/tmp/a.png`` is another file and
    stays as written. A text that never named the path gets the marker
    appended on its own line, so the model is told what it was given either
    way.
    """
    marker = f"[image: {name}]"
    spans = path_spans(raw_path, text)
    if not spans:
        return f"{text}\n{marker}" if text else marker
    for start, end in reversed(spans):
        text = text[:start] + marker + text[end:]
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
