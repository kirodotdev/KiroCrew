"""Inbound media for the WhatsApp channel: bytes into the shared ingest path.

:mod:`kiro_crew.whatsapp.media` decides WHAT arrived from the protobuf alone.
This is the other half: it fetches the bytes and hands them to
``messaging.attachments``, which owns the parts no channel should re-implement
(the size ceilings, the raster sniff by leading bytes, the document text
extraction, the redaction, and the prompt framing).

Two things here are not obvious from the call site:

- **The download is one blob, not a URL.** Every other channel hands the shared
  layer an HTTPS URL and lets it fetch. WhatsApp media is end-to-end encrypted
  and only the paired client can decrypt it, so the fetch goes through neonize
  and this module writes the plaintext to the destination the shared layer picked.
  ``Attachment.url`` therefore carries an opaque handle (the message id) purely
  to satisfy the shared layer's "has a source" check: a blank url is rejected
  there with ``[no download URL]``.
- **A voice note needs a filename with a real extension.** WhatsApp sends none,
  and the transcription backend selects its decoder from the suffix, so a voice
  note downloaded as ``voice`` with no extension transcribes to nothing. The
  suffix is derived from the mimetype, with ``audio/ogg`` pinned because
  WhatsApp ships voice as ``audio/ogg; codecs=opus``.
- **That derived suffix fills a gap; it never overrules the sender's name.** A
  document is the one kind that arrives WITH a filename, and the ingest layer
  picks its parser from that name's extension. A declared type is only a claim,
  so a PDF sent as ``application/octet-stream`` derives ``.bin``: appended, that
  turns ``report.pdf`` into ``report.pdf.bin``, which matches no parser and is
  refused without ever being downloaded.

Dependency direction is ``whatsapp -> messaging`` (allowed).
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
from pathlib import Path
from typing import Any

from kiro_crew.messaging.attachments import (
    Attachment,
    IngestLimits,
    IngestResult,
    ingest_attachments,
    transcribe_audio_attachments,
)
from kiro_crew.platform_compat import host_available_mib
from kiro_crew.whatsapp.media import KIND_VOICE, MediaDescription

logger = logging.getLogger(__name__)

#: One media object per message: WhatsApp sends one attachment per message, so a
#: higher cap could never fire and would only make the limit read as a policy
#: this channel does not have.
MAX_ATTACHMENTS_PER_MESSAGE = 1

#: Ceiling on a single downloaded object. OUR policy, not a sourced platform
#: figure: neither whatsmeow nor neonize declares a byte cap, and the sender's
#: declared ``fileLength`` is an unverified claim, so this bounds what the
#: gateway will hold in memory before the shared layer even sees it.
MAX_MEDIA_BYTES = 24 * 1024 * 1024

#: Multiple of a declared object's size that must be FREE before it is fetched.
#: The binding returns the whole object and the bytes are then copied once for the
#: off-loop write, so the transient peak is about twice the object; the rest is
#: headroom so a fetch cannot be the allocation that tips a starved host over.
_MEMORY_HEADROOM_FACTOR = 3

#: Mimetypes to treat as audio on this channel, beyond the shared defaults.
#: WhatsApp voice notes are ``audio/ogg; codecs=opus``.
AUDIO_MIMETYPES = ("audio/ogg", "audio/mpeg", "audio/mp4", "audio/aac", "audio/amr")

#: Fallback suffixes where ``mimetypes`` guesses badly or not at all. ``audio/ogg``
#: is the one that matters: the stdlib may answer ``.oga``, which the
#: transcription backend does not recognise.
_SUFFIX_OVERRIDES = {
    "audio/ogg": ".ogg",
    "image/webp": ".webp",
    "audio/amr": ".amr",
}

_DEFAULT_NAMES = {
    "image": "image",
    "sticker": "sticker",
    "voice": "voice",
    "audio": "audio",
    "document": "document",
}


def _base_of(mimetype: str) -> str:
    """*mimetype* lowercased with any parameters stripped."""
    return (mimetype or "").split(";", 1)[0].strip().lower()


def _pinned_suffix(mimetype: str) -> str:
    """The suffix :data:`_SUFFIX_OVERRIDES` pins for *mimetype*, or ``""``."""
    return _SUFFIX_OVERRIDES.get(_base_of(mimetype), "")


def _suffix_for(mimetype: str) -> str:
    """A filename suffix the transcription and vision paths can act on."""
    base = _base_of(mimetype)
    return _pinned_suffix(base) or mimetypes.guess_extension(base) or ""


def attachment_for(desc: MediaDescription, message_id: str) -> Attachment:
    """The shared :class:`Attachment` describing *desc*.

    ``size`` is the SENDER's declared length and is advisory: nothing verifies it
    before the fetch, so it informs the shared layer's early reject but is not the
    bound. :data:`MAX_MEDIA_BYTES` is.

    The extension the sender put on the name wins over the one derived from the
    declared type (see the module docstring): the derived suffix is appended only
    when the name carries none, which is every voice note, image and sticker,
    because WhatsApp sends a filename for documents alone.
    """
    mime_suffix = _suffix_for(desc.base_mimetype)
    name = desc.filename or f"{_DEFAULT_NAMES.get(desc.kind, 'file')}{mime_suffix}"
    own_suffix = Path(name).suffix
    if mime_suffix and not own_suffix:
        name = f"{name}{mime_suffix}"
        own_suffix = mime_suffix
    # A pinned suffix outranks the name as well, for the reason it exists at all:
    # the transcription backend does not recognise what this type is otherwise
    # called, and an ``audio/ogg`` attachment the sender named ``note.oga``
    # arrives at that same unrecognised suffix by the sender's route rather than
    # the stdlib's.
    hint = _pinned_suffix(desc.base_mimetype) or own_suffix or mime_suffix
    return Attachment(
        name=name,
        mimetype=desc.base_mimetype,
        size=int(desc.file_length or 0),
        # Opaque handle, not a fetchable URL: the shared layer only checks that a
        # source exists, and the real fetch goes through the paired client below.
        url=message_id or "whatsapp-media",
        suffix_hint=hint.lstrip("."),
    )


async def ingest_media(
    client: Any,
    message: Any,
    desc: MediaDescription,
    message_id: str,
) -> IngestResult:
    """Download *message*'s media and return prompt-ready material.

    Never raises: a single unreadable object becomes a rejection the caller
    surfaces, because losing the whole message over one failed photo is worse
    than telling the user the photo did not arrive.
    """

    async def _download(_handle: str, dest: str) -> None:
        """Fetch the decrypted bytes and write them to *dest*.

        The write is offloaded because the object can be tens of megabytes and
        ``TMPDIR`` is not guaranteed to be local disk: writing inline would stall
        the one gateway loop, and with it every other session and the liveness
        heartbeat. neonize's own ``path=`` form writes inline for exactly that
        reason, so the bytes form is used instead.
        """
        # The ceiling has to act on the DECLARED length, because there is nowhere
        # later that it can: the pinned binding returns the whole decrypted object
        # in one value (no streaming and no size-limited download symbol exists),
        # so by the time a length can be measured the allocation has happened. The
        # check below still runs, and it is what keeps an UNDERSTATED object off
        # the disk and out of the prompt; this one is what keeps the obvious cases
        # out of memory.
        declared = int(desc.file_length or 0)
        if declared <= 0:
            # Every media protobuf carries `fileLength` and `describe` reads it for
            # every kind, so an absent or zero length is not a legitimate shape: it
            # is the one input that makes the shared layer's `att.size and ...`
            # pre-check skip itself, which is exactly the bypass being closed.
            raise ValueError("whatsapp media declares no length; refusing to fetch it")
        if declared > MAX_MEDIA_BYTES:
            raise ValueError(f"media declares {declared} bytes, over the {MAX_MEDIA_BYTES} ceiling")
        free_mib = host_available_mib()
        # 0 means "could not determine", never "no memory", so an unreadable
        # reading allows the fetch rather than disabling media on that host.
        if free_mib and declared * _MEMORY_HEADROOM_FACTOR > free_mib * 1024 * 1024:
            raise ValueError(
                f"media declares {declared} bytes with only {free_mib} MiB free; refusing to fetch it"
            )
        data = await client.download_media(message)
        if not isinstance(data, (bytes, bytearray)):
            raise ValueError("whatsapp media download returned no bytes")
        if len(data) > MAX_MEDIA_BYTES:
            raise ValueError(f"media is {len(data)} bytes, over the {MAX_MEDIA_BYTES} ceiling")
        await asyncio.to_thread(Path(dest).write_bytes, bytes(data))

    result = await ingest_attachments(
        [attachment_for(desc, message_id)],
        download=_download,
        source="whatsapp",
        limits=IngestLimits(max_attachments=MAX_ATTACHMENTS_PER_MESSAGE),
        handle_audio=True,
        audio_mimetypes=AUDIO_MIMETYPES,
    )
    if desc.kind == KIND_VOICE or result.audio_paths:
        result = await transcribe_audio_attachments(result, "WhatsApp")
    return result
