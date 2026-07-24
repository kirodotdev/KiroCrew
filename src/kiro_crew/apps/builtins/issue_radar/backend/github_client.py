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
from urllib.parse import quote, urlparse

GH_TIMEOUT_SEC = 20.0
# Open issues are loaded in FULL via --paginate, which can span many pages on a
# busy repo (kirodotdev/Kiro ~2.6k open → ~26 pages), so it gets a much larger
# budget than the single-shot calls. The result is cached, so this cost is paid
# once per refresh, not per view.
GH_PAGINATE_TIMEOUT_SEC = 120.0

_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class RepoUrlError(ValueError):
    """Raised when a repo URL is not a well-formed https://github.com/<owner>/<repo> link."""


class GhCliError(RuntimeError):
    """Raised when ``gh`` is missing, unauthenticated, times out, or the API call fails."""


class GhPermissionError(GhCliError):
    """Raised when ``gh`` returns HTTP 403 because the caller lacks the required
    permission — either a read endpoint out of reach (notably listing
    collaborators without push access) or a write call (label edit / close /
    reopen) rejected for want of the triage/push right.

    A subclass of :class:`GhCliError` so existing ``except GhCliError`` handlers
    still catch it, but distinguishable so callers can degrade gracefully: the
    members path falls back to the issue-derived set, and the write routes
    special-case it into an HTTP 403 rather than a generic 502."""


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

# Trusted directories for gh resolution — only system-owned, non-user-writable
# locations. On macOS, Homebrew on Apple Silicon installs to /opt/homebrew/bin.
_TRUSTED_BIN_DIRS = ("/usr/local/bin", "/usr/bin", "/bin", "/opt/homebrew/bin")


def _gh_bin() -> str:
    """Absolute path to a trusted ``gh``, resolved once and cached.

    Resolves from known system directories and validates the candidate (and
    every parent) is canonical, root-owned, and non-writable by the gateway
    user — preventing a planted shim from receiving forwarded GitHub credentials.
    Uses the platform's ``_validate_provider_executable`` check (the same gate
    that protects git/glab provider paths). Set ``KIROCREW_ISSUE_RADAR_GH`` to
    an absolute path to override (must still pass validation).
    Raises :class:`GhCliError` if no valid executable is found."""
    global _gh_bin_cache
    if _gh_bin_cache:
        return _gh_bin_cache
    if sys.platform == "win32":
        raise GhCliError(
            "Issue Radar requires a POSIX platform (macOS/Linux); "
            "Windows is not supported — use WSL to run the KiroCrew gateway"
        )

    from kiro_crew.dashboard.handlers.source_providers import _validate_provider_executable

    # Operator override — still validated.
    override = os.environ.get("KIROCREW_ISSUE_RADAR_GH")
    if override:
        try:
            validated = _validate_provider_executable(override)
            _gh_bin_cache = validated
            return validated
        except (ValueError, OSError) as exc:
            raise GhCliError(
                f"KIROCREW_ISSUE_RADAR_GH={override!r} failed validation: {exc}"
            ) from exc

    # Resolve from trusted system dirs — validate each candidate.
    for d in _TRUSTED_BIN_DIRS:
        cand = os.path.join(d, "gh")
        if not os.path.isfile(cand):
            continue
        try:
            validated = _validate_provider_executable(cand)
            _gh_bin_cache = validated
            return validated
        except (ValueError, OSError):
            continue  # not root-owned or user-writable — skip

    raise GhCliError(
        "the `gh` CLI was not found in any trusted system directory "
        f"({', '.join(_TRUSTED_BIN_DIRS)}), or all candidates failed ownership "
        "validation; set KIROCREW_ISSUE_RADAR_GH to a root-owned gh executable"
    )


def _gh_env() -> dict[str, str]:
    """A minimal environment for ``gh``: the platform's safe-key base
    (PATH/HOME/XDG/…) plus gh's own auth + network/TLS vars when set — NOT the
    gateway's full environment, so unrelated secrets never reach the child."""
    from kiro_crew.apps.registry import minimal_env

    return minimal_env(**{k: os.environ[k] for k in _GH_ENV_PASSTHROUGH if k in os.environ})


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
        raise GhCliError("the `gh` CLI is not installed on this host") from exc
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
        tail = " ".join((proc.stderr or "").strip().splitlines()[-3:])
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
        tail = " ".join((proc.stderr or "").strip().splitlines()[-3:])
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


def list_closed_issues(owner: str, repo: str, *, timeout: float = GH_TIMEOUT_SEC) -> list[dict]:
    """The 100 most-recently-updated CLOSED issues (bounded — see ``_list_issues``)."""
    return _list_issues(owner, repo, "closed", timeout=timeout, paginate=False)


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
        tail = " ".join((proc.stderr or "").strip().splitlines()[-3:])
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
        tail = " ".join((proc.stderr or "").strip().splitlines()[-3:])
        raise GhCliError(f"could not read {owner}/{repo}#{int(number)} (exit {proc.returncode}): {tail}")

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
        tail = " ".join(stderr.strip().splitlines()[-3:])
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
