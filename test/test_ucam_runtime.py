import asyncio
import json
from types import SimpleNamespace

import pytest
import test_ucam_consumer as fixtures
from test_ucam_consumer import FakeAPI, FakeStdin, collect, projection, record

from kiro_crew import ucam_consumer as consumer
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.session_provider import AcpSessionProvider

binding = fixtures.binding


class RuntimeProvider:
    def __init__(self, events, reason="end_turn", owns_runtime=True):
        runtime = object.__new__(AcpRuntime)
        runtime._process = SimpleNamespace(stdin=FakeStdin(events))
        runtime._dead = False
        runtime._next_id = 1
        runtime._session_queues = {"synthetic-session": asyncio.Queue()}
        runtime._routed_requests = {}
        handle = SimpleNamespace(session_id="synthetic-session")
        self._client = AcpSessionProvider(handle, runtime, owns_runtime=owns_runtime)
        self.reason = reason

    async def stream(self, message):
        await self._client._runtime.send_request(
            "session/prompt",
            {"sessionId": self._client._session_id, "prompt": [{"type": "text", "text": message}]},
        )
        yield SimpleNamespace(kind="text_chunk", text="synthetic reply")
        if self.reason is not None:
            yield SimpleNamespace(kind="complete", stop_reason=self.reason)


@pytest.mark.asyncio
async def test_runtime_receipt_and_durable_result_before_collector_break(binding):
    events = []
    run = consumer.ConsumerRun(
        binding,
        "run-1",
        FakeAPI(projection(binding, [record(binding)]), events),
        clock=lambda: 1000,
    )
    store = consumer.RunStore(binding)
    await store.call("reserve", "request", "Task")
    await store.call("bind", "request", "run-1")
    run.store = store
    provider = RuntimeProvider(events)
    stream = run.stream(provider, "Task")
    async for event in stream:
        if event.kind == "complete":
            row = await store.call("get", "run-1")
            assert row["phase"] == "completed" and row["outcome"] == "success"
            assert json.loads(row["evidence"])["turn_result_ack"] is True
            break
    await stream.aclose()
    assert events == ["projection", "fetched", "write", "drain", "injected", "turn_result"]
    assert (await store.call("get", "run-1"))["phase"] == "completed"
    assert run.evidence()["native_sent"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["cancelled", "timeout", "error: cancel unacked", "", None])
async def test_runtime_non_success_terminal_is_failed(binding, reason):
    run = consumer.ConsumerRun(binding, "run-1", FakeAPI(projection(binding)), clock=lambda: 1000)
    with pytest.raises(consumer.ConsumerError, match="ucam_native_terminal"):
        await collect(run.stream(RuntimeProvider([], reason=reason), "Task"))
    assert run.sent


@pytest.mark.asyncio
async def test_shared_runtime_rejected_before_prompt(binding):
    events = []
    run = consumer.ConsumerRun(
        binding, "run-1", FakeAPI(projection(binding), events), clock=lambda: 1000
    )
    with pytest.raises(consumer.ConsumerError, match="ucam_dedicated_acp_required"):
        await collect(run.stream(RuntimeProvider(events, owns_runtime=False), "Task"))
    assert events == []


@pytest.mark.asyncio
async def test_runtime_unrelated_session_unchanged():
    events = []
    provider = RuntimeProvider(events)
    await collect(provider.stream("Unchanged"))
    assert events == ["write", "drain"]
    prompt = provider._client._runtime._process.stdin.writes[0]["params"]["prompt"]
    assert prompt == [{"type": "text", "text": "Unchanged"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["session", "transport"])
async def test_runtime_exact_binding_before_send(binding, mismatch):
    events = []
    provider = RuntimeProvider(events)
    run = consumer.ConsumerRun(
        binding, "run-1", FakeAPI(projection(binding), events), clock=lambda: 1000
    )
    run.transport = provider._client._runtime if mismatch == "session" else object()
    run.native_session_id = "wrong-session" if mismatch == "session" else "synthetic-session"
    token = consumer._active_run.set(run)
    try:
        with pytest.raises(consumer.ConsumerError, match="ucam_.*binding"):
            await collect(provider.stream("Task"))
    finally:
        consumer._active_run.reset(token)
    assert events == []


@pytest.mark.asyncio
async def test_runtime_projection_outage_no_send(binding):
    events = []
    api = FakeAPI(projection(binding), events)

    async def unavailable(*args, **kwargs):
        raise consumer.ConsumerError("ucam_api_unavailable")

    api.call = unavailable
    run = consumer.ConsumerRun(binding, "run-1", api, clock=lambda: 1000)
    with pytest.raises(consumer.ConsumerError, match="ucam_api_unavailable"):
        await collect(run.stream(RuntimeProvider(events), "Task"))
    assert events == [] and not run.sent
