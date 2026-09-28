"""GATES A7 + B5 (+ A4/B6 integration) — WorkflowRunner end-to-end.

A7: a run emits the full documented event stream in order (run_started → … →
run_finished/failed/cancelled). B5: a runaway script is killed by the wall-clock
timeout. Also exercises the A4 budget ceiling and B6 agent cap THROUGH the runner,
and the parallel/pipeline wiring — all against a STUB agent_fn (never real kiro-cli).

See ``docs/system-specs/modules/workflows.md`` and GATES.md (A7, B5, A4, B6).
"""

from __future__ import annotations

import asyncio

import pytest

from kiro_crew.workflows.runner import RunResult, WorkflowRunner

pytestmark = pytest.mark.asyncio

NOW = "2026-06-18T00:00:00Z"


async def _echo_agent(prompt: str, opts: dict):
    """Stub agent: returns the prompt back (no real LLM/kiro-cli)."""
    return f"echo:{prompt}"


def _runner(**kw) -> WorkflowRunner:
    kw.setdefault("agent_fn", _echo_agent)
    return WorkflowRunner(**kw)


def _types(res: RunResult) -> list[str]:
    return [e.type for e in res.events]


# --------------------------------------------------------------------------- #
# A7 — end-to-end event stream
# --------------------------------------------------------------------------- #

GOOD_SCRIPT = (
    'META = {"name": "demo", "description": "d"}\n'
    "async def workflow(ctx):\n"
    "    ctx.phase('Work')\n"
    "    ctx.log('starting')\n"
    "    r = await ctx.agent('hello')\n"
    "    return {'said': r}\n"
)


async def test_happy_path_emits_full_stream_in_order() -> None:
    res = await _runner().run(GOOD_SCRIPT, run_id="wf_1", now=NOW)
    assert res.ok
    assert res.result == {"said": "echo:hello"}
    assert _types(res) == [
        "run_started",
        "phase_started",
        "log",
        "agent_started",
        "agent_finished",
        "run_finished",
    ]
    # seq is monotonic from 0
    assert [e.seq for e in res.events] == list(range(len(res.events)))


async def test_run_started_first_and_finished_last() -> None:
    res = await _runner().run(GOOD_SCRIPT, run_id="wf_2", now=NOW, budget_total=5000)
    assert res.events[0].type == "run_started"
    assert res.events[0].data["budget_total"] == 5000
    assert res.events[0].data["name"] == "demo"
    assert res.events[-1].type == "run_finished"


async def test_invalid_script_fails_at_validate_stage() -> None:
    bad = "import os\n" + GOOD_SCRIPT
    res = await _runner().run(bad, run_id="wf_3", now=NOW)
    assert res.ok is False
    assert _types(res) == ["run_started", "run_failed"]
    assert res.events[-1].data["where"] == "validate"


async def test_script_exception_becomes_run_failed_exec() -> None:
    script = 'META = {"name": "boom"}\n' "async def workflow(ctx):\n" "    return 1 / 0\n"
    res = await _runner().run(script, run_id="wf_4", now=NOW)
    assert res.ok is False
    assert res.events[-1].type == "run_failed"
    assert res.events[-1].data["where"] == "exec"


# --------------------------------------------------------------------------- #
# B5 — wall-clock timeout
# --------------------------------------------------------------------------- #


async def test_wall_clock_timeout_kills_runaway() -> None:
    script = (
        'META = {"name": "slow"}\n'
        "async def workflow(ctx):\n"
        "    await ctx.agent('takes too long')\n"
        "    return 'never'\n"
    )

    # Block deterministically on an Event that is NEVER set (not a wall-clock
    # sleep race) so the wall-clock timeout fires at a well-defined point —
    # stable even under coverage's tracer slowdown (see LOOP.md intervention log).
    never = asyncio.Event()

    async def _hang_agent(prompt: str, opts: dict):
        await never.wait()  # blocks forever; only the run timeout breaks it
        return "done"

    res = await _runner(agent_fn=_hang_agent, timeout_secs=0.05).run(script, run_id="wf_5", now=NOW)
    assert res.ok is False
    assert res.error == "timeout"
    assert res.events[-1].type == "run_failed"
    assert res.events[-1].data["where"] == "ceiling"


