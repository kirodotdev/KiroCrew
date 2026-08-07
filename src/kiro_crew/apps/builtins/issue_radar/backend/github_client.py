"""Issue Radar — GitHub access via the user's existing ``gh`` CLI session.

No GitHub App, no PAT, no KiroCrew-hosted credential storage: every call
shells out to ``gh``, which owns its own token storage/refresh. This module
only needs to (a) parse a repo URL safely and (b) run ``gh api`` with a list
argv (never ``shell=True``) so there is no shell-injection surface.

The URL-parsing security model (host allowlist against SSRF, strict
owner/repo charset before it ever reaches a subprocess argv) mirrors
``code_review_sage/sage_lib/adapters.py:parse_repo_url`` — that implementation
is reused deliberately rather than re-derived, since it already defends
against SSRF/shell-injection and duplicating that logic slightly differently
would be a regression, not a rewrite.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlparse

from .errors import (
    ProviderCliError,
    ProviderPermissionError,
    ProviderSetupError,
    PrSearchError,
    RepoUrlError,
    sanitize_cli_stderr,
)

# ── exception aliases ────────────────────────────────────────────────────────
#
# The canonical classes live in ``errors`` so ``gitlab_client`` raises the SAME
# objects and ``routes.py``'s 26 ``except github_client.GhCliError`` clauses keep
# working for both providers. These are aliases, NOT subclasses: a subclass would
# mean a GitLab failure escaped those handlers as an unhandled 500.
#
# The historical names are kept because they are the documented surface every
# route and test already uses:
#
#   GhCliError        -- gh is missing, unauthenticated, timed out, or the API failed
#   GhSetupError      -- the HOST is not set up (binary absent / no auth session);
#                        carries ``reason`` ("not_installed" | "not_authenticated")
#                        so the connect dialog offers instructions, not a raw error
#   GhPermissionError -- HTTP 403 for want of a permission, so the members path can
#                        fall back to the derived roster and writes can map to 403
#   RepoUrlError      -- the URL is not a well-formed repo link (maps to 400)
GhCliError = ProviderCliError
GhSetupError = ProviderSetupError
GhPermissionError = ProviderPermissionError

__all__ = [
    "GhCliError",
    "GhPermissionError",
    "GhSetupError",
    "PrSearchError",
    "RepoUrlError",
]

GH_TIMEOUT_SEC = 20.0
# Open issues are loaded in FULL via --paginate, which can span many pages on a
# busy repo (kirodotdev/Kiro ~2.6k open → ~26 pages), so it gets a much larger
# budget than the single-shot calls. The result is cached, so this cost is paid
# once per refresh, not per view.
GH_PAGINATE_TIMEOUT_SEC = 120.0

_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def parse_github_repo_url(link: str) -> tuple[str, str]:
    """Parse ``(owner, repo)`` from a full ``https://github.com/<owner>/<repo>`` URL.

    Deliberately strict (full URL only, per product decision — no bare
    ``owner/repo`` shorthand): rejects non-github.com hosts (SSRF guard) and
    constrains owner/repo to a safe charset before either value is ever
    interpolated into a subprocess argv.
    """
    if not link or not isinstance(link, str):
        raise RepoUrlError("repo link is empty")
    parsed = urlparse(link.strip())
    host = (parsed.hostname or "").lower()
    if host not in {"github.com", "www.github.com"}:
        raise RepoUrlError(
            f"not a github.com URL: {link!r} (expected https://github.com/<owner>/<repo>)"
        )
    parts = [p for p in (parsed.path or "").split("/") if p]
    if len(parts) < 2:
        raise RepoUrlError(f"not a full repo URL: {link!r} (expected .../<owner>/<repo>)")
    owner, repo = parts[0], re.sub(r"\.git$", "", parts[1])
    if owner in (".", "..") or repo in (".", "..") or not (
        _SEGMENT_RE.match(owner) and _SEGMENT_RE.match(repo)
    ):
        raise RepoUrlError(f"invalid owner/repo segment in {link!r}")
    return owner, repo


# ── gh spawn hardening ───────────────────────────────────────────────────────
#
# These gh calls are benign-allowlisted in the repo's spawn audit: gh needs the
# host's OWN authenticated session and cannot be sandbox-routed (the sandbox
# would hide ~/.config/gh + the keychain, breaking auth). As defense-in-depth
# WITHIN that classification, every spawn goes through ``_gh_run``, which
# (1) resolves a trusted canonical ``gh`` (never a shim on the agent-writable
# front of PATH) and (2) hands the child a MINIMAL environment — PATH/HOME/XDG
# plus gh's own auth/network vars — instead of the gateway's full env, so
# unrelated secrets (AWS/Slack/SSH) can never leak to a substituted or
# compromised gh.

# gh's own auth + network/TLS vars, forwarded (when present) on top of the
# platform's minimal safe-key base; everything else in the parent env is dropped.
_GH_ENV_PASSTHROUGH = (
    "GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN",
    "GH_HOST", "GH_CONFIG_DIR",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "no_proxy", "all_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
)

_gh_bin_cache: str | None = None

# gh resolution reuses the SAME policy and search order as the Sidebar PR panel
# (source_providers.provider_executable_candidates) so both panels accept exactly
# the same gh installs and never drift. Imported lazily inside _gh_bin() (its
# owning module pulls in dashboard state, so a top-level import here would be
# circular).


def _gh_bin() -> str:
    """Absolute path to an acceptable ``gh``, resolved once and cached.

    Resolution and validation are shared with the Sidebar PR panel
    (``source_providers.provider_executable_candidates`` +
    ``_validate_provider_executable``): the well-known install dirs first, then
    the ambient ``PATH``, accepting the user's own install (Homebrew included)
    while refusing a binary owned by another user, a world-writable one, or one
    inside the agent-writable project/workspace tree. Set
    ``KIROCREW_ISSUE_RADAR_GH`` to an absolute path to override (still
    validated), or ``KIROCREW_PROVIDER_BIN_STRICT=1`` to require a root-owned
    ``gh``. Raises :class:`GhSetupError` if no acceptable executable is found."""
    global _gh_bin_cache
    if _gh_bin_cache:
        return _gh_bin_cache
    if sys.platform == "win32":
        raise GhCliError(
            "Issue Radar requires a POSIX platform (macOS/Linux); "
            "Windows is not supported — use WSL to run the Kiro Crew gateway"
        )

    from kiro_crew.dashboard.handlers.source_providers import (
        _validate_provider_executable,
        provider_executable_candidates,
    )

    # Operator override — still validated.
    override = os.environ.get("KIROCREW_ISSUE_RADAR_GH")
    if override:
        try:
            validated = _validate_provider_executable(override)
            _gh_bin_cache = validated
            return validated
        except (ValueError, OSError) as exc:
            # A host-setup problem the user must fix (wrong path, a binary owned
            # by another user), not a transient API failure — surface it as a
            # GhSetupError so the connect dialog offers instructions.
            raise GhSetupError(
                f"KIROCREW_ISSUE_RADAR_GH={override!r} failed validation: {exc}",
                reason="not_installed",
            ) from exc

    # Well-known install dirs first, then the ambient PATH.
    last_error = ""
    for cand in provider_executable_candidates("gh"):
        if not os.path.isfile(cand):
            continue
        try:
            validated = _validate_provider_executable(cand)
            _gh_bin_cache = validated
            return validated
        except (ValueError, OSError) as exc:
            last_error = str(exc)
            continue  # untrusted provenance — skip

    detail = f" (last check: {last_error})" if last_error else ""
    raise GhSetupError(
        "the `gh` CLI was not found on this host"
        f"{detail} — install it (`brew install gh` or your distro's package "
        "manager) and run `gh auth login`, or set KIROCREW_ISSUE_RADAR_GH to an "
        "absolute gh path",
        reason="not_installed",
    )


def _gh_env() -> dict[str, str]:
    """A minimal environment for ``gh``: the platform's safe-key base
    (PATH/HOME/XDG/…) plus gh's own auth + network/TLS vars when set — NOT the
    gateway's full environment, so unrelated secrets never reach the child."""
    from kiro_crew.apps.registry import minimal_env

    return minimal_env(**{k: os.environ[k] for k in _GH_ENV_PASSTHROUGH if k in os.environ})


def _stderr_tail(proc: subprocess.CompletedProcess) -> str:
    """Last few stderr lines, sanitized for display.

    These strings travel to the browser through the routes' error bodies, so host
    paths and private hosts are stripped while the actionable phrasing (auth,
    not-found, 403, timeout) is preserved.
    """
    return sanitize_cli_stderr(" ".join((proc.stderr or "").strip().splitlines()[-3:]))


