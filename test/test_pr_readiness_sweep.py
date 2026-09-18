"""Behavioural tests for .github/workflows/pr-readiness-sweep.yml.

The sweep's entire decision logic lives in one `run:` block of shell + jq that no
other test touches. These tests extract that script and execute it for real with
`gh` replaced by a stub, so the re-fire CONDITIONS are verified rather than
assumed -- and the condition is the whole point: too narrow and a frozen verdict
stays frozen, too broad and every genuinely-failing PR gets dispatched every 15
minutes forever.

The run block reads its evidence through .github/scripts/readiness_sweep_scan.py,
which pages the open pull requests over GraphQL. So each Runner links the REAL
scanner into the fixture workspace and the `gh` stub answers `api graphql` by
translating three REST-shaped fixtures (see GRAPHQL_STUB). The scanner, the shell
and the decision are therefore all exercised end to end, and the fixtures stay
readable as "statuses" and "check runs" rather than as GraphQL documents.

Skipped where the POSIX toolchain the script needs (bash, jq, GNU `date -d`) is
unavailable, which is the case on the Windows leg of the matrix. Mirrors the
explicit nt guard in test_issue_triage_workflow.py.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "pr-readiness-sweep.yml"
SCANNER = REPO_ROOT / ".github" / "scripts" / "readiness_sweep_scan.py"


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
    or not SCANNER.exists()
    or os.name == "nt"
    or shutil.which("bash") is None
    or shutil.which("jq") is None
    or not _gnu_date(),
    reason="requires the workflow plus a POSIX bash, jq and GNU date",
)

# `gh` stub. Three shapes are served, keyed on the subcommand:
#   api graphql             -> the fixture repository state, translated (see below)
#   api .../comments        -> the fixture issue comments, and the read is RECORDED
#   workflow run            -> RECORD the dispatch instead of firing it
#
# The comment read is recorded because mode 5 is now GATED on the PR's own
# `updatedAt`, and "this read did not happen" is the whole assertion of one test:
# a dispatch count cannot distinguish a read that found nothing from a read that
# was correctly skipped.
GH_STUB = r"""#!/usr/bin/env bash
set -euo pipefail
if [ "$1 ${2:-}" = "workflow run" ]; then
  # Record every -f key=value so the test can assert pr/sha were passed through.
  printf '%s\n' "$*" >> "$FIXTURES/dispatched.txt"
  exit 0
fi
if [ "$1 ${2:-}" = "api graphql" ]; then
  exec "$STUB_PYTHON" "$FIXTURES/graphql_stub.py" "$@"
fi
if [ "$1" = "api" ]; then
  case "${2:-}" in
    *"/comments")
      printf '%s\n' "$*" >> "$FIXTURES/comments_read.txt"
      cat "$FIXTURES/comments.json"; exit 0 ;;
  esac
fi
echo "gh stub: unhandled: $*" >&2
exit 90
"""

# The `gh api graphql` half of the stub, answering from these fixtures the way a
# real GraphQL server would. The fixtures stay in their REST-shaped, readable
# form (`statuses.json`, `check_runs.json`) and the translation happens here, so a
# test still reads as "a failure published at T, a check completed at T+1".
#
# Two queries are served, told apart by the query text: the pull-request page and
# the per-commit rollup-contexts page. Both page for real, at the 25-PR and
# 100-context sizes the scanner asks for, so the scanner's paging is exercised by
# these behavioural tests and not only by its unit tests.
GRAPHQL_STUB = '''#!/usr/bin/env python3
"""Answer `gh api graphql` from the sweep tests REST-shaped fixtures."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

FIXTURES = Path(os.environ["FIXTURES"])
PR_PAGE = 25
CONTEXTS_PAGE = 100

# Old, non-failure-class filler, used only to push the fixture's SECOND check-run
# page past the 100-node connection ceiling so the scanner must fetch a second
# contexts page to see it. NEUTRAL is not failure-class and the timestamp precedes
# every fixture value, so filler can change no decision.
FILLER = {
    "__typename": "CheckRun",
    "status": "COMPLETED",
    "conclusion": "NEUTRAL",
    "completedAt": "2000-01-01T00:00:00Z",
}


