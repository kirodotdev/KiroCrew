"""Channel-neutral inbound attachment ingestion.

Every chat channel receives files in its own envelope shape and fetches them with
its own auth, but what happens *after* the bytes land is identical: classify,
enforce limits, convert to something the model can actually consume, redact,
audit, and clean up. This module owns that shared half so each channel only
supplies metadata plus a download callback.

Terminal form per class -- there is no single pipe, because each class reaches
the model differently:

===========  =========================================================
class        becomes
===========  =========================================================
IMAGE        a local path the ACP prompt path inlines as an image block
             (see :mod:`kiro_crew.acp.prompt_blocks`)
TEXT         redacted, truncated text inlined into the prompt
DOCUMENT     text extracted via :mod:`kiro_crew.doc_parser`, then as TEXT
AUDIO        a local path for the caller to transcribe (opt-in)
VIDEO        rejected with a visible reason -- kiro-cli advertises
             ``promptCapabilities.image`` only, and the models behind it do
             not accept video
OTHER        rejected with a visible reason
===========  =========================================================

**Why rejections are returned rather than swallowed.** Silently dropping an
attachment is the original defect this module exists to fix: a user posts a file
and the reply simply ignores it, with no explanation. Callers are expected to
surface :attr:`IngestResult.rejections` to the user.

**Deliberately NOT here: the fetch itself.** Host allowlisting and auth are
channel-specific -- Slack needs a bearer token against ``files.slack.com``,
Discord needs no auth but signed ``cdn.discordapp.com`` URLs. The callback owns
that; this module owns everything after the bytes arrive.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from kiro_crew import transcribe
from kiro_crew.doc_parser import extract_text, is_parseable_document
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

# ── Classes ──
IMAGE = "image"
TEXT = "text"
DOCUMENT = "document"
AUDIO = "audio"
VIDEO = "video"
OTHER = "other"

#: Raster types that :func:`kiro_crew.acp.prompt_blocks.build_prompt_blocks` can
#: inline. Keep in step with ``IMAGE_MEDIA_TYPES`` there -- a type accepted here
#: but unknown to the encoder would download and then be ignored.
IMAGE_MIMETYPES = {
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "image/bmp",
}

#: Leading bytes per image type. Metadata and filenames are attacker-controlled;
#: bytes are not. A claimed PNG that does not start with the PNG signature is
#: rejected rather than handed to the model (CWE-434).
_MAGIC: dict[str, tuple[bytes, ...]] = {
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/gif": (b"GIF87a", b"GIF89a"),
    "image/webp": (b"RIFF",),  # RIFF....WEBP
    "image/bmp": (b"BM",),
}

_TEXT_PREFIXES = ("text/",)
_TEXT_EXACT = {"application/json", "application/xml", "application/javascript"}
_AUDIO_PREFIXES = ("audio/",)
_VIDEO_PREFIXES = ("video/",)

_SAFE_SUFFIX_RE = re.compile(r"[^a-zA-Z0-9]")

#: Async ``(url, dest_path) -> None``. Must raise on failure.
DownloadFn = Callable[[str, str], Awaitable[None]]


@dataclass
class IngestLimits:
    """Caps applied per attachment. Defaults match Slack's shipped behaviour."""

    max_image_bytes: int = 10 * 1024 * 1024
    max_text_bytes: int = 512 * 1024
    max_document_bytes: int = 20 * 1024 * 1024
    max_audio_bytes: int = 25 * 1024 * 1024
    #: Characters of extracted text injected into the prompt.
    max_text_inject: int = 50 * 1024
    #: Per-message attachment cap. Slack had none, so a single message could
    #: trigger unbounded downloads.
    max_attachments: int = 10


@dataclass
class Attachment:
    """One inbound file, normalized away from any channel's envelope shape."""

    name: str
    mimetype: str = ""
    size: int = 0
    url: str = ""
    #: Extension hint (Slack's ``filetype``, or a filename suffix). Sanitized
    #: before use -- never trusted as a path component.
    suffix_hint: str = ""


@dataclass
class IngestResult:
    #: Local paths to downloaded images. **The caller must delete these** once
    #: the turn has consumed them.
    image_paths: list[str] = field(default_factory=list)
    #: Local paths to downloaded audio, for the caller to transcribe. Caller
    #: deletes.
    audio_paths: list[str] = field(default_factory=list)
    #: Prompt-ready text fragments (already redacted and truncated).
    text_blocks: list[str] = field(default_factory=list)
    #: Human-readable reasons an attachment was not ingested. Surface these.
    rejections: list[str] = field(default_factory=list)

    @property
    def temp_paths(self) -> list[str]:
        """Every path the caller is responsible for cleaning up."""
        return [*self.image_paths, *self.audio_paths]


