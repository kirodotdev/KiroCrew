#!/usr/bin/env python3
"""pr_status.py - the decisive PR-readiness gate for the kirocrew-prepare-pr skill.

Prints PR state + every CI check + advisory unresolved-thread count and returns
an exit code that drives the poll loop. The aggregate ``PR Readiness`` status is
one signal folded with the rows, never an override of them: its FAILURE blocks
and its PENDING waits, but its green does not clear an observed failing or
pending row, because its context name is a forgeable display string.
Stdlib only; portable.

Usage:  python3 pr_status.py [pr-number] [--readiness-context NAME]
                             [--reviewers NAME1,NAME2] [--json]
        python3 pr_status.py --disposition-gate --repo OWNER/NAME --pr N
                             --head SHA
        python3 pr_status.py --supersession-gate --repo OWNER/NAME --pr N
                             --head SHA
        (no number -> auto-detect the PR for the current branch;
         --readiness-context / PREPARE_PR_READINESS_CONTEXT override the
         aggregate status-context name, default "PR Readiness";
         --reviewers / PREPARE_PR_REVIEWERS pin the reviewer fleet: only the
         named stamps are evaluated AND each named reviewer must have a fresh
         stamp; by default, every ``[<NAME>-REVIEWED]`` stamp found in bot
         comments is held to freshness, and absence is not required;
         --json appends one machine-readable object as the LAST line of stdout
         and changes nothing else -- same exit codes, same prose. Its
         ``progress_key`` sub-object is the only part safe to compare between
         runs; a monitoring loop uses it to tell a stalled PR from a moving one;
         --disposition-gate evaluates the disposition rule for an explicitly
         given repo/PR/head and also reports ``unpublished`` (whole-design
         lanes that still owe this head a verdict); it prints one JSON object
         and exits 0 --
         this is what pr-readiness.yml calls to enforce the rule server-side,
         so the rule keeps a single definition;
         --supersession-gate reports which verdicts for that head a later
         sample at the SAME head replaced, read out of the comment's stored
         edit history, and prints one JSON object and exits 0 on the same
         terms: ``ok`` false is UNKNOWN and must be treated as pending)

Exit codes:
   0  CLEAN     - open, non-draft, MERGEABLE, no CHANGES_REQUESTED, aggregate
                  PR Readiness (or the legacy full rollup) passed with no
                  observed failing row (a passed aggregate does not clear a
                  failing row), every reviewer stamp matches the current head,
                  no [BLOCK-MERGE] marker for the current head, and a
                  pull_request-event run exists for the current head (when the
                  repo uses Actions)
  10  RUNNING   - a required check is still queued/in-progress, or mergeability
                  has not been computed yet
  20  BLOCKED   - failing readiness, merge conflict, draft, CHANGES_REQUESTED,
                  a terminal PR state (MERGED/CLOSED), a stale reviewer stamp,
                  a blocking review marker on the current head, no
                  pull_request-event run for the current head, a disposition
                  comment violating the one-lane / one-rationale-per-finding
                  rule, a superseded verdict (a lane blocked this head in a
                  sample a later one replaced), or anything that cannot be
                  confirmed
   2  ENV ERROR - gh missing or not authenticated, or PR not found
"""

import importlib.machinery
import importlib.util
import json
import os
import re
import subprocess
import sys
import time


class _NoBytecodeSourceLoader(importlib.machinery.SourceFileLoader):
    """Load shipped source normally while suppressing cache writes."""

    def get_code(self, fullname):
        path = self.get_filename(fullname)
        source = self.get_data(path)
        return self.source_to_code(source, path)

    def set_data(self, path, data, *, _mode=0o666):
        return None


