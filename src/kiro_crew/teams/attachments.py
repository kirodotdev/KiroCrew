"""Teams-specific file policy for both directions.

Teams owns only what is genuinely Teams-shaped. Everything that is identical for
every channel stays in the neutral modules:

* inbound classification, size limits, image-signature validation, text/document
  extraction, redaction, rejection wording, transcription and temp-file
  ownership -> :mod:`kiro_crew.messaging.attachments`
* outbound reference scanning, the security floor, the raster sniff and the
  per-message budgets -> :mod:`kiro_crew.messaging.outbound_files`

What is Teams-shaped, and therefore lives here:

**Inbound: two attachment kinds with OPPOSITE auth.** A file the user uploads in
a personal chat arrives as ``application/vnd.microsoft.teams.file.download.info``
whose ``content.downloadUrl`` Microsoft documents as something the reader "can
issue an HTTP GET directly from" -- it carries its own capability and MUST be
fetched with a plain GET, because attaching the bot's Connector bearer token to a
host we do not control would hand a credential-equivalent secret to whoever that
host belongs to. An inline image instead arrives with a ``contentUrl`` on a
Microsoft-operated host, for which the guidance is contradictory: one revision
says the SDK handles authentication, the previous one and the shipped sample
attach the bot token. The two cases are one boolean apart and getting it backwards
is a credential leak in one direction and a broken feature in the other, so the
mapping is explicit per content type, the transport never guesses, and the client
decides on the host (see ``client.download_inbound_file``).

Teams also echoes the message body itself as a ``text/html`` (or ``text/plain``)
attachment on ordinary rich-text messages. Those are not files and are skipped,
because ingesting them would inject the same words twice into every prompt.

**Outbound: an inline image needs no hosting and no consent round trip.** An
``Attachment`` whose ``contentUrl`` is a ``data:image/...;base64,`` URI renders
in the conversation directly, which is the whole common case -- an agent-produced
chart. Anything that is NOT one of :data:`TEAMS_INLINE_IMAGE_MIMES` is refused
with a visible reason rather than dropped; sending a non-image file would need
the ``FileConsentCard`` round trip, which requires handling an ``invoke``
activity that this channel's fast-ack ingress does not do.
"""

from __future__ import annotations

import base64
import html as html_module
import logging
import mimetypes
import os
import re
from typing import TYPE_CHECKING, Any

from kiro_crew.messaging.attachments import (
    Attachment,
    IngestResult,
    append_attachment_context,
    ingest_attachments,
    transcribe_audio_attachments,
)
from kiro_crew.messaging.display_safety import redact_for_display
from kiro_crew.messaging.outbound_files import ExtractLimits, OutboundFile, Rejection
from kiro_crew.messaging.renderer import _default_redactor

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.teams.client import TeamsClient

logger = logging.getLogger(__name__)

# ``append_attachment_context`` is channel-neutral and lives in
# ``messaging.attachments``; re-exported so one module supplies the whole
# ingest+append pair (mirrors discord/telegram/weixin).
__all__ = [
    "TEAMS_FILE_DOWNLOAD_INFO",
    "TEAMS_INLINE_IMAGE_MIMES",
    "TEAMS_MAX_INLINE_IMAGES",
    "TEAMS_MAX_INLINE_IMAGE_BYTES",
    "TEAMS_MAX_INLINE_TOTAL_BYTES",
    "TEAMS_UPLOAD_LIMITS",
    "REASON_INLINE_UNSUPPORTED",
    "REASON_INLINE_UNDELIVERED",
    "append_attachment_context",
    "inline_image_attachment",
    "inline_image_name",
    "quoted_reply_text",
    "map_inbound_attachments",
    "process_teams_attachments",
    "undeliverable_rejection",
    "unsupported_inline_rejection",
]

# ── Inbound ──

#: Content type of a file a user uploaded into a personal chat. Its
#: ``content.downloadUrl`` is pre-authorized, so it is fetched WITHOUT the bot
#: token (see the module docstring).
TEAMS_FILE_DOWNLOAD_INFO = "application/vnd.microsoft.teams.file.download.info"

