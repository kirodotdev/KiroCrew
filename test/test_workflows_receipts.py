"""Workflow receipt wiring over real context/stores and deterministic raw events."""

import asyncio
from types import SimpleNamespace

import pytest
from test_workflows_private_execution import world as _world

from kiro_crew.acp.types import EVENT_COMPACTION_STATUS, EVENT_COMPLETE, EVENT_TEXT_CHUNK
from kiro_crew.dashboard.workflow_inject import inject_bound_workflow_result
from kiro_crew.providers.base import LLMEvent, LLMProvider
from kiro_crew.workflow_memory import WorkflowScope, authorize_run

world = _world


class ReceiptModel(LLMProvider):
    def __init__(self):
        self.sent = []
        self.mode = "success"
        self.sid = "conversation-one"

    @property
    def session_id(self):
        return self.sid

    async def start(self):
        pass

    async def shutdown(self):
        pass

    async def approve_tool(self, request_id, *, always=False):
        pass

    async def reject_tool(self, request_id):
        pass

    def context_usage_pct(self):
        return 0.0

    async def raw(self, message):
        self.sent.append(message)
        if self.mode == "error":
            raise RuntimeError("model failed")
        if self.mode == "cancel":
            raise asyncio.CancelledError()
        if self.mode == "compaction":
            yield LLMEvent(kind=EVENT_COMPACTION_STATUS)
        if self.mode != "empty":
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="answer")
        yield LLMEvent(
            kind=EVENT_COMPLETE,
            stop_reason="end_turn",
            synthetic_completion=self.mode == "synthetic",
            refusal=self.mode == "refusal",
        )

    async def stream(self, message):
        async for event in self.essential_delivery.stream(
            message, self.raw, lambda: self.context_incarnation
        ):
            yield event


async def consume(provider, message):
    async for _ in provider.stream(message):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["success", "error", "cancel", "empty", "synthetic", "refusal", "compaction"]
)
async def test_workflow_receipt_requires_productive_raw_success(world, mode):
    scope = await WorkflowScope.admit("wf_receipt", world.builder, "dashboard:alice")
    provider = ReceiptModel()
    key = scope.worker_key("worker")
    full = await scope.prompt(
        world.builder, key, "first", is_new=True, agent="kirocrew", cwd=None, provider=provider
    )
    assert provider.essential_delivery._pending is not None
    envelope = provider.essential_delivery._pending.envelope
    provider.mode = mode
    try:
        await consume(provider, full)
    except (RuntimeError, asyncio.CancelledError):
        assert mode in {"error", "cancel"}
    assert envelope in provider.sent[0]
    provider.mode = "success"
    # Retrying the exact same full candidate must keep failed-attempt essentials.
    await consume(provider, full)
    assert (envelope in provider.sent[1]) == (mode != "success")
    next_prompt = await scope.prompt(
        world.builder, key, "next", is_new=False, agent="kirocrew", cwd=None, provider=provider
    )
    await consume(provider, next_prompt)
    assert envelope not in provider.sent[-1]
    provider.sid = "conversation-two"
    restored = await scope.prompt(
        world.builder,
        key,
        "resume",
        is_new=True,
        resumed=True,
        agent="kirocrew",
        cwd=None,
        provider=provider,
    )
    await consume(provider, restored)
    assert envelope in provider.sent[-1]
    other = ReceiptModel()
    independent = await scope.prompt(
        world.builder,
        scope.worker_key("other"),
        "other",
        is_new=True,
        agent="kirocrew",
        cwd=None,
        provider=other,
    )
    await consume(other, independent)
    assert envelope in other.sent[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("carrier", [None, {}, {"member_id": 7}])
async def test_malformed_member_snapshot_cannot_deliver_without_canonical_identity(world, carrier):
    scope = await WorkflowScope.admit("wf_delivery", world.builder, "dashboard:alice")
    snapshot = {
        "run_id": scope.run_id,
        "session_key": "dashboard:bob",
        "result": "member result",
        "memory_store": scope.store,
        "execution_context": carrier,
    }
    # A malformed member carrier cannot become an ordinary Global result.
    assert not await inject_bound_workflow_result(SimpleNamespace(), scope.run_id, snapshot)


@pytest.mark.asyncio
async def test_owner_can_cancel_unavailable_store_without_reading_it(world, monkeypatch):
    from member_memory_helpers import make_request

    from kiro_crew.context import release_cached_memory_store
    from kiro_crew.dashboard.handlers.workflows import api_workflow_run_cancel
    from kiro_crew.memory_stores import resolve_store_path

    scope = await WorkflowScope.admit("wf_owner_cancel", world.builder, "dashboard:alice")
    path = resolve_store_path(world.stores["alice"])
    await asyncio.to_thread(release_cached_memory_store, world.stores["alice"])
    path.unlink()
    assert not path.exists()
    from kiro_crew.workflows.registry import RunHandle

    handle = RunHandle(
        run_id=scope.run_id,
        name="cancel",
        session_key=scope.origin,
        execution_context=scope.execution_context,
    )
    snapshot = handle.to_store_json()
    assert await authorize_run(scope.run_id, "", owner=True, record=snapshot) == scope
    assert (
        await authorize_run(scope.run_id, "", owner=True, require_active=False, record=snapshot)
        == scope
    )
    called = []

    async def cancel(run_id):
        called.append(run_id)
        return True

    state = SimpleNamespace(
        workflow_service=SimpleNamespace(
            registry=SimpleNamespace(get=lambda _: handle),
            cancel=cancel,
        )
    )
    request = make_request(
        state,
        "/api/workflows/runs/wf_owner_cancel/cancel",
        method="POST",
        body={},
        owner=True,
        session="",
        match_info={"run_id": scope.run_id},
    )
    response = await api_workflow_run_cancel(request)
    assert response.status == 200
    assert called == [scope.run_id]
