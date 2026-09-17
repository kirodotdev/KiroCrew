"""Reply-threading header tests: In-Reply-To / References / Subject prefix.

Covers the "reply header semantics: In-Reply-To / References chain, Subject
prefix handling" line of the G1 scope, plus that the builder writes the
computed values into the message and the parser reads them back.
"""

from __future__ import annotations

from kiro_crew.connections.vendors.gmail import (
    Mailbox,
    RecipientSet,
    add_reply_prefix,
    build_message,
    build_references_chain,
    parse_message,
    reply_headers,
)


def test_add_reply_prefix_adds_once() -> None:
    assert add_reply_prefix("Hello") == "Re: Hello"


def test_add_reply_prefix_idempotent_on_existing_prefix() -> None:
    assert add_reply_prefix("Re: Hello") == "Re: Hello"
    assert add_reply_prefix("RE: Hello") == "RE: Hello"
    assert add_reply_prefix("re:Hello") == "re:Hello"


def test_add_reply_prefix_leaves_count_form_undoubled() -> None:
    assert add_reply_prefix("Re[2]: Hello") == "Re[2]: Hello"


def test_add_reply_prefix_does_not_strip_fwd() -> None:
    # "Re: Fwd: x" is meaningful; a reply to a forward keeps the Fwd.
    assert add_reply_prefix("Fwd: report") == "Re: Fwd: report"


def test_add_reply_prefix_blank_subject() -> None:
    assert add_reply_prefix("") == "Re:"
    assert add_reply_prefix("   ") == "Re:"


def test_references_chain_appends_parent_id() -> None:
    chain = build_references_chain("<root@x> <a@x>", "<parent@x>")
    assert chain == "<root@x> <a@x> <parent@x>"


def test_references_chain_from_empty_parent_references() -> None:
    assert build_references_chain("", "<parent@x>") == "<parent@x>"


def test_references_chain_does_not_duplicate_parent_id() -> None:
    chain = build_references_chain("<root@x> <parent@x>", "<parent@x>")
    assert chain == "<root@x> <parent@x>"


def test_references_chain_normalizes_bare_message_id() -> None:
    assert build_references_chain("<root@x>", "parent@x") == "<root@x> <parent@x>"


def test_reply_headers_full() -> None:
    th = reply_headers(
        parent_message_id="<parent@x>",
        parent_references="<root@x> <mid@x>",
        parent_subject="Original topic",
    )
    assert th.in_reply_to == "<parent@x>"
    assert th.references == "<root@x> <mid@x> <parent@x>"
    assert th.subject == "Re: Original topic"


def test_reply_headers_no_parent_id_omits_threading() -> None:
    th = reply_headers("", "", "topic")
    assert th.in_reply_to == ""
    assert th.references == ""
    assert th.subject == "Re: topic"


def test_reply_headers_written_into_message_and_parsed_back() -> None:
    th = reply_headers("<parent@x>", "<root@x>", "topic")
    rs = RecipientSet(to=[Mailbox("to@example.com")])
    bm = build_message(
        sender="me@example.com",
        recipients=rs,
        subject=th.subject,
        text_body="reply body",
        in_reply_to=th.in_reply_to,
        references=th.references,
    )
    parsed = parse_message(bm.rfc822)
    assert parsed.in_reply_to == "<parent@x>"
    assert parsed.references == "<root@x> <parent@x>"
    assert parsed.subject == "Re: topic"
