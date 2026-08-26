"""Azure DevOps client for Issue Radar.

The THIRD function-for-function mirror of :mod:`github_client`, alongside
:mod:`gitlab_client`: same public names, same argument order, same return
shapes. The app's routes, caches and React components were written against
GitHub's field names, so this module normalizes Azure DevOps payloads INTO those
names rather than introducing a third vocabulary:

    Azure DevOps                     ->  normalized (GitHub-shaped)
    work item id                         number
    System.Title                         title
    System.State (template-defined)      state "open" / "closed"
    System.Tags "a; b"                   labels ["a", "b"]
    System.CommentCount                  comments
    System.CreatedBy.uniqueName          author
    pullRequestId                        number
    status active/completed/abandoned    state "open" / "closed"
    sourceRefName/targetRefName          head/base
    isDraft                              draft
    policy evaluation / build            check run

Three properties of Azure DevOps do not exist on either other provider and shape
everything below:

1. **Work items are PROJECT-scoped and carry no repository dimension.** There is
   no "issues in this repository" question to ask, so ``list_open_issues``
   ignores ``repo`` and lists the project's work items. Two repositories in one
   project legitimately return the same list.
2. **There is no filtered work-item list endpoint.** Listing is always two calls:
   a WIQL query that returns ids, then a batched hydrate of those ids.
3. **The state vocabulary belongs to the project's process template**, not to the
   platform. "Closed", "Done", "Completed" and "Resolved" are all real closing
   states in different templates, so nothing here hard-codes a state name: the
   closing states are read from the work item type's own state definitions and
   the open filter is built from that.

Auth and credential handling follow the same model as the other two clients: no
OAuth app, no PAT stored by Kiro Crew, no hosted backend. Every call shells out
to ``az devops invoke`` -- the Azure CLI's REST passthrough -- which owns its own
credential (an ``az login`` session, or ``AZURE_DEVOPS_EXT_PAT``). This module
only (a) parses a project URL safely and (b) runs ``az`` with a list argv (never
``shell=True``), so there is no shell-injection surface and no token in this
process.

``dev.azure.com`` is the ONLY reachable host. On-premises Azure DevOps Server is
out of scope because the ``azure-devops`` CLI extension does not support it at
all, so there is no credential path for it to use -- ``_resolve_host`` refuses
everything else, and refuses an empty host rather than defaulting, for the same
reason ``gitlab_client`` does: a call site that forgot the host must fail loudly
instead of silently targeting a host the caller never named.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from urllib.parse import quote, unquote, urlparse

from kiro_crew.apps.registry import minimal_env
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel

from .errors import (
    ProviderCliError,
    ProviderInvalidInputError,
    ProviderPermissionError,
    ProviderSetupError,
    PrSearchError,
    RepoUrlError,
    sanitize_cli_stderr,
)

# Historical aliases, mirroring github_client and gitlab_client so
# provider-agnostic callers can use any of the three modules interchangeably.
# ALIASES, never subclasses: ``except GhCliError`` in routes.py must catch an
# Azure failure, otherwise every one becomes a 500 instead of the 502/403 the
# route intends (see errors.py).
GhCliError = ProviderCliError
GhSetupError = ProviderSetupError
GhPermissionError = ProviderPermissionError
GhInvalidInputError = ProviderInvalidInputError

AZ_TIMEOUT_SEC = 25.0
# The work-item and pull-request listings span several calls each (WIQL + batched
# hydrate; PR pages + per-PR enrichment), so the paginating reads get a much
# larger budget than the single-shot ones. The result is cached, so the cost is
# paid once per refresh rather than per view.
AZ_PAGINATE_TIMEOUT_SEC = 150.0

# Mirrors the other two clients' constants so provider-agnostic routes can read
# any client's limits without branching.
CONTRIB_WINDOW_DAYS = 30
MAX_WINDOW_DAYS = 3650
PR_SEARCH_MAX = 300

# The one supported host. Azure DevOps Server (on-premises) is out of scope.
AZURE_HOST = "dev.azure.com"
# Legacy per-organization host, accepted when PARSING a pasted URL and
# canonicalized to AZURE_HOST so one organization cannot become two identities.
_LEGACY_HOST_SUFFIX = ".visualstudio.com"

# ``--api-version`` is MANDATORY on every ``az devops invoke`` call and differs
# per endpoint: the CLI's own default is 5.0, which predates most of what this
# module reads (and answers 400 for the rest). There is deliberately no single
# global version -- several of these resources exist only as previews, and
# sending a GA version to them fails, while sending a preview version to a GA
# resource pins us to a shape Microsoft may change.
_API_GIT = "7.1"
_API_WIT = "7.1"
_API_BUILD = "7.1"
_API_CORE = "7.1"
# Preview-only resources. The suffix is part of the version string, not a flag.
_API_WIT_COMMENTS = "7.1-preview.4"
_API_WIT_TAGS = "7.1-preview.1"
_API_WIT_STATES = "7.1-preview.1"
_API_POLICY = "7.1-preview.1"
_API_IDENTITY = "7.1-preview.1"

# Azure list responses come back as ``{"count": n, "value": [...]}`` and are
# paged with ``$top``/``$skip`` rather than a page number.
_PAGE_SIZE = 100
# Hard cap on pages walked by one paginated read, so a pathological project
# cannot make a single request loop indefinitely.
_MAX_PAGES = 40
# Work-item comments are their own paged resource, keyed by a continuation token
# rather than $skip, and Azure caps this page at 200.
_COMMENT_PAGE_SIZE = 200
# Ceiling on ids one WIQL query returns. WIQL's own default cap is far higher;
# this bounds the hydrate that follows, which is the expensive half.
_WIQL_TOP = 2000
# Documented per-call id cap on the work-item batch endpoint. Exceeding it is a
# 400, so ids are chunked at this size and the results stitched.
_BATCH_MAX_IDS = 200
# Pull requests whose card enrichment (policy evaluations) is fetched per list
# refresh. Rows past this keep ``checks_counts: None``, which keeps them OUT of
# the on-disk cache via ``enrichment_complete`` rather than caching a zeroed
# check state as authoritative.
_ENRICH_MAX_PULLS = 60
# Builds scanned when resolving "the runs for commit X". The builds endpoint has
# no commit filter, so the newest N are read and matched on ``sourceVersion``.
_BUILD_SCAN_TOP = 200
# Pull requests scanned when resolving "the PR whose head is commit X" -- needed
# because Azure keys policy evaluations by PR while the shared signature keys
# checks by sha.
_PR_LOOKUP_TOP = 200

# Azure has no colour on a work-item tag, so one is synthesized. Same neutral
# default the other two clients fall back to, so a tag renders like a label.
_SYNTHETIC_LABEL_COLOR = "888888"

# Conservative charset for an organization / project / repository segment.
# Azure allows spaces in project and repository names, so a space is permitted
# (values reach a subprocess as their own argv element, never a shell string),
# but nothing that could act as a path or query separator is.
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")
# Segments that are part of Azure's own routing rather than a name.
_RESERVED_SEGMENTS = frozenset({"_git", "_apis", "_workitems", "_build", "_settings", "_apps"})
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
# A GUID as Azure returns identity ids. Validated before one reaches an argv or a
# query string, since it is the one value here that comes from a prior response.
_GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
# A login as the person filters accept it: an Azure unique name is usually an
# email/UPN, so "@" and "+" are legal where they would not be on the other
# providers.
_LOGIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+@' -]{0,254}$")


def _bad_segment(segment: str) -> bool:
    """Whether a path segment is unusable as an org / project / repo name."""
    if not segment or segment in (".", ".."):
        return True
    if segment.lower() in _RESERVED_SEGMENTS:
        return True
    return not _SEGMENT_RE.match(segment)


_URL_PATH_SEPARATOR = re.compile(r"/+")


def _url_path_segments(path: str) -> list[str]:
    """The non-empty segments of a URL path.

    A URL path separator is ``/`` on every host platform, so this deliberately
    does NOT go through ``pathlib``/``os.path``: those rewrite the separator on
    Windows and would take the URL apart with the wrong one. Runs of separators
    collapse, so a doubled ``//`` yields no empty segment for a caller to mistake
    for a real one.
    """
    return [segment for segment in _URL_PATH_SEPARATOR.split(path or "") if segment]


def parse_azure_repo_url(link: str) -> tuple[str, str]:
    """Parse ``("{organization}/{project}", repository)`` from an Azure DevOps URL.

    Accepts the three forms a user can paste:

    * ``https://dev.azure.com/{org}/{project}/_git/{repo}`` -- the repository page
    * ``https://dev.azure.com/{org}/{project}`` -- the project page, where the
      repository defaults to the project name (Azure creates one so named)
    * ``https://{org}.visualstudio.com/{project}/_git/{repo}`` -- the legacy host,
      canonicalized to the modern identity so one organization does not end up
      with two cache trees

    A trailing ``/``, a ``.git`` suffix, and any deeper path or query after the
    repository segment (``/pullrequest/12``, ``?path=/src``) are IGNORED rather
    than folded into the name -- users paste whichever tab they are on.

    Deliberately strict, for the same reasons as the other two parsers: full URL
    only, HTTPS only, no userinfo, host must be Azure's, and every segment is
    charset-constrained before any value can reach a subprocess argv.
    """
    if not link or not isinstance(link, str):
        raise RepoUrlError("repo link is empty")
    try:
        parsed = urlparse(link.strip())
    except ValueError as exc:
        raise RepoUrlError(f"unparseable URL: {link!r}") from exc
    if parsed.scheme != "https":
        raise RepoUrlError(f"not an https URL: {link!r}")
    if parsed.username or parsed.password:
        raise RepoUrlError("URLs with embedded credentials are not accepted")
    # `hostname`/`port` parse the authority lazily, so a malformed one raises
    # HERE rather than in urlparse; both are client input, so both become the
    # same RepoUrlError the connect route maps to a 400.
    try:
        host = (parsed.hostname or "").lower().rstrip(".")
        _ = parsed.port
    except ValueError as exc:
        raise RepoUrlError(f"malformed host or port in {link!r}") from exc

    legacy_org = ""
    if host == AZURE_HOST:
        pass
    elif host.endswith(_LEGACY_HOST_SUFFIX):
        # The organization is the host's first label on the legacy form, so it is
        # taken from there rather than from the path.
        legacy_org = host[: -len(_LEGACY_HOST_SUFFIX)]
        if _bad_segment(legacy_org):
            raise RepoUrlError(f"invalid organization in {link!r}")
    else:
        raise RepoUrlError(
            f"not a supported Azure DevOps host: {link!r} -- only {AZURE_HOST} and the "
            "legacy {org}.visualstudio.com form are accepted (Azure DevOps Server is not supported)"
        )

    # Percent-decoded per segment, because a project or repository name may
    # legitimately contain a space. Decoding cannot be allowed to reintroduce a
    # separator, so a decoded segment carrying one is refused outright.
    raw_parts = _url_path_segments(parsed.path or "")
    parts: list[str] = []
    for raw in raw_parts:
        seg = unquote(raw)
        if "/" in seg or "\\" in seg or "?" in seg or "#" in seg:
            raise RepoUrlError(f"invalid path segment in {link!r}")
        parts.append(seg)

    if legacy_org:
        org = legacy_org
    else:
        if not parts:
            raise RepoUrlError(f"not a full Azure DevOps URL: {link!r}")
        org, parts = parts[0], parts[1:]
    if not parts:
        raise RepoUrlError(
            f"not a full Azure DevOps URL: {link!r} (expected .../{{project}} or .../{{project}}/_git/{{repo}})"
        )
    project, parts = parts[0], parts[1:]

    # Everything from the `_git` marker onward addresses a repository; without
    # the marker the URL names the project, whose default repository shares its
    # name. Anything after the repository segment is a page within it.
    if parts and parts[0] == "_git":
        repo = parts[1] if len(parts) > 1 else project
    elif parts and parts[0].lower() in _RESERVED_SEGMENTS:
        # A project-level page that is not a repository page (boards, pipelines).
        repo = project
    else:
        repo = project
    repo = re.sub(r"\.git$", "", repo)

    for seg in (org, project, repo):
        if _bad_segment(seg):
            raise RepoUrlError(f"invalid path segment in {link!r}")
    return f"{org}/{project}", repo


def _split_owner(owner: str) -> tuple[str, str]:
    """``"{org}/{project}"`` -> ``(org, project)``, split on the FIRST ``/`` only.

    Azure's ``owner`` carries two independent names, and neither may contain a
    slash, so a single split is exact. An owner with no ``/`` cannot address
    anything -- every route below needs both halves -- so it is refused rather
    than guessed at (defaulting the project to the organization would read a
    different project's work items).
    """
    text = str(owner or "").strip().strip("/")
    org, sep, project = text.partition("/")
    if not sep:
        raise ProviderCliError(
            f"an Azure DevOps owner must be '{{organization}}/{{project}}' (got {owner!r})"
        )
    org, project = org.strip(), project.strip()
    if _bad_segment(org) or _bad_segment(project):
        raise ProviderCliError(f"invalid Azure DevOps organization/project: {owner!r}")
    return org, project


def _check_repo(repo: str) -> str:
    """Validate a repository name before it reaches a route parameter."""
    name = str(repo or "").strip()
    if _bad_segment(name):
        raise ProviderCliError(f"invalid Azure DevOps repository name: {repo!r}")
    return name


def _org_url(org: str) -> str:
    """The ``--org`` value for an organization, always on the pinned host."""
    return f"https://{AZURE_HOST}/{quote(org, safe='')}"


# ── az spawn hardening ───────────────────────────────────────────────────────
#
# Mirrors gitlab_client's model exactly. ``az`` needs the user's OWN
# authenticated session and cannot be sandbox-routed (the sandbox would hide
# ~/.azure and break auth). As defense in depth every spawn goes through
# ``_az_run``, which (1) resolves ``az`` through the shared provider policy --
# accepting the user's own install but refusing one owned by another user, a
# world-writable one, or one inside the agent-writable project tree -- and (2)
# hands the child a MINIMAL environment (PATH/HOME/XDG plus az's own auth and
# network vars) instead of the gateway's full env, so unrelated secrets
# (AWS/Slack/SSH) can never reach a substituted or compromised az.
_AZ_ENV_PASSTHROUGH = (
    # The Azure DevOps extension's own credential, and az's config/extension roots
    # (which is where the `az login` session lives).
    "AZURE_DEVOPS_EXT_PAT",
    "AZURE_CONFIG_DIR",
    "AZURE_EXTENSION_DIR",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "all_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
)

_az_bin_cache: str | None = None


def _az_bin() -> str:
    """Absolute path to an acceptable ``az``, resolved once and cached.

    Resolution and validation are shared with the Sidebar PR panel
    (``source_providers.provider_executable_candidates`` +
    ``_validate_provider_executable``), exactly as ``gitlab_client._glab_bin``
    does for ``glab``: the well-known install dirs first, then the ambient
    ``PATH``, accepting the user's own install (Homebrew, apt, pipx) while
    refusing a binary owned by another user, a world-writable one, or one inside
    the agent-writable project tree.

    Set ``KIROCREW_ISSUE_RADAR_AZ`` to an absolute path to override (still
    validated), or ``KIROCREW_PROVIDER_BIN_STRICT=1`` to require a system-dir
    binary. ``az`` carries the same well-known-directory entries as ``gh`` and
    ``glab`` (``github_runner.PROVIDER_EXECUTABLE_CANDIDATES``), so strict mode
    resolves a packaged ``/usr/bin/az`` exactly as it does for the other two; what
    it hides is a user-owned install on ``PATH`` alone -- Homebrew, pipx, mise --
    which is what the override is for.
    """
    global _az_bin_cache
    if _az_bin_cache:
        return _az_bin_cache
    if sys.platform == "win32":
        raise ProviderCliError(
            "the Azure DevOps provider requires a POSIX platform (macOS/Linux); "
            "Windows is not supported -- use WSL to run the Kiro Crew gateway. "
            "GitHub and GitLab repositories do work on Windows"
        )

    # Deferred, matching gitlab_client._glab_bin: importing the dashboard handler
    # package pulls in the whole dashboard (~750 modules), and this module is also
    # loaded by the MCP server and the CLI, where none of it is wanted. Not a
    # circular import -- source_providers reaches nothing under issue_radar -- so
    # the only cost of hoisting it would be that import weight, paid by every
    # consumer whether or not it ever resolves a binary.
    from kiro_crew.dashboard.handlers.source_providers import (
        _validate_provider_executable,
        provider_executable_candidates,
    )

    override = os.environ.get("KIROCREW_ISSUE_RADAR_AZ")
    if override:
        try:
            validated = _validate_provider_executable(override)
            _az_bin_cache = validated
            return validated
        except (ValueError, OSError) as exc:
            raise ProviderSetupError(
                f"KIROCREW_ISSUE_RADAR_AZ={override!r} failed validation: {exc}",
                reason="not_installed",
            ) from exc

    last_error = ""
    for cand in provider_executable_candidates("az"):
        if not os.path.isfile(cand):
            continue
        try:
            validated = _validate_provider_executable(cand)
            _az_bin_cache = validated
            return validated
        except (ValueError, OSError) as exc:
            last_error = str(exc)
            continue  # untrusted provenance -- skip

    detail = f" (last check: {last_error})" if last_error else ""
    raise ProviderSetupError(
        "the `az` CLI was not found on this host"
        f"{detail} -- install the Azure CLI, add the Azure DevOps extension "
        "(`az extension add --name azure-devops`) and run `az login`, or set "
        "KIROCREW_ISSUE_RADAR_AZ to an absolute az path",
        reason="not_installed",
    )


def _resolve_host(host: str) -> str:
    """Re-check ``host`` at the spawn boundary; only ``dev.azure.com`` passes.

    Azure's host is pinned by ``provider.normalize_host``, but it is re-checked
    here so a corrupted config entry or a future code path that forgets to
    normalize cannot reach another server with the user's credential. An omitted
    host is refused rather than defaulted, mirroring
    ``gitlab_client._resolve_host``: a call that forgot the host must fail loudly
    instead of silently targeting a host the caller never named.

    Azure DevOps Server (on-premises) is refused because the ``azure-devops``
    extension does not support it at all -- there is no credential path for it,
    so accepting the host would only produce a confusing CLI failure.
    """
    if not host:
        raise ProviderCliError("an Azure DevOps host is required for az calls")
    normalized = host.lower().rstrip(".")
    if normalized != AZURE_HOST:
        raise ProviderCliError(
            f"Azure DevOps host {normalized!r} is not supported -- only {AZURE_HOST} "
            "(Azure DevOps Server / on-premises has no supported credential path)"
        )
    return normalized


def _az_env(host: str) -> dict[str, str]:
    """A minimal environment for ``az``: the platform's safe-key base plus az's
    own auth and network/TLS vars when set -- NOT the gateway's full environment.

    ``AZURE_DEVOPS_EXT_PAT`` is forwarded only for the pinned cloud host. It is a
    single ambient credential with no host binding, so forwarding it to anything
    else would hand a dev.azure.com token to that server.

    ``AZURE_EXTENSION_USE_DYNAMIC_INSTALL=no`` is a security setting, not a
    convenience one: with dynamic install enabled, a call naming an unknown
    command group makes az download and execute an extension wheel. An automated
    spawn must never install code, so a missing extension has to surface as an
    error the user resolves themselves.
    """
    passthrough = {k: os.environ[k] for k in _AZ_ENV_PASSTHROUGH if k in os.environ}
    if host != AZURE_HOST:
        passthrough.pop("AZURE_DEVOPS_EXT_PAT", None)
    passthrough["AZURE_EXTENSION_USE_DYNAMIC_INSTALL"] = "no"
    passthrough["AZURE_CORE_COLLECT_TELEMETRY"] = "0"
    passthrough["AZURE_CORE_NO_COLOR"] = "1"
    passthrough["NO_COLOR"] = "1"
    return minimal_env(**passthrough)


def _audit(op: str, target: str, outcome: str, *, error: str = "") -> None:
    """SEL event for an az spawn.

    The ``invoked`` record is written with ``critical=True``, which is what makes
    it a gate rather than a hope: the default path ENQUEUES the event and returns
    successfully even when the log cannot be written, so a pre-spawn audit without
    it would still let a provider mutation run unrecorded. ``critical`` writes
    synchronously and re-raises the filesystem failure, so :func:`_az_run` can
    refuse.

    The post-execution ``ok`` / failure records stay on the default enqueue path.
    By then the command has already run, so raising would replace the caller's real
    error with a logging one and change nothing about what happened.
    """
    sel().log_api_access(
        caller="core:issue-radar",
        operation=f"issue_radar.{op}",
        outcome=outcome,
        source="builtin-app",
        resources=target[:200],
        error=error[:200] if error else "",
        critical=outcome == "invoked",
    )


def _az_run(argv: list[str], *, host: str, timeout: float) -> subprocess.CompletedProcess:
    """Single spawn chokepoint for every ``az`` call -- replaces argv[0] with the
    trusted canonical az and passes the minimal env for the resolved host.

    Order matters: the host is re-resolved, the binary is re-validated, and the
    ``invoked`` audit is written, all BEFORE anything executes. A failure at any of
    those three points means no spawn happens.
    """
    resolved_host = _resolve_host(host)
    az = _az_bin()
    operation = f"az {' '.join(argv[1:3])}"  # e.g. "az devops invoke" (bounded)
    try:
        _audit("az_run", operation, "invoked")
    except Exception as exc:
        # Refuse rather than run unrecorded. Surfaced as a provider error so the
        # route answers 502 instead of an opaque 500.
        raise ProviderCliError(
            "refusing to run an az command because the security event log could "
            f"not record it: {exc}"
        ) from exc
    try:
        proc = subprocess.run(
            [az, *argv[1:]],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=_az_env(resolved_host),
        )
    except FileNotFoundError as exc:  # pragma: no cover -- _az_bin guards first
        _audit("az_run", operation, "failure", error="az not found")
        raise ProviderSetupError(
            "the `az` CLI is not installed on this host", reason="not_installed"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        _audit("az_run", operation, "failure", error=f"timeout after {timeout}s")
        raise ProviderCliError(f"`az` timed out after {timeout}s") from exc
    if proc.returncode != 0:
        _audit("az_run", operation, "failure", error=f"exit {proc.returncode}")
    else:
        _audit("az_run", operation, "ok")
    return proc


# Markers az prints when it has no usable credential for Azure DevOps, matched
# case-insensitively against the stderr tail so the connect dialog can offer a
# login instruction instead of an opaque exit code. TF400813 is Azure DevOps's
# own "the user is not authorized" code, which it also returns for an expired or
# absent session.
_AZ_AUTH_MARKERS = (
    "az login",
    "az devops login",
    "tf400813",
    "please run 'az login'",
    "unauthorized",
    "401",
    "before you can run this command you need to log in",
)

# Markers that mean the CLI or its azure-devops EXTENSION is missing. A missing
# extension is reported as ``not_installed`` too: the fix is the same class of
# user action (install something), and the connect dialog names it.
_AZ_MISSING_MARKERS = (
    'is not in the "az" command group',
    "az extension add",
    "extension is not installed",
    "no such command",
    "command not found",
)


def _raise_if_setup_failure(stderr_tail: str) -> None:
    """Re-classify a missing-CLI / missing-extension / unauthenticated failure.

    Order matters: the missing-extension check runs FIRST, because az's message
    for an absent extension can also mention logging in, and telling a user to
    authenticate a CLI that cannot serve the command at all sends them down the
    wrong path.
    """
    low = (stderr_tail or "").lower()
    if any(m in low for m in _AZ_MISSING_MARKERS):
        raise ProviderSetupError(
            "the `azure-devops` extension for the `az` CLI is not available -- "
            "install it with `az extension add --name azure-devops`",
            reason="not_installed",
        )
    if any(m in low for m in _AZ_AUTH_MARKERS):
        raise ProviderSetupError(
            f"the `az` CLI is not authenticated for {AZURE_HOST} -- run `az login` "
            "(or `az devops login` with a personal access token)",
            reason="not_authenticated",
        )


def _stderr_tail(proc: subprocess.CompletedProcess) -> str:
    return sanitize_cli_stderr(" ".join((proc.stderr or "").strip().splitlines()[-3:]))


def _is_forbidden(tail: str) -> bool:
    low = (tail or "").lower()
    return "403" in low or "forbidden" in low or "does not have permission" in low


def _body_file(body: object) -> str:
    """Write a request body to a fresh 0600 temp file and return its path.

    Azure's REST passthrough takes a body from a FILE (``--in-file``), not from
    stdin, so a body has to be materialized on disk. Two properties matter:

    * The mode is 0600 from creation (``mkstemp`` guarantees it), so a body is
      never briefly world-readable -- bodies here carry comment prose and, for a
      merge, the commit a merge is pinned to.
    * The name is UNIQUE per call. A fixed name would let two concurrent requests
      overwrite each other's body, so a merge could complete with another call's
      payload, and would be a symlink-attack target in a shared temp directory.

    The caller deletes it in a ``finally``.
    """
    fd, path = tempfile.mkstemp(prefix="kirocrew-az-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(body, handle)
    except Exception:
        os.unlink(path)
        raise
    return path


def _az_invoke(
    *,
    org: str,
    area: str,
    resource: str,
    host: str,
    timeout: float = AZ_TIMEOUT_SEC,
    route: dict[str, object] | None = None,
    query: dict[str, object] | None = None,
    method: str = "GET",
    body: object | None = None,
    api_version: str,
    media_type: str = "",
) -> object:
    """Run one ``az devops invoke`` call and parse the JSON response.

    ``az devops invoke`` is the Azure CLI's generic REST passthrough: the
    ``area``/``resource`` pair is resolved against the organization's own
    ``_apis`` resource-location document, so an unsupported pair fails loudly
    instead of silently addressing a different endpoint.

    ``route`` supplies the route template's placeholders (``project``,
    ``repositoryId``, ``pullRequestId``, ...) and ``query`` the query string.
    Both are passed as separate argv elements -- never assembled into a shell
    string -- so a value containing a space or an ``&`` cannot smuggle in another
    parameter.

    ``--detect false`` is always passed: detection would try to infer the
    organization from the current git remote, and the cwd of a gateway process is
    unrelated to the project being read.
    """
    argv = [
        "az",
        "devops",
        "invoke",
        "--org",
        _org_url(org),
        "--area",
        area,
        "--resource",
        resource,
        "--http-method",
        method,
        "--api-version",
        api_version,
        "--detect",
        "false",
        "--output",
        "json",
    ]
    if route:
        argv.append("--route-parameters")
        argv.extend(f"{key}={value}" for key, value in route.items())
    if query:
        argv.append("--query-parameters")
        argv.extend(f"{key}={value}" for key, value in query.items())
    if media_type:
        argv += ["--media-type", media_type]

    path = ""
    try:
        if body is not None:
            path = _body_file(body)
            argv += ["--in-file", path]
        proc = _az_run(argv, host=host, timeout=timeout)
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass

    target = f"{area}/{resource}"
    if proc.returncode != 0:
        tail = _stderr_tail(proc)
        _raise_if_setup_failure(tail)
        if _is_forbidden(tail):
            raise ProviderPermissionError(
                f"az devops invoke {target} was forbidden (exit {proc.returncode}): {tail}"
            )
        raise ProviderCliError(f"az devops invoke {target} failed (exit {proc.returncode}): {tail}")
    text = (proc.stdout or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProviderCliError(f"az returned unexpected output for {target}") from exc


def _az_invoke_paged(
    *,
    org: str,
    area: str,
    resource: str,
    host: str,
    timeout: float,
    route: dict[str, object] | None = None,
    query: dict[str, object] | None = None,
    api_version: str,
    limit: int = 0,
) -> list[dict]:
    """Walk ``$top``/``$skip`` over a list endpoint and concatenate the values.

    Azure pages by offset rather than by page number, and reports no "next page"
    link on most of these routes, so a short page is the end-of-data signal.
    Bounded by :data:`_MAX_PAGES` so one request cannot loop indefinitely on a
    pathological project, and by ``limit`` when the caller wants fewer.
    """
    out: list[dict] = []
    skip = 0
    for _ in range(_MAX_PAGES):
        top = _PAGE_SIZE
        if limit:
            remaining = limit - len(out)
            if remaining <= 0:
                break
            top = min(_PAGE_SIZE, remaining)
        page_query = dict(query or {})
        page_query["$top"] = top
        page_query["$skip"] = skip
        rows = _values(
            _az_invoke(
                org=org,
                area=area,
                resource=resource,
                host=host,
                timeout=timeout,
                route=route,
                query=page_query,
                api_version=api_version,
            )
        )
        out.extend(rows)
        if len(rows) < top:
            break
        skip += top
    return out


def _obj(data: object) -> dict:
    return data if isinstance(data, dict) else {}


def _values(data: object) -> list[dict]:
    """The rows of an Azure list response.

    Azure wraps a collection in ``{"count": n, "value": [...]}``, but a few
    routes answer with a bare array, so both shapes are accepted.
    """
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    inner = _obj(data).get("value")
    if isinstance(inner, list):
        return [row for row in inner if isinstance(row, dict)]
    return []


# ── normalization: Azure DevOps -> the GitHub-shaped vocabulary the app speaks ─


def _identity_login(raw: object) -> str | None:
    """An Azure identity reference -> a login string.

    ``uniqueName`` is the stable, human-typeable handle (usually an email/UPN) and
    is what the person filters match on, so it is preferred; ``displayName`` is
    the fallback for a service identity that has none.
    """
    if not isinstance(raw, dict):
        return None
    name = raw.get("uniqueName") or raw.get("displayName") or raw.get("principalName")
    text = str(name).strip() if name else ""
    return text or None


def _identity_logins(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [login for login in (_identity_login(item) for item in raw) if login]


def _identity_id(raw: object) -> str | None:
    """The GUID of an identity reference, validated before it can reach an argv."""
    if not isinstance(raw, dict):
        return None
    value = str(raw.get("id") or "").strip()
    return value if _GUID_RE.match(value) else None


def _tag_names(raw: object) -> list[str]:
    """``System.Tags`` -> a list of tag names.

    Azure stores tags as ONE delimited string (``"needs-triage; blocked"``), so
    this is a parse, not a field read. Tags are case sensitive and may contain
    neither ``,`` nor ``;`` -- the delimiter is why -- and Azure normalizes the
    separator to ``"; "`` on write.
    """
    text = str(raw or "").strip()
    if not text:
        return []
    return [part.strip() for part in text.split(";") if part.strip()]


def _tags_field(names: list[str]) -> str:
    """A tag list -> the delimited ``System.Tags`` string Azure stores."""
    return "; ".join(names)


def _check_label(label: str) -> str:
    """Validate one tag name before it is written into ``System.Tags``.

    ``,`` and ``;`` are the delimiters Azure splits the field on, so a name
    containing either would silently become two tags -- or corrupt a neighbouring
    one -- on the read-modify-write below. Refusing is the only honest answer:
    there is no escaping mechanism to fall back on.
    """
    name = str(label or "").strip()
    if not name:
        raise ProviderCliError("a tag name cannot be empty")
    if "," in name or ";" in name:
        raise ProviderCliError(
            f"Azure DevOps tags cannot contain ',' or ';' (got {label!r}) -- those are "
            "the delimiters the tag field is stored with"
        )
    if len(name) > 400:
        raise ProviderCliError(f"tag name is too long: {label!r}")
    return name


def _shape_labels(names: list[str]) -> list[dict]:
    """Tag names -> ``[{name, color, description}]``.

    The colour is SYNTHETIC: an Azure work-item tag has no colour and no
    description, so every tag renders in the same neutral colour the other two
    clients fall back to for an uncoloured label.
    """
    return [{"name": name, "color": _SYNTHETIC_LABEL_COLOR, "description": ""} for name in names]


def _work_item_url(org: str, project: str, number: int) -> str:
    """The web URL of a work item.

    Synthesized rather than read: the batch hydrate selects FIELDS, and Azure
    only returns ``_links`` for a full (unselected) read, so asking for the link
    would double the payload of every list refresh.
    """
    return f"https://{AZURE_HOST}/{quote(org, safe='')}/{quote(project, safe='')}/_workitems/edit/{int(number)}"


def _field(fields: dict, name: str, default: object = None) -> object:
    value = fields.get(name)
    return default if value is None else value


def _norm_issue(raw: dict, *, org: str, project: str, closed_states: frozenset[str]) -> dict:
    """One work item -> the list-view row shape ``github_client._ISSUE_JQ`` produces.

    ``author_association`` is ``None``: Azure has no computed
    contributor-relationship concept at all, so reporting one would be fiction.
    It only feeds :func:`derive_members`, which is honest about returning nothing.

    ``reactions``/``thumbs_up`` are ``0`` rather than ``None`` because a work item
    carries no reaction data of any kind -- zero is the true count, not an
    unknown.
    """
    fields = _obj(raw.get("fields"))
    number = raw.get("id")
    state = str(_field(fields, "System.State", "") or "")
    return {
        "number": number,
        "title": str(_field(fields, "System.Title", "") or ""),
        "url": _work_item_url(org, project, int(number)) if isinstance(number, int) else "",
        "labels": _tag_names(fields.get("System.Tags")),
        "comments": _field(fields, "System.CommentCount", 0),
        "reactions": 0,
        "thumbs_up": 0,
        "author_association": None,
        "updated_at": _field(fields, "System.ChangedDate"),
        "created_at": _field(fields, "System.CreatedDate"),
        "state": "closed" if state in closed_states else "open",
        "author": _identity_login(fields.get("System.CreatedBy")),
        # Azure allows exactly ONE assignee per work item, so the list is either
        # empty or a single entry -- the plural key is the app's shape, not a claim
        # that Azure supports several.
        "assignees": [
            login for login in (_identity_login(fields.get("System.AssignedTo")),) if login
        ],
        "body": str(_field(fields, "System.Description", "") or ""),
    }


def _norm_issue_detail(raw: dict, *, org: str, project: str, closed_states: frozenset[str]) -> dict:
    """One work item -> the detail-pane shape ``_ISSUE_DETAIL_JQ`` produces.

    ``state_reason`` is ``None``: Azure's closing REASON (``System.Reason``) is a
    process-template value ("Fixed", "Duplicate", "As Designed") with no mapping
    onto GitHub's two-valued ``completed``/``not_planned``, and inventing one
    would put a value the UI renders as a verdict behind a guess.

    ``locked`` is always ``False`` -- a work item has no discussion lock -- and
    ``milestone`` reports the iteration path, which is the closest Azure analogue
    of a milestone and is what a triage reader is looking for.
    """
    fields = _obj(raw.get("fields"))
    number = raw.get("id")
    state = str(_field(fields, "System.State", "") or "")
    is_closed = state in closed_states
    iteration = str(_field(fields, "System.IterationPath", "") or "")
    return {
        "number": number,
        "title": str(_field(fields, "System.Title", "") or ""),
        "body": str(_field(fields, "System.Description", "") or ""),
        "state": "closed" if is_closed else "open",
        "state_reason": None,
        "url": _work_item_url(org, project, int(number)) if isinstance(number, int) else "",
        "author": _identity_login(fields.get("System.CreatedBy")),
        "author_association": None,
        "created_at": _field(fields, "System.CreatedDate"),
        "updated_at": _field(fields, "System.ChangedDate"),
        # Azure records no separate close timestamp unless the template defines
        # Microsoft.VSTS.Common.ClosedDate, so it is read when present and the
        # last change is NOT substituted for it -- that would date the close to
        # whatever happened most recently.
        "closed_at": _field(fields, "Microsoft.VSTS.Common.ClosedDate") if is_closed else None,
        "closed_by": (
            _identity_login(fields.get("Microsoft.VSTS.Common.ClosedBy")) if is_closed else None
        ),
        "comments": _field(fields, "System.CommentCount", 0),
        "locked": False,
        "labels": _shape_labels(_tag_names(fields.get("System.Tags"))),
        "assignees": [
            login for login in (_identity_login(fields.get("System.AssignedTo")),) if login
        ],
        "milestone": (
            {"title": iteration.rsplit("\\", 1)[-1], "state": None, "due_on": None}
            if iteration
            else None
        ),
        "reactions": None,
    }


# ── project metadata: ids, identities, and the template's own state names ─────
#
# Three facts are needed repeatedly and are IMMUTABLE for the life of a process
# (a project's GUID never changes; a work item type's state definitions change
# only when an administrator edits the process), so they are cached per
# organization/project. The cache is keyed on the validated names, never on
# caller input verbatim, and holds no credential.
_project_id_cache: dict[tuple[str, str], str] = {}
_closed_states_cache: dict[tuple[str, str, str], frozenset[str]] = {}
_identity_cache: dict[str, dict] = {}

# Fallback closing states, used only when the project's own state definitions
# cannot be read. Deliberately the union of the closing states of the templates
# Microsoft ships (Agile, Scrum, CMMI, Basic), because a wrong answer here means
# a closed work item shows as open in the triage list.
_FALLBACK_CLOSED_STATES = frozenset({"Closed", "Done", "Completed", "Removed", "Resolved"})
# The state CATEGORIES that mean "not open". Categories are the template-agnostic
# layer Azure guarantees: every custom state maps into one of Proposed /
# InProgress / Resolved / Completed / Removed, so keying on them is what lets this
# module work on a custom process without knowing its state names.
_CLOSED_STATE_CATEGORIES = frozenset({"Completed", "Removed"})


def _project_id(org: str, project: str, *, host: str, timeout: float) -> str:
    """The project's GUID, which the policy-evaluation artifact id is built from."""
    cached = _project_id_cache.get((org, project))
    if cached:
        return cached
    data = _obj(
        _az_invoke(
            org=org,
            area="core",
            resource="projects",
            host=host,
            timeout=timeout,
            route={"projectId": project},
            api_version=_API_CORE,
        )
    )
    value = str(data.get("id") or "").strip()
    if not _GUID_RE.match(value):
        raise ProviderCliError(f"could not resolve the project id for {org}/{project}")
    _project_id_cache[(org, project)] = value
    return value