def _load_sibling(filename, name):
    """Load a sibling script without cwd, sys.path, or bytecode side effects."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    loader = _NoBytecodeSourceLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    if spec is None:  # pragma: no cover - defensive
        raise RuntimeError("cannot import kirocrew-prepare-pr sibling script: " + path)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _load_owner(stem):
    """Load the owner module ``<stem>.py`` beside this script.

    It is named under this module's own package, so a harness that loads the script
    under a dotted name gets its owners under the same prefix -- the name is what a
    coverage run measures by -- and a script run names them by their bare stem.
    """
    package = __name__.rpartition(".")[0]
    return _load_sibling(stem + ".py", package + "." + stem if package else stem)


# The script reads GitHub and prints its report; two owners hold what it reads out of
# the answers. ``pr_status_issue_links.py`` is the issue-link grammar, and
# ``pr_status_verdict.py`` classifies the check rows and decides readiness. Every name
# a test replaces on this module stays here, with every function that reads one. The
# verdict owner loads the review contract and this module takes it from there, so one
# load holds one contract.
_issue_links = _load_owner("pr_status_issue_links")
_verdict = _load_owner("pr_status_verdict")
_review_contract = _verdict._review_contract
# Imported, never shelled out to: a subprocess would give this script a second
# way to fail (PATH, interpreter, quoting) for information it already has the
# code for, and the exit-code contract belongs to green_age.py's own CLI.
_green_age = _load_sibling("green_age.py", "_prepare_pr_green_age")
REVIEWED_STAMP_RE = _review_contract.REVIEWED_STAMP_RE
BLOCK_MERGE_RE = _review_contract.BLOCK_MERGE_RE
DEFAULT_MARKER_AUTHORS = _review_contract.DEFAULT_MARKER_AUTHORS
DEFAULT_MARKER_BINDINGS = _review_contract.DEFAULT_MARKER_BINDINGS
VERDICT_LINE_RE = _review_contract.VERDICT_LINE_RE
WHOLE_DESIGN_LANES = _review_contract.WHOLE_DESIGN_LANES
_COMMENT_KEY_RE = _review_contract._COMMENT_KEY_RE
FINDING_RE = _review_contract.FINDING_RE
DISPOSITION_PREFIX = _review_contract.DISPOSITION_PREFIX
DISPOSITION_MARKER_RE = _review_contract.DISPOSITION_MARKER_RE
SPAN_CLAIM_RE = _review_contract.SPAN_CLAIM_RE
DISPOSITION_BULLET_RE = _review_contract.DISPOSITION_BULLET_RE
span_hash = _review_contract.span_hash
sha_matches = _review_contract.sha_matches
comment_key = _review_contract.comment_key
extract_findings = _review_contract.extract_findings
extract_design_items = _review_contract.extract_design_items
design_lane_verdicts = _review_contract.design_lane_verdicts
design_lane_punchlines = _review_contract.design_lane_punchlines
redact = _review_contract.redact
parse_disposition_record = _review_contract.parse_disposition_record
human_override_actors = _review_contract.human_override_actors
human_override_classes = _review_contract.human_override_classes

# The issue-link grammar.
_CLOSING_VERB = _issue_links._CLOSING_VERB
_REPO_SLUG = _issue_links._REPO_SLUG
_ISSUE_TARGET = _issue_links._ISSUE_TARGET
_CLOSING_REF = _issue_links._CLOSING_REF
_CLOSING_KW_RE = _issue_links._CLOSING_KW_RE
_CLOSING_REF_RE = _issue_links._CLOSING_REF_RE
_BARE_REF_RE = _issue_links._BARE_REF_RE
_FENCE_OPEN_RE = _issue_links._FENCE_OPEN_RE
_fence_opening = _issue_links._fence_opening
_is_closing_fence = _issue_links._is_closing_fence
_mask_html_comments = _issue_links._mask_html_comments
_mask_inline_code = _issue_links._mask_inline_code
_visible_markdown_prose = _issue_links._visible_markdown_prose
_GITHUB_ISSUE_NUMBER_MAX = _issue_links._GITHUB_ISSUE_NUMBER_MAX
_GITHUB_ISSUE_NUMBER_MAX_TEXT = _issue_links._GITHUB_ISSUE_NUMBER_MAX_TEXT
_normalize_issue_number = _issue_links._normalize_issue_number
_issue_number_sort_key = _issue_links._issue_number_sort_key
_normalize_repo_key = _issue_links._normalize_repo_key
_host_ref_repo_key = _issue_links._host_ref_repo_key
_undeclared_closing_numbers = _issue_links._undeclared_closing_numbers
_declared_closing_numbers = _issue_links._declared_closing_numbers
_DECLARING_VERB = _issue_links._DECLARING_VERB
_DECLARING_LINE_START_RE = _issue_links._DECLARING_LINE_START_RE
_DECLARING_REF_RE = _issue_links._DECLARING_REF_RE
declared_issue_numbers = _issue_links.declared_issue_numbers
closing_link_reason = _issue_links.closing_link_reason
# The readiness verdict.
PASS_CONCLUSIONS = _verdict.PASS_CONCLUSIONS
CTX_PASS = _verdict.CTX_PASS
CTX_RUNNING = _verdict.CTX_RUNNING
ROLLUP_HEAD_MOVED_NOTICE = _verdict.ROLLUP_HEAD_MOVED_NOTICE
_host_id = _verdict._host_id
classify_check = _verdict.classify_check
superseded_key = _verdict.superseded_key
_displaced = _verdict._displaced
collapse_superseded = _verdict.collapse_superseded
reviewer_round_settled = _verdict.reviewer_round_settled
build_report = _verdict.build_report
decide = _verdict.decide


# Strip ANSI escape sequences and C0/C1 control chars from untrusted printed
# text (PR titles / check names are attacker-controllable) to prevent
# terminal/prompt injection into the agent session. The C1 range (\x80-\x9f)
# matters: U+009B is the single-byte CSI, equivalent to ESC-[.
_CTRL_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def sanitize(s):
    return _CTRL_RE.sub("", s or "")


DEFAULT_READINESS_CONTEXT = "PR Readiness"


# Page cap so a pathological PR can't make us loop unbounded (100 * 50 = 5000).
_MAX_THREAD_PAGES = 50
_MAX_COMMENT_PAGES = 50

BLOCKING_LINE_RE = re.compile(r"^\s*BLOCKING\b", re.MULTILINE)

# Any revision a lane's stampless notice names. Every such notice writes the head
# it declined to review into its own prose, and that is what scopes the notice's
# exemption to one revision instead of standing forever.
BODY_SHA_RE = re.compile(r"\b[0-9a-f]{7,40}\b")


def fetch_disposition_comments(repo, number):
    return _review_contract.fetch_disposition_comments(repo, number, run)


def author_write_verdict(repo, login):
    return _review_contract.author_write_verdict(repo, login, run)


def author_is_repo_writer(repo, login):
    return _review_contract.author_is_repo_writer(repo, login, run)


def writer_disposition_records(repo, comments):
    return _review_contract.writer_disposition_records(repo, comments, run, author_write_verdict)


disposition_violations = _review_contract.disposition_violations
superseded_verdicts = _review_contract.superseded_verdicts


def resolve_marker_bindings(argv, environ):
    """Resolve the comment-key -> reviewer-name bindings (flag > env > default)."""
    raw = None
    for i, a in enumerate(argv):
        if a == "--marker-bindings" and i + 1 < len(argv):
            raw = argv[i + 1]
        elif a.startswith("--marker-bindings="):
            raw = a.split("=", 1)[1]
    if raw is None:
        raw = environ.get("PREPARE_PR_MARKER_BINDINGS")
    if not raw:
        return dict(DEFAULT_MARKER_BINDINGS)
    out = {}
    for pair in raw.split(","):
        if "=" in pair:
            k, _, v = pair.partition("=")
            if k.strip() and v.strip():
                out[k.strip()] = v.strip().upper()
    return out or dict(DEFAULT_MARKER_BINDINGS)


def resolve_marker_authors(argv, environ):
    """Resolve the comment-author allowlist for marker evaluation.

    Precedence mirrors the other seams: ``--marker-authors`` CLI flag >
    ``PREPARE_PR_MARKER_AUTHORS`` env var > DEFAULT_MARKER_AUTHORS. Logins are
    compared case-insensitively.
    """
    raw = None
    for i, a in enumerate(argv):
        if a == "--marker-authors" and i + 1 < len(argv):
            raw = argv[i + 1]
        elif a.startswith("--marker-authors="):
            raw = a.split("=", 1)[1]
    if raw is None:
        raw = environ.get("PREPARE_PR_MARKER_AUTHORS")
    if not raw:
        return {a.lower() for a in DEFAULT_MARKER_AUTHORS}
    return {n.strip().lower() for n in raw.split(",") if n.strip()} or {
        a.lower() for a in DEFAULT_MARKER_AUTHORS
    }


def resolve_readiness_context(argv, environ):
    """Resolve the aggregate-readiness status-context name.

    Precedence: ``--readiness-context`` CLI flag > ``PREPARE_PR_READINESS_CONTEXT``
    env var > the Kiro Crew default. Lets a project profile name a non-default
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


def resolve_reviewers(argv, environ):
    """Resolve an optional reviewer-name filter for marker evaluation.

    Precedence mirrors ``resolve_readiness_context``: ``--reviewers`` CLI flag >
    ``PREPARE_PR_REVIEWERS`` env var > None (discovery mode). Discovery mode
    evaluates every ``[<NAME>-REVIEWED]`` stamp found, which self-configures on
    any repo whose reviewers emit the stamp contract. Naming reviewers both
    scopes freshness to those names AND requires each to have a fresh stamp --
    a pinned reviewer that never posted reads as stale, so an emitter drift or
    a bot that fails to post cannot make the gate silently vacuous.
    """
    raw = None
    for i, a in enumerate(argv):
        if a == "--reviewers" and i + 1 < len(argv):
            raw = argv[i + 1]
        elif a.startswith("--reviewers="):
            raw = a.split("=", 1)[1]
    if raw is None:
        raw = environ.get("PREPARE_PR_REVIEWERS")
    if not raw:
        return None
    names = {n.strip().upper() for n in raw.split(",") if n.strip()}
    return names or None


def head_run_check_enabled(argv, environ):
    """Whether the pull_request-run-for-head assertion is enabled.

    ``--head-run-check=off`` / ``PREPARE_PR_HEAD_RUN_CHECK=off`` disable it --
    the escape hatch for a repo shape the event heuristic misreads, degrading
    that one gate to pre-existing behavior instead of a permanent block. Any
    other value (or unset) keeps it on.
    """
    val = None
    for i, a in enumerate(argv):
        if a == "--head-run-check" and i + 1 < len(argv):
            val = argv[i + 1]
        elif a.startswith("--head-run-check="):
            val = a.split("=", 1)[1]
    if val is None:
        val = environ.get("PREPARE_PR_HEAD_RUN_CHECK")
    return (val or "").strip().lower() not in ("off", "0", "false", "no")


_VALUE_FLAGS = (
    "--readiness-context",
    "--reviewers",
    "--head-run-check",
    "--marker-authors",
    "--marker-bindings",
)

# Flags that take NO value. positional_args must drop these too: a bare
# --json left in the positional list is read as the PR number, which silently
# resolves the wrong PR (or fails env-check) instead of erroring.
_BOOL_FLAGS = ("--json",)


def positional_args(argv):
    """Return argv with the flags (and any values they take) removed."""
    out = []
    skip = False
    for a in argv:
        if skip:
            skip = False
            continue
        if a in _BOOL_FLAGS:
            continue
        if a in _VALUE_FLAGS:
            skip = True
            continue
        if a.startswith(tuple(f + "=" for f in _VALUE_FLAGS)):
            continue
        out.append(a)
    return out


