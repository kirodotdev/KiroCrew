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
* ``Bcc`` is NEVER written into the message header block. The recipient set's
  Bcc addresses reach the transport through :meth:`RecipientSet.envelope_recipients`
  (returned separately), not through the serialized message — this is the one
  place the blind-copy-leak invariant is enforced.

The output is a :class:`BuiltMessage` carrying both the serialized bytes and
the envelope recipient list, so a send path has everything it needs without
re-parsing the bytes to recover the Bcc addresses.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.header import Header
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid

from kiro_crew.connections.vendors.gmail.addresses import (
    Mailbox,
    RecipientSet,
    format_mailbox_list,
)
from kiro_crew.connections.vendors.gmail.attachments import Attachment, InlineImage
from kiro_crew.connections.vendors.gmail.errors import (
    AttachmentTooLargeError,
    MissingHeaderError,
)
from kiro_crew.connections.vendors.gmail.raw import encode_raw


@dataclass
class BuiltMessage:
    """The product of :func:`build_message`.

    ``rfc822`` is the serialized message bytes (what a ``Bcc``-free reader
    sees). ``envelope_recipients`` is the full delivery list INCLUDING Bcc —
    the transport uses this, not the header block, so Bcc stays private.
    ``message_id`` is the generated (or supplied) ``Message-ID``.
    """

    rfc822: bytes
    envelope_recipients: list[str]
    message_id: str

    def raw(self) -> str:
        """Gmail ``messages.send`` ``raw`` form of the serialized bytes."""
        return encode_raw(self.rfc822)


def _set_addr_header(msg: EmailMessage, name: str, boxes: list[Mailbox]) -> None:
    if boxes:
        msg[name] = format_mailbox_list(boxes)


def _set_subject(msg: EmailMessage, subject: str) -> None:
    try:
        subject.encode("ascii")
        msg["Subject"] = subject
    except UnicodeEncodeError:
        msg["Subject"] = Header(subject, "utf-8").encode()


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
    if not recipients.envelope_recipients():
        raise MissingHeaderError("at least one To/Cc/Bcc recipient is required")
    if text_body is None and html_body is None:
        raise MissingHeaderError("a text or HTML body is required")
    if inline_images and html_body is None:
        raise MissingHeaderError("inline images require an HTML body")

    msg = EmailMessage()
    msg["From"] = _from_header(sender)
    for header_name, value in recipients.visible_header_pairs():
        msg[header_name] = value
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
        envelope_recipients=recipients.envelope_recipients(),
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
        msg.add_attachment(
            att.content,
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=att.filename,
        )


def _attach_inline_images(msg: EmailMessage, inline_images: list[InlineImage]) -> None:
    """Attach each inline image as a related resource of the HTML body part.

    ``EmailMessage.add_related`` on the HTML alternative wraps it in a
    ``multipart/related`` automatically and sets ``Content-Disposition: inline``
    plus the ``Content-ID`` from the ``cid`` argument.
    """
    html_part = _find_html_part(msg)
    if html_part is None:
        return
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
            # Re-set it to carry the filename WHILE keeping the inline
            # disposition (a filename kwarg to add_related would flip it to
            # attachment). The related part is the one whose Content-ID matches.
            related = _find_related_part(html_part, img.content_id)
            if related is not None:
                related.replace_header(
                    "Content-Disposition",
                    "inline",
                )
                related.set_param("filename", img.filename, header="Content-Disposition")


def _find_related_part(html_part: EmailMessage, content_id: str) -> EmailMessage | None:
    """Return the related sub-part whose Content-ID matches ``content_id``."""
    want = f"<{content_id}>"
    for part in html_part.walk():
        if part.get("Content-ID") == want:
            return part  # type: ignore[return-value]
    return None


def _find_html_part(msg: EmailMessage):
    """Return the ``text/html`` leaf of ``msg``, or ``None`` if absent."""
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            return part
    return None
