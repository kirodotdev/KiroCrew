"""A steer that cannot be injected at send time waits for the next tool boundary.

``session_send(steer=True)`` and ``session_broadcast(mode=steer)`` never leave an
urgent message in the end-of-turn queue while the target is mid-turn and injection is
unavailable at that instant. The gateway holds it as a PENDING STEER, retries at every
tool-call boundary of the same turn, and only when the turn ends first hands it to the
end-of-turn path, which starts the next turn at once.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from chat_test_helpers import _make_state
from session_control_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _enabled_fixture,
    _fresh_create_budget,
    _fresh_stop_windows,
    _key,
    _peer_target,
    _slot,
    _steerable,
)

from kiro_crew.crew_log import emit
from kiro_crew.crew_log import session_tree_projection as stp
from kiro_crew.crew_log.session_tree import OpenedRecord
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard import session_control as sc

TEXT = "stop, the base moved; rebase before you push"
#: How long a test awaits a task it expects to finish; only a hang ever waits it out.
_AWAIT_SECS = 30


def _running(slot):
    """A slot whose turn is in flight, ended by cancelling ``slot.task``."""
    slot.task = asyncio.get_running_loop().create_future()
    return slot


@pytest.fixture(name="_tree")
def _tree_fixture(tmp_path, monkeypatch):
    """The crew-log fold ``created_session_status`` reads, in memory only."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crewhome"))
    monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
    monkeypatch.setattr(
        "kiro_crew.executors.maintenance_executor",
        lambda: type("_NoPool", (), {"submit": staticmethod(lambda *a, **k: None)}),
    )
    stp.reset_for_tests()
    stp.projection().ensure_seeded()
    yield
    stp.reset_for_tests()


async def _send(state, caller, target="chat-2", message=TEXT):
    return await sc.send_to_target(
        state, caller_session_key=_key(caller), target=target, message=message, steer=True
    )


@pytest.mark.asyncio
async def test_an_unavailable_steer_is_injected_at_the_next_tool_boundary(tmp_path):
    """(a) No client at send time: held, not queued; the boundary injects it, once."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _running(_peer_target(state, "chat-2", caller))

    out = await _send(state, caller)

    assert out.get("pending_steer") is True, "the send must report a pending steer"
    from kiro_crew.dashboard import deferred_steer

    assert out["steered"] is False
    assert not target._queue, "a steer must never be a plain end-of-turn queue entry"

    # The first boundary still has no client: it stays pending and nothing is queued.
    await deferred_steer.flush_at_boundary(state, target)
    assert not target._queue

    # The turn now has a steer-capable client: the NEXT boundary delivers it.
    target._acp_client = _steerable(accepted=True)
    await deferred_steer.flush_at_boundary(state, target)

    target._acp_client.steer.assert_awaited_once()
    injected = target._acp_client.steer.await_args.args[0]
    assert TEXT in injected and injected.startswith("[sent by session ")
    assert not target._queue


@pytest.mark.asyncio
async def test_a_steer_whose_turn_ends_first_is_queued_and_the_next_turn_starts_at_once(tmp_path):
    """(b) The turn ends before any boundary delivered it: end-of-turn path, no idle wait."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _running(_peer_target(state, "chat-2", caller))

    out = await _send(state, caller)
    assert out.get("pending_steer") is True

    run_chat = MagicMock(return_value=MagicMock())
    try:
        with (
            patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()) as spawn,
            patch.object(chat_runner, "_run_chat", new=run_chat),
        ):
            await chat_runner._hand_off_queue(
                state, target, drain=True, allow_automatic_successor=False
            )
    finally:
        target.task.cancel()

    assert spawn.call_count == 1, "the next turn must start in the same hand-off"
    assert any(TEXT in str(call) for call in run_chat.call_args_list), "and it runs the steer text"
    assert not target._queue, "the entry was consumed by the started turn, not left waiting"


@pytest.mark.asyncio
async def test_a_resent_steer_is_delivered_exactly_once(tmp_path):
    """(c) A re-send of the same steer, held or still going into this turn, delivers once."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _running(_peer_target(state, "chat-2", caller))

    first = await _send(state, caller)
    second = await _send(state, caller)

    assert first.get("pending_steer") is True
    assert second.get("pending_steer") is True
    from kiro_crew.dashboard import deferred_steer

    assert second["steer_id"] == first["steer_id"], "the duplicate must map to the same steer"
    assert not target._queue

    target._acp_client = _steerable(accepted=True)
    await deferred_steer.flush_at_boundary(state, target)
    await deferred_steer.flush_at_boundary(state, target)
    assert target._acp_client.steer.await_count == 1, "delivered exactly once"

    # Settled (not held) but still pending inside this turn: the in-flight twin check
    # answers it as the same steer on its way.
    third = await _send(state, caller)
    assert third["steer_id"] == first["steer_id"], "a late retry is recognised as the same steer"
    assert third["pending_steer"] is False and third["steer_state"] == "steering", third
    assert target._acp_client.steer.await_count == 1


@pytest.mark.asyncio
async def test_the_reply_says_pending_steer_and_the_status_later_says_delivered(tmp_path, _tree):
    """(d) Reply text first, then ``delivered@<ts>`` on the target's status row."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _running(_peer_target(state, "chat-2", caller))
    target._created_by = caller.key
    proj = stp.projection()
    proj.apply(OpenedRecord(sid="sid-chat-1", slot="chat-1", created_at=1))
    proj.apply(OpenedRecord(sid="sid-chat-2", slot="chat-2", created_at=2, parent_slot="chat-1"))

    out = await _send(state, caller)
    assert out.get("steer_state") == "pending-steer"
    from kiro_crew.dashboard import deferred_steer

    status = await sc.created_session_status(state, caller_session_key=_key(caller))
    row = {r["target"]: r for r in status["sessions"]}[target.key]
    assert row["steer_receipts"] == [{"steer_id": out["steer_id"], "state": "pending-steer"}]

    target._acp_client = _steerable(accepted=True)
    await deferred_steer.flush_at_boundary(state, target)

    status = await sc.created_session_status(state, caller_session_key=_key(caller))
    row = {r["target"]: r for r in status["sessions"]}[target.key]
    receipt = row["steer_receipts"][0]
    assert receipt["steer_id"] == out["steer_id"]
    assert receipt["state"].startswith("delivered@")


