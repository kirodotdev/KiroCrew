"""A deliberate discard of a queued cross-session delivery is reported to its sender.

``session_send`` into a busy target answers ``started: False``: the message is on
the target's queue and will run later. Two deliberate actions take it off that
queue unrun: a person cancelling its queue card (``api_chat_slot_queue_cancel``)
and a hard stop clearing the queue (``stop_slot_turn``'s escalated branch). Every
sign of either lands on the TARGET, which the sender does not read, so without a
notice the receipt stays the sender's last, and now false, word on the message.

These tests pin that both sites read the entry's sender stamp and append a notice
to the sender's own transcript through ``notify_send_origin_discarded``, worded
for the discard rather than for a changed constraint, and that the recipient
check the drain's notice applies holds here too: no stamp, a self-send, a closed
sender and a reused key held by a different tab all write nothing.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import make_mocked_request
from chat_test_helpers import _make_state

from kiro_crew.dashboard import chat_handlers as ch
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import slot_history_key


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    """Run in the shipped (enabled) session-control state without reading config."""
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)


@pytest.fixture(autouse=True)
def _quiet_audit(monkeypatch):
    """Both discard sites write an SEL row; keep them inline and off disk."""
    fake = MagicMock()
    monkeypatch.setattr(sc, "sel", lambda: fake)
    monkeypatch.setattr(sc, "_sel_off_loop", lambda write, what: write())
    monkeypatch.setattr(ch, "sel", lambda: fake)
    return fake


def _busy(slot):
    """``running`` is derived (``task is not None and not task.done()``)."""
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    return slot


def _send(state, caller, target_key, message):
    """One ``session_send`` into a busy target, which queues it."""
    out = asyncio.run(
        sc.send_to_target(
            state,
            caller_session_key=slot_history_key(caller),
            target=target_key,
            message=message,
        )
    )
    assert out["started"] is False
    return out


def _notices(slot):
    return [m["content"] for m in slot.messages if m.get("role") == "notice"]


async def _never_runs(state, slot, prompt):  # pragma: no cover - queued, not run
    raise AssertionError("a queued prompt must not start a turn at enqueue")


def _cancel(state, slot, queue_id):
    """The queue card's DELETE, through the real route handler."""
    app = {"state": state}
    request = make_mocked_request(
        "DELETE",
        f"/api/chat/slots/{slot.key}/queue/{queue_id}",
        match_info={"slot": slot.key, "queue_id": queue_id},
        app=app,
    )
    return asyncio.run(ch.api_chat_slot_queue_cancel(request))


def _hard_stop(state, slot):
    """Two Stop presses: the first is cooperative, the second hard-kills."""
    state.sessions.stop_turn = AsyncMock(return_value="cancelled")
    asyncio.run(ch.stop_slot_turn(state, slot))
    assert slot._stop_state == "soft_pending"
    asyncio.run(ch.stop_slot_turn(state, slot))
    assert slot._stop_state == "killing"


# ── A queue-card cancel ──────────────────────────────────────────────────────


