"""URL validation: the only way a browser-supplied string becomes a provider ref.

Exact parsed-host checks decide which instance a credential-bearing read may
reach, so a URL that merely mentions a trusted host is refused. The built-in
grammars run first, the operator allowlists next, and registered providers last.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from urllib.parse import urlparse, urlunparse

from kiro_crew.dashboard.source_providers import contract, hosts, plugins
from kiro_crew.dashboard.source_providers.contract import RepoRef, SourceRef

# Path markers that identify a GitLab object, paired with the SourceRef kind
# they produce. Plain string literals matched with rfind -- deliberately not a
# regex alternation (see _parse_gitlab_path).
_GITLAB_PATH_MARKERS: tuple[tuple[str, str], ...] = (
    ("/-/merge_requests/", "change"),
    ("/-/issues/", "issue"),
)
# GitHub keeps issues and pull requests in one number space under two path
# segments. The captured segment is what derives the kind.
_GITHUB_PATH_RE = re.compile(r"/([^/]+)/([^/]+)/(pull|issues)/(\d+)", re.IGNORECASE)
_GITHUB_SEGMENT_KINDS = {"pull": "change", "issues": "issue"}


def _parse_gitlab_path(path: str) -> tuple[str, int, str]:
    """Split a GitLab MR/issue path into (project, number, kind) or raise ``ValueError``."""
    # String ops instead of a regex: the previous /(.+)/-/merge_requests/
    # pattern backtracked polynomially on adversarial paths. The
    # two markers are scanned independently and the RIGHTMOST valid one wins,
    # preserving the original ``rfind`` semantics (a project path that itself
    # contains the marker text is still split at the last occurrence) without
    # reintroducing an alternating pattern.
    best: tuple[str, int, str] | None = None
    best_idx = -1
    lowered = path.lower()
    for marker, kind in _GITLAB_PATH_MARKERS:
        idx = lowered.rfind(marker)
        if idx <= 0 or idx <= best_idx:
            continue
        project = path[1:idx]
        number_text = path[idx + len(marker) :]
        if not project or not number_text.isdigit():
            continue
        best_idx = idx
        best = (project, int(number_text), kind)
    if best is None:
        raise ValueError(
            "Expected a GitLab URL like https://gitlab.com/group/project/-/merge_requests/123 "
            "or https://gitlab.com/group/project/-/issues/123."
        )
    if any(segment in {"", ".", ".."} for segment in best[0].split("/")):
        raise ValueError("Invalid GitLab project path.")
    return best


def _gitlab_ref(host: str, path: str) -> SourceRef:
    """Build a GitLab ``SourceRef`` for an already-authorized host."""
    project, number, kind = _parse_gitlab_path(path)
    normalized = urlunparse(("https", host, path, "", "", ""))
    repo = project.rsplit("/", 1)[-1]
    owner = project.rsplit("/", 1)[0] if "/" in project else ""
    return SourceRef("gitlab", normalized, host, owner, repo, number, project=project, kind=kind)


# A Jira issue key: PROJECT-NUMBER where the project part starts with a letter,
# is uppercase alphanumeric, and Jira caps it at 10 characters. Mirrors the
# frontend's JIRA_KEY_RE in pullRequestLinks.ts -- the two parsers must agree so
# a chip the backend emits always re-parses on the frontend for the reveal.
_JIRA_KEY_RE = re.compile(r"[A-Z][A-Z0-9]{0,9}-\d+")


def _jira_ref(host: str, path: str) -> SourceRef:
    """Build a Jira ``SourceRef`` for an already-authorized host.

    Jira issues live at ``/browse/KEY-123``, possibly behind a context path
    (``/jira/browse/KEY-123`` on some Cloud tenants and Data Center installs).
    The prefix is preserved in the normalized URL so a self-hosted chip opens
    the real endpoint. The project key maps onto ``repo`` and the numeric tail
    onto ``number`` -- the same shape the frontend derives, so the sidebar chip
    label (``PROJ-123``) and the Issues panel identity agree end to end.
    """
    marker = "/browse/"
    browse_idx = path.find(marker)
    if browse_idx < 0:
        raise ValueError("Expected a Jira URL like https://org.atlassian.net/browse/PROJ-123.")
    # The key is the first segment after /browse/; deeper segments are Jira UI
    # state, not identity. Uppercase before validating -- Jira treats keys
    # case-insensitively and canonicalizing here keeps the dedup map in
    # state.py from splitting one issue across case variants.
    key = path[browse_idx + len(marker) :].split("/", 1)[0].upper()
    if not _JIRA_KEY_RE.fullmatch(key):
        raise ValueError("Expected a Jira URL like https://org.atlassian.net/browse/PROJ-123.")
    project_key, number_text = key.rsplit("-", 1)
    prefix = path[:browse_idx]
    normalized = urlunparse(("https", host, f"{prefix}{marker}{key}", "", "", ""))
    return SourceRef("jira", normalized, host, "", project_key, int(number_text), kind="issue")


def parse_source_url(raw_url: str) -> SourceRef:
    """Validate and normalize a supported pull/merge-request or issue URL.

    Public GitHub and gitlab.com are always accepted. A self-managed GitLab
    instance is accepted only when its exact ``host[:port]`` appears in the
    operator's ``dashboard.gitlab_hosts`` allowlist, so browser input can never
    choose which instance the credential-bearing CLI talks to.
    Exact parsed-host checks prevent URLs that merely mention a trusted host in
    their path, query, or userinfo from reaching a provider CLI.

    Issues and pull/merge requests share this one validator so both surfaces
    inherit the same host, scheme, and path guarantees. The returned
    ``SourceRef.kind`` says which namespace the number belongs to; every
    pull-request-only caller must gate on it via :func:`_require_change_ref`.
    """
    if not isinstance(raw_url, str) or not raw_url or len(raw_url) > contract._MAX_URL_LENGTH:
        raise ValueError("A pull-request URL is required.")
    parsed = urlparse(raw_url.strip())
    if parsed.scheme != "https" or parsed.username or parsed.password:
        raise ValueError("Only HTTPS pull-request URLs without userinfo are supported.")
    # Strip a trailing dot so an absolute-FQDN URL (``gitlab.acme.internal.``)
    # matches the allowlist, whose entries are canonicalized the same way by the
    # config loader (:func:`_coerce_gitlab_hosts`). Without this the two sides
    # can never agree and the host is rejected (fails closed).
    host = (parsed.hostname or "").lower().rstrip(".")
    path = parsed.path.rstrip("/")

    if host in {"github.com", "www.github.com"}:
        match = _GITHUB_PATH_RE.fullmatch(path)
        if not match:
            raise ValueError(
                "Expected a GitHub URL like https://github.com/org/repo/pull/123 "
                "or https://github.com/org/repo/issues/123."
            )
        owner, repo, segment, number = match.groups()
        if owner in {".", ".."} or repo in {".", ".."}:
            raise ValueError("Invalid GitHub owner/repo path.")
        normalized = urlunparse(("https", "github.com", path, "", "", ""))
        return SourceRef(
            "github",
            normalized,
            "github.com",
            owner,
            repo,
            int(number),
            kind=_GITHUB_SEGMENT_KINDS[segment.lower()],
        )

    if host in {"gitlab.com", "www.gitlab.com"}:
        return _gitlab_ref("gitlab.com", path)

    # A self-managed instance may listen on a non-default port, so the allowlist
    # is matched against host and host:port -- an entry without a port does not
    # authorize an arbitrary port on the same host. An explicit :443 is treated
    # as absent, matching the browser URL API (which drops the default HTTPS
    # port) so the same URL resolves identically on both sides.
    port = parsed.port
    candidate = f"{host}:{port}" if port and port != 443 else host
    if host and candidate in hosts._allowed_gitlab_hosts():
        return _gitlab_ref(candidate, path)

    # Jira: Atlassian Cloud (``*.atlassian.net``) is recognized automatically --
    # the suffix is Atlassian-operated, so it identifies the product the way
    # ``github.com`` does. Self-hosted Jira / Data Center requires an exact
    # entry in ``dashboard.jira_hosts``, the same allowlist discipline as
    # self-managed GitLab and checked AFTER it so a host an operator listed as
    # GitLab is never reinterpreted as Jira.
    is_cloud_jira = host.endswith(".atlassian.net") and len(host) > len(".atlassian.net")
    if host and (is_cloud_jira or candidate in hosts._allowed_jira_hosts()):
        return _jira_ref(candidate, path)

    # Registered providers are consulted LAST, so no edition plugin can reinterpret
    # a built-in host or an operator-allowlisted one, and the three built-in
    # grammars keep exactly the precedence they had. The scheme/userinfo/length
    # checks above have already run, so a plugin never sees an unvalidated URL.
    registered = plugins._parse_registered_source_url(raw_url)
    if registered is not None:
        return registered

    raise ValueError(
        "Only github.com pull requests and issues, gitlab.com merge requests and "
        "issues, merge requests or issues on a GitLab host listed in "
        "dashboard.gitlab_hosts, and Jira issues on *.atlassian.net or a host "
        "listed in dashboard.jira_hosts are supported."
    )


def _require_change_ref(ref: SourceRef) -> SourceRef:
    """Refuse an issue ref at a pull-request-only entry point.

    Issues and pull/merge requests now come out of the same validator, so every
    pre-existing caller would otherwise accept an issue URL. That is not merely
    a wrong-shaped read: on GitHub the two namespaces share one number counter,
    so ``.../issues/58`` would be handed to ``gh pr view`` and answer about pull
    request 58 -- a different object -- and on either provider an
    owner-authenticated mutation (resolve, auto-merge, mark-ready) would be
    aimed at whatever change carries that number. Fail closed with a
    ``ValueError``, which every caller already maps to a 400.
    """
    if ref.kind != "change":
        raise ValueError("This URL points at an issue, not a pull request or merge request.")
    return ref


def _strip_git_suffix(repo: str) -> str:
    return repo[:-4] if repo.endswith(".git") else repo


def parse_repo_url(raw_url: str) -> RepoRef:
    """Validate and normalize a bare repository URL (no pull/issue number).

    Reuses the exact host, scheme, and allowlist guarantees of
    :func:`parse_source_url`: HTTPS only, no userinfo, and a host that is
    github.com, gitlab.com, or an operator-allowlisted self-managed GitLab
    instance (via :func:`ensure_gitlab_hosts_loaded`). Fails closed on every
    other host so browser input can never point a credential-bearing CLI at an
    arbitrary server. A pull/issue URL (extra path segments) is refused rather
    than silently truncated to its owner/repo root.
    """
    if not isinstance(raw_url, str) or not raw_url or len(raw_url) > contract._MAX_URL_LENGTH:
        raise ValueError("A repository URL is required.")
    parsed = urlparse(raw_url.strip())
    if parsed.scheme != "https" or parsed.username or parsed.password:
        raise ValueError("Only HTTPS repository URLs without userinfo are supported.")
    host = (parsed.hostname or "").lower().rstrip(".")
    path = parsed.path.rstrip("/")
    segments = [segment for segment in PurePosixPath(path).parts if segment not in ("", "/")]

    if host in {"github.com", "www.github.com"}:
        # A repo root is exactly /owner/repo. A pull/issue/tree URL carries more
        # segments and is not a repository root -- refuse it.
        if len(segments) != 2:
            raise ValueError("Expected a GitHub repository URL like https://github.com/owner/repo.")
        owner, repo = segments[0], _strip_git_suffix(segments[1])
        if owner in {".", ".."} or repo in {".", ".."} or not repo:
            raise ValueError("Invalid GitHub owner/repo path.")
        return RepoRef("github", "github.com", owner, repo)

    # gitlab.com and allowlisted self-managed GitLab are recognized as valid git
    # hosts so parsing succeeds, but contributor fetching is GitHub-only in v1
    # (fetch_app_contributors returns [] for a non-github provider). Only the
    # host is authorized here; the project path is not deeply validated.
    port = parsed.port
    candidate = f"{host}:{port}" if port and port != 443 else host
    is_public_gitlab = host in {"gitlab.com", "www.gitlab.com"}
    if host and (is_public_gitlab or candidate in hosts._allowed_gitlab_hosts()):
        if len(segments) < 2:
            raise ValueError(
                "Expected a GitLab repository URL like https://gitlab.com/group/project."
            )
        gitlab_host = "gitlab.com" if is_public_gitlab else candidate
        owner = "/".join(segments[:-1])
        return RepoRef("gitlab", gitlab_host, owner, _strip_git_suffix(segments[-1]))

    raise ValueError(
        "Only github.com and gitlab.com (or a GitLab host listed in "
        "dashboard.gitlab_hosts) repository URLs are supported."
    )
