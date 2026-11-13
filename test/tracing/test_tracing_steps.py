"""Phase 4a: the steps inside a turn: derived model calls, compaction, approvals, steers."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from kiro_crew.acp import types as acp_types
from kiro_crew.acp.types import (
    EVENT_COMPACTION_STATUS,
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    AcpEvent,
    TurnUsage,
)
from kiro_crew.testing.clock import ManualClock
from kiro_crew.testing.wait import until_parked
from kiro_crew.tracing import provider, runs, steps, subagents, surfaces, turns

NS = "00112233445566778899aabbccddeeff"
KEY = b"k" * 32


@pytest.fixture
def spans():
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider.install_for_testing(provider.new_state(SimpleSpanProcessor(exporter), NS, KEY))
    yield exporter
    turns.reset_for_testing()
    runs.reset_for_testing()
    subagents.reset_for_testing()
    provider.reset_for_testing()


@pytest.fixture
def clock(monkeypatch):
    clock = ManualClock()
    clock.install(monkeypatch, steps)
    return clock


def _source(acp="acp-1", key="dashboard:chat-1"):
    return SimpleNamespace(session_id=acp, _session_key=key, _model="auto", _resolved_model_id="")


def _hex(value, width):
    return format(value, f"0{width}x")


def _named(exporter, name):
    return [s for s in exporter.get_finished_spans() if s.name == name]


def _turn_span(exporter, acp):
    (span,) = [
        s
        for s in exporter.get_finished_spans()
        if s.name.startswith("invoke_agent") and s.attributes["kirocrew.acp_session_id"] == acp
    ]
    return span


def _done(usage=None):
    return AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn", usage=usage or TurnUsage())


async def _stream(*events):
    for event in events:
        yield event


def test_the_event_kinds_and_the_timeout_cause_match_their_owners():
    from kiro_crew.constants import DENY_CAUSE_APPROVAL_TIMEOUT

    assert turns.EVENT_COMPLETE == acp_types.EVENT_COMPLETE
    assert turns.EVENT_TOOL_CALL == acp_types.EVENT_TOOL_CALL
    assert turns.EVENT_TEXT_CHUNK == acp_types.EVENT_TEXT_CHUNK
    assert turns.EVENT_COMPACTION_STATUS == acp_types.EVENT_COMPACTION_STATUS
    assert steps._CAUSE_TIMEOUT == DENY_CAUSE_APPROVAL_TIMEOUT


# ── derived steps and compaction ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_text_after_a_tool_group_is_the_next_step_and_no_step_carries_usage(spans):
    events = (
        AcpEvent(kind=EVENT_TEXT_CHUNK, text="looking"),
        AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="t1"),
        AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="t2"),
        AcpEvent(kind=EVENT_TEXT_CHUNK, text="found it"),
        AcpEvent(kind=EVENT_TEXT_CHUNK, text=" and more"),
        _done(TurnUsage(credits=2.5, input_tokens=10, output_tokens=4)),
    )
    async for _ in turns.traced(_source(), _stream(*events)):
        pass
    turn = _turn_span(spans, "acp-1")
    found = sorted(
        _named(spans, turns.SPAN_STEP), key=lambda s: s.attributes["kirocrew.step.index"]
    )
    assert [s.attributes["kirocrew.step.index"] for s in found] == [1, 2]
    for step in found:
        assert step.attributes["kirocrew.step.derived"] is True
        assert step.parent.span_id == turn.context.span_id
        assert not [k for k in step.attributes if "usage" in k or k.startswith("langfuse.")]
        assert "found it" not in str(step.attributes)
    # The spend stays on the turn.
    assert turn.attributes["kirocrew.usage.credits"] == 2.5


@pytest.mark.asyncio
async def test_a_compaction_inside_the_turn_is_one_span_with_its_result(spans):
    events = (
        AcpEvent(kind=EVENT_COMPACTION_STATUS, text="started"),
        # Another session's frame, fanned out to every co-tenant: not this turn's.
        AcpEvent(kind=EVENT_COMPACTION_STATUS, text="failed", runtime_global=True),
        AcpEvent(kind=EVENT_COMPACTION_STATUS, text="completed", title="summary text"),
        # A backend that reports only the end still gets one.
        AcpEvent(kind=EVENT_COMPACTION_STATUS, text="failed"),
        _done(),
    )
    async for _ in turns.traced(_source(), _stream(*events)):
        pass
    found = _named(spans, turns.SPAN_COMPACTION)
    assert sorted(s.attributes["kirocrew.compaction.outcome"] for s in found) == [
        "completed",
        "failed",
    ]
    (failed,) = [s for s in found if s.attributes["kirocrew.compaction.outcome"] == "failed"]
    assert failed.status.status_code.name == "ERROR"
    assert "summary" not in str([s.attributes for s in found])


# ── approvals ─────────────────────────────────────────────────────────────


class _Decider:
    """A channel decider whose person answers after *delay* seconds of the clock."""

    def __init__(self, clock, delay, verdict, cause=""):
        self._clock, self._delay, self._verdict, self._cause = clock, delay, verdict, cause
        self.last_deny_cause = ""

    async def __call__(self, event):
        self._clock.advance(self._delay)
        self.last_deny_cause = self._cause
        return self._verdict


async def _channel_approval(decider, asked=None, *, request_id=7):
    """Drive the channel driver's real approval ladder inside a traced channel turn."""
    from kiro_crew.messaging.driver import APPROVAL_INTERACTIVE, TurnDriver

    driver = SimpleNamespace(approval_mode=APPROVAL_INTERACTIVE, decider=decider)
    ask = SimpleNamespace(kind="permission_request", request_id=request_id, tool_call_id="tc-1")
    answers = []
    with surfaces.root(surfaces.TRIGGER_CHANNEL, channel="slack"):
        async for event in turns.traced(_source("acp-ch"), _stream(ask, _done())):
            if event is ask:
                answers.append(await TurnDriver._approve(driver, event, asked=asked))
    return answers


