"""Behavioural tests for .github/workflows/pr-readiness-sweep.yml.

The sweep's entire decision logic lives in one `run:` block of shell + jq that no
other test touches. These tests extract that script and execute it for real with
`gh` replaced by a stub, so the re-fire CONDITIONS are verified rather than
assumed -- and the condition is the whole point: too narrow and a frozen verdict
stays frozen, too broad and every genuinely-failing PR gets dispatched every 15
minutes forever.

Skipped where the POSIX toolchain the script needs (bash, jq, GNU `date -d`) is
unavailable, which is the case on the Windows leg of the matrix. Mirrors the
explicit nt guard in test_issue_triage_workflow.py.
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
    Path(__file__).resolve().parents[1]
    / ".github"
    / "workflows"
    / "pr-readiness-sweep.yml"
)


def _gnu_date() -> bool:
    """GNU `date -d` is required; BSD date uses -j -f and would silently differ."""
    return (
        subprocess.run(
            ["date", "-u", "-d", "2026-01-01T00:00:00Z", "+%s"],
            capture_output=True,
        ).returncode
        == 0
    )


pytestmark = pytest.mark.skipif(
    not WORKFLOW.exists()
    or os.name == "nt"
    or shutil.which("bash") is None
    or shutil.which("jq") is None
    or not _gnu_date(),
    reason="requires the workflow plus a POSIX bash, jq and GNU date",
)

# `gh` stub. Three shapes are served, keyed on the subcommand:
#   pr list                 -> the fixture PR list
#   api .../statuses        -> the fixture readiness status history
#   api .../check-runs      -> the fixture check runs
#   workflow run            -> RECORD the dispatch instead of firing it
GH_STUB = r"""#!/usr/bin/env bash
set -euo pipefail
if [ "$1 ${2:-}" = "pr list" ]; then
  cat "$FIXTURES/prs.json"
  exit 0
fi
if [ "$1 ${2:-}" = "workflow run" ]; then
  # Record every -f key=value so the test can assert pr/sha were passed through.
  printf '%s\n' "$*" >> "$FIXTURES/dispatched.txt"
  exit 0
fi
if [ "$1" = "api" ]; then
  case "${2:-}" in
    *"/check-runs") cat "$FIXTURES/check_runs.json"; exit 0 ;;
    *"/statuses")
      # Emulate a transport failure when the test asks for one: gh exits
      # non-zero having written nothing to stdout.
      if [ -f "$FIXTURES/statuses_fail" ]; then exit 1; fi
      cat "$FIXTURES/statuses.json"; exit 0 ;;
  esac
