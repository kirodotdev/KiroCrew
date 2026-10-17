"""The per-item GitHub reads behind the detail pane.

One issue's detail and a reference summary, the issue and PR timeline, the
blocked-by dependency graph, and the crew claim read back off an issue. Callers
reach every name here through :mod:`kiro_crew.apps.builtins.issue_radar.backend.github_client`.
"""

from __future__ import annotations

import json
from typing import Any

from . import github_normalization, github_queries
from .github_client_cli import (
    GH_PAGINATE_TIMEOUT_SEC,
    GH_TIMEOUT_SEC,
    GhCliError,
    _gh_run,
    _run_gh_api,
    _stderr_tail,
)

# ── single-issue detail + timeline (powers the detail pane) ──────────────────

# Shaped so the detail pane can render everything the list view omits — body,
# state_reason, author_association, closed_at/closed_by, locked, per-label
# colour, assignees, milestone, and the full reaction breakdown — in one call.
_ISSUE_DETAIL_JQ = (
    '{number: .number, title: .title, body: (.body // ""), state: .state, '
    "state_reason: .state_reason, url: .html_url, author: (.user.login // null), "
    "author_association: (.author_association // null), created_at: .created_at, "
    "updated_at: .updated_at, closed_at: .closed_at, closed_by: (.closed_by.login // null), "
    "comments: .comments, locked: .locked, "
    'labels: [.labels[] | {name: .name, color: .color, description: (.description // "")}], '
    "assignees: [.assignees[].login], "
    "milestone: (if .milestone then {title: .milestone.title, state: .milestone.state, "
    "due_on: .milestone.due_on} else null end), "
    'reactions: (if .reactions then {total: .reactions.total_count, plus1: .reactions["+1"], '
    'minus1: .reactions["-1"], laugh: .reactions.laugh, hooray: .reactions.hooray, '
    "confused: .reactions.confused, heart: .reactions.heart, rocket: .reactions.rocket, "
    "eyes: .reactions.eyes} else null end)}"
)


def get_issue_detail(
    owner: str, repo: str, number: int, *, timeout: float = GH_TIMEOUT_SEC
) -> dict:
    """Full detail for one issue via ``gh api repos/{o}/{r}/issues/{n}``.

    Returns the richer field set the detail pane needs but the list view omits
    (see ``_ISSUE_DETAIL_JQ``). ``number`` is coerced to ``int`` before it ever
    reaches the argv, so it cannot inject path segments. Uses the same
    single-object subprocess pattern as ``verify_repo_access`` (one compact JSON
    object on stdout) rather than the JSONL ``_run_gh_api`` path.
    """
    argv = [
        "gh",
        "api",
        f"repos/{owner}/{repo}/issues/{int(number)}",
        "--jq",
        _ISSUE_DETAIL_JQ,
    ]
    proc = _gh_run(argv, timeout=timeout)

    if proc.returncode != 0:
        tail = _stderr_tail(proc)
        raise GhCliError(
            f"could not read {owner}/{repo}#{int(number)} (exit {proc.returncode}): {tail}"
        )

    try:
        return json.loads(proc.stdout.strip())
    except json.JSONDecodeError as exc:
        raise GhCliError(f"gh returned unexpected output for {owner}/{repo}#{int(number)}") from exc


# One issue/PR reduced to what a REFERENCE preview needs: the identity, who
# opened it, when, and its lifecycle. Deliberately excludes body/labels/timeline —
# this backs a hover card and a kind lookup, so it must stay a single cheap call.
# ``is_pr`` comes from the ``pull_request`` key the issues endpoint adds for a PR,
# which is also the only way to tell an issue number from a PR number: GitHub's
# ``/issues/{n}`` URL silently redirects to ``/pull/{n}``, so a plain "#123" (or an
# ``/issues/123`` link) can be either.
_REF_SUMMARY_JQ = (
    "{number: .number, title: .title, state: .state, "
    "state_reason: .state_reason, url: .html_url, author: (.user.login // null), "
    "author_association: (.author_association // null), created_at: .created_at, "
    "updated_at: .updated_at, closed_at: .closed_at, comments: .comments, "
    "is_pr: (.pull_request != null), draft: (.draft // false), "
    "merged_at: (.pull_request.merged_at // null), "
    "labels: [.labels[] | {name: .name, color: .color}]}"
)


