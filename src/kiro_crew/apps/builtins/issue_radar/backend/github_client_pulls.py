"""Pull-request reads on GitHub.

The open and closed lists and their first page, one PR's detail with its
mergeability retry, the head commit's checks, the list-card enrichment and the
server-side search. Callers reach every name here through
:mod:`kiro_crew.apps.builtins.issue_radar.backend.github_client`.
"""

from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor

from . import github_normalization, github_queries
from .github_client_cli import (
    GH_PAGINATE_TIMEOUT_SEC,
    GH_TIMEOUT_SEC,
    GhCliError,
    _gh_run,
    _run_gh_api,
    _stderr_tail,
)
from .github_client_detail import _is_absent_or_forbidden

# ── pull requests (list + detail + changed files) ───────────────────────────
#
# PRs are fetched from the dedicated ``repos/{o}/{r}/pulls`` endpoint (NOT the
# issues endpoint) so the list carries PR-native fields the triage view needs:
# draft state, base/head refs, requested reviewers, and ``merged_at`` (the
# signal that distinguishes a merged PR from one closed unmerged). The single-PR
# detail endpoint adds the diff stats (additions/deletions/changed_files),
# review/comment counts, and mergeability. A PR's activity timeline reuses the
# shared ``list_issue_timeline`` (the ``issues/{n}/timeline`` endpoint serves
# PRs too — and now also yields ``reviewed`` / ``committed`` events).

# The pulls LIST endpoint omits diff stats + comment counts (those need the
# per-PR detail call), so the list JQ stays to the fields a row card renders.
_PR_JQ = (
    ".[] | {number: .number, title: .title, url: .html_url, state: .state, "
    "draft: (.draft // false), labels: [.labels[].name], "
    "author: (.user.login // null), "
    "author_association: (.author_association // null), "
    "updated_at: .updated_at, created_at: .created_at, "
    "closed_at: .closed_at, merged_at: .merged_at, "
    "assignees: [.assignees[].login], "
    "requested_reviewers: [.requested_reviewers[].login], "
    "base: (.base.ref // null), head: (.head.ref // null), "
    # The head COMMIT, not just the branch name. Free here (the list payload
    # already carries it), and it is what lets a bulk approve name the revision the
    # row was rendered at — a verdict pinned to nothing is a verdict on whatever
    # got pushed last.
    "head_sha: (.head.sha // null), "
    'body: (.body // "")}'
)


def _list_pulls(owner: str, repo: str, state: str, *, timeout: float, paginate: bool) -> list[dict]:
    """List pull requests of ``state`` (open|closed), most-recently-updated first.

    ``paginate=True`` loads the FULL set across every page (used for open PRs);
    ``paginate=False`` caps at a single ``per_page=100`` page (used for closed
    PRs, whose history can be very long — same bounded-backlog policy as
    ``_list_issues``). ``merged_at`` is present on every closed row, so the
    frontend derives "merged vs closed-unmerged" without an extra call.
    """
    path = f"repos/{owner}/{repo}/pulls?state={state}&sort=updated&direction=desc&per_page=100"
    return _run_gh_api(path, _PR_JQ, timeout=timeout, paginate=paginate)


def list_open_pulls(
    owner: str, repo: str, *, timeout: float = GH_PAGINATE_TIMEOUT_SEC
) -> list[dict]:
    """ALL open pull requests (paginated across every page — see ``_list_pulls``)."""
    return _list_pulls(owner, repo, "open", timeout=timeout, paginate=True)


def list_open_pulls_first_page(
    owner: str, repo: str, *, timeout: float = GH_TIMEOUT_SEC
) -> list[dict]:
    """The newest ``per_page=100`` open PRs in ONE request (no pagination).

    Serves the progressive first paint on a COLD cache, exactly as
    ``list_open_issues_first_page`` does for issues: ``list_open_pulls``
    paginates every page (tens of requests on a large repo) AND the route then
    runs the GraphQL enrichment before a single byte can render, so the first
    open of a busy repo blocks for seconds. This is the same first page the full
    fetch would return anyway — PRs are sorted most-recently-updated first — so
    the full set appends behind it with no reordering. Uses the ordinary
    ``GH_TIMEOUT_SEC``, not the paginate budget: it is a single page by
    construction. The rows are UN-enriched (no diff size / check state); the
    first-paint route returns them as-is and the authoritative fetch enriches.
    """
    return _list_pulls(owner, repo, "open", timeout=timeout, paginate=False)