@pytest.mark.asyncio
async def test_a_loss_recovery_backend_gets_the_peer_steer_at_the_boundary(tmp_path):
    """(e) A claude/codex-type client refuses a peer steer at send time by default.

    ``steer_needs_loss_recovery`` plus ``peer_steer_loss_recovery`` off means the
    send-time attempt answers unavailable. The held steer must still be injected at
    the next boundary, registered pending so a turn that drops it requeues it.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _running(_peer_target(state, "chat-2", caller))
    client = _steerable(accepted=True)
    client.steer_needs_loss_recovery = True
    target._acp_client = client

    out = await _send(state, caller)

    assert out.get("pending_steer") is True, "refused at send time, so held rather than queued"
    from kiro_crew.dashboard import deferred_steer

    client.steer.assert_not_awaited()
    assert not target._queue

    await deferred_steer.flush_at_boundary(state, target)

    client.steer.assert_awaited_once()
    assert any(TEXT in text for text in target._pending_steers), "registered, so it is tracked"
    assert not target._queue


def _loss_recovery_target(state):
    caller = _slot(state, "chat-1")
    target = _running(_peer_target(state, "chat-2", caller))
    client = _steerable(accepted=True)
    client.steer_needs_loss_recovery = True
    target._acp_client = client
    return caller, target, client


async def _hand_off(state, target):
    """End the target's turn the way ``_hand_off_queue`` does; return the spawn mock."""
    with (
        patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()) as spawn,
        patch.object(chat_runner, "_run_chat", new=MagicMock(return_value=MagicMock())),
    ):
        try:
            await chat_runner._hand_off_queue(
                state, target, drain=True, allow_automatic_successor=False
            )
        finally:
            if target.task is not None:
                target.task.cancel()
    return spawn


@pytest.mark.asyncio
async def test_an_injected_peer_steer_does_not_run_again_from_the_queue(tmp_path, monkeypatch):
    """(f) NO DOUBLE RUN: injected at the boundary, then the turn ends unconsumed.

    A loss-recovery harness may have acted on the steer before it dropped it, so the
    teardown must not queue it back: marked delivered before the injection, the
    requeue skips it and no next turn starts for it.
    """
    state = _make_state(tmp_path)
    caller, target, client = _loss_recovery_target(state)

    out = await _send(state, caller)
    assert out.get("pending_steer") is True
    from kiro_crew.dashboard import deferred_steer

    await deferred_steer.flush_at_boundary(state, target)
    client.steer.assert_awaited_once()
    assert target._deferred_steer_delivered, "marked delivered when it was injected"

    spawn = await _hand_off(state, target)

    assert spawn.call_count == 0, "no second run from the queue"
    assert not target._queue
    assert not target._pending_steers


@pytest.mark.asyncio
async def test_a_steer_whose_injection_fails_stays_pending_and_is_not_lost(tmp_path):
    """(g) No silent loss before delivery: a refused injection clears the mark.

    The client answers "not accepted", so the steer stays held with no delivered
    mark, and the turn ending then hands it to the queue and starts the next turn.
    """
    state = _make_state(tmp_path)
    caller, target, client = _loss_recovery_target(state)
    client.steer.return_value = False

    out = await _send(state, caller)
    assert out.get("pending_steer") is True
    from kiro_crew.dashboard import deferred_steer

    await deferred_steer.flush_at_boundary(state, target)

    assert not target._deferred_steer_delivered, "the mark is cleared when injection fails"
    assert len(target._deferred_steers) == 1

    spawn = await _hand_off(state, target)

    assert spawn.call_count == 1, "the held steer runs as the next turn"


@pytest.mark.asyncio
async def test_an_earlier_row_of_the_same_text_does_not_turn_a_delivery_into_a_requeue(tmp_path):
    """(h) The delivery id is fresh per attempt, never the dedup hash.

    A transcript row that carries the hash as its steer delivery id would otherwise
    read as "already persisted" and report ``queued@`` for a steer the client took.
    """
    state = _make_state(tmp_path)
    caller, target, client = _loss_recovery_target(state)

    out = await _send(state, caller)
    assert out.get("pending_steer") is True
    from kiro_crew.dashboard import deferred_steer

    target.messages.append(
        {"role": "user", "content": "earlier", "meta": {"steer_delivery_id": out["steer_id"]}}
    )
    await deferred_steer.flush_at_boundary(state, target)

    assert target._deferred_steer_receipts[out["steer_id"]]["state"].startswith("delivered@")


@pytest.mark.asyncio
async def test_a_steer_sent_during_the_hand_off_is_queued_not_stranded(tmp_path):
    """(i) A steer that arrives after the hand-off took its snapshot is not held.

    Held at that point it would sit in the held list while the slot went idle.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _running(_peer_target(state, "chat-2", caller))

    first = await _send(state, caller)
    assert first.get("pending_steer") is True
    from kiro_crew.dashboard import deferred_steer

    real = deferred_steer.requeue_all
    late: dict = {}

    async def _requeue_then_a_late_send(state_, slot_):
        await real(state_, slot_)
        late.update(await _send(state, caller, message="and rebase onto the new base"))

    with patch.object(deferred_steer, "requeue_all", _requeue_then_a_late_send):
        spawn = await _hand_off(state, target)

    assert late.get("pending_steer") is not True, "not held after the hand-off began"
    assert not target._deferred_steers, "nothing is stranded in the held list"
    assert spawn.call_count == 1


@pytest.mark.asyncio
async def test_a_cancelled_injection_is_not_queued_back(tmp_path):
    """(j) A cancelled attempt may have written the steer: marked, it never runs twice."""
    state = _make_state(tmp_path)
    caller, target, client = _loss_recovery_target(state)
    client.steer.side_effect = asyncio.CancelledError

    out = await _send(state, caller)
    assert out.get("pending_steer") is True
    from kiro_crew.dashboard import deferred_steer

    with pytest.raises(asyncio.CancelledError):
        await deferred_steer.flush_at_boundary(state, target)
    spawn = await _hand_off(state, target)

    assert spawn.call_count == 0, "the marked steer is settled, not queued"
    assert not target._queue


@pytest.mark.asyncio
async def test_two_overlapping_sends_of_one_steer_hold_one_entry(tmp_path):
    """(k) Both sends pass the first check before either holds: one entry results."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _running(_peer_target(state, "chat-2", caller))

    first, second = await asyncio.gather(_send(state, caller), _send(state, caller))

    assert first.get("pending_steer") is True and second.get("pending_steer") is True
    assert len(target._deferred_steers) == 1
    assert first["steer_id"] == second["steer_id"]


@pytest.mark.asyncio
async def test_a_delivered_mark_is_released_once_its_steer_is_consumed(tmp_path):
    """(l) The marks never outgrow the pending list."""
    state = _make_state(tmp_path)
    caller, target, client = _loss_recovery_target(state)

    out = await _send(state, caller)
    assert out.get("pending_steer") is True
    from kiro_crew.dashboard import deferred_steer

    await deferred_steer.flush_at_boundary(state, target)
    assert target._deferred_steer_delivered
    target._pending_steers.clear()
    await deferred_steer.flush_at_boundary(state, target)

    assert not target._deferred_steer_delivered