def get_ref_summary(owner: str, repo: str, number: int, *, timeout: float = GH_TIMEOUT_SEC) -> dict:
    """Compact summary of one issue OR pull request via ``gh api
    repos/{o}/{r}/issues/{n}`` (the issues endpoint answers for both).

    Backs the reference hover card and the issue-vs-PR resolution for a bare
    ``#123``. One request, no timeline, no diff — see ``_REF_SUMMARY_JQ``.
    ``number`` is coerced to ``int`` before it reaches the argv, so it cannot
    inject path segments.
    """
    argv = [
        "gh",
        "api",
        f"repos/{owner}/{repo}/issues/{int(number)}",
        "--jq",
        _REF_SUMMARY_JQ,
    ]
    proc = _gh_run(argv, timeout=timeout)

    if proc.returncode != 0:
        tail = _stderr_tail(proc)
        raise GhCliError(
            f"could not read {owner}/{repo}#{int(number)} (exit {proc.returncode}): {tail}"
        )

    try:
        return json.loads(proc.stdout.strip())
    except json.JSONDecodeError as exc:
        raise GhCliError(f"gh returned unexpected output for {owner}/{repo}#{int(number)}") from exc


def _norm_reactions(r: dict | None) -> dict | None:
    """Normalize a GitHub reactions object; ``None`` when there are none (so the
    UI only renders a reactions strip when it carries signal)."""
    return github_normalization.norm_reactions(r)


def _actor_login(ev: dict) -> str | None:
    return github_normalization.actor_login(ev)


def _normalize_timeline_event(ev: dict) -> dict | None:
    """Map one raw GitHub timeline event to the compact uniform shape the UI
    renders, or ``None`` to drop it.

    GitHub emits ~30 event types; only the ones that matter for triage are kept
    (comments, label/assignee/milestone changes, close/reopen/rename, and
    cross-references from other issues/PRs). The rest — subscribed, mentioned,
    review_requested, head_ref_*, and similar bookkeeping — are noise here and
    are dropped.
    """
    return github_normalization.normalize_timeline_event(
        ev,
        reaction_normalizer=_norm_reactions,
        actor_reader=_actor_login,
    )


def list_issue_timeline(
    owner: str, repo: str, number: int, *, timeout: float = GH_PAGINATE_TIMEOUT_SEC
) -> list[dict]:
    """Normalized, chronological timeline for one issue.

    Loads the FULL timeline (``--paginate``): a heavily-discussed issue can have
    hundreds of events, and a triage view should see all of them (same
    "load everything for open items" principle as ``list_open_issues``). Raw
    events are normalized to a compact uniform shape, noise is dropped, and the
    result is sorted oldest->newest so the pane reads like the GitHub thread.
    """
    path = f"repos/{owner}/{repo}/issues/{int(number)}/timeline?per_page=100"
    raw = _run_gh_api(path, ".[]", timeout=timeout, paginate=True)
    events = [e for e in (_normalize_timeline_event(ev) for ev in raw) if e is not None]
    events.sort(key=lambda e: e.get("created_at") or "")
    return events


# Inline review comments live on their own endpoint: the issues TIMELINE does not
# carry them, so a PR read purely from the timeline is missing exactly the
# comments that carry the review's substance ("this retry is unbounded" attached
# to the line it is about).
_PR_REVIEW_COMMENT_JQ = (
    # The endpoint answers a top-level ARRAY, so the projection is per element —
    # without the `.[] |` gh fails with "expected an object but got: array".
    '.[] | {kind: "review_comment", actor: (.user.login // null), '
    'created_at: .created_at, body: (.body // ""), '
    "author_association: (.author_association // null), "
    "path: (.path // null), line: (.line // .original_line // null), "
    "url: (.html_url // null)}"
)


