"""A workflow run record stays strict JSON the dashboard can parse.

Python's ``json`` writes ``float('inf')``/``nan`` as bare ``Infinity``/``NaN`` and
reads them back, so a run that returns ``ctx.budget.remaining()`` with no budget
finished fine while the dashboard's ``JSON.parse`` refused the snapshot. See
docs/system-specs/modules/workflows.md, ``registry.py`` strict-JSON contract.
"""

import json
import math

import pytest

from kiro_crew.dashboard.handlers import workflows as handlers
from kiro_crew.testing.wait import async_wait_until
from kiro_crew.workflows.events import EventStream
from kiro_crew.workflows.registry import (
    RunHandle,
    RunRegistry,
    start_background_run,
)
from kiro_crew.workflows.service import _AUTHOR_SYSTEM, WorkflowService
from kiro_crew.workflows.store import WorkflowRunStore

NOW = "2026-10-30T00:00:00Z"
SCRIPT = """META = {"name": "budget-echo"}
async def workflow(ctx):
    return {"spent": ctx.budget.spent(), "remaining": ctx.budget.remaining()}
"""


def _strict(text: str):
    """Parse the way a browser's ``JSON.parse`` does: no Infinity/NaN tokens."""

    def _refuse(token):
        pytest.fail(f"not JSON: {token}")

    return json.loads(text, parse_constant=_refuse)


@pytest.mark.asyncio
async def test_unbudgeted_remaining_reaches_the_dashboard_as_strict_json():
    svc = WorkflowService(sessions=None, persist=False)
    out = await svc.start(SCRIPT, name="budget-echo")
    rid = out["run_id"]

    def _terminal():
        snap = svc.status(rid)
        return snap if snap and snap["status"] != "running" else None

    snap = await async_wait_until(_terminal, describe=lambda: f"status: {svc.status(rid)}")
    assert snap["status"] == "finished", snap

    response = await handlers._json_response_off_loop(svc.result(rid))
    body = _strict(response.text)
    assert body["result"] == {"spent": 0, "remaining": None}
    finished = [e for e in body["events"] if e["type"] == "run_finished"]
    assert finished[0]["data"]["result"] == {"spent": 0, "remaining": None}


@pytest.mark.asyncio
async def test_terminal_merge_keeps_returned_agent_results_strict_json():
    registry = RunRegistry()
    returned_agent_results = {0: {"score": math.inf}}

    async def _run(_record):
        return None, "finished", None, returned_agent_results

    rid = await start_background_run(
        registry,
        _run,
        run_id="run-terminal-merge-inf",
        name="terminal-merge-inf",
    )
    handle = registry.get(rid)
    assert handle is not None and handle.task is not None
    await async_wait_until(
        handle.task.done,
        describe=lambda: f"status: {registry.status(rid)}",
    )

    snap = _strict(json.dumps(handle.snapshot(include_events=True, include_result=True)))
    assert snap["agent_results"]["0"] == {"score": None}
    assert returned_agent_results[0] == {"score": math.inf}


def test_the_run_store_writes_strict_json(tmp_path):
    registry = RunRegistry()
    handle = RunHandle("run-inf", "budget-echo")
    registry.register(handle, persist=False)
    stream = EventStream("run-inf")
    handle.events.append(stream.log(NOW, message="start"))
    handle.events.append(stream.run_finished(NOW, result={"r": math.inf}, duration_s=0.0))
    registry.record_agent_result("run-inf", 0, result={"score": math.nan}, ok=True)
    registry.mark_terminal("run-inf", "finished", result={"r": math.inf})

    store = WorkflowRunStore(base_dir=tmp_path)
    store.save("run-inf", handle.to_store_json())
    (written,) = tmp_path.rglob("*.json")
    stored = _strict(written.read_text(encoding="utf-8"))
    assert stored["result"] == {"r": None}
    assert stored["events"][-1]["data"]["result"] == {"r": None}


def test_a_run_stored_by_an_older_build_loads_clean():
    legacy = {
        "run_id": "old",
        "name": "old",
        "status": "finished",
        "result": {"remaining": math.inf, "ratio": -math.inf, "x": math.nan},
        "events": [
            {
                "run_id": "old",
                "seq": 0,
                "ts": NOW,
                "type": "run_finished",
                "data": {"result": {"remaining": math.inf}, "duration_s": 0.0},
            }
        ],
    }
    handle = RunHandle.from_store_json(legacy)
    snap = _strict(json.dumps(handle.snapshot(include_events=True, include_result=True)))
    assert snap["result"] == {"remaining": None, "ratio": None, "x": None}
    assert snap["events"][0]["data"]["result"] == {"remaining": None}


def test_the_authoring_prompt_warns_that_remaining_can_be_infinite():
    # The prompt is what leads an authored script to emit remaining() at all.
    prompt = " ".join(_AUTHOR_SYSTEM.split())
    assert "remaining() is float('inf') when total is None" in prompt
    assert "do not return or log it raw" in prompt


def test_json_portable_rebuilds_only_what_it_must():
    # Imported here so the tests above still collect when the fix is reverted.
    from kiro_crew.strict_json import json_portable

    clean = {"a": [1, 2.5, "s", None, True], "b": {"c": (1, 2)}}
    assert json_portable(clean) is clean

    dirty = {"keep": [1.0], "drop": [math.inf, {"n": math.nan}], "t": (math.inf,)}
    safe = json_portable(dirty)
    assert safe == {"keep": [1.0], "drop": [None, {"n": None}], "t": [None]}
    assert safe["keep"] is dirty["keep"]
    assert dirty["drop"][0] == math.inf  # the caller's object is not mutated
    assert json_portable(-math.inf) is None
    assert json_portable({1.5: math.inf}) == {1.5: None}
