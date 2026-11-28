"""``scripts/ci/python_next_import_sweep.py`` reports what a new Python breaks.

The Python 3.14 lane reads its verdict from this script, so two things are
pinned on a throwaway package: a compile-time ``SyntaxWarning`` and a module that
fails to import each fail the sweep by name, and a directory without an
``__init__.py`` is compiled but never imported.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS
from scripts.ci import python_next_import_sweep as sweep

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ci" / "python_next_import_sweep.py"


def _package(root: Path) -> str:
    name = f"sweep_pkg_{uuid.uuid4().hex}"
    pkg = root / name
    (pkg / "sub").mkdir(parents=True)
    (pkg / "scripts").mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "ok.py").write_text("VALUE = 1\n", encoding="utf-8")
    (pkg / "sub" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "sub" / "broken.py").write_text(
        "import a_module_that_is_not_installed_anywhere\n", encoding="utf-8"
    )
    # Not an import path: importing it would raise, so the sweep must not try.
    (pkg / "scripts" / "tool.py").write_text("raise SystemExit(3)\n", encoding="utf-8")
    return name


def test_a_module_that_fails_to_import_is_named_and_a_script_dir_is_not_imported(
    tmp_path: Path,
) -> None:
    name = _package(tmp_path)
    # A child interpreter, so the throwaway package never enters this
    # worker's module cache.
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path / name), "--package", name],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
        cwd=tmp_path,
        timeout=FALLBACK_TIMEOUT_SECS,
        check=False,
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "import: 3 module(s) imported, 1 failure(s)" in result.stdout
    failures = [line for line in result.stdout.splitlines() if line.startswith("FAIL ")]
    assert len(failures) == 1
    assert failures[0].startswith(f"FAIL {name}.sub.broken: ModuleNotFoundError:")


def test_a_compile_warning_fails_the_sweep_with_its_file_and_line(tmp_path: Path) -> None:
    clean = tmp_path / "clean.py"
    clean.write_text("x = 1\n", encoding="utf-8")
    # An invalid escape is a SyntaxWarning on every supported Python.
    noisy = tmp_path / "noisy.py"
    noisy.write_text('x = 1\ny = "\\d"\n', encoding="utf-8")

    failures = sweep.compile_failures([tmp_path])

    assert len(failures) == 1
    assert failures[0].startswith(f"{noisy}:2: SyntaxWarning: ")


def test_the_lane_stays_out_of_readiness_and_never_runs_a_free_threaded_build() -> None:
    """Advisory means absent from both of pr-readiness.yml's lists; a 3.14t leg
    needs cp314t wheels for every compiled dependency, which this lane does not
    have yet."""
    workflows = Path(__file__).resolve().parents[1] / ".github" / "workflows"
    readiness = (workflows / "pr-readiness.yml").read_text(encoding="utf-8")
    assert "python-next" not in readiness
    assert "Python 3.14" not in readiness
    lane = (workflows / "python-next.yml").read_text(encoding="utf-8")
    assert 'PYTHON_NEXT: "3.14"' in lane
    assert "3.14t" not in lane.split("jobs:", 1)[1]
