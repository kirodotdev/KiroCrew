"""Recipient (To/Cc/Bcc) and sendAs-alias tests for the Gmail MIME engine.

Covers To/Cc/Bcc parsing and construction (Bcc IS written into the built raw
message — Gmail's messages.send delivers blind copies from the Bcc header, so
omitting it would silently drop those recipients), the "sendAs alias as From
validation entry (validation only, no real call)" line, and the
"invalid/unverified alias" negative case.
"""

from __future__ import annotations

import pytest

from kiro_crew.connections.vendors.gmail import (
    Mailbox,
    RecipientSet,
    SendAsAlias,
    build_message,
    parse_message,
    validate_send_as,
)
from kiro_crew.connections.vendors.gmail.addresses import parse_mailbox_list
from kiro_crew.connections.vendors.gmail.errors import InvalidAliasError, MalformedMimeError


def test_to_cc_and_bcc_all_serialized_into_raw() -> None:
    rs = RecipientSet(
        to=[Mailbox("alice@example.com", "Alice")],
        cc=[Mailbox("carol@example.com")],
        bcc=[Mailbox("hidden@example.com", "Hidden")],
    )
    bm = build_message(sender="me@example.com", recipients=rs, subject="s", text_body="b")
    # All three recipient classes are written into the message. Gmail's
    # messages.send takes only `raw` and delivers to the To/Cc/Bcc headers, so
    # the Bcc recipient MUST be present in the serialized bytes or they receive
    # nothing; Gmail strips the Bcc header from the copies it delivers.
    parsed = parse_message(bm.rfc822)
    assert [m.address for m in parsed.to] == ["alice@example.com"]
    assert [m.address for m in parsed.cc] == ["carol@example.com"]
    assert [m.address for m in parsed.bcc] == ["hidden@example.com"]
    assert b"hidden@example.com" in bm.rfc822


def test_bcc_only_message_delivers_via_bcc_header() -> None:
    rs = RecipientSet(bcc=[Mailbox("secret@example.com")])
    bm = build_message(sender="me@example.com", recipients=rs, subject="s", text_body="b")
    parsed = parse_message(bm.rfc822)
    # A Bcc-only message has no To/Cc, but its Bcc header IS present and carries
    # the recipient — that header is how Gmail delivers the blind copy. No
    # `To: undisclosed-recipients:` is synthesized: a bare Bcc header is a valid
    # RFC 5322 destination and the transport delivers from it directly.
    assert parsed.to == []
    assert parsed.cc == []
    assert [m.address for m in parsed.bcc] == ["secret@example.com"]
    assert b"secret@example.com" in bm.rfc822


def test_mailbox_rejects_invalid_address() -> None:
    with pytest.raises(InvalidAliasError):
        Mailbox("not-an-email")
    with pytest.raises(InvalidAliasError):
        Mailbox("spaces in@example.com")
    with pytest.raises(InvalidAliasError):
        Mailbox("@example.com")
    with pytest.raises(InvalidAliasError):
        Mailbox("noatsign.example.com")
    # A single-label domain (e.g. localhost, intranet host) IS a valid
    # recipient and must NOT be dropped — regression guard for the data-loss
    # class where "user@localhost" was silently rejected.
    assert Mailbox("root@localhost").address == "root@localhost"
    assert Mailbox("no@domain").address == "no@domain"


def test_validate_send_as_accepts_verified_alias_case_insensitive() -> None:
    aliases = [
        SendAsAlias("primary@example.com", is_verified=True),
        SendAsAlias("Team.Alias@example.com", is_verified=True),
    ]
    assert validate_send_as("team.alias@EXAMPLE.com", aliases) == "Team.Alias@example.com"
    assert validate_send_as("primary@example.com", aliases) == "primary@example.com"


def test_validate_send_as_rejects_unverified_alias() -> None:
    aliases = [SendAsAlias("pending@example.com", is_verified=False)]
    with pytest.raises(InvalidAliasError) as exc:
        validate_send_as("pending@example.com", aliases)
    assert "not verified" in str(exc.value)


def test_validate_send_as_rejects_unknown_alias() -> None:
    aliases = [SendAsAlias("known@example.com", is_verified=True)]
    with pytest.raises(InvalidAliasError) as exc:
        validate_send_as("stranger@example.com", aliases)
    assert "not a verified sendAs alias" in str(exc.value)


