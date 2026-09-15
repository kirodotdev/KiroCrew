"""Hardening tests: no silent data loss or corruption in the MIME engine.

These lock in the four corruption/data-loss defects the review flagged:

* Bcc on a parsed message is surfaced, not silently dropped.
* An attached ``message/rfc822`` is captured whole as an attachment, not
  descended into (which would lose it and could hijack the body).
* A body with bytes invalid for its declared charset raises rather than
  silently returning replacement-character corruption with a checksum over it,
  and an invalid base64 attachment CTE is likewise refused (the defect is only
  visible after decoding the part).
* ``decode_raw`` rejects non-base64url input (non-ASCII, out-of-alphabet)
  instead of silently dropping characters or leaking an uncaught exception.
"""

from __future__ import annotations

import base64

import pytest

from kiro_crew.connections.vendors.gmail import (
    decode_raw,
    parse_message,
)
from kiro_crew.connections.vendors.gmail.errors import MalformedMimeError


def test_bcc_header_on_parse_is_surfaced() -> None:
    # A locally-assembled message that still carries a Bcc header (before the
    # MTA would strip it) must not silently drop the blind recipients.
    raw = (
        b"From: a@example.com\r\n"
        b"To: b@example.com\r\n"
        b"Bcc: hidden@example.com, other@example.com\r\n"
        b"Subject: hi\r\n"
        b"\r\n"
        b"body\r\n"
    )
    parsed = parse_message(raw)
    assert [m.address for m in parsed.bcc] == [
        "hidden@example.com",
        "other@example.com",
    ]


def test_message_rfc822_attachment_captured_not_descended() -> None:
    inner = (
        b"From: inner@example.com\r\n"
        b"To: inner-to@example.com\r\n"
        b"Subject: the inner message\r\n"
        b"\r\n"
        b"INNER BODY SHOULD NOT BECOME THE OUTER BODY\r\n"
    )
    boundary = b"BOUND1"
    outer = (
        b"From: a@example.com\r\n"
        b"To: b@example.com\r\n"
        b"Subject: outer\r\n"
        b'Content-Type: multipart/mixed; boundary="' + boundary + b'"\r\n'
        b"\r\n"
        b"--" + boundary + b"\r\n"
        b"Content-Type: text/plain\r\n\r\n"
        b"REAL OUTER BODY\r\n"
        b"--" + boundary + b"\r\n"
        b"Content-Type: message/rfc822\r\n"
        b'Content-Disposition: attachment; filename="forwarded.eml"\r\n\r\n'
        + inner
        + b"\r\n--"
        + boundary
        + b"--\r\n"
    )
    parsed = parse_message(outer)
    # The outer body is the real text part, not the inner message's body.
    assert parsed.text_body is not None
    assert "REAL OUTER BODY" in parsed.text_body
    assert "INNER BODY" not in parsed.text_body
    # The embedded message is captured as an attachment carrying its own bytes.
    assert len(parsed.attachments) == 1
    att = parsed.attachments[0]
    assert att.content_type == "message/rfc822"
    assert b"INNER BODY SHOULD NOT BECOME THE OUTER BODY" in att.content


def test_body_with_invalid_charset_bytes_raises() -> None:
    # Declares utf-8 but carries a lone 0xFF, invalid as utf-8. base64-encode
    # so the CTE is clean and the defect is purely the decoded bytes.
    bad = b"\xff\xfe invalid utf-8"
    b64 = base64.b64encode(bad)
    raw = (
        b"From: a@example.com\r\n"
        b"To: b@example.com\r\n"
        b"Subject: hi\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"Content-Transfer-Encoding: base64\r\n"
        b"\r\n" + b64 + b"\r\n"
    )
    with pytest.raises(MalformedMimeError):
        parse_message(raw)


def test_valid_utf8_body_still_decodes() -> None:
    raw = (
        b"From: a@example.com\r\n"
        b"To: b@example.com\r\n"
        b"Subject: hi\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"\r\n" + "\u4f60\u597d".encode("utf-8") + b"\r\n"
    )
    parsed = parse_message(raw)
    assert parsed.text_body is not None
    assert parsed.text_body.strip() == "\u4f60\u597d"


def test_decode_raw_rejects_non_ascii() -> None:
    with pytest.raises(MalformedMimeError):
        decode_raw("abcd\u00e9")  # non-ASCII char


