"""``build-windows-installer`` runs on a PR only when a Windows-packaging input changed.

The job is hosted-only (its installer test needs an interactive desktop), so it
runs on the shared GitHub-hosted Windows pool. It is gated the same way the
macos-15 desktop leg is: ``desktop-matrix``'s paths filter emits a
``windows_installer`` output, and the job's ``if:`` reads it.

Pinned here:

* the job still consumes the wheel and now waits on the resolver;
* the resolver's script says ``true`` for every event except a pull request
  that changed no Windows-packaging input (executed, not string-matched);
* every repository path the job's own steps name is covered by the filter, so
  editing what the job runs cannot skip the job that runs it.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]
BUILD_WORKFLOW = ROOT / ".github" / "workflows" / "build.yml"
_JOB = "build-windows-installer"


@pytest.fixture(scope="module")
def jobs() -> dict:
    return yaml.safe_load(BUILD_WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def _filter_paths(jobs: dict) -> list[str]:
    step = next(s for s in jobs["desktop-matrix"]["steps"] if s.get("id") == "filter")
    return yaml.safe_load(step["with"]["filters"])["windows_installer"]


def _covered(path: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        if pattern.endswith("/**"):
            # A directory the job works in counts as the whole tree under it.
            if path == pattern[:-3] or path.startswith(pattern[:-2]):
                return True
        elif path == pattern:
            return True
    return False


def _dot_sourced_helpers(script: str) -> set[str]:
    """Repo paths ``script`` dot-sources via ``. (Join-Path $PSScriptRoot "a" "b" ...)``."""
    text = (ROOT / script).read_text(encoding="utf-8")
    base = (ROOT / script).parent
    helpers: set[str] = set()
    dot_source = r'^\s*\.\s*\(Join-Path\s+\$PSScriptRoot\s+((?:"[^"]*"\s*)+)\)'
    for args in re.findall(dot_source, text, re.M):
        target = base.joinpath(*re.findall(r'"([^"]*)"', args)).resolve()
        helpers.add(target.relative_to(ROOT).as_posix())
    return helpers


def test_job_waits_on_the_resolver_and_reads_its_output(jobs: dict) -> None:
    job = jobs[_JOB]
    assert set(job["needs"]) == {"build-wheel", "desktop-matrix"}
    assert job["if"] == "needs.desktop-matrix.outputs.windows_installer == 'true'"
    outputs = jobs["desktop-matrix"]["outputs"]
    assert outputs["windows_installer"] == "${{ steps.compute.outputs.windows_installer }}"
    compute = next(s for s in jobs["desktop-matrix"]["steps"] if s.get("id") == "compute")
    assert compute["env"]["WINDOWS_CHANGED"] == "${{ steps.filter.outputs.windows_installer }}"


def test_no_workflow_level_paths_filter(jobs: dict) -> None:
    """A bare ``paths:`` would skip the whole run and leave Build unreported."""
    triggers = yaml.safe_load(BUILD_WORKFLOW.read_text(encoding="utf-8"))[True]
    for event in ("push", "pull_request", "merge_group"):
        body = triggers.get(event) or {}
        assert "paths" not in body and "paths-ignore" not in body, event


@pytest.mark.parametrize(
    "event, queue_on, windows_changed, expected",
    [
        ("pull_request", "false", "false", "false"),
        ("pull_request", "false", "", "false"),
        ("pull_request", "false", "true", "true"),
        ("pull_request", "true", "true", "true"),
        ("merge_group", "false", "", "true"),
        ("merge_group", "true", "", "true"),
        ("push", "false", "", "true"),
        ("push", "true", "", "true"),
        ("workflow_dispatch", "false", "", "true"),
    ],
)
def test_compute_emits_windows_installer(
    jobs: dict, tmp_path: Path, event: str, queue_on: str, windows_changed: str, expected: str
) -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not available")
    compute = next(s for s in jobs["desktop-matrix"]["steps"] if s.get("id") == "compute")
    script = tmp_path / "compute.sh"
    # Bytes, not write_text: on Windows text mode turns \n into \r\n and bash rejects CRLF.
    script.write_bytes(("set -euo pipefail\n" + compute["run"]).encode("utf-8"))
    output = tmp_path / "out"
    output.write_text("", encoding="utf-8")
    env = dict(os.environ)
    env.update(
        EVENT=event,
        QUEUE_ON=queue_on,
        DESKTOP_CHANGED="false",
        WINDOWS_CHANGED=windows_changed,
        GITHUB_OUTPUT=str(output),
    )
    subprocess.run([bash, str(script)], env=env, check=True, timeout=30)
    values = re.findall(r"^windows_installer=(\w+)$", output.read_text(encoding="utf-8"), re.M)
    assert values == [expected]


def test_filter_covers_every_repo_path_the_job_names(jobs: dict) -> None:
    patterns = _filter_paths(jobs)
    job = jobs[_JOB]
    named: set[str] = set()
    for step in job["steps"]:
        text = " ".join(
            str(v) for v in (step.get("run"), step.get("working-directory"), step.get("with"))
        ).replace(
            "\\", "/"
        )  # pwsh steps spell paths with backslashes
        named.update(re.findall(r"(?:packaging|website|\.github)/[\w./-]+\w", text))
    # Ignore runtime outputs the job writes, not inputs it reads.
    named = {p for p in named if "/backend-dist" not in p and "/runtime-evidence" not in p}
    assert {
        "packaging/precompile_windows.py",
        ".github/scripts/test-windows-installer.ps1",
    } <= named
    named.update(_dot_sourced_helpers(".github/scripts/test-windows-installer.ps1"))
    assert "scripts/windows-process-tree.ps1" in named
    missing = sorted(p for p in named if not _covered(p, patterns))
    assert not missing, f"build-windows-installer reads paths its PR filter misses: {missing}"
    for required in (
        ".github/workflows/build.yml",
        "website/electron/**",
        "packaging/**",
        "pyproject.toml",
        "setup.cfg",
        "setup.py",
        "MANIFEST.in",
    ):
        assert required in patterns, required
