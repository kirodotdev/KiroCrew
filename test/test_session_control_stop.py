"""Session control's stop verb and the retry guard on its escalation.

Covers ``stop_target`` reaching ``stop_slot_turn`` as the Stop button does, its
out-of-bounds refusal, an off-loop SEL prewarm, a config warm after it and before
the gate, and nothing between gate and stop; also a created session's ``tab_id``
and ``origin`` birth metadata and the create handler's config warm after ``_body``.

The retry cases pin that a repeat inside the window keeps the queue and steers and
logs ``escalation_withheld``, a repeat against a fresh turn cancels it, and a stop
past the window or a second button press escalates. Also covers a send kept queued
through a hard stop's settle, ``already_stopping``, and the window's keys and sweep.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
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
)

from kiro_crew.dashboard import chat_delivery as cd
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard import stop_retry
from kiro_crew.dashboard.handlers import session_control as handlers_sc

# ── Stopping ─────────────────────────────────────────────────────────────────


def test_birth_metadata_carries_the_slots_own_identity_and_origin(tmp_path, monkeypatch):
    """A created-then-idle session's only disk record is this metadata line.

    `_save_slot_to_history` returns early on an empty message window, so a session
    that is created and never messaged never reaches the normal save path. Omitting
    `tab_id` makes rehydrate mint a fresh one -- which breaks the ownership match
    this design depends on -- and omitting `origin` makes it come back
    unattributed, dropping it out of `slots:user`.

    Mutation guard: removing either key from the dict fails here.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    monkeypatch.setattr(sc, "_workspace_name_for_dir", lambda cfg, ws_dir: caller.workspace)

    result = asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))
    created = state.get_slot(result["target"])
    written = state.conversation_log.get_metadata(sc.slot_history_key(created))

    assert written.get("tab_id") == created._tab_id, (
        "tab_id must reach the birth metadata -- without it a restart remints the "
        "slot's identity and ownership stops matching"
    )
    assert written.get("origin") == created._origin, (
        "origin must reach the birth metadata -- without it the session comes back "
        "unattributed and leaves slots:user"
    )


def test_nothing_suspends_between_the_stop_gate_and_the_stop(tmp_path, monkeypatch):
    """The SEL prewarm must run BEFORE authorization, not between it and the act.

    `await` is a suspension point. With the prewarm sitting between
    `authorize_target` and `stop_slot_turn`, a user action landing in that window
    -- linking the target to a channel -- makes the decision stale, and a turn gets
    cancelled on a session that became channel-backed after `mirrored_target`
    already passed. `send_message` documents the same rule for its cooldown claim.

    Mutation guard: moving the prewarm back below the gate flips this order.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    order: list[str] = []

    def _sel():
        order.append("sel")
        return MagicMock()

    real_authorize = sc.authorize_target

    def _authorize(*a, **kw):
        order.append("authorize")
        return real_authorize(*a, **kw)

    async def _fake_stop(_state, slot, *, source, escalate=True):
        order.append("stop")
        return {"ok": True}

    monkeypatch.setattr(sc, "sel", _sel)
    monkeypatch.setattr(sc, "authorize_target", _authorize)
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.stop_slot_turn", _fake_stop)

    asyncio.run(sc.stop_target(state, caller_session_key=_key(caller), target=target.key))

    assert order[:1] == ["sel"], f"the prewarm must precede authorization; got {order}"
    assert order.index("authorize") < order.index("stop")
    # The decisive assertion: authorization and the act must be ADJACENT, so no
    # await can separate them.
    assert (
        order.index("stop") - order.index("authorize") == 1
    ), f"something ran between the gate and the act it authorizes: {order}"


def test_the_config_warm_is_the_last_suspension_before_the_stop_gate(tmp_path, monkeypatch):
    """A warm with an `await` after it is no warm at all.

    `session_control_enabled` is synchronous and its `KiroCrewConfig.load()`
    re-reads and validates the file on the first call after a config edit. The warm
    exists so the gate's own read is a cache hit -- but a config edit landing in any
    suspension BETWEEN the warm and the gate changes the fingerprint, so the gate
    misses the cache and does that read on the loop regardless.

    `stop_target` has its own SEL prewarm await, so warming in the handler (before
    the body read AND before that prewarm) left two suspensions in the gap. The warm
    therefore belongs immediately after the SEL prewarm.

    Mutation guard: moving the warm back above the SEL prewarm, or into the handler,
    puts `sel` between it and `authorize`.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    order: list[str] = []

    def _sel():
        order.append("sel")
        return MagicMock()

    real_authorize = sc.authorize_target

    def _authorize(*a, **kw):
        order.append("authorize")
        return real_authorize(*a, **kw)

    def _enabled():
        order.append("warm")
        return True

    async def _fake_stop(_state, slot, *, source, escalate=True):
        order.append("stop")
        return {"ok": True}

    monkeypatch.setattr(sc, "sel", _sel)
    monkeypatch.setattr(sc, "authorize_target", _authorize)
    monkeypatch.setattr(sc, "session_control_enabled", _enabled)
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.stop_slot_turn", _fake_stop)

    asyncio.run(sc.stop_target(state, caller_session_key=_key(caller), target=target.key))

    assert "warm" in order, "the config was never warmed"
    # The warm runs off-loop via to_thread, so it is a suspension; nothing that
    # suspends may follow it before the gate reads config.
    assert order.index("sel") < order.index("warm"), (
        "the config warm must come AFTER the SEL prewarm -- that prewarm is an "
        f"await, and it would otherwise invalidate the warm: {order}"
    )
    assert order.index("warm") < order.index("authorize"), f"warm must precede the gate: {order}"