def list_pr_review_comments(
    owner: str, repo: str, number: int, *, timeout: float = GH_PAGINATE_TIMEOUT_SEC
) -> list[dict]:
    """Normalized INLINE (code-anchored) review comments for one PR.

    Same normalized shape as the timeline events so the two can be merged and
    sorted together; ``kind`` is ``review_comment`` and the row carries the file
    path + line it is anchored to. Paginated, like the timeline.
    """
    path = f"repos/{owner}/{repo}/pulls/{int(number)}/comments?per_page=100"
    rows = _run_gh_api(path, _PR_REVIEW_COMMENT_JQ, timeout=timeout, paginate=True)
    return [r for r in rows if isinstance(r, dict)]


def _is_absent_or_forbidden(exc: GhCliError) -> bool:
    """Whether a `gh` failure means "this surface does not exist for you" (404 /
    403) as opposed to a TRANSIENT or recoverable failure (timeout, network, rate
    limit, expired credentials).

    The distinction decides whether a partial result may be treated as complete:
    a permanently unavailable endpoint can be skipped, but swallowing a timeout
    would cache a truncated conversation as if it were the whole thing.

    401 is deliberately NOT here. It means the session needs re-authenticating, so
    every other call is about to fail too — treating it as "endpoint absent" would
    quietly cache half a conversation, or a check list missing whichever surface
    the expiry happened to hit.
    """
    text = str(exc)
    return any(code in text for code in ("HTTP 404", "HTTP 403"))


def list_pr_timeline(
    owner: str, repo: str, number: int, *, timeout: float = GH_PAGINATE_TIMEOUT_SEC
) -> list[dict]:
    """The full PR conversation: issue timeline PLUS inline review comments,
    merged and sorted oldest->newest.

    A PR's review substance is split across two endpoints; reading only the
    timeline drops every code-anchored objection, which would leave both the
    detail pane and the AI summary claiming a quieter review than actually
    happened. Only a 404/403/401 on the inline endpoint is tolerated (that repo
    or token genuinely cannot serve it); a transient failure is raised, because
    the caller CACHES this list and a swallowed timeout would persist a partial
    conversation as complete.
    """
    events = list_issue_timeline(owner, repo, number, timeout=timeout)
    try:
        events.extend(list_pr_review_comments(owner, repo, number, timeout=timeout))
    except GhCliError as exc:
        if not _is_absent_or_forbidden(exc):
            raise
        # Endpoint unavailable for this repo/token; _gh_run already emitted an SEL
        # event for the failed call.
    events.sort(key=lambda e: e.get("created_at") or "")
    return events


# ── dependency edges (blocked-by / blocking graph) ───────────────────────────
#
# Two sources feed the graph, tagged by provenance:
#   • NATIVE — GitHub's issue-dependencies API. One call per open
#     issue reads its blocked_by set; a blocker of issue N is an edge
#     ``{blocked: N, blocker: B}``. The endpoint is young, so a repo/token/GHES
#     that has not enabled it answers 404/410 — handled as ZERO native edges for
#     that issue, never as a hard failure (same tolerance as the inline
#     review-comment endpoint above).
#   • INFERRED — the timeline cross-references we already normalize
#     (``_normalize_timeline_event`` → ``cross-referenced``). A same-repo item
#     that references issue N is read as a candidate blocker of N. Inferred edges
#     never write back to GitHub and lose to a native duplicate downstream (the
#     store's ``_normalize_deps`` does the native-wins dedup).
#
# ``nodes`` is populated from data the caller already has (the issues/pulls list
# caches) plus a bounded ``get_ref_summary`` fallback for any number that appears
# in an edge but is not in those caches (a closed/cross-repo-but-same-owner item
# a still-open issue references). Same-repo only — a cross-repo reference is
# dropped, per the M1 non-goal.

