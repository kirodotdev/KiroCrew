"""GitLab data access for Issue Radar, via the user's own ``glab`` CLI session.

The counterpart to :mod:`github_client`, and deliberately shaped as a
FUNCTION-FOR-FUNCTION mirror of it: same public names, same argument order, same
return shapes. Issue Radar's routes, caches, and React components were all
written against GitHub's field names, so this module normalizes GitLab payloads
INTO those names rather than introducing a second vocabulary. That choice keeps
the whole app -- 38 route handlers, ~20 cache files, ~30 components -- provider
agnostic without a parallel set of types:

    GitLab                     ->  normalized (GitHub-shaped)
    iid                            number
    web_url                        url
    state "opened"                 state "open"
    user_notes_count               comments
    upvotes                        thumbs_up
    author.username                author
    labels: ["a"]                  labels: ["a"]
    color "#d9534f"                color "d9534f"      (no leading '#')
    source_branch/target_branch     head/base
    draft                          draft
    pipeline job                    check run

Auth and credential handling follow the same model as ``github_client``: no
GitLab App, no PAT stored by KiroCrew, no hosted backend. Every call shells out
to ``glab``, which owns its own token storage, and this module only (a) parses a
project URL safely and (b) runs ``glab api`` with a list argv (never
``shell=True``) so there is no shell-injection surface.

Self-managed GitLab adds one dimension GitHub does not have -- the HOST -- and it
is the security-sensitive one. Three rules, all mirrored from
``source_providers._run_json`` (the Sidebar PR panel), which already solved this:

1. A host is only reachable if it is ``gitlab.com`` or appears verbatim in the
   operator's ``dashboard.gitlab_hosts`` allowlist. Browser input therefore can
   never choose which instance the credential-bearing CLI talks to (SSRF).
2. The host is REQUIRED on every call, never defaulted. A call site that forgot
   it would otherwise silently target gitlab.com, so an allowlisted self-managed
   project could be read -- or MUTATED -- on the public instance at the same
   project path. Failing loudly makes that class of bug impossible to introduce.
3. ``GITLAB_TOKEN`` is dropped from the child environment whenever the target is
   not gitlab.com. It is a single ambient credential with no host binding, so
   forwarding it while ``GITLAB_HOST`` points at a self-managed server would send
   a gitlab.com PAT -- and every permission it carries -- to that server.
"""

from __future__ import annotations

import json  # noqa: F401 -- historical module export
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlparse  # noqa: F401 -- historical module export

from kiro_crew.apps.registry import minimal_env
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.sel import sel

from . import gitlab_normalization as _normalization
from . import gitlab_transport as _transport
from .errors import (
    ProviderCliError,
    ProviderInvalidInputError,
    ProviderPermissionError,
    ProviderSetupError,
    PrSearchError,
)
from .errors import RepoUrlError as RepoUrlError  # noqa: F401 — public re-export
from .errors import (
    sanitize_cli_stderr,
)

# Historical aliases, mirroring github_client so provider-agnostic callers can
# use either module interchangeably (see errors.py for why these are aliases).
GhCliError = ProviderCliError
GhSetupError = ProviderSetupError
GhPermissionError = ProviderPermissionError

GL_TIMEOUT_SEC = 20.0
# Open issues are loaded in FULL across pages, which can span many requests on a
# busy project, so it gets a much larger budget than the single-shot calls. The
# result is cached, so this cost is paid once per refresh, not per view.
GL_PAGINATE_TIMEOUT_SEC = 120.0

# Mirrors github_client's constants so the provider-agnostic routes can read
# either module's limits without branching.
CONTRIB_WINDOW_DAYS = 30
MAX_WINDOW_DAYS = 3650
PR_SEARCH_MAX = 300

# Per-page ceiling GitLab enforces on its REST API.
_PAGE_SIZE = 100
# Hard cap on pages walked by a paginated read, so a pathological project cannot
# make one request loop indefinitely (github_client relies on `gh --paginate`
# plus a timeout for the same protection; glab has no equivalent flag for
# arbitrary API paths, so pagination is explicit here).
_MAX_PAGES = 40

_SEGMENT_RE = _transport.SEGMENT_RE
# A GitLab username is used in search filters that ride in a query string.
_USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
_GITLAB_RESERVED_SEGMENTS = _transport.GITLAB_RESERVED_SEGMENTS


def parse_gitlab_repo_url(link: str, *, allowed_hosts: frozenset[str] = frozenset()) -> tuple[str, str, str]:
    """Parse a project URL through the provider-specific transport boundary."""
    return _transport.parse_gitlab_repo_url(link, allowed_hosts=allowed_hosts)


def project_path(owner: str, repo: str) -> str:
    """URL-encode ``owner/repo`` into GitLab's single ``:id`` path parameter."""
    return _transport.project_path(owner, repo)


# ── glab spawn hardening ─────────────────────────────────────────────────────
#
# Mirrors github_client's model exactly. glab needs the host's OWN authenticated
# session and cannot be sandbox-routed (the sandbox would hide ~/.config/glab and
# the keychain, breaking auth). As defense-in-depth, every spawn goes through
# ``_glab_run``, which (1) resolves ``glab`` through the shared provider policy --
# accepting the user's own install but refusing one owned by another user, a
# world-writable one, or one inside the agent-writable project tree -- and (2)
# hands the child a MINIMAL environment
# -- PATH/HOME/XDG plus glab's own auth/network vars -- instead of the gateway's
# full env, so unrelated secrets (AWS/Slack/SSH) can never leak to a substituted
# or compromised glab.
_GLAB_ENV_PASSTHROUGH = _transport.GLAB_ENV_PASSTHROUGH

_glab_bin_cache: str | None = None


def _glab_bin() -> str:
    """Absolute path to an acceptable ``glab``, resolved once and cached.

    Resolution and validation are shared with the Sidebar PR panel
    (``source_providers.provider_executable_candidates`` +
    ``_validate_provider_executable``), exactly as ``github_client._gh_bin`` does
    for ``gh``: the well-known install dirs first, then the ambient ``PATH``,
    accepting the user's own install (Homebrew, asdf, mise, ``~/.local/bin``)
    while refusing a binary owned by another user, a world-writable one, or one
    inside the agent-writable project tree.

    Sharing the policy is the point, not an implementation detail: a GitLab user
    whose ``glab`` the PR panel accepts must not be told by Issue Radar that the
    same binary is untrusted. Set ``KIROCREW_ISSUE_RADAR_GLAB`` to an absolute
    path to override (still validated), or ``KIROCREW_PROVIDER_BIN_STRICT=1`` to
    require a system-dir ``glab``.
    """
    global _glab_bin_cache
    if _glab_bin_cache:
        return _glab_bin_cache

    from kiro_crew.dashboard.handlers.source_providers import (
        _validate_provider_executable,
        provider_executable_candidates,
    )

    override = os.environ.get("KIROCREW_ISSUE_RADAR_GLAB")
    if override:
        try:
            validated = _validate_provider_executable(override)
            _glab_bin_cache = validated
            return validated
        except (ValueError, OSError) as exc:
            raise ProviderSetupError(
                f"KIROCREW_ISSUE_RADAR_GLAB={override!r} failed validation: {exc}",
                reason="not_installed",
            ) from exc

    # Well-known install dirs first, then the ambient PATH.
    last_error = ""
    for cand in provider_executable_candidates("glab"):
        if not os.path.isfile(cand):
            continue
        try:
            validated = _validate_provider_executable(cand)
            _glab_bin_cache = validated
            return validated
        except (ValueError, OSError) as exc:
            last_error = str(exc)
            continue  # untrusted provenance — skip

    detail = f" (last check: {last_error})" if last_error else ""
    raise ProviderSetupError(
        "the `glab` CLI was not found on this host"
        f"{detail} — install it (`brew install glab` or your distro's package "
        "manager) and run `glab auth login`, or set KIROCREW_ISSUE_RADAR_GLAB to "
        "an absolute glab path",
        reason="not_installed",
    )


def allowed_hosts() -> frozenset[str]:
    """The operator's ``dashboard.gitlab_hosts`` allowlist.

    Read synchronously (this module is sync throughout, and routes call it via
    ``asyncio.to_thread``). Failure to read config yields an EMPTY set, so a
    broken config denies every self-managed host rather than widening access.
    """
    try:
        return frozenset(KiroCrewConfig.load().dashboard.gitlab_hosts)
    except Exception:  # noqa: BLE001 — fail closed, never widen
        return frozenset()


def _resolve_host(host: str) -> str:
    """Re-check ``host`` against the allowlist at the spawn boundary.

    ``parse_gitlab_repo_url`` already validated the host when the project was
    connected, but this is re-checked here so a caller cannot reach an
    unauthorized instance even if a future code path forgets to validate, and so
    an operator REMOVING a host from the allowlist takes effect immediately on
    an already-connected project. An omitted host is refused rather than
    silently resolved to gitlab.com.
    """
    return _transport.resolve_host(host, allowed_hosts=allowed_hosts())