#: Attachment content types Teams uses to echo the message BODY rather than to
#: carry a file. Skipped silently: they are the same words already in
#: ``activity.text``, and ingesting them would duplicate every rich-text message
#: into the prompt.
_BODY_CONTENT_TYPES = frozenset({"text/html", "text/plain"})

#: Marks the Teams client's own quotation of the message being replied to. In a 1:1
#: chat -- the only scope this channel serves -- right-click -> Reply prepends the
#: QUOTED message's text to ``activity.text``, so the field the rest of the pipeline
#: reads is the previous message with the user's own words tacked on the end. The
#: clean text is in the ``text/html`` body attachment, after this blockquote.
_REPLY_ITEMTYPE = "http://schema.skype.com/Reply"
_REPLY_BLOCKQUOTE_RE = re.compile(
    r"<blockquote\b[^>]{0,512}?itemtype\s*=\s*[\"']"
    + re.escape(_REPLY_ITEMTYPE)
    + r"[\"'][^>]{0,512}?>.{0,%d}?</blockquote>" % (32 * 1024),
    re.IGNORECASE | re.DOTALL,
)
#: Line-ish tags become newlines before the rest are dropped, so a multi-paragraph
#: reply does not collapse into one run-on line.
#:
#: No unbounded whitespace before a required literal, deliberately. ``<\s*br`` is a
#: polynomial-ReDoS shape: every ``<`` in the body is a candidate start, ``\s*``
#: consumes a run of tabs, the literal then fails, and it backtracks one character at
#: a time -- so a 64 KB body of ``<`` + tabs is quadratic work ON THE EVENT LOOP,
#: since this parse runs inline on the inbound path. The forms Teams actually emits
#: (``</p>``, ``<br>``, ``<br/>``, ``<br />``) need no more than one optional space.
_BREAK_RE = re.compile(r"</(?:p|div|li|tr)>|<br\s?/?>", re.IGNORECASE)
#: Bounded repetition: an unbounded ``[^>]*`` over attacker-supplied HTML is a
#: catastrophic-backtracking shape, and no legitimate tag is anywhere near this long.
_TAG_RE = re.compile(r"<[^>]{0,2048}>")

#: Ceiling on the HTML body this parse will look at. A Teams message body is a few
#: KB; past this the quote extraction is skipped and ``activity.text`` is used as-is,
#: which is the pre-existing behaviour rather than a new failure.
_MAX_REPLY_HTML_CHARS = 64 * 1024


#: Prefix of the Adaptive/Hero/Thumbnail card content types. A card is UI, never
#: a file, and one can legitimately ride an inbound activity (a submit echo), so
#: it is skipped rather than reported as an unsupported file.
_CARD_CONTENT_PREFIX = "application/vnd.microsoft.card."


def quoted_reply_text(raw_attachments: list[Any]) -> str:
    """The user's OWN words from a quote-reply, or ``""`` when this is not one.

    Returning ``""`` for anything unrecognised is deliberate: the caller keeps
    ``activity.text``, so a Teams client whose markup differs degrades to today's
    behaviour instead of losing the message.

    Two consequences of NOT doing this, both silent: a quote-replied ``/stop`` no
    longer starts with ``/`` so it reaches the model as prose and the turn keeps
    running, and a quote-replied question arrives with the previous message jammed
    onto the front of the prompt.
    """
    for raw in raw_attachments:
        if not isinstance(raw, dict):
            continue
        content_type = raw.get("contentType")
        if not isinstance(content_type, str) or content_type.lower().strip() != "text/html":
            continue
        html = raw.get("content")
        if not isinstance(html, str) or _REPLY_ITEMTYPE not in html:
            continue
        if len(html) > _MAX_REPLY_HTML_CHARS:
            logger.debug("Teams: quote-reply body over the parse cap; using activity.text")
            continue
        stripped = _REPLY_BLOCKQUOTE_RE.sub("", html, count=1)
        if stripped == html:
            # The marker was present but the blockquote did not match, so this is a
            # shape we do not understand. Do not guess.
            continue
        text = _TAG_RE.sub("", _BREAK_RE.sub("\n", stripped))
        text = html_module.unescape(text).strip()
        if text:
            return text
    return ""


