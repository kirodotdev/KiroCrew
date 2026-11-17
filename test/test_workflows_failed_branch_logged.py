"""A parallel task or pipeline stage that raises still resolves to None (A5),
but leaves a log event saying why, so a run of all-None results is explainable.
See docs/system-specs/modules/workflows.md, ``parallel`` / ``pipeline``.
"""

from __future__ import annotations

import pytest

from kiro_crew.workflows.runner import WorkflowRunner

NOW = "2026-06-18T00:00:00Z"


async def _echo(prompt, opts):
    return f"echo:{prompt}"


def _logs(res) -> list[str]:
    return [e.data["message"] for e in res.events if e.type == "log"]


@pytest.mark.asyncio
async def test_a_parallel_task_that_raises_is_logged_and_resolves_to_none() -> None:
    source = (
        'META = {"name": "p"}\n'
        "async def workflow(ctx):\n"
        '    return await ctx.parallel([lambda: ctx.agent("a", tools=["x"]),'
        ' lambda: ctx.agent("b")])\n'
    )
    res = await WorkflowRunner(agent_fn=_echo).run(source, run_id="wf_p", now=NOW)
    assert res.ok, res.error
    assert res.result == [None, "echo:b"]
    (message,) = _logs(res)
    assert message.startswith("parallel task 0 failed: TypeError:"), message


@pytest.mark.asyncio
async def test_a_pipeline_stage_that_raises_is_logged_and_drops_the_item() -> None:
    source = (
        'META = {"name": "q"}\n'
        "async def workflow(ctx):\n"
        '    return await ctx.pipeline([{"id": 1}, {}], lambda row: row["id"])\n'
    )
    res = await WorkflowRunner(agent_fn=_echo).run(source, run_id="wf_q", now=NOW)
    assert res.ok, res.error
    assert res.result == [1, None]
    (message,) = _logs(res)
    assert message.startswith("pipeline item 1 failed: KeyError:"), message


@pytest.mark.asyncio
async def test_failed_task_logs_stop_at_the_per_run_limit() -> None:
    source = (
        'META = {"name": "many"}\n'
        "async def workflow(ctx):\n"
        "    return await ctx.pipeline([{}] * 50, lambda row: row['id'])\n"
    )
    res = await WorkflowRunner(agent_fn=_echo).run(source, run_id="wf_m", now=NOW)
    assert res.ok, res.error
    assert res.result == [None] * 50
    logs = _logs(res)
    assert len(logs) == 20
    assert logs[-1].endswith("(further failed tasks in this run are not logged)")