async def test_timeout_never_leaks_cancellederror() -> None:
    """B5 regression: the wall-clock guard MUST convert a timeout into a clean
    ``run_failed(where="ceiling")`` and NEVER propagate ``asyncio.CancelledError``
    to the caller.

    The original ``asyncio.wait_for`` guard leaked the inner ``CancelledError`` on
    CPython 3.10 when the timeout raced task completion — reproducible under
    ``coverage`` instrumentation and on a loaded fleet under 16-worker xdist
    contention (a ``test_workflows_*`` node flipped RED with a *raw*
    ``CancelledError`` escaping ``run()``). With ``asyncio.wait(timeout=)`` the
    runner owns the cancellation, so a leak would surface here as a *raised*
    exception (deterministic failure) rather than a flaky one. ``timeout_secs=0``
    forces the timeout branch every time; looping makes the absence of any leak
    explicit across repeated runs.
    """

    async def _never_returns(prompt: str, opts: dict):
        import asyncio as _aio

        await _aio.sleep(3600)  # vastly longer than the timeout; only cancel breaks it
        return "unreachable"

    script = (
        'META = {"name": "runaway"}\n'
        "async def workflow(ctx):\n"
        "    await ctx.agent('spin')\n"
        "    return 'never'\n"
    )
    runner = _runner(agent_fn=_never_returns, timeout_secs=0)
    for i in range(5):
        # A leaked CancelledError would raise out of run() here (not be caught).
        res = await runner.run(script, run_id=f"wf_to_{i}", now=NOW)
        assert res.ok is False, f"iteration {i}"
        assert res.error == "timeout", f"iteration {i}"
        assert res.events[-1].type == "run_failed"
        assert res.events[-1].data["where"] == "ceiling"


async def test_caller_cancellation_yields_run_cancelled() -> None:
    """If the RUN task itself is cancelled by our caller (shutdown, not a timeout),
    the runner stops the in-flight script and reports ``run_cancelled`` — it must
    NOT surface as a timeout and must NOT leak ``CancelledError`` out of ``run()``.

    Distinct from the wall-clock guard: here cancellation arrives from OUTSIDE
    while the script is mid-flight (long timeout so the B5 ceiling can't fire first).
    """
    started = asyncio.Event()

    async def _hang_agent(prompt: str, opts: dict):
        started.set()
        await asyncio.Event().wait()  # blocks until the run task is cancelled
        return "done"

    script = (
        'META = {"name": "cancelme"}\n'
        "async def workflow(ctx):\n"
        "    await ctx.agent('spin')\n"
        "    return 'never'\n"
    )
    run_coro = _runner(agent_fn=_hang_agent, timeout_secs=3600).run(
        script, run_id="wf_cancel", now=NOW
    )
    run_task = asyncio.ensure_future(run_coro)
    await started.wait()  # ensure we're inside the agent before cancelling
    run_task.cancel()
    res = await run_task  # run() swallows the cancel and returns a result
    assert res.ok is False
    assert res.error == "cancelled"
    assert res.events[-1].type == "run_cancelled"


# --------------------------------------------------------------------------- #
# A4 / B6 — ceilings enforced THROUGH the runner
# --------------------------------------------------------------------------- #


async def test_agent_count_cap_enforced() -> None:
    script = (
        'META = {"name": "spam"}\n'
        "async def workflow(ctx):\n"
        "    n = 0\n"
        "    while True:\n"
        "        await ctx.agent('x')\n"
        "        n = n + 1\n"
        "    return n\n"
    )
    res = await _runner(max_agents_per_run=3).run(script, run_id="wf_6", now=NOW)
    # The cap raises BudgetExceeded → run_failed at the ceiling.
    assert res.ok is False
    assert res.events[-1].data["where"] == "ceiling"
    # exactly 3 agent_started events before the cap tripped
    assert sum(1 for e in res.events if e.type == "agent_started") == 3


async def test_failed_agent_resolves_to_none_not_run_failure() -> None:
    async def _boom_agent(prompt: str, opts: dict):
        raise RuntimeError("agent died")

    script = (
        'META = {"name": "resilient"}\n'
        "async def workflow(ctx):\n"
        "    r = await ctx.agent('try')\n"
        "    return {'r': r}\n"
    )
    res = await _runner(agent_fn=_boom_agent).run(script, run_id="wf_7", now=NOW)
    assert res.ok is True  # a dead agent → None, the run still succeeds
    assert res.result == {"r": None}
    finished = [e for e in res.events if e.type == "agent_finished"][0]
    assert finished.data["ok"] is False
    # ...and it says WHY. A bare ok=False made post-mortems impossible: you could
    # not tell a throttle from a bad prompt from a crashed backend.
    assert finished.data["error"] == "RuntimeError: agent died"
    assert res.agent_errors == {0: "RuntimeError: agent died"}


# --------------------------------------------------------------------------- #
# parallel / pipeline wired through ctx
# --------------------------------------------------------------------------- #