def run(args):
    try:
        p = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except OSError as exc:
        return 127, "", "{}: {}".format(args[0], exc)


def err(msg):
    sys.stderr.write(msg + "\n")


# statusCheckRollup needs Checks read access, which a fine-grained PAT
# structurally cannot grant, and gh resolves every field of one --json request
# atomically -- so bundling the rollup with the core fields makes the WHOLE
# read fail for those tokens. The rollup is therefore fetched in its own call
# (fetch_check_rollup) and degrades softly: the caller keeps the core metadata
# and reports CI as unknown instead of aborting. The second read re-fetches
# headRefOid and is discarded on a mismatch with the core read's head, so a
# push landing between the two reads can never pair one head's metadata with
# another head's checks. The parity-pinned copy in pr_findings.py keeps each
# command's check-rollup path explicit.
#
# That own call is GraphQL rather than `gh pr view --json statusCheckRollup`:
# the rows gh returns carry the workflow's display NAME and nothing else about
# the run (measured: __typename completedAt conclusion context detailsUrl name
# startedAt state status targetUrl workflowName), and collapse_superseded keys
# on the run itself -- its id, its triggering event, its workflow definition's
# id and its own conclusion -- all reached through the check suite. Nothing in
# the --json field set exposes them, so there is no selection to extend there.
ROLLUP_UNAVAILABLE_NOTICE = (
    "CI check status UNAVAILABLE - the statusCheckRollup fetch failed (a token "
    "without Checks read access, e.g. any fine-grained PAT, cannot fetch it); "
    "treat CI as UNKNOWN, not as 'no checks yet'"
)
# Owner, repository, number and cursor travel as GraphQL variables; the document
# is fixed text. The three strings go through gh's `-f` (raw string): `-F` is
# the TYPED form and turns an all-digit value into a JSON integer, which the
# host then rejects against `String!`, so an owner or repository named `2048`
# would read as UNAVAILABLE forever. Only `n` is `-F`, because `Int!` needs it.
# Both ids are nullable Int on the wire (an app-created check run has a
# checkSuite but no workflowRun); event, status and conclusion are what the
# collapse and classify_check read.
ROLLUP_QUERY = (
    "query($o:String!,$r:String!,$n:Int!,$c:String){repository(owner:$o,name:$r)"
    "{pullRequest(number:$n){headRefOid commits(last:1){nodes{commit{oid "
    "statusCheckRollup{contexts(first:100,after:$c){pageInfo{hasNextPage endCursor} "
    "nodes{__typename ... on CheckRun{name status conclusion startedAt detailsUrl "
    "checkSuite{conclusion workflowRun{databaseId event workflow{databaseId name}}}} "
    "... on StatusContext{context state targetUrl}}}}}}}}}}"
)
# A board past this many pages reads UNKNOWN rather than in part: a partial read
# could keep a displaced row whose successor sits on the page never fetched.
_MAX_ROLLUP_PAGES = 10


def flatten_rollup_row(node):
    """One rollup node in the flat shape the rest of this file reads, or None.

    A CheckRun's run fields are lifted out of its check suite and travel beside
    the row's own: ``workflowName`` (the display label the report prints),
    ``workflowRunId``, ``workflowRunEvent``, ``workflowDefinitionId`` and
    ``workflowRunConclusion``. The suite's conclusion IS the run's: WorkflowRun
    exposes none of its own, and CheckSuite.workflowRun is the inverse edge of
    the one followed here. A field the host withheld is left absent rather than
    defaulted, so a missing id reads as "unidentified" downstream and never as
    a shared key. A StatusContext passes through untouched. None for anything
    that is not a well-formed node, so the caller can refuse the whole page.
    """
    if not isinstance(node, dict) or not isinstance(node.get("__typename"), str):
        return None
    row = {k: v for k, v in node.items() if k != "checkSuite"}
    if node["__typename"] != "CheckRun":
        return row
    suite = node.get("checkSuite")
    run = suite.get("workflowRun") if isinstance(suite, dict) else None
    workflow = run.get("workflow") if isinstance(run, dict) else None
    name = workflow.get("name") if isinstance(workflow, dict) else None
    if isinstance(name, str):
        row["workflowName"] = name
    if isinstance(run, dict):
        if _host_id(run.get("databaseId")) is not None:
            row["workflowRunId"] = run["databaseId"]
        if isinstance(run.get("event"), str) and run["event"]:
            row["workflowRunEvent"] = run["event"]
    if isinstance(workflow, dict) and _host_id(workflow.get("databaseId")) is not None:
        row["workflowDefinitionId"] = workflow["databaseId"]
    if isinstance(suite, dict) and isinstance(suite.get("conclusion"), str) and suite["conclusion"]:
        row["workflowRunConclusion"] = suite["conclusion"]
    return row


def fetch_check_rollup(repo, number, expected_head):
    """Return (rollup rows, notice); the notice is non-empty when degraded.

    Pages through the head commit's statusCheckRollup. Every page re-reads the
    pull request's head AND names the commit the rollup hangs off; either
    disagreeing with ``expected_head`` discards the whole read, so a push that
    lands mid-pagination can never pair one head's metadata with another's
    checks. Any failed or malformed page, or a board past the page cap, reads
    UNAVAILABLE: rows so far are dropped, never reported as the board.
    """
    if "/" not in (repo or "") or not number:
        return [], ROLLUP_UNAVAILABLE_NOTICE
    owner, name = repo.split("/", 1)
    rows: list[dict] = []
    cursor = None
    for _ in range(_MAX_ROLLUP_PAGES):
        args = ["gh", "api", "graphql", "-f", "query=" + ROLLUP_QUERY]
        if cursor:
            args += ["-f", "c=" + cursor]
        args += ["-f", "o=" + owner, "-f", "r=" + name, "-F", "n=" + str(number)]
        rc, out, _ = run(args)
        if rc != 0 or not out.strip():
            return [], ROLLUP_UNAVAILABLE_NOTICE
        try:
            pr = json.loads(out)["data"]["repository"]["pullRequest"]
            head = (pr.get("headRefOid") or "").strip()
            commits = pr["commits"]["nodes"]
            commit = commits[0]["commit"] if commits else None
        except (ValueError, KeyError, TypeError, AttributeError, IndexError):
            return [], ROLLUP_UNAVAILABLE_NOTICE
        if expected_head and head != expected_head:
            return [], ROLLUP_HEAD_MOVED_NOTICE
        if commit is None:
            return [], ""  # a pull request with no commit has no checks
        if not isinstance(commit, dict) or not isinstance(commit.get("oid"), str):
            return [], ROLLUP_UNAVAILABLE_NOTICE
        if expected_head and commit["oid"] != expected_head:
            return [], ROLLUP_HEAD_MOVED_NOTICE
        rollup = commit.get("statusCheckRollup")
        if rollup is None:
            return rows, ""  # the host reports no checks for this head
        try:
            contexts = rollup["contexts"]
            nodes = contexts["nodes"]
            page = contexts["pageInfo"] or {}
            flat = [flatten_rollup_row(n) for n in nodes]
        except (KeyError, TypeError):
            return [], ROLLUP_UNAVAILABLE_NOTICE
        if any(r is None for r in flat):
            return [], ROLLUP_UNAVAILABLE_NOTICE
        rows.extend(flat)
        if not page.get("hasNextPage"):
            return rows, ""
        cursor = page.get("endCursor")
        if not isinstance(cursor, str) or not cursor:
            return [], ROLLUP_UNAVAILABLE_NOTICE
    return [], ROLLUP_UNAVAILABLE_NOTICE  # page cap with pages left: UNKNOWN, never partial


