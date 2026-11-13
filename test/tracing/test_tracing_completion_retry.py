"""A retried completion turn still reports on the same sub-agent runs (tracing/subagents.py).

A completion turn whose runtime dies before any output is requeued by
``_queue_recovery`` under a NEW queue id and runs again as a new turn. The trace
must hand that retry the same causes the first attempt took, or the retry roots an
unrelated trace. Drives the real runner through the real runtime-death arm; only
the model is stubbed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from test_chat_requeue_resolved_stop import _client, _drain, _state, _wire

from kiro_crew.acp.transport_errors import AcpProcessDied
from kiro_crew.dashboard import trace_binding
from kiro_crew.tracing import provider, runs, subagents, turns

NS = "00112233445566778899aabbccddeeff"


@pytest.fixture
def traced():
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    provider.install_for_testing(
        provider.new_state(SimpleSpanProcessor(InMemorySpanExporter()), NS, b"k" * 32)
    )
    yield
    turns.reset_for_testing()
    subagents.reset_for_testing()
    runs.reset_for_testing()
    provider.reset_for_testing()


@pytest.mark.asyncio
async def test_a_completion_retried_after_a_runtime_death_keeps_its_causes(
    tmp_path, monkeypatch, traced
):
    from kiro_crew.dashboard import chat_runner
    from kiro_crew.dashboard.chat import _run_chat
    from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

    prompts: list[str] = []

    async def _stream(msg):
        prompts.append(msg)
        if len(prompts) == 1:
            raise AcpProcessDied("kiro-cli exited")
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="read the result")
        yield LLMEvent(kind=EVENT_COMPLETE)

    bound: list[tuple[str, ...]] = []
    real_binding = trace_binding.trace_subagents.completion_binding

    def _record(causes):
        bound.append(causes)
        return real_binding(causes)

    monkeypatch.setattr(trace_binding.trace_subagents, "completion_binding", _record)
    state = _state(tmp_path, monkeypatch)
    _wire(state, _client(_stream))
    slot = state.get_or_create_slot("s1")
    slot._titled = True

    subagents.note_delivery("q-completion", ["sa_retry"])
    with patch.object(chat_runner, "_recovery_delay", new_callable=AsyncMock):
        with subagents.delivered(["q-completion"]):
            await _run_chat(state, slot, "[Subagent completion event] ...")
        await _drain(state)

    assert len(prompts) == 2  # the death was requeued and the retry ran
    assert bound == [("sa_retry",), ("sa_retry",)]
