"""``python -m ...spine`` entry point.

The module body is three lines that nothing else in the suite reaches, and they carry
the one contract the documented invocation depends on: the driver's return code must
become the process exit status (a spine run that fails has to fail the shell that
started it), and the arguments after the module name must reach ``driver.main``
unchanged. Driven through ``runpy`` with the real driver stubbed, so no pipeline runs.
"""

from __future__ import annotations

import runpy
import sys

import pytest

from kiro_crew.apps.builtins.auto_improvement.spine import driver

_SPINE_PKG = "kiro_crew.apps.builtins.auto_improvement.spine"


def _run_entry_point(monkeypatch, rc: int, argv: list[str]) -> list[list[str]]:
    calls: list[list[str]] = []

    def _fake_main(passed: list[str]) -> int:
        calls.append(list(passed))
        return rc

    monkeypatch.setattr(driver, "main", _fake_main)
    monkeypatch.setattr(sys, "argv", ["spine", *argv])
    with pytest.raises(SystemExit) as exc:
        runpy.run_module(_SPINE_PKG, run_name="__main__")
    assert exc.value.code == rc
    return calls


def test_failing_driver_becomes_a_nonzero_exit_status(monkeypatch):
    """A spine run that fails must fail the shell that invoked it."""
    calls = _run_entry_point(monkeypatch, 3, ["--dry-run"])
    assert calls == [["--dry-run"]]


def test_successful_driver_exits_zero(monkeypatch):
    calls = _run_entry_point(monkeypatch, 0, ["--go"])
    assert calls == [["--go"]]


def test_arguments_are_forwarded_without_the_program_name(monkeypatch):
    calls = _run_entry_point(monkeypatch, 0, ["--dry-run", "-c", "budget.usd=1"])
    assert calls == [["--dry-run", "-c", "budget.usd=1"]]