def failing_check_identity(entry):
    """Workflow-qualified label for a failing check, for ``progress_key``.

    Follows ``collapse_superseded()``'s identity axis -- a StatusContext is
    keyed by its context name, a CheckRun by (workflow, name) -- because a
    display name alone is not an identity: two workflows can publish the same
    check name. If one workflow's copy starts failing while the other's stops,
    a name-only list is byte-identical across that change, and a stall streak
    would run straight through a PR whose blocking check actually moved. The
    workflow is named by its display label here, not its definition id: this
    string is read by a person in the report as well as compared by the loop.
    """
    context = entry.get("context")
    if context:
        return sanitize(context)
    workflow = sanitize(entry.get("workflowName") or "")
    name = sanitize(entry.get("name") or "check")
    return "{} / {}".format(workflow, name) if workflow else name


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


def detect_repo(pr_url=""):
    """Return "owner/name" for the VIEWED PR, or "" when undetectable.

    Prefers the PR's own URL: the positional argument may be a full PR URL for
    a different repository than the cwd's checkout, and querying the checkout's
    repo for that PR's comments/runs would silently evaluate the wrong data
    (markers invisible, gates vacuous). Falls back to the cwd's repo only when
    no URL is available.
    """
    m = re.match(r"https?://[^/]+/([^/]+)/([^/]+)/pull/\d+", pr_url or "")
    if m:
        return "{}/{}".format(m.group(1), m.group(2))
    rc, repo, _ = run(["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"])
    return repo.strip() if rc == 0 and "/" in repo else ""


def fetch_issue_comments(repo, number, keep=None, run_command=None):
    return _review_contract.fetch_issue_comments(repo, number, run_command or run, keep=keep)


def is_trusted_bot_comment(c, trusted_authors):
    return _review_contract.is_trusted_bot_comment(c, trusted_authors)


def select_bot_comments(comments, trusted_authors):
    return [c for c in comments if is_trusted_bot_comment(c, trusted_authors)]


def fetch_bot_comments(repo, number, trusted_authors, run_command=None):
    """Trusted marker-source comments on the PR, across pages; None on error.

    Paginated (PRs here routinely carry 50+ bot comments; a single
    unpaginated read silently truncates). Returns None (uncertain, the caller
    fails closed) on any API/parse error or when the page cap is hit with more
    pages left.

    ``run_command`` lets a caller supply a BOUNDED runner. The default one has
    no timeout, which is right for the interactive report -- a slow read there
    is visible to the person waiting -- but wrong for a gate whose output a
    machine reads, because a hung call produces no output at all and empty
    output is not a clean result.
    """
    comments = fetch_issue_comments(
        repo,
        number,
        keep=lambda c: is_trusted_bot_comment(c, trusted_authors),
        run_command=run_command,
    )
    return None if comments is None else select_bot_comments(comments, trusted_authors)


def override_class_label(info):
    """`` as <class>[ #N]`` for an override row; empty when the record has no class line."""
    if not info:
        return ""
    return " as {}{}".format(
        sanitize(info.get("class") or ""),
        " #{}".format(sanitize(info["issue"])) if info.get("issue") else "",
    )


def evaluate_reviewer_markers(comments, head_sha, bindings, only=None, authors=None):
    """Evaluate reviewer stamps and blocking markers against the current head.

    Returns a dict:
      ok        -- False when the comments could not be read (fail-closed)
      stale     -- sorted reviewer names with no fresh stamp for the head
      blocking  -- sorted reviewer names with [BLOCK-MERGE] <current head>
      findings  -- {name: BLOCKING-line count} for fresh comments
      overridden -- {name: actor} for lanes a repository writer adjudicated at
                   this head instead of the model (see human_override_actors)
      override_classes -- {name: class record} for those lanes (see
                   parse_override_class); {} when the record has no class line
      stampless -- sorted bound lane names whose own slot carries no stamp of
                   its own AND whose body names this head: the "skipped" /
                   "could not complete" notice the whole-design lanes rewrite their
                   slot to, scoped to the revision it declined. A notice left by
                   an EARLIER head excuses nothing, or a lane that DID review
                   this head and failed to upsert its verdict would read as
                   deliberately silent. Discovery mode leaves those unenrolled;
                   a PINNED caller reads this to apply the same exemption
                   without respelling it.
      seen      -- sorted bound lane names that posted a slot at all, whatever
                   it holds. A PINNED caller reads this to tell "the lane has a
                   slot and it is stale" from "the lane posted nothing", which
                   ``stale`` alone cannot: pinning seeds every named lane as
                   not-fresh, so absence and staleness arrive indistinguishable.
                   Absence has two causes that read identically HERE -- a lane
                   that deliberately had nothing to say, and one whose upsert was
                   lost -- so a caller that must not hold a head on a lane a
                   re-run cannot fill intersects ``stale`` with this.
      pinned    -- whether ``only`` named the fleet. Empty ``stale`` means
                   "every REQUIRED lane stamped this head" only when pinned;
                   in discovery mode it means "every lane that POSTED is
                   fresh", which cannot see a lane that has not spoken yet.

    STRUCTURAL INVARIANT -- reviewer identity comes from WORKFLOW-AUTHORED
    bytes, never from model output. ``bindings`` maps each lane's comment
    upsert key (the leading ``<!-- key -->`` the workflow template writes
    before any model text) to its reviewer name. A stamp counts only when a
    bound comment's OWN name matches it: stamps for other names inside a
    lane's body are injected model output and are ignored, so no lane can
    forge another reviewer's freshness regardless of what its model emits.
    Comments without a bound leading key contribute no freshness. Fail-closed
    asymmetry: a [BLOCK-MERGE] for the current head gates from ANY trusted
    comment, bound or not -- injection can deny a review, never forge one.

    Reviewers required: names in ``only`` when set (a pinned fleet -- absence
    reads as stale, so emitter drift cannot silently un-gate), else every
    BOUND reviewer that posted a comment (discovery mode; a lane that never
    posted is not required, its CI gate covers absence).

    TWO KINDS OF PROOF, and a lane is answered by either. A fresh stamp proves
    a MODEL produced a verdict for this commit. An accepted human-override
    record proves a repository WRITER adjudicated it, on a path where the model
    is deliberately not re-run, so no stamp exists to find. The stamp is
    reported as ``fresh`` and the record as ``overridden``, never folded
    together: a reader auditing the head later must be able to tell "a model
    reviewed this" from "a human cleared this". ``authors`` is the comment-author
    allowlist the record's authority rests on -- checked here as well as by the
    caller's fetch, so the function alone refuses a forged record.
    """
    if comments is None or not head_sha:
        return {
            "ok": False,
            "stale": [],
            "blocking": [],
            "findings": {},
            "elided": [],
            "overridden": {},
            "stampless": [],
            "verdicts": {},
            "pinned": only is not None,
        }
    fresh_by_name: dict = {name: False for name in (only or ())}
    findings: dict = {}
    blocking = set()
    # Whole-design lanes (Design, UX, First Principles) end their body with a
    # `<Lane>-Verdict: PASS|BLOCK` line. Only BLOCK gates.
    verdicts: dict = {}
    # Reviewers whose freshness rests on an ELIDED stamp (see sha_matches). The
    # gate accepts those, but silently swallowing them would hide the emitter
    # defect for good: nobody would learn a lane is mangling the SHA it was
    # handed. Reported as an advisory note, never as a blocking reason -- and
    # deliberately absent from progress_key, which a polling loop diffs.
    elided = set()
    # Which bound lanes hold a slot at all, and which of those carry a stamp of
    # their own. Their difference is the stampless notice the enrolment rule
    # below exempts in discovery mode, reported as ``stampless`` so a PINNED
    # caller can apply the SAME exemption instead of respelling it: under a pin
    # absence must read as stale, and a lane that wrote "I did not review this
    # head" into its own slot is an absence a re-run cannot fill. Recorded
    # outside the ``only`` filter, so the answer describes the comment set
    # rather than the pin.
    slots_seen = set()
    slots_stamped = set()
    slots_naming_head = set()
    for c in comments:
        body = c.get("body") or ""
        name = bindings.get(comment_key(body))
        stamps = REVIEWED_STAMP_RE.findall(body)
        own_stamps = [sha for stamp_name, sha in stamps if stamp_name == name] if name else []
        if name:
            slots_seen.add(name)
            if own_stamps:
                slots_stamped.add(name)
            elif any(sha_matches(sha, head_sha) for sha in BODY_SHA_RE.findall(body)):
                # The notice has to name THIS head to excuse this head. Every
                # lane writes the revision into it ("did not produce a verdict
                # for `<sha>`"), and without the check a notice left by an
                # EARLIER head excuses the current one -- so a lane that did
                # review this head and failed to upsert its verdict reads as
                # deliberately silent, and the required status passes with no
                # verdict for the revision. Any of the body's revisions counts:
                # the lane authors that sentence, a stampless body carries no
                # verdict to forge, and matching loosely here can only ever
                # keep an exemption the lane itself wrote.
                slots_naming_head.add(name)
        if name and (only is None or name in only):
            # A bound lane is held to freshness in DISCOVERY mode only when
            # its comment carries at least one of its own stamps: the UX and
            # Design workflows rewrite their keyed comment to a stampless
            # "skipped"/"could not complete" notice by design (a lane that
            # could not run must not block), and enrolling those would pin exit 20 on a
            # green PR forever. A PINNED lane stays required regardless --
            # that is what pinning means.
            if only is not None or own_stamps:
                fresh = any(sha_matches(sha, head_sha) for sha in own_stamps)
                fresh_by_name[name] = fresh_by_name.get(name, False) or fresh
                if fresh:
                    findings[name] = len(BLOCKING_LINE_RE.findall(body))
                    if not any(head_sha.startswith(sha) for sha in own_stamps):
                        elided.add(name)
                    vm = VERDICT_LINE_RE.search(body)
                    if vm:
                        verdicts[name] = vm.group(1).upper()
        for sha in BLOCK_MERGE_RE.findall(body):
            if sha_matches(sha, head_sha):
                blocking.add(name or "(unattributed)")
    # Accepted human overrides for THIS head. A record naming one lane enrols
    # it, so that lane's presence in the set rests on the record itself rather
    # than on a stale duplicate comment; a `target=all` record answers for the
    # lanes already under evaluation without enrolling any.
    named_overrides, blanket_actor = human_override_actors(
        comments,
        head_sha,
        bindings,
        DEFAULT_MARKER_AUTHORS if authors is None else authors,
    )
    for name, actor in named_overrides.items():
        if only is None or name in only:
            fresh_by_name.setdefault(name, False)
    named_classes, blanket_class = human_override_classes(
        comments,
        head_sha,
        bindings,
        DEFAULT_MARKER_AUTHORS if authors is None else authors,
    )
    overridden = {}
    override_classes = {}
    for name, fresh in fresh_by_name.items():
        # A fresh stamp is the stronger statement and stays the reported one:
        # the model DID run for this commit, so calling the lane overridden
        # would understate the evidence on the PR.
        if fresh:
            continue
        actor = named_overrides.get(name) or blanket_actor
        if actor:
            overridden[name] = actor
            override_classes[name] = named_classes.get(name) or blanket_class or {}
    stale = sorted(n for n, fresh in fresh_by_name.items() if not fresh and n not in overridden)
    return {
        "ok": True,
        "stale": stale,
        "blocking": sorted(blocking),
        "findings": findings,
        "elided": sorted(elided),
        "overridden": dict(sorted(overridden.items())),
        "override_classes": dict(sorted(override_classes.items())),
        "stampless": sorted((slots_seen - slots_stamped) & slots_naming_head),
        "seen": sorted(slots_seen),
        "verdicts": verdicts,
        "pinned": only is not None,
    }


