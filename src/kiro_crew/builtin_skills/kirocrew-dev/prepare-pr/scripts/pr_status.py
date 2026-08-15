#!/usr/bin/env python3
"""pr_status.py - the decisive PR-readiness gate for the prepare-pr skill.

Prints PR state + every CI check + advisory unresolved-thread count and returns
an exit code that drives the poll loop. The aggregate ``PR Readiness`` status is
authoritative when present; older PRs fall back to the full check rollup.
Stdlib only; portable.

Usage:  python3 pr_status.py [pr-number] [--readiness-context NAME]
                             [--reviewers NAME1,NAME2]
        (no number -> auto-detect the PR for the current branch;
         --readiness-context / PREPARE_PR_READINESS_CONTEXT override the
         aggregate status-context name, default "PR Readiness";
         --reviewers / PREPARE_PR_REVIEWERS pin the reviewer fleet: only the
         named stamps are evaluated AND each named reviewer must have a fresh
         stamp; by default, every ``[<NAME>-REVIEWED]`` stamp found in bot
         comments is held to freshness, and absence is not required)

Exit codes:
   0  CLEAN     - open, non-draft, MERGEABLE, no CHANGES_REQUESTED, aggregate
                  PR Readiness (or the legacy full rollup) passed, every
                  reviewer stamp matches the current head, no [BLOCK-MERGE]
                  marker for the current head, and a pull_request-event run
                  exists for the current head (when the repo uses Actions)
  10  RUNNING   - a required check is still queued/in-progress, or mergeability
                  has not been computed yet
  20  BLOCKED   - failing readiness, merge conflict, draft, CHANGES_REQUESTED,
                  a terminal PR state (MERGED/CLOSED), a stale reviewer stamp,
                  a blocking review marker on the current head, no
                  pull_request-event run for the current head, or anything
                  that cannot be confirmed
   2  ENV ERROR - gh missing or not authenticated, or PR not found
"""
import json
import os
import re
import subprocess
import sys

# Strip ANSI escape sequences and C0/C1 control chars from untrusted printed
# text (PR titles / check names are attacker-controllable) to prevent
# terminal/prompt injection into the agent session. The C1 range (\x80-\x9f)
# matters: U+009B is the single-byte CSI, equivalent to ESC-[.
_CTRL_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def sanitize(s):
    return _CTRL_RE.sub("", s or "")


# Explicit state classification (classify every state; fail closed).
PASS_CONCLUSIONS = {"SUCCESS", "NEUTRAL", "SKIPPED"}
# StatusContext (legacy commit statuses) use .state rather than .conclusion.
CTX_PASS = {"SUCCESS"}
CTX_RUNNING = {"PENDING", "EXPECTED"}
DEFAULT_READINESS_CONTEXT = "PR Readiness"

