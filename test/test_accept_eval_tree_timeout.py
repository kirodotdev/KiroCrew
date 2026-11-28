"""The acceptance evaluator's per-check timeout ends the check's whole process tree.

``_exec`` runs every check through ``platform_compat.run_killing_tree_on_timeout``,
whose Windows arm kills a launcher's pipe-holding descendant so the ``error``
verdict arrives at ``TIMEOUT_SECS``. Run as a bare file with no ``kiro_crew``
importable, the script falls back to ``subprocess.run``. Both bundled copies of
the script are pinned. No real process is started: each spawn is a stub.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from skill_script_helpers import load_skill_script

from kiro_crew import platform_compat
from kiro_crew.skills import _BUILTIN_SKILLS_DIR

SCRIPTS = [
    Path(_BUILTIN_SKILLS_DIR) / skill / "scripts" / "accept_eval.py"
    for skill in ("goal-conductor", "goal-ledger-conductor")
]


def _load(script: Path):
    return load_skill_script(f"_accept_eval_tree_timeout_{script.parent.parent.name}", script)


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.parent.parent.name)
def test_a_timeout_runs_through_the_tree_killing_helper(script, monkeypatch):
    mod = _load(script)
    seen = []

    def _tree_run(args, **kwargs):
        seen.append((list(args), kwargs))
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(platform_compat, "run_killing_tree_on_timeout", _tree_run)

    problem, proc = mod._exec(["gh", "pr", "checks", "1"])

    assert proc is None
    assert problem == ("error", f"timed out after {mod.TIMEOUT_SECS}s")
    assert [argv for argv, _ in seen] == [["gh", "pr", "checks", "1"]]
    kwargs = seen[0][1]
    assert kwargs["timeout"] == mod.TIMEOUT_SECS
    assert kwargs["capture_output"] is True


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.parent.parent.name)
def test_a_subprocess_run_stub_still_answers_on_the_windows_arm(script, monkeypatch):
    """The evaluator's tests stub ``subprocess.run``; on Windows the stub must still win."""
    mod = _load(script)
    seen = []

    def _fake_run(args, **kwargs):
        seen.append(list(args))
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(mod.subprocess, "run", _fake_run)

    problem, proc = mod._exec(["gh", "pr", "checks", "1"])

    assert proc is None
    assert problem == ("error", f"timed out after {mod.TIMEOUT_SECS}s")
    assert seen == [["gh", "pr", "checks", "1"]]


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.parent.parent.name)
def test_a_package_without_the_helper_falls_back_to_subprocess_run(script, monkeypatch):
    mod = _load(script)
    seen = []

    def _fake_run(args, **kwargs):
        seen.append(list(args))
        return subprocess.CompletedProcess(args, 0, "ok", "")

    monkeypatch.delattr(platform_compat, "run_killing_tree_on_timeout")
    monkeypatch.setattr(mod.subprocess, "run", _fake_run)

    problem, proc = mod._exec(["gh", "pr", "checks", "1"])

    assert problem is None
    assert seen == [["gh", "pr", "checks", "1"]]


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.parent.parent.name)
def test_without_the_package_it_falls_back_to_subprocess_run(script, monkeypatch):
    mod = _load(script)
    seen = []

    def _fake_run(args, **kwargs):
        seen.append(list(args))
        return subprocess.CompletedProcess(args, 0, "ok", "")

    def _tree_run(args, **kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("the package helper ran with kiro_crew unimportable")

    monkeypatch.setattr(platform_compat, "run_killing_tree_on_timeout", _tree_run)
    monkeypatch.setattr(mod.subprocess, "run", _fake_run)
    monkeypatch.setitem(sys.modules, "kiro_crew", None)

    problem, proc = mod._exec(["gh", "pr", "checks", "1"])

    assert problem is None
    assert proc.stdout == "ok"
    assert seen == [["gh", "pr", "checks", "1"]]
