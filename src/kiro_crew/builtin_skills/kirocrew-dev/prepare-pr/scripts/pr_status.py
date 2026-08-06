#!/usr/bin/env python3
"""pr_status.py - the decisive PR-readiness gate for the prepare-pr skill.

Prints PR state + every CI check + advisory unresolved-thread count and returns
an exit code that drives the poll loop. The aggregate ``PR Readiness`` status is
authoritative when present; older PRs fall back to the full check rollup.
Stdlib only; portable.

Usage:  python3 pr_status.py [pr-number] [--readiness-context NAME]
        (no number -> auto-detect the PR for the current branch;
         --readiness-context / PREPARE_PR_READINESS_CONTEXT override the
         aggregate status-context name, default "PR Readiness")

Exit codes:
   0  CLEAN     - open, non-draft, MERGEABLE, no CHANGES_REQUESTED, and
                  aggregate PR Readiness (or the legacy full rollup) passed
  10  RUNNING   - a required check is still queued/in-progress, or mergeability
                  has not been computed yet
  20  BLOCKED   - failing readiness, merge conflict, draft, CHANGES_REQUESTED,
                  a terminal PR state (MERGED/CLOSED), or anything that cannot
                  be confirmed
   2  ENV ERROR - gh missing or not authenticated, or PR not found
"""
import json
import os
import re
import subprocess
import sys

# Strip ANSI escape sequences and C0/C1 control chars from untrusted printed
# text (PR titles / check names are attacker-controllable) to prevent
# terminal/prompt injection into the agent session.
_CTRL_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|[\x00-\x08\x0b-\x1f\x7f]")


def sanitize(s):
    return _CTRL_RE.sub("", s or "")


# Explicit state classification (classify every state; fail closed).
PASS_CONCLUSIONS = {"SUCCESS", "NEUTRAL", "SKIPPED"}
# StatusContext (legacy commit statuses) use .state rather than .conclusion.
CTX_PASS = {"SUCCESS"}
CTX_RUNNING = {"PENDING", "EXPECTED"}
DEFAULT_READINESS_CONTEXT = "PR Readiness"
# Page cap so a pathological PR can't make us loop unbounded (100 * 50 = 5000).
_MAX_THREAD_PAGES = 50


def resolve_readiness_context(argv, environ):
    """Resolve the aggregate-readiness status-context name.

    Precedence: ``--readiness-context`` CLI flag > ``PREPARE_PR_READINESS_CONTEXT``
    env var > the KiroCrew default. Lets a project profile name a non-default
    aggregate status; when unset, behavior is identical to before (the default
    context, with the full-rollup fallback when that context is absent).
    """
    for i, a in enumerate(argv):
        if a == "--readiness-context" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--readiness-context="):
            return a.split("=", 1)[1]
    env = environ.get("PREPARE_PR_READINESS_CONTEXT")
    return env if env else DEFAULT_READINESS_CONTEXT


def positional_args(argv):
    """Return argv with the ``--readiness-context`` flag (and value) removed."""
    out = []
    skip = False
    for a in argv:
        if skip:
            skip = False
            continue
        if a == "--readiness-context":
            skip = True
            continue
        if a.startswith("--readiness-context="):
            continue
        out.append(a)
    return out


def run(args):
    try:
        p = subprocess.run(args, capture_output=True, text=True)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except OSError as exc:
        return 127, "", "{}: {}".format(args[0], exc)


def err(msg):
    sys.stderr.write(msg + "\n")


def classify_check(entry):
    """Return 'pass' | 'running' | 'fail' for one statusCheckRollup entry.

    Fail-closed: any unrecognized COMPLETED conclusion or unknown shape counts
    as 'fail' rather than silently passing.
    """
    status = (entry.get("status") or "").upper()
    conclusion = (entry.get("conclusion") or "").upper()
    state = (entry.get("state") or "").upper()
    if status:  # CheckRun
        if status != "COMPLETED":
            return "running"  # queued/in-progress/any non-terminal state
        return "pass" if conclusion in PASS_CONCLUSIONS else "fail"
    if state:  # StatusContext
        if state in CTX_PASS:
            return "pass"
        if state in CTX_RUNNING:
            return "running"
        return "fail"
    return "fail"  # unknown shape -> fail closed


