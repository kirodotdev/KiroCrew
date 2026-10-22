"""Shared sweep harness for ``test_pr_readiness_sweep.py``.

Moved here verbatim so the test module stays under the file line cap: the
``gh`` and GraphQL stubs, the extraction of the sweep's one ``run:`` block,
the scanner install and the ``Runner`` that executes the block against one
fixture repository state. The module-level skipif and the ``script`` and
``runner`` fixtures stay in the test module, so its collection and skip
behaviour are unchanged.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "pr-readiness-sweep.yml"
SCANNER = REPO_ROOT / ".github" / "scripts" / "readiness_sweep_scan.py"
REPUBLISHER = REPO_ROOT / ".github" / "scripts" / "republish_withheld_verdict.py"


def _gnu_date() -> bool:
    """GNU `date -d` is required; BSD date uses -j -f and would silently differ."""
    return (
        subprocess.run(
            ["date", "-u", "-d", "2026-01-01T00:00:00Z", "+%s"],
            capture_output=True,
        ).returncode
        == 0
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
    *"/actions/workflows/"*)
      # Active workflow_dispatch runs for pr-readiness.yml; display_title
      # is set by pr-readiness.yml run-name and carries pr+sha.
      if [ -f "$FIXTURES/active_wd_runs.json" ]; then
        cat "$FIXTURES/active_wd_runs.json"
      else
        printf '{"workflow_runs":[]}'
      fi; exit 0 ;;
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
            # Review submissions and dismissals, as `[{"submittedAt": ...}]`.
            "timelineItems": {
                "nodes": _read("reviews_timeline.json", []),
            },
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


def install_scanner(fixtures: Path, work: Path) -> None:
    """Make the run block's `.github/scripts/...` path resolve inside `work`.

    The real scanner is copied in, not reimplemented: the sweep's decisions now
    depend on the JSON that script emits, so a fake would leave the two free to
    disagree exactly where a wrong field name or a missed page hides.
    """
    scripts = work / ".github" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / SCANNER.name).write_text(SCANNER.read_text(encoding="utf-8"), encoding="utf-8")
    # The withheld-verdict republisher the sweep invokes for a
    # verdict-unpublished pending. Copied in so the invocation path resolves;
    # the GH_STUB answers its read-only calls and records its rerun as a
    # dispatch, so a routing test sees whether the sweep reached it. Its own
    # logic is unit-tested in test_republish_withheld_verdict.py.
    (scripts / REPUBLISHER.name).write_text(
        REPUBLISHER.read_text(encoding="utf-8"), encoding="utf-8"
    )
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
        review_at: str | None = None,
        active_wd_runs: list[dict] | None = None,
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

        `review_at` is the newest human review submission or dismissal.

        `active_wd_runs` is a list of dicts each with keys ``id`` (int),
        ``pr`` (str), ``sha`` (str) and ``status`` (``queued`` or
        ``in_progress``). The stub serves them as existing workflow_dispatch
        runs so the sweep's skip set is exercised. ``None`` means no active
        runs (the normal case), which the stub answers with an empty list.
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
        reviews = [] if review_at is None else [{"submittedAt": review_at}]
        (self.fixtures / "reviews_timeline.json").write_text(json.dumps(reviews))
        if review_at is not None:
            pr_updated_at = max(pr_updated_at, review_at)
        # Fixtures for the active-dispatch skip set. display_title mirrors
        # pr-readiness.yml run-name: "PR-wd-{pr}-{sha}" for dispatch events.
        if active_wd_runs:
            run_list = [
                {
                    "id": r["id"],
                    "status": r["status"],
                    "event": "workflow_dispatch",
                    "display_title": f"PR-wd-{r['pr']}-{r['sha']}",
                }
                for r in active_wd_runs
            ]
            (self.fixtures / "active_wd_runs.json").write_text(
                json.dumps([{"workflow_runs": run_list}])
            )
        else:
            (self.fixtures / "active_wd_runs.json").unlink(missing_ok=True)
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
