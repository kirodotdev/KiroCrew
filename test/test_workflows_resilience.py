"""Failure resilience for dynamic workflow runs.

Regression suite for the wf_000032 post-mortem: an hour-long run hit the 3600s
wall-clock ceiling while its last agent was in flight, and the engine persisted
``result: null`` with ``agent_results: {}`` — silently discarding five completed
specialist payloads. The failed agents recorded ``ok: false`` with no error text
at all, so their cause was unrecoverable.

What is locked in here:

* the ceiling (and cancel / budget / crash) still terminates the run, but now
  hands back every agent result collected so far;
* each result is checkpointed onto the registry handle as it lands, so an
  interruption that never reaches a terminal return still has the work;
* a failed call records a bounded reason, distinguishing an exception from a
  schema miss from a plain empty answer;
* the ceiling is configurable per run but can never be removed;
* agent concurrency is bounded run-globally, not merely per combinator.
"""

from __future__ import annotations

import asyncio

import pytest

from kiro_crew.workflows.registry import STATUS_FAILED, RunRegistry
from kiro_crew.workflows.runner import WorkflowRunner

pytestmark = pytest.mark.asyncio

NOW = "2026-07-30T00:00:00Z"


async def _echo_agent(prompt: str, opts: dict):
    return f"echo:{prompt}"


def _runner(**kw) -> WorkflowRunner:
    kw.setdefault("agent_fn", _echo_agent)
    return WorkflowRunner(**kw)


# --------------------------------------------------------------------------- #
# partials survive every terminal path
# --------------------------------------------------------------------------- #

# Two agents finish, then the script blocks forever — the shape of wf_000032,
# where specialists completed and the critic was still running at the ceiling.
SCRIPT_TWO_THEN_HANG = (
    'META = {"name": "hang-after-work"}\n'
    "async def workflow(ctx):\n"
    "    await ctx.agent('first')\n"
    "    await ctx.agent('second')\n"
    "    await ctx.agent('never-returns')\n"
    "    return 'unreachable'\n"
)


def _hang_on(marker: str):
    """Agent stub that answers normally until it sees ``marker``, then blocks."""

    async def _agent(prompt: str, opts: dict):
        if marker in prompt:
            await asyncio.Event().wait()  # only cancellation breaks this
        return f"echo:{prompt}"

    return _agent


async def test_ceiling_returns_completed_agent_results() -> None:
    """The regression: a timeout used to return agent_results={} and lose the lot."""
    res = await _runner(agent_fn=_hang_on("never-returns"), timeout_secs=0.2).run(
        SCRIPT_TWO_THEN_HANG, run_id="wf_ceiling", now=NOW
    )
    assert res.ok is False
    assert res.error == "timeout"
    assert res.events[-1].type == "run_failed"
    assert res.events[-1].data["where"] == "ceiling"
    # The script never returned, so there is no whole...
    assert res.result is None
    # ...but the parts it already paid for are handed back.
    assert res.agent_results == {0: "echo:first", 1: "echo:second"}


async def test_cancellation_returns_completed_agent_results() -> None:
    run_coro = _runner(agent_fn=_hang_on("never-returns"), timeout_secs=3600).run(
        SCRIPT_TWO_THEN_HANG, run_id="wf_cancel", now=NOW
    )
    task = asyncio.ensure_future(run_coro)
    # Let the two quick agents land before cancelling the run out from under it.
    for _ in range(50):
        await asyncio.sleep(0)
    task.cancel()
    res = await task
    assert res.error == "cancelled"
    assert res.result is None
    assert res.agent_results == {0: "echo:first", 1: "echo:second"}


async def test_script_crash_returns_completed_agent_results() -> None:
    script = (
        'META = {"name": "crash-after-work"}\n'
        "async def workflow(ctx):\n"
        "    await ctx.agent('first')\n"
        "    raise RuntimeError('script blew up')\n"
    )
    res = await _runner().run(script, run_id="wf_crash", now=NOW)
    assert res.ok is False
    assert res.events[-1].data["where"] == "exec"
    assert res.agent_results == {0: "echo:first"}


