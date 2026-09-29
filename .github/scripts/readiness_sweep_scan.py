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
     "checks_complete": true}

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
import subprocess  # noqa: S404 - fixed argv, no shell
import sys
import time
from typing import Any

# Measured ceiling, not a guess. See the module docstring: 50 and 100 time out.
PR_PAGE_SIZE = 25
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


class Counters:
    """What the closing `::notice::` reports."""

    def __init__(self) -> None:
        self.pull_requests = 0
        self.pr_pages = 0
        self.context_pages = 0
        self.oversized = 0
        self.shrinks = 0
        self.left_out = 0


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


def _errors_by_pr_node(errors: Any) -> tuple[dict[int, set[str]], bool]:
    """Attribute each field error to the page node it nulled.

    Returns `(touched, page_wide)`: `touched[i]` holds `"checks"` when an error
    path under node `i` runs through `statusCheckRollup`, and `"status"` for any
    other field of that node (the status list, or the commit above it, which
    nulls the status too). An error with no path under the page's nodes cannot
    be placed and sets `page_wide`, which the caller treats as touching every
    node both ways.
    """
    touched: dict[int, set[str]] = {}
    page_wide = False
    for entry in errors if isinstance(errors, list) else []:
        path = entry.get("path") if isinstance(entry, dict) else None
        prefix = tuple(path[: len(PR_NODES_PATH)]) if isinstance(path, list) else ()
        deep = prefix == PR_NODES_PATH and len(path) > len(PR_NODES_PATH)
        index = path[len(PR_NODES_PATH)] if deep else None
        if not isinstance(index, int):
            page_wide = True
            continue
        rest = path[len(PR_NODES_PATH) + 1 :]
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


def _fold_checks(nodes: list[Any], completed: list[str], failed: list[str]) -> None:
    """Accumulate completion timestamps from one page of rollup contexts.

    Non-CheckRun nodes (the commit statuses the rollup mixes in) carry no
    conclusion and are skipped -- the readiness verdict is read from
    `status.contexts` instead, so nothing is lost here.
    """
    for node in nodes:
        if not isinstance(node, dict) or node.get("__typename") != "CheckRun":
            continue
        if node.get("status") != "COMPLETED":
            continue
        at = node.get("completedAt")
        if not at:
            continue
        completed.append(str(at))
        if str(node.get("conclusion") or "").upper() in FAILURE_CONCLUSIONS:
            failed.append(str(at))


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
    _fold_checks(contexts.get("nodes") or [], completed, failed)
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
        _fold_checks(contexts.get("nodes") or [], completed, failed)
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
        "checks_complete": checks_complete,
    }


def _repo(value: str) -> tuple[str, str]:
    owner, _, name = value.partition("/")
    if not owner or not name or "/" in name:
        raise argparse.ArgumentTypeError(f"expected OWNER/NAME, got {value!r}")
    return owner, name


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=_repo, metavar="OWNER/NAME")
    parser.add_argument(
        "--status-context",
        required=True,
        metavar="NAME",
        help='the commit status the sweep owns, e.g. "PR Readiness"',
    )
    args = parser.parse_args(argv)
    owner, name = args.repo

    counters = Counters()
    cursor: str | None = None
    page_size = PR_PAGE_SIZE
    # True for the first request after a shrink: the failed size already spent
    # its attempts ruling out a transient, so each smaller size is probed once.
    probe = False
    while True:
        variables: dict[str, str | int] = {"owner": owner, "name": name, "first": page_size}
        if cursor:
            variables["cursor"] = cursor
        document, reason = _graphql(
            PR_PAGE_QUERY,
            variables,
            required=("repository", "pullRequests"),
            attempts=1 if probe else ATTEMPTS,
        )
        if document is None:
            if page_size > 1:
                smaller = max(1, page_size // 2)
                print(
                    f"::warning::readiness sweep scan: a page of {page_size} pull requests "
                    f"failed ({reason}); retrying the same cursor at {smaller}.",
                    file=sys.stderr,
                )
                page_size = smaller
                probe = True
                counters.shrinks += 1
                continue
            print(
                f"::warning::readiness sweep scan: a page of 1 pull request failed "
                f"({reason}) after {counters.pull_requests} pull requests were emitted. "
                "A cursor comes only from the page that failed, so the walk ends here: "
                "pull requests after this point are not scanned in this sweep and the "
                "next sweep retries them.",
                file=sys.stderr,
            )
            break
        probe = False
        counters.pr_pages += 1
        connection = ((document.get("data") or {}).get("repository") or {}).get(
            "pullRequests"
        ) or {}
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
                status_context=args.status_context,
                counters=counters,
                checks_complete="checks" not in hit,
            )
            if record is None:
                continue
            counters.pull_requests += 1
            print(json.dumps(record))
        if document.get("errors"):
            counters.left_out += len(left_out)
            names = ", ".join(f"#{n}" for n in left_out)
            print(
                f"::warning::readiness sweep scan: a page of {page_size} pull requests "
                f"answered with field errors ({_error_summary(document['errors'])}). "
                f"Left out, readiness status unreadable: {names or 'none'}; a pull "
                "request whose rollup was nulled is emitted with checks_complete: false.",
                file=sys.stderr,
            )
        page_info = connection.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        next_cursor = page_info.get("endCursor")
        if not next_cursor:
            break
        cursor = str(next_cursor)

    print(
        f"::notice::readiness sweep scan: {counters.pull_requests} pull requests, "
        f"{counters.pr_pages} GraphQL pages, {counters.context_pages} context pages, "
        f"{counters.oversized} PRs with >{CONTEXTS_PAGE_SIZE} contexts, "
        f"{counters.shrinks} page-size reductions (ending at {page_size}), "
        f"{counters.left_out} PRs left out on field errors",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    sys.exit(main())