def collapse_superseded(rollup):
    """Collapse re-run check attempts to the newest run per check identity.

    GitHub keeps superseded attempts (typically CANCELLED) in the rollup next
    to the run that replaced them; counting them inflates the failure count
    with entries that are no longer live. Identity is the workflow-qualified
    check name for CheckRuns and the context name for StatusContexts; newest
    is decided by startedAt (ISO-8601, so string comparison orders correctly).
    Entries that cannot be strictly ordered against the current winner are all
    kept -- when in doubt, over-report rather than hide a live failure.
    """
    winners = {}
    order = []
    undecidable = []
    for e in rollup:
        context = e.get("context")
        if context:
            key = ("ctx", context, "")
        else:
            key = ("run", e.get("workflowName") or "", e.get("name") or "")
        started = e.get("startedAt") or ""
        if key not in winners:
            winners[key] = (started, e)
            order.append(key)
            continue
        prev_started, prev = winners[key]
        if started and prev_started:
            if started > prev_started:
                winners[key] = (started, e)
        else:
            undecidable.append(e)  # no ordering evidence -> keep both
    return [winners[k][1] for k in order] + undecidable


def unresolved_thread_count(number):
    """Count unresolved review threads across all pages for advisory output."""
    rc, repo, _ = run(["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"])
    if rc != 0 or "/" not in repo:
        return None
    owner, name = repo.split("/", 1)
    query = (
        "query($o:String!,$r:String!,$n:Int!,$c:String){repository(owner:$o,"
        "name:$r){pullRequest(number:$n){reviewThreads(first:100,after:$c)"
        "{pageInfo{hasNextPage endCursor} nodes{isResolved}}}}}"
    )
    cursor = None
    count = 0
    for _ in range(_MAX_THREAD_PAGES):
        args = [
            "gh",
            "api",
            "graphql",
            "-f",
            "query=" + query,
            "-F",
            "o=" + owner,
            "-F",
            "r=" + name,
            "-F",
            "n=" + str(number),
        ]
        if cursor:
            args += ["-F", "c=" + cursor]
        rc, out, _ = run(args)
        if rc != 0 or not out:
            return None
        try:
            rt = json.loads(out)["data"]["repository"]["pullRequest"]["reviewThreads"]
        except (ValueError, KeyError, TypeError):
            return None
        count += sum(1 for t in (rt.get("nodes") or []) if not t.get("isResolved", False))
        page = rt.get("pageInfo") or {}
        if not page.get("hasNextPage") or not page.get("endCursor"):
            return count
        cursor = page["endCursor"]
    return None  # hit the page cap with more pages left -> uncertain (fail-closed)


