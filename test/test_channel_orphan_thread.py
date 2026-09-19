"""A reply whose parent is gone posts top-level, and its thread pointer is cleared with it.

Resolution shares the append's lock so the pair is decided against the index the append writes to.
A parent present when the caller chose the thread id can be absent by then: the ``_MAX_MESSAGES``
rolloff evicts the oldest message, and an all-scope clear empties the index outright.
``reply_to`` is set only when the parent is found, so RETAINING the thread id there would store a
pointer at a message no reader can resolve alongside an empty ``reply_to`` -- a pair that
disagrees with itself. Clearing both keeps them consistent and keeps the message, which is the
only outcome that loses neither the content nor the reader's ability to place it.
"""

from __future__ import annotations

import pytest

from kiro_crew.channel import _MAX_MESSAGES, Channel


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
async def test_the_appends_own_rolloff_cannot_leave_a_pointer_at_the_parent_it_evicted():
    """Resolution and eviction are the same append, so both fields stay set while the parent
    goes -- a pair that agrees with itself, which ``test_the_pair_is_never_half_set`` cannot see.
    """
    ch = Channel(id="c1", topic="review")
    await ch.post("alice", "the parent", from_role="alice")
    parent_id = ch.messages[0].id

    for i in range(_MAX_MESSAGES - 1):
        await ch.post("filler", f"m{i}", from_role="filler")
    assert len(ch.messages) == _MAX_MESSAGES
    assert ch.messages[0].id == parent_id, "the parent must be the next message to roll off"

    await ch.post("bob", "the reply", from_role="bob", thread_id=parent_id)

    reply = ch.messages[-1]
    assert reply.content == "the reply", "the reply must survive its parent's eviction"
    assert parent_id not in ch._msg_index, "control: this append had to evict the parent"
    assert reply.thread_id is None, (
        "the stored thread id outlived the parent this same append evicted, so a reader "
        f"resolves it to nothing; got {reply.thread_id!r}"
    )
    assert reply.reply_to is None, "reply_to names the sender of a message that is no longer there"


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
