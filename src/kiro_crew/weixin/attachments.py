"""Weixin-specific adapter over channel-neutral attachment ingestion.

Weixin owns only what is genuinely iLink-shaped: the envelope mapping
(``image_item`` / ``voice_item`` / ``file_item`` / ``video_item`` -> the shared
:class:`Attachment`) and the CDN download-and-decrypt callback. Classification,
size limits, image-signature validation, text extraction, redaction, rejection
wording, transcription and temp-file ownership all stay in
:mod:`kiro_crew.messaging.attachments`.

Mirrors :mod:`kiro_crew.telegram.attachments` and
:mod:`kiro_crew.discord.attachments` -- same shared pipeline, only the envelope
shape and the download mechanics differ.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
from pathlib import Path
from typing import Any

import aiohttp

from kiro_crew.messaging.attachments import (
    Attachment,
    IngestResult,
    append_attachment_context,
    ingest_attachments,
    transcribe_audio_attachments,
)
from kiro_crew.weixin.client import ITEM_FILE, ITEM_IMAGE, ITEM_VIDEO, ITEM_VOICE
from kiro_crew.weixin.media import CDN_BASE_URL, download_media, media_ref

logger = logging.getLogger(__name__)

# ``append_attachment_context`` is channel-neutral and lives in
# ``messaging.attachments``; re-exported so one module supplies the whole
# ingest+append pair (mirrors discord/telegram).
__all__ = [
    "append_attachment_context",
    "process_weixin_attachments",
]

#: The item field carrying the CDN reference, per item type.
_MEDIA_FIELD = {
    ITEM_IMAGE: "image_item",
    ITEM_VOICE: "voice_item",
    ITEM_FILE: "file_item",
    ITEM_VIDEO: "video_item",
}

#: ``voice_item.encode_type`` -> (mimetype, extension). iLink voice is normally
#: SILK (6), which no transcription backend we ship can read; it is still mapped
#: to an ``audio/*`` type so the shared pipeline routes it down the audio path
#: and produces a visible "transcription failed" note instead of silence.
_VOICE_ENCODINGS = {
    1: ("audio/L16", "pcm"),
    2: ("audio/adpcm", "adpcm"),
    4: ("audio/speex", "spx"),
    5: ("audio/amr", "amr"),
    6: ("audio/silk", "silk"),
    7: ("audio/mpeg", "mp3"),
    8: ("audio/ogg", "ogg"),
}

#: iLink never labels an image's format -- the envelope carries only CDN
#: coordinates. Declaring JPEG is safe rather than a guess that can mislead:
#: ``messaging.attachments`` re-sniffs every downloaded image by magic bytes,
#: rejects non-images outright, and renames the temp file to what it actually
#: is, so a PNG declared here as JPEG still reaches the model correctly typed.
_IMAGE_FALLBACK_MIME = "image/jpeg"


def _int_or_zero(value: Any) -> int:
    """*value* as a non-negative int, else 0 (iLink ships sizes as int OR str)."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value if value >= 0 else 0
    if isinstance(value, str):
        try:
            parsed = int(value.strip() or "0")
        except ValueError:
            return 0
        return parsed if parsed >= 0 else 0
    return 0


def _voice_mime_ext(sub: dict[str, Any]) -> tuple[str, str]:
    encode_type = sub.get("encode_type")
    if isinstance(encode_type, int) and encode_type in _VOICE_ENCODINGS:
        return _VOICE_ENCODINGS[encode_type]
    return _VOICE_ENCODINGS[6]


def _describe(item: dict[str, Any]) -> tuple[str, str, int]:
    """``(name, mimetype, size)`` for one media item."""
    item_type = item.get("type")
    field = _MEDIA_FIELD[item_type] if item_type in _MEDIA_FIELD else ""
    sub = item.get(field) if field else None
    sub = sub if isinstance(sub, dict) else {}

    if item_type == ITEM_IMAGE:
        size = _int_or_zero(sub.get("hd_size")) or _int_or_zero(sub.get("mid_size"))
        return "image.jpg", _IMAGE_FALLBACK_MIME, size

    if item_type == ITEM_VOICE:
        mime, ext = _voice_mime_ext(sub)
        return f"voice.{ext}", mime, 0

    if item_type == ITEM_FILE:
        raw_name = sub.get("file_name")
        name = raw_name if isinstance(raw_name, str) and raw_name.strip() else "file.bin"
        mime = mimetypes.guess_type(name)[0] or ""
        return name, mime, _int_or_zero(sub.get("len"))

    if item_type == ITEM_VIDEO:
        return "video.mp4", "video/mp4", _int_or_zero(sub.get("video_size"))

    return "", "", 0