fi
echo "gh stub: unhandled: $*" >&2
exit 90
"""


def _script() -> str:
    spec = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = spec["jobs"]["sweep"]["steps"]
    runs = [s["run"] for s in steps if "run" in s]
    assert len(runs) == 1, f"sweep step count changed: {len(runs)}"
    return runs[0]


@pytest.fixture(scope="module")
def script() -> str:
    return _script()


class Runner:
    """Executes the sweep's one step against one fixture repository state."""

    def __init__(self, root: Path, script: str) -> None:
        self.script = script
        self.fixtures = root / "fixtures"
        self.work = root / "work"
        bindir = root / "bin"
        for d in (self.fixtures, self.work, bindir):
            d.mkdir(parents=True)
        stub = bindir / "gh"
        stub.write_text(GH_STUB)
        stub.chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "FIXTURES": str(self.fixtures),
            "REPO": "kirodotdev/KiroCrew",
            "STATUS_CONTEXT": "PR Readiness",
            "STALE_MINUTES": "15",
            "MAX_DISPATCH": "10",
            "PR_LIST_LIMIT": "900",
        }

    def sweep(
        self,
        *,
        state: str | None,
        status_at: str | None = None,
        check_completed_at: str | None = None,
        check_conclusion: str = "success",
        extra_check_page: tuple[str, str] | None = None,
        statuses_read_fails: bool = False,
        pr: int = 2064,
        sha: str = "4328fd0f941f09ff10f245fbdb4accf7c246febe",
        context: str = "PR Readiness",
        max_dispatch: str = "10",
        pr_updated_at: str = "2020-01-01T00:00:00Z",
        extra_statuses: list[dict] | None = None,
    ) -> list[str]:
        """Run the sweep over ONE pull request; return the dispatches recorded.

        `state=None` means the head SHA carries NO readiness status at all, which
        is the unpublished-verdict freeze mode.
        """
        (self.fixtures / "prs.json").write_text(
            json.dumps(
                [{"number": pr, "headRefOid": sha, "updatedAt": pr_updated_at}]
            )
        )
        statuses = (
            []
            if state is None
            else [{"context": context, "state": state, "updated_at": status_at}]
        )
        # `/statuses` returns newest-first, and the sweep takes the FIRST entry
        # matching its own context, so extras are appended after. The fixture is
        # written in the `--paginate --slurp` shape the sweep now requests: an
        # OUTER array of pages, each page being the endpoint's own array.
        statuses += extra_statuses or []
        (self.fixtures / "statuses.json").write_text(json.dumps([statuses]))
        fail_marker = self.fixtures / "statuses_fail"
        if statuses_read_fails:
            fail_marker.write_text("")
        else:
            fail_marker.unlink(missing_ok=True)
        runs = (
            []
            if check_completed_at is None
            else [
                {
                    "status": "completed",
                    "conclusion": check_conclusion,
                    "completed_at": check_completed_at,
                }
            ]
        )
        # Slurped shape again: an array of PAGES, each `{"check_runs": [...]}`.
        # `extra_check_page` adds a second page so the pagination fix is exercised
        # rather than assumed -- unslurped, jq would emit one `max` per page.
        pages = [{"check_runs": runs}]
        if extra_check_page is not None:
            conclusion, completed_at = extra_check_page
            pages.append(
                {
                    "check_runs": [
                        {
                            "status": "completed",
                            "conclusion": conclusion,
                            "completed_at": completed_at,
                        }
                    ]
                }
            )
        (self.fixtures / "check_runs.json").write_text(json.dumps(pages))
        applied = self.fixtures / "dispatched.txt"
        applied.unlink(missing_ok=True)

        proc = subprocess.run(  # noqa: S603 - fixed argv, test-local stub
            ["bash", "-c", self.script],
            cwd=self.work,
            env={**self.env, "MAX_DISPATCH": max_dispatch},
            text=True,
            capture_output=True,
        )
        # The sweep must never fail a run: a nudge it cannot make is not an error.
        assert proc.returncode == 0, proc.stderr
        self.last_stdout = proc.stdout
        if not applied.exists():
            return []
        return applied.read_text().splitlines()


@pytest.fixture
def runner(tmp_path: Path, script: str) -> Runner:
    return Runner(tmp_path, script)


# ── The pending freeze (the sweep's original purpose) ────────────────────────


def test_stale_pending_is_refired(runner: Runner) -> None:
    dispatched = runner.sweep(state="pending", status_at="2020-01-01T00:00:00Z")
    assert len(dispatched) == 1
    assert "pr=2064" in dispatched[0]
    assert "sha=4328fd0f941f09ff10f245fbdb4accf7c246febe" in dispatched[0]


