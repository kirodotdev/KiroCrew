"""Where a pending steer goes when it cannot be injected before the turn moves on.

The queue half of ``test_pending_steer``: the sender's notice when a held steer is
refused, the fence the queue fallbacks gate on, the queue order of held steers ahead of
plain sends, a turn change, the boundary flush passes, a real Claude client that only
buffered the steer, and a client that cannot steer at all.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_test_helpers import _make_state
from pending_steer_helpers import (
    _AWAIT_SECS,
    TEXT,
    _hand_off,
    _loss_recovery_target,
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
    _steerable,
)

from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard import session_control as sc
from kiro_crew.testing.wait import await_until


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


@pytest.mark.asyncio
async def test_a_tool_result_in_a_running_stream_injects_a_held_steer(tmp_path):
    """A real ``_run_chat`` stream: the tool result is the boundary that flushes a held steer.

    The steer is held while the turn has no client. The turn then publishes a
    steer-capable client, a tool call finishes, and the stream loop must inject the
    held text into that same turn instead of leaving it for the end-of-turn queue.
    """
    from chat_runner_coverage_helpers import (
        _async_iter,
        _coding_tool_call,
        _coding_tool_result,
        _complete,
        _drive,
    )

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _running(_peer_target(state, "chat-2", caller))
    target._titled = True

    out = await _send(state, caller)
    assert out.get("pending_steer") is True, "held while the turn has no client"
    assert not target._queue

    client = AsyncMock()
    client.context_usage_pct = MagicMock(return_value=0.0)
    client.context_usage_unknown = MagicMock(return_value=False)
    client.context_window_tokens = MagicMock(return_value=0)
    client.context_used_tokens = MagicMock(return_value=0)
    client.mcp_session_report = MagicMock(return_value=None)
    client.set_mcp_report_changed_hook = MagicMock()
    client.last_prompt_stats = None
    client.exit_code = None
    # The inner client the turn publishes on the slot, which the boundary steers.
    inner = _steerable(accepted=True)
    client.client = inner
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
    state.sessions.consume_replay_suppression = MagicMock(return_value=False)
    state.sessions.consume_needs_reinjection = MagicMock(return_value=False)
    state._hook_store = MagicMock(fire=AsyncMock(return_value=[]))
    # The boundary flush is a task that hops to threads to warm its gate, while a
    # real model step after a tool result runs for seconds. A script that completes
    # right after the result ends the turn first on a slow runner, and the steer is
    # queued instead. So the model step here lasts until the steer is injected.
    steered = asyncio.Event()

    def _took_the_steer(*_args, **_kwargs):
        steered.set()
        return True

    inner.steer.side_effect = _took_the_steer

    async def _model_step_outlasting_the_flush():
        yield _coding_tool_call()
        yield _coding_tool_result()
        await await_until(steered, what="the tool boundary to inject the held steer")
        yield _complete()

    calls = {"n": 0}

    def _stream(*_args, **_kwargs):
        # Only the first turn carries the tool call, as _set_stream scripts it.
        calls["n"] += 1
        if calls["n"] == 1:
            return _model_step_outlasting_the_flush()
        return _async_iter([_complete()])

    client.stream = MagicMock(side_effect=_stream)
    client.stream_command = MagicMock(side_effect=_stream)

    await _drive(state, target, "do the work")

    inner.steer.assert_awaited_once()
    injected = inner.steer.await_args.args[0]
    assert TEXT in injected and injected.startswith("[sent by session ")
    assert not target._deferred_steers, "the held steer settled at the boundary"


async def _hold_and_spy_the_fence(tmp_path, monkeypatch, *, fenced_at_send, fenced_now):
    """Hold a steer with one fence, then make the live read return the other.

    Returns ``(state, target, seen)``: ``seen`` collects the ownership fence each
    later authorization of that steer is given.
    """
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
        caller_fenced=fenced_at_send,
    )
    assert out.get("pending_steer") is True

    seen: list = []
    real = sm.authorize_target

    def _spy(*args, **kwargs):
        seen.append(kwargs.get("precomputed_ownership_fenced"))
        return real(*args, **kwargs)

    monkeypatch.setattr(sm, "authorize_target", _spy)
    monkeypatch.setattr(sm, "_caller_is_ownership_fenced", lambda *_a: fenced_now)
    return state, target, seen


_FENCE_DIRECTIONS = pytest.mark.parametrize(
    "fenced_at_send,fenced_now",
    [
        pytest.param(True, False, id="fenced-at-send-only"),
        pytest.param(False, True, id="fenced-now-only"),
    ],
)


@pytest.mark.asyncio
@_FENCE_DIRECTIONS
async def test_the_turn_end_queue_fallback_gates_on_the_tighter_fence(
    tmp_path, monkeypatch, fenced_at_send, fenced_now
):
    """The turn ends with the steer held: ``requeue_all`` queues it through a gate
    that is fenced when either the send or the queue write says so."""
    from kiro_crew.dashboard import deferred_steer

    state, target, seen = await _hold_and_spy_the_fence(
        tmp_path, monkeypatch, fenced_at_send=fenced_at_send, fenced_now=fenced_now
    )
    try:
        await deferred_steer.requeue_all(state, target)
    finally:
        target.task.cancel()

    assert seen == [True], "the queue fallback must authorize with the tighter fence"
    assert any(TEXT in str(entry) for entry in target._queue), "and it queued the steer"
    assert not target._deferred_steers


@pytest.mark.asyncio
@_FENCE_DIRECTIONS
async def test_the_stale_turn_queue_fallback_gates_on_the_tighter_fence(
    tmp_path, monkeypatch, fenced_at_send, fenced_now
):
    """A boundary of a later turn finds the steer held for an ended one: it queues it
    through a gate that is fenced when either the send or the queue write says so."""
    from kiro_crew.dashboard import deferred_steer

    state, target, seen = await _hold_and_spy_the_fence(
        tmp_path, monkeypatch, fenced_at_send=fenced_at_send, fenced_now=fenced_now
    )
    held_in_turn = target._turn_generation
    target.task.cancel()
    _running(target)  # the next turn, which the held steer was not sent into
    assert target._turn_generation != held_in_turn
    target._acp_client = _steerable(accepted=True)
    try:
        await deferred_steer.flush_at_boundary(state, target)
    finally:
        target.task.cancel()

    assert seen == [True], "the stale-turn arm must authorize with the tighter fence"
    target._acp_client.steer.assert_not_awaited()
    assert any(TEXT in str(entry) for entry in target._queue), "and it queued the steer"
    assert not target._deferred_steers


def _queued_order(target, *texts):
    """The index in ``target._queue`` of the entry carrying each text, in *texts* order."""
    contents = [entry["content"] for entry in target._queue]
    return [next(i for i, c in enumerate(contents) if text in c) for text in texts]


@pytest.mark.asyncio
async def test_a_held_steer_the_turn_outlived_queues_ahead_of_a_later_plain_send(tmp_path):
    """A held steer joins the HEAD of the queue at the turn's end, as an unconsumed steer
    does, so a plain message sent after it does not run first."""
    from kiro_crew.dashboard import deferred_steer

    state = _make_state(tmp_path)
    caller, target, client = _loss_recovery_target(state)
    held = await _send(state, caller)
    assert held.get("pending_steer") is True
    later = "a plain note sent after the steer"
    assert (await _send_plain(state, caller, later))["started"] is False

    try:
        await deferred_steer.requeue_all(state, target)
    finally:
        target.task.cancel()

    assert _queued_order(target, TEXT, later) == [0, 1], [e["content"] for e in target._queue]
    assert target._deferred_steer_receipts[held["steer_id"]]["state"].startswith("queued@")


@pytest.mark.asyncio
async def test_several_held_steers_queue_in_send_order_ahead_of_plain_sends(tmp_path):
    """Two held steers keep the order they were sent in, both ahead of queued plain text."""
    from kiro_crew.dashboard import deferred_steer

    state = _make_state(tmp_path)
    caller, target, client = _loss_recovery_target(state)
    plain = "a plain note queued before either steer"
    second = "and run the full gate after the rebase"
    assert (await _send_plain(state, caller, plain))["started"] is False
    assert (await _send(state, caller)).get("pending_steer") is True
    assert (await _send(state, caller, message=second)).get("pending_steer") is True

    try:
        await deferred_steer.requeue_all(state, target)
    finally:
        target.task.cancel()

    assert _queued_order(target, TEXT, second, plain) == [0, 1, 2], [
        e["content"] for e in target._queue
    ]
    assert not target._deferred_steers


@pytest.mark.asyncio
async def test_a_held_steer_orphaned_by_a_turn_change_queues_at_the_head(tmp_path):
    """The stale-turn arm queues a held steer its turn outlived at the HEAD too."""
    from kiro_crew.dashboard import deferred_steer

    state = _make_state(tmp_path)
    caller, target, client = _loss_recovery_target(state)
    assert (await _send(state, caller)).get("pending_steer") is True
    later = "a plain note sent after the steer"
    assert (await _send_plain(state, caller, later))["started"] is False
    target.task.cancel()
    _running(target)  # the next turn, which the held steer was not sent into
    try:
        await deferred_steer.flush_at_boundary(state, target)
    finally:
        target.task.cancel()

    client.steer.assert_not_awaited()
    assert _queued_order(target, TEXT, later) == [0, 1], [e["content"] for e in target._queue]


def _turn_changes_on_call(target, nth):
    """A ``prewarm_enabled_check`` whose *nth* call is a hand-off: turn G ends, G+1 starts.

    The hand-off closes steer admission for G and the queued turn's task assignment
    moves ``_turn_generation`` to G+1, as ``_hand_off_queue`` and the ``task`` setter do.
    """
    calls = []

    async def _check():
        calls.append(True)
        if len(calls) == nth:
            target._deferred_steer_ended_gen = target._turn_generation
            target.task.cancel()
            _running(target)

    return _check


@pytest.mark.asyncio
async def test_a_steer_sent_during_a_turn_change_is_injected_into_the_new_turn(
    tmp_path, monkeypatch
):
    """A steer whose send spans a turn change is held for the turn running at the hold."""
    from kiro_crew.dashboard import deferred_steer

    state = _make_state(tmp_path)
    caller, target, client = _loss_recovery_target(state)
    # Call 1 is the prewarm before the gate; call 2 is the re-gate after the steer RPC.
    monkeypatch.setattr(sc, "prewarm_enabled_check", _turn_changes_on_call(target, 2))
    sent_in = target._turn_generation

    out = await _send(state, caller)

    assert target._turn_generation == sent_in + 1, "the turn changed during the send"
    assert out.get("pending_steer") is True, out
    try:
        await deferred_steer.flush_at_boundary(state, target)
    finally:
        target.task.cancel()

    client.steer.assert_awaited_once()
    assert TEXT in client.steer.await_args.args[0]
    assert not target._queue, [e["content"] for e in target._queue]
    assert target._deferred_steer_receipts[out["steer_id"]]["state"].startswith("delivered@")


def _gated_target(state):
    """A running target whose steers each wait for ``release``; records overlap of flushes."""
    caller = _slot(state, "chat-1")
    target = _running(_peer_target(state, "chat-2", caller))
    release = asyncio.Event()
    started = asyncio.Event()

    async def _gated_steer(_text):
        started.set()
        await release.wait()
        return True

    client = _steerable(accepted=True)
    client.steer_needs_loss_recovery = False
    client.steer = AsyncMock(side_effect=_gated_steer)
    return caller, target, client, release, started


@pytest.mark.asyncio
async def test_a_steer_held_during_a_running_flush_gets_a_follow_up_pass(tmp_path):
    """A boundary that arrives while a flush awaits an ack runs one more pass afterwards.

    The running pass walks the held list it copied at its start, so a steer held
    meanwhile is tried by the follow-up pass, with no third boundary.
    """
    from kiro_crew.dashboard import deferred_steer

    state = _make_state(tmp_path)
    caller, target, client, release, started = _gated_target(state)
    first = await _send(state, caller)
    assert first.get("pending_steer") is True
    target._acp_client = client

    deferred_steer.schedule_flush(state, target)
    task = target._deferred_flush_task
    await asyncio.wait_for(started.wait(), _AWAIT_SECS)

    # Steer B is held while A's injection awaits its ack (no client at B's send).
    target._acp_client = None
    other = "a second steer, held while the first one is in flight"
    second = await _send(state, caller, message=other)
    assert second.get("pending_steer") is True, second
    target._acp_client = client

    deferred_steer.schedule_flush(state, target)  # the second boundary
    assert target._deferred_flush_task is task, "single flight: no second task"
    release.set()
    try:
        await asyncio.wait_for(task, _AWAIT_SECS)
    finally:
        target.task.cancel()

    assert target._deferred_flush_task is task, "the follow-up pass runs in the same task"
    assert client.steer.await_count == 2, client.steer.await_args_list
    assert other in client.steer.await_args_list[1].args[0]
    assert not target._deferred_steers, "B is not left held"
    receipts = target._deferred_steer_receipts
    assert receipts[first["steer_id"]]["state"].startswith("delivered@")
    assert receipts[second["steer_id"]]["state"].startswith("delivered@")


@pytest.mark.asyncio
async def test_a_flush_with_no_boundary_meanwhile_runs_one_pass(tmp_path, monkeypatch):
    """No boundary arrived during the flush: exactly one pass, one attempt per steer."""
    from kiro_crew.dashboard import deferred_steer

    state = _make_state(tmp_path)
    caller, target, client, release, _started = _gated_target(state)
    assert (await _send(state, caller)).get("pending_steer") is True
    target._acp_client = client
    passes = []
    real = deferred_steer.flush_at_boundary

    async def _counted(*args):
        passes.append(True)
        await real(*args)

    monkeypatch.setattr(deferred_steer, "flush_at_boundary", _counted)

    release.set()
    deferred_steer.schedule_flush(state, target)
    try:
        await asyncio.wait_for(target._deferred_flush_task, _AWAIT_SECS)
    finally:
        target.task.cancel()

    assert len(passes) == 1
    client.steer.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_cancelled_flush_starts_no_follow_up_pass(tmp_path, monkeypatch):
    """The hand-off's settle cancels a stuck flush: a boundary marked meanwhile restarts nothing."""
    from kiro_crew.dashboard import deferred_steer

    state = _make_state(tmp_path)
    caller, target, client, _release, started = _gated_target(state)
    assert (await _send(state, caller)).get("pending_steer") is True
    target._acp_client = client
    monkeypatch.setattr(
        deferred_steer, "FLUSH_SETTLE_SECS", 0.05
    )  # flake-ok: the steer is never released, so the wait always expires

    deferred_steer.schedule_flush(state, target)
    task = target._deferred_flush_task
    await asyncio.wait_for(started.wait(), _AWAIT_SECS)
    deferred_steer.schedule_flush(state, target)  # a boundary while the flush awaits
    try:
        await deferred_steer._settle_flush_task(target)
        for _ in range(5):
            await asyncio.sleep(0)
    finally:
        target.task.cancel()

    assert task.cancelled()
    assert target._deferred_flush_task is task, "no new task after the cancel"
    client.steer.assert_awaited_once()


