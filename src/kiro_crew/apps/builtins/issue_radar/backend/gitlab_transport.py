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
import time
from collections.abc import Callable, Mapping
from urllib.parse import quote, urlparse

from .errors import (
    ProviderCliError,
    ProviderPermissionError,
    ProviderSetupError,
    RepoUrlError,
)

#: GitLab's own ceiling on a single path segment. ``Namespace`` validates ``path``
#: with ``length: { maximum: URL_MAX_LENGTH }``, and that constant is 255. Each
#: segment becomes one filesystem component, so 255 is also ``NAME_MAX`` -- this
#: bound keeps every individual segment inside a path component. Bounding a
#: segment's SIZE and the segment COUNT below together bound the whole parsed
#: value, keeping ``owner/repo`` well inside the one argv element it rides in.
GITLAB_MAX_SEGMENT_CHARS = 255
SEGMENT_RE = re.compile(rf"^[A-Za-z0-9._-]{{1,{GITLAB_MAX_SEGMENT_CHARS}}}$")
_URL_PATH_SEPARATOR = "/"

#: GitLab's own ceiling on how deep a group may sit. ``Namespace`` validates
#: ``nesting_level_allowed``, which refuses a group whose ``ancestors.count``
#: exceeds ``Group::NUMBER_OF_ANCESTORS_ALLOWED``; that constant is 20, and the
#: documented maximum hierarchy depth states the same number
#: (https://docs.gitlab.com/user/group/#recommended-limits-for-group-structure).
GITLAB_MAX_GROUP_ANCESTORS = 20

#: The most group segments a namespace can legally carry. ``ancestors`` excludes
#: the group itself, so the deepest accepted path is the ancestors that check
#: allows plus the group they are ancestors OF -- which is the one extra segment.
MAX_NAMESPACE_SEGMENTS = GITLAB_MAX_GROUP_ANCESTORS + 1
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


def _parse_https_link(link: str, *, allowed_hosts: frozenset[str]) -> tuple[str, str]:
    """Return the authorized ``(host, raw path)`` of an https GitLab link."""
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
    return resolved_host, parsed.path or ""


def parse_gitlab_repo_url(
    link: str, *, allowed_hosts: frozenset[str] = frozenset()
) -> tuple[str, str, str]:
    """Parse a validated ``(host, namespace, project)`` from a GitLab URL."""
    if not link or not isinstance(link, str):
        raise RepoUrlError("repo link is empty")
    resolved_host, raw_path = _parse_https_link(link, allowed_hosts=allowed_hosts)

    path = raw_path.rstrip("/")
    marker_index = path.find("/-/")
    if marker_index >= 0:
        path = path[:marker_index]
    # URL paths use a forward slash on every host platform; this is not a
    # filesystem path separator.
    parts = [part for part in path.split(_URL_PATH_SEPARATOR) if part]
    if len(parts) < 2:
        raise RepoUrlError(f"not a full project URL: {link!r} (expected .../<group>/<project>)")
    # Nesting bounds the namespace at BOTH ends: at least one group above, and no
    # more group levels than GitLab itself will create below. The loop after this
    # checks each segment's SHAPE; this checks their COUNT, which is the thing that
    # decides how deep a path every consumer of the namespace goes on to build.
    if len(parts) - 1 > MAX_NAMESPACE_SEGMENTS:
        raise RepoUrlError(
            f"namespace in {link!r} is nested deeper than GitLab allows "
            f"(at most {MAX_NAMESPACE_SEGMENTS} group levels)"
        )
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


def validate_group_path(path: str) -> str:
    """Return ``path`` when it is a well-formed GitLab group path, else raise.

    A group path is one to :data:`MAX_NAMESPACE_SEGMENTS` segments joined by a
    slash (``acme`` or ``acme/platform/web``). Every segment passes the same shape
    check as a project URL's segments, and GitLab's system paths are refused, so
    the value can only ever address one group and never a different API route.
    """
    if not isinstance(path, str):
        raise RepoUrlError("group path must be a string")
    parts = path.split(_URL_PATH_SEPARATOR)
    if not path or not all(parts):
        raise RepoUrlError(f"not a GitLab group path: {path!r}")
    if len(parts) > MAX_NAMESPACE_SEGMENTS:
        raise RepoUrlError(
            f"group path {path!r} is nested deeper than GitLab allows "
            f"(at most {MAX_NAMESPACE_SEGMENTS} group levels)"
        )
    for segment in parts:
        if segment in (".", "..") or not SEGMENT_RE.match(segment):
            raise RepoUrlError(f"invalid group path segment in {path!r}")
        if segment.lower() in GITLAB_RESERVED_SEGMENTS:
            raise RepoUrlError(f"{path!r} is a GitLab system path, not a group")
    return path


