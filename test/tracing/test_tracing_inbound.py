"""A caller's W3C ``traceparent`` parents the turn it starts (``tracing/inbound.py``)."""

from __future__ import annotations

import asyncio
import contextvars
from types import SimpleNamespace

import pytest

from kiro_crew.acp.types import EVENT_COMPLETE, AcpEvent, TurnUsage
from kiro_crew.dashboard import trace_binding
from kiro_crew.tracing import ids, inbound, provider, runs, surfaces, turns

NS = "00112233445566778899aabbccddeeff"
KEY = b"k" * 32
TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
PARENT = "00f067aa0ba902b7"
VALID = f"00-{TRACE}-{PARENT}-01"


class _Headers:
    """The slice of a request's headers ``inbound.begin`` reads."""

    def __init__(self, *values: str) -> None:
        self._values = list(values)

    def getall(self, name: str, default: list[str]) -> list[str]:
        assert name == "traceparent"
        return list(self._values) or default


class _Source:
    session_id = "acp-1"
    _session_key = "hook:one"
    _model = "auto"
    _resolved_model_id = ""
    active_agent = "kirocrew-default"


@pytest.fixture(autouse=True)
def _floor(_floor_monkeypatch):
    return _floor_monkeypatch


@pytest.fixture
def spans():
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider.install_for_testing(provider.new_state(SimpleSpanProcessor(exporter), NS, KEY))
    yield exporter
    turns.reset_for_testing()
    runs.reset_for_testing()
    provider.reset_for_testing()


def _accept(on: bool) -> None:
    provider._accept_traceparent = on


async def _events():
    yield AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn", usage=TurnUsage())


async def _stream_turn() -> None:
    async for _ in turns.traced(_Source(), _events()):
        pass


def _turn_span(exporter):
    (span,) = [s for s in exporter.get_finished_spans() if s.name.startswith("invoke_agent")]
    return span


def _hex(value: int, width: int) -> str:
    return format(value, f"0{width}x")


@pytest.mark.parametrize(
    "value",
    [
        f"01-{TRACE}-{PARENT}-01",  # a version this reader does not know
        f"ff-{TRACE}-{PARENT}-01",
        f"00-{TRACE}-{PARENT}",  # too short
        f"00-{TRACE}-{PARENT}-01-extra",  # extra field
        f"00-{TRACE.upper()}-{PARENT}-01",  # uppercase hex
        f"00-{'0' * 32}-{PARENT}-01",  # all-zero trace id
        f"00-{TRACE}-{'0' * 16}-01",  # all-zero parent id
        f"00-{TRACE}-{PARENT}-0g",  # not hex
        f"00_{TRACE}_{PARENT}_01",
        "",
    ],
)
def test_a_malformed_traceparent_is_not_read(value):
    assert ids.parse_traceparent(value) is None


def test_a_valid_traceparent_gives_its_trace_and_parent_ids():
    assert ids.parse_traceparent(VALID) == (TRACE, PARENT)
    assert ids.parse_traceparent(f"00-{TRACE}-{PARENT}-00") == (TRACE, PARENT)


@pytest.mark.asyncio
async def test_a_webhook_turn_joins_the_callers_trace_and_the_header_names_its_span(spans):
    _accept(True)

    ctx = contextvars.copy_context()
    header = ctx.run(inbound.begin, _Headers(VALID))

    async def turn() -> None:
        with surfaces.root(surfaces.TRIGGER_WEBHOOK):
            await _stream_turn()

    await _run_in(ctx, turn())

    span = _turn_span(spans)
    assert _hex(span.context.trace_id, 32) == TRACE
    assert _hex(span.parent.span_id, 16) == PARENT
    assert header == f"00-{TRACE}-{_hex(span.context.span_id, 16)}-01"


@pytest.mark.asyncio
async def test_a_dashboard_turn_joins_the_callers_trace_and_the_header_names_its_span(spans):
    _accept(True)
    ctx = contextvars.copy_context()
    header = ctx.run(inbound.begin, _Headers(VALID))
    state = SimpleNamespace(_slots={})

    async def turn() -> None:
        # What turn_entry and chat_runner do for a turn, in order.
        turn_id = inbound.claim_turn_id()
        assert turn_id
        token = await trace_binding.bind_turn(state, SimpleNamespace(key="s"), turn_id, top=True)
        try:
            await _stream_turn()
        finally:
            turns.release_turn_root(token)

    await _run_in(ctx, turn())
    span = _turn_span(spans)
    assert _hex(span.context.trace_id, 32) == TRACE
    assert _hex(span.parent.span_id, 16) == PARENT
    assert header == f"00-{TRACE}-{_hex(span.context.span_id, 16)}-01"


@pytest.mark.asyncio
async def test_a_retried_turn_stays_in_the_callers_trace_and_the_header_names_attempt_one(spans):
    _accept(True)
    ctx = contextvars.copy_context()
    header = ctx.run(inbound.begin, _Headers(VALID))

    async def turn() -> None:
        with surfaces.root(surfaces.TRIGGER_WEBHOOK):
            await _stream_turn()
            await _stream_turn()  # the retry: same turn id, attempt 2

    await _run_in(ctx, turn())
    by_attempt = {
        s.attributes["kirocrew.attempt"]: s
        for s in spans.get_finished_spans()
        if s.name.startswith("invoke_agent")
    }
    assert sorted(by_attempt) == [1, 2]
    for span in by_attempt.values():
        assert _hex(span.context.trace_id, 32) == TRACE
        assert _hex(span.parent.span_id, 16) == PARENT
    assert header == f"00-{TRACE}-{_hex(by_attempt[1].context.span_id, 16)}-01"


def _run_in(ctx, coro):
    return asyncio.get_running_loop().create_task(coro, context=ctx)


@pytest.mark.asyncio
async def test_the_setting_off_leaves_the_turn_and_the_response_as_they_were(spans):
    _accept(False)
    assert inbound.begin(_Headers(VALID)) == ""
    assert inbound.claim_turn_id() == ""
    await _stream_turn()
    span = _turn_span(spans)
    assert span.parent is None
    assert _hex(span.context.trace_id, 32) != TRACE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sent",
    [
        (f"00-{TRACE}-{PARENT}-01-extra",),
        (f"00-{TRACE.upper()}-{PARENT}-01",),
        (VALID, VALID),  # two headers: no single value to read
        (),
    ],
)
async def test_an_invalid_or_absent_header_is_ignored_and_the_turn_runs_as_before(spans, sent):
    _accept(True)
    assert inbound.begin(_Headers(*sent)) == ""
    assert inbound.claim_turn_id() == ""
    await _stream_turn()
    assert _turn_span(spans).parent is None


def test_with_tracing_off_nothing_is_bound_and_no_header_is_returned():
    provider.reset_for_testing()
    _accept(True)
    assert provider.peek() is None
    assert inbound.begin(_Headers(VALID)) == ""
    assert inbound.claim_turn_id() == ""
    _accept(False)


@pytest.mark.asyncio
async def test_only_the_first_turn_takes_the_callers_id(spans):
    _accept(True)
    inbound.begin(_Headers(VALID))
    first = inbound.claim_turn_id()
    assert first
    assert inbound.claim_turn_id() == ""
    assert inbound.join_for(first) is not None
    assert inbound.join_for("0" * 32) is None