@pytest.mark.asyncio
async def test_the_boundary_flush_runs_as_one_task_and_does_not_block_the_loop(tmp_path):
    """(m) The stream loop only schedules the flush: it must keep reading."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _running(_peer_target(state, "chat-2", caller))

    out = await _send(state, caller)
    assert out.get("pending_steer") is True
    from kiro_crew.dashboard import deferred_steer

    release = asyncio.Event()

    async def _slow_steer(_text):
        await release.wait()
        return True

    client = _steerable(accepted=True)
    client.steer = MagicMock(side_effect=_slow_steer)
    target._acp_client = client

    deferred_steer.schedule_flush(state, target)
    task = target._deferred_flush_task
    await asyncio.sleep(0)
    deferred_steer.schedule_flush(state, target)

    assert not task.done(), "schedule_flush returned while the steer was still in flight"
    assert target._deferred_flush_task is task, "single flight: no second task"
    release.set()
    await asyncio.wait_for(task, _AWAIT_SECS)
    assert client.steer.call_count == 1


@pytest.mark.asyncio
async def test_the_tighter_ownership_fence_wins_at_the_boundary(tmp_path, monkeypatch):
    """(n) The fence read at send time is stale by the time the boundary runs."""
    sm = sc  # the facade: a name several owners bind is patched through it, never on one owner

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _running(_peer_target(state, "chat-2", caller))
    target._created_by = caller.key

    out = await sc.send_to_target(
        state,
        caller_session_key=_key(caller),
        target="chat-2",
        message=TEXT,
        steer=True,
        caller_fenced=True,
    )
    assert out.get("pending_steer") is True
    from kiro_crew.dashboard import deferred_steer

    seen: list = []
    real = sm.authorize_target

    def _spy(*args, **kwargs):
        seen.append(kwargs.get("precomputed_ownership_fenced"))
        return real(*args, **kwargs)

    monkeypatch.setattr(sm, "authorize_target", _spy)
    monkeypatch.setattr(sm, "_caller_is_ownership_fenced", lambda *_a: False)
    target._acp_client = _steerable(accepted=True)
    await deferred_steer.flush_at_boundary(state, target)

    assert seen == [True], "fenced at send time stays fenced even if the boundary read is looser"


@pytest.mark.asyncio
async def test_two_unidentified_senders_with_one_text_are_not_one_steer(tmp_path, monkeypatch):
    """(o) No caller identity, no dedup: the ``unknown`` bucket must not merge senders."""
    sm = sc  # the facade: a name several owners bind is patched through it, never on one owner

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _running(_peer_target(state, "chat-2", caller))
    monkeypatch.setattr(sm, "caller_slot_key", lambda *_a: "")
    monkeypatch.setattr(sm, "authorize_target", lambda *_a, **_k: target)

    first = await _send(state, caller)
    second = await _send(state, caller)

    assert first.get("pending_steer") is True and second.get("pending_steer") is True
    assert first["steer_id"] != second["steer_id"]
    assert len(target._deferred_steers) == 2


@pytest.mark.asyncio
async def test_a_hard_stop_discards_held_steers_and_tells_their_senders(tmp_path, monkeypatch):
    """(p) A hard stop owes a held steer what it owes a queued one: a notice and a receipt."""
    from unittest.mock import AsyncMock

    from kiro_crew.dashboard import chat_handlers as ch

    state = _make_state(tmp_path)
    fake = MagicMock()
    for module in (sc, ch):
        monkeypatch.setattr(module, "sel", lambda fake=fake: fake)
    monkeypatch.setattr(sc, "_sel_off_loop", lambda write, what: write())
    caller = _slot(state, "chat-1")
    target = _running(_peer_target(state, "chat-2", caller))

    out = await _send(state, caller)
    assert out.get("pending_steer") is True

    state.sessions.stop_turn = AsyncMock(return_value="cancelled")
    await ch.stop_slot_turn(state, target)
    await ch.stop_slot_turn(state, target)

    assert not target._deferred_steers
    assert target._deferred_steer_receipts[out["steer_id"]]["state"].startswith("discarded@")
    notices = [m["content"] for m in caller.messages if m.get("role") == "notice"]
    assert len(notices) == 1 and "hard stop" in notices[0]


@pytest.mark.asyncio
async def test_a_failure_at_a_boundary_keeps_the_steer_held(tmp_path, monkeypatch):
    """(q) An error from the attempt is not a refusal: the steer stays for the next boundary."""
    sm = sc  # the facade: a name several owners bind is patched through it, never on one owner

    state = _make_state(tmp_path)
    caller, target, client = _loss_recovery_target(state)

    out = await _send(state, caller)
    assert out.get("pending_steer") is True
    from kiro_crew.dashboard import deferred_steer

    real = sm.containment_meta

    def _fails(*_a, **_k):
        raise RuntimeError("the containment read failed")

    monkeypatch.setattr(sm, "containment_meta", _fails)
    await deferred_steer.flush_at_boundary(state, target)

    assert len(target._deferred_steers) == 1, "still held"
    assert not target._deferred_steer_delivered, "and not marked delivered"
    monkeypatch.setattr(sm, "containment_meta", real)
    await deferred_steer.flush_at_boundary(state, target)
    assert not target._deferred_steers, "delivered at the next boundary"


@pytest.mark.asyncio
async def test_a_steer_sent_during_a_hand_off_with_nothing_held_is_not_held(tmp_path):
    """(r) Admission closes at the start of every hand-off, not only when steers are held."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _running(_peer_target(state, "chat-2", caller))
    probe = await _send(state, caller)
    assert probe.get("pending_steer") is True, "holding works at all"
    target._deferred_steers.clear()
    target._deferred_steer_receipts.clear()
    target.queue_append("a queued user message")
    late: dict = {}

    async def _late_send_then_start(*_a, **_k):
        late.update(await _send(state, caller, message="and rebase onto the new base"))
        return True

    with (
        patch.object(chat_runner, "_start_next_queued_turn", _late_send_then_start),
        patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()),
    ):
        try:
            await chat_runner._hand_off_queue(
                state, target, drain=True, allow_automatic_successor=False
            )
        finally:
            if target.task is not None:
                target.task.cancel()

    assert late.get("pending_steer") is not True
    assert not target._deferred_steers, "nothing is stranded in the held list"
    assert any("rebase onto the new base" in q.get("content", "") for q in target._queue)


@pytest.mark.asyncio
async def test_an_attempt_that_a_hard_stop_discarded_does_not_inject(tmp_path, monkeypatch):
    """(s) A discard during the attempt's gate wins: no injection, receipt stays discarded."""
    sm = sc  # the facade: a name several owners bind is patched through it, never on one owner

    state = _make_state(tmp_path)
    caller, target, client = _loss_recovery_target(state)
    out = await _send(state, caller)
    assert out.get("pending_steer") is True
    from kiro_crew.dashboard import deferred_steer

    async def _discard_while_gating():
        deferred_steer.discard_all(target, lambda _held: None)

    monkeypatch.setattr(sm, "prewarm_enabled_check", _discard_while_gating)
    await deferred_steer.flush_at_boundary(state, target)

    client.steer.assert_not_awaited()
    assert target._deferred_steer_receipts[out["steer_id"]]["state"].startswith("discarded@")


@pytest.mark.asyncio
async def test_an_accepted_steer_that_races_a_stop_keeps_its_delivered_mark(tmp_path):
    """(t) A REQUEUED answer after the client took the text is ambiguous: the mark stays."""
    state = _make_state(tmp_path)
    caller, target, client = _loss_recovery_target(state)
    out = await _send(state, caller)
    assert out.get("pending_steer") is True
    from kiro_crew.dashboard import deferred_steer

    async def _accepted_while_a_stop_lands(_text):
        target._stop_generation += 1
        return True

    client.steer = MagicMock(side_effect=_accepted_while_a_stop_lands)
    await deferred_steer.flush_at_boundary(state, target)

    assert target._deferred_steer_delivered, "the teardown requeue still skips it"
    assert target._deferred_steer_receipts[out["steer_id"]]["state"].startswith("delivered@")