def _glab_env(host: str) -> dict[str, str]:
    """A minimal environment for ``glab``: the platform's safe-key base
    (PATH/HOME/XDG/…) plus glab's own auth + network/TLS vars when set -- NOT the
    gateway's full environment, so unrelated secrets never reach the child.

    ``GITLAB_HOST`` is pinned to the resolved host so a self-managed default in
    glab's own config cannot redirect the bare API paths to a different instance.
    ``GITLAB_TOKEN`` is withheld for non-gitlab.com hosts (see the module
    docstring, rule 3).
    """
    return _transport.glab_env(
        host,
        source_env=os.environ,
        passthrough_keys=_GLAB_ENV_PASSTHROUGH,
        minimal_env=minimal_env,
    )


def _audit(op: str, target: str, outcome: str, *, error: str = "") -> None:
    """SEL event for every glab spawn (reads and writes). Fire-and-forget."""
    sel().log_api_access(
        caller="core:issue-radar",
        operation=f"issue_radar.{op}",
        outcome=outcome,
        source="builtin-app",
        resources=target[:200],
        error=error[:200] if error else "",
    )


def _glab_run(
    argv: list[str], *, host: str, timeout: float, input_text: str | None = None
) -> subprocess.CompletedProcess:
    """Single spawn chokepoint for every ``glab`` call -- replaces argv[0] with
    the trusted canonical glab and passes the minimal, host-pinned env."""
    resolved_host = _resolve_host(host)
    glab = _glab_bin()
    operation = f"glab {' '.join(argv[1:3])}"  # e.g. "glab api projects/…" (bounded)
    try:
        # Bytes, decoded below under our own control. See the long note in
        # github_runner.run_gh: `text=True` follows the locale (the ANSI codepage
        # on Windows) and crashes in subprocess's reader thread on non-ASCII
        # output, while `errors="replace"` would let U+FFFD through into a JSON
        # string value and on into stored issue records.
        proc = subprocess.run(
            [glab, *argv[1:]],
            capture_output=True,
            timeout=timeout,
            check=False,
            input=input_text.encode("utf-8") if input_text is not None else None,
            env=_glab_env(resolved_host),
        )
    except FileNotFoundError as exc:  # pragma: no cover — _glab_bin guards first
        _audit("glab_run", operation, "failure", error="glab not found")
        raise ProviderSetupError(
            "the `glab` CLI is not installed on this host", reason="not_installed"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        _audit("glab_run", operation, "failure", error=f"timeout after {timeout}s")
        raise ProviderCliError(f"`glab` timed out after {timeout}s") from exc
    try:
        decoded = subprocess.CompletedProcess(
            proc.args,
            proc.returncode,
            stdout=proc.stdout.decode("utf-8") if proc.stdout is not None else None,
            stderr=proc.stderr.decode("utf-8") if proc.stderr is not None else None,
        )
    except UnicodeDecodeError as exc:
        # Stream offset only -- never the offending bytes, which are payload.
        _audit("glab_run", operation, "failure", error="undecodable output")
        raise ProviderCliError(
            f"`glab` returned output that is not valid UTF-8 (at byte {exc.start})"
        ) from exc
    if decoded.returncode != 0:
        _audit("glab_run", operation, "failure", error=f"exit {decoded.returncode}")
    else:
        _audit("glab_run", operation, "ok")
    return decoded


# Markers ``glab`` prints when the CLI itself has no usable credentials for the
# target host (as opposed to a project simply being out of reach for an
# authenticated user). Matched case-insensitively against the stderr tail so the
# connect dialog can offer `glab auth login` instead of an opaque exit code.
_GLAB_AUTH_MARKERS = _transport.GLAB_AUTH_MARKERS


def _raise_if_auth_failure(stderr_tail: str, host: str) -> None:
    """Re-classify an unauthenticated ``glab`` failure as ProviderSetupError."""
    _transport.raise_if_auth_failure(stderr_tail, host, markers=_GLAB_AUTH_MARKERS)


def _stderr_tail(proc: subprocess.CompletedProcess) -> str:
    return _transport.stderr_tail(proc, sanitize=sanitize_cli_stderr)


def _is_forbidden(tail: str) -> bool:
    return _transport.is_forbidden(tail)


def _glab_api(
    path: str,
    *,
    host: str,
    timeout: float = GL_TIMEOUT_SEC,
    paginate: bool = False,
    method: str = "GET",
    body: dict | None = None,
) -> object:
    """Run ``glab api <path>`` and parse the JSON response.

    Unlike ``gh``, glab has no ``--paginate`` for arbitrary API paths, so
    ``paginate=True`` walks ``page=N`` explicitly until a short page arrives or
    :data:`_MAX_PAGES` is reached, concatenating the arrays. ``path`` must
    already be built from validated segments by the caller; ``body`` rides on
    stdin as JSON so values with spaces/specials are never argv.
    """
    return _transport.glab_api(
        path,
        host=host,
        timeout=timeout,
        paginate=paginate,
        method=method,
        body=body,
        run=_glab_run,
        page_size=_PAGE_SIZE,
        max_pages=_MAX_PAGES,
        stderr_tail=_stderr_tail,
        raise_if_auth_failure=_raise_if_auth_failure,
        is_forbidden=_is_forbidden,
    )


def _rows(data: object) -> list[dict]:
    return _normalization._rows(data)


def _obj(data: object) -> dict:
    return _normalization._obj(data)


# Compatibility wrappers keep the longstanding gitlab_client patch/import surface.
def _norm_state(state: object) -> str:
    return _normalization._norm_state(state)


def _hex_color(value: object) -> str:
    return _normalization._hex_color(value)


def _label_names(raw: object) -> list[str]:
    return _normalization._label_names(raw)


def _username(user: object) -> str | None:
    return _normalization._username(user)


def _usernames(raw: object) -> list[str]:
    return _normalization._usernames(raw)


_ACCESS_LEVEL_ROLES = _normalization._ACCESS_LEVEL_ROLES


def _role_for_access_level(level: object) -> str:
    return _normalization._role_for_access_level(level)


def _permissions_for_access_level(level: int) -> dict:
    return _normalization._permissions_for_access_level(level)


def _access_level(details: dict) -> int:
    return _normalization._access_level(details)


def _norm_issue(raw: dict) -> dict:
    return _normalization._norm_issue(raw)


def _norm_issue_detail(raw: dict, labels_by_name: dict[str, dict]) -> dict:
    return _normalization._norm_issue_detail(raw, labels_by_name)


def _shape_labels(raw: object) -> list[dict]:
    return _normalization._shape_labels(raw)


def verify_repo_access(owner: str, repo: str, *, host: str = "", timeout: float = GL_TIMEOUT_SEC) -> dict:
    """Verify the project exists and the current ``glab`` session can read it.

    Returns the same small summary dict shape ``github_client.verify_repo_access``
    does, so the connect route needs no branching: ``full_name``, ``private``,
    ``open_issues_count``, ``description``, ``permissions``.

    GitLab reports visibility as ``public``/``internal``/``private`` — both
    ``internal`` and ``private`` are non-public, and the UI's badge only
    distinguishes public from not, so both map to ``private: True``.
    """
    details = _obj(
        _glab_api(f"projects/{project_path(owner, repo)}", host=host, timeout=timeout)
    )
    if not details:
        raise ProviderCliError(f"could not read {owner}/{repo} on {host}")
    counts = _obj(details.get("statistics"))
    open_issues = details.get("open_issues_count")
    if open_issues is None:
        open_issues = counts.get("open_issues_count") or 0
    return {
        "full_name": details.get("path_with_namespace") or f"{owner}/{repo}",
        "private": str(details.get("visibility") or "private").lower() != "public",
        "open_issues_count": open_issues,
        "description": details.get("description"),
        "permissions": _permissions_for_access_level(_access_level(details)),
    }


def get_repo_permissions(owner: str, repo: str, *, host: str = "", timeout: float = GL_TIMEOUT_SEC) -> dict:
    """The authenticated ``glab`` user's permission object for the project."""
    perms = verify_repo_access(owner, repo, host=host, timeout=timeout).get("permissions")
    return perms if isinstance(perms, dict) else {}


def _list_issues(
    owner: str, repo: str, state: str, *, host: str, timeout: float, paginate: bool
) -> list[dict]:
    """List issues of ``state``, most-recently-updated first.

    ``scope=all`` is REQUIRED: GitLab's project-issues endpoint historically
    defaults to issues created by the caller, which on someone else's project
    silently returns almost nothing — the exact failure mode that would make the
    triage view look empty rather than broken.
    """
    gl_state = "opened" if state == "open" else "closed"
    # A SINGLE-page listing must ask for a full page: GitLab defaults to 20 while
    # GitHub's equivalent path asks for 100, so without this the closed list was a
    # fifth of the size the UI (and this docstring) promises. The paginated path
    # already gets ``per_page`` from ``_glab_api``, so it is not added twice.
    path = (
        f"projects/{project_path(owner, repo)}/issues"
        f"?state={gl_state}&scope=all&order_by=updated_at&sort=desc"
        + ("" if paginate else f"&per_page={_PAGE_SIZE}")
    )
    data = _glab_api(path, host=host, timeout=timeout, paginate=paginate)
    return [_norm_issue(row) for row in _rows(data)]


def list_open_issues(
    owner: str, repo: str, *, host: str = "", timeout: float = GL_PAGINATE_TIMEOUT_SEC
) -> list[dict]:
    """Every OPEN issue (all pages) — the triage view's working set."""
    return _list_issues(owner, repo, "open", host=host, timeout=timeout, paginate=True)


def list_open_issues_first_page(
    owner: str, repo: str, *, host: str = "", timeout: float = GL_TIMEOUT_SEC
) -> list[dict]:
    """The newest single page of OPEN issues, in ONE request (no pagination).

    The progressive first paint on a cold cache — the same first page (full issue
    shape, most-recently-updated first) that ``list_open_issues`` returns, so the
    full paginated set appends behind it with no reordering. Mirrors
    ``github_client.list_open_issues_first_page``."""
    return _list_issues(owner, repo, "open", host=host, timeout=timeout, paginate=False)


def list_closed_issues(
    owner: str, repo: str, *, host: str = "", timeout: float = GL_TIMEOUT_SEC
) -> list[dict]:
    """The most recently updated CLOSED issues (single page, like GitHub's)."""
    return _list_issues(owner, repo, "closed", host=host, timeout=timeout, paginate=False)


def list_recent_open_issues(
    owner: str, repo: str, limit: int = 30, *, host: str = "", timeout: float = GL_TIMEOUT_SEC
) -> list[dict]:
    """The newest open issues by CREATION time — the watcher's poll query.

    Ordered by ``created_at`` (not ``updated_at``): the watcher notifies on
    issues that are NEW, and an old issue that just got a comment must not look
    new. Mirrors ``github_client.list_recent_open_issues``.
    """
    capped = max(1, min(int(limit), _PAGE_SIZE))
    path = (
        f"projects/{project_path(owner, repo)}/issues"
        f"?state=opened&scope=all&order_by=created_at&sort=desc&per_page={capped}"
    )
    data = _glab_api(path, host=host, timeout=timeout)
    return [_norm_issue(row) for row in _rows(data)][:capped]


def list_repo_labels(owner: str, repo: str, *, host: str = "", timeout: float = GL_TIMEOUT_SEC) -> list[dict]:
    """Every label defined on the project, with its configured colour."""
    path = f"projects/{project_path(owner, repo)}/labels"
    return _shape_labels(_glab_api(path, host=host, timeout=timeout, paginate=True))


def list_repo_collaborators(
    owner: str, repo: str, *, host: str = "", timeout: float = GL_PAGINATE_TIMEOUT_SEC
) -> list[dict]:
    """Authoritative member roster: everyone with access to the project.

    ``members/all`` includes members inherited from ancestor groups, which is
    what "everyone with access" means on GitLab — the direct-members endpoint
    would omit the entire group in the common case of a group-owned project.
    Returns ``[{login, role_name}]`` in github_client's vocabulary.
    """
    path = f"projects/{project_path(owner, repo)}/members/all"
    data = _glab_api(path, host=host, timeout=timeout, paginate=True)
    out: list[dict] = []
    for row in _rows(data):
        login = _username(row)
        if login:
            out.append({"login": login, "role_name": _role_for_access_level(row.get("access_level"))})
    return out


def derive_members(issues: list[dict]) -> list[dict]:
    """Return no inferred roster when GitLab's authoritative endpoint is forbidden."""
    return _normalization.derive_members(issues)


def get_current_login(*, host: str = "", timeout: float = GL_TIMEOUT_SEC) -> str | None:
    """The authenticated ``glab`` user's username, or ``None`` if unavailable."""
    try:
        user = _obj(_glab_api("user", host=host, timeout=timeout))
    except ProviderCliError:
        return None
    return _username(user)


def list_contributed_repos(
    login: str,
    *,
    host: str = "",
    within_days: int = CONTRIB_WINDOW_DAYS,
    timeout: float = GL_PAGINATE_TIMEOUT_SEC,
) -> tuple[list[dict], bool]:
    """Projects the current user is a member of, most recently active first.

    GitHub's version walks the user's public event feed; GitLab exposes the
    better answer directly (``projects?membership=true``), so this uses it. The
    result shape matches github_client's: ``{"repos": [...], "truncated": bool}``
    with each row carrying ``owner``/``repo``/``pushed_at``/``private``.

    Returns ``(rows, truncated)`` -- the same tuple github_client returns, so the
    route unpacks one shape. ``within_days`` filters on last activity, so the
    connect picker's "recent" framing stays honest.
    """
    del login  # membership is resolved from the authenticated glab session
    path = "projects?membership=true&order_by=last_activity_at&sort=desc&simple=true"
    data = _glab_api(path, host=host, timeout=timeout, paginate=True)
    rows = _rows(data)
    cutoff = _cutoff_iso(within_days)
    out: list[dict] = []
    for row in rows:
        full = str(row.get("path_with_namespace") or "")
        if "/" not in full:
            continue
        namespace, project = full.rsplit("/", 1)
        activity = str(row.get("last_activity_at") or "")
        if cutoff and activity and activity < cutoff:
            continue
        out.append(
            {
                "owner": namespace,
                "repo": project,
                "pushed_at": row.get("last_activity_at"),
                "private": str(row.get("visibility") or "private").lower() != "public",
                "description": row.get("description"),
            }
        )
    return out, len(rows) >= _MAX_PAGES * _PAGE_SIZE


def _cutoff_iso(window_days: int) -> str:
    """ISO cutoff for a lookback window, or ``""`` when the window is unbounded."""
    try:
        days = int(window_days)
    except (TypeError, ValueError):
        return ""
    if days <= 0 or days >= MAX_WINDOW_DAYS:
        return ""
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def get_issue_detail(
    owner: str, repo: str, number: int, *, host: str = "", timeout: float = GL_TIMEOUT_SEC
) -> dict:
    """Full detail for one issue.

    ``number`` is the project-scoped ``iid`` (what the UI shows and what a URL
    contains), coerced to ``int`` before it can reach an argv. Label colours need
    the project's label set, which is fetched alongside; a failure there degrades
    to uncoloured labels rather than failing the whole detail read, since the
    body and timeline are the point of the pane.
    """
    iid = int(number)
    detail = _obj(
        _glab_api(
            f"projects/{project_path(owner, repo)}/issues/{iid}", host=host, timeout=timeout
        )
    )
    if not detail:
        raise ProviderCliError(f"could not read {owner}/{repo}#{iid} on {host}")
    try:
        labels_by_name = {
            lab["name"]: lab for lab in list_repo_labels(owner, repo, host=host, timeout=timeout)
        }
    except ProviderCliError:
        labels_by_name = {}
    return _norm_issue_detail(detail, labels_by_name)


# ── reference summary (hover card / `#123` resolution) ──────────────────────


def get_ref_summary(
    owner: str, repo: str, number: int, *, host: str = "", timeout: float = GL_TIMEOUT_SEC
) -> dict:
    """Compact summary of one referenced ISSUE — the GitLab twin of
    ``github_client.get_ref_summary``.

    Deliberately issue-only, and that is a provider difference rather than a gap.
    GitHub shares ONE number sequence between issues and pull requests, so a bare
    ``#123`` is ambiguous and its ``/issues/{n}`` endpoint answers for both; the
    caller needs ``is_pr`` to learn which it got. GitLab keeps two independent
    sequences and two sigils -- ``#5`` is issue 5, ``!5`` is merge request 5, and
    they are unrelated items. Falling back to the merge-request endpoint when
    issue ``n`` is absent would therefore not "resolve" the reference, it would
    describe a different item under the number the user asked about, so a missing
    issue raises instead. ``is_pr`` is always ``False`` for the same reason.

    ``number`` is coerced to ``int`` before it can reach an argv.
    """
    iid = int(number)
    raw = _obj(
        _glab_api(
            f"projects/{project_path(owner, repo)}/issues/{iid}", host=host, timeout=timeout
        )
    )
    if not raw:
        raise ProviderCliError(f"could not read {owner}/{repo}#{iid} on {host or 'gitlab.com'}")

    label_names = _label_names(raw.get("labels"))
    labels_by_name: dict[str, dict] = {}
    if label_names:
        # Only paid for when the issue actually has labels: the hover card renders
        # each in its configured colour, which the issue payload does not carry.
        try:
            labels_by_name = {
                lab["name"]: lab
                for lab in list_repo_labels(owner, repo, host=host, timeout=timeout)
            }
        except ProviderCliError:
            labels_by_name = {}

    return {
        "number": raw.get("iid"),
        "title": raw.get("title") or "",
        "state": _norm_state(raw.get("state")),
        # GitLab has no close-reason concept.
        "state_reason": None,
        "url": raw.get("web_url") or "",
        "author": _username(raw.get("author")),
        "author_association": None,
        "created_at": raw.get("created_at"),
        "updated_at": raw.get("updated_at"),
        "closed_at": raw.get("closed_at"),
        "comments": raw.get("user_notes_count") or 0,
        "is_pr": False,
        "draft": False,
        "merged_at": None,
        "labels": [
            {"name": name, "color": _hex_color((labels_by_name.get(name) or {}).get("color"))}
            for name in label_names
        ],
    }


# ── timeline ────────────────────────────────────────────────────────────────
#
# GitHub serves one unified `issues/{n}/timeline`. GitLab splits the same
# information across three endpoints, so the timeline is assembled here:
#
#   notes                  -- human comments AND system notes (system: true)
#   resource_label_events  -- label added/removed, with the actor
#   resource_state_events  -- close/reopen, with the actor
#
# System notes are GitLab's prose rendering of events it has no typed endpoint
# for (assignment, milestone, rename, cross-reference). They are parsed into the
# same typed shapes GitHub emits, so the UI renders one vocabulary; anything
# unrecognized is dropped rather than shown as raw prose, matching
# github_client's "drop the noise" behavior.

# GitLab writes these as the note body; the leading verb identifies the event.
_SYSTEM_NOTE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("assigned to @", "assigned"),
    ("unassigned @", "unassigned"),
    ("changed title from", "renamed"),
    ("mentioned in issue", "cross-referenced"),
    ("mentioned in merge request", "cross-referenced"),
    ("mentioned in commit", "referenced"),
    ("changed milestone to", "milestoned"),
    ("removed milestone", "demilestoned"),
)

