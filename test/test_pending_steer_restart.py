"""A held steer survives a gateway restart as a queued peer message.

The persistence half of ``test_pending_steer``: what the save writes for held steers
and how a restart reads them back.
"""

from __future__ import annotations

import pytest
from chat_test_helpers import _make_state
from pending_steer_helpers import (
    TEXT,
    _running,
    _send,
    _send_plain,
)
from session_control_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _enabled_fixture,
    _fresh_create_budget,
    _fresh_stop_windows,
    _key,
    _peer_target,
    _slot,
)


def _saveable(slot):
    """*slot* with a transcript row, so a save writes its metadata line."""
    slot.append("user", "the running turn")
    slot.drain()
    return slot


async def _saved_queue(state, target):
    """Save *target* as the gateway does and return the ``queued_prompts`` it wrote.

    The queue writer a send started runs first, so this save is not refused as
    stale behind it.
    """
    from kiro_crew.dashboard.chat_persistence import _save_slot_to_history
    from kiro_crew.testing.wait import async_wait_until

    await async_wait_until(lambda: not target._queue_persist_inflight)
    _save_slot_to_history(state, target, closed=False)
    meta = state.conversation_log._read_metadata(_key(target))
    return (meta or {}).get("queued_prompts") or []


def _restart(state, target):
    """Restore *target* from its saved line into a fresh slot, as a gateway restart does."""
    from kiro_crew.dashboard.chat_persistence import _rehydrate_slot_from_history

    del state._slots[target.key]
    restored = _rehydrate_slot_from_history(state, target.key)
    assert restored is not None
    return restored


@pytest.mark.asyncio
async def test_a_held_steer_survives_a_restart_as_a_queued_peer_message(tmp_path):
    """A held steer is saved as a plain queue entry, so a restart hands it back as the
    same card a queued peer send restores as, at the head of the queue."""
    from kiro_crew.dashboard.session_control_parts.delivery_containment import (
        SEND_ORIGIN_META_KEY,
        SESSION_SEND_QUEUED_META,
    )

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _saveable(_running(_peer_target(state, "chat-2", caller)))
    plain = "a plain note queued before the steer"
    try:
        assert (await _send_plain(state, caller, plain))["started"] is False
        held = await _send(state, caller)
        assert held.get("pending_steer") is True
        saved = await _saved_queue(state, target)
    finally:
        target.task.cancel()

    assert [TEXT in e["content"] for e in saved] == [True, False], saved
    assert saved[0]["meta"][SEND_ORIGIN_META_KEY], "the sender stamp is saved"
    assert len(target._deferred_steers) == 1, "the live slot keeps its held steer"
    assert len(target._queue) == 1, "the save does not touch the live queue"

    restored = _restart(state, target)
    card, queued = restored._queue
    assert TEXT in card["content"] and plain in queued["content"]
    assert card["kind"] == "" and queued["kind"] == ""
    assert card["meta"].keys() == queued["meta"].keys(), (card["meta"], queued["meta"])
    assert card["meta"][SESSION_SEND_QUEUED_META] is True
    assert card["meta"]["relay"]["senderKey"] == queued["meta"]["relay"]["senderKey"]
    assert not restored._deferred_steers


@pytest.mark.asyncio
async def test_a_marked_or_settled_held_steer_is_not_saved(tmp_path):
    """A steer marked delivered may have reached the harness, and a settled one is gone:
    neither is saved, so a restart never runs either a second time."""
    from kiro_crew.dashboard import deferred_steer

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _saveable(_running(_peer_target(state, "chat-2", caller)))
    marked, settled = "the marked steer", "the settled steer"
    try:
        first = await _send(state, caller, message=marked)
        second = await _send(state, caller, message=settled)
        assert (await _send(state, caller)).get("pending_steer") is True
        target._deferred_steer_delivered[first["steer_id"]] = marked
        deferred_steer.settle_held(target, second["steer_id"], deferred_steer.DELIVERED)
        saved = await _saved_queue(state, target)
    finally:
        target.task.cancel()

    contents = [e["content"] for e in saved]
    assert len(contents) == 1 and TEXT in contents[0], contents