def _gh_run(argv: list[str], *, timeout: float, input_text: str | None = None) -> subprocess.CompletedProcess:
    """Single spawn chokepoint for every ``gh`` call — replaces argv[0] with the
    trusted canonical gh and passes the minimal env (see the hardening note
    above). Emits an SEL tool-invocation event on success, failure, and timeout
    (matching ``source_providers._run_json``)."""
    gh = _gh_bin()
    operation = f"gh {' '.join(argv[1:3])}"  # e.g. "gh api repos/…" (bounded)
    try:
        proc = subprocess.run(
            [gh, *argv[1:]],
            capture_output=True, text=True, timeout=timeout, check=False,
            input=input_text, env=_gh_env(),
        )
    except FileNotFoundError as exc:  # pragma: no cover — _gh_bin guards first
        _audit("gh_run", operation, "failure", error="gh not found")
        raise GhSetupError(
            "the `gh` CLI is not installed on this host", reason="not_installed"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        _audit("gh_run", operation, "failure", error=f"timeout after {timeout}s")
        raise GhCliError(f"`gh` timed out after {timeout}s") from exc
    if proc.returncode != 0:
        _audit("gh_run", operation, "failure", error=f"exit {proc.returncode}")
    else:
        _audit("gh_run", operation, "ok")
    return proc


def _audit(op: str, target: str, outcome: str, *, error: str = "") -> None:
    """SEL event for every gh spawn (reads and writes). Fire-and-forget."""
    from kiro_crew.sel import sel
    sel().log_api_access(
        caller="core:issue-radar",
        operation=f"issue_radar.{op}",
        outcome=outcome,
        source="builtin-app",
        resources=target[:200],
        error=error[:200] if error else "",
    )


def _run_gh_api(path: str, jq_filter: str, *, timeout: float = GH_TIMEOUT_SEC, paginate: bool = True) -> list[dict]:
    """Run ``gh api <path> --jq <filter>`` and parse JSONL stdout.

    List argv only (never ``shell=True``); ``path`` must already be built from
    charset-validated owner/repo segments by the caller. ``paginate`` follows
    ``Link`` headers to fetch every page (used for open issues/labels); pass
    ``paginate=False`` to cap at a single ``per_page`` page (used for closed
    issues, which can number in the thousands).
    """
    argv = ["gh", "api", path]
    if paginate:
        argv.append("--paginate")
    argv += ["--jq", jq_filter]
    proc = _gh_run(argv, timeout=timeout)

    if proc.returncode != 0:
        tail = _stderr_tail(proc)
        _raise_if_auth_failure(tail)
        raise GhCliError(f"gh api {path} failed (exit {proc.returncode}): {tail}")

    out: list[dict] = []
    for line in (proc.stdout or "").splitlines():  # --jq emits JSONL, one object per line
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


# Markers `gh` prints when the CLI itself has no usable credentials (as opposed
# to a repo simply being out of reach for an authenticated user). Matched
# case-insensitively against the stderr tail so the connect dialog can offer
# `gh auth login` instead of echoing an opaque exit code.
_GH_AUTH_MARKERS = (
    "gh auth login",
    "not logged in",
    "authentication required",
    "requires authentication",
    "bad credentials",
    "http 401",
)


def _raise_if_auth_failure(stderr_tail: str) -> None:
    """Re-classify an unauthenticated `gh` failure as :class:`GhSetupError`.

    No-op for every other failure, so the caller still raises its own, more
    specific ``GhCliError`` with the full context.
    """
    low = (stderr_tail or "").lower()
    if any(m in low for m in _GH_AUTH_MARKERS):
        raise GhSetupError(
            "the `gh` CLI is not authenticated — run `gh auth login`",
            reason="not_authenticated",
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
        "gh", "api", f"repos/{owner}/{repo}",
        "--jq", "{full_name: .full_name, private: .private, "
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
    "thumbs_up: (.reactions[\"+1\"] // 0), "
    "author_association: (.author_association // null), "
    "updated_at: .updated_at, created_at: .created_at, state: .state, "
    "author: (.user.login // null), assignees: [.assignees[].login], "
    "body: (.body // \"\")}"
)


def _list_issues(owner: str, repo: str, state: str, *, timeout: float, paginate: bool) -> list[dict]:
    """List issues of ``state`` (excludes PRs), most-recently-updated first.

    ``paginate=True`` loads the FULL set across every page (used for open
    issues); ``paginate=False`` caps at a single ``per_page=100`` page (used
    for closed issues, whose backlog can be tens of thousands — pulling all of
    them would be slow and is out of scope for a triage view).
    """
    path = f"repos/{owner}/{repo}/issues?state={state}&sort=updated&direction=desc&per_page=100"
    return _run_gh_api(path, _ISSUE_JQ, timeout=timeout, paginate=paginate)


def list_open_issues(owner: str, repo: str, *, timeout: float = GH_PAGINATE_TIMEOUT_SEC) -> list[dict]:
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


def probe_open_list(
    owner: str, repo: str, kind: str, *, timeout: float = GH_TIMEOUT_SEC
) -> dict:
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
_CONTRIB_EVENT_TYPES = frozenset({
    "PushEvent",
    "PullRequestEvent",
    "PullRequestReviewEvent",
    "PullRequestReviewCommentEvent",
    "IssuesEvent",
    "IssueCommentEvent",
    "CommitCommentEvent",
    "CreateEvent",
    "ReleaseEvent",
})

_EVENT_JQ = (
    ".[] | {type: .type, repo: (.repo.name // null), "
    "created_at: (.created_at // null)}"
)


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
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=days) if days else None
    )

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
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


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
    jq_filter = ".[] | {name: .name, color: .color, description: (.description // \"\")}"
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
_MEMBER_ASSOC_RANK = {"OWNER": 3, "MEMBER": 2, "COLLABORATOR": 1}


def list_repo_collaborators(owner: str, repo: str, *, timeout: float = GH_PAGINATE_TIMEOUT_SEC) -> list[dict]:
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
    best: dict[str, str] = {}
    for iss in issues:
        login = iss.get("author")
        assoc = iss.get("author_association")
        if not login or assoc not in _MEMBER_ASSOC_RANK:
            continue
        current = best.get(login)
        if current is None or _MEMBER_ASSOC_RANK[assoc] > _MEMBER_ASSOC_RANK[current]:
            best[login] = assoc
    return [{"login": login, "association": assoc} for login, assoc in sorted(best.items())]


# ── single-issue detail + timeline (powers the detail pane) ──────────────────

# Shaped so the detail pane can render everything the list view omits — body,
# state_reason, author_association, closed_at/closed_by, locked, per-label
# colour, assignees, milestone, and the full reaction breakdown — in one call.
_ISSUE_DETAIL_JQ = (
    "{number: .number, title: .title, body: (.body // \"\"), state: .state, "
    "state_reason: .state_reason, url: .html_url, author: (.user.login // null), "
    "author_association: (.author_association // null), created_at: .created_at, "
    "updated_at: .updated_at, closed_at: .closed_at, closed_by: (.closed_by.login // null), "
    "comments: .comments, locked: .locked, "
    "labels: [.labels[] | {name: .name, color: .color, description: (.description // \"\")}], "
    "assignees: [.assignees[].login], "
    "milestone: (if .milestone then {title: .milestone.title, state: .milestone.state, "
    "due_on: .milestone.due_on} else null end), "
    "reactions: (if .reactions then {total: .reactions.total_count, plus1: .reactions[\"+1\"], "
    "minus1: .reactions[\"-1\"], laugh: .reactions.laugh, hooray: .reactions.hooray, "
    "confused: .reactions.confused, heart: .reactions.heart, rocket: .reactions.rocket, "
    "eyes: .reactions.eyes} else null end)}"
)


def get_issue_detail(owner: str, repo: str, number: int, *, timeout: float = GH_TIMEOUT_SEC) -> dict:
    """Full detail for one issue via ``gh api repos/{o}/{r}/issues/{n}``.

    Returns the richer field set the detail pane needs but the list view omits
    (see ``_ISSUE_DETAIL_JQ``). ``number`` is coerced to ``int`` before it ever
    reaches the argv, so it cannot inject path segments. Uses the same
    single-object subprocess pattern as ``verify_repo_access`` (one compact JSON
    object on stdout) rather than the JSONL ``_run_gh_api`` path.
    """
    argv = [
        "gh", "api", f"repos/{owner}/{repo}/issues/{int(number)}",
        "--jq", _ISSUE_DETAIL_JQ,
    ]
    proc = _gh_run(argv, timeout=timeout)

    if proc.returncode != 0:
        tail = _stderr_tail(proc)
        raise GhCliError(f"could not read {owner}/{repo}#{int(number)} (exit {proc.returncode}): {tail}")

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
        "gh", "api", f"repos/{owner}/{repo}/issues/{int(number)}",
        "--jq", _REF_SUMMARY_JQ,
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
    if not r:
        return None
    total = r.get("total_count") or 0
    if total <= 0:
        return None
    return {
        "total": total,
        "plus1": r.get("+1", 0), "minus1": r.get("-1", 0),
        "laugh": r.get("laugh", 0), "hooray": r.get("hooray", 0),
        "confused": r.get("confused", 0), "heart": r.get("heart", 0),
        "rocket": r.get("rocket", 0), "eyes": r.get("eyes", 0),
    }


def _actor_login(ev: dict) -> str | None:
    return (ev.get("actor") or {}).get("login")


def _normalize_timeline_event(ev: dict) -> dict | None:
    """Map one raw GitHub timeline event to the compact uniform shape the UI
    renders, or ``None`` to drop it.

    GitHub emits ~30 event types; only the ones that matter for triage are kept
    (comments, label/assignee/milestone changes, close/reopen/rename, and
    cross-references from other issues/PRs). The rest — subscribed, mentioned,
    review_requested, head_ref_*, and similar bookkeeping — are noise here and
    are dropped.
    """
    etype = ev.get("event")
    created = ev.get("created_at")
    if etype == "commented":
        return {
            "kind": "comment",
            "actor": (ev.get("user") or {}).get("login"),
            "created_at": created,
            "body": ev.get("body") or "",
            "author_association": ev.get("author_association"),
            "reactions": _norm_reactions(ev.get("reactions")),
        }
    if etype in ("labeled", "unlabeled"):
        lab = ev.get("label") or {}
        return {"kind": etype, "actor": _actor_login(ev), "created_at": created,
                "label": {"name": lab.get("name"), "color": lab.get("color")}}
    if etype in ("assigned", "unassigned"):
        return {"kind": etype, "actor": _actor_login(ev), "created_at": created,
                "assignee": (ev.get("assignee") or {}).get("login")}
    if etype == "closed":
        return {"kind": "closed", "actor": _actor_login(ev), "created_at": created,
                "state_reason": ev.get("state_reason"), "commit_id": ev.get("commit_id")}
    if etype == "reopened":
        return {"kind": "reopened", "actor": _actor_login(ev), "created_at": created}
    if etype == "renamed":
        rn = ev.get("rename") or {}
        return {"kind": "renamed", "actor": _actor_login(ev), "created_at": created,
                "rename": {"from": rn.get("from"), "to": rn.get("to")}}
    if etype in ("milestoned", "demilestoned"):
        return {"kind": etype, "actor": _actor_login(ev), "created_at": created,
                "milestone": (ev.get("milestone") or {}).get("title")}
    if etype == "cross-referenced":
        src = (ev.get("source") or {}).get("issue") or {}
        return {"kind": "cross-referenced", "actor": _actor_login(ev), "created_at": created,
                "source": {"number": src.get("number"), "title": src.get("title"),
                           "url": src.get("html_url"), "state": src.get("state"),
                           "is_pr": bool(src.get("pull_request"))}}
    if etype == "referenced":
        return {"kind": "referenced", "actor": _actor_login(ev), "created_at": created,
                "commit_id": ev.get("commit_id")}
    # ── pull-request-only timeline events (never emitted for plain issues) ──
    # A PR's timeline additionally carries code reviews and commits. They are
    # additive here: an issue timeline never contains them, so keeping them in
    # the shared normalizer only enriches the PR detail pane. ``reviewed`` uses
    # ``submitted_at`` (not ``created_at``) and ``committed`` uses the commit
    # author's date, so both fall back to those before the generic ``created``.
    if etype == "reviewed":
        return {"kind": "reviewed", "actor": (ev.get("user") or {}).get("login"),
                "created_at": ev.get("submitted_at") or created,
                "review_state": ev.get("state"), "body": ev.get("body") or ""}
    if etype == "committed":
        # Commit events have no ``actor`` object — the author is embedded, and
        # the human-facing login (when present) lives on ``.author`` too.
        author = ev.get("author") or {}
        return {"kind": "committed",
                "actor": author.get("name") or (ev.get("committer") or {}).get("name"),
                "created_at": author.get("date") or (ev.get("committer") or {}).get("date") or created,
                "commit_id": ev.get("sha"),
                "message": (ev.get("message") or "").splitlines()[0] if ev.get("message") else ""}
    return None


