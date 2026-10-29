"""Inbound files for the Signal channel: signal-cli's stored copies into the shared ingest.

The shared half is :mod:`kiro_crew.messaging.attachments`: the per-kind size
ceilings, the raster sniff by leading bytes, document text extraction, the
redaction, the temp-file staging and the prompt framing. This module supplies
only what is true of Signal, the same split :mod:`kiro_crew.whatsapp.attachments`
makes:

- **The fetch is ``getAttachment``, not a URL.** signal-cli downloads and
  decrypts every attachment itself while it receives, stores it under its data
  dir, and names it in the envelope by an id. ``getAttachment`` hands the bytes
  back base64-encoded, so the channel never has to know (or guess) where that
  data dir is. ``Attachment.url`` therefore carries the id purely to satisfy the
  shared layer's "has a source" check.
- **The bytes come back on one JSON-RPC line**, and the peer drops any line past
  :data:`kiro_crew.imessage.rpc.STDOUT_LINE_LIMIT` (the call would then wait out
  its timeout). :data:`MAX_MEDIA_BYTES` is derived from that limit so an
  attachment that cannot come back is refused up front with a reason instead.
- **A voice note needs a filename with a real extension.** Signal sends none, and
  the transcription backend picks its decoder from the suffix, so the suffix is
  derived from the type (Signal records voice as ``audio/aac``) and fills a gap
  only: a document's own name keeps its own extension, because the ingest layer
  picks its parser from it.

Voice notes are transcribed through the same shared
:func:`~kiro_crew.messaging.attachments.transcribe_audio_attachments` WhatsApp
uses.

Dependency direction is ``signal_cli -> messaging`` (allowed).
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew.imessage.rpc import STDOUT_LINE_LIMIT
from kiro_crew.messaging.attachments import (
    Attachment,
    IngestLimits,
    IngestResult,
    cleanup_offloaded,
    ingest_attachments,
    transcribe_audio_attachments,
)
from kiro_crew.platform_compat import host_available_mib

if TYPE_CHECKING:
    from kiro_crew.signal_cli.client import SignalAttachmentRef, SignalClient

logger = logging.getLogger(__name__)

#: Attachments ingested from one message. Signal lets a sender attach many
#: files to one message (an album); this bounds the fetches and the prompt one
#: message can cause. Our policy, not a platform figure. Past it, the shared
#: layer says how many were ignored.
MAX_ATTACHMENTS_PER_MESSAGE = 4

#: Room in a ``getAttachment`` answer line for everything that is not the
#: base64 payload (the JSON-RPC envelope around ``{"data": ...}``).
_RESPONSE_OVERHEAD_BYTES = 4096

#: Ceiling on one fetched attachment. Derived, not chosen: the bytes return
#: base64-encoded (4 characters per 3 bytes) on ONE stdout line, and the peer
#: drops a line past :data:`STDOUT_LINE_LIMIT`, so anything larger could never
#: arrive. Below WhatsApp's 24 MiB ceiling for that reason; the shared per-kind
#: caps (``IngestLimits``) still apply on top.
MAX_MEDIA_BYTES = (STDOUT_LINE_LIMIT - _RESPONSE_OVERHEAD_BYTES) * 3 // 4

#: Multiple of a declared size that must be free before it is fetched: the JSON
#: line, the decoded bytes and the off-loop copy are all alive at once (the
#: same headroom reasoning as WhatsApp's).
_MEMORY_HEADROOM_FACTOR = 4

#: Types to treat as audio on this channel, beyond the shared defaults. Signal
#: records voice notes as ``audio/aac``.
AUDIO_MIMETYPES = ("audio/aac", "audio/mp4", "audio/mpeg", "audio/ogg", "audio/x-m4a")

#: Suffixes the transcription backend recognises where the stdlib guesses
#: badly or not at all (``audio/aac`` has no stdlib mapping on every Python).
_SUFFIX_OVERRIDES = {
    "audio/aac": ".aac",
    "audio/ogg": ".ogg",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "image/webp": ".webp",
}


def _base_of(mimetype: str) -> str:
    """*mimetype* lowercased with any parameters stripped."""
    return (mimetype or "").split(";", 1)[0].strip().lower()


def _suffix_for(mimetype: str) -> str:
    base = _base_of(mimetype)
    return _SUFFIX_OVERRIDES.get(base) or mimetypes.guess_extension(base) or ""


def _default_name(ref: "SignalAttachmentRef") -> str:
    base = _base_of(ref.content_type)
    if ref.voice_note:
        return "voice"
    for prefix in ("image", "audio", "video"):
        if base.startswith(f"{prefix}/"):
            return prefix
    return "file"


def attachment_for(ref: "SignalAttachmentRef") -> Attachment:
    """The shared :class:`Attachment` describing *ref*.

    The sender's filename is untrusted display text: it never names a path (the
    shared layer stages every file under a fresh temp name), it only labels the
    attachment and lends its extension to the parser choice. Only its final
    component is kept.
    """
    mime_suffix = _suffix_for(ref.content_type)
    name = Path(ref.filename.replace("\\", "/")).name if ref.filename else ""
    name = name or f"{_default_name(ref)}{mime_suffix}"
    own_suffix = Path(name).suffix
    if mime_suffix and not own_suffix:
        name = f"{name}{mime_suffix}"
        own_suffix = mime_suffix
    hint = _SUFFIX_OVERRIDES.get(_base_of(ref.content_type)) or own_suffix or mime_suffix
    return Attachment(
        name=name,
        mimetype=_base_of(ref.content_type),
        size=ref.size,
        # The id, not a URL: the fetch goes through signal-cli (see the module
        # docstring); the shared layer only checks that a source exists.
        url=ref.id,
        suffix_hint=hint.lstrip("."),
    )


async def ingest_inbound_attachments(
    client: "SignalClient", refs: "list[SignalAttachmentRef]", sender: str
) -> IngestResult:
    """Fetch *refs* through signal-cli and return prompt-ready material.

    Never raises for one bad attachment: each failure becomes a rejection the
    caller surfaces, because losing the whole message over one unreadable photo
    is worse than telling the user that photo did not arrive. The caller owns
    the returned ``temp_paths`` and must clean them up.
    """
    by_id = {ref.id: ref for ref in refs}

    async def _download(attachment_id: str, dest: str) -> None:
        ref = by_id.get(attachment_id)
        declared = ref.size if ref is not None else 0
        # The declared size is the sender's claim; it refuses the obvious cases
        # before anything is held in memory. The length check after the fetch
        # is the one that holds.
        if declared > MAX_MEDIA_BYTES:
            raise ValueError(f"over this channel's {MAX_MEDIA_BYTES}-byte limit")
        free_mib = host_available_mib()
        # 0 means "could not determine", never "no memory".
        if declared and free_mib and declared * _MEMORY_HEADROOM_FACTOR > free_mib * 1024 * 1024:
            raise ValueError("not enough free memory to fetch it")
        data = await client.get_attachment(attachment_id, sender)
        if len(data) > MAX_MEDIA_BYTES:
            raise ValueError(f"over this channel's {MAX_MEDIA_BYTES}-byte limit")
        # Off the loop: TMPDIR is not guaranteed to be local disk.
        await asyncio.to_thread(Path(dest).write_bytes, data)

    attachments = [attachment_for(ref) for ref in refs]
    result = await ingest_attachments(
        attachments,
        download=_download,
        source="signal",
        limits=IngestLimits(max_attachments=MAX_ATTACHMENTS_PER_MESSAGE),
        handle_audio=True,
        audio_mimetypes=AUDIO_MIMETYPES,
    )
    if result.audio_paths:
        # The caller owns ``temp_paths`` only once this returns. Transcription
        # can be cancelled (a /stop, a shutdown) after the ingest wrote the
        # decrypted files, and the caller would then never see their paths, so
        # this owns the cleanup on that path, as ``ingest_attachments`` does for
        # its own.
        try:
            result = await transcribe_audio_attachments(result, "Signal")
        except BaseException:
            await cleanup_offloaded(result.temp_paths)
            raise
    return result