# A host closes an issue on merge ONLY for these verbs. "Related: #n", "Part of
# #n" and a bare "#n" render as links and close nothing, which is how finished
# work merges while its issue stays open forever.
_CLOSING_VERB = r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)"
# A repository slug, in GitHub's own charset. Deliberately narrow so a stray
# path fragment cannot masquerade as a qualified reference.
_REPO_SLUG = r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*"
# The three reference targets the host actually resolves.
_ISSUE_TARGET = (
    r"(?:(?:" + _REPO_SLUG + r")?#\d+"
    r"|https?://[A-Za-z0-9.-]+/" + _REPO_SLUG + r"/issues/\d+)"
)
_CLOSING_REF = _CLOSING_VERB + r"[ \t]*:?[ \t]+" + _ISSUE_TARGET
# THE ACCEPTED EXPLICIT-TRAILER GRAMMAR, in full:
#
#   trailer := indent? bullet? ref (sep ref)* punct? html-comment?
#   ref     := verb ':'? sp target
#   verb    := close|closes|closed | fix|fixes|fixed | resolve|resolves|resolved
#   target  := '#123' | 'owner/repo#123' | 'https://host/owner/repo/issues/123'
#   sep     := ',' | ';' | 'and'
#
# Two properties are load-bearing.
#
# (1) The trailer must occupy the WHOLE visible line. An unanchored substring
# match also accepted prose that merely MENTIONS a past close -- "Fixed #123 in
# an earlier release; this PR only adds tests." -- and then told the author the
# keyword was fine and the NUMBER was wrong, the one reading that is never true
# for that line. A declaration is a trailer, not a mention. (Trailing
# whitespace, one sentence-ending '.'/';', a CR from a CRLF body, and a trailing
# HTML comment stay accepted, since none of them make the line prose.)
#
# (2) Qualified and URL targets are accepted. The host resolves them, so a body
# carrying one is NOT a body that forgot the verb. No reconciliation against the
# host's own repository identity is needed to accept them here: this classifier
# is only ever reached when the host resolved NOTHING, and the message it
# produces ("the verb is fine, check the reference") is correct whether the
# reference names this repository or another one.
#
# Markdown code fences are NOT stripped -- a fenced line that is itself a bare
# trailer still matches, which is why the notice text names that possibility
# rather than claiming the number is wrong.
_CLOSING_KW_RE = re.compile(
    r"^[ \t]*(?:[-*+][ \t]+)?"
    + _CLOSING_REF
    + r"(?:(?:[ \t]*[,;][ \t]*|[ \t]+and[ \t]+)"
    + _CLOSING_REF
    + r")*[ \t]*[.;]?[ \t]*(?:<!--.*?-->[ \t]*)?\r?$",
    re.IGNORECASE | re.MULTILINE,
)
# Any issue-ish reference at all, used to tell "forgot the verb" from
# "genuinely closes nothing". Mirrors the same three targets, so a qualified
# ref or an issue URL written without a verb is reported as a missing keyword
# rather than as a body with no issue link at all.
_BARE_REF_RE = re.compile(
    r"(?<![\w/])(?:" + _REPO_SLUG + r")?#\d+\b"
    r"|https?://[A-Za-z0-9.-]+/" + _REPO_SLUG + r"/issues/\d+\b",
    re.IGNORECASE,
)
# Explicit opt-out so an issue-less PR can say so once instead of being asked
# every round. Anchored at column 0 and requires the colon, because an
# UNANCHORED substring is satisfied by any prose that merely discusses this
# check — including the instruction block of our own body template, which would
# make an author who copies the template and skips that section look like they
# declared something. A declaration is a trailer, not a mention.
# The phrasing deliberately contains no GitHub closing keyword
# (close/closes/closed, fix/fixes/fixed, resolve/resolves/resolved): a keyword
# directly before the colon would turn a '#<n>' at the start of the <why> into
# '<keyword>: #<n>', which GitHub parses as a close-on-merge trigger — the
# opt-out would then auto-close the very issue it explains not closing.
_NO_ISSUE_RE = re.compile(r"^no linked issue[ \t]*:", re.IGNORECASE | re.MULTILINE)


def closing_link_reason(body, closing_refs):
    """Return an advisory reason when no issue will close, else None.

    ADVISORY ONLY — the caller prints this, it never changes the exit code. An
    issue-less PR is legitimate, and a green PR should not be held on
    bookkeeping.

    ``closing_refs`` is the host's OWN resolution of the body (the
    ``closingIssuesReferences`` field), so it is the truth about what will
    actually close. The body regexes only classify *why* it resolved to
    nothing, which is what makes the message actionable. The accepted
    explicit-trailer grammar is stated in full above ``_CLOSING_KW_RE``: a
    whole visible line carrying one or more ``<verb> <target>`` references,
    where a target may be ``#123``, ``owner/repo#123`` or an issue URL.
    """
    if closing_refs:
        return None
    body = body or ""
    if _NO_ISSUE_RE.search(body):
        return None
    if _CLOSING_KW_RE.search(body):
        # Verb and reference are both well-formed but the host resolved
        # nothing: wrong repository, a code fence, or an issue that is already
        # closed/nonexistent. Never report this as the missing-verb case.
        return (
            "body has a closing keyword but the host resolved no issue "
            "(check the repository and number, and that it is not inside a "
            "code fence)"
        )
    if _BARE_REF_RE.search(body):
        return (
            "body references an issue with no closing keyword - use "
            "'Fixes #<n>' so it closes on merge, or state 'no linked issue: <why>'"
        )
    return (
        "no issue link - add 'Fixes #<n>', or state 'no linked issue: <why>' "
        "to record that the omission is deliberate"
    )


# Page cap so a pathological PR can't make us loop unbounded (100 * 50 = 5000).
_MAX_THREAD_PAGES = 50
_MAX_COMMENT_PAGES = 50

