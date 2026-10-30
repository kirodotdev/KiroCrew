"""Steer delivery into a running turn and how ``send_to_target`` reports it.

Covers ``chat_delivery.steer_into_running_turn`` telling a consumed steer from a
requeued or hard-kill-discarded one by its delivery id, and refusing a second
identical steer while the first is still in flight. A requeue it observes records
``message/queued`` under the queue entry's id, while a delivered steer or a merely
expected requeue records none.

Also covers ``send_to_target``: a containment stop that raises or would hit a
rolled-over turn, the prewarm ahead of the post-RPC regate, the immediate queue
write for a send to a busy target, and a requeue onto a replaced slot refused with
``SessionControlError``.
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

    A steer whose pending entry the running turn has already consumed is still
    awaiting and still owns its `_steer_delivery_ids` entry. Admitting a second
    identical steer at that moment overwrites the first caller's live id, and
    reconciliation then removes the second's -- letting the first's row persist
    twice.

    Mutation guard: checking only `_pending_steers` admits the second steer.
    """
    from kiro_crew.dashboard import chat_delivery

    state = _make_state(tmp_path)
    slot = _busy(_slot(state, "chat-2"))
    text = "run the deploy"
    # A steerable client is required or the function returns STEER_UNAVAILABLE from
    # its top guard and never reaches the one under test -- which is how the first
    # version of this test passed against the unfixed code.
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

    Counting queue entries that match the TEXT cannot tell this steer's requeue
    apart from another client queueing the same words in the same window. Reading
    that as "mine was requeued" makes the caller skip persisting, and the steer the
    turn actually consumed loses its transcript row.

    Mutation guard: a content count sees the queue grow and returns the requeued
    outcome, so nothing persists.
    """
    from kiro_crew.dashboard import chat_delivery

    state = _make_state(tmp_path)
    slot = _busy(_slot(state, "chat-2"))
    text = "run the deploy"
    slot._acp_client = _steerable(accepted=True)

    def _consume_and_let_someone_else_queue(*_a, **_kw):
        # The running turn consumes our registration, and an unrelated client
        # queues the very same words while the RPC is still suspended. That entry
        # carries no delivery id, so it is not ours.
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

    A steer reported delivered can still be sitting pending when its turn ends, and
    that turn's teardown requeues it to a LATER turn. A ledger entry written from
    here would already be on disk saying the earlier turn received it, and an
    append-only entry cannot be moved the way the transcript row can. So this path
    writes no ledger entry at all, which is why the vocabulary carries no steer type.

    Mutation guard: recording the delivered case here -- from a live ordinal or any
    other guess -- reddens this.
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
    # Spied on the shared write seam rather than on one entry point, so an entry
    # written under ANY type is caught rather than only a steer-shaped one.
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
        # The running turn takes the registration, which is what makes the
        # reconciliation report delivered.
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

    On this path the steer is still pending and a stop has landed, so the teardown
    is expected to requeue the text. It may not: a second stop can hard-kill and
    discard the pending steers first, and then a `message/queued` written here
    permanently claims a queue entry that was never made. The text still reaches
    the log if it runs, as the `message/received` of the turn that runs it.

    Mutation guard: recording the queued outcome here reddens this.
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
        # A stop lands while the steer is still registered, and nothing has moved
        # it into the queue: exactly the state where a requeue is only expected.
        slot._stop_generation = int(getattr(slot, "_stop_generation", 0) or 0) + 1
        return True

    slot._acp_client.steer = AsyncMock(side_effect=_stop_without_requeueing)

    result = asyncio.run(chat_delivery.steer_into_running_turn(state, slot, text))

    assert result == chat_delivery.STEER_REQUEUED
    assert queued == [], "an expected requeue is not an observed one"


def test_a_requeued_steer_is_recorded_as_a_queued_message(tmp_path, monkeypatch):
    """The one ledger fact this path CAN prove, named by the right id.

    The requeue moves the text straight into the slot queue without passing the
    append that records `message/queued`, so nothing else in the system knows it
    happened. The id recorded is the QUEUE ENTRY's own -- the same quantity
    `queue_for_next_turn` records, so one reader joins both against the queue. The
    client's `sendId` is a different namespace minted by a different party and
    would look like a queue id without being one. No turn rides on it: a queued
    message belongs to no turn until the one that runs it starts.

    Mutation guard: recording `send_id` reddens this, because the two differ here.
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
    """The other side: a real requeue carries the id and must report requeued.

    Mutation guard: dropping the id probe reports this as delivered and the drain
    then appends the row a second time.
    """
    from kiro_crew.dashboard import chat_delivery

    state = _make_state(tmp_path)
    slot = _busy(_slot(state, "chat-2"))
    text = "run the deploy"
    slot._acp_client = _steerable(accepted=True)

    def _requeue_like_the_teardown(*_a, **_kw):
        # The teardown moves the pending steer into the queue, carrying the id it
        # was registered under -- exactly what `_requeue_unconsumed_steers` does.
        did = slot._steer_delivery_ids.get(text, "")
        slot._pending_steers.clear()
        slot.queue_insert(0, text, meta={"steer_delivery_id": did})
        return True

    slot._acp_client.steer = AsyncMock(side_effect=_requeue_like_the_teardown)

    result = asyncio.run(chat_delivery.steer_into_running_turn(state, slot, text))

    assert result == chat_delivery.STEER_REQUEUED, "our own requeue must be recognised"