def main(argv):
    if run(["gh", "auth", "status"])[0] != 0:
        err("ERROR: gh not found or not authenticated. Run: gh auth login")
        return 2

    readiness_context = resolve_readiness_context(argv, os.environ)
    pos = positional_args(argv[1:])
    pr = pos[0] if pos else ""
    if not pr:
        pr = run(["gh", "pr", "view", "--json", "number", "-q", ".number"])[1]
    if not pr:
        err("ERROR: no PR number given and none found for the current branch.")
        return 2

    fields = (
        "number,title,state,isDraft,mergeable,mergeStateStatus,"
        "reviewDecision,url,headRefName,statusCheckRollup"
    )
    rc, out, _ = run(["gh", "pr", "view", pr, "--json", fields])
    if rc != 0 or not out:
        err("ERROR: could not read PR #" + str(pr))
        return 2
    d = json.loads(out)

    state = (d.get("state") or "").upper()
    draft = bool(d.get("isDraft"))
    mergeable = (d.get("mergeable") or "").upper()
    merge_state = (d.get("mergeStateStatus") or "").upper()
    decision = (d.get("reviewDecision") or "NONE").upper()
    rollup = collapse_superseded(d.get("statusCheckRollup") or [])

    print("=" * 54)
    print("PR #{}  [{}{}]".format(d.get("number"), state, " draft" if draft else ""))
    print("title (untrusted): " + sanitize(d.get("title") or ""))
    print("branch: " + sanitize(d.get("headRefName") or ""))
    print("url:    " + (d.get("url") or ""))
    print(
        "mergeable={}  mergeState={}  reviewDecision={}".format(
            mergeable or "?", merge_state or "?", decision
        )
    )

    print("-- CI checks " + "-" * 40)
    n_running = n_fail = 0
    readiness_kind = None
    for e in rollup:
        kind = classify_check(e)
        if kind == "running":
            n_running += 1
        elif kind == "fail":
            n_fail += 1
        name = sanitize(e.get("name") or e.get("context") or "check")
        # Only the legacy StatusContext we publish is authoritative. A CheckRun
        # can share the display name but is a different, independently writable
        # namespace and must remain part of the ordinary rollup.
        if e.get("context") == readiness_context:
            readiness_kind = kind
        shown = (e.get("status") or "-") + "/" + (e.get("conclusion") or e.get("state") or "-")
        print("  - {}: {}  [{}]".format(name, shown, kind))
    print("  rollup: total={} running={} failing={}".format(len(rollup), n_running, n_fail))
    print("  aggregate readiness: {}".format(readiness_kind or "not published"))

    n_unresolved = unresolved_thread_count(d.get("number"))
    print("-- Review threads " + "-" * 35)
    print(
        "  unresolved threads (advisory): " + ("?" if n_unresolved is None else str(n_unresolved))
    )
    print("=" * 54)

    # ---- Decision (fail-closed) --------------------------------------------
    # A non-open PR is terminal, and must be decided BEFORE the mergeability
    # wait: GitHub reports mergeable=UNKNOWN for merged/closed PRs forever, so
    # waiting on it would return 10 on every poll and a loop would never stop.
    if state != "OPEN":
        print("STATUS: BLOCKED - PR state is {} (not OPEN; terminal)".format(state or "?"))
        return 20
    # Once published, the aggregate is authoritative over stale duplicate
    # checks in the rollup. Legacy PRs without it still use the full rollup.
    if readiness_kind == "running" or (readiness_kind is None and n_running > 0):
        print("STATUS: RUNNING (round not complete)")
        return 10
    # Mergeability not yet computed by GitHub -> unknown -> wait, don't pass.
    if mergeable not in ("MERGEABLE", "CONFLICTING"):
        print("STATUS: RUNNING (mergeability not yet computed: {})".format(mergeable or "UNKNOWN"))
        return 10

    reasons = []
    if readiness_kind == "fail":
        reasons.append("{} reported action required".format(readiness_context))
    elif readiness_kind is None and n_fail > 0:
        reasons.append("{} check(s) failed".format(n_fail))
    if len(rollup) == 0:
        reasons.append("no CI checks reported - cannot confirm CI (fail-closed)")
    if draft:
        reasons.append("PR is a draft")
    if mergeable == "CONFLICTING" or merge_state in ("DIRTY", "CONFLICTING"):
        reasons.append("merge conflict / not mergeable")
    if merge_state == "BEHIND":
        reasons.append("branch is BEHIND base - re-sync onto the latest base")
    elif merge_state and merge_state not in (
        "CLEAN",
        "HAS_HOOKS",
        "UNSTABLE",
        "BLOCKED",
        "DIRTY",
        "CONFLICTING",
        "DRAFT",
    ):
        # BLOCKED = pending required review (expected for a review-ready PR);
        # anything unrecognized is fail-closed.
        reasons.append("unrecognized merge state '{}' (fail-closed)".format(merge_state))
    if decision == "CHANGES_REQUESTED":
        reasons.append("review decision is CHANGES_REQUESTED")

    if reasons:
        print("STATUS: BLOCKED - " + "; ".join(reasons))
        return 20

    print("STATUS: CLEAN (readiness passed, mergeable, no blocking review decision)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