def list_closed_pulls(owner: str, repo: str, *, timeout: float = GH_TIMEOUT_SEC) -> list[dict]:
    """The 100 most-recently-updated CLOSED pull requests (bounded — includes
    both merged and closed-unmerged; the frontend splits them on ``merged_at``)."""
    return _list_pulls(owner, repo, "closed", timeout=timeout, paginate=False)


# The single-PR detail — a superset of the list row: adds diff stats, review /
# comment counts, mergeability, merged-by, and full label objects.
_PR_DETAIL_JQ = (
    '{number: .number, title: .title, body: (.body // ""), state: .state, '
    "draft: (.draft // false), merged: (.merged // false), url: .html_url, "
    "author: (.user.login // null), author_association: (.author_association // null), "
    "created_at: .created_at, updated_at: .updated_at, closed_at: .closed_at, "
    "merged_at: .merged_at, merged_by: (.merged_by.login // null), "
    "comments: (.comments // 0), review_comments: (.review_comments // 0), "
    "commits: (.commits // 0), additions: (.additions // 0), "
    "deletions: (.deletions // 0), changed_files: (.changed_files // 0), "
    "mergeable: .mergeable, mergeable_state: (.mergeable_state // null), "
    # Whether GitHub's own auto-merge is already armed, so the pane's control
    # reflects reality instead of always offering "enable". Null when it is off.
    "auto_merge: (if .auto_merge then {method: (.auto_merge.merge_method // null), "
    "enabled_by: (.auto_merge.enabled_by.login // null)} else null end), "
    "base: (.base.ref // null), head: (.head.ref // null), "
    "head_sha: (.head.sha // null), "
    'labels: [.labels[] | {name: .name, color: .color, description: (.description // "")}], '
    "assignees: [.assignees[].login], "
    "requested_reviewers: [.requested_reviewers[].login], "
    "milestone: (if .milestone then {title: .milestone.title, state: .milestone.state, "
    "due_on: .milestone.due_on} else null end)}"
)


def get_pr_detail(
    owner: str,
    repo: str,
    number: int,
    *,
    timeout: float = GH_TIMEOUT_SEC,
    resolve_mergeable: bool = True,
) -> dict:
    """Full detail for one pull request via ``gh api repos/{o}/{r}/pulls/{n}``.

    Returns the richer field set the detail pane needs but the list view omits
    (diff stats, review/comment counts, mergeability — see ``_PR_DETAIL_JQ``).
    ``number`` is coerced to ``int`` before it reaches the argv, so it cannot
    inject path segments. Same single-object subprocess pattern as
    ``get_issue_detail``.

    Mergeability needs a SECOND request. GitHub computes a PR's merge commit
    lazily: the first GET kicks off that background job and answers
    ``mergeable: null`` / ``mergeable_state: "unknown"``, and only a follow-up
    request sees the real verdict (measured on this repo: every PR reported
    ``unknown`` first, then ``true`` / ``blocked`` a moment later). So when the
    first answer is unknown we wait briefly and ask once more — otherwise the
    detail pane would permanently read "Unknown", and the cache would store it.

    ``resolve_mergeable=False`` skips that retry+sleep. A caller that reads only a
    field GitHub returns EAGERLY (``head_sha`` for the head-moved verdict check)
    does not need the lazy merge state, and paying the 1.5s sleep + second call per
    row of a bulk approve is pure waste — ``head_sha`` is stable in the first
    response. It never WEAKENS anything: the first read is still a live read of the
    current head, which is all the pin requires.
    """
    detail = _fetch_pr_detail_once(owner, repo, number, timeout=timeout)
    if not resolve_mergeable:
        return detail
    if detail.get("mergeable") is None or detail.get("mergeable_state") in (None, "unknown"):
        time.sleep(_MERGEABLE_RETRY_DELAY_SEC)
        try:
            retried = _fetch_pr_detail_once(owner, repo, number, timeout=timeout)
        except GhCliError:
            # The retry is an OPTIONAL improvement on an answer we already have.
            # Letting its failure propagate would turn a usable detail response
            # into a 502 and render no PR at all.
            return detail
        # Only accept the retry if it actually resolved: a still-unknown answer
        # (or a PR GitHub genuinely cannot compute) leaves the first one in place.
        if retried.get("mergeable") is not None or retried.get("mergeable_state") not in (
            None,
            "unknown",
        ):
            return retried
    return detail