def test_fresh_pending_is_left_alone(runner: Runner) -> None:
    """Inside STALE_MINUTES the fan-out may genuinely still be running."""
    from datetime import datetime, timedelta, timezone

    recent = (datetime.now(timezone.utc) - timedelta(minutes=2)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    assert runner.sweep(state="pending", status_at=recent) == []


# ── The re-run freeze (the case this change adds) ────────────────────────────


def test_failure_with_later_check_evidence_is_refired(runner: Runner) -> None:
    """The PR #2064 incident, reduced.

    `gh run rerun --failed` creates a new run ATTEMPT whose completion emits no
    fresh `workflow_run: completed`, so the aggregator never re-evaluates. Here
    the verdict was published at 19:01:24Z and a check finished at 19:16:13Z --
    evidence that landed after the verdict, making it stale by construction.
    """
    dispatched = runner.sweep(
        state="failure",
        status_at="2026-08-07T19:01:24Z",
        check_completed_at="2026-08-07T19:16:13Z",
    )
    assert len(dispatched) == 1
    assert "pr=2064" in dispatched[0]


def test_failure_with_no_later_evidence_is_left_alone(runner: Runner) -> None:
    """The anti-storm property, and the reason age is NOT the test here.

    A PR that is genuinely failing has an old terminal verdict and no newer check
    evidence. Dispatching on `state == failure` alone would nudge it every 15
    minutes forever; this asserts it is nudged zero times.
    """
    assert (
        runner.sweep(
            state="failure",
            status_at="2026-08-07T19:16:13Z",
            check_completed_at="2026-08-07T19:01:24Z",
        )
        == []
    )


def test_failure_refire_is_self_terminating(runner: Runner) -> None:
    """Republishing must end the loop.

    After a nudge, readiness becomes the NEWEST timestamp for that SHA. The next
    sweep therefore sees no evidence newer than the verdict and stops -- which is
    what makes a scheduled re-fire safe rather than a dispatch loop.
    """
    # Same evidence, but the verdict has since been republished after it.
    assert (
        runner.sweep(
            state="failure",
            status_at="2026-08-07T19:20:00Z",
            check_completed_at="2026-08-07T19:16:13Z",
        )
        == []
    )


def test_failure_with_no_completed_checks_is_left_alone(runner: Runner) -> None:
    """No check evidence at all means nothing proves the verdict stale."""
    assert (
        runner.sweep(state="failure", status_at="2026-08-07T19:01:24Z") == []
    )


def test_check_completing_in_the_same_second_is_not_new_evidence(runner: Runner) -> None:
    """The publish and the completion that triggered it race within a second.

    Without the margin, every ordinary terminal verdict would look stale on the
    very next sweep.
    """
    assert (
        runner.sweep(
            state="failure",
            status_at="2026-08-07T19:01:24Z",
            check_completed_at="2026-08-07T19:01:26Z",
        )
        == []
    )


# ── The green freeze: a verdict contradicted by later FAILING evidence ───────


@pytest.mark.parametrize("state", ["success", "error"])
def test_green_verdict_with_later_failing_evidence_is_refired(
    runner: Runner, state: str
) -> None:
    """The unsafe direction of the same re-run mechanism.

    A job re-run that flips a lane red after a green verdict emits no fresh
    `workflow_run: completed`, so the required aggregate stays green over a
    now-red revision -- which PERMITS a merge, where a stale red only blocks one.
    """
    dispatched = runner.sweep(
        state=state,
        status_at="2026-08-07T19:01:24Z",
        check_completed_at="2026-08-07T19:16:13Z",
        check_conclusion="failure",
    )
    assert len(dispatched) == 1
    assert "pr=2064" in dispatched[0]


@pytest.mark.parametrize(
    "conclusion", ["timed_out", "cancelled", "action_required", "stale", "startup_failure"]
)
def test_every_failure_class_conclusion_counts_as_red_evidence(
    runner: Runner, conclusion: str
) -> None:
    """The lane reader in pr-readiness.yml treats all six as failure-class.

    If the sweep recognised only `failure`, a lane cancelled or timed out by a
    re-run would leave the green verdict frozen.
    """
    assert (
        len(
            runner.sweep(
                state="success",
                status_at="2026-08-07T19:01:24Z",
                check_completed_at="2026-08-07T19:16:13Z",
                check_conclusion=conclusion,
            )
        )
        == 1
    )


def test_a_later_passing_check_never_refires_a_green_verdict(runner: Runner) -> None:
    """The anti-storm property for this path, and why the test is narrowed.

    Housekeeping check-runs (`Strip stale workflow-change override`, `Fork
    workflow-change guard`) legitimately complete days after a verdict on a
    long-lived PR. An unnarrowed "any check completed later" test would re-fire
    most green PRs on every sweep while proving nothing, and a later pass cannot
    turn a green verdict red anyway.
    """
    assert (
        runner.sweep(
            state="success",
            status_at="2026-08-07T19:01:24Z",
            check_completed_at="2026-08-25T09:18:07Z",
            check_conclusion="skipped",
        )
        == []
    )


def test_green_refire_is_self_terminating(runner: Runner) -> None:
    """Republishing must end this loop too, exactly as it does for `failure`."""
    assert (
        runner.sweep(
            state="success",
            status_at="2026-08-07T19:20:00Z",
            check_completed_at="2026-08-07T19:16:13Z",
            check_conclusion="failure",
        )
        == []
    )


def test_green_verdict_with_no_check_evidence_is_left_alone(runner: Runner) -> None:
    """An ordinary green PR is never nudged."""
    assert runner.sweep(state="success", status_at="2020-01-01T00:00:00Z") == []


# ── The unpublished freeze: no readiness status was ever written ─────────────


def test_a_missing_readiness_status_is_refired(runner: Runner) -> None:
    """The PR #2783 incident, reduced.

    `pr-readiness.yml` does not retry its status POST and instructs a human to
    re-run the workflow. When that POST failed on `gh: HTTP 503`, the SHA carried
    no readiness status -- no `pending` to age out, no event pending -- and the
    only automatic re-runner skipped the PR because it had no status to read. The
    one case the publisher delegates to a re-run was the one case nothing re-ran.
    """
    dispatched = runner.sweep(state=None, pr_updated_at="2026-08-17T14:20:00Z")
    assert len(dispatched) == 1
    assert "pr=2064" in dispatched[0]
    assert "sha=4328fd0f941f09ff10f245fbdb4accf7c246febe" in dispatched[0]


def test_a_brand_new_pull_request_is_left_alone(runner: Runner) -> None:
    """Within STALE_MINUTES of the last push, the PR's own run really is coming.

    This is what keeps the new path from dispatching against every PR opened in
    the last quarter of an hour.
    """
    from datetime import datetime, timedelta, timezone

    recent = (datetime.now(timezone.utc) - timedelta(minutes=2)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    assert runner.sweep(state=None, pr_updated_at=recent) == []


def test_a_missing_status_still_respects_the_dispatch_cap(runner: Runner) -> None:
    """The runaway backstop applies to the new path as well."""
    assert (
        runner.sweep(
            state=None, pr_updated_at="2026-08-17T14:20:00Z", max_dispatch="0"
        )
        == []
    )


def test_an_unparseable_pr_timestamp_is_left_alone(runner: Runner) -> None:
    """Fail closed on a timestamp the sweep cannot read, rather than dispatching."""
    assert runner.sweep(state=None, pr_updated_at="not-a-date") == []


# ── Truncation: the oldest PRs must never be dropped silently ────────────────


def test_the_open_pr_listing_is_not_capped_near_the_real_backlog(script: str) -> None:
    """`gh pr list` returns newest-first and truncates SILENTLY at --limit.

    A ceiling near the real open-PR count drops the OLDEST PRs -- precisely the
    frozen ones this sweep exists to rescue -- so the limit must stay well clear
    of it and a hit must be reported rather than absorbed.
    """
    assert "--limit 300" not in script
    assert '--limit "$PR_LIST_LIMIT"' in script
    assert "::warning::" in script


def test_a_truncated_listing_is_reported(runner: Runner) -> None:
    """Hitting the ceiling is an action item, not a measurement."""
    runner.env["PR_LIST_LIMIT"] = "1"
    runner.sweep(state="success", status_at="2020-01-01T00:00:00Z")
    assert "::warning::" in runner.last_stdout
    assert "hit its ceiling" in runner.last_stdout


def test_a_different_status_context_never_drives_the_decision(runner: Runner) -> None:
    """Only the aggregate this sweep owns may be read.

    The SHA carries a FRESH `PR Readiness` pending (nothing to rescue) alongside a
    long-stale failing `Coverage Gate`. A sweep that matched on the wrong context
    would read the Coverage Gate failure, see later check evidence, and dispatch.
    """
    from datetime import datetime, timedelta, timezone

    recent = (datetime.now(timezone.utc) - timedelta(minutes=2)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    assert (
        runner.sweep(
            state="pending",
            status_at=recent,
            check_completed_at="2026-08-07T19:16:13Z",
            check_conclusion="failure",
            extra_statuses=[
                {
                    "context": "Coverage Gate",
                    "state": "failure",
                    "updated_at": "2020-01-01T00:00:00Z",
                }
            ],
        )
        == []
    )


def test_only_a_foreign_status_reads_as_an_unpublished_verdict(runner: Runner) -> None:
    """A SHA with other statuses but no readiness one is still unpublished.

    This is the #2783 shape generalised: what makes the verdict absent is that no
    `PR Readiness` context exists, not that the SHA is bare. Treating it as
    "already has a status" would leave the required aggregate permanently missing.
    """
    dispatched = runner.sweep(
        state=None,
        pr_updated_at="2026-08-17T14:20:00Z",
        extra_statuses=[
            {
                "context": "Coverage Gate",
                "state": "success",
                "updated_at": "2026-08-17T14:00:00Z",
            }
        ],
    )
    assert len(dispatched) == 1


def test_max_dispatch_caps_the_sweep(runner: Runner) -> None:
    """The runaway backstop still applies to the new path."""
    assert (
        runner.sweep(
            state="failure",
            status_at="2026-08-07T19:01:24Z",
            check_completed_at="2026-08-07T19:16:13Z",
            max_dispatch="0",
        )
        == []
    )


# ── Fairness: oldest-stale-first, never PR-list order ────────────────────────

# A `gh` stub that serves per-SHA readiness statuses, so several PRs can be
# frozen for different lengths of time in one sweep. The base stub keys statuses
# only on the subcommand (one fixture for all PRs), which cannot express "PR A is
# staler than PR B" -- the exact thing this ordering test must vary.
GH_STUB_PER_SHA = r"""#!/usr/bin/env bash
set -euo pipefail
if [ "$1 ${2:-}" = "pr list" ]; then
  cat "$FIXTURES/prs.json"; exit 0
fi
if [ "$1 ${2:-}" = "workflow run" ]; then
  printf '%s\n' "$*" >> "$FIXTURES/dispatched.txt"; exit 0
fi
if [ "$1" = "api" ]; then
  case "${2:-}" in
    *"/check-runs") echo '[{"check_runs":[]}]'; exit 0 ;;
    *"/commits/"*"/statuses")
      sha="${2#*/commits/}"; sha="${sha%%/statuses}"
      cat "$FIXTURES/status_${sha}.json"; exit 0 ;;
  esac
fi
echo "gh stub: unhandled: $*" >&2
exit 90
"""


def test_dispatch_is_oldest_stale_first(tmp_path: Path, script: str) -> None:
    """The longest-frozen PR is dispatched first, regardless of PR-list order.

    This is the anti-starvation property: with a per-sweep cap, dispatching in
    `gh pr list` order (newest-first) permanently defers the oldest, lowest-
    numbered frozen PRs. Ordering by how long each PR has been stale fixes that.
    """
    fixtures = tmp_path / "fixtures"
    work = tmp_path / "work"
    bindir = tmp_path / "bin"
    for d in (fixtures, work, bindir):
        d.mkdir(parents=True)
    stub = bindir / "gh"
    stub.write_text(GH_STUB_PER_SHA)
    stub.chmod(0o755)

    # PRs as `gh pr list` returns them (newest-numbered first), each frozen for a
    # DIFFERENT length of time. Staleness order (oldest first) is 3120, 3400, 3612
    # -- the opposite of the list order for the newest entry.
    prs = [
        {"number": 3612, "headRefOid": "aaa"},
        {"number": 3120, "headRefOid": "bbb"},
        {"number": 3400, "headRefOid": "ccc"},
    ]
    (fixtures / "prs.json").write_text(json.dumps(prs))
    ages = {
        "aaa": "2020-01-01T00:00:03Z",  # least stale
        "bbb": "2020-01-01T00:00:01Z",  # most stale -> first
        "ccc": "2020-01-01T00:00:02Z",
    }
    for sha, at in ages.items():
        # Slurped shape: an outer array of pages.
        (fixtures / f"status_{sha}.json").write_text(
            json.dumps([[{"context": "PR Readiness", "state": "pending", "updated_at": at}]])
        )

    proc = subprocess.run(  # noqa: S603 - fixed argv, test-local stub
        ["bash", "-c", script],
        cwd=work,
        env={
            **os.environ,
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "FIXTURES": str(fixtures),
            "REPO": "kirodotdev/KiroCrew",
            "STATUS_CONTEXT": "PR Readiness",
            "STALE_MINUTES": "15",
            "MAX_DISPATCH": "200",
            "PR_LIST_LIMIT": "900",
        },
        text=True,
        capture_output=True,
    )
    assert proc.returncode == 0, proc.stderr

    dispatched = (fixtures / "dispatched.txt").read_text().splitlines()
    order = []
    for line in dispatched:
        for tok in line.split():
            if tok.startswith("pr="):
                order.append(int(tok.split("=", 1)[1]))
    assert order == [3120, 3400, 3612], f"expected oldest-first, got {order}"


def test_the_sweep_never_recomputes_a_verdict_itself(script: str) -> None:
    """It may only nudge the authoritative workflow.

    A sweep that published its own verdict would be a second source of truth for
    a required status -- and could mark a PR ready without the reviewers.
    """
    assert "gh workflow run pr-readiness.yml" in script
    for forbidden in ("/statuses -X POST", "--method POST", "-X POST"):
        assert forbidden not in script, f"sweep must not write statuses: {forbidden}"


# ── Paginated reads: one verdict per SHA, not one per page ───────────────────


def test_check_evidence_is_read_across_every_page(runner: Runner) -> None:
    """`--paginate` alone makes jq emit one `max` PER PAGE.

    `date -d` then rejects the multi-line string, the epoch reads 0, and the PR is
    skipped -- silently exempting every PR with more than 100 check-runs, which on
    this repo is any PR whose lanes have been re-run. The newest evidence here
    lives on the SECOND page, so a page-blind read cannot find it.
    """
    dispatched = runner.sweep(
        state="failure",
        status_at="2026-08-07T19:01:24Z",
        check_completed_at="2026-08-07T19:00:00Z",
        extra_check_page=("success", "2026-08-07T19:16:13Z"),
    )
    assert len(dispatched) == 1
    assert "pr=2064" in dispatched[0]


def test_a_green_verdict_sees_failing_evidence_on_a_later_page(runner: Runner) -> None:
    """Same pagination property for the green arm."""
    dispatched = runner.sweep(
        state="success",
        status_at="2026-08-07T19:01:24Z",
        check_completed_at="2026-08-07T19:00:00Z",
        check_conclusion="success",
        extra_check_page=("failure", "2026-08-07T19:16:13Z"),
    )
    assert len(dispatched) == 1


# ── Transport failure is not an absent verdict ───────────────────────────────


def test_a_failed_statuses_read_is_not_treated_as_unpublished(runner: Runner) -> None:
    """A statuses-API 503 and a genuinely absent status both yield empty jq output.

    Conflating them would turn transient GitHub trouble into a spurious re-fire of
    an arbitrary old PR -- on a shared token budget, at 15-minute intervals, on
    every PR at once. The read is therefore checked for failure BEFORE the filter.
    """
    dispatched = runner.sweep(
        state=None, pr_updated_at="2026-08-17T14:20:00Z", statuses_read_fails=True
    )
    assert dispatched == []
    assert "statuses lookup failed" in runner.last_stdout