def head_run_exists(repo, head_sha, rollup=()):
    """Whether a pull_request-event workflow run exists for the current head.

    A conflicted or stale PR dispatches no pull_request workflows at all, so
    every check visible belongs to an older head and a status-only loop reports
    'nothing new' forever. The decision reads the CURRENT HEAD's own runs and
    their events -- never repo-wide history, which is wrong in both directions
    (a repo that switched to push-only triggers retains historical
    pull_request runs; a pull_request_target/workflow_run fork-safe repo never
    dispatches the event at all). Returns:
      True   -- a pull_request run exists for this head
      "skip" -- the head has runs, but its CI is driven by other events
                (push / pull_request_target / workflow_run); the PR is not
                held to an event its repo does not use for it
      False  -- an Actions-shaped rollup with NO runs for this head at all:
                the visible checks cannot belong to this head (stale)
      None   -- API error (the caller fails closed with an explicit reason)

    ``rollup`` is this head's own check rows. fetch_check_rollup discards a
    rollup read off any other commit, so the run behind each row ran for this
    head, and a row whose run a pull_request event started settles the question
    without the listing. The listing is still read otherwise, for a run with no
    check row yet, but it is a filtered search that can answer no runs for a head
    whose runs exist, even while that head's own rows name them. An empty listing
    therefore reads False only when no row names a run either.
    """
    if not repo or not head_sha:
        return None
    row_events = {row["workflowRunEvent"] for row in rollup if row.get("workflowRunEvent")}
    if "pull_request" in row_events:
        return True
    rc, out, _ = run(
        [
            "gh",
            "api",
            "repos/{}/actions/runs?head_sha={}&per_page=100".format(repo, head_sha),
        ]
    )
    if rc != 0 or not out:
        return None
    try:
        runs = json.loads(out).get("workflow_runs") or []
        events = {r.get("event") for r in runs if isinstance(r, dict)}
    except (ValueError, TypeError, AttributeError):
        return None
    if "pull_request" in events:
        return True
    if events or row_events:
        return "skip"
    return False


def probe_green_age(base, head_sha, pr):
    """One line on whether this head's green still describes today's base.

    INFORMATION, NEVER A GATE. The verdict is not passed to :func:`decide`, no
    exit code depends on it, and any failure inside ``green_age`` degrades to an
    "unavailable" line. A merger who is about to merge a ten-hour-old green needs
    to know the base moved underneath it; a poll cycle that could not measure
    that must not therefore call a red PR green, or a green one red.

    The base defaults to ``main`` when the host did not report one, because the
    line is advisory and a wrong default costs a wrong line, not a wrong verdict.
    """
    try:
        return _green_age.summarize(
            base=base or "main",
            head=head_sha or "HEAD",
            pr=str(pr or ""),
            runner=run,
        )
    except Exception as exc:  # noqa: BLE001 - an advisory line may never raise
        return {"ok": False, "reason": "probe failed ({})".format(type(exc).__name__)}


def green_age_line(summary):
    """The printed form of :func:`probe_green_age`, indented like its siblings."""
    return "  " + _green_age.format_line(summary)


def _flag_value(argv, name):
    """Value of ``--name VALUE`` or ``--name=VALUE`` in argv, else ""."""
    for i, a in enumerate(argv):
        if a == name and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return ""