def test_a_steer_a_hard_kill_discarded_is_not_reported_as_delivered(tmp_path):
    """A hard kill discards the text, so no row may claim it was delivered.

    A hard kill clears `_pending_steers` AND drops the matching
    `_steer_delivery_ids` entry, while the running turn CONSUMING a steer leaves
    that entry in place. The delivery id is therefore what tells the two apart --
    without it, absence alone is ambiguous and the reconciliation has to guess.

    Reporting delivery for a discarded steer persists a transcript row for text
    that never ran and answers the caller `steered: true`. `STEER_UNAVAILABLE`
    instead falls through to the queue, so the message becomes a visible,
    cancellable card; resending cannot duplicate anything, because the killed turn
    did not run it.

    Mutation guard: without the delivery-id check this returns the delivered path.
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
    """The other side of the same discrimination -- and the more dangerous one.

    A consume leaves the delivery id in place. Telling that caller to resend is
    the one answer that can run the text twice, so a surviving id keeps the
    delivered path even though the turn was stopped afterwards.

    Mutation guard: reading absence as discard would send this down the queue path
    and risk a duplicate execution.
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

    By the time the containment stop runs, `STEER_STEERED` means kiro-cli has already
    taken the text: the turn will act on it whatever happens next. The stop only
    narrows what that turn goes on to publish. If it raises and the exception escapes,
    the caller is told the send failed for text that was in fact delivered, and the
    obvious client response -- send it again -- delivers the same message twice.

    Containment does not depend on the stop succeeding: the audience fence recorded
    before the RPC stays on the slot, and the publisher consults it at delivery.

    Mutation guard: drop the `try/except` around `stop_slot_turn` and this raises
    instead of returning, and the audit loses `steer_containment_stop_failed`.
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
        # A change the code names explicitly: the slot object behind this key is
        # swapped while the RPC is suspended, so the turn holding the text belongs
        # to a session the name has stopped resolving to. Chosen over binding a
        # mirror because it needs no session-store fixture to be deterministic.
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

    `slot.running` is true for ANY turn. If the steered turn ends while the RPC is
    suspended and a queued prompt starts the next one, an unguarded stop cancels work
    that never received this steer -- someone else's turn, killed for a constraint
    that has nothing to do with it. `_turn_generation` increments on every task
    assignment, so comparing it identifies the turn across the suspension.

    The steered turn is over by then, so nothing is lost by not stopping it: whatever
    it published is published, and the fence still withholds the legs of the turn that
    holds it.

    Mutation guard: drop the generation comparison and the stop fires here.
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
        # The turn that took the text finishes and the next one starts, which is what
        # a queue drain does between turns. Driven through the same setter the runtime
        # uses so the generation moves exactly as it would in production.
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

    `authorize_target` reaches `session_control_enabled`, which on a cache invalidated
    by a config edit runs `KiroCrewConfig.load()` synchronously and stalls the shared
    loop for every other session. The warm at the top of `send_to_target` predates the
    steer RPC, so the suspension voids its guarantee -- which is the reopened hole
    `prewarm_enabled_check`'s own docstring describes.

    Asserted on source order because what is being guarded is the function's own
    `await`, which a runtime probe cannot observe without racing it.

    Mutation guard: delete the second warm and the regate is the first thing after
    the RPC again.
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

    A send to a busy target queues the prompt and the function then returns a
    success receipt. Until the drain reaches it the queue is its only record, so a
    restart inside the ordinary flush interval loses a message the sender was told had
    landed. Every other producer that appends and reports success writes immediately.

    Asserted on BEHAVIOUR, not on the order of two lines in this module's source:
    the append and the write both live in `state.enqueue_or_run_prompt`. What the
    sender is owed is the write, wherever it is started from.

    Mutation guard: remove the `start_queue_persist` call from
    `enqueue_or_run_prompt`'s queue branch and no write starts here.
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

    `STEER_REQUEUED` means the turn ended under the RPC and its teardown moved the
    text onto THIS slot object's queue. If the key stopped resolving to this object
    in that window -- the target closed and recreated -- that queue belongs to
    something no drain will reach, so the prompt is gone. Reporting `queued` for it
    tells the sender their words are waiting when they are not, and the sender has
    no way to find out.

    The sibling arms both guard this: the steered arm records `slot_replaced` and
    the fallback arm re-runs the gate. This arm was the gap.

    Mutation guard: drop the identity check and this returns ok instead of raising.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _busy(_peer_target(state, "chat-2", caller))

    client = MagicMock()
    client.supports_steer = True

    async def _requeue_then_replace_the_slot(*_a, **_kw):
        # The teardown's requeue, then the close-and-recreate that detaches the
        # object holding it. Ordered this way because that is the sequence the
        # finding describes: the text is already on the old queue when the key
        # moves.
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
