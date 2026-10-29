"""Signal's half of outbound file delivery: what this channel will upload, and how.

The channel-neutral half is :mod:`kiro_crew.messaging.outbound_files`: it finds
``![chart](/tmp/chart.png)`` references in a reply, clears each through the
security floor (approved roots, the sensitive-path denylist, no symlinks, a
descriptor-pinned read, a raster magic-byte allowlist, per-message budgets) and
hands back the rewritten text plus the validated BYTES. What it leaves to a
channel is the policy -- how many files, how big -- and the upload itself.
Both are here, mirroring :mod:`kiro_crew.whatsapp.files`.

**The bytes travel as a ``data:`` URI.** signal-cli's ``send`` takes each
attachment as a file path OR an RFC 2397 data URI with an optional
``filename=`` parameter. Handing it the path would re-open a name the gates
already resolved, which the shared contract forbids (anything able to write
that directory could swap the file in between), so every upload is the
validated bytes, encoded: :func:`data_uri`.

**The ceilings are OUR policy, not a platform figure** (Signal itself accepts
attachments far larger). They are WhatsApp's numbers, for the same reasons:
bytes held in memory between extraction and upload, and how many separate
media messages one turn drops into a personal chat.

**Planning runs BEFORE splitting**, so a reference is never cut in half by the
length splitter, and **a refusal is spoken, not swallowed**: every rejection
reaches the user through :func:`rejection_note`.

Dependency direction is ``signal_cli -> messaging`` (allowed).
"""

from __future__ import annotations

import base64
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from kiro_crew.messaging.outbound_files import (
    ExtractLimits,
    OutboundFile,
    Rejection,
    extract_local_refs_off_loop,
    filename_passes_screen,
)
from kiro_crew.messaging.upload_gate import uploads_restricted

logger = logging.getLogger(__name__)

#: Files one reply may upload (WhatsApp's chosen ceiling, same reasoning).
SIGNAL_MAX_UPLOAD_FILES = 4

#: Bytes one uploaded file may carry. Chosen, not sourced.
SIGNAL_MAX_FILE_BYTES = 8 * 1024 * 1024

#: Bytes one reply may hand the transport in total. Deliberately below
#: ``SIGNAL_MAX_UPLOAD_FILES * SIGNAL_MAX_FILE_BYTES`` so it actually binds:
#: it is the memory peak extraction holds until the sends finish.
SIGNAL_MAX_TOTAL_UPLOAD_BYTES = 16 * 1024 * 1024

#: Refusals named individually in :func:`rejection_note`; the rest are counted.
SIGNAL_MAX_REJECTION_LINES = 3

#: Characters of a refused path shown in the note. Longer paths keep their
#: TAIL, because the filename is what identifies the picture to the user.
SIGNAL_REJECTION_DEST_CHARS = 60

#: Reason code this channel adds to the shared ``REASON_*`` set: the upload
#: itself failed. A rejection rather than a log line, because extraction has
#: already cut the reference out of the delivered text.
REASON_UPLOAD_FAILED = "upload_failed"

#: Reason code for a reply whose session may not ship local files at all.
REASON_RESTRICTED = "restricted_session"

_NOTE_HEADER = "Not sent:"
_NOTE_BULLET = "•"


@dataclass
class UploadPlan:
    """The reply minus the markup of every file in :attr:`files`, plus refusals."""

    text: str
    files: list[OutboundFile] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)


def signal_limits() -> ExtractLimits:
    """The per-message budgets handed to the shared extractor (built per call,
    so the constants above stay the single owner of each number)."""
    return ExtractLimits(
        max_files=SIGNAL_MAX_UPLOAD_FILES,
        max_total_bytes=SIGNAL_MAX_TOTAL_UPLOAD_BYTES,
        max_file_bytes=SIGNAL_MAX_FILE_BYTES,
    )


def _upload_restricted_probe(_slot: str) -> "tuple[bool, str | None]":
    """The persisted-transcript probe :func:`uploads_restricted` takes.

    Only consulted for a ``dashboard:`` session key, which a Signal turn never
    has (``supports_session_resume`` is False), so it is never reached; should
    that change, "could not tell" denies.
    """
    return False, None