# Reviewer-marker contract (mirrored in pr_findings.py; a parity test pins the
# two copies together -- each script stays standalone-copyable by design).
# The review workflows stamp their verdict comment with a per-SHA proof line
# and, only for a blocking verdict, a second per-SHA block marker:
#   [<NAME>-REVIEWED] <full-sha>     e.g. [GPT-REVIEWED] / [OPUS-REVIEWED] /
#                                         [DESIGN-REVIEWED] / [UX-REVIEWED]
#   [BLOCK-MERGE] <full-sha>
# Advisory findings appear as lines beginning with the literal token FINDING.
# The conclusion of the review workflow run is deliberately NOT a signal here:
# on this repo it is unreliable in both directions (red on healthy reviews,
# green while the body carries findings) -- the stamp and the body are the
# signal. Bots update their comment in place, so an old [BLOCK-MERGE] for a
# superseded head disappears or keeps naming the old SHA; matching against the
# current head filters both.
REVIEWED_STAMP_RE = re.compile(r"\[([A-Z][A-Z0-9_-]*)-REVIEWED\]\s+([0-9a-f]{7,40})\b")
BLOCK_MERGE_RE = re.compile(r"\[BLOCK-MERGE\]\s+([0-9a-f]{7,40})\b")
FINDING_LINE_RE = re.compile(r"^\s*FINDING\b", re.MULTILINE)

# Only comments authored by the repo's own workflow actor count as marker
# sources. `user.type == "Bot"` alone is spoofable: a third-party app that
# echoes PR-controlled text (a coverage bot quoting a diff, a triage bot
# quoting the body) would post an attacker-chosen `[<NAME>-REVIEWED] <head>`
# and forge freshness. The review workflows all post through the Actions
# actor; same-repo workflows share the emitters' trust level, third-party
# apps do not. Override with --marker-authors / PREPARE_PR_MARKER_AUTHORS for
# a repo whose reviewers post under app-specific logins.
DEFAULT_MARKER_AUTHORS = ("github-actions[bot]",)

# Reviewer identity must come from WORKFLOW-AUTHORED bytes, never from model
# output: each review workflow upserts its comment with its own HTML key as
# the comment's LEADING bytes, written by the workflow template before any
# model text is interpolated. Binding each key to its reviewer name means a
# stamp counts only inside its own lane's comment -- injected model output in
# one lane can never stamp another reviewer's freshness, whatever names it
# emits. Override with --marker-bindings / PREPARE_PR_MARKER_BINDINGS
# ("key=NAME,key=NAME") for a repo with different comment keys.
DEFAULT_MARKER_BINDINGS = (
    ("codex-ai-review", "GPT"),
    ("claude-ai-review", "OPUS"),
    ("design-review", "DESIGN"),
    ("ux-review", "UX"),
)
# The key is authoritative only at the very start of the body (template-
# controlled position); anywhere later it could be model output.
_COMMENT_KEY_RE = re.compile(r"\A\s*<!--\s*([a-z0-9-]+)\s*-->")


def comment_key(body):
    m = _COMMENT_KEY_RE.match(body or "")
    return m.group(1) if m else ""


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


def sha_matches(stamp_sha, head_sha):
    """True when a stamped SHA identifies the current head (>=7-hex prefix)."""
    return bool(stamp_sha) and len(stamp_sha) >= 7 and head_sha.startswith(stamp_sha)


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


def positional_args(argv):
    """Return argv with the value-taking flags (and their values) removed."""
    out = []
    skip = False
    for a in argv:
        if skip:
            skip = False
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


def fetch_bot_comments(repo, number, trusted_authors):
    """Trusted marker-source comments on the PR, across pages; None on error.

    Paginated by hand (PRs here routinely carry 50+ bot comments; a single
    unpaginated read silently truncates). A comment counts only when its
    author is a Bot AND its login is in ``trusted_authors``: the Bot-type
    check alone is spoofable -- any third-party app that echoes PR-controlled
    text would post an attacker-chosen marker and forge freshness. Returns
    None (uncertain, the caller fails closed) on any API/parse error or when
    the page cap is hit with more pages left.
    """
    if not repo:
        return None
    comments: list = []
    for page in range(1, _MAX_COMMENT_PAGES + 1):
        rc, out, _ = run(
            [
                "gh",
                "api",
                "repos/{}/issues/{}/comments?per_page=100&page={}".format(repo, number, page),
            ]
        )
        if rc != 0 or not out:
            return None
        try:
            batch = json.loads(out)
        except ValueError:
            return None
        if not isinstance(batch, list):
            return None
        for c in batch:
            if not isinstance(c, dict):
                continue
            user = c.get("user") or {}
            if user.get("type") != "Bot":
                continue
            if (user.get("login") or "").lower() not in trusted_authors:
                continue
            comments.append(c)
        if len(batch) < 100:
            return comments
    return None