def test_cancelling_a_queued_delivery_tells_its_sender(tmp_path):
    """The defect: the cancel retracts the card on the target and says nothing to
    the session holding the "queued" receipt."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = _busy(state.get_or_create_slot("chat-2"))
    _send(state, caller, "chat-2", "please summarize the log")
    queue_id = target._queue[0]["id"]

    resp = _cancel(state, target, queue_id)

    assert resp.status == 200
    assert target._queue == []
    sent = _notices(caller)
    assert len(sent) == 1, "the sending session was told nothing"
    assert "chat-2" in sent[0]
    assert "cancelled from that session's queue" in sent[0]
    assert "will not run" in sent[0]
    assert "please summarize the log" in sent[0]
    # An operator's discard changes no authorization, so the drain's wording
    # would describe something that did not happen.
    assert "authorization" not in sent[0]


def test_cancelling_one_entry_tells_only_its_own_sender(tmp_path):
    """The cancel removes one card. The sender of a neighbouring entry still has a
    message that will run, and must not be told otherwise."""
    state = _make_state(tmp_path)
    first = state.get_or_create_slot("chat-1")
    second = state.get_or_create_slot("chat-2")
    target = _busy(state.get_or_create_slot("chat-3"))
    _send(state, first, "chat-3", "alpha task")
    _send(state, second, "chat-3", "beta task")
    alpha_id = target._queue[0]["id"]

    _cancel(state, target, alpha_id)

    assert len(target._queue) == 1
    assert "beta task" in target._queue[0]["content"]
    assert "alpha task" in _notices(first)[-1]
    assert _notices(second) == []


def test_cancelling_a_human_typed_entry_tells_nobody(tmp_path):
    """A composer entry carries no sender stamp, so the cancel is exactly what it
    was: the text returns to the composer and no notice is written anywhere."""
    state = _make_state(tmp_path)
    other = state.get_or_create_slot("chat-1")
    slot = _busy(state.get_or_create_slot("chat-2"))
    slot.enqueue_or_run_prompt("typed by a person", _never_runs, state)
    queue_id = slot._queue[0]["id"]

    resp = _cancel(state, slot, queue_id)

    assert resp.status == 200
    assert slot._queue == []
    assert _notices(slot) == []
    assert _notices(other) == []


def test_a_cancel_of_an_unknown_id_tells_nobody(tmp_path):
    """A 404 discards nothing, so it reports nothing."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = _busy(state.get_or_create_slot("chat-2"))
    _send(state, caller, "chat-2", "still queued")

    resp = _cancel(state, target, "no-such-id")

    assert resp.status == 404
    assert len(target._queue) == 1
    assert _notices(caller) == []


# ── A hard stop ──────────────────────────────────────────────────────────────


def test_a_hard_stop_tells_every_sender_its_message_was_discarded(tmp_path):
    """The hard kill clears the whole queue, so each stamped entry's sender is told
    about its own message and not about a peer's."""
    state = _make_state(tmp_path)
    first = state.get_or_create_slot("chat-1")
    second = state.get_or_create_slot("chat-2")
    target = _busy(state.get_or_create_slot("chat-3"))
    _send(state, first, "chat-3", "alpha task")
    _send(state, second, "chat-3", "beta task")

    _hard_stop(state, target)

    assert list(target._queue) == []
    alpha = _notices(first)
    beta = _notices(second)
    assert len(alpha) == 1 and len(beta) == 1
    assert "chat-3" in alpha[0]
    assert "a hard stop of that session discarded its queue" in alpha[0]
    assert "will not run" in alpha[0]
    assert "alpha task" in alpha[0] and "beta task" not in alpha[0]
    assert "beta task" in beta[0] and "alpha task" not in beta[0]
    assert "authorization" not in alpha[0]