def safe_suffix(hint: str, default: str = "bin") -> str:
    """A filesystem-safe extension derived from *hint*.

    Strips everything non-alphanumeric so an attachment name can never steer the
    destination path (``../``, absolute paths, NUL bytes).
    """
    clean = _SAFE_SUFFIX_RE.sub("", hint or "")
    return "." + (clean or default)


def classify(mimetype: str, name: str = "", audio_mimetypes: tuple[str, ...] = ()) -> str:
    """Map a mimetype (with filename as a fallback signal) to a class.

    ``audio_mimetypes`` lets a channel declare prefixes it treats as audio even
    though they are not ``audio/*``. This is not hypothetical: Slack ships voice
    memos as ``video/webm``, and its transcription path already encodes that
    (``slack/events.py``). Without the override the same file classifies as
    VIDEO here and a "video is not supported" note lands beside the transcript.
    The knowledge belongs to the channel, so the channel supplies it rather than
    this module accumulating per-channel special cases.
    """
    mt = (mimetype or "").lower()
    if mt in IMAGE_MIMETYPES:
        return IMAGE
    if any(mt.startswith(p) for p in (*audio_mimetypes, *_AUDIO_PREFIXES)):
        return AUDIO
    if any(mt.startswith(p) for p in _VIDEO_PREFIXES):
        return VIDEO
    if any(mt.startswith(p) for p in _TEXT_PREFIXES) or mt in _TEXT_EXACT:
        return TEXT
    if is_parseable_document(mimetype=mt, filename=name):
        return DOCUMENT
    return OTHER


def sniff_image_mime(path: str) -> str | None:
    """The image type *path* actually is, by leading bytes, or ``None``.

    Metadata and filenames are attacker-controlled; bytes are not. Returning the
    detected type (rather than merely accepting/rejecting the declared one) does
    double duty:

    * a file that matches NO known image signature is not an image at all and is
      rejected -- that is the CWE-434 case, e.g. a script declared ``image/png``
    * a genuine JPEG that the channel mislabelled ``image/png`` still works, and
      the temp file gets the correct suffix, so the ACP encoder -- which derives
      ``mimeType`` from the suffix -- puts truthful metadata on the wire
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(16)
    except OSError:
        return None
    for mime, prefixes in _MAGIC.items():
        if any(head.startswith(p) for p in prefixes):
            # WebP and other RIFF containers share the "RIFF" prefix; confirm the
            # form tag so a RIFF/WAVE file is not mistaken for an image.
            if mime == "image/webp" and head[8:12] != b"WEBP":
                continue
            return mime
    return None


#: Canonical suffix per image mimetype, so a downloaded file is renamed to match
#: what it actually is.
_MIME_SUFFIX = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
}


def _audit(source: str, operation: str, outcome: str, name: str, error: str = "") -> None:
    sel().log_api_access(
        caller="attachment_ingest",
        operation=operation,
        outcome=outcome,
        source=source,
        resources=name,
        error=error,
    )


def _make_temp(suffix: str) -> str:
    """Create an empty temp file and return its path (blocking; call offloaded).

    ``mkstemp`` and its ``close`` are paired here deliberately so ONE executor
    hop covers both. ``mkstemp`` is the expensive half -- directory lookups plus
    an ``O_CREAT|O_EXCL`` open, retried on name collision -- while the ``close``
    releases a descriptor whose file has no dirty pages, because nothing has been
    written to it yet. Offloading only the ``close`` would move a temp-directory
    stall up one line rather than removing it.
    """
    fd, dest = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    return dest


async def _fetch(download: DownloadFn, url: str, suffix: str) -> str:
    """Download to a fresh temp file and return its path. Raises on failure."""
    # Offloaded: this runs on the gateway event loop, and TMPDIR is not
    # guaranteed to be local (a network- or FUSE-backed temp dir can stall),
    # which would freeze every other session until the watchdog intervened.
    dest = await asyncio.to_thread(_make_temp, suffix)
    try:
        await download(url, dest)
    except BaseException:
        try:
            os.unlink(dest)
        except OSError:
            pass
        raise
    return dest


def _read_text_file(path: str, limit: int) -> str:
    """Read a text attachment and redact+truncate it (blocking; call offloaded)."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return _clean_text(fh.read(), limit)


def _truncate(content: str, limit: int) -> tuple[str, str]:
    if len(content) > limit:
        return content[:limit], "\n[… truncated]"
    return content, ""


def _clean_text(content: str, limit: int) -> str:
    """Redact then truncate. Redaction runs FIRST so a secret cannot survive by
    sitting past the truncation point."""
    content, _ = redact_exfiltration_urls(content)
    content, _ = redact_credentials(content)
    body, tail = _truncate(content, limit)
    return body + tail


