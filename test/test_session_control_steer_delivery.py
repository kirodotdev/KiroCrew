"""Steer delivery into a running turn and how ``send_to_target`` reports it.

``chat_delivery.steer_into_running_turn`` tells a consumed steer from a requeued or
discarded one by its delivery id, not by its text.
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state
from session_control_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _busy,
    _enabled_fixture,
    _fresh_create_budget,
    _fresh_stop_windows,
    _key,
    _peer_target,
    _slot,
    _steerable,
)

from kiro_crew.dashboard import session_control as sc
from kiro_crew.testing.wait import await_until


def test_an_identical_steer_is_refused_while_the_first_is_still_in_flight(tmp_path):
    """In flight means either marker, not just the pending list.

    A consumed steer still owns its `_steer_delivery_ids` entry; a second identical
    steer would overwrite it and the first's row would persist twice.
    """
    from kiro_crew.dashboard import chat_delivery

    state = _make_state(tmp_path)
    slot = _busy(_slot(state, "chat-2"))
    text = "run the deploy"
    # Without a steerable client the top guard returns STEER_UNAVAILABLE and the
    # guard under test is never reached.
    slot._acp_client = _steerable(accepted=True)

    # The first steer is mid-flight with its pending entry already consumed.
    slot._pending_steers = []
    slot._steer_delivery_ids[text] = "id-of-the-first-caller"

    result = asyncio.run(chat_delivery.steer_into_running_turn(state, slot, text))

    assert (
        result == chat_delivery.STEER_UNAVAILABLE
    ), "the second identical steer must be refused down the queue path"
    assert (
        slot._steer_delivery_ids[text] == "id-of-the-first-caller"
    ), "the first caller's delivery id must not be overwritten"
    slot._acp_client.steer.assert_not_awaited()


def test_an_unrelated_identical_queue_item_is_not_read_as_our_requeue(tmp_path):
    """Requeue detection is by delivery id, so identical text cannot impersonate it.

    Another client may queue the same words in the same window; reading that as our
    requeue would skip persisting, and the consumed steer would lose its row.
    """
    from kiro_crew.dashboard import chat_delivery

    state = _make_state(tmp_path)
    slot = _busy(_slot(state, "chat-2"))
    text = "run the deploy"
    slot._acp_client = _steerable(accepted=True)

    def _consume_and_let_someone_else_queue(*_a, **_kw):
        # The turn consumes ours; another client queues the same words, no delivery id.
        slot._pending_steers.clear()
        slot.queue_append(text)
        return True

    slot._acp_client.steer = AsyncMock(side_effect=_consume_and_let_someone_else_queue)

    result = asyncio.run(chat_delivery.steer_into_running_turn(state, slot, text))

    assert (
        result == chat_delivery.STEER_STEERED
    ), "an identical queue entry without our delivery id must not read as our requeue"


def test_the_delivery_path_never_claims_a_turn_consumed_a_steer(tmp_path, monkeypatch):
    """The steer RPC proves the bytes left, not that any turn received them.

    A delivered steer can still be requeued to a later turn by the teardown, and an
    append-only ledger entry cannot be moved, so this path writes none.
    """
    from kiro_crew.crew_log import emit as crew_log_emit
    from kiro_crew.dashboard import chat_delivery

    state = _make_state(tmp_path)
    slot = _busy(_slot(state, "chat-2"))
    text = "use the other branch"
    slot._acp_client = _steerable(accepted=True)

    appended: list[str] = []
    queued_for: list[str] = []
    monkeypatch.setattr(crew_log_emit, "session_id_of", lambda _client: "acp-1")
    # Spied on the shared write seam, so an entry of ANY type is caught.
    monkeypatch.setattr(
        crew_log_emit,
        "_write",
        lambda _sid, entry_type, *a, **kw: appended.append(entry_type),
    )
    monkeypatch.setattr(
        crew_log_emit, "on_message_queued", lambda sid, **_kw: queued_for.append(sid)
    )
    # A turn IS running, so a guess would have had something plausible to record.
    monkeypatch.setattr(crew_log_emit, "live_turn", lambda _sid: 13)

    def _consume_inside_the_rpc(*_a, **_kw):
        # The running turn takes the registration, so reconciliation reports delivered.
        slot._pending_steers.clear()
        slot._steer_confirmed.add(slot._steer_delivery_ids[text])
        return True

    slot._acp_client.steer = AsyncMock(side_effect=_consume_inside_the_rpc)

    result = asyncio.run(chat_delivery.steer_into_running_turn(state, slot, text))

    assert result == chat_delivery.STEER_STEERED
    assert appended == [], "the delivery path must not assert consumption"
    assert queued_for == [], "and it is not a queued message either"


def test_a_stop_race_that_only_expects_a_requeue_records_nothing(tmp_path, monkeypatch):
    """An expected requeue is a prediction, and this log records observation.

    A second stop can hard-kill and discard the pending steer, so a `message/queued`
    written here could claim a queue entry that was never made.
    """
    from kiro_crew.crew_log import emit as crew_log_emit
    from kiro_crew.dashboard import chat_delivery

    state = _make_state(tmp_path)
    slot = _busy(_slot(state, "chat-2"))
    text = "stop and do this instead"
    slot._acp_client = _steerable(accepted=True)

    queued: list[str] = []
    monkeypatch.setattr(crew_log_emit, "session_id_of", lambda _client: "acp-1")
    monkeypatch.setattr(crew_log_emit, "on_message_queued", lambda sid, **_kw: queued.append(sid))

    def _stop_without_requeueing(*_a, **_kw):
        # A stop lands while the steer is still registered and not yet queued.
        slot._stop_generation = int(getattr(slot, "_stop_generation", 0) or 0) + 1
        return True

    slot._acp_client.steer = AsyncMock(side_effect=_stop_without_requeueing)

    result = asyncio.run(chat_delivery.steer_into_running_turn(state, slot, text))

    assert result == chat_delivery.STEER_REQUEUED
    assert queued == [], "an expected requeue is not an observed one"


def test_a_requeued_steer_is_recorded_as_a_queued_message(tmp_path, monkeypatch):
    """The one ledger fact this path CAN prove, named by the right id.

    The id recorded is the queue entry's own, as `queue_for_next_turn` records it.
    The client's `sendId` is a different namespace that only looks like a queue id.
    """
    from kiro_crew.crew_log import emit as crew_log_emit
    from kiro_crew.dashboard import chat_delivery

    state = _make_state(tmp_path)
    slot = _busy(_slot(state, "chat-2"))
    text = "run the deploy"
    slot._acp_client = _steerable(accepted=True)

    queued: list[dict] = []
    monkeypatch.setattr(crew_log_emit, "session_id_of", lambda _client: "acp-1")
    monkeypatch.setattr(
        crew_log_emit, "on_message_queued", lambda sid, **kw: queued.append(dict(kw))
    )
    seen: dict[str, str] = {}

    def _requeue_like_the_teardown(*_a, **_kw):
        did = slot._steer_delivery_ids.get(text, "")
        slot._pending_steers.clear()
        seen["qid"] = str(slot.queue_insert(0, text, meta={"steer_delivery_id": did}))
        return True

    slot._acp_client.steer = AsyncMock(side_effect=_requeue_like_the_teardown)

    result = asyncio.run(
        chat_delivery.steer_into_running_turn(state, slot, text, send_id="s-client-side")
    )

    assert result == chat_delivery.STEER_REQUEUED
    assert len(queued) == 1 and queued[0]["source"] == "steer"
    assert seen["qid"], "the harness never queued anything -- the test proves nothing"
    assert (
        queued[0]["queued_seq"] == seen["qid"]
    ), "the entry must name the queue entry it became, not the client's send id"
    assert queued[0]["queued_seq"] != "s-client-side"


def test_our_own_requeue_is_still_detected_by_its_delivery_id(tmp_path):
    """A real requeue carries the id and must report requeued.

    Reported as delivered, the drain would append the row a second time.
    """
    from kiro_crew.dashboard import chat_delivery

    state = _make_state(tmp_path)
    slot = _busy(_slot(state, "chat-2"))
    text = "run the deploy"
    slot._acp_client = _steerable(accepted=True)

    def _requeue_like_the_teardown(*_a, **_kw):
        # As `_requeue_unconsumed_steers` does: queue the steer with its delivery id.
        did = slot._steer_delivery_ids.get(text, "")
        slot._pending_steers.clear()
        slot.queue_insert(0, text, meta={"steer_delivery_id": did})
        return True

    slot._acp_client.steer = AsyncMock(side_effect=_requeue_like_the_teardown)

    result = asyncio.run(chat_delivery.steer_into_running_turn(state, slot, text))

    assert result == chat_delivery.STEER_REQUEUED, "our own requeue must be recognised"


def test_a_steer_a_hard_kill_discarded_is_not_reported_as_delivered(tmp_path):
    """A hard kill discards the text, so no row may claim it was delivered.

    A hard kill drops the `_steer_delivery_ids` entry; a consume leaves it. Here
    `STEER_UNAVAILABLE` falls through to the queue, which cannot duplicate anything.
    """
    from kiro_crew.dashboard import chat_delivery

    state = _make_state(tmp_path)
    slot = _busy(_slot(state, "chat-2"))
    text = "change the plan"
    slot._acp_client = _steerable(accepted=True)

    # The hard kill ran while the steer RPC was suspended: pending entry cleared,
    # delivery id dropped with it, stop generation advanced.
    def _clear_like_a_hard_kill(*_a, **_kw):
        slot._pending_steers.clear()
        slot._steer_delivery_ids.clear()
        slot._stop_generation = int(getattr(slot, "_stop_generation", 0) or 0) + 1
        return True

    slot._acp_client.steer = AsyncMock(side_effect=_clear_like_a_hard_kill)

    result = asyncio.run(chat_delivery.steer_into_running_turn(state, slot, text))

    assert (
        result == chat_delivery.STEER_UNAVAILABLE
    ), "a discarded steer must not be reported as delivered"


def test_a_steer_consumed_before_a_stop_is_still_reported_as_delivered(tmp_path):
    """A consumed steer keeps the delivered path even if the turn is then stopped.

    Telling that caller to resend is the one answer that can run the text twice.
    """
    from kiro_crew.dashboard import chat_delivery

    state = _make_state(tmp_path)
    slot = _busy(_slot(state, "chat-2"))
    text = "change the plan"
    slot._acp_client = _steerable(accepted=True)

    def _consume_then_stop(*_a, **_kw):
        # The running turn consumed the registration; the delivery id survives.
        slot._pending_steers.clear()
        slot._stop_generation = int(getattr(slot, "_stop_generation", 0) or 0) + 1
        return True

    slot._acp_client.steer = AsyncMock(side_effect=_consume_then_stop)

    result = asyncio.run(chat_delivery.steer_into_running_turn(state, slot, text))

    assert (
        result != chat_delivery.STEER_UNAVAILABLE
    ), "a consumed steer must never be answered with 'resend'"


@pytest.mark.asyncio
async def test_a_failed_containment_stop_still_reports_the_delivery(tmp_path, monkeypatch):
    """A stop that raises must not turn a consumed steer into a reported failure.

    The text is already delivered; a reported failure invites a resend that runs it
    twice. The audience fence recorded before the RPC still contains the turn.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _busy(_peer_target(state, "chat-2", caller))

    async def _exploding_stop(_state, _slot, **kwargs):
        raise RuntimeError("session was already gone")

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.stop_slot_turn", _exploding_stop)

    audits: list[dict] = []
    monkeypatch.setattr(sc, "_audit", lambda **kw: audits.append(kw))

    client = MagicMock()
    client.supports_steer = True

    async def _steer_then_replace_the_slot(*_a, **_kw):
        # The slot behind this key is swapped mid-RPC, an audience change that needs
        # no session-store fixture.
        state._slots["chat-2"] = _peer_target(state, "chat-2-replacement", caller)
        return True

    client.steer = _steer_then_replace_the_slot
    target._acp_client = client

    out = await sc.send_to_target(
        state,
        caller_session_key=_key(caller),
        target="chat-2",
        message="the turn already has this",
        steer=True,
    )

    assert out["ok"] is True, "a delivered steer must not be reported as failed"
    assert out["steered"] is True, "the turn consumed the text; say so"
    sends = [a for a in audits if a.get("operation") == "send"]
    assert sends, "the successful delivery must still be audited"
    detail = sends[-1]["detail"]
    assert detail.get("steer_containment_stop_failed") is True, (
        "a stop that raised must be visible in the trail, not swallowed: " f"{detail}"
    )
    assert detail.get("steer_stopped_on"), "the constraint that newly held must be named"


