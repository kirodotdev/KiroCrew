"""The viewer's open-PR heads and each repo's recent workflow runs.

The CI-run reads behind the own-PR failure watch and the CI runs dashboard; the
per-commit run read the watch also uses lives with the PR actions in
``github_client_writes``. Callers reach every name here through
:mod:`kiro_crew.apps.builtins.issue_radar.backend.github_client`.
"""

from __future__ import annotations

import json
import re

from .github_client_cli import (
    GH_TIMEOUT_SEC,
    GhCliError,
    _gh_run,
    _raise_if_auth_failure,
    _run_gh_api,
    _stderr_tail,
)
from .github_client_repo_reads import _parse_gh_timestamp

# ── CI-runs reads (the own-PR failure watch and the CI runs dashboard) ────────
#
# Both are READS through the user's existing ``gh`` session. The watcher calls
# :func:`search_viewer_open_pull_heads` once per cycle for the repos that opted
# in, then :func:`list_pr_workflow_runs` per head commit; the dashboard calls
# :func:`list_recent_workflow_runs` only while its tab is open.

# Owner/repo segments are charset-checked again here because they are spliced
# into a search QUERY, where a space or a colon would add a qualifier.
_SEARCH_REPO_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")

#: Repos per search query. GitHub caps a search query at 256 characters, and a
#: handful of ``repo:`` qualifiers keeps a query of long names under it.
CI_SEARCH_REPOS_PER_QUERY = 5

#: Open PRs read per search page (GitHub's own per-page cap).
CI_SEARCH_PAGE_SIZE = 100

#: Pages read per search query. A query that still has more after this many is
#: reported as truncated rather than read partially.
CI_SEARCH_MAX_PAGES = 5

_VIEWER_PULLS_QUERY = (
    "query($q:String!,$n:Int!,$after:String){ viewer{ login }"
    " search(query:$q, type:ISSUE, first:$n, after:$after){ pageInfo{ hasNextPage endCursor }"
    " nodes{ ... on PullRequest {"
    " number title url headRefOid author{ login } repository{ nameWithOwner } } } } }"
)

_RECENT_RUN_JQ = (
    '.workflow_runs[] | {id: .id, name: (.name // .display_title // "workflow"), '
    "status: .status, conclusion: .conclusion, url: .html_url, "
    "branch: (.head_branch // null), "
    "pull_requests: [(.pull_requests // [])[] | .number], "
    "created_at: .created_at, started_at: (.run_started_at // null), "
    "updated_at: (.updated_at // null)}"
)

#: How many recent runs the dashboard lists per repo (one page).
CI_RECENT_RUNS_PER_REPO = 20


def search_viewer_open_pull_heads(
    repos: list[tuple[str, str]], *, timeout: float = GH_TIMEOUT_SEC
) -> dict:
    """The signed-in user's open PRs across ``repos``, with each head commit.

    Returns ``{"pulls": [{owner, repo, number, title, url, head_sha}],
    "truncated": [(owner, repo)]}``. One GraphQL search per
    :data:`CI_SEARCH_REPOS_PER_QUERY` repos, scoped with ``author:@me`` and read
    page by page up to :data:`CI_SEARCH_MAX_PAGES`; the repos of a search that
    still had more pages are listed in ``truncated`` and none of their PRs are
    returned, so a caller never mistakes a partial list for the whole one. A
    node whose author is not the viewer is dropped as well, so a teammate's PR
    never reaches the caller whatever the search returns. A repo segment
    outside the GitHub charset is skipped rather than spliced into the query.
    Raises :class:`GhCliError` when ``gh`` fails.
    """
    valid = [
        (o, r)
        for o, r in repos
        if _SEARCH_REPO_SEGMENT_RE.match(o or "") and _SEARCH_REPO_SEGMENT_RE.match(r or "")
    ]
    pulls: list[dict] = []
    truncated: list[tuple[str, str]] = []
    seen: set[tuple[str, int]] = set()
    for start in range(0, len(valid), CI_SEARCH_REPOS_PER_QUERY):
        chunk = valid[start : start + CI_SEARCH_REPOS_PER_QUERY]
        query = " ".join(
            ["is:pr", "is:open", "archived:false", "author:@me", "sort:updated-desc"]
            + [f"repo:{o}/{r}" for o, r in chunk]
        )
        chunk_rows: list[dict] = []
        cursor: str | None = None
        complete = False
        for _page in range(CI_SEARCH_MAX_PAGES):
            data = _own_pulls_page(query, cursor, timeout=timeout)
            login = ((data.get("viewer") or {}).get("login") or "").strip()
            if not login:
                raise GhCliError("gh did not report the signed-in user")
            search = data.get("search") or {}
            for node in search.get("nodes") or []:
                row = _own_pull_row(node, login)
                if row is not None:
                    chunk_rows.append(row)
            info = search.get("pageInfo") or {}
            end = info.get("endCursor")
            if info.get("hasNextPage") is not True or not isinstance(end, str) or not end:
                complete = True
                break
            cursor = end
        if not complete:
            truncated.extend(chunk)
            continue
        for row in chunk_rows:
            ident = (f"{row['owner']}/{row['repo']}".lower(), row["number"])
            if ident in seen:
                continue
            seen.add(ident)
            pulls.append(row)
    return {"pulls": pulls, "truncated": truncated}


