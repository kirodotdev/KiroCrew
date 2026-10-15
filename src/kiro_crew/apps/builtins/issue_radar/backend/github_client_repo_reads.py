"""Repository-level GitHub reads.

The repo access check, the open and closed issue lists with their change probe and
new-issue poll, the viewer's login and contributed repos, the labels, and the member
roster. Callers reach every name here through :mod:`kiro_crew.apps.builtins.issue_radar.backend.github_client`.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from . import github_normalization
from .github_client_cli import (
    GH_PAGINATE_TIMEOUT_SEC,
    GH_TIMEOUT_SEC,
    GhCliError,
    GhPermissionError,
    _gh_run,
    _raise_if_auth_failure,
    _run_gh_api,
    _stderr_tail,
)


def verify_repo_access(owner: str, repo: str, *, timeout: float = GH_TIMEOUT_SEC) -> dict:
    """Verify the repo exists and the current `gh` session can read it.

    Per product decision, this checks existence + read access only — it does
    NOT check push/admin/maintain permissions. Returns a small repo summary
    dict on success. Raises GhCliError on any failure (repo not found,
    private + no access, gh unauthenticated, network error, timeout) — the
    caller maps this to a 502 (upstream/auth problem) vs a 400 (bad URL,
    raised earlier by parse_github_repo_url as RepoUrlError).
    """
    argv = [
        "gh",
        "api",
        f"repos/{owner}/{repo}",
        "--jq",
        "{full_name: .full_name, private: .private, "
        "open_issues_count: .open_issues_count, description: .description, "
        "permissions: .permissions}",
    ]
    proc = _gh_run(argv, timeout=timeout)

    if proc.returncode != 0:
        tail = _stderr_tail(proc)
        raise GhCliError(f"could not read {owner}/{repo} (exit {proc.returncode}): {tail}")

    try:
        return json.loads(proc.stdout.strip())
    except json.JSONDecodeError as exc:
        raise GhCliError(f"gh returned unexpected output for {owner}/{repo}") from exc


_ISSUE_JQ = (
    ".[] | select(.pull_request == null) | "
    "{number: .number, title: .title, url: .html_url, "
    "labels: [.labels[].name], comments: .comments, "
    "reactions: (.reactions.total_count // 0), "
    'thumbs_up: (.reactions["+1"] // 0), '
    "author_association: (.author_association // null), "
    "updated_at: .updated_at, created_at: .created_at, state: .state, "
    "author: (.user.login // null), assignees: [.assignees[].login], "
    'body: (.body // "")}'
)


def _list_issues(
    owner: str, repo: str, state: str, *, timeout: float, paginate: bool
) -> list[dict]:
    """List issues of ``state`` (excludes PRs), most-recently-updated first.

    ``paginate=True`` loads the FULL set across every page (used for open
    issues); ``paginate=False`` caps at a single ``per_page=100`` page (used
    for closed issues, whose backlog can be tens of thousands — pulling all of
    them would be slow and is out of scope for a triage view).
    """
    path = f"repos/{owner}/{repo}/issues?state={state}&sort=updated&direction=desc&per_page=100"
    return _run_gh_api(path, _ISSUE_JQ, timeout=timeout, paginate=paginate)


def list_open_issues(
    owner: str, repo: str, *, timeout: float = GH_PAGINATE_TIMEOUT_SEC
) -> list[dict]:
    """ALL open issues (paginated across every page — see ``_list_issues``).

    Returns ``[{number, title, url, labels, comments, reactions, thumbs_up,
    author_association, updated_at, state, author, assignees, created_at,
    body}]``.
    """
    return _list_issues(owner, repo, "open", timeout=timeout, paginate=True)


def list_open_issues_first_page(
    owner: str, repo: str, *, timeout: float = GH_TIMEOUT_SEC
) -> list[dict]:
    """The newest ``per_page=100`` open issues in ONE request (no pagination).

    Serves the progressive first paint on a COLD cache: ``list_open_issues``
    paginates every page (tens of requests on a large repo, all before anything
    can render), so the first open of such a repo blocks for seconds. This is the
    same first page that fetch would return anyway — issues are sorted
    most-recently-updated first and both use it — so the full set appends behind
    it with no reordering. Uses the ordinary ``GH_TIMEOUT_SEC``, not the paginate
    budget: it is a single page by construction.
    """
    return _list_issues(owner, repo, "open", timeout=timeout, paginate=False)


def list_closed_issues(owner: str, repo: str, *, timeout: float = GH_TIMEOUT_SEC) -> list[dict]:
    """The 100 most-recently-updated CLOSED issues (bounded — see ``_list_issues``)."""
    return _list_issues(owner, repo, "closed", timeout=timeout, paginate=False)


# ── Cheap change probe for the OPEN lists ────────────────────────────────────
# One search call that answers "could this repo's open issue/PR list have changed
# since I last fetched it?" without walking the list. The open lists are FULLY
# paginated (`list_open_issues` / `list_open_pulls`), so on a large repo a
# speculative refetch costs tens of REST requests plus a multi-MB cache rewrite;
# this reduces the common "nothing changed" case to a single request.
#
# Two fields, because either alone has a blind spot:
#   * ``top_updated_at`` — the newest `updated_at` in the open set. Catches a new
#     item, an edit, a comment, a label/assignee change, a reopen.
#   * ``total_count`` — catches a CLOSE, which removes an item from the open set
#     without bumping any remaining item's timestamp.
#
# The search API is used rather than `repos/.../issues` because it reports
# `total_count` in the same response, `is:issue` / `is:pr` separates the two
# (the REST issues endpoint mixes PRs in, so a one-item peek could return a PR),
# and search has its OWN rate limit (30/min authenticated) — so the probe does
# not consume the core 5,000/hr budget the user's own `gh` work draws on.
_LIST_PROBE_JQ = "{total_count: .total_count, top_updated_at: (.items[0].updated_at // null)}"

_PROBE_KINDS = ("issue", "pr")


def probe_open_list(owner: str, repo: str, kind: str, *, timeout: float = GH_TIMEOUT_SEC) -> dict:
    """Return ``{"total_count": int, "top_updated_at": str | None}`` for a repo's
    OPEN issues (``kind="issue"``) or OPEN PRs (``kind="pr"``).

    Raises :class:`GhCliError` on a failed or unparseable call. Callers treat a
    probe failure as "assume it changed" and fall through to the full fetch, so a
    broken probe degrades to the previous behaviour instead of serving stale data.

    Probe values are only ever compared against ANOTHER probe (recorded when the
    list was last fetched), never against the cached rows — so a systematic
    difference between what search counts and what the REST list returns cancels
    out instead of silently reporting "changed" on every poll.
    """
    if kind not in _PROBE_KINDS:
        raise GhCliError(f"unsupported probe kind: {kind!r}")
    q = f"repo:{owner}/{repo} is:{kind} state:open"
    path = f"search/issues?q={quote(q, safe='')}&sort=updated&order=desc&per_page=1"
    rows = _run_gh_api(path, _LIST_PROBE_JQ, timeout=timeout, paginate=False)
    if not rows:
        raise GhCliError(f"probe for {owner}/{repo} {kind} returned no envelope")
    total = rows[0].get("total_count")
    if not isinstance(total, int):
        raise GhCliError(f"probe for {owner}/{repo} {kind} returned no total_count")
    top = rows[0].get("top_updated_at")
    return {"total_count": total, "top_updated_at": top if isinstance(top, str) else None}


# Cheapest possible new-issue poll: a single page of the most-recently-CREATED
# open issues (NOT the full paginated backlog). The background watcher
# (backend/watch.py) calls this every minute and only needs enough fields to
# spot a new issue number and describe it in a notification.
_ISSUE_POLL_JQ = (
    ".[] | select(.pull_request == null) | "
    "{number: .number, title: .title, url: .html_url, "
    "created_at: .created_at, author: (.user.login // null)}"
)


def list_recent_open_issues(
    owner: str, repo: str, limit: int = 30, *, timeout: float = GH_TIMEOUT_SEC
) -> list[dict]:
    """The ``limit`` most-recently-CREATED open issues (excludes PRs), newest
    first — the cheap single-page poll the background watcher uses to detect new
    issues (contrast ``list_open_issues``, which paginates the entire backlog).

    ``limit`` is coerced to a bounded int (1–100) before it reaches the query
    string, so it can neither inject query params nor request an unbounded page.
    Returns ``[{number, title, url, created_at, author}]``.
    """
    lim = max(1, min(int(limit), 100))
    path = f"repos/{owner}/{repo}/issues?state=open&sort=created&direction=desc&per_page={lim}"
    return _run_gh_api(path, _ISSUE_POLL_JQ, timeout=timeout, paginate=False)


# The repo picker's list: repos the authenticated user personally CONTRIBUTED
# to recently, newest contribution first.
#
# Sourced from the user's EVENT FEED (`users/{login}/events`), not
# `user/repos?sort=pushed`. The latter answers "which of my repos changed" —
# it fires for a teammate's push and says nothing about whether *this* user did
# anything, and it cannot tell you when they last did. The event feed is
# per-actor, so each entry is an action the user themselves took, and its
# `created_at` is exactly "when I last contributed here".
#
# Event types are filtered to actual contributions: pushes, PRs, reviews,
# review comments, issues, issue/commit comments, branch/tag creation and
# releases. Watch/Fork/Member events are excluded — starring a repo is not
# contributing to it.
_CONTRIB_EVENT_TYPES = frozenset(
    {
        "PushEvent",
        "PullRequestEvent",
        "PullRequestReviewEvent",
        "PullRequestReviewCommentEvent",
        "IssuesEvent",
        "IssueCommentEvent",
        "CommitCommentEvent",
        "CreateEvent",
        "ReleaseEvent",
    }
)

_EVENT_JQ = ".[] | {type: .type, repo: (.repo.name // null), " "created_at: (.created_at // null)}"


#: Default trailing window for "repos I contributed to". Single source of truth
#: for the backend — the route reads it rather than repeating the literal.
CONTRIB_WINDOW_DAYS = 30

#: Upper bound for a caller-supplied window. Guards ``timedelta(days=...)``,
#: which raises OverflowError on absurd values; also clamped here so a direct
#: caller (not just the HTTP route) can't trip it.
MAX_WINDOW_DAYS = 3650


#: Events fetched per call. A busy contributor can fill this in days, which is
#: why the result reports truncation rather than implying completeness.
_EVENT_PAGE_SIZE = 100


def list_contributed_repos(
    login: str, *, within_days: int = CONTRIB_WINDOW_DAYS, timeout: float = GH_TIMEOUT_SEC
) -> tuple[list[dict], bool]:
    """Repos ``login`` personally contributed to within the last ``within_days``.

    Powers the connect dialog's picker, so the user picks from repos they
    actually worked on instead of pasting URLs. Ordered by most-recent
    contribution first, each row carrying ``last_contributed_at`` — the
    timestamp of that user's latest contribution to the repo, which is what the
    UI renders ("3 days ago").

    Returns ``(rows, truncated)``. ``truncated`` is True when the event page
    came back full, meaning activity older than the newest 100 events was not
    examined and the list may be MISSING repos the user contributed to inside
    the window. The UI must not present a truncated list as complete — a picker
    that looks exhaustive leads the user to conclude they didn't work on a repo.

    Single page (no ``--paginate``): this is a picker, not an audit. GitHub's
    feed is itself capped (roughly the last 90 days / 300 events).

    ``within_days=0`` disables the window. Rows are ``[{owner, repo, full_name,
    last_contributed_at, contribution_count}]``.
    """
    path = f"users/{quote(login, safe='')}/events?per_page={_EVENT_PAGE_SIZE}"
    events = _run_gh_api(path, _EVENT_JQ, timeout=timeout, paginate=False)
    truncated = len(events) >= _EVENT_PAGE_SIZE

    days = max(0, min(int(within_days), MAX_WINDOW_DAYS))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days) if days else None

    # full_name -> {last contribution ts, how many contribution events}
    by_repo: dict[str, dict] = {}
    for ev in events:
        if ev.get("type") not in _CONTRIB_EVENT_TYPES:
            continue
        full_name = ev.get("repo")
        if not isinstance(full_name, str) or full_name.count("/") != 1:
            continue
        when = _parse_gh_timestamp(ev.get("created_at"))
        if when is None or (cutoff is not None and when < cutoff):
            continue
        row = by_repo.get(full_name)
        if row is None:
            owner, _, repo = full_name.partition("/")
            by_repo[full_name] = {
                "owner": owner,
                "repo": repo,
                "full_name": full_name,
                "last_contributed_at": ev["created_at"],
                "contribution_count": 1,
                "_when": when,
            }
        else:
            row["contribution_count"] += 1
            # The feed is newest-first, but don't rely on it — keep the max.
            if when > row["_when"]:
                row["_when"] = when
                row["last_contributed_at"] = ev["created_at"]

    rows = sorted(by_repo.values(), key=lambda r: r["_when"], reverse=True)
    for r in rows:
        del r["_when"]
    return rows, truncated


def _parse_gh_timestamp(value: object) -> datetime | None:
    """Parse a GitHub ISO-8601 UTC stamp (``2026-07-25T20:57:01Z``) to an aware
    datetime, or None when absent/malformed."""
    return github_normalization.parse_gh_timestamp(value)


def get_current_login(*, timeout: float = GH_TIMEOUT_SEC) -> str | None:
    """Return the login of the authenticated `gh` user (``gh api user``).

    Powers the "requested by me" / "assigned to me" filters — the frontend
    compares this against each issue's ``author`` / ``assignees``. Returns
    ``None`` if gh reports an empty login; raises GhCliError if gh is missing,
    unauthenticated, times out, or errors.
    """
    argv = ["gh", "api", "user", "--jq", ".login"]
    proc = _gh_run(argv, timeout=timeout)

    if proc.returncode != 0:
        tail = _stderr_tail(proc)
        _raise_if_auth_failure(tail)
        raise GhCliError(f"gh api user failed (exit {proc.returncode}): {tail}")

    return (proc.stdout or "").strip() or None


def list_repo_labels(owner: str, repo: str, *, timeout: float = GH_TIMEOUT_SEC) -> list[dict]:
    """List every label defined on the repo, with its GitHub-configured colour.

    Returns ``[{name, color, description}]`` where ``color`` is the 6-hex
    string GitHub stores (no leading ``#``). Powers the left-rail filter
    column; the frontend builds a name->color map from this so issue-list and
    detail chips render in the repo's real label colours.
    """
    path = f"repos/{owner}/{repo}/labels?per_page=100"
    jq_filter = '.[] | {name: .name, color: .color, description: (.description // "")}'
    return _run_gh_api(path, jq_filter, timeout=timeout)


# ── repo membership ──────────────────────────────────────────────────────────
#
# The authoritative member roster is the repo's COLLABORATORS
# (``repos/{o}/{r}/collaborators?affiliation=all``): everyone with access —
# org members with access (direct or via team) plus outside collaborators —
# each with a ``role_name`` (admin/maintain/write/triage/read). This is the
# complete list a triage view wants, independent of who happened to open an
# issue.
#
# That endpoint requires PUSH access, so on a read-only repo (e.g. a pull-only
# fork) it 403s. In that case we fall back to ``derive_members`` — the distinct
# authors whose ``author_association`` marks them a member — which is always
# available to any reader. Callers cache whichever result they get (with a
# ``source`` marker) so the detail badge and the "created by member" filter read
# it instantly.
_MEMBER_ASSOC_RANK = github_normalization.MEMBER_ASSOC_RANK


def list_repo_collaborators(
    owner: str, repo: str, *, timeout: float = GH_PAGINATE_TIMEOUT_SEC
) -> list[dict]:
    """Authoritative member roster: everyone with access to the repo.

    ``gh api repos/{o}/{r}/collaborators?affiliation=all`` (paginated). Returns
    ``[{login, role_name}]`` where ``role_name`` ∈ admin/maintain/write/triage/
    read. REQUIRES push access — GitHub 403s otherwise, which is surfaced as
    ``GhPermissionError`` so the caller can fall back to ``derive_members`` for
    read-only repos.
    """
    path = f"repos/{owner}/{repo}/collaborators?per_page=100&affiliation=all"
    jq_filter = ".[] | {login: .login, role_name: (.role_name // null)}"
    try:
        return _run_gh_api(path, jq_filter, timeout=timeout, paginate=True)
    except GhCliError as exc:
        msg = str(exc)
        if "403" in msg or "push access" in msg.lower():
            raise GhPermissionError(
                f"push access required to list collaborators for {owner}/{repo}"
            ) from exc
        raise


def derive_members(issues: list[dict]) -> list[dict]:
    """FALLBACK roster (read-only repos): distinct members among the AUTHORS of
    ``issues``.

    Used only when ``list_repo_collaborators`` is forbidden. Returns
    ``[{"login", "association"}]`` sorted by login. When one author appears
    under several associations across issues, the strongest
    (OWNER > MEMBER > COLLABORATOR) wins. Authors whose association is not a
    member association (CONTRIBUTOR, FIRST_TIME_CONTRIBUTOR, NONE, …) and
    author-less issues are ignored. This only ever sees members who happened to
    open an issue — hence it is a fallback, not the primary source.
    """
    return github_normalization.derive_members(issues)