_TITLE_CHANGE_RE = _normalization._TITLE_CHANGE_RE
_MENTION_REF_RE = _normalization._MENTION_REF_RE
_ASSIGNEE_RE = _normalization._ASSIGNEE_RE
_COMMIT_REF_RE = _normalization._COMMIT_REF_RE


def _norm_note(note: dict) -> dict | None:
    # Read the facade table at call time so monkeypatches and extensions keep working.
    return _normalization.norm_note(note, patterns=_SYSTEM_NOTE_PATTERNS)


def _norm_label_event(event: dict, labels_by_name: dict[str, dict]) -> dict | None:
    return _normalization._norm_label_event(event, labels_by_name)


def _norm_state_event(event: dict) -> dict | None:
    return _normalization._norm_state_event(event)


def _assemble_timeline(
    owner: str, repo: str, kind: str, number: int, *, host: str, timeout: float,
) -> tuple[list[dict], list[dict]]:
    """Merge notes + label events + state events into one sorted timeline.

    ``kind`` is ``"issues"`` or ``"merge_requests"``. A failure on a SECONDARY
    stream (label/state events) degrades to omitting those entries rather than
    failing the whole pane: the comments are the substance, and an older GitLab
    may not serve the resource-event endpoints at all.

    Returns ``(events, notes)`` — the sorted timeline AND the raw ``notes`` it
    fetched. The MR timeline needs those same notes a second time to promote
    positioned (inline) ones, and re-reading ``{base}/notes`` there was a duplicate
    paginated round-trip on every PR-detail load; returning them reuses the one
    fetch. The issue timeline ignores the second element.
    """
    base = f"projects/{project_path(owner, repo)}/{kind}/{int(number)}"
    notes = _rows(_glab_api(f"{base}/notes?order_by=created_at&sort=asc", host=host, timeout=timeout, paginate=True))
    events: list[dict] = [e for e in (_norm_note(n) for n in notes) if e is not None]

    try:
        labels_by_name = {lab["name"]: lab for lab in list_repo_labels(owner, repo, host=host, timeout=timeout)}
    except ProviderCliError:
        labels_by_name = {}
    for path, normalizer in (
        (f"{base}/resource_label_events", lambda e: _norm_label_event(e, labels_by_name)),
        (f"{base}/resource_state_events", _norm_state_event),
    ):
        try:
            raw = _rows(_glab_api(path, host=host, timeout=timeout, paginate=True))
        except ProviderCliError:
            continue
        events.extend(e for e in (normalizer(item) for item in raw) if e is not None)

    events.sort(key=lambda e: e.get("created_at") or "")
    return events, notes


