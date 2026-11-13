"""A rerun runs under the time limit and token budget its run was launched with.

Neither limit was stored on the run, so a rerun fell back to the default
wall-clock ceiling and no budget at all. See docs/system-specs/modules/workflows.md,
"Run registry, persistence, and resume".
"""

from __future__ import annotations

import pytest
from test_workflows_service import GOOD_SCRIPT, FakeSessions, _wait_terminal

from kiro_crew.workflows.registry import RunHandle
from kiro_crew.workflows.service import WorkflowService

pytestmark = pytest.mark.asyncio

SIX_HOURS = 6 * 3600


def _limits(handle: RunHandle) -> tuple:
    # getattr: a handle without the fields fails the assert, not an AttributeError.
    return (getattr(handle, "timeout_secs", None), getattr(handle, "budget_total", None))


async def test_a_rerun_keeps_the_launch_limits_and_an_explicit_timeout_wins() -> None:
    svc = WorkflowService(sessions=FakeSessions([]), persist=False)
    first = await svc.start(
        GOOD_SCRIPT, timeout_secs=SIX_HOURS, budget_total=900_000
    )  # flake-ok: a stored run limit, not a wait
    assert "run_id" in first, first
    await _wait_terminal(svc, first["run_id"])
    prior = svc.registry.get(first["run_id"])
    assert _limits(prior) == (SIX_HOURS, 900_000)

    ceilings: list[int] = []
    build = svc._runner

    def spy(run_id, **kwargs):
        runner = build(run_id, **kwargs)
        ceilings.append(runner._timeout_secs)
        return runner

    svc._runner = spy  # type: ignore[method-assign]
    out = await svc.rerun_subtree(first["run_id"], 0, owner=True)
    assert "run_id" in out, out
    await _wait_terminal(svc, out["run_id"])
    rerun = svc.registry.get(out["run_id"])
    assert ceilings == [SIX_HOURS]
    assert _limits(rerun) == (SIX_HOURS, 900_000)

    out = await svc.rerun_subtree(
        first["run_id"], 0, owner=True, timeout_secs=600
    )  # flake-ok: a stored run limit, not a wait
    assert "run_id" in out, out
    await _wait_terminal(svc, out["run_id"])
    assert ceilings[-1] == 600


async def test_the_limits_survive_the_store_and_an_old_record_loads_without_them() -> None:
    handle = RunHandle(
        "wf_x", "x", timeout_secs=SIX_HOURS, budget_total=0
    )  # flake-ok: a stored run limit, not a wait
    again = RunHandle.from_store_json(handle.to_store_json())
    assert (again.timeout_secs, again.budget_total) == (SIX_HOURS, 0)
    old = handle.to_store_json()
    old.pop("timeout_secs")
    old["budget_total"] = "lots"
    loaded = RunHandle.from_store_json(old)
    assert (loaded.timeout_secs, loaded.budget_total) == (None, None)