def _closed_state_names(
    org: str, project: str, work_item_type: str, *, host: str, timeout: float
) -> frozenset[str]:
    """The state names that mean "not open" for one work item type.

    Read from the type's OWN state definitions and selected by state CATEGORY, so
    a custom process whose closing state is called "Shipped" is handled without
    this module knowing the name. Falls back to
    :data:`_FALLBACK_CLOSED_STATES` when the definitions cannot be read (an older
    server, or a caller without process read access) rather than treating every
    state as open.
    """
    key = (org, project, work_item_type)
    cached = _closed_states_cache.get(key)
    if cached is not None:
        return cached
    try:
        rows = _values(
            _az_invoke(
                org=org,
                area="wit",
                resource="states",
                host=host,
                timeout=timeout,
                route={"project": project, "type": work_item_type},
                api_version=_API_WIT_STATES,
            )
        )
    except ProviderCliError:
        return _FALLBACK_CLOSED_STATES
    names = {
        str(row.get("name"))
        for row in rows
        if row.get("name") and str(row.get("category") or "") in _CLOSED_STATE_CATEGORIES
    }
    resolved = frozenset(names) if names else _FALLBACK_CLOSED_STATES
    _closed_states_cache[key] = resolved
    return resolved


def _project_closed_states(org: str, project: str, *, host: str, timeout: float) -> frozenset[str]:
    """Closing state names across every work item type in the project.

    A WIQL query spans types, so the open filter needs the UNION of the closing
    states of all of them: a state that closes a Bug but not a Task must still be
    excluded when the query returns both.
    """
    try:
        types = _values(
            _az_invoke(
                org=org,
                area="wit",
                resource="workitemtypes",
                host=host,
                timeout=timeout,
                route={"project": project},
                api_version=_API_WIT,
            )
        )
    except ProviderCliError:
        return _FALLBACK_CLOSED_STATES
    names: set[str] = set()
    for row in types:
        for state in _values(row.get("states")):
            if state.get("name") and str(state.get("category") or "") in _CLOSED_STATE_CATEGORIES:
                names.add(str(state["name"]))
    return frozenset(names) if names else _FALLBACK_CLOSED_STATES