def test_the_mcp_status_line_shows_steer_receipts():
    """(u) The sender can read the receipt ids and states the send reply promised."""
    from kiro_crew.mcp_dashboard_session_runs import _status_activity_suffix

    line = _status_activity_suffix(
        {"steer_receipts": [{"steer_id": "ab12cd34ef56ab12", "state": "pending-steer"}]}
    )

    assert "ab12cd34ef56ab12 pending-steer" in line


@pytest.mark.asyncio
async def test_a_caller_unfenced_at_send_time_is_fenced_when_the_boundary_says_so(
    tmp_path, monkeypatch
):
    """(v) Tightest wins in both directions: fenced at either moment means fenced."""
    sm = sc  # the facade: a name several owners bind is patched through it, never on one owner

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _running(_peer_target(state, "chat-2", caller))
    target._created_by = caller.key

    out = await sc.send_to_target(
        state,
        caller_session_key=_key(caller),
        target="chat-2",
        message=TEXT,
        steer=True,
        caller_fenced=False,
    )
    assert out.get("pending_steer") is True
    from kiro_crew.dashboard import deferred_steer

    seen: list = []
    real = sm.authorize_target

    def _spy(*args, **kwargs):
        seen.append(kwargs.get("precomputed_ownership_fenced"))
        return real(*args, **kwargs)

    monkeypatch.setattr(sm, "authorize_target", _spy)
    monkeypatch.setattr(sm, "_caller_is_ownership_fenced", lambda *_a: True)
    target._acp_client = _steerable(accepted=True)
    await deferred_steer.flush_at_boundary(state, target)

    assert seen == [True]


@pytest.mark.asyncio
async def test_a_cancelled_attempt_on_a_keep_every_steer_harness_queues_once(tmp_path, monkeypatch):
    """(w) Unmarked path: the cancelled RPC left its text pending AND the entry held.

    The teardown requeue queues the pending text; the held entry must not queue it
    again, or the steer runs twice.
    """
    from kiro_crew.dashboard import deferred_steer

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _running(_peer_target(state, "chat-2", caller))
    out = await _send(state, caller)
    assert out.get("pending_steer") is True

    started = asyncio.Event()

    async def _never_returns(_text):
        started.set()
        await asyncio.Event().wait()

    client = _steerable(accepted=True)
    client.steer_needs_loss_recovery = False
    client.steer = MagicMock(side_effect=_never_returns)
    target._acp_client = client
    monkeypatch.setattr(
        deferred_steer, "FLUSH_SETTLE_SECS", 0.05
    )  # flake-ok: the steer never returns, so the wait always expires

    deferred_steer.schedule_flush(state, target)
    await asyncio.wait_for(started.wait(), _AWAIT_SECS)
    task = target._deferred_flush_task
    task.cancel()
    await asyncio.wait({task}, timeout=_AWAIT_SECS)
    assert any(TEXT in text for text in target._pending_steers), "registered pending"
    assert not target._deferred_steers, "the held entry is settled: one path queues it"
    run_chat = MagicMock(return_value=MagicMock())
    try:
        with (
            patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()),
            patch.object(chat_runner, "_run_chat", new=run_chat),
        ):
            await chat_runner._hand_off_queue(
                state, target, drain=True, allow_automatic_successor=False
            )
    finally:
        if target.task is not None:
            target.task.cancel()

    texts = [str(call.args[2]) for call in run_chat.call_args_list]
    assert "".join(texts).count(TEXT) == 1, "the steer runs once"


@pytest.mark.asyncio
async def test_a_hard_stop_during_the_end_of_turn_queueing_wins(tmp_path, monkeypatch):
    """(x) A discard that lands while requeue_all awaits is not undone by the queue write."""
    from kiro_crew.dashboard import deferred_steer

    sm = sc  # the facade: a name several owners bind is patched through it, never on one owner

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _running(_peer_target(state, "chat-2", caller))
    out = await _send(state, caller)
    assert out.get("pending_steer") is True

    async def _stop_lands_during_the_gate():
        deferred_steer.discard_all(target, lambda _held: None)

    monkeypatch.setattr(sm, "prewarm_enabled_check", _stop_lands_during_the_gate)
    await deferred_steer.requeue_all(state, target)

    assert not target._queue, "a discarded steer is not queued"
    assert target._deferred_steer_receipts[out["steer_id"]]["state"].startswith("discarded@")


@pytest.mark.asyncio
async def test_a_broadcast_row_for_a_held_duplicate_is_that_pending_steer(tmp_path):
    """(y) A broadcast re-send of a held steer is that steer; the tool row says so."""
    from kiro_crew.mcp_dashboard import TABLE
    from kiro_crew.mcp_tools.dashboard_client import InMemoryDashboardClient
    from kiro_crew.mcp_tools.table import Caller, ToolContext

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _running(_peer_target(state, "chat-2", caller))
    target._created_by = caller.key
    first = await _send(state, caller)
    assert first.get("pending_steer") is True

    result = await sc.broadcast_to_targets(
        state,
        caller_session_key=_key(caller),
        message=TEXT,
        mode="steer",
        targets=["chat-2"],
    )
    row = result["results"][0]
    assert row["steer_id"] == first["steer_id"] and row["pending_steer"] is True
    assert len(target._deferred_steers) == 1, "the re-send is not held a second time"

    dash = InMemoryDashboardClient(
        {"POST /api/session-control/broadcast": {**result, "mode": "steer"}}
    )
    text = TABLE.call(
        "session_broadcast",
        {"message": TEXT, "mode": "steer", "targets": ["chat-2"]},
        ToolContext(dash, Caller.strict("dashboard:chat-1-100")),
    )
    assert "pending-steer " + first["steer_id"] in text, text

    # The one not-pending duplicate: the same steer still on its way into the turn.
    on_its_way = {**row, "pending_steer": False, "steer_state": "steering"}
    dash = InMemoryDashboardClient(
        {
            "POST /api/session-control/broadcast": {
                **result,
                "results": [on_its_way],
                "mode": "steer",
            }
        }
    )
    text = TABLE.call(
        "session_broadcast",
        {"message": TEXT, "mode": "steer", "targets": ["chat-2"]},
        ToolContext(dash, Caller.strict("dashboard:chat-1-100")),
    )
    assert "already on its way into its running turn; nothing was sent again" in text, text


def test_a_held_steer_is_a_pending_steer_receipt_not_a_queued_one():
    """(z) The outgoing card never reads a held steer as queued."""
    from kiro_crew.dashboard import session_send_receipts as receipts

    assert receipts.outcome_of({"pending_steer": True}) == "pending-steer"
    assert "pending-steer" in receipts.OUTCOMES