def test_a_soft_stop_keeps_the_queue_and_tells_nobody(tmp_path):
    """Control: the first, cooperative press keeps the queue, so the message will
    still run and its sender must not be told otherwise."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = _busy(state.get_or_create_slot("chat-2"))
    _send(state, caller, "chat-2", "still going to run")
    state.sessions.stop_turn = AsyncMock(return_value="cancelled")

    asyncio.run(ch.stop_slot_turn(state, target))

    assert target._stop_state == "soft_pending"
    assert len(target._queue) == 1
    assert _notices(caller) == []


def test_a_hard_stop_reports_only_the_entries_it_took(tmp_path, monkeypatch):
    """The entries reported are the entries the kill removes, and no others.

    The kill takes the queue before it awaits the sub-agent settle, so a send that
    lands during the settle stays queued for the next turn. Its message will still
    run, so its sender must not be told it was discarded, while the sender of the
    entry the kill took is.
    """
    state = _make_state(tmp_path)
    early = state.get_or_create_slot("chat-1")
    late = state.get_or_create_slot("chat-2")
    target = _busy(state.get_or_create_slot("chat-3"))
    _send(state, early, "chat-3", "queued before the stop")

    async def _settle_while_a_send_lands(state_, slot_, contents):
        out = await sc.send_to_target(
            state_,
            caller_session_key=slot_history_key(late),
            target="chat-3",
            message="queued during the settle",
        )
        assert out["started"] is False

    monkeypatch.setattr(ch, "_settle_discarded_stage_deliveries", _settle_while_a_send_lands)

    _hard_stop(state, target)

    assert len(target._queue) == 1
    assert "queued during the settle" in target._queue[0]["content"]
    assert "queued before the stop" in _notices(early)[-1]
    assert _notices(late) == []


def test_a_hard_stop_of_a_human_typed_queue_tells_nobody(tmp_path):
    """A composer entry has no peer waiting on it; the kill is unchanged for it."""
    state = _make_state(tmp_path)
    other = state.get_or_create_slot("chat-1")
    slot = _busy(state.get_or_create_slot("chat-2"))
    slot.enqueue_or_run_prompt("typed by a person", _never_runs, state)

    _hard_stop(state, slot)

    assert list(slot._queue) == []
    assert _notices(other) == []
    assert not any("discarded before it ran" in n for n in _notices(slot))


def test_a_hard_stop_tells_a_closed_sender_nothing(tmp_path):
    """The sender was closed while its message waited; there is no transcript to
    write to, and the kill proceeds."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = _busy(state.get_or_create_slot("chat-2"))
    _send(state, caller, "chat-2", "orphaned relay")
    state._slots.pop(caller.key)

    _hard_stop(state, target)

    assert list(target._queue) == []
    assert _notices(caller) == []


def test_a_hard_stop_does_not_reach_the_next_tenant_of_a_reused_key(tmp_path):
    """The identity check the drain's notice applies holds here: a closed sender's
    key recreated by a different occupant receives nothing, excerpt included."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = _busy(state.get_or_create_slot("chat-2"))
    _send(state, caller, "chat-2", "the previous tenant's secret")
    del state._slots["chat-1"]
    replacement = state.get_or_create_slot("chat-1")
    assert replacement._tab_id != caller._tab_id

    _hard_stop(state, target)

    assert _notices(replacement) == []
    assert not any("previous tenant's secret" in m["content"] for m in replacement.messages)


# ── The notifier's own edges ─────────────────────────────────────────────────


def test_discard_notifier_writes_nothing_without_a_live_matching_sender(tmp_path):
    """The four non-notice cases answer False and write nothing: no stamp, a
    self-send, a closed sender and a key held by a different tab."""
    state = _make_state(tmp_path)
    sender = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    cause = sc.SEND_DISCARD_QUEUE_CANCEL

    def _notify(meta):
        return sc.notify_send_origin_discarded(
            state, entry_meta=meta, target_slot=target, text="t", cause=cause
        )

    assert _notify(None) is False
    assert _notify({}) is False
    assert _notify(sc.send_origin_meta(state, target.key)) is False
    assert _notify({sc.SEND_ORIGIN_META_KEY: {"slot": "chat-gone", "tab": sender._tab_id}}) is False
    assert _notify({sc.SEND_ORIGIN_META_KEY: {"slot": sender.key, "tab": "another-tab"}}) is False
    assert _notify({sc.SEND_ORIGIN_META_KEY: {"slot": sender.key}}) is False
    assert _notices(sender) == []

    assert _notify(sc.send_origin_meta(state, sender.key)) is True
    assert len(_notices(sender)) == 1


def test_an_unknown_discard_cause_writes_nothing_and_does_not_raise(tmp_path):
    """The discard has already happened; a reporting fault must not surface as a
    failed cancel or stop, and must not write a notice with no reason in it."""
    state = _make_state(tmp_path)
    sender = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")

    assert (
        sc.notify_send_origin_discarded(
            state,
            entry_meta=sc.send_origin_meta(state, sender.key),
            target_slot=target,
            text="t",
            cause="not-a-cause",
        )
        is False
    )
    assert _notices(sender) == []