def list_issue_timeline(owner: str, repo: str, number: int, *, timeout: float = GH_PAGINATE_TIMEOUT_SEC) -> list[dict]:
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
    ".[] | {kind: \"review_comment\", actor: (.user.login // null), "
    "created_at: .created_at, body: (.body // \"\"), "
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


# ── write primitives (triage actions: label + state) ────────────────────────
#
# These are the ONLY mutating calls Issue Radar makes. Per the feature design,
# they are the write half of the "suggest → confirm" loop (accept an AI-suggested
# label, hand-pick a label, close/reopen a triaged issue) — deliberately NOT a
# GitHub client clone (no title/body edit, no label CRUD). Every one:
#   • is a list-argv subprocess (never ``shell=True``);
#   • coerces ``number`` to ``int`` before it reaches the path;
#   • sends its request body as JSON on stdin (``--input -``) so label names /
#     state reasons are DATA, never argv the shell could reinterpret;
#   • URL-encodes any value that must sit in the path (the label name on DELETE).
# The route layer additionally gates them on triage/push access and validates
# label names against the repo's real label set before calling in here.


def _run_gh_write(
    method: str, path: str, payload: dict | None = None, *, timeout: float = GH_TIMEOUT_SEC
) -> object:
    """Run ``gh api --method <METHOD> <path>`` (optionally with a JSON stdin body)
    and return the parsed JSON response (dict/list), or ``None`` on empty output.

    A non-zero exit whose stderr carries ``HTTP 403`` (or 401/404 on a write —
    GitHub returns 404 rather than 403 when the token cannot even see the write
    surface) is raised as :class:`GhPermissionError` so the route maps it to an
    HTTP 403 instead of a generic 502. ``payload`` is serialized to JSON and fed
    on stdin, never interpolated into argv.
    """
    argv = ["gh", "api", "--method", method, path]
    input_text: str | None = None
    if payload is not None:
        argv += ["--input", "-"]
        input_text = json.dumps(payload)
    proc = _gh_run(argv, timeout=timeout, input_text=input_text)

    if proc.returncode != 0:
        stderr = proc.stderr or ""
        tail = sanitize_cli_stderr(" ".join(stderr.strip().splitlines()[-3:]))
        if "HTTP 403" in stderr or "HTTP 401" in stderr:
            raise GhPermissionError(
                f"GitHub refused the write ({method} {path}) — your `gh` session "
                f"lacks the required triage/push access: {tail}"
            )
        raise GhCliError(f"gh api {method} {path} failed (exit {proc.returncode}): {tail}")

    out = (proc.stdout or "").strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def _shape_labels(raw: object) -> list[dict]:
    """Normalize a GitHub label array (as returned by the labels endpoints) to
    the ``[{name, color, description}]`` shape the detail pane + caches use.
    Tolerates a non-list (returns ``[]``)."""
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for lab in raw:
        if isinstance(lab, dict) and lab.get("name"):
            out.append({
                "name": lab.get("name"),
                "color": lab.get("color") or "888888",
                "description": lab.get("description") or "",
            })
    return out


def get_repo_permissions(owner: str, repo: str, *, timeout: float = GH_TIMEOUT_SEC) -> dict:
    """Return the authenticated `gh` user's permission object for the repo
    (``{admin, maintain, push, triage, pull}``), or ``{}`` if GitHub omits it.

    Thin wrapper over :func:`verify_repo_access` (which already selects
    ``.permissions``); the write routes use it to gate on ``triage``/``push``."""
    perms = verify_repo_access(owner, repo, timeout=timeout).get("permissions")
    return perms if isinstance(perms, dict) else {}


def add_issue_labels(
    owner: str, repo: str, number: int, labels: list[str], *, timeout: float = GH_TIMEOUT_SEC
) -> list[dict]:
    """Add ``labels`` to an issue (``POST .../issues/{n}/labels``).

    GitHub is additive + idempotent here (already-present labels are no-ops) and
    returns the issue's FULL label set after the add, which is returned shaped.
    ``labels`` is sent as a JSON body on stdin, so names with spaces/specials
    (e.g. ``good first issue``) are safe."""
    data = _run_gh_write(
        "POST", f"repos/{owner}/{repo}/issues/{int(number)}/labels",
        {"labels": list(labels)}, timeout=timeout,
    )
    return _shape_labels(data)


def remove_issue_label(
    owner: str, repo: str, number: int, label: str, *, timeout: float = GH_TIMEOUT_SEC
) -> list[dict] | None:
    """Remove ONE label from an issue (``DELETE .../issues/{n}/labels/{label}``).

    The label name is URL-encoded into the path (GitHub has no bulk-remove; it
    is one call per label). Returns the issue's remaining labels, shaped. A
    ``404`` (the label was not on the issue) is idempotent success but the
    remaining set is unknown, so it returns ``None`` — the caller then re-reads
    the authoritative set rather than assuming the labels were cleared."""
    enc = quote(label, safe="")
    try:
        data = _run_gh_write(
            "DELETE", f"repos/{owner}/{repo}/issues/{int(number)}/labels/{enc}",
            None, timeout=timeout,
        )
    except GhCliError as exc:
        if "HTTP 404" in str(exc) or "Label does not exist" in str(exc):
            return None
        raise
    return _shape_labels(data)


def set_issue_state(
    owner: str, repo: str, number: int, state: str, state_reason: str | None = None,
    *, timeout: float = GH_TIMEOUT_SEC,
) -> dict:
    """Close or reopen an issue (``PATCH .../issues/{n}``).

    ``state`` is ``"open"`` or ``"closed"``. On close, ``state_reason`` may be
    ``"completed"`` (default) or ``"not_planned"``; on reopen the reason is
    cleared. Returns ``{"state", "state_reason"}`` from the updated issue."""
    payload: dict[str, object] = {"state": state}
    if state == "closed":
        payload["state_reason"] = state_reason or "completed"
    else:
        # Reopen: clear any prior close reason (GitHub accepts null here).
        payload["state_reason"] = None
    data = _run_gh_write(
        "PATCH", f"repos/{owner}/{repo}/issues/{int(number)}", payload, timeout=timeout
    )
    if isinstance(data, dict):
        return {"state": data.get("state", state), "state_reason": data.get("state_reason")}
    return {"state": state, "state_reason": payload.get("state_reason")}


def create_label(
    owner: str, repo: str, name: str, color: str = "888888", description: str = "",
    *, timeout: float = GH_TIMEOUT_SEC,
) -> dict:
    """Create a new label on the repo (``POST repos/{o}/{r}/labels``).

    ``color`` is a 6-hex string WITHOUT a leading ``#`` (GitHub's labels API
    rejects the ``#``); a leading ``#`` is stripped defensively. ``name`` /
    ``description`` ride in a JSON stdin body (never argv), so names with
    spaces/specials (e.g. ``good first issue``) are safe. The caller gates this
    on triage/push access; a 403/401 surfaces as :class:`GhPermissionError`.

    Idempotent: if the label already exists (GitHub 422) the existing label is
    re-read and returned rather than raising. Returns ``{name, color,
    description}`` (via :func:`_shape_labels`)."""
    hexcolor = (color or "").lstrip("#").strip() or "888888"
    payload = {"name": name, "color": hexcolor, "description": description or ""}
    try:
        data = _run_gh_write("POST", f"repos/{owner}/{repo}/labels", payload, timeout=timeout)
    except GhPermissionError:
        raise  # no write access — route maps to HTTP 403
    except GhCliError as exc:
        # 422 == label already exists: idempotent success. Re-read the existing
        # label so the returned color/description are truthful, not our request.
        if "HTTP 422" in str(exc) or "already_exists" in str(exc):
            existing = _run_gh_write(
                "GET", f"repos/{owner}/{repo}/labels/{quote(name, safe='')}", None, timeout=timeout
            )
            shaped = _shape_labels([existing] if isinstance(existing, dict) else [])
            return shaped[0] if shaped else dict(payload)
        raise
    shaped = _shape_labels([data] if isinstance(data, dict) else [])
    return shaped[0] if shaped else dict(payload)


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
    "body: (.body // \"\")}"
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


def list_open_pulls(owner: str, repo: str, *, timeout: float = GH_PAGINATE_TIMEOUT_SEC) -> list[dict]:
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
    "{number: .number, title: .title, body: (.body // \"\"), state: .state, "
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
    "labels: [.labels[] | {name: .name, color: .color, description: (.description // \"\")}], "
    "assignees: [.assignees[].login], "
    "requested_reviewers: [.requested_reviewers[].login], "
    "milestone: (if .milestone then {title: .milestone.title, state: .milestone.state, "
    "due_on: .milestone.due_on} else null end)}"
)


