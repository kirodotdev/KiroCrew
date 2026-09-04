"""GATE B10 — SEL audit log for every workflow run.

A run must record (via the SEL security event log): author, runner, args, each
agent call, and a result hash. The audit sink is injectable so tests capture
records without touching the real SEL log; the default sink writes to
``kiro_crew.sel``. Audit failures must never break a run.

See ``docs/system-specs/modules/workflows.md`` and GATES.md (B10).
"""

from __future__ import annotations

import pytest

from kiro_crew.workflows.runner import WorkflowRunner, _result_hash

pytestmark = pytest.mark.asyncio

NOW = "2026-06-18T00:00:00Z"


def _capturing_audit(sink: list):
    def audit(event_type: str, *, run_id: str, fields: dict) -> None:
        sink.append((event_type, run_id, fields))

    return audit


async def _echo(prompt: str, opts: dict):
    return f"echo:{prompt}"


GOOD = (
    'META = {"name": "demo"}\n'
    "async def workflow(ctx):\n"
    "    await ctx.agent('a')\n"
    "    await ctx.agent('b')\n"
    "    return {'done': True}\n"
)


async def test_run_emits_started_and_finished_audit_with_author() -> None:
    sink: list = []
    runner = WorkflowRunner(agent_fn=_echo, audit=_capturing_audit(sink))
    await runner.run(GOOD, run_id="wf_a", now=NOW, args={"k": 1}, author="a-contributor")

    types = [e[0] for e in sink]
    assert "run_started" in types
    assert "run_finished" in types

    started = next(f for t, _, f in sink if t == "run_started")
    assert started["author"] == "a-contributor"
    assert started["arg_keys"] == ["k"]  # arg KEYS only (not raw values)

    finished = next(f for t, _, f in sink if t == "run_finished")
    assert finished["author"] == "a-contributor"
    assert finished["outcome"] == "ok"
    assert finished["agent_calls"] == 2
    assert finished["result_hash"] == _result_hash({"done": True})


async def test_each_agent_call_is_audited() -> None:
    sink: list = []
    runner = WorkflowRunner(agent_fn=_echo, audit=_capturing_audit(sink))
    await runner.run(GOOD, run_id="wf_b", now=NOW, author="a-contributor")

    agent_events = [f for t, _, f in sink if t == "agent_call"]
    assert len(agent_events) == 2
    assert {e["call_index"] for e in agent_events} == {0, 1}
    assert all(e["outcome"] == "ok" for e in agent_events)
    assert all(e["runner"] for e in agent_events)


async def test_result_hash_never_leaks_raw_result() -> None:
    sink: list = []
    secret = (
        'META = {"name": "s"}\n'
        "async def workflow(ctx):\n"
        "    return {'secret': 'do-not-log-me'}\n"
    )
    await WorkflowRunner(agent_fn=_echo, audit=_capturing_audit(sink)).run(
        secret, run_id="wf_c", now=NOW
    )
    finished = next(f for t, _, f in sink if t == "run_finished")
    # only a hash is recorded, never the raw secret payload
    assert "do-not-log-me" not in str(finished)
    assert len(finished["result_hash"]) == 16


async def test_failed_agent_audited_as_failed() -> None:
    sink: list = []

    async def boom(prompt: str, opts: dict):
        raise RuntimeError("dead")

    script = (
        'META = {"name": "x"}\n'
        "async def workflow(ctx):\n"
        "    await ctx.agent('go')\n"
        "    return 1\n"
    )
    await WorkflowRunner(agent_fn=boom, audit=_capturing_audit(sink)).run(
        script, run_id="wf_d", now=NOW
    )
    agent_events = [f for t, _, f in sink if t == "agent_call"]
    assert agent_events and agent_events[0]["outcome"] == "failed"


async def test_audit_failure_never_breaks_run() -> None:
    def exploding_audit(event_type: str, *, run_id: str, fields: dict) -> None:
        raise RuntimeError("audit sink is down")

    # A broken audit sink must not fail the run. The runner wraps EVERY sink
    # (injected or default) in _guarded_audit, so even a sink that raises on every
    # call is tolerated: run an actual workflow through it and assert it succeeds.
    runner = WorkflowRunner(agent_fn=_echo, audit=exploding_audit)
    res = await runner.run(GOOD, run_id="wf_e", now=NOW)
    assert res.ok and res.result == {"done": True}

    # The default sink also swallows any internal error (sel() unavailable, etc.).
    from kiro_crew.workflows.runner import _default_audit

    _default_audit("run_started", run_id="x", fields={"runner": "x"})  # must not raise
    # result hash is stable + key-order-independent + short (16 hex chars)
    assert _result_hash({"a": 1, "b": 2}) == _result_hash({"b": 2, "a": 1})
    assert len(_result_hash({"a": 1})) == 16
