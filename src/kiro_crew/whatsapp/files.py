"""WhatsApp's half of outbound file delivery: what this channel will upload.

The channel-neutral half is :mod:`kiro_crew.messaging.outbound_files`: it finds
``![chart](/tmp/chart.png)`` references in a reply, clears each one through the
security floor, and hands back the rewritten text plus the validated bytes. What
it deliberately leaves to a channel is the policy -- how many files one message
may carry, how big each may be, and what the user is told about a reference that
could not be sent. That policy is here, in one call
(:func:`plan_uploads_off_loop`) so the renderer does not have to know the shape
of the shared extractor.

**The ceilings here are OUR policy, not a platform figure.** WhatsApp publishes
no per-file or per-message media limit this repo can source, and the Web protocol
``neonize`` speaks does not report one, so every constant below is a chosen
number justified by what it costs us: bytes held in memory between extraction and
upload, and how many separate media messages one turn drops into a personal chat.
None of them may be read as "what WhatsApp allows".

**Planning runs BEFORE splitting.** The renderer must plan the whole reply and
then split what remains: a reference cut in half by the length splitter leaves
half a markdown link in each chunk, which no later pass recognises and the user
reads as broken markup.

**A planned file must be decodable, because the transport decodes it.**
``neonize``'s image build opens the bytes with Pillow to make a thumbnail before
it uploads anything, so bytes that sniff as a raster yet cannot be decoded (a
truncated PNG is the common case) raise inside the send and cost the whole
message rather than one picture. The plan screens those out first, through the
memoized Pillow handle in :mod:`kiro_crew.imaging`, and the refusal is spoken by
:func:`rejection_note`, so the user still learns which picture is missing and
why. Pillow is never imported at this module's top: ``whatsapp`` is reachable
from the gateway's import graph, so an import here lands on every operator's boot
path whether or not the channel is enabled.

**A refusal is spoken, not swallowed.** A reply that references a picture, with
no picture and no explanation, is worse than a sentence saying the file was too
large. Every rejection the extractor reports, plus every one added here, reaches
the user through :func:`rejection_note`.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from kiro_crew.imaging import image_dimensions, pil_available
from kiro_crew.messaging.outbound_files import (
    ExtractLimits,
    ExtractResult,
    OutboundFile,
    Rejection,
    extract_local_refs,
    extract_local_refs_off_loop,
)

logger = logging.getLogger(__name__)

#: Files one reply may upload. A chosen ceiling, not a sourced platform limit:
#: each file is its own media message with a pause between sends, so a reply full
#: of images arrives as a burst that reads as spam in a personal chat. Four keeps
#: a turn to a handful of media messages; references past it are reported.
WHATSAPP_MAX_UPLOAD_FILES = 4

#: Bytes one uploaded file may carry. A chosen ceiling, not a sourced platform
#: limit. A chart or screenshot is well under a megabyte, and 8 MiB still covers a
#: photo-sized PNG while bounding both the memory one file occupies and how long a
#: single upload holds the channel's send path.
WHATSAPP_MAX_FILE_BYTES = 8 * 1024 * 1024

#: Bytes one reply may hand the transport in total. A chosen ceiling, not a
#: sourced platform limit. Deliberately BELOW
#: ``WHATSAPP_MAX_UPLOAD_FILES * WHATSAPP_MAX_FILE_BYTES``, so it actually binds:
#: extraction holds every validated file in memory until the transport has sent
#: it, and this is that peak.
WHATSAPP_MAX_TOTAL_UPLOAD_BYTES = 16 * 1024 * 1024

#: Source pixels (width x height) an uploaded image may have. A chosen ceiling,
#: not a sourced platform limit, and a CPU/memory bound rather than a size
#: preference: ``neonize`` fully decodes the raster and rescales it to a thumbnail
#: before uploading, at roughly 4 bytes per source pixel. 40M pixels clears any
#: real screenshot or camera frame while keeping that decode off the order of a
#: gigabyte.
WHATSAPP_MAX_IMAGE_PIXELS = 40_000_000

#: Refusals named individually in :func:`rejection_note`. The note rides in the
#: reply, so an agent that emitted twenty broken references must not turn the
#: answer into a list of complaints; the rest are counted.
WHATSAPP_MAX_REJECTION_LINES = 3

#: Characters of a refused path shown in the note. Longer paths keep their TAIL,
#: because the filename is what identifies the picture to the user.
WHATSAPP_REJECTION_DEST_CHARS = 60

#: WhatsApp's inline-code marker. A refused path is wrapped in it so the dialect
#: leaves it alone: ``/tmp/my_chart_1.png`` written bare renders as italics with
#: the underscores eaten, which shows the user a path that is not the one the
#: agent wrote.
_INLINE_CODE = "`"

#: WhatsApp renders no small text, so the note is an ordinary bulleted block.
_NOTE_BULLET = "•"
#: Reads correctly for one refusal and for several, so the note needs no
#: singular/plural branch.
_NOTE_HEADER = "Not sent:"

#: Reason codes this channel adds to the shared ``REASON_*`` set. A caller that
#: wants to re-word or branch matches on these rather than on the prose.
REASON_UNDECODABLE = "undecodable"
REASON_OVER_PIXEL_BUDGET = "over_pixel_budget"
#: The upload itself failed at the wire. A rejection rather than a log line:
#: extraction has already taken the markdown reference out of the delivered text,
#: so a silent failure leaves the reader with neither the picture nor the path.
REASON_UPLOAD_FAILED = "upload_failed"


@dataclass
class UploadPlan:
    """Everything the renderer needs to deliver one reply.

    :attr:`text` is the reply minus the markup of every file in :attr:`files`,
    ready to be converted and split. A reference that could not be sent keeps its
    markup, so its path stays visible next to the reason in
    :func:`rejection_note`.
    """

    text: str
    files: list[OutboundFile] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)


def whatsapp_limits() -> ExtractLimits:
    """The per-message budgets this channel hands the shared extractor.

    Built per call rather than as a module-level instance so the constants above
    stay the single owner of each number: a patched ceiling takes effect, instead
    of being frozen into a value captured at import.
    """
    return ExtractLimits(
        max_files=WHATSAPP_MAX_UPLOAD_FILES,
        max_total_bytes=WHATSAPP_MAX_TOTAL_UPLOAD_BYTES,
        max_file_bytes=WHATSAPP_MAX_FILE_BYTES,
    )


def _decode_refusal(file: OutboundFile) -> Rejection | None:
    """Why *file* cannot be handed to the transport, or ``None`` if it can.

    Header-only inspection: :func:`kiro_crew.imaging.image_dimensions` reads the
    container header without decoding pixels, so screening costs nothing next to
    the decode it prevents.
    """
    if not pil_available():
        # Nothing is verifiable without Pillow, and the transport's own image
        # build needs Pillow too: on a hand-stripped install let that send fail
        # loudly rather than refuse every picture here for an unrelated reason.
        return None
    dims = image_dimensions(file.data)
    if dims is None:
        return Rejection(file.path, REASON_UNDECODABLE, "the image could not be decoded")
    if dims[0] * dims[1] > WHATSAPP_MAX_IMAGE_PIXELS:
        return Rejection(
            file.path,
            REASON_OVER_PIXEL_BUDGET,
            f"{dims[0]}x{dims[1]} is past this channel's image size limit",
        )
    return None


def _plan_from(result: ExtractResult) -> UploadPlan:
    """Apply the decode screen to an extraction result.

    A file screened out here has already had its markup cut from the text, which
    is the one thing the shared module warns against doing after extraction. It
    is not the silent drop that warning is about: the refusal is carried in
    :attr:`UploadPlan.rejections` and named in :func:`rejection_note`, so the user
    is told which file is missing. The alternative is worse, because the decode
    happens inside the transport and takes the whole reply down with it.
    """
    files: list[OutboundFile] = []
    rejections = list(result.rejections)
    for file in result.files:
        refusal = _decode_refusal(file)
        if refusal is None:
            files.append(file)
            continue
        logger.info("whatsapp: local image not uploaded (%s)", refusal.reason)
        rejections.append(refusal)
    return UploadPlan(text=result.rewritten_text, files=files, rejections=rejections)


def plan_uploads(text: str, *, within_root: str) -> UploadPlan:
    """Plan *text*'s file uploads. Blocking: reads and decodes file headers.

    *within_root* is the only tree a reference may name, and it must be the
    provider's resolved working directory: the reply text is not trustworthy
    input, since a prompt-injected agent chooses what it writes.

    Never raises. A reply must still go out when every reference in it turns out
    to be unusable, so an unexpected failure degrades to "send the text as
    written, upload nothing".

    Async callers MUST use :func:`plan_uploads_off_loop`: the gateway runs every
    channel and the liveness heartbeat on one event loop.
    """
    try:
        result = extract_local_refs(text, limits=whatsapp_limits(), within_root=within_root)
    except Exception:
        logger.warning("whatsapp: outbound file planning failed", exc_info=True)
        return UploadPlan(text=text or "")
    return _plan_from(result)


async def plan_uploads_off_loop(text: str, *, within_root: str) -> UploadPlan:
    """:func:`plan_uploads` with both blocking halves moved off the event loop.

    Two hops, each through the contract that owns it: the shared
    :func:`extract_local_refs_off_loop` for the filesystem work, then a thread for
    the decode screen's header reads. Calling either on the loop would freeze
    every other session for the duration.
    """
    try:
        result = await extract_local_refs_off_loop(
            text, within_root=within_root, limits=whatsapp_limits()
        )
    except Exception:
        logger.warning("whatsapp: outbound file planning failed", exc_info=True)
        return UploadPlan(text=text or "")
    return await asyncio.to_thread(_plan_from, result)


def _short_dest(dest: str) -> str:
    """A refused destination, bounded and safe to show in WhatsApp's dialect."""
    # A backtick inside the path would close the inline-code span early and let
    # the rest of the path be formatted; there is no escape for it in the dialect.
    clean = dest.replace(_INLINE_CODE, "")
    if len(clean) > WHATSAPP_REJECTION_DEST_CHARS:
        clean = f"…{clean[-(WHATSAPP_REJECTION_DEST_CHARS - 1) :]}"
    return f"{_INLINE_CODE}{clean}{_INLINE_CODE}"