def evaluate_reviewer_markers(comments, head_sha, bindings, only=None):
    """Evaluate reviewer stamps and blocking markers against the current head.

    Returns a dict:
      ok        -- False when the comments could not be read (fail-closed)
      stale     -- sorted reviewer names with no fresh stamp for the head
      blocking  -- sorted reviewer names with [BLOCK-MERGE] <current head>
      findings  -- {name: advisory FINDING-line count} for fresh comments

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
    """
    if comments is None or not head_sha:
        return {"ok": False, "stale": [], "blocking": [], "findings": {}}
    fresh_by_name: dict = {name: False for name in (only or ())}
    findings: dict = {}
    blocking = set()
    for c in comments:
        body = c.get("body") or ""
        name = bindings.get(comment_key(body))
        stamps = REVIEWED_STAMP_RE.findall(body)
        if name and (only is None or name in only):
            own_stamps = [sha for stamp_name, sha in stamps if stamp_name == name]
            # A bound lane is held to freshness in DISCOVERY mode only when
            # its comment carries at least one of its own stamps: the UX and
            # Design workflows rewrite their keyed comment to a stampless
            # "skipped"/"could not complete" notice by design (advisory lanes
            # must not block), and enrolling those would pin exit 20 on a
            # green PR forever. A PINNED lane stays required regardless --
            # that is what pinning means.
            if only is not None or own_stamps:
                fresh = any(sha_matches(sha, head_sha) for sha in own_stamps)
                fresh_by_name[name] = fresh_by_name.get(name, False) or fresh
                if fresh:
                    findings[name] = len(FINDING_LINE_RE.findall(body))
        for sha in BLOCK_MERGE_RE.findall(body):
            if sha_matches(sha, head_sha):
                blocking.add(name or "(unattributed)")
    stale = sorted(n for n, fresh in fresh_by_name.items() if not fresh)
    return {"ok": True, "stale": stale, "blocking": sorted(blocking), "findings": findings}


