"""Multipart structure and inline-image tests for the Gmail MIME engine.

Covers the "HTML + plain multipart/alternative" and "inline images: cid:,
Content-ID, Content-Disposition: inline, multipart/related nesting" lines of
the G1 scope. Structure is asserted against the parsed MIME tree, not the raw
string, so the checks are about the real content types the engine produced.
"""

from __future__ import annotations

from email import message_from_bytes
from email.policy import default as default_policy

import pytest

from kiro_crew.connections.vendors.gmail import (
    InlineImage,
    Mailbox,
    RecipientSet,
    build_message,
    parse_message,
)
from kiro_crew.connections.vendors.gmail.errors import MissingHeaderError

_RS = RecipientSet(to=[Mailbox("to@example.com")])


def _content_types(raw: bytes) -> list[str]:
    msg = message_from_bytes(raw, policy=default_policy)
    return [p.get_content_type() for p in msg.walk()]


def test_text_only_is_single_leaf() -> None:
    bm = build_message(sender="f@example.com", recipients=_RS, subject="s", text_body="hi")
    types = _content_types(bm.rfc822)
    assert types == ["text/plain"]


def test_html_only_is_single_leaf() -> None:
    bm = build_message(sender="f@example.com", recipients=_RS, subject="s", html_body="<p>hi</p>")
    types = _content_types(bm.rfc822)
    assert types == ["text/html"]


def test_text_and_html_is_multipart_alternative() -> None:
    bm = build_message(
        sender="f@example.com",
        recipients=_RS,
        subject="s",
        text_body="hi",
        html_body="<p>hi</p>",
    )
    types = _content_types(bm.rfc822)
    assert types[0] == "multipart/alternative"
    assert "text/plain" in types
    assert "text/html" in types


def test_inline_image_produces_multipart_related_with_content_id() -> None:
    img = InlineImage("logo1", b"\x89PNG-bytes", "image/png")
    bm = build_message(
        sender="f@example.com",
        recipients=_RS,
        subject="s",
        html_body='<img src="cid:logo1">',
        inline_images=[img],
    )
    msg = message_from_bytes(bm.rfc822, policy=default_policy)
    types = [p.get_content_type() for p in msg.walk()]
    assert "multipart/related" in types
    assert "image/png" in types
    # The image part carries a Content-ID matching the cid reference and is
    # dispositioned inline.
    img_parts = [p for p in msg.walk() if p.get_content_type() == "image/png"]
    assert len(img_parts) == 1
    cid = img_parts[0].get("Content-ID")
    assert cid == "<logo1>"
    assert (img_parts[0].get_content_disposition() or "").lower() == "inline"


def test_text_html_and_inline_image_wrap_related_around_html_only() -> None:
    img = InlineImage("logo2", b"IMG", "image/gif")
    bm = build_message(
        sender="f@example.com",
        recipients=_RS,
        subject="s",
        text_body="plain fallback",
        html_body='<img src="cid:logo2">',
        inline_images=[img],
    )
    msg = message_from_bytes(bm.rfc822, policy=default_policy)
    # Top level is the alternative; the related wraps only the HTML branch, so
    # the plain-text alternative (no cid: refs) stays a sibling of the related,
    # not a child of it.
    assert msg.get_content_type() == "multipart/alternative"
    top_children = [p.get_content_type() for p in msg.iter_parts()]
    assert "text/plain" in top_children
    assert "multipart/related" in top_children
    related = next(p for p in msg.iter_parts() if p.get_content_type() == "multipart/related")
    related_children = [p.get_content_type() for p in related.iter_parts()]
    assert "text/html" in related_children
    assert "image/gif" in related_children


def test_inline_image_roundtrips_with_matching_cid() -> None:
    img = InlineImage("pic99", b"binary-image-data", "image/jpeg")
    bm = build_message(
        sender="f@example.com",
        recipients=_RS,
        subject="s",
        html_body='<img src="cid:pic99">',
        inline_images=[img],
    )
    parsed = parse_message(bm.rfc822)
    assert len(parsed.inline_images) == 1
    got = parsed.inline_images[0]
    assert got.content_id == "pic99"
    assert got.cid_reference() == "cid:pic99"
    assert got.verify(img.content)


def test_inline_image_without_html_body_is_refused() -> None:
    img = InlineImage("x", b"data")
    with pytest.raises(MissingHeaderError):
        build_message(
            sender="f@example.com",
            recipients=_RS,
            subject="s",
            text_body="only text",
            inline_images=[img],
        )