@pytest.mark.asyncio
async def test_an_unconfirmed_injected_steer_tells_its_sender_when_the_turn_ends(tmp_path):
    """(ab) At most once is not silent: the sender hears the steer may not have run."""
    state = _make_state(tmp_path)
    caller, target, client = _loss_recovery_target(state)
    out = await _send(state, caller)
    assert out.get("pending_steer") is True
    from kiro_crew.dashboard import deferred_steer

    await deferred_steer.flush_at_boundary(state, target)
    spawn = await _hand_off(state, target)

    assert spawn.call_count == 0, "still not queued back"
    notices = [m["content"] for m in caller.messages if m.get("role") == "notice"]
    assert len(notices) == 1 and "may not have run" in notices[0]
    assert target._deferred_steer_receipts[out["steer_id"]]["state"].startswith("unconfirmed@")


@pytest.mark.asyncio
async def test_a_hard_stop_after_an_injection_tells_the_sender_with_the_key_off(
    tmp_path, monkeypatch
):
    """(ac) An injected, unconfirmed steer is discarded by a hard stop: not silently."""
    from unittest.mock import AsyncMock

    from kiro_crew.dashboard import chat_handlers as ch

    state = _make_state(tmp_path)
    fake = MagicMock()
    for module in (sc, ch):
        monkeypatch.setattr(module, "sel", lambda fake=fake: fake)
    monkeypatch.setattr(sc, "_sel_off_loop", lambda write, what: write())
    caller, target, _client = _loss_recovery_target(state)
    out = await _send(state, caller)
    assert out.get("pending_steer") is True
    from kiro_crew.dashboard import deferred_steer

    await deferred_steer.flush_at_boundary(state, target)
    assert target._deferred_steer_receipts[out["steer_id"]]["state"].startswith("delivered@")

    state.sessions.stop_turn = AsyncMock(return_value="cancelled")
    await ch.stop_slot_turn(state, target)
    await ch.stop_slot_turn(state, target)

    notices = [m["content"] for m in caller.messages if m.get("role") == "notice"]
    assert len(notices) == 1, "the sender hears that the steer was discarded"
    assert target._deferred_steer_receipts[out["steer_id"]]["state"].startswith("unconfirmed@")


@pytest.mark.asyncio
@pytest.mark.parametrize("settled_by", ["delivered", "queued"])
async def test_a_resend_to_a_target_that_went_idle_starts_a_turn(tmp_path, monkeypatch, settled_by):
    """(ad) Only a HELD steer is "the same steer": once it settled, an idle target runs a re-send."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _running(_peer_target(state, "chat-2", caller))
    first = await _send(state, caller)
    assert first.get("pending_steer") is True
    from kiro_crew.dashboard import deferred_steer

    if settled_by == "delivered":
        target._acp_client = _steerable(accepted=True)
        await deferred_steer.flush_at_boundary(state, target)
    else:
        with (
            patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()),
            patch.object(chat_runner, "_run_chat", new=MagicMock(return_value=MagicMock())),
        ):
            await chat_runner._hand_off_queue(
                state, target, drain=True, allow_automatic_successor=False
            )
    assert target._deferred_steer_receipts[first["steer_id"]]["state"].startswith(settled_by)
    target.task.cancel()
    target.task = None
    target._queue.clear()

    async def _fake_run_chat(_state, _slot, _prompt):
        return None

    monkeypatch.setattr("kiro_crew.dashboard.chat_runner._run_chat", _fake_run_chat)

    again = await _send(state, caller)
    # flake-ok: one yield lets the started turn task run, as the idle-target test in test_session_control_send does
    await asyncio.sleep(0)

    assert again["started"] is True, again
    assert "steer_state" not in again, "a settled steer is never answered as already sent"


def test_status_receipts_list_the_newest_settled_steer_first():
    """(ae) The status line cuts the list; the newest receipt must not be what is cut."""
    from kiro_crew.dashboard import deferred_steer

    slot = MagicMock()
    slot._deferred_steers = []
    slot._deferred_steer_receipts = {f"s{n}": {"state": f"delivered@{n}"} for n in range(10)}

    assert [r["steer_id"] for r in deferred_steer.receipts(slot)][:2] == ["s9", "s8"]


@pytest.mark.asyncio
async def test_a_hard_stop_during_the_stale_turn_fallback_queues_nothing(tmp_path, monkeypatch):
    """(af) The rollover fallback honours a discard that lands while its gate awaits."""
    state = _make_state(tmp_path)
    caller, target, _client = _loss_recovery_target(state)
    out = await _send(state, caller)
    assert out.get("pending_steer") is True
    from kiro_crew.dashboard import deferred_steer

    target._turn_generation += 1

    async def _discard_while_gating():
        deferred_steer.discard_all(target, lambda _held: None)

    monkeypatch.setattr(sc, "prewarm_enabled_check", _discard_while_gating)
    await deferred_steer.flush_at_boundary(state, target)

    assert not target._queue, "a steer reported as discarded must not be queued"
    assert target._deferred_steer_receipts[out["steer_id"]]["state"].startswith("discarded@")


@pytest.mark.asyncio
async def test_a_replacement_of_a_discarded_steer_is_not_injected_by_the_old_attempt(
    tmp_path, monkeypatch
):
    """(ag) A discard plus a re-send of the same text leaves a new entry; the old attempt skips it."""
    state = _make_state(tmp_path)
    caller, target, client = _loss_recovery_target(state)
    out = await _send(state, caller)
    assert out.get("pending_steer") is True
    from kiro_crew.dashboard import deferred_steer

    resent = []

    async def _discard_and_resend_while_gating():
        if resent:
            return
        resent.append(True)
        deferred_steer.discard_all(target, lambda _held: None)
        again = await _send(state, caller)
        assert again.get("pending_steer") is True and again["steer_id"] == out["steer_id"]

    monkeypatch.setattr(sc, "prewarm_enabled_check", _discard_and_resend_while_gating)
    await deferred_steer.flush_at_boundary(state, target)

    client.steer.assert_not_awaited()
    assert len(target._deferred_steers) == 1, "the replacement stays held for its own boundary"


@pytest.mark.asyncio
async def test_an_overlapping_identical_send_is_the_same_steer_not_a_second_one(tmp_path):
    """(ah) A send refused because its twin is mid-RPC is not held to inject again later."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _running(_peer_target(state, "chat-2", caller))
    release = asyncio.Event()
    started = asyncio.Event()

    async def _slow_steer(_text):
        started.set()
        await release.wait()
        return True

    client = _steerable(accepted=True)
    client.steer = MagicMock(side_effect=_slow_steer)
    target._acp_client = client

    first = asyncio.ensure_future(_send(state, caller))
    try:
        # The twin is really inside its RPC before the second send starts; a bare
        # number of loop yields is not enough on a loaded runner.
        await asyncio.wait_for(started.wait(), _AWAIT_SECS)
        second = await _send(state, caller)
        pending, held = second.get("pending_steer"), list(target._deferred_steers)
    finally:
        release.set()
        await asyncio.wait_for(first, _AWAIT_SECS)

    assert pending is not True, "the twin in flight must not be held again"
    assert not held
    from kiro_crew.dashboard import deferred_steer

    await deferred_steer.flush_at_boundary(state, target)
    assert client.steer.call_count == 1, "the text ran once"