def test_create_warms_the_config_after_reading_the_body():
    """The body read suspends, so a warm above it can be stale by the gate.

    `create_session`'s first statement is the synchronous enabled check, so the warm
    has to sit below `_body` -- which awaits `request.json()` -- and above the call.
    Asserted on source order because the suspension being guarded against is the
    handler's own `await`, which a runtime probe cannot observe without racing it.

    Mutation guard: moving the warm back above `_body` flips these positions.
    """
    src = Path(handlers_sc.__file__).read_text(encoding="utf-8")
    create = src[src.index("async def api_session_control_create") :]
    create = create[: create.index("async def api_session_control_stop")]
    body_at = create.index("await _body(request)")
    warm_at = create.index("await sc.prewarm_enabled_check()")
    call_at = create.index("await sc.create_session(")
    assert body_at < warm_at < call_at, (
        "the config warm must sit between the body read and create_session, so no "
        "await separates it from create_session's own synchronous gate"
    )


def test_stop_constructs_the_sel_off_loop_before_stopping(tmp_path, monkeypatch):
    """`stop_slot_turn`'s idle branch logs with no await before it.

    So a first `session_stop` on a fresh gateway would construct the log on the
    loop. Mutation guard: dropping the prewarm runs `sel()` on the loop thread.
    """
    import threading

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    seen: dict[str, int] = {}

    def _sel():
        seen.setdefault("thread", threading.get_ident())
        return MagicMock()

    monkeypatch.setattr(sc, "sel", _sel)

    async def _fake_stop(_state, slot, *, source, escalate=True):
        return {"ok": True}

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.stop_slot_turn", _fake_stop)

    async def _drive() -> int:
        await sc.stop_target(state, caller_session_key=_key(caller), target=target.key)
        return threading.get_ident()

    loop_thread = asyncio.run(_drive())

    assert "thread" in seen, "the SEL was never constructed"
    assert (
        seen["thread"] != loop_thread
    ), "the SEL must be constructed off the loop thread before the stop"