@pytest.mark.asyncio
async def test_a_rolled_over_turn_is_not_cancelled_by_containment(tmp_path, monkeypatch):
    """The stop must reach the turn this text entered, not whichever runs later.

    `slot.running` is true for ANY turn, so `_turn_generation`, bumped on every task
    assignment, identifies the steered turn across the suspension.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _busy(_peer_target(state, "chat-2", caller))

    stops: list[str] = []

    async def _fake_stop(_state, _slot, **kwargs):
        stops.append(_slot.key)
        return {"stopped": True}

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.stop_slot_turn", _fake_stop)

    audits: list[dict] = []
    monkeypatch.setattr(sc, "_audit", lambda **kw: audits.append(kw))

    client = MagicMock()
    client.supports_steer = True

    async def _steer_then_roll_the_turn(*_a, **_kw):
        # The next turn starts through the runtime's own setter, so the generation moves.
        target.task = asyncio.current_task()
        # ...and an audience change, so the stop would otherwise fire.
        state._slots["chat-2"] = _peer_target(state, "chat-2-replacement", caller)
        return True

    client.steer = _steer_then_roll_the_turn
    target._acp_client = client

    out = await sc.send_to_target(
        state,
        caller_session_key=_key(caller),
        target="chat-2",
        message="this belonged to the turn that already ended",
        steer=True,
    )

    assert out["steered"] is True
    assert not stops, f"a successor turn must not be cancelled: {stops}"
    detail = [a for a in audits if a.get("operation") == "send"][-1]["detail"]
    assert "turn_rolled_over_stop_skipped" in detail["steer_stopped_on"], (
        "the skipped stop must be visible in the trail rather than looking like a "
        f"containment that never triggered: {detail}"
    )


def test_the_post_rpc_regate_warms_the_config_first():
    """The fallback gate sits after an await, so it needs its own warm.

    On a cold cache `authorize_target` runs `KiroCrewConfig.load()` on the loop, and the
    top-of-function warm is voided by the steer RPC. Asserted on source order.
    """
    send = inspect.getsource(sc.send_to_target)
    rpc_at = send.index("session_deliver.peer(steer=True)")
    regate_at = send.index("regated = authorize_target(")
    warm_at = send.index("await prewarm_enabled_check()", rpc_at)
    assert rpc_at < warm_at < regate_at, (
        "the post-RPC regate must be preceded by its own prewarm: "
        f"rpc={rpc_at} warm={warm_at} regate={regate_at}"
    )


@pytest.mark.asyncio
async def test_the_inter_stage_append_persists_before_returning_success(tmp_path):
    """An acknowledged prompt must not live only in memory.

    A send to a busy target is queued and acknowledged, so a restart before the next
    flush would lose it unless the queue write starts at once.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    # A turn still in flight on the target, so the send queues.
    target.task = MagicMock(done=MagicMock(return_value=False))
    loop = asyncio.get_running_loop()
    flushed: list = []
    write_started = asyncio.Event()

    def recording_flush(slot):
        flushed.append(slot)
        loop.call_soon_threadsafe(write_started.set)

    state.flush_slot_now = recording_flush

    out = await sc.send_to_target(
        state,
        caller_session_key=_key(caller),
        target="chat-2",
        message="acknowledged, so it must be durable",
    )

    assert out["started"] is False, "the prompt was queued, so a receipt was given"
    # Started, not awaited: the write runs on an executor thread, so this waits for
    # that thread's signal and never counts event-loop turns.
    await await_until(write_started, what="the immediate queue write on its executor thread")
    assert flushed == [target], (
        "the immediate queue write must have been started for the slot whose queue "
        f"now holds the acknowledged prompt: {flushed}"
    )


