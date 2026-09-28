"""The run-detail endpoint's ``plan`` field — the graph view's planned half.

The decisions pinned here are all about NOT harming the snapshot: the plan is absent
rather than null when there is none, the live registry record is never mutated, and a
failure to predict a shape never fails the run's own record.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import workflows as handlers

pytestmark = pytest.mark.asyncio

SCRIPT = (
    "META = {}\n"
    "\n"
    "async def workflow(ctx):\n"
    '    with ctx.phase("Read"):\n'
    '        await ctx.agent("look at it", label="look")\n'
)


def _client(detail: dict) -> TestClient:
    svc = SimpleNamespace(result=lambda _rid: detail)
    app = web.Application()
    app["state"] = SimpleNamespace(workflow_service=svc)
    app.router.add_get("/api/workflows/runs/{run_id}", handlers.api_workflow_run_get)
    return TestClient(TestServer(app))


async def _get(detail: dict, *, want_plan: bool = True) -> dict:
    async with _client(detail) as client:
        url = "/api/workflows/runs/wf_1" + ("?plan=1" if want_plan else "")
        response = await client.get(url)
        assert response.status == 200
        return await response.json()


async def test_a_python_source_yields_the_planned_phase_graph() -> None:
    body = await _get({"run_id": "wf_1", "status": "running", "events": [], "source": SCRIPT})
    assert body["plan"] == {
        "phases": [
            {
                "title": "Read",
                "certain": True,
                "nodes": [{"kind": "agent", "label": "look", "certain": True}],
            }
        ],
        "truncated": False,
        "titleLimit": 120,
    }


async def test_a_plan_is_derived_only_when_the_caller_asks_for_it() -> None:
    # The tree view polls this endpoint and never draws the plan, so it must not pay
    # for the parse. Same readable source as the test above, no flag, no plan.
    body = await _get(
        {"run_id": "wf_1", "status": "running", "events": [], "source": SCRIPT},
        want_plan=False,
    )
    assert "plan" not in body


async def test_without_the_flag_the_source_is_not_even_parsed(monkeypatch) -> None:
    # Absence of the key would also hold if the parse ran and its result were dropped,
    # which is the version that still costs a poll. Pin the parse itself.
    calls: list[str] = []

    def _boom(source: str):
        calls.append(source)
        raise AssertionError("the plan must not be derived without ?plan=1")

    monkeypatch.setattr(handlers, "plan_from_source", _boom)
    body = await _get(
        {"run_id": "wf_1", "status": "running", "events": [], "source": SCRIPT},
        want_plan=False,
    )
    assert "plan" not in body
    assert calls == []


async def test_a_snapshot_without_a_source_carries_no_plan_key() -> None:
    body = await _get({"run_id": "wf_1", "status": "running", "events": []})
    assert "plan" not in body


async def test_a_source_with_no_entrypoint_carries_no_plan_key() -> None:
    # Absent, never null: "we cannot read a plan" and "the plan is empty" must look
    # different to the UI, which draws the actual run alone in the first case.
    body = await _get(
        {"run_id": "wf_1", "status": "running", "events": [], "source": '{"tasks": []}'}
    )
    assert "plan" not in body


async def test_the_rest_of_the_snapshot_is_passed_through_untouched() -> None:
    detail = {
        "run_id": "wf_1",
        "status": "finished",
        "result": {"report": "ok"},
        "events": [{"run_id": "wf_1", "seq": 1, "ts": "t", "type": "log", "data": {}}],
        "source": SCRIPT,
    }
    body = await _get(detail)
    assert body["result"] == {"report": "ok"}
    assert body["events"] == detail["events"]
    assert body["status"] == "finished"


async def test_the_live_registry_record_is_not_mutated() -> None:
    detail = {"run_id": "wf_1", "status": "running", "events": [], "source": SCRIPT}
    await _get(detail)
    assert "plan" not in detail, "the snapshot belongs to the registry; it must be copied"


async def test_a_preview_failure_still_returns_the_run() -> None:
    """A preview is decoration on a record the caller asked for."""
    detail = {"run_id": "wf_1", "status": "running", "events": [], "source": SCRIPT}

    def _boom(_source: str) -> dict:
        raise RuntimeError("previewer exploded")

    original = handlers.plan_from_source
    handlers.plan_from_source = _boom  # type: ignore[assignment]
    try:
        body = await _get(detail)
    finally:
        handlers.plan_from_source = original  # type: ignore[assignment]
    assert body["run_id"] == "wf_1"
    assert "plan" not in body


async def test_an_llm_authored_label_is_redacted_like_the_rest_of_the_snapshot() -> None:
    # Plan labels come from the script's own string literals, so they ride the same
    # redaction path every other LLM-derived string on this endpoint does.
    script = (
        "async def workflow(ctx):\n"
        '    await ctx.agent("x", label="ghp_0123456789abcdefghijklmnopqrstuvwxyz")\n'
    )
    body = await _get({"run_id": "wf_1", "status": "running", "events": [], "source": script})
    label = body["plan"]["phases"][0]["nodes"][0]["label"]
    assert "ghp_0123456789abcdefghijklmnopqrstuvwxyz" not in label