async def _buffered_by_a_real_claude_client(state, spawned):
    """Hold a peer steer for a real claude client and run a boundary while a call is open.

    The client buffers the injection behind the still-open call and answers True with
    nothing written, so the steer is marked delivered but never left the gateway.
    Returns the caller, the target, the client, its wire, the running turn and the
    send reply.
    """
    from test_claude_steering_request import (
        _client,
        _close_tool,
        _open_tool,
        _start_turn,
        _wait_closed,
        _Wire,
    )

    from kiro_crew.dashboard import deferred_steer
    from kiro_crew.testing.wait import async_wait_until

    caller = _slot(state, "chat-1")
    target = _running(_peer_target(state, "chat-2", caller))
    client = _client()
    wire = _Wire(client)
    turn = await _start_turn(client, spawned)
    target._acp_client = client

    out = await _send(state, caller)
    assert out.get("pending_steer") is True, "refused at send time on claude, so held"

    # Two parallel tool calls; the first one's result is the boundary.
    wire.inbound.put_nowait(_open_tool("tc1"))
    wire.inbound.put_nowait(_open_tool("tc2"))
    await async_wait_until(lambda: len(client._active_tool_calls) == 2)
    wire.inbound.put_nowait(_close_tool("tc1"))
    await _wait_closed(client, "tc1")
    await deferred_steer.flush_at_boundary(state, target)
    assert target._deferred_steer_delivered, "marked before the client answered"
    assert client._steers_held, "the client buffered it behind the open call"
    assert wire.steering_frames() == []
    return caller, target, client, wire, turn, out