async def plan_uploads(text: str, *, within_root: str, session_key: str) -> UploadPlan:
    """Plan *text*'s file uploads, off the event loop. Never raises.

    *within_root* is the only tree a reference may name -- the provider's
    resolved working directory -- because the reply text is untrusted input.
    An empty or relative root, or a session the shared upload gate restricts
    (incognito / temporary), uploads nothing: the text then goes out as
    written, with its references visible, and a restricted session is told why.
    """
    if not text or not within_root or not os.path.isabs(within_root):
        return UploadPlan(text=text or "")
    try:
        result = await extract_local_refs_off_loop(
            text, within_root=within_root, limits=signal_limits()
        )
    except Exception:
        logger.warning("signal: outbound file planning failed", exc_info=True)
        return UploadPlan(text=text)
    if not result.files:
        return UploadPlan(text=result.rewritten_text, rejections=list(result.rejections))
    # The same restricted-session ceiling every uploading channel asks, AFTER
    # extraction found something to ship (so an ordinary reply costs nothing).
    # It audits its own denial.
    try:
        restricted = await uploads_restricted(
            None,
            session_key,
            channel_type="signal",
            persisted_probe=_upload_restricted_probe,
        )
    except Exception:
        logger.warning("signal: upload gate failed; uploading nothing", exc_info=True)
        restricted = True
    if restricted:
        # Keep the original text (markup and all) so nothing silently vanishes.
        refused = [
            Rejection(f.path, REASON_RESTRICTED, "this conversation may not send files")
            for f in result.files
        ]
        return UploadPlan(text=text, rejections=refused + list(result.rejections))
    return UploadPlan(
        text=result.rewritten_text, files=list(result.files), rejections=list(result.rejections)
    )


def data_uri(data: bytes, mime: str, filename: str = "") -> str:
    """*data* as the RFC 2397 data URI signal-cli's ``send`` accepts.

    ``data:<mime>;filename=<name>;base64,<payload>``. signal-cli URL-decodes the
    parameter value and accepts exactly one ``key=value`` pair here, so the name
    is percent-encoded whole (no ``;``, ``,`` or ``=`` survives raw). A mime that
    is not ``type/subtype`` falls back to ``application/octet-stream``, because a
    URI signal-cli cannot parse is retried by it as a FILE PATH.
    """
    clean_mime = (mime or "").split(";", 1)[0].strip().lower()
    major, _, minor = clean_mime.partition("/")
    if not major or not minor or "/" in minor:
        clean_mime = "application/octet-stream"
    params = f";filename={quote(filename, safe='')}" if filename else ""
    payload = base64.b64encode(data).decode("ascii")
    return f"data:{clean_mime}{params};base64,{payload}"


#: The stem a document goes out under when its own name fails the screen.
DOCUMENT_FALLBACK_STEM = "file"

#: An extension short and plain enough to keep on the fallback name: it can carry
#: neither a credential nor an address, and it keeps the file openable.
_PLAIN_SUFFIX_RE = re.compile(r"\.[A-Za-z0-9]{1,10}")


def document_filename(name: str) -> str:
    """The name a ``send_document`` attachment travels under.

    *name* is the file's basename (``file_send`` admitted the file, not its
    name, and the name is metadata the recipient's phone shows). It is kept
    whole, extension included, when it passes the shared upload-name screen
    (:func:`~kiro_crew.messaging.outbound_files.filename_passes_screen`, the one
    ``upload_filename`` applies); otherwise it is replaced by
    :data:`DOCUMENT_FALLBACK_STEM` plus its extension when that extension is
    plain, so a ``report.pdf`` named after a key still opens as a PDF.
    """
    if not name:
        return DOCUMENT_FALLBACK_STEM
    if filename_passes_screen(name):
        return name
    suffix = Path(name).suffix
    return DOCUMENT_FALLBACK_STEM + (suffix if _PLAIN_SUFFIX_RE.fullmatch(suffix) else "")


def _short_dest(dest: str) -> str:
    if len(dest) > SIGNAL_REJECTION_DEST_CHARS:
        return f"…{dest[-(SIGNAL_REJECTION_DEST_CHARS - 1):]}"
    return dest


def rejection_note(rejections: list[Rejection]) -> str:
    """A plain-text block naming why each file was not sent, or ``""``.

    Plain text because this channel sends no markup (see ``plaintext.py``).
    """
    if not rejections:
        return ""
    lines = [_NOTE_HEADER]
    for rejection in rejections[:SIGNAL_MAX_REJECTION_LINES]:
        detail = rejection.detail or "the upload failed"
        if rejection.dest:
            lines.append(f"{_NOTE_BULLET} {_short_dest(rejection.dest)}: {detail}")
        else:
            lines.append(f"{_NOTE_BULLET} {detail}")
    hidden = len(rejections) - SIGNAL_MAX_REJECTION_LINES
    if hidden > 0:
        lines.append(f"{_NOTE_BULLET} and {hidden} more")
    return "\n".join(lines)


def provider_cwd(sessions: Any, session_key: str) -> str:
    """The acquired provider's working directory, or ``""`` when unknown.

    The one tree an outbound file reference may name. Empty disables uploads
    rather than falling back to anything wider.
    """
    try:
        provider = sessions.get_provider(session_key)
    except Exception:  # noqa: BLE001: no provider means no uploads, never a failure
        return ""
    return str(getattr(provider, "cwd", "") or "")