async def ingest_attachments(
    attachments: list[Attachment],
    *,
    download: DownloadFn,
    source: str,
    limits: IngestLimits | None = None,
    handle_audio: bool = False,
    audio_mimetypes: tuple[str, ...] = (),
) -> IngestResult:
    """Download and convert *attachments* into prompt-ready material.

    ``handle_audio=False`` skips audio entirely, for channels that transcribe it
    on a separate upstream path (Slack). ``True`` downloads it and returns the
    paths in :attr:`IngestResult.audio_paths` for the caller to transcribe.

    ``audio_mimetypes`` declares channel-specific types to treat as audio (see
    :func:`classify`) -- Slack's ``video/webm`` voice memos being the real case.

    Never raises for a single bad attachment: each failure becomes a rejection so
    one unreadable file cannot lose the rest of the message.
    """
    lim = limits or IngestLimits()
    out = IngestResult()

    for att in attachments[: lim.max_attachments]:
        kind = classify(att.mimetype, att.name, audio_mimetypes)

        if kind == AUDIO and not handle_audio:
            continue  # transcribed upstream

        if not att.url:
            out.rejections.append(f"[Attachment {att.name} — no download URL]")
            _audit(source, f"{source}.attachment_skip", "skipped", att.name, "no url")
            continue

        if kind == VIDEO:
            out.rejections.append(
                f"[Attachment {att.name} — video is not supported; "
                "send a screenshot or a text summary instead]"
            )
            _audit(source, f"{source}.attachment_skip", "skipped", att.name, "video unsupported")
            continue

        if kind == OTHER:
            out.rejections.append(
                f"[Attached file: {att.name} ({att.mimetype or 'unknown type'}, "
                f"{att.size} bytes) — unsupported type]"
            )
            _audit(
                source,
                f"{source}.attachment_skip",
                "skipped",
                att.name,
                f"unsupported mimetype: {att.mimetype}",
            )
            continue

        cap = {
            IMAGE: lim.max_image_bytes,
            TEXT: lim.max_text_bytes,
            DOCUMENT: lim.max_document_bytes,
            AUDIO: lim.max_audio_bytes,
        }[kind]

        # Pre-download check on channel-reported size. Cheap, but advisory only:
        # the value is attacker-influenced and defaults to 0 when absent, which
        # is exactly how Slack's cap could be bypassed. The post-download check
        # below is the one that actually holds.
        if att.size and att.size > cap:
            out.rejections.append(
                f"[Attachment {att.name} ({att.size} bytes) — too large, limit {cap}]"
            )
            _audit(source, f"{source}.attachment_skip", "skipped", att.name, f"too large: {att.size}")
            continue

        try:
            dest = await _fetch(download, att.url, safe_suffix(att.suffix_hint or att.name.rsplit(".", 1)[-1]))
        except Exception:
            logger.exception("%s: failed to download attachment %s", source, att.name)
            out.rejections.append(f"[Attachment {att.name} — download failed]")
            _audit(source, f"{source}.attachment_download", "error", att.name, "download_failed")
            continue

        try:
            # Authoritative size check: metadata may have lied or been absent.
            actual = os.path.getsize(dest)
            if actual > cap:
                out.rejections.append(
                    f"[Attachment {att.name} ({actual} bytes) — too large, limit {cap}]"
                )
                _audit(
                    source,
                    f"{source}.attachment_skip",
                    "skipped",
                    att.name,
                    f"too large after download: {actual}",
                )
                continue

            if kind == IMAGE:
                # Offloaded: opens and reads the file header.
                actual_mime = await asyncio.to_thread(sniff_image_mime, dest)
                if actual_mime is None:
                    out.rejections.append(
                        f"[Attachment {att.name} — not a readable image "
                        f"(declared {att.mimetype or 'unknown'})]"
                    )
                    _audit(
                        source,
                        f"{source}.attachment_skip",
                        "skipped",
                        att.name,
                        "content_signature_mismatch",
                    )
                    continue
                # Rename to the TRUE type's suffix: the ACP encoder derives
                # mimeType from the suffix, so a mislabelled file would otherwise
                # travel with wrong metadata. REPLACE the declared suffix rather
                # than appending, so the name does not claim two types at once.
                want = _MIME_SUFFIX.get(actual_mime, "")
                if want and os.path.splitext(dest)[1].lower() != want:
                    renamed = os.path.splitext(dest)[0] + want
                    try:
                        os.replace(dest, renamed)
                        dest = renamed
                    except OSError:
                        logger.debug("%s: could not retype %s", source, dest, exc_info=True)
                out.image_paths.append(dest)
                dest = ""  # ownership transferred to the caller
                _audit(source, f"{source}.attachment_download", "success", att.name)

            elif kind == AUDIO:
                out.audio_paths.append(dest)
                dest = ""
                _audit(source, f"{source}.attachment_download", "success", att.name)

            elif kind == TEXT:
                # Offloaded for the same reason as the DOCUMENT branch below:
                # this reads file CONTENT (up to max_text_bytes) on the gateway
                # event loop. Leaving plain text inline while offloading PDF
                # parsing would be an arbitrary split -- both are blocking reads.
                body = await asyncio.to_thread(
                    _read_text_file, dest, lim.max_text_inject
                )
                out.text_blocks.append(f"[File: {att.name}]\n{body}\n[End of file]")
                _audit(source, f"{source}.attachment_download", "success", att.name)

            else:  # DOCUMENT
                # Parsing a PDF/docx is synchronous CPU work on inputs up to
                # max_document_bytes. This runs inside the gateway's event loop
                # task (Socket Mode -> _route_message -> process_slack_files),
                # so doing it inline stalls EVERY other session's streaming for
                # the duration. Offload to a worker thread.
                #
                # Deliberately asyncio.to_thread rather than subprocess_executor():
                # that pool is reserved for subprocess/PTY teardown and orphan
                # reaping, and its own docstring notes those workers must stay
                # free to recover from a wedged kernel resource. Parking a slow
                # document parse there would undermine that isolation.
                raw = await asyncio.to_thread(
                    extract_text, dest, mimetype=att.mimetype, filename=att.name
                )
                if not raw:
                    out.rejections.append(
                        f"[Attached document: {att.name} — could not extract text]"
                    )
                    _audit(
                        source,
                        f"{source}.attachment_parse",
                        "error",
                        att.name,
                        "no_text_extracted",
                    )
                    continue
                body = _clean_text(raw, lim.max_text_inject)
                out.text_blocks.append(f"[Document: {att.name}]\n{body}\n[End of document]")
                _audit(source, f"{source}.attachment_download", "success", att.name)
        except Exception:
            logger.exception("%s: failed to process attachment %s", source, att.name)
            out.rejections.append(f"[Attachment {att.name} — could not be processed]")
            _audit(source, f"{source}.attachment_parse", "error", att.name, "process_failed")
        finally:
            # Anything not handed to the caller is deleted here.
            if dest:
                try:
                    os.unlink(dest)
                except OSError:
                    pass

    if len(attachments) > lim.max_attachments:
        dropped = len(attachments) - lim.max_attachments
        out.rejections.append(
            f"[{dropped} more attachment(s) ignored — limit {lim.max_attachments} per message]"
        )

    return out


