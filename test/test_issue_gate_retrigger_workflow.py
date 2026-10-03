"""Behavioural tests for .github/workflows/issue-gate-retrigger.yml.

When triage puts a verdict label on an issue, the workflow re-runs the failed
`Issue Gate` job of every open PR that declares the issue. These tests run the
step's script for real with `gh` replaced by a stub, and pin the label set to
the one `issue-gate.yml` accepts.

Skipped where bash or jq is missing, and on Windows (same guard as
test_issue_summary_workflow.py).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "issue-gate-retrigger.yml"
GATE = ROOT / ".github" / "workflows" / "issue-gate.yml"

REPO = "kirodotdev/KiroCrew"

GH_STUB = r"""#!/usr/bin/env bash
set -euo pipefail
if [ "$1" = "run" ] && [ "$2" = "rerun" ]; then
  echo "$3 $4" >> "$FIXTURES/reruns.txt"
  if [ "${RERUN_FAIL:-}" = "$3" ]; then exit 1; fi
  exit 0
fi
if [ "$1" = "api" ]; then
  ENDPOINT="$2"
  PROGRAM="."
  while [ $# -gt 0 ]; do
    if [ "$1" = "--jq" ]; then PROGRAM="$2"; fi
    shift
  done
  case "$ENDPOINT" in
    graphql) jq -r "$PROGRAM" "$FIXTURES/timeline.json"; exit 0 ;;
    *runs\?head_sha=*)
      sha="${ENDPOINT#*head_sha=}"; sha="${sha%%&*}"
      f="$FIXTURES/runs-$sha.json"
      if [ ! -f "$f" ]; then echo '{"workflow_runs": []}' > "$f"; fi
      jq -r "$PROGRAM" "$f"; exit 0 ;;
  esac
fi
echo "gh stub: unhandled: $*" >&2
exit 90
"""

pytestmark = pytest.mark.skipif(
    not WORKFLOW.exists()
    or os.name == "nt"
    or shutil.which("bash") is None
    or shutil.which("jq") is None,
    reason="requires the workflow file plus a POSIX bash and jq",
)


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def _pr(number: int, sha: str, state: str = "OPEN", repo: str = REPO) -> dict:
    return {
        "source": {
            "number": number,
            "state": state,
            "headRefOid": sha,
            "repository": {"nameWithOwner": repo},
        }
    }


def _run(tmp_path: Path, nodes: list[dict], runs: dict[str, list[dict]], **env: str):
    fixtures = tmp_path / "fixtures"
    bindir = tmp_path / "bin"
    fixtures.mkdir()
    bindir.mkdir()
    stub = bindir / "gh"
    stub.write_text(GH_STUB)
    stub.chmod(0o755)
    (fixtures / "timeline.json").write_text(
        json.dumps({"data": {"repository": {"issue": {"timelineItems": {"nodes": nodes}}}}})
    )
    for sha, items in runs.items():
        (fixtures / f"runs-{sha}.json").write_text(json.dumps({"workflow_runs": items}))
    script = _workflow()["jobs"]["retrigger"]["steps"][0]["run"]
    proc = subprocess.run(  # noqa: S603 - fixed argv, test-local stub
        ["bash", "-c", script],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "FIXTURES": str(fixtures),
            "GH_TOKEN": "stub",
            "REPO": REPO,
            "ISSUE": "4100",
            "MAX_PRS": "20",
            **env,
        },
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    reruns_file = fixtures / "reruns.txt"
    reruns = reruns_file.read_text().split() if reruns_file.exists() else []
    return proc, [r for r in reruns if r != "--failed"]


def _failed(run_id: int) -> dict:
    return {"id": run_id, "status": "completed", "conclusion": "failure"}


def test_verdict_labels_match_the_gate() -> None:
    gate_env = yaml.safe_load(GATE.read_text())["jobs"]["issue-gate"]["steps"][1]["env"]
    verdicts = set(gate_env["TRIAGE_VERDICT_LABELS"].split())
    condition = _workflow()["jobs"]["retrigger"]["if"]
    listed = set(json.loads(condition.split("fromJSON('", 1)[1].split("')", 1)[0]))
    assert listed == verdicts


def test_triggers_only_on_issue_labeled() -> None:
    # PyYAML reads the bare `on:` key as boolean True.
    on = _workflow()[True]
    assert on == {"issues": {"types": ["labeled"]}}


def test_reruns_the_failed_gate_of_an_open_pr(tmp_path: Path) -> None:
    proc, reruns = _run(tmp_path, [_pr(7, "aaa")], {"aaa": [_failed(111)]})
    assert proc.returncode == 0, proc.stderr
    assert reruns == ["111"]


def test_rerun_uses_failed_jobs_only(tmp_path: Path) -> None:
    _run(tmp_path, [_pr(7, "aaa")], {"aaa": [_failed(111)]})
    assert (tmp_path / "fixtures" / "reruns.txt").read_text().split() == ["111", "--failed"]


def test_leaves_green_running_and_missing_gates_alone(tmp_path: Path) -> None:
    nodes = [_pr(1, "s1"), _pr(2, "s2"), _pr(3, "s3")]
    runs = {
        "s1": [{"id": 1, "status": "completed", "conclusion": "success"}],
        "s2": [{"id": 2, "status": "in_progress", "conclusion": None}],
    }
    proc, reruns = _run(tmp_path, nodes, runs)
    assert proc.returncode == 0, proc.stderr
    assert reruns == []


def test_ignores_closed_prs_other_repos_and_issue_sources(tmp_path: Path) -> None:
    nodes = [
        _pr(1, "s1", state="CLOSED"),
        _pr(2, "s2", state="MERGED"),
        _pr(3, "s3", repo="someone/fork"),
        {"source": {}},  # an issue, not a PR, cross-referenced it
    ]
    runs = {sha: [_failed(i)] for i, sha in enumerate(("s1", "s2", "s3"), start=1)}
    proc, reruns = _run(tmp_path, nodes, runs)
    assert proc.returncode == 0, proc.stderr
    assert reruns == []
    assert "nothing to re-run" in proc.stdout


def test_duplicate_references_rerun_once(tmp_path: Path) -> None:
    proc, reruns = _run(tmp_path, [_pr(7, "aaa"), _pr(7, "aaa")], {"aaa": [_failed(111)]})
    assert proc.returncode == 0, proc.stderr
    assert reruns == ["111"]


def test_caps_the_number_of_prs(tmp_path: Path) -> None:
    nodes = [_pr(i, f"s{i}") for i in range(1, 6)]
    runs = {f"s{i}": [_failed(100 + i)] for i in range(1, 6)}
    proc, reruns = _run(tmp_path, nodes, runs, MAX_PRS="2")
    assert proc.returncode == 0, proc.stderr
    assert len(reruns) == 2
    assert "re-running only the first 2" in proc.stdout


def test_one_failed_rerun_does_not_stop_the_others(tmp_path: Path) -> None:
    nodes = [_pr(1, "s1"), _pr(2, "s2")]
    runs = {"s1": [_failed(101)], "s2": [_failed(102)]}
    proc, reruns = _run(tmp_path, nodes, runs, RERUN_FAIL="101")
    assert proc.returncode == 1
    assert reruns == ["101", "102"]
