"""A caller's traceparent survives ``/prompts get``: the model turn takes the id the header names."""

from __future__ import annotations

import asyncio
import contextvars
from unittest.mock import patch

import pytest
from test_active_turn_session_key import _state_and_slot, _stream_completes

from kiro_crew.dashboard import trace_binding
from kiro_crew.dashboard.chat_runner import _run_chat
from kiro_crew.tracing import ids, inbound, provider, runs, turns

NS = "00112233445566778899aabbccddeeff"
KEY = b"k" * 32
TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
PARENT = "00f067aa0ba902b7"
VALID = f"00-{TRACE}-{PARENT}-01"


class _Headers:
    def getall(self, name: str, default: list[str]) -> list[str]:
        return [VALID]


@pytest.fixture(autouse=True)
def _floor(_floor_monkeypatch):
    return _floor_monkeypatch


@pytest.fixture
def tracing_on():
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    provider.install_for_testing(
        provider.new_state(SimpleSpanProcessor(InMemorySpanExporter()), NS, KEY)
    )
    provider._accept_traceparent = True
    yield
    turns.reset_for_testing()
    runs.reset_for_testing()
    provider.reset_for_testing()


@pytest.mark.asyncio
async def test_a_prompt_expanding_turn_joins_the_callers_trace_under_the_id_the_header_names(
    tmp_path, tracing_on
):
    state, slot, client = _state_and_slot(tmp_path)
    _stream_completes(client)
    ctx = contextvars.copy_context()
    header = ctx.run(inbound.begin, _Headers())
    bound: list[tuple[str, object]] = []
    real_bind = trace_binding.bind_turn

    async def spy(st, sl, turn_id, *, top):
        bound.append((turn_id, inbound.join_for(turn_id)))
        return await real_bind(st, sl, turn_id, top=top)

    with (
        patch(
            "kiro_crew.dashboard.chat_runner._resolve_prompt_mention",
            lambda message, project_dir: ("expanded prompt body", "ok", ""),
        ),
        patch.object(trace_binding, "bind_turn", spy),
    ):
        await asyncio.get_running_loop().create_task(
            _run_chat(state, slot, "/prompts get demo"), context=ctx
        )

    assert len(bound) == 1, "only the model-producing turn binds a trace root"
    turn_id, join = bound[0]
    assert join is not None, "the model turn left the caller's trace"
    assert header == ids.format_traceparent(
        TRACE, ids.span_id(NS, ids.KIND_TURN, turn_id, 1)
    ), "the header names a span the model turn never emits"
