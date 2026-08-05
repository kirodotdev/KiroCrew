"""Slack file attachment processing — a thin adapter over the neutral layer.

Slack owns exactly two things here: mapping its envelope (``files[]`` dicts with
``url_private_download`` / ``filetype`` / ``mimetype``) onto
:class:`~kiro_crew.messaging.attachments.Attachment`, and supplying an
authenticated download callback. Classification, size caps, magic-byte
validation, redaction, document extraction, SEL audit, and temp cleanup all live
in :mod:`kiro_crew.messaging.attachments` so other channels reuse them instead of
growing a second copy.

Audio is skipped here: Slack transcribes it on a separate upstream path
(``transcribe.py``) before file processing runs.

Behaviour:
- returns an ``(image_paths, text_blocks)`` contract that ``events.py`` and the
  busy-message queue consume directly
- images land in temp files the caller must delete
- text and documents are redacted, then truncated, then wrapped in
  ``[File: …]`` / ``[Document: …]`` markers
- size is re-checked AFTER download, so an absent or dishonest Slack ``size``
  (which defaults to 0) cannot bypass the cap
- image bytes must match the declared mimetype's signature
- a per-message attachment cap is enforced
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from kiro_crew.messaging.attachments import (
    Attachment,
    IngestLimits,
    ingest_attachments,
    safe_suffix,
)

if TYPE_CHECKING:
    from kiro_crew.slack.gateway import GatewayOrchestrator

logger = logging.getLogger(__name__)

# Slack's shipped caps, expressed against the neutral limit object.
_LIMITS = IngestLimits(
    max_image_bytes=10 * 1024 * 1024,
    max_text_bytes=512 * 1024,
    max_document_bytes=20 * 1024 * 1024,
    max_text_inject=50 * 1024,
)

# Re-exported names. `test_slack_files.py` asserts against these directly, so
# keeping them exported keeps that suite a real regression check.
_MAX_IMAGE_BYTES = _LIMITS.max_image_bytes
_MAX_TEXT_BYTES = _LIMITS.max_text_bytes
_MAX_DOC_BYTES = _LIMITS.max_document_bytes
_MAX_TEXT_INJECT = _LIMITS.max_text_inject

#: Mimetype prefixes Slack uses for voice memos. ``video/webm`` is NOT a
#: mistake: Slack ships voice clips with a video container mimetype, and the
#: transcription path in ``slack/events.py`` has always relied on that. Defined
#: HERE (not in events.py) because this module sits below it in the import
#: graph, so both the transcriber and the ingestion adapter can share ONE
#: definition. A single definition keeps them in sync, so a transcribed voice
#: memo is not also rejected as unsupported video.
SLACK_AUDIO_MIMETYPES: tuple[str, ...] = ("audio/", "video/webm")
_safe_suffix = safe_suffix


def _to_attachment(f: dict) -> Attachment:
    """Map one Slack ``files[]`` entry onto the neutral shape."""
    return Attachment(
        name=f.get("name", "unknown"),
        mimetype=f.get("mimetype", ""),
        size=f.get("size", 0) or 0,
        # url_private_download serves raw bytes; url_private can return an HTML
        # preview page for some types.
        url=f.get("url_private_download") or f.get("url_private", ""),
        suffix_hint=f.get("filetype", ""),
    )


async def process_slack_files(
    orch: GatewayOrchestrator,
    files: list[dict],
) -> tuple[list[str], list[str]]:
    """Process non-audio file attachments from a Slack message.

    Returns:
        (image_paths, text_blocks) — local paths for images (caller
        must clean up) and text strings ready for prompt injection.
    """
    if not orch.slack:
        return [], []

    client = orch.slack

    async def _download(url: str, dest: str) -> None:
        # Slack's downloader attaches the bot bearer token; only Slack knows
        # that, which is why the fetch stays channel-owned.
        await client.download_file(url, dest)

    result = await ingest_attachments(
        [_to_attachment(f) for f in files],
        download=_download,
        source="slack",
        limits=_LIMITS,
        handle_audio=False,  # transcribed upstream
        audio_mimetypes=SLACK_AUDIO_MIMETYPES,
    )

    # Rejection notes ride along as prompt text, matching the previous behaviour
    # of inlining "unsupported type" / "too large" notes for the model to see.
    return result.image_paths, [*result.text_blocks, *result.rejections]
