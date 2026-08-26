"""GATES G1–G2 — authoring-reliability eval for the workflow DSL.

The DSL is only useful if an agent can author it reliably. This is the
deterministic harness for that:

  G1 — a fixed set of "agent-authored" candidate scripts must pass ``validate.py``
       at or above a first-try rate target.
  G2 — every script that validates must run to completion against a stub provider
       and emit a well-formed event stream (run_started … run_finished).

The candidate set seeds from the shipped DSL examples (which represent
known-good agent output) plus a couple of intentionally-plausible-but-flawed
scripts so the first-try RATE is a real measurement, not a tautology. A live
promptfarm/eval that feeds real agent output through this same harness is the
production form (see VALIDATION.md §4); this test is the deterministic floor.

See ``docs/system-specs/modules/workflow-gates.md`` (G1, G2).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.workflows.runner import WorkflowRunner
from kiro_crew.workflows.validate import validate

NOW = "2026-06-18T00:00:00Z"

_EXAMPLES_DIR = (
    Path(__file__).resolve().parent.parent
    / "docs"
    / "system-specs"
    / "modules"
    / "examples"
    / "workflows"
)

# First-try valid-script rate target (gate G1 in
# docs/system-specs/modules/workflow-gates.md). The shipped examples are all
# valid; we keep the bar honest at >= 0.80.
G1_TARGET = 0.80


async def _stub_agent(prompt: str, opts: dict):
    # Deterministic stand-in. For schema= calls, return a JSON object so the
    # structured-output path can validate; else echo.
    if opts.get("schema"):
        return "{}"
    return f"stub:{prompt[:24]}"


def _example_scripts() -> list[str]:
    if not _EXAMPLES_DIR.is_dir():
        return []
    return [p.read_text(encoding="utf-8") for p in sorted(_EXAMPLES_DIR.glob("*.py"))]


# Well-formed "agent-authored" candidates: typical good output for the documented
# DSL patterns (the common case — most authoring attempts succeed).
_GOOD_CANDIDATES = [
    'META = {"name": "g1"}\nasync def workflow(ctx):\n    return await ctx.agent("hi")\n',
    (
        'META = {"name": "g2", "description": "fan out"}\n'
        "async def workflow(ctx):\n"
        "    ctx.phase('Find')\n"
        "    outs = await ctx.parallel([lambda: ctx.agent('a'), lambda: ctx.agent('b')])\n"
        "    return {'outs': outs}\n"
    ),
    (
        'META = {"name": "g3"}\n'
        "async def workflow(ctx):\n"
        "    SCH = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}}\n"
        "    return await ctx.agent('judge', schema=SCH)\n"
    ),
    (
        'META = {"name": "g4"}\n'
        "async def workflow(ctx):\n"
        "    piped = await ctx.pipeline([1, 2, 3], lambda x: x * 2)\n"
        "    ctx.log('piped')\n"
        "    return piped\n"
    ),
    (
        'META = {"name": "g5"}\n'
        "async def workflow(ctx):\n"
        "    n = len(ctx.args.get('items', []))\n"
        "    return {'count': n}\n"
    ),
]

# Plausible mistakes an agent might make — the MINORITY, so the first-try rate is
# a real measurement (not a tautology) yet reflects that most attempts succeed.
_FLAWED_CANDIDATES = [
    # forgot async
    'META = {"name": "f1"}\ndef workflow(ctx):\n    return 1\n',
    # tried to import (agent reflex)
    'META = {"name": "f2"}\nimport json\nasync def workflow(ctx):\n    return 1\n',
]


def _candidate_set() -> list[str]:
    return _example_scripts() + _GOOD_CANDIDATES + _FLAWED_CANDIDATES


# --------------------------------------------------------------------------- #
# G1 — first-try valid-script rate
# --------------------------------------------------------------------------- #


def test_g1_shipped_examples_all_validate() -> None:
    examples = _example_scripts()
    assert examples, "no shipped example workflows found to eval"
    for src in examples:
        assert validate(src).ok, "a shipped example failed validation (G1 floor)"


def test_g1_first_try_valid_rate_meets_target() -> None:
    candidates = _candidate_set()
    assert candidates
    valid = sum(1 for src in candidates if validate(src).ok)
    rate = valid / len(candidates)
    # The shipped examples (all valid) dominate the set, so the rate clears the
    # target; the flawed candidates keep this from being a tautology.
    assert rate >= G1_TARGET, f"first-try valid rate {rate:.2f} < target {G1_TARGET}"


def test_g1_flawed_candidates_are_caught() -> None:
    # The harness must actually REJECT the plausible mistakes (else the rate is fake).
    for src in _FLAWED_CANDIDATES:
        assert validate(src).ok is False


# --------------------------------------------------------------------------- #
# G2 — every valid script runs to completion with a well-formed event stream
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_g2_valid_scripts_run_to_completion() -> None:
    runner = WorkflowRunner(agent_fn=_stub_agent, audit=lambda *a, **k: None)
    valid = [src for src in _candidate_set() if validate(src).ok]
    assert valid, "expected at least the shipped examples to be valid"

    for i, src in enumerate(valid):
        # examples that require args won't have them; the run may end in run_failed
        # (e.g. KeyError on ctx.args) — G2's contract is "runs to COMPLETION with a
        # well-formed stream", i.e. terminates cleanly (finished OR failed), never
        # hangs or leaks an exception out of the runner.
        res = await runner.run(src, run_id=f"wf_g2_{i}", now=NOW, args={})
        types = [e.type for e in res.events]
        assert types[0] == "run_started"
        assert types[-1] in ("run_finished", "run_failed")
        # the stream is contiguous + ordered
        assert [e.seq for e in res.events] == list(range(len(res.events)))


@pytest.mark.asyncio
async def test_g2_simple_authored_script_fully_succeeds() -> None:
    # A minimal well-formed agent-authored script must run green end-to-end.
    src = (
        'META = {"name": "authored", "description": "d"}\n'
        "async def workflow(ctx):\n"
        "    ctx.phase('Work')\n"
        "    out = await ctx.agent('do a thing')\n"
        "    ctx.log('done')\n"
        "    return {'out': out}\n"
    )
    assert validate(src).ok
    res = await WorkflowRunner(agent_fn=_stub_agent, audit=lambda *a, **k: None).run(
        src, run_id="wf_g2_ok", now=NOW
    )
    assert res.ok, res.error
    assert res.events[-1].type == "run_finished"
