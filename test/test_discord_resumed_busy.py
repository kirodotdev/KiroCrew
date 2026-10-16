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

The hand-off's later life -- the drop notice a DM gets when the drain drops its
queued entry, the recovery requeues that keep or refuse that address, the bounded
fence set, a handler cancelled inside the steer RPC, the channel origin a later
directive carries, a close racing the steer, and the live queue's bound -- is pinned
in ``test_discord_resumed_busy_after_handoff.py``. The builders both modules use live
in ``discord_resumed_busy_helpers.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord_resumed_busy_helpers
import pytest
from chat_test_helpers import _make_state
from discord_resumed_busy_helpers import (
    _REFUSAL,
    _SECRET,
    _TYPED,
    _bound_to_busy_dashboard,
    _busy,
    _sent_texts,
    _steerable,
)
from test_discord import _prime_live
from test_discord_sessions import _bind_to_chat1, _dispatcher, _log, _message

from kiro_crew.dashboard import chat_delivery as cd
from kiro_crew.dashboard import chat_runner as cr
from kiro_crew.dashboard import session_control as sc
from kiro_crew.history import HUMAN_TURN_META_KEY
from kiro_crew.messaging.transport import InboundMessage

# The shared autouse fixture, bound here by name so pytest registers it for this module.
_isolated_home = discord_resumed_busy_helpers._isolated_home


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


@pytest.mark.parametrize("command", ["/clear", "/model gpt-5", "/not-a-command at all"])
@pytest.mark.asyncio
async def test_a_queued_handoff_entry_is_turn_content_never_a_dashboard_command(
    tmp_path, command
) -> None:
    """``!queue /clear`` during a dashboard turn queues the WORDS "/clear", stamped so
    the drain never reads them as the command that wipes the session.

    The channel's own queue already replays every entry with command interpretation
    off; this entry crosses into the slot's queue in its place and keeps that rule.
    The directive prefix is stripped before the hand-off, so the text alone cannot
    tell the drain -- the entry's meta does.
    """
    dispatcher, client, sessions, state, slot, enqueued = await _bound_to_busy_dashboard(
        tmp_path, steer_client=None
    )

    await dispatcher.handle_message(_message(f"!queue {command}"))

    (entry,) = slot._queue
    assert entry["content"] == command
    assert entry["meta"].get(cr.COMMANDS_OFF_META_KEY) is True
    assert enqueued == []


@pytest.mark.asyncio
async def test_the_drain_keeps_a_stamped_entry_out_of_command_interpretation(
    tmp_path, monkeypatch
) -> None:
    """The stamp reaches ``_run_chat`` as ``_commands_off``; a plain dashboard entry
    carries nothing, so the composer's own queued ``/clear`` still runs as before."""
    state = _make_state(tmp_path)
    state.subagents = None
    slot = _busy(state.get_or_create_slot("chat-1"))
    cd.queue_for_next_turn(
        state,
        slot,
        "/clear",
        directive_user_origin=True,
        directive_channel_origin=True,
        commands_off=True,
    )
    slot.task = None
    captured: dict[str, object] = {}

    def _stub_run_chat(_state, _slot, _prompt, **kwargs):
        captured.update(kwargs)

        async def _done():
            return None

        return _done()

    def _fake_spawn(_state, _slot, coro):
        coro.close()
        task = MagicMock()
        task.done.return_value = True
        return task

    monkeypatch.setattr(cr, "_run_chat", _stub_run_chat)
    monkeypatch.setattr(cr, "spawn_guarded_turn", _fake_spawn)

    assert await cr._start_next_queued_turn(state, slot) is True
    assert captured["_commands_off"] is True

    captured.clear()
    slot = _busy(state.get_or_create_slot("chat-2"))
    cd.queue_for_next_turn(state, slot, "/clear", directive_user_origin=True)
    slot.task = None
    assert await cr._start_next_queued_turn(state, slot) is True
    assert "_commands_off" not in captured, "the composer's own queued command is unchanged"


@pytest.mark.parametrize("off", [True, False])
@pytest.mark.asyncio
async def test_run_chat_hands_a_commands_off_message_to_the_model_as_text(
    tmp_path, monkeypatch, off
) -> None:
    """With the switch, "/clear" goes to ``stream`` as text; without it, to
    ``stream_command`` -- the dashboard's own path is the parity half."""
    from chat_test_helpers import pin_kiro_cli_harness

    pin_kiro_cli_harness()
    from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.context_builder = None
    state.consolidator = None
    state._hook_store = None
    state._yolo = False
    slot = state.get_or_create_slot("s1")
    client = MagicMock()
    client.context_usage_pct = MagicMock(return_value=10.0)
    streamed: list[tuple[str, str]] = []

    def _stream(kind):
        async def _run(msg):
            streamed.append((kind, msg))
            yield LLMEvent(kind=EVENT_COMPLETE)

        return _run

    client.stream = _stream("stream")
    client.stream_command = _stream("stream_command")
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

    await cr._run_chat(state, slot, "/clear", _commands_off=off)

    kinds = [k for k, _ in streamed]
    if off:
        assert kinds == ["stream"], streamed
        assert "/clear" in streamed[0][1]
    else:
        assert kinds == ["stream_command"], streamed


@pytest.mark.parametrize("off", [True, False])
@pytest.mark.asyncio
async def test_a_recovery_requeue_keeps_the_commands_off_stamp(tmp_path, monkeypatch, off) -> None:
    """A commands-off turn that dies before output is requeued verbatim by the
    runner's recovery; the requeued entry carries the stamp, so the retry's drain
    hands "/clear" to the model again instead of running it. A plain turn's requeue
    carries no stamp, so the composer's own retried command still runs."""
    from chat_test_helpers import pin_kiro_cli_harness

    pin_kiro_cli_harness()
    from kiro_crew.acp.client import AcpError

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.context_builder = None
    state.consolidator = None
    state._hook_store = None
    state._yolo = False
    slot = state.get_or_create_slot("s1")
    client = MagicMock()
    client.context_usage_pct = MagicMock(return_value=10.0)

    async def _die(_msg):
        raise AcpError("ACP process exited (code=-15)")
        yield  # an async generator

    client.stream = _die
    client.stream_command = _die
    client.shutdown = AsyncMock()
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
    state.sessions.reset = AsyncMock()

    async def _no_drain(_state, _slot, **_kwargs) -> bool:
        return False

    monkeypatch.setattr(cr, "_start_next_queued_turn", _no_drain)

    await cr._run_chat(state, slot, "/clear", _commands_off=off)

    requeued = [q for q in slot._queue if q.get("content") == "/clear"]
    assert requeued, f"expected the verbatim recovery requeue, queue={slot._queue}"
    assert (requeued[0].get("meta") or {}).get(cr.COMMANDS_OFF_META_KEY) is (True if off else None)


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
