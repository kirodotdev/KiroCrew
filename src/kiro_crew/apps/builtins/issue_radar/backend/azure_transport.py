"""Azure DevOps URL and ``az devops invoke`` transport helpers.

The security-sensitive spawn remains in :mod:`azure_client`.  Helpers in this
module build and interpret a call, while the facade injects its current spawn and
response bindings so the established ``azure_client`` monkeypatch seam remains
authoritative.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from urllib.parse import quote, unquote, urlparse

from .errors import (
    ProviderCliError,
    ProviderPermissionError,
    ProviderSetupError,
    RepoUrlError,
    sanitize_cli_stderr,
)

AZURE_HOST = "dev.azure.com"
_LEGACY_HOST_SUFFIX = ".visualstudio.com"

_PAGE_SIZE = 100
_MAX_PAGES = 40

_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")
_RESERVED_SEGMENTS = frozenset({"_git", "_apis", "_workitems", "_build", "_settings", "_apps"})
_URL_PATH_SEPARATOR = re.compile(r"/+")

_AZ_ENV_PASSTHROUGH = (
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

_AZ_AUTH_MARKERS = (
    "az login",
    "az devops login",
    "tf400813",
    "please run 'az login'",
    "unauthorized",
    "401",
    "before you can run this command you need to log in",
)

_AZ_MISSING_MARKERS = (
    'is not in the "az" command group',
    "az extension add",
    "extension is not installed",
    "no such command",
    "command not found",
)


def _bad_segment(segment: str) -> bool:
    """Whether a path segment is unusable as an org / project / repo name."""
    if not segment or segment in (".", ".."):
        return True
    if segment.lower() in _RESERVED_SEGMENTS:
        return True
    return not bool(_SEGMENT_RE.match(segment))


def _url_path_segments(path: str) -> list[str]:
    """Return non-empty URL path segments without platform path semantics."""
    return [segment for segment in _URL_PATH_SEPARATOR.split(path or "") if segment]


def parse_azure_repo_url(link: str) -> tuple[str, str]:
    """Parse ``("{organization}/{project}", repository)`` from an Azure URL."""
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
    try:
        host = (parsed.hostname or "").lower().rstrip(".")
        _ = parsed.port
    except ValueError as exc:
        raise RepoUrlError(f"malformed host or port in {link!r}") from exc

    legacy_org = ""
    if host == AZURE_HOST:
        pass
    elif host.endswith(_LEGACY_HOST_SUFFIX):
        legacy_org = host[: -len(_LEGACY_HOST_SUFFIX)]
        if _bad_segment(legacy_org):
            raise RepoUrlError(f"invalid organization in {link!r}")
    else:
        raise RepoUrlError(
            f"not a supported Azure DevOps host: {link!r} -- only {AZURE_HOST} and the "
            "legacy {org}.visualstudio.com form are accepted (Azure DevOps Server is not supported)"
        )

    parts: list[str] = []
    for raw in _url_path_segments(parsed.path or ""):
        segment = unquote(raw)
        if "/" in segment or "\\" in segment or "?" in segment or "#" in segment:
            raise RepoUrlError(f"invalid path segment in {link!r}")
        parts.append(segment)

    if legacy_org:
        org = legacy_org
    else:
        if not parts:
            raise RepoUrlError(f"not a full Azure DevOps URL: {link!r}")
        org, parts = parts[0], parts[1:]
    if not parts:
        raise RepoUrlError(
            f"not a full Azure DevOps URL: {link!r} "
            "(expected .../{project} or .../{project}/_git/{repo})"
        )
    project, parts = parts[0], parts[1:]

    if parts and parts[0] == "_git":
        repo = parts[1] if len(parts) > 1 else project
    else:
        repo = project
    repo = re.sub(r"\.git$", "", repo)

    for segment in (org, project, repo):
        if _bad_segment(segment):
            raise RepoUrlError(f"invalid path segment in {link!r}")
    return f"{org}/{project}", repo


def _split_owner(owner: str) -> tuple[str, str]:
    """Split and validate the provider-neutral Azure owner value."""
    text = str(owner or "").strip().strip("/")
    org, separator, project = text.partition("/")
    if not separator:
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


def _org_url(org: str, *, quote_value: Callable[..., str] = quote) -> str:
    """Return the pinned-cloud organization URL consumed by ``az``."""
    return f"https://{AZURE_HOST}/{quote_value(org, safe='')}"


def _resolve_host(host: str) -> str:
    """Re-check the pinned Azure cloud host at the spawn boundary."""
    if not host:
        raise ProviderCliError("an Azure DevOps host is required for az calls")
    normalized = host.lower().rstrip(".")
    if normalized != AZURE_HOST:
        raise ProviderCliError(
            f"Azure DevOps host {normalized!r} is not supported -- only {AZURE_HOST} "
            "(Azure DevOps Server / on-premises has no supported credential path)"
        )
    return normalized


def _az_env(
    host: str,
    *,
    source_env: Mapping[str, str],
    passthrough_keys: tuple[str, ...],
    minimal_env: Callable[..., dict[str, str]],
) -> dict[str, str]:
    """Build the minimal ``az`` environment for the already-resolved host."""
    passthrough = {key: source_env[key] for key in passthrough_keys if key in source_env}
    if host != AZURE_HOST:
        passthrough.pop("AZURE_DEVOPS_EXT_PAT", None)
    # Automated provider calls must never download and execute an extension.
    passthrough["AZURE_EXTENSION_USE_DYNAMIC_INSTALL"] = "no"
    passthrough["AZURE_CORE_COLLECT_TELEMETRY"] = "0"
    passthrough["AZURE_CORE_NO_COLOR"] = "1"
    passthrough["NO_COLOR"] = "1"
    return minimal_env(**passthrough)


def _raise_if_setup_failure(stderr_tail: str) -> None:
    """Classify extension/auth failures before generic CLI failures."""
    low = (stderr_tail or "").lower()
    if any(marker in low for marker in _AZ_MISSING_MARKERS):
        raise ProviderSetupError(
            "the `azure-devops` extension for the `az` CLI is not available -- "
            "install it with `az extension add --name azure-devops`",
            reason="not_installed",
        )
    if any(marker in low for marker in _AZ_AUTH_MARKERS):
        raise ProviderSetupError(
            f"the `az` CLI is not authenticated for {AZURE_HOST} -- run `az login` "
            "(or `az devops login` with a personal access token)",
            reason="not_authenticated",
        )


def _stderr_tail(
    proc: subprocess.CompletedProcess, *, sanitize: Callable[[str], str] = sanitize_cli_stderr
) -> str:
    return sanitize(" ".join((proc.stderr or "").strip().splitlines()[-3:]))


def _is_forbidden(tail: str) -> bool:
    low = (tail or "").lower()
    return "403" in low or "forbidden" in low or "does not have permission" in low


def _body_file(body: object) -> str:
    """Write a request body to a unique 0600 file; the caller must unlink it."""
    fd, path = tempfile.mkstemp(prefix="kirocrew-az-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(body, handle)
    except Exception:
        os.unlink(path)
        raise
    return path


def invoke(
    *,
    run: Callable[..., subprocess.CompletedProcess],
    body_file: Callable[[object], str],
    org_url: Callable[[str], str],
    raise_if_setup_failure: Callable[[str], None],
    stderr_tail: Callable[[subprocess.CompletedProcess], str],
    is_forbidden: Callable[[str], bool],
    org: str,
    area: str,
    resource: str,
    host: str,
    timeout: float,
    route: dict[str, object] | None,
    query: dict[str, object] | None,
    method: str,
    body: object | None,
    api_version: str,
    media_type: str,
) -> object:
    """Build one invoke argv, run it through the facade, and parse its response."""
    argv = [
        "az",
        "devops",
        "invoke",
        "--org",
        org_url(org),
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
            path = body_file(body)
            argv += ["--in-file", path]
        proc = run(argv, host=host, timeout=timeout)
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass

    target = f"{area}/{resource}"
    if proc.returncode != 0:
        tail = stderr_tail(proc)
        raise_if_setup_failure(tail)
        if is_forbidden(tail):
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


def invoke_paged(
    *,
    invoke_one: Callable[..., object],
    values: Callable[[object], list[dict]],
    page_size: int,
    max_pages: int,
    org: str,
    area: str,
    resource: str,
    host: str,
    timeout: float,
    route: dict[str, object] | None,
    query: dict[str, object] | None,
    api_version: str,
    limit: int,
) -> list[dict]:
    """Walk Azure's bounded ``$top``/``$skip`` pagination."""
    out: list[dict] = []
    skip = 0
    for _ in range(max_pages):
        top = page_size
        if limit:
            remaining = limit - len(out)
            if remaining <= 0:
                break
            top = min(page_size, remaining)
        page_query = dict(query or {})
        page_query["$top"] = top
        page_query["$skip"] = skip
        rows = values(
            invoke_one(
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
