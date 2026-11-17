"""A run that passes its ``budget_total`` gets one warning and keeps running.

Enforcing the budget (stopping the run) is a separate maintainer decision; see
docs/system-specs/modules/workflows.md, ``Budget``.
"""

from __future__ import annotations

import logging

import pytest

from kiro_crew.workflows.runner import WorkflowRunner

NOW = "2026-06-18T00:00:00Z"
SCRIPT = (
    'META = {"name": "spend"}\n'
    "async def workflow(ctx):\n"
    "    out = []\n"
    "    for i in range(3):\n"
    "        out.append(await ctx.agent('step ' + str(i)))\n"
    "    return len(out)\n"
)


async def _long_reply(prompt, opts):
    return "x" * 400  # about 100 tokens by the estimate


def _warnings(res) -> list[str]:
    return [
        e.data["message"]
        for e in res.events
        if e.type == "log" and "budget_total" in e.data["message"]
    ]


@pytest.mark.asyncio
async def test_a_run_past_its_budget_warns_once_and_finishes(caplog) -> None:
    runner = WorkflowRunner(agent_fn=_long_reply, audit=lambda *a, **k: None)
    with caplog.at_level(logging.WARNING, logger="kiro_crew.workflows.budget_watch"):
        res = await runner.run(SCRIPT, run_id="wf_bw", now=NOW, budget_total=150)
    assert res.ok is True, res.error
    assert res.result == 3  # not stopped
    warnings = _warnings(res)
    assert len(warnings) == 1, warnings
    warning = warnings[0]
    assert warning.startswith("budget_total 150 passed: about ")
    assert "the run is not stopped" in warning
    assert any("budget_total 150 passed" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_no_budget_or_a_budget_not_reached_gives_no_warning() -> None:
    runner = WorkflowRunner(agent_fn=_long_reply, audit=lambda *a, **k: None)
    for budget_total in (None, 100_000):
        res = await runner.run(SCRIPT, run_id="wf_bw2", now=NOW, budget_total=budget_total)
        assert res.ok is True, res.error
        assert _warnings(res) == []


def test_the_estimate_counts_prompt_and_result_and_warns_once() -> None:
    from kiro_crew.workflows.budget_watch import BudgetWatch

    watch = BudgetWatch(10)
    assert watch.add("p" * 20, "r" * 20) == ""  # 10 tokens: at, not past
    assert watch.add("p", None) != ""  # 11 > 10
    assert watch.add("p" * 400, "r") == ""  # already warned
    assert watch.estimated == 111  # 10 + 1 (at least one per call) + 401 // 4


@pytest.mark.asyncio
async def test_each_schema_re_ask_is_metered() -> None:
    replies = iter(["x" * 400, '{"a": 1}'])  # one invalid turn, then a valid one

    async def _agent(prompt, opts):
        return next(replies)

    script = (
        'META = {"name": "schema-spend"}\n'
        "async def workflow(ctx):\n"
        "    return await ctx.agent('go', schema={'type': 'object'})\n"
    )
    runner = WorkflowRunner(agent_fn=_agent, audit=lambda *a, **k: None)
    res = await runner.run(script, run_id="wf_bw3", now=NOW, budget_total=100)
    assert res.ok is True, res.error
    assert res.result == {"a": 1}
    # Only the discarded first reply carries the run past 100 tokens.
    assert len(_warnings(res)) == 1
