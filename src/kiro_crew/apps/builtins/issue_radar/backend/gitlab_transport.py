"""Pure GitLab URL, environment, and API transport helpers.

The executable lookup and real subprocess spawn stay in ``gitlab_client``: the
spawn audit identifies that exact module/function as the reviewed chokepoint.
Callers inject the current facade bindings so tests and provider integrations can
continue patching ``gitlab_client`` without reaching into this module.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Mapping
from urllib.parse import quote, urlparse

from .errors import (
    ProviderCliError,
    ProviderPermissionError,
    ProviderSetupError,
    RepoUrlError,
)

SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_URL_PATH_SEPARATOR = "/"
GITLAB_RESERVED_SEGMENTS = frozenset(
    {"-", "groups", "projects", "admin", "dashboard", "explore", "help", "users", "api"}
)

GLAB_ENV_PASSTHROUGH = (
    "GITLAB_TOKEN",
    "GLAB_CONFIG_DIR",
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

GLAB_AUTH_MARKERS = (
    "glab auth login",
    "not logged in",
    "no token provided",
    "authentication required",
    "requires authentication",
    "401 unauthorized",
    "http 401",
)


def parse_gitlab_repo_url(
    link: str, *, allowed_hosts: frozenset[str] = frozenset()
) -> tuple[str, str, str]:
    """Parse a validated ``(host, namespace, project)`` from a GitLab URL."""
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

    # hostname/port parse lazily, so malformed authorities must be translated to
    # the same client-input error as every other unusable URL.
    try:
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError as exc:
        raise RepoUrlError(f"malformed host or port in {link!r}") from exc

    # A portless allowlist entry must not authorize arbitrary ports. Explicit
    # HTTPS :443 is canonicalized away to match browser URL handling.
    candidate = f"{host}:{port}" if port and port != 443 else host
    if host in {"gitlab.com", "www.gitlab.com"}:
        resolved_host = "gitlab.com"
    elif host and candidate in allowed_hosts:
        resolved_host = candidate
    else:
        raise RepoUrlError(
            f"not a supported GitLab host: {link!r} — gitlab.com is always accepted; "
            "a self-managed instance must be listed in the dashboard.gitlab_hosts config"
        )

    path = (parsed.path or "").rstrip("/")
    marker_index = path.find("/-/")
    if marker_index >= 0:
        path = path[:marker_index]
    # URL paths use a forward slash on every host platform; this is not a
    # filesystem path separator.
    parts = [part for part in path.split(_URL_PATH_SEPARATOR) if part]
    if len(parts) < 2:
        raise RepoUrlError(f"not a full project URL: {link!r} (expected .../<group>/<project>)")
    parts[-1] = re.sub(r"\.git$", "", parts[-1])
    for segment in parts:
        if segment in (".", "..") or not SEGMENT_RE.match(segment):
            raise RepoUrlError(f"invalid path segment in {link!r}")
    if any(segment.lower() in GITLAB_RESERVED_SEGMENTS for segment in parts):
        raise RepoUrlError(f"{link!r} is a GitLab system path, not a project")
    return resolved_host, _URL_PATH_SEPARATOR.join(parts[:-1]), parts[-1]


def project_path(owner: str, repo: str) -> str:
    """Encode a namespace/project as GitLab's single ``:id`` parameter."""
    return quote(f"{owner}/{repo}", safe="")


def resolve_host(host: str, *, allowed_hosts: frozenset[str]) -> str:
    """Authorize and canonicalize a host at the subprocess boundary."""
    if not host:
        raise ProviderCliError("a GitLab host is required for glab calls")
    normalized = host.lower().rstrip(".")
    if normalized in {"gitlab.com", "www.gitlab.com"}:
        return "gitlab.com"
    if normalized not in allowed_hosts:
        raise ProviderCliError(
            f"GitLab host {normalized!r} is not listed in the dashboard.gitlab_hosts allowlist"
        )
    return normalized


def glab_env(
    host: str,
    *,
    source_env: Mapping[str, str],
    passthrough_keys: tuple[str, ...],
    minimal_env: Callable[..., dict[str, str]],
) -> dict[str, str]:
    """Build a minimal, host-pinned environment for ``glab``."""
    passthrough = {key: source_env[key] for key in passthrough_keys if key in source_env}
    # GITLAB_TOKEN has no host binding and must never cross into a private host.
    if host != "gitlab.com":
        passthrough.pop("GITLAB_TOKEN", None)
    passthrough["GITLAB_HOST"] = host
    passthrough["GLAB_PAGER"] = "cat"
    passthrough["NO_COLOR"] = "1"
    return minimal_env(**passthrough)


def raise_if_auth_failure(stderr_tail: str, host: str, *, markers: tuple[str, ...]) -> None:
    """Map an unauthenticated CLI failure to an actionable setup error."""
    low = (stderr_tail or "").lower()
    if any(marker in low for marker in markers):
        raise ProviderSetupError(
            f"the `glab` CLI is not authenticated for {host} — "
            f"run `glab auth login --hostname {host}`",
            reason="not_authenticated",
        )


def stderr_tail(proc: subprocess.CompletedProcess, *, sanitize: Callable[[str], str]) -> str:
    """Return the sanitized final three stderr lines."""
    return sanitize(" ".join((proc.stderr or "").strip().splitlines()[-3:]))


def is_forbidden(tail: str) -> bool:
    """Whether stderr identifies an authorization failure."""
    low = (tail or "").lower()
    return "403" in low or "forbidden" in low or "insufficient" in low


def glab_api(
    path: str,
    *,
    host: str,
    timeout: float,
    paginate: bool,
    method: str,
    body: dict | None,
    run: Callable[..., subprocess.CompletedProcess],
    page_size: int,
    max_pages: int,
    stderr_tail: Callable[[subprocess.CompletedProcess], str],
    raise_if_auth_failure: Callable[[str, str], None],
    is_forbidden: Callable[[str], bool],
) -> object:
    """Run ``glab api`` through an injected spawn and parse explicit pages."""
    pages: list[object] = []
    page = 1
    while True:
        separator = "&" if "?" in path else "?"
        target = f"{path}{separator}page={page}&per_page={page_size}" if paginate else path
        argv = ["glab", "api", target]
        if method != "GET":
            argv += ["--method", method]
        input_text = None
        if body is not None:
            argv += ["--input", "-"]
            input_text = json.dumps(body)

        proc = run(argv, host=host, timeout=timeout, input_text=input_text)
        if proc.returncode != 0:
            tail = stderr_tail(proc)
            raise_if_auth_failure(tail, host)
            if is_forbidden(tail):
                raise ProviderPermissionError(
                    f"glab api {path} was forbidden (exit {proc.returncode}): {tail}"
                )
            raise ProviderCliError(f"glab api {path} failed (exit {proc.returncode}): {tail}")

        text = (proc.stdout or "").strip()
        if not text:
            data: object = [] if paginate else {}
        else:
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ProviderCliError(f"glab returned unexpected output for {path}") from exc
        if not paginate:
            return data
        if not isinstance(data, list):
            return data
        pages.extend(data)
        if len(data) < page_size or page >= max_pages:
            return pages
        page += 1
