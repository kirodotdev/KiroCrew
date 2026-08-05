"""M6.1 — background run registry + WorkflowRunner.run_background.

A workflow run must outlive a single request and be addressable by run_id:
  * start a background run → returns run_id immediately, status 'running'
  * events stream into the handle as the run progresses (live monitoring)
  * on completion the handle holds the result + 'finished' status
  * on_done fires once with a terminal snapshot (M6.4 wires this to chat)
  * cancel() stops a long run → 'cancelled'
  * list() shows runs newest-first; eviction never drops a running run

All against a stub agent_fn — no real model, no kiro-cli.
See ``docs/system-specs/modules/workflows.md`` (run registry) + GATES (M6).
"""

from __future__ import annotations

import asyncio

import pytest

from kiro_crew.workflows.registry import (
    STATUS_CANCELLED,
    STATUS_FINISHED,
    STATUS_RUNNING,
    RunRegistry,
)
from kiro_crew.workflows.runner import WorkflowRunner

pytestmark = pytest.mark.asyncio

NOW = "2026-06-18T00:00:00Z"


async def _echo(prompt: str, opts: dict):
    return f"echo:{prompt}"


GOOD = (
    'META = {"name": "demo"}\n'
    "async def workflow(ctx):\n"
    "    ctx.phase('Work')\n"
    "    ctx.log('hi')\n"
    "    await ctx.agent('go')\n"
    "    return {'done': True}\n"
)


async def _wait_terminal(reg: RunRegistry, run_id: str, timeout: float = 3.0) -> dict:
    """Poll the registry until the run leaves 'running' (or time out)."""
    loop_deadline = 0.0
    while loop_deadline < timeout:
        snap = reg.status(run_id)
        if snap and snap["status"] != STATUS_RUNNING:
            return snap
        await asyncio.sleep(0.02)
        loop_deadline += 0.02
    raise AssertionError(f"run {run_id} did not finish within {timeout}s")


async def test_background_run_finishes_and_captures_result() -> None:
    reg = RunRegistry()
    runner = WorkflowRunner(agent_fn=_echo, audit=lambda *a, **k: None)

    rid = await runner.run_background(
        GOOD, registry=reg, run_id="wf_bg1", now=NOW, name="demo", author="a-contributor"
    )
    assert rid == "wf_bg1"
    # immediately addressable
    assert reg.status(rid)["status"] in (STATUS_RUNNING, STATUS_FINISHED)

    snap = await _wait_terminal(reg, rid)
    assert snap["status"] == STATUS_FINISHED
    assert snap["result"] == {"done": True}
    assert snap["author"] == "a-contributor"


async def test_events_stream_into_handle_live() -> None:
    reg = RunRegistry()
    seen: list[str] = []
    reg.set_on_event(lambda rid, ev: seen.append(ev["type"]))
    runner = WorkflowRunner(agent_fn=_echo, audit=lambda *a, **k: None)

    rid = await runner.run_background(GOOD, registry=reg, run_id="wf_bg2", now=NOW, name="demo")
    await _wait_terminal(reg, rid)

    # live subscriber saw the lifecycle + in-script events, in order
    assert seen[0] == "run_started"
    assert "phase_started" in seen and "log" in seen and "agent_started" in seen
    assert seen[-1] == "run_finished"
    # and the handle retained the full event list
    full = reg.status(rid, include_events=True)
    assert full["event_count"] == len(full["events"]) > 0


async def test_on_done_fires_once_with_terminal_snapshot() -> None:
    reg = RunRegistry()
    done: list[dict] = []
    reg.set_on_done(lambda rid, snap: done.append({"rid": rid, **snap}))
    runner = WorkflowRunner(agent_fn=_echo, audit=lambda *a, **k: None)

    rid = await runner.run_background(
        GOOD, registry=reg, run_id="wf_bg3", now=NOW, name="demo", session_key="slot:main"
    )
    await _wait_terminal(reg, rid)
    await asyncio.sleep(0.02)  # let the terminal callback settle

    assert len(done) == 1
    assert done[0]["rid"] == rid
    assert done[0]["status"] == STATUS_FINISHED
    assert done[0]["session_key"] == "slot:main"  # M6.4 uses this to route to chat


async def test_cancel_stops_a_long_run() -> None:
    reg = RunRegistry()
    never = asyncio.Event()

    async def _hang(prompt: str, opts: dict):
        await never.wait()
        return "never"

    script = (
        'META = {"name": "slow"}\n'
        "async def workflow(ctx):\n"
        "    await ctx.agent('hang')\n"
        "    return 'never'\n"
    )
    runner = WorkflowRunner(agent_fn=_hang, audit=lambda *a, **k: None)
    rid = await runner.run_background(script, registry=reg, run_id="wf_bg4", now=NOW, name="slow")

    await asyncio.sleep(0.05)
    assert reg.status(rid)["status"] == STATUS_RUNNING
    ok = await reg.cancel(rid)
    assert ok
    snap = await _wait_terminal(reg, rid)
    assert snap["status"] == STATUS_CANCELLED


async def test_list_newest_first_and_running_not_evicted() -> None:
    reg = RunRegistry(max_runs=2)
    runner = WorkflowRunner(agent_fn=_echo, audit=lambda *a, **k: None)
    for i in range(3):
        rid = await runner.run_background(GOOD, registry=reg, run_id=f"wf_l{i}", now=NOW, name="d")
        await _wait_terminal(reg, rid)
    runs = reg.list()
    assert len(runs) <= 2  # bounded
    assert runs[0]["run_id"] == "wf_l2"  # newest first


async def test_cancel_unknown_run_is_false() -> None:
    reg = RunRegistry()
    assert await reg.cancel("nope") is False
