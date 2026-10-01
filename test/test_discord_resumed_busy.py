"""A Discord message into a BUSY resumed dashboard session reaches that slot's own
mid-turn machinery instead of a refusal.

A DM bound to a dashboard session (``!sessions`` or the dashboard mirror menu)
cannot use the Discord-side mid-turn machinery while the dashboard is driving:
the Discord queue drains only at the tail of a Discord-driven turn and its replay
skips resume routing, so a message queued there would run in the NATIVE session.
The dashboard slot has its own steer path and its own queue, drained by the
dashboard turn loop; these tests pin that the resumed branch hands the message to
THOSE, records the same audience fence the peer-steer path records, confirms in
the channel, and keeps the refusal for the cases the slot cannot take.

The dashboard state is the real ``DashboardState`` with a real slot, because the
hand-off writes the slot's queue, its pending-steer bookkeeping and its audience
fences -- a hand-rolled fake would let the routing decision pass against a slot
shape production never has.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state
from test_discord import _prime_live
from test_discord_sessions import _bind_to_chat1, _dispatcher, _log, _message

from kiro_crew.dashboard import channel_handoff as ch
from kiro_crew.dashboard import chat_runner as cr
from kiro_crew.dashboard import session_control as sc
from kiro_crew.history import HUMAN_TURN_META_KEY
from kiro_crew.messaging.transport import InboundMessage

# The refusal every case below either avoids or keeps.
_REFUSAL = "busy with a turn started elsewhere"
# A credential the display redaction rewrites, inside otherwise ordinary text.
_SECRET = "AKIAIOSFODNN7EXAMPLE"
_TYPED = f"use https://docs.example.com/page?key={_SECRET} for this"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))


def _steerable(accepted: bool = True) -> MagicMock:
    client = MagicMock()
    client.supports_steer = True
    client.steer_needs_loss_recovery = False
    client.steer = AsyncMock(return_value=accepted)
    return client


def _busy(slot):
    """``running`` is derived from the task, so a busy slot is expressed by one."""
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    return slot


async def _bound_to_busy_dashboard(tmp_path, *, steer_client: MagicMock | None):
    """DM ``c1`` bound to ``dashboard:chat-1``, whose slot is mid-turn.

    Returns the dispatcher, its client, the Discord-side sessions fake, the real
    dashboard state, the slot, and the list of Discord-side ``enqueue`` calls (the
    channel's own queue, which the resumed path must never touch).
    """
    log = _log()
    dispatcher, client, sessions = _dispatcher({"u1"}, log)
    state = _make_state(tmp_path)
    dispatcher._session_resume.dashboard_state = state
    slot = _busy(state.get_or_create_slot("chat-1"))
    if steer_client is not None:
        slot._acp_client = steer_client
    await _bind_to_chat1(dispatcher, client, log)
    sessions.is_busy = lambda key: True  # type: ignore[method-assign]
    enqueued: list = []
    sessions.enqueue = lambda *a, **k: enqueued.append((a, k)) or True  # type: ignore[attr-defined]
    return dispatcher, client, sessions, state, slot, enqueued


def _sent_texts(client) -> list[str]:
    return [t for t, _ in client.sent]


@pytest.mark.asyncio
async def test_steer_mode_hands_the_message_to_the_slot_steer_path(tmp_path) -> None:
    """Default (steer) mode: the text cuts into the dashboard's running turn.

    The same injection the dashboard composer uses -- ``steer_into_running_turn``
    on the slot's published client -- and none of the Discord-side machinery: no
    refusal, no Discord queue entry, no Discord-driven turn.
    """
    steer_client = _steerable()
    dispatcher, client, sessions, state, slot, enqueued = await _bound_to_busy_dashboard(
        tmp_path, steer_client=steer_client
    )

    await dispatcher.handle_message(_message("hurry up, skip the tests"))

    steer_client.steer.assert_awaited_once_with("hurry up, skip the tests")
    assert not any(_REFUSAL in t for t in _sent_texts(client)), _sent_texts(client)
    assert any("Steering" in t for t in _sent_texts(client)), _sent_texts(client)
    assert enqueued == [], "the Discord-side queue drains into the native session"
    assert sessions.last_key == "", "no Discord-driven turn ran anywhere"


@pytest.mark.asyncio
async def test_a_landed_steer_records_the_audience_fence_the_peer_path_records(tmp_path) -> None:
    """The reply's cross-surface publication is judged against the admission.

    Recorded BEFORE the RPC and retained for the whole turn, exactly as
    ``session_control.send_to_target`` does: an unchanged audience publishes
    normally, and a constraint that newly holds since the admission withholds the
    cross-surface leg (``cross_surface_withheld``).
    """
    fence_during_rpc: list[int] = []
    steer_client = _steerable()

    async def _observe_then_accept(_msg):
        fence_during_rpc.append(len(slot._steer_audience_fences))
        return True

    steer_client.steer = AsyncMock(side_effect=_observe_then_accept)
    dispatcher, client, sessions, state, slot, _ = await _bound_to_busy_dashboard(
        tmp_path, steer_client=steer_client
    )

    await dispatcher.handle_message(_message("look at the failing lane first"))

    assert fence_during_rpc == [1], "the admission must be recorded before the RPC"
    assert len(slot._steer_audience_fences) == 1, "and retained for the whole turn"
    (admission,) = slot._steer_audience_fences.values()
    assert sc.QUEUED_CONTAINMENT_META_KEY in admission
    assert cr.cross_surface_withheld(state, slot) is False, "nothing moved: publish"
    state.sessions.set_mirror_link("dashboard:chat-1", "C0FFEE", "1758.0004")
    assert cr.cross_surface_withheld(state, slot) is True, "a newly held constraint withholds"


@pytest.mark.asyncio
async def test_a_refused_steer_queues_and_releases_the_audience_record(tmp_path) -> None:
    """A steer the client declined queues instead and lets go of its fence.

    The fence follows the message INTO the turn. Declined, the text never entered
    it, so this steer's hold is released and -- with no other holder -- the record
    is gone: a mirror bound afterwards must not withhold the reply of a turn that
    never received the channel text.
    """
    dispatcher, client, sessions, state, slot, enqueued = await _bound_to_busy_dashboard(
        tmp_path, steer_client=_steerable(accepted=False)
    )

    await dispatcher.handle_message(_message("plan B then"))

    assert slot._steer_audience_fences == {} and slot._steer_audience_fence_holders == {}
    state.sessions.set_mirror_link("dashboard:chat-1", "C0FFEE", "1758.0004")
    assert cr.cross_surface_withheld(state, slot) is False, "no fence for text that never entered"
    assert [q["content"] for q in slot._queue] == ["plan B then"]
    assert any("Queued" in t for t in _sent_texts(client)), _sent_texts(client)


@pytest.mark.asyncio
async def test_an_unsteerable_backend_leaves_no_fence_behind(tmp_path) -> None:
    """A pre-RPC refusal leaves no fence on the slot.

    A backend outside the steer-capable set answers ``STEER_UNAVAILABLE`` before
    any RPC; the text takes the queue arm. A fence kept for that admission would
    stay for the whole turn and withhold the dashboard turn's cross-surface leg
    as soon as a mirror is bound -- for a message the turn never received -- so
    the admission's hold is released and the record goes with it.
    """
    client_no_steer = MagicMock()
    client_no_steer.supports_steer = False
    dispatcher, client, sessions, state, slot, enqueued = await _bound_to_busy_dashboard(
        tmp_path, steer_client=client_no_steer
    )

    await dispatcher.handle_message(_message("switch to the other branch"))

    assert [q["content"] for q in slot._queue] == ["switch to the other branch"]
    assert slot._steer_audience_fences == {}, "no fence for a message that never entered"
    assert slot._steer_audience_fence_holders == {}
    state.sessions.set_mirror_link("dashboard:chat-1", "C0FFEE", "1758.0004")
    assert cr.cross_surface_withheld(state, slot) is False
    assert any("Queued" in t for t in _sent_texts(client)), _sent_texts(client)
    assert enqueued == []


@pytest.mark.asyncio
async def test_a_declined_steer_keeps_a_fence_a_landed_sibling_holds(tmp_path) -> None:
    """Two messages under one audience: one lands, one is declined -- the fence stays.

    The record is shared by audience; the landed steer's hold keeps it for the
    turn, so the sibling's release cannot publish a leg the landed text's fence
    withholds.
    """
    steer_client = _steerable()
    dispatcher, client, sessions, state, slot, _ = await _bound_to_busy_dashboard(
        tmp_path, steer_client=steer_client
    )
    steer_client.steer = AsyncMock(side_effect=[True, False])

    await dispatcher.handle_message(_message("first, landed"))
    await dispatcher.handle_message(_message("second, declined"))

    assert steer_client.steer.await_count == 2
    assert len(slot._steer_audience_fences) == 1, "the landed steer's record stays"
    assert list(slot._steer_audience_fence_holders.values()) == [1]
    state.sessions.set_mirror_link("dashboard:chat-1", "C0FFEE", "1758.0004")
    assert cr.cross_surface_withheld(state, slot) is True, "the landed text is fenced"
    assert [q["content"] for q in slot._queue] == ["second, declined"]


@pytest.mark.asyncio
async def test_a_steer_that_wakes_in_the_next_turn_leaves_that_turns_fence_alone(tmp_path) -> None:
    """The release is bound to the turn the steer was admitted to.

    Steer A is suspended across turn N's teardown, which requeues A's text and
    clears both fence maps. Turn N+1 starts and steer B is admitted under the
    same audience key -- the key is deterministic, so B's record is identical to
    the one A held. A then wakes as ``STEER_REQUEUED``. An unbound release would
    take B's hold, pop B's admission and let N+1 publish B's leg to a mirror
    bound after B was admitted; bound to A's turn generation, A releases nothing.
    """
    steer_client = _steerable()
    dispatcher, client, sessions, state, slot, _ = await _bound_to_busy_dashboard(
        tmp_path, steer_client=steer_client
    )
    calls: list[str] = []

    async def _steer(msg):
        calls.append(msg)
        if msg == "A: first, suspended across the teardown":
            # Turn N ends while A's RPC is suspended: the teardown requeues A's
            # pending steer and clears the fence maps ...
            cr._requeue_unconsumed_steers(state, slot)
            slot._steer_audience_fences.clear()
            slot._steer_audience_fence_holders.clear()
            # ... turn N+1 starts (a new task bumps the turn generation) ...
            _busy(slot)
            # ... and B steers N+1 under the same audience, landing.
            await dispatcher.handle_message(_message("B: steers the next turn"))
            return True
        return True

    steer_client.steer = AsyncMock(side_effect=_steer)

    await dispatcher.handle_message(_message("A: first, suspended across the teardown"))

    assert calls == ["A: first, suspended across the teardown", "B: steers the next turn"]
    assert [q["content"] for q in slot._queue] == ["A: first, suspended across the teardown"]
    assert len(slot._steer_audience_fences) == 1, "B's record survives A's late wake"
    assert list(slot._steer_audience_fence_holders.values()) == [1], "B's hold intact"
    state.sessions.set_mirror_link("dashboard:chat-1", "C0FFEE", "1758.0004")
    assert cr.cross_surface_withheld(state, slot) is True, "N+1 still withholds B's leg"
    assert any("Queued" in t for t in _sent_texts(client)), _sent_texts(client)


@pytest.mark.asyncio
async def test_a_requeued_steer_releases_its_hold(tmp_path) -> None:
    """A steer the turn's teardown requeues runs as its own turn, not in this one."""
    steer_client = _steerable()
    dispatcher, client, sessions, state, slot, _ = await _bound_to_busy_dashboard(
        tmp_path, steer_client=steer_client
    )

    async def _requeue_then_accept(_msg):
        cr._requeue_unconsumed_steers(state, slot)
        return True

    steer_client.steer = AsyncMock(side_effect=_requeue_then_accept)

    await dispatcher.handle_message(_message("try the other branch"))

    assert [q["content"] for q in slot._queue] == ["try the other branch"]
    assert slot._steer_audience_fences == {} and slot._steer_audience_fence_holders == {}
    assert any("Queued" in t for t in _sent_texts(client)), _sent_texts(client)


@pytest.mark.parametrize(
    "how",
    [
        pytest.param("override", id="!queue-prefix"),
        pytest.param("config", id="queue_mode=queue"),
        pytest.param("no-steer-client", id="steer-unavailable"),
        pytest.param("loss-recovery", id="steer_needs_loss_recovery"),
    ],
)
@pytest.mark.asyncio
async def test_queue_mode_lands_the_message_in_the_slot_queue(tmp_path, how) -> None:
    """Queue mode, ``!queue``, and an unavailable steer all reach the SLOT's queue.

    That queue is drained by the dashboard turn loop, so ordering is the
    dashboard's; the Discord-side ``_drain_queue`` never sees the entry. The entry
    carries the drain's admission stamp and BOTH provenance marks a channel
    human's text carries on the Slack linked-thread path: user origin (the
    session's own human typed it) and channel origin (channel authority is the
    narrower credential boundary).
    """
    steer_client: MagicMock | None = _steerable()
    if how == "no-steer-client":
        steer_client = None
    elif how == "loss-recovery":
        # codex can drop a steer it already took; the composer accepts that
        # because its human watches the turn. A channel human cannot, so the
        # text takes the queue.
        steer_client.steer_needs_loss_recovery = True
    dispatcher, client, sessions, state, slot, enqueued = await _bound_to_busy_dashboard(
        tmp_path, steer_client=steer_client
    )
    text = "!queue hold this for after" if how == "override" else "hold this for after"
    if how == "config":
        # The mode is read at point of use from the live config snapshot, not
        # from the boot copy; conftest resets the snapshot around every test.
        dispatcher.cfg.messaging.queue_mode = "queue"
        _prime_live(dispatcher.cfg)

    await dispatcher.handle_message(_message(text))

    assert not any(_REFUSAL in t for t in _sent_texts(client)), _sent_texts(client)
    assert any("Queued" in t for t in _sent_texts(client)), _sent_texts(client)
    if steer_client is not None:
        steer_client.steer.assert_not_awaited()
    (entry,) = slot._queue
    assert entry["content"] == "hold this for after"
    assert entry.get("_directive_user_origin") is True
    assert entry.get("_directive_channel_origin") is True
    assert sc.QUEUED_CONTAINMENT_META_KEY in entry["meta"]
    assert enqueued == [], "never the Discord-side queue"
    assert sessions.last_key == ""


@pytest.mark.asyncio
async def test_a_message_with_attachments_is_never_steered(tmp_path) -> None:
    """Attachments keep the queue-only rule's half that holds: no steer.

    ``_session/steer`` carries text only, and the dashboard slot's queue cannot
    carry Discord attachment material either (it is downloaded into temp files
    owned by the consuming turn, which the dashboard drain has no hook to own).
    So the message is refused with wording that names the attachments and the
    remedy -- the files stay with the user instead of being dropped on the floor
    or answered without.
    """
    steer_client = _steerable()
    dispatcher, client, sessions, state, slot, enqueued = await _bound_to_busy_dashboard(
        tmp_path, steer_client=steer_client
    )
    msg = InboundMessage(
        channel_type="discord",
        user_id="u1",
        conversation_id="c1",
        text="here is the screenshot",
        attachments=[SimpleNamespace(url="https://cdn.example/x.png", name="x.png")],
    )

    await dispatcher.handle_message(msg)

    steer_client.steer.assert_not_awaited()
    assert slot._queue == []
    assert enqueued == []
    assert any("attachment" in t for t in _sent_texts(client)), _sent_texts(client)
    assert any("busy" in t for t in _sent_texts(client)), _sent_texts(client)


@pytest.mark.asyncio
async def test_an_incognito_target_takes_the_steer_path(tmp_path) -> None:
    """A restricted (incognito/temporary) slot is taken like any other.

    Those modes keep their transcript and queue in History and withhold only what
    is derived from the chat; the steer row and the queue entry are the records
    the slot's own composer writes in that mode, so a restricted slot persists
    them and takes the same steer/queue path.
    """
    steer_client = _steerable()
    dispatcher, client, sessions, state, slot, enqueued = await _bound_to_busy_dashboard(
        tmp_path, steer_client=steer_client
    )
    slot.memory_mode = "incognito"
    assert slot.is_restricted

    await dispatcher.handle_message(_message("private follow-up"))

    steer_client.steer.assert_awaited_once_with("private follow-up")
    assert not any(_REFUSAL in t for t in _sent_texts(client)), _sent_texts(client)
    assert any("Steering" in t for t in _sent_texts(client)), _sent_texts(client)
    assert len(slot._steer_audience_fences) == 1
    assert enqueued == []


@pytest.mark.asyncio
async def test_a_temporary_target_takes_the_queue_path(tmp_path) -> None:
    """The queue arm too: a temporary slot's queue is the same durable queue."""
    dispatcher, client, sessions, state, slot, enqueued = await _bound_to_busy_dashboard(
        tmp_path, steer_client=None
    )
    slot.memory_mode = "temporary"

    await dispatcher.handle_message(_message("private follow-up"))

    assert [q["content"] for q in slot._queue] == ["private follow-up"]
    assert any("Queued for that session" in t for t in _sent_texts(client)), _sent_texts(client)
    assert enqueued == []


@pytest.mark.asyncio
async def test_a_slot_that_is_not_driving_the_turn_is_still_refused(tmp_path) -> None:
    """The lease is held, but not by the dashboard turn loop.

    A Discord-driven turn on the resumed key holds ``is_busy`` while the slot is
    idle: it has no client to steer into and no drain coming, and the queue-or-run
    admission would START a second turn against the held lease. The slot's
    machinery cannot take the message, so the refusal stands.
    """
    dispatcher, client, sessions, state, slot, enqueued = await _bound_to_busy_dashboard(
        tmp_path, steer_client=_steerable()
    )
    slot.task = None

    await dispatcher.handle_message(_message("are you there?"))

    assert any(_REFUSAL in t for t in _sent_texts(client)), _sent_texts(client)
    assert slot.task is None and slot._queue == []
    assert enqueued == []


@pytest.mark.asyncio
async def test_a_closing_slot_is_still_refused(tmp_path) -> None:
    steer_client = _steerable()
    dispatcher, client, sessions, state, slot, enqueued = await _bound_to_busy_dashboard(
        tmp_path, steer_client=steer_client
    )
    slot.begin_close()

    await dispatcher.handle_message(_message("one more thing"))

    assert any(_REFUSAL in t for t in _sent_texts(client)), _sent_texts(client)
    steer_client.steer.assert_not_awaited()
    assert slot._queue == []


@pytest.mark.asyncio
async def test_a_resumed_key_with_no_open_slot_is_still_refused(tmp_path) -> None:
    """No tab holds the session: nothing can steer or drain, so refuse."""
    log = _log()
    dispatcher, client, sessions = _dispatcher({"u1"}, log)
    dispatcher._session_resume.dashboard_state = _make_state(tmp_path)  # no chat-1 slot
    await _bind_to_chat1(dispatcher, client, log)
    sessions.is_busy = lambda key: True  # type: ignore[method-assign]
    enqueued: list = []
    sessions.enqueue = lambda *a, **k: enqueued.append((a, k)) or True  # type: ignore[attr-defined]

    await dispatcher.handle_message(_message("hello?"))

    assert any(_REFUSAL in t for t in _sent_texts(client)), _sent_texts(client)
    assert enqueued == []
    assert sessions.last_key == ""


@pytest.mark.asyncio
async def test_a_discord_native_busy_session_keeps_handle_busy(tmp_path) -> None:
    """Unbound DM, busy native session: today's ``_handle_busy`` path, unchanged.

    The native provider in this harness does not support steer, so the message
    takes the Discord-side queue with its receipt -- and none of the resumed
    path's wording appears.
    """
    dispatcher, client, sessions = _dispatcher({"u1"}, _log())
    dispatcher._session_resume.dashboard_state = _make_state(tmp_path)
    sessions.is_busy = lambda key: True  # type: ignore[method-assign]
    enqueued: list = []
    sessions.enqueue = lambda *a, **k: enqueued.append((a, k)) or True  # type: ignore[attr-defined]
    native_key = dispatcher.current_session_key("u1")

    await dispatcher.handle_message(_message("native follow-up"))

    assert len(enqueued) == 1
    args, _kwargs = enqueued[0]
    assert args[0] == native_key and args[2] == "native follow-up"
    assert not any("that session" in t for t in _sent_texts(client)), _sent_texts(client)
    assert not any(_REFUSAL in t for t in _sent_texts(client)), _sent_texts(client)


@pytest.mark.asyncio
async def test_a_channel_steer_keeps_the_display_redaction_on_its_row_and_card(tmp_path) -> None:
    """The steer carries user provenance, but a channel author is not the reader.

    The session's own human's composer steer is stored and shown as typed; a
    channel human's is stored sanitized and pushed display-redacted -- the same
    rule ``queue_entry_is_user_origin`` applies to a queue entry carrying both
    stamps -- while the row still carries the human-turn marker.
    """
    steer_client = _steerable()
    dispatcher, client, sessions, state, slot, _ = await _bound_to_busy_dashboard(
        tmp_path, steer_client=steer_client
    )
    state.broadcast_ws = MagicMock()

    await dispatcher.handle_message(_message(_TYPED))

    steer_client.steer.assert_awaited_once_with(_TYPED)
    steer_row = next(m for m in slot.messages if m.get("meta", {}).get("steer"))
    assert _SECRET not in steer_row["content"]
    assert steer_row["meta"].get(HUMAN_TURN_META_KEY) is True
    (push,) = [c.args[1] for c in state.broadcast_ws.call_args_list if c.args[0] == "steer_push"]
    assert _SECRET not in push["content"]


@pytest.mark.asyncio
async def test_a_channel_queued_message_is_pushed_display_redacted(tmp_path) -> None:
    """The ``queue_push`` frame applies the same rule as the queue view."""
    dispatcher, client, sessions, state, slot, _ = await _bound_to_busy_dashboard(
        tmp_path, steer_client=None
    )
    state.broadcast_ws = MagicMock()

    await dispatcher.handle_message(_message(_TYPED))

    (entry,) = slot._queue
    assert entry["content"] == _TYPED, "raw at rest: the drain hands the model the real text"
    (push,) = [c.args[1] for c in state.broadcast_ws.call_args_list if c.args[0] == "queue_push"]
    assert _SECRET not in push["content"]


@pytest.mark.asyncio
async def test_a_slot_replaced_under_the_steer_rpc_is_refused_not_queued_onto_the_detached_one(
    tmp_path,
) -> None:
    """The steer RPC suspends; a close-and-recreate under the same key lands inside it.

    The key still resolves -- to a fresh object -- while the steer was handed to
    the old one. A fallback append would land on the detached slot, which no
    drain reaches, and the DM would be told the message was queued. Compared by
    object identity, as ``send_to_target`` re-gates its own fallback, and refused
    with the remedy instead.
    """
    steer_client = _steerable(accepted=False)
    dispatcher, client, sessions, state, slot, enqueued = await _bound_to_busy_dashboard(
        tmp_path, steer_client=steer_client
    )

    async def _replace_slot_then_decline(_msg):
        state._slots.pop("chat-1")
        _busy(state.get_or_create_slot("chat-1"))
        return False

    steer_client.steer = AsyncMock(side_effect=_replace_slot_then_decline)

    await dispatcher.handle_message(_message("did you see the new failure?"))

    fresh = state.get_slot("chat-1")
    assert fresh is not slot
    assert slot._queue == [] and fresh._queue == []
    assert fresh._steer_audience_fences == {}, "nothing was recorded on the object that lives on"
    assert any("changed while your message was in flight" in t for t in _sent_texts(client))
    assert not any("Queued" in t for t in _sent_texts(client)), _sent_texts(client)
    assert enqueued == []


@pytest.mark.asyncio
async def test_a_requeue_onto_a_replaced_slot_is_reported_not_yet_saved(tmp_path) -> None:
    """``STEER_REQUEUED`` onto the handed-to object while a fresh object holds the key.

    The handed-to slot is judged first: its queue carries the delivery, and the
    persistence witness says that queue is not on disk, so the honest answer is
    the not-yet-saved refusal -- never "queued for that session" (no drain reaches
    the detached queue) and never "NOT delivered" (the human would resend text the
    archive may still carry).
    """
    steer_client = _steerable()
    dispatcher, client, sessions, state, slot, _ = await _bound_to_busy_dashboard(
        tmp_path, steer_client=steer_client
    )

    async def _requeue_then_replace(_msg):
        # The turn's teardown requeues the pending steer on THIS object...
        cr._requeue_unconsumed_steers(state, slot)
        # ...and the slot is closed and recreated under the same key.
        state._slots.pop("chat-1")
        _busy(state.get_or_create_slot("chat-1"))
        return True

    steer_client.steer = AsyncMock(side_effect=_requeue_then_replace)

    await dispatcher.handle_message(_message("try the other branch"))

    assert [q["content"] for q in slot._queue] == ["try the other branch"], "sits on the old object"
    assert state.get_slot("chat-1")._queue == []
    texts = _sent_texts(client)
    assert any("had not been saved with it yet" in t for t in texts), texts
    assert not any(
        "Queued" in t or "changed while your message was in flight" in t for t in texts
    ), texts


@pytest.mark.asyncio
async def test_an_accepted_steer_survives_a_recreate_under_the_same_key(tmp_path) -> None:
    """Red-first: an accepted steer whose key is closed and recreated during the RPC.

    The closing turn took the text and the steer persisted its row on the handed-to
    slot; the successor knows nothing of it. Judging the successor alone answered
    "NOT delivered" and the human resent text that had already run. The handed-to
    slot is judged first, so the answer is the not-yet-saved refusal instead.
    """
    steer_client = _steerable()
    dispatcher, client, sessions, state, slot, enqueued = await _bound_to_busy_dashboard(
        tmp_path, steer_client=steer_client
    )

    async def _replace_then_accept(_msg):
        state._slots.pop("chat-1")
        _busy(state.get_or_create_slot("chat-1"))
        return True

    steer_client.steer = AsyncMock(side_effect=_replace_then_accept)

    await dispatcher.handle_message(_message("keep going"))

    steer_row = next(m for m in slot.messages if m.get("meta", {}).get("steer"))
    assert steer_row["meta"].get("steer_delivery_id")
    assert state.get_slot("chat-1")._queue == [] and slot._queue == []
    texts = _sent_texts(client)
    assert not any("changed while your message was in flight" in t for t in texts), texts
    assert not any("Steering" in t or "Queued" in t or "Delivered" in t for t in texts), texts
    assert any("had not been saved with it yet" in t for t in texts), texts
    assert enqueued == []


@pytest.mark.asyncio
async def test_a_slot_that_stopped_driving_during_the_rpc_is_refused_on_the_fallback(
    tmp_path,
) -> None:
    """The admission gate is re-run on the far side of the suspension.

    A declined steer whose turn ended meanwhile has no drain coming: queueing
    would strand the text until an unrelated later turn, so the fallback refuses.
    """
    steer_client = _steerable(accepted=False)
    dispatcher, client, sessions, state, slot, _ = await _bound_to_busy_dashboard(
        tmp_path, steer_client=steer_client
    )

    async def _turn_ends_then_declines(_msg):
        slot.task = None
        return False

    steer_client.steer = AsyncMock(side_effect=_turn_ends_then_declines)

    await dispatcher.handle_message(_message("one more"))

    assert slot._queue == []
    assert any(_REFUSAL in t for t in _sent_texts(client)), _sent_texts(client)


def _discord_transport(*, allowed: set[str] = frozenset({"u1"})) -> SimpleNamespace:
    """A registered Discord transport shaped like the send ladder consults it."""
    return SimpleNamespace(
        channel_type="discord",
        capabilities=SimpleNamespace(
            supports_proactive_send=True, max_message_chars=2000, supports_session_resume=True
        ),
        send_message=AsyncMock(return_value="mid-1"),
        # Discord's own rule for a DM route: the roster is consulted through the
        # principal, never through the DM channel id.
        may_send_to=lambda conversation_id, thread_id=None, principal="": principal in allowed,
    )


async def _settle_background(state) -> None:
    for task in list(getattr(state, "_background_tasks", ())):
        await task


@pytest.mark.asyncio
async def test_a_dropped_queued_message_is_reported_back_to_the_dm(tmp_path) -> None:
    """The DM was told "queued"; when the drain drops the entry, the DM is told that too.

    A dashboard sender gets its notice on its own transcript; a channel sender
    reads neither the target's transcript nor the SEL, so the drop is reported into
    the conversation that queued it, through the same governed send ladder every
    proactive channel delivery takes -- with the principal the channel authorized on
    inbound, which is what the recipient check needs and a dashboard key cannot
    supply.
    """
    dispatcher, client, sessions, state, slot, _ = await _bound_to_busy_dashboard(
        tmp_path, steer_client=None
    )
    transport = _discord_transport()
    state.register_channel_transport(transport)
    await dispatcher.handle_message(_message(_TYPED))
    (entry,) = slot._queue
    stamp = entry["meta"][sc.CHANNEL_RECIPIENT_META_KEY]
    assert stamp == {"channel_type": "discord", "conversation_id": "c1", "principal": "u1"}

    # A constraint newly holds at the drain: the session gains a mirror.
    state.sessions.set_mirror_link("dashboard:chat-1", "C0FFEE", "1758.0004")
    cr._drop_stale_admissions(state, slot)
    await _settle_background(state)

    assert slot._queue == []
    transport.send_message.assert_awaited_once()
    conversation, notice = transport.send_message.await_args.args
    assert conversation == "c1"
    assert "dropped before it ran" in notice and "will not run" in notice
    assert "send it again" in notice
    assert _SECRET not in notice, "the quoted excerpt takes the egress redaction"
    assert 'Text: "use ' in notice and "for this" in notice, "and still names the message"


@pytest.mark.asyncio
async def test_a_requeued_channel_steer_carries_the_drop_notice_address(tmp_path) -> None:
    """The recipient rides the steer's admission dict, which the requeue copies."""
    steer_client = _steerable()
    dispatcher, client, sessions, state, slot, _ = await _bound_to_busy_dashboard(
        tmp_path, steer_client=steer_client
    )
    transport = _discord_transport()
    state.register_channel_transport(transport)

    async def _requeue_instead(_msg):
        cr._requeue_unconsumed_steers(state, slot)
        return True

    steer_client.steer = AsyncMock(side_effect=_requeue_instead)
    await dispatcher.handle_message(_message("steer that the turn never took"))
    (entry,) = slot._queue
    assert entry["meta"][sc.CHANNEL_RECIPIENT_META_KEY]["conversation_id"] == "c1"

    state.sessions.set_mirror_link("dashboard:chat-1", "C0FFEE", "1758.0004")
    cr._drop_stale_admissions(state, slot)
    await _settle_background(state)

    transport.send_message.assert_awaited_once()
    assert "steer that the turn never took" in transport.send_message.await_args.args[1]


@pytest.mark.asyncio
async def test_a_revoked_recipient_gets_no_drop_notice(tmp_path) -> None:
    """The ladder's recipient re-check stands between the stamp and the send."""
    dispatcher, client, sessions, state, slot, _ = await _bound_to_busy_dashboard(
        tmp_path, steer_client=None
    )
    transport = _discord_transport(allowed=set())
    state.register_channel_transport(transport)
    await dispatcher.handle_message(_message("queued, then revoked"))

    state.sessions.set_mirror_link("dashboard:chat-1", "C0FFEE", "1758.0004")
    cr._drop_stale_admissions(state, slot)
    await _settle_background(state)

    assert slot._queue == [], "the drop itself is the authorization decision and stands"
    transport.send_message.assert_not_awaited()


def _two_roster_transport(*, allowed_threads: set[str], allowed: set[str]) -> SimpleNamespace:
    """A Discord transport with the REAL two-arm recipient rule: a thread route
    answers from the thread roster, anything else from the user roster via the
    principal -- and a thread not on its roster falls through to that DM arm."""
    return SimpleNamespace(
        channel_type="discord",
        capabilities=SimpleNamespace(
            supports_proactive_send=True, max_message_chars=2000, supports_session_resume=True
        ),
        send_message=AsyncMock(return_value="mid-1"),
        may_send_to=lambda conversation_id, thread_id=None, principal="": (
            conversation_id in allowed_threads or (bool(principal) and principal in allowed)
        ),
    )


@pytest.mark.asyncio
async def test_a_thread_revoked_before_the_drain_gets_no_drop_notice(tmp_path) -> None:
    """Red-first: a thread route's drop-notice address carries no principal.

    ``may_send_to`` answers a thread from the thread roster and falls through to
    the DM arm only when the thread is not on it. Stamping the user id as the
    principal made a thread the operator removed from ``allowed_thread_ids`` pass
    that DM arm, so the notice -- excerpt included -- posted into the revoked
    thread. Only a DM route supplies an identity.
    """
    dispatcher, client, sessions, state, slot, _ = await _bound_to_busy_dashboard(
        tmp_path, steer_client=None
    )
    allowed_threads = {"t9"}
    transport = _two_roster_transport(allowed_threads=allowed_threads, allowed={"u1"})
    state.register_channel_transport(transport)

    msg = _message("posted from the thread", channel_id="t9", thread_id="t9")
    await dispatcher._handle_resumed_busy("dashboard:chat-1", msg, msg.text, "queue")
    (entry,) = slot._queue
    stamp = entry["meta"][sc.CHANNEL_RECIPIENT_META_KEY]
    assert stamp == {"channel_type": "discord", "conversation_id": "t9", "principal": ""}

    # The operator removes the thread from the roster before the drain runs.
    allowed_threads.clear()
    state.sessions.set_mirror_link("dashboard:chat-1", "C0FFEE", "1758.0004")
    cr._drop_stale_admissions(state, slot)
    await _settle_background(state)

    assert slot._queue == []
    transport.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_allowed_thread_still_gets_its_drop_notice(tmp_path) -> None:
    """Counterfactual: a thread still on the roster is answered from that roster."""
    dispatcher, client, sessions, state, slot, _ = await _bound_to_busy_dashboard(
        tmp_path, steer_client=None
    )
    transport = _two_roster_transport(allowed_threads={"t9"}, allowed={"u1"})
    state.register_channel_transport(transport)

    msg = _message("posted from the thread", channel_id="t9", thread_id="t9")
    await dispatcher._handle_resumed_busy("dashboard:chat-1", msg, msg.text, "queue")
    state.sessions.set_mirror_link("dashboard:chat-1", "C0FFEE", "1758.0004")
    cr._drop_stale_admissions(state, slot)
    await _settle_background(state)

    transport.send_message.assert_awaited_once()
    assert transport.send_message.await_args.args[0] == "t9"


def test_the_channel_recipient_stamp_does_not_survive_a_restore(tmp_path) -> None:
    """It names a write target on a network surface, so an edited line cannot
    make the drain post the entry's text into a conversation of its choosing."""
    from kiro_crew.dashboard.slot_queue_repository import sanitize_restored_queue

    raw = [
        {
            "id": "q1",
            "content": "hello",
            "meta": {
                sc.CHANNEL_RECIPIENT_META_KEY: {
                    "channel_type": "discord",
                    "conversation_id": "c-anyone",
                    "principal": "u1",
                },
                "sendId": "s-1",
            },
        }
    ]
    (restored,) = sanitize_restored_queue(raw)
    assert sc.CHANNEL_RECIPIENT_META_KEY not in restored["meta"]
    assert restored["meta"]["sendId"] == "s-1"


@pytest.mark.parametrize(
    "meta",
    [
        None,
        {},
        {sc.CHANNEL_RECIPIENT_META_KEY: 17},
        {sc.CHANNEL_RECIPIENT_META_KEY: {"channel_type": "discord"}},
        {sc.CHANNEL_RECIPIENT_META_KEY: {"channel_type": "", "conversation_id": "c1"}},
        {sc.CHANNEL_RECIPIENT_META_KEY: {"channel_type": "discord", "conversation_id": 5}},
        {
            sc.CHANNEL_RECIPIENT_META_KEY: {
                "channel_type": "discord",
                "conversation_id": "c1",
                "principal": 3,
            }
        },
    ],
)
def test_channel_recipient_readers_read_junk_as_no_recipient(meta) -> None:
    assert sc.channel_recipient_of(meta) is None


def test_channel_recipient_meta_stamps_a_whole_address_or_nothing() -> None:
    assert sc.channel_recipient_meta("", "c1", "u1") == {}
    assert sc.channel_recipient_meta("discord", "", "u1") == {}
    stamp = sc.channel_recipient_meta("discord", "c1", "")
    assert sc.channel_recipient_of(stamp) == ("discord", "c1", "")


@pytest.mark.asyncio
async def test_repeated_handoffs_under_one_audience_record_one_fence(tmp_path) -> None:
    """The fence set is bounded: keyed by audience, not by message.

    A long dashboard turn can take many Discord steers. Each landed steer's
    bookkeeping clears when the turn consumes it, but its fence is retained for
    the turn -- so a per-message record would grow without bound. One record per
    audience per turn: re-recording the same audience is a no-op, and the turn's
    teardown clears the set.
    """
    steer_client = _steerable()
    dispatcher, client, sessions, state, slot, _ = await _bound_to_busy_dashboard(
        tmp_path, steer_client=steer_client
    )

    for n in range(5):
        await dispatcher.handle_message(_message(f"steer number {n}"))

    assert steer_client.steer.await_count == 5
    assert len(slot._steer_audience_fences) == 1
    (admission,) = slot._steer_audience_fences.values()
    assert (
        admission[sc.QUEUED_CONTAINMENT_META_KEY]
        == sc.containment_meta(state, slot)[sc.QUEUED_CONTAINMENT_META_KEY]
    )
    assert cr.cross_surface_withheld(state, slot) is False


@pytest.mark.asyncio
async def test_a_changed_audience_records_its_own_fence(tmp_path) -> None:
    """The key is the audience, so a containment change between two steers is two
    records -- and the publisher then judges against the one that newly holds."""
    steer_client = _steerable()
    dispatcher, client, sessions, state, slot, _ = await _bound_to_busy_dashboard(
        tmp_path, steer_client=steer_client
    )

    await dispatcher.handle_message(_message("before the mirror"))
    state.sessions.set_mirror_link("dashboard:chat-1", "C0FFEE", "1758.0004")
    await dispatcher.handle_message(_message("after the mirror"))

    assert steer_client.steer.await_count == 2
    assert len(slot._steer_audience_fences) == 2
    assert (
        cr.cross_surface_withheld(state, slot) is True
    ), "the first admission never saw the mirror"


@pytest.mark.asyncio
async def test_a_full_fence_set_queues_the_handoff_and_keeps_every_fence(tmp_path) -> None:
    """At the fence cap a new audience is neither recorded over a fence nor refused.

    A fence dropped is a cross-surface leg published that should have been
    withheld, so nothing is evicted; and the queue arm records no fence and is
    already where a declined steer goes, so the text takes it instead of a refusal
    that tells the author to wait.
    """
    steer_client = _steerable()
    dispatcher, client, sessions, state, slot, enqueued = await _bound_to_busy_dashboard(
        tmp_path, steer_client=steer_client
    )
    from kiro_crew.dashboard.chat_delivery import MAX_PENDING_STEERS

    # MAX_PENDING_STEERS distinct audiences already recorded this turn; the live
    # audience is none of them, so this message names a new one.
    recorded = {f"peer-{n}": {"marker": n} for n in range(MAX_PENDING_STEERS)}
    slot._steer_audience_fences.update(recorded)

    await dispatcher.handle_message(_message("one more thing"))

    steer_client.steer.assert_not_awaited()
    assert slot._steer_audience_fences == recorded, "every fence intact, none replaced or added"
    assert [q["content"] for q in slot._queue] == ["one more thing"]
    assert enqueued == []
    assert any("Queued for that session" in t for t in _sent_texts(client)), _sent_texts(client)
    assert not any(_REFUSAL in t or "Steering" in t for t in _sent_texts(client))


@pytest.mark.asyncio
async def test_the_drop_notice_resolves_the_send_ladder_off_the_loop_thread(
    tmp_path, monkeypatch
) -> None:
    """The drain is synchronous on the gateway loop; the ladder's governance vet is
    the call every other async caller offloads. So the resolve runs inside the
    scheduled task, on a worker thread, and the drop path itself never calls it.
    """
    import threading

    dispatcher, client, sessions, state, slot, _ = await _bound_to_busy_dashboard(
        tmp_path, steer_client=None
    )
    transport = _discord_transport()
    state.register_channel_transport(transport)
    await dispatcher.handle_message(_message("queued, then dropped"))

    resolver_threads: list[int] = []
    real_resolve = cr._resolve_channel_target

    def _observing_resolve(*args, **kwargs):
        resolver_threads.append(threading.get_ident())
        return real_resolve(*args, **kwargs)

    monkeypatch.setattr(cr, "_resolve_channel_target", _observing_resolve)
    loop_thread = threading.get_ident()

    state.sessions.set_mirror_link("dashboard:chat-1", "C0FFEE", "1758.0004")
    cr._drop_stale_admissions(state, slot)
    assert resolver_threads == [], "the drop path itself must not run the ladder"
    await _settle_background(state)

    assert len(resolver_threads) == 1 and resolver_threads[0] != loop_thread
    transport.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_channel_notifier_reports_scheduling_not_delivery(tmp_path) -> None:
    """True once a stamp is present and the task exists; False for no stamp."""
    state = _make_state(tmp_path)
    slot = _busy(state.get_or_create_slot("chat-1"))
    assert (
        sc.notify_channel_recipient_dropped(
            state, entry_meta={}, target_slot=slot, text="x", constraints=["mirrored"]
        )
        is False
    )
    stamped = sc.channel_recipient_meta("discord", "c1", "u1")
    assert (
        sc.notify_channel_recipient_dropped(
            state, entry_meta=stamped, target_slot=slot, text="x", constraints=["mirrored"]
        )
        is True
    )
    # No transport is registered, so the task resolves nothing and sends nothing;
    # it still has to finish cleanly.
    await _settle_background(state)


@pytest.mark.asyncio
async def test_a_requeued_channel_steer_keeps_its_channel_provenance(tmp_path) -> None:
    """A steer the turn never consumed re-enters the queue as a channel message.

    ``_requeue_unconsumed_steers`` derives ``directive_user_origin`` from the
    per-steer map; the channel mark rides a sibling map so a requeued Discord
    steer runs its own turn under the same channel authority a queued one does.
    """
    state = _make_state(tmp_path)
    slot = _busy(state.get_or_create_slot("chat-1"))
    text = "steer that never landed"
    slot._pending_steers.append(text)
    slot._steer_user_origin[text] = True
    slot._steer_channel_origin[text] = True
    slot._steer_admissions[text] = sc.containment_meta(state, slot)

    cr._requeue_unconsumed_steers(state, slot)

    (entry,) = slot._queue
    assert entry["content"] == text
    assert entry.get("_directive_user_origin") is True
    assert entry.get("_directive_channel_origin") is True
    assert slot._steer_channel_origin == {}, "popped in lockstep with the other maps"


async def _handler_cancelled_inside_the_steer_rpc(tmp_path, *, accepted: bool):
    """Run the DM through the dispatcher and cancel the handler while the RPC is suspended.

    Discord's transport close cancels its handler tasks as an ordinary path
    (``client.py`` gathers them on close), so this is a cancellation the hand-off
    meets in production, not a crash. The RPC is held on an event, the handler is
    cancelled while it waits, and only then does the client answer *accepted*.
    Returns everything the assertions read once the loop has run on.
    """
    steer_client = _steerable()
    dispatcher, client, sessions, state, slot, enqueued = await _bound_to_busy_dashboard(
        tmp_path, steer_client=steer_client
    )
    rpc_entered = asyncio.Event()
    release_rpc = asyncio.Event()

    async def _answer_after_release(_msg):
        rpc_entered.set()
        await release_rpc.wait()
        return accepted

    steer_client.steer = AsyncMock(side_effect=_answer_after_release)

    handler = asyncio.ensure_future(dispatcher.handle_message(_message("keep the old name")))
    await rpc_entered.wait()
    handler.cancel()
    with pytest.raises(asyncio.CancelledError):
        await handler
    release_rpc.set()
    # The hand-off task has no await left after the RPC; a few iterations let it finish.
    for _ in range(10):
        await asyncio.sleep(0)
    return client, slot, enqueued


@pytest.mark.asyncio
async def test_a_cancelled_handler_still_reconciles_an_accepted_steer(tmp_path) -> None:
    """The hand-off finishes without its caller: row written, bookkeeping released.

    Awaited inline, the handler's cancellation lands inside the steer RPC: the
    client has the text, and everything behind the RPC -- the transcript row, the
    pop of the per-steer maps -- is skipped, so the text runs with no row while
    the maps keep its entry for the slot's lifetime (the ``steering_consumed``
    settle only removes the pending entry and patches an existing row). Shielded
    and strongly held, the hand-off task completes the reconciliation; the caller
    unwinds without the outcome, so no confirmation reaches the closing DM.
    """
    client, slot, enqueued = await _handler_cancelled_inside_the_steer_rpc(tmp_path, accepted=True)

    steer_row = next((m for m in slot.messages if m.get("meta", {}).get("steer")), None)
    assert steer_row is not None, "the accepted steer has its transcript row"
    assert steer_row["content"] == "keep the old name"
    assert steer_row["meta"].get(HUMAN_TURN_META_KEY) is True
    assert slot._steer_delivery_ids == {} and slot._steer_user_origin == {}
    assert slot._steer_channel_origin == {} and slot._steer_admissions == {}
    assert slot._pending_steers == ["keep the old name"], "delivered and live: the turn consumes it"
    assert len(slot._steer_audience_fences) == 1, "the audience's record stays for the turn"
    assert slot._queue == [] and enqueued == []
    assert ch._HANDOFFS_IN_FLIGHT == set(), "the strong reference is released on completion"
    assert not any("Steering" in t or "Queued" in t for t in _sent_texts(client))


@pytest.mark.asyncio
async def test_a_cancelled_handler_still_takes_the_queue_fallback_for_a_declined_steer(
    tmp_path,
) -> None:
    """The other half of the reconciliation survives the cancellation too.

    A steer the client declines is unwound and the text falls through to the
    slot's queue. Inline, the cancelled RPC leaves the optimistic registration
    standing and the text nowhere: a pending entry the teardown requeues onto a
    queue the DM was never told about, or discards on a hard kill.
    """
    client, slot, enqueued = await _handler_cancelled_inside_the_steer_rpc(tmp_path, accepted=False)

    assert [q["content"] for q in slot._queue] == ["keep the old name"]
    assert slot._pending_steers == [] and slot._steer_delivery_ids == {}
    assert slot._steer_user_origin == {} and slot._steer_channel_origin == {}
    assert enqueued == [], "the Discord-side queue stays out of it"
    assert ch._HANDOFFS_IN_FLIGHT == set()
    assert not any("Queued" in t for t in _sent_texts(client)), "no live transport to confirm to"


async def _turn_emits_monitor_watch_after_a_steer(tmp_path, monkeypatch, *, channel: bool):
    """Drive a real dashboard-origin turn that takes a steer, then emits ``monitor_watch``.

    The harness is ``test_acp_tool_identity``'s: the real ``_run_chat`` loop over a
    fake ACP client streaming ``AcpEvent``s, with ``apply_session_directive``
    replaced by a spy. Between the directive tool's call and its result the turn
    takes a steer -- a Discord human's through the hand-off (*channel*), or the
    composer's own (the counterfactual) -- so the directive the model emits next
    is one the steer could have shaped. Returns the spy and the slot.
    """
    from test_acp_tool_identity import _stub_state

    from kiro_crew import session_directive
    from kiro_crew.acp.types import (
        EVENT_COMPLETE,
        EVENT_TEXT_CHUNK,
        EVENT_TOOL_CALL,
        EVENT_TOOL_RESULT,
        AcpEvent,
    )
    from kiro_crew.dashboard.chat_delivery import steer_into_running_turn

    state = _stub_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot._titled = True
    args = {
        "kind": "github_pull_request",
        "target": "https://github.com/example/repo/pull/1",
        "objective": "review_ready",
    }
    marker = session_directive.encode("monitor_watch", args, "watching")
    outcomes: list = []

    async def _stream(_msg):
        if outcomes:
            # The steer the fake client never echoes as consumed is requeued at the
            # turn's end and runs as its own turn through this same stream: that
            # turn emits no directive, so the one directive under test is the
            # running dashboard turn's.
            yield AcpEvent(kind=EVENT_TEXT_CHUNK, text="done")
            yield AcpEvent(kind=EVENT_COMPLETE)
            return
        yield AcpEvent(
            kind=EVENT_TOOL_CALL,
            tool_call_id="tc-watch",
            title="Watching the pull request",
            tool_name="monitor_watch",
            mcp_server_name="kirocrew-core",
        )
        # The turn is running (its task is live) while the steer is admitted; the
        # harness awaits ``_run_chat`` directly, so the liveness the gate reads is
        # expressed by a task the way every other test here expresses it.
        _busy(slot)
        try:
            if channel:
                outcomes.append(
                    await ch.hand_to_resumed_slot(
                        state,
                        "dashboard:chat-1",
                        "watch that PR for me",
                        mode="steer",
                        has_attachments=False,
                        channel_type="discord",
                        conversation_id="c1",
                        principal="u1",
                    )
                )
            else:
                outcomes.append(
                    await steer_into_running_turn(
                        state, slot, "watch that PR for me", user_origin=True
                    )
                )
        finally:
            slot.task = None
        yield AcpEvent(
            kind=EVENT_TOOL_RESULT, tool_call_id="tc-watch", tool_output=marker, tool_final=True
        )
        yield AcpEvent(kind=EVENT_TEXT_CHUNK, text="ok")
        yield AcpEvent(kind=EVENT_COMPLETE)

    inner = _steerable()
    client = MagicMock()
    client.stream = _stream
    client.stream_command = _stream
    client.context_usage_pct = MagicMock(return_value=1.0)
    client.client = inner  # the inner ACP client the turn publishes on the slot
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
    spy = AsyncMock(return_value="[applied]")
    monkeypatch.setattr(cr, "apply_session_directive", spy)

    await cr._run_chat(state, slot, "go", _directive_user_origin=True)
    task = getattr(slot, "task", None)
    if task is not None:
        await task
    assert inner.steer.await_count == 1, (inner.steer.await_count, outcomes)
    inner.steer.assert_awaited_with("watch that PR for me")
    return spy, slot, outcomes


@pytest.mark.asyncio
async def test_a_directive_after_a_channel_steer_is_stamped_channel_origin(tmp_path, monkeypatch):
    """A Discord steer into a dashboard-origin turn narrows that turn's directives.

    The turn's ``_directive_channel_origin`` is its opener's (False here) and every
    directive it applies is stamped with it; a channel human's text injected
    mid-turn can shape the ``monitor_watch`` the model emits next, so unchanged the
    stamp would hand a channel-shaped input the dashboard's authority. The steer's
    admission sets the slot's ``_turn_channel_narrowed``, and the directive is
    filed as channel-created -- the authority a channel-origin turn's would carry.
    """
    spy, slot, outcomes = await _turn_emits_monitor_watch_after_a_steer(
        tmp_path, monkeypatch, channel=True
    )

    assert outcomes and outcomes[0].kind == ch.HANDOFF_STEERED
    spy.assert_called_once()
    assert spy.call_args.args[3] == "monitor_watch"
    assert spy.call_args.kwargs["producer_is_channel"] is True
    assert spy.call_args.kwargs["producer_is_user_facing"] is True
    assert slot._turn_channel_narrowed is False, "the narrowing ends with the turn"


@pytest.mark.asyncio
async def test_the_composers_own_steer_does_not_narrow_the_turn(tmp_path, monkeypatch):
    """Counterfactual: the session's own human steering from the composer changes
    nothing -- the directive keeps the dashboard-origin stamp the turn opened with."""
    spy, slot, outcomes = await _turn_emits_monitor_watch_after_a_steer(
        tmp_path, monkeypatch, channel=False
    )

    assert outcomes == ["steered"]
    spy.assert_called_once()
    assert spy.call_args.kwargs["producer_is_channel"] is False
    assert slot._turn_channel_narrowed is False


@pytest.mark.asyncio
async def test_a_close_during_the_steer_reports_the_text_queued_for_the_next_resume(
    tmp_path,
) -> None:
    """The tab is closed while the RPC is suspended; the text is NOT lost.

    ``_close_slot`` pops the slot and cancels its turn, whose teardown requeues the
    pending steer onto the popped object's queue, and the close's save archives
    that queue as ``queued_prompts``, drained when the session is next resumed.
    Reporting "NOT delivered" here makes the human resend and run the text twice;
    the outcome is read from the ARCHIVED record -- the persistence witness says
    the copy on disk carries the entry -- and worded as what it is.
    """
    steer_client = _steerable()
    dispatcher, client, sessions, state, slot, enqueued = await _bound_to_busy_dashboard(
        tmp_path, steer_client=steer_client
    )

    from kiro_crew.dashboard.slot_queue_repository import queue_persist_signature

    async def _close_then_accept(_msg):
        state._slots.pop("chat-1")
        cr._requeue_unconsumed_steers(state, slot)
        # The close's save archived the popped slot with that queue: the
        # persistence witness now matches the live queue.
        slot._queue_persisted_sig = queue_persist_signature(slot.durable_queue_entries())
        return True

    steer_client.steer = AsyncMock(side_effect=_close_then_accept)

    await dispatcher.handle_message(_message("finish the rename"))

    assert [q["content"] for q in slot._queue] == ["finish the rename"], "the archived queue"
    assert slot.queue_persist_pending is False
    assert state.get_slot("chat-1") is None
    texts = _sent_texts(client)
    assert not any("changed while your message was in flight" in t for t in texts), texts
    assert not any(_REFUSAL in t for t in texts), texts
    assert any("next resumed" in t for t in texts), texts
    assert enqueued == []


@pytest.mark.asyncio
async def test_a_close_before_the_teardown_requeues_reports_not_yet_saved(tmp_path) -> None:
    """The RPC returns between the pop and the cancelled turn's teardown.

    The client accepted the text and the steer persisted its row -- in memory, on
    a popped object the close's save may or may not still write. Only the archive
    can promise the text survives, so the outcome is the honest one: not yet
    saved, watch before resending. Neither "Steering" nor "Queued" is true.
    """
    steer_client = _steerable()
    dispatcher, client, sessions, state, slot, _ = await _bound_to_busy_dashboard(
        tmp_path, steer_client=steer_client
    )

    async def _close_then_accept(_msg):
        state._slots.pop("chat-1")
        return True

    steer_client.steer = AsyncMock(side_effect=_close_then_accept)

    await dispatcher.handle_message(_message("finish the rename"))

    steer_row = next(m for m in slot.messages if m.get("meta", {}).get("steer"))
    assert steer_row["meta"].get("steer_delivery_id"), "the row names its delivery"
    texts = _sent_texts(client)
    assert not any("Steering" in t or "Queued" in t or "Delivered" in t for t in texts), texts
    assert any("had not been saved with it yet" in t for t in texts), texts


@pytest.mark.asyncio
async def test_a_close_that_archived_before_the_requeue_does_not_report_queued(tmp_path) -> None:
    """Red-first for the archive-before-cleanup race.

    The close's teardown wait is bounded; a cleanup that outlasts it requeues the
    steer onto an object the archive already left behind, and nothing revisits a
    popped object. Here the registration is still only in memory (a Stop bumped
    the generation during the RPC, so the steer reports "requeued" by prediction)
    and the persistence witness says the archive lacks it: "queued" would name a
    message the archive does not carry.
    """
    steer_client = _steerable()
    dispatcher, client, sessions, state, slot, enqueued = await _bound_to_busy_dashboard(
        tmp_path, steer_client=steer_client
    )

    async def _close_and_stop_then_accept(_msg):
        state._slots.pop("chat-1")
        slot._stop_generation += 1
        return True

    steer_client.steer = AsyncMock(side_effect=_close_and_stop_then_accept)

    await dispatcher.handle_message(_message("finish the rename"))

    assert slot._pending_steers == ["finish the rename"], "registered in memory only"
    assert slot._queue == [], "no requeue happened, so nothing is archived"
    texts = _sent_texts(client)
    assert not any("Queued" in t or "next resumed" in t for t in texts), texts
    assert any("had not been saved with it yet" in t for t in texts), texts
    assert enqueued == []


@pytest.mark.asyncio
async def test_a_requeue_the_archive_missed_is_not_reported_queued(tmp_path) -> None:
    """The requeued entry sits on the popped object but the archive was written without it."""
    steer_client = _steerable()
    dispatcher, client, sessions, state, slot, _ = await _bound_to_busy_dashboard(
        tmp_path, steer_client=steer_client
    )

    async def _archive_then_requeue(_msg):
        state._slots.pop("chat-1")
        # The close's save ran first (the queue archived without this entry) and
        # the cancelled turn's teardown requeued afterwards.
        cr._requeue_unconsumed_steers(state, slot)
        return True

    steer_client.steer = AsyncMock(side_effect=_archive_then_requeue)

    await dispatcher.handle_message(_message("finish the rename"))

    assert [q["content"] for q in slot._queue] == ["finish the rename"]
    assert slot.queue_persist_pending is True, "the copy on disk lacks the entry"
    texts = _sent_texts(client)
    assert not any("Queued" in t for t in texts), texts
    assert any("had not been saved with it yet" in t for t in texts), texts


@pytest.mark.asyncio
async def test_a_successor_holding_the_text_in_its_queue_reports_queued(tmp_path) -> None:
    """A slot recreated under the same key that carries the text in its queue.

    The successor's queue is one a drain reaches, so the text runs there; the
    ordinary queued confirmation is right, and nothing is appended a second time.
    """
    steer_client = _steerable(accepted=False)
    dispatcher, client, sessions, state, slot, enqueued = await _bound_to_busy_dashboard(
        tmp_path, steer_client=steer_client
    )

    async def _replace_and_carry(_msg):
        # The restored entry carries the steer's delivery id in its durable meta,
        # the way a teardown requeue writes it and a resume restores it.
        did = slot._steer_delivery_ids["finish the rename"]
        state._slots.pop("chat-1")
        fresh = _busy(state.get_or_create_slot("chat-1"))
        fresh.queue_append("finish the rename", meta={"steer_delivery_id": did})
        return False

    steer_client.steer = AsyncMock(side_effect=_replace_and_carry)

    await dispatcher.handle_message(_message("finish the rename"))

    fresh = state.get_slot("chat-1")
    assert fresh is not slot
    assert [q["content"] for q in fresh._queue] == ["finish the rename"], "once, on the live object"
    assert slot._queue == [], "never appended to the detached object"
    texts = _sent_texts(client)
    assert any("Queued for that session" in t and "next resumed" not in t for t in texts), texts
    assert not any("changed while your message was in flight" in t for t in texts), texts
    assert enqueued == []


@pytest.mark.asyncio
async def test_a_successor_that_ran_the_text_reports_delivered(tmp_path) -> None:
    """The successor's transcript names this delivery: the text ran there as its own turn."""
    steer_client = _steerable(accepted=False)
    dispatcher, client, sessions, state, slot, _ = await _bound_to_busy_dashboard(
        tmp_path, steer_client=steer_client
    )

    async def _replace_and_run(_msg):
        # The drained row unions the entry's meta, delivery id included.
        did = slot._steer_delivery_ids["finish the rename"]
        state._slots.pop("chat-1")
        fresh = _busy(state.get_or_create_slot("chat-1"))
        fresh.append("user", "finish the rename", "msg msg-u", meta={"steer_delivery_ids": [did]})
        return False

    steer_client.steer = AsyncMock(side_effect=_replace_and_run)

    await dispatcher.handle_message(_message("finish the rename"))

    fresh = state.get_slot("chat-1")
    assert fresh._queue == [] and slot._queue == []
    texts = _sent_texts(client)
    assert any("Delivered to that session" in t for t in texts), texts
    assert not any("changed while your message was in flight" in t for t in texts), texts


@pytest.mark.asyncio
async def test_a_close_during_a_declined_steer_still_refuses(tmp_path) -> None:
    """Counterfactual: nothing holds the text, so the refusal stands.

    A declined steer's registration is unwound and the session is closed with no
    successor: no queue the close archives carries the text and no turn ran it.
    Refusing is the truthful answer, and the fallback never appends to the
    detached object.
    """
    steer_client = _steerable(accepted=False)
    dispatcher, client, sessions, state, slot, enqueued = await _bound_to_busy_dashboard(
        tmp_path, steer_client=steer_client
    )

    async def _close_then_decline(_msg):
        state._slots.pop("chat-1")
        return False

    steer_client.steer = AsyncMock(side_effect=_close_then_decline)

    await dispatcher.handle_message(_message("finish the rename"))

    assert slot._queue == [] and slot._pending_steers == []
    texts = _sent_texts(client)
    assert any("changed while your message was in flight" in t for t in texts), texts
    assert not any("Queued" in t or "Delivered" in t for t in texts), texts
    assert enqueued == []


@pytest.mark.asyncio
async def test_a_stranded_message_whose_text_recurs_in_the_successor_transcript_is_still_refused(
    tmp_path,
) -> None:
    """Reconciliation is by delivery id, never by content.

    A slot recreated under the same key restores its whole prior transcript. A
    short message that recurs there ("continue") must not prove a delivery that
    never happened: the declined steer's registration is unwound, no record
    carries this delivery's id, and the author is told so rather than left
    waiting for a reply that never comes.
    """
    steer_client = _steerable(accepted=False)
    dispatcher, client, sessions, state, slot, enqueued = await _bound_to_busy_dashboard(
        tmp_path, steer_client=steer_client
    )

    async def _replace_with_history(_msg):
        state._slots.pop("chat-1")
        fresh = _busy(state.get_or_create_slot("chat-1"))
        # The restored transcript: the same words, from an earlier turn.
        fresh.append("user", "continue", "msg msg-u")
        fresh.append("assistant", "Continuing.", "msg msg-a")
        return False

    steer_client.steer = AsyncMock(side_effect=_replace_with_history)

    await dispatcher.handle_message(_message("continue"))

    fresh = state.get_slot("chat-1")
    assert fresh._queue == [] and slot._queue == []
    texts = _sent_texts(client)
    assert any("changed while your message was in flight" in t for t in texts), texts
    assert not any("Delivered" in t or "Queued" in t for t in texts), texts
    assert enqueued == []


@pytest.mark.asyncio
async def test_a_full_live_queue_refuses_the_enqueue(tmp_path) -> None:
    """The slot's live queue is bounded; the hand-off refuses at the bound.

    Every producer that appends to the live queue guards ``MAX_LIVE_QUEUE_ENTRIES``
    itself. Appending past it, or evicting a waiting entry (someone's text with no
    other copy), are both wrong; the channel human still holds the text and is
    told why it was not added.
    """
    from kiro_crew.dashboard.slot_queue_repository import MAX_LIVE_QUEUE_ENTRIES

    dispatcher, client, sessions, state, slot, enqueued = await _bound_to_busy_dashboard(
        tmp_path, steer_client=None
    )
    for n in range(MAX_LIVE_QUEUE_ENTRIES):
        slot.queue_append(f"waiting {n}")

    await dispatcher.handle_message(_message("one more"))

    assert len(slot._queue) == MAX_LIVE_QUEUE_ENTRIES, "nothing appended past the bound"
    assert not any(q["content"] == "one more" for q in slot._queue)
    texts = _sent_texts(client)
    assert any("queue is full" in t and "NOT added" in t for t in texts), texts
    assert not any("Queued for that session" in t for t in texts), texts
    assert enqueued == []


@pytest.mark.asyncio
async def test_the_fence_cap_fallthrough_still_meets_the_queue_bound(tmp_path) -> None:
    """At the fence cap the queue arm is the fallback, and its bound is the one refusal left."""
    from kiro_crew.dashboard.chat_delivery import MAX_PENDING_STEERS
    from kiro_crew.dashboard.slot_queue_repository import MAX_LIVE_QUEUE_ENTRIES

    steer_client = _steerable()
    dispatcher, client, sessions, state, slot, _ = await _bound_to_busy_dashboard(
        tmp_path, steer_client=steer_client
    )
    recorded = {f"peer-{n}": {"marker": n} for n in range(MAX_PENDING_STEERS)}
    slot._steer_audience_fences.update(recorded)
    for n in range(MAX_LIVE_QUEUE_ENTRIES):
        slot.queue_append(f"waiting {n}")

    await dispatcher.handle_message(_message("one more"))

    steer_client.steer.assert_not_awaited()
    assert slot._steer_audience_fences == recorded
    assert len(slot._queue) == MAX_LIVE_QUEUE_ENTRIES
    assert any("queue is full" in t for t in _sent_texts(client)), _sent_texts(client)
