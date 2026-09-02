"""RED test for WorkflowService.rerun_subtree: a whitespace-only ``source`` must
yield ``edited=False`` (a real bool), not the empty string produced by an
``and``-chain short-circuit."""

from __future__ import annotations

import pytest

from kiro_crew.workflows.registry import RunHandle
from kiro_crew.workflows.service import WorkflowService


class _FakeRunner:
    """Stand-in so rerun_subtree never spawns a real background run."""

    async def run_background(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        return "ignored"


@pytest.mark.asyncio
async def test_agent_defect() -> None:
    service = WorkflowService(sessions=None, persist=False)
    # A prior run with a stored source so rerun_subtree proceeds to build a result.
    handle = RunHandle(
        run_id="wf_000001",
        name="prior",
        source="META = {}\nasync def workflow(ctx):\n    return 1\n",
    )
    service.registry.register(handle)
    # Replace the runner with a no-op so no agents are spawned. ``**kw`` absorbs
    # the keyword-only options _runner takes (e.g. timeout_secs).
    service._runner = lambda run_id, **kw: _FakeRunner()  # type: ignore[assignment,method-assign]

    result = await service.rerun_subtree("wf_000001", source="   ")

    assert result["edited"] is False
