"""Construct an RFC 5322 / MIME message from structured parts — pure logic.

:func:`build_message` assembles the correct MIME tree for whatever combination
of parts a caller supplies, choosing the minimal structure each case needs:

* text only, or HTML only → a single leaf part;
* text + HTML → ``multipart/alternative``;
* HTML + inline images → the HTML part becomes a ``multipart/related`` wrapping
  the HTML and each image; if a text alternative is also present the result is
  ``multipart/alternative(text/plain, multipart/related(text/html, image…))`` —
  the ``related`` wraps only the HTML branch, which is the widely-compatible
  structure the stdlib emits and what mail clients expect (the plain-text
  alternative has no ``cid:`` references, so it does not belong inside the
  ``related``);
* any attachments → a top-level ``multipart/mixed`` wrapping the body subtree
  and each attachment part.

Encoding rules enforced here:

* Non-ASCII header text (``Subject``, display names) is emitted as RFC 2047
  encoded-words, and the body text part declares ``charset="utf-8"``, so a
  Chinese subject and body round-trip exactly.
* Non-ASCII attachment filenames use the RFC 2231 ``filename*=UTF-8''…`` form.
* ``To``, ``Cc`` AND ``Bcc`` are all written into the message header block.
  Gmail's ``users.messages.send`` accepts only a ``raw`` field and delivers to
  the recipients named in the ``To`` / ``Cc`` / ``Bcc`` headers — there is no
  separate envelope, so a ``Bcc`` omitted from ``raw`` is a blind recipient who
  silently never receives the mail. Gmail strips the ``Bcc`` header from the
  copies it delivers, providing blind-copy privacy on this transport. Reference:
  https://developers.google.com/gmail/api/reference/rest/v1/users.messages/send

The output is a :class:`BuiltMessage` carrying the serialized bytes; the
recipients are the To/Cc/Bcc headers within those bytes (there is no separate
envelope list on the ``messages.send`` transport).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import compat32 as compat32_policy
from email.utils import format_datetime, make_msgid

from kiro_crew.connections.vendors.gmail.addresses import (
    Mailbox,
    RecipientSet,
    format_mailbox_list,
)
from kiro_crew.connections.vendors.gmail.attachments import Attachment, InlineImage
from kiro_crew.connections.vendors.gmail.errors import (
    AttachmentTooLargeError,
    InvalidAliasError,
    MissingHeaderError,
)
from kiro_crew.connections.vendors.gmail.raw import encode_raw

# A single RFC 5322 msg-id: <local@domain>, no whitespace, exactly one pair of
# angle brackets. A caller-supplied Message-ID must match this exactly so it
# cannot desync the header (a bare token or multiple ids).
_MSGID_RE = re.compile(r"^<[^<>@\s]+@[^<>@\s]+>$")


@dataclass
class BuiltMessage:
    """The product of :func:`build_message`.

    ``rfc822`` is the serialized message bytes — the full RFC 5322 message
    INCLUDING its ``Bcc`` header, since Gmail's ``messages.send`` derives blind
    delivery from that header and strips it from the copies it delivers.
    ``message_id`` is the generated (or supplied) ``Message-ID``. There is no
    separate envelope-recipient list: on this transport the recipients ARE the
    To/Cc/Bcc headers in ``rfc822`` / :meth:`raw`.
    """

    rfc822: bytes
    message_id: str

    def raw(self) -> str:
        """Gmail ``messages.send`` ``raw`` form of the serialized bytes."""
        return encode_raw(self.rfc822)


def _set_addr_header(msg: EmailMessage, name: str, boxes: list[Mailbox]) -> None:
    if boxes:
        msg[name] = format_mailbox_list(boxes)


def _set_subject(msg: EmailMessage, subject: str) -> None:
    # EmailMessage's modern policy RFC 2047-encodes a non-ASCII header value on
    # assignment AND folds it correctly. Assigning a pre-encoded
    # Header(...).encode() string instead inserts hard newlines that the
    # EmailMessage setter then rejects with ValueError on an ordinary ~20-char
    # Unicode subject — so hand the raw Unicode straight to the policy.
    msg["Subject"] = subject


def _apply_filename(part: EmailMessage, filename: str) -> None:
    """Set a Content-Disposition filename, RFC 2231-encoding non-ASCII names.

    The stdlib ``EmailMessage`` add_* helpers already emit ``filename*`` for
    non-ASCII names when handed a ``filename`` kwarg, so we route through the
    param setter it uses. This keeps a Chinese filename intact on round-trip.
    """
    part.add_header("Content-Disposition", "attachment", filename=filename)


def build_message(
    *,
    sender: str,
    recipients: RecipientSet,
    subject: str,
    text_body: str | None = None,
    html_body: str | None = None,
    inline_images: list[InlineImage] | None = None,
    attachments: list[Attachment] | None = None,
    in_reply_to: str = "",
    references: str = "",
    message_id: str | None = None,
    date: datetime | None = None,
    max_total_bytes: int | None = None,
) -> BuiltMessage:
    """Assemble a MIME message from structured parts.

    ``sender`` is the ``From`` addr_spec (already alias-validated by the caller
    via :func:`validate_send_as` when a ``sendAs`` alias is in play). At least
    one of ``text_body`` / ``html_body`` must be provided, and at least one
    recipient must exist, or :class:`MissingHeaderError` is raised.

    ``inline_images`` require an ``html_body`` (a ``cid:`` reference lives in
    HTML); passing inline images with no HTML raises :class:`MissingHeaderError`.

    When ``max_total_bytes`` is set, the serialized size is checked after
    assembly and :class:`AttachmentTooLargeError` is raised if it is exceeded —
    a total-message ceiling distinct from the per-attachment cap.
    """
    inline_images = inline_images or []
    attachments = attachments or []

    if not sender.strip():
        raise MissingHeaderError("From (sender) is required")
    if not recipients.has_recipients():
        raise MissingHeaderError("at least one To/Cc/Bcc recipient is required")
    if text_body is None and html_body is None:
        raise MissingHeaderError("a text or HTML body is required")
    if inline_images and html_body is None:
        raise MissingHeaderError("inline images require an HTML body")
    # A CR/LF in any single-value header input (sender, subject, threading
    # headers) is the classic header-injection vector and, unwrapped by the
    # Mailbox gate, would otherwise reach the EmailMessage setter / as_bytes()
    # and raise an uncaught ValueError. Refuse it as a typed MissingHeaderError
    # up front. (Recipient CR/LF is already rejected at Mailbox construction.)
    for _hname, _hval in (
        ("From", sender),
        ("Subject", subject),
        ("In-Reply-To", in_reply_to),
        ("References", references),
        ("Message-ID", message_id or ""),
    ):
        if "\r" in _hval or "\n" in _hval:
            raise MissingHeaderError(f"{_hname} contains a line break (header injection)")

    # Validate the sender is a real addr-spec before assignment: an unvalidated
    # structured header can crash the EmailMessage setter or desync the emitted
    # output. Route it through Mailbox (which builds+validates an Address and
    # translates the failure) rather than assigning a raw string.
    try:
        Mailbox(address=sender.strip())
    except InvalidAliasError as exc:
        raise MissingHeaderError(
            f"From (sender) is not a valid address: {sender!r}: {exc}"
        ) from exc
    # A caller-supplied Message-ID must be exactly one RFC 5322 msgid
    # (``<id@domain>``): a bare or multi-token value would desync the header.
    if message_id is not None:
        mid_candidate = message_id.strip()
        if not _MSGID_RE.fullmatch(mid_candidate):
            raise MissingHeaderError(f"Message-ID is not a single RFC 5322 msg-id: {message_id!r}")

    msg = EmailMessage()
    msg["From"] = _from_header(sender)
    # Assign recipients as Address objects so EmailMessage's policy encodes and
    # folds non-ASCII display names safely (pre-encoded strings crash the
    # setter on ordinary Unicode names). To/Cc/Bcc are ALL written into the
    # header block: Gmail's messages.send accepts only `raw` and delivers to
    # the recipients named in the To/Cc/Bcc headers — there is no separate
    # envelope, so a Bcc omitted here is a blind recipient who silently never
    # receives the mail. Gmail strips the Bcc header from the copies it
    # delivers, providing the blind-copy privacy on this transport.
    for header_name, addrs in recipients.header_addresses():
        msg[header_name] = addrs
    _set_subject(msg, subject)

    mid = message_id or make_msgid()
    msg["Message-ID"] = mid
    msg["Date"] = format_datetime(date or datetime.now(timezone.utc))
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references

    _fill_body(msg, text_body, html_body, inline_images, attachments)

    rfc822 = msg.as_bytes()
    if max_total_bytes is not None and len(rfc822) > max_total_bytes:
        raise AttachmentTooLargeError(
            f"assembled message is {len(rfc822)} bytes, exceeds cap {max_total_bytes}"
        )

    return BuiltMessage(
        rfc822=rfc822,
        message_id=mid,
    )


def _from_header(sender: str) -> str:
    return sender.strip()


def _fill_body(
    msg: EmailMessage,
    text_body: str | None,
    html_body: str | None,
    inline_images: list[InlineImage],
    attachments: list[Attachment],
) -> None:
    """Populate ``msg`` with the correct MIME subtree for the given parts."""
    # 1. Establish the body content (leaf, or multipart/alternative).
    if html_body is not None:
        if text_body is not None:
            msg.set_content(text_body)
            msg.add_alternative(html_body, subtype="html")
        else:
            msg.set_content(html_body, subtype="html")
    else:
        msg.set_content(text_body or "")

    # Attach inline images as related resources of the HTML part; add_related
    # promotes the HTML into a multipart/related for us.
    if html_body is not None and inline_images:
        _attach_inline_images(msg, inline_images)

    # 2. Attach file attachments (promotes body to multipart/mixed as needed).
    for att in attachments:
        maintype, _, subtype = att.content_type.partition("/")
        # Media types are case-insensitive (RFC 2045 §5.1), so normalize before
        # dispatch: an uppercase "MESSAGE/delivery-status" must still take the
        # message/* branch, not fall through to the raw-bytes path that crashes
        # as_bytes().
        maintype = (maintype or "application").lower()
        subtype = (subtype or "octet-stream").lower()
        if maintype == "message":
            # A message/* attachment (message/rfc822, message/delivery-status,
            # …) must preserve the caller's EXACT bytes: parsing under the
            # default policy and re-serializing normalizes LF -> CRLF (and can
            # refold headers), which breaks a checksum the caller took over the
            # original bytes. Parsing under COMPAT32 instead keeps the inner
            # message's line endings verbatim, and the generator flattens it
            # faithfully. Attaching raw bytes with maintype="message" builds an
            # invalid payload the generator crashes on, so a parsed Message is
            # required. add_attachment given a Message infers message/rfc822 and
            # drops the real subtype, so a non-rfc822
            # subtype is corrected on the just-appended outer part afterwards.
            inner = BytesParser(policy=compat32_policy).parsebytes(att.content)
            msg.add_attachment(inner, filename=att.filename)
            if subtype != "rfc822":
                _fix_message_attachment_subtype(msg, subtype)
        else:
            msg.add_attachment(
                att.content,
                maintype=maintype,
                subtype=subtype,
                filename=att.filename,
            )


def _fix_message_attachment_subtype(msg: EmailMessage, subtype: str) -> None:
    """Rewrite the JUST-APPENDED ``message/rfc822`` attachment part's subtype.

    ``add_attachment(<Message>)`` always emits ``Content-Type: message/rfc822``
    regardless of the source part's real type, because its content-manager does
    not accept a subtype for a ``Message`` object. A ``message/delivery-status``
    (or any non-rfc822 ``message/*``) attachment would therefore be silently
    relabeled. Correct ONLY the outer part just appended by ``add_attachment``
    — the last direct child of the message's top-level payload — rather than
    walking the whole tree: a walk could descend into a NESTED
    ``message/rfc822`` (e.g. inside a forwarded ``message/global``) and rewrite
    that instead, corrupting the inner subtype while leaving the outer
    mislabeled.
    """
    payload = msg.get_payload()
    if not isinstance(payload, list) or not payload:
        return
    target = payload[-1]
    if (
        isinstance(target, EmailMessage)
        and target.get_content_type() == "message/rfc822"
        and target.get_content_disposition() == "attachment"
    ):
        target.replace_header("Content-Type", f"message/{subtype}")


def _attach_inline_images(msg: EmailMessage, inline_images: list[InlineImage]) -> None:
    """Attach each inline image as a related resource of the HTML body part.

    ``EmailMessage.add_related`` on the HTML alternative wraps it in a
    ``multipart/related`` automatically and sets ``Content-Disposition: inline``
    plus the ``Content-ID`` from the ``cid`` argument.
    """
    html_part = _find_html_part(msg)
    if html_part is None:
        return
    # Reject duplicate content IDs: two inline images sharing one Content-ID
    # make the HTML `cid:` reference ambiguous, and a later filename update
    # keyed by Content-ID would corrupt the FIRST matching part. Each cid must
    # be unique.
    seen_cids: set[str] = set()
    for img in inline_images:
        if img.content_id in seen_cids:
            raise MissingHeaderError(f"duplicate inline-image Content-ID: {img.content_id!r}")
        seen_cids.add(img.content_id)
    for img in inline_images:
        maintype, _, subtype = img.content_type.partition("/")
        html_part.add_related(
            img.content,
            maintype=maintype or "image",
            subtype=subtype or "png",
            cid=f"<{img.content_id}>",
        )
        if img.filename:
            # add_related sets Content-Disposition: inline with no filename.
            # Re-set it on the JUST-APPENDED related part (the last related
            # child) to carry the filename WHILE keeping the inline disposition
            # (a filename kwarg to add_related would flip it to attachment).
            related = _last_related_part(html_part)
            if related is not None:
                related.replace_header(
                    "Content-Disposition",
                    "inline",
                )
                related.set_param("filename", img.filename, header="Content-Disposition")


def _last_related_part(html_part: EmailMessage) -> EmailMessage | None:
    """Return the most-recently-added related sub-part (last inline resource).

    ``add_related`` appends the new resource as the last child of the
    ``multipart/related`` it wraps the HTML in, so the just-added part is the
    final walked sub-part carrying a ``Content-ID`` — targeting it directly
    avoids a Content-ID search that would hit the FIRST match on a duplicate id.
    """
    target: EmailMessage | None = None
    for part in html_part.walk():
        if part.get("Content-ID") is not None:
            target = part  # type: ignore[assignment]
    return target


def _find_html_part(msg: EmailMessage):
    """Return the ``text/html`` leaf of ``msg``, or ``None`` if absent."""
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            return part
    return None