def get_pr_detail(
    owner: str, repo: str, number: int, *, timeout: float = GH_TIMEOUT_SEC,
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
        if retried.get("mergeable") is not None or retried.get("mergeable_state") not in (None, "unknown"):
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
        "gh", "api", f"repos/{owner}/{repo}/pulls/{int(number)}",
        "--jq", _PR_DETAIL_JQ,
    ]
    proc = _gh_run(argv, timeout=timeout)

    if proc.returncode != 0:
        tail = _stderr_tail(proc)
        raise GhCliError(f"could not read {owner}/{repo} PR #{int(number)} (exit {proc.returncode}): {tail}")

    try:
        return json.loads(proc.stdout.strip())
    except json.JSONDecodeError as exc:
        raise GhCliError(f"gh returned unexpected output for {owner}/{repo} PR #{int(number)}") from exc


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
    "summary: ((.output.title // .output.summary) // \"\"), "
    "app: (.app.name // null), "
    "source: ((.app.slug // .app.name) // \"check\")}"
)

# Commit statuses have no queued/in-progress distinction: the state itself
# carries "pending", so status is reported as completed and the mapping below
# routes "pending" into the running bucket.
_COMMIT_STATUS_JQ = (
    ".statuses[] | {name: .context, status: \"completed\", conclusion: .state, "
    "url: (.target_url // null), started_at: .created_at, completed_at: .updated_at, "
    "summary: (.description // \"\"), app: null, "
    "source: \"status\"}"
)

# GitHub conclusion / state -> coarse bucket. Anything unrecognized is treated as
# "other" (informational), never silently as success.
_CHECK_FAILURE_CONCLUSIONS = {
    "failure", "timed_out", "action_required", "startup_failure", "stale", "error",
}
_CHECK_RUNNING_STATES = {
    "queued", "in_progress", "pending", "waiting", "requested",
    # GraphQL's rollup/context vocabulary adds this one; harmless for REST.
    "expected",
}
_CHECK_OTHER_CONCLUSIONS = {"neutral", "skipped", "cancelled", "canceled"}


def _check_bucket(status: str | None, conclusion: str | None) -> str:
    """Coarse bucket for one check: ``failure`` | ``running`` | ``success`` |
    ``other``. Status is consulted first — an in-flight run has no conclusion
    yet — then the conclusion value.

    This is the ONLY bucketing table in the module: the REST check rows, the
    GraphQL per-context rows and the GraphQL aggregate rollup all funnel through
    it (values are case-folded, so GraphQL's ``IN_PROGRESS`` and REST's
    ``in_progress`` are the same input). Keeping one table is what actually makes
    "a card dot and the detail sidebar can never disagree about red" true —
    parallel tables would only be edit-locked by convention.
    """
    st = (status or "").lower()
    cc = (conclusion or "").lower()
    if st in _CHECK_RUNNING_STATES or cc in _CHECK_RUNNING_STATES:
        return "running"
    if cc in _CHECK_FAILURE_CONCLUSIONS:
        return "failure"
    if cc == "success":
        return "success"
    if cc in _CHECK_OTHER_CONCLUSIONS:
        return "other"
    # Completed with an unknown/absent conclusion — informational, not passing.
    return "other"


def _check_identity(row: dict) -> tuple[str, str]:
    """The identity two check rows must share to be considered the same check.

    NOT the display name alone: different apps (and the legacy Status API) may
    publish a check with the same name, and collapsing across publishers would
    let one app's later success hide another app's failure. ``source`` is the
    publishing app's slug (or ``"status"`` for a commit status).
    """
    return (str(row.get("source") or ""), str(row.get("name") or ""))


def _dedupe_checks(rows: list[dict]) -> list[dict]:
    """Keep only the LATEST row per (publisher, check name).

    Two rows can share an identity for the same head sha even though GitHub's
    ``filter=latest`` already collapses re-ATTEMPTS: the same workflow file can be
    started as two separate RUNS for one sha (observed on this repo —
    ``code-review.yml`` triggered twice 12s apart, two check-suites, each
    ``run_attempt: 1``), and each run contributes its own row per job. The later
    run supersedes the earlier one, so latest wins.
    """
    def _key(r: dict) -> tuple[str, str]:
        # started_at first: an OLDER run that finished (completed 10:10) must not
        # outrank a NEWER run that is still going (started 10:15, no completed_at),
        # which is exactly what comparing completed_at first would do — the UI
        # would show a stale pass while its replacement was still running.
        return (str(r.get("started_at") or ""), str(r.get("completed_at") or ""))

    best: dict[tuple[str, str], dict] = {}
    for r in rows:
        if not r.get("name"):
            # A nameless row would render as a blank line.
            continue
        ident = _check_identity(r)
        prev = best.get(ident)
        if prev is None or _key(r) >= _key(prev):
            best[ident] = r
    return list(best.values())


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
        out.append({
            "name": r.get("name"),
            "bucket": _check_bucket(r.get("status"), r.get("conclusion")),
            "status": r.get("status"),
            "conclusion": r.get("conclusion"),
            "url": r.get("url"),
            "summary": (r.get("summary") or "")[:300],
            "app": r.get("app"),
            "started_at": r.get("started_at"),
            "completed_at": r.get("completed_at"),
        })
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

# Our own lifecycle names -> GraphQL PullRequestState literals. The values are
# interpolated into the query, so they come from THIS map only — never from
# caller input — which keeps the query free of injection surface.
_GRAPHQL_PR_STATES = {"open": "OPEN", "closed": "CLOSED, MERGED"}

# The bucket keys every counts dict carries, so the frontend never has to guard a
# missing key and the render order of the card's badges is fixed.
_CHECK_BUCKETS = ("failure", "running", "success", "other")

# How many rollup contexts one GraphQL page carries. A PR with more than this has
# a TRUNCATED tally, which the row reports so the card can fall back to the
# aggregate rollup instead of presenting an incomplete count as complete.
_ROLLUP_CONTEXT_PAGE = 100

# One PR's contexts, projected into the SAME row shape the REST check list uses
# (name / source / status / conclusion / timestamps) so they can go through
# _dedupe_checks and _check_bucket unchanged. Re-implementing either on a
# bespoke string protocol is what previously let the card and the sidebar
# disagree; sharing the code makes agreement structural.
_ROLLUP_CONTEXTS_JQ = (
    "[(.commits.nodes[0].commit.statusCheckRollup.contexts.nodes[]? | "
    "{name: ((.name // .context) // \"\"), "
    "source: ((.checkSuite.app.slug // .checkSuite.app.name) // \"status\"), "
    "status: (.status // null), "
    "conclusion: ((.conclusion // .state) // null), "
    "started_at: ((.startedAt // .createdAt) // null), "
    "completed_at: (.completedAt // null)})]"
)

# The GraphQL selection for one PR's card enrichment, shared by both fetchers so
# the two paths can never drift apart in what they ask for.
_PR_SUMMARY_SELECTION = (
    " number additions deletions changedFiles"
    # ``state`` + ``mergedAt`` fix the "already merged" half of the auto-merge defect: a
    # row cached before a PR merged was still offered auto-merge, and the provider
    # answered "Pull request is already merged". The list row has a ``state`` from its
    # own source, but a SEARCH row's can be equally stale, and this is the read that
    # happens closest to the action. ``mergedAt`` is required alongside ``state``
    # because ``state`` alone cannot express the lifecycle in REST's vocabulary:
    # GraphQL has a distinct ``MERGED`` state, REST has only ``open``/``closed`` plus a
    # ``merged_at`` timestamp, and the row shape is REST's — correcting a stale
    # ``state`` to ``closed`` without the timestamp would render a merged PR with the
    # red closed-unmerged icon.
    #
    # These three ARE free (measured: adding them changes neither this query's latency
    # nor its success rate). ``mergeStateStatus`` is deliberately NOT here — it is the
    # one field that is not. See :data:`_PR_READINESS_SELECTION`.
    " mergeable state mergedAt"
    # ``oid`` is the head COMMIT. Free here — this selection already walks the last
    # commit for its check rollup — and it is what gives SEARCH rows a head sha:
    # ``_PR_SEARCH_JQ`` cannot supply one (GitHub's search API does not expose it),
    # so without this a person-filtered selection could not be bulk-approved, since
    # a review has to name the revision it was formed on.
    " commits(last:1){nodes{commit{oid statusCheckRollup{state"
    f"  contexts(first:{_ROLLUP_CONTEXT_PAGE}){{pageInfo{{hasNextPage}} nodes{{ __typename"
    "   ... on CheckRun{name conclusion status startedAt completedAt"
    "    checkSuite{app{slug name}}}"
    "   ... on StatusContext{context state createdAt} }}}}}}"
)

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
_PR_READINESS_SELECTION = " number mergeStateStatus"

_PR_READINESS_JQ_BODY = (
    "{number: .number, merge_state_status: (.mergeStateStatus // null)}"
)

# Smaller than `_SUMMARY_BATCH` (100) on purpose: this is the field GitHub COMPUTES, and
# the by-number form asks for N of them in one query. 50 is the largest page measured
# comfortable; the page-size ceiling is exactly what the split exists to respect.
_READINESS_BATCH = 50

# The JQ projection applied to ONE PR node (shared for the same reason).
_PR_SUMMARY_JQ_BODY = (
    "{number: .number, additions: .additions, deletions: .deletions, "
    "changed_files: (.changedFiles // 0), "
    # Carried through under GraphQL's own names; `_parse_summary_rows` lowercases them
    # into REST's vocabulary, which is the spelling every reader already uses.
    "mergeable_raw: (.mergeable // null), "
    "pr_state: (.state // null), "
    "pr_merged_at: (.mergedAt // null), "
    "head_sha: (.commits.nodes[0].commit.oid // null), "
    "rollup: (.commits.nodes[0].commit.statusCheckRollup.state // null), "
    "contexts_truncated: "
    "(.commits.nodes[0].commit.statusCheckRollup.contexts.pageInfo.hasNextPage // false), "
    f"contexts: {_ROLLUP_CONTEXTS_JQ}}}"
)