@pytest.mark.asyncio
async def test_a_steer_the_harness_took_as_its_turn_ended_is_not_run_again(tmp_path):
    """(ai) Written but unconfirmed counts as may-have-run: no requeue, the sender is told."""
    from unittest.mock import AsyncMock

    state = _make_state(tmp_path)
    caller, target, client = _loss_recovery_target(state)
    out = await _send(state, caller)
    assert out.get("pending_steer") is True
    from kiro_crew.dashboard import deferred_steer

    async def _injected_then_the_turn_was_over(text):
        # What a claude client reports for a steer the adapter answered `injected` after
        # its turn ended: False to the caller, the text kept as unresolved.
        client.held_steers_possibly_delivered = frozenset({text.strip()})
        return False

    client.steer = AsyncMock(side_effect=_injected_then_the_turn_was_over)

    await deferred_steer.flush_at_boundary(state, target)
    assert target._deferred_steer_delivered, "the delivered mark survives an unresolved write"

    spawn = await _hand_off(state, target)

    assert spawn.call_count == 0, "no second run from the queue"
    assert not target._queue
    notices = [m["content"] for m in caller.messages if m.get("role") == "notice"]
    assert len(notices) == 1 and "may not have run" in notices[0]
    assert target._deferred_steer_receipts[out["steer_id"]]["state"].startswith("unconfirmed@")


@pytest.mark.asyncio
async def test_a_shared_runtime_steer_injected_as_its_turn_ended_is_not_run_again(tmp_path):
    """(ai2) As (ai), through a real codex session handle behind its provider."""
    from test_codex_steering_request import _handle as _codex_handle
    from test_codex_steering_request import _Runtime as _CodexRuntime

    from kiro_crew.acp.session_provider import AcpSessionProvider
    from kiro_crew.dashboard import deferred_steer
    from kiro_crew.testing.wait import async_wait_until, await_until

    state = _make_state(tmp_path)
    caller, target, _mock = _loss_recovery_target(state)
    rt = _CodexRuntime()
    handle = _codex_handle(rt)
    target._acp_client = AcpSessionProvider(handle, rt, session_key="k")  # type: ignore[arg-type]
    out = await _send(state, caller)
    assert out.get("pending_steer") is True

    flush = asyncio.ensure_future(deferred_steer.flush_at_boundary(state, target))
    await async_wait_until(lambda: rt.requests, describe=lambda: f"requests={rt.requests!r}")
    handle._turn_done.set()  # the terminal is read before the steering answer
    await await_until(flush, what="the boundary flush")
    rt.answer.set_result({"outcome": "injected"})
    assert target._deferred_steer_delivered, "the delivered mark survives a late injected"

    spawn = await _hand_off(state, target)

    assert spawn.call_count == 0, "no second run from the queue"
    assert not target._queue


@pytest.mark.asyncio
async def test_a_resend_of_an_unconfirmed_steer_starts_a_turn(tmp_path, monkeypatch):
    """(aj) The unconfirmed notice invites a re-send: on an idle target it starts a turn."""
    state = _make_state(tmp_path)
    caller, target, _client = _loss_recovery_target(state)
    first = await _send(state, caller)
    assert first.get("pending_steer") is True
    from kiro_crew.dashboard import deferred_steer

    await deferred_steer.flush_at_boundary(state, target)
    spawn = await _hand_off(state, target)
    assert target._deferred_steer_receipts[first["steer_id"]]["state"].startswith("unconfirmed@")
    assert spawn.call_count == 0
    target.task = None

    async def _fake_run_chat(_state, _slot, _prompt):
        return None

    monkeypatch.setattr("kiro_crew.dashboard.chat_runner._run_chat", _fake_run_chat)

    again = await _send(state, caller)
    # flake-ok: one yield lets the started turn task run, as the idle-target test in test_session_control_send does
    await asyncio.sleep(0)

    assert again["started"] is True and "steer_state" not in again, again


@pytest.mark.asyncio
async def test_with_the_recovery_key_on_an_injected_held_steer_is_still_requeued(tmp_path):
    """(ak) The operator who turned the key on wants requeue-on-loss: no delivered mark."""
    state = _make_state(tmp_path)
    caller, target, client = _loss_recovery_target(state)
    out = await _send(state, caller)
    assert out.get("pending_steer") is True
    from kiro_crew.dashboard import chat_delivery, deferred_steer

    key_on = MagicMock()
    key_on.agent.peer_steer_loss_recovery = True
    with patch.object(chat_delivery.live, "snapshot", return_value=key_on):
        await deferred_steer.flush_at_boundary(state, target)
    client.steer.assert_awaited_once()
    assert not target._deferred_steer_delivered, "no mark: the teardown requeue applies"

    spawn = await _hand_off(state, target)

    assert spawn.call_count == 1, "the dropped steer is requeued and runs once"
    notices = [m["content"] for m in caller.messages if m.get("role") == "notice"]
    assert not notices


@pytest.mark.asyncio
async def test_a_hard_stop_during_an_injection_keeps_the_resend_out(tmp_path):
    """(al) A marked held steer a hard stop discards may have run: its receipt says
    unconfirmed, and a re-send while its text is still in this turn is not held again."""
    state = _make_state(tmp_path)
    caller, target, client = _loss_recovery_target(state)
    out = await _send(state, caller)
    assert out.get("pending_steer") is True
    from kiro_crew.dashboard import deferred_steer

    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow_steer(_text):
        started.set()
        await release.wait()
        return True

    client.steer = MagicMock(side_effect=_slow_steer)
    flush = asyncio.ensure_future(deferred_steer.flush_at_boundary(state, target))
    try:
        await asyncio.wait_for(started.wait(), _AWAIT_SECS)
        deferred_steer.discard_all(target, lambda _held: None)
        receipt = target._deferred_steer_receipts[out["steer_id"]]["state"]
    finally:
        release.set()
        await asyncio.wait_for(flush, _AWAIT_SECS)

    assert receipt.startswith("unconfirmed@"), receipt
    again = await _send(state, caller)
    assert again["steer_id"] == out["steer_id"] and again["started"] is False
    assert again["pending_steer"] is False and again["steer_state"] == "steering", again
    assert not target._deferred_steers


async def _consumed_then_resent(state, caller, target, client):
    """Inject a held steer, let the turn consume it, then send the same text again.

    Returns the first and second replies. The second send arrives before another tool
    boundary, so the first steer's delivered mark is still on the slot.
    """
    from kiro_crew.dashboard import deferred_steer

    first = await _send(state, caller)
    assert first.get("pending_steer") is True
    await deferred_steer.flush_at_boundary(state, target)
    client.steer.assert_awaited_once()
    assert first["steer_id"] in target._deferred_steer_delivered
    # The running turn consumed it and the steer path confirmed it: what a confirmed
    # consume leaves is no pending text and no delivery registration.
    injected = client.steer.await_args.args[0]
    target._pending_steers.remove(injected)
    target._steer_delivery_ids.pop(injected, None)

    client.supports_steer = False
    second = await _send(state, caller)
    assert second["steer_id"] == first["steer_id"]
    assert second.get("pending_steer") is True, "a settled steer's resend is a fresh held one"
    return first, second