# How long to wait before re-asking for a PR whose mergeability came back
# unknown. Long enough for GitHub's background computation on a normal PR, short
# enough that it is not felt on top of the detail fetch.
_MERGEABLE_RETRY_DELAY_SEC = 1.5


def _fetch_pr_detail_once(
    owner: str, repo: str, number: int, *, timeout: float = GH_TIMEOUT_SEC
) -> dict:
    """One ``gh api pulls/{n}`` round-trip, parsed. See :func:`get_pr_detail`."""
    argv = [
        "gh",
        "api",
        f"repos/{owner}/{repo}/pulls/{int(number)}",
        "--jq",
        _PR_DETAIL_JQ,
    ]
    proc = _gh_run(argv, timeout=timeout)

    if proc.returncode != 0:
        tail = _stderr_tail(proc)
        raise GhCliError(
            f"could not read {owner}/{repo} PR #{int(number)} (exit {proc.returncode}): {tail}"
        )

    try:
        return json.loads(proc.stdout.strip())
    except json.JSONDecodeError as exc:
        raise GhCliError(
            f"gh returned unexpected output for {owner}/{repo} PR #{int(number)}"
        ) from exc


# ── automated checks on a PR ("auto review" results) ─────────────────────────
#
# What actually reviews a PR automatically is TWO different GitHub surfaces, and
# a triage view needs both merged into one list:
#   * check-runs  (``commits/{sha}/check-runs``)  — GitHub Actions jobs and
#     Checks-API apps, i.e. CI plus any review bot;
#   * commit statuses (``commits/{sha}/status``)  — the older Status API, still
#     used by plenty of external services.
# Both hang off the PR's HEAD COMMIT, so the caller passes the head sha (taken
# from the PR detail it already fetched — no extra PR round-trip).
#
# Every row is normalized to one shape with a coarse ``bucket`` the UI can act on
# without re-deriving GitHub's ~10 conclusion values: failures and in-flight runs
# are surfaced explicitly, successes collapse behind a count.

# ``source`` is the PUBLISHER identity (the app that reported the check), kept
# because de-duplication keys on it: GitHub lets two different apps publish
# checks under the SAME display name, and collapsing those by name alone would
# let one app's success hide the other's failure.
_CHECK_RUN_JQ = (
    ".check_runs[] | {name: .name, status: .status, conclusion: .conclusion, "
    "url: (.details_url // .html_url // null), "
    "started_at: .started_at, completed_at: .completed_at, "
    'summary: ((.output.title // .output.summary) // ""), '
    "app: (.app.name // null), "
    'source: ((.app.slug // .app.name) // "check")}'
)

# Commit statuses have no queued/in-progress distinction: the state itself
# carries "pending", so status is reported as completed and
# ``github_normalization.CHECK_RUNNING_STATES`` routes "pending" into the
# running bucket.
_COMMIT_STATUS_JQ = (
    '.statuses[] | {name: .context, status: "completed", conclusion: .state, '
    "url: (.target_url // null), started_at: .created_at, completed_at: .updated_at, "
    'summary: (.description // ""), app: null, '
    'source: "status"}'
)


def _check_bucket(status: str | None, conclusion: str | None) -> str:
    """Coarse bucket for one check: ``failure`` | ``running`` | ``success`` |
    ``other``. Status is consulted first — an in-flight run has no conclusion
    yet — then the conclusion value.

    The table lives in ``github_normalization`` and is the ONLY one: the REST
    check rows, the GraphQL per-context rows and the GraphQL aggregate rollup all
    funnel through it (values are case-folded, so GraphQL's ``IN_PROGRESS`` and
    REST's ``in_progress`` are the same input). Keeping one table makes
    "a card dot and the detail sidebar can never disagree about red" true —
    parallel tables would only be edit-locked by convention.
    """
    return github_normalization.check_bucket(status, conclusion)


def _check_identity(row: dict) -> tuple[str, str]:
    """The identity two check rows must share to be considered the same check.

    NOT the display name alone: different apps (and the legacy Status API) may
    publish a check with the same name, and collapsing across publishers would
    let one app's later success hide another app's failure. ``source`` is the
    publishing app's slug (or ``"status"`` for a commit status).
    """
    return github_normalization.check_identity(row)


