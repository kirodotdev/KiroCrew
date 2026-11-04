"""Depth and fan-out caps on workflow runs started from inside other runs.

``workflows/nesting.py`` owns the caps; ``WorkflowService`` checks every start,
intent start and rerun against them. See docs/system-specs/modules/workflows.md.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from _hot_reload_helpers import write_config as _write
from config_live_helpers import (  # noqa: F401 -- fixtures are used by name
    cfg_file_fixture,
)

import kiro_crew.llm_helpers as llm_helpers
from kiro_crew.config import live
from kiro_crew.config.live import ConfigChange
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.sections_agent import AgentConfig
from kiro_crew.workflows import agent_exec, agent_pool
from kiro_crew.workflows.nesting import (
    ERROR_CODE_DEPTH,
    ERROR_CODE_FANOUT,
    NestingLedger,
    run_id_of_worker_key,
)
from kiro_crew.workflows.service import WorkflowService

QUICK = (
    'META = {"name": "quick", "description": "d"}\n'
    "async def workflow(ctx):\n"
    "    return {'ok': True}\n"
)
HOLD = (
    'META = {"name": "hold", "description": "d"}\n'
    "async def workflow(ctx):\n"
    "    return await ctx.agent('hold')\n"
)

# Turns a lost run into a failed test well under pytest-timeout; never a barrier.
_HANG_GUARD_SECS = 30.0


class _Sessions:
    async def get_or_create(self, key, **kw):
        return SimpleNamespace(), True, False

    def release(self, key, *, cleanup=False):
        pass

    async def destroy(self, key):
        pass


def _service(**kw) -> WorkflowService:
    # The per-call path: the warm pool needs a provider this fake does not model.
    return WorkflowService(sessions=_Sessions(), persist=False, pool_agents=False, **kw)


def _never_active(_run_id: str) -> bool:
    return False


def _always_active(_run_id: str) -> bool:
    return True


@pytest.fixture
def gate(monkeypatch) -> asyncio.Event:
    """Hold every ``ctx.agent`` step until the test sets the event."""
    release = asyncio.Event()

    async def held(provider, message, **kw):
        await release.wait()
        return "done"

    for mod in (llm_helpers, agent_exec, agent_pool):
        monkeypatch.setattr(mod, "stream_and_collect", held)
    return release


async def _settle(svc: WorkflowService, run_id: str) -> None:
    handle = svc.registry.get(run_id)
    assert handle is not None and handle.task is not None
    await asyncio.wait_for(asyncio.shield(handle.task), timeout=_HANG_GUARD_SECS)


# ── the ledger alone ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "key, run_id",
    [
        ("wf:wf_000007:0", "wf_000007"),
        ("wf-pool:wf_000007:3", "wf_000007"),
        ("wf-unpooled:wf_000007:1", "wf_000007"),
        ("wf-worker:wf_000007:abc", "wf_000007"),
        ("wf-author:wf_000007:a1", "wf_000007"),
        ("subagent:abc", ""),
        ("dashboard:main", ""),
        ("", ""),
    ],
)
def test_worker_keys_name_their_run(key: str, run_id: str) -> None:
    assert run_id_of_worker_key(key) == run_id


def test_the_defaults_are_four_deep_and_three_wide() -> None:
    ledger = NestingLedger()
    assert (ledger.max_depth, ledger.max_nested_runs) == (4, 3)
    agent = AgentConfig()
    assert (agent.workflow_max_depth, agent.workflow_max_nested_runs) == (4, 3)


def test_depth_at_the_cap_is_admitted_and_one_past_is_refused() -> None:
    ledger = NestingLedger(max_depth=4)
    assert ledger.admit("dashboard:main", "wf_1", is_active=_never_active) is None
    for depth, (parent, child) in enumerate(
        [("wf_1", "wf_2"), ("wf_2", "wf_3"), ("wf_3", "wf_4")], start=2
    ):
        assert ledger.admit(f"wf-pool:{parent}:0", child, is_active=_never_active) is None
        ledger.settle(child, started=True)
        assert ledger.depth_of(child) == depth
    refusal = ledger.admit("wf-pool:wf_4:0", "wf_5", is_active=_never_active)
    assert refusal is not None and refusal["error_code"] == ERROR_CODE_DEPTH
    assert "nested 5 deep" in refusal["error"] and "cap of 4" in refusal["error"]
    assert ledger.depth_of("wf_5") == 0, "a refused start leaves no lineage behind"


def test_depth_follows_a_subagent_chain_back_to_its_run() -> None:
    parents = {"subagent:a": "wf-pool:wf_1:0", "subagent:b": "subagent:a"}
    ledger = NestingLedger(max_depth=2, parent_session_of=parents.get)
    assert ledger.admit("dashboard:main", "wf_1", is_active=_never_active) is None
    assert ledger.parent_run_of("subagent:b") == "wf_1"
    assert ledger.admit("subagent:b", "wf_2", is_active=_never_active) is None
    assert ledger.depth_of("wf_2") == 2


def test_a_subagent_with_no_run_behind_it_starts_a_root_run() -> None:
    ledger = NestingLedger(parent_session_of={"subagent:a": "dashboard:main"}.get)
    assert ledger.admit("subagent:a", "wf_1", is_active=_never_active) is None
    assert ledger.depth_of("wf_1") == 1


def test_a_parent_run_the_ledger_does_not_know_counts_as_depth_one() -> None:
    ledger = NestingLedger(max_depth=2)
    assert ledger.admit("wf:wf_gone:0", "wf_2", is_active=_never_active) is None
    assert ledger.depth_of("wf_2") == 2


def test_a_renamed_reservation_keeps_its_depth_and_slot() -> None:
    ledger = NestingLedger(max_nested_runs=1)
    ledger.admit("dashboard:main", "wf_1", is_active=_never_active)
    assert ledger.admit("wf:wf_1:0", "task-plan:x", is_active=_never_active) is None
    ledger.rename("task-plan:x", "wf_2")
    ledger.settle("wf_2", started=True)
    assert ledger.depth_of("wf_2") == 2
    refusal = ledger.admit("wf:wf_1:0", "wf_3", is_active="wf_2".__eq__)
    assert refusal is not None and refusal["error_code"] == ERROR_CODE_FANOUT


def test_a_cycle_in_recorded_parents_ends_the_walk() -> None:
    parents = {"subagent:a": "subagent:b", "subagent:b": "subagent:a"}
    ledger = NestingLedger(parent_session_of=parents.get)
    assert ledger.parent_run_of("subagent:a") == ""


def test_fanout_counts_children_still_in_flight() -> None:
    active = {"wf_2", "wf_3", "wf_4"}
    ledger = NestingLedger(max_nested_runs=3)
    ledger.admit("dashboard:main", "wf_1", is_active=_never_active)
    for child in sorted(active):
        assert ledger.admit("wf:wf_1:0", child, is_active=active.__contains__) is None
        ledger.settle(child, started=True)
    refusal = ledger.admit("wf:wf_1:0", "wf_5", is_active=active.__contains__)
    assert refusal is not None and refusal["error_code"] == ERROR_CODE_FANOUT
    active.discard("wf_2")
    assert ledger.admit("wf:wf_1:0", "wf_6", is_active=active.__contains__) is None


def test_an_unregistered_reservation_holds_its_fanout_slot() -> None:
    ledger = NestingLedger(max_nested_runs=1)
    ledger.admit("dashboard:main", "wf_1", is_active=_never_active)
    assert ledger.admit("wf:wf_1:0", "wf_2", is_active=_never_active) is None
    # wf_2 is not registered yet, so is_active says no; the reservation counts.
    refusal = ledger.admit("wf:wf_1:0", "wf_3", is_active=_never_active)
    assert refusal is not None and refusal["error_code"] == ERROR_CODE_FANOUT
    ledger.settle("wf_2", started=False)
    assert ledger.admit("wf:wf_1:0", "wf_3", is_active=_never_active) is None


def test_the_fanout_cap_is_per_parent() -> None:
    ledger = NestingLedger(max_nested_runs=1)
    ledger.admit("dashboard:main", "wf_1", is_active=_never_active)
    ledger.admit("dashboard:main", "wf_2", is_active=_never_active)
    assert ledger.admit("wf:wf_1:0", "wf_3", is_active=_always_active) is None
    assert ledger.admit("wf:wf_2:0", "wf_4", is_active=_always_active) is None


@pytest.mark.parametrize(
    "value, depth, width", [(0, 1, 1), (99, 16, 32), (True, 4, 3), ("5", 4, 3), (2, 2, 2)]
)
def test_setters_clamp_into_the_band(value, depth: int, width: int) -> None:
    ledger = NestingLedger()
    ledger.set_max_depth(value)
    ledger.set_max_nested_runs(value)
    assert (ledger.max_depth, ledger.max_nested_runs) == (depth, width)


# ── the service ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_service_refuses_a_start_one_level_past_the_cap() -> None:
    svc = _service(max_nesting_depth=4)
    caller = "dashboard:main"
    run_ids = []
    for _ in range(4):
        out = await svc.start(QUICK, session_key=caller)
        assert "run_id" in out, out
        run_ids.append(out["run_id"])
        caller = f"wf-pool:{out['run_id']}:0"
    out = await svc.start(QUICK, session_key=caller)
    assert out.get("error_code") == ERROR_CODE_DEPTH, out
    assert "run_id" not in out
    assert {r["run_id"] for r in svc.list_runs()} == set(run_ids)
    for run_id in run_ids:
        await _settle(svc, run_id)


@pytest.mark.asyncio
async def test_intent_starts_and_reruns_are_capped_too() -> None:
    svc = _service(max_nesting_depth=1)
    root = await svc.start(QUICK, session_key="dashboard:main")
    await _settle(svc, root["run_id"])
    worker = f"wf:{root['run_id']}:0"
    by_intent = await svc.start_from_intent("do a thing", session_key=worker)
    assert by_intent.get("error_code") == ERROR_CODE_DEPTH, by_intent
    rerun = await svc.rerun_subtree(root["run_id"], 0, caller_session=worker)
    assert rerun.get("error_code") == ERROR_CODE_DEPTH, rerun


@pytest.mark.asyncio
async def test_the_service_refuses_a_fourth_child_in_flight(gate: asyncio.Event) -> None:
    svc = _service()
    root = await svc.start(HOLD, session_key="dashboard:main")
    worker = f"wf-pool:{root['run_id']}:0"
    children = []
    for _ in range(3):
        out = await svc.start(HOLD, session_key=worker)
        assert "run_id" in out, out
        children.append(out["run_id"])
    refused = await svc.start(HOLD, session_key=worker)
    assert refused.get("error_code") == ERROR_CODE_FANOUT, refused
    gate.set()
    for run_id in [root["run_id"], *children]:
        await _settle(svc, run_id)
    again = await svc.start(QUICK, session_key=worker)
    assert "run_id" in again, "a finished child frees its slot"
    await _settle(svc, again["run_id"])


@pytest.mark.asyncio
async def test_a_run_started_from_an_author_session_counts_one_level_deeper() -> None:
    svc = _service(max_nesting_depth=2)
    root = await svc.start(QUICK, session_key="dashboard:main")
    child = await svc.start(QUICK, session_key=f"wf:{root['run_id']}:0")
    assert "run_id" in child, child
    out = await svc.start(QUICK, session_key=f"wf-author:{child['run_id']}:a1")
    assert out.get("error_code") == ERROR_CODE_DEPTH, out
    for run_id in (root["run_id"], child["run_id"]):
        await _settle(svc, run_id)


@pytest.mark.asyncio
async def test_the_service_counts_depth_through_a_subagent() -> None:
    parents: dict[str, str] = {}
    svc = _service(max_nesting_depth=2, parent_session_of=parents.get)
    root = await svc.start(QUICK, session_key="dashboard:main")
    parents["subagent:s1"] = f"wf:{root['run_id']}:0"
    child = await svc.start(QUICK, session_key="subagent:s1")
    assert "run_id" in child, child
    parents["subagent:s2"] = f"wf:{child['run_id']}:0"
    grandchild = await svc.start(QUICK, session_key="subagent:s2")
    assert grandchild.get("error_code") == ERROR_CODE_DEPTH, grandchild
    for run_id in (root["run_id"], child["run_id"]):
        await _settle(svc, run_id)


PLAN = {
    "id": "wd_plan",
    "slug": "plan",
    "revision": 1,
    "name": "plan",
    "format": "task-plan",
    "source": "tasks: []\n",
}


class _TaskRunner:
    """Starts a saved task plan the way TaskRunner does: as a host run."""

    def __init__(self) -> None:
        self.svc: WorkflowService | None = None
        self.started: list[str] = []

    async def start_workflow_definition(
        self, definition, *, session_key, nesting_reservation="", **_kw
    ):
        assert self.svc is not None
        run_id = await self.svc.begin_host_run(
            name="plan",
            source_format="task-plan",
            driver="taskrunner",
            session_key=session_key,
            nesting_reservation=nesting_reservation,
        )
        self.started.append(run_id)
        return {"task_id": f"t{len(self.started)}", "run_id": run_id}


def _plan_service(**kw) -> tuple[WorkflowService, _TaskRunner]:
    runner = _TaskRunner()
    svc = _service(
        task_runner=runner, definition_library=SimpleNamespace(get=lambda _ref: PLAN), **kw
    )
    runner.svc = svc
    return svc, runner


@pytest.mark.asyncio
async def test_a_saved_task_plan_started_from_a_worker_is_capped() -> None:
    svc, runner = _plan_service(max_nesting_depth=1)
    root = await svc.start(QUICK, session_key="dashboard:main")
    out = await svc.start_definition("plan", session_key=f"wf:{root['run_id']}:0")
    assert out.get("error_code") == ERROR_CODE_DEPTH, out
    assert runner.started == [], "a refused plan never reaches the task runner"
    await _settle(svc, root["run_id"])


@pytest.mark.asyncio
async def test_a_task_plan_counts_against_its_parents_fanout() -> None:
    svc, runner = _plan_service(max_nested_runs=1)
    root = await svc.start(QUICK, session_key="dashboard:main")
    worker = f"wf:{root['run_id']}:0"
    first = await svc.start_definition("plan", session_key=worker)
    assert first.get("run_id") == runner.started[0], first
    second = await svc.start_definition("plan", session_key=worker)
    assert second.get("error_code") == ERROR_CODE_FANOUT, second
    await _settle(svc, root["run_id"])


@pytest.mark.asyncio
async def test_a_task_plan_step_session_continues_the_lineage() -> None:
    steps: dict[str, str] = {}
    svc, _runner = _plan_service(
        max_nesting_depth=2,
        run_of_session=lambda key: steps.get(key.rsplit(":", 1)[0]),
    )
    root = await svc.start(QUICK, session_key="dashboard:main")
    plan = await svc.start_definition("plan", session_key=f"wf:{root['run_id']}:0")
    assert svc.nesting.depth_of(plan["run_id"]) == 2
    steps[f"taskrunner:{plan['task_id']}"] = plan["run_id"]
    out = await svc.start(QUICK, session_key=f"taskrunner:{plan['task_id']}:task0")
    assert out.get("error_code") == ERROR_CODE_DEPTH, out
    await _settle(svc, root["run_id"])


@pytest.mark.asyncio
async def test_a_task_run_started_from_a_worker_is_tracked() -> None:
    """``task_run`` reaches ``begin_host_run`` directly: not refused, but tracked."""
    steps: dict[str, str] = {}
    svc = _service(
        max_nesting_depth=2,
        max_nested_runs=1,
        run_of_session=lambda key: steps.get(key.rsplit(":", 1)[0]),
    )
    root = await svc.start(QUICK, session_key="dashboard:main")
    worker = f"wf:{root['run_id']}:0"
    host = await svc.begin_host_run(
        name="task", source_format="task-plan", driver="taskrunner", session_key=worker
    )
    assert svc.nesting.depth_of(host) == 2
    sibling = await svc.start(QUICK, session_key=worker)
    assert sibling.get("error_code") == ERROR_CODE_FANOUT, sibling
    steps["taskrunner:t9"] = host
    from_step = await svc.start(QUICK, session_key="taskrunner:t9:task0")
    assert from_step.get("error_code") == ERROR_CODE_DEPTH, from_step
    await _settle(svc, root["run_id"])


# ── the config knobs ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_service_binds_both_caps_live() -> None:
    live.reset_for_tests()
    svc = _service()
    names = {s.name for s in live.watch().subscriptions()}
    assert {"bind[agent.workflow_max_depth]", "bind[agent.workflow_max_nested_runs]"} <= names
    cfg = KiroCrewConfig()
    cfg.agent.workflow_max_depth = 2
    cfg.agent.workflow_max_nested_runs = 5
    await live.watch()._dispatch(
        ConfigChange(
            old=None,
            new=cfg,
            changed=frozenset({"agent.workflow_max_depth", "agent.workflow_max_nested_runs"}),
        )
    )
    assert (svc.nesting.max_depth, svc.nesting.max_nested_runs) == (2, 5)
    live.reset_for_tests()


def test_the_loader_clamps_both_knobs(cfg_file: Path) -> None:
    _write(cfg_file, {"agent": {"workflow_max_depth": 99, "workflow_max_nested_runs": 0}})
    agent = KiroCrewConfig.load().agent
    assert (agent.workflow_max_depth, agent.workflow_max_nested_runs) == (16, 1)


def test_the_loader_reads_a_pinned_value(cfg_file: Path) -> None:
    _write(cfg_file, {"agent": {"workflow_max_depth": 6, "workflow_max_nested_runs": 2}})
    agent = KiroCrewConfig.load().agent
    assert (agent.workflow_max_depth, agent.workflow_max_nested_runs) == (6, 2)