def _attachment_name(raw: dict[str, Any], default: str) -> str:
    name = raw.get("name")
    return name if isinstance(name, str) and name.strip() else default


def _download_info(raw: dict[str, Any]) -> tuple[Attachment, bool] | None:
    """Map a personal-chat file upload; ``False`` = fetch with NO bot token."""
    content = raw.get("content")
    content = content if isinstance(content, dict) else {}
    url = content.get("downloadUrl")
    if not isinstance(url, str) or not url:
        return None
    name = _attachment_name(raw, "file.bin")
    # ``content.fileType`` is the extension Teams recorded (``"pdf"``). Prefer it
    # over the name's suffix for the type guess, then let the neutral pipeline
    # re-sniff the downloaded bytes -- a declared type never decides anything on
    # its own here.
    file_type = content.get("fileType")
    suffix = file_type if isinstance(file_type, str) and file_type else ""
    if not suffix and "." in name:
        suffix = name.rsplit(".", 1)[-1]
    mimetype = mimetypes.guess_type(f"x.{suffix}" if suffix else name)[0] or ""
    return Attachment(name=name, mimetype=mimetype, size=0, url=url, suffix_hint=suffix), False


def _inline_image(raw: dict[str, Any], content_type: str) -> tuple[Attachment, bool] | None:
    """Map an inline image; ``True`` = the fetch needs the bot's Connector token."""
    url = raw.get("contentUrl")
    if not isinstance(url, str) or not url.lower().startswith("https://"):
        # A ``data:`` or relative ``contentUrl`` is not something to fetch, and a
        # non-https one must never see a credential.
        return None
    name = _attachment_name(raw, "image")
    suffix = name.rsplit(".", 1)[-1] if "." in name else content_type.partition("/")[2]
    return Attachment(name=name, mimetype=content_type, size=0, url=url, suffix_hint=suffix), True


def map_inbound_attachments(
    raw_attachments: list[Any],
) -> tuple[list[tuple[Attachment, bool]], list[str]]:
    """Split an activity's attachments into ``(mapped, unsupported_types)``.

    Each mapped entry is ``(Attachment, needs_bot_token)``. Deny-by-default: an
    attachment whose content type is not a recognized file-bearing kind is NOT
    fetched, and its content type is returned so the caller can say so out loud
    instead of dropping it in silence.
    """
    mapped: list[tuple[Attachment, bool]] = []
    unsupported: list[str] = []
    for raw in raw_attachments:
        if not isinstance(raw, dict):
            continue
        content_type = raw.get("contentType")
        content_type = content_type.lower().strip() if isinstance(content_type, str) else ""
        if not content_type:
            continue
        if content_type in _BODY_CONTENT_TYPES or content_type.startswith(_CARD_CONTENT_PREFIX):
            continue
        entry: tuple[Attachment, bool] | None
        if content_type == TEAMS_FILE_DOWNLOAD_INFO:
            entry = _download_info(raw)
        elif content_type.startswith("image/"):
            entry = _inline_image(raw, content_type)
        else:
            unsupported.append(content_type)
            continue
        if entry is None:
            unsupported.append(content_type)
            continue
        mapped.append(entry)
    return mapped, unsupported