def disposition_gate(argv, environ):
    """Evaluate the reviewer-record rules the required status needs; exit 0.

    This is the server-side entry point: pr-readiness.yml calls
    it so a disposition record violating the one-lane / one-rationale-per-
    finding rule fails the repository's required status for EVERY writer, not
    only for a writer running the kirocrew-prepare-pr loop. It exists as a mode of this
    script rather than as a workflow-side reimplementation so the rule keeps a
    single definition -- the same ``disposition_violations`` the local gate
    calls, over the same records the adjudication ledger admits.

    TWO questions, one trusted comment read, because they are answered from the
    same set: the disposition rule, and which whole-design lanes owe this head a
    verdict they never published (``unpublished``). A lane that reports
    ``success`` having failed to write its comment is otherwise indistinguishable
    from a reviewed one on the server side, and scoring it as reviewed is what
    let the required status read green while this script read BLOCKED.

    Usage: --disposition-gate --repo OWNER/NAME --pr N --head SHA
    (--marker-bindings / --marker-authors and their env forms apply as usual.)

    Prints ``{"ok", "violations", "unpublished", "comments", "records",
    "unverified", "error"}``. ``ok`` is False when the record set could not be
    established, which the caller must treat as UNKNOWN (pending) rather than as
    a red: a transient API failure red-lighting the required status is that class
    of bug. Exit status is 0 for both outcomes -- the JSON carries the verdict,
    so a non-zero exit means only that this script itself failed to run, and
    the caller can tell the two apart. Enforcement scope is deliberately
    identical to the ledger's admission scope: an author the collaborators
    permission API does not confirm as a writer is DROPPED here exactly as
    codex-review.yml drops them, so this gate never blocks on a record that
    holds no downgrade power (``unverified`` counts those, for observability).
    """
    repo = _flag_value(argv, "--repo").strip()
    number = _flag_value(argv, "--pr").strip()
    head_sha = _flag_value(argv, "--head").strip()
    result = {
        "ok": False,
        "violations": [],
        "comments": 0,
        "records": 0,
        "unverified": 0,
        "unpublished": [],
        "error": "",
    }
    try:
        if not repo or not number or not head_sha:
            result["error"] = "--repo, --pr and --head are all required"
        else:
            bindings = resolve_marker_bindings(argv, environ)
            # ONE paginated read serves both selectors: this mode runs on
            # every full readiness evaluation, and the hourly GITHUB_TOKEN
            # pool every workflow here shares is what it would otherwise
            # spend walking the same comment pages twice.
            # Filtered page by page, so a comment neither selector wants is
            # never retained.
            authors = resolve_marker_authors(argv, environ)
            all_comments = fetch_issue_comments(
                repo,
                number,
                keep=lambda c: (
                    _review_contract.is_disposition_comment(c) or is_trusted_bot_comment(c, authors)
                ),
            )
            comments = bot_comments = None
            if all_comments is not None:
                comments = _review_contract.select_disposition_comments(all_comments)
                bot_comments = select_bot_comments(all_comments, authors)
            records = writer_disposition_records(repo, comments)
            if comments is None or bot_comments is None or records is None:
                result["error"] = "disposition or marker comments could not be read"
            else:
                result["comments"] = len(comments)
                result["records"] = len(records)
                result["unverified"] = len(comments) - len(records)
                # One violation per line downstream, so a newline inside one
                # would forge an extra blocker line. Nothing in the strings can
                # carry one today (logins, span ids and target= are all charset-
                # limited), which is exactly why flattening here is free.
                result["violations"] = [
                    " ".join(sanitize(v).split())
                    for v in disposition_violations(records, bot_comments, head_sha, bindings)
                ]
                # The whole-design lanes that owe this head a verdict and have
                # none. Free here: the trusted comment set is already read, and
                # ``evaluate_reviewer_markers`` is the definition both gates
                # answer from, so the required status and ``pr_status.py``
                # cannot disagree about a head.
                #
                # PINNED on the lane set, not discovered from what posted: a
                # lane whose FIRST publish fails leaves no slot at all, and
                # discovery cannot see a lane that has not spoken, so the
                # absence would score as reviewed -- the same fail-open one
                # round later, reached by a PR's ordinary first round. Under a
                # pin, absence reads as stale, which is the whole point.
                #
                # Two exemptions survive the pin, and neither is respelled
                # here. A lane a writer adjudicated at this head is already out
                # of ``stale`` (the override path does not re-run the model, so
                # no stamp can exist). A lane holding a stampless notice THAT
                # NAMES THIS HEAD is ``stampless`` -- it said it did not review
                # this head, and a re-run produces the same notice, so holding
                # it would strand a green PR on a lane nothing can fill. A
                # notice naming an earlier head is not an answer for this one.
                #
                # Scoped to slots that EXIST: a lane with no slot at all is not
                # reported here. Absence has two causes that read identically
                # from the comments -- a lane that published nothing because it
                # deliberately had nothing to say, and one whose upsert was
                # lost -- and only the lane itself can tell them apart, by
                # leaving a notice. Reporting absence without that would hold
                # every backend-only revision on a lane a re-run cannot fill.
                # Closing that half means giving the scope-skip arms a notice to
                # write, which is a change to those lanes, not to this reader.
                markers = evaluate_reviewer_markers(
                    bot_comments,
                    head_sha,
                    bindings,
                    only=WHOLE_DESIGN_LANES,
                    authors=resolve_marker_authors(argv, environ),
                )
                exempt = set(markers["stampless"])
                present = set(markers["seen"])
                result["unpublished"] = [
                    name for name in markers["stale"] if name not in exempt and name in present
                ]
                result["ok"] = True
    except Exception as exc:  # noqa: BLE001 - any failure is "unknown", never red
        result["error"] = "{}: {}".format(type(exc).__name__, exc)
    print(json.dumps(result, sort_keys=True))
    return 0


# A gate's output is read by a machine, and empty output is not a clean result:
# a caller wrapping it in `timeout 60` gets rc=124 and an empty stdout, which is
# indistinguishable from a gate that answered "nothing found" unless the caller
# knows to check. `run` has no timeout, which is right for the interactive report
# and wrong here, so the gate path gets its own bounded runner and an overall
# budget. Together they guarantee the gate PRINTS, within roughly the budget plus
# one call, whatever the network does.
_GATE_CALL_TIMEOUT_SECS = 20
_GATE_TOTAL_BUDGET_SECS = 45


def bounded_run(args, timeout=_GATE_CALL_TIMEOUT_SECS):
    """``run`` with a per-call wall-clock bound; 124 and a reason on timeout."""
    try:
        p = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", "{}: no answer within {}s".format(args[0], timeout)
    except OSError as exc:
        return 127, "", "{}: {}".format(args[0], exc)


def supersession_gate(argv, environ):
    """Report verdicts for one head that a later sample at the same head replaced.

    A lane's marker comment is one slot keyed on the lane alone, so a second
    sample at one head overwrites the first and the board keeps only the
    survivor. Every body the slot held survives in GraphQL userContentEdits, so
    this gate reads that history rather than adding a record to it.

    Usage: --supersession-gate --repo OWNER/NAME --pr N --head SHA
    (--marker-bindings / --marker-authors and their env forms apply as usual.)

    Prints ``{"ok", "blocking_dropped", "superseded", "lanes_seen", "lanes",
    "error"}``. ``superseded`` is the total count of replaced same-head samples,
    ``lanes_seen`` is how many lanes were examined, and ``blocking_dropped``
    names the lanes where a replaced sample BLOCKED this head and the presented
    body does not -- the only direction that can turn a judged block into a clean
    board. A sample blocks in either spelling the lanes use, ``[BLOCK-MERGE]
    <head>`` or a whole-design ``<Lane>-Verdict: BLOCK`` line. A presented body
    whose workflow-authored heading says adjudication downgraded this head is a
    SANCTIONED clear and is not named.

    ``ok`` False means UNKNOWN and must be treated as pending, never as "nothing
    was superseded". It covers an unreadable history, a head where NO lane was
    examined, and the reads not finishing inside the gate's own time budget --
    because a clean answer over an empty population, and no answer at all, are
    both calm reported from having observed nothing. Every call is bounded and the
    whole gate is bounded, so it always prints. Exit status is 0 for both
    outcomes, so a non-zero exit means this script itself failed to run.
    """
    repo = _flag_value(argv, "--repo").strip()
    number = _flag_value(argv, "--pr").strip()
    head_sha = _flag_value(argv, "--head").strip()
    result = {
        "ok": False,
        "blocking_dropped": [],
        "superseded": 0,
        "lanes_seen": 0,
        "lanes": [],
        "error": "",
    }
    try:
        if not repo or not number or not head_sha:
            result["error"] = "--repo, --pr and --head are all required"
        else:
            deadline = time.monotonic() + _GATE_TOTAL_BUDGET_SECS
            bindings = resolve_marker_bindings(argv, environ)
            authors = resolve_marker_authors(argv, environ)
            comments = fetch_bot_comments(repo, number, authors, bounded_run)
            if comments is None:
                result["error"] = "bot comments could not be read"
            else:
                found = superseded_verdicts(
                    comments, head_sha, bindings, bounded_run, authors, deadline
                )
                result["ok"] = bool(found["ok"])
                result["error"] = found["error"]
                result["blocking_dropped"] = found["blocking_dropped"]
                result["lanes"] = found["lanes"]
                result["lanes_seen"] = found["lanes_seen"]
                result["superseded"] = sum(len(e["superseded"]) for e in found["lanes"])
    except Exception as exc:  # noqa: BLE001 - any failure is "unknown", never red
        result["ok"] = False
        result["error"] = "{}: {}".format(type(exc).__name__, exc)
    print(json.dumps(result, sort_keys=True))
    return 0