async def test_parallel_and_pipeline_through_ctx() -> None:
    script = (
        'META = {"name": "fanout"}\n'
        "async def workflow(ctx):\n"
        "    outs = await ctx.parallel([lambda: ctx.agent('a'), lambda: ctx.agent('b')])\n"
        "    piped = await ctx.pipeline([1, 2], lambda prev: prev * 10)\n"
        "    return {'outs': outs, 'piped': piped}\n"
    )
    res = await _runner().run(script, run_id="wf_8", now=NOW)
    assert res.ok, res.error
    assert res.result["outs"] == ["echo:a", "echo:b"]
    assert res.result["piped"] == [10, 20]


# --------------------------------------------------------------------------- #
# M6.7 — author-in-run: intent → "Authoring" phase → execution, no blocking
# --------------------------------------------------------------------------- #


async def test_author_in_run_emits_authoring_phase_then_executes() -> None:
    """intent + author_fn: a visible Authoring phase precedes the run, progress
    streams as log events, and the authored script then executes to completion."""
    authored = (
        'META = {"name": "authored-demo"}\n'
        "async def workflow(ctx):\n"
        "    ctx.phase('Work')\n"
        "    return {'r': await ctx.agent('go')}\n"
    )

    async def _author_fn(intent: str, *, on_progress=None):
        if on_progress:
            on_progress("drafting…")
        return {"ok": True, "source": authored, "meta": {"name": "authored-demo"}}

    res = await _runner().run(
        "", run_id="wf_a1", now=NOW, intent="build me a thing", author_fn=_author_fn
    )
    assert res.ok, res.error
    assert res.result == {"r": "echo:go"}
    assert res.source == authored
    types = _types(res)
    # run_started is first; an Authoring phase + progress log precede execution.
    assert types[0] == "run_started"
    assert "phase_started" in types and "log" in types
    titles = [e.data["title"] for e in res.events if e.type == "phase_started"]
    assert "Authoring" in titles and "Work" in titles
    logs = [e.data["message"] for e in res.events if e.type == "log"]
    assert any("drafting" in m for m in logs)
    assert types[-1] == "run_finished"


async def test_author_in_run_failure_becomes_run_failed() -> None:
    """If authoring can't produce a valid script, the run fails cleanly with a
    run_failed(where='author') — never crashes, never blocks."""

    async def _bad_author(intent: str, *, on_progress=None):
        return {"ok": False, "errors": ["no META", "no workflow()"]}

    res = await _runner().run(
        "", run_id="wf_a2", now=NOW, intent="nonsense", author_fn=_bad_author
    )
    assert not res.ok
    failed = [e for e in res.events if e.type == "run_failed"]
    assert failed and failed[-1].data["where"] == "author"


async def test_author_in_run_exception_is_captured() -> None:
    """An exception inside authoring is captured as a failed run, not propagated."""

    async def _explode(intent: str, *, on_progress=None):
        raise RuntimeError("model down")

    res = await _runner().run(
        "", run_id="wf_a3", now=NOW, intent="x", author_fn=_explode
    )
    assert not res.ok
    assert "model down" in (res.error or "")
    assert any(e.type == "run_failed" for e in res.events)


async def test_author_in_run_publishes_source_before_execution() -> None:
    """on_source fires the instant authoring completes — BEFORE the (blocking)
    agent step — so 'View source' works mid-run, not only after it finishes.
    Regression: source was written onto the handle only after run() returned."""
    authored = (
        'META = {"name": "src-demo"}\n'
        "async def workflow(ctx):\n"
        "    ctx.phase('Work')\n"
        "    return {'r': await ctx.agent('go')}\n"
    )

    async def _author_fn(intent: str, *, on_progress=None):
        return {"ok": True, "source": authored, "meta": {"name": "src-demo"}}

    gate = asyncio.Event()
    published: list[str] = []

    async def _blocking_agent(prompt: str, opts: dict):
        # Block the run inside execution so we can assert source was already
        # published during the Authoring→exec transition, before completion.
        await gate.wait()
        return "done"

    runner = WorkflowRunner(agent_fn=_blocking_agent)
    task = asyncio.ensure_future(
        runner.run(
            "", run_id="wf_src", now=NOW, intent="x", author_fn=_author_fn,
            on_source=published.append,
        )
    )
    # Yield until the blocked agent step is reached; source must already be set.
    for _ in range(50):
        if published:
            break
        await asyncio.sleep(0.01)
    assert published == [authored], "source not published before execution blocked"
    gate.set()
    res = await task
    assert res.ok and res.source == authored
