"""A pending steer sent again, or broadcast, is the same steer, never a second one.

The resend and broadcast half of ``test_pending_steer``: a steer the turn already
consumed, a broadcast of a held, injected or unmarked steer, and another sender's
broadcast of the same body.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from chat_test_helpers import _make_state
from pending_steer_helpers import (
    TEXT,
    _hand_off,
    _loss_recovery_target,
    _running,
    _send,
)
from session_control_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _enabled_fixture,
    _fresh_create_budget,
    _fresh_stop_windows,
    _key,
    _peer_target,
    _slot,
    _steerable,
)

from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard import session_control as sc


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

    target._acp_client = None  # no client at this instant: the resend is held
    second = await _send(state, caller)
    assert second["steer_id"] == first["steer_id"]
    assert second.get("pending_steer") is True, "a settled steer's resend is a fresh held one"
    target._acp_client = client
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
    target._acp_client = None  # no client at this instant: the send is held
    first = await _send(state, caller)
    assert first["pending_steer"] is True
    target._acp_client = client
    await deferred_steer.flush_at_boundary(state, target)
    client.steer.assert_awaited_once()
    assert not target._deferred_steer_delivered, "keeps every steer: no mark"
    target._acp_client = None  # the immediate path is unavailable at this instant

    row = await _broadcast(state, caller)

    assert row.get("steer_state") == "steering" and row["pending_steer"] is False, row
    assert not target._deferred_steers
    target._acp_client = client
    await deferred_steer.flush_at_boundary(state, target)
    client.steer.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_broadcast_during_the_hand_off_of_a_pending_steer_runs_it_once(tmp_path):
    """(as) The hand-off requeues the pending original; a broadcast of it then is that steer."""
    from kiro_crew.dashboard import deferred_steer

    state = _make_state(tmp_path)
    caller, target, client = _keep_every_target(state)
    await _send(state, caller)  # steered, pending, unmarked
    target._acp_client = None  # no client at this instant: the send is held
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
    target._acp_client = None  # no client at this instant: the send is held
    first = await _send(state, caller)
    assert first["pending_steer"] is True
    target._acp_client = client
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