def fetch_pr_summaries(
    owner: str, repo: str, state: str = "open", *, timeout: float = GH_TIMEOUT_SEC
) -> dict[int, dict]:
    """``{number: {additions, deletions, changed_files, checks_state, checks_counts}}``
    for a repo's PRs, in ONE GraphQL call (see the module note above).

    ``checks_state`` is the aggregate status-check rollup and ``checks_counts`` the
    per-bucket tally of the individual checks, both bucketed the same way as
    :func:`list_pr_checks` (``success`` / ``failure`` / ``running`` / ``other``).
    ``checks_state`` is ``None`` when the PR has no checks at all. Raises
    :class:`GhCliError` on a failed call — callers treat the enrichment as
    optional and continue without it.
    """
    gql_state = _GRAPHQL_PR_STATES.get(state)
    if gql_state is None:
        raise GhCliError(f"unsupported state for PR summaries: {state!r}")
    query = (
        "query($owner:String!,$name:String!){"
        " repository(owner:$owner,name:$name){"
        f"  pullRequests(states:[{gql_state}], first:100,"
        "   orderBy:{field:UPDATED_AT,direction:DESC}){"
        "   nodes{" + _PR_SUMMARY_SELECTION + " } } } }"
    )
    argv = [
        "gh", "api", "graphql",
        "-f", f"query={query}",
        "-F", f"owner={owner}",
        "-F", f"name={repo}",
        "--jq", f".data.repository.pullRequests.nodes[] | {_PR_SUMMARY_JQ_BODY}",
    ]
    proc = _gh_run(argv, timeout=timeout)
    if proc.returncode != 0:
        tail = _stderr_tail(proc)
        raise GhCliError(f"gh api graphql (pr summaries) failed (exit {proc.returncode}): {tail}")

    return _parse_summary_rows(proc.stdout or "")


def fetch_pr_readiness(
    owner: str, repo: str, state: str = "open", *, timeout: float = GH_TIMEOUT_SEC
) -> dict[int, str | None]:
    """``{number: mergeable_state}`` for a repo's PRs — its own LEAN GraphQL call.

    Separate from :func:`fetch_pr_summaries` because ``mergeStateStatus`` is the one
    field here GitHub has to COMPUTE (a merge commit per PR); folded into the card
    selection the combined query 502s at this page size. See
    :data:`_PR_READINESS_SELECTION` for the measurements.

    Best-effort, like the card enrichment: raises :class:`GhCliError` and the caller
    continues without readiness, which costs the bulk bar's merge/arm split for that
    fetch but leaves every other field intact.
    """
    gql_state = _GRAPHQL_PR_STATES.get(state)
    if gql_state is None:
        raise GhCliError(f"unsupported state for PR readiness: {state!r}")
    query = (
        "query($owner:String!,$name:String!){"
        " repository(owner:$owner,name:$name){"
        f"  pullRequests(states:[{gql_state}], first:100,"
        "   orderBy:{field:UPDATED_AT,direction:DESC}){"
        "   nodes{" + _PR_READINESS_SELECTION + " } } } }"
    )
    argv = [
        "gh", "api", "graphql",
        "-f", f"query={query}",
        "-F", f"owner={owner}",
        "-F", f"name={repo}",
        "--jq", f".data.repository.pullRequests.nodes[] | {_PR_READINESS_JQ_BODY}",
    ]
    proc = _gh_run(argv, timeout=timeout)
    if proc.returncode != 0:
        tail = _stderr_tail(proc)
        raise GhCliError(f"gh api graphql (pr readiness) failed (exit {proc.returncode}): {tail}")
    return _parse_readiness_rows(proc.stdout or "")


def fetch_pr_readiness_by_number(
    owner: str, repo: str, numbers: list[int], *, timeout: float = GH_TIMEOUT_SEC
) -> dict[int, str | None]:
    """:func:`fetch_pr_readiness` for an EXPLICIT number list (the SEARCH path).

    Same reason :func:`fetch_pr_summaries_by_number` exists: a person-filtered search
    can return a PR that ranks outside the state-scoped window.
    """
    out: dict[int, str | None] = {}
    wanted = [n for n in numbers if isinstance(n, int) and n > 0]
    for start in range(0, len(wanted), _READINESS_BATCH):
        batch = wanted[start:start + _READINESS_BATCH]
        fields = " ".join(
            f"p{n}: pullRequest(number:{n}){{{_PR_READINESS_SELECTION} }}" for n in batch
        )
        query = (
            "query($owner:String!,$name:String!){"
            f" repository(owner:$owner,name:$name){{ {fields} }} }}"
        )
        argv = [
            "gh", "api", "graphql",
            "-f", f"query={query}",
            "-F", f"owner={owner}",
            "-F", f"name={repo}",
            "--jq", ".data.repository | to_entries[] | .value | select(. != null) | "
                    + _PR_READINESS_JQ_BODY,
        ]
        proc = _gh_run(argv, timeout=timeout)
        if proc.returncode != 0:
            tail = _stderr_tail(proc)
            raise GhCliError(
                f"gh api graphql (pr readiness by number) failed "
                f"(exit {proc.returncode}): {tail}"
            )
        out.update(_parse_readiness_rows(proc.stdout or ""))
    return out


def _parse_readiness_rows(stdout: str) -> dict[int, str | None]:
    """Parse the readiness JQ stream into ``{number: mergeable_state}`` (lowercased)."""
    out: dict[int, str | None] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        number = row.get("number")
        if not isinstance(number, int):
            continue
        out[number] = _lower_or_none(row.get("merge_state_status"))
    return out


# How many PR numbers to request per by-number GraphQL call. Each number costs
# one aliased field, so this bounds the query size while keeping the call count
# low (the search cap of 300 rows -> at most 3 calls).
_SUMMARY_BATCH = 100


def fetch_pr_summaries_by_number(
    owner: str, repo: str, numbers: list[int], *, timeout: float = GH_TIMEOUT_SEC
) -> dict[int, dict]:
    """Same payload as :func:`fetch_pr_summaries`, but for an EXPLICIT number list.

    The state-scoped variant only covers the most recently updated 100 PRs, which
    is exactly the window the search path exists to escape: a person filter can
    legitimately return a PR that ranks 147th by update time. Addressing PRs by
    number keeps enrichment complete for whatever the search returned.

    Numbers are ints (validated by the caller's own parsing), so they carry no
    injection surface even though they are interpolated as GraphQL aliases.
    """
    out: dict[int, dict] = {}
    wanted = [n for n in numbers if isinstance(n, int) and n > 0]
    for start in range(0, len(wanted), _SUMMARY_BATCH):
        batch = wanted[start:start + _SUMMARY_BATCH]
        fields = " ".join(
            f"p{n}: pullRequest(number:{n}){{{_PR_SUMMARY_SELECTION} }}"
            for n in batch
        )
        query = (
            "query($owner:String!,$name:String!){"
            f" repository(owner:$owner,name:$name){{ {fields} }} }}"
        )
        argv = [
            "gh", "api", "graphql",
            "-f", f"query={query}",
            "-F", f"owner={owner}",
            "-F", f"name={repo}",
            "--jq", ".data.repository | to_entries[] | .value | select(. != null) | "
                    + _PR_SUMMARY_JQ_BODY,
        ]
        proc = _gh_run(argv, timeout=timeout)
        if proc.returncode != 0:
            tail = _stderr_tail(proc)
            raise GhCliError(
                f"gh api graphql (pr summaries by number) failed (exit {proc.returncode}): {tail}"
            )
        out.update(_parse_summary_rows(proc.stdout or ""))
    return out


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
    rows = [c for c in contexts if isinstance(c, dict)] if isinstance(contexts, list) else []
    counts = {bucket: 0 for bucket in _CHECK_BUCKETS}
    for row in _dedupe_checks(rows):
        counts[_check_bucket(row.get("status"), row.get("conclusion"))] += 1
    return counts


def _lower_or_none(value: object) -> str | None:
    """A GraphQL enum lowered into REST's vocabulary, or ``None`` if absent.

    ``None`` is preserved rather than becoming ``""``: an absent mergeability is
    "GitHub has not computed this yet" (it is asynchronous, and a cold read answers
    ``UNKNOWN``), which callers must be able to tell from a real state. An empty
    string would compare unequal to every ready-state and so read as a confident
    "not ready".
    """
    return value.strip().lower() or None if isinstance(value, str) else None


def _graphql_mergeable(value: object) -> bool | None:
    """GraphQL's ``MERGEABLE`` / ``CONFLICTING`` / ``UNKNOWN`` as REST's tri-state bool.

    REST reports ``mergeable`` as ``true`` / ``false`` / ``null``; GraphQL reports the
    same fact as an enum with an explicit ``UNKNOWN``. ``UNKNOWN`` maps to ``None``,
    NOT to ``False`` — the two mean very different things, and the whole reason this
    normalization is a named function is that collapsing them is the easy mistake.
    Note that ``mergeable`` alone never means "ready to merge": it means "no merge
    CONFLICTS", which is why the readiness gate keys off ``mergeable_state``.
    """
    if not isinstance(value, str):
        return None
    normalized = value.strip().upper()
    if normalized == "MERGEABLE":
        return True
    if normalized == "CONFLICTING":
        return False
    return None


def _rest_pr_state(value: object) -> str | None:
    """GraphQL's ``OPEN`` / ``CLOSED`` / ``MERGED`` as REST's ``open`` / ``closed``.

    REST models a merged PR as ``state: "closed"`` carrying a ``merged_at`` timestamp;
    GraphQL models it as a third state. The row shape here is REST's, and every reader
    (``PrList.prStateVisual``, ``review.ts``, the closed-list split) compares against
    REST's two values — so ``MERGED`` has to become ``closed``, with the merge
    distinguished by the accompanying ``pr_merged_at``. Returning ``"merged"`` would
    match no branch in any of those readers and paint a merged PR as open.
    """
    normalized = _lower_or_none(value)
    if normalized is None:
        return None
    return "closed" if normalized in ("closed", "merged") else normalized


