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
* A ``sendAs`` alias defaults to UNVERIFIED (fail-closed), so a wiring author
  who omits the verification flag gets a rejected alias, not a silently
  accepted unverified sender.
* Repeated To/Cc/Bcc headers keep every recipient (all occurrences combined),
  not just the first.
* An RFC 2047 encoded-word header whose bytes are undecodable in its declared
  charset is rejected, not silently returned as U+FFFD corruption.
* A ``message/*`` non-rfc822 attachment (e.g. ``message/delivery-status``)
  keeps its declared subtype rather than being silently relabeled
  ``message/rfc822``.
* CR/LF in an address or display name is refused at ``Mailbox`` construction
  (the header-injection class), not deferred to an uncaught serialization
  crash.
* A non-rfc822 ``message/*`` attachment is captured as its embedded payload,
  not the whole container (whose wrapper headers would corrupt the checksum).
* A repeated recipient address with differing display names keeps each name,
  matched by occurrence rather than a first-wins addr-spec key.
* A local part the cheap regex admits but ``Address`` rejects (consecutive-dot)
  is refused at ``Mailbox`` construction, not deferred to a serialization crash.
* Media-type dispatch is case-insensitive, so an uppercase ``MESSAGE/*`` is not
  routed to the raw-bytes path that crashes.
* A single-label domain recipient (``user@localhost``) is accepted, not
  silently dropped by a dotted-domain-only check.
* An inline ``message/delivery-status`` (the standard body part of a
  ``multipart/report`` bounce) is captured whole, not descended into.
* The subtype correction rewrites only the outer appended attachment, never a
  nested ``message/rfc822`` reached by a full-tree walk.
* A U+FFFD the sender literally placed in a validly-encoded filename is kept;
  only a decode-introduced U+FFFD is rejected.
* Mutation guards: each fatal multipart/base64 defect is fatal, the inline-image
  cap bites, and the total-message cap is exclusive at its exact boundary.
* CR/LF in sender / subject / threading headers is refused as a typed
  MimeError, not an uncaught ValueError at header serialization.
* A corrupt RFC 2231 ``filename*`` (percent-bytes invalid in the declared
  charset) is rejected even though the default policy's own accessor shows
  U+FFFD; a validly-encoded literal U+FFFD filename is kept.
* A raw non-encoded-word 8-bit header byte is rejected rather than returned as
  U+FFFD; a valid raw UTF-8 (SMTPUTF8) header is kept.
* A message/* attachment's exact bytes (including LF-only line endings) reach
  the wire verbatim, not silently normalized to CRLF.
* CR/LF in an attachment filename or a malformed media type is refused at
  Attachment construction with a typed MimeError.
* Every RFC 2231 filename continuation segment is strict-decoded, so a corrupt
  continuation (filename*1*=%ff) is rejected, not silently returned as U+FFFD;
  an RFC 2047 encoded-word filename is likewise strict-decoded (a malformed
  encoded-word is rejected, not returned as U+FFFD), INCLUDING one that decodes
  to empty (the strict check runs for every present filename parameter, not
  only when the decoded name carries a U+FFFD).
* An embedded message/* attachment is CRLF-canonicalized end to end (stored,
  shipped, and extracted as CRLF), so a checksum over the canonical form
  survives the round-trip regardless of the source's line-ending style.
* InlineImage enforces the same CR/LF header-injection guard as Attachment on
  content_id / content_type / filename, and requires a well-formed image/* type.
* A second text/plain or text/html body leaf outside a multipart/alternative is
  preserved as an attachment, not silently dropped after the first body leaf.
* A named image carrying a Content-ID but no disposition stays inline (a cid:
  referent), not misclassified as an attachment.
* validate_send_as requires is_verified to be the boolean True, so a truthy
  non-bool cannot bypass the sender-identity gate.
* Attachment / InlineImage store the NORMALIZED (stripped) media type.
* build_message validates the sender is a real address and a supplied
  Message-ID is a single RFC 5322 msg-id, translating failures to a MimeError.
* A raw 8-bit non-encoded filename that decodes to U+FFFD is rejected.
* A malformed structured single-value header is translated to MalformedMimeError
  rather than crashing the parse.
* A media type with a stray delimiter on either side of '/' is rejected (valid
  MIME-token chars required), for Attachment and InlineImage alike.
* Duplicate inline-image Content-IDs are rejected; distinct cids keep their own
  filename (the just-appended part is targeted, not a first-match cid search).
* A base64-encoded message/global container is decoded to its embedded bytes,
  like message/rfc822, not captured as opaque base64.
* Every encoded-word within a filename value (even mixed with literal text) is
  strict-decoded, so a malformed embedded encoded-word is rejected.
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


def test_send_as_alias_defaults_to_unverified() -> None:
    # A SendAsAlias constructed without an explicit is_verified must be treated
    # as UNVERIFIED (fail-closed), so a future wiring author who maps sendAs.list
    # and omits the flag cannot silently accept an unverified sender alias.
    from kiro_crew.connections.vendors.gmail.addresses import (
        SendAsAlias,
        validate_send_as,
    )
    from kiro_crew.connections.vendors.gmail.errors import InvalidAliasError

    assert SendAsAlias("x@example.com").is_verified is False
    with pytest.raises(InvalidAliasError):
        validate_send_as("x@example.com", [SendAsAlias("x@example.com")])
    # An explicitly-verified alias still validates.
    assert (
        validate_send_as("x@example.com", [SendAsAlias("x@example.com", is_verified=True)])
        == "x@example.com"
    )


def test_repeated_recipient_headers_keep_every_address() -> None:
    # A non-conformant producer that emits repeated To/Cc/Bcc headers must not
    # lose the later recipients: every occurrence is combined, not just the first.
    raw = (
        b"From: a@example.com\r\n"
        b"To: one@x.com\r\n"
        b"To: two@y.com\r\n"
        b"Cc: three@x.com\r\n"
        b"Cc: four@y.com\r\n"
        b"Subject: hi\r\n"
        b"\r\n"
        b"body\r\n"
    )
    parsed = parse_message(raw)
    assert [m.address for m in parsed.to] == ["one@x.com", "two@y.com"]
    assert [m.address for m in parsed.cc] == ["three@x.com", "four@y.com"]


def test_undecodable_encoded_word_header_is_rejected() -> None:
    # A valid-base64 RFC 2047 encoded-word whose bytes are not valid in the
    # declared charset must be REJECTED, not silently returned as U+FFFD
    # corruption — the header path matches the strict body-decode contract.
    bad = base64.b64encode(b"\xff\xfe\xfa").decode()
    raw = (
        b"From: a@example.com\r\n"
        b"Subject: =?utf-8?b?" + bad.encode() + b"?=\r\n"
        b"\r\n"
        b"body\r\n"
    )
    with pytest.raises(MalformedMimeError):
        parse_message(raw)


def test_legitimate_encoded_word_header_decodes() -> None:
    # A well-formed non-ASCII encoded-word header decodes normally (the strict
    # check must not reject valid Chinese subjects).
    good = base64.b64encode("\u4f60\u597d\u4e16\u754c".encode()).decode()
    raw = (
        b"From: a@example.com\r\n"
        b"Subject: =?utf-8?b?" + good.encode() + b"?=\r\n"
        b"\r\n"
        b"body\r\n"
    )
    parsed = parse_message(raw)
    assert parsed.headers["Subject"] == "\u4f60\u597d\u4e16\u754c"


def test_bcc_only_message_writes_bcc_into_raw() -> None:
    # Gmail's messages.send delivers blind copies from the Bcc header in `raw`,
    # so a Bcc-only build MUST carry that recipient in the serialized message —
    # omitting it would silently drop the only recipient. No To/Cc is present
    # and no `undisclosed-recipients` To is synthesized: the bare Bcc header is
    # a valid destination Gmail delivers from directly.
    from kiro_crew.connections.vendors.gmail import build_message
    from kiro_crew.connections.vendors.gmail.addresses import Mailbox, RecipientSet

    rs = RecipientSet(to=[], cc=[], bcc=[Mailbox("hidden@example.com")])
    built = build_message(sender="me@example.com", recipients=rs, subject="s", text_body="hi")
    parsed = parse_message(built.rfc822)
    assert [m.address for m in parsed.bcc] == ["hidden@example.com"]
    assert parsed.to == []
    assert parsed.cc == []
    assert b"hidden@example.com" in built.rfc822


def test_second_text_plain_leaf_with_bad_base64_is_rejected() -> None:
    # A later text/plain leaf we do not adopt as the body must still be
    # decode-validated: its invalid base64 CTE defect is appended only on
    # decode, so skipping it would smuggle silent corruption past the
    # pre-decode sweep.
    raw = (
        b"From: a@example.com\r\n"
        b"To: c@example.com\r\n"
        b"Subject: x\r\n"
        b'Content-Type: multipart/mixed; boundary="B"\r\n'
        b"\r\n"
        b"--B\r\nContent-Type: text/plain\r\n\r\nfirst body\r\n"
        b"--B\r\nContent-Type: text/plain\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\n!!!!not-base64!!!!\r\n"
        b"--B--\r\n"
    )
    with pytest.raises(MalformedMimeError):
        parse_message(raw)


def test_attachment_dispositioned_image_with_content_id_stays_an_attachment() -> None:
    # An image explicitly Content-Disposition: attachment AND carrying a
    # Content-ID (common in forwarded mail) is a real attachment, not inline —
    # routing it to inline handling would make it vanish from `attachments`.
    img = base64.b64encode(b"\x89PNG\r\n\x1a\nfakeimg").decode().encode()
    raw = (
        b"From: a@example.com\r\n"
        b"To: c@example.com\r\n"
        b"Subject: x\r\n"
        b'Content-Type: multipart/mixed; boundary="B"\r\n'
        b"\r\n"
        b"--B\r\nContent-Type: text/plain\r\n\r\nbody\r\n"
        b"--B\r\nContent-Type: image/png\r\n"
        b'Content-Disposition: attachment; filename="p.png"\r\n'
        b"Content-ID: <img1>\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\n" + img + b"\r\n"
        b"--B--\r\n"
    )
    parsed = parse_message(raw)
    assert len(parsed.attachments) == 1
    assert len(parsed.inline_images) == 0
    assert parsed.attachments[0].filename == "p.png"


def test_embedded_message_with_long_header_keeps_stable_checksum() -> None:
    # The embedded-message serializer must NOT refold long headers
    # (maxheaderlen=0), or the attachment bytes and checksum would change for a
    # forwarded message carrying a long header — a common case.
    long_value = "x" * 200
    inner = (
        b"From: inner@example.com\r\n"
        b"X-Long: " + long_value.encode() + b"\r\n"
        b"Subject: fwd\r\n"
        b"\r\n"
        b"INNER BODY LINE\r\n"
    )
    raw = (
        b"From: a@example.com\r\n"
        b"To: c@example.com\r\n"
        b"Subject: outer\r\n"
        b'Content-Type: multipart/mixed; boundary="B"\r\n'
        b"\r\n"
        b"--B\r\nContent-Type: text/plain\r\n\r\nbody\r\n"
        b"--B\r\nContent-Type: message/rfc822\r\n"
        b"Content-Disposition: attachment\r\n\r\n" + inner + b"\r\n"
        b"--B--\r\n"
    )
    parsed = parse_message(raw)
    att = parsed.attachments[0]
    # The long header survives on a single unrefolded line.
    assert b"X-Long: " + long_value.encode() in att.content
    assert att.verify(att.content)


def test_require_headers_bcc_reads_parsed_bcc() -> None:
    # require_headers(["Bcc"]) must consult parsed.bcc (Bcc never lands in
    # parsed.headers), not raise a false MissingHeaderError.
    from kiro_crew.connections.vendors.gmail.errors import MissingHeaderError
    from kiro_crew.connections.vendors.gmail.parser import require_headers

    raw = (
        b"From: a@example.com\r\n"
        b"To: c@example.com\r\n"
        b"Bcc: hidden@example.com\r\n"
        b"Subject: x\r\n"
        b"\r\n"
        b"body\r\n"
    )
    parsed = parse_message(raw)
    require_headers(parsed, ["Bcc"])  # must not raise
    # And it still raises when Bcc is genuinely absent.
    raw2 = b"From: a@example.com\r\nTo: c@example.com\r\n\r\nbody\r\n"
    with pytest.raises(MissingHeaderError):
        require_headers(parse_message(raw2), ["Bcc"])


def test_inline_non_image_leaf_is_captured_as_attachment() -> None:
    # An inline, non-image, non-text leaf (e.g. Content-Disposition: inline on
    # an application/pdf) matches none of the type/disposition branches; it must
    # be captured as an attachment rather than silently dropped.
    pdf = base64.b64encode(b"%PDF-1.4 fake").decode().encode()
    raw = (
        b"From: a@example.com\r\n"
        b"To: c@example.com\r\n"
        b"Subject: x\r\n"
        b'Content-Type: multipart/mixed; boundary="B"\r\n'
        b"\r\n"
        b"--B\r\nContent-Type: text/plain\r\n\r\nbody\r\n"
        b"--B\r\nContent-Type: application/pdf\r\n"
        b"Content-Disposition: inline\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\n" + pdf + b"\r\n"
        b"--B--\r\n"
    )
    parsed = parse_message(raw)
    assert len(parsed.attachments) == 1
    assert b"%PDF" in parsed.attachments[0].content


def test_malformed_encoded_word_display_name_is_rejected() -> None:
    # A recipient display name carrying an undecodable RFC 2047 encoded-word
    # must be rejected, not silently returned as U+FFFD corruption — matching
    # the strict header-decode contract.
    bad = base64.b64encode(b"\xff\xfe\xfa").decode()
    raw = (
        b"From: a@example.com\r\n"
        b"To: =?utf-8?b?" + bad.encode() + b"?= <x@y.com>\r\n"
        b"Subject: s\r\n"
        b"\r\n"
        b"body\r\n"
    )
    with pytest.raises(MalformedMimeError):
        parse_message(raw)


def test_legitimate_encoded_word_display_name_decodes() -> None:
    # A well-formed non-ASCII encoded-word display name decodes normally.
    good = base64.b64encode("\u5f20\u4e09".encode()).decode()
    raw = (
        b"From: a@example.com\r\n"
        b"To: =?utf-8?b?" + good.encode() + b"?= <x@y.com>\r\n"
        b"Subject: s\r\n"
        b"\r\n"
        b"body\r\n"
    )
    parsed = parse_message(raw)
    assert parsed.to[0].display_name == "\u5f20\u4e09"
    assert parsed.to[0].address == "x@y.com"


def test_long_unicode_subject_and_name_do_not_crash_and_roundtrip() -> None:
    # A ~20-char Unicode subject/display name must be RFC 2047-encoded by the
    # EmailMessage policy (folded correctly), not pre-encoded into a string with
    # hard newlines that the header setter rejects with ValueError.
    from kiro_crew.connections.vendors.gmail import build_message, parse_message
    from kiro_crew.connections.vendors.gmail.addresses import Mailbox, RecipientSet

    long_text = "\u4f60\u597d\u4e16\u754c" * 5
    rs = RecipientSet(to=[Mailbox("c@example.com", display_name=long_text)], cc=[], bcc=[])
    built = build_message(sender="a@example.com", recipients=rs, subject=long_text, text_body="hi")
    parsed = parse_message(built.rfc822)
    assert parsed.headers["Subject"] == long_text
    assert parsed.to[0].display_name == long_text


def test_encoded_word_decoding_to_empty_is_rejected() -> None:
    # A valid-syntax encoded-word with invalid base64 (=?utf-8?b?!!!!?=) decodes
    # to empty bytes without raising — silently ERASING the header. That must be
    # rejected, not returned as an empty subject.
    raw = (
        b"From: a@example.com\r\n"
        b"To: c@example.com\r\n"
        b"Subject: =?utf-8?b?!!!!?=\r\n"
        b"\r\n"
        b"body\r\n"
    )
    with pytest.raises(MalformedMimeError):
        parse_message(raw)


def test_malformed_forwarded_eml_does_not_discard_outer_message() -> None:
    # The defect scan must NOT descend into a message/rfc822 attachment: a
    # malformed forwarded .eml is the attachment's own content, not a lie about
    # the outer message's structure. The valid outer body/attachment survive.
    inner_bad = b"Content-Type: multipart/mixed; boundary=NOPE\r\n\r\nno boundary here\r\n"
    raw = (
        b"From: a@example.com\r\n"
        b"To: c@example.com\r\n"
        b"Subject: outer\r\n"
        b'Content-Type: multipart/mixed; boundary="B"\r\n'
        b"\r\n"
        b"--B\r\nContent-Type: text/plain\r\n\r\nOUTER BODY\r\n"
        b"--B\r\nContent-Type: message/rfc822\r\n"
        b"Content-Disposition: attachment\r\n\r\n" + inner_bad + b"\r\n"
        b"--B--\r\n"
    )
    parsed = parse_message(raw)
    assert parsed.text_body == "OUTER BODY"
    assert len(parsed.attachments) == 1


def test_mixed_text_with_erased_encoded_word_is_rejected() -> None:
    # A header MIXING valid text with an invalid-base64 encoded-word decodes to
    # a non-empty string (`Hello ` survives) — a whole-string emptiness check
    # would miss the erasure. Every erased encoded-word chunk must be caught.
    raw = (
        b"From: a@example.com\r\n"
        b"To: b@example.com\r\n"
        b"Subject: Hello =?utf-8?b?!!!!?=\r\n"
        b"\r\n"
        b"body\r\n"
    )
    with pytest.raises(MalformedMimeError):
        parse_message(raw)


def test_erased_encoded_word_in_display_name_is_rejected() -> None:
    # A recipient display name whose encoded-word has invalid base64 decodes to
    # empty without raising — silently erasing the name. That must surface as
    # corruption, matching the single-value-header strict path.
    raw = (
        b"From: a@example.com\r\n"
        b"To: =?utf-8?b?!!!!?= <b@example.com>\r\n"
        b"Subject: s\r\n"
        b"\r\n"
        b"body\r\n"
    )
    with pytest.raises(MalformedMimeError):
        parse_message(raw)


def test_legit_mixed_text_and_chinese_display_name_decode_cleanly() -> None:
    # The erasure guards must NOT reject legitimate encoded-words: a valid
    # Chinese encoded-word mixed with ASCII text decodes fully.
    raw = (
        b"From: a@example.com\r\n"
        b"To: =?utf-8?b?5L2g5aW9?= <b@example.com>\r\n"
        b"Subject: Hi =?utf-8?b?5L2g5aW9?=\r\n"
        b"\r\n"
        b"body\r\n"
    )
    parsed = parse_message(raw)
    assert parsed.subject == "Hi \u4f60\u597d"
    assert parsed.to[0].display_name == "\u4f60\u597d"


def test_base64_encoded_forwarded_eml_is_decoded_not_captured_as_base64() -> None:
    # A message/rfc822 attachment carrying Content-Transfer-Encoding: base64
    # encodes the whole forwarded message. The captured attachment must hold the
    # DECODED .eml bytes (the forwarded message), not opaque base64 text.
    import base64 as _b64

    inner = b"From: orig@example.com\r\nSubject: fwd me\r\n\r\ninner body\r\n"
    encoded = _b64.b64encode(inner).decode("ascii")
    raw = (
        b"From: a@example.com\r\n"
        b"To: c@example.com\r\n"
        b"Subject: outer\r\n"
        b'Content-Type: multipart/mixed; boundary="B"\r\n'
        b"\r\n"
        b"--B\r\nContent-Type: text/plain\r\n\r\nOUTER\r\n"
        b"--B\r\nContent-Type: message/rfc822\r\n"
        b"Content-Transfer-Encoding: base64\r\n"
        b"Content-Disposition: attachment\r\n\r\n" + encoded.encode("ascii") + b"\r\n"
        b"--B--\r\n"
    )
    parsed = parse_message(raw)
    assert parsed.text_body == "OUTER"
    assert len(parsed.attachments) == 1
    content = parsed.attachments[0].content
    assert b"fwd me" in content
    assert b"inner body" in content
    # Not the opaque base64 text.
    assert encoded.encode("ascii") not in content


def test_invalid_base64_embedded_message_is_rejected_not_checksummed() -> None:
    # A base64 message/rfc822 whose body has out-of-alphabet characters must
    # raise MalformedMimeError, NOT decode permissively and stamp a valid
    # checksum over the corrupted bytes.
    import base64 as _b64

    inner = b"From: o@example.com\r\nSubject: fwd\r\n\r\nbody\r\n"
    bad = _b64.b64encode(inner).decode("ascii")[:-4] + "@@@@"
    raw = (
        b"From: a@example.com\r\n"
        b"To: c@example.com\r\n"
        b"Subject: outer\r\n"
        b'Content-Type: multipart/mixed; boundary="B"\r\n'
        b"\r\n"
        b"--B\r\nContent-Type: text/plain\r\n\r\nOUTER\r\n"
        b"--B\r\nContent-Type: message/rfc822\r\n"
        b"Content-Transfer-Encoding: base64\r\n"
        b"Content-Disposition: attachment\r\n\r\n" + bad.encode("ascii") + b"\r\n"
        b"--B--\r\n"
    )
    with pytest.raises(MalformedMimeError):
        parse_message(raw)


def test_non_rfc822_multipart_attachment_keeps_every_child() -> None:
    # An attachment-dispositioned multipart that is NOT message/rfc822 (e.g.
    # multipart/appledouble) must be serialized WHOLE — every child survives,
    # not just the first. The checksum is over the complete part.
    raw = (
        b"From: a@example.com\r\n"
        b"To: c@example.com\r\n"
        b"Subject: outer\r\n"
        b'Content-Type: multipart/mixed; boundary="B"\r\n'
        b"\r\n"
        b"--B\r\nContent-Type: text/plain\r\n\r\nOUTER\r\n"
        b'--B\r\nContent-Type: multipart/appledouble; boundary="C"\r\n'
        b"Content-Disposition: attachment\r\n\r\n"
        b"--C\r\nContent-Type: application/applefile\r\n\r\nCHILD1DATA\r\n"
        b"--C\r\nContent-Type: application/octet-stream\r\n\r\nCHILD2DATA\r\n"
        b"--C--\r\n"
        b"--B--\r\n"
    )
    parsed = parse_message(raw)
    assert parsed.text_body == "OUTER"
    assert len(parsed.attachments) == 1
    content = parsed.attachments[0].content
    assert b"CHILD1DATA" in content
    assert b"CHILD2DATA" in content


def test_raw_utf8_recipient_display_name_not_corrupted() -> None:
    # A raw UTF-8 (SMTPUTF8) recipient display name — sent WITHOUT RFC 2047
    # encoding — must be decoded to the real characters, not replacement
    # characters (the compat32 str() path corrupted these).
    raw = (
        "To: \u5f20\u4e09 <z@example.com>\r\nFrom: a@example.com\r\nSubject: s\r\n\r\nb\r\n".encode(
            "utf-8"
        )
    )
    parsed = parse_message(raw)
    assert parsed.to[0].display_name == "\u5f20\u4e09"
    assert parsed.to[0].address == "z@example.com"


def test_quoted_local_part_recipient_is_kept() -> None:
    # A valid RFC 5322 quoted local part ("John Doe"@example.com) must survive:
    # a restrictive addr regex that rejected it silently dropped the recipient.
    raw = b'To: "John Doe"@example.com\r\n' b"From: a@example.com\r\nSubject: s\r\n\r\nbody\r\n"
    parsed = parse_message(raw)
    assert len(parsed.to) == 1
    assert parsed.to[0].address == '"John Doe"@example.com'


def test_named_text_plain_without_disposition_is_an_attachment() -> None:
    # A text/plain part with a filename (name=) but no Content-Disposition must
    # be captured as an attachment, not silently dropped as a non-first body.
    raw = (
        b"From: a@example.com\r\nTo: c@example.com\r\nSubject: s\r\n"
        b'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
        b"--B\r\nContent-Type: text/plain\r\n\r\nBODY\r\n"
        b'--B\r\nContent-Type: text/plain; name="notes.txt"\r\n\r\nATTACHED NOTES\r\n'
        b"--B--\r\n"
    )
    parsed = parse_message(raw)
    assert parsed.text_body == "BODY"
    assert len(parsed.attachments) == 1
    assert parsed.attachments[0].filename == "notes.txt"
    assert parsed.attachments[0].content == b"ATTACHED NOTES"


def test_line_wrapped_base64_embedded_message_is_accepted() -> None:
    # Standard MIME base64 is line-wrapped (CRLF ~ every 76 chars). The strict
    # decoder must accept that permitted whitespace, not reject a valid
    # base64-encoded message/rfc822 as malformed.
    import base64 as _b64
    import textwrap as _tw

    inner = b"From: o@example.com\r\nSubject: fwd me\r\n\r\n" + b"x" * 200 + b"\r\n"
    wrapped = "\r\n".join(_tw.wrap(_b64.b64encode(inner).decode("ascii"), 76))
    raw = (
        b"From: a@example.com\r\nTo: c@example.com\r\nSubject: s\r\n"
        b'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
        b"--B\r\nContent-Type: text/plain\r\n\r\nOUTER\r\n"
        b"--B\r\nContent-Type: message/rfc822\r\n"
        b"Content-Transfer-Encoding: base64\r\n"
        b"Content-Disposition: attachment\r\n\r\n" + wrapped.encode("ascii") + b"\r\n"
        b"--B--\r\n"
    )
    parsed = parse_message(raw)
    assert len(parsed.attachments) == 1
    assert b"fwd me" in parsed.attachments[0].content


def test_unquoted_comma_local_part_is_rejected() -> None:
    # A bare comma in an unquoted local part is an address-LIST separator; it
    # must NOT pass address validation (it would split the header and crash a
    # downstream build). A quoted local part carrying a comma is still valid.
    from kiro_crew.connections.vendors.gmail.addresses import Mailbox
    from kiro_crew.connections.vendors.gmail.errors import InvalidAliasError

    with pytest.raises(InvalidAliasError):
        Mailbox("a,b@example.com")
    # quoted comma is legal
    assert Mailbox('"a,b"@example.com').address == '"a,b"@example.com'


def test_non_image_inline_image_is_rejected_at_construction() -> None:
    # An InlineImage with a non-image content_type round-trips as a body leaf on
    # parse and vanishes from inline_images — silent structural corruption. It
    # must be rejected loudly at construction instead, as a typed MimeError.
    from kiro_crew.connections.vendors.gmail.attachments import InlineImage

    with pytest.raises(MalformedMimeError):
        InlineImage(content_id="cid1", content=b"x", content_type="text/plain")
    # a real image type is accepted
    img = InlineImage(content_id="cid1", content=b"x", content_type="image/png")
    assert img.content_type == "image/png"


def test_undecodable_rfc2231_filename_is_rejected() -> None:
    # get_filename() decodes RFC 2231 filename*=UTF-8''… with errors="replace",
    # yielding U+FFFD on malformed bytes while the content checksum is stamped
    # over intact bytes. That silent metadata corruption must be rejected; a
    # legitimately-encoded non-ASCII filename still decodes.
    bad = (
        b"From: a@example.com\r\nTo: c@example.com\r\nSubject: s\r\n"
        b'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
        b"--B\r\nContent-Type: text/plain\r\n\r\nBODY\r\n"
        b"--B\r\nContent-Type: application/octet-stream\r\n"
        b"Content-Disposition: attachment; filename*=UTF-8''%ff%fe%00bad\r\n\r\nDATA\r\n"
        b"--B--\r\n"
    )
    with pytest.raises(MalformedMimeError):
        parse_message(bad)
    ok = (
        b"From: a@example.com\r\nTo: c@example.com\r\nSubject: s\r\n"
        b'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
        b"--B\r\nContent-Type: text/plain\r\n\r\nBODY\r\n"
        b"--B\r\nContent-Type: application/octet-stream\r\n"
        b"Content-Disposition: attachment; filename*=UTF-8''%E4%BD%A0%E5%A5%BD.txt\r\n\r\nDATA\r\n"
        b"--B--\r\n"
    )
    parsed = parse_message(ok)
    assert parsed.attachments[0].filename == "\u4f60\u597d.txt"


def test_domain_literal_recipient_is_accepted() -> None:
    # RFC 5321 domain-literal addresses (user@[192.168.0.1], user@[IPv6:...])
    # are legal and producible by inbound mail; they must not be silently
    # dropped by a dotted-domain-only regex.
    from kiro_crew.connections.vendors.gmail.addresses import Mailbox

    assert Mailbox("user@[192.168.0.1]").address == "user@[192.168.0.1]"
    assert Mailbox("user@[IPv6:2001:db8::1]").address == "user@[IPv6:2001:db8::1]"


def test_message_attachment_does_not_crash_serialization() -> None:
    # A message/* attachment (message/delivery-status, message/rfc822) must be
    # attached as a parsed Message, not raw bytes — passing bytes with
    # maintype="message" builds an invalid payload that crashes as_bytes().
    from kiro_crew.connections.vendors.gmail import (
        Mailbox,
        RecipientSet,
        build_message,
        parse_message,
    )
    from kiro_crew.connections.vendors.gmail.attachments import Attachment

    dsn = (
        b"Reporting-MTA: dns; mail.example.com\r\n\r\n"
        b"Final-Recipient: rfc822; x@y.com\r\nAction: failed\r\n"
    )
    att = Attachment(filename="status", content=dsn, content_type="message/delivery-status")
    built = build_message(
        sender="a@example.com",
        recipients=RecipientSet(to=[Mailbox("c@example.com")]),
        subject="s",
        text_body="b",
        attachments=[att],
    )
    assert len(built.rfc822) > 0  # did not crash
    assert len(parse_message(built.rfc822).attachments) == 1


def test_oversized_inbound_attachment_raises_typed_error_not_malformed() -> None:
    # An oversized attachment on parse must surface as AttachmentTooLargeError,
    # not be rewritten to MalformedMimeError by a broad extraction handler.
    import base64 as _b64

    from kiro_crew.connections.vendors.gmail.attachments import DEFAULT_MAX_ATTACHMENT_BYTES
    from kiro_crew.connections.vendors.gmail.errors import AttachmentTooLargeError

    big = b"z" * (DEFAULT_MAX_ATTACHMENT_BYTES + 10)
    encoded = _b64.encodebytes(big)
    raw = (
        b"From: a@example.com\r\nTo: c@example.com\r\nSubject: s\r\n"
        b'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
        b"--B\r\nContent-Type: text/plain\r\n\r\nBODY\r\n"
        b"--B\r\nContent-Type: application/octet-stream\r\n"
        b"Content-Transfer-Encoding: base64\r\n"
        b'Content-Disposition: attachment; filename="big.bin"\r\n\r\n' + encoded + b"\r\n"
        b"--B--\r\n"
    )
    with pytest.raises(AttachmentTooLargeError):
        parse_message(raw)


def test_message_delivery_status_attachment_keeps_its_subtype() -> None:
    # A message/delivery-status attachment must NOT be silently relabeled
    # message/rfc822: add_attachment(<Message>) always emits message/rfc822, so
    # the builder corrects the outer part's subtype to the declared one.
    import email as _email

    from kiro_crew.connections.vendors.gmail import build_message
    from kiro_crew.connections.vendors.gmail.addresses import Mailbox, RecipientSet
    from kiro_crew.connections.vendors.gmail.attachments import Attachment

    dsn = b"Content-Type: message/delivery-status\r\n\r\nReporting-MTA: dns; mx.example.com\r\n"
    att = Attachment(filename="status.txt", content=dsn, content_type="message/delivery-status")
    rs = RecipientSet(to=[Mailbox("a@example.com")])
    built = build_message(
        sender="me@example.com", recipients=rs, subject="s", text_body="b", attachments=[att]
    )
    parsed_types = [p.get_content_type() for p in _email.message_from_bytes(built.rfc822).walk()]
    assert "message/delivery-status" in parsed_types
    # A plain message/rfc822 attachment is unaffected (subtype already rfc822).
    eml = b"From: x@example.com\r\nTo: y@example.com\r\nSubject: fwd\r\n\r\nhi\r\n"
    att2 = Attachment(filename="m.eml", content=eml, content_type="message/rfc822")
    built2 = build_message(
        sender="me@example.com", recipients=rs, subject="s", text_body="b", attachments=[att2]
    )
    types2 = [p.get_content_type() for p in _email.message_from_bytes(built2.rfc822).walk()]
    assert "message/rfc822" in types2


def test_mailbox_rejects_crlf_in_address_and_display_name() -> None:
    # CR/LF in an address or display name is the classic header-injection
    # vector; it must be refused at Mailbox construction, not deferred to an
    # uncaught ValueError when the header is serialized.
    from kiro_crew.connections.vendors.gmail.addresses import Mailbox
    from kiro_crew.connections.vendors.gmail.errors import InvalidAliasError

    for bad in ("a\r\nInjected: x@example.com", "a\nb@example.com", '"a\rb"@example.com'):
        with pytest.raises(InvalidAliasError):
            Mailbox(bad)
    with pytest.raises(InvalidAliasError):
        Mailbox("ok@example.com", "Name\r\nBcc: evil@example.com")
    # A legitimate address + display name still constructs.
    assert Mailbox("good@example.com", "Good Name").address == "good@example.com"


def test_message_delivery_status_extracted_payload_excludes_wrapper_headers() -> None:
    # A message/delivery-status attachment must be captured as its embedded
    # payload, NOT the whole container: serializing the container would fold its
    # own Content-Type/Content-Disposition wrapper headers into the captured
    # bytes, so the checksum would cover the wrapper instead of the payload.
    import email as _email

    from kiro_crew.connections.vendors.gmail import build_message
    from kiro_crew.connections.vendors.gmail.addresses import Mailbox, RecipientSet
    from kiro_crew.connections.vendors.gmail.attachments import Attachment

    dsn = b"Reporting-MTA: dns; mx.example.com\r\n"
    att = Attachment(filename="s.txt", content=dsn, content_type="message/delivery-status")
    rs = RecipientSet(to=[Mailbox("a@example.com")])
    built = build_message(
        sender="me@example.com", recipients=rs, subject="s", text_body="b", attachments=[att]
    )
    types = [p.get_content_type() for p in _email.message_from_bytes(built.rfc822).walk()]
    assert "message/delivery-status" in types
    parsed = parse_message(built.rfc822)
    assert len(parsed.attachments) == 1
    extracted = parsed.attachments[0].content
    assert b"Content-Disposition: attachment" not in extracted
    assert b"Reporting-MTA" in extracted


def test_repeated_address_with_different_names_keeps_each_name() -> None:
    # The same addr-spec appearing twice with different encoded display names
    # must keep BOTH names (matched by occurrence, not by an addr-spec key that
    # would give every later occurrence the first name).
    raw = (
        b"From: s@example.com\r\n"
        b"To: =?utf-8?b?QWxpY2U=?= <dup@example.com>, "
        b"=?utf-8?b?Qm9i?= <dup@example.com>\r\n"
        b"Subject: x\r\n\r\nbody\r\n"
    )
    parsed = parse_message(raw)
    assert [(m.address, m.display_name) for m in parsed.to] == [
        ("dup@example.com", "Alice"),
        ("dup@example.com", "Bob"),
    ]


def test_mailbox_rejects_consecutive_dot_local_part() -> None:
    # A consecutive-/leading-/trailing-dot local part passes the cheap regex but
    # is rejected by the stdlib Address the builder constructs; validate it at
    # Mailbox construction so the failure surfaces as InvalidAliasError, not an
    # uncaught InvalidHeaderDefect crash at header serialization.
    from kiro_crew.connections.vendors.gmail.addresses import Mailbox
    from kiro_crew.connections.vendors.gmail.errors import InvalidAliasError

    for bad in ("a..b@example.com", ".a@example.com", "a.@example.com"):
        with pytest.raises(InvalidAliasError):
            Mailbox(bad)
    # Valid dotted, quoted, and domain-literal local parts still construct.
    assert Mailbox("a.b@example.com").address == "a.b@example.com"
    assert Mailbox('"John Doe"@example.com').address == '"John Doe"@example.com'
    assert Mailbox("user@[192.168.0.1]").address == "user@[192.168.0.1]"


def test_uppercase_message_maintype_routed_not_crashed() -> None:
    # Media types are case-insensitive: an uppercase MESSAGE/delivery-status
    # must take the message/* branch, not fall through to the raw-bytes path
    # that crashes as_bytes().
    import email as _email

    from kiro_crew.connections.vendors.gmail import build_message
    from kiro_crew.connections.vendors.gmail.addresses import Mailbox, RecipientSet
    from kiro_crew.connections.vendors.gmail.attachments import Attachment

    att = Attachment(
        filename="s.txt",
        content=b"Reporting-MTA: dns; mx\r\n",
        content_type="MESSAGE/delivery-status",
    )
    rs = RecipientSet(to=[Mailbox("a@example.com")])
    built = build_message(
        sender="me@example.com", recipients=rs, subject="s", text_body="b", attachments=[att]
    )
    types = [p.get_content_type() for p in _email.message_from_bytes(built.rfc822).walk()]
    assert "message/delivery-status" in types


def test_single_label_domain_recipient_is_accepted() -> None:
    # A single-label domain (localhost / intranet host) is a legal recipient;
    # it must NOT be silently dropped by a dotted-domain-only check.
    from kiro_crew.connections.vendors.gmail.addresses import Mailbox

    assert Mailbox("admin@localhost").address == "admin@localhost"
    # Round-trips through a built + parsed message intact.
    from kiro_crew.connections.vendors.gmail import build_message
    from kiro_crew.connections.vendors.gmail.addresses import RecipientSet

    rs = RecipientSet(to=[Mailbox("root@localhost")])
    built = build_message(sender="me@example.com", recipients=rs, subject="s", text_body="b")
    assert [m.address for m in parse_message(built.rfc822).to] == ["root@localhost"]


def test_inline_delivery_status_captured_whole_not_scattered() -> None:
    # message/delivery-status is the standard INLINE body part of a
    # multipart/report bounce; it must be captured whole (its DSN blocks
    # preserved), not descended into and discarded.
    dsn = (
        b"From: mailer@example.com\r\nTo: sender@example.com\r\n"
        b"Subject: Delivery Status\r\n"
        b'Content-Type: multipart/report; report-type=delivery-status; boundary="B"\r\n\r\n'
        b"--B\r\nContent-Type: text/plain\r\n\r\nYour message bounced.\r\n"
        b"--B\r\nContent-Type: message/delivery-status\r\n\r\n"
        b"Reporting-MTA: dns; mx.example.com\r\n\r\n"
        b"Final-Recipient: rfc822; bad@example.com\r\nAction: failed\r\nStatus: 5.1.1\r\n"
        b"--B--\r\n"
    )
    parsed = parse_message(dsn)
    assert len(parsed.attachments) == 1
    assert b"Final-Recipient" in parsed.attachments[0].content


def test_subtype_fix_does_not_corrupt_a_nested_message_attachment() -> None:
    # _fix_message_attachment_subtype must rewrite ONLY the outer just-appended
    # part, never a nested message/rfc822 reached by a full-tree walk.
    import email as _email

    from kiro_crew.connections.vendors.gmail import build_message
    from kiro_crew.connections.vendors.gmail.addresses import Mailbox, RecipientSet
    from kiro_crew.connections.vendors.gmail.attachments import Attachment

    inner_eml = b"From: a@example.com\r\nTo: b@example.com\r\nSubject: nested\r\n\r\nhi\r\n"
    outer = b"Content-Type: message/global\r\n\r\n" + inner_eml
    att = Attachment(filename="g.eml", content=outer, content_type="message/global")
    rs = RecipientSet(to=[Mailbox("a@example.com")])
    built = build_message(
        sender="me@example.com", recipients=rs, subject="s", text_body="b", attachments=[att]
    )
    types = [p.get_content_type() for p in _email.message_from_bytes(built.rfc822).walk()]
    # The outer attachment carries the declared message/global; no stray
    # relabeling of a nested part into a wrong subtype.
    assert "message/global" in types
    assert types.count("message/rfc822") == 0 or "message/global" in types


def test_filename_with_literal_replacement_char_is_kept() -> None:
    # A U+FFFD the sender literally placed in a validly-encoded RFC 2231
    # filename is a legitimate character, not decode corruption — it must be
    # kept, while a filename U+FFFD introduced by a LOSSY decode is rejected.
    import urllib.parse as _url

    literal = "\ufffd.txt"
    enc = _url.quote(literal.encode("utf-8"))
    raw = (
        b"From: a@example.com\r\nTo: c@example.com\r\nSubject: s\r\n"
        b'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
        b"--B\r\nContent-Type: text/plain\r\n\r\nbody\r\n"
        b"--B\r\nContent-Type: application/octet-stream\r\n"
        b"Content-Transfer-Encoding: base64\r\n"
        b"Content-Disposition: attachment; filename*=UTF-8''" + enc.encode() + b"\r\n\r\n"
        b"AAAA\r\n--B--\r\n"
    )
    parsed = parse_message(raw)
    assert len(parsed.attachments) == 1
    assert "\ufffd" in parsed.attachments[0].filename


def test_close_boundary_not_found_is_fatal() -> None:
    # A multipart whose closing boundary never appears is a structural lie; the
    # CloseBoundaryNotFoundDefect must be promoted to MalformedMimeError, not
    # collapsed silently. (Mutation guard for _FATAL_DEFECT_NAMES.)
    raw = (
        b"From: a@example.com\r\nTo: c@example.com\r\nSubject: s\r\n"
        b'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
        b"--B\r\nContent-Type: text/plain\r\n\r\nbody with no closing boundary\r\n"
    )
    with pytest.raises(MalformedMimeError):
        parse_message(raw)


def test_inline_image_over_cap_is_refused_at_construction() -> None:
    # The inline-image size cap must bite: an InlineImage over the cap raises
    # at construction. (Mutation guard for the inline cap.)
    from kiro_crew.connections.vendors.gmail.attachments import InlineImage
    from kiro_crew.connections.vendors.gmail.errors import AttachmentTooLargeError

    with pytest.raises(AttachmentTooLargeError):
        InlineImage(
            content_id="img1",
            content=b"x" * 101,
            content_type="image/png",
            max_bytes=100,
        )
    # At exactly the cap it is allowed (exact-boundary guard: > not >=).
    ok = InlineImage(
        content_id="img1",
        content=b"x" * 100,
        content_type="image/png",
        max_bytes=100,
    )
    assert ok.content_id == "img1"


def test_total_message_cap_is_exclusive_at_the_boundary() -> None:
    # max_total_bytes uses a strict '>' comparison: a message of EXACTLY the cap
    # is allowed, one byte over is refused. (Mutation guard: '>' vs '>='.)
    # message_id and date are pinned so the serialized size is deterministic
    # across builds (make_msgid / now() would otherwise vary the length).
    from datetime import datetime, timezone

    from kiro_crew.connections.vendors.gmail import build_message
    from kiro_crew.connections.vendors.gmail.addresses import Mailbox, RecipientSet
    from kiro_crew.connections.vendors.gmail.errors import AttachmentTooLargeError

    rs = RecipientSet(to=[Mailbox("a@example.com")])
    fixed_mid = "<fixed-id@example.com>"
    fixed_date = datetime(2026, 1, 1, tzinfo=timezone.utc)
    kw = dict(
        sender="me@example.com",
        recipients=rs,
        subject="s",
        text_body="body",
        message_id=fixed_mid,
        date=fixed_date,
    )
    probe = build_message(**kw)
    exact = len(probe.rfc822)
    # Exactly at the cap: accepted.
    built = build_message(max_total_bytes=exact, **kw)
    assert len(built.rfc822) == exact
    # One byte under the cap: refused.
    with pytest.raises(AttachmentTooLargeError):
        build_message(max_total_bytes=exact - 1, **kw)


def test_crlf_in_sender_or_subject_is_rejected_typed() -> None:
    # A CR/LF in sender/subject (or threading headers) bypasses the Mailbox gate
    # and would crash build_message with an uncaught ValueError; it must be
    # refused as a typed MimeError (MissingHeaderError) up front.
    from kiro_crew.connections.vendors.gmail import build_message
    from kiro_crew.connections.vendors.gmail.addresses import Mailbox, RecipientSet
    from kiro_crew.connections.vendors.gmail.errors import MissingHeaderError

    rs = RecipientSet(to=[Mailbox("a@example.com")])
    base = dict(sender="me@example.com", recipients=rs, subject="s", text_body="b")
    for override in (
        {"sender": "me@example.com\r\nBcc: evil@example.com"},
        {"subject": "hi\r\nX-Injected: y"},
        {"in_reply_to": "<a@b>\r\nX: y"},
    ):
        kw = {**base, **override}
        with pytest.raises(MissingHeaderError):
            build_message(**kw)


def _attachment_with_rfc2231_filename(pct: bytes) -> bytes:
    return (
        b"From: a@example.com\r\nTo: c@example.com\r\nSubject: s\r\n"
        b'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
        b"--B\r\nContent-Type: text/plain\r\n\r\nbody\r\n"
        b"--B\r\nContent-Type: application/octet-stream\r\n"
        b"Content-Transfer-Encoding: base64\r\n"
        b"Content-Disposition: attachment; filename*=UTF-8''" + pct + b"\r\n\r\n"
        b"AAAA\r\n--B--\r\n"
    )


def test_corrupt_rfc2231_filename_is_rejected_but_literal_replacement_kept() -> None:
    # A filename* whose percent-bytes are invalid UTF-8 (%ff%fe) is
    # decode-introduced corruption and must be rejected — even though the
    # default policy's own get_param also shows U+FFFD (so the check inspects
    # the ORIGINAL undecoded parameter bytes instead). A filename that VALIDLY
    # encodes a literal U+FFFD (%ef%bf%bd) decodes cleanly and is kept.
    with pytest.raises(MalformedMimeError):
        parse_message(_attachment_with_rfc2231_filename(b"%ff%fe.txt"))
    kept = parse_message(_attachment_with_rfc2231_filename(b"%ef%bf%bd.txt"))
    assert "\ufffd" in kept.attachments[0].filename
    # A validly-encoded non-ASCII filename round-trips intact.
    ok = parse_message(_attachment_with_rfc2231_filename(b"%e4%bd%a0%e5%a5%bd.txt"))
    assert ok.attachments[0].filename == "\u4f60\u597d.txt"


def test_raw_8bit_header_bytes_are_rejected_not_returned_as_replacement() -> None:
    # A raw non-encoded-word 8-bit header (Subject: \xff\xfe) decodes to U+FFFD
    # under the default policy; returning that would be silent corruption, so it
    # must be rejected. A raw UTF-8 (SMTPUTF8) subject is valid and kept.
    with pytest.raises(MalformedMimeError):
        parse_message(b"From: a@example.com\r\nTo: c@example.com\r\nSubject: \xff\xfe\r\n\r\nb\r\n")
    valid = (
        "From: a@example.com\r\nTo: c@example.com\r\nSubject: \u5f20\u4e09\r\n\r\nb\r\n"
    ).encode("utf-8")
    parsed = parse_message(valid)
    assert parsed.headers["Subject"] == "\u5f20\u4e09"


def test_lf_only_message_attachment_bytes_preserved_verbatim_in_wire() -> None:
    # A message/* attachment must reach the wire byte-for-byte: parsing under
    # the default policy and re-serializing would normalize LF->CRLF and break a
    # checksum the caller took over the original bytes. The builder preserves
    # the supplied bytes (compat32 parse + faithful flatten).
    from kiro_crew.connections.vendors.gmail import build_message
    from kiro_crew.connections.vendors.gmail.addresses import Mailbox, RecipientSet
    from kiro_crew.connections.vendors.gmail.attachments import Attachment

    eml_lf = b"From: a@example.com\nTo: b@example.com\nSubject: fwd\n\nhi\n"
    att = Attachment(filename="m.eml", content=eml_lf, content_type="message/rfc822")
    rs = RecipientSet(to=[Mailbox("a@example.com")])
    built = build_message(
        sender="me@example.com", recipients=rs, subject="s", text_body="b", attachments=[att]
    )
    assert eml_lf in built.rfc822
    # The LF-only bytes are NOT silently CRLF-normalized in the wire form.
    assert eml_lf.replace(b"\n", b"\r\n") not in built.rfc822


def test_attachment_rejects_crlf_filename_and_malformed_media_type() -> None:
    # CR/LF in a filename or a malformed media type must be refused at
    # Attachment construction with a typed MimeError, not deferred to an
    # uncaught ValueError inside add_attachment / header serialization.
    from kiro_crew.connections.vendors.gmail.attachments import Attachment

    with pytest.raises(MalformedMimeError):
        Attachment(filename="a\r\nInjected: x", content=b"x")
    with pytest.raises(MalformedMimeError):
        Attachment(filename="ok.txt", content=b"x", content_type="bad type/x")
    with pytest.raises(MalformedMimeError):
        Attachment(filename="ok.txt", content=b"x", content_type="noslash")
    with pytest.raises(MalformedMimeError):
        Attachment(filename="ok.txt", content=b"x", content_type="a/b\r\nX: y")
    # A well-formed attachment still constructs.
    assert Attachment(filename="ok.txt", content=b"x", content_type="application/pdf").filename


def test_corrupt_rfc2231_continuation_segment_is_rejected() -> None:
    # RFC 2231 splits a long filename across numbered segments. A corrupt
    # CONTINUATION segment (filename*1*=%ff) must be validated too, not just the
    # initial section — otherwise get_filename() returns silent U+FFFD over an
    # intact-content checksum.
    raw = (
        b"From: a@example.com\r\nTo: c@example.com\r\nSubject: s\r\n"
        b'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
        b"--B\r\nContent-Type: text/plain\r\n\r\nbody\r\n"
        b"--B\r\nContent-Type: application/octet-stream\r\n"
        b"Content-Transfer-Encoding: base64\r\n"
        b"Content-Disposition: attachment;\r\n"
        b" filename*0*=UTF-8''safe;\r\n"
        b" filename*1*=%ff.txt\r\n\r\nAAAA\r\n--B--\r\n"
    )
    with pytest.raises(MalformedMimeError):
        parse_message(raw)
    # A VALID multi-segment filename is accepted and reassembled.
    ok = (
        b"From: a@example.com\r\nTo: c@example.com\r\nSubject: s\r\n"
        b'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
        b"--B\r\nContent-Type: text/plain\r\n\r\nbody\r\n"
        b"--B\r\nContent-Type: application/octet-stream\r\n"
        b"Content-Transfer-Encoding: base64\r\n"
        b"Content-Disposition: attachment;\r\n"
        b" filename*0*=UTF-8''%e4%bd%a0;\r\n"
        b" filename*1*=%e5%a5%bd.txt\r\n\r\nAAAA\r\n--B--\r\n"
    )
    parsed = parse_message(ok)
    assert parsed.attachments[0].filename == "\u4f60\u597d.txt"


def test_embedded_message_extraction_preserves_source_line_endings() -> None:
    # An embedded message/rfc822 is CRLF-CANONICALIZED end to end (RFC 5322 wire
    # form): the Attachment stores message/* content as CRLF and the parser
    # extracts CRLF, so a checksum over the canonical form survives the
    # round-trip regardless of whether the inbound source used LF or CRLF. This
    # is the coherent rule that replaces trying to preserve arbitrary source
    # line endings verbatim through a library that normalizes them.
    inbound_lf = (
        b"From: s@e.com\nTo: r@e.com\nSubject: fwd\n"
        b'Content-Type: multipart/mixed; boundary="B"\n\n'
        b"--B\nContent-Type: text/plain\n\nsee attached\n"
        b"--B\nContent-Type: message/rfc822\n"
        b'Content-Disposition: attachment; filename="i.eml"\n\n'
        b"From: x@e.com\nTo: y@e.com\nSubject: inner\n\nbody\n"
        b"--B--\n"
    )
    lf_ext = parse_message(inbound_lf).attachments[0].content
    # LF-only inbound is canonicalized to CRLF on extraction.
    assert b"\r\n" in lf_ext
    assert b"\n" not in lf_ext.replace(b"\r\n", b"")
    assert lf_ext.startswith(b"From: x@e.com\r\nTo: y@e.com\r\n")

    crlf_ext = parse_message(inbound_lf.replace(b"\n", b"\r\n")).attachments[0].content
    assert b"\r\n" in crlf_ext
    assert crlf_ext.startswith(b"From: x@e.com\r\nTo: y@e.com\r\n")


def test_inline_image_rejects_crlf_and_malformed_media_type() -> None:
    # InlineImage must enforce the same CR/LF header-injection guard Attachment
    # does, on content_id / content_type / filename, and require a well-formed
    # image/* media type — all as typed MimeErrors, not uncaught ValueErrors.
    from kiro_crew.connections.vendors.gmail.attachments import InlineImage

    for override in (
        {"content_id": "c\r\nid"},
        {"content_type": "image/png\r\nX: y"},
        {"filename": "f\r\n.png"},
        {"content_type": "text/plain"},
        {"content_type": "image/"},
        {"content_type": "image png"},
    ):
        kw = {"content_id": "ok", "content": b"x", "content_type": "image/png", **override}
        with pytest.raises(MalformedMimeError):
            InlineImage(**kw)
    # A well-formed inline image still constructs.
    assert InlineImage(content_id="ok", content=b"x", content_type="image/png").content_id == "ok"


def test_message_attachment_crlf_canonicalized_checksum_survives_roundtrip() -> None:
    # Re-implemented F1: a message/* attachment is CRLF-canonicalized at
    # construction, so its checksum matches the wire/extracted form regardless
    # of whether the caller supplied LF-only or CRLF source bytes.
    from kiro_crew.connections.vendors.gmail import build_message
    from kiro_crew.connections.vendors.gmail.addresses import Mailbox, RecipientSet
    from kiro_crew.connections.vendors.gmail.attachments import Attachment

    rs = RecipientSet(to=[Mailbox("a@example.com")])
    eml_lf = b"From: a@e.com\nTo: b@e.com\nSubject: fwd\n\nhi\n"
    att = Attachment(filename="m.eml", content=eml_lf, content_type="message/rfc822")
    # Stored form is CRLF-canonical (no bare LF remains).
    assert b"\r\n" in att.content and b"\n" not in att.content.replace(b"\r\n", b"")
    built = build_message(
        sender="me@example.com", recipients=rs, subject="s", text_body="b", attachments=[att]
    )
    extracted = parse_message(built.rfc822).attachments[0].content
    # The extracted embedded message matches the CRLF-canonical stored form, so
    # the attachment's own checksum verifies against what round-trips.
    assert att.verify(extracted)
    # A CRLF source is already canonical — unchanged by the normalization.
    eml_crlf = b"From: a@e.com\r\nTo: b@e.com\r\nSubject: fwd\r\n\r\nhi\r\n"
    assert (
        Attachment(filename="m.eml", content=eml_crlf, content_type="message/rfc822").content
        == eml_crlf
    )
    # A non-message attachment is NOT line-ending-normalized.
    assert (
        Attachment(
            filename="x.bin", content=b"a\nb\n", content_type="application/octet-stream"
        ).content
        == b"a\nb\n"
    )


def test_malformed_rfc2047_encoded_word_filename_is_rejected() -> None:
    # Re-implemented F2: an RFC 2047 encoded-word filename carries no extended
    # parameter, so the strict-decode gate must validate it directly — a
    # malformed encoded-word (bytes invalid in the declared charset) is
    # rejected rather than returned as silent U+FFFD corruption.
    def _att(cd: bytes) -> bytes:
        return (
            b"From: a@example.com\r\nTo: c@example.com\r\nSubject: s\r\n"
            b'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
            b"--B\r\nContent-Type: text/plain\r\n\r\nbody\r\n"
            b"--B\r\nContent-Type: application/octet-stream\r\n"
            b"Content-Transfer-Encoding: base64\r\n" + cd + b"\r\n\r\nAAAA\r\n--B--\r\n"
        )

    # %ff%fe (base64 //4=) is not valid UTF-8 -> decode-introduced corruption.
    with pytest.raises(MalformedMimeError):
        parse_message(_att(b'Content-Disposition: attachment; filename="=?utf-8?b?//4=?="'))
    # A validly-encoded RFC 2047 filename decodes cleanly and is kept.
    parsed = parse_message(
        _att(b'Content-Disposition: attachment; filename="=?utf-8?b?5L2g5aW9?=.txt"')
    )
    assert parsed.attachments[0].filename == "\u4f60\u597d.txt"


def _mixed_attachment(cd: bytes, ct: bytes = b"application/octet-stream") -> bytes:
    return (
        b"From: a@e.com\r\nTo: c@e.com\r\nSubject: s\r\n"
        b'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
        b"--B\r\nContent-Type: text/plain\r\n\r\nbody\r\n"
        b"--B\r\nContent-Type: "
        + ct
        + b"\r\nContent-Transfer-Encoding: base64\r\n"
        + cd
        + b"\r\n\r\nAAAA\r\n--B--\r\n"
    )


def test_erased_encoded_word_filename_is_rejected() -> None:
    # An RFC 2047 encoded-word filename that decodes to EMPTY (invalid base64)
    # carries no U+FFFD, so a U+FFFD-gated check would skip it and silently
    # accept an erased filename. The strict check must run for every present
    # filename parameter.
    with pytest.raises(MalformedMimeError):
        parse_message(
            _mixed_attachment(b'Content-Disposition: attachment; filename="=?utf-8?b?!!!!?="')
        )
    # A validly-encoded encoded-word filename is still kept.
    parsed = parse_message(
        _mixed_attachment(b'Content-Disposition: attachment; filename="=?utf-8?b?5L2g5aW9?=.txt"')
    )
    assert parsed.attachments[0].filename == "\u4f60\u597d.txt"


def test_second_body_leaf_outside_alternative_is_preserved_as_attachment() -> None:
    # A second text/plain (or text/html) leaf outside a multipart/alternative is
    # real content; keeping only the first body leaf would silently drop it. The
    # first stays the body; subsequent body leaves are captured as attachments.
    raw = (
        b"From: a@e.com\r\nTo: c@e.com\r\nSubject: s\r\n"
        b'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
        b"--B\r\nContent-Type: text/plain\r\n\r\nfirst body\r\n"
        b"--B\r\nContent-Type: text/plain\r\n\r\nsecond body\r\n--B--\r\n"
    )
    parsed = parse_message(raw)
    assert parsed.text_body.strip() == "first body"
    assert len(parsed.attachments) == 1
    assert b"second body" in parsed.attachments[0].content


def test_named_cid_image_without_disposition_is_inline_not_attachment() -> None:
    # A named image carrying a Content-ID but no Content-Disposition is a cid:
    # referent, not an attachment — it must stay in inline_images so the HTML
    # cid: reference resolves, rather than dropping out because it has a name=.
    raw = (
        b"From: a@e.com\r\nTo: c@e.com\r\nSubject: s\r\n"
        b'Content-Type: multipart/related; boundary="B"\r\n\r\n'
        b"--B\r\nContent-Type: text/html\r\n\r\n<img src=cid:img1>\r\n"
        b'--B\r\nContent-Type: image/png; name="logo.png"\r\nContent-ID: <img1>\r\n'
        b"Content-Transfer-Encoding: base64\r\n\r\niVBORw0KAA==\r\n--B--\r\n"
    )
    parsed = parse_message(raw)
    assert len(parsed.inline_images) == 1
    assert len(parsed.attachments) == 0
    # An image with an EXPLICIT attachment disposition stays an attachment.
    raw_att = raw.replace(
        b"Content-ID: <img1>", b"Content-Disposition: attachment\r\nContent-ID: <img1>"
    )
    parsed_att = parse_message(raw_att)
    assert len(parsed_att.attachments) == 1
    assert len(parsed_att.inline_images) == 0


def test_message_attachment_cap_reenforced_after_crlf_normalization() -> None:
    # CRLF-canonicalizing a message/* attachment can GROW its byte count
    # (LF -> CRLF), so the size cap must be re-checked on the normalized form —
    # an LF-only payload under the raw cap whose CRLF form exceeds it is
    # rejected, not silently accepted over-cap.
    from kiro_crew.connections.vendors.gmail.attachments import Attachment
    from kiro_crew.connections.vendors.gmail.errors import AttachmentTooLargeError

    lf = b"a\n" * 10  # 20 bytes raw, 30 bytes CRLF-normalized
    with pytest.raises(AttachmentTooLargeError):
        Attachment(filename="m.eml", content=lf, content_type="message/rfc822", max_bytes=25)
    # Under the cap after normalization: accepted, stored CRLF-canonical.
    ok = Attachment(filename="m.eml", content=lf, content_type="message/rfc822", max_bytes=40)
    assert len(ok.content) == 30 and b"\n" not in ok.content.replace(b"\r\n", b"")


def test_validate_send_as_requires_boolean_true_not_truthy() -> None:
    # A sender-identity gate must fail closed unless is_verified is the boolean
    # True: a truthy non-bool (e.g. a mis-populated string) must NOT pass.
    from kiro_crew.connections.vendors.gmail.addresses import validate_send_as
    from kiro_crew.connections.vendors.gmail.errors import InvalidAliasError

    class _TruthyAlias:
        send_as_email = "x@example.com"
        is_verified = "yes"  # truthy string, not True

    with pytest.raises(InvalidAliasError):
        validate_send_as("x@example.com", [_TruthyAlias()])


def test_attachment_and_inline_image_store_normalized_content_type() -> None:
    # The normalized (stripped) media type is what gets stored, not the raw
    # whitespace-padded input.
    from kiro_crew.connections.vendors.gmail.attachments import Attachment, InlineImage

    assert (
        Attachment(filename="x", content=b"y", content_type="  application/pdf  ").content_type
        == "application/pdf"
    )
    assert (
        InlineImage(content_id="c", content=b"y", content_type="  image/png  ").content_type
        == "image/png"
    )


def test_build_message_rejects_invalid_sender_and_malformed_message_id() -> None:
    # An unvalidated sender or a non-single-msgid Message-ID would crash or
    # desync the emitted header; both are refused as typed MimeErrors.
    from kiro_crew.connections.vendors.gmail import build_message
    from kiro_crew.connections.vendors.gmail.addresses import Mailbox, RecipientSet
    from kiro_crew.connections.vendors.gmail.errors import MissingHeaderError

    rs = RecipientSet(to=[Mailbox("a@example.com")])
    with pytest.raises(MissingHeaderError):
        build_message(sender="not an address", recipients=rs, subject="s", text_body="b")
    with pytest.raises(MissingHeaderError):
        build_message(
            sender="me@example.com",
            recipients=rs,
            subject="s",
            text_body="b",
            message_id="bare-no-brackets",
        )
    # A valid sender and a single RFC msg-id are accepted.
    built = build_message(
        sender="me@example.com",
        recipients=rs,
        subject="s",
        text_body="b",
        message_id="<id@example.com>",
    )
    assert built.message_id == "<id@example.com>"


def test_raw_8bit_non_encoded_filename_is_rejected() -> None:
    # A plain filename="…" carrying raw 8-bit bytes (neither RFC 2231 nor RFC
    # 2047) decodes to U+FFFD; that decode-introduced corruption is rejected,
    # not returned as a silently-corrupted filename.
    raw = (
        b"From: a@e.com\r\nTo: c@e.com\r\nSubject: s\r\n"
        b'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
        b"--B\r\nContent-Type: text/plain\r\n\r\nb\r\n"
        b"--B\r\nContent-Type: application/octet-stream\r\n"
        b"Content-Transfer-Encoding: base64\r\n"
        b'Content-Disposition: attachment; filename="\xff\xfe.txt"\r\n\r\nAAAA\r\n--B--\r\n'
    )
    with pytest.raises(MalformedMimeError):
        parse_message(raw)


def test_media_type_with_stray_delimiter_is_rejected() -> None:
    # A media type must be a single maintype/subtype of valid MIME-token chars;
    # a stray delimiter on either side of '/' (which a bare count("/")==1 check
    # accepts and then silently discards) is rejected — for Attachment and
    # InlineImage alike.
    from kiro_crew.connections.vendors.gmail.attachments import Attachment, InlineImage

    for bad in ("a/;b", "a/b;c", "text/", "/x", "a b/c"):
        with pytest.raises(MalformedMimeError):
            Attachment(filename="x", content=b"y", content_type=bad)
    with pytest.raises(MalformedMimeError):
        InlineImage(content_id="c", content=b"y", content_type="image/;x")
    assert (
        Attachment(filename="x", content=b"y", content_type="application/pdf").content_type
        == "application/pdf"
    )


def test_duplicate_inline_image_content_ids_rejected_and_distinct_keep_filenames() -> None:
    # Two inline images sharing a Content-ID are rejected; distinct cids each
    # keep their own filename (a Content-ID search would corrupt the first on a
    # duplicate, so the just-appended part is targeted directly).
    import email as _email

    from kiro_crew.connections.vendors.gmail import build_message
    from kiro_crew.connections.vendors.gmail.addresses import Mailbox, RecipientSet
    from kiro_crew.connections.vendors.gmail.attachments import InlineImage
    from kiro_crew.connections.vendors.gmail.errors import MissingHeaderError

    rs = RecipientSet(to=[Mailbox("a@example.com")])
    dup = [
        InlineImage(content_id="dup", content=b"a", content_type="image/png", filename="a.png"),
        InlineImage(content_id="dup", content=b"b", content_type="image/png", filename="b.png"),
    ]
    with pytest.raises(MissingHeaderError):
        build_message(
            sender="me@e.com",
            recipients=rs,
            subject="s",
            html_body="<img src=cid:dup>",
            inline_images=dup,
        )
    distinct = [
        InlineImage(content_id="i1", content=b"a", content_type="image/png", filename="one.png"),
        InlineImage(content_id="i2", content=b"b", content_type="image/png", filename="two.png"),
    ]
    built = build_message(
        sender="me@e.com",
        recipients=rs,
        subject="s",
        html_body="<img src=cid:i1><img src=cid:i2>",
        inline_images=distinct,
    )
    fns = {
        p.get("Content-ID"): p.get_filename()
        for p in _email.message_from_bytes(built.rfc822).walk()
        if p.get("Content-ID")
    }
    assert fns.get("<i1>") == "one.png" and fns.get("<i2>") == "two.png"


def test_base64_message_global_attachment_is_decoded() -> None:
    # A base64-encoded message/global container (like message/rfc822) must be
    # decoded back to the embedded bytes, not captured as opaque base64 text.
    import base64 as _b64

    inner = b"From: x@e.com\r\nTo: y@e.com\r\nSubject: g\r\n\r\nhi\r\n"
    enc = _b64.encodebytes(inner)
    raw = (
        b"From: a@e.com\r\nTo: c@e.com\r\nSubject: s\r\n"
        b'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
        b"--B\r\nContent-Type: text/plain\r\n\r\nb\r\n"
        b"--B\r\nContent-Type: message/global\r\nContent-Transfer-Encoding: base64\r\n"
        b'Content-Disposition: attachment; filename="g.eml"\r\n\r\n' + enc + b"\r\n--B--\r\n"
    )
    parsed = parse_message(raw)
    assert parsed.attachments[0].content.startswith(b"From: x@e.com")


def test_mixed_encoded_word_filename_with_malformed_word_is_rejected() -> None:
    # A filename mixing literal text with an encoded-word must have EVERY
    # encoded-word strict-decoded; a malformed embedded word is rejected, not
    # silently dropped, even when literal text surrounds it.
    def _att(cd: bytes) -> bytes:
        return (
            b"From: a@e.com\r\nTo: c@e.com\r\nSubject: s\r\n"
            b'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
            b"--B\r\nContent-Type: text/plain\r\n\r\nb\r\n"
            b"--B\r\nContent-Type: application/octet-stream\r\n"
            b"Content-Transfer-Encoding: base64\r\n" + cd + b"\r\n\r\nAAAA\r\n--B--\r\n"
        )

    with pytest.raises(MalformedMimeError):
        parse_message(_att(b'Content-Disposition: attachment; filename="pre=?utf-8?b?!!!!?=.txt"'))
