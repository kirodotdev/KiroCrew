"""Session control's stop verb and the retry guard on its escalation.

A repeat inside the retry window keeps the queue instead of hard-killing, because an MCP
client re-sends a stop that timed out; a stop past the window or a second button press
still escalates.
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

    `_save_slot_to_history` skips an empty message window. Without `tab_id` rehydrate
    mints a new one and ownership stops matching; without `origin` the session drops
    out of `slots:user`.
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

    An `await` between `authorize_target` and `stop_slot_turn` lets a user link the
    target to a channel after `mirrored_target` passed, so the decision goes stale.
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
    # Authorization and the act must be adjacent, so no await can separate them.
    assert (
        order.index("stop") - order.index("authorize") == 1
    ), f"something ran between the gate and the act it authorizes: {order}"


def test_the_config_warm_is_the_last_suspension_before_the_stop_gate(tmp_path, monkeypatch):
    """A warm with an `await` after it is no warm at all.

    The warm makes the gate's synchronous `KiroCrewConfig.load()` a cache hit; a config
    edit in any suspension between them misses the cache. So the warm sits right after
    the SEL prewarm await, not in the handler.
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

    Asserted on source order: the suspension guarded against is the handler's own
    `await`, which a runtime probe cannot observe without racing it.
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

    So a first `session_stop` on a fresh gateway would construct the log on the loop.
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
    # A first stop carries `escalate=True`; the retry guard withholds it only for a
    # repeat.
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

    The hard kill clears both lists. ``stop_turn`` answers "cancelled" so the soft stop
    stays pending, the only state from which escalation is reachable.
    """
    _busy(slot)
    slot._queue.append({"id": "q1", "content": "the next thing"})
    slot._pending_steers.append("steer-1")
    slot._steer_delivery_ids["steer-1"] = "delivery-1"
    state.sessions.stop_turn = AsyncMock(return_value="cancelled")
    return slot


def test_a_retried_stop_keeps_the_queue_instead_of_escalating(tmp_path, monkeypatch):
    """A timeout retry must not be read as a second press that hard-kills.

    An MCP client re-sends after `_post`'s 30s timeout, and a hard kill clears
    `_queue` and `_pending_steers`.
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

    A repeat after the target picked up its next turn is a first stop for that turn.
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

    A stop that still finds the target winding down after the window must hard-kill,
    or the agent surface loses the kill entirely.
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
    """An absorbed retry is logged, so the audit can tell it from a stop nobody made."""
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

    A second press is a decision. Only the RPC, which cannot tell a decision from a
    re-sent request, gives that up.
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

    ``during`` runs inside the settle call, before the caller resumes. The slot reads
    answer "none", as an idle manager does, because a stop's slots update serializes
    every slot.
    """

    def __init__(self, during=None) -> None:
        self.settled: list = []
        self._during = during

    async def settle_queued_delivery(self, deliveries) -> None:
        self.settled.extend(deliveries)
        if self._during is not None:
            self._during()

    def running_agents_for(self, parent_key: str) -> list[dict]:
        return []

    def published_queued_depths(self) -> dict[str, int]:
        return {}

    def published_queued_labels(self) -> dict[str, dict]:
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
    """A send acknowledged as queued during the hard kill's settle stays queued.

    The kill discards only what was queued when it was pressed.
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

    The de-duplicated retry routinely lands on the second, so `already_stopping`
    tells them apart.
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

    A sliding window would put the hard kill out of reach of any caller polling
    faster than the window.
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
    """The map must not grow for the gateway's lifetime."""
    stop_retry.allow_escalation("chat-1", "chat-2", now=100.0)
    stop_retry.allow_escalation("chat-3", "chat-4", now=100.0)
    stop_retry.allow_escalation("chat-5", "chat-6", now=100.0 + stop_retry.WINDOW_SECS + 1.0)
    assert list(stop_retry._windows) == [("chat-5", "chat-6")]