def _parse_summary_rows(stdout: str) -> dict[int, dict]:
    """Parse the shared per-PR summary JQ stream (see ``_PR_SUMMARY_JQ_BODY``)."""
    out: dict[int, dict] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        number = row.get("number")
        if not isinstance(number, int):
            continue
        rollup = row.get("rollup")
        out[number] = {
            "additions": row.get("additions") or 0,
            "deletions": row.get("deletions") or 0,
            "changed_files": row.get("changed_files") or 0,
            # Deliberately NOT coerced to "" — an absent oid means "we do not know
            # this row's head commit", and a blank string would read as a known
            # value that then fails the caller's sha validation with a confusing
            # error instead of simply leaving the row un-approvable in bulk.
            "head_sha": row.get("head_sha") or None,
            # GraphQL SHOUTS its enums (`CLEAN`, `MERGEABLE`, `OPEN`) where REST speaks
            # lowercase (`clean`, `open`). Normalized here, at the single point where
            # GraphQL output becomes an internal row, so no reader has to know which
            # transport its row came from — `routes._MERGE_ALLOWED_STATES` and the
            # frontend's `MERGE_READY_STATES` both compare lowercase, and an un-lowered
            # `CLEAN` would silently miss both and read as "not ready".
            #
            # `None` stays `None`: unknown mergeability must not collapse to a value.
            # GitHub computes it asynchronously and answers `UNKNOWN` on a cold read,
            # and treating that as "not ready" would be a guess presented as a fact.
            #
            # `mergeable_state` is filled by the SEPARATE readiness call
            # (`_PR_READINESS_SELECTION`) and is seeded None here so the key always
            # exists — absent would be falsy, i.e. indistinguishable from "not ready".
            "mergeable_state": None,
            "mergeable": _graphql_mergeable(row.get("mergeable_raw")),
            # GraphQL's MERGED/CLOSED/OPEN collapsed into REST's open|closed, because
            # the row shape is REST's and every reader compares against those two.
            # `MERGED` becomes `closed` and is distinguished by `pr_merged_at`, exactly
            # as REST does it (`PrList.prStateVisual` checks `merged_at` FIRST, so
            # reporting `merged` here would match no branch and paint a merged PR as
            # open — the bug this normalization exists to avoid).
            "pr_state": _rest_pr_state(row.get("pr_state")),
            "pr_merged_at": row.get("pr_merged_at") or None,
            # Same bucketing table as every other surface; an unrecognized rollup
            # value lands in "other" and so must not read as passing.
            "checks_state": _check_bucket(None, rollup) if rollup else None,
            "checks_counts": _count_context_buckets(row.get("contexts")),
            # More contexts than one page: the tally is incomplete, so the card
            # must show the aggregate rollup rather than a partial count that
            # could omit the only failing check.
            "checks_truncated": bool(row.get("contexts_truncated")),
        }
    return out


def summarize_checks(checks: list[dict]) -> dict:
    """``{checks_counts, checks_state}`` derived from an ALREADY-bucketed check list.

    Lets a fresh ``/pull`` fetch update the list card without re-running the
    GraphQL enrichment: the detail call has just read the authoritative checks, so
    the card's tally and dot are computed from exactly those rows. Priority for
    the single ``checks_state`` mirrors what the rollup means — anything failing
    dominates, then anything still running, then passing, then informational —
    so the dot never reads greener than the list it summarizes.
    """
    counts = {bucket: 0 for bucket in _CHECK_BUCKETS}
    for c in checks:
        if not isinstance(c, dict):
            continue
        bucket = c.get("bucket")
        counts[bucket if isinstance(bucket, str) and bucket in counts else "other"] += 1
    for bucket in ("failure", "running", "success", "other"):
        if counts[bucket]:
            state: str | None = bucket
            break
    else:
        state = None  # no checks at all -> the card shows no dot
    # Derived from the authoritative, fully-paginated detail read, so the tally is
    # complete by construction.
    return {"checks_counts": counts, "checks_state": state, "checks_truncated": False}


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
        n for n in (pr.get("number") for pr in pulls)
        if isinstance(n, int) and n not in summaries
    ]
    if missing:
        try:
            summaries.update(fetch_pr_summaries_by_number(owner, repo, missing))
        except GhCliError:
            pass
    return summaries


def _enrich_readiness(owner: str, repo: str, pulls: list[dict], state: str) -> dict[int, str | None]:
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
        n for n in (pr.get("number") for pr in pulls)
        if isinstance(n, int) and n not in readiness
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
    pulls: list[dict], summaries: dict[int, dict],
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
    ready = readiness or {}
    for pr in pulls:
        number = pr.get("number")
        # Readiness first, and OUTSIDE the summary branch — the two calls fail
        # independently, so a row with no summary can still have a known merge state.
        # Always assigned so the key exists even when unknown: absent is falsy, i.e.
        # indistinguishable from "not ready", which would put the row back into the
        # auto-merge batch the provider refuses.
        pr["mergeable_state"] = ready.get(number) if isinstance(number, int) else None
        extra = summaries.get(number) if isinstance(number, int) else None
        if not extra:
            pr["additions"] = None
            pr["deletions"] = None
            pr["changed_files"] = None
            pr["checks_state"] = None
            pr["checks_counts"] = None
            pr["checks_truncated"] = False
            # `mergeable_state` was already set above from the independent readiness
            # call — do NOT clear it here; that call may well have succeeded.
            pr["mergeable"] = None
            # NOT cleared: an un-enriched row keeps whatever head sha its source
            # gave it. The LIST path already carries one from `_PR_JQ`, and blanking
            # it here on a failed GraphQL call would take bulk approve away from
            # rows that never needed the enrichment for it.
            pr.setdefault("head_sha", None)
            continue
        pr["additions"] = extra.get("additions", 0)
        pr["deletions"] = extra.get("deletions", 0)
        pr["changed_files"] = extra.get("changed_files", 0)
        # Only fills a GAP. The list rows already have it from `_PR_JQ`; the SEARCH
        # rows do not (GitHub's search API does not expose the head commit), and
        # this is the one call that already walks the head commit anyway.
        if not pr.get("head_sha"):
            pr["head_sha"] = extra.get("head_sha")
        # `mergeable` is "no merge CONFLICTS" — NOT "ready to merge". A PR with
        # unsatisfied required reviews is `mergeable: true` with
        # `mergeable_state: "blocked"`, which is why the readiness gate keys off the
        # latter (set above, from its own call).
        pr["mergeable"] = extra.get("mergeable")
        # A row whose live state disagrees with its cached one is corrected HERE, which
        # is the closest read to the action. #1265 was armed for auto-merge from a row
        # cached while it was still open, and GitHub answered "already merged".
        live_state = extra.get("pr_state")
        if live_state:
            pr["state"] = live_state
            # Written TOGETHER with the state, never separately: the two are one fact in
            # REST's shape, and a `closed` with no `merged_at` is the red
            # closed-unmerged icon. Only ever fills a gap — a row that already carries a
            # timestamp keeps it.
            if extra.get("pr_merged_at") and not pr.get("merged_at"):
                pr["merged_at"] = extra.get("pr_merged_at")
        pr["checks_state"] = extra.get("checks_state")
        pr["checks_counts"] = extra.get("checks_counts") or {b: 0 for b in _CHECK_BUCKETS}
        pr["checks_truncated"] = bool(extra.get("checks_truncated"))
    return pulls


def enrichment_complete(pulls: list[dict]) -> bool:
    """Whether every row actually got its card enrichment.

    ``False`` means at least one row's diff size / check state is unknown (the
    GraphQL call failed), which the route uses to keep the incomplete rows OUT of
    the on-disk list cache so the next read retries instead of serving unknowns
    forever.
    """
    return all(pr.get("checks_counts") is not None for pr in pulls)


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

_PR_SEARCH_JQ = (
    ".items[] | {number: .number, title: .title, url: .html_url, "
    "state: .state, draft: (.draft // false), labels: [.labels[].name], "
    "author: (.user.login // null), "
    "author_association: (.author_association // null), "
    "updated_at: .updated_at, created_at: .created_at, "
    "closed_at: .closed_at, "
    "merged_at: (.pull_request.merged_at // null), "
    "assignees: [.assignees[].login], "
    "requested_reviewers: [], base: null, head: null, "
    # Present but NULL: GitHub's search API does not expose the head commit, so the
    # key exists for row-shape parity with `_PR_JQ` and is filled in by the
    # by-number enrichment (`_apply_summaries`), which already walks that commit.
    "head_sha: null, "
    "body: (.body // \"\")}"
)

# Bound the search too — a person filter should never stream thousands of rows.
# Public because the route reports it to the client: the UI has to be able to say
# "newest 300" rather than implying completeness.
PR_SEARCH_MAX = 300

# Hard stop on pages walked, so a pathological `per_page`/`limit` combination can
# never turn one filter toggle into an unbounded request loop.
_SEARCH_MAX_PAGES = 10

# GitHub logins: alphanumerics and hyphens only. Validated before a login can
# reach the search query string, so it cannot inject extra qualifiers.
_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")

# PR lifecycle -> search qualifiers. ``closed`` means closed WITHOUT being
# merged, matching the frontend's three-way split (open / merged / closed).
_PR_STATE_QUALIFIERS = {
    "open": ["is:open"],
    "merged": ["is:merged"],
    "closed": ["is:closed", "is:unmerged"],
}


def build_pr_search_query(
    owner: str, repo: str, *, state: str = "open",
    author: str | None = None, assignee: str | None = None,
    review_requested: str | None = None,
) -> str:
    """Assemble the search ``q`` for a per-person PR query.

    Scoped to one repo and to pull requests, plus the lifecycle qualifiers for
    ``state`` and one qualifier per supplied login. Every login is charset-
    validated (:data:`_LOGIN_RE`) BEFORE it lands in the query, so a hostile
    value cannot smuggle in extra qualifiers. Raises :class:`PrSearchError` on an
    unknown state, an invalid login, or when no person qualifier was given (an
    unfiltered search would just duplicate the list endpoint).
    """
    if state not in _PR_STATE_QUALIFIERS:
        raise PrSearchError(f"unsupported state for PR search: {state!r}")
    parts = [f"repo:{owner}/{repo}", "is:pr", *_PR_STATE_QUALIFIERS[state]]
    people = [
        ("author", author),
        ("assignee", assignee),
        ("review-requested", review_requested),
    ]
    added = 0
    for qualifier, login in people:
        if not login:
            continue
        if not _LOGIN_RE.match(login):
            raise PrSearchError(f"invalid GitHub login: {login!r}")
        parts.append(f"{qualifier}:{login}")
        added += 1
    if added == 0:
        raise PrSearchError("PR search needs at least one person qualifier")
    return " ".join(parts)