def _own_pulls_page(query: str, cursor: str | None, *, timeout: float) -> dict:
    """One page of the own-PR search; raises :class:`GhCliError` on any failure."""
    argv = [
        "gh",
        "api",
        "graphql",
        "-f",
        f"query={_VIEWER_PULLS_QUERY}",
        "-f",
        f"q={query}",
        "-F",
        f"n={CI_SEARCH_PAGE_SIZE}",
    ]
    if cursor is not None:
        argv += ["-f", f"after={cursor}"]
    argv += ["--jq", ".data"]
    proc = _gh_run(argv, timeout=timeout)
    if proc.returncode != 0:
        tail = _stderr_tail(proc)
        _raise_if_auth_failure(tail)
        raise GhCliError(f"gh api graphql (own pulls) failed (exit {proc.returncode}): {tail}")
    try:
        data = json.loads((proc.stdout or "").strip() or "{}")
    except json.JSONDecodeError as exc:
        raise GhCliError("gh returned unexpected output for the own-pulls search") from exc
    if not isinstance(data, dict):
        raise GhCliError("gh returned unexpected output for the own-pulls search")
    return data


def _own_pull_row(node: object, viewer: str) -> dict | None:
    """Normalize one search node, or ``None`` when it is not the viewer's PR."""
    if not isinstance(node, dict):
        return None
    author = ((node.get("author") or {}).get("login") or "").strip()
    if author.lower() != viewer.lower():
        return None
    number = node.get("number")
    sha = node.get("headRefOid")
    full = (node.get("repository") or {}).get("nameWithOwner") or ""
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        return None
    if not isinstance(sha, str) or not re.match(r"^[0-9a-fA-F]{7,64}$", sha):
        return None
    owner, _, repo = str(full).partition("/")
    if not owner or not repo:
        return None
    return {
        "owner": owner,
        "repo": repo,
        "number": number,
        "title": _bounded_str(node.get("title"), _CI_NAME_MAX_CHARS) or "",
        "url": _bounded_str(node.get("url"), _CI_URL_MAX_CHARS) or "",
        "head_sha": sha,
    }


def list_recent_workflow_runs(
    owner: str, repo: str, *, timeout: float = GH_TIMEOUT_SEC
) -> list[dict]:
    """The most recent GitHub Actions runs in a repo, newest first.

    One page of :data:`CI_RECENT_RUNS_PER_REPO` runs. Each row carries
    status, conclusion, branch, the PR numbers GitHub links to the run (empty for
    a run from a fork), and ``duration_sec`` from the run's start to its last
    update (``None`` until it has started).
    """
    rows = _run_gh_api(
        f"repos/{owner}/{repo}/actions/runs?per_page={CI_RECENT_RUNS_PER_REPO}",
        _RECENT_RUN_JQ,
        timeout=timeout,
        paginate=False,
    )
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        run_id = row.get("id")
        if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id <= 0:
            continue
        started = _parse_gh_timestamp(row.get("started_at") or row.get("created_at"))
        updated = _parse_gh_timestamp(row.get("updated_at"))
        duration = None
        if started is not None and updated is not None and updated >= started:
            duration = int((updated - started).total_seconds())
        prs = row.get("pull_requests")
        out.append(
            {
                "id": run_id,
                "name": _bounded_str(row.get("name"), _CI_NAME_MAX_CHARS) or "workflow",
                "status": _bounded_str(row.get("status"), _CI_TOKEN_MAX_CHARS),
                "conclusion": _bounded_str(row.get("conclusion"), _CI_TOKEN_MAX_CHARS),
                "url": _bounded_str(row.get("url"), _CI_URL_MAX_CHARS),
                "branch": _bounded_str(row.get("branch"), _CI_NAME_MAX_CHARS),
                "pull_requests": [
                    n
                    for n in (prs if isinstance(prs, list) else [])
                    if isinstance(n, int) and not isinstance(n, bool) and n > 0
                ][:CI_RUN_PR_NUMBERS_MAX],
                "created_at": _bounded_str(row.get("created_at"), _CI_TOKEN_MAX_CHARS),
                "started_at": _bounded_str(row.get("started_at"), _CI_TOKEN_MAX_CHARS),
                "updated_at": _bounded_str(row.get("updated_at"), _CI_TOKEN_MAX_CHARS),
                "duration_sec": duration,
            }
        )
    return out[:CI_RECENT_RUNS_PER_REPO]


#: Field bounds for the CI rows this module retains: names, titles and branches;
#: links; short tokens (status, conclusion, timestamps); PR numbers per run.
_CI_NAME_MAX_CHARS = 300
_CI_URL_MAX_CHARS = 500
_CI_TOKEN_MAX_CHARS = 40
CI_RUN_PR_NUMBERS_MAX = 10


def _bounded_str(value: object, limit: int) -> str | None:
    """``value`` cut to ``limit`` characters, or ``None`` when it is not a string."""
    return value[:limit] if isinstance(value, str) else None