@pytest.mark.asyncio
async def test_a_steer_a_real_claude_client_only_buffered_is_queued_when_the_turn_stops(tmp_path):
    """A held steer that never left the gateway runs as the next turn.

    The turn is stopped before the open call closes, which drops the client's buffer
    unwritten. The delivered mark must not keep the text out of the queue.
    """
    from test_claude_steering_request import _Tasks

    from kiro_crew.dashboard.chat_turn.steer_queue import _mark_held_steers_possibly_delivered
    from kiro_crew.testing.wait import await_until

    state = _make_state(tmp_path)
    async with _Tasks() as spawned:
        caller, target, client, wire, turn, out = await _buffered_by_a_real_claude_client(
            state, spawned
        )
        wire.end_turn("cancelled")
        await await_until(turn, what="the turn")
        assert wire.steering_frames() == [], "nothing was ever written"
        # The turn's release reads the client before it drops the ref.
        _mark_held_steers_possibly_delivered(target, client)
        target._acp_client = None

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
            if target.task is not None:
                target.task.cancel()

    assert spawn.call_count == 1, "the steer runs as the next turn"
    assert any(TEXT in str(call) for call in run_chat.call_args_list)
    assert target._deferred_steer_receipts[out["steer_id"]]["state"].startswith("queued@")
    assert not any("may not have run" in notice for notice in _sender_notices(caller))


