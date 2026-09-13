"""A reply whose parent is gone posts top-level, and its thread pointer is cleared with it.

Resolution shares the append's lock because an all-scope clear wipes the message index under that
same lock, so a parent present when the caller read it can be gone by the time the append runs.
``reply_to`` is set only when the parent is found, so RETAINING the thread id there would store a
pointer at a message no reader can resolve alongside an empty ``reply_to`` -- a pair that
disagrees with itself. Clearing both keeps them consistent and keeps the message, which is the
only outcome that loses neither the content nor the reader's ability to place it.
"""

from __future__ import annotations

import pytest

from kiro_crew.channel import Channel


@pytest.mark.asyncio
async def test_a_reply_to_a_live_parent_keeps_its_thread_pointer():
    """The positive control: with the parent present, both halves of the pair are set."""
    ch = Channel(id="c1", topic="review")
    await ch.post("alice", "the parent", from_role="alice")
    parent = ch.messages[-1]

    await ch.post("bob", "the reply", from_role="bob", thread_id=parent.id)
    reply = ch.messages[-1]

    assert reply.thread_id == parent.id
    assert reply.reply_to == "alice", "the reply must name whom it answers"
    assert parent.reply_count == 1, "the parent's reply count must move"


@pytest.mark.asyncio
async def test_a_reply_to_a_vanished_parent_posts_top_level():
    """The declared behaviour: the message survives, and neither half of the pair is left set."""
    ch = Channel(id="c1", topic="review")
    await ch.post("alice", "the parent", from_role="alice")
    gone_id = ch.messages[-1].id

    # Exactly what an all-scope clear does to the index this resolution reads.
    ch.messages.clear()
    ch._msg_index.clear()

    await ch.post("bob", "the reply", from_role="bob", thread_id=gone_id)

    assert len(ch.messages) == 1, "the message was dropped rather than posted top-level"
    orphan = ch.messages[-1]
    assert orphan.content == "the reply"
    assert orphan.thread_id is None, (
        "the thread pointer survived its parent, so a reader resolves it to nothing while "
        f"reply_to says there is no parent; got {orphan.thread_id!r}"
    )
    assert (
        orphan.reply_to is None
    ), "reply_to is set for a parent that does not exist, so the pair disagrees with itself"


@pytest.mark.asyncio
async def test_the_pair_is_never_half_set():
    """Whatever happens to the parent, the two fields agree: both set, or neither."""
    ch = Channel(id="c1", topic="review")
    await ch.post("alice", "one", from_role="alice")
    live = ch.messages[-1].id
    await ch.post("bob", "two", from_role="bob", thread_id=live)
    ch._msg_index.pop(live)
    await ch.post("carol", "three", from_role="carol", thread_id=live)
    await ch.post("dave", "four", from_role="dave")

    for msg in ch.messages:
        assert (msg.thread_id is None) == (msg.reply_to is None), (
            f"half-set thread pointer on {msg.content!r}: "
            f"thread_id={msg.thread_id!r} reply_to={msg.reply_to!r}"
        )
