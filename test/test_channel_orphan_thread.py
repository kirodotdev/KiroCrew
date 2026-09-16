"""A reply whose parent is gone posts top-level, and its thread pointer is cleared with it.

Resolution, append and rolloff run as one await-free window on the event loop, so the pair is
decided against the index the append writes to. A parent present when the caller chose the thread
id can be absent by then: the ``_MAX_MESSAGES`` rolloff evicts the oldest message, and an
all-scope clear empties the index outright.
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


@pytest.mark.asyncio
async def test_an_older_reply_loses_its_pointer_when_a_later_post_evicts_the_parent():
    """The append that evicts a parent is usually not the reply's own, so repairing only the
    message being appended leaves an older reply agreeing with itself and resolving to nothing.
    """
    ch = Channel(id="c1", topic="review")
    await ch.post("alice", "the parent", from_role="alice")
    parent_id = ch.messages[0].id
    await ch.post("bob", "the older reply", from_role="bob", thread_id=parent_id)
    assert ch.messages[1].thread_id == parent_id, "control: the reply starts out threaded"

    for i in range(_MAX_MESSAGES - 2):
        await ch.post("filler", f"m{i}", from_role="filler")
    assert len(ch.messages) == _MAX_MESSAGES
    assert ch.messages[0].id == parent_id, "the parent must be the next message to roll off"

    await ch.post("carol", "the post that evicts", from_role="carol")

    assert parent_id not in ch._msg_index, "control: that post had to evict the parent"
    reply = ch.messages[0]
    assert reply.content == "the older reply", "the reply must survive its parent's eviction"
    assert reply.thread_id is None, (
        "a retained earlier reply kept a pointer at the parent a later append evicted, so the "
        f"transcript filters it out and no thread panel reaches it; got {reply.thread_id!r}"
    )
    assert reply.reply_to is None, "reply_to names the sender of a message that is no longer there"


def _drain(inbox) -> list[str]:
    ids = []
    while not inbox.empty():
        ids.append(inbox.get_nowait().id)
    return ids


@pytest.mark.asyncio
async def test_a_reply_orphaned_by_its_own_append_is_delivered_where_it_was_persisted():
    """Eviction rewrites the pair on the stored message, so delivery has to route on that and
    not on the locals resolved before it -- those still name the parent that has just gone.
    """
    ch = Channel(id="c1", topic="review", _broadcast_fn=lambda t, d: None)
    orch = ch.add_agent(role="Orchestrator", agent_name="m", task="coord", is_orchestrator=True)
    orch.state = "listening"
    ch.orchestrator_id = orch.id
    spec = ch.add_agent(role="Specialist", agent_name="s", task="work")
    spec.state = "listening"

    await ch.post(spec.id, "the parent", from_role="Specialist")
    parent_id = ch.messages[0].id
    for i in range(_MAX_MESSAGES - 1):
        await ch.post("filler", f"m{i}", from_role="filler")
    assert len(ch.messages) == _MAX_MESSAGES
    assert ch.messages[0].id == parent_id, "the parent must be the next message to roll off"
    _drain(orch.inbox)
    _drain(spec.inbox)

    reply = await ch.post("human", "the reply", from_role="Human", thread_id=parent_id)

    assert parent_id not in ch._msg_index, "control: this append had to evict the parent"
    assert reply.thread_id is None, "control: the eviction cleared the stored pair"
    to_spec = _drain(spec.inbox)
    assert reply.id in _drain(orch.inbox), (
        "the reply persisted top-level, where every human message is the orchestrator's, but "
        "routing kept the pre-eviction pair and no later path re-delivers it"
    )
    assert reply.id not in to_spec, (
        "the reply reached the evicted parent's sender on a thread pointer that is no longer "
        "stored, so delivery and the persisted transcript disagree"
    )


@pytest.mark.asyncio
async def test_an_agent_reply_orphaned_by_its_own_append_still_reaches_the_parents_sender():
    """A human's orphaned reply falls through to the orchestrator's top-level branch. An agent's
    matches no branch once the pair is cleared -- not human, mentions nobody -- and nothing
    re-delivers it, so the sender that was waiting is left parked.
    """
    ch = Channel(id="c1", topic="review", _broadcast_fn=lambda t, d: None)
    orch = ch.add_agent(role="Orchestrator", agent_name="m", task="coord", is_orchestrator=True)
    orch.state = "listening"
    ch.orchestrator_id = orch.id
    spec = ch.add_agent(role="Specialist", agent_name="s", task="work")
    spec.state = "listening"

    await ch.post(orch.id, "the assignment", from_role="Orchestrator")
    parent_id = ch.messages[0].id
    for i in range(_MAX_MESSAGES - 1):
        await ch.post("filler", f"m{i}", from_role="filler")
    assert len(ch.messages) == _MAX_MESSAGES
    assert ch.messages[0].id == parent_id, "the parent must be the next message to roll off"
    _drain(orch.inbox)
    _drain(spec.inbox)

    report = await ch.post(spec.id, "done", from_role="Specialist", thread_id=parent_id)

    assert parent_id not in ch._msg_index, "control: this append had to evict the parent"
    assert report.thread_id is None, "control: the eviction cleared the stored pair"
    assert report.id in _drain(orch.inbox), (
        "the specialist's report reached no inbox at all: the cleared pair matches no thread "
        "branch, its sender is not human and it mentions nobody, so the orchestrator stays "
        "parked in subscribe() and the collaboration stalls with the report only in the log"
    )


@pytest.mark.asyncio
async def test_a_later_eviction_does_not_rewrite_a_message_already_queued_for_delivery():
    """Eviction clears the pair on the STORED message in place. A turn already queued must route
    on what it was delivered, or a later append silently re-roots work that is under way.
    """
    ch = Channel(id="c1", topic="review", _broadcast_fn=lambda t, d: None)
    orch = ch.add_agent(role="Orchestrator", agent_name="m", task="coord", is_orchestrator=True)
    orch.state = "listening"
    ch.orchestrator_id = orch.id
    spec = ch.add_agent(role="Specialist", agent_name="s", task="work")
    spec.state = "listening"

    await ch.post(orch.id, "the assignment", from_role="Orchestrator")
    parent_id = ch.messages[0].id
    for i in range(_MAX_MESSAGES - 2):
        await ch.post("filler", f"m{i}", from_role="filler")
    _drain(orch.inbox)
    _drain(spec.inbox)

    report = await ch.post(spec.id, "done", from_role="Specialist", thread_id=parent_id)
    assert len(ch.messages) == _MAX_MESSAGES, "control: this append must NOT have evicted anything"
    assert ch.messages[0].id == parent_id, "the parent must still be the next to roll off"
    assert report.thread_id == parent_id, "control: the report was persisted as a threaded reply"

    # The orchestrator is mid-turn, so its report sits unconsumed while the next post lands.
    await ch.post("carol", "the post that evicts", from_role="carol")

    assert parent_id not in ch._msg_index, "control: that post had to evict the parent"
    assert report.thread_id is None, "control: the sweep cleared the pair on the stored message"
    queued = orch.inbox.get_nowait()
    assert queued.id == report.id, "control: the report must be the message queued for delivery"
    assert queued.thread_id == parent_id, (
        "a later append rewrote the thread id of a message already queued, so the consumer roots "
        "its turn under its own report instead of top-level and the answer is filtered out of "
        f"the transcript; got {queued.thread_id!r}"
    )
    assert queued.reply_to == orch.id, (
        "reply_to was rewritten underneath a queued turn, changing who the consumer answers; "
        f"got {queued.reply_to!r}"
    )
