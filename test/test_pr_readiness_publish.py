"""Behavioural tests for the publish step of .github/workflows/pr-readiness.yml.

`PR Readiness` is the only required status check on protected branches, so the
commit status this step POSTs is the verdict the repository actually gates on.
The labels beside it are advisory decoration.

Two runs of this workflow can evaluate the SAME revision concurrently: every
`pull_request_target` run gets its own concurrency group keyed on `run_id`
(deliberately, so a superseded run never shows as cancelled), which is correct
for two DIFFERENT revisions and leaves two runs on one revision racing. When
that race is lost on a label call, the step must still publish the verdict --
otherwise a cosmetic 404 decides whether the required check reports anything.

These tests extract the step's shell and execute it for real against a `gh`
stub, so the ordering and the tolerance are verified rather than assumed.
Mirrors the harness in test_pr_readiness_sweep.py.

Skipped where the POSIX toolchain the script needs (bash, jq) is unavailable,
which is the case on the Windows leg of the matrix.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = (
    Path(__file__).resolve().parents[1] / ".github" / "workflows" / "pr-readiness.yml"
)

PUBLISH_STEP = "Publish status and label"
CLEANUP_STEP = "Remove readiness labels from closed pull request"

pytestmark = pytest.mark.skipif(
    not WORKFLOW.exists()
    or os.name == "nt"
    or shutil.which("bash") is None
    or shutil.which("jq") is None,
    reason="requires the workflow plus a POSIX bash and jq",
)

SHA = "4328fd0f941f09ff10f245fbdb4accf7c246febe"

# `gh` stub. Every call is RECORDED to $FIXTURES/calls.txt so ordering can be
# asserted, and any call whose recorded name appears in $FIXTURES/fail_* is made
# to fail the way the real API fails that race.
GH_STUB = r"""#!/usr/bin/env bash
set -uo pipefail
printf '%s\n' "$*" >> "$FIXTURES/calls.txt"

# gh pr view <PR> --repo <REPO> --json headRefOid --jq .headRefOid
if [ "$1 ${2:-}" = "pr view" ]; then
  cat "$FIXTURES/head_sha.txt"
  exit 0
fi

if [ "$1 ${2:-}" = "label list" ]; then
  cat "$FIXTURES/repo_labels.txt"
  exit 0
fi

if [ "$1 ${2:-}" = "label create" ]; then
  if [ -f "$FIXTURES/fail_label_create" ]; then
    echo 'HTTP 422: Validation Failed (https://api.github.com/repos/o/r/labels)' >&2
    echo 'Label already exists' >&2
    exit 1
  fi
  exit 0
fi

if [ "$1" = "api" ]; then
  # --method DELETE .../issues/N/labels/<encoded>
  if [ "${2:-}" = "--method" ] && [ "${3:-}" = "DELETE" ]; then
    if [ -f "$FIXTURES/fail_label_delete" ]; then
      echo 'gh: Label does not exist (HTTP 404)' >&2
      exit 1
    fi
    printf '%s\n' "$*" >> "$FIXTURES/deleted.txt"
    exit 0
  fi
  if [ "${2:-}" = "--method" ] && [ "${3:-}" = "POST" ]; then
    body="$(cat)"
    case "${4:-}" in
      *"/statuses/"*)
        if [ -f "$FIXTURES/fail_status" ]; then
          echo 'gh: Server Error (HTTP 500)' >&2
          exit 1
        fi
        printf '%s\n' "$body" > "$FIXTURES/published_status.json"
        exit 0
        ;;
      *"/labels")
        printf '%s\n' "$body" >> "$FIXTURES/added.txt"
        exit 0
        ;;
    esac
  fi
  case "${2:-}" in
    *"/issues/"*"/labels") cat "$FIXTURES/pr_labels.txt"; exit 0 ;;
  esac