async def process_teams_attachments(
    client: "TeamsClient",
    raw_attachments: list[Any],
) -> IngestResult:
    """Download and ingest the files on one inbound activity.

    The per-URL auth decision is frozen HERE, before any fetch, so the download
    callback cannot mistake a pre-authorized ``downloadUrl`` for a Microsoft-hosted
    ``contentUrl``: the map is keyed by the exact URL the mapping approved, and an
    unknown URL defaults to the unauthenticated fetch (the safe direction -- it
    can fail, but it cannot leak the bot's token).

    Returned temp paths stay caller-owned and must outlive the consuming turn,
    exactly as for Discord/Telegram/Weixin.
    """
    mapped, unsupported = map_inbound_attachments(raw_attachments)
    if not mapped:
        result = IngestResult()
        result.rejections.extend(_unsupported_lines(unsupported))
        return result

    needs_token = {att.url: authenticated for att, authenticated in mapped}

    async def _download(url: str, dest: str) -> None:
        await client.download_inbound_file(url, dest, authenticated=needs_token.get(url, False))

    result = await ingest_attachments(
        [att for att, _ in mapped],
        download=_download,
        source="teams",
        handle_audio=True,
    )
    result.rejections.extend(_unsupported_lines(unsupported))
    return await transcribe_audio_attachments(result, "Teams")


def _unsupported_lines(content_types: list[str]) -> list[str]:
    """One visible line per attachment kind Teams sent that is not a file.

    Names the CONTENT TYPE rather than the file name: the type is what explains
    the refusal, and the name is user data that does not need to be echoed back
    into the prompt to make the refusal understandable.
    """
    return [
        f"[Attachment of type {content_type} — unsupported here]" for content_type in content_types
    ]


# ── Outbound ──

#: Raster types Teams renders from a ``data:`` URI attachment. Narrower than the
#: neutral sniffer's set on purpose. Teams documents PNG, JPEG and GIF for a
#: picture message, and states that animated GIF is not supported:
#:
#: * GIF is excluded because whether a GIF is animated is NOT decidable from its
#:   leading bytes, so accepting the format means sometimes sending the one shape
#:   Teams says it will not render. A visible refusal beats a broken message.
#: * WebP and BMP are not in Teams' documented set at all.
TEAMS_INLINE_IMAGE_MIMES = frozenset({"image/png", "image/jpeg"})

#: Per-image ceiling, matching Teams' documented 1 MB picture limit. Fed to
#: extraction as ``max_file_bytes`` so an oversize image is refused BY THE READ
#: with its markdown left intact, never uploaded and rejected by the Connector
#: after its reference was already cut out of the text. Base64 inflation is free
#: here: Teams' ~100 KB activity-payload budget explicitly excludes a base64
#: image, so the picture limit is the only one that binds.
#:
#: Teams also caps a picture at 1024x1024 pixels. Dimensions are NOT pre-checked:
#: reading them means decoding a header per format, and refusing an ordinary
#: 1200-pixel-wide chart would make the feature useless. An activity Teams rejects
#: instead degrades through the renderer's visible per-image refusal.
TEAMS_MAX_INLINE_IMAGE_BYTES = 1024 * 1024

#: References examined per reply. Each accepted image is its own activity and
#: Teams allows 7 requests/second per thread, so a low cap is a rate-limit bound
#: as much as a work bound.
TEAMS_MAX_INLINE_IMAGES = 4

#: Aggregate bytes one seal may hold in memory, and the total handed to the
#: Connector for one reply.
TEAMS_MAX_INLINE_TOTAL_BYTES = TEAMS_MAX_INLINE_IMAGES * TEAMS_MAX_INLINE_IMAGE_BYTES

#: Budgets for :func:`kiro_crew.messaging.outbound_files.extract_local_refs`.
TEAMS_UPLOAD_LIMITS = ExtractLimits(
    max_files=TEAMS_MAX_INLINE_IMAGES,
    max_total_bytes=TEAMS_MAX_INLINE_TOTAL_BYTES,
    max_file_bytes=TEAMS_MAX_INLINE_IMAGE_BYTES,
)

#: A real raster the neutral extractor accepted that Teams cannot render inline.
#: Channel-owned rather than added to the neutral ``REASON_*`` table: the reason
#: is a property of this platform, not of extraction.
REASON_INLINE_UNSUPPORTED = "teams_inline_unsupported"
#: An image whose own activity could not be delivered.
REASON_INLINE_UNDELIVERED = "teams_inline_undelivered"