# The dependencies API answers a list of issue objects; only the number, state,
# title and PR-ness are needed to both draw the edge and seed the node.
_DEP_ISSUE_JQ = github_queries.DEP_ISSUE_JQ


def _dep_node_kind(is_pr: bool) -> str:
    return github_normalization.dep_node_kind(is_pr)


def _dep_node_state(state: Any, merged_at: Any = None) -> str:
    """Normalize a node's lifecycle to ``open`` / ``closed`` / ``merged``.

    A PR that GitHub reports as ``closed`` but carries a ``merged_at`` is shown as
    ``merged`` — the auto-unlock semantics turn on merged-vs-closed, and the graph
    view colours by it — while everything else collapses to open/closed."""
    return github_normalization.dep_node_state(state, merged_at)


def _is_deps_feature_absent(exc: GhCliError) -> bool:
    """Whether a dependencies-endpoint failure means the FEATURE is absent (404 on
    a host/plan without native dependencies, 410 gone) — the only case an empty
    native edge list is the truth.

    Deliberately narrower than ``_is_absent_or_forbidden``: a 403 here is a
    permission/rate signal, not absence. These reads run only for a repo whose
    open-issues list was just fetched successfully, so revoked access surfaces
    on that call first; a 403 that reaches this point must PROPAGATE — swallowing
    it would let a refresh overwrite the cached graph with a wrong-empty one and
    fire a false unlock (blocker count 1 -> 0) for every dependent item.
    """
    return github_queries.is_deps_feature_absent(exc)


def list_issue_blocked_by(
    owner: str, repo: str, number: int, *, timeout: float = GH_TIMEOUT_SEC
) -> list[dict]:
    """Native blockers of ONE issue via
    ``gh api repos/{o}/{r}/issues/{n}/dependencies/blocked_by``.

    Returns the compact ``_DEP_ISSUE_JQ`` shape for each blocker. A 404/410 (the
    dependencies feature is not available for this repo/token/host) is tolerated
    as an empty list rather than raised, so a repo that never adopted native
    dependencies still gets its inferred graph. ``number`` is coerced to ``int``
    before it reaches the path so it cannot inject segments.
    """
    return github_queries.list_issue_blocked_by(
        owner,
        repo,
        number,
        timeout=timeout,
        run_api=_run_gh_api,
        absent_classifier=_is_deps_feature_absent,
    )


def _inferred_blockers_from_timeline(
    owner: str, repo: str, number: int, *, timeout: float
) -> list[dict]:
    """Same-repo items that cross-reference issue ``number``, read as candidate
    blockers of it.

    A ``cross-referenced`` timeline event on issue N records that some other item
    mentions N; we treat that other item as a potential blocker of N. Cross-repo
    sources carry a full URL to a different repo — those are dropped (M1 is
    same-repo only). Returns the compact node shape so the caller can seed
    ``nodes`` without a second read.
    """
    return github_queries.inferred_blockers_from_timeline(
        owner,
        repo,
        number,
        timeout=timeout,
        timeline_loader=list_issue_timeline,
        absent_classifier=_is_deps_feature_absent,
        event_filter=_inferred_blockers_from_events,
    )


def _inferred_blockers_from_events(
    events: list[dict], owner: str, repo: str, number: int
) -> list[dict]:
    """The shared filter both timeline sources feed: keep same-repo
    cross-reference sources, drop self-references and anything whose URL does not
    name THIS owner/repo (an empty URL is dropped rather than assumed local, so a
    cross-repo edge can never sneak in via a missing field)."""
    return github_queries.inferred_blockers_from_events(events, owner, repo, number)


def _batch_dependency_graph(
    owner: str, repo: str, *, timeout: float = GH_TIMEOUT_SEC
) -> dict[int, dict] | None:
    """Read native and inferred dependency inputs in one bounded GraphQL walk."""
    return github_queries.batch_dependency_graph(
        owner,
        repo,
        timeout=timeout,
        gh_run=_gh_run,
    )


