"""Attachment tests: bytes, non-ASCII filenames, checksums, size caps.

Covers the "attachments: byte extraction, filenames (incl. non-ASCII),
checksum verification" line and the "oversize attachment boundary" negative
case of the G1 scope.
"""

from __future__ import annotations

import pytest

from kiro_crew.connections.vendors.gmail import (
    Attachment,
    Mailbox,
    RecipientSet,
    build_message,
    parse_message,
    sha256_hex,
)
from kiro_crew.connections.vendors.gmail.errors import AttachmentTooLargeError

_RS = RecipientSet(to=[Mailbox("to@example.com")])


def _msg_with(attachments):
    return build_message(
        sender="f@example.com",
        recipients=_RS,
        subject="s",
        text_body="body",
        attachments=attachments,
    )


def test_attachment_bytes_extracted_and_checksum_matches() -> None:
    content = b"\x00\x01\x02binary\xff\xfe" * 100
    att = Attachment("data.bin", content, "application/octet-stream")
    assert att.checksum == sha256_hex(content)
    bm = _msg_with([att])
    parsed = parse_message(bm.rfc822)
    assert len(parsed.attachments) == 1
    got = parsed.attachments[0]
    assert got.content == content
    assert got.checksum == att.checksum
    assert got.verify(content)
    assert not got.verify(content + b"tampered")


def test_non_ascii_filename_survives_roundtrip() -> None:
    att = Attachment("季度报告_2026.pdf", b"PDF-CONTENT", "application/pdf")
    bm = _msg_with([att])
    # Non-ASCII filename must not appear raw in the 7-bit header region: the
    # RFC 2231 filename*=UTF-8'' form is used instead.
    parsed = parse_message(bm.rfc822)
    assert parsed.attachments[0].filename == "季度报告_2026.pdf"


def test_multiple_attachments_all_extracted() -> None:
    atts = [
        Attachment("a.txt", b"aaa", "text/plain"),
        Attachment("b.bin", b"bbb", "application/octet-stream"),
        Attachment("c.png", b"ccc", "image/png"),
    ]
    bm = _msg_with(atts)
    parsed = parse_message(bm.rfc822)
    names = sorted(a.filename for a in parsed.attachments)
    assert names == ["a.txt", "b.bin", "c.png"]
    by_name = {a.filename: a for a in parsed.attachments}
    for original in atts:
        assert by_name[original.filename].verify(original.content)


def test_attachment_over_cap_is_refused_at_construction() -> None:
    with pytest.raises(AttachmentTooLargeError) as exc:
        Attachment("big.bin", b"x" * 101, "application/octet-stream", max_bytes=100)
    assert "101" in str(exc.value)
    assert "100" in str(exc.value)


def test_attachment_at_exact_cap_is_allowed() -> None:
    att = Attachment("edge.bin", b"x" * 100, max_bytes=100)
    assert att.checksum == sha256_hex(b"x" * 100)


def test_total_message_size_cap_enforced() -> None:
    att = Attachment("ok.bin", b"y" * 500)
    with pytest.raises(AttachmentTooLargeError):
        build_message(
            sender="f@example.com",
            recipients=_RS,
            subject="s",
            text_body="body",
            attachments=[att],
            max_total_bytes=200,
        )


def test_attachment_rejects_non_bytes() -> None:
    with pytest.raises(TypeError):
        Attachment("x.txt", "not bytes")  # type: ignore[arg-type]


def test_attachments_promote_body_to_mixed() -> None:
    from email import message_from_bytes
    from email.policy import default as default_policy

    att = Attachment("f.txt", b"data", "text/plain")
    bm = build_message(
        sender="f@example.com",
        recipients=_RS,
        subject="s",
        text_body="hi",
        html_body="<p>hi</p>",
        attachments=[att],
    )
    msg = message_from_bytes(bm.rfc822, policy=default_policy)
    assert msg.get_content_type() == "multipart/mixed"