def _dedupe_checks(rows: list[dict]) -> list[dict]:
    """Keep only the LATEST row per (publisher, check name).

    Two rows can share an identity for the same head sha even though GitHub's
    ``filter=latest`` already collapses re-ATTEMPTS: the same workflow file can be
    started as two separate RUNS for one sha (observed on this repo —
    ``code-review.yml`` triggered twice 12s apart, two check-suites, each
    ``run_attempt: 1``), and each run contributes its own row per job. The later
    run supersedes the earlier one, so latest wins.
    """

    return github_normalization.dedupe_checks(rows, identity=_check_identity)


def list_pr_checks(
    owner: str, repo: str, sha: str, *, timeout: float = GH_TIMEOUT_SEC
) -> list[dict]:
    """The automated checks on a PR's head commit — CI jobs, Checks-API review
    bots, and legacy commit statuses — merged, de-duplicated, and bucketed.

    ``sha`` is charset-validated before it reaches the path (a commit sha is hex,
    so anything else is rejected outright). A surface that answers 403/404 is
    skipped — many repos use only one of the two — but a TRANSIENT failure is
    raised even if the other surface returned rows, because a partial answer gets
    cached and one passing check-run would then mask a failing commit status. If
    every surface is unavailable the error is raised rather than reported as "no
    checks".
    Result is ordered failures → running → other → success, then by name, so the
    rows that need attention come first.
    """
    if not re.match(r"^[0-9a-fA-F]{7,64}$", sha or ""):
        raise GhCliError(f"invalid commit sha: {sha!r}")

    rows: list[dict] = []
    errors: list[str] = []
    for path, jq_filter in (
        (f"repos/{owner}/{repo}/commits/{sha}/check-runs?per_page=100", _CHECK_RUN_JQ),
        (f"repos/{owner}/{repo}/commits/{sha}/status?per_page=100", _COMMIT_STATUS_JQ),
    ):
        try:
            rows.extend(_run_gh_api(path, jq_filter, timeout=timeout, paginate=True))
        except GhCliError as exc:
            # A 403/404 means this repo/token does not have that surface at all —
            # plenty of repos use only one of the two — so it is skipped. Anything
            # else (auth expired, network, timeout, 5xx) is TRANSIENT, and a
            # transient failure must not be absorbed even when the other surface
            # returned rows: one passing check-run would then be cached and shown
            # as "passing" while a required commit status was actually failing.
            if not _is_absent_or_forbidden(exc):
                raise GhCliError(
                    f"could not read checks for {owner}/{repo}@{sha[:12]}: {exc}"
                ) from exc
            errors.append(str(exc))
            continue
    if errors and not rows:
        # Every surface this repo has is unavailable to us; an empty list would be
        # cached and written over a known failure as "no checks" — a silent lie.
        raise GhCliError(
            f"could not read checks for {owner}/{repo}@{sha[:12]}: " + " | ".join(errors)
        )

    out: list[dict] = []
    for r in _dedupe_checks(rows):
        out.append(
            {
                "name": r.get("name"),
                "bucket": _check_bucket(r.get("status"), r.get("conclusion")),
                "status": r.get("status"),
                "conclusion": r.get("conclusion"),
                "url": r.get("url"),
                "summary": (r.get("summary") or "")[:300],
                "app": r.get("app"),
                "started_at": r.get("started_at"),
                "completed_at": r.get("completed_at"),
            }
        )
    order = {"failure": 0, "running": 1, "other": 2, "success": 3}
    out.sort(key=lambda c: (order.get(c["bucket"], 9), (c["name"] or "").lower()))
    return out


# ── list-card enrichment: diff size + aggregate check state (ONE GraphQL call) ─
#
# The REST ``pulls`` list omits ``additions``/``deletions`` (they exist only on
# the single-PR GET) and carries no check state at all. Getting either per row
# over REST would cost one detail call plus one-or-two check calls PER PR — ~100+
# subprocess spawns for a 50-PR repo, minutes of latency.
#
# GraphQL answers both for the WHOLE list in a single request (measured: ~2.4s
# for 50 open PRs on kirodotdev/KiroCrew), so the enrichment is one extra call
# regardless of list size. It is also strictly OPTIONAL: a failure here leaves the
# rows un-enriched rather than failing the list, because the diff size and the
# check dot are nice-to-have decoration on a card, not its reason to exist.