@pytest.mark.asyncio
async def test_an_approvals_wait_is_the_persons_delay(spans, clock):
    assert await _channel_approval(_Decider(clock, 7.25, True)) == [True]
    (approval,) = _named(spans, steps.SPAN_APPROVAL)
    turn = _turn_span(spans, "acp-ch")
    assert approval.parent.span_id == turn.context.span_id
    assert approval.context.trace_id == turn.context.trace_id
    assert approval.attributes["kirocrew.approval.wait_ms"] == 7250
    assert approval.attributes["kirocrew.approval.decided_by"] == "person"
    assert approval.attributes["kirocrew.approval.outcome"] == "approved"
    assert approval.attributes["kirocrew.tool_call_id"] == "tc-1"
    assert approval.attributes["kirocrew.trigger"] == "channel"


@pytest.mark.asyncio
async def test_a_channel_approval_with_request_id_zero_exports_one_span(spans, clock):
    assert await _channel_approval(_Decider(clock, 1, True), request_id=0) == [True]
    assert len(_named(spans, steps.SPAN_APPROVAL)) == 1


@pytest.mark.asyncio
async def test_an_expired_prompt_is_decided_by_the_timeout(spans, clock):
    assert await _channel_approval(_Decider(clock, 300, False, "approval_timeout")) == [False]
    (approval,) = _named(spans, steps.SPAN_APPROVAL)
    assert approval.attributes["kirocrew.approval.wait_ms"] == 300_000
    assert approval.attributes["kirocrew.approval.decided_by"] == "timeout"
    assert approval.attributes["kirocrew.approval.outcome"] == "rejected"


@pytest.mark.asyncio
async def test_the_wait_counts_from_when_the_prompt_became_answerable(spans, clock):
    # The channel posts the prompt before the decider waits; an answer can land then.
    asked = steps.asked_now()
    clock.advance(4)
    assert await _channel_approval(_Decider(clock, 1, True), asked=asked) == [True]
    (approval,) = _named(spans, steps.SPAN_APPROVAL)
    assert approval.attributes["kirocrew.approval.wait_ms"] == 5000
    assert approval.end_time - approval.start_time == 5_000_000_000


@pytest.mark.asyncio
async def test_a_cancelled_channel_approval_is_decided_by_the_host(spans):
    never = asyncio.Event()  # no one answers

    class _Stuck:
        last_deny_cause = ""

        async def __call__(self, event):
            await never.wait()
            return True

    task = asyncio.ensure_future(_channel_approval(_Stuck()))
    await until_parked(never)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    (approval,) = _named(spans, steps.SPAN_APPROVAL)
    assert approval.attributes["kirocrew.approval.decided_by"] == "policy"
    assert approval.attributes["kirocrew.approval.outcome"] == "rejected"


def test_who_decided_follows_the_host_and_the_cause():
    assert steps.decided_by() == "person"
    assert steps.decided_by(host=True) == "policy"
    assert steps.decided_by(cause="approval_undeliverable") == "policy"
    assert steps.decided_by(host=True, cause="approval_timeout") == "timeout"


@pytest.mark.asyncio
async def test_a_spawn_approval_sits_under_the_parents_spawn_span(spans, clock):
    parent = turns.start_turn(_source("acp-parent", "dashboard:parent"))
    spawn_span = "ab" * 8
    subagents._put(
        subagents._origins,
        "sa1",
        subagents.Origin(parent.trace_id, parent.span_id, spawn_span, "dashboard:parent", "p", 0.0),
    )
    approval = steps.approval_opened("spawn:sa1", subagent_id="sa1", spawn=True)
    clock.advance(2)
    steps.approval_closed(approval, approved=False, by=steps.decided_by(host=True))
    parent.finish(None)
    (span,) = _named(spans, steps.SPAN_APPROVAL)
    assert _hex(span.context.trace_id, 32) == parent.trace_id
    assert _hex(span.parent.span_id, 16) == spawn_span
    assert span.attributes["kirocrew.subagent_id"] == "sa1"
    assert span.attributes["kirocrew.approval.decided_by"] == "policy"
    assert span.attributes["kirocrew.approval.wait_ms"] == 2000


