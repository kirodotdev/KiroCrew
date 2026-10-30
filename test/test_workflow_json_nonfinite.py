"""A run that carries a non-finite float still reads back as strict JSON.

``ctx.budget.remaining()`` is ``math.inf`` on a run without a budget, and a script can
put that value anywhere in its record: a log line, a phase result, the run result.
Python's ``json`` writes it as a bare ``Infinity`` token, which the browser's
``JSON.parse`` refuses, so the Workflows panel could not open the run at all.
"""

from __future__ import annotations

import json
import math
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import workflows as handlers
from kiro_crew.workflows.context import Budget
from kiro_crew.workflows.store import WorkflowRunStore


def _strict_loads(text: str):
    """``json.loads`` that refuses ``Infinity``, ``-Infinity`` and ``NaN``, as browsers do."""

    def _refuse(token: str):
        raise ValueError(f"non-standard JSON token {token!r}")

    return json.loads(text, parse_constant=_refuse)


def _unbudgeted_run(run_id: str = "wf_000002") -> dict:
    remaining = Budget(total=None).remaining()
    assert remaining == math.inf
    return {
        "run_id": run_id,
        "status": "finished",
        "source": "async def workflow(ctx):\n    return {'left': ctx.budget.remaining()}\n",
        "result": {"left": remaining, "floor": -math.inf, "odd": math.nan, "spent": 0.5},
        "events": [{"type": "log", "data": {"message": "left", "value": remaining}}],
    }


async def _get_detail_text(detail: dict, query: str = "") -> str:
    svc = SimpleNamespace(result=lambda _rid: detail)
    app = web.Application()
    app["state"] = SimpleNamespace(workflow_service=svc)
    app.router.add_get("/api/workflows/runs/{run_id}", handlers.api_workflow_run_get)
    async with TestClient(TestServer(app)) as client:
        response = await client.get(f"/api/workflows/runs/{detail['run_id']}{query}")
        assert response.status == 200
        return await response.text()


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["", "?plan=1"], ids=["snapshot", "snapshot-with-source-plan"])
async def test_an_unbudgeted_run_detail_is_strict_json(query: str) -> None:
    body = _strict_loads(await _get_detail_text(_unbudgeted_run(), query))

    assert body["result"] == {"left": None, "floor": None, "odd": None, "spent": 0.5}
    assert body["events"][0]["data"]["value"] is None
    assert body["source"].startswith("async def workflow(ctx):")


def test_the_store_writes_strict_json_for_an_unbudgeted_run(tmp_path) -> None:
    store = WorkflowRunStore(base_dir=tmp_path)
    store.save("wf_000002", _unbudgeted_run())

    on_disk = _strict_loads(next(store.runs_dir.glob("*.json")).read_text(encoding="utf-8"))

    assert on_disk["result"]["left"] is None
    assert on_disk["events"][0]["data"]["value"] is None


def test_the_store_writes_strict_json_for_a_tuple_result(tmp_path) -> None:
    store = WorkflowRunStore(base_dir=tmp_path)
    run = _unbudgeted_run()
    run["result"] = (Budget(total=None).remaining(), 1)
    store.save("wf_000002", run)

    on_disk = _strict_loads(next(store.runs_dir.glob("*.json")).read_text(encoding="utf-8"))

    assert on_disk["result"] == [None, 1]


@pytest.mark.asyncio
async def test_a_stored_run_holding_a_bare_infinity_still_opens(tmp_path) -> None:
    store = WorkflowRunStore(base_dir=tmp_path)
    store.save("wf_000002", {"run_id": "wf_000002", "status": "finished"})
    path = next(store.runs_dir.glob("*.json"))
    # The file a build without the store-side guard wrote: Python's default dumps.
    path.write_text(json.dumps(_unbudgeted_run()), encoding="utf-8")
    assert "Infinity" in path.read_text(encoding="utf-8")

    (loaded,) = store.load_all()
    body = _strict_loads(await _get_detail_text(loaded))

    assert body["result"]["left"] is None
    assert body["status"] == "finished"


def test_live_run_event_is_strict_json() -> None:
    """The live ``workflow_run_event`` push carries the run's raw result.

    A script can return a tuple, which nothing turns into a list before the
    broadcast, so a tuple holding ``math.inf`` must still leave as ``null``.
    """
    remaining = Budget(total=None).remaining()
    event = {
        "type": "run_finished",
        "data": {
            "result": {"remaining": remaining, "pair": (math.nan, 1), "nested": [(remaining,)]},
            "duration_s": 0.5,
        },
    }

    safe = handlers._redact_obj(event)

    assert safe["data"] == {
        "result": {"remaining": None, "pair": [None, 1], "nested": [[None]]},
        "duration_s": 0.5,
    }
    assert _strict_loads(json.dumps(safe, allow_nan=False))["data"] == safe["data"]