def rejection_note(rejections: list[Rejection]) -> str:
    """A user-facing block naming why each file was not sent, or ``""``.

    Written so it survives :func:`kiro_crew.whatsapp.renderer.to_whatsapp_text`
    BYTE-IDENTICALLY, because the renderer's send path converts everything it puts
    on the wire and there is no unconverted send to reach for. Two halves make that
    true: the bullet and header are already dialect the conversion does not match,
    and every path rides in an inline-code span, which the conversion leaves alone
    (``renderer._sub_outside_code``) precisely so a filename holding ``__`` is not
    reformatted into ``*``. A change to either side has to keep the round trip
    exact -- the alternative, asking callers to append this after conversion, is
    not enforceable when the transport converts again on its own.
    """
    if not rejections:
        return ""
    lines = [_NOTE_HEADER]
    for rejection in rejections[:WHATSAPP_MAX_REJECTION_LINES]:
        # A message-level refusal (the per-message file cap) names no single
        # reference, so its detail stands alone.
        label = _short_dest(rejection.dest) if rejection.dest else ""
        prefix = f"{_NOTE_BULLET} {label}: " if label else f"{_NOTE_BULLET} "
        lines.append(f"{prefix}{rejection.detail}")
    hidden = len(rejections) - WHATSAPP_MAX_REJECTION_LINES
    if hidden > 0:
        lines.append(f"{_NOTE_BULLET} and {hidden} more")
    return "\n".join(lines)
