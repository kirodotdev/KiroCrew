"""Parse an RFC 5322 / MIME message into structured parts — pure logic.

:func:`parse_message` is the inverse of :func:`.builder.build_message`: it takes
message bytes (or a decoded :func:`.raw.decode_raw` result) and returns a
:class:`ParsedMessage` exposing decoded headers, the text and HTML bodies, and
every attachment and inline image as :class:`.attachments.Attachment` /
:class:`.attachments.InlineImage` objects — each carrying its raw bytes and a
recomputed checksum, so a caller can verify an extracted attachment against the
one that was constructed.

Decoding rules:

* RFC 2047 encoded-word headers (``Subject``, display names) are decoded back
  to Unicode.
* A body part's declared ``charset`` is honored, so a UTF-8 Chinese body comes
  back as the original text.
* An inline image is recognized by ``Content-Disposition: inline`` OR a
  ``Content-ID`` (Gmail-built related parts carry both); its ``cid`` token is
  exposed stripped of angle brackets, matching what an HTML ``cid:`` reference
  uses.

No silent data loss or corruption:

* A ``Bcc`` header present on the parsed bytes is surfaced on
  :attr:`ParsedMessage.bcc`, never dropped.
* An attached ``message/rfc822`` is captured whole as an attachment, not
  descended into (which would lose the attachment and could let its nested body
  masquerade as this message's own body).
* A body whose bytes are invalid for its declared charset is raised as
  malformed, never silently ``errors="replace"``-normalized into corruption
  carrying a checksum over that corruption.

Malformed input raises :class:`.errors.MalformedMimeError` rather than
returning a half-parsed object. A well-formed message that merely lacks a
header the caller required is NOT malformed — use :func:`require_headers` for
that check, which raises :class:`.errors.MissingHeaderError`.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from email.generator import BytesGenerator
from email.message import EmailMessage as _EmailMessage
from email.message import Message
from email.parser import BytesParser
from email.policy import compat32 as compat32_policy
from email.policy import default as default_policy

from kiro_crew.connections.vendors.gmail.addresses import Mailbox, parse_mailbox_list
from kiro_crew.connections.vendors.gmail.attachments import Attachment, InlineImage
from kiro_crew.connections.vendors.gmail.errors import (
    MalformedMimeError,
    MissingHeaderError,
)


@dataclass
class ParsedMessage:
    """The structured result of parsing a MIME message.

    ``headers`` holds decoded single-value headers by canonical name.
    ``text_body`` / ``html_body`` are ``None`` when absent. ``attachments`` and
    ``inline_images`` carry recomputed checksums for byte-level verification.
    """

    headers: dict[str, str] = field(default_factory=dict)
    to: list[Mailbox] = field(default_factory=list)
    cc: list[Mailbox] = field(default_factory=list)
    bcc: list[Mailbox] = field(default_factory=list)
    text_body: str | None = None
    html_body: str | None = None
    attachments: list[Attachment] = field(default_factory=list)
    inline_images: list[InlineImage] = field(default_factory=list)

    @property
    def subject(self) -> str:
        return self.headers.get("Subject", "")

    @property
    def from_(self) -> str:
        return self.headers.get("From", "")

    @property
    def message_id(self) -> str:
        return self.headers.get("Message-ID", "")

    @property
    def in_reply_to(self) -> str:
        return self.headers.get("In-Reply-To", "")

    @property
    def references(self) -> str:
        return self.headers.get("References", "")


# Headers whose decoded value we surface as a plain string. The email package's
# ``default`` policy already decodes RFC 2047 for us when we str() a header.
_SINGLE_HEADERS = (
    "From",
    "Subject",
    "Message-ID",
    "In-Reply-To",
    "References",
    "Date",
)


def parse_message(data: bytes) -> ParsedMessage:
    """Parse message bytes into a :class:`ParsedMessage`.

    Raises :class:`MalformedMimeError` if the bytes cannot be parsed as a MIME
    message or if a declared multipart carries no usable parts.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("parse_message expects bytes")
    if not data.strip():
        raise MalformedMimeError("empty message")

    try:
        parser = BytesParser(_EmailMessage, policy=default_policy)
        msg = parser.parsebytes(bytes(data))
    except Exception as exc:  # the email parser is broad; normalize its failures
        raise MalformedMimeError(f"could not parse message: {exc}") from exc

    parsed = ParsedMessage()
    _reject_serious_defects(msg)
    for name in _SINGLE_HEADERS:
        raw = msg.get(name)
        if raw is not None:
            parsed.headers[name] = str(raw)

    to_raw = msg.get("To")
    if to_raw is not None:
        parsed.to = parse_mailbox_list(str(to_raw))
    cc_raw = msg.get("Cc")
    if cc_raw is not None:
        parsed.cc = parse_mailbox_list(str(cc_raw))
    # A well-formed delivered message is Bcc-stripped by the sending MTA, but a
    # message assembled locally (e.g. round-tripped before send, or captured
    # pre-transmission) can still carry a Bcc header. Surface it rather than
    # silently dropping blind-recipient data — a caller that must not leak it
    # decides that, not a silent parse.
    bcc_raw = msg.get("Bcc")
    if bcc_raw is not None:
        parsed.bcc = parse_mailbox_list(str(bcc_raw))

    # A multipart declared with no boundary / no parts is malformed. The
    # email parser records this as a structural defect (see
    # _reject_serious_defects) rather than raising, and also collapses the
    # payload to a non-list; both are caught above and here.
    if msg.is_multipart():
        payload = msg.get_payload()
        if not isinstance(payload, list) or not payload:
            raise MalformedMimeError("multipart message has no parts")

    _walk_parts(msg, parsed)
    return parsed