def list_issue_timeline(
    owner: str, repo: str, number: int, *, host: str = "", timeout: float = GL_PAGINATE_TIMEOUT_SEC
) -> list[dict]:
    """Normalized, chronological timeline for one issue."""
    events, _notes = _assemble_timeline(owner, repo, "issues", number, host=host, timeout=timeout)
    return events


def list_pr_timeline(
    owner: str, repo: str, number: int, *, host: str = "", timeout: float = GL_PAGINATE_TIMEOUT_SEC
) -> list[dict]:
    """Normalized timeline for one merge request, including inline comments.

    GitLab keeps inline (diff) comments in the SAME notes stream as discussion
    comments, distinguished by a ``position`` object — unlike GitHub, where they
    live on a separate endpoint. So the MR timeline is the shared assembly plus a
    promotion of positioned notes to ``review_comment`` entries carrying their file
    and line, which is what makes a review's substance visible. The assembly already
    fetched the notes, so it hands them back rather than us re-reading ``{base}/
    notes`` — that re-read was a duplicate paginated round-trip on every load.
    """
    events, notes = _assemble_timeline(
        owner, repo, "merge_requests", number, host=host, timeout=timeout
    )
    inline: list[dict] = []
    positioned_bodies: set[tuple[str, str]] = set()
    for note in notes:
        position = _obj(note.get("position"))
        if note.get("system") or not position:
            continue
        body = str(note.get("body") or "")
        created = str(note.get("created_at") or "")
        positioned_bodies.add((created, body))
        inline.append(
            {
                "kind": "review_comment",
                "actor": _username(note.get("author")),
                "created_at": note.get("created_at"),
                "body": body,
                "author_association": None,
                "path": position.get("new_path") or position.get("old_path"),
                "line": position.get("new_line") or position.get("old_line"),
                "url": None,
            }
        )
    # A positioned note was already emitted as a plain "comment" by the shared
    # assembly; drop that copy so the pane shows each note once, as the richer
    # inline entry.
    events = [
        e
        for e in events
        if not (
            e.get("kind") == "comment"
            and (str(e.get("created_at") or ""), str(e.get("body") or "")) in positioned_bodies
        )
    ]
    events.extend(inline)
    events.sort(key=lambda e: e.get("created_at") or "")
    return events


# ── write operations ────────────────────────────────────────────────────────


def add_issue_labels(
    owner: str, repo: str, number: int, labels: list[str], *, host: str = "", timeout: float = GL_TIMEOUT_SEC
) -> list[dict]:
    """Add ``labels`` to an issue and return its FULL resulting label set.

    GitLab's ``add_labels`` is additive and idempotent (already-present labels
    are no-ops), matching GitHub's behavior. Names ride in a JSON stdin body, so
    labels with spaces or specials are safe.
    """
    data = _obj(
        _glab_api(
            f"projects/{project_path(owner, repo)}/issues/{int(number)}",
            host=host,
            timeout=timeout,
            method="PUT",
            body={"add_labels": ",".join(labels)},
        )
    )
    return _resolve_label_details(owner, repo, _label_names(data.get("labels")), host=host, timeout=timeout)


def remove_issue_label(
    owner: str, repo: str, number: int, label: str, *, host: str = "", timeout: float = GL_TIMEOUT_SEC
) -> list[dict] | None:
    """Remove ONE label from an issue; returns the remaining labels, shaped.

    Removing a label the issue does not carry is idempotent success on GitLab
    and the response still reports the authoritative set, so — unlike GitHub's
    404 path — this never needs to return ``None``. The ``None`` return stays in
    the signature for parity with github_client, whose callers handle it.
    """
    data = _obj(
        _glab_api(
            f"projects/{project_path(owner, repo)}/issues/{int(number)}",
            host=host,
            timeout=timeout,
            method="PUT",
            body={"remove_labels": label},
        )
    )
    return _resolve_label_details(owner, repo, _label_names(data.get("labels")), host=host, timeout=timeout)


def _resolve_label_details(
    owner: str, repo: str, names: list[str], *, host: str, timeout: float
) -> list[dict]:
    """Attach colour/description to label NAMES returned by a write call.

    GitLab's issue-update response lists labels as bare names, but the caller
    (and the cache it writes) expects ``{name, color, description}``. A failure
    to read the project's labels degrades to the neutral colour rather than
    failing a write that already succeeded.
    """
    try:
        by_name = {lab["name"]: lab for lab in list_repo_labels(owner, repo, host=host, timeout=timeout)}
    except ProviderCliError:
        by_name = {}
    return [
        {
            "name": name,
            "color": _hex_color(by_name.get(name, {}).get("color")),
            "description": by_name.get(name, {}).get("description") or "",
        }
        for name in names
    ]


def set_issue_state(
    owner: str,
    repo: str,
    number: int,
    state: str,
    state_reason: str | None = None,
    *,
    host: str = "",
    timeout: float = GL_TIMEOUT_SEC,
) -> dict:
    """Close or reopen an issue. ``state`` is ``"open"`` or ``"closed"``.

    ``state_reason`` is accepted for signature parity and ignored: GitLab has no
    close-reason concept, so reporting one back would be fiction. The returned
    ``state_reason`` is always ``None``.
    """
    del state_reason
    event = "close" if state == "closed" else "reopen"
    data = _obj(
        _glab_api(
            f"projects/{project_path(owner, repo)}/issues/{int(number)}",
            host=host,
            timeout=timeout,
            method="PUT",
            body={"state_event": event},
        )
    )
    return {"state": _norm_state(data.get("state")) or state, "state_reason": None}


