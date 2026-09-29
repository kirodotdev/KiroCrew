#!/usr/bin/env python3
"""Scan every open pull request for pr-readiness-sweep.yml, over GraphQL.

The sweep needs three facts per open pull request: its head SHA, the state and
timestamp of the "PR Readiness" commit status on that SHA, and the newest
completed / newest failure-class check-run on it. Read over REST that is one
`/statuses` call plus one or two `/check-runs` calls PER pull request -- about
1500-2500 requests per sweep at 500 open PRs, four sweeps an hour, on the
installation REST pool that every workflow in this repository shares. On
2026-09-15 that pool was exhausted: 36 `PR Readiness` runs and every AI review
lane failed closed, and this sweep's own log read `statuses lookup failed;
skipping` for pull request after pull request -- the backstop taken out by its
own cost, exactly when a frozen status could not be corrected by anything else.

GraphQL bills against a SEPARATE pool, and one query carries all three facts for
a page of pull requests. So the whole scan is a handful of requests and the
sweep no longer competes with the lanes it exists to unfreeze.

Page size starts at 25 and that number is measured, not chosen for tidiness:
asking for each pull request's `statusCheckRollup.contexts(first: 100)`
alongside the status, `first: 100` and `first: 50` both answered HTTP 504,
`first: 30` took 10s, and `first: 25` took 4s and was stable at roughly 10
rate-limit points per page.

Page cost is content-dependent, so a page that times out at 25 can time out at
25 on every sweep, and ending the walk there would leave every pull request
older than that page unscanned for good. A page that fails its three attempts
is therefore retried at the SAME cursor with a smaller size, halving 25 -> 12 ->
6 -> 3 -> 1. The three attempts at the original size rule out a transient, so
each smaller size is probed once. Once a size succeeds the walk continues from
that page's `endCursor` AT THAT SIZE for the rest of the sweep -- the size is
never grown back within a sweep, the next sweep starts at 25 again. That keeps
the request count bounded: each size is given up at most once per sweep, so the
fallback adds at most 15 failed requests (3 attempts at 25 plus 3 at each size
it later fails at, or one probe each when the whole cascade fails at once), and
in the worst case -- a sweep forced down to size 1 on its first page -- one
request per remaining open pull request. GraphQL charges points by the nodes
asked for (one point a call at least), not by the request, so the smaller
pages cost about the same budget spread over more, slower calls.

Output is one JSON object per line on stdout, one per open pull request:

    {"number": 123, "sha": "<oid>", "pr_updated_at": "<ISO8601>",
     "readiness": {"state": "pending", "updated_at": "<ISO8601>"} | null,
     "newest_completed_check_at": "<ISO8601>" | null,
     "newest_failed_check_at": "<ISO8601>" | null,
     "checks_in_flight": 0,
     "checks_complete": true,
     "checks_requested": true}

`checks_in_flight` counts the check-runs on the head that have not completed
(queued, in progress, waiting). The workflow reads it to hold a recompute
while a head's lanes are still landing: readiness cannot leave `pending`
until every monitored lane has finished, so a recompute fired on each
completion in turn re-derives the same pending, once per lane. A page the
walk could not read leaves the count LOW, never high, so a non-zero count is
always real and a zero one is only trusted alongside `checks_complete`.

Two scopes (`--mode`). `full` is the walk described above: every open pull
request, rollup and all, 25 a page. `delivery` walks the same set on a LIGHT
page -- no rollup contexts, so 100 a page and a few seconds each -- and then
reads the rollup only for the pull requests that can be stale on check
evidence (`_is_candidate`), addressing those by number in aliased batches. A
pull request the delivery scan did not read evidence for is emitted with
`checks_requested: false`, which the workflow tells apart from evidence it asked
for and could not fully read (`checks_complete: false`): nothing is missing,
nothing is reported partial, and the classification decides it on the pull
request's own age or activity. Measured live: the heavy walk was 25 pages and
261 seconds at 625 open pull requests -- longer than the sweep's cadence; the
delivery scan read 117 candidates in 59.

A 200 body can carry `errors` next to usable `data`: GitHub nulls the one field
it could not resolve (one pull request's rollup timing out, say) and reports it
under `errors` with the field's path, and the rest of the page is intact. Such
a page is kept and its cursor followed. The pull requests the errors touched are
told apart by that path: one whose readiness status could not be read is LEFT
OUT of the output, because an emitted record with a null `readiness` would read
as "no verdict published" and re-fire that pull request on transient trouble;
one whose rollup could not be read is emitted with `checks_complete: false`, so
the evidence is marked partial rather than read as "no checks". Only a body
whose `data` lacks the connection being walked is a failed attempt.

Degradation is deliberate: when even a page of one pull request fails, the walk
ends with a `::warning::` (saying how many pull requests were emitted before
it) and exit 0, because a cursor is only obtainable from the page that failed.
The pull requests past it are simply not scanned this sweep and the next sweep
retries them -- the same trade the workflow's concurrency block already takes,
which is to run less completely rather than not at all. A stale verdict
survives 15 more minutes; a sweep that exits non-zero rescues nothing at all.

stdlib only: the workflow runs this straight off a sparse checkout with no
`pip install` step, and `gh` (already authenticated by `GH_TOKEN`) is the only
external command.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess  # noqa: S404 - fixed argv, no shell
import sys
import time
from datetime import datetime, timezone
from typing import Any

# Measured ceiling, not a guess. See the module docstring: 50 and 100 time out.
PR_PAGE_SIZE = 25
# The LIGHT page carries no rollup contexts, which is what made 100 time out, so
# it pages at the connection maximum. Measured against the live repository: 625
# open pull requests in 7 pages and 19 seconds, where the heavy walk took 25
# pages and 261.
LIGHT_PAGE_SIZE = 100
# How many pull requests one evidence read addresses by number. The same
# rollup-contexts weight the heavy page carries, so the same measured ceiling.
EVIDENCE_BATCH_SIZE = PR_PAGE_SIZE
# How long after its last activity a terminal verdict's pull request is still a
# delivery candidate (and, in the workflow, still worth a disposition-comment
# read). A disposition edit IS activity, so it bumps `updatedAt` to the edit
# time and every sweep inside this window sees it; the first one dispatches
# and the republished verdict is newer than the edit, so the ones after find
# nothing. Without the bound the comparison "moved since the verdict" was true
# for 478 of 566 terminal verdicts at once -- anything bumps `updatedAt`, a
# label, a bot comment, a review -- and each cost a paginated REST comment read
# every sweep, ~3 minutes and 500-900 requests of the shared pool per tick,
# for a mode that fires a handful of times a day. Six hours is 72 cadences and
# ten times the longest scheduler delay measured (35 minutes), so a sweep that
# never runs inside it has a bigger problem than a missed comment edit.
DEFAULT_ACTIVITY_WINDOW_SECONDS = 6 * 3600
# A GraphQL connection serves at most 100 nodes per request, and this
# repository's pull requests carry 65-98 rollup contexts today -- so a single
# pull request can already exceed one page, and `hasNextPage` must be followed.
CONTEXTS_PAGE_SIZE = 100

# The failure class, as GraphQL spells it. Semantically identical to the
# lower-case list the workflow's REST reader used
# (failure/timed_out/cancelled/action_required/stale/startup_failure); GraphQL
# enums are UPPER_CASE. test_readiness_sweep_scan.py compares the two
# case-insensitively so they cannot drift apart.
FAILURE_CONCLUSIONS = frozenset(
    {
        "FAILURE",
        "TIMED_OUT",
        "CANCELLED",
        "ACTION_REQUIRED",
        "STALE",
        "STARTUP_FAILURE",
    }
)

ATTEMPTS = 3
# Two waits for three attempts.
BACKOFF_SECONDS = (2.0, 4.0)

# One page of open pull requests with everything the sweep decides on.
#
# `commits(last: 1)` is the head commit. `status.contexts` is the COMMIT STATUS
# list, which is where "PR Readiness" lives and which GraphQL returns already
# collapsed to the latest status per context -- the same answer REST's
# newest-first `/statuses` first entry gave. The rollup is a different thing: it
# mixes check-runs in, and the readiness verdict must never be read from it.
#
# `$first` is the page size, 25 until a page fails (see the module docstring).
#
# UPDATED_AT DESC is the order `gh pr list` used, kept so the log reads the same
# way; the sweep sorts its own candidates by staleness afterwards, so the order
# here decides nothing.
PR_PAGE_QUERY = """
query($owner: String!, $name: String!, $first: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: $first, after: $cursor,
                 orderBy: {field: UPDATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        updatedAt
        headRefOid
        commits(last: 1) {
          nodes {
            commit {
              status { contexts { context state createdAt } }
              statusCheckRollup {
                contexts(first: 100) {
                  totalCount
                  pageInfo { hasNextPage endCursor }
                  nodes {
                    __typename
                    ... on CheckRun { status conclusion completedAt }
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""

# The LIGHT page of the delivery scan: everything the heavy page carries EXCEPT
# the rollup contexts, plus the rollup's one-word aggregate `state`. The
# contexts are what cost the heavy page its 10 seconds, and most open pull
# requests never need them read -- see `_is_candidate`. The aggregate state is
# cheap and is what tells a green verdict apart from a green verdict over a
# revision on which some check has since gone red.
LIGHT_PAGE_QUERY = """
query($owner: String!, $name: String!, $first: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: $first, after: $cursor,
                 orderBy: {field: UPDATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        updatedAt
        headRefOid
        commits(last: 1) {
          nodes {
            commit {
              status { contexts { context state createdAt } }
              statusCheckRollup { state }
            }
          }
        }
      }
    }
  }
}
"""

# One pull request, addressed by number, with the full heavy selection. The
# delivery scan aliases up to EVIDENCE_BATCH_SIZE of these into one request
# (`p0: pullRequest(number: 14547) {...} p1: ...`), which is how a handful of
# candidates are read with the rollup weight of a handful and not of the whole
# open set. The number is inlined rather than passed as a variable: it is an
# integer from GitHub's own response, and a per-alias variable list buys
# nothing over that. `status.contexts` is re-read here too, so a candidate is
# decided on the verdict as it stands NOW rather than as the light page saw
# it a few seconds earlier.
EVIDENCE_NODE = """
  p%d: pullRequest(number: %d) {
    number
    updatedAt
    headRefOid
    commits(last: 1) {
      nodes {
        commit {
          status { contexts { context state createdAt } }
          statusCheckRollup {
            contexts(first: 100) {
              totalCount
              pageInfo { hasNextPage endCursor }
              nodes {
                __typename
                ... on CheckRun { status conclusion completedAt }
              }
            }
          }
        }
      }
    }
  }"""


def evidence_query(numbers: list[int]) -> str:
    """The aliased evidence read for `numbers`, in order (alias index = list index)."""
    body = "".join(EVIDENCE_NODE % (index, number) for index, number in enumerate(numbers))
    return (
        "query($owner: String!, $name: String!) {\n"
        "  repository(owner: $owner, name: $name) {" + body + "\n  }\n}\n"
    )


# Rollup aggregate states that say some check on the revision is red. Read on
# the light page for a GREEN verdict only: a red verdict's rollup is red by
# construction, because the readiness status itself is one of the rollup's
# contexts, so the aggregate cannot tell a stale red from a current one.
RED_ROLLUP_STATES = frozenset({"FAILURE", "ERROR"})


# Remaining rollup pages for ONE commit, reached by oid. There is no way to
# resume a nested connection from the pull-request query, so the commit is
# re-addressed directly.
CONTEXTS_PAGE_QUERY = """
query($owner: String!, $name: String!, $oid: GitObjectID!, $after: String!) {
  repository(owner: $owner, name: $name) {
    object(oid: $oid) {
      ... on Commit {
        statusCheckRollup {
          contexts(first: 100, after: $after) {
            totalCount
            pageInfo { hasNextPage endCursor }
            nodes {
              __typename
              ... on CheckRun { status conclusion completedAt }
            }
          }
        }
      }
    }
  }
}
"""

# Where a field error's `path` enters the pull-request page: everything after
# the node index names the field that came back null.
PR_NODES_PATH = ("repository", "pullRequests", "nodes")
# Where an alias of the evidence read enters its document: `("repository",
# "p3", ...)`, the alias index being the candidate's position in the batch.
EVIDENCE_ALIAS_PATH = ("repository",)
EVIDENCE_ALIAS = re.compile(r"^p(\d+)$")


class Counters:
    """What the closing `::notice::` reports."""

    def __init__(self) -> None:
        self.pull_requests = 0
        self.pr_pages = 0
        self.light_pages = 0
        self.candidates = 0
        self.evidence_pages = 0
        self.context_pages = 0
        self.oversized = 0
        self.shrinks = 0
        self.left_out = 0
        # The page size the last walk ended at, shrinks included; a walk that
        # ends on a failed page never yields that size, so it is recorded here.
        self.ending_page_size = PR_PAGE_SIZE


def _graphql(
    query: str,
    variables: dict[str, str | int],
    *,
    required: tuple[str, ...],
    attempts: int = ATTEMPTS,
) -> tuple[dict[str, Any] | None, str]:
    """Run one `gh api graphql` call with retries; `(document, "")` or `(None, reason)`.

    A nullable variable that is absent from the map is null to the server, which
    is how the first page asks for no cursor. String values go over `-f` (raw
    string), never `-F`: `-F` applies magic type conversion, and a cursor that
    happened to be all digits would be sent as a number and rejected. Integer
    values (the page size, an `Int!`) go over `-F` for exactly that conversion.

    A body is usable when `data` carries every step of `required` non-null --
    the connection the caller is about to walk. It is returned even when the
    body also carries `errors` (field-level failures GitHub reports alongside
    the fields it nulled); the caller reads `document["errors"]` and marks what
    they touched. A body with no usable `data` is a failed attempt like a
    non-zero exit, and is retried.
    """
    argv = ["gh", "api", "graphql", "-f", f"query={query}"]
    for name, value in variables.items():
        argv += ["-F" if isinstance(value, int) else "-f", f"{name}={value}"]

    reason = "unknown failure"
    for attempt in range(attempts):
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv, capture_output=True, text=True
        )
        if proc.returncode != 0:
            reason = _first_line(proc.stderr) or f"gh exited {proc.returncode}"
        else:
            try:
                document = json.loads(proc.stdout)
            except (ValueError, TypeError):
                reason = "gh returned a response that is not JSON"
            else:
                if isinstance(document, dict) and _usable(document.get("data"), required):
                    return document, ""
                errors = document.get("errors") if isinstance(document, dict) else None
                reason = _error_summary(errors) if errors else "GraphQL response carried no data"
        if attempt + 1 < attempts:
            time.sleep(BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)])
    return None, reason


def _usable(data: Any, required: tuple[str, ...]) -> bool:
    node = data
    for key in required:
        if not isinstance(node, dict):
            return False
        node = node.get(key)
    return node is not None


def _first_line(text: str | None) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _error_summary(errors: Any) -> str:
    if isinstance(errors, list):
        messages = [
            str(entry.get("message", entry))
            for entry in errors
            if isinstance(entry, dict) or entry is not None
        ]
        if messages:
            return "; ".join(messages[:3])
    return "GraphQL returned errors"


def _errors_by_pr_node(errors: Any, *, aliased: bool = False) -> tuple[dict[int, set[str]], bool]:
    """Attribute each field error to the page node it nulled.

    Returns `(touched, page_wide)`: `touched[i]` holds `"checks"` when an error
    path under node `i` runs through `statusCheckRollup`, and `"status"` for any
    other field of that node (the status list, or the commit above it, which
    nulls the status too). An error with no path under the page's nodes cannot
    be placed and sets `page_wide`, which the caller treats as touching every
    node both ways.

    A connection page's nodes sit at PR_NODES_PATH + index; an evidence read's
    sit at EVIDENCE_ALIAS_PATH + `pN`, where N is the index (`aliased=True`).
    """
    touched: dict[int, set[str]] = {}
    page_wide = False
    base = EVIDENCE_ALIAS_PATH if aliased else PR_NODES_PATH
    for entry in errors if isinstance(errors, list) else []:
        path = entry.get("path") if isinstance(entry, dict) else None
        prefix = tuple(path[: len(base)]) if isinstance(path, list) else ()
        deep = prefix == base and len(path) > len(base)
        index: Any = path[len(base)] if deep else None
        if aliased and isinstance(index, str):
            match = EVIDENCE_ALIAS.match(index)
            index = int(match.group(1)) if match else None
        if not isinstance(index, int):
            page_wide = True
            continue
        rest = path[len(base) + 1 :]
        touched.setdefault(index, set()).add("checks" if "statusCheckRollup" in rest else "status")
    return touched, page_wide


def _head_commit(pr: dict[str, Any]) -> dict[str, Any]:
    nodes = ((pr.get("commits") or {}).get("nodes")) or []
    if not nodes:
        return {}
    return (nodes[0] or {}).get("commit") or {}


def _readiness(commit: dict[str, Any], status_context: str) -> dict[str, str] | None:
    """The commit status for `status_context`, or None when the SHA carries none.

    Read from `status.contexts` and never from the rollup: the rollup interleaves
    check-runs, and a check-run named like the aggregate would then be able to
    decide the sweep. States are lower-cased to the spelling the workflow's
    `case` arms use (GitHub's REST spelling).
    """
    contexts = ((commit.get("status") or {}).get("contexts")) or []
    for context in contexts:
        if (context or {}).get("context") != status_context:
            continue
        return {
            "state": str(context.get("state") or "").lower(),
            "updated_at": str(context.get("createdAt") or ""),
        }
    return None


def _fold_checks(nodes: list[Any], completed: list[str], failed: list[str]) -> int:
    """Accumulate completion timestamps from one page of rollup contexts.

    Returns how many CheckRun nodes on the page are NOT completed (queued,
    in progress, waiting): the count the workflow uses to tell a head whose
    lanes are still landing from one that has settled. Non-CheckRun nodes
    (the commit statuses the rollup mixes in) carry no conclusion and are
    skipped -- the readiness verdict is read from `status.contexts` instead,
    so nothing is lost here.
    """
    in_flight = 0
    for node in nodes:
        if not isinstance(node, dict) or node.get("__typename") != "CheckRun":
            continue
        if node.get("status") != "COMPLETED":
            in_flight += 1
            continue
        at = node.get("completedAt")
        if not at:
            continue
        completed.append(str(at))
        if str(node.get("conclusion") or "").upper() in FAILURE_CONCLUSIONS:
            failed.append(str(at))
    return in_flight


def _newest(values: list[str]) -> str | None:
    """Chronological max. GitHub emits zero-padded UTC ISO 8601, so max() is sound."""
    return max(values) if values else None


def _scan_pr(
    pr: dict[str, Any],
    *,
    owner: str,
    name: str,
    status_context: str,
    counters: Counters,
    checks_complete: bool = True,
) -> dict[str, Any] | None:
    """One output record. `checks_complete=False` when the page already nulled the rollup."""
    number = pr.get("number")
    sha = pr.get("headRefOid")
    if not isinstance(number, int) or not sha:
        return None

    commit = _head_commit(pr)
    completed: list[str] = []
    failed: list[str] = []

    contexts = ((commit.get("statusCheckRollup") or {}).get("contexts")) or {}
    in_flight = _fold_checks(contexts.get("nodes") or [], completed, failed)
    total = contexts.get("totalCount") or 0
    if isinstance(total, int) and total > CONTEXTS_PAGE_SIZE:
        counters.oversized += 1

    page_info = contexts.get("pageInfo") or {}
    while page_info.get("hasNextPage"):
        after = page_info.get("endCursor")
        if not after:
            # hasNextPage with no cursor: nothing can resume the walk.
            checks_complete = False
            break
        document, reason = _graphql(
            CONTEXTS_PAGE_QUERY,
            {"owner": owner, "name": name, "oid": str(sha), "after": str(after)},
            required=("repository",),
        )
        if document is None:
            checks_complete = False
            print(
                f"::warning::readiness sweep scan: PR #{number} check-run page failed "
                f"{ATTEMPTS} GraphQL attempts ({reason}); evaluating it on the pages "
                "already read. Never falls back to REST -- the shared REST pool is "
                "what this scan exists to stay out of.",
                file=sys.stderr,
            )
            break
        counters.context_pages += 1
        contexts = (
            (((document.get("data") or {}).get("repository") or {}).get("object") or {}).get(
                "statusCheckRollup"
            )
            or {}
        ).get("contexts") or {}
        in_flight += _fold_checks(contexts.get("nodes") or [], completed, failed)
        if document.get("errors"):
            # A field error nulled part of this page: whatever it held past the
            # nodes that did arrive was never read.
            checks_complete = False
            print(
                f"::warning::readiness sweep scan: PR #{number} check-run page answered "
                f"with field errors ({_error_summary(document['errors'])}); evaluating "
                "it on the nodes that arrived.",
                file=sys.stderr,
            )
            break
        if not contexts:
            # `hasNextPage` promised more, and the follow-up resolved to nothing:
            # the head moved between the two queries (`object: null`), or the
            # rollup vanished. Either way the pages past this one were never
            # read, and saying otherwise would let a stale-green verdict pass as
            # fully evidenced.
            checks_complete = False
            break
        page_info = contexts.get("pageInfo") or {}

    return {
        "number": number,
        "sha": str(sha),
        "pr_updated_at": str(pr.get("updatedAt") or ""),
        "readiness": _readiness(commit, status_context),
        "newest_completed_check_at": _newest(completed),
        "newest_failed_check_at": _newest(failed),
        "checks_in_flight": in_flight,
        "checks_complete": checks_complete,
        "checks_requested": True,
    }


def _light_record(pr: dict[str, Any], *, status_context: str) -> dict[str, Any] | None:
    """A record with NO check evidence: the light page read, the rollup not.

    `checks_requested: false` is how the workflow tells this apart from a
    rollup it asked for and could not fully read (`checks_complete: false`,
    which it reports as partial evidence). Nothing was asked for here, so
    nothing is missing; the classification sees no evidence and, for the
    verdicts this is emitted for, decides on the pull request's own age or
    activity instead.
    """
    number = pr.get("number")
    sha = pr.get("headRefOid")
    if not isinstance(number, int) or not sha:
        return None
    return {
        "number": number,
        "sha": str(sha),
        "pr_updated_at": str(pr.get("updatedAt") or ""),
        "readiness": _readiness(_head_commit(pr), status_context),
        "newest_completed_check_at": None,
        "newest_failed_check_at": None,
        "checks_in_flight": 0,
        "checks_complete": False,
        "checks_requested": False,
    }


def _epoch(value: Any) -> int:
    """GitHub's zero-padded UTC ISO 8601 as seconds; 0 when absent or malformed."""
    if not isinstance(value, str) or not value:
        return 0
    try:
        return int(
            datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
        )
    except ValueError:
        return 0


def _is_candidate(
    pr: dict[str, Any], *, status_context: str, now: int, activity_window: int
) -> bool:
    """Whether a light-page pull request needs its check evidence read.

    This is the whole saving of the delivery scan, so each arm names the
    staleness mode (see the workflow header) it keeps observable:

    - a `pending` verdict: mode 1, the ordinary delivery of a lane completion,
      is decided on check evidence and nothing else, so every pending is read;
    - a terminal verdict on a pull request active inside the window: the
      activity may be the disposition edit of mode 5, which the workflow reads
      comments for, and a pull request being worked on is where a re-run
      (modes 2 and 4) is likeliest, so its evidence is read while it is warm;
    - a GREEN verdict whose rollup aggregate is red: mode 4 by its cheapest
      symptom -- some check on the revision is red under a verdict that says
      none is -- read to find out whether that check completed after the
      verdict. A red verdict's rollup is red by construction (the verdict is
      one of its contexts), so this arm cannot exist for mode 2.

    Everything else -- an unpublished verdict, which mode 3 decides on the
    pull request's age alone, and a terminal verdict on a pull request nobody
    has touched inside the window -- is emitted unread. The one shape that
    leaves unobserved is a re-run on a QUIET pull request whose `in_progress`
    event GitHub also dropped (readiness holds `pending` on that event, which
    would have made it a candidate); the full scan, which reads every rollup,
    is what still finds those, and the workflow runs it on the schedule.
    """
    commit = _head_commit(pr)
    readiness = _readiness(commit, status_context)
    if readiness is None:
        return False
    state = readiness["state"]
    if state == "pending":
        return True
    if state not in ("failure", "success", "error"):
        return False
    verdict_at = _epoch(readiness["updated_at"])
    pr_updated = _epoch(pr.get("updatedAt"))
    if pr_updated > verdict_at and pr_updated >= now - activity_window:
        return True
    rollup_state = str(((commit.get("statusCheckRollup") or {}).get("state")) or "").upper()
    return state in ("success", "error") and rollup_state in RED_ROLLUP_STATES


def _repo(value: str) -> tuple[str, str]:
    owner, _, name = value.partition("/")
    if not owner or not name or "/" in name:
        raise argparse.ArgumentTypeError(f"expected OWNER/NAME, got {value!r}")
    return owner, name


def _walk_pages(
    query: str,
    *,
    owner: str,
    name: str,
    page_size: int,
    counters: Counters,
    what: str,
):
    """Walk one open-pull-request connection, yielding `(document, connection, page_size)`.

    The shrink cascade lives here so both the heavy and the light walk degrade
    the same way: a page that fails its attempts is retried at the same cursor
    at half the size, down to one; a page of one that still fails ends the
    walk with a `::warning::` and the pages past it wait for the next sweep.
    `what` prefixes the walk's name in those warnings (`""` for the heavy walk,
    whose wording the workflow tests pin; `"light "` for the light one).
    """
    cursor: str | None = None
    counters.ending_page_size = page_size
    # True for the first request after a shrink: the failed size already spent
    # its attempts ruling out a transient, so each smaller size is probed once.
    probe = False
    while True:
        variables: dict[str, str | int] = {"owner": owner, "name": name, "first": page_size}
        if cursor:
            variables["cursor"] = cursor
        document, reason = _graphql(
            query,
            variables,
            required=("repository", "pullRequests"),
            attempts=1 if probe else ATTEMPTS,
        )
        if document is None:
            if page_size > 1:
                smaller = max(1, page_size // 2)
                print(
                    f"::warning::readiness sweep scan: a {what}page of {page_size} pull "
                    f"requests failed ({reason}); retrying the same cursor at {smaller}.",
                    file=sys.stderr,
                )
                page_size = smaller
                counters.ending_page_size = page_size
                probe = True
                counters.shrinks += 1
                continue
            print(
                f"::warning::readiness sweep scan: a {what}page of 1 pull request failed "
                f"({reason}) after {counters.pull_requests} pull requests were emitted. "
                "A cursor comes only from the page that failed, so the walk ends here: "
                "pull requests after this point are not scanned in this sweep and the "
                "next sweep retries them.",
                file=sys.stderr,
            )
            return
        probe = False
        connection = ((document.get("data") or {}).get("repository") or {}).get(
            "pullRequests"
        ) or {}
        yield document, connection, page_size
        page_info = connection.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            return
        next_cursor = page_info.get("endCursor")
        if not next_cursor:
            return
        cursor = str(next_cursor)


def _report_field_errors(
    document: dict[str, Any], *, page_size: int, left_out: list[Any], what: str
) -> None:
    names = ", ".join(f"#{n}" for n in left_out)
    print(
        f"::warning::readiness sweep scan: a {what}page of {page_size} pull requests "
        f"answered with field errors ({_error_summary(document['errors'])}). "
        f"Left out, readiness status unreadable: {names or 'none'}; a pull "
        "request whose rollup was nulled is emitted with checks_complete: false.",
        file=sys.stderr,
    )


def _full_scan(*, owner: str, name: str, status_context: str, counters: Counters) -> None:
    """Every open pull request with its rollup read: the complete walk."""
    for document, connection, page_size in _walk_pages(
        PR_PAGE_QUERY, owner=owner, name=name, page_size=PR_PAGE_SIZE, counters=counters, what=""
    ):
        counters.pr_pages += 1
        nodes = connection.get("nodes") or []
        touched, page_wide = _errors_by_pr_node(document.get("errors"))
        left_out: list[Any] = []
        for index, pr in enumerate(nodes):
            pr = pr or {}
            hit = {"status", "checks"} if page_wide else touched.get(index, set())
            if "status" in hit:
                # The readiness status could not be read. A record with a null
                # `readiness` would read as "no verdict published" and re-fire
                # this pull request on transient trouble, so it is left out
                # instead and the next sweep reads it again.
                left_out.append(pr.get("number"))
                continue
            record = _scan_pr(
                pr,
                owner=owner,
                name=name,
                status_context=status_context,
                counters=counters,
                checks_complete="checks" not in hit,
            )
            if record is None:
                continue
            counters.pull_requests += 1
            print(json.dumps(record))
        if document.get("errors"):
            counters.left_out += len(left_out)
            _report_field_errors(document, page_size=page_size, left_out=left_out, what="")


def _read_evidence(
    candidates: list[dict[str, Any]],
    *,
    owner: str,
    name: str,
    status_context: str,
    counters: Counters,
) -> None:
    """Emit a full record for each candidate, read by number in aliased batches.

    A batch that fails its attempts is retried at half the size from the same
    offset, the way a connection page is; a batch of one that still fails emits
    that candidate's LIGHT record with `checks_requested: true` and
    `checks_complete: false`, so the workflow reports its evidence as partial
    rather than silently deciding on none -- the read was owed and did not
    happen. The batch is re-read from the response's own `status.contexts`, so
    a candidate whose verdict moved between the two reads is decided on the
    newer one, and one whose readiness status the read nulled is left out like
    any other unreadable status.
    """
    offset = 0
    batch_size = EVIDENCE_BATCH_SIZE
    probe = False
    while offset < len(candidates):
        batch = candidates[offset : offset + batch_size]
        numbers = [int(pr["number"]) for pr in batch]
        document, reason = _graphql(
            evidence_query(numbers),
            {"owner": owner, "name": name},
            required=EVIDENCE_ALIAS_PATH,
            attempts=1 if probe else ATTEMPTS,
        )
        if document is None:
            if batch_size > 1:
                smaller = max(1, batch_size // 2)
                print(
                    f"::warning::readiness sweep scan: an evidence read of {batch_size} pull "
                    f"requests failed ({reason}); retrying the same candidates at {smaller}.",
                    file=sys.stderr,
                )
                batch_size = smaller
                counters.ending_page_size = batch_size
                probe = True
                counters.shrinks += 1
                continue
            pr = batch[0]
            print(
                f"::warning::readiness sweep scan: the evidence read for PR #{pr.get('number')} "
                f"failed ({reason}); emitting it with no check evidence, marked partial.",
                file=sys.stderr,
            )
            record = _light_record(pr, status_context=status_context)
            if record is not None:
                record["checks_requested"] = True
                counters.pull_requests += 1
                print(json.dumps(record))
            offset += 1
            continue
        probe = False
        counters.evidence_pages += 1
        repository = (document.get("data") or {}).get("repository") or {}
        touched, page_wide = _errors_by_pr_node(document.get("errors"), aliased=True)
        left_out: list[Any] = []
        for index, light in enumerate(batch):
            pr = repository.get(f"p{index}") or {}
            hit = {"status", "checks"} if page_wide else touched.get(index, set())
            if "status" in hit or not pr:
                # Nulled, or the pull request vanished (closed between the two
                # reads): the verdict as it stands cannot be read, so nothing is
                # emitted and the next sweep reads it again.
                left_out.append(light.get("number"))
                continue
            record = _scan_pr(
                pr,
                owner=owner,
                name=name,
                status_context=status_context,
                counters=counters,
                checks_complete="checks" not in hit,
            )
            if record is None:
                continue
            counters.pull_requests += 1
            print(json.dumps(record))
        if document.get("errors"):
            counters.left_out += len(left_out)
            _report_field_errors(
                document, page_size=batch_size, left_out=left_out, what="evidence "
            )
        offset += len(batch)


def _delivery_scan(
    *,
    owner: str,
    name: str,
    status_context: str,
    counters: Counters,
    now: int,
    activity_window: int,
) -> None:
    """The light walk over every open pull request, then evidence for the candidates only."""
    candidates: list[dict[str, Any]] = []
    for document, connection, page_size in _walk_pages(
        LIGHT_PAGE_QUERY,
        owner=owner,
        name=name,
        page_size=LIGHT_PAGE_SIZE,
        counters=counters,
        what="light ",
    ):
        counters.light_pages += 1
        nodes = connection.get("nodes") or []
        touched, page_wide = _errors_by_pr_node(document.get("errors"))
        left_out: list[Any] = []
        for index, pr in enumerate(nodes):
            pr = pr or {}
            hit = {"status", "checks"} if page_wide else touched.get(index, set())
            if "status" in hit:
                left_out.append(pr.get("number"))
                continue
            if _is_candidate(
                pr, status_context=status_context, now=now, activity_window=activity_window
            ):
                candidates.append(pr)
                continue
            record = _light_record(pr, status_context=status_context)
            if record is None:
                continue
            counters.pull_requests += 1
            print(json.dumps(record))
        if document.get("errors"):
            counters.left_out += len(left_out)
            _report_field_errors(document, page_size=page_size, left_out=left_out, what="light ")
    counters.candidates = len(candidates)
    _read_evidence(
        candidates, owner=owner, name=name, status_context=status_context, counters=counters
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=_repo, metavar="OWNER/NAME")
    parser.add_argument(
        "--status-context",
        required=True,
        metavar="NAME",
        help='the commit status the sweep owns, e.g. "PR Readiness"',
    )
    parser.add_argument(
        "--mode",
        choices=("full", "delivery"),
        default="full",
        help=(
            "full (default): read every open pull request's check evidence -- 25 per page, "
            "measured 25 pages and 261 s at 625 open. delivery: read every pull request's "
            "verdict on a light page (100 per page, 7 pages, 19 s) and the evidence only "
            "for the ones that can be stale on evidence -- see _is_candidate."
        ),
    )
    parser.add_argument(
        "--activity-window-seconds",
        type=int,
        default=DEFAULT_ACTIVITY_WINDOW_SECONDS,
        metavar="SECONDS",
        help="delivery mode: a terminal verdict is a candidate while its pull request was active this recently",
    )
    args = parser.parse_args(argv)
    owner, name = args.repo

    counters = Counters()
    if args.mode == "delivery":
        _delivery_scan(
            owner=owner,
            name=name,
            status_context=args.status_context,
            counters=counters,
            now=int(time.time()),
            activity_window=args.activity_window_seconds,
        )
        print(
            f"::notice::readiness sweep scan ({args.mode}): {counters.pull_requests} pull requests, "
            f"{counters.light_pages} light pages, {counters.candidates} candidates read for "
            f"evidence in {counters.evidence_pages} reads, {counters.context_pages} context "
            f"pages, {counters.oversized} PRs with >{CONTEXTS_PAGE_SIZE} contexts, "
            f"{counters.shrinks} page-size reductions (ending at {counters.ending_page_size}), "
            f"{counters.left_out} PRs left out on field errors",
            file=sys.stderr,
        )
        return 0

    _full_scan(owner=owner, name=name, status_context=args.status_context, counters=counters)
    print(
        f"::notice::readiness sweep scan: {counters.pull_requests} pull requests, "
        f"{counters.pr_pages} GraphQL pages, {counters.context_pages} context pages, "
        f"{counters.oversized} PRs with >{CONTEXTS_PAGE_SIZE} contexts, "
        f"{counters.shrinks} page-size reductions (ending at {counters.ending_page_size}), "
        f"{counters.left_out} PRs left out on field errors",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    sys.exit(main())