def head_run_exists(repo, head_sha):
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
    """
    if not repo or not head_sha:
        return None
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
    if events:
        return "skip"
    return False


def decide(
    state,
    mergeable,
    merge_state,
    decision,
    draft,
    readiness_kind,
    n_running,
    n_fail,
    n_checks,
    readiness_context,
    marker_eval=None,
    head_run="skip",
):
    """Resolve PR state to (exit_code, status line). Fail-closed.

    Exit codes: 0 = clean, 10 = wait (nothing to do yet), 20 = act.

    Precedence is the load-bearing part, and it is ordered by "can waiting
    change this answer?" rather than by how the fields arrive:

    1. A non-open PR is terminal, and must be decided BEFORE any wait: GitHub
       reports mergeable=UNKNOWN for merged/closed PRs forever, so waiting on
       it returns 10 on every poll and a loop never stops.
    2. Conditions waiting CANNOT fix outrank "still running". A conflicted PR is
       the case that matters: the host cannot build a merge ref for it, so it
       dispatches no pull_request workflows at all and every check visible
       belongs to the old head. Ranking in-flight checks first reports "running"
       forever while nothing can complete -- a stall only a human notices.
       BEHIND, draft and CHANGES_REQUESTED behave the same way: each survives
       any amount of waiting and needs the author to act.
    3. Only then is "still running" a wait, and an uncomputed mergeability too.
    4. Everything left is a check-result verdict -- including the reviewer-side
       conditions: ``marker_eval`` (from evaluate_reviewer_markers; None skips
       the gate) and ``head_run`` (True/False/None from head_run_exists; the
       string "skip" skips the gate). Both are evaluated ONLY here, after the
       running gate: while a round is in flight the reviewer bots may simply
       not have posted yet, and a stale stamp mid-round is expected, not a
       defect. Both fail closed on "could not read". Advisory FINDING counts
       never gate -- whether a non-blocking finding should hold the loop open
       is a judgment call the exit code deliberately does not make.
    """
    if state != "OPEN":
        return 20, "STATUS: BLOCKED - PR state is {} (not OPEN; terminal)".format(state or "?")

    blocked_now = []
    if mergeable == "CONFLICTING" or merge_state in ("DIRTY", "CONFLICTING"):
        blocked_now.append("merge conflict / not mergeable")
    if merge_state == "BEHIND":
        blocked_now.append("branch is BEHIND base - re-sync onto the latest base")
    if draft:
        blocked_now.append("PR is a draft")
    if decision == "CHANGES_REQUESTED":
        blocked_now.append("review decision is CHANGES_REQUESTED")
    if blocked_now:
        return 20, "STATUS: BLOCKED - " + "; ".join(blocked_now)

    # Once published, the aggregate is authoritative over stale duplicate
    # checks in the rollup. Legacy PRs without it still use the full rollup.
    if readiness_kind == "running" or (readiness_kind is None and n_running > 0):
        return 10, "STATUS: RUNNING (round not complete)"
    if mergeable not in ("MERGEABLE", "CONFLICTING"):
        return 10, "STATUS: RUNNING (mergeability not yet computed: {})".format(
            mergeable or "UNKNOWN"
        )

    reasons = []
    if readiness_kind == "fail":
        reasons.append("{} reported action required".format(readiness_context))
    elif readiness_kind is None and n_fail > 0:
        reasons.append("{} check(s) failed".format(n_fail))
    if n_checks == 0:
        reasons.append("no CI checks reported - cannot confirm CI (fail-closed)")
    if marker_eval is not None:
        if not marker_eval.get("ok"):
            reasons.append("reviewer comments could not be read (fail-closed)")
        else:
            if marker_eval.get("blocking"):
                reasons.append(
                    "blocking review marker [BLOCK-MERGE] on current head from: "
                    + ", ".join(marker_eval["blocking"])
                )
            if marker_eval.get("stale"):
                reasons.append(
                    "stale reviewer stamp(s) - no [<NAME>-REVIEWED] for current head: "
                    + ", ".join(marker_eval["stale"])
                )
    if head_run is False:
        reasons.append(
            "no pull_request-event workflow run for the current head - the "
            "checks shown may belong to an older head (stale or conflicted PR)"
        )
    elif head_run is None:
        reasons.append(
            "could not confirm a pull_request-event run for the current head (fail-closed)"
        )
    if merge_state and merge_state not in (
        "CLEAN",
        "HAS_HOOKS",
        "UNSTABLE",
        "BLOCKED",
        "DIRTY",
        "CONFLICTING",
        "DRAFT",
        "BEHIND",
    ):
        # BLOCKED = pending required review (expected for a review-ready PR);
        # anything unrecognized is fail-closed.
        reasons.append("unrecognized merge state '{}' (fail-closed)".format(merge_state))

    if reasons:
        return 20, "STATUS: BLOCKED - " + "; ".join(reasons)
    return 0, "STATUS: CLEAN (readiness passed, mergeable, no blocking review decision)"


def main(argv):
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
        "reviewDecision,url,headRefName,headRefOid,statusCheckRollup,"
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
    _closing = closing_link_reason(d.get("body"), _closes)
    if _closing:
        print("  NOTICE: " + _closing)

    n_unresolved = unresolved_thread_count(d.get("number"))
    print("-- Review threads " + "-" * 35)
    print(
        "  unresolved threads (advisory): " + ("?" if n_unresolved is None else str(n_unresolved))
    )

    # Reviewer-side conditions (issue #2550): the stamp and the comment body
    # are the signal -- never the review workflow's run conclusion, which is
    # unreliable in both directions on this repo.
    head_sha = (d.get("headRefOid") or "").strip()
    repo = detect_repo(d.get("url") or "")
    marker_authors = resolve_marker_authors(argv, os.environ)
    marker_bindings = resolve_marker_bindings(argv, os.environ)
    marker_eval = evaluate_reviewer_markers(
        fetch_bot_comments(repo, d.get("number"), marker_authors),
        head_sha,
        marker_bindings,
        only=reviewers_filter,
    )
    print("-- Reviewer markers (head {}) ".format(sanitize(head_sha[:12]) or "?") + "-" * 20)
    if not marker_eval["ok"]:
        print("  ERROR: bot comments could not be read (fail-closed)")
    elif not marker_eval["findings"] and not marker_eval["stale"]:
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
                "  - {}: fresh{}{}".format(
                    sanitize(name),
                    "  [BLOCK-MERGE]" if name in marker_eval["blocking"] else "",
                    "  ({} advisory FINDING line(s))".format(marker_eval["findings"][name])
                    if marker_eval["findings"][name]
                    else "",
                )
            )
        for name in marker_eval["stale"]:
            print("  - {}: STALE (stamp names an older head)".format(sanitize(name)))

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
        head_run = head_run_exists(repo, head_sha)
        if head_run is True:
            run_shown = "yes"
        elif head_run is False:
            run_shown = "NO (stale or conflicted?)"
        elif head_run == "skip":
            run_shown = "n/a (this head's CI is driven by other events)"
        else:
            run_shown = "? (could not confirm)"
        print("  pull_request run for current head: " + run_shown)
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
    )
    print(status)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