@pytest.mark.asyncio
async def test_a_resend_of_a_consumed_steer_is_queued_when_the_turn_ends(tmp_path):
    """(am) A consumed steer's delivered mark does not settle its resend as delivered."""
    state = _make_state(tmp_path)
    caller, target, client = _loss_recovery_target(state)
    _first, second = await _consumed_then_resent(state, caller, target, client)

    spawn = await _hand_off(state, target)

    receipt = target._deferred_steer_receipts[second["steer_id"]]["state"]
    assert receipt.startswith("queued@"), receipt
    assert spawn.call_count == 1, "the resend runs as the next turn"


@pytest.mark.asyncio
async def test_a_hard_stop_discards_a_resend_of_a_consumed_steer_as_never_run(
    tmp_path, monkeypatch
):
    """(an) The fresh resend was never injected: a hard stop reads it as discarded."""
    from unittest.mock import AsyncMock

    from kiro_crew.dashboard import chat_handlers as ch

    state = _make_state(tmp_path)
    fake = MagicMock()
    for module in (sc, ch):
        monkeypatch.setattr(module, "sel", lambda fake=fake: fake)
    monkeypatch.setattr(sc, "_sel_off_loop", lambda write, what: write())
    caller, target, client = _loss_recovery_target(state)
    _first, second = await _consumed_then_resent(state, caller, target, client)

    state.sessions.stop_turn = AsyncMock(return_value="cancelled")
    await ch.stop_slot_turn(state, target)
    await ch.stop_slot_turn(state, target)

    receipt = target._deferred_steer_receipts[second["steer_id"]]["state"]
    assert receipt.startswith("discarded@"), receipt
    notices = [m["content"] for m in caller.messages if m.get("role") == "notice"]
    assert len(notices) == 1 and "discarded before it ran" in notices[0], notices


@pytest.mark.asyncio
async def test_a_broadcast_of_an_injected_steer_still_in_flight_is_that_steer(tmp_path):
    """(ao) The same body re-sent by broadcast while the injected steer is pending.

    The broadcast's prompt carries another provenance line, so it is a different text
    with the same steer id. It is the same steer: nothing is held, and the turn's end
    does not run the injected original a second time.
    """
    from kiro_crew.dashboard import deferred_steer

    state = _make_state(tmp_path)
    caller, target, client = _loss_recovery_target(state)
    target._created_by = caller.key
    first = await _send(state, caller)
    assert first.get("pending_steer") is True
    await deferred_steer.flush_at_boundary(state, target)
    client.steer.assert_awaited_once()
    injected = client.steer.await_args.args[0]
    assert target._deferred_steer_delivered.get(first["steer_id"]) == injected
    assert injected in target._pending_steers, "still in flight: not consumed yet"

    result = await sc.broadcast_to_targets(
        state,
        caller_session_key=_key(caller),
        message=TEXT,
        mode="steer",
        targets=["chat-2"],
    )
    row = result["results"][0]
    assert row["steer_id"] == first["steer_id"], row
    assert row.get("pending_steer") is False and row.get("steer_state") == "steering", row
    assert not target._deferred_steers, "the re-send is not held"
    assert target._deferred_steer_delivered.get(first["steer_id"]) == injected
    client.steer.assert_awaited_once()

    spawn = await _hand_off(state, target)

    assert spawn.call_count == 0, "the injected original does not run again from the queue"
    assert not target._queue
    receipt = target._deferred_steer_receipts[first["steer_id"]]["state"]
    assert not receipt.startswith("queued@"), receipt


def _keep_every_target(state):
    """A running kiro-cli-like target: it keeps every steer, so none is marked delivered."""
    caller = _slot(state, "chat-1")
    target = _running(_peer_target(state, "chat-2", caller))
    target._created_by = caller.key
    client = _steerable(accepted=True)
    client.steer_needs_loss_recovery = False
    target._acp_client = client
    return caller, target, client


async def _broadcast(state, caller, target="chat-2", message=TEXT):
    result = await sc.broadcast_to_targets(
        state,
        caller_session_key=_key(caller),
        message=message,
        mode="steer",
        targets=[target],
    )
    return result["results"][0]


@pytest.mark.asyncio
async def test_a_broadcast_of_a_pending_unmarked_steer_is_that_steer(tmp_path):
    """(ap) Steered at once and still pending, unmarked: a broadcast of it is the same steer."""
    state = _make_state(tmp_path)
    caller, target, client = _keep_every_target(state)
    first = await _send(state, caller)
    assert first["steered"] is True

    row = await _broadcast(state, caller)

    assert row.get("steer_state") == "steering" and row["steered"] is False, row
    client.steer.assert_awaited_once()
    assert sum(TEXT in text for text in target._pending_steers) == 1


@pytest.mark.asyncio
async def test_a_send_of_a_pending_unmarked_broadcast_steer_is_that_steer(tmp_path):
    """(aq) The reverse order: broadcast first, then ``session_send`` of the same body."""
    state = _make_state(tmp_path)
    caller, target, client = _keep_every_target(state)
    first = await _broadcast(state, caller)
    assert first["steered"] is True

    second = await _send(state, caller)

    assert second.get("steer_state") == "steering" and second["steered"] is False, second
    client.steer.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_broadcast_of_an_injected_unmarked_held_steer_is_not_held_again(tmp_path):
    """(ar) Held, injected at a boundary without a mark, still pending: not held a second time."""
    from kiro_crew.dashboard import deferred_steer

    state = _make_state(tmp_path)
    caller, target, client = _keep_every_target(state)
    client.supports_steer = False
    first = await _send(state, caller)
    assert first["pending_steer"] is True
    client.supports_steer = True
    await deferred_steer.flush_at_boundary(state, target)
    client.steer.assert_awaited_once()
    assert not target._deferred_steer_delivered, "keeps every steer: no mark"
    client.supports_steer = False  # the immediate path is unavailable at this instant

    row = await _broadcast(state, caller)

    assert row.get("steer_state") == "steering" and row["pending_steer"] is False, row
    assert not target._deferred_steers
    client.supports_steer = True
    await deferred_steer.flush_at_boundary(state, target)
    client.steer.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_broadcast_during_the_hand_off_of_a_pending_steer_runs_it_once(tmp_path):
    """(as) The hand-off requeues the pending original; a broadcast of it then is that steer."""
    from kiro_crew.dashboard import deferred_steer

    state = _make_state(tmp_path)
    caller, target, client = _keep_every_target(state)
    await _send(state, caller)  # steered, pending, unmarked
    client.supports_steer = False
    other = await _send(state, caller, message="and also run the tests")
    assert other["pending_steer"] is True  # so the hand-off awaits requeue_all
    real = deferred_steer.requeue_all
    late: dict = {}

    async def _requeue_with_a_resend(state_, slot_):
        late["during"] = await _broadcast(state, caller)
        await real(state_, slot_)

    run_chat = MagicMock(return_value=MagicMock())
    with (
        patch.object(deferred_steer, "requeue_all", _requeue_with_a_resend),
        patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()),
        patch.object(chat_runner, "_run_chat", new=run_chat),
    ):
        try:
            await chat_runner._hand_off_queue(
                state, target, drain=True, allow_automatic_successor=False
            )
        finally:
            if target.task is not None:
                target.task.cancel()

    assert late["during"].get("steer_state") == "steering", late
    ran = "".join(str(call.args[2]) for call in run_chat.call_args_list)
    queued = "".join(str(entry.get("content", "")) for entry in target._queue)
    assert ran.count(TEXT) + queued.count(TEXT) == 1, (ran, queued)


