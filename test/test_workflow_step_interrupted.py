"""A workflow step whose agent turn was cancelled from outside fails the step.

A gateway restart cancels the running turns. Returning their cut-off text as the
step result made the run read ``finished`` with partial answers. Both executors
(``agent_exec.build_agent_fn`` and ``agent_pool._run_step``) must raise instead.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import kiro_crew.workflows.agent_exec as agent_exec
import kiro_crew.workflows.agent_pool as agent_pool
from kiro_crew.workflows.runner import WorkflowRunner

_STOPPED = ["cancelled", "error: cancel unacked"]


def _stream_ending(stop_reason: str, text: str):
    async def stream(provider, message, *, on_complete=None, **kw):
        on_complete(SimpleNamespace(stop_reason=stop_reason))
        return text

    return stream


class _Sessions:
    def __init__(self) -> None:
        self.released: list[str] = []

    async def get_or_create(self, key, **kw):
        return SimpleNamespace(key=key), True, False

    def release(self, key, *, cleanup=False):
        self.released.append(key)


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_reason", _STOPPED)
async def test_per_call_step_fails_a_turn_stopped_from_outside(monkeypatch, stop_reason) -> None:
    monkeypatch.setattr(
        agent_exec, "stream_and_collect", _stream_ending(stop_reason, "fake slow chunk 0")
    )
    sessions = _Sessions()
    fn = agent_exec.build_agent_fn(sessions, run_id="wf_x")

    with pytest.raises(Exception, match="stopped before it finished"):
        await fn("slow", {})
    assert len(sessions.released) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_reason", _STOPPED)
async def test_pooled_step_fails_a_turn_stopped_from_outside(monkeypatch, stop_reason) -> None:
    monkeypatch.setattr(
        agent_pool, "stream_and_collect", _stream_ending(stop_reason, "fake slow chunk 0")
    )

    with pytest.raises(Exception, match="stopped before it finished"):
        await agent_pool._run_step(object(), "slow", key="wf-pool:wf_x:0")


@pytest.mark.asyncio
async def test_an_ordinary_end_turn_keeps_its_text(monkeypatch) -> None:
    monkeypatch.setattr(agent_exec, "stream_and_collect", _stream_ending("end_turn", "done"))
    monkeypatch.setattr(agent_pool, "stream_and_collect", _stream_ending("end_turn", "done"))

    assert await agent_exec.build_agent_fn(_Sessions(), run_id="wf_x")("quick", {}) == "done"
    assert await agent_pool._run_step(object(), "quick", key="wf-pool:wf_x:0") == "done"


_SLOW_SCRIPT = "\n".join(
    [
        'META = {"name": "slow"}',
        "async def workflow(ctx):",
        "    return await ctx.agent('slow')",
        "",
    ]
)


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_reason", _STOPPED)
async def test_a_run_whose_step_was_stopped_ends_failed(monkeypatch, stop_reason) -> None:
    monkeypatch.setattr(
        agent_exec, "stream_and_collect", _stream_ending(stop_reason, "fake slow chunk 0")
    )
    runner = WorkflowRunner(agent_fn=agent_exec.build_agent_fn(_Sessions(), run_id="wf_r"))

    res = await runner.run(_SLOW_SCRIPT, run_id="wf_r", now="2026-01-01T00:00:00Z")

    assert res.ok is False
    assert res.result is None
    terminal = res.events[-1]
    assert terminal.type == "run_failed"
    assert terminal.data["where"] == "interrupted"
    assert "stopped before it finished" in terminal.data["error"]
    assert "run_finished" not in [e.type for e in res.events]


@pytest.mark.asyncio
async def test_a_run_whose_step_failed_on_its_own_still_finishes() -> None:
    async def agent(prompt, opts):
        raise RuntimeError("agent died")

    res = await WorkflowRunner(agent_fn=agent).run(
        _SLOW_SCRIPT, run_id="wf_o", now="2026-01-01T00:00:00Z"
    )

    assert res.ok is True
    assert res.events[-1].type == "run_finished"
