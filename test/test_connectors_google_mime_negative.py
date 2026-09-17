"""Parser negative-path tests: malformed MIME and missing required headers.

Covers the "malformed MIME" and "missing required header" hard negative cases
of the G1 scope, and the deliberate distinction between the two: bad bytes
raise MalformedMimeError; valid MIME lacking a caller-required header raises
MissingHeaderError only when require_headers asks for it.
"""

from __future__ import annotations

import pytest

from kiro_crew.connections.vendors.gmail import (
    parse_message,
    require_headers,
)
from kiro_crew.connections.vendors.gmail.errors import (
    MalformedMimeError,
    MissingHeaderError,
)


def test_empty_bytes_is_malformed() -> None:
    with pytest.raises(MalformedMimeError):
        parse_message(b"")
    with pytest.raises(MalformedMimeError):
        parse_message(b"   \r\n  ")


def test_non_bytes_input_type_error() -> None:
    with pytest.raises(TypeError):
        parse_message("a string")  # type: ignore[arg-type]


def test_multipart_declared_without_parts_is_malformed() -> None:
    # A message that declares multipart but carries no boundary/parts.
    raw = (
        b"From: a@example.com\r\n"
        b"To: b@example.com\r\n"
        b'Content-Type: multipart/mixed; boundary="XYZ"\r\n'
        b"\r\n"
        b"there is no boundary delimiter anywhere in this body\r\n"
    )
    with pytest.raises(MalformedMimeError):
        parse_message(raw)


def test_well_formed_plain_message_parses() -> None:
    raw = (
        b"From: a@example.com\r\n"
        b"To: b@example.com\r\n"
        b"Subject: hi\r\n"
        b"\r\n"
        b"body text\r\n"
    )
    parsed = parse_message(raw)
    assert parsed.from_ == "a@example.com"
    assert parsed.subject == "hi"
    assert parsed.text_body is not None
    assert parsed.text_body.strip() == "body text"


def test_require_headers_passes_when_present() -> None:
    raw = b"From: a@example.com\r\n" b"To: b@example.com\r\n" b"Subject: hi\r\n" b"\r\n" b"body\r\n"
    parsed = parse_message(raw)
    # Should not raise.
    require_headers(parsed, ["From", "Subject", "To"])


def test_require_headers_raises_on_missing_from() -> None:
    raw = b"To: b@example.com\r\nSubject: hi\r\n\r\nbody\r\n"
    parsed = parse_message(raw)
    with pytest.raises(MissingHeaderError) as exc:
        require_headers(parsed, ["From"])
    assert "From" in str(exc.value)


def test_require_headers_raises_on_missing_recipient() -> None:
    raw = b"From: a@example.com\r\nSubject: hi\r\n\r\nbody\r\n"
    parsed = parse_message(raw)
    with pytest.raises(MissingHeaderError) as exc:
        require_headers(parsed, ["To"])
    assert "To" in str(exc.value)


def test_valid_mime_missing_header_is_not_malformed() -> None:
    # Parsing succeeds; only require_headers surfaces the missing-header fact.
    raw = b"From: a@example.com\r\n\r\nbody\r\n"
    parsed = parse_message(raw)  # no raise
    assert parsed.from_ == "a@example.com"
    with pytest.raises(MissingHeaderError):
        require_headers(parsed, ["Subject"])