@pytest.mark.asyncio
async def test_a_hard_stop_of_a_steer_a_real_claude_client_only_buffered_says_it_will_not_run(
    tmp_path, monkeypatch
):
    """The text never left the gateway, so the sender hears it will not run."""
    from test_claude_steering_request import _Tasks

    from kiro_crew.dashboard import chat_handlers as ch

    state = _make_state(tmp_path)
    fake = MagicMock()
    for module in (sc, ch):
        monkeypatch.setattr(module, "sel", lambda fake=fake: fake)
    monkeypatch.setattr(sc, "_sel_off_loop", lambda write, what: write())
    async with _Tasks() as spawned:
        caller, target, _client, _wire, _turn, out = await _buffered_by_a_real_claude_client(
            state, spawned
        )
        state.sessions.stop_turn = AsyncMock(return_value="cancelled")
        await ch.stop_slot_turn(state, target)
        await ch.stop_slot_turn(state, target)

    notices = _sender_notices(caller)
    assert len(notices) == 1, notices
    assert "It was not delivered and will not run." in notices[0]
    assert target._deferred_steer_receipts[out["steer_id"]]["state"].startswith("discarded@")


@pytest.mark.asyncio
async def test_a_boundary_during_a_flush_runs_no_pass_once_the_hand_off_began(
    tmp_path, monkeypatch
):
    """The hand-off reads the held list once the flush is settled: no pass may follow it."""
    from kiro_crew.dashboard import deferred_steer

    state = _make_state(tmp_path)
    caller, target, client, release, started = _gated_target(state)
    assert (await _send(state, caller)).get("pending_steer") is True
    target._acp_client = client
    passes = []
    real = deferred_steer.flush_at_boundary

    async def _counted(*args):
        passes.append(True)
        await real(*args)

    monkeypatch.setattr(deferred_steer, "flush_at_boundary", _counted)

    deferred_steer.schedule_flush(state, target)
    task = target._deferred_flush_task
    await asyncio.wait_for(started.wait(), _AWAIT_SECS)
    deferred_steer.schedule_flush(state, target)  # a boundary while the flush awaits
    # The hand-off starts before the pass ends (`_hand_off_queue`'s first step).
    target._deferred_steer_ended_gen = target._turn_generation
    release.set()
    try:
        await asyncio.wait_for(task, _AWAIT_SECS)
    finally:
        target.task.cancel()

    assert len(passes) == 1, "no pass after the hand-off began"
    assert target._deferred_flush_dirty is False


@pytest.mark.asyncio
async def test_a_steer_to_a_client_that_cannot_steer_is_queued_not_held(tmp_path):
    """No boundary injection can happen, so the steer queues as a plain send would.

    Past the held-steer cap too: each one is queued, none is refused.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _running(_peer_target(state, "chat-2", caller))
    client = _steerable(accepted=True)
    client.supports_steer = False
    target._acp_client = client
    from kiro_crew.dashboard import deferred_steer

    try:
        replies = [
            await _send(state, caller, message=f"{TEXT} ({n})")
            for n in range(deferred_steer.MAX_DEFERRED_STEERS + 1)
        ]
    finally:
        target.task.cancel()

    assert not any(reply.get("pending_steer") for reply in replies), replies
    assert not target._deferred_steers
    assert len(target._queue) == deferred_steer.MAX_DEFERRED_STEERS + 1
    client.steer.assert_not_awaited()
