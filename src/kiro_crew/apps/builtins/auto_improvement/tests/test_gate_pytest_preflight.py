"""A gate interpreter without pytest is a preflight failure, not an invalid agent test.

Every gate step runs ``sys.executable -m pytest``. The packaged app's bundled interpreter
has no pytest, so without a preflight each candidate fails T2 "reproducing test does not
collect" while no test runs. The run must refuse up front and say why.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.auto_improvement.backend import deps
from kiro_crew.apps.builtins.auto_improvement.backend import runner as R

_IMPORT_PATH_OVERRIDES = ("PYTHONPATH", "PYTHONHOME")


@pytest.fixture(scope="module")
def python_without_pytest(tmp_path_factory: pytest.TempPathFactory) -> str:
    """A real interpreter that cannot import pytest: a fresh venv with nothing in it.

    Probed without the inherited import-path overrides, as every test using it runs.
    """
    venv = tmp_path_factory.mktemp("bare") / "venv"
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True, timeout=120
    )
    exe = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    env = {k: v for k, v in os.environ.items() if k not in _IMPORT_PATH_OVERRIDES}
    probe = subprocess.run(
        [str(exe), "-c", "import pytest"], capture_output=True, timeout=60, env=env
    )
    assert probe.returncode != 0, "a bare venv must not be able to import pytest"
    return str(exe)


@pytest.fixture
def gate_without_pytest(python_without_pytest: str, monkeypatch: pytest.MonkeyPatch) -> str:
    """Make the bare venv the gate interpreter, with no import-path override to leak pytest."""
    for name in _IMPORT_PATH_OVERRIDES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(sys, "executable", python_without_pytest)
    return python_without_pytest


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.apps.builtins.auto_improvement.backend import store

    (tmp_path / "data").mkdir()
    (tmp_path / "scratch").mkdir()
    _floor_monkeypatch.setattr(store, "data_dir", lambda: tmp_path / "data")
    _floor_monkeypatch.setattr(store, "scratch_dir", lambda: tmp_path / "scratch")


def test_the_probe_passes_with_an_interpreter_that_has_pytest() -> None:
    assert deps.gate_pytest_problem() == ""


def test_the_probe_gets_the_gates_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Host variables survive (a user-site pytest needs APPDATA/PYTHONUSERBASE),
    inherited pytest options are cleared, and credentials are dropped."""
    seen: dict[str, str] = {}

    def _fake_run(
        argv: list[str], *, env: dict[str, str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        seen.update(env)
        return subprocess.CompletedProcess(argv, 0, "pytest 9", "")

    monkeypatch.setenv("APPDATA", "appdata-for-user-site")
    monkeypatch.setenv("PYTEST_ADDOPTS", "-p no_such_plugin_for_this_probe")
    monkeypatch.setenv("GITHUB_TOKEN", "not-a-real-token")
    monkeypatch.setattr(deps.subprocess, "run", _fake_run)
    assert deps.gate_pytest_problem() == ""
    assert seen["APPDATA"] == "appdata-for-user-site"
    assert seen["PYTEST_ADDOPTS"] == ""
    assert "GITHUB_TOKEN" not in seen


def test_check_deps_blocks_when_the_gate_interpreter_has_no_pytest(
    gate_without_pytest: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(deps, "_gh_authenticated", lambda: (True, "authenticated"))
    report = deps.check_deps()
    assert "pytest" in report["blocking"]
    assert report["ok"] is False
    entry = next(d for d in report["deps"] if d["id"] == "pytest")
    assert entry["required"] is True
    assert "pytest is not installed" in entry["detail"]


def test_a_run_refuses_to_start_instead_of_failing_every_candidate(
    gate_without_pytest: str,
) -> None:
    supervisor = R.RunSupervisor()
    with pytest.raises(RuntimeError, match="pytest is not installed"):
        supervisor.start({})
    assert supervisor.status()["status"] == R.STATUS_IDLE


def test_calibration_refuses_too(gate_without_pytest: str) -> None:
    supervisor = R.RunSupervisor()
    with pytest.raises(RuntimeError, match="pytest is not installed"):
        supervisor.calibrate({})
    assert supervisor.status()["status"] == R.STATUS_IDLE


def test_a_stop_during_the_probe_cancels_the_calibration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disabling the app while the probe runs must not let calibration start after it."""
    supervisor = R.RunSupervisor()

    def _probe_then_stop() -> str:
        supervisor.stop()
        return ""

    monkeypatch.setattr(deps, "gate_pytest_problem", _probe_then_stop)
    with pytest.raises(RuntimeError, match="stopped before it started"):
        supervisor.calibrate({})
    assert supervisor.status()["status"] == R.STATUS_IDLE