def search_pulls(
    owner: str, repo: str, *, state: str = "open",
    author: str | None = None, assignee: str | None = None,
    review_requested: str | None = None,
    timeout: float = GH_PAGINATE_TIMEOUT_SEC, limit: int = PR_SEARCH_MAX,
) -> list[dict]:
    """Search a repo's PRs by person, server-side (see the module note above).

    Returns rows in the SAME shape as ``list_open_pulls`` so the frontend can
    swap data sources without a second row type — with ``base``/``head`` null and
    ``requested_reviewers`` empty (not exposed by the search API). Paginated and
    then capped at ``limit``.
    """
    q = build_pr_search_query(
        owner, repo, state=state, author=author, assignee=assignee,
        review_requested=review_requested,
    )
    cap = max(1, int(limit))
    # Paginated EXPLICITLY, one page at a time, stopping as soon as the cap is
    # met: `gh --paginate` would walk every page GitHub offers (up to the search
    # maximum) before we sliced it down, so a prolific author's filter could burn
    # a dozen extra requests — and hit the timeout — for rows nobody asked for.
    per_page = min(100, cap)
    rows: list[dict] = []
    page = 1
    while len(rows) < cap and page <= _SEARCH_MAX_PAGES:
        path = (
            f"search/issues?q={quote(q, safe='')}&sort=updated&order=desc"
            f"&per_page={per_page}&page={page}"
        )
        batch = _run_gh_api(path, _PR_SEARCH_JQ, timeout=timeout, paginate=False)
        rows.extend(batch)
        if len(batch) < per_page:
            break  # last page
        page += 1
    return rows[:cap]


# ── pull-request actions (the write surface the PR pane's buttons drive) ──────
#
# Everything below MUTATES a pull request or its CI. They are the actions a
# maintainer performs from GitHub's own PR page — close/reopen, approve, comment,
# enable auto-merge, cancel a workflow run — done in-app so a triage pass does not
# require a browser round-trip.
#
# Three properties are load-bearing for all of them:
#
#   * They go through :func:`_run_gh_write`, so a 403/401 becomes
#     :class:`GhPermissionError` and the route answers 403 rather than a generic
#     502, and every body rides on stdin as JSON (never argv).
#   * Every one is addressed by the PR/run NUMBER coerced with ``int()``, so no
#     caller-supplied text reaches a path segment.
#   * Merging is offered in TWO forms, and neither can bypass a gate. A direct
#     :func:`merge_pull_request` is a request GitHub itself adjudicates — branch
#     protection, required reviews and required checks are enforced SERVER-side and
#     an unsatisfied PR comes back 405, so the button cannot land unreviewed code.
#     :func:`enable_auto_merge` covers the other case: the PR is not mergeable YET
#     and should land by itself once its checks pass. Shipping only the second left
#     a repo with no branch rule (auto-merge is unavailable there) with no merge
#     path at all — see the note on :func:`merge_pull_request`.


def set_pr_state(
    owner: str, repo: str, number: int, state: str, *, timeout: float = GH_TIMEOUT_SEC
) -> dict:
    """Close or reopen a PULL REQUEST (``PATCH .../pulls/{n}``).

    Deliberately NOT :func:`set_issue_state`, even though GitHub's issues
    endpoint accepts a PR number: closing through ``issues/{n}`` works but
    reopening does not report the PR-native fields the pane reads back
    (``draft``/``merged``), and a MERGED PR must not be reopenable at all —
    GitHub rejects that, and routing through the PR endpoint is what surfaces the
    rejection instead of silently succeeding against the issue shadow.

    Returns ``{state, merged, draft}`` from the updated PR.
    """
    if state not in ("open", "closed"):
        raise GhCliError(f"invalid PR state: {state!r}")
    data = _run_gh_write(
        "PATCH", f"repos/{owner}/{repo}/pulls/{int(number)}", {"state": state}, timeout=timeout
    )
    if isinstance(data, dict):
        return {
            "state": data.get("state", state),
            "merged": bool(data.get("merged", False)),
            "draft": bool(data.get("draft", False)),
        }
    return {"state": state, "merged": False, "draft": False}


# GitHub's review verbs. ``APPROVE`` and ``REQUEST_CHANGES`` are the two that
# carry a verdict; ``COMMENT`` leaves review prose with no verdict. Anything else
# is refused rather than passed through, so a typo cannot become an unintended
# approval.
PR_REVIEW_EVENTS = ("APPROVE", "REQUEST_CHANGES", "COMMENT")


def submit_pr_review(
    owner: str, repo: str, number: int, event: str, body: str = "", head_sha: str = "",
    *, timeout: float = GH_TIMEOUT_SEC,
) -> dict:
    """Submit a REVIEW on a PR (``POST .../pulls/{n}/reviews``).

    ``event`` is one of :data:`PR_REVIEW_EVENTS`. ``body`` is optional for
    ``APPROVE`` but REQUIRED by GitHub for ``REQUEST_CHANGES`` and ``COMMENT``,
    which is validated here so the failure is a clear 400 rather than a 422 from
    the API.

    ``head_sha`` is REQUIRED and rides as GitHub's ``commit_id``. Without it the
    review attaches to whatever the head is at the moment the request lands, so a
    force-push between the render and the click records an APPROVAL of code the
    reviewer never saw.

    **``commit_id`` is ATTRIBUTION, not a rejecting precondition** — unlike the
    ``sha`` parameter on :func:`merge_pull_request`, which GitHub really does check
    and 409s. GitHub accepts a review naming a commit that is no longer the head; it
    just records the review against that commit, and whether the stale approval still
    counts toward branch protection depends on the repo's
    "dismiss stale pull request approvals" setting. So the pin makes the verdict
    HONEST (it says which revision was reviewed, and a repo with dismissal on
    discards it) but it cannot by itself make a stale approval fail. The refusal is
    the ROUTE's job: ``routes._handle_pull_review`` re-reads the PR's live head and
    answers 409 before calling this, exactly as ``_handle_pull_merge`` does. Do not
    move that check in here — it needs a provider read the caller has already paid
    for, and both routes share the taxonomy.

    Note that GitHub refuses to let an author approve their OWN PR (422). That is
    not pre-checked here: the caller would have to fetch the PR's author to know,
    and the API's refusal is authoritative and already surfaces as an error.

    Returns ``{id, state, submitted_at}`` of the created review.
    """
    verb = (event or "").strip().upper()
    if verb not in PR_REVIEW_EVENTS:
        raise GhCliError(f"invalid review event: {event!r}")
    text = (body or "").strip()
    if verb in ("REQUEST_CHANGES", "COMMENT") and not text:
        raise GhCliError(f"a {verb} review requires a comment body")
    sha = (head_sha or "").strip()
    if not re.match(r"^[0-9a-fA-F]{7,64}$", sha):
        raise GhCliError(
            "refusing to review without the head commit it was read at "
            f"(got {head_sha!r})"
        )
    payload: dict[str, object] = {"event": verb, "commit_id": sha}
    if text:
        payload["body"] = text
    data = _run_gh_write(
        "POST", f"repos/{owner}/{repo}/pulls/{int(number)}/reviews", payload, timeout=timeout
    )
    if isinstance(data, dict):
        return {
            "id": data.get("id"),
            "state": data.get("state"),
            "submitted_at": data.get("submitted_at"),
        }
    return {"id": None, "state": verb, "submitted_at": None}


def add_issue_comment(
    owner: str, repo: str, number: int, body: str, *, timeout: float = GH_TIMEOUT_SEC
) -> dict:
    """Post a plain comment on a PR or issue (``POST .../issues/{n}/comments``).

    The issues endpoint is correct for both: a PR's conversation comments ARE
    issue comments on GitHub (only inline review comments live elsewhere), which
    is the same reason the timeline reader uses ``issues/{n}/timeline``.

    Returns ``{id, url, created_at}``.
    """
    text = (body or "").strip()
    if not text:
        raise GhCliError("a comment needs a body")
    data = _run_gh_write(
        "POST", f"repos/{owner}/{repo}/issues/{int(number)}/comments",
        {"body": text}, timeout=timeout,
    )
    if isinstance(data, dict):
        return {
            "id": data.get("id"),
            "url": data.get("html_url"),
            "created_at": data.get("created_at"),
        }
    return {"id": None, "url": None, "created_at": None}


def add_pr_comment(
    owner: str, repo: str, number: int, body: str, *, timeout: float = GH_TIMEOUT_SEC
) -> dict:
    """Post a conversation comment on a PULL REQUEST.

    On GitHub this is literally :func:`add_issue_comment` — a PR's conversation
    comments ARE issue comments, drawn from one number sequence per repo. It exists
    as its own name because GitLab's issues and merge requests are separate
    collections with INDEPENDENT numbering, so a single shared entry point would be
    a silent way to comment on the wrong item there. Routes call the PR-specific
    function for a PR on both providers; here the two coincide.
    """
    return add_issue_comment(owner, repo, number, body, timeout=timeout)


# GitHub's merge methods, as accepted by the auto-merge mutation.
PR_MERGE_METHODS = ("MERGE", "SQUASH", "REBASE")


def _pr_node_id(owner: str, repo: str, number: int, *, timeout: float) -> str:
    """The GraphQL node id for a PR — required by the auto-merge mutations, which
    have no REST equivalent."""
    data = _run_gh_write("GET", f"repos/{owner}/{repo}/pulls/{int(number)}", None, timeout=timeout)
    node_id = data.get("node_id") if isinstance(data, dict) else None
    if not isinstance(node_id, str) or not node_id:
        raise GhCliError(f"could not resolve a node id for {owner}/{repo}#{int(number)}")
    return node_id