# Defect classes that mean the bytes are not the message they claim to be — a
# boundary was declared but the delimiter never appeared, a multipart
# Content-Type resolved to a non-multipart structure, or a part's declared
# Content-Transfer-Encoding could not decode the body it carries (invalid
# base64). These are structural lies about the message content, distinct from
# cosmetic defects (a header with a trailing space) the parser also records but
# which do not make the message unusable. The base64 defects close the
# attachment side of the "silent corruption" class the text path handles by
# strict decode. Referenced by class NAME so a stdlib version that reorders the
# module does not break the check.
_FATAL_DEFECT_NAMES = frozenset(
    {
        "StartBoundaryNotFoundDefect",
        "CloseBoundaryNotFoundDefect",
        "MultipartInvariantViolationDefect",
        "InvalidBase64CharactersDefect",
        "InvalidBase64PaddingDefect",
        "InvalidBase64LengthDefect",
    }
)


def _reject_serious_defects(msg: Message) -> None:
    """Raise :class:`MalformedMimeError` on a structurally-lying multipart.

    The email parser does not raise on a multipart whose boundary never
    appears; it records a defect and collapses the payload. A connector send
    path must treat that as malformed rather than silently losing the body, so
    this promotes the fatal defect classes to an exception. Base64 CTE defects
    are only appended AFTER a part is decoded, so they are additionally checked
    per-part in :func:`_decoded_payload`.
    """
    for part in msg.walk():
        for defect in getattr(part, "defects", []) or []:
            if type(defect).__name__ in _FATAL_DEFECT_NAMES:
                raise MalformedMimeError(f"malformed multipart: {type(defect).__name__}")


def _walk_parts(msg: Message, parsed: ParsedMessage) -> None:
    """Recurse the MIME tree, filling bodies / attachments / inline images.

    Recursion is explicit rather than via ``Message.walk()`` because ``walk()``
    descends INTO a ``message/rfc822`` attachment's own sub-tree, which would
    both lose the attached message as an attachment and let its nested body be
    mistaken for this message's body. Here a ``message/rfc822`` (or any
    ``multipart`` explicitly dispositioned as an attachment) is captured whole
    and NOT descended into.
    """
    if msg.is_multipart():
        disposition = (msg.get_content_disposition() or "").lower()
        ctype = msg.get_content_type()
        # An attached message/digest is a container, but it is an ATTACHMENT,
        # not part of this message's own body structure — capture it whole.
        if ctype == "message/rfc822" or disposition == "attachment":
            _collect_message_attachment(msg, parsed)
            return
        payload = msg.get_payload()
        if isinstance(payload, list):
            for sub in payload:
                if isinstance(sub, Message):
                    _walk_parts(sub, parsed)
        return

    _collect_leaf(msg, parsed)