fi
echo "gh stub: unhandled: $*" >&2
exit 90
"""


def _step(name: str) -> str:
    spec = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = spec["jobs"]["readiness"]["steps"]
    matches = [s["run"] for s in steps if s.get("name") == name and "run" in s]
    assert len(matches) == 1, f"expected exactly one {name!r} step, got {len(matches)}"
    return matches[0]


@pytest.fixture(scope="module")
def publish_script() -> str:
    return _step(PUBLISH_STEP)


@pytest.fixture(scope="module")
def cleanup_script() -> str:
    return _step(CLEANUP_STEP)


class Result:
    def __init__(self, proc: subprocess.CompletedProcess[str], fixtures: Path) -> None:
        self.proc = proc
        self._fixtures = fixtures

    @property
    def ok(self) -> bool:
        return self.proc.returncode == 0

    @property
    def published(self) -> dict[str, str] | None:
        path = self._fixtures / "published_status.json"
        if not path.exists():
            return None
        return json.loads(path.read_text())

    @property
    def deleted(self) -> list[str]:
        path = self._fixtures / "deleted.txt"
        return path.read_text().splitlines() if path.exists() else []

    @property
    def added(self) -> str:
        """The label-add request bodies, as one blob (jq writes them multi-line)."""
        path = self._fixtures / "added.txt"
        return path.read_text() if path.exists() else ""

    @property
    def calls(self) -> list[str]:
        path = self._fixtures / "calls.txt"
        return path.read_text().splitlines() if path.exists() else []


class Runner:
    """Executes one readiness step against one fixture repository state."""

    def __init__(self, root: Path, script: str) -> None:
        self.script = script
        self.fixtures = root / "fixtures"
        self.work = root / "work"
        self.tmp = root / "runner-tmp"
        bindir = root / "bin"
        for d in (self.fixtures, self.work, self.tmp, bindir):
            d.mkdir(parents=True)
        stub = bindir / "gh"
        stub.write_text(GH_STUB)
        stub.chmod(0o755)
        (self.tmp / "pr-readiness-summary.md").write_text("## summary\n")
        self.summary = root / "step-summary.md"
        self.summary.write_text("")
        self.env = {
            **os.environ,
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "FIXTURES": str(self.fixtures),
            "REPO": "kirodotdev/KiroCrew",
            "PR": "2064",
            "SHA": SHA,
            "URL": "https://github.com/kirodotdev/KiroCrew/actions/runs/1",
            "RUNNER_TEMP": str(self.tmp),
            "GITHUB_STEP_SUMMARY": str(self.summary),
            "GH_TOKEN": "stub",
        }

    def run(
        self,
        *,
        target_label: str = "readiness: passed",
        status_state: str = "success",
        description: str = "all checks passed",
        pr_labels: tuple[str, ...] = ("readiness: checking",),
        repo_labels: tuple[str, ...] = (
            "readiness: checking",
            "readiness: action required",
            "readiness: passed",
        ),
        head_sha: str = SHA,
        fail_label_delete: bool = False,
        fail_label_create: bool = False,
        fail_status: bool = False,
    ) -> Result:
        (self.fixtures / "head_sha.txt").write_text(head_sha + "\n")
        (self.fixtures / "pr_labels.txt").write_text("\n".join(pr_labels) + "\n")
        (self.fixtures / "repo_labels.txt").write_text("\n".join(repo_labels) + "\n")
        for name, on in (
            ("fail_label_delete", fail_label_delete),
            ("fail_label_create", fail_label_create),
            ("fail_status", fail_status),
        ):
            flag = self.fixtures / name
            flag.unlink(missing_ok=True)
            if on:
                flag.write_text("1")
        for stale in ("calls.txt", "deleted.txt", "added.txt", "published_status.json"):
            (self.fixtures / stale).unlink(missing_ok=True)

        proc = subprocess.run(  # noqa: S603 - fixed argv, test-local stub
            ["bash", "-c", self.script],
            cwd=self.work,
            env={
                **self.env,
                "TARGET_LABEL": target_label,
                "STATUS_STATE": status_state,
                "DESCRIPTION": description,
            },
            text=True,
            capture_output=True,
        )
        return Result(proc, self.fixtures)


@pytest.fixture
def runner(tmp_path: Path, publish_script: str) -> Runner:
    return Runner(tmp_path, publish_script)


# ── The verdict lands ────────────────────────────────────────────────────────


def test_the_verdict_and_the_label_are_both_published(runner: Runner) -> None:
    result = runner.run(target_label="readiness: passed")
    assert result.ok, result.proc.stderr
    assert result.published == {
        "state": "success",
        "target_url": runner.env["URL"],
        "description": "all checks passed",
        "context": "PR Readiness",
    }
    assert len(result.deleted) == 1, result.deleted
    assert "readiness%3A%20checking" in result.deleted[0]
    assert "readiness: passed" in result.added


def test_a_stale_revision_publishes_nothing(runner: Runner) -> None:
    result = runner.run(head_sha="0000000000000000000000000000000000000000")
    assert result.ok, result.proc.stderr
    assert result.published is None
    assert result.deleted == []
    assert result.added == ""


# ── Losing a label race must not withhold the verdict ────────────────────────


def test_a_label_removed_by_a_concurrent_run_still_publishes(runner: Runner) -> None:
    """The 404 that froze 62 of this workflow's 100 most recent failures.

    A peer run on the same revision removes the label between this run's
    snapshot and its DELETE. The removal has already reached the state this run
    wanted, so it is not an error -- and it must not cost the verdict.
    """
    result = runner.run(fail_label_delete=True)
    assert result.ok, result.proc.stderr
    assert result.published is not None
    assert result.published["context"] == "PR Readiness"


def test_a_label_created_by_a_concurrent_run_still_publishes(runner: Runner) -> None:
    result = runner.run(
        repo_labels=("some-unrelated-label",), fail_label_create=True
    )
    assert result.ok, result.proc.stderr
    assert result.published is not None


def test_the_verdict_is_published_before_any_label_call(runner: Runner) -> None:
    """Ordering is the structural half of the fix.

    Tolerating the two known races removes the two failures we have seen;
    publishing first is what stops any FUTURE label-call failure from being able
    to withhold the verdict at all.
    """
    result = runner.run()
    assert result.ok, result.proc.stderr
    status_calls = [i for i, c in enumerate(result.calls) if "/statuses/" in c]
    label_calls = [i for i, c in enumerate(result.calls) if "label" in c]
    assert status_calls, result.calls
    assert label_calls, result.calls
    assert status_calls[0] < label_calls[0], result.calls


# ── But a real failure must still be a failure ───────────────────────────────


def test_a_failure_to_publish_the_verdict_fails_the_step(runner: Runner) -> None:
    """The tolerance is scoped to the advisory labels, not to the verdict.

    Without this the fix would be indistinguishable from swallowing errors,
    which would buy quiet at the cost of the signal.
    """
    result = runner.run(fail_status=True)
    assert not result.ok
    assert result.published is None


def test_an_unexpected_label_error_still_fails_the_step(
    tmp_path: Path, publish_script: str
) -> None:
    """Only the two documented races are tolerated; a 500 is still a failure."""
    runner = Runner(tmp_path, publish_script)
    stub = tmp_path / "bin" / "gh"
    stub.write_text(
        GH_STUB.replace(
            "echo 'gh: Label does not exist (HTTP 404)' >&2",
            "echo 'gh: Server Error (HTTP 500)' >&2",
        )
    )
    stub.chmod(0o755)
    result = runner.run(fail_label_delete=True)
    assert not result.ok
    # The verdict still landed, because it is published first.
    assert result.published is not None


# ── The closed-PR cleanup step shares the same race ──────────────────────────


def test_the_cleanup_step_tolerates_a_concurrent_removal(
    tmp_path: Path, cleanup_script: str
) -> None:
    """Same snapshot-then-remove shape, same 404, and it must not fail the run.

    Fixing only the publish step would leave this sibling behind.
    """
    runner = Runner(tmp_path, cleanup_script)
    result = runner.run(fail_label_delete=True)
    assert result.ok, result.proc.stderr