@pytest.mark.asyncio
async def test_several_held_steers_are_saved_in_send_order_at_the_head(tmp_path):
    """The saved order is the order the turn's end would queue them in."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _saveable(_running(_peer_target(state, "chat-2", caller)))
    plain, second = "a plain note queued first", "and run the full gate after the rebase"
    try:
        assert (await _send_plain(state, caller, plain))["started"] is False
        assert (await _send(state, caller)).get("pending_steer") is True
        assert (await _send(state, caller, message=second)).get("pending_steer") is True
        saved = await _saved_queue(state, target)
    finally:
        target.task.cancel()

    contents = [e["content"] for e in saved]
    assert len(contents) == 3, contents
    assert TEXT in contents[0] and second in contents[1] and plain in contents[2], contents


async def _hold_steers(state, caller, messages):
    """Hold one steer per message on the running ``chat-2``, in order."""
    for message in messages:
        assert (await _send(state, caller, message=message)).get("pending_steer") is True


async def _queue_user_prompt(state, target, monkeypatch, caplog, message):
    """Queue *message* as the user's own prompt; return its id and its ``queue_push``."""
    import logging

    from kiro_crew.dashboard.chat_delivery import queue_for_next_turn

    frames: list[tuple[str, dict]] = []
    monkeypatch.setattr(state, "broadcast_ws", lambda kind, data: frames.append((kind, data)))
    with caplog.at_level(logging.WARNING):
        qid = queue_for_next_turn(state, target, message, directive_user_origin=True)
    (push,) = [d for k, d in frames if k == "queue_push"]
    return qid, push


def _assert_reported_not_durable(target, qid, push, caplog):
    """The prompt *qid* is reported as the queue-only overflow is: logged, refused,
    and its card marked not durable."""
    from kiro_crew.dashboard.chat_delivery import queue_view
    from kiro_crew.dashboard.slot_queue_repository import refused_durable_ids

    assert any("NOT persisted" in r.getMessage() and qid in r.getMessage() for r in caplog.records)
    assert push["meta"]["durable"] is False
    refused = refused_durable_ids(target.durable_source())
    assert qid in refused
    (card,) = [v for v in queue_view(target._queue, refused) if v["id"] == qid]
    assert card["meta"]["durable"] is False
    assert target._queue[-1]["id"] == qid, "the send is still accepted"


@pytest.mark.asyncio
async def test_a_prompt_the_held_steers_push_past_the_count_cap_is_reported(
    tmp_path, monkeypatch, caplog
):
    """The held steers lead the saved queue, so they count toward its entry cap: a user
    prompt they push past it is warned about and marked, as one the queue pushes is."""
    from kiro_crew.dashboard import deferred_steer
    from kiro_crew.dashboard.slot_queue_repository import MAX_DURABLE_QUEUE_ENTRIES

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _saveable(_running(_peer_target(state, "chat-2", caller)))
    steers = [f"held steer number {i}" for i in range(deferred_steer.MAX_DEFERRED_STEERS)]
    try:
        await _hold_steers(state, caller, steers)
        for i in range(MAX_DURABLE_QUEUE_ENTRIES - len(steers)):
            target.queue_append(f"earlier {i}")
        qid, push = await _queue_user_prompt(state, target, monkeypatch, caplog, "the last one")
        _assert_reported_not_durable(target, qid, push, caplog)
        saved = await _saved_queue(state, target)
    finally:
        target.task.cancel()

    contents = [e["content"] for e in saved]
    assert len(contents) == MAX_DURABLE_QUEUE_ENTRIES
    assert "the last one" not in contents
    assert all(message in content for message, content in zip(steers, contents))


@pytest.mark.asyncio
async def test_a_prompt_the_held_steers_push_past_the_byte_budget_is_reported(
    tmp_path, monkeypatch, caplog
):
    """Large held steers spend the saved queue's byte budget ahead of the queue: a user
    prompt that does not fit is warned about and marked, as one the queue pushes is."""
    from kiro_crew.dashboard.session_control_parts.session_messaging import (
        MAX_SEND_MESSAGE_CHARS,
    )

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _saveable(_running(_peer_target(state, "chat-2", caller)))
    steers = [f"large steer {i} " + "s" * (MAX_SEND_MESSAGE_CHARS - 1_000) for i in range(5)]
    prompt = "my queued prompt " + "p" * 20_000
    try:
        await _hold_steers(state, caller, steers)
        qid, push = await _queue_user_prompt(state, target, monkeypatch, caplog, prompt)
        _assert_reported_not_durable(target, qid, push, caplog)
        saved = await _saved_queue(state, target)
    finally:
        target.task.cancel()

    contents = [e["content"] for e in saved]
    assert prompt not in contents
    assert len(contents) == len(steers)
    assert all(message in content for message, content in zip(steers, contents))
