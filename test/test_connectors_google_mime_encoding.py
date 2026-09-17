"""Encoding round-trips for the Gmail MIME engine: RFC 2047, charset, base64url.

Covers the "Chinese header and body encoding" and "base64url raw round-trip"
lines of the G1 scope. Every assertion is against real serialized bytes, not a
mock — the engine is pure logic, so the test exercises it end to end.
"""

from __future__ import annotations

import base64

import pytest

from kiro_crew.connections.vendors.gmail import (
    Mailbox,
    RecipientSet,
    build_message,
    decode_raw,
    encode_raw,
    parse_message,
)
from kiro_crew.connections.vendors.gmail.errors import MalformedMimeError


def _simple(**kw):
    rs = RecipientSet(to=[Mailbox("to@example.com")])
    base = dict(sender="from@example.com", recipients=rs, subject="s", text_body="b")
    base.update(kw)
    return build_message(**base)


def test_chinese_subject_encoded_word_roundtrips() -> None:
    bm = _simple(subject="中文主题 with ascii")
    # Header block must be 7-bit clean (RFC 2047 encoded-word).
    header_block = bm.rfc822.split(b"\r\n\r\n", 1)[0].split(b"\n\n", 1)[0]
    header_block.decode("ascii")  # raises if any raw non-ASCII leaked
    parsed = parse_message(bm.rfc822)
    assert parsed.subject == "中文主题 with ascii"


def test_chinese_body_charset_roundtrips() -> None:
    bm = _simple(text_body="你好，世界 — grüße")
    parsed = parse_message(bm.rfc822)
    assert parsed.text_body is not None
    assert parsed.text_body.strip() == "你好，世界 — grüße"


def test_non_ascii_display_name_encoded_and_decoded() -> None:
    rs = RecipientSet(to=[Mailbox("a@example.com", "爱丽丝")])
    bm = build_message(sender="from@example.com", recipients=rs, subject="s", text_body="b")
    header_block = bm.rfc822.split(b"\n\n", 1)[0]
    header_block.decode("ascii")  # display name must be encoded-word, not raw
    parsed = parse_message(bm.rfc822)
    assert parsed.to[0].display_name == "爱丽丝"
    assert parsed.to[0].address == "a@example.com"


def test_encode_raw_is_url_safe_and_unpadded() -> None:
    # Bytes chosen so standard base64 would contain '+' and '/'.
    data = bytes(range(256))
    raw = encode_raw(data)
    assert "+" not in raw and "/" not in raw
    assert not raw.endswith("=")
    assert decode_raw(raw) == data


def test_decode_raw_tolerates_missing_padding() -> None:
    data = b"three"  # length forces base64 padding
    std = base64.urlsafe_b64encode(data).decode("ascii")
    assert std.endswith("=")
    unpadded = std.rstrip("=")
    assert decode_raw(unpadded) == data
    assert decode_raw(std) == data


def test_decode_raw_rejects_non_base64url() -> None:
    with pytest.raises(MalformedMimeError):
        decode_raw("not*valid*base64url!!")


def test_built_message_raw_matches_bytes() -> None:
    bm = _simple(subject="中文", text_body="你好")
    assert decode_raw(bm.raw()) == bm.rfc822


def test_encode_raw_type_error_on_str() -> None:
    with pytest.raises(TypeError):
        encode_raw("not bytes")  # type: ignore[arg-type]
