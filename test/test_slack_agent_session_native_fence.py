"""The native turn's write fence (opened in ``_run_turn``, closed by ``handle_message``)
and the approval resume in the permission ladder, exercised end to end."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from test_slack_agent_session_status import _caps, make_client  # noqa: F401 - autouse fixture

from kiro_crew.slack import handler
from kiro_crew.slack.agent_sessions import fence
from kiro_crew.testing.wait import await_until

pytestmark = pytest.mark.asyncio


class NoopSlack:
    """Every Slack call is an async no-op; set_thread_status optionally raises."""

    def __init__(self, raise_on_status: bool = False) -> None:
        self.raise_on_status = raise_on_status
        self.calls: list[str] = []

    async def set_thread_status(self, *a: Any, **k: Any) -> None:
        self.calls.append("set_thread_status")
        if self.raise_on_status:
            raise RuntimeError("boom")

    def __getattr__(self, name: str) -> Any:
        async def _noop(*a: Any, **k: Any) -> Any:
            self.calls.append(name)
            return None

        return _noop


@pytest.fixture
def admit(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    async def _yes(*a: Any, **k: Any) -> bool:
        return True

    async def _none(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(handler, "channel_inbound_permitted", _yes)
    monkeypatch.setattr(handler, "_hydrate_thread_overrides", _none)
    monkeypatch.setattr(handler, "_hydrate_conv_header", _none)
    monkeypatch.setattr(handler, "_hydrate_conv_flags", lambda *a, **k: None)
    monkeypatch.setattr(handler, "admit_inbound_callback", _yes)
    return monkeypatch


async def _call(slack: Any, msg_ts: str = "1700.2") -> None:
    await handler.handle_message(slack, SimpleNamespace(), "C1", "hi", "1700.1", msg_ts, "U1")


def _live_scopes() -> list[fence.TurnScope]:
    return [s for s in list(fence._LIVE) if s.channel == "C1"]


async def test_real_run_turn_raising_after_enter_closes_the_fence(
    admit: pytest.MonkeyPatch,
) -> None:
    async def _dispatch(turn: Any) -> bool:
        turn._agent = None
        return True

    admit.setattr(handler, "_dispatch_before_turn", _dispatch)
    slack = NoopSlack(raise_on_status=True)
    with pytest.raises(RuntimeError, match="boom"):
        await _call(slack)
    assert "set_thread_status" in slack.calls  # the real _run_turn entered the fence first
    assert fence.current_scope() is None
    assert _live_scopes() == []


async def test_run_turn_returning_false_closes_the_fence(admit: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []

    async def _dispatch(turn: Any) -> bool:
        return True

    async def _run(turn: Any) -> bool:
        turn._turn_scope = handler.enter_turn(turn.channel, turn.reply_ts)
        seen.append(fence.current_scope())
        return False

    async def _deliver(turn: Any) -> None:  # pragma: no cover - must not run
        raise AssertionError("delivered")

    admit.setattr(handler, "_dispatch_before_turn", _dispatch)
    admit.setattr(handler, "_run_turn", _run)
    admit.setattr(handler, "_deliver_answer", _deliver)
    await _call(NoopSlack())
    assert seen[0] is not None
    assert fence.current_scope() is None
    assert _live_scopes() == []


async def test_a_declined_dispatch_never_exits(admit: pytest.MonkeyPatch) -> None:
    exits: list[Any] = []

    async def _dispatch(turn: Any) -> bool:
        return False

    admit.setattr(handler, "_dispatch_before_turn", _dispatch)
    admit.setattr(handler, "exit_turn", lambda reset: exits.append(reset))
    await _call(NoopSlack())
    assert exits == []
    assert fence.current_scope() is None


async def test_the_turn_closes_its_scope_once_through_the_facade(
    admit: pytest.MonkeyPatch,
) -> None:
    """Non-vacuity for the test above: a patch of ``handler.exit_turn`` reaches the close."""
    opened: list[Any] = []
    exits: list[Any] = []
    real_exit = handler.exit_turn

    async def _dispatch(turn: Any) -> bool:
        return True

    async def _run(turn: Any) -> bool:
        turn._turn_scope = handler.enter_turn(turn.channel, turn.reply_ts)
        opened.append(turn._turn_scope)
        return True

    async def _deliver(turn: Any) -> None:
        turn.close_turn_scope()  # an early close leaves nothing for the finally

    def _exit(reset: Any) -> None:
        exits.append(reset)
        real_exit(reset)

    admit.setattr(handler, "_dispatch_before_turn", _dispatch)
    admit.setattr(handler, "_run_turn", _run)
    admit.setattr(handler, "_deliver_answer", _deliver)
    admit.setattr(handler, "exit_turn", _exit)
    await _call(NoopSlack())
    assert exits == opened and len(exits) == 1
    assert fence.current_scope() is None
    assert _live_scopes() == []


async def test_the_delivery_tail_is_fenced_and_the_next_turn_is_not(
    admit: pytest.MonkeyPatch,
) -> None:
    client, web = make_client(live=False)
    client._inject_team = lambda channel, kwargs: None  # type: ignore[attr-defined]
    muted_in_tail: list[bool] = []

    async def _dispatch(turn: Any) -> bool:
        return True

    async def _run(turn: Any) -> bool:
        turn._turn_scope = handler.enter_turn(turn.channel, turn.reply_ts)
        return True

    async def _deliver(turn: Any) -> None:
        fence.mute_turns("C1", "1700.1")  # native Stop lands mid-delivery
        muted_in_tail.append(fence.turn_muted("C1", "1700.1"))
        await client.post_message("C1", "answer", "1700.1")  # answer / footer / notice
        try:
            raise RuntimeError("tail torn down")
        finally:
            await client.post_message("C1", "late notice", "1700.1")  # tail finally

    admit.setattr(handler, "_dispatch_before_turn", _dispatch)
    admit.setattr(handler, "_run_turn", _run)
    admit.setattr(handler, "_deliver_answer", _deliver)
    with pytest.raises(RuntimeError, match="torn down"):
        await _call(client)
    assert muted_in_tail == [True]
    assert web.methods() == []  # nothing posted after the Stop, finally included
    assert fence.current_scope() is None
    await client.post_message("C1", "next turn", "1700.1")
    assert web.methods() == ["chat.postMessage"]


async def test_a_nested_replay_restores_then_closes_the_outer_scope(
    admit: pytest.MonkeyPatch,
) -> None:
    outer_after_nested: list[Any] = []

    async def _dispatch(turn: Any) -> bool:
        return True

    async def _run(turn: Any) -> bool:
        turn._turn_scope = handler.enter_turn(turn.channel, turn.reply_ts)
        if turn.msg_ts == "1700.2":
            outer = fence.current_scope()
            with pytest.raises(RuntimeError):
                await _call(NoopSlack(), msg_ts="1700.3")  # the nested replay raises
            outer_after_nested.append(fence.current_scope() is outer)
            return False
        raise RuntimeError("nested boom")

    admit.setattr(handler, "_dispatch_before_turn", _dispatch)
    admit.setattr(handler, "_run_turn", _run)
    await _call(NoopSlack())
    assert outer_after_nested == [True]
    assert fence.current_scope() is None
    assert _live_scopes() == []


# ── the approval resume ────────────────────────────────────────────────


class Rec:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def log_tool_invocation(self, **kw: Any) -> None:
        self.rows.append(kw)

    def __getattr__(self, name: str) -> Any:
        return lambda *a, **k: None


def _ladder_turn(answer: Any) -> Any:
    status = SimpleNamespace(pause_stall_watchdog=lambda: None, resume_stall_watchdog=lambda: None)
    task = SimpleNamespace(await_approval=lambda: None, resume=lambda: None)
    return SimpleNamespace(
        slack=NoopSlack(),
        channel="C1",
        reply_ts="1700.1",
        approval_mode=handler.APPROVAL_INTERACTIVE,
        context_builder=None,
        session_key="slack:probe-ladder",
        _agent=None,
        status_ctrl=status,
        answer=answer,
        task=task,
    )


def _real_answer(slack: Any) -> Any:
    answer = handler._AnswerStream.__new__(handler._AnswerStream)
    answer.slack = slack
    answer.channel = "C1"
    answer.reply_ts = "1700.1"
    answer.status_dirty = True
    answer.use_slack_stream = True
    answer.active_task_id = ""
    answer.accumulated = ""

    async def _prep() -> None:
        return None

    answer.prepare_for_approval = _prep
    return answer


def _patch_ladder(monkeypatch: pytest.MonkeyPatch, outcome: str) -> Rec:
    rec = Rec()

    async def _request(*a: Any, **k: Any) -> str:
        return outcome

    monkeypatch.setattr(handler, "_request_approval", _request)
    monkeypatch.setattr(handler, "_should_auto_approve_spawn", lambda *a, **k: False)
    monkeypatch.setattr(handler, "is_yolo_mode", lambda: False)
    monkeypatch.setattr(handler, "sel", lambda: rec)
    return rec


@pytest.mark.parametrize("outcome", ["approved", "rejected"])
async def test_every_interactive_outcome_resumes_processing(
    monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    sent: list[tuple[Any, ...]] = []

    class S(NoopSlack):
        async def set_session_status(self, *a: Any) -> bool:
            sent.append(a)
            return True

    rec = _patch_ladder(monkeypatch, outcome)
    event = SimpleNamespace(title="bash", tool_kind="execute", request_id="r1")
    rejected = await handler._answer_permission_request(
        _ladder_turn(_real_answer(S())), None, event
    )
    assert rejected is (outcome == "rejected")
    assert sent == [("C1", "1700.1", "processing")]
    assert [r["outcome"] for r in rec.rows] == [outcome]


async def test_the_decision_audit_survives_a_cancel_during_the_resume_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Stop/teardown landing on the resume's Slack round-trip must not lose the SEL row.

    The timeout arm of _request_approval rejects the tool and defers its audit to
    the caller (``audited=False``), so this row is the decision's only record.
    """
    entered = asyncio.Event()

    class SlowSlack(NoopSlack):
        async def set_session_status(self, *a: Any) -> bool:
            entered.set()
            await asyncio.Event().wait()  # agents.sessions.setStatus in flight
            return True

    rec = _patch_ladder(monkeypatch, "rejected")  # e.g. the prompt timed out
    event = SimpleNamespace(title="bash", tool_kind="execute", request_id="r1")
    turn = _ladder_turn(_real_answer(SlowSlack()))
    t = asyncio.ensure_future(handler._answer_permission_request(turn, None, event))
    await await_until(entered, what="the resume call to start")
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await await_until(t, what="the cancelled permission answer")
    assert [r["outcome"] for r in rec.rows] == ["rejected"]


