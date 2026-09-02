"""Discord-specific adapter over channel-neutral attachment ingestion.

Discord owns only what is genuinely Discord-shaped: the envelope mapping and the
signed-CDN download callback. Classification, limits, signature validation,
extraction, redaction, rejection text, transcription and temp-file ownership all
stay in ``kiro_crew.messaging.attachments``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from kiro_crew.messaging.attachments import (
    Attachment,
    IngestResult,
    append_attachment_context,
    ingest_attachments,
    transcribe_audio_attachments,
)

# ``append_attachment_context`` is channel-neutral and lives in
# ``messaging.attachments``; it is re-exported here so existing
# ``from kiro_crew.discord.attachments import append_attachment_context``
# callers keep resolving. Declared in ``__all__`` rather than carrying a
# blanket ``# noqa: F401`` on the import block, which would also suppress
# genuine unused-import warnings for the names above that ARE used.
__all__ = [
    "append_attachment_context",
    "process_discord_attachments",
]

if TYPE_CHECKING:
    from kiro_crew.discord.client import DiscordClient


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
    return await transcribe_audio_attachments(result, "Discord")


# append_attachment_context is re-exported from messaging.attachments (imported above).
# Kept in this module's public API so existing `from kiro_crew.discord.attachments
# import append_attachment_context` continues to resolve without a code change.
