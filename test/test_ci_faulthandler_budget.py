"""The CI faulthandler dump fires only after a test's own pytest-timeout."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "ci_faulthandler_budget_test_helper", ROOT / "scripts/ci_faulthandler_budget.py"
)
PLUGIN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PLUGIN)


class _Config:
    _env_timeout = 120.0
    _env_timeout_method = "signal"
    _env_timeout_func_only = False
    _env_timeout_disable_debugger_detection = False

    def __init__(self, grace: float, armed: bool) -> None:
        self.stash = pytest.Stash()
        self._grace = grace
        if armed:
            self.stash[PLUGIN._FD_KEY] = 2

    def getoption(self, name: str) -> float:
        assert name == "faulthandler_grace"
        return self._grace


class _Item:
    def __init__(self, mark=None, *, grace: float = 30.0, armed: bool = True) -> None:
        self.config = _Config(grace, armed)
        self._mark = mark

    def get_closest_marker(self, name: str):
        assert name == "timeout"
        return self._mark


@pytest.mark.parametrize(
    ("mark", "func_only", "expected"),
    [
        (None, False, 150.0),
        (pytest.mark.timeout(900).mark, False, 930.0),
        (pytest.mark.timeout(900, func_only=True).mark, True, 930.0),
        (pytest.mark.timeout(900, func_only=True).mark, False, None),
        (None, True, None),
        (pytest.mark.timeout(0).mark, False, None),
    ],
    ids=["default", "long-mark", "func-only-call", "func-only-protocol", "call-span", "no-timeout"],
)
def test_the_dump_lands_after_the_spans_own_timeout(mark, func_only, expected) -> None:
    assert PLUGIN.dump_after(_Item(mark), func_only) == expected


def test_nothing_is_armed_when_the_grace_is_off() -> None:
    assert PLUGIN.dump_after(_Item(armed=False, grace=0.0), False) is None


def test_no_workflow_arms_a_fixed_faulthandler_deadline() -> None:
    # One deadline for every test dumps a test still inside a longer budget, and
    # that dump can segfault the worker running it.
    assignment = re.compile(r"faulthandler_timeout\s*=")
    offenders = [
        path.name
        for path in sorted((ROOT / ".github" / "workflows").glob("*.yml"))
        if assignment.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