def _resolve_assignee_ids(
    owner: str, repo: str, usernames: list[str], *, host: str, timeout: float
) -> list[int]:
    """Resolve project-member usernames to the numeric ids GitLab's issue-update
    API requires (``assignee_ids``).

    GitLab addresses assignees by numeric user id, not username, so the editor's
    logins have to be translated. The project MEMBER roster is the right source:
    GitLab refuses to assign a non-member and would silently drop an unknown id
    while still answering 200, so an unassignable login is rejected HERE (as
    :class:`ProviderInvalidInputError`) rather than vanishing from the write. One
    roster read covers every username in the request.
    """
    if not usernames:
        return []
    members = _rows(
        _glab_api(
            f"projects/{project_path(owner, repo)}/members/all",
            host=host, timeout=timeout, paginate=True,
        )
    )
    by_name = {
        str(m.get("username")).lower(): m.get("id")
        for m in members
        if m.get("username") and isinstance(m.get("id"), int)
    }
    ids: list[int] = []
    missing: list[str] = []
    for name in usernames:
        uid = by_name.get(name.lower())
        if isinstance(uid, int):
            ids.append(uid)
        else:
            missing.append(name)
    if missing:
        raise ProviderInvalidInputError(
            "GitLab will not assign: "
            + ", ".join(missing)
            + " -- an assignee must be a member of the project.",
            values=missing,
        )
    return ids


def set_issue_assignees(
    owner: str, repo: str, number: int, assignees: list[str], *, host: str = "", timeout: float = GL_TIMEOUT_SEC
) -> list[str]:
    """REPLACE an issue's assignees with ``assignees`` (usernames).

    Parity with github_client.set_issue_assignees: the set is REPLACED, not
    merged, and an empty list clears every assignee. GitLab addresses assignees
    by numeric ``assignee_ids``, so the usernames are resolved against the
    project members first (an empty list stays empty and skips the lookup), and
    an unresolvable username raises :class:`ProviderInvalidInputError` -- the same
    class GitHub's 422 maps to -- so the route answers 400 either way.

    Resolving BEFORE the write is what makes the two providers agree. GitLab does
    not validate ``assignee_ids`` the way GitHub validates logins: it ignores an id
    it will not honour and answers 200, so sending an unresolved name would report
    success for an assignment that never happened.

    GitLab Free caps an issue at ONE assignee and silently keeps only the first of
    a longer list, so the returned set is read back from the response rather than
    echoing the request.

    Returns the issue's authoritative assignee usernames after the change."""
    ids = _resolve_assignee_ids(owner, repo, assignees, host=host, timeout=timeout)
    # ``assignee_ids: []`` is how GitLab clears assignees; send it explicitly.
    data = _obj(
        _glab_api(
            f"projects/{project_path(owner, repo)}/issues/{int(number)}",
            host=host,
            timeout=timeout,
            method="PUT",
            body={"assignee_ids": ids},
        )
    )
    return _usernames(data.get("assignees"))


def create_label(
    owner: str,
    repo: str,
    name: str,
    color: str = "888888",
    description: str = "",
    *,
    host: str = "",
    timeout: float = GL_TIMEOUT_SEC,
) -> dict:
    """Create a new label on the project.

    ``color`` is a 6-hex string WITHOUT a leading ``#`` in the app's vocabulary;
    GitLab's API REQUIRES the ``#``, so it is added here (and a caller-supplied
    one tolerated). Idempotent: an already-existing label (GitLab 409) is
    re-read and returned rather than raising, matching github_client's handling
    of GitHub's 422.
    """
    hexcolor = _hex_color(color)
    payload = {"name": name, "color": f"#{hexcolor}", "description": description or ""}
    try:
        data = _glab_api(
            f"projects/{project_path(owner, repo)}/labels",
            host=host,
            timeout=timeout,
            method="POST",
            body=payload,
        )
    except ProviderPermissionError:
        raise  # no write access — route maps to HTTP 403
    except ProviderCliError as exc:
        message = str(exc).lower()
        if "409" in message or "already exists" in message or "has already been taken" in message:
            existing = _shape_labels(
                _glab_api(
                    f"projects/{project_path(owner, repo)}/labels?search={quote(name, safe='')}",
                    host=host,
                    timeout=timeout,
                )
            )
            match = next((lab for lab in existing if lab.get("name") == name), None)
            if match:
                return match
            return {"name": name, "color": hexcolor, "description": description or ""}
        raise
    shaped = _shape_labels([data] if isinstance(data, dict) else [])
    return shaped[0] if shaped else {"name": name, "color": hexcolor, "description": description or ""}


# ── merge requests ──────────────────────────────────────────────────────────


def _norm_pull(raw: dict) -> dict:
    return _normalization._norm_pull(raw)


def _list_pulls(owner: str, repo: str, state: str, *, host: str, timeout: float, paginate: bool) -> list[dict]:
    # "closed" on GitLab excludes merged MRs, but the app's closed tab means
    # "no longer open" — so a closed listing asks for all and filters, rather
    # than silently hiding every merged MR.
    gl_state = "opened" if state == "open" else "all"
    # Full page on the single-page path, for the same reason as the issue list:
    # GitLab's default of 20 would make the closed list a fifth of GitHub's 100.
    path = (
        f"projects/{project_path(owner, repo)}/merge_requests"
        f"?state={gl_state}&scope=all&order_by=updated_at&sort=desc"
        + ("" if paginate else f"&per_page={_PAGE_SIZE}")
    )
    rows = [_norm_pull(row) for row in _rows(_glab_api(path, host=host, timeout=timeout, paginate=paginate))]
    if state != "open":
        rows = [row for row in rows if row.get("state") == "closed"]
    return rows


def list_open_pulls(owner: str, repo: str, *, host: str = "", timeout: float = GL_PAGINATE_TIMEOUT_SEC) -> list[dict]:
    return _list_pulls(owner, repo, "open", host=host, timeout=timeout, paginate=True)


def list_open_pulls_first_page(
    owner: str, repo: str, *, host: str = "", timeout: float = GL_TIMEOUT_SEC
) -> list[dict]:
    """The newest single page of OPEN MRs, in ONE request (no pagination).

    The progressive first paint on a cold cache — the same first page (full MR
    shape, most-recently-updated first) that ``list_open_pulls`` returns, so the
    full paginated set appends behind it with no reordering. GitLab already
    inlines each MR's ``head_pipeline`` (so ``_norm_pull`` writes the card
    enrichment eagerly and ``enrich_pulls`` is a no-op), meaning this page is
    already card-complete. Mirrors ``github_client.list_open_pulls_first_page``.
    """
    return _list_pulls(owner, repo, "open", host=host, timeout=timeout, paginate=False)


def list_closed_pulls(owner: str, repo: str, *, host: str = "", timeout: float = GL_TIMEOUT_SEC) -> list[dict]:
    return _list_pulls(owner, repo, "closed", host=host, timeout=timeout, paginate=False)


def get_pr_detail(
    owner: str, repo: str, number: int, *, host: str = "", timeout: float = GL_TIMEOUT_SEC,
    resolve_mergeable: bool = True,
) -> dict:
    """Full detail for one merge request, in ``_PR_DETAIL_JQ``'s shape.

    ``changes_count`` is GitLab's only cheap size signal and is a STRING that may
    be approximate (``"20+"``), so it maps to ``changed_files`` while
    ``additions``/``deletions`` are ``None`` — reading real line counts would
    require pulling the whole diff on every detail view. The UI already treats
    those as optional.

    ``resolve_mergeable`` is accepted for signature parity with the GitHub client
    (see its docstring) and is a no-op here: GitLab reports mergeability in the one
    detail response, so there is no lazy retry to skip.
    """
    _ = resolve_mergeable  # parity-only; GitLab has no lazy-mergeability retry
    iid = int(number)
    raw = _obj(
        _glab_api(
            f"projects/{project_path(owner, repo)}/merge_requests/{iid}", host=host, timeout=timeout
        )
    )
    if not raw:
        raise ProviderCliError(f"could not read {owner}/{repo}!{iid} on {host}")
    detail = _norm_pull(raw)
    changed = str(raw.get("changes_count") or "").rstrip("+")
    detail.update(
        {
            "additions": None,
            "deletions": None,
            "changed_files": int(changed) if changed.isdigit() else None,
            "commits": raw.get("commits_count"),
            "comments": raw.get("user_notes_count") or 0,
            "review_comments": None,
            "merged": bool(raw.get("merged_at")),
            "mergeable": _mergeable(raw),
            "mergeable_state": str(raw.get("detailed_merge_status") or raw.get("merge_status") or "unknown"),
            "merged_by": _username(raw.get("merged_by")),
            # ``head_sha`` is NOT re-derived here: ``_norm_pull`` already set it from
            # the same expression, and two copies of a value a merge is pinned to is
            # one copy too many.
            # "Merge when pipeline succeeds" is GitLab's auto-merge. Normalized to
            # the same optional object the GitHub path reports, so the pane's
            # control reads one shape.
            "auto_merge": (
                {
                    "method": "SQUASH" if raw.get("squash") else None,
                    # NOT falling back to the assignee: GitLab populates
                    # ``merge_user`` only AFTER the merge, so for an armed-but-
                    # unmerged MR it is reliably null and the fallback would name
                    # whoever happens to be assigned as the person who armed it.
                    # None means "we do not know", which is the truth.
                    "enabled_by": _username(raw.get("merge_user")),
                }
                if raw.get("merge_when_pipeline_succeeds")
                else None
            ),
        }
    )
    return detail


