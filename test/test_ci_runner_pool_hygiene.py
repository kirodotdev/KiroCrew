"""Structural guards for the runner-pool hygiene in .github/workflows.

Three cheap rules keep this repository from holding hosted runners it does not
need: heavy optional PR jobs skip while a PR is a draft, a PR workflow whose
runs a newer push supersedes carries a cancelling `concurrency` group, and a job
that can hang has a
`timeout-minutes` cap. These tests pin each rule where it was applied, plus the
two invariants that make a draft skip safe:

* a draft-skipped workflow must list `ready_for_review`, or leaving draft
  starts no run and the PR is never checked on its final head;
* no lane that pr-readiness.yml reads may be draft-skipped, because readiness
  counts a workflow-level `skipped` run as non-blocking.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
DRAFT_GUARD = "github.event.pull_request.draft != true"

DRAFT_SKIPPED = ("docker-smoke.yml", "crew-image-build.yml", "denial-differential.yml")


def _load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _on(document: dict) -> dict:
    # PyYAML reads the bare key `on` as boolean True.
    return document.get("on") or document.get(True) or {}


def _pull_request_types(document: dict) -> list[str]:
    trigger = _on(document).get("pull_request")
    if trigger is None:
        return []
    return list((trigger or {}).get("types") or ["opened", "synchronize", "reopened"])


def _draft_gated(document: dict) -> bool:
    return any(
        "pull_request.draft" in str(job.get("if", "")) for job in document.get("jobs", {}).values()
    )


def _readiness_lane_files() -> set[str]:
    text = (WORKFLOWS / "pr-readiness.yml").read_text(encoding="utf-8")
    return set(re.findall(r'"([a-z0-9-]+\.yml)\|', text))


@pytest.mark.parametrize("name", DRAFT_SKIPPED)
def test_draft_skipped_workflow_skips_every_job_on_drafts(name: str) -> None:
    for job_id, job in _load(name)["jobs"].items():
        assert DRAFT_GUARD in str(job.get("if", "")), f"{name}: {job_id}"


def test_every_draft_gated_workflow_reruns_on_ready_for_review() -> None:
    gated = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not _draft_gated(document):
            continue
        gated.append(path.name)
        types = _pull_request_types(document)
        assert types, f"{path.name}: draft-gated but has no pull_request trigger"
        assert (
            "ready_for_review" in types
        ), f"{path.name} skips drafts but never runs when the PR leaves draft"
        # Keep the default types: dropping one would stop the lane on pushes.
        for required in ("opened", "synchronize", "reopened"):
            assert required in types, f"{path.name}: lost `{required}`"
    assert set(DRAFT_SKIPPED) <= set(gated)


def test_no_readiness_lane_is_draft_skipped() -> None:
    lanes = _readiness_lane_files()
    # The parse must find the lane table, or this test passes vacuously.
    assert {"ci.yml", "claude-review.yml", "codex-review.yml"} <= lanes
    for name in sorted(lanes):
        path = WORKFLOWS / name
        if path.exists():
            assert not _draft_gated(_load(name)), (
                f"{name} is a pr-readiness lane: a draft-skipped run reads as " "non-blocking there"
            )


def test_internal_content_scan_gate_only_cancels_superseded_pr_runs() -> None:
    concurrency = _load("internal-content-scan-gate.yml")["concurrency"]
    group = concurrency["group"]
    # PR runs share a group per PR; push and merge-group runs key on their
    # commit, so the next push to main can never cancel or evict one.
    assert "github.event.pull_request.number || github.sha" in group
    cancel = concurrency["cancel-in-progress"]
    assert cancel is not True
    assert cancel == "${{ github.event_name == 'pull_request' }}"


def test_fork_pr_label_job_has_a_timeout() -> None:
    job = _load("fork-pr-label.yml")["jobs"]["label"]
    assert 0 < int(job["timeout-minutes"]) <= 10


def test_desktop_matrix_job_has_a_timeout() -> None:
    # A hang in this resolver holds its runner and stalls every desktop leg
    # that `needs` it. The job is seconds of work (shallow checkout plus a
    # depth-1 fetch of one base SHA), so no ci.yml `changes`-style budget for a
    # full-history fetch is needed -- just a cap.
    job = _load("build.yml")["jobs"]["desktop-matrix"]
    assert 0 < int(job["timeout-minutes"]) <= 15


def test_pages_deploy_is_never_cancelled_in_flight() -> None:
    # pages.yml has no pull_request trigger, so every run it can have publishes
    # to the github-pages environment. A queued duplicate is cheap to discard;
    # an interrupted production deploy is not.
    concurrency = _load("pages.yml")["concurrency"]
    assert concurrency["cancel-in-progress"] is False


def test_client_py_only_cancels_superseded_pr_runs() -> None:
    # Pushes to main share the `github.ref` group key, so a bare
    # cancel-in-progress would let a second push kill the first one's in-flight
    # run -- and this suite is the only coverage the kirocrew-client
    # distribution has. Merge-group entries are unaffected either way: their
    # `github.ref` is a unique gh-readonly-queue ref.
    concurrency = _load("client-py.yml")["concurrency"]
    cancel = concurrency["cancel-in-progress"]
    assert cancel is not True
    assert cancel == "${{ github.event_name == 'pull_request' }}"
