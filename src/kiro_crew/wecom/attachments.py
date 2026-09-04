"""Map WeCom media items onto the shared ingest pipeline.

``wecom/media.py`` owns the protocol work (encrypted CDN download). This module
owns only the translation: a WeCom media record becomes a channel-neutral
:class:`~kiro_crew.messaging.attachments.Attachment`, and the shared pipeline
keeps classification, per-type limits, signature validation and temp-file
ownership in one place for every channel.

The download function handed to the pipeline closes over the item's OWN
``aeskey``, because WeCom keys each object separately — there is no per-app
secret to look up, so the key has to travel with the item rather than being
resolved at download time.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
from typing import Any

import aiohttp

from kiro_crew.messaging.attachments import (
    Attachment,
    IngestLimits,
    IngestResult,
    cleanup_offloaded,
    ingest_attachments,
    safe_suffix,
    transcribe_audio_attachments,
)
from kiro_crew.wecom.media import (
    MAX_MEDIA_BYTES,
    WECOM_MAX_PLAINTEXT_BYTES,
    download_media,
    media_items,
)

logger = logging.getLogger(__name__)

#: How many attachment batches may download+decrypt at once, process-wide.
#:
#: Every inbound frame becomes its own turn task, so a burst of authorized media
#: would otherwise start an unbounded number of ingests, each holding up to
#: ``max_attachments`` x 20 MB of ciphertext AND a decrypted copy in memory before
#: any of them reaches a session lock. Two concurrent batches bounds the worst
#: case to something a gateway can survive; the rest wait, which costs latency
#: rather than the process.
_MAX_CONCURRENT_INGESTS = 2
_INGEST_GATE: asyncio.Semaphore | None = None


def _ingest_gate() -> asyncio.Semaphore:
    """The process-wide ingest bound, created on the running loop.

    Built lazily rather than at import: a module-level ``asyncio.Semaphore`` binds
    to whatever loop happens to be current at import time, which is not the
    gateway's under any test runner or a re-created loop.
    """
    global _INGEST_GATE
    if _INGEST_GATE is None:
        _INGEST_GATE = asyncio.Semaphore(_MAX_CONCURRENT_INGESTS)
    return _INGEST_GATE


def _write_bytes(dest: str, data: bytes) -> None:
    """Blocking write, offloaded by the caller (TMPDIR may not be local)."""
    with open(dest, "wb") as fh:
        fh.write(data)


#: WeCom's documented ceiling for a ``file`` object. Every non-image attachment
#: this channel can receive arrives as one, whatever its content type, so it is the
#: real bound on a document AND on an audio file. Taken from ``media`` rather than
#: restated, so this plaintext ceiling and the ciphertext download cap derived from
#: it cannot drift apart.
_WECOM_FILE_BYTES = WECOM_MAX_PLAINTEXT_BYTES

#: Per-item ceilings. The three TRANSPORT ceilings are WeCom's own documented
#: maxima rather than the shared defaults, so an item the platform accepted is not
#: refused locally (and vice versa).
#:
#: ``max_audio_bytes`` is the file ceiling, not WeCom's 2 MB voice-message limit:
#: a voice message never reaches the ingest path at all (the platform transcribes
#: it and ``media_items`` excludes it), so the only audio that gets here is a file
#: the user attached — and refusing a 5 MB ``recording.mp3`` that WeCom itself
#: carried is exactly the local-only rejection these overrides exist to prevent.
#:
#: ``max_text_bytes`` is deliberately LEFT at the shared default and is NOT a
#: transport ceiling: it budgets how much file CONTENT is read into gateway memory,
#: of which only ``max_text_inject`` (50 KiB) can ever reach the prompt. Raising it
#: to the 20 MB transport limit would read 20 MB to use 50 KiB. Slack ships the
#: same asymmetry for the same reason.
WECOM_INGEST_LIMITS = IngestLimits(
    max_image_bytes=10 * 1024 * 1024,
    max_document_bytes=_WECOM_FILE_BYTES,
    max_audio_bytes=_WECOM_FILE_BYTES,
    max_attachments=10,
)

#: MIME fallbacks by WeCom item kind, used only when the filename yields nothing.
#: The shared pipeline SNIFFS an image's real type from its leading bytes, so this
#: is a starting hint — never the authority on what a file is.
_KIND_MIME = {
    "image": "image/png",
    "file": "application/octet-stream",
    "video": "video/mp4",
}


def _to_attachment(item: dict[str, Any]) -> Attachment | None:
    """One WeCom media record as a neutral attachment, or None if unusable."""
    kind = str(item.get("kind", ""))
    url = str(item.get("url", "") or "")
    if not url or kind not in _KIND_MIME:
        return None
    name = str(item.get("filename", "") or item.get("name", "") or f"wecom-{kind}")
    size_raw = item.get("filesize", item.get("size", 0))
    try:
        size = int(size_raw)
    except (TypeError, ValueError):
        size = 0
    # A ``file`` item carries no type, and defaulting it to octet-stream made the
    # shared classifier call every document unsupported -- so a PDF was refused
    # while the shipped doc said files work. The FILENAME is the only type signal
    # WeCom gives for a document, so it is used first and the per-kind value is the
    # fallback.
    mimetype = _KIND_MIME[kind]
    if kind == "file":
        guessed, _enc = mimetypes.guess_type(name)
        if guessed:
            mimetype = guessed
    return Attachment(
        name=name,
        mimetype=mimetype,
        size=size,
        url=url,
        suffix_hint=safe_suffix(name.rsplit(".", 1)[-1] if "." in name else kind),
    )


def to_attachments(body: dict[str, Any]) -> list[tuple[Attachment, str]]:
    """Every downloadable media item in *body*, paired with its own ``aeskey``."""
    out: list[tuple[Attachment, str]] = []
    for item in media_items(body):
        att = _to_attachment(item)
        if att is None:
            continue
        out.append((att, str(item.get("aeskey", "") or "")))
    return out


async def process_wecom_attachments(
    pairs: list[tuple[Attachment, str]],
    *,
    proxy: str | None = None,
) -> IngestResult:
    """Download, decrypt and ingest inbound WeCom media.

    A dedicated ``aiohttp`` session is opened for the batch and closed with it,
    rather than borrowing the client's: these downloads outlive nothing and must
    not keep the WS session's connector busy, and a media fetch failing must not
    disturb the long connection.

    ``proxy`` MUST be supplied by the caller. Its own session does not inherit the
    client's, and aiohttp does not read ``HTTPS_PROXY`` unless asked, so on a host
    whose only egress is a proxy an unproxied download fails while the WebSocket
    (which IS proxied) stays connected and the badge stays green — the picture just
    never arrives.

    Concurrency is bounded process-wide (see ``_MAX_CONCURRENT_INGESTS``).
    """
    keys = {att.url: aeskey for att, aeskey in pairs}
    attachments = [att for att, _ in pairs]
    if not attachments:
        return IngestResult()

    async with _ingest_gate(), aiohttp.ClientSession() as session:

        async def _download(url: str, dest: str) -> None:
            # The pipeline owns the temp file and hands us its path; decryption
            # happens in memory (bounded by MAX_MEDIA_BYTES) and only the
            # plaintext is written, so a partially-decrypted object never lands on
            # disk for the sniffer to misread.
            plaintext = await download_media(
                session, url, keys.get(url, ""), proxy=proxy, max_bytes=MAX_MEDIA_BYTES
            )
            await asyncio.to_thread(_write_bytes, dest, plaintext)

        result = await ingest_attachments(
            attachments,
            download=_download,
            source="wecom",
            limits=WECOM_INGEST_LIMITS,
            # TRUE, even though a WeCom *voice message* never reaches here (the
            # platform hands back its own transcript, and ``media_items`` excludes
            # voice for that reason). What does reach here is an audio FILE the
            # user attached, e.g. recording.mp3 sent as msgtype=file. With
            # handle_audio=False the shared pipeline skips an AUDIO-classified item
            # with no rejection and no audit row, so an audio-only message produced
            # an empty turn and the sender was told nothing at all.
            handle_audio=True,
        )
    # Transcription is the channel-neutral second half, and it is what turns an
    # unavailable STT backend into a VISIBLE rejection rather than silence. Outside
    # the session block: the files are already local, and it must not hold the
    # ingest gate while a model runs.
    #
    # Guarded for the same reason the ingest loop is: a cancellation here (gateway
    # shutdown) would return nothing to the dispatcher, so its own cleanup never
    # sees these paths and the user's DECRYPTED bytes stay readable on disk.
    try:
        return await transcribe_audio_attachments(result, "wecom")
    except BaseException:
        await cleanup_offloaded(result.temp_paths)
        raise