# The GraphQL selection for one PR's card enrichment, shared by both fetchers so
# the two paths can never drift apart in what they ask for.
_PR_SUMMARY_SELECTION = github_queries.PR_SUMMARY_SELECTION

# ``mergeStateStatus`` — its OWN query, deliberately, and this is the expensive one.
#
# It is GraphQL's spelling of REST's ``mergeable_state``, and the bulk bar needs it to
# tell "not ready yet, so arming auto-merge is meaningful" from "ready NOW, so GitHub
# refuses to arm at all" (`Pull request is in clean status`). Without it that bar is
# structurally blind — ``_PR_JQ`` carries no mergeability — so every ticked row was
# offered auto-merge and the provider rejected each already-clean one individually.
#
# **Why it cannot ride on ``_PR_SUMMARY_SELECTION``.** Unlike every other field added
# for this feature, ``mergeStateStatus`` is not a stored value: GitHub computes a merge
# commit per PR to answer it. Folded into the card selection — which already walks each
# PR's head commit and paginates its whole check rollup — the combined query reliably
# **502s at ``first:100``**, on this repo and on others. Measured, per field, same page
# size: the selection without it succeeds, ``mergeable``/``state``/``mergedAt`` added
# succeed, ``mergeStateStatus`` added fails every time. The failure is not graceful:
# both enrichment paths carry this selection, so a 502 leaves EVERY row with a null
# diff size and check tally, `enrichment_complete` returns False so the route declines
# to cache, and the list re-fetches on every load — while `mergeable_state` ends up
# None for all of them, leaving the bulk bar exactly as blind as before. That trades a
# 7-refusal annoyance for a total enrichment outage on the large repos most likely to
# use bulk actions.
#
# Alone, with no rollup and no commit walk, the same field is comfortable at
# ``first:100``. So it gets a second, LEAN call: one extra request per list fetch,
# independently failable, and a failure costs only the readiness field rather than the
# whole card payload.
_PR_READINESS_SELECTION = github_queries.PR_READINESS_SELECTION

# Smaller than `_SUMMARY_BATCH` (100) on purpose: this is the field GitHub COMPUTES, and
# the by-number form asks for N of them in one query. 50 is the largest page measured
# comfortable; the page-size ceiling is exactly what the split exists to respect.
_READINESS_BATCH = github_queries.READINESS_BATCH

# The JQ projection applied to ONE PR node (shared for the same reason).
_PR_SUMMARY_JQ_BODY = github_queries.PR_SUMMARY_JQ_BODY


def fetch_pr_summaries(
    owner: str, repo: str, state: str = "open", *, timeout: float = GH_TIMEOUT_SEC
) -> dict[int, dict]:
    """Fetch one state window of card summaries with the lean query family."""
    return github_queries.fetch_pr_summaries(
        owner,
        repo,
        state,
        timeout=timeout,
        gh_run=_gh_run,
        stderr_tail=_stderr_tail,
        parse_rows=_parse_summary_rows,
    )


def fetch_pr_readiness(
    owner: str, repo: str, state: str = "open", *, timeout: float = GH_TIMEOUT_SEC
) -> dict[int, str | None]:
    """Fetch merge readiness separately from the heavier card summary query."""
    return github_queries.fetch_pr_readiness(
        owner,
        repo,
        state,
        timeout=timeout,
        gh_run=_gh_run,
        stderr_tail=_stderr_tail,
        parse_rows=_parse_readiness_rows,
    )


def fetch_pr_readiness_by_number(
    owner: str, repo: str, numbers: list[int], *, timeout: float = GH_TIMEOUT_SEC
) -> dict[int, str | None]:
    """Fetch the independent readiness field for explicit PR numbers."""
    return github_queries.fetch_pr_readiness_by_number(
        owner,
        repo,
        numbers,
        timeout=timeout,
        gh_run=_gh_run,
        stderr_tail=_stderr_tail,
        parse_rows=_parse_readiness_rows,
    )


def _parse_readiness_rows(stdout: str) -> dict[int, str | None]:
    """Parse readiness rows while preserving the facade normalization seam."""
    return github_queries.parse_readiness_rows(stdout, lower=_lower_or_none)


