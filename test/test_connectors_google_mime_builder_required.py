"""Builder required-field negative tests for the Gmail MIME engine.

Covers the "missing required header" negative case from the construction side:
build_message refuses to assemble a message with no sender, no recipient, or
no body rather than emitting a structurally-incomplete message.
"""

from __future__ import annotations

import pytest

from kiro_crew.connections.vendors.gmail import (
    Mailbox,
    RecipientSet,
    build_message,
)
from kiro_crew.connections.vendors.gmail.errors import MissingHeaderError


def test_missing_sender_refused() -> None:
    rs = RecipientSet(to=[Mailbox("to@example.com")])
    with pytest.raises(MissingHeaderError):
        build_message(sender="  ", recipients=rs, subject="s", text_body="b")


def test_no_recipients_refused() -> None:
    with pytest.raises(MissingHeaderError):
        build_message(
            sender="f@example.com",
            recipients=RecipientSet(),
            subject="s",
            text_body="b",
        )


def test_no_body_refused() -> None:
    rs = RecipientSet(to=[Mailbox("to@example.com")])
    with pytest.raises(MissingHeaderError):
        build_message(sender="f@example.com", recipients=rs, subject="s")


def test_message_id_generated_when_absent() -> None:
    rs = RecipientSet(to=[Mailbox("to@example.com")])
    bm = build_message(sender="f@example.com", recipients=rs, subject="s", text_body="b")
    assert bm.message_id.startswith("<") and bm.message_id.endswith(">")


def test_supplied_message_id_preserved() -> None:
    rs = RecipientSet(to=[Mailbox("to@example.com")])
    bm = build_message(
        sender="f@example.com",
        recipients=rs,
        subject="s",
        text_body="b",
        message_id="<custom@id>",
    )
    assert bm.message_id == "<custom@id>"