def test_a_stop_that_ended_the_turn_keeps_it_silent_after_another_lets_go() -> None:
    """Stop A holds a turn and is still deciding; Stop B holds it too and ends it.
    A then lets go (it ended nothing): the turn B ended stays silenced."""
    turn = fence.enter_turn("C9", "1700.9")
    try:
        held_a = fence.mute_turns("C9", "1700.9")
        held_b = fence.mute_turns("C9", "1700.9")
        assert held_a and held_b
        fence.release(held_b, ended=True)
        fence.release(held_a, ended=False)
        assert fence.current_scope() is not None and fence.current_scope().stopped
    finally:
        fence.exit_turn(turn)


def test_a_turn_stays_silent_while_any_stop_still_holds_it() -> None:
    turn = fence.enter_turn("C9", "1700.8")
    try:
        held_a = fence.mute_turns("C9", "1700.8")
        held_b = fence.mute_turns("C9", "1700.8")
        fence.release(held_a, ended=False)
        scope = fence.current_scope()
        assert scope is not None and scope.stopped
        fence.release(held_b, ended=False)
        assert not scope.stopped
    finally:
        fence.exit_turn(turn)


def test_an_ended_turn_is_not_held_again() -> None:
    turn = fence.enter_turn("C9", "1700.7")
    try:
        fence.release(fence.mute_turns("C9", "1700.7"), ended=True)
        assert fence.mute_turns("C9", "1700.7") == []
    finally:
        fence.exit_turn(turn)
