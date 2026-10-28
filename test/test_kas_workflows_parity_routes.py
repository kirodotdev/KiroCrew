"""The ``/api/workflows/kas`` parity routes, through the real route table.

Owner and engine checks are the existing ``_refusal`` (stubbed here; it is
pinned by the routes that already use it). Each test drives the route the
dashboard calls and reads the KAS verb the fake process received.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from kas_workflow_helpers import (
    LOOP_PLAN,
    driven,
    failed_state,
    loop_state,
    make_router,
    parallel_state,
    question_request,
)

import kiro_crew.dashboard.handlers.kas_workflows as kas_handlers
from kiro_crew.dashboard import server
from kiro_crew.kas_workflows import router as router_mod
from kiro_crew.kas_workflows import wire
from kiro_crew.testing.wait import async_wait_until

pytestmark = pytest.mark.asyncio


@pytest.fixture
def kas(monkeypatch):
    router = make_router(audit=lambda **_k: None)
    refusals: list[str] = []

    async def no_refusal(_request, operation):
        refusals.append(operation)
        return None

    audits: list[tuple[str, str]] = []
    monkeypatch.setattr(router_mod, "_ROUTER", router)
    monkeypatch.setattr(kas_handlers, "_refusal", no_refusal)
    monkeypatch.setattr(
        kas_handlers, "_audit_authorization", lambda _r, op, outcome: audits.append((op, outcome))
    )
    return SimpleNamespace(router=router, refusals=refusals, audits=audits)


def _app() -> web.Application:
    app = web.Application()
    app["state"] = SimpleNamespace(get_slot=lambda _k: None)
    server._register_mcp_routes(app)
    return app


async def test_grant_route_calls_resume_with_extend_repeat(kas):
    rt = driven(kas.router, loop_state(), LOOP_PLAN)
    async with TestClient(TestServer(_app())) as client:
        bad = await client.post(
            "/api/workflows/kas/runs/w1/steps/loop/grant", json={"iterations": 0}
        )
        assert bad.status == 400
        resp = await client.post(
            "/api/workflows/kas/runs/w1/steps/loop/grant", json={"iterations": 3}
        )
        assert resp.status == 200, await resp.text()
        body = await resp.json()
    assert body["workflowId"] == "w1" and body["row"]["workflowId"] == "w1"
    assert rt.calls[-1][0] == wire.METHOD_RESUME
    assert rt.calls[-1][1]["extendRepeat"] == {"nodeId": "loop", "additionalIterations": 3}
    assert ("workflow.kas.grant", "allowed:3") in kas.audits
    assert "workflow.kas.grant" in kas.refusals


async def test_complete_route_maps_a_kas_refusal_to_409(kas):
    rt = driven(kas.router, loop_state(), LOOP_PLAN)
    rt.answers[wire.METHOD_UPDATE] = {"updated": False, "message": "only completed"}
    async with TestClient(TestServer(_app())) as client:
        assert (
            await client.post(
                "/api/workflows/kas/runs/w1/steps/loop/complete", json={"status": "x"}
            )
        ).status == 400
        resp = await client.post(
            "/api/workflows/kas/runs/w1/steps/loop/complete", json={"status": "failed"}
        )
        assert resp.status == 409
        assert "only completed" in (await resp.json())["error"]
        rt.answers[wire.METHOD_UPDATE] = {"updated": True, "message": "done"}
        ok = await client.post(
            "/api/workflows/kas/runs/w1/steps/loop/complete",
            json={"status": "completed", "reason": "enough", "path": "loop"},
        )
        assert ok.status == 200
        assert (await ok.json())["message"] == "done"
    assert rt.calls[-1][1]["statusReason"] == "enough"


async def test_retry_and_restart_routes_are_not_taken_by_the_action_route(kas):
    rt = driven(kas.router, failed_state())
    rt.answers[wire.METHOD_RETRY] = {"retriedNodeIds": ["code"]}
    async with TestClient(TestServer(_app())) as client:
        resp = await client.post("/api/workflows/kas/runs/w1/retry", json={})
        assert resp.status == 200, await resp.text()
        assert (await resp.json())["retriedNodeIds"] == ["code"]
        bad = await client.post("/api/workflows/kas/runs/w1/retry", json={"nodeId": 3})
        assert bad.status == 400
        again = await client.post("/api/workflows/kas/runs/w1/restart", json={})
        assert again.status == 200, await again.text()
    assert [m for m, _ in rt.calls][-2:] == [wire.METHOD_RETRY, wire.METHOD_INVOKE]


async def test_step_cancel_route_sends_session_cancel(kas):
    rt = driven(kas.router, parallel_state())
    async with TestClient(TestServer(_app())) as client:
        missing = await client.post("/api/workflows/kas/runs/w1/steps/nope/cancel", json={})
        assert missing.status == 404
        resp = await client.post("/api/workflows/kas/runs/w1/steps/b/cancel", json={"path": "b"})
        assert resp.status == 200, await resp.text()
    assert rt.notifications == [(wire.METHOD_SESSION_CANCEL, {"sessionId": "sb"})]


async def test_questions_are_in_state_and_answered_by_key(kas):
    rt = driven(kas.router, parallel_state())
    kas.router._step_runtime["sa"] = kas.router._run_runtime["w1"]
    kas.router.sink_for(rt).on_step_request(
        5, "sa", "session/request_permission", question_request()
    )
    await async_wait_until(lambda: kas.router.questions(), timeout=2.0)
    async with TestClient(TestServer(_app())) as client:
        state = await (await client.get("/api/workflows/kas/state")).json()
        (held,) = state["questions"]
        assert state["approvals"] == []
        key = held["key"]
        assert (
            await client.post(f"/api/workflows/kas/questions/{key}", json={"answers": {}})
        ).status == 400
        wrong = await client.post(
            f"/api/workflows/kas/questions/{key}", json={"answers": {"tc-9": "Oracle"}}
        )
        assert wrong.status == 409
        resp = await client.post(
            f"/api/workflows/kas/questions/{key}", json={"answers": {"tc-9": "Postgres"}}
        )
        assert resp.status == 200
        assert await resp.json() == {"answered": True, "dismissed": False}
    assert rt.responses == [(5, {"outcome": {"outcome": "selected", "optionId": "pg"}})]


async def test_a_question_can_be_dismissed(kas):
    rt = driven(kas.router, parallel_state())
    kas.router._step_runtime["sa"] = kas.router._run_runtime["w1"]
    kas.router.sink_for(rt).on_step_request(
        6, "sa", "session/request_permission", question_request()
    )
    await async_wait_until(lambda: kas.router.questions(), timeout=2.0)
    key = kas.router.questions()[0]["key"]
    async with TestClient(TestServer(_app())) as client:
        resp = await client.post(f"/api/workflows/kas/questions/{key}", json={"dismiss": True})
        assert await resp.json() == {"answered": False, "dismissed": True}
    assert rt.responses == [(6, {"outcome": {"outcome": "cancelled"}})]


def test_launch_options_are_shape_checked():
    assert kas_handlers._launch_options({"model": "claude-opus-4", "effort": "high"}) == {
        "model": "claude-opus-4",
        "effort": "high",
    }
    assert kas_handlers._launch_options({"model": "auto", "effort": ""}) == {}
    # creatorEffort is the gateway setting agent.kas_creator_effort, not a launch
    # option: POST /runs ignores it, whatever it holds.
    assert kas_handlers._launch_options({"creatorEffort": "max"}) == {}
    assert kas_handlers._launch_options({"creatorEffort": "huge"}) == {}
    for bad in ({"model": "a b"}, {"effort": 3}):
        assert isinstance(kas_handlers._launch_options(bad), web.Response)


async def test_run_start_passes_model_and_effort_to_the_launch(kas, tmp_path, monkeypatch):
    slot = SimpleNamespace(key="chat-1", project=str(tmp_path))
    started: list[str] = []
    slot.enqueue_or_run_prompt = lambda message, *_a: started.append(message) or True
    app = web.Application()
    app["state"] = SimpleNamespace(
        get_slot=lambda k: slot if k == "chat-1" else None, push_slots_update=lambda: None
    )
    server._register_mcp_routes(app)
    async with TestClient(TestServer(app)) as client:
        bad = await client.post(
            "/api/workflows/kas/runs",
            json={"slot": "chat-1", "source": "bundled://x", "model": "a b"},
        )
        assert bad.status == 400
        resp = await client.post(
            "/api/workflows/kas/runs",
            json={"slot": "chat-1", "source": "bundled://x", "model": "m-1", "effort": "low"},
        )
        assert resp.status == 202, await resp.text()
    (launch,) = kas.router._launches.values()
    assert (launch.model, launch.effort) == ("m-1", "low")
    assert started and "launch" in started[0]


async def test_run_start_fits_the_display_name_to_kas_run_label_rules(kas, tmp_path):
    # KAS rejects a runLabel over 100 characters or holding a double quote or a
    # control character, and the launch only reaches KAS after this route
    # answered 202, so the route rewrites the name to fit instead.
    slot = SimpleNamespace(key="chat-1", project=str(tmp_path))
    slot.enqueue_or_run_prompt = lambda *_a: True
    app = web.Application()
    app["state"] = SimpleNamespace(
        get_slot=lambda k: slot if k == "chat-1" else None, push_slots_update=lambda: None
    )
    server._register_mcp_routes(app)
    long_name = ' Nightly "fix"\nrun ' + "x" * 200
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/workflows/kas/runs",
            json={"slot": "chat-1", "source": "bundled://x", "runLabel": long_name},
        )
        assert resp.status == 202, await resp.text()
        blank = await client.post(
            "/api/workflows/kas/runs",
            json={"slot": "chat-1", "source": "bundled://x", "runLabel": " \n\t "},
        )
        assert blank.status == 202, await blank.text()
    first, second = kas.router._launches.values()
    assert first.run_label is not None
    assert first.run_label.startswith("Nightly 'fix' run xxx")
    assert len(first.run_label) <= 100
    assert '"' not in first.run_label and "\n" not in first.run_label
    assert second.run_label is None


async def test_a_driverless_finished_run_offers_only_delete(kas):
    kas.router.mirror.apply_load({"workflowId": "w1", "state": failed_state()})
    async with TestClient(TestServer(_app())) as client:
        row = await (await client.get("/api/workflows/kas/runs/w1")).json()
    assert row["actions"] == ["delete"]
    assert row["questions"] == []