def _open_state_name(
    org: str, project: str, work_item_type: str, *, host: str, timeout: float
) -> str:
    """The state to reopen a work item INTO, for its own type.

    Chosen by category rather than by name: the first ``Proposed`` state is the
    template's own entry state ("New" on Agile, "To Do" on Basic), so reopening
    lands where a freshly created item would rather than in a state the template
    may not even define.
    """
    try:
        rows = _values(
            _az_invoke(
                org=org,
                area="wit",
                resource="states",
                host=host,
                timeout=timeout,
                route={"project": project, "type": work_item_type},
                api_version=_API_WIT_STATES,
            )
        )
    except ProviderCliError as exc:
        raise ProviderCliError(
            f"cannot reopen a {work_item_type}: the project's state definitions are unreadable, "
            "so the state to reopen into is unknown"
        ) from exc
    for category in ("Proposed", "InProgress"):
        for row in rows:
            if str(row.get("category") or "") == category and row.get("name"):
                return str(row["name"])
    raise ProviderCliError(f"the {work_item_type} type defines no open state to reopen into")


def _closing_state_name(
    org: str, project: str, work_item_type: str, *, host: str, timeout: float
) -> str:
    """The state to close a work item INTO, for its own type (category ``Completed``).

    ``Removed`` is deliberately not used as a fallback: it is Azure's "this should
    never have existed" state, which is a different act from closing and on some
    templates hides the item from the board entirely.
    """
    try:
        rows = _values(
            _az_invoke(
                org=org,
                area="wit",
                resource="states",
                host=host,
                timeout=timeout,
                route={"project": project, "type": work_item_type},
                api_version=_API_WIT_STATES,
            )
        )
    except ProviderCliError as exc:
        raise ProviderCliError(
            f"cannot close a {work_item_type}: the project's state definitions are unreadable, "
            "so the closing state is unknown"
        ) from exc
    for row in rows:
        if str(row.get("category") or "") == "Completed" and row.get("name"):
            return str(row["name"])
    raise ProviderCliError(f"the {work_item_type} type defines no completed state")


def _current_identity(org: str, *, host: str, timeout: float) -> dict:
    """``{"id": guid, "login": str}`` for the authenticated ``az`` session.

    Azure serves identity from a different area than everything else here, and
    which one answers depends on how the session was established (an ``az login``
    ARM session versus ``az devops login`` with a PAT), so two candidates are
    tried in order. Both are reads; the first that yields a GUID wins. A failure
    to resolve is raised rather than swallowed by the callers that NEED the GUID
    (a review vote and arming auto-complete are both addressed by reviewer id),
    while :func:`get_current_login` swallows it.
    """
    cached = _identity_cache.get(org)
    if cached:
        return cached
    candidates = (
        {"area": "connectionData", "resource": "connectionData", "route": None},
        {"area": "profile", "resource": "profiles", "route": {"id": "me"}},
    )
    last: ProviderCliError | None = None
    for candidate in candidates:
        try:
            data = _obj(
                _az_invoke(
                    org=org,
                    area=str(candidate["area"]),
                    resource=str(candidate["resource"]),
                    host=host,
                    timeout=timeout,
                    route=candidate["route"] if isinstance(candidate["route"], dict) else None,
                    api_version=_API_IDENTITY,
                )
            )
        except ProviderSetupError:
            raise  # not authenticated / not installed is final, not a candidate miss
        except ProviderCliError as exc:
            last = exc
            continue
        user = _obj(data.get("authenticatedUser")) or data
        identity = _identity_id(user)
        login = _identity_login(user) or str(user.get("emailAddress") or "").strip() or None
        if identity:
            resolved = {"id": identity, "login": login}
            _identity_cache[org] = resolved
            return resolved
    if last is not None:
        raise last
    raise ProviderCliError(f"could not resolve the authenticated identity on {org}")


# ── WIQL ─────────────────────────────────────────────────────────────────────
#
# There is no filtered work-item list endpoint, so every listing starts with a
# WIQL query. WIQL is a query LANGUAGE, which makes every interpolated value an
# injection surface: a project name or a state name carrying a quote could
# otherwise close the literal and append its own clause -- and the clause that
# matters is the open-state filter, so the failure mode is a triage list showing
# closed items, not a syntax error someone notices.


def _wiql_literal(value: str) -> str:
    """One value as a safe WIQL single-quoted literal.

    A literal quote is escaped by DOUBLING it, which is WIQL's own mechanism.
    Anything that cannot be escaped that way -- a control character, a newline --
    is refused rather than stripped: silently dropping a character changes which
    items the query matches, and a name we cannot represent exactly is a name we
    must not guess at.
    """
    text = str(value or "")
    if not text:
        raise ProviderCliError("cannot build a WIQL query from an empty value")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
        raise ProviderCliError(f"value is not safe to interpolate into WIQL: {value!r}")
    return "'" + text.replace("'", "''") + "'"


def _open_work_items_wiql(project: str, closed_states: frozenset[str], *, order_by: str) -> str:
    """WIQL selecting the ids of the project's OPEN work items.

    Filters on STATE rather than on work item TYPE. "Issue" is a type that exists
    only in some process templates (Basic and Agile have it; Scrum does not), so
    restricting to it would silently return nothing on a Scrum project and would
    hide Bugs and Tasks everywhere else. The state names come from the project's
    own definitions -- see :func:`_project_closed_states` -- so nothing here
    assumes Agile.

    ``order_by`` is a fixed field name chosen by the caller from a closed set,
    never caller input.
    """
    states = ", ".join(_wiql_literal(name) for name in sorted(closed_states))
    return (
        "SELECT [System.Id] FROM WorkItems "
        f"WHERE [System.TeamProject] = {_wiql_literal(project)} "
        f"AND [System.State] NOT IN ({states}) "
        f"ORDER BY [{order_by}] DESC"
    )


def _wiql_ids(
    org: str,
    project: str,
    query: str,
    *,
    host: str,
    timeout: float,
    top: int,
) -> list[int]:
    """Run a WIQL query and return the work item ids it selected, in query order."""
    data = _obj(
        _az_invoke(
            org=org,
            area="wit",
            resource="wiql",
            host=host,
            timeout=timeout,
            route={"project": project},
            query={"$top": max(1, int(top))},
            method="POST",
            body={"query": query},
            api_version=_API_WIT,
        )
    )
    ids: list[int] = []
    for row in _values(data.get("workItems")):
        value = row.get("id")
        if isinstance(value, int):
            ids.append(value)
    return ids


# The fields every list row and detail pane reads. Requested explicitly rather
# than pulling whole work items: a full hydrate carries every custom field the
# process defines, which on a mature project is an order of magnitude more bytes
# for data nothing here renders.
_WORK_ITEM_FIELDS = (
    "System.Id",
    "System.Title",
    "System.State",
    "System.WorkItemType",
    "System.Tags",
    "System.CreatedBy",
    "System.CreatedDate",
    "System.ChangedDate",
    "System.AssignedTo",
    "System.Description",
    "System.CommentCount",
    "System.IterationPath",
    "Microsoft.VSTS.Common.ClosedDate",
    "Microsoft.VSTS.Common.ClosedBy",
)


def _hydrate_work_items(
    org: str, project: str, ids: list[int], *, host: str, timeout: float
) -> list[dict]:
    """Fetch the fields of ``ids`` in id ORDER, chunked at the batch endpoint's cap.

    The batch endpoint takes at most :data:`_BATCH_MAX_IDS` ids per call and
    answers 400 above it, so the ids are chunked and the results stitched back
    into the order they were given -- which is the WIQL query's order, and
    therefore the order the list view expects.
    """
    if not ids:
        return []
    by_id: dict[int, dict] = {}
    for start in range(0, len(ids), _BATCH_MAX_IDS):
        chunk = ids[start : start + _BATCH_MAX_IDS]
        rows = _values(
            _az_invoke(
                org=org,
                area="wit",
                resource="workitemsbatch",
                host=host,
                timeout=timeout,
                route={"project": project},
                method="POST",
                body={"ids": chunk, "fields": list(_WORK_ITEM_FIELDS)},
                api_version=_API_WIT,
            )
        )
        for row in rows:
            value = row.get("id")
            if isinstance(value, int):
                by_id[value] = row
    return [by_id[i] for i in ids if i in by_id]


# ── public surface: mirrors github_client function-for-function ──────────────


def verify_repo_access(
    owner: str, repo: str, *, host: str = "", timeout: float = AZ_TIMEOUT_SEC
) -> dict:
    """Verify the repository exists and the current ``az`` session can read it.

    Returns the same small summary shape the other two clients do, so the connect
    route needs no branching: ``full_name``, ``private``, ``open_issues_count``,
    ``description``, ``permissions``.

    ``open_issues_count`` is the PROJECT's open work item count, not the
    repository's -- work items have no repository dimension (see the module
    docstring), so this is the only count that exists. ``description`` is the
    project's, for the same reason: an Azure repository carries none.
    """
    org, project = _split_owner(owner)
    name = _check_repo(repo)
    details = _obj(
        _az_invoke(
            org=org,
            area="git",
            resource="repositories",
            host=host,
            timeout=timeout,
            route={"project": project, "repositoryId": name},
            api_version=_API_GIT,
        )
    )
    if not details:
        raise ProviderCliError(f"could not read {owner}/{repo} on {host}")
    project_info = _obj(details.get("project"))
    try:
        open_issues = len(
            _wiql_ids(
                org,
                project,
                _open_work_items_wiql(
                    project,
                    _project_closed_states(org, project, host=host, timeout=timeout),
                    order_by="System.ChangedDate",
                ),
                host=host,
                timeout=timeout,
                top=_WIQL_TOP,
            )
        )
    except ProviderCliError:
        # A project whose boards are disabled has no work item store at all, and
        # that must not make an otherwise readable repository look unreachable.
        open_issues = 0
    return {
        "full_name": f"{org}/{project}/{details.get('name') or name}",
        "private": str(project_info.get("visibility") or "private").lower() != "public",
        "open_issues_count": open_issues,
        "description": project_info.get("description"),
        "permissions": _permissions(org, project, host=host, timeout=timeout),
    }


def _permissions(org: str, project: str, *, host: str, timeout: float) -> dict:
    """The caller's permission object, in GitHub's ``{admin, maintain, push, triage, pull}``.

    Azure exposes no per-user effective-permission read through the CLI's REST
    passthrough (the security namespace / ACL routes speak in permission bitmasks
    against a namespace GUID, which is a different and far heavier surface), so
    this is derived from PROJECT TEAM MEMBERSHIP: a member of any project team
    holds the Contributors rights, which are what ``triage`` and ``push`` gate on
    here.

    This field decides which controls the UI offers, and ``routes._repo_can_write``
    consults it before admitting a mutation, so it is a GATE and it reports NO write
    access on Azure DevOps.

    The reason is that the only signal reachable through the CLI passthrough is
    project TEAM MEMBERSHIP, and membership does not imply repository write.
    Azure's permissions are per-repository ACLs that a project can override: a team
    member can have Git "Contribute" denied while still being able to edit work
    items, or the reverse. Reporting ``push``/``triage`` from membership therefore
    over-grants -- it offers write controls to someone the repository will refuse,
    and it does so through a gate whose answer is cached. There is no
    effective-permission read available here to replace it with (Azure exposes only
    namespace ACL bitmasks, which this transport cannot address).

    So the honest answer is false, and the consequence is deliberate: Azure repos
    are read-only through this app's own controls. A user who holds the rights
    still acts in Azure DevOps directly, where the permission is evaluated against
    their real identity rather than inferred here.

    ``pull`` stays true: read access is already demonstrated by the project being
    reachable and connected at all.
    """
    return {"admin": False, "maintain": False, "push": False, "triage": False, "pull": True}


def get_repo_permissions(
    owner: str, repo: str, *, host: str = "", timeout: float = AZ_TIMEOUT_SEC
) -> dict:
    """The authenticated ``az`` user's permission object for the project."""
    org, project = _split_owner(owner)
    _check_repo(repo)
    return _permissions(org, project, host=host, timeout=timeout)


def _list_work_items(
    owner: str, repo: str, *, host: str, timeout: float, order_by: str, top: int
) -> list[dict]:
    """The PROJECT's open work items, newest first by ``order_by``.

    Two calls by necessity: WIQL selects the ids (no filtered list endpoint
    exists), then the batch endpoint hydrates them.
    """
    org, project = _split_owner(owner)
    del repo  # work items are project-scoped; see list_open_issues
    closed_states = _project_closed_states(org, project, host=host, timeout=timeout)
    ids = _wiql_ids(
        org,
        project,
        _open_work_items_wiql(project, closed_states, order_by=order_by),
        host=host,
        timeout=timeout,
        top=top,
    )
    rows = _hydrate_work_items(org, project, ids, host=host, timeout=timeout)
    return [_norm_issue(row, org=org, project=project, closed_states=closed_states) for row in rows]


def list_open_issues(
    owner: str, repo: str, *, host: str = "", timeout: float = AZ_PAGINATE_TIMEOUT_SEC
) -> list[dict]:
    """Every OPEN work item in the PROJECT -- the triage view's working set.

    ``repo`` is accepted for signature parity and IGNORED: an Azure DevOps work
    item belongs to a project and has no repository dimension at all, so there is
    no "work items in this repository" question to ask. Two repositories connected
    from the same project legitimately return the same list, and the UI says so
    rather than implying a per-repository view.
    """
    return _list_work_items(
        owner, repo, host=host, timeout=timeout, order_by="System.ChangedDate", top=_WIQL_TOP
    )


def list_open_issues_first_page(
    owner: str, repo: str, *, host: str = "", timeout: float = AZ_TIMEOUT_SEC
) -> list[dict]:
    """The newest single page of OPEN work items -- the progressive first paint.

    The same leading rows :func:`list_open_issues` returns (same shape, same
    most-recently-changed order), so the full set appends behind it with no
    reordering. ``repo`` is ignored here too.
    """
    return _list_work_items(
        owner, repo, host=host, timeout=timeout, order_by="System.ChangedDate", top=_PAGE_SIZE
    )


def list_closed_issues(
    owner: str, repo: str, *, host: str = "", timeout: float = AZ_TIMEOUT_SEC
) -> list[dict]:
    """The most recently changed CLOSED work items (single page, like GitHub's).

    ``repo`` is ignored, as for the open list.
    """
    org, project = _split_owner(owner)
    del repo
    closed_states = _project_closed_states(org, project, host=host, timeout=timeout)
    states = ", ".join(_wiql_literal(name) for name in sorted(closed_states))
    query = (
        "SELECT [System.Id] FROM WorkItems "
        f"WHERE [System.TeamProject] = {_wiql_literal(project)} "
        f"AND [System.State] IN ({states}) "
        "ORDER BY [System.ChangedDate] DESC"
    )
    ids = _wiql_ids(org, project, query, host=host, timeout=timeout, top=_PAGE_SIZE)
    rows = _hydrate_work_items(org, project, ids, host=host, timeout=timeout)
    return [_norm_issue(row, org=org, project=project, closed_states=closed_states) for row in rows]


def list_recent_open_issues(
    owner: str, repo: str, limit: int = 30, *, host: str = "", timeout: float = AZ_TIMEOUT_SEC
) -> list[dict]:
    """The newest open work items by CREATION time -- the watcher's poll query.

    Ordered by ``System.CreatedDate`` rather than by last change: the watcher
    notifies on items that are NEW, and an old item that just got a comment must
    not look new.
    """
    capped = max(1, min(int(limit), _PAGE_SIZE))
    rows = _list_work_items(
        owner, repo, host=host, timeout=timeout, order_by="System.CreatedDate", top=capped
    )
    return rows[:capped]


