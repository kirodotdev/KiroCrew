"""Discord-specific adapter over channel-neutral attachment ingestion.

Discord owns the envelope mapping, CDN download callback, and voice-message
transcription. Classification, limits, signature validation, extraction,
redaction, rejection text, and temp-file ownership stay in
``kiro_crew.messaging.attachments``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from kiro_crew.messaging.attachments import (
    Attachment,
    IngestResult,
    ingest_attachments,
)
from kiro_crew.transcribe import is_available as stt_available
from kiro_crew.transcribe import transcribe_audio

if TYPE_CHECKING:
    from kiro_crew.discord.client import DiscordClient

logger = logging.getLogger(__name__)


def _to_attachment(raw: Any) -> Attachment:
    """Map one Discord attachment object onto the shared neutral shape."""
    if not isinstance(raw, dict):
        return Attachment(name="unknown")

    filename = raw.get("filename")
    name = filename if isinstance(filename, str) and filename else "unknown"
    content_type = raw.get("content_type")
    mimetype = content_type if isinstance(content_type, str) else ""
    url_value = raw.get("url")
    url = url_value if isinstance(url_value, str) else ""
    size_value = raw.get("size")
    size = (
        size_value
        if isinstance(size_value, int)
        and not isinstance(size_value, bool)
        and size_value >= 0
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


async def process_discord_attachments(
    client: DiscordClient,
    raw_attachments: list[Any],
) -> IngestResult:
    """Ingest Discord files and transcribe any ``audio/*`` attachments.

    Discord voice messages are audio attachments (currently Opus in OGG), so
    the shared classifier's normal ``audio/`` rule is sufficient: no
    channel-specific mimetype override is needed. The returned temp paths stay
    caller-owned and must live until the consuming turn finishes.
    """

    result = await ingest_attachments(
        [_to_attachment(raw) for raw in raw_attachments],
        download=client.download_attachment,
        source="discord",
        handle_audio=True,
    )

    if not result.audio_paths:
        return result

    try:
        available = await asyncio.to_thread(stt_available)
    except Exception:
        logger.exception("Discord: failed to check speech-to-text availability")
        available = False

    if not available:
        result.rejections.extend(
            "[Audio attachment — transcription is unavailable]"
            for _ in result.audio_paths
        )
        return result

    for path in result.audio_paths:
        try:
            transcript = await transcribe_audio(path)
        except Exception:
            logger.exception("Discord: audio transcription raised")
            transcript = None
        if transcript:
            result.text_blocks.append(
                "[Voice memo transcription]\n"
                f"{transcript}\n"
                "[End of transcription]"
            )
        else:
            result.rejections.append("[Audio attachment — transcription failed]")

    return result


def append_attachment_context(text: str, result: IngestResult) -> str:
    """Append prompt-ready attachment material using Slack's proven layout."""
    if result.image_paths:
        paths_text = "\n".join(result.image_paths)
        text = f"{text}\n{paths_text}" if text else paths_text

    blocks = [*result.text_blocks, *result.rejections]
    if blocks:
        blocks_text = "\n\n".join(blocks)
        text = f"{text}\n\n{blocks_text}" if text else blocks_text
    return text
