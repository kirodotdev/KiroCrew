"""Slack file attachment processing — a thin adapter over the neutral layer.

Slack owns exactly two things on the INBOUND side: mapping its envelope
(``files[]`` dicts with ``url_private_download`` / ``filetype`` / ``mimetype``)
onto :class:`~kiro_crew.messaging.attachments.Attachment`, and supplying an
authenticated download callback. Classification, size caps, magic-byte
validation, redaction, document extraction, SEL audit, and temp cleanup all live
in :mod:`kiro_crew.messaging.attachments` so other channels reuse them instead of
growing a second copy.

Audio is skipped here: Slack transcribes it on a separate upstream path
(``transcribe.py``) before file processing runs. The two visible outcomes of that
path when speech-to-text cannot produce words live here as
:data:`VOICE_MEMO_UNAVAILABLE` / :data:`VOICE_MEMO_FAILED`, beside the mimetype
table, so the transcriber and the ingestion adapter share one vocabulary.

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

The OUTBOUND half is the mirror image: :mod:`kiro_crew.messaging.outbound_files`
decides which local image references in a reply may be sent and hands back the
validated bytes, and this module supplies the Slack-specific upload: the
budgets Slack's own limits imply, the filename and title it puts on a file, and
the ``files_upload_v2`` call itself. Both halves live here because they answer the
same question in opposite directions, and splitting them across modules is how
the inbound cap and the outbound ceiling start disagreeing about what a picture
is.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
from collections.abc import Sequence
from typing import TYPE_CHECKING

from kiro_crew.messaging.attachments import (
    Attachment,
    IngestLimits,
    ingest_attachments,
    safe_suffix,
)
from kiro_crew.messaging.outbound_files import ExtractLimits, OutboundFile, Rejection
from kiro_crew.platform_compat import restrict_dir_to_owner
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

if TYPE_CHECKING:
    from kiro_crew.slack.client import SlackClientOps
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

#: What the model is told when a voice memo produced no words. Byte-identical to
#: the blocks :func:`kiro_crew.messaging.attachments.transcribe_audio_attachments`
#: emits for the other channels, because a Slack voice memo and a Discord one that
#: fail the same way must read the same way; Slack cannot call that function
#: (it downloads through the bot token on its own upstream path), so the wording
#: is pinned against it by ``test_slack_backports.py`` rather than copied and
#: left to drift. Silence is the one thing neither may be: the sender sees a
#: successful send, so a dropped memo looks to them like an ignored message.
VOICE_MEMO_UNAVAILABLE = "[Audio attachment — transcription is unavailable]"
VOICE_MEMO_FAILED = "[Audio attachment — transcription failed]"


def is_voice_memo(file: dict) -> bool:
    """Whether one Slack ``files[]`` entry is a voice memo / audio clip.

    The single predicate for "this attachment is speech": the transcriber uses it
    to pick what to send to speech-to-text, and the message path uses it to know
    how many memos owe the sender an answer. Two spellings of the mimetype test
    is how a memo gets transcribed but never reported, or reported but never
    transcribed.
    """
    mimetype = file.get("mimetype", "") or ""
    return any(mimetype.startswith(prefix) for prefix in SLACK_AUDIO_MIMETYPES)


def voice_memo_notes(count: int, note: str) -> list[str]:
    """*count* copies of *note*, one per memo that produced no transcript.

    One line per memo rather than a single summary line, matching the neutral
    half: the model is told how many attachments arrived, which is what lets it
    answer "I got your two voice notes but could not hear either".
    """
    return [note] * max(0, count)


# ── Outbound uploads (the Slack half of messaging/outbound_files.py) ──────────

#: Per-file ceiling for an image extracted out of a reply. Deliberately a
#: SEPARATE constant from the inbound ``max_image_bytes`` that carries the same
#: number: one bounds bytes accepted from a user, the other bounds bytes sent to
#: a workspace, and tying them to one symbol would make a change for one silently
#: retune the other. Slack's own upload ceiling is far higher; this is the size of
#: picture a chat reply is expected to carry, and the read is capped at it so an
#: oversize file is refused rather than allocated.
SLACK_MAX_UPLOAD_FILE_BYTES = 10 * 1024 * 1024
#: References turned into uploads for one reply. Each one is its own
#: ``files_upload_v2`` call, so this also bounds how many calls a single reply can
#: cost.
SLACK_MAX_UPLOAD_FILES = 10
#: Aggregate ceiling for one reply, and the MEMORY bound: extraction holds every
#: validated file's bytes until the last upload returns, so a reply's peak is this
#: value rather than the size of whatever it happened to reference.
SLACK_MAX_TOTAL_UPLOAD_BYTES = 25 * 1024 * 1024

#: Slack's budgets expressed against the neutral extractor's limit object.
UPLOAD_LIMITS = ExtractLimits(
    max_files=SLACK_MAX_UPLOAD_FILES,
    max_total_bytes=SLACK_MAX_TOTAL_UPLOAD_BYTES,
    max_file_bytes=SLACK_MAX_UPLOAD_FILE_BYTES,
)

#: Rejection reason for an upload Slack itself refused. The neutral module owns
#: the codes for the decisions IT makes; the upload is the channel's half, so its
#: failure code is the channel's too.
REASON_UPLOAD_FAILED = "upload_failed"

#: Characters allowed in the filename Slack displays. Everything else is folded
#: to ``_``: the name is derived from an LLM-authored path, so it reaches neither
#: a filesystem nor a header with anything that could steer either.
_UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
#: Cap on the display title taken from a reference's alt text.
_TITLE_CHARS = 200
#: Cap on the filename stem, leaving room for the sniffed extension.
_STEM_CHARS = 64


def _redact(text: str) -> str:
    """Both mandatory egress scanners over a string the model authored."""
    out, _ = redact_exfiltration_urls(text)
    out, _ = redact_credentials(out)
    return out


def upload_filename(file: OutboundFile, index: int) -> str:
    """A display- and filesystem-safe name for one uploaded file.

    The extension comes from the SNIFFED type, never the written path's, so a
    ``chart.png`` that is really a JPEG arrives with truthful metadata. The stem
    is re-scanned after sanitizing and replaced wholesale when redaction changes
    it, because a path is model-authored text and a filename is a place a secret
    can be smuggled out in plain sight.
    """
    ext = safe_suffix(file.mime.rsplit("/", 1)[-1], default="bin")
    stem = _UNSAFE_FILENAME_RE.sub("_", os.path.basename(file.path))
    stem = os.path.splitext(stem)[0].strip("._")[:_STEM_CHARS]
    name = f"{stem or f'image_{index}'}{ext}"
    return name if _redact(name) == name else f"image_{index}{ext}"


def upload_title(file: OutboundFile, filename: str) -> str:
    """The title Slack shows above the file: the alt text, redacted and capped."""
    alt = _redact(file.alt or "").strip()
    return alt[:_TITLE_CHARS] or filename


async def upload_outbound_files(
    client: SlackClientOps,
    channel: str,
    thread_ts: str,
    files: Sequence[OutboundFile],
) -> list[Rejection]:
    """Upload each extracted file into *channel* / *thread_ts*.

    Returns one :class:`Rejection` per file Slack would not take, so the caller
    can say so in the thread. A failure here is per file, because Slack's upload
    verb takes one file per call, and it must never be silent: the reference has
    already been cut out of the reply text by then, so an unreported failure is a
    reply that talks about a picture with neither the picture nor a reason.

    **The bytes travel, not the path.** ``OutboundFile.data`` is what every gate
    in :mod:`kiro_crew.messaging.outbound_files` was applied to, and the
    agent-authored path is never re-opened. Slack's verb reads a path, so the
    validated bytes are staged into an owner-only directory this process just
    created and removed as soon as the call returns: the name handed to Slack
    exists for one upload and nothing else can write it.
    """
    rejections: list[Rejection] = []
    for index, file in enumerate(files):
        filename = upload_filename(file, index)
        try:
            await _upload_one(client, channel, thread_ts, file, filename)
        except Exception:
            logger.warning("slack: uploading %s failed", filename, exc_info=True)
            rejections.append(
                Rejection(file.path, REASON_UPLOAD_FAILED, "the upload to Slack failed")
            )
    return rejections


async def _upload_one(
    client: SlackClientOps,
    channel: str,
    thread_ts: str,
    file: OutboundFile,
    filename: str,
) -> None:
    """Stage one file's validated bytes and hand Slack the path it wants.

    Every filesystem step runs off the event loop: this is the gateway's only
    thread, and a 10 MiB write on it stalls every other session.
    """
    staging = await asyncio.to_thread(tempfile.mkdtemp, prefix="kirocrew-slack-upload-")
    try:
        # mkdtemp is already 0700 on POSIX; this adds the Windows DACL (where a
        # mode is inert) and is fail-loud, so a directory that could not be
        # locked down becomes a reported rejection rather than a world-readable
        # copy of whatever the agent rendered.
        await asyncio.to_thread(restrict_dir_to_owner, staging)
        staged = os.path.join(staging, filename)
        await asyncio.to_thread(_write_bytes, staged, file.data)
        await client.upload_file(
            channel,
            thread_ts,
            staged,
            filename,
            upload_title(file, filename),
        )
    finally:
        await asyncio.to_thread(shutil.rmtree, staging, True)


def _write_bytes(path: str, data: bytes) -> None:
    with open(path, "wb") as fh:
        fh.write(data)


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