# Statuses that map onto GitHub's ``mergeable`` — i.e. "the branches do not
# conflict". Deliberately the SAME weak claim GitHub's field makes, no stronger:
# ``can_be_merged`` is the legacy ``merge_status`` value and speaks only to conflicts,
# while ``mergeable`` is the modern ``detailed_merge_status`` and does imply the
# approval/discussion/pipeline rules are met. Folding them together is correct HERE
# (the reader wants a conflict signal) and would be wrong for a merge gate — which is
# why ``routes._MERGE_ALLOWED_STATES`` keys off the raw status instead and admits only
# the modern value.
_MERGEABLE_STATUSES = _normalization._MERGEABLE_STATUSES
_MERGE_STATUS_PENDING = _normalization._MERGE_STATUS_PENDING


def _mergeable(raw: dict) -> bool | None:
    return _normalization._mergeable(raw)


_JOB_FAILURE_STATUSES = _normalization._JOB_FAILURE_STATUSES
_JOB_RUNNING_STATUSES = _normalization._JOB_RUNNING_STATUSES
_JOB_OTHER_STATUSES = _normalization._JOB_OTHER_STATUSES


def _job_bucket(status: str, allow_failure: bool) -> str:
    return _normalization._job_bucket(status, allow_failure)


def _norm_job(job: dict) -> dict:
    return _normalization._norm_job(job)


_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")


def list_pr_checks(
    owner: str, repo: str, sha: str, *, host: str = "", timeout: float = GL_TIMEOUT_SEC
) -> list[dict]:
    """Check-run-shaped rows for the pipeline that ran on commit ``sha``.

    Keyed on the head SHA (not the MR iid) to match
    ``github_client.list_pr_checks``, and because that is the correct semantics:
    an MR accumulates one pipeline per pushed commit, and a pipeline for an
    older commit describes code that no longer exists. Asking GitLab for the
    pipelines of a specific SHA gets the run that matches what the user is
    looking at.

    ``sha`` is charset-validated before reaching the query string, since it is
    the one value here that originates from a previous API response rather than
    from a connected-repo record.
    """
    if not _SHA_RE.match(sha or ""):
        raise ProviderCliError(f"invalid commit sha {sha!r}")
    pipelines = _rows(
        _glab_api(
            f"projects/{project_path(owner, repo)}/pipelines?sha={quote(sha, safe='')}",
            host=host,
            timeout=timeout,
        )
    )
    if not pipelines:
        return []
    latest = max(pipelines, key=lambda p: str(p.get("created_at") or ""))
    pipeline_id = latest.get("id")
    if not isinstance(pipeline_id, int):
        return []
    jobs = _rows(
        _glab_api(
            f"projects/{project_path(owner, repo)}/pipelines/{pipeline_id}/jobs",
            host=host,
            timeout=timeout,
            paginate=True,
        )
    )
    return [_norm_job(job) for job in jobs]


_CHECK_BUCKETS = _normalization._CHECK_BUCKETS


def summarize_checks(checks: list[dict]) -> dict:
    return _normalization.summarize_checks(checks)


def enrich_pulls(owner: str, repo: str, pulls: list[dict], state: str, *, host: str = "") -> list[dict]:
    return _normalization.enrich_pulls(owner, repo, pulls, state, host=host)


def enrich_pulls_by_number(owner: str, repo: str, pulls: list[dict], *, host: str = "") -> list[dict]:
    return _normalization.enrich_pulls_by_number(owner, repo, pulls, host=host)


def enrichment_complete(pulls: list[dict]) -> bool:
    return _normalization.enrichment_complete(pulls)


def _pipeline_summary(raw: dict) -> dict:
    return _normalization._pipeline_summary(raw)


# ── cheap open-list probe (poll gating) ─────────────────────────────────────
#
# The list routes poll with ``poll=1`` and serve the cache when a cheap probe
# shows the open list has not moved, rather than re-paginating a busy project
# every minute. The probe value is only ever compared against ANOTHER probe
# recorded when the list was last fetched, never against the cached rows, so a
# systematic difference between what the probe counts and what the list returns
# cancels out instead of reporting "changed" forever.
#
# GitHub answers this in one search call carrying ``total_count``. GitLab has no
# equivalent envelope, so the two kinds are served differently and honestly:
#
#   issues -- ``issues_statistics`` gives an EXACT open count in one call, so the
#     probe is as strong as GitHub's: closing any issue, anywhere in the list,
#     changes it.
#   merge requests -- GitLab exposes no MR count without reading response
#     headers, which this module deliberately does not depend on (it parses only
#     ``glab api`` JSON). Rather than invent a weaker signal that could report
#     "unchanged" after a non-top MR closed, this raises and lets the caller take
#     its documented probe-unavailable path: keep serving the cache, bounded by
#     the staleness ceiling, and refetch when that expires.
_PROBE_KINDS = ("issue", "pr")


def probe_open_list(
    owner: str, repo: str, kind: str, *, host: str = "", timeout: float = GL_TIMEOUT_SEC
) -> dict:
    """``{"total_count": int, "top_updated_at": str | None}`` for a project's
    OPEN issues.

    Raises :class:`ProviderCliError` on a failed or unparseable call, and for
    ``kind="pr"`` -- see the note above on why an MR probe is refused rather than
    approximated. Callers already treat a probe failure as a soft signal, so this
    degrades to the pre-poll behaviour instead of serving anything stale.
    """
    if kind not in _PROBE_KINDS:
        raise ProviderCliError(f"unsupported probe kind: {kind!r}")
    if kind == "pr":
        raise ProviderCliError(
            "GitLab exposes no cheap open merge-request count; "
            "falling back to the staleness ceiling"
        )
    project = project_path(owner, repo)
    stats = _obj(
        _glab_api(f"projects/{project}/issues_statistics?scope=all", host=host, timeout=timeout)
    )
    counts = _obj(_obj(stats.get("statistics")).get("counts"))
    total = counts.get("opened")
    if not isinstance(total, int):
        raise ProviderCliError(f"probe for {owner}/{repo} {kind} returned no open count")
    # The newest updated_at is a second, independent signal: an EDIT to an
    # existing issue moves it without changing the count.
    top = _rows(
        _glab_api(
            f"projects/{project}/issues"
            "?state=opened&scope=all&order_by=updated_at&sort=desc&per_page=1",
            host=host,
            timeout=timeout,
        )
    )
    top_updated = top[0].get("updated_at") if top else None
    return {
        "total_count": total,
        "top_updated_at": top_updated if isinstance(top_updated, str) else None,
    }


# ── merge-request search ────────────────────────────────────────────────────


def build_pr_search_query(
    owner: str, repo: str, *, state: str = "open",
    author: str | None = None, assignee: str | None = None,
    review_requested: str | None = None,
) -> str:
    """Assemble the query parameters for a per-person merge-request search.

    GitHub takes a single search-qualifier string; GitLab takes discrete query
    parameters. The CALLER-FACING signature is identical on purpose -- the route
    passes the same keyword arguments to whichever client it holds, so a
    provider-specific spelling here would be a ``TypeError`` at request time
    rather than a compile error.

    The person filters map onto GitLab's own vocabulary:
    ``author`` -> ``author_username``, ``assignee`` -> ``assignee_username``,
    ``review_requested`` -> ``reviewer_username``.

    Every username is charset-validated BEFORE it lands in the query string, so a
    hostile value cannot smuggle in extra parameters. Raises
    :class:`PrSearchError` on an unknown state, an invalid username, or when no
    person filter was given -- an unfiltered search would just duplicate the list
    endpoint, which is the same reason github_client refuses it.
    """
    if state not in ("open", "closed", "merged", "all"):
        raise PrSearchError(f"unsupported state for MR search: {state!r}")
    # ``closed`` asks GitLab for its OWN closed state, which already excludes
    # merged MRs -- the same thing this route's "closed" means (closed WITHOUT
    # merge). Asking for ``all`` and dropping merged rows afterwards looked
    # equivalent but was not: the cap is applied to the fetched rows, so on a busy
    # project 300 newer MERGED MRs could fill it and hide every genuinely closed
    # match. Filtering after a cap can only ever lose rows it cannot see.
    gl_state = {"open": "opened", "closed": "closed", "merged": "merged", "all": "all"}[state]
    params = [f"state={gl_state}", "scope=all"]
    people = [
        ("author_username", author),
        ("assignee_username", assignee),
        ("reviewer_username", review_requested),
    ]
    added = 0
    for param, username in people:
        if not username:
            continue
        if not _USERNAME_RE.match(username):
            raise PrSearchError(f"invalid GitLab username: {username!r}")
        params.append(f"{param}={quote(username, safe='')}")
        added += 1
    if added == 0:
        raise PrSearchError("MR search needs at least one person filter")
    del owner, repo  # the project is addressed by path, not by a qualifier
    return "&".join(params)