def _args() -> dict:
    """gh passes `-f name=value`; collect them."""
    out = {}
    argv = sys.argv[1:]
    for i, token in enumerate(argv):
        if token in ("-f", "-F") and i + 1 < len(argv):
            name, _, value = argv[i + 1].partition("=")
            out[name] = value
    return out


def _read(name, default):
    path = FIXTURES / name
    if not path.exists():
        return default
    return json.loads(path.read_text())


def _statuses(sha):
    """Flatten the paginated fixture. A per-SHA file wins when one exists."""
    per_sha = FIXTURES / ("status_" + sha + ".json")
    if per_sha.exists():
        pages = json.loads(per_sha.read_text())
    else:
        pages = _read("statuses.json", [])
    return [entry for page in pages for entry in page]


def _check_nodes():
    pages = _read("check_runs.json", [])
    nodes = []
    for index, page in enumerate(pages):
        if index == 1 and len(nodes) < CONTEXTS_PAGE:
            nodes += [dict(FILLER)] * (CONTEXTS_PAGE - len(nodes))
        for run in page.get("check_runs", []):
            conclusion = run.get("conclusion")
            nodes.append(
                {
                    "__typename": "CheckRun",
                    "status": str(run.get("status", "")).upper(),
                    "conclusion": None if conclusion is None else str(conclusion).upper(),
                    "completedAt": run.get("completed_at"),
                }
            )
    return nodes


def _contexts(nodes, offset):
    window = nodes[offset : offset + CONTEXTS_PAGE]
    end = offset + len(window)
    return {
        "totalCount": len(nodes),
        "pageInfo": {"hasNextPage": end < len(nodes), "endCursor": "ctx-%d" % end},
        "nodes": window,
    }


def _commit(pr):
    sha = str(pr.get("headRefOid", ""))
    contexts = [
        {
            "context": entry.get("context"),
            "state": str(entry.get("state") or "").upper(),
            "createdAt": entry.get("updated_at"),
        }
        for entry in _statuses(sha)
    ]
    return {
        "status": {"contexts": contexts},
        "statusCheckRollup": {"contexts": _contexts(_check_nodes(), 0)},
    }


def _pr_page(args):
    prs = _read("prs.json", [])
    offset = int(args["cursor"].split("-")[-1]) if "cursor" in args else 0
    window = prs[offset : offset + PR_PAGE]
    end = offset + len(window)
    nodes = [
        {
            "number": pr.get("number"),
            "updatedAt": pr.get("updatedAt"),
            "headRefOid": pr.get("headRefOid"),
            "commits": {"nodes": [{"commit": _commit(pr)}]},
        }
        for pr in window
    ]
    return {
        "data": {
            "repository": {
                "pullRequests": {
                    "pageInfo": {
                        "hasNextPage": end < len(prs),
                        "endCursor": "pr-%d" % end,
                    },
                    "nodes": nodes,
                }
            }
        }
    }


def _contexts_page(args):
    offset = int(args.get("after", "ctx-0").split("-")[-1])
    rollup = {"contexts": _contexts(_check_nodes(), offset)}
    return {"data": {"repository": {"object": {"statusCheckRollup": rollup}}}}


