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
import re
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

# `gh` stub. Four shapes are served, keyed on the subcommand:
#   api graphql             -> the fixture repository state, translated (see below)
#   api .../comments        -> the fixture issue comments, and the read is RECORDED
#   api .../status          -> the fixture commit statuses WITH descriptions, through
#                              real jq so the workflow's own filter is exercised; the
#                              read is RECORDED, and a marker file makes it fail
#   workflow run            -> RECORD the dispatch instead of firing it
#
# The comment read is recorded because mode 5 is now GATED on the PR's own
# `updatedAt`, and "this read did not happen" is the whole assertion of one test:
# a dispatch count cannot distinguish a read that found nothing from a read that
# was correctly skipped.
#
# The status read is recorded for the mirror reason on mode 1: it is made only for
# a pending the evidence test would otherwise skip, so "no read happened" is how a
# test proves a pending with later check evidence never pays for it.
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
    *"/status")
      printf '%s\n' "$*" >> "$FIXTURES/status_read.txt"
      if [ -f "$FIXTURES/status_read_fails" ]; then exit 1; fi
      filter=""
      want=0
      for arg in "$@"; do
        if [ "$want" = 1 ]; then filter="$arg"; want=0; continue; fi
        if [ "$arg" = "--jq" ]; then want=1; fi
      done
      if [ -n "$filter" ]; then
        jq -r "$filter" < "$FIXTURES/status_payload.json"
      else
        cat "$FIXTURES/status_payload.json"
      fi
      exit 0 ;;
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
        description: str = "11 readiness check(s) still pending; waiting on CI (not started)",
        status_read_fails: bool = False,
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

        `description` is the readiness status's own description, which only the
        REST endpoint carries. It defaults to an ordinary lane-pending sentence,
        so a test opts INTO the read-failure shape rather than out of
        it. `status_read_fails=True` makes that read fail, to pin which way mode 1
        degrades when it cannot classify a pending.
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
        # The REST `/commits/<sha>/status` payload mode 1 reads to tell a
        # read-failure pending from a lane-pending. The GraphQL scan
        # carries no description, so this endpoint is the only place one exists.
        (self.fixtures / "status_payload.json").write_text(
            json.dumps(
                {
                    "statuses": [
                        {"context": context, "state": state or "", "description": description}
                    ]
                    + (extra_statuses or [])
                }
            )
        )
        fails = self.fixtures / "status_read_fails"
        if status_read_fails:
            fails.write_text("")
        else:
            fails.unlink(missing_ok=True)
        applied = self.fixtures / "dispatched.txt"
        applied.unlink(missing_ok=True)
        self.comments_read = self.fixtures / "comments_read.txt"
        self.comments_read.unlink(missing_ok=True)
        self.status_read = self.fixtures / "status_read.txt"
        self.status_read.unlink(missing_ok=True)

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
    """The dropped-event freeze: a lane finished after the pending was published.

    The fixture carries the check evidence that makes it that shape: the verdict
    is old and a lane completed later, so the recompute reads something the frozen
    verdict never saw.
    """
    dispatched = runner.sweep(
        state="pending",
        status_at="2020-01-01T00:00:00Z",
        check_completed_at="2020-01-01T00:30:00Z",
    )
    assert len(dispatched) == 1
    assert "pr=2064" in dispatched[0]
    assert "sha=4328fd0f941f09ff10f245fbdb4accf7c246febe" in dispatched[0]


def test_stale_pending_with_no_later_check_evidence_is_left_alone(runner: Runner) -> None:
    """A pending newer than every check on its head cannot change, so it is not nudged.

    A readiness lane added to the monitored list after a head was pushed has zero
    runs on that immutable head and can never acquire one, so the aggregator
    counts it "(not started)" and republishes the same pending for as long as the
    pull request stays open. Age alone made the sweep re-fire that recompute every
    cycle, and each recompute re-derived the identical verdict, so the nudge
    burned Actions minutes on 22 pull requests and changed nothing.

    The test is the one modes 2, 4 and 5 already use, read in the pending
    direction: the verdict here is NEWER than the newest completed check, so no
    evidence has landed since it was computed and a recompute has nothing new to
    read. Self-terminating rather than permanent -- the moment any lane completes
    after the verdict the condition below flips and the nudge resumes.
    """
    assert (
        runner.sweep(
            state="pending",
            status_at="2026-08-07T19:16:13Z",
            check_completed_at="2026-08-07T19:01:24Z",
        )
        == []
    )