def fetch_dependency_edges(
    owner: str,
    repo: str,
    open_issues: list[dict],
    node_hints: dict[int, dict] | None = None,
    *,
    timeout: float = GH_TIMEOUT_SEC,
) -> tuple[list[dict], dict[str, dict]]:
    """Build normalized dependency edges and nodes for the open issue set."""
    return github_queries.fetch_dependency_edges(
        owner,
        repo,
        open_issues,
        node_hints,
        timeout=timeout,
        batch_loader=_batch_dependency_graph,
        blockers_loader=list_issue_blocked_by,
        inferred_loader=_inferred_blockers_from_timeline,
        event_filter=_inferred_blockers_from_events,
        ref_loader=get_ref_summary,
        node_kind=_dep_node_kind,
        node_state=_dep_node_state,
    )


# ── crew claim protocol (reading a claim back off the issue) ──────────────────
#
# A crew's claim on an issue lives in a COMMENT, not in a label and not only in
# Kiro Crew's own store: the comment is the authority, so the claim survives a
# gateway restart, is visible to a human reading the issue on GitHub, and is
# readable by a crew running in a different process. The `crew:` labels are a
# cheap index over it, never the source of truth.
#
# The machine-readable half is an HTML comment at the end of that body:
#
#   <!-- kirocrew-crew id=c_7f3a phase=implementing pr=2271 updated=2026-08-08T20:44:12Z -->
#
# HTML so GitHub renders nothing, and parsed instead of the prose so a crew can
# rewrite its progress notes freely without breaking the protocol.
#
# Everything below is PURE — it takes rows already normalized by
# ``_normalize_timeline_event`` and spawns no process. It lives here rather than in
# the store because the rows are this module's shape and the marker's dependency on
# a comment's ``id``/``updated_at`` is this module's contract.


def _parse_crew_marker(body: str) -> dict | None:
    """The crew payload parsed out of ONE comment body, or ``None`` if it has none.

    Returns ``{crew_id, phase, pr, updated}``. ``pr`` is an int or ``None``;
    ``updated`` is the validated ISO-8601-``Z`` string or ``None`` (see
    :data:`github_normalization.CREW_CLAIM_ISO_Z_RE` — a malformed stamp is
    unparseable, NOT fresh).

    The FIRST marker in a body wins. A body carrying two is malformed either way,
    and first-wins at least makes which one is honoured deterministic rather than
    dependent on how the prose was assembled.
    """
    return github_normalization.parse_crew_marker(body)


def find_crew_claim(timeline_rows: list[dict], crew_id: str = "") -> list[dict]:
    """Crew claims found in a normalized issue timeline, oldest comment id FIRST.

    Takes the output of :func:`list_issue_timeline` and returns one
    ``{comment_id, crew_id, phase, pr, updated, actor, created_at}`` entry per
    comment carrying a crew marker. A row with no marker is skipped, and so is any
    row that is not a ``comment``: a ``review_comment`` lives at a DIFFERENT
    endpoint (``pulls/comments/{id}``), so treating one as a claim would hand
    :func:`update_issue_comment` an id it cannot address.

    ``crew_id`` filters to one crew's own claims — the "where is MY comment so I can
    PATCH it" read. A list is returned either way, so callers never branch on the
    return type; a single-claim caller takes ``[0]``. It is a list and not a single
    entry because a duplicated post (a retried comment) is a real state a crew must
    be able to SEE rather than have silently collapsed. The default ``""`` means
    unfiltered, so it never matches the id-less markers described below.

    **Ordering is part of the protocol, not presentation.** Collisions are resolved
    by "smallest comment id wins" — the crew that got there first keeps the claim and
    the other yields — so ascending comment id makes the winner ``[0]``. An entry
    whose comment id is unknown sorts LAST: it cannot demonstrate it was first, so it
    must not be able to win a collision, while still being visible as a claim.
    """
    return github_normalization.find_crew_claim(
        timeline_rows,
        crew_id,
        marker_parser=_parse_crew_marker,
    )
