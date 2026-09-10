from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_ucam_consumer import FakeAPI, FakeProvider, Request
from test_ucam_consumer import binding as base_binding
from test_ucam_consumer import collect, projection
from test_ucam_consumer import routes as base_routes

from kiro_crew import ucam_consumer as consumer

binding = base_binding
routes = base_routes


@pytest.mark.asyncio
async def test_idempotent_route_and_conflicting_body(binding, routes, monkeypatch):
    monkeypatch.setattr(routes, "load_binding", AsyncMock(return_value=binding))
    spawn = AsyncMock(return_value="run-1")
    ctx = SimpleNamespace(name=consumer.APP_NAME, spawn=SimpleNamespace(run=spawn))
    request = Request(body={"task": "Task", "request_id": "id-1"})
    first, second = await routes.consume(request, ctx), await routes.consume(request, ctx)
    assert first.status == second.status == 202
    assert first.body == second.body
    conflict = await routes.consume(Request(body={"task": "Other", "request_id": "id-1"}), ctx)
    assert conflict.status == 409
    spawn.assert_awaited_once()


@pytest.mark.asyncio
async def test_ambiguous_dispatch_is_not_repeated(binding, routes, monkeypatch):
    monkeypatch.setattr(routes, "load_binding", AsyncMock(return_value=binding))
    spawn = AsyncMock(side_effect=RuntimeError("unavailable"))
    ctx = SimpleNamespace(name=consumer.APP_NAME, spawn=SimpleNamespace(run=spawn))
    request = Request(body={"task": "Task", "request_id": "id-1"})
    assert (await routes.consume(request, ctx)).status == 503
    assert (await routes.consume(request, ctx)).status == 409
    spawn.assert_awaited_once()


@pytest.mark.asyncio
async def test_concurrent_reservation_and_namespace(binding):
    store = consumer.RunStore(binding)
    responses = await asyncio.gather(*(store.call("reserve", "id", "Task") for _ in range(8)))
    assert sum(fresh for _, fresh in responses) == 1
    await store.call("bind", "id", "run-1")
    assert await consumer.RunStore(replace(binding, owner_sub="other")).call("get", "run-1") is None


@pytest.mark.asyncio
async def test_native_result_evidence_and_route_identity(binding, routes, monkeypatch):
    store = consumer.RunStore(binding)
    await store.call("reserve", "id", "Task")
    await store.call("bind", "id", "run-1")
    run = consumer.ConsumerRun(binding, "run-1", FakeAPI(projection(binding)), clock=lambda: 1000)
    run.store = store
    await collect(run.stream(FakeProvider([]), "Task"))
    row = await store.call("get", "run-1")
    assert row["phase"] == "completed" and row["text"] == "synthetic reply"
    evidence = json.loads(row["evidence"])
    assert evidence["native_sent"] is True
    assert evidence["fetched_ack"] and evidence["injected_ack"] and evidence["turn_result_ack"]
    assert evidence["generation"] == binding.generation and evidence["epoch"] == 1
    assert evidence["digest"] == projection(binding)["digest"]
    monkeypatch.setattr(routes, "load_binding", AsyncMock(return_value=binding))
    ctx = SimpleNamespace(name=consumer.APP_NAME)
    request = Request()
    request.match_info = {"run_id": "run-1"}
    response = await routes.result(request, ctx)
    assert response.status == 200
    assert json.loads(response.body)["evidence"] == evidence
    request["user"] = "other"
    assert (await routes.result(request, ctx)).status == 403
    request["user"] = consumer.APP_NAME
    request.match_info = {"run_id": "unregistered"}
    assert (await routes.result(request, ctx)).status == 404