def _run_gh_graphql_mutation(
    mutation: str, variables: dict[str, str], *, timeout: float
) -> dict:
    """Run a GraphQL mutation via ``gh api graphql`` and return ``.data``.

    Variables are passed with ``-F`` (never interpolated into the query text), and
    a permission failure is mapped to :class:`GhPermissionError` exactly as
    :func:`_run_gh_write` does — GraphQL reports authorization in the errors array
    with a 200 status, so the string check is on stdout as well as stderr.
    """
    argv = ["gh", "api", "graphql", "-f", f"query={mutation}"]
    for name, value in variables.items():
        argv += ["-F", f"{name}={value}"]
    proc = _gh_run(argv, timeout=timeout)
    combined = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    if proc.returncode != 0 or '"errors"' in (proc.stdout or ""):
        tail = sanitize_cli_stderr(" ".join(combined.strip().splitlines()[-3:]))
        lowered = combined.lower()
        if (
            "HTTP 403" in combined
            or "HTTP 401" in combined
            or "not authorized" in lowered
            or "must have push access" in lowered
            or "resource not accessible" in lowered
        ):
            raise GhPermissionError(
                f"GitHub refused the request — your `gh` session lacks the "
                f"required access: {tail}"
            )
        raise GhCliError(f"gh api graphql failed (exit {proc.returncode}): {tail}")
    try:
        parsed = json.loads((proc.stdout or "").strip() or "{}")
    except json.JSONDecodeError as exc:
        raise GhCliError("gh returned unexpected output for a GraphQL mutation") from exc
    data = parsed.get("data") if isinstance(parsed, dict) else None
    return data if isinstance(data, dict) else {}


_ENABLE_AUTO_MERGE = (
    "mutation($pr:ID!,$method:PullRequestMergeMethod!){"
    " enablePullRequestAutoMerge(input:{pullRequestId:$pr, mergeMethod:$method}){"
    "  pullRequest{ number autoMergeRequest{ enabledAt mergeMethod } } } }"
)

_DISABLE_AUTO_MERGE = (
    "mutation($pr:ID!){"
    " disablePullRequestAutoMerge(input:{pullRequestId:$pr}){"
    "  pullRequest{ number autoMergeRequest{ enabledAt } } } }"
)


def merge_pull_request(
    owner: str, repo: str, number: int, method: str = "SQUASH", head_sha: str = "",
    *, timeout: float = GH_TIMEOUT_SEC,
) -> dict:
    """Merge a pull request now (``PUT .../pulls/{n}/merge``).

    **This cannot bypass a gate, and that is why it is safe to offer.** Branch
    protection — required reviews, required status checks, required conversation
    resolution — is enforced by GitHub on this endpoint, not by the caller: a PR
    that has not satisfied its rules comes back **405 Method Not Allowed** and
    nothing is merged. A 409 means the head moved since the caller last read it.
    Both surface as errors rather than being reported as a merge.

    So the honest division of labour is:

    * this function — the PR is mergeable *now* and the operator says land it;
    * :func:`enable_auto_merge` — the PR is *not* mergeable yet and should land by
      itself once its checks pass.

    Shipping only the second is what an earlier revision did, on the theory that a
    direct merge could bypass review. It cannot; GitHub adjudicates. What that
    omission actually did was leave a repository with no branch rule — where
    auto-merge is simply unavailable — with **no merge path at all**, which is a
    worse outcome than the one it was guarding against.

    ``method`` is one of :data:`PR_MERGE_METHODS`; a repo that disallows the chosen
    method answers 405 too, so the error is the repo's own policy speaking.

    ``head_sha`` is REQUIRED and is sent as GitHub's ``sha`` precondition, so the
    merge is pinned to the commit the caller actually looked at. Without it, a push
    landing between the read and the click merges code nobody reviewed — and on a repo
    with no branch protection there is nothing else to catch that, which is exactly
    the case this function exists to serve. A moved head answers 409 rather than
    merging. It is a positional parameter with an empty default only so the two
    clients keep identical signatures; an empty value is refused here, not defaulted.

    Returns ``{merged, sha, message}``.
    """
    verb = (method or "").strip().upper()
    if verb not in PR_MERGE_METHODS:
        raise GhCliError(f"invalid merge method: {method!r}")
    sha = (head_sha or "").strip()
    if not re.match(r"^[0-9a-fA-F]{7,64}$", sha):
        raise GhCliError(
            "refusing to merge without the head commit it was reviewed at "
            f"(got {head_sha!r})"
        )
    data = _run_gh_write(
        "PUT", f"repos/{owner}/{repo}/pulls/{int(number)}/merge",
        {"merge_method": verb.lower(), "sha": sha}, timeout=timeout,
    )
    if isinstance(data, dict):
        return {
            "merged": bool(data.get("merged", True)),
            "sha": data.get("sha"),
            "message": data.get("message") or "",
        }
    return {"merged": True, "sha": None, "message": ""}


def enable_auto_merge(
    owner: str, repo: str, number: int, method: str = "SQUASH",
    *, timeout: float = GH_TIMEOUT_SEC,
) -> dict:
    """Arm GitHub's OWN auto-merge on a PR (GraphQL ``enablePullRequestAutoMerge``).

    For the PR that is not mergeable YET: GitHub merges it once the repository's
    required reviews and status checks pass. The complement of
    :func:`merge_pull_request`, which is for the PR that is mergeable now.

    Requires the repo to have 'Allow auto-merge' enabled and a branch rule, and
    GitHub also refuses to arm a PR that is already clean (there is nothing to wait
    for). Either way it answers with an error, which surfaces to the caller rather
    than being swallowed into a false success: GitHub's own text names the reason, and
    the route relays it verbatim rather than substituting a guess at which of the two
    causes applied.

    Returns ``{auto_merge: bool, method, enabled_at}``.
    """
    verb = (method or "").strip().upper()
    if verb not in PR_MERGE_METHODS:
        raise GhCliError(f"invalid merge method: {method!r}")
    node_id = _pr_node_id(owner, repo, number, timeout=timeout)
    data = _run_gh_graphql_mutation(
        _ENABLE_AUTO_MERGE, {"pr": node_id, "method": verb}, timeout=timeout
    )
    request = (
        (data.get("enablePullRequestAutoMerge") or {}).get("pullRequest") or {}
    ).get("autoMergeRequest") or {}
    # Derived from what came BACK, not asserted. A hardcoded True made the response a
    # claim rather than an observation: the only thing between a failed mutation and a
    # reported success was the errors-array check, and the equivalent shortcut on the
    # GitLab path is what let an immediate merge report itself as "armed".
    return {
        "auto_merge": bool(request),
        "method": request.get("mergeMethod") or (verb if request else None),
        "enabled_at": request.get("enabledAt"),
    }


def disable_auto_merge(
    owner: str, repo: str, number: int, *, timeout: float = GH_TIMEOUT_SEC
) -> dict:
    """Disarm GitHub's auto-merge on a PR (``disablePullRequestAutoMerge``).

    The inverse of :func:`enable_auto_merge`, so an accidental arm is reversible
    from the same place it was set. Returns ``{auto_merge: False}``."""
    node_id = _pr_node_id(owner, repo, number, timeout=timeout)
    _run_gh_graphql_mutation(_DISABLE_AUTO_MERGE, {"pr": node_id}, timeout=timeout)
    return {"auto_merge": False, "method": None, "enabled_at": None}


# One workflow run as the actions surface needs it: enough to name it, say
# whether it is still cancellable, and link out.
_WORKFLOW_RUN_JQ = (
    ".workflow_runs[] | {id: .id, name: (.name // .display_title // \"workflow\"), "
    "status: .status, conclusion: .conclusion, url: .html_url, "
    "event: (.event // null), created_at: .created_at}"
)

# A run in one of these states has not finished, so cancelling it is meaningful.
# Anything else (completed) can only be RE-RUN, never cancelled.
_RUN_CANCELLABLE_STATES = frozenset({
    "queued", "in_progress", "waiting", "requested", "pending",
})


def list_pr_workflow_runs(
    owner: str, repo: str, sha: str, *, timeout: float = GH_TIMEOUT_SEC
) -> list[dict]:
    """The GitHub Actions runs for a PR's head commit.

    Separate from :func:`list_pr_checks` because a CHECK is not a RUN: the checks
    surface reports per-job results (and merges in commit statuses from services
    that have no runs at all), while cancelling or re-running is an operation on
    the parent workflow RUN and needs its id. Asking for the runs by head sha is
    what ties them to the PR without a second PR round-trip.

    ``sha`` is charset-validated before it reaches the path, as in
    :func:`list_pr_checks`. Each row carries ``cancellable``/``rerunnable`` so the
    UI never offers an action GitHub will refuse.
    """
    if not re.match(r"^[0-9a-fA-F]{7,64}$", sha or ""):
        raise GhCliError(f"invalid commit sha: {sha!r}")
    rows = _run_gh_api(
        f"repos/{owner}/{repo}/actions/runs?head_sha={sha}&per_page=100",
        _WORKFLOW_RUN_JQ, timeout=timeout, paginate=False,
    )
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        status = str(row.get("status") or "")
        out.append({
            **row,
            "cancellable": status in _RUN_CANCELLABLE_STATES,
            # A finished run can be re-run; an in-flight one cannot.
            "rerunnable": status == "completed",
        })
    return out


def cancel_workflow_run(
    owner: str, repo: str, run_id: int, *, timeout: float = GH_TIMEOUT_SEC
) -> dict:
    """Cancel one in-flight Actions run (``POST .../actions/runs/{id}/cancel``).

    GitHub answers 202 with an empty body, so success is the ABSENCE of an error
    rather than anything in the response. A run that already finished answers 409;
    that is reported as an error rather than a no-op success, because "cancel"
    silently doing nothing is indistinguishable from it having worked.

    Returns ``{run_id, cancelled: True}``.
    """
    _run_gh_write(
        "POST", f"repos/{owner}/{repo}/actions/runs/{int(run_id)}/cancel", None, timeout=timeout
    )
    return {"run_id": int(run_id), "cancelled": True}


def rerun_workflow_run(
    owner: str, repo: str, run_id: int, *, failed_only: bool = False,
    timeout: float = GH_TIMEOUT_SEC,
) -> dict:
    """Re-run a completed Actions run, or only its failed jobs.

    ``failed_only`` selects ``/rerun-failed-jobs``, which is the cheaper and more
    common intent after a flake. Returns ``{run_id, rerun: True, failed_only}``.
    """
    verb = "rerun-failed-jobs" if failed_only else "rerun"
    _run_gh_write(
        "POST", f"repos/{owner}/{repo}/actions/runs/{int(run_id)}/{verb}", None, timeout=timeout
    )
    return {"run_id": int(run_id), "rerun": True, "failed_only": bool(failed_only)}