def _collect_leaf(part: Message, parsed: ParsedMessage) -> None:
    """Classify and collect one non-multipart leaf part."""
    ctype = part.get_content_type()
    disposition = (part.get_content_disposition() or "").lower()
    content_id = part.get("Content-ID")

    is_inline_image = ctype.startswith("image/") and (
        disposition == "inline" or content_id is not None
    )
    is_attachment = disposition == "attachment"

    if is_inline_image:
        _collect_inline_image(part, parsed, content_id)
    elif is_attachment:
        _collect_attachment(part, parsed)
    elif ctype == "text/plain" and parsed.text_body is None:
        parsed.text_body = _decode_text(part)
    elif ctype == "text/html" and parsed.html_body is None:
        parsed.html_body = _decode_text(part)
    elif not disposition and ctype not in ("text/plain", "text/html"):
        # A non-text, non-dispositioned leaf (rare) is treated as an
        # attachment so its bytes are not silently dropped.
        _collect_attachment(part, parsed)


def _embedded_message_bytes(part: Message) -> bytes:
    """Serialize the message EMBEDDED in a ``message/rfc822`` part, faithfully.

    ``Message.as_bytes()`` on the container is wrong for two reasons the review
    caught: it prepends the CONTAINER's own headers (``Content-Type:
    message/rfc822`` etc.) to the bytes, and it re-serializes through a policy
    that normalizes CRLF -> LF and can refold headers — so a checksum over it is
    a checksum over a rewritten form, not the forwarded message. Serialize the
    embedded message itself (``get_payload()[0]``) with a CRLF-preserving
    generator so the attachment holds the forwarded ``.eml`` content as-is.
    """
    payload = part.get_payload()
    if isinstance(payload, list) and payload and isinstance(payload[0], Message):
        inner = payload[0]
        buf = io.BytesIO()
        # compat32 with an explicit CRLF linesep serializes without the header
        # refolding the default policy applies, preserving the wire form.
        gen = BytesGenerator(
            buf,
            mangle_from_=False,
            policy=compat32_policy.clone(linesep="\r\n"),
        )
        gen.flatten(inner)
        return buf.getvalue()
    # Fallback (an attachment-dispositioned multipart that is not
    # message/rfc822): serialize the part itself, still CRLF-faithful.
    buf = io.BytesIO()
    gen = BytesGenerator(buf, mangle_from_=False, policy=compat32_policy.clone(linesep="\r\n"))
    gen.flatten(part)
    return buf.getvalue()


def _collect_message_attachment(part: Message, parsed: ParsedMessage) -> None:
    """Capture an attached ``message/rfc822`` (or attachment multipart) whole.

    The embedded message's own bytes become the attachment content (see
    :func:`_embedded_message_bytes` for why the container's ``as_bytes()`` is
    not used), so the forwarded message survives faithfully with a checksum, and
    its inner parts are never mistaken for this message's own body.
    """
    try:
        content = _embedded_message_bytes(part)
    except Exception as exc:
        raise MalformedMimeError(f"could not serialize embedded message attachment: {exc}") from exc
    filename = part.get_filename() or "attached-message.eml"
    try:
        parsed.attachments.append(
            Attachment(
                filename=filename,
                content=content,
                content_type=part.get_content_type(),
            )
        )
    except Exception as exc:
        raise MalformedMimeError(f"could not extract embedded message attachment: {exc}") from exc