@pytest.mark.asyncio
async def test_native_timeout_persists_failure(binding, monkeypatch):
    store = consumer.RunStore(binding)
    await store.call("reserve", "id", "Task")
    await store.call("bind", "id", "run-1")
    run = consumer.ConsumerRun(binding, "run-1", FakeAPI(projection(binding)), clock=lambda: 1000)
    run.store = store
    provider = FakeProvider([])

    async def hanging(message):
        await asyncio.Event().wait()
        yield

    provider.stream = hanging
    monkeypatch.setattr(consumer, "TURN_TIMEOUT", 0.01)
    with pytest.raises(asyncio.TimeoutError):
        await collect(run.stream(provider, "Task"))
    assert (await store.call("get", "run-1"))["outcome"] == "ucam_native_timeout"


@pytest.mark.asyncio
async def test_host_failure_before_observer_is_terminal_without_wait(binding, routes, monkeypatch):
    store = consumer.RunStore(binding)
    await store.call("reserve", "id", "Task")
    await store.call("bind", "id", "run-1")
    monkeypatch.setattr(routes, "load_binding", AsyncMock(return_value=binding))
    ctx = SimpleNamespace(
        name=consumer.APP_NAME, spawn=SimpleNamespace(is_done=lambda run_id: True)
    )
    request = Request()
    request.match_info = {"run_id": "run-1"}
    response = await routes.result(request, ctx)
    body = json.loads(response.body)
    assert body["phase"] == "failed" and body["outcome"] == "ucam_native_result_missing"
    assert body["evidence"] == {} and body["text"] == ""
    with pytest.raises(consumer.ConsumerError, match="ucam_run_registration"):
        await store.registered("run-1")


@pytest.mark.asyncio
async def test_expired_reservation_never_writes_late_native_prompt(binding):
    now = [1000]
    provider = FakeProvider([])

    def delayed_next_id():
        now[0] = 1002
        return 1

    provider._client._next_req_id = delayed_next_id
    run = consumer.ConsumerRun(
        binding, "late-run", FakeAPI(projection(binding)), clock=lambda: now[0]
    )
    run.registration_expires_at = 1001
    with pytest.raises(consumer.ConsumerError, match="ucam_run_deadline"):
        await collect(run.stream(provider, "Task"))
    assert not provider._client._process.stdin.writes


@pytest.mark.asyncio
async def test_expired_get_durably_finishes_reservation(binding, monkeypatch):
    now = [1000]
    monkeypatch.setattr(consumer.time, "time", lambda: now[0])
    store = consumer.RunStore(binding)
    await store.call("reserve", "id", "Task")
    await store.call("bind", "id", "run-1")
    now[0] = 1000 + consumer.RESERVATION_TIMEOUT + 1
    result = await store.call("get", "run-1")
    assert result["phase"] == "failed" and result["outcome"] == "ucam_run_deadline"
    with sqlite3.connect(store.path) as database:
        persisted = database.execute(
            "SELECT phase,outcome FROM runs WHERE namespace=? AND run_id=?",
            (store.namespace, "run-1"),
        ).fetchone()
    assert persisted == ("failed", "ucam_run_deadline")
    with pytest.raises(consumer.ConsumerError, match="ucam_run_registration"):
        await store.registered("run-1")


@pytest.mark.asyncio
async def test_late_finish_cannot_resurrect_expired_run_but_retains_send_evidence(
    binding, monkeypatch
):
    now = [1000]
    monkeypatch.setattr(consumer.time, "time", lambda: now[0])
    store = consumer.RunStore(binding)
    await store.call("reserve", "id", "Task")
    await store.call("bind", "id", "run-1")
    now[0] += consumer.RESERVATION_TIMEOUT + 1
    await store.call("get", "run-1")
    await store.call(
        "finish",
        "run-1",
        "completed",
        "late text",
        "success",
        {"native_sent": True, "injected_ack": True},
    )
    await store.call("finish", "run-1", "running", "", "", {"native_sent": False})
    result = await store.call("get", "run-1")
    assert result["phase"] == "failed" and result["outcome"] == "ucam_run_deadline"
    assert result["text"] == ""
    assert json.loads(result["evidence"]) == {"native_sent": True, "injected_ack": True}