@pytest.mark.asyncio
async def test_a_broadcast_of_a_pending_steer_with_the_recovery_key_on_is_that_steer(tmp_path):
    """(at) Loss-prone with ``peer_steer_loss_recovery`` on: no mark, still the same steer."""
    from kiro_crew.dashboard import chat_delivery

    state = _make_state(tmp_path)
    caller, target, client = _keep_every_target(state)
    client.steer_needs_loss_recovery = True
    key_on = MagicMock()
    key_on.agent.peer_steer_loss_recovery = True
    with patch.object(chat_delivery.live, "snapshot", return_value=key_on):
        first = await _send(state, caller)
        row = await _broadcast(state, caller)

    assert first["steered"] is True
    assert row.get("steer_state") == "steering" and row["steered"] is False, row
    client.steer.assert_awaited_once()


@pytest.mark.asyncio
async def test_another_senders_broadcast_of_the_same_body_is_its_own_steer(tmp_path):
    """(au) The caller is part of the steer: another sender's same body is delivered too."""
    state = _make_state(tmp_path)
    caller, target, client = _keep_every_target(state)
    other = _slot(state, "chat-3")
    first = await _send(state, caller)
    assert first["steered"] is True

    row = await _broadcast(state, other)

    assert row["steered"] is True, row
    assert client.steer.await_count == 2


@pytest.mark.asyncio
async def test_a_hard_stop_of_an_injected_unmarked_held_steer_tells_its_sender(
    tmp_path, monkeypatch
):
    """(av) Kept-every harness: injected at a boundary, unconsumed, then a hard stop.

    The text is discarded before the turn read it, so its receipt does not read
    delivered and its sender gets the one hard-stop notice, as a marked steer's does.
    """
    from unittest.mock import AsyncMock

    from kiro_crew.dashboard import chat_handlers as ch
    from kiro_crew.dashboard import deferred_steer

    state = _make_state(tmp_path)
    fake = MagicMock()
    for module in (sc, ch):
        monkeypatch.setattr(module, "sel", lambda fake=fake: fake)
    monkeypatch.setattr(sc, "_sel_off_loop", lambda write, what: write())
    caller, target, client = _keep_every_target(state)
    client.supports_steer = False
    first = await _send(state, caller)
    assert first["pending_steer"] is True
    client.supports_steer = True
    await deferred_steer.flush_at_boundary(state, target)
    assert any(TEXT in text for text in target._pending_steers), "injected, not consumed"
    assert not target._deferred_steer_delivered, "keeps every steer: no mark"

    state.sessions.stop_turn = AsyncMock(return_value="cancelled")
    await ch.stop_slot_turn(state, target)
    await ch.stop_slot_turn(state, target)

    receipt = target._deferred_steer_receipts[first["steer_id"]]["state"]
    assert receipt.startswith("unconfirmed@"), receipt
    notices = [m["content"] for m in caller.messages if m.get("role") == "notice"]
    assert len(notices) == 1, notices


def _refuse_sender(monkeypatch):
    """The gate refuses the sender from here on, as it does after a revoked grant."""

    def _refused(*_a, **_k):
        raise sc.SessionControlError("not allowed", status=403, code="not_allowed")

    monkeypatch.setattr(sc, "authorize_target", _refused)


def _sender_notices(caller):
    return [m["content"] for m in caller.messages if m.get("role") == "notice"]


@pytest.mark.asyncio
async def test_a_held_steer_refused_at_a_boundary_tells_its_sender(tmp_path, monkeypatch):
    """(aw) The sender was told pending-steer; a refusal at the boundary owes it a notice."""
    from kiro_crew.dashboard import deferred_steer

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _running(_peer_target(state, "chat-2", caller))
    out = await _send(state, caller)
    assert out.get("pending_steer") is True

    _refuse_sender(monkeypatch)
    target._acp_client = _steerable(accepted=True)
    await deferred_steer.flush_at_boundary(state, target)

    target._acp_client.steer.assert_not_awaited()
    assert target._deferred_steer_receipts[out["steer_id"]]["state"].startswith("refused@")
    notices = _sender_notices(caller)
    assert len(notices) == 1, notices
    assert "It was not delivered and will not run." in notices[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", ["raises", "returns_false"])
async def test_a_held_steer_refused_at_the_turn_end_tells_its_sender(
    tmp_path, monkeypatch, refusal
):
    """(ax) The end-of-turn queueing refuses the held steer: one notice, receipt refused."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _running(_peer_target(state, "chat-2", caller))
    out = await _send(state, caller)
    assert out.get("pending_steer") is True

    if refusal == "raises":
        _refuse_sender(monkeypatch)
    else:
        # The target resolves to another session: the queue fallback answers False.
        monkeypatch.setattr(sc, "authorize_target", lambda *_a, **_k: MagicMock())
    spawn = await _hand_off(state, target)

    assert spawn.call_count == 0 and not target._queue
    assert target._deferred_steer_receipts[out["steer_id"]]["state"].startswith("refused@")
    notices = _sender_notices(caller)
    assert len(notices) == 1, notices
    assert "It was not delivered and will not run." in notices[0]


@pytest.mark.asyncio
async def test_a_steer_a_hard_stop_discarded_during_its_attempt_gets_only_the_stop_notice(
    tmp_path, monkeypatch
):
    """(ay) Discarded while its gate awaited, then refused: the stop's one notice, no second."""
    from kiro_crew.dashboard import deferred_steer

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _running(_peer_target(state, "chat-2", caller))
    out = await _send(state, caller)
    assert out.get("pending_steer") is True

    async def _discard_while_gating():
        deferred_steer.discard_all(
            target,
            lambda held: sc.notify_send_origin_discarded(
                state,
                entry_meta=held.origin_meta,
                target_slot=target,
                text=held.text,
                cause=sc.SEND_DISCARD_HARD_STOP,
            ),
        )

    monkeypatch.setattr(sc, "prewarm_enabled_check", _discard_while_gating)
    _refuse_sender(monkeypatch)
    await deferred_steer.flush_at_boundary(state, target)

    assert target._deferred_steer_receipts[out["steer_id"]]["state"].startswith("discarded@")
    notices = _sender_notices(caller)
    assert len(notices) == 1 and "hard stop" in notices[0], notices
