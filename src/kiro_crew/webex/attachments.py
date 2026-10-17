"""Webex inbound attachment ingest.

Maps a Webex message's ``files[]`` onto the neutral
:mod:`kiro_crew.messaging.attachments` pipeline, which owns everything that is
not Webex-specific: per-kind classification, the size and count caps, magic-byte
sniffing, temp-file ownership, the SEL audit, and the prompt-side context block.

What is Webex-specific and therefore lives here: the envelope shape (an opaque
``/v1/contents/{id}`` URL with no name or type until you ask), a HEAD probe to
learn the filename/mimetype/size before any bytes move, and a download callback
that carries the bearer token. The anti-malware state machine those downloads
have to survive belongs to the client (:meth:`WebexClient.download_content`).

Dependency direction is ``webex -> messaging`` (allowed).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from kiro_crew.messaging.attachments import (
    Attachment,
    IngestResult,
    ingest_attachments,
    safe_suffix,
    transcribe_audio_attachments,
)

if TYPE_CHECKING:
    from kiro_crew.webex.client import WebexClient, WebexInbound

logger = logging.getLogger(__name__)


async def to_attachments(client: "WebexClient", urls: tuple[str, ...]) -> list[Attachment]:
    """Probe each content URL and describe it as a neutral :class:`Attachment`.

    Webex hands out opaque URLs, so name/type/size come from a HEAD rather than
    the message body. A probe that fails still yields an entry: the shared ingest
    turns an unusable attachment into a visible rejection line, which is the
    honest outcome — dropping it silently would leave the user believing the
    agent saw their file.
    """
    out: list[Attachment] = []
    for url in urls:
        name, mimetype, size = await client.head_content(url)
        if not name:
            # Nothing is knowable about it yet; give the ingest a stable
            # placeholder so its rejection names something.
            name = "attachment"
        out.append(
            Attachment(
                name=name,
                mimetype=mimetype,
                size=size,
                url=url,
                suffix_hint=safe_suffix(name or mimetype),
            )
        )
    return out


async def process_webex_attachments(client: "WebexClient", inbound: "WebexInbound") -> IngestResult:
    """Download and convert an inbound message's files into prompt material.

    Audio is downloaded and transcribed here (``handle_audio=True``) because
    Webex has no server-side transcript to prefer, unlike iLink voice clips.
    """

    async def _download(url: str, dest: str) -> None:
        await client.download_content(url, dest)

    result = await ingest_attachments(
        await to_attachments(client, inbound.file_urls),
        download=_download,
        source="webex",
        handle_audio=True,
    )
    return await transcribe_audio_attachments(result, "Webex")