def _to_attachment(item: dict[str, Any]) -> tuple[Attachment, str] | None:
    """Map one media item to ``(Attachment, aes_key_b64)``, or None if not media.

    The CDN's ``encrypt_query_param`` is carried in :attr:`Attachment.url` as an
    opaque handle -- the same shape Telegram uses for ``file_id`` -- and the AES
    key travels beside it so it never has to be re-derived at download time. An
    item with no ``encrypt_query_param`` yields ``url=""``, which the shared
    pipeline already turns into a visible "no download URL" rejection.
    """
    if not isinstance(item, dict) or item.get("type") not in _MEDIA_FIELD:
        return None
    name, mimetype, size = _describe(item)
    if not name:
        return None
    param, aes_key = media_ref(item, _MEDIA_FIELD[item["type"]])
    suffix = name.rsplit(".", 1)[-1] if "." in name else ""
    return (
        Attachment(name=name, mimetype=mimetype, size=size, url=param, suffix_hint=suffix),
        aes_key,
    )


def _voice_transcript(item: Any) -> str | None:
    """Server-side speech-to-text already present on ONE inbound voice item.

    iLink sometimes fills ``voice_item.text`` with its own transcription. Using
    it costs no download and no local STT, and it is the ONLY path that yields a
    usable transcript for SILK-encoded voice, which our transcription backends
    cannot decode. Returns ``None`` when this item carries no usable transcript,
    so the caller can still download that one item -- a transcript on one voice
    message says nothing about the next.
    """
    if not isinstance(item, dict) or item.get("type") != ITEM_VOICE:
        return None
    sub = item.get("voice_item")
    if not isinstance(sub, dict):
        return None
    text = sub.get("text")
    if isinstance(text, str) and text.strip():
        return f"[Voice message transcript]\n{text.strip()}"
    return None


async def process_weixin_attachments(
    items: list[Any],
    *,
    cdn_base_url: str = CDN_BASE_URL,
    session: aiohttp.ClientSession | None = None,
) -> IngestResult:
    """Download, decrypt and ingest the media items of one inbound message.

    A voice item that already carries a server-side transcript short-circuits:
    its text is used directly and that item's audio is never downloaded, because
    SILK (iLink's default voice codec) is not decodable by our transcription
    backends, making the local path strictly worse when the server gave us one.
    The short-circuit is per item -- a transcript on one voice message does not
    suppress the download of another that came without one.

    The returned temp paths stay caller-owned and must outlive the consuming
    turn -- exactly as for Telegram and Discord.
    """
    server_transcripts: list[str] = []
    transcribed_idx: set[int] = set()
    for idx, item in enumerate(items):
        transcript = _voice_transcript(item)
        if transcript is not None:
            server_transcripts.append(transcript)
            transcribed_idx.add(idx)

    pairs: list[tuple[Attachment, str]] = []
    for idx, item in enumerate(items):
        # Skip the audio download only for the items the server transcribed.
        if idx in transcribed_idx:
            continue
        mapped = _to_attachment(item)
        if mapped is None:
            continue
        pairs.append(mapped)

    if not pairs:
        result = IngestResult()
        result.text_blocks.extend(server_transcripts)
        return result

    keys = {att.url: aes_key for att, aes_key in pairs if att.url}

    owned_session = session is None
    http = session or aiohttp.ClientSession()
    try:

        async def _download(param: str, dest: str) -> None:
            """Download callback: CDN fetch + AES-128-ECB decrypt -> *dest*.

            The write is offloaded: a decrypted object can be up to
            ``MAX_CDN_BYTES`` (32 MB) and ``TMPDIR`` is not guaranteed to be
            local disk, so writing it inline would stall the single gateway
            event loop -- and with it every other session and the liveness
            heartbeat -- for the duration of the write. Mirrors the Telegram and
            Discord download callbacks.
            """
            data = await download_media(
                http,
                encrypt_query_param=param,
                aes_key_b64=keys.get(param, ""),
                cdn_base_url=cdn_base_url,
            )
            await asyncio.to_thread(Path(dest).write_bytes, data)

        result = await ingest_attachments(
            [att for att, _ in pairs],
            download=_download,
            source="weixin",
            handle_audio=True,
        )
    finally:
        if owned_session:
            await http.close()

    result.text_blocks.extend(server_transcripts)
    return await transcribe_audio_attachments(result, "Weixin")
