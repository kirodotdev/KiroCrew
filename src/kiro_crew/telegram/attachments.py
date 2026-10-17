"""Telegram-specific adapter over channel-neutral attachment ingestion.

Telegram owns only what is genuinely Telegram-shaped: the envelope mapping
(photo/document/audio/voice/video → shared ``Attachment``) and the two-step
``getFile`` download callback. Classification, limits, signature validation,
extraction, redaction, rejection text, transcription and temp-file ownership all
stay in ``kiro_crew.messaging.attachments``.

Mirrors ``kiro_crew.discord.attachments`` — same shared pipeline, only the
envelope shape and the download auth differ.
"""

from __future__ import annotations

import mimetypes
from typing import TYPE_CHECKING, Any

from kiro_crew.messaging.attachments import (
    Attachment,
    IngestResult,
    append_attachment_context,
    ingest_attachments,
    transcribe_audio_attachments,
)

if TYPE_CHECKING:
    from kiro_crew.telegram.client import TelegramClient

# ``append_attachment_context`` is channel-neutral and lives in
# ``messaging.attachments``; re-exported here so a caller can take the whole
# ingest+append pair from one module (mirrors ``discord.attachments``).
# Declared in ``__all__`` rather than carrying a blanket ``# noqa: F401`` on
# the import block, which would also suppress genuine unused-import warnings
# for the names above that ARE used.
__all__ = [
    "append_attachment_context",
    "process_telegram_attachments",
]


#: Extensions for the mimetypes Telegram delivers WITHOUT a ``file_name`` --
#: voice notes and video_notes carry only ``file_id`` + ``mime_type``. Pinned
#: rather than left to ``mimetypes.guess_extension`` because that maps
#: ``audio/ogg`` to ``.oga``, which some transcription backends reject.
_MIME_EXT_OVERRIDES = {
    "audio/ogg": ".ogg",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/x-wav": ".wav",
    "audio/wav": ".wav",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "image/jpeg": ".jpg",
    "image/png": ".png",
}


def _ext_for_mime(mimetype: str) -> str:
    """A concrete file extension for *mimetype*, defaulting to ``.bin``."""
    if not mimetype:
        return ".bin"
    override = _MIME_EXT_OVERRIDES.get(mimetype.lower())
    if override:
        return override
    return mimetypes.guess_extension(mimetype.lower()) or ".bin"


def _to_attachment(raw: Any) -> Attachment:
    """Map one Telegram file object onto the shared neutral shape.

    Telegram's file objects vary by type:
    - Photo: ``file_id``, ``file_unique_id``, ``width``, ``height``,
      ``file_size`` — no ``file_name``/``mime_type``, so ``_dispatch``
      synthesizes them.
    - Document/Audio/Voice/Video: adds ``file_name`` and ``mime_type``.

    The download URL is NOT on the object — it requires a ``getFile`` round
    trip — so ``file_id`` is carried in ``Attachment.url`` and the download
    callback resolves it.
    """
    if not isinstance(raw, dict):
        return Attachment(name="unknown")

    mime_type = raw.get("mime_type")
    mimetype = mime_type if isinstance(mime_type, str) else ""
    file_name = raw.get("file_name")
    if isinstance(file_name, str) and file_name:
        name = file_name
    else:
        # Voice notes and video_notes have NO file_name. A literal "file"
        # fallback gave the downloaded temp file a ".file" suffix, and the
        # transcription backend keys off the extension -- so the voice memo was
        # downloaded, handed over, and then rejected as an unknown format.
        # Derive the extension from mime_type instead.
        name = f"file{_ext_for_mime(mimetype)}"
    file_id = raw.get("file_id")
    url = file_id if isinstance(file_id, str) else ""
    file_size = raw.get("file_size")
    size = (
        file_size
        if isinstance(file_size, int) and not isinstance(file_size, bool) and file_size >= 0
        else 0
    )
    suffix = name.rsplit(".", 1)[-1] if "." in name else ""
    return Attachment(
        name=name,
        mimetype=mimetype,
        size=size,
        url=url,
        suffix_hint=suffix,
    )


async def process_telegram_attachments(
    client: "TelegramClient",
    raw_attachments: list[Any],
) -> IngestResult:
    """Ingest Telegram files and transcribe any audio/voice attachments.

    Telegram voice messages are ``audio/ogg`` (Opus in an OGG container), so the
    shared classifier's normal ``audio/`` rule already covers them — no
    channel-specific mimetype override is needed. The returned temp paths stay
    caller-owned and must outlive the consuming turn.
    """

    async def _download(file_id: str, dest: str) -> None:
        """Download callback: resolves ``file_id`` via Telegram's getFile."""
        await client.download_file(file_id, dest)

    result = await ingest_attachments(
        [_to_attachment(raw) for raw in raw_attachments],
        download=_download,
        source="telegram",
        handle_audio=True,
    )
    return await transcribe_audio_attachments(result, "Telegram")