def unsupported_inline_rejection(file: OutboundFile) -> Rejection:
    """Refusal for a raster Teams will not render inline.

    Names the resolved path, because the byte-level type is only knowable AFTER
    the read -- by which point extraction has already cut the markdown out. The
    path therefore stays visible in the reply the way rejected markup would,
    which is the property that matters: the user is never told about a picture
    that is not there.
    """
    return Rejection(
        file.path,
        REASON_INLINE_UNSUPPORTED,
        "Teams renders only PNG and JPEG inline",
    )


def undeliverable_rejection(file: OutboundFile) -> Rejection:
    """Refusal for an image Teams accepted the shape of but did not deliver."""
    return Rejection(file.path, REASON_INLINE_UNDELIVERED, "the image could not be delivered")


#: Everything outside this set is collapsed to ``_`` in an attachment name, so an
#: LLM-authored caption can never steer a path, a header, or Teams' own rendering.
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
#: Display name length. Long enough to stay recognisable, short enough that the
#: name cannot become the payload.
_MAX_INLINE_NAME_CHARS = 64
#: Canonical extension per inlinable type, so the name never claims a type the
#: bytes are not.
_INLINE_SUFFIX = {"image/png": ".png", "image/jpeg": ".jpg"}

#: Raster extensions replaced rather than appended to, so a name derived from
#: ``chart.png`` for JPEG bytes reads ``chart.jpg`` instead of claiming both.
_REPLACEABLE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})


def inline_image_name(caption: str, file: OutboundFile) -> str:
    """A safe display name for an inline image, from *caption* or the file name.

    Teams RENDERS an attachment name, so this is a display sink like the answer
    body, and both of its sources are untrusted: *caption* is LLM-authored alt
    text, and the fallback basename comes from an LLM-authored path. Sanitized to a
    conservative character set, stripped of any leading dot or underscore (so it
    cannot read as a hidden or path-relative name), and re-suffixed from the SNIFFED
    type, so the name cannot disagree with the bytes.

    Both the SOURCE and the finished name are scanned, and any hit replaces the
    name wholesale. Scanning here rather than trusting the caller is what closes the
    real hole: ``_SAFE_NAME_RE`` preserves ``[A-Za-z0-9._-]``, which is every
    character an ``AKIA…`` key id or a ``ghp_…`` token needs, and extraction has
    already cut the path out of the answer body -- so for an empty caption this name
    is the ONLY surviving sink. Scanning the source too is not belt-and-braces: the
    64-char cut below can slice a token down to a prefix the scanner no longer
    matches, which would ship most of a secret past a check on the result alone.
    """
    raw = caption.strip() or os.path.basename(file.path)
    suffix = _INLINE_SUFFIX.get(file.mime, ".png")
    scanned, _ = redact_for_display(raw, _default_redactor)
    if scanned != raw:
        return f"image{suffix}"
    stem = _SAFE_NAME_RE.sub("_", raw).strip("._")
    head, ext = os.path.splitext(stem)
    if ext.lower() in _REPLACEABLE_EXTS:
        stem = head
    stem = stem[:_MAX_INLINE_NAME_CHARS]
    name = f"{stem or 'image'}{suffix}"
    scanned, _ = redact_for_display(name, _default_redactor)
    return name if scanned == name else f"image{suffix}"


def inline_image_attachment(file: OutboundFile, name: str) -> dict[str, Any]:
    """One Bot Framework ``Attachment`` carrying *file* as a ``data:`` URI.

    Blocking-free but CPU-bound: base64 of at most
    :data:`TEAMS_MAX_INLINE_IMAGE_BYTES`, which the caller runs off the event loop
    together with the rest of its per-file work.

    ``file.data`` is encoded -- never a re-read of ``file.path``. Every gate the
    neutral extractor applied (denylist, symlink refusal, descriptor-pinned read,
    byte signature) was applied to those exact bytes, and a path resolved a second
    time here could name a different file by then.
    """
    encoded = base64.b64encode(file.data).decode("ascii")
    return {
        "contentType": file.mime,
        "contentUrl": f"data:{file.mime};base64,{encoded}",
        "name": name,
    }
