"""The hosted private-namespace E2E lane's time budget must add up.

The lane's pytest `--timeout` is a backstop. The test module owns the real budget,
and these tests pin the relationship between the two so neither can drift into the
state where the backstop fires first and reports nothing about which workflow hung.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"
MODULE = ROOT / "test" / "e2e" / "test_private_workflow_memory.py"
JOB = "e2e-private-namespace"


def _job():
    return yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"][JOB]


def _pytest_timeout():
    """The `--timeout=N` the lane actually runs the module under."""
    runs = [step["run"] for step in _job()["steps"] if "run" in step]
    commands = [run for run in runs if "test_private_workflow_memory.py" in run]
    assert len(commands) == 1, commands
    match = re.search(r"--timeout=(\d+)", commands[0])
    assert match, commands[0]
    return int(match.group(1))


def _tree():
    return ast.parse(MODULE.read_text(encoding="utf-8"))


def _constants():
    found = {}
    for node in _tree().body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found[target.id] = node.value.value
    return found


def _wait_call_count():
    return sum(
        1
        for node in ast.walk(_tree())
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_finished"
    )


def test_the_module_budget_sits_under_the_lane_backstop():
    constants = _constants()
    budget = constants["_TEST_BUDGET_SECONDS"]
    per_wait = constants["_PER_WAIT_SECONDS"]
    backstop = _pytest_timeout()
    job_cap = _job()["timeout-minutes"] * 60

    # A single wait may never outlast the budget that bounds all of them.
    assert per_wait <= budget
    # The module must run out of time first, so its own diagnostic is what fails.
    assert budget < backstop
    # The backstop is still a backstop: the job must outlast it.
    assert backstop < job_cap


def test_per_wait_budgets_alone_overrun_the_backstop():
    """The shared budget is load-bearing: per-wait caps alone do not fit."""
    constants = _constants()
    waits = _wait_call_count()

    assert waits > 1, waits
    # Nine waits at the per-wait cap ask for far more than the lane grants, which
    # is why a budget spanning the whole test exists rather than per-wait caps only.
    assert waits * constants["_PER_WAIT_SECONDS"] > _pytest_timeout()


def test_every_wait_is_bounded_by_the_shared_budget():
    """No wait may compute its own deadline straight from the clock."""
    source = MODULE.read_text(encoding="utf-8")
    helper = source.split("def _wait_deadline()", 1)[1].split("\ndef ", 1)[0]

    assert "_test_deadline" in helper
    assert "_PER_WAIT_SECONDS" in helper
    # `_finished` takes its deadline from the helper, never from a bare clock read.
    finished = source.split("def _finished(", 1)[1].split("\ndef ", 1)[0]
    assert "_wait_deadline()" in finished
    assert "time.monotonic() + " not in finished
