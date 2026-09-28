"""Scenario: the built wheel installs into a clean venv and the CLI runs.

The flow a new user performs: pip install, then run the thing. The failure this
catches is the one no in-tree test can see, because every other test imports
from ``src/``: a module, package-data file or entry point that exists in the
checkout and is missing from the built distribution. ``kirocrew doctor`` is the
second half -- it exercises the installed package's own self-checks, so a wheel
that imports but cannot resolve its data files fails here rather than on a
user's machine.

Bounded end to end, and entirely inside a scratch tree: the build writes to a
temporary directory and the install goes into a temporary venv, so neither the
checkout's ``build/`` nor the developer's environment is touched.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.timeout(1800)

BUILD_TIMEOUT = 900.0
INSTALL_TIMEOUT = 900.0
RUN_TIMEOUT = 300.0


def _run(argv: list[str], *, cwd: Path, timeout: float, env: dict[str, str] | None = None):
    return subprocess.run(
        argv,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
    )


def test_built_wheel_installs_and_the_cli_answers(
    pod, tmp_path: Path, skip_unless_required
) -> None:
    # Depends on `pod` so the scenario runs in the same suite session and its
    # preconditions (a provisioned checkout) are already established. It does not
    # touch the pod: this half of the release path is about the distribution.
    checkout = pod.checkout
    outdir = tmp_path / "dist"

    # Build from a COPY of the tracked tree, never from the checkout itself:
    # setuptools writes `build/` and `*.egg-info` next to `pyproject.toml`, and a
    # scenario run must leave the developer's worktree exactly as it found it.
    # `git archive` copies what a release build would see (tracked files only,
    # no `.venv`, no `node_modules`), which also keeps the copy small.
    source = tmp_path / "source"
    source.mkdir()
    archived = _run(
        [
            "git",
            "-C",
            str(checkout),
            "archive",
            "--format=tar",
            "-o",
            str(tmp_path / "src.tar"),
            "HEAD",
        ],
        cwd=checkout,
        timeout=RUN_TIMEOUT,
    )
    assert archived.returncode == 0, f"git archive failed: {archived.stderr}"
    with tarfile.open(tmp_path / "src.tar") as tar:
        tar.extractall(source, filter="data")
    # The built SPA is untracked but is what `pod up` serves; the wheel carries
    # it when present, so copy it beside the sources the same way a release does.
    dist_src = checkout / "src" / "kiro_crew" / "static" / "dist"
    if dist_src.is_dir():
        shutil.copytree(dist_src, source / "src" / "kiro_crew" / "static" / "dist")

    build = _run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(outdir)],
        cwd=source,
        timeout=BUILD_TIMEOUT,
    )
    if build.returncode != 0:
        if "No module named build" in (build.stderr or ""):
            skip_unless_required("`python -m build` is not installed in this interpreter")
        pytest.fail(
            f"`python -m build --wheel` failed (exit {build.returncode}).\n"
            f"stdout tail: {(build.stdout or '')[-3000:]}\n"
            f"stderr tail: {(build.stderr or '')[-3000:]}"
        )

    wheels = sorted(outdir.glob("*.whl"))
    assert (
        len(wheels) == 1
    ), f"expected exactly one wheel in {outdir}, got {[w.name for w in wheels]}"
    wheel = wheels[0]

    venv_dir = tmp_path / "venv"
    made = _run([sys.executable, "-m", "venv", str(venv_dir)], cwd=tmp_path, timeout=RUN_TIMEOUT)
    assert made.returncode == 0, f"venv creation failed: {made.stderr}"

    bin_dir = venv_dir / ("Scripts" if os.name == "nt" else "bin")
    py = bin_dir / ("python.exe" if os.name == "nt" else "python")
    cli = bin_dir / ("kirocrew.exe" if os.name == "nt" else "kirocrew")

    install = _run(
        [str(py), "-m", "pip", "install", "--no-input", str(wheel)],
        cwd=tmp_path,
        timeout=INSTALL_TIMEOUT,
    )
    if install.returncode != 0:
        pytest.fail(
            f"installing {wheel.name} into a clean venv failed (exit {install.returncode}).\n"
            f"stderr tail: {(install.stderr or '')[-3000:]}"
        )
    assert cli.is_file(), f"the wheel installed no `kirocrew` entry point in {bin_dir}"

    # Point the installed CLI at its own throwaway data home, so neither command
    # can read or write the developer's real ~/.kiro/crew.
    env = dict(os.environ)
    env["KIROCREW_HOME"] = str(tmp_path / "home")
    env.pop("KIROCREW_KIRO_BIN", None)

    version = _run([str(cli), "--version"], cwd=tmp_path, timeout=RUN_TIMEOUT, env=env)
    assert version.returncode == 0, (
        f"`kirocrew --version` from the installed wheel failed (exit {version.returncode}).\n"
        f"stderr: {version.stderr}"
    )
    assert version.stdout.strip(), "`kirocrew --version` printed nothing"

    # `doctor` reports problems by exit code, and a fresh throwaway home has
    # plenty of them (no config, no credentials), so the assertion is that it RAN
    # to a verdict rather than that the verdict was clean. A crash -- an import
    # error, a missing data file, a traceback -- is what this catches.
    doctor = _run([str(cli), "doctor"], cwd=tmp_path, timeout=RUN_TIMEOUT, env=env)
    combined = (doctor.stdout or "") + (doctor.stderr or "")
    assert (
        "Traceback (most recent call last)" not in combined
    ), f"`kirocrew doctor` from the installed wheel raised.\n{combined[-3000:]}"
    assert combined.strip(), "`kirocrew doctor` produced no output at all"