def list_repo_labels(
    owner: str, repo: str, *, host: str = "", timeout: float = AZ_TIMEOUT_SEC
) -> list[dict]:
    """Every tag DEFINED in the project, in the app's label shape.

    Azure's label system for work items is ``System.Tags``, and the project's tag
    definitions are the closest thing to a label set. The colour is SYNTHETIC and
    the description is always empty: an Azure tag has neither, so both are
    reported as the neutral default rather than as data read from the server.
    """
    org, project = _split_owner(owner)
    del repo  # tags are project-scoped, like the work items they annotate
    rows = _values(
        _az_invoke(
            org=org,
            area="wit",
            resource="tags",
            host=host,
            timeout=timeout,
            route={"project": project},
            api_version=_API_WIT_TAGS,
        )
    )
    names = [str(row.get("name")) for row in rows if row.get("name")]
    return _shape_labels(names)


def list_repo_collaborators(
    owner: str, repo: str, *, host: str = "", timeout: float = AZ_PAGINATE_TIMEOUT_SEC
) -> list[dict]:
    """Authoritative member roster: the members of the project's teams.

    Azure has no repository-level collaborator list -- access is granted through
    project teams and groups -- so the project's teams are walked and their
    members unioned. Returns ``[{login, role_name}]`` in github_client's
    vocabulary.

    ``role_name`` is ``"write"`` for a member and ``"admin"`` for a team
    administrator, which are the only two distinctions the team-member payload
    supports. A 403 raises :class:`ProviderPermissionError` so the route degrades
    to the issue-derived set, same as the other clients.
    """
    org, project = _split_owner(owner)
    del repo
    teams = _values(
        _az_invoke(
            org=org,
            area="core",
            resource="teams",
            host=host,
            timeout=timeout,
            route={"projectId": project},
            api_version=_API_CORE,
        )
    )
    seen: dict[str, str] = {}
    for team in teams:
        team_id = str(team.get("id") or "")
        if not _GUID_RE.match(team_id):
            continue
        members = _values(
            _az_invoke(
                org=org,
                area="core",
                resource="members",
                host=host,
                timeout=timeout,
                route={"projectId": project, "teamId": team_id},
                api_version=_API_CORE,
            )
        )
        for member in members:
            login = _identity_login(member.get("identity")) or _identity_login(member)
            if not login:
                continue
            role = "admin" if member.get("isTeamAdmin") else "write"
            # An admin on ANY team outranks plain membership on another.
            if seen.get(login) != "admin":
                seen[login] = role
    return [{"login": login, "role_name": role} for login, role in seen.items()]


def _iter_project_identities(org: str, project: str, *, host: str, timeout: float):
    """Every project team member, yielded as its Azure identity reference.

    The same roster :func:`list_repo_collaborators` reads -- Azure grants access
    through project TEAMS rather than per-repository ACLs -- but yielded lazily so
    a lookup stops at its first match instead of paying for every team.

    A team whose member list is unreadable is SKIPPED rather than failing the
    walk: the identity being looked for may still sit on a team this caller can
    see, and aborting because one team is hidden would report a real member as
    unknown. An unreadable TEAM LIST is different and propagates, because then
    nothing was searched at all and a caller must not read that as "no match".
    """
    teams = _values(
        _az_invoke(
            org=org,
            area="core",
            resource="teams",
            host=host,
            timeout=timeout,
            route={"projectId": project},
            api_version=_API_CORE,
        )
    )
    for team in teams:
        team_id = str(team.get("id") or "")
        if not _GUID_RE.match(team_id):
            continue
        try:
            members = _values(
                _az_invoke(
                    org=org,
                    area="core",
                    resource="members",
                    host=host,
                    timeout=timeout,
                    route={"projectId": project, "teamId": team_id},
                    api_version=_API_CORE,
                )
            )
        except ProviderCliError:
            continue
        for member in members:
            yield _obj(member.get("identity")) or member


def _identity_matches(identity: dict, target: str) -> bool:
    """True when *target* (already case-folded) names *identity*.

    Both ``uniqueName`` and ``displayName`` are matched because both are what a
    person types: the picker shows the display name while the roster and the
    person filters key on the unique name.
    """
    name = (_identity_login(identity) or "").lower()
    display = str(identity.get("displayName") or "").lower()
    return target in (name, display)


def derive_members(issues: list[dict]) -> list[dict]:
    """FALLBACK roster. Always ``[]`` on Azure DevOps.

    GitHub derives membership from ``author_association``, which Azure does not
    report in any form (see :func:`_norm_issue`). Returning empty is the honest
    answer: the caller only reaches this when the member read was forbidden, and
    inventing a roster from work item authors would badge non-members as members.
    The route surfaces the empty roster with its ``source`` marker so the UI can
    say so.
    """
    del issues
    return []


def get_current_login(*, host: str = "", timeout: float = AZ_TIMEOUT_SEC) -> str | None:
    """The authenticated ``az`` user's login, or ``None`` if unavailable.

    Swallows :class:`ProviderCliError` and answers ``None``, matching
    gitlab_client's contract rather than github_client's (which raises).

    Azure needs an ORGANIZATION to answer any question, including "who am I",
    because every REST route is organization-scoped. The organization is taken
    from the CLI's own configured default -- see
    :func:`_default_organization` -- which is the closest analogue of the ambient
    session the other two providers resolve this from.
    """
    try:
        org = _default_organization(host=host, timeout=timeout)
        return _current_identity(org, host=host, timeout=timeout).get("login")
    except ProviderCliError:
        return None


def _default_organization(*, host: str, timeout: float) -> str:
    """The organization ``az devops`` is configured to default to.

    The two other providers answer session-scoped questions ("my projects", "who
    am I") without a namespace, because their credential IS the session. Azure's
    REST surface has no such route: every path is organization-scoped, and the
    endpoint that lists a user's organizations lives on a different service host
    the CLI's passthrough does not address. The CLI's own default organization is
    therefore the only session-derived answer available, and it is read from local
    config -- no HTTP, no credential.

    Raises :class:`ProviderCliError` when no default is set, naming the command
    that sets one, rather than guessing at an organization.
    """
    _resolve_host(host)
    proc = _az_run(["az", "devops", "configure", "--list"], host=host, timeout=timeout)
    if proc.returncode != 0:
        raise ProviderCliError(f"could not read the az devops configuration: {_stderr_tail(proc)}")
    for line in (proc.stdout or "").splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "organization":
            parsed = urlparse(value.strip())
            segments = _url_path_segments(parsed.path or "")
            if segments and not _bad_segment(unquote(segments[-1])):
                return unquote(segments[-1])
    raise ProviderCliError(
        "no default Azure DevOps organization is configured -- run "
        "`az devops configure --defaults organization=https://dev.azure.com/{org}`"
    )


def list_contributed_repos(
    login: str,
    *,
    host: str = "",
    within_days: int = CONTRIB_WINDOW_DAYS,
    timeout: float = AZ_PAGINATE_TIMEOUT_SEC,
) -> tuple[list[dict], bool]:
    """Repositories in the session's organization, for the connect picker.

    Returns ``(rows, truncated)`` -- the same tuple the other two clients return,
    with gitlab's row shape (``owner``/``repo``/``pushed_at``/``private``/
    ``description``), where ``owner`` is ``"{organization}/{project}"``.

    ``within_days`` is accepted and CANNOT be applied: Azure's repository payload
    carries no activity timestamp of any kind, so ``pushed_at`` is ``None`` and
    there is nothing to compare a cutoff against. Filtering on something else (a
    project's creation date, say) would silently hide active repositories, so the
    window is documented as unapplied instead.
    """
    del login  # the organization and its projects come from the az session
    del within_days  # no activity timestamp exists to filter on -- see the docstring
    org = _default_organization(host=host, timeout=timeout)
    projects = _az_invoke_paged(
        org=org,
        area="core",
        resource="projects",
        host=host,
        timeout=timeout,
        api_version=_API_CORE,
    )
    rows: list[dict] = []
    truncated = False
    for project in projects:
        name = str(project.get("name") or "")
        if _bad_segment(name):
            continue
        private = str(project.get("visibility") or "private").lower() != "public"
        try:
            repos = _values(
                _az_invoke(
                    org=org,
                    area="git",
                    resource="repositories",
                    host=host,
                    timeout=timeout,
                    route={"project": name},
                    api_version=_API_GIT,
                )
            )
        except ProviderPermissionError:
            # A project whose code is out of reach is skipped rather than failing
            # the whole picker; the user simply cannot connect that one.
            truncated = True
            continue
        for repo in repos:
            repo_name = str(repo.get("name") or "")
            if _bad_segment(repo_name) or repo.get("isDisabled"):
                continue
            rows.append(
                {
                    "owner": f"{org}/{name}",
                    "repo": repo_name,
                    # The picker keys its rows on `full_name` and hands it to
                    # publicRepoUrl, which splits it into org / project / repo to
                    # rebuild an Azure URL with its `_git` segment. Omitting it
                    # crashes selection on `undefined.split`, so the three-part
                    # form is the contract, not a convenience.
                    "full_name": f"{org}/{name}/{repo_name}",
                    "pushed_at": None,
                    "private": private,
                    "description": project.get("description"),
                }
            )
    return rows, truncated or len(projects) >= _MAX_PAGES * _PAGE_SIZE


def _work_item(org: str, project: str, number: int, *, host: str, timeout: float) -> dict:
    """One work item with the fields this module reads."""
    rows = _hydrate_work_items(org, project, [int(number)], host=host, timeout=timeout)
    if not rows:
        raise ProviderCliError(f"could not read {org}/{project}#{int(number)} on {AZURE_HOST}")
    return rows[0]


def get_issue_detail(
    owner: str, repo: str, number: int, *, host: str = "", timeout: float = AZ_TIMEOUT_SEC
) -> dict:
    """Full detail for one work item.

    ``number`` is the work item id (collection-unique on Azure, unlike GitLab's
    project-scoped iid), coerced to ``int`` before it can reach an argv. ``repo``
    is ignored: a work item is addressed by project and id.
    """
    org, project = _split_owner(owner)
    del repo
    raw = _work_item(org, project, number, host=host, timeout=timeout)
    fields = _obj(raw.get("fields"))
    work_item_type = str(_field(fields, "System.WorkItemType", "") or "")
    closed_states = (
        _closed_state_names(org, project, work_item_type, host=host, timeout=timeout)
        if work_item_type
        else _project_closed_states(org, project, host=host, timeout=timeout)
    )
    return _norm_issue_detail(raw, org=org, project=project, closed_states=closed_states)


def get_ref_summary(
    owner: str, repo: str, number: int, *, host: str = "", timeout: float = AZ_TIMEOUT_SEC
) -> dict:
    """Compact summary of one referenced WORK ITEM (hover card, ``#123``).

    Deliberately work-item-only, and that is a provider difference rather than a
    gap -- the same one gitlab_client documents. GitHub shares ONE number sequence
    between issues and pull requests, so its ``/issues/{n}`` endpoint answers for
    both and the caller needs ``is_pr`` to learn which it got. Azure allocates work
    item ids and pull request ids from entirely different services, so ``#5`` and
    ``!5`` are unrelated items; falling back to the pull request endpoint when work
    item 5 is absent would describe a different item under the number the user
    asked about. A missing work item raises instead, and ``is_pr`` is always
    ``False``.
    """
    org, project = _split_owner(owner)
    del repo
    raw = _work_item(org, project, number, host=host, timeout=timeout)
    fields = _obj(raw.get("fields"))
    work_item_type = str(_field(fields, "System.WorkItemType", "") or "")
    closed_states = (
        _closed_state_names(org, project, work_item_type, host=host, timeout=timeout)
        if work_item_type
        else _project_closed_states(org, project, host=host, timeout=timeout)
    )
    state = str(_field(fields, "System.State", "") or "")
    is_closed = state in closed_states
    return {
        "number": raw.get("id"),
        "title": str(_field(fields, "System.Title", "") or ""),
        "state": "closed" if is_closed else "open",
        "state_reason": None,
        "url": _work_item_url(org, project, int(number)),
        "author": _identity_login(fields.get("System.CreatedBy")),
        "author_association": None,
        "created_at": _field(fields, "System.CreatedDate"),
        "updated_at": _field(fields, "System.ChangedDate"),
        "closed_at": _field(fields, "Microsoft.VSTS.Common.ClosedDate") if is_closed else None,
        "comments": _field(fields, "System.CommentCount", 0),
        "is_pr": False,
        "draft": False,
        "merged_at": None,
        "labels": [
            {"name": name, "color": _SYNTHETIC_LABEL_COLOR}
            for name in _tag_names(fields.get("System.Tags"))
        ],
    }


# ── timeline ────────────────────────────────────────────────────────────────
#
# GitHub serves one unified `issues/{n}/timeline`. Azure splits the same
# information across two endpoints, so the work item timeline is assembled here:
#
#   comments   -- human discussion (a preview-only resource)
#   updates    -- the revision log: every field change, with the actor
#
# An update carries oldValue/newValue per field, so the typed events GitHub emits
# (labeled, assigned, closed, renamed) are RECONSTRUCTED from field diffs rather
# than read as events. Anything whose field is not one of those is dropped rather
# than shown as a raw field name, matching the other clients' "drop the noise"
# behaviour.

# Field -> the event kind its change becomes. Only fields the triage pane renders
# are listed; everything else in an update is noise here.
_TRACKED_UPDATE_FIELDS = (
    "System.Tags",
    "System.State",
    "System.AssignedTo",
    "System.Title",
    "System.IterationPath",
)


def _update_actor(update: dict) -> str | None:
    return _identity_login(update.get("revisedBy"))


def _update_when(update: dict) -> object:
    fields = _obj(update.get("fields"))
    changed = _obj(fields.get("System.ChangedDate")).get("newValue")
    return changed or update.get("revisedDate")


def _tag_events(update: dict, actor: str | None, created: object) -> list[dict]:
    """Label add/remove events reconstructed from a ``System.Tags`` diff.

    Azure records the whole delimited string before and after, not a per-tag
    event, so the two sets are differenced here. Order within the field is not
    meaningful, so a pure reordering produces no events.
    """
    change = _obj(_obj(update.get("fields")).get("System.Tags"))
    before = set(_tag_names(change.get("oldValue")))
    after = set(_tag_names(change.get("newValue")))
    events: list[dict] = []
    for name in sorted(after - before):
        events.append(
            {
                "kind": "labeled",
                "actor": actor,
                "created_at": created,
                "label": {"name": name, "color": _SYNTHETIC_LABEL_COLOR},
            }
        )
    for name in sorted(before - after):
        events.append(
            {
                "kind": "unlabeled",
                "actor": actor,
                "created_at": created,
                "label": {"name": name, "color": _SYNTHETIC_LABEL_COLOR},
            }
        )
    return events


def _state_events(
    update: dict, actor: str | None, created: object, closed_states: frozenset[str]
) -> list[dict]:
    """close / reopen events reconstructed from a ``System.State`` diff.

    A move between two OPEN states (New -> Active) is not a timeline event in
    GitHub's vocabulary and is dropped, so the pane does not fill with workflow
    churn the UI cannot render.
    """
    change = _obj(_obj(update.get("fields")).get("System.State"))
    before = str(change.get("oldValue") or "")
    after = str(change.get("newValue") or "")
    if not after or before == after:
        return []
    was_closed, is_closed = before in closed_states, after in closed_states
    if is_closed and not was_closed:
        return [
            {
                "kind": "closed",
                "actor": actor,
                "created_at": created,
                # Azure's System.Reason is a process-template value with no mapping
                # onto GitHub's completed / not_planned pair -- see _norm_issue_detail.
                "state_reason": None,
                "commit_id": None,
            }
        ]
    if was_closed and not is_closed:
        return [{"kind": "reopened", "actor": actor, "created_at": created}]
    return []


def _assignee_events(update: dict, actor: str | None, created: object) -> list[dict]:
    change = _obj(_obj(update.get("fields")).get("System.AssignedTo"))
    before = _identity_login(change.get("oldValue"))
    after = _identity_login(change.get("newValue"))
    events: list[dict] = []
    if before and before != after:
        events.append(
            {
                "kind": "unassigned",
                "actor": actor,
                "created_at": created,
                "assignee": before,
            }
        )
    if after and after != before:
        events.append(
            {
                "kind": "assigned",
                "actor": actor,
                "created_at": created,
                "assignee": after,
            }
        )
    return events


def _norm_update(update: dict, closed_states: frozenset[str]) -> list[dict]:
    """One work item revision -> zero or more normalized timeline events."""
    fields = _obj(update.get("fields"))
    if not any(name in fields for name in _TRACKED_UPDATE_FIELDS):
        return []
    actor = _update_actor(update)
    created = _update_when(update)
    events: list[dict] = []
    events.extend(_tag_events(update, actor, created))
    events.extend(_state_events(update, actor, created, closed_states))
    events.extend(_assignee_events(update, actor, created))
    title = _obj(fields.get("System.Title"))
    if title.get("newValue") and title.get("oldValue") != title.get("newValue"):
        events.append(
            {
                "kind": "renamed",
                "actor": actor,
                "created_at": created,
                "rename": {"from": title.get("oldValue"), "to": title.get("newValue")},
            }
        )
    iteration = _obj(fields.get("System.IterationPath"))
    if iteration.get("newValue") and iteration.get("oldValue") != iteration.get("newValue"):
        events.append(
            {
                "kind": "milestoned",
                "actor": actor,
                "created_at": created,
                "milestone": str(iteration["newValue"]).rsplit("\\", 1)[-1],
            }
        )
    return events


def _norm_work_item_comment(comment: dict) -> dict:
    """One work item comment -> the normalized ``comment`` timeline entry.

    ``id`` and ``updated_at`` are load-bearing rather than decoration: the crew
    claim protocol keeps ONE comment as its public ledger and rewrites it, so it
    needs the id to address its own comment and the modified time to prove the
    claim is alive (``created_at`` on an edited comment is still the original post
    time). Azure supplies both.
    """
    modified = comment.get("modifiedDate") or comment.get("createdDate")
    return {
        "kind": "comment",
        "id": comment.get("id"),
        "actor": _identity_login(comment.get("createdBy")),
        "created_at": comment.get("createdDate"),
        "updated_at": modified,
        "body": str(comment.get("text") or ""),
        "author_association": None,
        "reactions": None,
    }