def search_pulls(
    owner: str, repo: str, *, host: str = "", state: str = "open",
    author: str | None = None, assignee: str | None = None,
    review_requested: str | None = None,
    timeout: float = GL_PAGINATE_TIMEOUT_SEC, limit: int = PR_SEARCH_MAX,
) -> list[dict]:
    """Search a project's merge requests by person, server-side.

    Returns rows in the SAME shape as :func:`list_open_pulls`, so the frontend can
    swap data sources without a second row type. ``limit`` is honoured (the caller
    asks for one more than it will show, to tell "capped" from "exactly full").
    """
    query = build_pr_search_query(
        owner, repo, state=state, author=author, assignee=assignee,
        review_requested=review_requested,
    )
    path = f"projects/{project_path(owner, repo)}/merge_requests?{query}&order_by=updated_at&sort=desc"
    rows = _rows(_glab_api(path, host=host, timeout=timeout, paginate=True))
    # The ceiling is PR_SEARCH_MAX + 1, not PR_SEARCH_MAX. The route asks for one
    # MORE row than it will show and reports "truncated" when it gets it, so
    # clamping to the display cap silently discarded that sentinel and every
    # over-cap result set was presented as complete.
    capped = max(1, min(int(limit), PR_SEARCH_MAX + 1))
    out = [_norm_pull(row) for row in rows][:capped]
    if state == "closed":
        # Belt to GitLab's own state filter above: "closed" means closed WITHOUT
        # being merged, matching the GitHub path.
        out = [row for row in out if not row.get("merged_at")]
    return out


# ── merge-request actions (parity with github_client's PR action surface) ─────
#
# GitLab's vocabulary differs from GitHub's at every one of these, so each
# function maps the app's provider-neutral request onto GitLab's own concept:
#
#   close/reopen        -> a ``state_event`` on the MR (same shape as an issue)
#   approve             -> the dedicated /approve endpoint, NOT a review object;
#                          GitLab has no REQUEST_CHANGES verb at all
#   comment             -> a note
#   auto-merge          -> "merge when pipeline succeeds" (MWPS), the closest
#                          native equivalent, and likewise not an immediate merge
#   cancel / retry CI   -> a pipeline, not a workflow run
#
# Where GitLab genuinely has no equivalent (requesting changes; re-running only
# the failed jobs) the function REFUSES rather than approximating — silently
# turning "request changes" into a plain comment would tell the user a verdict was
# recorded when none was.


def set_pr_state(
    owner: str, repo: str, number: int, state: str, *, host: str = "",
    timeout: float = GL_TIMEOUT_SEC,
) -> dict:
    """Close or reopen a merge request. ``state`` is ``"open"`` or ``"closed"``."""
    if state not in ("open", "closed"):
        raise ProviderCliError(f"invalid MR state: {state!r}")
    event = "close" if state == "closed" else "reopen"
    data = _obj(
        _glab_api(
            f"projects/{project_path(owner, repo)}/merge_requests/{int(number)}",
            host=host,
            timeout=timeout,
            method="PUT",
            body={"state_event": event},
        )
    )
    return {
        "state": _norm_state(data.get("state")) or state,
        "merged": str(data.get("state") or "") == "merged",
        "draft": bool(data.get("draft") or data.get("work_in_progress") or False),
    }


# GitLab records approval as its own resource and has NO "request changes" verb —
# the closest thing is unapproving, which is not a verdict on a revision. So only
# the two GitLab can honour are accepted, and REQUEST_CHANGES is refused loudly.
PR_REVIEW_EVENTS = ("APPROVE", "COMMENT")


def submit_pr_review(
    owner: str, repo: str, number: int, event: str, body: str = "", head_sha: str = "",
    *, host: str = "", timeout: float = GL_TIMEOUT_SEC,
) -> dict:
    """Approve a merge request, or leave review prose on it.

    ``APPROVE`` calls GitLab's ``/approve`` endpoint, then posts any accompanying
    ``body`` as a separate note (GitLab's approval carries no text of its own). That
    ORDER is load-bearing — see the comment at the call site: the two calls are not
    atomic, and approving first is what keeps a retry from duplicating the note.
    ``COMMENT`` is just a note.

    ``head_sha`` is REQUIRED and rides as GitLab's ``sha`` precondition on
    ``/approve``, which is the same guarantee GitHub's ``commit_id`` gives: a push
    landing between the render and the click makes GitLab refuse (409) rather than
    record an approval of code the reviewer never saw. A ``COMMENT`` has no such
    parameter — a note is not a verdict and GitLab anchors it to the MR, not to a
    revision — but the sha is still required of the CALLER so the two verbs cannot
    diverge into "one of them checks and the other does not".

    ``REQUEST_CHANGES`` is REFUSED: GitLab has no such verb, and mapping it to an
    unapproval or a bare comment would report a verdict the platform never
    recorded.
    """
    verb = (event or "").strip().upper()
    if verb == "REQUEST_CHANGES":
        raise ProviderCliError(
            "GitLab has no 'request changes' review verb — leave a comment, or "
            "unapprove the merge request on GitLab"
        )
    if verb not in PR_REVIEW_EVENTS:
        raise ProviderCliError(f"invalid review event: {event!r}")
    text = (body or "").strip()
    if verb == "COMMENT" and not text:
        raise ProviderCliError("a COMMENT review requires a comment body")
    sha = (head_sha or "").strip()
    if not _SHA_RE.match(sha):
        raise ProviderCliError(
            "refusing to review without the head commit it was read at "
            f"(got {head_sha!r})"
        )
    base = f"projects/{project_path(owner, repo)}/merge_requests/{int(number)}"
    if verb == "COMMENT":
        add_pr_comment(owner, repo, number, text, host=host, timeout=timeout)
        return {"id": None, "state": "COMMENTED", "submitted_at": None}

    # APPROVE. The approval goes FIRST and the optional note second, which is the
    # opposite of what reads naturally — and is deliberate.
    #
    # These are two non-atomic calls, so one of them can fail after the other
    # succeeded, and the caller's only recovery is to retry the pair. Posting the note
    # first meant a retry after a failed /approve posted the note AGAIN, so the user
    # accumulated duplicate comments on the merge request while still not having
    # approved it. Approving first makes the retry safe in the direction that matters:
    # GitLab's /approve is idempotent (re-approving an already-approved MR is a no-op),
    # so a retry after a failed NOTE re-approves harmlessly and then posts the note
    # once. The residual failure mode is an approval with no note attached, which is
    # visible on the MR and recoverable by commenting — strictly better than silently
    # duplicating prose.
    data = _obj(
        _glab_api(
            f"{base}/approve", host=host, timeout=timeout, method="POST", body={"sha": sha}
        )
    )
    if text:
        add_pr_comment(owner, repo, number, text, host=host, timeout=timeout)
    return {
        "id": data.get("id"),
        "state": "APPROVED",
        "submitted_at": data.get("updated_at") or data.get("created_at"),
    }


def _add_note(
    owner: str, repo: str, number: int, body: str, collection: str, *, host: str, timeout: float
) -> dict:
    """Post a note on an issue or a merge request.

    ``collection`` is required and explicit because GitLab keeps issues and merge
    requests in SEPARATE number sequences — unlike GitHub, where one issues
    endpoint serves both — so the number alone does not identify the item and
    posting to the wrong collection would comment on an unrelated one. That is
    also why the public surface is two named functions rather than one with a mode
    flag: a defaulted target is a silent way to comment on the wrong thing.
    """
    text = (body or "").strip()
    if not text:
        raise ProviderCliError("a comment needs a body")
    data = _obj(
        _glab_api(
            f"projects/{project_path(owner, repo)}/{collection}/{int(number)}/notes",
            host=host,
            timeout=timeout,
            method="POST",
            body={"body": text},
        )
    )
    return {
        "id": data.get("id"),
        # GitLab's note response carries no direct web URL, so the caller links to
        # the MR/issue itself rather than fabricating an anchor.
        "url": None,
        "created_at": data.get("created_at"),
    }


def add_issue_comment(
    owner: str, repo: str, number: int, body: str, *, host: str = "",
    timeout: float = GL_TIMEOUT_SEC,
) -> dict:
    """Post a note on an ISSUE."""
    return _add_note(owner, repo, number, body, "issues", host=host, timeout=timeout)