# How many PR numbers to request per by-number GraphQL call. Each number costs
# one aliased field, so this bounds the query size while keeping the call count
# low (the search cap of 300 rows -> at most 3 calls).
_SUMMARY_BATCH = github_queries.SUMMARY_BATCH


def fetch_pr_summaries_by_number(
    owner: str, repo: str, numbers: list[int], *, timeout: float = GH_TIMEOUT_SEC
) -> dict[int, dict]:
    """Fetch card summaries for explicit PR numbers in bounded batches."""
    return github_queries.fetch_pr_summaries_by_number(
        owner,
        repo,
        numbers,
        timeout=timeout,
        gh_run=_gh_run,
        stderr_tail=_stderr_tail,
        parse_rows=_parse_summary_rows,
    )


def _count_context_buckets(contexts: object) -> dict[str, int]:
    """Tally normalized rollup context rows into the four buckets.

    The rows arrive in the same shape as the REST check rows, so they go through
    the SAME :func:`_dedupe_checks` (publisher + name identity, latest run wins)
    and the SAME :func:`_check_bucket` — a card's counts and the detail sidebar's
    list therefore cannot disagree about how many checks a PR has or what colour
    they are.

    Every bucket key is always present so the card never has to guard a hole, and
    an unrecognized state counts as ``other`` rather than passing.
    """
    return github_normalization.count_context_buckets(
        contexts,
        dedupe=_dedupe_checks,
        bucket=_check_bucket,
    )


def _lower_or_none(value: object) -> str | None:
    """A GraphQL enum lowered into REST's vocabulary, or ``None`` if absent.

    ``None`` is preserved rather than becoming ``""``: an absent mergeability is
    "GitHub has not computed this yet" (it is asynchronous, and a cold read answers
    ``UNKNOWN``), which callers must be able to tell from a real state. An empty
    string would compare unequal to every ready-state and so read as a confident
    "not ready".
    """
    return github_normalization.lower_or_none(value)


def _graphql_mergeable(value: object) -> bool | None:
    """GraphQL's ``MERGEABLE`` / ``CONFLICTING`` / ``UNKNOWN`` as REST's tri-state bool.

    REST reports ``mergeable`` as ``true`` / ``false`` / ``null``; GraphQL reports the
    same fact as an enum with an explicit ``UNKNOWN``. ``UNKNOWN`` maps to ``None``,
    NOT to ``False`` — the two mean very different things, and the whole reason this
    normalization is a named function is that collapsing them is the easy mistake.
    Note that ``mergeable`` alone never means "ready to merge": it means "no merge
    CONFLICTS", which is why the readiness gate keys off ``mergeable_state``.
    """
    return github_normalization.graphql_mergeable(value)


def _rest_pr_state(value: object) -> str | None:
    """GraphQL's ``OPEN`` / ``CLOSED`` / ``MERGED`` as REST's ``open`` / ``closed``.

    REST models a merged PR as ``state: "closed"`` carrying a ``merged_at`` timestamp;
    GraphQL models it as a third state. The row shape here is REST's, and every reader
    (``PrList.prStateVisual``, ``review.ts``, the closed-list split) compares against
    REST's two values — so ``MERGED`` has to become ``closed``, with the merge
    distinguished by the accompanying ``pr_merged_at``. Returning ``"merged"`` would
    match no branch in any of those readers and paint a merged PR as open.
    """
    return github_normalization.rest_pr_state(value)


def _parse_summary_rows(stdout: str) -> dict[int, dict]:
    """Parse the shared per-PR summary JQ stream (see ``_PR_SUMMARY_JQ_BODY``)."""
    return github_normalization.parse_summary_rows(
        stdout,
        mergeable_normalizer=_graphql_mergeable,
        state_normalizer=_rest_pr_state,
        bucket=_check_bucket,
        context_counter=_count_context_buckets,
    )


def summarize_checks(checks: list[dict]) -> dict:
    """``{checks_counts, checks_state}`` derived from an ALREADY-bucketed check list.

    Lets a fresh ``/pull`` fetch update the list card without re-running the
    GraphQL enrichment: the detail call has just read the authoritative checks, so
    the card's tally and dot are computed from exactly those rows. Priority for
    the single ``checks_state`` mirrors what the rollup means — anything failing
    dominates, then anything still running, then passing, then informational —
    so the dot never reads greener than the list it summarizes.
    """
    return github_normalization.summarize_checks(checks)