def list_issue_timeline(
    owner: str, repo: str, number: int, *, host: str = "", timeout: float = AZ_PAGINATE_TIMEOUT_SEC
) -> list[dict]:
    """Normalized, chronological timeline for one work item.

    A failure on the SECONDARY stream (the revision log) degrades to omitting
    those entries rather than failing the whole pane: the comments are the
    substance, and the updates endpoint can be far larger than the comment list on
    a long-lived work item.
    """
    org, project = _split_owner(owner)
    del repo
    work_item_id = int(number)
    # Azure pages work-item comments with a continuation token, and a timeline that
    # stops at the first page silently drops later discussion -- the omission looks
    # like a quiet conversation rather than a truncated read. Bounded by _MAX_PAGES
    # for the same reason every other paged read here is: a pathological item must
    # not hold the request open indefinitely.
    comments: list[dict] = []
    token = ""
    for _ in range(_MAX_PAGES):
        query: dict[str, object] = {"$top": _COMMENT_PAGE_SIZE, "order": "asc"}
        if token:
            query["continuationToken"] = token
        page = _obj(
            _az_invoke(
                org=org,
                area="wit",
                resource="comments",
                host=host,
                timeout=timeout,
                route={"project": project, "workItemId": work_item_id},
                query=query,
                api_version=_API_WIT_COMMENTS,
            )
        )
        comments.extend(_values(page.get("comments")))
        token = str(page.get("continuationToken") or "")
        if not token:
            break
    events: list[dict] = [_norm_work_item_comment(row) for row in comments]

    closed_states = _project_closed_states(org, project, host=host, timeout=timeout)
    try:
        updates = _az_invoke_paged(
            org=org,
            area="wit",
            resource="updates",
            host=host,
            timeout=timeout,
            route={"project": project, "id": work_item_id},
            api_version=_API_WIT,
        )
    except ProviderCliError:
        updates = []
    for update in updates:
        events.extend(_norm_update(update, closed_states))

    events.sort(key=lambda e: str(e.get("created_at") or ""))
    return events


# Azure records a reviewer's vote as a SYSTEM thread carrying this property, so a
# vote is recoverable even though system comments are otherwise dropped.
_VOTE_PROPERTY_KEYS = ("CodeReviewVoteResult", "Microsoft.TeamFoundation.Discussion.VoteResult")
# Azure's vote scale. 5 ("approved with suggestions") maps to APPROVED rather than
# to a fourth UI state: it IS an approval, and inventing a state the shared pane
# does not render would show nothing at all.
_VOTE_STATES = {
    10: "APPROVED",
    5: "APPROVED",
    0: "COMMENTED",
    -5: "CHANGES_REQUESTED",
    -10: "CHANGES_REQUESTED",
}


def _thread_property(thread: dict, keys: tuple[str, ...]) -> object:
    """Read one of Azure's thread ``properties`` entries.

    A property value is wrapped as ``{"type": ..., "$value": ...}``, and the key
    spelling has varied across API versions, so several are tried.
    """
    props = _obj(thread.get("properties"))
    for key in keys:
        if key in props:
            entry = props[key]
            if isinstance(entry, dict):
                return entry.get("$value")
            return entry
    return None


def _norm_thread_comment(thread: dict, comment: dict) -> dict | None:
    """One PR thread comment -> a normalized timeline entry, or ``None`` to drop it.

    ``commentType == "system"`` is dropped: Azure writes an entry there for every
    vote, every reviewer addition and every push, so keeping them would bury the
    human discussion under "X voted" noise. A thread with a ``threadContext``
    (a file and line) becomes a ``review_comment``, which is what makes a review's
    substance visible; everything else is a plain ``comment``.
    """
    if str(comment.get("commentType") or "") == "system":
        return None
    body = str(comment.get("content") or "")
    if not body.strip():
        return None
    context = _obj(thread.get("threadContext"))
    path = context.get("filePath")
    if not path:
        return {
            "kind": "comment",
            "id": comment.get("id"),
            "actor": _identity_login(comment.get("author")),
            "created_at": comment.get("publishedDate"),
            "updated_at": comment.get("lastUpdatedDate") or comment.get("publishedDate"),
            "body": body,
            "author_association": None,
            "reactions": None,
        }
    line = _obj(context.get("rightFileStart")).get("line") or _obj(
        context.get("leftFileStart")
    ).get("line")
    return {
        "kind": "review_comment",
        "actor": _identity_login(comment.get("author")),
        "created_at": comment.get("publishedDate"),
        "body": body,
        "author_association": None,
        "path": path,
        "line": line,
        "url": None,
    }


def list_pr_timeline(
    owner: str, repo: str, number: int, *, host: str = "", timeout: float = AZ_PAGINATE_TIMEOUT_SEC
) -> list[dict]:
    """Normalized timeline for one pull request, including inline comments.

    Azure keeps PR discussion in THREADS rather than in a flat comment list, and
    an inline (diff) comment is just a thread carrying a ``threadContext``, so both
    kinds come from one read. Reviewer votes are recovered from the system threads
    that record them, which is why those are inspected before being dropped.

    Azure's thread resolution has SEVEN states (active, fixed, wontFix, closed,
    byDesign, pending, unknown) against the app's two-state model, so mapping it
    would be lossy in a way that matters (``wontFix`` and ``fixed`` are opposite
    outcomes that both read as resolved). The timeline therefore reports the
    comments themselves and leaves resolution to the provider's own UI rather than
    flattening a verdict.
    """
    org, project = _split_owner(owner)
    name = _check_repo(repo)
    pr_id = int(number)
    threads = _values(
        _az_invoke(
            org=org,
            area="git",
            resource="pullRequestThreads",
            host=host,
            timeout=timeout,
            route={"project": project, "repositoryId": name, "pullRequestId": pr_id},
            api_version=_API_GIT,
        )
    )
    events: list[dict] = []
    for thread in threads:
        vote = _thread_property(thread, _VOTE_PROPERTY_KEYS)
        if vote is not None:
            try:
                # Via str because a property value arrives untyped: Azure has
                # written this as both a number and a numeric string.
                value = int(str(vote).strip())
            except (TypeError, ValueError):
                value = 0
            author = None
            comments = _values(thread.get("comments"))
            if comments:
                author = _identity_login(comments[0].get("author"))
            events.append(
                {
                    "kind": "reviewed",
                    "actor": author,
                    "created_at": thread.get("publishedDate"),
                    "review_state": _VOTE_STATES.get(value, "COMMENTED"),
                    "body": "",
                }
            )
            continue
        for comment in _values(thread.get("comments")):
            entry = _norm_thread_comment(thread, comment)
            if entry is not None:
                events.append(entry)
    events.sort(key=lambda e: str(e.get("created_at") or ""))
    return events


# ── write operations ────────────────────────────────────────────────────────


def _patch_work_item(
    org: str,
    project: str,
    number: int,
    ops: list[dict],
    *,
    host: str,
    timeout: float,
) -> dict:
    """Apply a JSON-Patch document to one work item.

    Work item writes are JSON-Patch, not JSON, and Azure rejects the request
    outright when the content type says otherwise -- hence the explicit
    ``media_type``. The body rides in a temp file like every other body here, so
    no prose ever reaches an argv.
    """
    return _obj(
        _az_invoke(
            org=org,
            area="wit",
            resource="workitems",
            host=host,
            timeout=timeout,
            route={"project": project, "id": int(number)},
            method="PATCH",
            body=ops,
            api_version=_API_WIT,
            media_type="application/json-patch+json",
        )
    )


def _write_tags(
    org: str,
    project: str,
    number: int,
    names: list[str],
    *,
    rev: object,
    host: str,
    timeout: float,
) -> list[str]:
    """Replace a work item's tag field with ``names`` and return what Azure stored.

    ``System.Tags`` is a single delimited STRING field, and Azure exposes no
    add-one-tag or remove-one-tag operation, so every tag edit is unavoidably a
    read-modify-write of the whole set. That makes it lost-update-prone: another
    client adding a tag between our read and our write would be silently erased by
    our full-field value.

    ``rev`` is the revision the caller's ``names`` was computed from, and it is
    sent as a JSON Patch ``test`` on ``/rev`` ahead of the field write. Azure
    evaluates the operations in order and rejects the WHOLE patch when the test
    fails, so a concurrent edit makes this call fail loudly instead of deleting
    someone else's tag. The refusal surfaces as :class:`ProviderCliError`; the
    caller re-reads and retries rather than this function looping, because a retry
    here would hide a genuinely contended item behind an unbounded read-write
    cycle.

    A missing ``rev`` is refused rather than sent unguarded: silently degrading to
    the lost-update behaviour is the failure this guard exists to prevent.
    """
    if not isinstance(rev, int):
        raise ProviderCliError(
            f"work item {int(number)} carried no integer revision, so a tag write "
            "cannot be guarded against a concurrent edit"
        )
    data = _patch_work_item(
        org,
        project,
        number,
        [
            {"op": "test", "path": "/rev", "value": rev},
            {"op": "add", "path": "/fields/System.Tags", "value": _tags_field(names)},
        ],
        host=host,
        timeout=timeout,
    )
    return _tag_names(_obj(data.get("fields")).get("System.Tags"))


def add_issue_labels(
    owner: str,
    repo: str,
    number: int,
    labels: list[str],
    *,
    host: str = "",
    timeout: float = AZ_TIMEOUT_SEC,
) -> list[dict]:
    """Add tags to a work item and return its FULL resulting tag set.

    Azure has no add-one-tag operation: ``System.Tags`` is a single delimited
    string, so this is a read-modify-write. That makes it non-atomic -- two
    concurrent label edits can lose one another's addition -- which is inherent to
    the field's shape rather than a choice made here, and is why the write reads
    the current value immediately before patching rather than trusting a cached
    row.

    Tags are CASE SENSITIVE on Azure, so ``bug`` and ``Bug`` are two tags; the
    caller's spelling is preserved exactly. A name containing ``,`` or ``;`` is
    refused (see :func:`_check_label`).
    """
    org, project = _split_owner(owner)
    del repo
    additions = [_check_label(name) for name in labels]
    raw = _work_item(org, project, number, host=host, timeout=timeout)
    current = _tag_names(_obj(raw.get("fields")).get("System.Tags"))
    merged = list(current)
    for name in additions:
        if name not in merged:
            merged.append(name)
    if merged == current:
        # Idempotent: nothing to write, and Azure would answer with an unchanged
        # revision anyway. Skipping the write keeps the revision log clean.
        return _shape_labels(current)
    return _shape_labels(
        _write_tags(org, project, number, merged, rev=raw.get("rev"), host=host, timeout=timeout)
    )


def remove_issue_label(
    owner: str,
    repo: str,
    number: int,
    label: str,
    *,
    host: str = "",
    timeout: float = AZ_TIMEOUT_SEC,
) -> list[dict] | None:
    """Remove ONE tag from a work item; returns the remaining tags, shaped.

    Removing a tag the item does not carry is a no-op success, and the authoritative
    remaining set is still returned -- so, as on GitLab, this never needs to answer
    ``None``. The ``None`` stays in the return type for parity with github_client,
    whose callers handle it.
    """
    org, project = _split_owner(owner)
    del repo
    target = _check_label(label)
    raw = _work_item(org, project, number, host=host, timeout=timeout)
    current = _tag_names(_obj(raw.get("fields")).get("System.Tags"))
    remaining = [name for name in current if name != target]
    if remaining == current:
        return _shape_labels(current)
    return _shape_labels(
        _write_tags(org, project, number, remaining, rev=raw.get("rev"), host=host, timeout=timeout)
    )


def set_issue_state(
    owner: str,
    repo: str,
    number: int,
    state: str,
    state_reason: str | None = None,
    *,
    host: str = "",
    timeout: float = AZ_TIMEOUT_SEC,
) -> dict:
    """Close or reopen a work item. ``state`` is ``"open"`` or ``"closed"``.

    The concrete state name is resolved from the item's OWN type through the
    process template's state categories, so this works on a custom process whose
    closing state is not called "Closed".

    ``state_reason`` is accepted for signature parity and IGNORED, and the returned
    ``state_reason`` is always ``None`` -- exactly as gitlab_client does. Azure's
    ``System.Reason`` is a template-defined value with no mapping onto GitHub's
    two, so translating one would report a reason the platform did not record.
    """
    del state_reason
    org, project = _split_owner(owner)
    del repo
    if state not in ("open", "closed"):
        raise ProviderCliError(f"invalid work item state: {state!r}")
    raw = _work_item(org, project, number, host=host, timeout=timeout)
    work_item_type = str(_field(_obj(raw.get("fields")), "System.WorkItemType", "") or "")
    if not work_item_type:
        raise ProviderCliError(f"could not read the work item type of #{int(number)}")
    if state == "closed":
        target = _closing_state_name(org, project, work_item_type, host=host, timeout=timeout)
    else:
        target = _open_state_name(org, project, work_item_type, host=host, timeout=timeout)
    data = _patch_work_item(
        org,
        project,
        number,
        [{"op": "add", "path": "/fields/System.State", "value": target}],
        host=host,
        timeout=timeout,
    )
    written = str(_field(_obj(data.get("fields")), "System.State", "") or "")
    closed_states = _closed_state_names(org, project, work_item_type, host=host, timeout=timeout)
    if written in closed_states:
        resolved = "closed"
    elif written:
        resolved = "open"
    else:
        # No usable System.State came back -- absent, null and "" all normalize to
        # "" -- so report what was asked for rather than guessing a category.
        resolved = state
    return {"state": resolved, "state_reason": None}


def set_issue_assignees(
    owner: str,
    repo: str,
    number: int,
    assignees: list[str],
    *,
    host: str = "",
    timeout: float = AZ_TIMEOUT_SEC,
) -> list[str]:
    """REPLACE a work item's assignee and return the set Azure actually stored.

    Parity with github_client.set_issue_assignees -- replace semantics, an empty
    list clears, and the result is read back from the RESPONSE rather than echoed
    from the request -- with one structural difference that cannot be papered
    over: ``System.AssignedTo`` is a SINGLE identity field, so a work item cannot
    carry a set at all.

    More than one name is therefore REFUSED as
    :class:`ProviderInvalidInputError`, the same class an unassignable GitHub
    login maps to, so the route answers 400 either way. Assigning the first and
    dropping the rest would report success for names the item does not carry --
    the exact failure the read-back exists to prevent -- and is the direction that
    fails silently, so it is not taken.

    The login is resolved against the project's team roster BEFORE the write, for
    the reason gitlab_client.set_issue_assignees resolves ids first: Azure's
    identity picker binds the field to a project member, and a name it cannot bind
    fails with a message naming the FIELD, not the value. Resolving here turns
    that into a refusal that names the login the user actually typed.

    Unlike :func:`_write_tags` this sends no ``/rev`` precondition, because the
    hazard is not the same: a tag edit is a read-modify-write of a delimited
    string shared with every other tag, while this field is replaced wholesale.
    The lost-update guard for a wholesale replace lives one level up, where
    ``routes._replace_assignees_checked`` refuses when the forge no longer holds
    the set the editor was rendered from.
    """
    org, project = _split_owner(owner)
    del repo
    wanted = [name.strip() for name in assignees if isinstance(name, str) and name.strip()]
    if len(wanted) > 1:
        raise ProviderInvalidInputError(
            "an Azure DevOps work item holds exactly one assignee, so "
            + ", ".join(wanted)
            + " cannot all be assigned -- pick one.",
            values=list(wanted),
        )

    value = ""
    if wanted:
        login = wanted[0]
        target = login.lower()
        resolved = None
        try:
            for identity in _iter_project_identities(org, project, host=host, timeout=timeout):
                if _identity_matches(identity, target):
                    resolved = _identity_login(identity)
                    if resolved:
                        break
        except ProviderCliError as exc:
            raise ProviderInvalidInputError(
                f"cannot assign {login!r}: the project's team roster is unreadable, so "
                "the name cannot be checked against it.",
                values=[login],
            ) from exc
        if not resolved:
            raise ProviderInvalidInputError(
                f"Azure DevOps will not assign {login!r} -- an assignee must be a member "
                f"of a team in {org}/{project}.",
                values=[login],
            )
        # The roster's own spelling, not the caller's: the field is bound to an
        # identity, and echoing a display-name match back as the field value
        # would send something the picker cannot resolve.
        value = resolved

    # An empty string is how the field is CLEARED. A JSON Patch ``remove`` would
    # fail outright on a work item that never carried an assignee, which is the
    # common case for "clear it" arriving from a stale editor.
    data = _patch_work_item(
        org,
        project,
        number,
        [{"op": "add", "path": "/fields/System.AssignedTo", "value": value}],
        host=host,
        timeout=timeout,
    )
    written = _identity_login(_obj(data.get("fields")).get("System.AssignedTo"))
    return [written] if written else []


def create_label(
    owner: str,
    repo: str,
    name: str,
    color: str = "888888",
    description: str = "",
    *,
    host: str = "",
    timeout: float = AZ_TIMEOUT_SEC,
) -> dict:
    """Register a tag for use on the project's work items.

    Azure has NO create-tag endpoint: a tag definition comes into existence when a
    tag is first applied to a work item (the tags API can rename and delete, not
    create). So this validates the name, returns the existing definition when the
    project already has one, and otherwise returns the shaped tag without a write
    -- the definition materializes the first time :func:`add_issue_labels` applies
    it, which is what every caller of this function does next.

    ``color`` and ``description`` are accepted for signature parity and cannot be
    stored: an Azure tag has neither.

    Azure has no create-tag-definition endpoint -- a tag comes into existence when
    it is first APPLIED to a work item -- so this cannot create anything. It
    therefore REFUSES a name that does not already exist rather than returning a
    shaped tag: answering successfully would put a phantom label in the caller's
    cache and in the label palette, offering the user a tag that no work item can
    be filtered by and that the next cache refresh silently removes. An existing
    tag is returned as Azure holds it, which makes the call idempotent for the
    only input it can actually satisfy.
    """
    del color, description
    org, project = _split_owner(owner)
    del repo
    tag = _check_label(name)
    try:
        existing = {
            row["name"]: row for row in list_repo_labels(owner, project, host=host, timeout=timeout)
        }
    except ProviderCliError:
        existing = {}
    if tag in existing:
        return existing[tag]
    raise ProviderCliError(
        f"Azure DevOps cannot create the tag {tag!r} on its own: a work item tag is "
        "created by applying it to a work item, so there is no tag to create in "
        "advance. Apply it to an item instead."
    )


def _comment_text(body: str) -> str:
    """The vetted text for a comment this app is about to publish.

    Refuses an empty body, then REDACTS before the text can reach a provider. A
    comment body is frequently model-authored (a crew's reply, an AI summary the
    user accepted), and posting it puts that text somewhere public and permanent,
    so a leaked credential or an exfiltration URL cannot be walked back.

    Both redactions run because they catch different things: one strips URLs that
    would smuggle data out to a third party, the other strips secrets that appear
    in the text itself. Redaction is idempotent, so a caller that already redacted
    (the crew path does) loses nothing by passing through here.
    """
    text = (body or "").strip()
    if not text:
        raise ProviderCliError("a comment needs a body")
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    return text