def test_validate_send_as_rejects_syntactically_invalid_from() -> None:
    with pytest.raises(InvalidAliasError):
        validate_send_as("garbage", [SendAsAlias("x@example.com")])


def test_validate_send_as_empty_alias_set_always_rejects() -> None:
    with pytest.raises(InvalidAliasError):
        validate_send_as("anyone@example.com", [])


def test_parse_mailbox_list_parses_names_and_addresses() -> None:
    boxes = parse_mailbox_list("Alice <alice@example.com>, bob@example.com")
    assert [(b.display_name, b.address) for b in boxes] == [
        ("Alice", "alice@example.com"),
        ("", "bob@example.com"),
    ]


def test_parse_mailbox_list_decodes_rfc2047_display_name() -> None:
    # =?utf-8?b?5byg5LiJ?= is the base64 encoded-word for 张三.
    boxes = parse_mailbox_list("=?utf-8?b?5byg5LiJ?= <z@example.com>")
    assert [(b.display_name, b.address) for b in boxes] == [("\u5f20\u4e09", "z@example.com")]


def test_parse_mailbox_list_recovers_raw_utf8_display_name() -> None:
    # A raw (non-encoded-word) UTF-8 display name, as compat32 exposes it via
    # latin-1 mojibake, is recovered to its real characters.
    mojibake = "\u5f20\u4e09".encode("utf-8").decode("latin-1")
    boxes = parse_mailbox_list(f"{mojibake} <z@example.com>")
    assert boxes[0].display_name == "\u5f20\u4e09"


def test_parse_mailbox_list_skips_malformed_tokens() -> None:
    # A group placeholder / a token with no addr-spec is dropped tolerantly;
    # a valid address in the same list still parses.
    boxes = parse_mailbox_list("undisclosed-recipients:;, good@example.com")
    assert [b.address for b in boxes] == ["good@example.com"]


def test_parse_mailbox_list_rejects_erased_encoded_word() -> None:
    # An encoded-word with invalid base64 decodes to empty bytes, silently
    # erasing the display name — that is corruption, not a legitimate empty name.
    with pytest.raises(MalformedMimeError):
        parse_mailbox_list("=?utf-8?b?!!!!?= <z@example.com>")


def test_bcc_display_name_roundtrips_through_raw() -> None:
    # A non-ASCII Bcc display name must survive into raw as an RFC 2047
    # encoded-word and decode back exactly — Bcc is a first-class header now,
    # so it gets the same encoding fidelity as To/Cc.
    rs = RecipientSet(
        to=[Mailbox("alice@example.com")],
        bcc=[Mailbox("hidden@example.com", "\u5f20\u4e09")],
    )
    bm = build_message(sender="me@example.com", recipients=rs, subject="s", text_body="b")
    parsed = parse_message(bm.rfc822)
    assert [(m.address, m.display_name) for m in parsed.bcc] == [
        ("hidden@example.com", "\u5f20\u4e09")
    ]


def test_multiple_bcc_recipients_all_written_into_raw() -> None:
    # Every Bcc address must reach raw; none may be silently dropped, since on
    # messages.send the Bcc header is the sole delivery channel for them.
    rs = RecipientSet(
        bcc=[Mailbox("a@example.com"), Mailbox("b@example.com"), Mailbox("c@example.com")],
    )
    bm = build_message(sender="me@example.com", recipients=rs, subject="s", text_body="b")
    parsed = parse_message(bm.rfc822)
    assert [m.address for m in parsed.bcc] == [
        "a@example.com",
        "b@example.com",
        "c@example.com",
    ]


def test_built_message_exposes_no_envelope_recipient_list() -> None:
    # On the messages.send transport the recipients ARE the To/Cc/Bcc headers
    # in raw; there is no separate envelope. BuiltMessage must therefore expose
    # no envelope_recipients attribute (a fake API describing a channel that
    # does not exist), and RecipientSet exposes has_recipients(), not a synthetic
    # union.
    rs = RecipientSet(to=[Mailbox("a@example.com")])
    bm = build_message(sender="me@example.com", recipients=rs, subject="s", text_body="b")
    assert not hasattr(bm, "envelope_recipients")
    assert rs.has_recipients() is True
    assert RecipientSet().has_recipients() is False
    assert not hasattr(rs, "envelope_recipients")