def _enrich_summaries(owner: str, repo: str, pulls: list[dict], state: str) -> dict[int, dict]:
    """The card-summary family: the state-scoped query plus its by-number top-up.

    The state-scoped GraphQL query returns at most 100 PRs while the REST list
    paginates ALL of them, so any row beyond that window is topped up by a
    by-number lookup. Without it those rows would report ``0`` additions and no
    checks — unavailable data rendered as a confident "no diff, no checks".

    Best effort: on failure the affected rows are simply absent from the map and
    :func:`_apply_summaries` records them as ``None`` (unknown, not "nothing").
    """
    try:
        summaries = fetch_pr_summaries(owner, repo, state)
    except GhCliError:
        summaries = {}
    missing = [
        n for n in (pr.get("number") for pr in pulls) if isinstance(n, int) and n not in summaries
    ]
    if missing:
        try:
            summaries.update(fetch_pr_summaries_by_number(owner, repo, missing))
        except GhCliError:
            pass
    return summaries


def _enrich_readiness(
    owner: str, repo: str, pulls: list[dict], state: str
) -> dict[int, str | None]:
    """The merge-readiness family: the state-scoped query plus its by-number top-up.

    A SECOND, lean call — it cannot ride on the card selection without 502ing it
    (see ``_PR_READINESS_SELECTION``). Independently failable: losing it costs the
    bulk bar's arm/merge split, not the whole card payload.

    Topped up by number for the same reason the summaries are: the state-scoped
    query is capped at ``first:100`` while the REST list paginates ALL open PRs, so
    on a repo with more than 100 the tail came back with no readiness at all.
    Unknown readiness is offered NEITHER merge verb, so those rows were silently
    unactionable in the bulk bar, precisely on the large repos bulk actions exist
    for.

    Membership, NOT truthiness. ``UNKNOWN`` is a legitimate ANSWER, not an absent
    one: GitHub computes mergeability asynchronously and roughly half a cold page
    comes back that way, and ``_parse_readiness_rows`` records it as the string
    ``'unknown'``. So the key IS present, and testing the value instead would
    re-request every such row on every fetch: a guaranteed extra query per list
    load that answers ``UNKNOWN`` again.
    """
    try:
        readiness = fetch_pr_readiness(owner, repo, state)
    except GhCliError:
        readiness = {}
    missing_readiness = [
        n for n in (pr.get("number") for pr in pulls) if isinstance(n, int) and n not in readiness
    ]
    if missing_readiness:
        try:
            readiness.update(fetch_pr_readiness_by_number(owner, repo, missing_readiness))
        except GhCliError:
            pass
    return readiness


def enrich_pulls(owner: str, repo: str, pulls: list[dict], state: str) -> list[dict]:
    """Merge :func:`fetch_pr_summaries` into REST list rows, in place-ish.

    The card summaries and the separate merge readiness are two INDEPENDENT
    GraphQL families (they must stay two calls — readiness cannot ride on the
    card selection without 502ing it), and neither derives from the other, so
    they run CONCURRENTLY on two threads rather than back-to-back. Each family is
    blocking ``gh`` subprocess I/O, so a thread apiece overlaps the two round
    trips and the enrichment leg costs the slower family instead of their sum.

    Best effort by design: on any failure the affected rows report ``None`` for
    diff size and check state (unknown, not "nothing"), so the list still renders
    and the route declines to cache the incomplete rows. Each family swallows its
    own ``GhCliError`` internally, so one failing does not sink the other.
    """
    with ThreadPoolExecutor(max_workers=2) as pool:
        summaries_f = pool.submit(_enrich_summaries, owner, repo, pulls, state)
        readiness_f = pool.submit(_enrich_readiness, owner, repo, pulls, state)
        summaries = summaries_f.result()
        readiness = readiness_f.result()
    return _apply_summaries(pulls, summaries, readiness)