def test_stop_goes_through_the_same_path_as_the_stop_button(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    _busy(target)

    seen: dict[str, object] = {}

    async def _fake_stop(_state, slot, *, source, escalate=True):
        seen["slot"] = slot.key
        seen["source"] = source
        seen["escalate"] = escalate
        return {"ok": True}

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.stop_slot_turn", _fake_stop)

    out = asyncio.run(sc.stop_target(state, caller_session_key=_key(caller), target="chat-2"))

    assert out["target"] == "chat-2"
    # No `force`: `stop_slot_turn` escalates on a second press regardless of one,
    # so the tool does not advertise a flag a first call cannot honour. A FIRST
    # stop still carries `escalate=True` — the retry guard withholds it only for a
    # repeat, so the button's semantics are unchanged for a call that is not one.
    assert seen == {"slot": "chat-2", "source": "session_control", "escalate": True}


def test_stop_is_refused_for_a_session_out_of_bounds(tmp_path):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    _slot(state, "chat-hidden", memory_mode="incognito")
    with pytest.raises(sc.SessionControlError):
        asyncio.run(sc.stop_target(state, caller_session_key=_key(caller), target="chat-hidden"))


# ── session_stop is safe to re-send ──────────────────────────────────


def _stoppable(state, slot):
    """A target with a live turn and unconsumed work behind it.

    Both lists are what the hard-kill path clears, so they are the evidence a
    retry read as an escalation would destroy. ``stop_turn`` answers "cancelled"
    rather than "idle" so the soft stop stays PENDING — the state a retry arrives
    into, and the only state from which escalation is reachable at all.
    """
    _busy(slot)
    slot._queue.append({"id": "q1", "content": "the next thing"})
    slot._pending_steers.append("steer-1")
    slot._steer_delivery_ids["steer-1"] = "delivery-1"
    state.sessions.stop_turn = AsyncMock(return_value="cancelled")
    return slot


def test_a_retried_stop_keeps_the_queue_instead_of_escalating(tmp_path, monkeypatch):
    """The defect: a timeout retry is read as a second press and discards work.

    `stop_slot_turn` hard-kills on any second stop while the first is still
    pending, and the kill clears `_queue` and `_pending_steers`. An MCP client
    that got no response inside `_post`'s 30s timeout re-sends the same request,
    so a caller that asked once silently got the destructive variant — the
    exposure is worst for the unattended agent this verb exists for, which
    retries without anyone deciding anything.

    Mutation guard: dropping `escalate=` from `stop_target`'s call, or the
    `escalate and` guard in `stop_slot_turn`, empties both lists here and leaves
    `_stop_state` at "killing".
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _stoppable(state, _peer_target(state, "chat-2", caller))
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.sel", lambda: MagicMock())

    first = asyncio.run(sc.stop_target(state, caller_session_key=_key(caller), target="chat-2"))
    assert target._stop_state == "soft_pending", "fixture must leave a stop pending"

    second = asyncio.run(sc.stop_target(state, caller_session_key=_key(caller), target="chat-2"))

    assert first.get("info") is None, "the first stop is the real cooperative one"
    assert second["info"] == "stop already in progress"
    assert target._stop_state == "soft_pending", "the retry escalated to a hard kill"
    assert [q["id"] for q in target._queue] == ["q1"], "the retry discarded queued work"
    assert target._pending_steers == ["steer-1"], "the retry discarded a pending steer"
    assert target._steer_delivery_ids == {"steer-1": "delivery-1"}


def test_a_repeat_still_stops_a_target_that_started_running_again(tmp_path, monkeypatch):
    """Withholding the escalation must not withhold the STOP.

    The retry guard suppresses a kill, not a cancel. A repeat that arrives after
    the first stop has settled and the target has picked up its next turn is a
    plain first stop as far as that turn is concerned, and has to cancel it.

    Mutation guard: short-circuiting `stop_target` on a repeat — returning the
    first call's answer without calling `stop_slot_turn` — leaves this second turn
    running.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _stoppable(state, _peer_target(state, "chat-2", caller))
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.sel", lambda: MagicMock())

    asyncio.run(sc.stop_target(state, caller_session_key=_key(caller), target="chat-2"))
    # The first stop lands and the target drains its queue into a new turn.
    target._stop_state = "idle"
    target._stop_event_id = None

    out = asyncio.run(sc.stop_target(state, caller_session_key=_key(caller), target="chat-2"))

    assert out.get("info") is None, "a repeat against a fresh turn is a real stop"
    assert target._stop_state == "soft_pending"
    assert state.sessions.stop_turn.await_count == 2


def test_a_stop_after_the_window_still_escalates(tmp_path, monkeypatch):
    """Escalation is delayed, not removed.

    A stop that STILL finds the target winding down once the window has closed is
    the case escalating was written for, and the capability has to survive: the
    alternative contract (never escalate from the RPC) was rejected because it
    takes the hard kill away from the agent surface entirely.

    Mutation guard: withholding escalation unconditionally leaves `_stop_state` at
    "soft_pending" and the queue intact.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _stoppable(state, _peer_target(state, "chat-2", caller))
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.sel", lambda: MagicMock())

    asyncio.run(sc.stop_target(state, caller_session_key=_key(caller), target="chat-2"))
    # The window closes, so the next stop is a decision rather than a retry.
    monkeypatch.setattr(stop_retry, "WINDOW_SECS", 0.0)

    asyncio.run(sc.stop_target(state, caller_session_key=_key(caller), target="chat-2"))

    assert target._stop_state == "killing"
    assert list(target._queue) == [], "an escalation discards the queue, by design"


def test_a_withheld_escalation_is_recorded(tmp_path, monkeypatch):
    """Read from the other side: the absorbed retry must be visible too.

    The issue's complaint is that queued messages went "with no record that a
    retry rather than a decision caused it". Suppressing the kill silently would
    leave the same gap inverted — an audit in which a de-duplicated retry is
    indistinguishable from a stop nobody made.

    Mutation guard: dropping the metadata key.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    _stoppable(state, _peer_target(state, "chat-2", caller))
    logged: list[dict] = []
    fake_sel = MagicMock()
    fake_sel.log_tool_invocation = lambda **kw: logged.append(kw)
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.sel", lambda: fake_sel)

    asyncio.run(sc.stop_target(state, caller_session_key=_key(caller), target="chat-2"))
    asyncio.run(sc.stop_target(state, caller_session_key=_key(caller), target="chat-2"))

    noop = [row for row in logged if row.get("outcome") == "noop"]
    assert noop, f"the retry did not reach the no-op branch: {[r.get('outcome') for r in logged]}"
    assert noop[-1]["metadata"].get("escalation_withheld") is True


def test_the_stop_button_still_escalates_on_a_second_press(tmp_path, monkeypatch):
    """The button's contract is unchanged, and that is the point of the default.

    A person pressing Stop again has watched the cooperative stop fail to take, so
    the second press IS a decision. Only the RPC, which cannot tell a decision
    from a re-sent request, gives that up.

    Mutation guard: defaulting `escalate` to False in `stop_slot_turn` breaks this
    without touching the session-control tests above.
    """
    from kiro_crew.dashboard.chat_handlers import stop_slot_turn

    state = _make_state(tmp_path)
    slot = _stoppable(state, _slot(state, "chat-1"))
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.sel", lambda: MagicMock())

    asyncio.run(stop_slot_turn(state, slot))
    assert slot._stop_state == "soft_pending"

    asyncio.run(stop_slot_turn(state, slot))

    assert slot._stop_state == "killing"
    assert list(slot._queue) == []


def _owed_completion(slot, text: str = "[Subagent completion event] a1 finished") -> str:
    """Queue a sub-agent completion that is owed to the parent's next turn."""
    from kiro_crew.subagent import SubagentDelivery

    slot._queue.append({"id": "q-owed", "content": text})
    slot.note_pending_subagent_delivery(
        text, [SubagentDelivery(agent_id="a1", elapsed=1.0, credits=0.0)]
    )
    return text


class _SettlingManager:
    """The slice of ``SubagentManager`` a hard stop settles discarded deliveries through.

    ``during`` runs inside the settle's suspension: the window in which the real
    manager waits on the child's teardown gate and writes its tombstones on a thread.

    A stop pushes a slots update, and a push whose coalescing window is open
    serializes every slot, which reads the manager's running and queued sub-agents.
    This double answers those reads with "none", as an idle manager does.
    """

    def __init__(self, during=None) -> None:
        self.settled: list = []
        self._during = during

    async def settle_queued_delivery(self, deliveries) -> None:
        self.settled.extend(deliveries)
        # flake-ok: pre-existing single yield standing in for the settle's suspension point; moved verbatim and it waits for nothing
        await asyncio.sleep(0)
        if self._during is not None:
            self._during()

    def running_agents_for(self, parent_key: str) -> list[dict]:
        return []

    def published_queued_depths(self) -> dict[str, int]:
        return {}


@pytest.fixture
def _slots_window_always_open(monkeypatch):
    """Make every ``push_slots_update`` broadcast at once instead of coalescing.

    The coalescing window is measured on ``_slots_broadcast_clock``. A clock that
    jumps a minute on every read puts each push past the window, so a push always
    serializes the slot list rather than depending on how long the test took.
    """
    import itertools

    ticks = itertools.count(1000.0, 60.0)
    monkeypatch.setattr("kiro_crew.dashboard.state._slots_broadcast_clock", lambda: next(ticks))


@pytest.mark.usefixtures("_slots_window_always_open")
def test_a_send_that_lands_while_a_hard_stop_settles_stays_queued(tmp_path, monkeypatch):
    """The hard kill discards what was queued when it was pressed and settles the
    sub-agent deliveries those entries owed. That settle can suspend, and a send
    that lands meanwhile takes the busy branch and is acknowledged as queued, so the
    kill must leave it queued, as it does a send that lands a moment later.

    Mutation guard: clearing ``slot._queue`` after the settle's await wipes the send.
    """
    from kiro_crew.dashboard.chat_handlers import stop_slot_turn

    state = _make_state(tmp_path)
    slot = _busy(_slot(state, "chat-1"))
    _owed_completion(slot)
    state.sessions.stop_turn = AsyncMock(return_value="cancelled")
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.sel", lambda: MagicMock())
    sent: list[str] = []

    def _send() -> None:
        sent.append(
            cd.queue_for_next_turn(state, slot, "and now do this", directive_user_origin=True)
        )

    state.subagents = _SettlingManager(during=_send)

    asyncio.run(stop_slot_turn(state, slot))
    assert slot._stop_state == "soft_pending"
    asyncio.run(stop_slot_turn(state, slot))

    assert slot._stop_state == "killing"
    assert sent, "precondition: the send landed during the settle"
    queued = [(entry["id"], entry["content"]) for entry in slot._queue]
    assert queued == [
        (sent[0], "and now do this")
    ], f"the send acknowledged during the hard stop was discarded: queue = {queued}"


@pytest.mark.usefixtures("_slots_window_always_open")
def test_a_hard_stop_still_discards_and_settles_what_was_queued_before_it(tmp_path, monkeypatch):
    """Control: the first press keeps the queue, and the hard kill clears what was
    queued before it and settles the sub-agent delivery the cleared entry owed."""
    from kiro_crew.dashboard.chat_handlers import stop_slot_turn

    state = _make_state(tmp_path)
    slot = _busy(_slot(state, "chat-1"))
    owed_text = _owed_completion(slot)
    slot._queue.append({"id": "q2", "content": "the next thing"})
    state.sessions.stop_turn = AsyncMock(return_value="cancelled")
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.sel", lambda: MagicMock())
    manager = _SettlingManager()
    state.subagents = manager

    asyncio.run(stop_slot_turn(state, slot))
    assert slot._stop_state == "soft_pending"
    assert [entry["id"] for entry in slot._queue] == ["q-owed", "q2"]
    assert manager.settled == []

    asyncio.run(stop_slot_turn(state, slot))
    assert slot._stop_state == "killing"
    assert list(slot._queue) == []
    assert [delivery.agent_id for delivery in manager.settled] == ["a1"]
    assert slot.take_pending_subagent_deliveries([owed_text]) == []


def test_the_no_op_reply_says_which_of_its_two_facts_it_hit(tmp_path, monkeypatch):
    """`info` alone merges "was never running" with "its cancel is in flight".

    The de-duplicated retry lands on the second one routinely now, and a caller
    that renders both alike tells that caller the opposite of what happened.

    Mutation guard: hardcoding `already_stopping` either way collapses the two.
    """
    from kiro_crew.dashboard.chat_handlers import stop_slot_turn

    state = _make_state(tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.sel", lambda: MagicMock())

    stopping = _stoppable(state, _slot(state, "chat-1"))
    asyncio.run(stop_slot_turn(state, stopping))
    still_stopping = asyncio.run(stop_slot_turn(state, stopping, escalate=False))
    assert still_stopping == {
        "ok": True,
        "info": "stop already in progress",
        "already_stopping": True,
    }

    idle = _slot(state, "chat-2")
    assert asyncio.run(stop_slot_turn(state, idle)) == {
        "ok": True,
        "info": "not running",
        "already_stopping": False,
    }


def test_the_window_is_anchored_at_the_first_stop_not_slid_by_repeats():
    """Escalation is suppressed for ONE window, not for as long as retries arrive.

    A sliding window would put a hard kill out of reach of any caller polling
    faster than the window — trading a silent queue loss for a capability that can
    never be reached again.

    Mutation guard: refreshing the stored timestamp on a repeat makes the third
    call read as a repeat as well.
    """
    first_at = 100.0
    assert stop_retry.allow_escalation("chat-1", "chat-2", now=first_at) is True
    assert stop_retry.allow_escalation("chat-1", "chat-2", now=first_at + 80.0) is False
    assert (
        stop_retry.allow_escalation("chat-1", "chat-2", now=first_at + stop_retry.WINDOW_SECS)
        is True
    )


def test_the_window_outlasts_the_request_timeout_it_absorbs():
    """A window at or under `_post`'s 30s timeout expires before its own retry.

    The retry this exists to absorb cannot be sent until the first request has
    timed out, so the window is sized against that number rather than picked.
    """
    assert stop_retry.WINDOW_SECS > 30.0


def test_the_window_is_keyed_per_caller_and_target():
    """A different caller's FIRST stop is its own decision, not somebody's retry.

    Keying on the target alone would suppress that call — removing escalation from
    the RPC rather than making a retry safe.
    """
    assert stop_retry.allow_escalation("chat-1", "chat-2", now=100.0) is True
    assert stop_retry.allow_escalation("chat-9", "chat-2", now=100.0) is True
    assert stop_retry.allow_escalation("chat-1", "chat-3", now=100.0) is True
    assert stop_retry.allow_escalation("chat-1", "chat-2", now=100.0) is False


def test_an_unattributable_stop_cannot_escalate():
    """Fails closed: an empty key cannot be matched against a first call.

    Granting the kill there would hand the destructive variant to exactly the
    caller whose retries cannot be recognized. Withholding costs only the
    escalation — the cooperative stop still lands.
    """
    assert stop_retry.allow_escalation("", "chat-2", now=100.0) is False
    assert stop_retry.allow_escalation("chat-1", "", now=100.0) is False


def test_expired_windows_are_swept_rather_than_accumulated():
    """The map must not grow for the gateway's lifetime.

    Mutation guard: dropping the sweep keeps both keys, so a long-lived gateway
    holds one entry per pair of slots that ever stopped each other.
    """
    stop_retry.allow_escalation("chat-1", "chat-2", now=100.0)
    stop_retry.allow_escalation("chat-3", "chat-4", now=100.0)
    stop_retry.allow_escalation("chat-5", "chat-6", now=100.0 + stop_retry.WINDOW_SECS + 1.0)
    assert list(stop_retry._windows) == [("chat-5", "chat-6")]
