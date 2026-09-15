"""Tests for the ctx.phase() context-manager fix (community bug: abhijek 7/14).

Validates that:
1. ``with ctx.phase("title"):`` works at runtime (no AttributeError).
2. ``with ctx.log("msg"):`` works at runtime.
3. ``with ctx.agent(...):`` is rejected at validation time.
4. ``async with ctx.phase(...):`` is rejected at validation time.
5. A hello-world workflow using ``with ctx.phase()`` runs end-to-end through
   the runner and produces the correct event stream.
"""

from __future__ import annotations

import pytest

from kiro_crew.workflows.runner import WorkflowRunner
from kiro_crew.workflows.validate import validate

pytestmark = pytest.mark.asyncio

NOW = "2026-07-17T00:00:00Z"


async def _echo_agent(prompt: str, opts: dict):
    """Stub agent: returns the prompt back (no real LLM/kiro-cli)."""
    return f"echo:{prompt}"


def _runner(**kw) -> WorkflowRunner:
    kw.setdefault("agent_fn", _echo_agent)
    return WorkflowRunner(**kw)


# --------------------------------------------------------------------------- #
# Layer 1: ctx.phase() returns a context manager — runtime works
# --------------------------------------------------------------------------- #

SCRIPT_WITH_PHASE_CM = (
    'META = {"name": "phase-cm", "description": "tests with ctx.phase"}\n'
    "async def workflow(ctx):\n"
    "    with ctx.phase('read'):\n"
    "        r = await ctx.agent('hello')\n"
    "    with ctx.phase('write'):\n"
    "        ctx.log('done')\n"
    "    return {'result': r}\n"
)


async def test_with_ctx_phase_runs_successfully() -> None:
    """The community-reported bug: with ctx.phase() raised AttributeError."""
    res = await _runner().run(SCRIPT_WITH_PHASE_CM, run_id="phase_cm_1", now=NOW)
    assert res.ok, f"expected ok, got error: {res.error}"
    assert res.result == {"result": "echo:hello"}


async def test_with_ctx_phase_emits_phase_events() -> None:
    """Both phases emit phase_started events in order."""
    res = await _runner().run(SCRIPT_WITH_PHASE_CM, run_id="phase_cm_2", now=NOW)
    assert res.ok
    types = [e.type for e in res.events]
    # Expect: run_started, phase_started(read), agent_started, agent_finished,
    #         phase_started(write), log, run_finished
    assert "phase_started" in types
    phase_events = [e for e in res.events if e.type == "phase_started"]
    assert len(phase_events) == 2
    assert phase_events[0].data["title"] == "read"
    assert phase_events[1].data["title"] == "write"


SCRIPT_WITH_LOG_CM = (
    'META = {"name": "log-cm", "description": "tests with ctx.log"}\n'
    "async def workflow(ctx):\n"
    "    with ctx.log('starting'):\n"
    "        r = await ctx.agent('work')\n"
    "    return r\n"
)


async def test_with_ctx_log_runs_successfully() -> None:
    """with ctx.log() also works without crashing."""
    res = await _runner().run(SCRIPT_WITH_LOG_CM, run_id="log_cm_1", now=NOW)
    assert res.ok, f"expected ok, got error: {res.error}"
    assert res.result == "echo:work"


# --------------------------------------------------------------------------- #
# Layer 2: validation rejects invalid with-blocks
# --------------------------------------------------------------------------- #


def test_validate_rejects_with_ctx_agent() -> None:
    """with ctx.agent() is invalid — agent is async, not a CM."""
    script = (
        'META = {"name": "bad", "description": "d"}\n'
        "async def workflow(ctx):\n"
        "    with ctx.agent('hi'):\n"
        "        pass\n"
    )
    result = validate(script)
    assert not result.ok
    assert any("with ctx.agent" in e for e in result.errors)


def test_validate_rejects_with_ctx_parallel() -> None:
    """with ctx.parallel() is invalid — parallel is async, not a CM."""
    script = (
        'META = {"name": "bad", "description": "d"}\n'
        "async def workflow(ctx):\n"
        "    with ctx.parallel([]):\n"
        "        pass\n"
    )
    result = validate(script)
    assert not result.ok
    assert any("with ctx.parallel" in e for e in result.errors)


def test_validate_rejects_async_with_ctx_phase() -> None:
    """async with ctx.phase() is invalid — phase is sync, not an async CM."""
    script = (
        'META = {"name": "bad", "description": "d"}\n'
        "async def workflow(ctx):\n"
        "    async with ctx.phase('x'):\n"
        "        pass\n"
    )
    result = validate(script)
    assert not result.ok
    assert any("async with ctx.phase" in e for e in result.errors)


def test_validate_allows_with_ctx_phase() -> None:
    """with ctx.phase() is now valid and should pass validation."""
    script = (
        'META = {"name": "good", "description": "d"}\n'
        "async def workflow(ctx):\n"
        "    with ctx.phase('work'):\n"
        "        r = await ctx.agent('do stuff')\n"
        "    return r\n"
    )
    result = validate(script)
    assert result.ok, f"unexpected errors: {result.errors}"


def test_validate_allows_bare_ctx_phase() -> None:
    """The original bare-call pattern still works."""
    script = (
        'META = {"name": "good", "description": "d"}\n'
        "async def workflow(ctx):\n"
        "    ctx.phase('work')\n"
        "    r = await ctx.agent('do stuff')\n"
        "    return r\n"
    )
    result = validate(script)
    assert result.ok, f"unexpected errors: {result.errors}"


# --------------------------------------------------------------------------- #
# Layer 3: e2e smoke — author + run a hello-world workflow
# --------------------------------------------------------------------------- #


HELLO_WORLD_SCRIPT = (
    'META = {"name": "hello-world", "description": "minimal e2e"}\n'
    "async def workflow(ctx):\n"
    "    with ctx.phase('greet'):\n"
    "        greeting = await ctx.agent('say hello')\n"
    "    return {'greeting': greeting}\n"
)


async def test_hello_world_e2e_through_runner() -> None:
    """Full e2e: validate + exec a hello-world workflow with ctx.phase CM."""
    # Validate first
    vr = validate(HELLO_WORLD_SCRIPT)
    assert vr.ok, f"validation failed: {vr.errors}"

    # Run through the runner
    res = await _runner().run(HELLO_WORLD_SCRIPT, run_id="hello_e2e", now=NOW)
    assert res.ok, f"run failed: {res.error}"
    assert res.result == {"greeting": "echo:say hello"}
    # Confirm the event stream is well-formed
    assert res.events[0].type == "run_started"
    assert res.events[-1].type == "run_finished"
    # Phase event present
    phase_events = [e for e in res.events if e.type == "phase_started"]
    assert len(phase_events) == 1
    assert phase_events[0].data["title"] == "greet"