def enrich_pulls_by_number(owner: str, repo: str, pulls: list[dict]) -> list[dict]:
    """Enrichment for SEARCH rows, addressed by the numbers actually returned.

    Same best-effort contract as :func:`enrich_pulls`: a failed call leaves the
    rows un-enriched rather than failing the response.
    """
    try:
        summaries = fetch_pr_summaries_by_number(
            owner, repo, [pr.get("number") for pr in pulls]  # type: ignore[misc]
        )
    except GhCliError:
        summaries = {}
    # Readiness by number, for the same reason the summaries are: a search hit can rank
    # outside the state-scoped window. Separate + independently failable, as above.
    try:
        readiness = fetch_pr_readiness_by_number(
            owner, repo, [pr.get("number") for pr in pulls]  # type: ignore[misc]
        )
    except GhCliError:
        readiness = {}
    return _apply_summaries(pulls, summaries, readiness)


def _apply_summaries(
    pulls: list[dict],
    summaries: dict[int, dict],
    readiness: dict[int, str | None] | None = None,
) -> list[dict]:
    """Write the enrichment fields onto every row.

    A row with no summary gets ``None`` — NOT ``0`` / empty counts. The distinction
    matters because the caller persists these rows: zeros would present an
    unavailable diff size and an unread check state as confident facts ("no
    changes, no checks") and the unbounded list cache would keep serving that
    until a manual refresh. ``None`` says "unknown", which the card renders as
    absent and :func:`enrichment_complete` reports so the route can skip caching.

    ``readiness`` comes from the SEPARATE :func:`fetch_pr_readiness` call and is
    applied independently of ``summaries``: either can fail on its own, and a row can
    legitimately have a diff size but unknown readiness (or the reverse).
    """
    return github_normalization.apply_summaries(pulls, summaries, readiness)


def enrichment_complete(pulls: list[dict]) -> bool:
    """Whether every row actually got its card enrichment.

    ``False`` means at least one row's diff size / check state is unknown (the
    GraphQL call failed), which the route uses to keep the incomplete rows OUT of
    the on-disk list cache so the next read retries instead of serving unknowns
    forever.
    """
    return github_normalization.enrichment_complete(pulls)


# ── server-side PR search ("by person" filters) ──────────────────────────────
#
# The bounded ``list_open_pulls`` / ``list_closed_pulls`` pair deliberately caps
# the closed set at one page, which keeps a big repo's list view fast — but it
# makes any CLIENT-side "authored by me" style filter unsound: your older PRs
# simply are not in the window (kirodotdev/KiroCrew has 363 closed PRs, so a PR
# merged a couple of days ago already ranks ~147th and falls outside).
#
# So the per-person filters are answered by GitHub's SEARCH API instead: the
# qualifier does the filtering server-side over the WHOLE repo, and the result
# set is complete for that person regardless of repo size. The list view keeps
# its bound; only these filters switch data source.
#
# Search results are issue-shaped, so a few PR-native fields are unavailable
# (``base``/``head`` refs, ``requested_reviewers``). That is fine: the filters
# that need them are expressed as QUALIFIERS (``review-requested:<login>``), so
# the query itself does the work and the missing field is never read back.

_PR_SEARCH_JQ = github_queries.PR_SEARCH_JQ

# Bound the search too — a person filter should never stream thousands of rows.
# Public because the route reports it to the client: the UI has to be able to say
# "newest 300" rather than implying completeness.
PR_SEARCH_MAX = github_queries.PR_SEARCH_MAX


def build_pr_search_query(
    owner: str,
    repo: str,
    *,
    state: str = "open",
    author: str | None = None,
    assignee: str | None = None,
    review_requested: str | None = None,
) -> str:
    """Build a validated, repo-scoped GitHub PR search query."""
    return github_queries.build_pr_search_query(
        owner,
        repo,
        state=state,
        author=author,
        assignee=assignee,
        review_requested=review_requested,
    )


def search_pulls(
    owner: str,
    repo: str,
    *,
    state: str = "open",
    author: str | None = None,
    assignee: str | None = None,
    review_requested: str | None = None,
    timeout: float = GH_PAGINATE_TIMEOUT_SEC,
    limit: int = PR_SEARCH_MAX,
) -> list[dict]:
    """Search PRs by person with explicit, bounded page reads."""
    return github_queries.search_pulls(
        owner,
        repo,
        state=state,
        author=author,
        assignee=assignee,
        review_requested=review_requested,
        timeout=timeout,
        limit=limit,
        query_builder=build_pr_search_query,
        run_api=_run_gh_api,
    )