def main() -> int:
    if (FIXTURES / "graphql_fail").exists():
        # A GraphQL transport failure: non-zero exit, nothing on stdout.
        return 1
    args = _args()
    query = args.get("query", "")
    payload = _pr_page(args) if "pullRequests(" in query else _contexts_page(args)
    json.dump(payload, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


def _script() -> str:
    spec = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = spec["jobs"]["sweep"]["steps"]
    runs = [s["run"] for s in steps if "run" in s]
    assert len(runs) == 1, f"sweep step count changed: {len(runs)}"
    return runs[0]


@pytest.fixture(scope="module")
def script() -> str:
    return _script()


def install_scanner(fixtures: Path, work: Path) -> None:
    """Make the run block's `.github/scripts/...` path resolve inside `work`.

    The real scanner is copied in, not reimplemented: the sweep's decisions now
    depend on the JSON that script emits, so a fake would leave the two free to
    disagree exactly where a wrong field name or a missed page hides.
    """
    scripts = work / ".github" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / SCANNER.name).write_text(SCANNER.read_text(encoding="utf-8"), encoding="utf-8")
    (fixtures / "graphql_stub.py").write_text(GRAPHQL_STUB, encoding="utf-8")


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
        install_scanner(self.fixtures, self.work)
        self.env = {
            **os.environ,
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "FIXTURES": str(self.fixtures),
            # The stub is bash and must name an interpreter for the translator;
            # `python3` on PATH is not necessarily the one running these tests.
            "STUB_PYTHON": sys.executable,
            "REPO": "kirodotdev/KiroCrew",
            "STATUS_CONTEXT": "PR Readiness",
            "STALE_MINUTES": "15",
            "MAX_DISPATCH": "10",
        }

    def sweep(
        self,
        *,
        state: str | None,
        status_at: str | None = None,
        check_completed_at: str | None = None,
        check_conclusion: str = "success",
        extra_check_page: tuple[str, str] | None = None,
        graphql_read_fails: bool = False,
        pr: int = 2064,
        sha: str = "4328fd0f941f09ff10f245fbdb4accf7c246febe",
        context: str = "PR Readiness",
        max_dispatch: str = "10",
        pr_updated_at: str | None = None,
        extra_statuses: list[dict] | None = None,
        disposition_at: str | None = None,
        other_comments: list[dict] | None = None,
    ) -> list[str]:
        """Run the sweep over ONE pull request; return the dispatches recorded.

        `state=None` means the head SHA carries NO readiness status at all, which
        is the unpublished-verdict freeze mode.

        `pr_updated_at=None` derives the PR's last-activity time from the comment
        fixture, because that is what GitHub does: creating or editing a comment
        bumps the pull request's `updatedAt`. Mode 5's read is gated on that
        timestamp, so a fixture whose comments post-date an `updatedAt` frozen in
        2020 is not a state the API can produce, and a test built on one would
        pass for the wrong reason. Pass it explicitly to model a PR touched by
        something else -- a label, a review -- after the verdict.
        """
        comment_times = [
            at
            for at in [
                disposition_at,
                *[c.get("updated_at") for c in other_comments or []],
            ]
            if at
        ]
        if pr_updated_at is None:
            pr_updated_at = max(["2020-01-01T00:00:00Z", *comment_times])
        (self.fixtures / "prs.json").write_text(
            json.dumps([{"number": pr, "headRefOid": sha, "updatedAt": pr_updated_at}])
        )
        statuses = (
            [] if state is None else [{"context": context, "state": state, "updated_at": status_at}]
        )
        # `/statuses` returns newest-first, and the sweep takes the FIRST entry
        # matching its own context, so extras are appended after. The fixture is
        # written in the `--paginate --slurp` shape the sweep now requests: an
        # OUTER array of pages, each page being the endpoint's own array.
        statuses += extra_statuses or []
        (self.fixtures / "statuses.json").write_text(json.dumps([statuses]))
        fail_marker = self.fixtures / "graphql_fail"
        if graphql_read_fails:
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
        # Slurped shape for the issue-comments read mode 5 makes: an array of
        # PAGES, each page being the endpoint's own array of comments.
        comments = (
            []
            if disposition_at is None
            else [
                {
                    "body": "<!-- ai-review-disposition target=gpt head=abc1234 -->\nruling",
                    "updated_at": disposition_at,
                }
            ]
        )
        comments += other_comments or []
        (self.fixtures / "comments.json").write_text(json.dumps([comments]))
        applied = self.fixtures / "dispatched.txt"
        applied.unlink(missing_ok=True)
        self.comments_read = self.fixtures / "comments_read.txt"
        self.comments_read.unlink(missing_ok=True)

        proc = subprocess.run(  # noqa: S603 - fixed argv, test-local stub
            ["bash", "-c", self.script],
            cwd=self.work,
            env={**self.env, "MAX_DISPATCH": max_dispatch},
            text=True,
            encoding="utf-8",
            capture_output=True,
        )
        # The sweep must never fail a run: a nudge it cannot make is not an error.
        assert proc.returncode == 0, proc.stderr
        self.last_stdout = proc.stdout
        self.last_stderr = proc.stderr
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

    recent = (datetime.now(timezone.utc) - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert runner.sweep(state="pending", status_at=recent) == []


# ── The re-run freeze (the case this change adds) ────────────────────────────


def test_failure_with_later_check_evidence_is_refired(runner: Runner) -> None:
    """A failure with later check evidence and no fresh event is still re-fired.

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
    assert runner.sweep(state="failure", status_at="2026-08-07T19:01:24Z") == []


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
def test_green_verdict_with_later_failing_evidence_is_refired(runner: Runner, state: str) -> None:
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
    """A missing readiness status is re-fired.

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

    recent = (datetime.now(timezone.utc) - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert runner.sweep(state=None, pr_updated_at=recent) == []


def test_a_missing_status_still_respects_the_dispatch_cap(runner: Runner) -> None:
    """The runaway backstop applies to the new path as well."""
    assert runner.sweep(state=None, pr_updated_at="2026-08-17T14:20:00Z", max_dispatch="0") == []


def test_an_unparseable_pr_timestamp_is_left_alone(runner: Runner) -> None:
    """Fail closed on a timestamp the sweep cannot read, rather than dispatching."""
    assert runner.sweep(state=None, pr_updated_at="not-a-date") == []


# ── Truncation: the oldest PRs must never be dropped silently ────────────────


def test_the_open_pr_scan_has_no_silent_ceiling(script: str) -> None:
    """The oldest open PRs must not be droppable at all.

    `gh pr list` returned newest-first and truncated SILENTLY at `--limit`, so a
    ceiling that fell behind the real open-PR count dropped the OLDEST PRs --
    precisely the frozen ones this sweep exists to rescue -- and the guard against
    it was a warning nobody could act on until it had already happened. Cursor
    paging removes the ceiling instead of sizing it, so the property is now
    structural: there is no limit left to outgrow, and none may come back.
    """
    # Comments are stripped first: the block SHOULD still explain what `gh pr
    # list` did and why the ceiling was dangerous. What must not survive is the
    # executable form of it.
    code = "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))
    assert "gh pr list" not in code
    assert "PR_LIST_LIMIT" not in code
    assert "--limit" not in code
    assert "readiness_sweep_scan.py" in code


def test_every_open_pull_request_is_scanned_across_pages(tmp_path: Path, script: str) -> None:
    """Nothing past the first page is dropped.

    The scan pages 25 pull requests at a time -- measured, not tidy: with each
    PR's rollup contexts attached, 100 and 50 per page both answered HTTP 504.
    Here 30 PRs are open and all five stale ones live on the SECOND page, so a
    scan that stopped after one page would rescue none of them and report a
    plausible-looking 25.
    """
    from datetime import datetime, timedelta, timezone

    fixtures = tmp_path / "fixtures"
    work = tmp_path / "work"
    bindir = tmp_path / "bin"
    for d in (fixtures, work, bindir):
        d.mkdir(parents=True)
    stub = bindir / "gh"
    stub.write_text(GH_STUB)
    stub.chmod(0o755)
    install_scanner(fixtures, work)

    fresh = (datetime.now(timezone.utc) - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    prs = []
    for index in range(30):
        sha = f"sha{index:02d}"
        prs.append(
            {
                "number": 100 + index,
                "headRefOid": sha,
                "updatedAt": "2020-01-01T00:00:00Z",
            }
        )
        at = "2020-01-01T00:00:00Z" if index >= 25 else fresh
        (fixtures / f"status_{sha}.json").write_text(
            json.dumps([[{"context": "PR Readiness", "state": "pending", "updated_at": at}]])
        )
    (fixtures / "prs.json").write_text(json.dumps(prs))

    proc = subprocess.run(  # noqa: S603 - fixed argv, test-local stub
        ["bash", "-c", script],
        cwd=work,
        env={
            **os.environ,
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "FIXTURES": str(fixtures),
            "STUB_PYTHON": sys.executable,
            "REPO": "kirodotdev/KiroCrew",
            "STATUS_CONTEXT": "PR Readiness",
            "STALE_MINUTES": "15",
            "MAX_DISPATCH": "200",
        },
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "Scanning 30 open pull request(s)" in proc.stdout

    dispatched = (fixtures / "dispatched.txt").read_text().splitlines()
    numbers = sorted(
        int(token.split("=", 1)[1])
        for line in dispatched
        for token in line.split()
        if token.startswith("pr=")
    )
    assert numbers == [125, 126, 127, 128, 129]


def test_a_different_status_context_never_drives_the_decision(runner: Runner) -> None:
    """Only the aggregate this sweep owns may be read.

    The SHA carries a FRESH `PR Readiness` pending (nothing to rescue) alongside a
    long-stale failing `Coverage Gate`. A sweep that matched on the wrong context
    would read the Coverage Gate failure, see later check evidence, and dispatch.
    """
    from datetime import datetime, timedelta, timezone

    recent = (datetime.now(timezone.utc) - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
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

    This is the same shape generalised: what makes the verdict absent is that no
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

# Per-SHA readiness statuses need no second stub: the translator prefers a
# `status_<sha>.json` fixture over the shared `statuses.json` when one exists,
# which is what lets one sweep hold several PRs frozen for different lengths of
# time -- the exact thing the ordering test must vary.


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
    stub.write_text(GH_STUB)
    stub.chmod(0o755)
    install_scanner(fixtures, work)

    # PRs as `gh pr list` returns them (newest-numbered first), each frozen for a
    # DIFFERENT length of time. Staleness order (oldest first) is 3120, 3400, 3612
    # -- the opposite of the list order for the newest entry.
    prs = [
        {"number": 3612, "headRefOid": "aaa"},
        {"number": 3120, "headRefOid": "bbb"},
        {"number": 3400, "headRefOid": "ccc"},
    ]
    # No `updatedAt`: these are `pending` verdicts, so neither the unpublished arm
    # nor mode 5's gate reads it, and leaving it out keeps the fixture about the
    # one thing this test varies.
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
            "STUB_PYTHON": sys.executable,
            "REPO": "kirodotdev/KiroCrew",
            "STATUS_CONTEXT": "PR Readiness",
            "STALE_MINUTES": "15",
            "MAX_DISPATCH": "200",
        },
        text=True,
        encoding="utf-8",
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


def test_a_failed_read_is_not_treated_as_unpublished(runner: Runner) -> None:
    """A GraphQL failure and a genuinely absent verdict must not read alike.

    Conflating them would turn transient GitHub trouble into a spurious re-fire of
    an arbitrary old PR -- on a shared token budget, at 15-minute intervals, on
    every PR at once. The scan enforces the distinction by SHAPE rather than by an
    ordering rule in the shell: a PR it could not read is absent from its output
    entirely, so the unpublished arm never sees one. It also exits 0 with a
    warning rather than failing, because a sweep that runs on the pages it did
    read still rescues those PRs, and the next sweep retries the rest.
    """
    dispatched = runner.sweep(
        state=None, pr_updated_at="2026-08-17T14:20:00Z", graphql_read_fails=True
    )
    assert dispatched == []
    assert "Scanning 0 open pull request(s)" in runner.last_stdout
    assert "::warning::" in runner.last_stderr
    assert "the walk ends here" in runner.last_stderr


# ── The disposition-comment freeze (the verdict depends on comment bytes) ─


def test_failure_with_a_later_disposition_edit_is_refired(runner: Runner) -> None:
    """A disposition-rule violation fails readiness, so the verdict
    depends on comment bytes -- and the aggregator has no `issue_comment`
    trigger. Correcting the comment produces no event and no check-run, so
    without this mode the red freezes on an unchanged commit."""
    dispatched = runner.sweep(
        state="failure",
        status_at="2026-08-30T19:01:24Z",
        check_completed_at="2026-08-30T18:55:00Z",
        disposition_at="2026-08-30T19:20:00Z",
    )
    assert len(dispatched) == 1
    assert "pr=2064" in dispatched[0]
    assert "disposition record changed later" in runner.last_stdout


def test_failure_with_an_older_disposition_is_left_alone(runner: Runner) -> None:
    """The writer read the listing and ruled BEFORE the verdict -- that is the
    ordinary case, and the red is current, not stale. Re-firing here would
    dispatch every genuinely-violating PR every 15 minutes forever."""
    assert (
        runner.sweep(
            state="failure",
            status_at="2026-08-30T19:01:24Z",
            check_completed_at="2026-08-30T18:55:00Z",
            disposition_at="2026-08-30T18:40:00Z",
            pr_updated_at="2026-08-30T19:30:00Z",
        )
        == []
    )


def test_disposition_refire_is_self_terminating(runner: Runner) -> None:
    """After the nudge republishes, readiness is the newest timestamp again, so
    the same comment edit is never counted twice -- the property that makes this
    safe to run on a schedule."""
    assert (
        runner.sweep(
            state="failure",
            status_at="2026-08-30T19:25:00Z",
            check_completed_at="2026-08-30T18:55:00Z",
            disposition_at="2026-08-30T19:20:00Z",
            pr_updated_at="2026-08-30T19:30:00Z",
        )
        == []
    )


def test_a_disposition_edit_in_the_same_second_is_not_new_evidence(runner: Runner) -> None:
    assert (
        runner.sweep(
            state="failure",
            status_at="2026-08-30T19:01:24Z",
            check_completed_at="2026-08-30T18:55:00Z",
            disposition_at="2026-08-30T19:01:24Z",
            pr_updated_at="2026-08-30T19:30:00Z",
        )
        == []
    )


def test_a_later_ordinary_comment_is_not_disposition_evidence(runner: Runner) -> None:
    """Only disposition-marked comments are evidence. A review bot rewriting its
    comment in place, or any human reply, must not nudge a legitimately red PR --
    that is what would turn this into a per-sweep re-fire."""
    assert (
        runner.sweep(
            state="failure",
            status_at="2026-08-30T19:01:24Z",
            check_completed_at="2026-08-30T18:55:00Z",
            other_comments=[
                {
                    "body": "<!-- codex-ai-review -->\nGPT 5.6 Review",
                    "updated_at": "2026-08-30T19:40:00Z",
                }
            ],
        )
        == []
    )


def test_later_check_evidence_still_wins_without_reading_comments(runner: Runner) -> None:
    """Mode 2 is unchanged and still reports its own reason: a PR with later
    check evidence must not be re-attributed to the comment path."""
    dispatched = runner.sweep(
        state="failure",
        status_at="2026-08-30T19:01:24Z",
        check_completed_at="2026-08-30T19:16:13Z",
    )
    assert len(dispatched) == 1
    assert "a check completed later" in runner.last_stdout
    assert "disposition record changed later" not in runner.last_stdout


def test_failure_with_no_checks_and_a_later_disposition_is_still_refired(
    runner: Runner,
) -> None:
    """A PR whose head carries no completed check-run at all is not skipped
    outright by the failure arm. The comment path must still be reachable for
    it, since a disposition violation can be the ONLY reason readiness is red."""
    dispatched = runner.sweep(
        state="failure",
        status_at="2026-08-30T19:01:24Z",
        check_completed_at=None,
        disposition_at="2026-08-30T19:20:00Z",
    )
    assert len(dispatched) == 1
    assert "disposition record changed later" in runner.last_stdout


def test_green_verdict_with_a_later_disposition_is_refired(runner: Runner) -> None:
    """The PERMITTING direction, and the half that matters: a writer can post a
    rule-violating record AFTER readiness published success. The record carries
    ledger downgrade power immediately, the revision now violates the rule, and
    no event re-evaluates it -- so the required status stays green over a
    revision that should be red, which permits a merge."""
    dispatched = runner.sweep(
        state="success",
        status_at="2026-08-30T19:01:24Z",
        check_completed_at="2026-08-30T18:55:00Z",
        disposition_at="2026-08-30T19:20:00Z",
    )
    assert len(dispatched) == 1
    assert "disposition record changed later" in runner.last_stdout


def test_green_verdict_with_an_older_disposition_is_left_alone(runner: Runner) -> None:
    assert (
        runner.sweep(
            state="success",
            status_at="2026-08-30T19:01:24Z",
            check_completed_at="2026-08-30T18:55:00Z",
            disposition_at="2026-08-30T18:30:00Z",
            pr_updated_at="2026-08-30T19:30:00Z",
        )
        == []
    )


def test_green_disposition_refire_is_self_terminating(runner: Runner) -> None:
    assert (
        runner.sweep(
            state="success",
            status_at="2026-08-30T19:25:00Z",
            check_completed_at="2026-08-30T18:55:00Z",
            disposition_at="2026-08-30T19:20:00Z",
            pr_updated_at="2026-08-30T19:30:00Z",
        )
        == []
    )


def test_a_later_bot_comment_never_refires_a_green_verdict(runner: Runner) -> None:
    """The review bots rewrite their comments in place on every push. If those
    counted, this would re-fire most green PRs on every sweep."""
    assert (
        runner.sweep(
            state="success",
            status_at="2026-08-30T19:01:24Z",
            other_comments=[
                {
                    "body": "<!-- design-review -->\nDesign Review",
                    "updated_at": "2026-08-30T19:40:00Z",
                }
            ],
        )
        == []
    )


def test_failing_check_evidence_still_wins_on_a_green_verdict(runner: Runner) -> None:
    dispatched = runner.sweep(
        state="success",
        status_at="2026-08-30T19:01:24Z",
        check_completed_at="2026-08-30T19:16:13Z",
        check_conclusion="failure",
    )
    assert len(dispatched) == 1
    assert "a check FAILED later" in runner.last_stdout
    assert "disposition record changed later" not in runner.last_stdout


def test_a_disposition_three_seconds_after_a_red_verdict_is_evidence(runner: Runner) -> None:
    """No margin on comment evidence. The check-evidence modes tolerate 5s so a
    concurrently-completing check is not read as new; `-le` already excludes the
    same second, so a margin here only created a 1-5s window in which a record
    could be posted and then never re-examined -- nothing else observes
    comments, so that window was permanent."""
    dispatched = runner.sweep(
        state="failure",
        status_at="2026-08-30T19:01:24Z",
        check_completed_at="2026-08-30T18:55:00Z",
        disposition_at="2026-08-30T19:01:27Z",
    )
    assert len(dispatched) == 1


def test_a_disposition_three_seconds_after_a_green_verdict_is_evidence(
    runner: Runner,
) -> None:
    dispatched = runner.sweep(
        state="success",
        status_at="2026-08-30T19:01:24Z",
        disposition_at="2026-08-30T19:01:27Z",
    )
    assert len(dispatched) == 1


def test_a_same_second_disposition_still_terminates_the_loop(runner: Runner) -> None:
    """The property the margin was there to protect, which strict `-le` already
    gives: a record stamped in the same second as the publish is not evidence, so
    a republish cannot re-fire on the record it just answered."""
    assert (
        runner.sweep(
            state="success",
            status_at="2026-08-30T19:01:24Z",
            disposition_at="2026-08-30T19:01:24Z",
            pr_updated_at="2026-08-30T19:10:00Z",
        )
        == []
    )


def test_comments_are_not_read_when_the_pr_is_untouched_since_the_verdict(
    runner: Runner,
) -> None:
    """The one read left on the shared REST pool is skipped when it cannot find
    anything.

    Creating OR editing a comment bumps the pull request's `updatedAt`, so an
    `updatedAt` no newer than the verdict proves no disposition record moved after
    it. The fixture here is deliberately impossible -- a record stamped 19:20 on a
    PR last touched 19:00 -- because that is what makes the assertion sharp: the
    read is not merely fruitless, it never happens, and no dispatch count could
    tell those two apart.
    """
    assert (
        runner.sweep(
            state="failure",
            status_at="2026-08-30T19:01:24Z",
            check_completed_at="2026-08-30T18:55:00Z",
            disposition_at="2026-08-30T19:20:00Z",
            pr_updated_at="2026-08-30T19:00:00Z",
        )
        == []
    )
    assert not runner.comments_read.exists()