def add_issue_comment(
    owner: str,
    repo: str,
    number: int,
    body: str,
    *,
    host: str = "",
    timeout: float = AZ_TIMEOUT_SEC,
) -> dict:
    """Post a comment on a WORK ITEM.

    Work item comments live on a PREVIEW resource -- Azure has never promoted them
    to a GA api-version -- which is why this call's version differs from the rest
    of the ``wit`` area.
    """
    org, project = _split_owner(owner)
    del repo
    text = _comment_text(body)
    data = _obj(
        _az_invoke(
            org=org,
            area="wit",
            resource="comments",
            host=host,
            timeout=timeout,
            route={"project": project, "workItemId": int(number)},
            method="POST",
            body={"text": text},
            api_version=_API_WIT_COMMENTS,
        )
    )
    return {
        "id": data.get("id"),
        # A work item comment has no direct web URL of its own, so the caller links
        # to the work item rather than fabricating an anchor.
        "url": None,
        "created_at": data.get("createdDate"),
    }


def add_pr_comment(
    owner: str,
    repo: str,
    number: int,
    body: str,
    *,
    host: str = "",
    timeout: float = AZ_TIMEOUT_SEC,
) -> dict:
    """Post a comment on a PULL REQUEST, as a new thread.

    A general PR comment on Azure is a thread with NO ``threadContext`` -- that is
    what distinguishes it from an inline diff comment. A separate function from
    :func:`add_issue_comment` because work items and pull requests are different
    services with independent id sequences, so the number alone does not identify
    the item and posting to the wrong one would comment on something unrelated.
    """
    org, project = _split_owner(owner)
    name = _check_repo(repo)
    text = _comment_text(body)
    data = _obj(
        _az_invoke(
            org=org,
            area="git",
            resource="pullRequestThreads",
            host=host,
            timeout=timeout,
            route={"project": project, "repositoryId": name, "pullRequestId": int(number)},
            method="POST",
            body={
                "comments": [{"parentCommentId": 0, "content": text, "commentType": "text"}],
                "status": "active",
            },
            api_version=_API_GIT,
        )
    )
    comments = _values(data.get("comments"))
    first = comments[0] if comments else {}
    return {
        "id": first.get("id") or data.get("id"),
        "url": None,
        "created_at": first.get("publishedDate") or data.get("publishedDate"),
    }


# ── pull requests ───────────────────────────────────────────────────────────


def _branch_name(ref: object) -> str | None:
    """``refs/heads/main`` -> ``main``; anything else passes through unchanged."""
    text = str(ref or "")
    if not text:
        return None
    return text[len("refs/heads/") :] if text.startswith("refs/heads/") else text


def _pr_labels(raw: object) -> list[str]:
    """Azure PR labels, which are its own tag objects and may be deactivated."""
    return [
        str(row.get("name")) for row in _values(raw) if row.get("name") and row.get("active", True)
    ]


def _norm_pull(raw: dict) -> dict:
    """One Azure pull request -> the row shape ``github_client._PR_JQ`` produces.

    ``state`` folds ``completed`` (merged) and ``abandoned`` into ``closed``,
    because that is what the app's open/closed filter means; the distinction
    survives in ``merged_at``, exactly as it does on the other two providers.

    ``updated_at`` needs an explanation: Azure exposes NO modification timestamp
    on a pull request. The closing date is used when the PR is closed and the
    creation date otherwise, which is the best available answer -- so an edit to an
    open PR does not move it in a most-recently-updated ordering. The list is
    therefore ordered by creation on this provider (see :func:`_list_pulls`), and
    the probe that gates polling does not claim otherwise.

    ``assignees`` is always empty: an Azure pull request has reviewers, not
    assignees, and reporting reviewers as assignees would badge a reviewer as
    owning the change.
    """
    status = str(raw.get("status") or "").lower()
    merged = status == "completed"
    closed_at = raw.get("closedDate")
    return {
        "number": raw.get("pullRequestId"),
        "title": str(raw.get("title") or ""),
        "url": _pr_web_url(raw),
        "state": "open" if status == "active" else "closed",
        "draft": bool(raw.get("isDraft")),
        "labels": _pr_labels(raw.get("labels")),
        "author": _identity_login(raw.get("createdBy")),
        "author_association": None,
        "updated_at": closed_at or raw.get("creationDate"),
        "created_at": raw.get("creationDate"),
        "closed_at": closed_at if status in ("completed", "abandoned") else None,
        "merged_at": closed_at if merged else None,
        "assignees": [],
        "requested_reviewers": _identity_logins(raw.get("reviewers")),
        "base": _branch_name(raw.get("targetRefName")),
        "head": _branch_name(raw.get("sourceRefName")),
        # The head COMMIT, for the same reason the other clients carry it in the
        # list row: a bulk approve or a merge has to name the revision the row was
        # rendered at. Azure calls it the last merge SOURCE commit.
        "head_sha": _obj(raw.get("lastMergeSourceCommit")).get("commitId"),
        "body": str(raw.get("description") or ""),
        # Card enrichment is NOT available in the list payload on Azure (checks are
        # policy evaluations, addressed per PR), so these stay unknown here and
        # ``enrich_pulls`` fills them. ``checks_counts: None`` is what keeps an
        # unenriched row out of the on-disk cache via ``enrichment_complete``.
        "additions": None,
        "deletions": None,
        "changed_files": None,
        "checks_state": None,
        "checks_counts": None,
        "checks_truncated": False,
    }


def _pr_web_url(raw: dict) -> str:
    """The PR's web URL.

    Azure's payload carries only an API ``url`` (and a ``_links.web`` that is
    absent on the list route), so the human URL is composed from the repository's
    project and name when the web link is missing.
    """
    web = _obj(_obj(raw.get("_links")).get("web")).get("href")
    if web:
        return str(web)
    repo = _obj(raw.get("repository"))
    project = _obj(repo.get("project")).get("name")
    org = _org_from_api_url(repo.get("url"))
    number = raw.get("pullRequestId")
    if not (project and org and repo.get("name") and number):
        return ""
    return (
        f"https://{AZURE_HOST}/{quote(str(org), safe='')}/{quote(str(project), safe='')}"
        f"/_git/{quote(str(repo['name']), safe='')}/pullrequest/{int(number)}"
    )


def _org_from_api_url(url: object) -> str | None:
    """The organization from an Azure API URL, for composing a web link.

    Azure's API URLs come in two forms -- ``dev.azure.com/{org}/...`` and the
    legacy ``{org}.visualstudio.com/...`` -- so both are read here rather than
    assuming the modern one.
    """
    try:
        parsed = urlparse(str(url or ""))
        host = (parsed.hostname or "").lower()
    except ValueError:
        return None
    if host.endswith(_LEGACY_HOST_SUFFIX):
        candidate = host[: -len(_LEGACY_HOST_SUFFIX)]
        return candidate if not _bad_segment(candidate) else None
    if host == AZURE_HOST:
        segments = _url_path_segments(parsed.path or "")
        if segments:
            candidate = unquote(segments[0])
            return candidate if not _bad_segment(candidate) else None
    return None


def _list_pulls(
    owner: str, repo: str, state: str, *, host: str, timeout: float, paginate: bool
) -> list[dict]:
    """List pull requests of ``state``, newest first.

    ``searchCriteria.status`` is passed EXPLICITLY on every call: Azure defaults it
    to ``active`` server-side, so an omitted status would silently return only
    open PRs on the closed tab. The app's "closed" means "no longer open", which
    covers both ``completed`` and ``abandoned`` -- two separate Azure statuses --
    so the closed listing asks for ``all`` and filters, rather than hiding every
    abandoned PR (or every merged one).
    """
    org, project = _split_owner(owner)
    name = _check_repo(repo)
    status = "active" if state == "open" else "all"
    # A closed list is a FILTER over a mixed page, so the cap has to come AFTER the
    # filter. Capping the fetch instead returns an empty closed history whenever the
    # newest page happens to be all active pull requests -- Azure has no
    # closed-only status, so `all` is the only way to ask.
    fetch_everything = paginate or state != "open"
    rows = _az_invoke_paged(
        org=org,
        area="git",
        resource="pullRequests",
        host=host,
        timeout=timeout,
        route={"project": project, "repositoryId": name},
        query={"searchCriteria.status": status},
        api_version=_API_GIT,
        limit=0 if fetch_everything else _PAGE_SIZE,
    )
    out = [_norm_pull(row) for row in rows]
    if state != "open":
        out = [row for row in out if row.get("state") == "closed"]
        if not paginate:
            out = out[:_PAGE_SIZE]
    return out


def list_open_pulls(
    owner: str, repo: str, *, host: str = "", timeout: float = AZ_PAGINATE_TIMEOUT_SEC
) -> list[dict]:
    """Every OPEN (active) pull request."""
    return _list_pulls(owner, repo, "open", host=host, timeout=timeout, paginate=True)


def list_open_pulls_first_page(
    owner: str, repo: str, *, host: str = "", timeout: float = AZ_TIMEOUT_SEC
) -> list[dict]:
    """The newest single page of OPEN pull requests, in ONE request.

    The progressive first paint on a cold cache: the same leading rows
    :func:`list_open_pulls` returns, so the full set appends behind it with no
    reordering. The rows are UNENRICHED (no check state) -- Azure needs a per-PR
    call for that -- so the first-paint route returns them as-is and the
    authoritative fetch enriches.
    """
    return _list_pulls(owner, repo, "open", host=host, timeout=timeout, paginate=False)


def list_closed_pulls(
    owner: str, repo: str, *, host: str = "", timeout: float = AZ_TIMEOUT_SEC
) -> list[dict]:
    """The most recently created CLOSED pull requests -- completed and abandoned."""
    return _list_pulls(owner, repo, "closed", host=host, timeout=timeout, paginate=False)


# Azure's ``mergeStatus`` speaks ONLY to whether the branches conflict, which makes
# it the exact analogue of GitHub's weak ``mergeable`` field -- and NOT of a merge
# gate. Anything not yet computed is reported as unknown (None) rather than as
# not-mergeable, so the UI does not flash a false conflict warning mid-computation.
_MERGE_SUCCEEDED = frozenset({"succeeded"})
_MERGE_PENDING = frozenset({"queued", "notset", "", "none"})


def _mergeable(raw: dict) -> bool | None:
    status = str(raw.get("mergeStatus") or "").lower()
    if status in _MERGE_PENDING:
        return None
    return status in _MERGE_SUCCEEDED


def get_pr_detail(
    owner: str,
    repo: str,
    number: int,
    *,
    host: str = "",
    timeout: float = AZ_TIMEOUT_SEC,
    resolve_mergeable: bool = True,
) -> dict:
    """Full detail for one pull request, in ``_PR_DETAIL_JQ``'s shape.

    Diff size is reported as ``None`` rather than ``0``: Azure has no per-PR diff
    statistic, and reading real line counts would mean pulling the whole diff on
    every detail view. The UI already treats those as optional, and a zero would
    present an unread diff as a confident "no changes".

    ``resolve_mergeable`` is load-bearing here rather than parity-only. The value
    the app's merge gate reads (``mergeable_state``) requires a SECOND call -- the
    PR's policy evaluations -- because Azure's own ``mergeStatus`` knows about
    conflicts and nothing else. A caller that only needs an eagerly-returned field
    (``head_sha`` for the head-moved check) passes ``False`` and skips that call,
    which is the same intent the flag has on the GitHub client.
    """
    org, project = _split_owner(owner)
    name = _check_repo(repo)
    pr_id = int(number)
    raw = _obj(
        _az_invoke(
            org=org,
            area="git",
            resource="pullRequests",
            host=host,
            timeout=timeout,
            route={"project": project, "repositoryId": name, "pullRequestId": pr_id},
            api_version=_API_GIT,
        )
    )
    if not raw:
        raise ProviderCliError(f"could not read {owner}/{repo}!{pr_id} on {host}")
    detail = _norm_pull(raw)
    status = str(raw.get("status") or "").lower()
    completion = _obj(raw.get("completionOptions"))
    auto_by = _obj(raw.get("autoCompleteSetBy"))
    detail.update(
        {
            # `labels` is re-shaped, not inherited. _norm_pull produces the LIST-row
            # form (bare names) while the detail contract is objects carrying
            # name/color/description -- github_client's get_pr_detail does the same.
            # Leaving the list shape here makes PrDetail read `.name` off a string
            # and the labels vanish from the pane.
            "labels": _shape_labels(_pr_labels(raw.get("labels"))),
            "commits": None,
            "comments": None,
            "review_comments": None,
            "merged": status == "completed",
            "mergeable": _mergeable(raw),
            "mergeable_state": (
                _mergeable_state(org, project, name, raw, host=host, timeout=timeout)
                if resolve_mergeable
                else "unknown"
            ),
            "merged_by": _identity_login(raw.get("closedBy")) if status == "completed" else None,
            # Azure's auto-complete is a real, reversible armed state -- the identity
            # that armed it is stored on the PR -- so this reports who armed it and
            # which strategy it will use, rather than a guess.
            "auto_merge": (
                {
                    "method": _MERGE_STRATEGY_NAMES.get(
                        str(completion.get("mergeStrategy") or "").lower()
                    ),
                    "enabled_by": _identity_login(auto_by),
                }
                if _identity_id(auto_by)
                else None
            ),
        }
    )
    return detail


# The app's merge gate (``routes._MERGE_ALLOWED_STATES``) admits only
# ``clean`` / ``has_hooks`` / ``mergeable``, and it is right to: a value that means
# "no conflicts" says nothing about unmet policies. Azure's own ``mergeStatus`` is
# exactly that weak value, so ``mergeable`` is reported ONLY when every blocking
# policy evaluation has passed as well -- which is the same claim GitLab's modern
# ``mergeable`` makes. Everything else reports a specific reason, all of which the
# route refuses, leaving the user the auto-complete path where Azure itself decides.
_STATE_DIRTY = "dirty"
_STATE_DRAFT = "draft"
_STATE_BLOCKED = "blocked"
_STATE_CHECKING = "checking"
_STATE_UNKNOWN = "unknown"


def _mergeable_state(
    org: str, project: str, repo: str, raw: dict, *, host: str, timeout: float
) -> str:
    """The merge-gate verdict for one PR, in the vocabulary the route adjudicates."""
    status = str(raw.get("status") or "").lower()
    if status != "active":
        return _STATE_UNKNOWN
    if raw.get("isDraft"):
        return _STATE_DRAFT
    merge_status = str(raw.get("mergeStatus") or "").lower()
    if merge_status in _MERGE_PENDING:
        return _STATE_CHECKING
    if merge_status not in _MERGE_SUCCEEDED:
        return _STATE_DIRTY
    number = raw.get("pullRequestId")
    if not isinstance(number, int):
        return _STATE_UNKNOWN
    try:
        evaluations = _policy_evaluations(org, project, number, host=host, timeout=timeout)
    except ProviderCliError:
        # A gate that cannot read the policies must not claim they are satisfied.
        return _STATE_UNKNOWN
    for evaluation in evaluations:
        if not _obj(evaluation.get("configuration")).get("isBlocking", True):
            continue
        if str(evaluation.get("status") or "").lower() != "approved":
            return _STATE_BLOCKED
    return "mergeable"


def _policy_evaluations(
    org: str, project: str, number: int, *, host: str, timeout: float
) -> list[dict]:
    """The policy evaluations for one pull request.

    Azure addresses these by ARTIFACT ID rather than by PR number, and the artifact
    id embeds the project's GUID -- which is why the project id is resolved (and
    cached) at all.
    """
    project_guid = _project_id(org, project, host=host, timeout=timeout)
    artifact = f"vstfs:///CodeReview/CodeReviewId/{project_guid}/{int(number)}"
    return _values(
        _az_invoke(
            org=org,
            area="policy",
            resource="evaluations",
            host=host,
            timeout=timeout,
            route={"project": project},
            query={"artifactId": artifact},
            api_version=_API_POLICY,
        )
    )


# ── policy evaluations and builds as checks ─────────────────────────────────
#
# GitHub reports per-PR status as check runs plus commit statuses. Azure has two
# separate things that both mean "a check on this PR": POLICY EVALUATIONS (the
# branch policies -- required reviewers, comment resolution, required builds) and
# the BUILDS themselves. Both are bucketed into the same vocabulary
# ``gitlab_client._norm_job`` uses so the shared summary logic and UI colours
# apply unchanged, including its behaviour that a cancelled unit is
# informational rather than failing.
_EVALUATION_BUCKETS = {
    "approved": "success",
    "rejected": "failure",
    "broken": "failure",
    "queued": "running",
    "running": "running",
    "notapplicable": "other",
}
_BUILD_RESULT_BUCKETS = {
    "succeeded": "success",
    "failed": "failure",
    # Azure's "partially succeeded" means some tasks failed but were allowed to:
    # the build did not fail, so reporting it red would show a red PR that Azure
    # itself considers passing -- the same reason GitLab's allow_failure jobs
    # bucket as informational.
    "partiallysucceeded": "other",
    "canceled": "other",
    "cancelled": "other",
    "none": "other",
}
_BUILD_RUNNING_STATUSES = frozenset({"notstarted", "inprogress", "postponed", "none", ""})
_BUILD_FINISHED_STATUSES = frozenset({"completed"})


def _evaluation_bucket(status: str) -> str:
    return _EVALUATION_BUCKETS.get((status or "").lower(), "other")


def _norm_evaluation(evaluation: dict) -> dict:
    """One policy evaluation -> the check-row shape ``_CHECK_RUN_JQ`` produces.

    ``bucket`` is precomputed because :func:`summarize_checks` reads it, and
    ``source`` carries the publisher identity the dedupe keys on -- the policy
    TYPE, so a required-reviewers policy and a build policy are distinct
    publishers rather than one lump.
    """
    status = str(evaluation.get("status") or "")
    bucket = _evaluation_bucket(status)
    config = _obj(evaluation.get("configuration"))
    policy_type = _obj(config.get("type"))
    display = str(policy_type.get("displayName") or "policy")
    settings = _obj(config.get("settings"))
    detail = str(settings.get("displayName") or "")
    blocking = bool(config.get("isBlocking", True))
    return {
        "name": detail or display,
        "status": "in_progress" if bucket == "running" else "completed",
        "conclusion": {"failure": "failure", "success": "success", "running": None}.get(
            bucket, "neutral"
        ),
        "bucket": bucket if blocking or bucket != "failure" else "other",
        "url": None,
        "started_at": evaluation.get("startedDate"),
        "completed_at": evaluation.get("completedDate"),
        "summary": display if detail else "",
        "app": "Azure DevOps policy",
        "source": str(policy_type.get("id") or "policy"),
    }


