"""``make backend PY=...`` must build from the interpreter the caller named.

``ensure-python.sh`` records the interpreter it settles on in
``<data-home>/python-bin``, and the ``backend`` recipe reads that marker before
it falls back to ``$(PY)``. A ``PY=`` on the make command line is an explicit
choice, though, and the recipe's own error text tells users to run
``make backend PY=python3.12``. If the marker kept winning, that advice would be
a silent no-op on every host where the marker names an unusable interpreter.

The tests run the real Makefile from a scratch directory, so its relative
``bash ensure-python.sh`` reaches a stub that records nothing. Both candidate
interpreters are stubs that log their own name and fail the version probe; the
recipe then stops at its ">= 3.12" gate before any venv or pip work, and its
error line names the interpreter it chose.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.skipif(
        os.name == "nt",
        reason="the Makefile backend recipe is POSIX sh; Windows builds through make.ps1",
    ),
    pytest.mark.skipif(shutil.which("make") is None, reason="GNU make is not installed"),
]

MAKEFILE = Path(__file__).parent.parent / "Makefile"


def _stub_python(path: Path, log: Path) -> Path:
    """A fake interpreter that records that it ran and fails every probe."""
    path.write_text(f'#!/bin/sh\necho "{path.name}" >> "{log}"\nexit 1\n', encoding="utf-8")
    path.chmod(0o755)
    return path


def _run_backend(
    tmp_path: Path, *make_args: str, env_py: str | None = None
) -> tuple[subprocess.CompletedProcess[str], str]:
    """Run ``make backend`` with a marker naming ``marker-python``; return (proc, log)."""
    work = tmp_path / "work"
    work.mkdir()
    (work / "ensure-python.sh").write_text("exit 0\n", encoding="utf-8")

    log = tmp_path / "invoked.log"
    log.touch()
    marker_py = _stub_python(tmp_path / "marker-python", log)

    home = tmp_path / "home"
    home.mkdir()
    (home / "python-bin").write_text(f"{marker_py}\n", encoding="utf-8")

    env = dict(os.environ)
    env["KIROCREW_HOME"] = str(home)
    env.pop("PY", None)
    if env_py is not None:
        env["PY"] = env_py
    env.pop("MAKEFLAGS", None)
    env.pop("MFLAGS", None)

    proc = subprocess.run(
        [
            "make",
            "--no-print-directory",
            "-f",
            str(MAKEFILE),
            "-C",
            str(work),
            "backend",
            *make_args,
        ],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    return proc, log.read_text(encoding="utf-8")


def test_a_command_line_py_wins_over_the_python_bin_marker(tmp_path):
    cli_py = _stub_python(tmp_path / "cli-python", tmp_path / "invoked.log")

    proc, invoked = _run_backend(tmp_path, f"PY={cli_py}")

    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert f"'{cli_py}' is not Python >= 3.12" in proc.stderr, proc.stderr
    assert "cli-python" in invoked
    assert "marker-python" not in invoked, (
        "the recipe probed the python-bin marker's interpreter although PY= was "
        f"given on the command line; probes: {invoked!r}"
    )


def test_without_a_command_line_py_the_marker_is_used(tmp_path):
    """The control: the marker still decides when the caller names nothing."""
    proc, invoked = _run_backend(tmp_path)

    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert f"'{tmp_path / 'marker-python'}' is not Python >= 3.12" in proc.stderr, proc.stderr
    assert invoked.split() == ["marker-python"]


def test_an_empty_command_line_py_still_reads_the_marker(tmp_path):
    """``make backend PY=`` names nothing, like make.ps1's empty ``-Py``."""
    proc, invoked = _run_backend(tmp_path, "PY=")

    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert invoked.split() == ["marker-python"], invoked


def test_an_exported_py_does_not_override_the_marker(tmp_path):
    """``PY`` is a generic name another tool may have exported; only ``make PY=`` counts."""
    env_py = _stub_python(tmp_path / "env-python", tmp_path / "invoked.log")

    proc, invoked = _run_backend(tmp_path, env_py=str(env_py))

    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert invoked.split() == ["marker-python"], invoked