def _decoded_payload(part: Message) -> bytes:
    """Decode a leaf's payload and reject post-decode corruption.

    ``get_payload(decode=True)`` applies the part's Content-Transfer-Encoding.
    For an invalid base64 body the stdlib does NOT raise: it silently returns
    the raw (undecoded) bytes and APPENDS a base64 defect to ``part.defects``
    only after this call. The parse-time :func:`_reject_serious_defects` sweep
    runs before any part is decoded, so it cannot see that defect — this
    re-checks the part's defects immediately after decoding it, reusing the
    same fatal-defect set, so silent CTE corruption becomes a
    :class:`MalformedMimeError` rather than a checksum over garbage.
    """
    payload = part.get_payload(decode=True)
    for defect in getattr(part, "defects", []) or []:
        if type(defect).__name__ in _FATAL_DEFECT_NAMES:
            raise MalformedMimeError(
                f"malformed content-transfer-encoding: {type(defect).__name__}"
            )
    if not isinstance(payload, (bytes, bytearray)):
        return b""
    return bytes(payload)


def _decode_text(part: Message) -> str:
    """Decode a text leaf using its declared charset — STRICTLY.

    Invalid bytes for the declared charset are NOT silently replaced: silent
    replacement returns corrupted content plus a checksum over that corruption,
    reported as success. A decode failure is raised as
    :class:`MalformedMimeError` so the caller sees the corruption instead of a
    plausible-but-wrong body. An unknown charset LABEL (as opposed to invalid
    bytes) falls back to utf-8 strict, since the label may simply be
    non-standard while the bytes are fine.
    """
    data = _decoded_payload(part)
    charset = part.get_content_charset() or "utf-8"
    try:
        return data.decode(charset)
    except LookupError:
        # Unknown charset label — try utf-8 strictly rather than assuming.
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MalformedMimeError(
                f"body declares unknown charset {charset!r} and is not utf-8: {exc}"
            ) from exc
    except UnicodeDecodeError as exc:
        raise MalformedMimeError(f"body is not valid {charset}: {exc}") from exc


def _collect_attachment(part: Message, parsed: ParsedMessage) -> None:
    content = _decoded_payload(part)
    filename = part.get_filename() or ""
    try:
        parsed.attachments.append(
            Attachment(
                filename=filename,
                content=content,
                content_type=part.get_content_type(),
            )
        )
    except MalformedMimeError:
        raise
    except Exception as exc:
        raise MalformedMimeError(f"could not extract attachment: {exc}") from exc


def _collect_inline_image(part: Message, parsed: ParsedMessage, content_id: str | None) -> None:
    content = _decoded_payload(part)
    cid = (content_id or "").strip().strip("<>").strip()
    if not cid:
        # An inline image with no usable Content-ID cannot be referenced from
        # the body; treat it as a regular attachment so its bytes survive.
        _collect_attachment(part, parsed)
        return
    try:
        parsed.inline_images.append(
            InlineImage(
                content_id=cid,
                content=content,
                content_type=part.get_content_type(),
                filename=part.get_filename() or "",
            )
        )
    except MalformedMimeError:
        raise
    except Exception as exc:
        raise MalformedMimeError(f"could not extract inline image: {exc}") from exc


def require_headers(parsed: ParsedMessage, names: list[str]) -> None:
    """Raise :class:`MissingHeaderError` if any named header is absent/empty.

    A separate step from parsing: a message can be valid MIME yet lack a header
    a particular caller's contract requires (e.g. a send path requiring
    ``From`` and a recipient). This distinguishes "not valid MIME" (parse
    raises) from "valid MIME, missing what I need" (this raises).
    """
    for name in names:
        if name.lower() == "to":
            if not parsed.to:
                raise MissingHeaderError("missing required header: To")
            continue
        if name.lower() == "cc":
            if not parsed.cc:
                raise MissingHeaderError("missing required header: Cc")
            continue
        if not parsed.headers.get(name):
            raise MissingHeaderError(f"missing required header: {name}")