async def test_budget_ceiling_returns_completed_agent_results() -> None:
    script = (
        'META = {"name": "budget-after-work"}\n'
        "async def workflow(ctx):\n"
        "    await ctx.agent('first')\n"
        "    await ctx.agent('second')\n"
        "    return 'done'\n"
    )
    # A cap of 1 lets the first call through and trips on the second.
    res = await _runner(max_agents_per_run=1).run(script, run_id="wf_budget", now=NOW)
    assert res.ok is False
    assert res.events[-1].data["where"] == "ceiling"
    assert res.agent_results == {0: "echo:first"}


# --------------------------------------------------------------------------- #
# per-call checkpointing (durability before the terminal state)
# --------------------------------------------------------------------------- #


async def test_agent_results_checkpoint_as_each_call_lands() -> None:
    """Results must reach the host DURING the run, not only at its end."""
    seen: list[tuple[int, object, bool, str]] = []

    def _sink(call_index: int, *, result, ok: bool, error: str) -> None:
        seen.append((call_index, result, ok, error))

    script = (
        'META = {"name": "checkpointed"}\n'
        "async def workflow(ctx):\n"
        "    await ctx.agent('one')\n"
        "    await ctx.agent('two')\n"
        "    return 'ok'\n"
    )
    res = await _runner().run(
        script, run_id="wf_ckpt", now=NOW, on_agent_result=_sink
    )
    assert res.ok
    assert seen == [
        (0, "echo:one", True, ""),
        (1, "echo:two", True, ""),
    ]


async def test_checkpoint_sink_failure_never_breaks_the_run() -> None:
    def _bad_sink(call_index: int, **kw) -> None:
        raise RuntimeError("sink exploded")

    script = (
        'META = {"name": "bad-sink"}\n'
        "async def workflow(ctx):\n"
        "    return await ctx.agent('one')\n"
    )
    res = await _runner().run(
        script, run_id="wf_badsink", now=NOW, on_agent_result=_bad_sink
    )
    assert res.ok
    assert res.result == "echo:one"


async def test_registry_keeps_checkpointed_results_when_run_hits_ceiling() -> None:
    """End-to-end: a background run killed at the ceiling still has its payloads
    on the handle — which is what ``workflow_result`` reads."""
    registry = RunRegistry()
    runner = _runner(agent_fn=_hang_on("never-returns"), timeout_secs=0.2)
    run_id = await runner.run_background(
        SCRIPT_TWO_THEN_HANG,
        registry=registry,
        run_id="wf_bg_ceiling",
        now=NOW,
        name="bg-ceiling",
    )
    handle = registry.get(run_id)
    assert handle is not None and handle.task is not None
    await handle.task

    assert handle.status == STATUS_FAILED
    assert handle.error == "timeout"
    assert handle.result is None
    assert handle.agent_results == {0: "echo:first", 1: "echo:second"}
    # The detail snapshot (what workflow_result returns) surfaces them, and the
    # compact one carries the count so a completion message can mention it.
    full = handle.snapshot(include_events=True)
    assert full["partial_results"] == {"0": "echo:first", "1": "echo:second"}
    assert handle.snapshot(include_events=False)["partial_result_count"] == 2


async def test_successful_run_reports_no_partials() -> None:
    """A finished run's ``result`` already carries the synthesis — partials would
    just double the payload, so they stay absent."""
    registry = RunRegistry()
    script = (
        'META = {"name": "fine"}\n'
        "async def workflow(ctx):\n"
        "    return await ctx.agent('one')\n"
    )
    run_id = await runner_bg(registry, script, "wf_bg_ok")
    handle = registry.get(run_id)
    assert handle is not None
    assert handle.result == "echo:one"
    full = handle.snapshot(include_events=True)
    assert "partial_results" not in full
    assert "partial_result_count" not in handle.snapshot(include_events=False)
    # The resume cache is still populated (that is what rerun_subtree replays).
    assert handle.agent_results == {0: "echo:one"}