@pytest.mark.asyncio
async def test_a_requeue_onto_a_replaced_slot_is_refused_not_reported_as_sent(
    tmp_path, monkeypatch
):
    """A requeue that lands on a detached slot must not report success.

    `STEER_REQUEUED` put the text on THIS slot object's queue. If the key now resolves
    to a recreated slot, no drain reaches that queue and the prompt is gone.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _busy(_peer_target(state, "chat-2", caller))

    client = MagicMock()
    client.supports_steer = True

    async def _requeue_then_replace_the_slot(*_a, **_kw):
        # The requeue lands first, then a close-and-recreate moves the key.
        target._pending_steers.clear()
        state._slots["chat-2"] = _peer_target(state, "chat-2-replacement", caller)
        return True

    client.steer = _requeue_then_replace_the_slot
    target._acp_client = client

    # Patched on chat_delivery, NOT on sc: `send_to_target` imports this name
    # through a deferred import inside the function, so a patch on the importing
    # module is never consulted.
    import kiro_crew.dashboard.chat_delivery as cd

    async def _fake_steer(*_a, **_kw):
        await _requeue_then_replace_the_slot()
        return cd.STEER_REQUEUED

    monkeypatch.setattr(cd, "steer_into_running_turn", _fake_steer)

    with pytest.raises(sc.SessionControlError) as caught:
        await sc.send_to_target(
            state,
            caller_session_key=_key(caller),
            target="chat-2",
            message="this was requeued onto a slot that no longer exists",
            steer=True,
        )

    assert caught.value.code == "target_moved", (
        "a requeue onto a detached slot must be refused as target_moved, not "
        f"reported as delivered: {caught.value.code}"
    )