def add_pr_comment(
    owner: str, repo: str, number: int, body: str, *, host: str = "",
    timeout: float = GL_TIMEOUT_SEC,
) -> dict:
    """Post a note on a MERGE REQUEST.

    A separate function from :func:`add_issue_comment` because on GitLab these are
    different collections with independent numbering; on GitHub they are the same
    endpoint, and ``github_client.add_pr_comment`` is an alias for exactly that
    reason. Routes always call the PR one for a PR, so the two providers cannot
    disagree about which item got the comment.
    """
    return _add_note(owner, repo, number, body, "merge_requests", host=host, timeout=timeout)


# GitLab's merge-method vocabulary maps onto the project's own merge-method
# setting rather than a per-request choice; the app keeps GitHub's names so one
# request shape serves both providers, and translates here.
#
# ``REBASE`` is deliberately ABSENT, and this tuple is deliberately shorter than
# GitHub's. GitLab's ``/merge`` endpoint has no rebase option at all — merge-commit
# vs. semi-linear vs. fast-forward is the PROJECT's ``merge_method`` setting, and the
# only per-request lever is ``squash``. So accepting ``REBASE`` here meant the request
# translated to ``squash: false`` and GitLab produced a MERGE COMMIT: the caller named
# one history shape and silently got another, on the one operation that is
# irreversible. A method the provider cannot honour is refused rather than
# approximated — the same rule the rest of this module follows for "request changes"
# and a full CI re-run. (Rebasing an MR is a separate ``/rebase`` endpoint that does
# not merge, so it is not a substitute.)
#
# ``routes._pr_merge_method_field`` reads this tuple off the KEY's own client, so the
# divergence needs no route change — which is exactly why it reads it per-provider
# instead of reaching for github_client's copy.
PR_MERGE_METHODS = ("MERGE", "SQUASH")


def merge_pull_request(
    owner: str, repo: str, number: int, method: str = "SQUASH", head_sha: str = "",
    *, host: str = "", timeout: float = GL_TIMEOUT_SEC,
) -> dict:
    """Merge a merge request now (``PUT .../merge_requests/{iid}/merge``).

    Like the GitHub path, this cannot bypass a gate: GitLab enforces the project's
    approval rules and pipeline requirements on this endpoint and answers **405**
    for an MR that has not satisfied them, or **406** when it cannot be merged
    (conflicts). Both surface as errors.

    ``method`` only decides ``squash`` — GitLab's merge-vs-rebase behaviour is a
    PROJECT setting rather than a per-request flag, which is why ``REBASE`` is not in
    :data:`PR_MERGE_METHODS` here even though it is on GitHub: this endpoint cannot
    honour it, and translating it to ``squash: false`` would hand back a merge commit
    under a rebase label. ``squash`` is sent EXPLICITLY either way: GitLab reads a
    missing ``squash`` as "leave as-is", so omitting it would inherit whatever the MR
    was last armed with instead of what this call asked for.

    ``head_sha`` is REQUIRED and rides as GitLab's ``sha`` precondition — the same
    guarantee as the GitHub path: the merge is pinned to the commit the caller looked
    at, so a push landing in between answers 409 instead of merging unreviewed code.

    Returns ``{merged, sha, message}`` in the GitHub-shaped vocabulary the route
    and the UI speak.
    """
    verb = (method or "").strip().upper()
    if verb not in PR_MERGE_METHODS:
        raise ProviderCliError(f"invalid merge method: {method!r}")
    sha = (head_sha or "").strip()
    if not re.match(r"^[0-9a-fA-F]{7,64}$", sha):
        raise ProviderCliError(
            "refusing to merge without the head commit it was reviewed at "
            f"(got {head_sha!r})"
        )
    payload: dict[str, object] = {"squash": verb == "SQUASH", "sha": sha}
    data = _obj(
        _glab_api(
            f"projects/{project_path(owner, repo)}/merge_requests/{int(number)}/merge",
            host=host,
            timeout=timeout,
            method="PUT",
            body=payload,
        )
    )
    state = str(data.get("state") or "")
    return {
        # GitLab reports the resulting STATE rather than a boolean; "merged" is the
        # only value that means the merge happened.
        "merged": state == "merged",
        "sha": data.get("merge_commit_sha") or data.get("sha"),
        "message": data.get("merge_error") or "",
    }


def enable_auto_merge(
    owner: str, repo: str, number: int, method: str = "SQUASH", *, host: str = "",
    timeout: float = GL_TIMEOUT_SEC,
) -> dict:
    """REFUSED on GitLab. Arming a deferred merge cannot be made safe here.

    GitLab has no independent "arm auto-merge" verb.
    ``merge_when_pipeline_succeeds`` is a *modifier on the merge endpoint*
    (``PUT .../merge_requests/{iid}/merge``), and it defers only while a pipeline is
    genuinely in flight — with no running pipeline GitLab merges the MR **then and
    there**.

    An earlier revision tried to contain that by reading the head pipeline first and
    only calling ``/merge`` when a run was live. That check is **not atomic**: a
    pipeline that finishes in the window between the read and the call turns the same
    request into an immediate merge. Since arming is offered as a BULK action with no
    typed confirmation (it is advertised as reversible), losing that race would merge
    a whole selection irreversibly — so the honest answer is to not offer it at all
    rather than to narrow the window and hope.

    The capability is not lost, it is relocated to the affordance that is safe:
    :func:`merge_pull_request` merges when the caller means now, and GitLab's own web
    UI owns the deferred case. Raising here — rather than returning a falsy result —
    means the route surfaces a real error and the user is never told a deferral is
    pending when none is.
    """
    del owner, repo, number, method, host, timeout
    raise ProviderCliError(
        "GitLab has no separate auto-merge to arm: the flag rides on the merge "
        "endpoint and merges immediately when no pipeline is running, which cannot "
        "be made safe from here. Merge the merge request directly, or set "
        "\"merge when pipeline succeeds\" on GitLab."
    )


def disable_auto_merge(
    owner: str, repo: str, number: int, *, host: str = "", timeout: float = GL_TIMEOUT_SEC
) -> dict:
    """REFUSED on GitLab, for symmetry with :func:`enable_auto_merge`.

    Nothing can be armed from this app on GitLab, so there is nothing here to cancel.
    An MR armed on GitLab's own web UI is cancelled there; calling
    ``/cancel_merge_when_pipeline_succeeds`` on an unarmed MR answers 406 anyway, so
    offering the control would only ever produce an error.
    """
    del owner, repo, number, host, timeout
    raise ProviderCliError(
        "GitLab auto-merge is not managed from this app — cancel \"merge when "
        "pipeline succeeds\" on GitLab."
    )


_PIPELINE_CANCELLABLE_STATES = _normalization._PIPELINE_CANCELLABLE_STATES
_PIPELINE_RETRYABLE_STATES = _normalization._PIPELINE_RETRYABLE_STATES
_PIPELINE_FINISHED_STATES = _normalization._PIPELINE_FINISHED_STATES
_PIPELINE_CONCLUSION = _normalization._PIPELINE_CONCLUSION


def _norm_pipeline_run(row: dict) -> dict | None:
    return _normalization._norm_pipeline_run(row)


def list_pr_workflow_runs(
    owner: str, repo: str, sha: str, *, host: str = "", timeout: float = GL_TIMEOUT_SEC
) -> list[dict]:
    """The CI pipelines for an MR's head commit — GitLab's analogue of a workflow
    run (see github_client for why checks and runs are separate surfaces).

    ``sha`` is charset-validated before it reaches the query, as on the GitHub
    path. Rows carry ``cancellable``/``rerunnable`` so the UI never offers an
    action GitLab will refuse.
    """
    if not re.match(r"^[0-9a-fA-F]{7,64}$", sha or ""):
        raise ProviderCliError(f"invalid commit sha: {sha!r}")
    rows = _rows(
        _glab_api(
            f"projects/{project_path(owner, repo)}/pipelines?sha={sha}",
            host=host,
            timeout=timeout,
            paginate=False,
        )
    )
    out = [_norm_pipeline_run(row) for row in rows]
    return [row for row in out if row is not None]


def cancel_workflow_run(
    owner: str, repo: str, run_id: int, *, host: str = "", timeout: float = GL_TIMEOUT_SEC
) -> dict:
    """Cancel one running pipeline."""
    _glab_api(
        f"projects/{project_path(owner, repo)}/pipelines/{int(run_id)}/cancel",
        host=host,
        timeout=timeout,
        method="POST",
    )
    return {"run_id": int(run_id), "cancelled": True}


def rerun_workflow_run(
    owner: str, repo: str, run_id: int, *, failed_only: bool = False, host: str = "",
    timeout: float = GL_TIMEOUT_SEC,
) -> dict:
    """Retry a finished pipeline.

    GitLab's ``/retry`` already retries only the failed and canceled jobs, so
    ``failed_only`` is accepted for signature parity and is a no-op — there is no
    "retry everything from scratch" verb to distinguish it from. The returned
    ``failed_only`` reports what GitLab actually did, not what was asked, so a
    caller is never told a full re-run happened.
    """
    del failed_only
    _glab_api(
        f"projects/{project_path(owner, repo)}/pipelines/{int(run_id)}/retry",
        host=host,
        timeout=timeout,
        method="POST",
    )
    return {"run_id": int(run_id), "rerun": True, "failed_only": True}
