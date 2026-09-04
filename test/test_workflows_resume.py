"""M6.6 / M5 — resume + restart-subtree ("restart parts" at runtime).

A re-run replays the unchanged prefix of agent calls from the prior run's cache
and re-executes from a chosen call_index. Asserts:
  * runner: replay_results/replay_before reuse cached results for early calls,
    re-execute later ones (proven by a counting agent_fn)
  * RunResult.agent_results captures every call for the next resume
  * WorkflowService.rerun_subtree(run_id, from_index) launches a new run that
    replays before from_index and re-calls after — from the stored handle
  * from_index=0 re-runs everything fresh

All against stub agent_fns — no real model.
See GATES (M6.6) and docs/system-specs/modules/workflows.md.
"""

from __future__ import annotations

import asyncio

import pytest

from kiro_crew.workflows.runner import WorkflowRunner
from kiro_crew.workflows.service import WorkflowService

pytestmark = pytest.mark.asyncio

NOW = "2026-06-18T00:00:00Z"

# A workflow with three distinct agent calls (call_index 0,1,2).
THREE_CALLS = (
    'META = {"name": "three"}\n'
    "async def workflow(ctx):\n"
    "    a = await ctx.agent('first')\n"
    "    b = await ctx.agent('second')\n"
    "    c = await ctx.agent('third')\n"
    "    return {'a': a, 'b': b, 'c': c}\n"
)


def _counting_agent():
    """agent_fn that tags each reply with a live-call counter, to detect replay."""
    state = {"n": 0}

    async def fn(prompt: str, opts: dict):
        state["n"] += 1
        return f"live#{state['n']}:{prompt}"

    return fn, state


# --------------------------------------------------------------------------- #
# Runner-level replay
# --------------------------------------------------------------------------- #


async def test_runresult_captures_agent_results() -> None:
    fn, _ = _counting_agent()
    res = await WorkflowRunner(agent_fn=fn, audit=lambda *a, **k: None).run(
        THREE_CALLS, run_id="r1", now=NOW
    )
    assert res.ok
    assert set(res.agent_results.keys()) == {0, 1, 2}


async def test_replay_before_reuses_prefix_and_reexecutes_tail() -> None:
    # First run captures three results.
    fn1, st1 = _counting_agent()
    first = await WorkflowRunner(agent_fn=fn1, audit=lambda *a, **k: None).run(
        THREE_CALLS, run_id="r1", now=NOW
    )
    assert st1["n"] == 3  # three live calls

    # Re-run replaying calls 0,1 from cache; only call 2 should re-execute live.
    fn2, st2 = _counting_agent()
    second = await WorkflowRunner(agent_fn=fn2, audit=lambda *a, **k: None).run(
        THREE_CALLS,
        run_id="r2",
        now=NOW,
        replay_results=first.agent_results,
        replay_before=2,
    )
    assert st2["n"] == 1  # ONLY the tail (call 2) re-executed
    # calls 0,1 are the cached values from the first run; call 2 is fresh
    assert second.result["a"] == first.agent_results[0]
    assert second.result["b"] == first.agent_results[1]
    assert second.result["c"].startswith("live#1:")  # re-executed this run


async def test_replay_before_zero_reruns_everything() -> None:
    fn1, _ = _counting_agent()
    first = await WorkflowRunner(agent_fn=fn1, audit=lambda *a, **k: None).run(
        THREE_CALLS, run_id="r1", now=NOW
    )
    fn2, st2 = _counting_agent()
    await WorkflowRunner(agent_fn=fn2, audit=lambda *a, **k: None).run(
        THREE_CALLS, run_id="r2", now=NOW, replay_results=first.agent_results, replay_before=0
    )
    assert st2["n"] == 3  # nothing replayed → all three re-executed


# --------------------------------------------------------------------------- #
# Service-level rerun_subtree
# --------------------------------------------------------------------------- #


class FakeSessions:
    async def get_or_create(self, key, **kw):
        return object(), True, False

    def release(self, key, *, cleanup=False):
        pass


async def _wait(svc, rid, timeout=3.0):
    t = 0.0
    while t < timeout:
        s = svc.status(rid)
        if s and s["status"] != "running":
            return s
        await asyncio.sleep(0.02)
        t += 0.02
    raise AssertionError("run did not finish")


async def test_service_rerun_subtree_from_index(monkeypatch) -> None:
    # Patch the agent_fn the service builds so calls are counted + deterministic.
    import kiro_crew.workflows.agent_exec as ae

    calls = {"n": 0}

    def fake_build(sessions, *, run_id, **kw):
        async def fn(prompt, opts):
            calls["n"] += 1
            return f"live:{prompt}"

        return fn

    monkeypatch.setattr(ae, "build_agent_fn", fake_build)
    # service.py imported build_agent_fn by name — patch there too
    import kiro_crew.workflows.service as svc_mod

    monkeypatch.setattr(svc_mod, "build_agent_fn", fake_build)

    # pool_agents=False: this test exercises rerun/replay semantics via the
    # per-call build_agent_fn it patches above — not the warm-session pool.
    svc = WorkflowService(sessions=FakeSessions(), pool_agents=False)
    out = await svc.start(THREE_CALLS, name="three")
    rid = out["run_id"]
    await _wait(svc, rid)
    assert calls["n"] == 3  # original run made 3 live calls

    # Restart from index 2 → replay 0,1, re-execute only call 2.
    calls["n"] = 0
    rr = await svc.rerun_subtree(rid, from_index=2)
    assert "run_id" in rr and rr["replayed_before"] == 2
    await _wait(svc, rr["run_id"])
    assert calls["n"] == 1  # only the tail re-ran


async def test_rerun_unknown_run_errors() -> None:
    svc = WorkflowService(sessions=FakeSessions())
    out = await svc.rerun_subtree("nope", 0)
    assert "error" in out