def main(argv):
    # Before the auth probe and PR detection below: this mode is given its
    # repo/PR/head explicitly and must stay usable from a workflow runner,
    # where `gh auth status` prose is noise and the JSON is the whole output.
    if "--disposition-gate" in argv[1:]:
        return disposition_gate(argv, os.environ)

    if "--supersession-gate" in argv[1:]:
        return supersession_gate(argv, os.environ)

    if run(["gh", "auth", "status"])[0] != 0:
        err("ERROR: gh not found or not authenticated. Run: gh auth login")
        return 2

    readiness_context = resolve_readiness_context(argv, os.environ)
    reviewers_filter = resolve_reviewers(argv, os.environ)
    pos = positional_args(argv[1:])
    pr = pos[0] if pos else ""
    if not pr:
        pr = run(["gh", "pr", "view", "--json", "number", "-q", ".number"])[1]
    if not pr:
        err("ERROR: no PR number given and none found for the current branch.")
        return 2

    fields = (
        "number,title,state,isDraft,mergeable,mergeStateStatus,"
        "reviewDecision,url,headRefName,headRefOid,baseRefName,"
        "body,closingIssuesReferences"
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
    head_sha = (d.get("headRefOid") or "").strip()
    # The viewed PR's own repository: the rollup read addresses it by owner,
    # name and number, and the positional argument may be a URL for a repository
    # other than the cwd's checkout.
    repo = detect_repo(d.get("url") or "")
    rollup_entries, rollup_notice = fetch_check_rollup(repo, d.get("number"), head_sha)
    rollup = collapse_superseded(rollup_entries)

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
    if rollup_notice:
        print("  NOTICE: " + rollup_notice)
    n_running = n_fail = 0
    failing_checks = []
    readiness_kind = None
    for e in rollup:
        kind = classify_check(e)
        if kind == "running":
            n_running += 1
        elif kind == "fail":
            n_fail += 1
        name = sanitize(e.get("name") or e.get("context") or "check")
        if kind == "fail":
            failing_checks.append(failing_check_identity(e))
        # Only the legacy StatusContext we publish is authoritative. A CheckRun
        # can share the display name but is a different, independently writable
        # namespace and must remain part of the ordinary rollup.
        if e.get("context") == readiness_context:
            readiness_kind = kind
        shown = (e.get("status") or "-") + "/" + (e.get("conclusion") or e.get("state") or "-")
        print("  - {}: {}  [{}]".format(name, shown, kind))
    print("  rollup: total={} running={} failing={}".format(len(rollup), n_running, n_fail))
    print("  aggregate readiness: {}".format(readiness_kind or "not published"))
    _closes = d.get("closingIssuesReferences") or []
    print(
        "  closes on merge: {}".format(
            ", ".join("#{}".format(i.get("number")) for i in _closes) if _closes else "nothing"
        )
    )
    # Advisory, never a gate. The measured failure was that nobody was ever
    # ASKED for a trailer, not that authors refuse to write one: across 600
    # merged PRs the host's auto-close worked every time it had a keyword to
    # act on. So report the gap where the author will see it and let them
    # decide -- blocking a green PR on bookkeeping costs more than it saves,
    # and an issue-less PR is legitimate.
    _closing = closing_link_reason(d.get("body"), _closes, repo)
    if _closing:
        print("  NOTICE: " + _closing)

    n_unresolved = unresolved_thread_count(d.get("number"))
    print("-- Review threads " + "-" * 35)
    print(
        "  unresolved threads (advisory): " + ("?" if n_unresolved is None else str(n_unresolved))
    )

    # Reviewer-side conditions: the stamp and the comment body
    # are the signal -- never the review workflow's run conclusion, which is
    # unreliable in both directions on this repo.
    marker_authors = resolve_marker_authors(argv, os.environ)
    marker_bindings = resolve_marker_bindings(argv, os.environ)
    bot_comments = fetch_bot_comments(repo, d.get("number"), marker_authors)
    marker_eval = evaluate_reviewer_markers(
        bot_comments,
        head_sha,
        marker_bindings,
        only=reviewers_filter,
        authors=marker_authors,
    )
    print("-- Reviewer markers (head {}) ".format(sanitize(head_sha[:12]) or "?") + "-" * 20)
    if not marker_eval["ok"]:
        print("  ERROR: bot comments could not be read (fail-closed)")
    elif (
        not marker_eval["findings"]
        and not marker_eval["stale"]
        and not marker_eval.get("overridden")
    ):
        if reviewers_filter:
            print(
                "  (no [<NAME>-REVIEWED] stamps found for filter: "
                + ", ".join(sorted(reviewers_filter))
                + ")"
            )
        else:
            print("  (no [<NAME>-REVIEWED] stamps found in bot comments)")
    else:
        for name in sorted(marker_eval["findings"]):
            print(
                "  - {}: fresh{}{}{}{}".format(
                    sanitize(name),
                    "  [BLOCK-MERGE]" if name in marker_eval["blocking"] else "",
                    (
                        "  ({} BLOCKING line(s))".format(marker_eval["findings"][name])
                        if marker_eval["findings"][name]
                        else ""
                    ),
                    (
                        "  [stamp elided the head's middle - emitter transcription "
                        "artifact, verified against this head]"
                        if name in (marker_eval.get("elided") or ())
                        else ""
                    ),
                    (
                        "  verdict={}{}".format(
                            marker_eval["verdicts"][name],
                            (
                                "  <- whole-design review: read it BEFORE fixing "
                                "line-level findings"
                                if marker_eval["verdicts"][name] == "BLOCK"
                                else ""
                            ),
                        )
                        if name in (marker_eval.get("verdicts") or {})
                        else ""
                    ),
                )
            )
        for name in marker_eval["stale"]:
            print("  - {}: STALE (stamp names an older head)".format(sanitize(name)))
        # Named distinctly from `fresh`, because the two are different
        # evidence: no model verdict exists for this head, and the row must not
        # read as though one does.
        for name, actor in sorted((marker_eval.get("overridden") or {}).items()):
            print(
                "  - {}: OVERRIDDEN by @{}{} (human judgment recorded for this head; "
                "the model was not re-run)".format(
                    sanitize(name),
                    sanitize(actor),
                    override_class_label((marker_eval.get("override_classes") or {}).get(name)),
                )
            )

    # Each whole-design lane's reason, read from the lane bodies rather than
    # from the marker rows above: a pinned fleet such as `--reviewers gpt,opus`
    # lists no whole-design lane there. Printed every cycle so a BLOCK cannot hide
    # behind a pin. It changes no exit code.
    if marker_eval.get("ok") and bot_comments is not None:
        punchlines = design_lane_punchlines(bot_comments, head_sha, marker_bindings)
        lane_verdicts = design_lane_verdicts(bot_comments, head_sha, marker_bindings)
        print("-- Whole-design punchlines, untrusted (head {}) --".format(sanitize(head_sha[:12])))
        if not punchlines:
            print("  (none to check on this head)")
        for name in sorted(punchlines):
            print(
                "  - {} ({}): {}".format(
                    sanitize(name),
                    sanitize(lane_verdicts.get(name) or "?"),
                    sanitize(redact(punchlines[name]))[:280],
                )
            )

    # Disposition-rule gate: a repository writer's disposition
    # comment must claim exactly one span= finding identity from its own
    # target= lane. Each record is validated against the findings stamped for
    # the head its head= says it judged (in the ordinary fix-then-push round
    # that is the PRIOR head) and the current one, so the rule "one comment
    # covers one lane, one rationale covers one finding" is mechanical rather
    # than prose.
    disposition_comments = fetch_disposition_comments(repo, d.get("number"))
    disposition_records = writer_disposition_records(repo, disposition_comments)
    disposition_violation_list: list = []
    # BOTH inputs must be readable: the records (the rulings) and the trusted
    # reviewer comments (the finding map the rulings are validated against).
    # An unreadable finding map is not "no findings" -- computing with an
    # empty map would silently drop every evidence-bearing class while the
    # round runs, and the judged-head evidence is rewritten in place when it
    # completes.
    disposition_ok = disposition_records is not None and bot_comments is not None
    if disposition_ok:
        disposition_violation_list = disposition_violations(
            disposition_records, bot_comments, head_sha, marker_bindings
        )
    disposition_eval = {"ok": disposition_ok, "violations": disposition_violation_list}
    # A second sample at ONE head replaces the first in the lane's slot, so the
    # board keeps only the survivor and the replaced verdict is presented
    # nowhere. Report it here from the stored history rather than leaving the
    # local loop to infer it. Costs one GraphQL read per bound lane, so it runs
    # only when the markers themselves were readable -- with those unread there
    # is no lane set to ask about.
    supersession_eval = None
    if marker_eval.get("ok") and bot_comments is not None:
        supersession_eval = superseded_verdicts(
            bot_comments, head_sha, marker_bindings, run, marker_authors
        )
        print("-- Superseded verdicts (head {}) ".format(sanitize(head_sha[:12]) or "?") + "-" * 17)
        if not supersession_eval["ok"]:
            print(
                "  UNKNOWN: {} (fail-closed - this is not a clean result)".format(
                    sanitize(supersession_eval["error"] or "supersession could not be evaluated")
                )
            )
        else:
            replaced = [e for e in supersession_eval["lanes"] if e["superseded"]]
            if not replaced:
                print(
                    "  (no verdict for this head was replaced by a later sample; "
                    "{} lane(s) examined)".format(supersession_eval["lanes_seen"])
                )
            for entry in replaced:
                for sample in entry["superseded"]:
                    print(
                        "  - {}: sample at {} was replaced{}{}{}".format(
                            sanitize(entry["lane"]),
                            sanitize(sample["at"]),
                            "  [BLOCK-MERGE]" if sample["blocking"] else "",
                            (
                                "  verdict {}".format(sanitize(sample["verdict"]))
                                if sample["verdict"]
                                else ""
                            ),
                            (
                                "  ({} BLOCKING line(s))".format(sample["findings"])
                                if sample["findings"]
                                else ""
                            ),
                        )
                    )
            for lane in supersession_eval["blocking_dropped"]:
                print(
                    "  BLOCKING DROPPED: {} blocked this head in a replaced sample and "
                    "the body now presented does not - read the history before "
                    "treating this lane as clear".format(sanitize(lane))
                )

    print("-- Disposition records (one lane, one rationale per finding) " + "-" * 6)
    if not disposition_ok:
        print("  ERROR: disposition records could not be established (fail-closed)")
    elif not disposition_comments:
        print("  (no disposition comments)")
    else:
        # Both counts, so a degraded state is visible instead of reading like
        # an empty PR: zero verified records above a non-zero comment count
        # means the check is INERT for those comments -- non-writer authors,
        # or a token that cannot read collaborator permissions (the endpoint
        # needs push access).
        print(
            "  disposition-marked comment(s): {}  from verified writers: {}".format(
                len(disposition_comments), len(disposition_records)
            )
        )
        if not disposition_records:
            print(
                "  NOTICE: none verified as a repository writer's - the "
                "disposition check is inert for these comments"
            )
        unknown_targets = sorted(
            {
                r["target"]
                for r in disposition_records
                if not r["malformed"]
                and r["target"] not in {n.lower() for n in marker_bindings.values()}
            }
        )
        if unknown_targets:
            # Advisory only: an unbound lane (e.g. first-principles) has no
            # extractable finding identity, but a typo'd target= looks the
            # same and silently escapes the claim requirement -- say so.
            print(
                "  NOTICE: target lane(s) outside the marker bindings "
                "(claim checks do not apply): " + ", ".join(sanitize(t) for t in unknown_targets)
            )
        for v in disposition_violation_list:
            print("  VIOLATION: " + sanitize(v))
        if not disposition_violation_list:
            print("  (no disposition-rule violations)")

    # Assert a pull_request-event run exists for the current head, but only on
    # a PR that demonstrably uses Actions (a rollup entry with a workflowName);
    # a repo reporting only legacy statuses must not be blocked forever by an
    # assertion about workflows it does not run.
    head_run = "skip"
    if (
        head_sha
        and any(e.get("workflowName") for e in rollup)
        and head_run_check_enabled(argv, os.environ)
    ):
        head_run = head_run_exists(repo, head_sha, rollup)
        if head_run is True:
            run_shown = "yes"
        elif head_run is False:
            run_shown = "NO (stale or conflicted?)"
        elif head_run == "skip":
            run_shown = "n/a (this head's CI is driven by other events)"
        else:
            run_shown = "? (could not confirm)"
        print("  pull_request run for current head: " + run_shown)
    # Whether that run's verdict still describes today's base. Printed beside the
    # rollup because it qualifies the rollup: a green measured on a base that has
    # since moved in this PR's own files is a green about a tree nobody has.
    green_age = probe_green_age(d.get("baseRefName") or "", head_sha, d.get("number"))
    print(green_age_line(green_age))
    print("=" * 54)

    code, status = decide(
        state=state,
        mergeable=mergeable,
        merge_state=merge_state,
        decision=decision,
        draft=draft,
        readiness_kind=readiness_kind,
        n_running=n_running,
        n_fail=n_fail,
        n_checks=len(rollup),
        readiness_context=readiness_context,
        marker_eval=marker_eval,
        head_run=head_run,
        rollup_notice=rollup_notice,
        disposition_eval=disposition_eval,
        supersession_eval=supersession_eval,
    )
    print(status)
    if "--json" in argv[1:]:
        # Last line of stdout, one compact line, keys sorted: a polling loop
        # compares consecutive runs byte-for-byte, so the serialization has to
        # be stable for an unchanged PR.
        print(
            json.dumps(
                build_report(
                    number=d.get("number"),
                    url=d.get("url") or "",
                    head_sha=head_sha,
                    readiness_kind=readiness_kind,
                    failing_checks=failing_checks,
                    n_fail=n_fail,
                    n_unresolved=n_unresolved,
                    marker_eval=marker_eval,
                    code=code,
                    status=status,
                    green_age=green_age,
                    supersession_eval=supersession_eval,
                ),
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