def _norm_build(build: dict) -> dict:
    """One pipeline build -> the check-row shape ``_CHECK_RUN_JQ`` produces."""
    status = str(build.get("status") or "").lower()
    result = str(build.get("result") or "").lower()
    if status in _BUILD_FINISHED_STATUSES:
        bucket = _BUILD_RESULT_BUCKETS.get(result, "other")
    elif status in _BUILD_RUNNING_STATUSES:
        bucket = "running"
    else:
        bucket = "other"
    definition = _obj(build.get("definition"))
    return {
        "name": str(definition.get("name") or build.get("buildNumber") or "build"),
        "status": "completed" if status in _BUILD_FINISHED_STATUSES else "in_progress",
        "conclusion": {"failure": "failure", "success": "success", "running": None}.get(
            bucket, "neutral"
        ),
        "bucket": bucket,
        "url": _obj(_obj(build.get("_links")).get("web")).get("href"),
        "started_at": build.get("startTime") or build.get("queueTime"),
        "completed_at": build.get("finishTime"),
        "summary": str(build.get("buildNumber") or ""),
        "app": "Azure Pipelines",
        "source": "azure-pipelines",
    }


def _builds_for_sha(
    org: str, project: str, repo: str, sha: str, *, host: str, timeout: float
) -> list[dict]:
    """Builds whose source commit is ``sha``.

    Azure's build list has no commit filter, so the newest
    :data:`_BUILD_SCAN_TOP` builds for the repository are read and matched on
    ``sourceVersion`` in Python. Bounded deliberately: an unbounded walk of a busy
    project's build history to find one commit's runs would be slower than the
    detail pane it serves.
    """
    rows = _values(
        _az_invoke(
            org=org,
            area="build",
            resource="builds",
            host=host,
            timeout=timeout,
            route={"project": project},
            query={
                "repositoryId": f"{project}/{repo}",
                "repositoryType": "TfsGit",
                "$top": _BUILD_SCAN_TOP,
                "queryOrder": "queueTimeDescending",
            },
            api_version=_API_BUILD,
        )
    )
    return [row for row in rows if str(row.get("sourceVersion") or "").lower() == sha.lower()]


def _pr_number_for_sha(
    org: str, project: str, repo: str, sha: str, *, host: str, timeout: float
) -> int | None:
    """The pull request whose head commit is ``sha``, or ``None``.

    Needed because the shared ``list_pr_checks`` signature keys checks by SHA
    while Azure keys policy evaluations by PULL REQUEST. Active PRs are searched
    first (the overwhelmingly common case for a check read), then all, both
    bounded.
    """
    for status in ("active", "all"):
        rows = _az_invoke_paged(
            org=org,
            area="git",
            resource="pullRequests",
            host=host,
            timeout=timeout,
            route={"project": project, "repositoryId": repo},
            query={"searchCriteria.status": status},
            api_version=_API_GIT,
            limit=_PR_LOOKUP_TOP,
        )
        for row in rows:
            head = str(_obj(row.get("lastMergeSourceCommit")).get("commitId") or "")
            if head.lower() == sha.lower():
                number = row.get("pullRequestId")
                return number if isinstance(number, int) else None
    return None


def list_pr_checks(
    owner: str, repo: str, sha: str, *, host: str = "", timeout: float = AZ_TIMEOUT_SEC
) -> list[dict]:
    """Check-run-shaped rows for commit ``sha``: its builds plus its PR's policies.

    Keyed on the head SHA to match the other two clients, and because that is the
    correct semantics: a run against an older commit describes code that no longer
    exists. Azure needs one extra step for the policy half -- evaluations are
    addressed by pull request, so the PR whose head is ``sha`` is resolved first;
    if no PR matches (a commit on a branch with no open PR), the builds are still
    returned rather than nothing.

    ``sha`` is charset-validated before reaching a query string, since it is the
    one value here that originates from a previous API response rather than from a
    connected-repo record.
    """
    if not _SHA_RE.match(sha or ""):
        raise ProviderCliError(f"invalid commit sha {sha!r}")
    org, project = _split_owner(owner)
    name = _check_repo(repo)
    rows = [
        _norm_build(build)
        for build in _builds_for_sha(org, project, name, sha, host=host, timeout=timeout)
    ]
    try:
        number = _pr_number_for_sha(org, project, name, sha, host=host, timeout=timeout)
    except ProviderCliError:
        number = None
    if number is not None:
        try:
            rows.extend(
                _norm_evaluation(evaluation)
                for evaluation in _policy_evaluations(
                    org, project, number, host=host, timeout=timeout
                )
            )
        except ProviderCliError:
            # Policies out of reach must not blank the builds that were read.
            pass
    return rows


_CHECK_BUCKETS = ("failure", "running", "success", "other")


def summarize_checks(checks: list[dict]) -> dict:
    """``{checks_counts, checks_state, checks_truncated}`` from bucketed rows.

    Byte-identical contract to the other two clients', including the
    ``checks_state`` priority (anything failing dominates, then running, then
    success, then informational) so the card's dot never reads greener than the
    list it summarizes, and ``None`` when there are no checks at all.
    """
    counts = dict.fromkeys(_CHECK_BUCKETS, 0)
    for row in checks:
        if not isinstance(row, dict):
            continue
        bucket = row.get("bucket")
        counts[bucket if isinstance(bucket, str) and bucket in counts else "other"] += 1
    for bucket in _CHECK_BUCKETS:
        if counts[bucket]:
            state: str | None = bucket
            break
    else:
        state = None  # no checks at all -> the card shows no dot
    return {"checks_counts": counts, "checks_state": state, "checks_truncated": False}


def _enrich_rows(owner: str, repo: str, pulls: list[dict], *, host: str) -> list[dict]:
    """Attach card enrichment (check state) to PR rows, in place.

    Azure's PR list payload carries no check state at all, so unlike GitLab this
    cannot be a no-op: each row's checks come from its own policy evaluations. That
    is one call per PR, so it is bounded at :data:`_ENRICH_MAX_PULLS`. Rows past
    the bound keep ``checks_counts: None``, which makes
    :func:`enrichment_complete` answer ``False`` and keeps the whole set OUT of
    the on-disk cache -- the same invariant the other clients rely on -- rather
    than caching a zeroed check state as authoritative.

    A per-PR failure leaves that row unenriched for the same reason: a row whose
    checks could not be read must not be persisted as having none.
    """
    org, project = _split_owner(owner)
    _check_repo(repo)
    for row in pulls[:_ENRICH_MAX_PULLS]:
        number = row.get("number")
        if not isinstance(number, int):
            continue
        try:
            evaluations = _policy_evaluations(
                org, project, number, host=host, timeout=AZ_TIMEOUT_SEC
            )
        except ProviderCliError:
            continue
        row.update(summarize_checks([_norm_evaluation(item) for item in evaluations]))
    return pulls


def enrich_pulls(
    owner: str, repo: str, pulls: list[dict], state: str, *, host: str = ""
) -> list[dict]:
    """Attach card enrichment to each PR row.

    ``state`` is accepted for signature parity and unused: the enrichment is the
    same read for an open PR and a closed one, and Azure has no cheaper path for
    either.
    """
    del state
    return _enrich_rows(owner, repo, pulls, host=host)


def enrich_pulls_by_number(
    owner: str, repo: str, pulls: list[dict], *, host: str = ""
) -> list[dict]:
    """Signature-parity counterpart to :func:`enrich_pulls`, for search results."""
    return _enrich_rows(owner, repo, pulls, host=host)


def enrichment_complete(pulls: list[dict]) -> bool:
    """Whether every row carries its card enrichment.

    Reads the same ``checks_counts is not None`` invariant the other clients do:
    a row that arrived unenriched must keep itself OUT of the on-disk list cache
    rather than being cached as authoritative.
    """
    return all(pr.get("checks_counts") is not None for pr in pulls)


# ── cheap open-list probe (poll gating) ─────────────────────────────────────
#
# The list routes poll with ``poll=1`` and serve the cache when a cheap probe
# shows the open list has not moved. The probe value is only ever compared
# against ANOTHER probe recorded when the list was last fetched, never against the
# cached rows, so a systematic difference between what the probe counts and what
# the list returns cancels out instead of reporting "changed" forever.
#
# Azure serves the two kinds very differently, and only one honestly:
#
#   work items -- the WIQL query returns the ids themselves, so the count is
#     EXACT: closing any work item, anywhere in the list, changes it. The newest
#     ChangedDate is a second, independent signal that catches an edit that does
#     not change the count.
#   pull requests -- the PR list endpoint exposes no total count in its body, and
#     Azure has no PR modification timestamp to fall back on (see ``_norm_pull``),
#     so any signal here would be weaker than the caller believes and could report
#     "unchanged" after a non-top PR closed. Refused instead, exactly as
#     gitlab_client refuses it.
_PROBE_KINDS = ("issue", "pr")


def probe_open_list(
    owner: str, repo: str, kind: str, *, host: str = "", timeout: float = AZ_TIMEOUT_SEC
) -> dict:
    """``{"total_count": int, "top_updated_at": str | None}`` for the project's
    OPEN work items.

    Raises :class:`ProviderCliError` on a failed call, and for ``kind="pr"`` -- see
    the note above on why a PR probe is refused rather than approximated. Callers
    already treat a probe failure as a soft signal, so this degrades to the
    pre-poll behaviour instead of serving anything stale.
    """
    if kind not in _PROBE_KINDS:
        raise ProviderCliError(f"unsupported probe kind: {kind!r}")
    if kind == "pr":
        raise ProviderCliError(
            "Azure DevOps exposes no cheap open pull-request count; "
            "falling back to the staleness ceiling"
        )
    org, project = _split_owner(owner)
    del repo  # work items are project-scoped
    closed_states = _project_closed_states(org, project, host=host, timeout=timeout)
    ids = _wiql_ids(
        org,
        project,
        _open_work_items_wiql(project, closed_states, order_by="System.ChangedDate"),
        host=host,
        timeout=timeout,
        top=_WIQL_TOP,
    )
    top_updated: str | None = None
    if ids:
        rows = _hydrate_work_items(org, project, ids[:1], host=host, timeout=timeout)
        if rows:
            changed = _obj(rows[0].get("fields")).get("System.ChangedDate")
            top_updated = str(changed) if changed else None
    return {"total_count": len(ids), "top_updated_at": top_updated}


# ── pull-request search ─────────────────────────────────────────────────────


def build_pr_search_query(
    owner: str,
    repo: str,
    *,
    state: str = "open",
    author: str | None = None,
    assignee: str | None = None,
    review_requested: str | None = None,
) -> str:
    """Assemble the ``searchCriteria.*`` fragment for a per-person PR search.

    GitHub takes a single search-qualifier string; Azure takes discrete
    ``searchCriteria`` parameters. The CALLER-FACING signature is identical on
    purpose -- the route passes the same keyword arguments to whichever client it
    holds, so a provider-specific spelling here would be a ``TypeError`` at
    request time rather than a compile error.

    The person filters are emitted as PLACEHOLDERS (``{creatorId}`` /
    ``{reviewerId}``), not as values, because Azure's criteria take identity GUIDs
    rather than names and resolving a login to a GUID is a network call this pure
    function must not make. :func:`search_pulls` substitutes them. ``assignee``
    maps onto ``reviewerId`` as well: an Azure pull request has no assignee, and a
    reviewer is the nearest thing the criteria can express -- which is why an
    assignee filter and a review-requested filter cannot be combined here.

    Raises :class:`PrSearchError` on an unknown state, an invalid login, or when no
    person filter was given -- an unfiltered search would just duplicate the list
    endpoint, the same reason the other two clients refuse it.
    """
    if state not in ("open", "closed", "merged", "all"):
        raise PrSearchError(f"unsupported state for pull-request search: {state!r}")
    # Azure has no distinct "merged" status: a merged PR is ``completed``, and an
    # abandoned one is ``abandoned``. So "merged" asks for completed, and "closed"
    # asks for abandoned -- closed WITHOUT merge, which is what the route's closed
    # tab means and what the GitHub path returns.
    az_status = {"open": "active", "closed": "abandoned", "merged": "completed", "all": "all"}[
        state
    ]
    params = [f"searchCriteria.status={az_status}"]
    people: list[tuple[str, str | None]] = [
        ("searchCriteria.creatorId", author),
        ("searchCriteria.reviewerId", review_requested or assignee),
    ]
    added = 0
    for param, login in people:
        if not login:
            continue
        if not _LOGIN_RE.match(login):
            raise PrSearchError(f"invalid Azure DevOps login: {login!r}")
        placeholder = param.rsplit(".", 1)[-1]
        params.append(f"{param}={{{placeholder}}}")
        added += 1
    if added == 0:
        raise PrSearchError("pull-request search needs at least one person filter")
    del owner, repo  # the repository is addressed by route parameter, not by a qualifier
    return "&".join(params)


def _resolve_login_id(org: str, project: str, login: str, *, host: str, timeout: float) -> str:
    """Resolve a login to the identity GUID Azure's search criteria require.

    Azure's ``searchCriteria.creatorId`` / ``reviewerId`` take a GUID, not a name,
    and a name they cannot parse is IGNORED rather than rejected -- which would
    turn a person filter into an unfiltered list and present every open PR as that
    person's. So the login is resolved against the project's team members (the same
    roster :func:`list_repo_collaborators` reads) and an unresolvable one raises
    :class:`PrSearchError` instead.
    """
    target = login.strip().lower()
    try:
        for identity in _iter_project_identities(org, project, host=host, timeout=timeout):
            if not _identity_matches(identity, target):
                continue
            # A match whose id is not a GUID cannot reach an argv, so the walk
            # continues rather than failing: another team may carry the same
            # person with a usable identity reference.
            resolved = _identity_id(identity)
            if resolved:
                return resolved
    except ProviderCliError as exc:
        raise PrSearchError(
            f"cannot resolve {login!r} to an Azure DevOps identity: the project's teams "
            "are unreadable"
        ) from exc
    raise PrSearchError(f"no Azure DevOps identity matches {login!r} in {org}/{project}")


def search_pulls(
    owner: str,
    repo: str,
    *,
    host: str = "",
    state: str = "open",
    author: str | None = None,
    assignee: str | None = None,
    review_requested: str | None = None,
    timeout: float = AZ_PAGINATE_TIMEOUT_SEC,
    limit: int = PR_SEARCH_MAX,
) -> list[dict]:
    """Search a repository's pull requests by person, server-side.

    Returns rows in the SAME shape as :func:`list_open_pulls`, so the frontend can
    swap data sources without a second row type. ``limit`` is honoured, and the
    ceiling is ``PR_SEARCH_MAX + 1`` rather than ``PR_SEARCH_MAX``: the route asks
    for one MORE row than it will show and reports "truncated" when it gets it, so
    clamping to the display cap would discard that sentinel and present every
    over-cap result set as complete.
    """
    org, project = _split_owner(owner)
    name = _check_repo(repo)
    query_fragment = build_pr_search_query(
        owner,
        repo,
        state=state,
        author=author,
        assignee=assignee,
        review_requested=review_requested,
    )
    query: dict[str, object] = {}
    for pair in query_fragment.split("&"):
        key, _, value = pair.partition("=")
        if value == "{creatorId}":
            query[key] = _resolve_login_id(
                org, project, str(author), host=host, timeout=AZ_TIMEOUT_SEC
            )
        elif value == "{reviewerId}":
            query[key] = _resolve_login_id(
                org, project, str(review_requested or assignee), host=host, timeout=AZ_TIMEOUT_SEC
            )
        else:
            query[key] = value
    capped = max(1, min(int(limit), PR_SEARCH_MAX + 1))
    rows = _az_invoke_paged(
        org=org,
        area="git",
        resource="pullRequests",
        host=host,
        timeout=timeout,
        route={"project": project, "repositoryId": name},
        query=query,
        api_version=_API_GIT,
        limit=capped,
    )
    out = [_norm_pull(row) for row in rows][:capped]
    if state == "closed":
        # Belt to the status filter above: "closed" means closed WITHOUT being
        # merged, matching the GitHub path.
        out = [row for row in out if not row.get("merged_at")]
    return out


# ── pull-request actions (parity with github_client's PR action surface) ──────
#
# Azure's vocabulary differs from GitHub's at every one of these, so each function
# maps the app's provider-neutral request onto Azure's own concept:
#
#   close/reopen        -> the PR's ``status`` (abandoned / active)
#   review verdict      -> a reviewer VOTE on a numeric scale
#   comment             -> a thread with no threadContext
#   merge               -> "complete" the PR, with a merge strategy
#   auto-merge          -> ``autoCompleteSetBy``, a real reversible armed state
#   cancel / re-run CI  -> a pipeline BUILD, not a workflow run
#
# Unlike GitLab, Azure can express all three review verdicts and all three merge
# strategies, and it has a genuine arm-and-cancel auto-merge -- so nothing here is
# refused for want of a native equivalent.


def _pr_patch(
    org: str,
    project: str,
    repo: str,
    number: int,
    payload: dict,
    *,
    host: str,
    timeout: float,
) -> dict:
    """PATCH one pull request.

    ``repositoryId`` is passed even though a pull request id is unique across the
    whole collection: every mutating PR route still requires it in the path, so
    omitting it would 404 rather than resolve.
    """
    return _obj(
        _az_invoke(
            org=org,
            area="git",
            resource="pullRequests",
            host=host,
            timeout=timeout,
            route={"project": project, "repositoryId": repo, "pullRequestId": int(number)},
            method="PATCH",
            body=payload,
            api_version=_API_GIT,
        )
    )


def set_pr_state(
    owner: str,
    repo: str,
    number: int,
    state: str,
    *,
    host: str = "",
    timeout: float = AZ_TIMEOUT_SEC,
) -> dict:
    """Close or reopen a pull request. ``state`` is ``"open"`` or ``"closed"``.

    Closing sets ``abandoned``, which is Azure's reversible "closed without
    merging" -- not ``completed``, which would MERGE the pull request. Reopening an
    abandoned PR sets it back to ``active``; a COMPLETED pull request cannot be
    reopened at all, and Azure answers with an error rather than this function
    pretending otherwise.
    """
    if state not in ("open", "closed"):
        raise ProviderCliError(f"invalid pull request state: {state!r}")
    org, project = _split_owner(owner)
    name = _check_repo(repo)
    data = _pr_patch(
        org,
        project,
        name,
        number,
        {"status": "abandoned" if state == "closed" else "active"},
        host=host,
        timeout=timeout,
    )
    status = str(data.get("status") or "").lower()
    if status == "active":
        resolved = "open"
    elif status:
        resolved = "closed"
    else:
        # No usable status came back -- absent, null and "" all normalize to "" --
        # so report what was asked for rather than reading it as "closed".
        resolved = state
    return {
        "state": resolved,
        "merged": status == "completed",
        "draft": bool(data.get("isDraft")),
    }