def parse_gitlab_group(
    value: str, *, allowed_hosts: frozenset[str] = frozenset()
) -> tuple[str, str]:
    """Parse a validated ``(host, group path)`` from a group link or a bare path.

    A bare path such as ``acme/platform`` is a gitlab.com group. A pasted group
    link (``https://gitlab.com/acme/platform``, with or without GitLab's legacy
    ``/groups/`` prefix and any ``/-/…`` page suffix) names its own instance,
    which must be gitlab.com or listed in ``dashboard.gitlab_hosts``.
    """
    if not value or not isinstance(value, str) or not value.strip():
        raise RepoUrlError("group is empty")
    text = value.strip()
    if "://" in text:
        resolved_host, raw_path = _parse_https_link(text, allowed_hosts=allowed_hosts)
        marker_index = raw_path.find("/-/")
        if marker_index >= 0:
            raw_path = raw_path[:marker_index]
        path = raw_path.strip(_URL_PATH_SEPARATOR)
        legacy_prefix = "groups" + _URL_PATH_SEPARATOR
        if path.startswith(legacy_prefix):
            path = path[len(legacy_prefix) :]
    else:
        resolved_host = "gitlab.com"
        path = text.strip(_URL_PATH_SEPARATOR)
    return resolved_host, validate_group_path(path)


def group_path(path: str) -> str:
    """Validate and encode a group path as GitLab's single ``:id`` parameter."""
    return quote(validate_group_path(path), safe="")


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


def is_not_found(tail: str) -> bool:
    """Whether stderr carries GitLab's 404 answer.

    Both the status code and the phrase must appear. A group path or a username
    can contain ``404`` but never a space, so a request URL echoed in the tail
    cannot match on its own.
    """
    text = tail or ""
    return bool(re.search(r"\b404\b", text)) and "not found" in text.lower()


def is_forbidden(tail: str) -> bool:
    """Whether stderr identifies an authorization failure."""
    low = (tail or "").lower()
    return "403" in low or "forbidden" in low or "insufficient" in low


#: Seconds to wait before each re-send of a request GitLab refused with HTTP 429.
#: Opening one project fans out into dozens of calls (every open-issue page, the
#: open MR list, then notes + labels + label/state events + pipelines per detail
#: pane), and a self-managed instance's per-user throttle trips on that burst.
#: ``glab api`` exits on the first 429 without retrying and reports no
#: ``Retry-After``, so the transport backs off itself. Three short waits clear a
#: burst window and cap the added latency at seven seconds per call, however
#: many pages the call reads.
RATE_LIMIT_BACKOFF_SEC: tuple[float, ...] = (1.0, 2.0, 4.0)


def is_rate_limited(tail: str) -> bool:
    """Whether stderr identifies a GitLab HTTP 429 throttle response."""
    low = (tail or "").lower()
    return "http 429" in low or "too many requests" in low


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
    sleep: Callable[[float], None] = time.sleep,
) -> object:
    """Run ``glab api`` through an injected spawn and parse explicit pages.

    A page GitLab throttles (HTTP 429) is re-sent after the next delay in
    ``RATE_LIMIT_BACKOFF_SEC``. The delays are one budget for the whole call, not
    per page, so a long list waits at most seven seconds in total; once they are
    spent the call fails, so a short burst window costs latency instead of an
    error pane. A 429 means the request was
    not processed, so re-sending a write is as safe as re-sending a read.
    """
    pages: list[object] = []
    page = 1
    throttled = 0
    while True:
        separator = "&" if "?" in path else "?"
        target = f"{path}{separator}page={page}&per_page={page_size}" if paginate else path
        argv = ["glab", "api", target]
        if method != "GET":
            argv += ["--method", method]
        input_text = None
        if body is not None:
            # Strict GitLab instances reject a body without an explicit
            # Content-Type as HTTP 415; glab does not set one for stdin input.
            argv += ["--header", "Content-Type: application/json", "--input", "-"]
            input_text = json.dumps(body)

        proc = run(argv, host=host, timeout=timeout, input_text=input_text)
        if proc.returncode != 0:
            tail = stderr_tail(proc)
            raise_if_auth_failure(tail, host)
            if is_forbidden(tail):
                raise ProviderPermissionError(
                    f"glab api {path} was forbidden (exit {proc.returncode}): {tail}"
                )
            if is_rate_limited(tail):
                if throttled < len(RATE_LIMIT_BACKOFF_SEC):
                    sleep(RATE_LIMIT_BACKOFF_SEC[throttled])
                    throttled += 1
                    continue
                raise ProviderCliError(
                    f"GitLab is rate-limiting requests (HTTP 429) and glab api {path} "
                    f"still failed after {throttled} retries; try again in a minute"
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