def test_decode_raw_rejects_out_of_alphabet_ascii() -> None:
    # '*' and '!' are not in the base64url alphabet; must be rejected, not
    # silently discarded.
    with pytest.raises(MalformedMimeError):
        decode_raw("AAAA****")
    with pytest.raises(MalformedMimeError):
        decode_raw("bad!input")


def test_decode_raw_still_roundtrips_valid_input() -> None:
    from kiro_crew.connections.vendors.gmail import encode_raw

    data = bytes(range(256))
    assert decode_raw(encode_raw(data)) == data


def test_invalid_base64_attachment_cte_is_malformed() -> None:
    # A part declaring base64 CTE but carrying non-base64 bytes is corruption:
    # get_payload(decode=True) silently returns the raw bytes and appends an
    # InvalidBase64*Defect only after decode, so the parser must re-check the
    # part's defects post-decode and refuse rather than checksum garbage.
    raw = (
        b"From: a@example.com\r\n"
        b"To: b@example.com\r\n"
        b"Subject: hi\r\n"
        b'Content-Type: application/octet-stream; name="x.bin"\r\n'
        b'Content-Disposition: attachment; filename="x.bin"\r\n'
        b"Content-Transfer-Encoding: base64\r\n"
        b"\r\n"
        b"!!!!not-base64!!!!\r\n"
    )
    with pytest.raises(MalformedMimeError):
        parse_message(raw)


def test_decode_raw_rejects_standard_base64_chars() -> None:
    # '+' and '/' are the STANDARD base64 alphabet, not URL-safe base64url;
    # decode_raw's contract is base64url only, so they must be rejected rather
    # than silently accepted by the alphabet translation.
    with pytest.raises(MalformedMimeError):
        decode_raw("ab+d")
    with pytest.raises(MalformedMimeError):
        decode_raw("ab/d")


def test_inline_image_filename_preserved_and_stays_inline() -> None:
    from kiro_crew.connections.vendors.gmail import (
        InlineImage,
        Mailbox,
        RecipientSet,
        build_message,
    )

    img = InlineImage("logo1", b"IMGDATA", "image/png", filename="logo.png")
    bm = build_message(
        sender="f@example.com",
        recipients=RecipientSet(to=[Mailbox("t@example.com")]),
        subject="s",
        html_body='<img src="cid:logo1">',
        inline_images=[img],
    )
    # Disposition must remain inline (a filename must not flip it to attachment).
    assert b"inline" in bm.rfc822.lower()
    parsed = parse_message(bm.rfc822)
    assert len(parsed.inline_images) == 1
    got = parsed.inline_images[0]
    assert got.filename == "logo.png"
    assert got.content_id == "logo1"
    assert got.verify(img.content)


def test_message_rfc822_attachment_bytes_are_faithful() -> None:
    # The embedded message must be captured as its OWN bytes (CRLF preserved,
    # no container headers prepended), not the container re-serialized via
    # as_bytes() (which normalizes line endings and prepends the wrapper).
    inner = b"From: inner@example.com\r\nSubject: fwd\r\n\r\nINNER BODY LINE\r\n"
    boundary = b"BB"
    outer = (
        b"From: a@example.com\r\n"
        b"To: b@example.com\r\n"
        b"Subject: outer\r\n"
        b'Content-Type: multipart/mixed; boundary="' + boundary + b'"\r\n\r\n'
        b"--" + boundary + b"\r\n"
        b"Content-Type: text/plain\r\n\r\nOUTER\r\n"
        b"--" + boundary + b"\r\n"
        b"Content-Type: message/rfc822\r\n"
        b'Content-Disposition: attachment; filename="f.eml"\r\n\r\n'
        + inner
        + b"\r\n--"
        + boundary
        + b"--\r\n"
    )
    parsed = parse_message(outer)
    assert len(parsed.attachments) == 1
    att = parsed.attachments[0]
    # No container header leaked into the attachment bytes.
    assert b"Content-Type: message/rfc822" not in att.content
    # The forwarded message's own header/body survives with CRLF preserved.
    assert att.content.startswith(b"From: inner@example.com")
    assert b"INNER BODY LINE" in att.content
    assert b"\r\n" in att.content
    # Checksum is over exactly those preserved bytes.
    assert att.verify(att.content)