async def runner_bg(registry: RunRegistry, script: str, run_id: str) -> str:
    """Run ``script`` to completion as a background run; returns its run_id."""
    rid = await _runner().run_background(
        script, registry=registry, run_id=run_id, now=NOW, name=run_id
    )
    handle = registry.get(rid)
    assert handle is not None and handle.task is not None
    await handle.task
    return rid


async def test_terminal_merge_never_erases_checkpoints() -> None:
    """``_drive`` used to assign ``handle.agent_results = agent_results or {}``,
    so any terminal path handing back an empty map wiped the checkpoints."""
    registry = RunRegistry()
    registry.record_agent_result("missing-run", 0, result="x")  # no-op, must not raise

    handle_id = await runner_bg(
        registry,
        'META = {"name": "merge"}\nasync def workflow(ctx):\n    return await ctx.agent("a")\n',
        "wf_merge",
    )
    handle = registry.get(handle_id)
    assert handle is not None
    registry.record_agent_result(handle_id, 5, result="late", ok=True)
    assert handle.agent_results[0] == "echo:a"
    assert handle.agent_results[5] == "late"


# --------------------------------------------------------------------------- #
# failure diagnostics
# --------------------------------------------------------------------------- #


async def test_schema_miss_is_distinguished_from_an_exception() -> None:
    """Both yield None; only the reason tells you which knob to turn."""

    async def _off_schema(prompt: str, opts: dict):
        return "not json at all"

    script = (
        'META = {"name": "schema"}\n'
        "async def workflow(ctx):\n"
        "    return await ctx.agent('x', schema={'type': 'object'})\n"
    )
    res = await _runner(agent_fn=_off_schema).run(script, run_id="wf_schema", now=NOW)
    assert res.result is None
    assert res.agent_errors == {0: "no schema-valid result after bounded re-asks"}


async def test_empty_answer_is_distinguished_from_an_exception() -> None:
    async def _none_agent(prompt: str, opts: dict):
        return None

    script = (
        'META = {"name": "empty"}\n'
        "async def workflow(ctx):\n"
        "    return await ctx.agent('x')\n"
    )
    res = await _runner(agent_fn=_none_agent).run(script, run_id="wf_empty", now=NOW)
    assert res.agent_errors == {0: "agent returned no result"}


async def test_agent_error_is_bounded() -> None:
    async def _verbose_boom(prompt: str, opts: dict):
        raise ValueError("x" * 5000)

    script = (
        'META = {"name": "verbose"}\n'
        "async def workflow(ctx):\n"
        "    return await ctx.agent('x')\n"
    )
    res = await _runner(agent_fn=_verbose_boom).run(script, run_id="wf_big", now=NOW)
    # Bounded so a wide fan-out of failures cannot bloat the persisted record.
    assert len(res.agent_errors[0]) < 600
    assert res.agent_errors[0].startswith("ValueError: xxx")


# --------------------------------------------------------------------------- #
# the ceiling is configurable but never removable
# --------------------------------------------------------------------------- #


async def test_per_run_timeout_applies() -> None:
    """A tiny per-run ceiling must actually cut the run short."""
    script = (
        'META = {"name": "slow"}\n'
        "async def workflow(ctx):\n"
        "    return await ctx.agent('never-returns')\n"
    )
    res = await _runner(agent_fn=_hang_on("never"), timeout_secs=0.2).run(
        script, run_id="wf_tiny", now=NOW
    )
    assert res.error == "timeout"


