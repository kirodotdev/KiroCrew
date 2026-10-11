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
from kiro_crew.workflows.store import WorkflowRunStore

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
        replay_fingerprints=first.agent_fingerprints,
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
        THREE_CALLS,
        run_id="r2",
        now=NOW,
        replay_results=first.agent_results,
        replay_before=0,
        replay_fingerprints=first.agent_fingerprints,
    )
    assert st2["n"] == 3  # nothing replayed → all three re-executed


async def test_replay_without_fingerprints_runs_every_call_live() -> None:
    # A cached run with no call fingerprints (a record written before they
    # existed) cannot tell which result belongs to which call, so nothing replays.
    fn1, _ = _counting_agent()
    first = await WorkflowRunner(agent_fn=fn1, audit=lambda *a, **k: None).run(
        THREE_CALLS, run_id="r1", now=NOW
    )
    fn2, st2 = _counting_agent()
    second = await WorkflowRunner(agent_fn=fn2, audit=lambda *a, **k: None).run(
        THREE_CALLS, run_id="r2", now=NOW, replay_results=first.agent_results, replay_before=3
    )
    assert st2["n"] == 3
    assert second.result["a"].startswith("live#1:")


async def test_replay_hands_identical_prompts_their_results_in_order() -> None:
    script = (
        'META = {"name": "twice"}\n'
        "async def workflow(ctx):\n"
        "    return [await ctx.agent('same'), await ctx.agent('same')]\n"
    )
    fn1, _ = _counting_agent()
    first = await WorkflowRunner(agent_fn=fn1, audit=lambda *a, **k: None).run(
        script, run_id="r1", now=NOW
    )
    fn2, st2 = _counting_agent()
    second = await WorkflowRunner(agent_fn=fn2, audit=lambda *a, **k: None).run(
        script,
        run_id="r2",
        now=NOW,
        replay_results=first.agent_results,
        replay_before=1,
        replay_fingerprints=first.agent_fingerprints,
    )
    assert st2["n"] == 1
    assert second.result == [first.agent_results[0], "live#1:same"]


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


# Each chain's second prompt is built from its first call's result, so a replay
# that hands a call another call's result changes the run's output.
PIPELINE_TWO_STAGES = (
    'META = {"name": "pipe"}\n'
    "async def workflow(ctx):\n"
    "    async def draft(item):\n"
    "        return await ctx.agent('draft:' + item)\n"
    "    async def refine(prev):\n"
    "        return await ctx.agent('refine:' + prev)\n"
    "    return await ctx.pipeline(['A', 'B'], draft, refine)\n"
)

PARALLEL_TWO_STEP = (
    'META = {"name": "par"}\n'
    "async def workflow(ctx):\n"
    "    async def two_step(item):\n"
    "        drafted = await ctx.agent('draft:' + item)\n"
    "        return await ctx.agent('refine:' + drafted)\n"
    "    return await ctx.parallel([lambda: two_step('A'), lambda: two_step('B')])\n"
)


def _patch_agent_fn(monkeypatch) -> dict:
    """Route the service's per-run agent_fn to ``holder["fn"]``, set per run."""
    import kiro_crew.workflows.agent_exec as ae
    import kiro_crew.workflows.service as svc_mod

    holder: dict = {}

    def fake_build(sessions, *, run_id, **kw):
        return holder["fn"]

    monkeypatch.setattr(ae, "build_agent_fn", fake_build)
    monkeypatch.setattr(svc_mod, "build_agent_fn", fake_build)
    return holder


def _logging_agent(log: list, hold_a: "asyncio.Event | None" = None):
    """agent_fn that suspends like a real model call and logs each live prompt.

    With ``hold_a``, "draft:A" returns only after "refine:R(draft:B)" has
    started, so chain B starts its second call before chain A does.
    """

    async def fn(prompt: str, opts: dict):
        log.append(prompt)
        if hold_a is not None:
            if prompt == "refine:R(draft:B)":
                hold_a.set()
            if prompt == "draft:A":
                await asyncio.wait_for(hold_a.wait(), timeout=5)
        await asyncio.sleep(0)
        return "R(" + prompt + ")"

    return fn


async def test_rerun_of_a_pipeline_gives_each_replayed_call_its_own_result(monkeypatch) -> None:
    agents = _patch_agent_fn(monkeypatch)
    live: list = []
    agents["fn"] = _logging_agent(live, hold_a=asyncio.Event())
    svc = WorkflowService(sessions=FakeSessions(), pool_agents=False)
    rid = (await svc.start(PIPELINE_TWO_STAGES, name="pipe"))["run_id"]
    first = await _wait(svc, rid)
    assert live == ["draft:A", "draft:B", "refine:R(draft:B)", "refine:R(draft:A)"]

    # Restart at the prior run's call 3: calls 0..2 replay, only call 3 runs live.
    again: list = []
    agents["fn"] = _logging_agent(again)
    rr = await svc.rerun_subtree(rid, from_index=3)
    second = await _wait(svc, rr["run_id"])
    assert second["result"] == first["result"]
    assert first["result"] == ["R(refine:R(draft:A))", "R(refine:R(draft:B))"]
    assert again == ["refine:R(draft:A)"]


