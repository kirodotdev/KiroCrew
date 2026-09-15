"""Recipient (To/Cc/Bcc) and sendAs-alias tests for the Gmail MIME engine.

Covers the "To/Cc/Bcc parsing and construction (Bcc must not leak into the sent
body header)" line, the "sendAs alias as From validation entry (validation
only, no real call)" line, and the "invalid/unverified alias" negative case.
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
from kiro_crew.connections.vendors.gmail.errors import InvalidAliasError


def test_to_and_cc_serialized_bcc_absent_from_header_block() -> None:
    rs = RecipientSet(
        to=[Mailbox("alice@example.com", "Alice")],
        cc=[Mailbox("carol@example.com")],
        bcc=[Mailbox("hidden@example.com", "Hidden")],
    )
    bm = build_message(sender="me@example.com", recipients=rs, subject="s", text_body="b")
    # The Bcc recipient must be in the envelope (they must receive the mail)…
    assert "hidden@example.com" in bm.envelope_recipients
    # …but must NEVER appear in the serialized message the recipients can read.
    lowered = bm.rfc822.decode("latin1").lower()
    assert "hidden@example.com" not in lowered
    assert "bcc:" not in lowered
    # To and Cc are present and parse back.
    parsed = parse_message(bm.rfc822)
    assert [m.address for m in parsed.to] == ["alice@example.com"]
    assert [m.address for m in parsed.cc] == ["carol@example.com"]


def test_envelope_recipients_dedup_preserves_order() -> None:
    rs = RecipientSet(
        to=[Mailbox("a@example.com"), Mailbox("b@example.com")],
        cc=[Mailbox("b@example.com"), Mailbox("c@example.com")],
        bcc=[Mailbox("a@example.com"), Mailbox("d@example.com")],
    )
    assert rs.envelope_recipients() == [
        "a@example.com",
        "b@example.com",
        "c@example.com",
        "d@example.com",
    ]


def test_bcc_only_message_still_has_recipients_but_no_visible_header() -> None:
    rs = RecipientSet(bcc=[Mailbox("secret@example.com")])
    bm = build_message(sender="me@example.com", recipients=rs, subject="s", text_body="b")
    assert bm.envelope_recipients == ["secret@example.com"]
    parsed = parse_message(bm.rfc822)
    assert parsed.to == []
    assert parsed.cc == []


def test_mailbox_rejects_invalid_address() -> None:
    with pytest.raises(InvalidAliasError):
        Mailbox("not-an-email")
    with pytest.raises(InvalidAliasError):
        Mailbox("no@domain")
    with pytest.raises(InvalidAliasError):
        Mailbox("spaces in@example.com")


def test_validate_send_as_accepts_verified_alias_case_insensitive() -> None:
    aliases = [
        SendAsAlias("primary@example.com", is_verified=True, treat_as_default=True),
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