async def test_service_threads_and_clamps_a_per_run_timeout() -> None:
    """The per-run override must reach the runner through the SERVICE entry point.

    `_runner(timeout_secs=…)` is where the ceiling is actually chosen, so a test
    that only passes `timeout_secs` to the WorkflowRunner constructor would not
    cover the "configurable per run" claim at all.
    """
    from kiro_crew.workflows.runner import MAX_RUN_TIMEOUT_SECS, MIN_RUN_TIMEOUT_SECS
    from kiro_crew.workflows.service import WorkflowService

    svc = WorkflowService(sessions=None, persist=False, timeout_secs=1800)
    assert svc.timeout_secs == 1800  # service default, clamped at construction
    assert svc._runner("wf_a")._timeout_secs == 1800  # no override → default
    assert svc._runner("wf_b", timeout_secs=7200)._timeout_secs == 7200  # lengthened
    # Out-of-range and junk overrides can never remove or shrink past the bounds.
    assert svc._runner("wf_c", timeout_secs=99999999)._timeout_secs == MAX_RUN_TIMEOUT_SECS
    assert svc._runner("wf_d", timeout_secs=1)._timeout_secs == MIN_RUN_TIMEOUT_SECS
    assert svc._runner("wf_e", timeout_secs=0)._timeout_secs == 1800
    assert svc._runner("wf_f", timeout_secs=None)._timeout_secs == 1800


async def test_service_default_timeout_is_itself_clamped() -> None:
    from kiro_crew.workflows.runner import DEFAULT_RUN_TIMEOUT_SECS, MAX_RUN_TIMEOUT_SECS
    from kiro_crew.workflows.service import WorkflowService

    assert WorkflowService(sessions=None, persist=False).timeout_secs == (
        DEFAULT_RUN_TIMEOUT_SECS
    )
    # A deployment cannot disable the backstop through config.
    assert WorkflowService(sessions=None, persist=False, timeout_secs=0).timeout_secs == (
        DEFAULT_RUN_TIMEOUT_SECS
    )
    assert WorkflowService(
        sessions=None, persist=False, timeout_secs=99999999
    ).timeout_secs == MAX_RUN_TIMEOUT_SECS


# --------------------------------------------------------------------------- #
# concurrency is bounded run-globally
# --------------------------------------------------------------------------- #


async def test_concurrency_is_bounded_across_separate_combinators() -> None:
    """Each combinator builds its OWN semaphore, so two overlapping fan-outs could
    exceed the configured cap in aggregate. The run-global slot in ``ctx.agent``
    is what makes the cap honest."""
    live = 0
    peak = 0

    async def _tracking_agent(prompt: str, opts: dict):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        try:
            await asyncio.sleep(0.02)
            return f"echo:{prompt}"
        finally:
            live -= 1

    # Two nested parallel fan-outs of 3 → 6 agent calls in flight at once unless
    # something bounds the RUN rather than each combinator.
    script = (
        'META = {"name": "overlap"}\n'
        "async def workflow(ctx):\n"
        "    async def _fanout(tag):\n"
        "        return await ctx.parallel([\n"
        "            lambda: ctx.agent(tag + '1'),\n"
        "            lambda: ctx.agent(tag + '2'),\n"
        "            lambda: ctx.agent(tag + '3'),\n"
        "        ])\n"
        "    return await ctx.parallel([lambda: _fanout('a'), lambda: _fanout('b')])\n"
    )
    res = await _runner(agent_fn=_tracking_agent, concurrency=2).run(
        script, run_id="wf_conc", now=NOW
    )
    assert res.ok
    assert peak <= 2, f"run-global cap of 2 exceeded: peak={peak}"


async def test_no_cap_leaves_agent_calls_unbounded() -> None:
    """concurrency=None keeps the documented "no limit" behaviour (tests rely on
    it, and the slot helper must be a genuine no-op rather than a cap of 1)."""
    live = 0
    peak = 0

    async def _tracking_agent(prompt: str, opts: dict):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        try:
            await asyncio.sleep(0.02)
            return f"echo:{prompt}"
        finally:
            live -= 1

    script = (
        'META = {"name": "unbounded"}\n'
        "async def workflow(ctx):\n"
        "    return await ctx.parallel([\n"
        "        lambda: ctx.agent('a'),\n"
        "        lambda: ctx.agent('b'),\n"
        "        lambda: ctx.agent('c'),\n"
        "    ])\n"
    )
    res = await _runner(agent_fn=_tracking_agent, concurrency=None).run(
        script, run_id="wf_unbounded", now=NOW
    )
    assert res.ok
    assert peak == 3