def test_a_pending_with_no_checks_at_all_is_left_alone(runner: Runner) -> None:
    """Zero completed checks is not evidence of a freeze.

    A head whose lanes are all still queueing carries no completed check-run, and
    its pending is honest: the completion events are still owed and each one
    recomputes. Nudging here cannot help, because the recompute reads the same
    empty evidence the verdict already read.
    """
    assert runner.sweep(state="pending", status_at="2020-01-01T00:00:00Z") == []


def test_fresh_pending_is_left_alone(runner: Runner) -> None:
    """Inside STALE_MINUTES the fan-out may genuinely still be running."""
    from datetime import datetime, timedelta, timezone

    recent = (datetime.now(timezone.utc) - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert runner.sweep(state="pending", status_at=recent) == []


# ── The evaluate-to-publish window ──────────────────────────────────────────

PUBLISH_LAG_SECONDS = 300


def test_a_check_completed_inside_the_publish_lag_is_evidence(runner: Runner) -> None:
    """A lane the verdict could not have seen still counts, though it pre-dates it.

    The aggregator reads lane state, then finishes its other reads and writes its
    summary before publishing, so its view is older than its publish stamp. A
    lane completing in that gap is invisible to the verdict; if its own
    completion event is then dropped, comparing against the stamp alone calls the
    check old and nothing ever recomputes. The verdict here is published two
    minutes after the check, inside the bound.
    """
    dispatched = runner.sweep(
        state="pending",
        status_at="2026-08-07T19:16:13Z",
        check_completed_at="2026-08-07T19:14:13Z",
    )
    assert len(dispatched) == 1
    assert "pr=2064" in dispatched[0]


def test_a_check_one_second_inside_the_publish_lag_is_evidence(runner: Runner) -> None:
    """The near edge, to pin the bound's value and not merely its existence."""
    assert (
        len(
            runner.sweep(
                state="pending",
                status_at="2026-08-07T19:16:13Z",
                check_completed_at="2026-08-07T19:11:14Z",
            )
        )
        == 1
    )


def test_a_check_at_the_publish_lag_floor_is_not_evidence(runner: Runner) -> None:
    """The far edge. The window is BOUNDED, which is what makes mode 1 terminate.

    A check exactly publish_lag_seconds before the verdict is old evidence: the
    aggregator's gap cannot have been that wide, so the verdict did read it.
    Without this edge the floor would drift toward "any check at all", which is
    the age-only retry the evidence test replaces.
    """
    assert (
        runner.sweep(
            state="pending",
            status_at="2026-08-07T19:16:13Z",
            check_completed_at="2026-08-07T19:11:13Z",
        )
        == []
    )


def test_a_republished_verdict_carries_the_same_check_below_the_floor(
    runner: Runner,
) -> None:
    """Self-termination, as the sweep actually reaches it.

    A rescue dispatches the aggregator, which republishes. The next sweep sees
    the SAME newest check against a publication that is now at least
    stale_seconds newer -- because nothing is re-examined before then -- and
    stale_seconds is larger than the lag, so the check is below the floor and the
    nudge does not repeat. This models the second pass: the check that earned the
    first rescue, one stale window later.
    """
    assert (
        runner.sweep(
            state="pending",
            status_at="2026-08-07T19:31:14Z",
            check_completed_at="2026-08-07T19:14:13Z",
        )
        == []
    )


def test_the_publish_lag_stays_below_the_staleness_window() -> None:
    """The ordering the termination argument rests on, asserted against the file.

    If the lag ever grew past stale_seconds, a rescue's own republish would keep
    the check inside the new window and mode 1 would nudge the same head every
    sweep -- the loop this whole change removes, reintroduced by a constant.
    """
    sweep = WORKFLOW.read_text(encoding="utf-8")
    assert "publish_lag_seconds=%d" % PUBLISH_LAG_SECONDS in sweep
    assert "$(( updated_epoch - publish_lag_seconds ))" in sweep
    stale = re.search(r'STALE_MINUTES:\s*"(\d+)"', sweep)
    assert stale, "STALE_MINUTES is unreadable, so the termination bound cannot be checked"
    assert PUBLISH_LAG_SECONDS < int(stale.group(1)) * 60


# ── The read-failure pending: age is its only signal ────────────────────────

READ_FAILURE_TOKEN = "[read-failed]"

TRANSPORT_DESCRIPTION = (
    READ_FAILURE_TOKEN + " Readiness could not be evaluated"
    " (transient GitHub API failure); it will be re-evaluated"
)

# The SECOND read-failure site's description. It shares the token and nothing
# else: the prose is the publisher's ordinary pending sentence, because this
# pending is one entry in the waiting list rather than a whole-verdict bail-out.
DISPOSITION_DESCRIPTION = (
    READ_FAILURE_TOKEN + " 1 readiness check(s) still pending;"
    " waiting on disposition records could not be read"
)

# Every phrase the publisher uses for a pending it publishes because a READ
# failed. Each such site must stamp the token; a site that does not is a pending
# the sweep will strand at the evidence test forever.
READ_FAILURE_VOCABULARY = re.compile(
    r"could not be (?:read|evaluated|established)|unreadable", re.IGNORECASE
)


def test_a_stale_transport_read_failure_pending_is_refired_without_later_evidence(
    runner: Runner,
) -> None:
    """The pending shape that keeps its age-based retry.

    pr-readiness.yml publishes this verdict when a read-only call keeps failing
    after its bounded retries, at the END of the job -- so it can post-date every
    check on the head and hold no later check evidence by construction. The
    evidence test can therefore never fire it, while a recompute resolves it
    outright, because the next run's reads succeed. Without the carve-out the
    required status stays pending with nothing able to clear it but a push.

    The fixture is deliberately the SAME shape the lane-pending test asserts is
    left alone -- verdict at 19:16:13Z, newest completed check at 19:01:24Z -- so
    the description is the only thing that differs and the only thing that can
    explain the opposite outcome.
    """
    dispatched = runner.sweep(
        state="pending",
        status_at="2026-08-07T19:16:13Z",
        check_completed_at="2026-08-07T19:01:24Z",
        description=TRANSPORT_DESCRIPTION,
    )
    assert len(dispatched) == 1
    assert "pr=2064" in dispatched[0]
    assert "sha=4328fd0f941f09ff10f245fbdb4accf7c246febe" in dispatched[0]


def test_a_read_failure_pending_is_refired_whatever_its_prose_says(
    runner: Runner,
) -> None:
    """The discriminator is the token, so a differently-worded site is rescued too.

    The publisher writes a read-failure pending from more than one site, and only
    one of them phrases it as "Readiness could not be evaluated": an unreadable
    disposition record set is one entry in the waiting list, so it arrives under
    the ordinary "N readiness check(s) still pending" sentence. Matching that
    prose recognises the first site and strands the second at exactly the freeze
    this carve-out exists to prevent. Same fixture as the transport case, with
    only the prose changed.
    """
    dispatched = runner.sweep(
        state="pending",
        status_at="2026-08-07T19:16:13Z",
        check_completed_at="2026-08-07T19:01:24Z",
        description=DISPOSITION_DESCRIPTION,
    )
    assert len(dispatched) == 1
    assert "pr=2064" in dispatched[0]


@pytest.mark.parametrize(
    "description",
    [
        READ_FAILURE_TOKEN + " Readiness could not be evalu",
        READ_FAILURE_TOKEN + " 12 readiness check(s) still p",
        READ_FAILURE_TOKEN,
    ],
    ids=["transport", "disposition", "token-only"],
)
def test_the_token_survives_the_description_length_cap(runner: Runner, description: str) -> None:
    """A truncated description still classifies, because the token leads it.

    A commit status description is capped at 140 characters and the tail is what
    gets cut, so the publisher puts the token first and the sweep matches the
    front. These fixtures keep only what a cap would leave behind.
    """
    assert (
        len(
            runner.sweep(
                state="pending",
                status_at="2026-08-07T19:16:13Z",
                check_completed_at="2026-08-07T19:01:24Z",
                description=description,
            )
        )
        == 1
    )


def test_the_publisher_and_the_sweep_agree_on_the_read_failure_token() -> None:
    """Pin the literal, so editing either file cannot silently strand the retry.

    The sweep classifies on a token another workflow writes. Nothing in either
    file's own tests would notice the two drifting apart, and the cost of drift
    is the exact freeze this carve-out exists to prevent.
    """
    publisher = (REPO_ROOT / ".github" / "workflows" / "pr-readiness.yml").read_text(
        encoding="utf-8"
    )
    sweep = WORKFLOW.read_text(encoding="utf-8")
    declaration = 'READ_FAILURE_TOKEN="%s"' % READ_FAILURE_TOKEN
    assert declaration in sweep
    assert declaration in publisher
    assert '"$READ_FAILURE_TOKEN"*) return 0 ;;' in sweep


def test_every_read_failure_pending_the_publisher_writes_is_stamped() -> None:
    """A new read-failure site cannot ship unstamped.

    This is the failure mode a prose match hides: someone adds a third pending
    for a read that failed, words it their own way, and the sweep -- which can
    only see the token -- leaves it frozen. Nothing at runtime complains, because
    the verdict is a legitimate pending; it simply never clears.

    So the publisher is read as text. Every `pending+=` whose subject is a failed
    read must set the flag that stamps the token, and the whole-verdict bail-out
    must carry the token itself, first, ahead of its prose.
    """
    publisher = (REPO_ROOT / ".github" / "workflows" / "pr-readiness.yml").read_text(
        encoding="utf-8"
    )
    lines = publisher.splitlines()

    sites = [
        (number, line)
        for number, line in enumerate(lines)
        if "pending+=(" in line and READ_FAILURE_VOCABULARY.search(line)
    ]
    assert sites, "no read-failure pending site found; the vocabulary has drifted"
    for number, line in sites:
        window = "\n".join(lines[number : number + 3])
        unstamped = "line %d publishes a read-failure pending unstamped: %s" % (
            number + 1,
            line.strip(),
        )
        assert "read_failure=true" in window, unstamped

    assert 'echo "description=$READ_FAILURE_TOKEN Readiness could not be evaluated' in publisher
    assert 'prefix="$READ_FAILURE_TOKEN "' in publisher


def test_an_ordinary_lane_pending_still_pays_no_status_read(runner: Runner) -> None:
    """The classifying read is made only where it can change the outcome.

    A pending with later check evidence is re-fired by the evidence test itself,
    so it never reaches the read. Asserting the recorder stays absent is the only
    way to prove that: a dispatch count cannot tell a read that happened from one
    that was skipped.
    """
    assert (
        len(
            runner.sweep(
                state="pending",
                status_at="2020-01-01T00:00:00Z",
                check_completed_at="2020-01-01T00:30:00Z",
            )
        )
        == 1
    )
    assert not runner.status_read.exists()


def test_an_unreadable_description_leaves_the_pending_alone(runner: Runner) -> None:
    """The classifier fails CLOSED toward the evidence test.

    When the read breaks, the sweep cannot tell a read-failure pending from a
    lane-pending. Treating an unknown as a read failure would re-fire every
    ordinary lane-pending whenever the endpoint was unwell, which is the loop the
    evidence test removes; treating it as ordinary costs one sweep interval of
    delay on a rescue that the next sweep makes.
    """
    assert (
        runner.sweep(
            state="pending",
            status_at="2026-08-07T19:16:13Z",
            check_completed_at="2026-08-07T19:01:24Z",
            description=TRANSPORT_DESCRIPTION,
            status_read_fails=True,
        )
        == []
    )


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
    # Mode 1 needs a check that completed AFTER the verdict, so the stale PRs are
    # rescuable at all. This fixture is global to every PR the stub serves, and
    # that is harmless here: it post-dates the five stale verdicts and pre-dates
    # the fresh ones, which the age gate excludes before evidence is read.
    (fixtures / "check_runs.json").write_text(
        json.dumps(
            [
                {
                    "check_runs": [
                        {
                            "status": "completed",
                            "conclusion": "success",
                            "completed_at": "2020-06-01T00:00:00Z",
                        }
                    ]
                }
            ]
        )
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
    # One check completed after all three verdicts, so each is a mode 1 rescue and
    # the test varies only the staleness order.
    (fixtures / "check_runs.json").write_text(
        json.dumps(
            [
                {
                    "check_runs": [
                        {
                            "status": "completed",
                            "conclusion": "success",
                            "completed_at": "2020-01-01T00:01:00Z",
                        }
                    ]
                }
            ]
        )
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