def cleanup(paths: list[str]) -> None:
    """Best-effort deletion of temp paths handed back by :func:`ingest_attachments`."""
    for p in paths:
        try:
            os.unlink(p)
        except OSError:
            pass


async def transcribe_audio_attachments(result: IngestResult, source: str) -> IngestResult:
    """Transcribe every audio path on *result* in place, then return it.

    Channel-neutral second half of audio ingestion: :func:`ingest_attachments`
    (with ``handle_audio=True``) downloads audio and hands back
    :attr:`IngestResult.audio_paths`; this turns those paths into prompt-ready
    transcript blocks, or into visible rejections when speech-to-text is
    unavailable or fails.

    Lives here rather than in each channel adapter so the transcript wording and
    the STT-unavailable handling cannot drift between channels — Discord and
    Telegram would otherwise each carry a byte-identical copy of this block.

    ``source`` names the channel for log lines only; it does not change behaviour.
    """
    if not result.audio_paths:
        return result

    try:
        available = await asyncio.to_thread(transcribe.is_available)
    except Exception:
        logger.exception("%s: failed to check speech-to-text availability", source)
        available = False

    if not available:
        result.rejections.extend(
            "[Audio attachment — transcription is unavailable]" for _ in result.audio_paths
        )
        return result

    for path in result.audio_paths:
        try:
            transcript = await transcribe.transcribe_audio(path)
        except Exception:
            logger.exception("%s: audio transcription raised", source)
            transcript = None
        if transcript:
            result.text_blocks.append(
                f"[Voice memo transcription]\n{transcript}\n[End of transcription]"
            )
        else:
            result.rejections.append("[Audio attachment — transcription failed]")

    return result


def append_attachment_context(text: str, result: IngestResult) -> str:
    """Append prompt-ready attachment material to the user's message text.

    Image paths are appended as bare lines (the ACP encoder's path regex picks
    them up and inlines each as a base64 image content block). Text and
    rejection blocks follow, separated by blank lines for prompt readability.

    Channel-neutral: every transport that ingests attachments uses this same
    layout, so the model sees a consistent attachment presentation regardless
    of whether the user sent from Slack, Discord, or Telegram.
    """
    if result.image_paths:
        paths_text = "\n".join(result.image_paths)
        text = f"{text}\n{paths_text}" if text else paths_text

    blocks = [*result.text_blocks, *result.rejections]
    if blocks:
        blocks_text = "\n\n".join(blocks)
        text = f"{text}\n\n{blocks_text}" if text else blocks_text
    return text