# COMMENT only. Azure records a review as a numeric reviewer VOTE rather than as a
# review object, and that vote carries no revision -- so the two VERDICT verbs are
# refused rather than approximated, for the ordering reason spelled out in
# :func:`submit_pr_review`. This is the same shape of honest refusal gitlab_client
# makes for the verdict IT cannot express.
PR_REVIEW_EVENTS = ("COMMENT",)

# The two verdict verbs Azure can express but cannot bind to a revision. Named so
# the refusal can explain WHICH verb was asked for, and so a reader sees they were
# considered rather than overlooked.
_UNBINDABLE_REVIEW_VERBS = ("APPROVE", "REQUEST_CHANGES")


def submit_pr_review(
    owner: str,
    repo: str,
    number: int,
    event: str,
    body: str = "",
    head_sha: str = "",
    *,
    host: str = "",
    timeout: float = AZ_TIMEOUT_SEC,
) -> dict:
    """Post a review on a pull request. ``COMMENT`` is the only verb Azure accepts.

    ``COMMENT`` records no vote and posts prose only, because a comment is not a
    verdict -- so it is expressible here. ``APPROVE`` and ``REQUEST_CHANGES`` are
    REFUSED, and the refusal is the substance of this function rather than a gap in
    it: Azure's reviewer vote is a numeric value attached to the PULL REQUEST, with
    no revision parameter to bind it to the commit the reviewer actually read. The
    full ordering argument is in the refusal branch below.

    ``head_sha`` is REQUIRED, matching both other clients, even though nothing can
    be done with it on the wire here. It keeps the verbs from diverging into "one
    of them names the revision and the other does not", and it gives the route's
    own head-moved check (which re-reads the live head and answers 409) a value to
    compare against.
    """
    verb = (event or "").strip().upper()
    if verb in _UNBINDABLE_REVIEW_VERBS:
        # Azure's reviewer vote takes no revision parameter: it attaches to the PULL
        # REQUEST, not to a commit. So between the route's head-moved check and this
        # call, a push can land and the verdict then records against code nobody
        # read. Azure resetting votes on push does NOT close that ordering -- the
        # reset fires with the push, before our vote arrives. There is no
        # compensating fix either: withdrawing a vote after re-reading the head is
        # itself a write that can fail, and its failure leaves an approval standing
        # on unreviewed code, which is the wrong direction to fail in.
        #
        # So the verb is REFUSED rather than approximated, the same choice
        # gitlab_client makes for the verdict it cannot express. Commenting still
        # works, and a human can vote in Azure's own UI where they can see the head
        # they are voting on.
        raise ProviderCliError(
            f"Azure DevOps cannot record a {verb} review that is bound to the "
            "commit it was formed on -- its reviewer vote attaches to the pull "
            "request, not to a revision, so a push landing first would apply the "
            "verdict to unreviewed code. Post a comment instead, or vote in Azure "
            "DevOps directly."
        )
    if verb not in PR_REVIEW_EVENTS:
        raise ProviderCliError(f"invalid review event: {event!r}")
    text = (body or "").strip()
    if not text:
        raise ProviderCliError(f"a {verb} review requires a comment body")
    sha = (head_sha or "").strip()
    if not _SHA_RE.match(sha):
        raise ProviderCliError(
            "refusing to review without the head commit it was read at " f"(got {head_sha!r})"
        )
    # Called for their validation, not their values: the owner must still split into
    # an org/project pair and the repository name must still pass the segment
    # charset, so a malformed identity is refused here rather than inside the
    # comment call.
    _split_owner(owner)
    _check_repo(repo)
    if verb == "COMMENT":
        add_pr_comment(owner, repo, number, text, host=host, timeout=timeout)
        return {"id": None, "state": "COMMENTED", "submitted_at": None}

    # Unreachable: PR_REVIEW_EVENTS holds only COMMENT and every other verb was
    # refused above. Kept as a loud failure rather than a silent fall-through, so a
    # future verb added to the tuple cannot quietly return None.
    raise ProviderCliError(f"unhandled review event: {verb!r}")


# Azure supports all three history shapes as a per-request completion option, so
# this tuple is the full set -- unlike GitLab, where merge-vs-rebase is a project
# setting and REBASE has to be refused.
PR_MERGE_METHODS = ("MERGE", "SQUASH", "REBASE")

# The app's method -> Azure's ``completionOptions.mergeStrategy``.
#
# ``MERGE`` maps to ``noFastForward``, which is what "always create a merge commit"
# is called on Azure. ``REBASE`` maps to ``rebase`` (replay then fast-forward), NOT
# to ``rebaseMerge`` (replay then still create a merge commit) -- the two produce
# different histories, and the caller asking for a rebase means the linear one.
_MERGE_STRATEGIES = {
    "MERGE": "noFastForward",
    "SQUASH": "squash",
    "REBASE": "rebase",
}
# The reverse map, for reporting an already-armed auto-complete's strategy back in
# the app's vocabulary. ``rebaseMerge`` is included on the read side because Azure's
# own web UI can arm it even though this module never sets it.
_MERGE_STRATEGY_NAMES = {
    "nofastforward": "MERGE",
    "squash": "SQUASH",
    "rebase": "REBASE",
    "rebasemerge": "REBASE",
}


def merge_pull_request(
    owner: str,
    repo: str,
    number: int,
    method: str = "SQUASH",
    head_sha: str = "",
    *,
    host: str = "",
    timeout: float = AZ_TIMEOUT_SEC,
) -> dict:
    """Complete (merge) a pull request now.

    Azure merges a PR by PATCHing it to ``completed`` with the commit it is pinned
    to. Like the other two providers this cannot bypass a gate: Azure enforces the
    branch's policies on completion and refuses a PR that has not satisfied them,
    which surfaces as an error.

    ``head_sha`` is REQUIRED and rides as ``lastMergeSourceCommit``, which Azure
    treats as a real precondition: it refuses the completion when that is no longer
    the PR's last source commit, so a push landing between the review and the click
    cannot merge unreviewed code. It is a positional parameter with an empty default
    only so the three module signatures stay identical; an empty value is refused
    here, never defaulted.

    ``bypassPolicy`` is deliberately never sent. It is Azure's override switch, and
    a button that silently sheds a required policy is the one thing the provider
    would not adjudicate for us.

    Returns ``{merged, sha, message}`` in the GitHub-shaped vocabulary the route and
    the UI speak.
    """
    verb = (method or "").strip().upper()
    if verb not in PR_MERGE_METHODS:
        raise ProviderCliError(f"invalid merge method: {method!r}")
    sha = (head_sha or "").strip()
    if not _SHA_RE.match(sha):
        raise ProviderCliError(
            "refusing to merge without the head commit it was reviewed at " f"(got {head_sha!r})"
        )
    org, project = _split_owner(owner)
    name = _check_repo(repo)
    data = _pr_patch(
        org,
        project,
        name,
        number,
        {
            "status": "completed",
            "lastMergeSourceCommit": {"commitId": sha},
            "completionOptions": {
                "mergeStrategy": _MERGE_STRATEGIES[verb],
                # Delete the source branch: matches what the other two providers'
                # repository settings do by default and what the app's users expect
                # after a merge. Explicit either way, because Azure reads an omitted
                # option as "leave as armed", which would inherit whatever the PR
                # was last set to rather than what this call asked for.
                "deleteSourceBranch": False,
                "bypassPolicy": False,
            },
        },
        host=host,
        timeout=timeout,
    )
    status = str(data.get("status") or "").lower()
    return {
        "merged": status == "completed",
        "sha": _obj(data.get("lastMergeCommit")).get("commitId"),
        "message": str(data.get("mergeFailureMessage") or ""),
    }


def enable_auto_merge(
    owner: str,
    repo: str,
    number: int,
    method: str = "SQUASH",
    *,
    host: str = "",
    timeout: float = AZ_TIMEOUT_SEC,
) -> dict:
    """Arm Azure's auto-complete on a pull request.

    Azure has a REAL deferred-merge verb, unlike GitLab: setting
    ``autoCompleteSetBy`` to an identity arms the PR, and Azure completes it by
    itself once every policy passes. Nothing merges at call time -- an unarmed PR
    with no outstanding policy is completed by Azure a moment later, which is the
    same behaviour GitHub's auto-merge has -- so this is genuinely the reversible
    arming action the bulk affordance advertises, and :func:`disable_auto_merge`
    really does cancel it.

    The merge strategy is armed alongside, so the deferred merge produces the
    history the caller chose rather than whatever the PR was last set to.

    ``auto_merge`` in the result is DERIVED from what came back, not asserted: a
    hardcoded ``True`` would make the response a claim rather than an observation.

    Returns ``{auto_merge, method, enabled_at}``.
    """
    verb = (method or "").strip().upper()
    if verb not in PR_MERGE_METHODS:
        raise ProviderCliError(f"invalid merge method: {method!r}")
    org, project = _split_owner(owner)
    name = _check_repo(repo)
    identity = _current_identity(org, host=host, timeout=timeout)
    identity_id = str(identity.get("id") or "")
    if not _GUID_RE.match(identity_id):
        raise ProviderCliError(
            "cannot arm auto-complete: the authenticated Azure DevOps identity could not be resolved"
        )
    data = _pr_patch(
        org,
        project,
        name,
        number,
        {
            "autoCompleteSetBy": {"id": identity_id},
            "completionOptions": {
                "mergeStrategy": _MERGE_STRATEGIES[verb],
                "deleteSourceBranch": False,
                "bypassPolicy": False,
            },
        },
        host=host,
        timeout=timeout,
    )
    armed = _identity_id(data.get("autoCompleteSetBy"))
    strategy = str(_obj(data.get("completionOptions")).get("mergeStrategy") or "").lower()
    return {
        "auto_merge": bool(armed),
        "method": _MERGE_STRATEGY_NAMES.get(strategy) or (verb if armed else None),
        # Azure records no timestamp for the arming, and substituting "now" would
        # report a time nothing stored.
        "enabled_at": None,
    }


# The identity that CLEARS an armed auto-complete. Azure has no "unset" verb for
# ``autoCompleteSetBy``: patching it to the empty GUID is the documented way to
# cancel, and an omitted field would leave the PR armed.
_EMPTY_GUID = "00000000-0000-0000-0000-000000000000"


def disable_auto_merge(
    owner: str, repo: str, number: int, *, host: str = "", timeout: float = AZ_TIMEOUT_SEC
) -> dict:
    """Cancel Azure's auto-complete on a pull request.

    The genuine inverse of :func:`enable_auto_merge` -- so an accidental arm is
    reversible from the same place it was set, which is what makes offering the arm
    in bulk defensible. Returns ``{auto_merge: False, method: None, enabled_at: None}``.
    """
    org, project = _split_owner(owner)
    name = _check_repo(repo)
    data = _pr_patch(
        org,
        project,
        name,
        number,
        {"autoCompleteSetBy": {"id": _EMPTY_GUID}},
        host=host,
        timeout=timeout,
    )
    still_armed = _identity_id(data.get("autoCompleteSetBy"))
    if still_armed and still_armed != _EMPTY_GUID:
        raise ProviderCliError(
            "Azure DevOps still reports auto-complete as armed after the cancel request"
        )
    return {"auto_merge": False, "method": None, "enabled_at": None}


# Azure build status -> the GitHub CONCLUSION vocabulary the shared UI compares
# against. Only the spellings that differ need an entry; note "canceled" (one l) is
# Azure's spelling and the UI keys on GitHub's "cancelled", so passing it through
# would leave a consumer unable to see a cancelled build.
_BUILD_CONCLUSIONS = {
    "succeeded": "success",
    "failed": "failure",
    "partiallysucceeded": "neutral",
    "canceled": "cancelled",
    "cancelled": "cancelled",
}


def list_pr_workflow_runs(
    owner: str, repo: str, sha: str, *, host: str = "", timeout: float = AZ_TIMEOUT_SEC
) -> list[dict]:
    """The pipeline builds for a PR's head commit -- Azure's analogue of a
    workflow run (see github_client for why checks and runs are separate surfaces).

    ``sha`` is charset-validated before it reaches a query, as on the other two
    paths. Rows carry ``cancellable``/``rerunnable`` so the UI never offers an
    action Azure will refuse: a finished build can only be re-queued, and an
    in-flight one can only be cancelled.
    """
    if not _SHA_RE.match(sha or ""):
        raise ProviderCliError(f"invalid commit sha: {sha!r}")
    org, project = _split_owner(owner)
    name = _check_repo(repo)
    out: list[dict] = []
    for build in _builds_for_sha(org, project, name, sha, host=host, timeout=timeout):
        build_id = build.get("id")
        if not isinstance(build_id, int):
            continue
        status = str(build.get("status") or "").lower()
        result = str(build.get("result") or "").lower()
        finished = status in _BUILD_FINISHED_STATUSES
        definition = _obj(build.get("definition"))
        out.append(
            {
                "id": build_id,
                "name": str(definition.get("name") or build.get("buildNumber") or "build"),
                # Normalized to the same two fields the GitHub rows use, so the UI reads
                # one shape: Azure reports status and result separately, like GitHub's
                # status and conclusion, but spells both differently.
                "status": "completed" if finished else status,
                "conclusion": _BUILD_CONCLUSIONS.get(result, result or None) if finished else None,
                "url": _obj(_obj(build.get("_links")).get("web")).get("href"),
                "event": build.get("reason"),
                "created_at": build.get("queueTime") or build.get("startTime"),
                "cancellable": not finished,
                "rerunnable": finished,
            }
        )
    return out


def _read_build(org: str, project: str, run_id: int, *, host: str, timeout: float) -> dict:
    """One pipeline build, by id."""
    return _obj(
        _az_invoke(
            org=org,
            area="build",
            resource="builds",
            host=host,
            timeout=timeout,
            route={"project": project, "buildId": int(run_id)},
            api_version=_API_BUILD,
        )
    )


def _assert_build_belongs_to_repo(
    org: str, project: str, run_id: int, name: str, *, host: str, timeout: float
) -> dict:
    """Refuse a build id that is not this repository's, returning it when it is.

    A build id is PROJECT-scoped on Azure, not repository-scoped, so a caller
    holding a connected repo A can name a build belonging to repo B in the same
    project and the mutation would land on B: the route's connected-repo gate
    authorizes A and never sees that the id points elsewhere. Every run mutation
    therefore reads the build first and compares its repository, which is the only
    place that binding can be checked.

    Compared EXACTLY, matching the rest of this client: Azure repository names are
    not documented as case-insensitive, so folding could accept a different
    repository that differs only by case.
    """
    build = _read_build(org, project, run_id, host=host, timeout=timeout)
    owner_name = str(_obj(build.get("repository")).get("name") or "")
    if not owner_name:
        raise ProviderCliError(
            f"could not determine which repository build {int(run_id)} belongs to, "
            "so the request is refused"
        )
    if owner_name != name:
        raise ProviderCliError(
            f"build {int(run_id)} belongs to repository {owner_name!r}, not {name!r} -- "
            "a build id is project-scoped, so it is refused rather than acted on"
        )
    return build


def cancel_workflow_run(
    owner: str, repo: str, run_id: int, *, host: str = "", timeout: float = AZ_TIMEOUT_SEC
) -> dict:
    """Cancel one in-flight pipeline build.

    Azure cancels by PATCHing the build to ``cancelling`` -- the participle is the
    real value: the request records the intent and the agent stops the build
    shortly after, so a successful call means "cancellation accepted", not
    "already stopped". A build that has already finished is refused by Azure and
    surfaces as an error rather than as a no-op success, because a cancel that
    silently does nothing is indistinguishable from one that worked.

    Returns ``{run_id, cancelled: True}``.
    """
    org, project = _split_owner(owner)
    name = _check_repo(repo)
    _assert_build_belongs_to_repo(org, project, run_id, name, host=host, timeout=timeout)
    _az_invoke(
        org=org,
        area="build",
        resource="builds",
        host=host,
        timeout=timeout,
        route={"project": project, "buildId": int(run_id)},
        method="PATCH",
        body={"status": "cancelling"},
        api_version=_API_BUILD,
    )
    return {"run_id": int(run_id), "cancelled": True}


def rerun_workflow_run(
    owner: str,
    repo: str,
    run_id: int,
    *,
    failed_only: bool = False,
    host: str = "",
    timeout: float = AZ_TIMEOUT_SEC,
) -> dict:
    """Re-queue a finished pipeline build.

    Azure has no "re-run" verb: a retry is a NEW build queued from the same
    definition, branch and commit, which is why the original is read first. The
    returned ``run_id`` is the NEW build's id, so a caller can follow what it
    actually started rather than polling a run that will never change again.

    ``failed_only`` is accepted for signature parity and cannot be honoured -- Azure
    re-runs whole stages, not the failed jobs of a completed build -- so the
    returned ``failed_only`` reports what Azure actually did (``False``), never what
    was asked. A caller is therefore not told a cheap partial retry happened when a
    full build was queued.
    """
    del failed_only
    org, project = _split_owner(owner)
    _check_repo(repo)
    original = _assert_build_belongs_to_repo(
        org, project, run_id, _check_repo(repo), host=host, timeout=timeout
    )
    definition_id = _obj(original.get("definition")).get("id")
    if not isinstance(definition_id, int):
        raise ProviderCliError(f"could not read the pipeline definition of build {int(run_id)}")
    payload: dict[str, object] = {"definition": {"id": definition_id}}
    # Pinned to the ORIGINAL branch and commit. Queuing without them would build
    # the branch's current tip, which is a different revision than the one the user
    # asked to retry.
    if original.get("sourceBranch"):
        payload["sourceBranch"] = original["sourceBranch"]
    if original.get("sourceVersion"):
        payload["sourceVersion"] = original["sourceVersion"]
    queued = _obj(
        _az_invoke(
            org=org,
            area="build",
            resource="builds",
            host=host,
            timeout=timeout,
            route={"project": project},
            method="POST",
            body=payload,
            api_version=_API_BUILD,
        )
    )
    new_id = queued.get("id")
    return {
        "run_id": new_id if isinstance(new_id, int) else int(run_id),
        "rerun": True,
        "failed_only": False,
    }