@pytest.mark.asyncio
@pytest.mark.parametrize(("by", "expected"), [("host", "policy"), ("", None)])
async def test_a_child_tool_approval_sits_under_the_childs_own_turn(spans, by, expected):
    # The real prompt watch the sub-agent ladder opens and closes (subagent_manager/run.py).
    from kiro_crew.subagent_manager.run import _ApprovalPrompt

    run = SimpleNamespace(
        _crew_log_approval_id=lambda info, rid: f"{info.id}:{rid}",
        _record_crew_log_tool_approval_requested=lambda info, **kw: ("", 0),
        _record_crew_log_approval_decided=lambda origin, **kw: None,
    )
    info = SimpleNamespace(id="sa2", _awaiting_approval=False)
    ask = SimpleNamespace(
        request_id=1, event=SimpleNamespace(tool_name="shell", title="t", tool_call_id="tc-9")
    )
    token = turns.bind_turn_root("e" * 32, attrs={"kirocrew.subagent_id": "sa2"})
    try:
        child = turns.start_turn(_source("acp-child", "subagent:sa2"))
        prompt = _ApprovalPrompt(run, info)
        prompt.closed(prompt.opened(ask), "approved", by)
        child.finish(None)
    finally:
        turns.release_turn_root(token)
    (span,) = _named(spans, steps.SPAN_APPROVAL)
    assert span.parent.span_id == child.turn_span.get_span_context().span_id
    assert span.attributes["kirocrew.tool_call_id"] == "tc-9"
    # An answer the ladder cannot attribute names nobody, never "person".
    assert span.attributes.get("kirocrew.approval.decided_by") == expected


# ── steers ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_steer_is_a_point_span_with_its_mode_only(spans):
    turn = turns.start_turn(_source("acp-s"))
    steps.steered(steps.STEER_INTERRUPT, source=_source("acp-s"))
    steps.steered("anything else", source=_source("acp-s"))  # not a mode: nothing
    steps.steered(steps.STEER_INTERRUPT, source=_source("acp-none"))  # no open turn
    turn.finish(None)
    (steer,) = _named(spans, steps.SPAN_STEER)
    assert steer.parent.span_id == turn.turn_span.get_span_context().span_id
    assert steer.attributes["kirocrew.steer.mode"] == "interrupt"
    assert steer.start_time == steer.end_time


@pytest.mark.asyncio
async def test_a_held_steer_dropped_at_turn_end_emits_no_span(spans, monkeypatch):
    from unittest.mock import AsyncMock

    from kiro_crew.acp.client import AcpClient
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_CLAUDE

    client = AcpClient(acp_backend=ACP_BACKEND_CLAUDE)
    client._session_id = "acp-s"
    client._agent_version = "0.89.1"
    client._steering_advertised = True
    client._steering_turn_req = 1
    client._turn_done.clear()
    client._active_tool_calls.add("tc-1")
    send = AsyncMock()
    monkeypatch.setattr(client, "_send_request", send)
    turn = turns.start_turn(_source(client._session_id))
    try:
        assert await client.steer("private held steer") is True
        assert client._steers_held == [(1, "private held steer")]
        assert client._release_accepted_steers(1, "end_turn", None) == []
        await client._send_held_steers()
        assert client._steers_held == []
        send.assert_not_awaited()
    finally:
        turn.finish(None)
    assert _named(spans, steps.SPAN_STEER) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("held", [False, True], ids=["immediate", "held"])
async def test_an_injected_steer_emits_exactly_one_span(spans, monkeypatch, held):
    from unittest.mock import AsyncMock

    from kiro_crew.acp.client import AcpClient
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_CLAUDE

    client = AcpClient(acp_backend=ACP_BACKEND_CLAUDE)
    client._session_id = "acp-s"
    client._agent_version = "0.89.1"
    client._steering_advertised = True
    client._steering_turn_req = 1
    client._turn_done.clear()
    if held:
        client._active_tool_calls.add("tc-1")

    async def send_request(method, params, *, on_id):
        assert method == acp_types.METHOD_SESSION_STEERING
        assert _named(spans, steps.SPAN_STEER) == []
        on_id(2)
        assert client._settle_steering_answer(
            acp_types.JsonRpcMessage(id=2, result={"outcome": "injected"})
        )
        return 2

    send = AsyncMock(side_effect=send_request)
    monkeypatch.setattr(client, "_send_request", send)
    turn = turns.start_turn(_source(client._session_id))
    try:
        assert await client.steer("private injected steer") is True
        if held:
            assert _named(spans, steps.SPAN_STEER) == []
            send.assert_not_awaited()
            client._active_tool_calls.discard("tc-1")
            await client._send_held_steers()
        send.assert_awaited_once()
        assert client._steers_accepted == [
            (1, acp_types.wrap_user_message("private injected steer"))
        ]
    finally:
        turn.finish(None)
    (steer,) = _named(spans, steps.SPAN_STEER)
    assert steer.parent.span_id == turn.turn_span.get_span_context().span_id
    assert steer.attributes == {**turn.common, "kirocrew.steer.mode": steps.STEER_INTERRUPT}
    assert steer.start_time == steer.end_time