async def test_rerun_of_parallel_calls_that_finished_out_of_order(monkeypatch) -> None:
    agents = _patch_agent_fn(monkeypatch)
    live: list = []
    agents["fn"] = _logging_agent(live, hold_a=asyncio.Event())
    svc = WorkflowService(sessions=FakeSessions(), pool_agents=False)
    rid = (await svc.start(PARALLEL_TWO_STEP, name="par"))["run_id"]
    first = await _wait(svc, rid)
    assert live == ["draft:A", "draft:B", "refine:R(draft:B)", "refine:R(draft:A)"]

    # Replay every call: the rerun makes no live call and returns the same result.
    again: list = []
    agents["fn"] = _logging_agent(again)
    rr = await svc.rerun_subtree(rid, from_index=4)
    second = await _wait(svc, rr["run_id"])
    assert second["result"] == first["result"]
    assert first["result"] == ["R(refine:R(draft:A))", "R(refine:R(draft:B))"]
    assert again == []


PARALLEL_SAME_CALL = (
    'META = {"name": "same"}\n'
    "async def workflow(ctx):\n"
    "    async def branch(item):\n"
    "        own = await ctx.agent('first:' + item)\n"
    "        shared = await ctx.agent('same')\n"
    "        return [own, shared]\n"
    "    return await ctx.parallel([lambda: branch('A'), lambda: branch('B')])\n"
)

PIPELINE_SAME_CALL = (
    'META = {"name": "same"}\n'
    "async def workflow(ctx):\n"
    "    async def first(item):\n"
    "        return await ctx.agent('first:' + item)\n"
    "    async def second(prev):\n"
    "        return [prev, await ctx.agent('same')]\n"
    "    return await ctx.pipeline(['A', 'B'], first, second)\n"
)


@pytest.mark.parametrize(
    "script", [PARALLEL_SAME_CALL, PIPELINE_SAME_CALL], ids=["parallel", "pipeline"]
)
async def test_rerun_gives_each_branch_its_own_result_for_the_same_call(
    monkeypatch, script
) -> None:
    """Two branches (or pipeline items) make the same call, with the same prompt and
    options, and got different answers. Branch B's call started first, so it holds the
    lower call index; on the rerun branch A reaches its call first. Each branch must
    replay its own answer."""
    agents = _patch_agent_fn(monkeypatch)
    b_same_started = asyncio.Event()
    answers: list = []

    async def answers_in_start_order(prompt: str, opts: dict):
        if prompt == "first:A":
            await asyncio.wait_for(b_same_started.wait(), timeout=5)
        if prompt == "same":
            answers.append(prompt)
            b_same_started.set()
            answer = "same#" + str(len(answers))
            await asyncio.sleep(0)
            return answer
        await asyncio.sleep(0)
        return "R(" + prompt + ")"

    agents["fn"] = answers_in_start_order
    svc = WorkflowService(sessions=FakeSessions(), pool_agents=False)
    rid = (await svc.start(script, name="same"))["run_id"]
    first = await _wait(svc, rid)
    assert first["result"] == [["R(first:A)", "same#2"], ["R(first:B)", "same#1"]]

    again: list = []
    agents["fn"] = _logging_agent(again)
    rr = await svc.rerun_subtree(rid, from_index=4)
    second = await _wait(svc, rr["run_id"])
    assert again == [], f"the rerun made live calls: {again}"
    assert (
        second["result"] == first["result"]
    ), f"the branches swapped their replayed answers: {second['result']}"


async def test_restart_of_a_run_a_gateway_restart_interrupted_replays_its_finished_calls(
    tmp_path, monkeypatch
) -> None:
    """A run the gateway's restart cut off is restored as failed with the results its
    calls had checkpointed. Restarting it at call 2 replays the two finished calls and
    runs only the third live: each call's fingerprint was checkpointed with its result,
    not only when ``run()`` returned."""
    agents = _patch_agent_fn(monkeypatch)
    third_started = asyncio.Event()
    never = asyncio.Event()

    async def blocks_on_third(prompt: str, opts: dict):
        if prompt == "third":
            third_started.set()
            await never.wait()
        return "R(" + prompt + ")"

    agents["fn"] = blocks_on_third
    store = WorkflowRunStore(base_dir=tmp_path / "workflows")
    before = WorkflowService(sessions=FakeSessions(), pool_agents=False, store=store)
    rid = (await before.start(THREE_CALLS, name="three"))["run_id"]
    handle = before.registry.get(rid)
    try:
        # The calls run in order, so "third" starts only after the first two settled.
        await asyncio.wait_for(third_started.wait(), timeout=10)
        assert sorted(handle.agent_results) == [0, 1]
        # The registry's own checkpoint write, then the next boot reading it back.
        await before.registry.persist_async(rid)
        after = await WorkflowService.create(
            sessions=FakeSessions(), pool_agents=False, store=store
        )
        restored = after.registry.get(rid)
        assert restored is not None and restored.status == "failed"
        assert sorted(restored.agent_results) == [0, 1]

        rerun_live: list = []

        async def logs(prompt: str, opts: dict):
            rerun_live.append(prompt)
            return "R(" + prompt + ")"

        agents["fn"] = logs
        rr = await after.rerun_subtree(rid, from_index=2)
        final = await _wait(after, rr["run_id"])
    finally:
        task = getattr(handle, "task", None)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert rerun_live == [
        "third"
    ], f"restarting the interrupted run at call 2 ran these calls live: {rerun_live}"
    assert final["result"] == {"a": "R(first)", "b": "R(second)", "c": "R(third)"}
