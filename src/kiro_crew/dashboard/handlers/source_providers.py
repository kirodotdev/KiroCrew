"""Pull-request source data and owner-only review-thread mutation.

The browser sends a GitHub pull-request or GitLab merge-request URL. This
module validates the parsed host and path, then delegates authentication to a
validated absolute provider CLI. Credentials stay inside ``gh``/``glab`` and are
never returned to the browser. Credential-backed access is restricted to the
configured dashboard owner. Standalone local dashboards use their signed
bootstrap identity as the implicit owner when no channel owner is configured.
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import hashlib
import json
import logging
import os
import re
import stat
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import quote, urlparse, urlunparse

from aiohttp import web

from kiro_crew import platform_compat
from kiro_crew.sandbox import create_subprocess_limited, sandboxed_spawn_argv
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

_MAX_URL_LENGTH = 2048
# Hard per-section limits enforced while draining provider stdout. Diff-bearing
# sections get more room than metadata/checks, but no subprocess may retain the
# old payload-sized allowance independently.
_METADATA_OUTPUT_BYTES = 1 * 1024 * 1024
_DISCUSSION_OUTPUT_BYTES = 2 * 1024 * 1024
_DIFF_OUTPUT_BYTES = 4 * 1024 * 1024
_CHECKS_OUTPUT_BYTES = 1 * 1024 * 1024
_MAX_ERROR_BYTES = 64 * 1024
# Bound the normalized aggregate returned to the browser. Reservations below
# conservatively cover raw bytes, decoded JSON, normalized copies, and Python
# object overhead while a complete direct fetch remains alive.
_MAX_PAYLOAD_BYTES = 8 * 1024 * 1024
_SECONDARY_PAGE_SIZE = 100
_COMMAND_TIMEOUT_SECS = 30
_CACHE_TTL_SECS = 30
_CACHE_MAX_ENTRIES = 32
_CACHE_MAX_BYTES = 48 * 1024 * 1024
_PROVIDER_CONCURRENCY = 4
# Bound direct full/check fetches by task count and retained-memory weight.
# Same-URL callers coalesce before admission; detached stale tasks keep their
# reservation until their underlying task actually completes.
_DIRECT_FETCH_PENDING_MAX = 16
_DIRECT_FETCH_MAX_RESERVED_BYTES = 128 * 1024 * 1024
_FULL_FETCH_RESERVATION_BYTES = 64 * 1024 * 1024
_CHECKS_FETCH_RESERVATION_BYTES = 8 * 1024 * 1024
# An issue payload is metadata plus comments -- no diffs, no check rollup -- so
# both its aggregate cache and its retained-memory lease sit well below the
# pull-request figures. TTL and entry count are shared with the PR cache.
_ISSUE_CACHE_MAX_BYTES = 16 * 1024 * 1024
_ISSUE_FETCH_RESERVATION_BYTES = 16 * 1024 * 1024
_PROVIDER_EXECUTABLE_OVERRIDES = {
    "gh": "KIROCREW_GH_BIN",
    "glab": "KIROCREW_GLAB_BIN",
}
# Opt-in hardening for shared/multi-tenant hosts: restore the historical rule
# that a provider CLI must be root-owned and unwritable by the gateway user.
# Off by default — see _validate_provider_executable for the current policy.
_STRICT_PROVIDER_BIN_ENV = "KIROCREW_PROVIDER_BIN_STRICT"
_TRUTHY_ENV_VALUES = frozenset({"1", "true", "yes", "on"})
# Well-known install dirs searched (in order) before the ambient PATH. Shared
# with Issue Radar's gh resolver (issue_radar/backend/github_client.py) so both
# panels accept exactly the same set of gh locations and never drift.
_PROVIDER_EXECUTABLE_DIRS = (
    "/usr/local/libexec/kirocrew",
    "/usr/libexec/kirocrew",
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin",
    "/home/linuxbrew/.linuxbrew/bin",
)
_PROVIDER_EXECUTABLE_CANDIDATES = {
    executable: tuple(
        f"{directory}/{executable}" for directory in _PROVIDER_EXECUTABLE_DIRS
    )
    for executable in ("gh", "glab")
}
# Provider commands are absolute. Keep PATH deterministic only for trusted
# system helpers a provider may invoke; never inherit a workspace-controlled
# PATH or search it for gh/glab.
_PROVIDER_SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
# Only variables needed to configure the provider CLI, reach its API, and use
# that provider's authentication cross this trust boundary. In particular,
# unrelated gateway/AWS/Slack credentials and arbitrary PATH entries are never
# inherited.
_PROVIDER_BASE_ENV_KEYS = frozenset(
    {
        "APPDATA",
        "COMSPEC",
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "NO_PROXY",
        "PATHEXT",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "XDG_CONFIG_HOME",
        "https_proxy",
        "http_proxy",
        "no_proxy",
    }
)
_PROVIDER_AUTH_ENV_KEYS = {
    "gh": frozenset({"GH_CONFIG_DIR", "GH_TOKEN", "GITHUB_TOKEN"}),
    "glab": frozenset({"GLAB_CONFIG_DIR", "GITLAB_TOKEN"}),
}
# url -> (stored_at, serialized_size_bytes, normalized_payload)
_CACHE: dict[str, tuple[float, int, dict[str, Any]]] = {}
_CACHE_LOCK = asyncio.Lock()
_FULL_FETCH_INFLIGHT: dict[str, asyncio.Task[dict[str, Any]]] = {}
_FULL_FETCH_TASKS: dict[str, set[asyncio.Task[dict[str, Any]]]] = {}
_FULL_FETCH_GENERATIONS: dict[str, int] = {}
_CHECKS_FETCH_INFLIGHT: dict[str, asyncio.Task[list[dict[str, Any]]]] = {}
# Issues get their own cache and inflight map rather than sharing the
# pull-request ones: the two live at different URLs but the same normalized-URL
# key space would still be shared, and a PR mutation's cache invalidation
# (_invalidate_pull_request_cache) must not evict issue payloads it knows
# nothing about. No generation map is needed -- this phase never mutates an
# issue, so there is no post-mutation write to order against.
_ISSUE_CACHE: dict[str, tuple[float, int, dict[str, Any]]] = {}
_ISSUE_CACHE_LOCK = asyncio.Lock()
_ISSUE_FETCH_INFLIGHT: dict[str, asyncio.Task[dict[str, Any]]] = {}
_ISSUE_FETCH_TASKS: dict[str, set[asyncio.Task[dict[str, Any]]]] = {}
_DIRECT_FETCH_RESERVATIONS: dict[asyncio.Task[Any], int] = {}
_provider_semaphore = asyncio.Semaphore(_PROVIDER_CONCURRENCY)
_SAFE_ERROR_RE = re.compile(r"\s+")
_PROVIDER_TOOL_NAME = "source_provider_cli"
logger = logging.getLogger(__name__)


class SourceProviderError(RuntimeError):
    """A provider CLI could not return the requested source data."""


def _sel():
    import kiro_crew.dashboard.handlers as _pkg  # circular import: package exports this module

    return _pkg.sel()


def _audit_provider_cli(
    executable: str,
    outcome: str,
    reason: str,
    *,
    critical: bool = False,
) -> None:
    """Emit a credential-free provider lifecycle event."""
    provider = executable if executable in {"gh", "glab"} else "unknown"
    try:
        _sel().log_tool_invocation(
            session_key="dashboard:source-provider",
            source="dashboard",
            tool_name=_PROVIDER_TOOL_NAME,
            tool_kind="provider_cli",
            outcome=outcome,
            downstream_service=provider,
            error=reason,
            metadata={"provider": provider, "reason": reason},
            critical=critical,
        )
    except Exception:
        if critical:
            raise
        logger.debug("SEL provider CLI audit failed", exc_info=True)


def _path_parents(path: Path) -> list[Path]:
    """Return every parent through the filesystem root."""
    parents: list[Path] = []
    current = path.parent
    while True:
        parents.append(current)
        if current.parent == current:
            return parents
        current = current.parent


def _strict_provider_bins() -> bool:
    """True when the operator opted into the root-owned-only provider policy."""
    return os.environ.get(_STRICT_PROVIDER_BIN_ENV, "").strip().lower() in _TRUTHY_ENV_VALUES


def _agent_writable_roots() -> tuple[Path, ...]:
    """Trees the agent itself writes: the active project checkout and the LLM
    workspace root (worktrees, venvs, scratch files, downloaded repos).

    A provider CLI resolved inside one of these is refused — a repo-planted
    ``gh`` shim is the substitution vector the model itself controls, and it is
    the same check codex applies to its own sandbox helper (reject a binary
    found inside the workspace, accept anything else).
    """
    raw_roots = [os.environ.get("KIROCREW_PROJECT_DIR")]
    try:
        from kiro_crew.config.loader import workspace_root

        raw_roots.append(str(workspace_root()))
    except Exception:  # pragma: no cover - config unavailable in isolation
        logger.debug("workspace root unavailable for provider CLI validation", exc_info=True)
    roots: list[Path] = []
    for raw in raw_roots:
        if not raw:
            continue
        try:
            roots.append(Path(raw).resolve())
        except OSError:
            continue
    return tuple(roots)


def _check_provider_path_component(path: Path, *, label: str, uid: int, strict: bool) -> None:
    """Apply the ownership/permission policy to one path component."""
    try:
        path_stat = path.stat()
    except OSError as exc:
        raise ValueError("executable hierarchy is not accessible") from exc
    if strict:
        if path_stat.st_uid != 0:
            raise ValueError(f"{label} is not root-owned")
        if path_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH) or os.access(path, os.W_OK):
            raise ValueError(f"{label} is writable by the gateway user")
        return
    # Relaxed policy: the gateway user's own installs are fine; a binary owned
    # by a third account or writable by the whole host is not.
    if path_stat.st_uid not in (0, uid):
        raise ValueError(f"{label} is owned by another user (uid {path_stat.st_uid})")
    if path_stat.st_mode & stat.S_IWOTH:
        # A world-writable DIRECTORY is tolerated when it is sticky (`/tmp`,
        # 1777): only the owner may replace an entry, so the uid check above
        # still decides. Without the sticky bit any local account can swap the
        # entry out, and a world-writable FILE can be rewritten in place.
        if not (stat.S_ISDIR(path_stat.st_mode) and path_stat.st_mode & stat.S_ISVTX):
            raise ValueError(f"{label} is world-writable")


def _validate_provider_executable(candidate: str) -> str:
    """Return the canonical path of a provider CLI we will run, or raise.

    Default policy — *if `gh` works in your terminal, it works here*. Any
    executable the gateway user could run interactively is accepted, including
    the ordinary user-owned Homebrew/Linuxbrew/asdf installs: requiring a
    root-owned copy made every stock ``brew install gh`` fail and pushed users
    into a ``sudo cp`` ritual for a CLI they had already installed and
    authenticated. What stays refused is provenance the user did not choose:

    * a binary (or parent dir) owned by another unprivileged account,
    * anything world-writable, e.g. a ``/tmp`` shim (a world-writable *directory*
      is tolerated only when sticky, where the owner check still decides),
    * anything inside the agent's project checkout or workspace root, the one
      substitution vector the model itself controls (``_agent_writable_roots``).

    Provider children still receive only the minimal, provider-scoped env built
    by ``_provider_env`` (no AWS/Slack/gateway secrets, no inherited PATH), and
    every spawn is SEL-audited — containment and audit carry the trust boundary
    instead of binary provenance.

    A gateway running as **root** is refused outright, in both modes: every
    process it spawns (including the agent's own shell) would be root too, which
    makes the ownership and agent-tree checks vacuous.

    Set ``KIROCREW_PROVIDER_BIN_STRICT=1`` on shared or multi-tenant hosts to
    restore the previous rule: canonical, symlink-free, root-owned and
    unwritable by the gateway user through every parent.
    """
    if not os.path.isabs(candidate):
        raise ValueError("path must be absolute")
    getuid = getattr(os, "getuid", None)
    geteuid = getattr(os, "geteuid", getuid)
    if getuid is None or geteuid is None:
        raise ValueError("filesystem ownership checks are unavailable")
    if geteuid() == 0:
        raise ValueError("provider execution is disabled for a root gateway")
    strict = _strict_provider_bins()

    original = Path(candidate)
    try:
        resolved = original.resolve(strict=True)
    except OSError as exc:
        raise ValueError("path does not exist") from exc
    if strict and original != resolved:
        raise ValueError("path must be canonical and contain no symlinks")

    try:
        if not stat.S_ISREG(resolved.stat().st_mode):
            raise ValueError("path is not a regular file")
    except OSError as exc:
        raise ValueError("executable hierarchy is not accessible") from exc
    if not os.access(resolved, os.X_OK):
        raise ValueError("file is not executable")

    if not strict:
        for root in _agent_writable_roots():
            if resolved == root or root in resolved.parents:
                raise ValueError(f"executable is inside the agent-writable tree {root}")

    uid = geteuid()
    _check_provider_path_component(resolved, label="executable", uid=uid, strict=strict)
    # A symlink's own directory chain is part of the provenance too (relaxed
    # mode allows symlinks, so /opt/homebrew/bin gets checked as well).
    parents = list(_path_parents(resolved))
    if not strict and original != resolved:
        parents += [p for p in _path_parents(original) if p not in parents]
    for parent in parents:
        try:
            if not stat.S_ISDIR(parent.stat().st_mode):
                raise ValueError("executable parent is not a directory")
        except OSError as exc:
            raise ValueError("executable hierarchy is not accessible") from exc
        _check_provider_path_component(parent, label="executable parent", uid=uid, strict=strict)
    return str(resolved)


def provider_executable_candidates(executable: str) -> tuple[str, ...]:
    """Absolute paths to try for *executable*, in resolution order.

    The well-known install dirs come first (a managed root-owned copy still
    wins when one exists), then every ``PATH`` hit — so the install the user
    already runs from their terminal is found even when it lives somewhere this
    module has never heard of (asdf, mise, ``~/.local/bin``). ``PATH`` is not
    consulted in strict mode, which by definition only trusts system dirs.
    """
    ordered: dict[str, None] = dict.fromkeys(_PROVIDER_EXECUTABLE_CANDIDATES.get(executable, ()))
    if not _strict_provider_bins():
        for entry in (os.environ.get("PATH") or "").split(os.pathsep):
            if not entry:
                continue
            found = os.path.join(entry, executable)
            if os.path.isfile(found) and os.access(found, os.X_OK):
                ordered.setdefault(os.path.abspath(found), None)
    return tuple(ordered)


def _provider_setup_message(executable: str, override_name: str, last_error: str) -> str:
    """User-facing guidance when no acceptable provider CLI was found."""
    provider = "GitHub" if executable == "gh" else "GitLab"
    detail = f"\nLast check reported: {last_error}.\n" if last_error else ""
    if _strict_provider_bins():
        managed_dir = os.path.dirname(_PROVIDER_EXECUTABLE_CANDIDATES[executable][0])
        return (
            f"Can't load pull requests: {_STRICT_PROVIDER_BIN_ENV} is set, so this "
            f"host only accepts a root-owned {executable}.\n"
            "\n"
            f"  sudo mkdir -p {managed_dir}\n"
            f'  sudo cp "$(command -v {executable})" {managed_dir}/{executable}\n'
            f"  sudo chown -R root {managed_dir}\n"
            f"  sudo chmod 755 {managed_dir}/{executable}\n"
            f"{detail}"
            "\n"
            f"You won't have to sign in again -- your existing "
            f"`{executable} auth login` credentials are reused automatically.\n"
            "\n"
            f"Alternative: point {override_name} at an already-trusted, absolute "
            f"{executable} path, or unset {_STRICT_PROVIDER_BIN_ENV}."
        )
    return (
        f"Can't load pull requests: the {provider} CLI ({executable}) isn't "
        "available to the Kiro Crew gateway.\n"
        "\n"
        "Install it and sign in, then click Retry:\n"
        "\n"
        f"  brew install {executable}      # or your distro's package manager\n"
        f"  {executable} auth login\n"
        f"{detail}"
        "\n"
        f"Already installed? The gateway searches the standard install dirs plus "
        f"its own PATH and accepts your own {executable} -- Homebrew included. It "
        "still refuses one owned by another user, one that is world-writable, and "
        "one inside your project or workspace tree, since the agent can write "
        "there.\n"
        "\n"
        f"Alternative: point {override_name} at an absolute {executable} path."
    )


def _resolve_provider_executable(executable: str) -> str:
    """Resolve gh/glab: explicit override, well-known install dirs, then PATH."""
    if executable not in _PROVIDER_EXECUTABLE_CANDIDATES:
        raise SourceProviderError("unsupported provider command")
    override_name = _PROVIDER_EXECUTABLE_OVERRIDES[executable]
    override = os.environ.get(override_name)
    if override is not None:
        try:
            return _validate_provider_executable(override)
        except ValueError as exc:
            raise SourceProviderError(
                f"{override_name} is not a trusted executable: {exc}"
            ) from exc

    last_error = ""
    for candidate in provider_executable_candidates(executable):
        try:
            return _validate_provider_executable(candidate)
        except ValueError as exc:
            message = str(exc)
            # "does not exist" is noise on a host that simply lacks that dir;
            # keep the most informative rejection for the setup message.
            if message != "path does not exist":
                last_error = message
            continue
    raise SourceProviderError(_provider_setup_message(executable, override_name, last_error))


@dataclass(frozen=True)
class SourceRef:
    provider: str
    url: str
    host: str
    owner: str
    repo: str
    number: int
    project: str = ""
    # Which namespace the number belongs to: "change" (pull/merge request) or
    # "issue". Defaults to "change" so every pre-existing construction site and
    # test fixture keeps its current meaning. Load-bearing for safety, not just
    # display: GitHub shares one number counter between issues and pull
    # requests, so an issue ref reaching a pull-request-only path would address a
    # DIFFERENT object with the same number. See :func:`_require_change_ref`.
    kind: str = "change"


_GITLAB_HOSTS_TTL_SECS = 30.0
# Cached allowlist snapshot. Populated only by _load_gitlab_hosts() running in a
# worker thread, so every reader on the event loop is a pure dict lookup.
_gitlab_hosts_snapshot: frozenset[str] = frozenset()
_gitlab_hosts_loaded_at = 0.0
# Bumped whenever the snapshot's CONTENT changes. Consumers that memoize a
# parse result (per-slot sidebar source links) fold this into their cache key so
# a later allowlist load invalidates decisions made against the cold snapshot.
_gitlab_hosts_generation = 0
_gitlab_hosts_lock = asyncio.Lock()


def gitlab_hosts_generation() -> int:
    """Monotonic counter identifying the current allowlist snapshot."""
    return _gitlab_hosts_generation


def _gitlab_hosts_fresh() -> bool:
    return bool(
        _gitlab_hosts_loaded_at
        and time.monotonic() - _gitlab_hosts_loaded_at < _GITLAB_HOSTS_TTL_SECS
    )


def _publish_gitlab_hosts(hosts: frozenset[str]) -> None:
    """Install a freshly loaded snapshot, bumping the generation on real change."""
    global _gitlab_hosts_snapshot, _gitlab_hosts_loaded_at, _gitlab_hosts_generation
    if hosts != _gitlab_hosts_snapshot:
        _gitlab_hosts_snapshot = hosts
        _gitlab_hosts_generation += 1
    _gitlab_hosts_loaded_at = time.monotonic()


def _load_gitlab_hosts() -> frozenset[str]:
    """Read the configured self-managed GitLab hosts. BLOCKING -- never on the loop.

    ``KiroCrewConfig.load()`` stats, reads, parses, and validates config files, so
    a slow or network-backed config directory would stall the sole event loop.
    Callers reach this only through :func:`ensure_gitlab_hosts_loaded`.
    """
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        return frozenset(KiroCrewConfig.load().dashboard.gitlab_hosts)
    except Exception:
        logger.debug("self-hosted GitLab allowlist unavailable", exc_info=True)
        return frozenset()


async def ensure_gitlab_hosts_loaded() -> frozenset[str]:
    """Refresh the cached allowlist off the event loop when the TTL has expired.

    Awaited by every async entry point before it validates a URL, so an operator
    adding an instance takes effect within one TTL without a gateway restart and
    without a config read ever running on the loop.

    The refresh is serialized: two concurrent loads could otherwise interleave so
    that a reader holding the PRE-revocation config installs its snapshot after
    the post-revocation one and resets the TTL, re-admitting a host the operator
    just removed for another full interval. The lock plus a post-acquire
    freshness recheck means exactly one load happens per expiry.
    """
    global _gitlab_hosts_snapshot, _gitlab_hosts_loaded_at
    if _gitlab_hosts_fresh():
        return _gitlab_hosts_snapshot
    async with _gitlab_hosts_lock:
        # Another waiter may have refreshed while this one queued.
        if _gitlab_hosts_fresh():
            return _gitlab_hosts_snapshot
        hosts = await asyncio.to_thread(_load_gitlab_hosts)
        _publish_gitlab_hosts(hosts)
        return hosts


def _allowed_gitlab_hosts() -> frozenset[str]:
    """Return the cached self-managed GitLab hosts without touching the filesystem.

    Safe to call from sync code on the event loop (URL parsing, slot
    serialization). The snapshot comes only from config -- never from the browser
    -- and is matched exactly, so neither a pasted URL nor a lookalike suffix can
    widen it. Before the first :func:`ensure_gitlab_hosts_loaded` completes the
    snapshot is empty, which fails closed (a self-managed URL is simply not
    recognized yet).
    """
    return _gitlab_hosts_snapshot


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
    return SourceRef(
        "gitlab", normalized, host, owner, repo, number, project=project, kind=kind
    )


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
    if not isinstance(raw_url, str) or not raw_url or len(raw_url) > _MAX_URL_LENGTH:
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
    if host and candidate in _allowed_gitlab_hosts():
        return _gitlab_ref(candidate, path)

    raise ValueError(
        "Only github.com pull requests and issues, gitlab.com merge requests and "
        "issues, and merge requests or issues on a GitLab host listed in "
        "dashboard.gitlab_hosts are supported."
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


def _safe_error(stderr: bytes) -> str:
    text = stderr.decode("utf-8", errors="replace").strip()
    text = redact_exfiltration_urls(text)[0]
    text = redact_credentials(text)[0]
    text = _SAFE_ERROR_RE.sub(" ", text)
    return text[:600] or "provider command failed"


class _ProviderOutputTooLarge(RuntimeError):
    """A provider subprocess exceeded an output stream's byte limit."""


async def _read_stream_limited(stream: asyncio.StreamReader, limit: int, label: str) -> bytes:
    """Drain one subprocess pipe while enforcing a hard byte limit."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await stream.read(min(64 * 1024, limit - total + 1))
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > limit:
            raise _ProviderOutputTooLarge(f"provider {label} was too large")
        chunks.append(chunk)


async def _terminate_process(proc: asyncio.subprocess.Process) -> None:
    """Kill and reap a provider process tree after timeout, overflow, or cancellation."""
    if proc.returncode is None:
        try:
            platform_compat.kill_process_tree(proc.pid, platform_compat.SIGKILL)
        except (OSError, ValueError):
            # Best-effort PID fallback if group lookup races with launcher exit.
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
    with contextlib.suppress(ProcessLookupError):
        await proc.wait()


async def _collect_process_output(
    proc: asyncio.subprocess.Process,
    executable: str,
    max_output_bytes: int,
) -> tuple[bytes, bytes]:
    """Read both pipes concurrently with hard limits and bounded lifetime."""
    if proc.stdout is None or proc.stderr is None:
        await _terminate_process(proc)
        raise SourceProviderError(f"{executable} did not expose provider output")
    tasks = [
        asyncio.create_task(_read_stream_limited(proc.stdout, max_output_bytes, "response")),
        asyncio.create_task(_read_stream_limited(proc.stderr, _MAX_ERROR_BYTES, "error output")),
        asyncio.create_task(proc.wait()),
    ]
    try:
        stdout, stderr, _ = await asyncio.wait_for(
            asyncio.gather(*tasks), timeout=_COMMAND_TIMEOUT_SECS
        )
        if not isinstance(stdout, bytes) or not isinstance(stderr, bytes):
            raise SourceProviderError(f"{executable} returned invalid provider output")
        return stdout, stderr
    except asyncio.TimeoutError as exc:
        await _terminate_process(proc)
        raise SourceProviderError(f"{executable} timed out reading the pull request") from exc
    except _ProviderOutputTooLarge as exc:
        await _terminate_process(proc)
        raise SourceProviderError(str(exc)) from exc
    except asyncio.CancelledError:
        await _terminate_process(proc)
        raise
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _run_json(
    *argv: str,
    max_output_bytes: int = _METADATA_OUTPUT_BYTES,
    host: str = "",
) -> Any:
    """Run an allowlisted provider CLI with isolation, bounds, and SEL audit.

    ``host`` is REQUIRED for ``glab`` and must already have passed
    :func:`parse_source_url`; it is re-checked here so a caller cannot reach an
    unauthorized instance even if a future code path forgets to validate, and an
    omitted host is refused rather than silently resolved to gitlab.com.
    """
    executable = argv[0] if argv else ""
    if max_output_bytes <= 0 or max_output_bytes > _DIFF_OUTPUT_BYTES:
        _audit_provider_cli(executable, "denied", "invalid_output_limit")
        raise SourceProviderError("invalid provider output limit")
    if executable not in {"gh", "glab"}:
        _audit_provider_cli(executable, "denied", "unsupported_provider")
        raise SourceProviderError("unsupported provider command")
    gitlab_host = "gitlab.com"
    if executable == "glab":
        # Required, not defaulted: a call site that forgets `host` would
        # otherwise silently target gitlab.com, so an allowlisted self-managed MR
        # could be read -- or mutated -- on the PUBLIC instance at the same
        # project/IID. Failing loudly makes that class of bug impossible to
        # introduce, including from future mutation endpoints.
        if not host:
            _audit_provider_cli(executable, "denied", "host_not_specified")
            raise SourceProviderError("a GitLab host is required for glab calls")
        if host not in {"gitlab.com", "www.gitlab.com"}:
            if host not in _allowed_gitlab_hosts():
                _audit_provider_cli(executable, "denied", "host_not_allowlisted")
                raise SourceProviderError("GitLab host is not allowlisted")
            gitlab_host = host
    if platform_compat.IS_WINDOWS:
        _audit_provider_cli(executable, "denied", "sandbox_unavailable")
        raise SourceProviderError(
            "Pull-request source providers are not supported on Windows because "
            "OS-level provider sandboxing is unavailable."
        )
    try:
        resolved_executable = _resolve_provider_executable(executable)
    except SourceProviderError:
        _audit_provider_cli(executable, "denied", "executable_untrusted")
        raise

    allowed_env_keys = _PROVIDER_BASE_ENV_KEYS | _PROVIDER_AUTH_ENV_KEYS[executable]
    if executable == "glab" and gitlab_host != "gitlab.com":
        # GITLAB_TOKEN is a single ambient credential with no host binding, so
        # forwarding it while GITLAB_HOST points at a self-managed instance would
        # send a gitlab.com PAT (and every permission it carries) to that server.
        # Self-managed hosts must therefore authenticate from the per-host entry
        # in glab's own config (reachable via GLAB_CONFIG_DIR), which is scoped to
        # the host it was created for.
        allowed_env_keys = allowed_env_keys - {"GITLAB_TOKEN"}
    base_env = {key: value for key, value in os.environ.items() if key in allowed_env_keys}
    base_env.update(
        {
            "GH_PAGER": "cat",
            "GLAB_PAGER": "cat",
            "NO_COLOR": "1",
            "PATH": _PROVIDER_SYSTEM_PATH,
        }
    )
    if executable == "gh":
        # All accepted GitHub URLs normalize to github.com. Pin bare API paths
        # to the same host instead of honoring a configured enterprise default.
        base_env["GH_HOST"] = "github.com"
    else:
        # Pin the CLI to the host parse_source_url authorized for this URL, so a
        # self-managed default in glab config can't redirect the bare API paths
        # to a different instance.
        base_env["GITLAB_HOST"] = gitlab_host

    cleanup_path: str | None = None
    invoked = False
    try:
        async with _provider_semaphore:
            try:
                wrapped_argv, env, cleanup_path = sandboxed_spawn_argv(
                    [resolved_executable, *argv[1:]], mode="standard", env=base_env
                )
            except RuntimeError as exc:
                _audit_provider_cli(executable, "denied", "sandbox_rejected")
                raise SourceProviderError(f"{executable} could not start securely: {exc}") from exc
            audit_task = asyncio.create_task(
                asyncio.to_thread(
                    _audit_provider_cli,
                    executable,
                    "invoked",
                    "dispatch",
                    critical=True,
                )
            )
            try:
                await asyncio.shield(audit_task)
            except asyncio.CancelledError:
                # The worker thread cannot be cancelled once running. Wait for
                # it to settle so an on-disk invoked event is paired with the
                # outer request_cancelled terminal event before we re-raise.
                while not audit_task.done():
                    try:
                        await asyncio.shield(audit_task)
                    except asyncio.CancelledError:
                        continue
                if audit_task.exception() is None:
                    invoked = True
                raise
            except Exception as exc:
                raise SourceProviderError("provider audit unavailable") from exc
            invoked = True
            try:
                proc = await create_subprocess_limited(
                    *wrapped_argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                    start_new_session=platform_compat.IS_POSIX,
                    creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
                )
            except OSError as exc:
                raise SourceProviderError(f"{executable} could not start") from exc
            stdout, stderr = await _collect_process_output(proc, executable, max_output_bytes)
        if proc.returncode != 0:
            message = _safe_error(stderr)
            lowered = message.lower()
            if (
                "unauthenticated" in lowered
                or "not logged in" in lowered
                or "authentication" in lowered
            ):
                message = f"{message} Run `{executable} auth login`, then retry."
            raise SourceProviderError(message)
        try:
            result = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SourceProviderError(f"{executable} returned invalid JSON") from exc
    except asyncio.CancelledError:
        if invoked:
            _audit_provider_cli(executable, "failed", "request_cancelled")
        raise
    except SourceProviderError:
        if invoked:
            _audit_provider_cli(executable, "failed", "provider_error")
        raise
    except Exception:
        if invoked:
            _audit_provider_cli(executable, "failed", "internal_error")
        raise
    finally:
        if cleanup_path:
            with contextlib.suppress(OSError):
                os.unlink(cleanup_path)
    _audit_provider_cli(executable, "completed", "success")
    return result


def _or_empty(value: Any) -> Any:
    """Coerce an already-recorded gather failure into an empty section."""
    if isinstance(value, BaseException):
        return []
    return value


def _mark_partial(partial_sections: list[str], section: str) -> None:
    """Add a partial-result section once while preserving display order."""
    if section not in partial_sections:
        partial_sections.append(section)


def _as_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _redact_provider_data(value: Any) -> Any:
    """Recursively redact secrets and suspicious URLs in provider-controlled data."""
    if isinstance(value, str):
        value = redact_exfiltration_urls(value)[0]
        return redact_credentials(value)[0]
    if isinstance(value, list):
        return [_redact_provider_data(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_provider_data(item) for key, item in value.items()}
    return value


def _payload_size_bytes(data: dict[str, Any]) -> int:
    """Return the compact UTF-8 JSON size used for response and cache bounds."""
    return len(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _author(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("login") or value.get("username") or value.get("name") or "")
    return str(value or "")


# --- Shared chip-status projection ------------------------------------------
#
# The sidebar chips and the detail panel derive the same {state, ci} chip
# projection from two different provider reads (a lightweight chip fetch and a
# full payload). The two caches are mutually invalidating, so the
# invariant "both projections agree" is load-bearing: any vocabulary drift turns
# a common steady state into a sustained cache ping-pong. To make drift
# structurally impossible rather than convention-enforced, BOTH paths route
# every raw provider value through the single functions below. Do not inline a
# second copy of this vocabulary anywhere.


def _rollup_ci(buckets: list[str]) -> str | None:
    """Roll per-check buckets up to a single chip CI value (or ``None``)."""
    if not buckets:
        return None
    if "failed" in buckets:
        return "failed"
    if "pending" in buckets:
        return "running"
    return "passed"


def _project_state(raw_state: str, *, draft: bool) -> str | None:
    """Map a provider PR/MR lifecycle state to the chip ``state`` vocabulary.

    GitHub reports OPEN/MERGED/CLOSED; GitLab opened/merged/closed/locked. A
    ``draft`` flag only means "draft" while the PR is still open — GitLab keeps
    ``draft: true`` on an MR closed while in draft, so the draft mapping must be
    gated on the open state or the two paths diverge (chip "draft" vs full
    "closed") and ping-pong forever.

    ``locked`` is GitLab's *transient* state while a merge is in progress — it
    is not a terminal lifecycle. Mapping it to ``closed`` painted a false
    "closed" glyph on an MR that is actually mid-merge and conflicted with the
    detail panel's own locked handling. Project nothing for it (both paths agree
    on "no lifecycle change") and let the next read resolve to merged/closed once
    GitLab settles.
    """
    state = raw_state.lower()
    if state in {"open", "opened"}:
        return "draft" if draft else "open"
    if state == "merged":
        return "merged"
    if state == "closed":
        return "closed"
    return None


def _gitlab_status_bucket(status: str) -> str:
    """Map a single GitLab *job* status to a check bucket for the Checks list.

    This is the per-job display vocabulary and is deliberately FAITHFUL: a job
    that failed buckets as ``failed`` even when it is ``allow_failure`` (GitLab
    shows such a job as failed-but-allowed, and hiding it as ``skipped`` made the
    Checks tab claim "all checks passed" while a job was red). The single CI
    glyph is NOT rolled up from these job buckets for GitLab — it comes from the
    pipeline aggregate via ``_gitlab_aggregate_ci`` — so a faithful failed job
    here never diverges the chip from the full-payload projection.
    """
    state = status.lower()
    if state in {"success", "passed"}:
        return "passed"
    if state in {"skipped", "manual"}:
        return "skipped"
    if state in {"failed", "canceled", "cancelled"}:
        return "failed"
    return "pending"


def _gitlab_aggregate_ci(status: str) -> str | None:
    """Project a GitLab *pipeline aggregate* status to the chip CI vocabulary.

    Single source of truth for the GitLab CI glyph, called by BOTH the chip path
    (which reads the pipeline aggregate directly) and the full-payload path (via
    ``ciStatus`` stamped by ``_fetch_gitlab``). Because both sides call this one
    function on the same aggregate value, the two projections cannot drift by
    construction — regardless of how the vocabulary is mapped below.

    The aggregate is authoritative and lossless for the glyph: GitLab folds
    ``allow_failure`` failures into a ``success`` aggregate, so an allowed
    failure correctly reads "passed" here while its job still shows failed in the
    Checks list. ``manual`` is a *blocking* gate (the pipeline is waiting on a
    manual action), so it maps to ``running`` — not ``passed`` — because work is
    still outstanding.
    """
    state = status.lower()
    if state in {"success", "passed"}:
        return "passed"
    if state in {"failed", "canceled", "cancelled"}:
        return "failed"
    if state == "skipped":
        # A wholly skipped pipeline has no failures and nothing outstanding.
        return "passed"
    if not state:
        return None
    # running / pending / created / scheduled / preparing / waiting_for_resource
    # and manual (a blocking manual gate is still outstanding work).
    return "running"


def _github_check(item: dict[str, Any]) -> dict[str, Any]:
    conclusion = str(item.get("conclusion") or item.get("state") or "").upper()
    status = str(item.get("status") or "").upper()
    if status and status != "COMPLETED":
        bucket = "pending"
    elif conclusion in {"SUCCESS", "NEUTRAL"}:
        bucket = "passed"
    elif conclusion in {"SKIPPED", "STALE"}:
        bucket = "skipped"
    elif conclusion in {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "ERROR"}:
        bucket = "failed"
    else:
        bucket = "pending"
    return {
        "name": item.get("name") or item.get("context") or "Check",
        "workflow": item.get("workflowName") or "",
        "status": status,
        "conclusion": conclusion,
        "bucket": bucket,
        "url": item.get("detailsUrl") or item.get("targetUrl") or "",
        "startedAt": item.get("startedAt") or "",
        "completedAt": item.get("completedAt") or "",
    }


def _github_check_identity(
    item: dict[str, Any], check: dict[str, Any], index: int
) -> tuple[str, ...]:
    """The identity two rollup rows must share to be the same check.

    NOT the display name alone. ``workflow`` separates two workflows that publish
    a check with the same job name — including every matrix leg of an Actions
    job, since GitHub appends the matrix values to the check-run name even when
    the workflow sets an explicit ``name:`` (``Backend Tests (3.12, 4)``), so
    sibling shards never share an identity and one shard's failure can never be
    folded into another's success.

    The row KIND separates GitHub's two rollup shapes. ``__typename`` comes
    straight from the GraphQL union and is present on every row ``gh`` returns; a
    row without it (a hand-built dict) is classified from the fields each shape
    carries — ``status``/``conclusion`` are check-run-only and ``context`` is
    status-only. Do NOT discriminate on the absence of ``name``: a status row
    carrying both ``context`` and ``name`` would be read as a check-run and
    collide with a nameless one, which ``_github_check`` normalizes to the same
    ``"Check"`` placeholder.

    A check-run with NO workflow (published by an app outside Actions) is left
    deliberately UNCOLLAPSED — its per-row detail URL, else its position, joins
    the identity. Such rows are the one case this payload cannot adjudicate: the
    requested ``statusCheckRollup`` fields carry no check-suite or run-attempt
    id, so a superseded re-run is indistinguishable from a same-named check from
    a different app. Over-counting a re-run is a cosmetic miss; collapsing two
    apps would hide a real failure behind the other's later success, so the tie
    breaks toward never hiding red.
    """
    kind = str(item.get("__typename") or "")
    if not kind:
        status_shaped = "context" in item and not ("status" in item or "conclusion" in item)
        kind = "StatusContext" if status_shaped else "CheckRun"
    if kind == "CheckRun" and not check["workflow"]:
        return (kind, "", check["name"], check["url"] or f"#{index}")
    return (kind, check["workflow"], check["name"])


def _github_check_rank(check: dict[str, Any]) -> tuple[str, str]:
    """Recency key for two rows that share a check identity.

    ``startedAt`` leads: an OLDER run that finished must not outrank a NEWER one
    that is still going (no ``completedAt`` yet), which is exactly what comparing
    ``completedAt`` first would do — the panel would show a stale pass while its
    replacement was mid-flight. GitHub leaves ``startedAt`` null while a check-run
    is still QUEUED, so a started-less row that is still outstanding sorts above
    every timestamp instead of losing to the completed run it supersedes.
    """
    started = str(check.get("startedAt") or "")
    if not started and check.get("bucket") == "pending":
        return ("\uffff", "")
    return (started, str(check.get("completedAt") or ""))


def _github_checks(rollup: list[Any]) -> list[dict[str, Any]]:
    """Project GitHub's status-check rollup, keeping only the LATEST run per check.

    ``statusCheckRollup`` returns EVERY check-run recorded against the head sha,
    not one per check. The same workflow file can be dispatched twice for one sha
    (a push immediately followed by an edit event, say), producing two check
    suites whose jobs each contribute a row — and a concurrency group cancels the
    first suite, so the loser lands as ``CANCELLED``. Rendering the raw rollup
    therefore (a) inflated the totals the panel reports (observed 49 rows where
    GitHub's own UI counted 41) and (b) let a superseded ``CANCELLED`` row roll up
    to a red CI glyph on a pull request whose replacement run passed — a red that
    no amount of refreshing could clear, because the stale row is genuinely still
    in the provider payload.

    GitHub's UI collapses each check to its latest run; mirror that. Identity
    comes from ``_github_check_identity``, which is deliberately conservative:
    anything it cannot prove is the same check stays its own row, because
    over-counting is cosmetic while collapsing two distinct checks would hide a
    failure behind another's success.

    First-appearance order is preserved (dict insertion order survives value
    replacement) so a re-run does not reshuffle the list under the caller.
    """
    best: dict[tuple[str, ...], dict[str, Any]] = {}
    for index, item in enumerate(rollup):
        if not isinstance(item, dict):
            continue
        check = _github_check(item)
        identity = _github_check_identity(item, check, index)
        previous = best.get(identity)
        if previous is None or _github_check_rank(check) >= _github_check_rank(previous):
            best[identity] = check
    return list(best.values())


def _github_comment(item: dict[str, Any], kind: str) -> dict[str, Any]:
    return {
        "id": str(item.get("id") or item.get("databaseId") or ""),
        "kind": kind,
        "author": _author(item.get("author") or item.get("user")),
        "body": item.get("body") or "",
        "state": item.get("state") or "",
        "createdAt": item.get("createdAt")
        or item.get("submittedAt")
        or item.get("created_at")
        or "",
        "url": item.get("url") or item.get("html_url") or "",
        "path": item.get("path") or "",
        "line": item.get("line") or item.get("original_line"),
        "threadId": "",
        "resolvable": False,
        "resolved": False,
    }


_GITHUB_REVIEW_THREADS_QUERY = (
    "query($owner:String!,$repo:String!,$number:Int!)"
    "{repository(owner:$owner,name:$repo)"
    "{pullRequest(number:$number)"
    "{reviewThreads(first:100){nodes{id isResolved "
    "comments(first:10){nodes{databaseId}}}}}}}"
)


def _github_thread_ids(payload: Any) -> set[str]:
    """Return review-thread IDs scoped to the queried pull request."""
    if not isinstance(payload, dict):
        return set()
    try:
        nodes = payload["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
    except (KeyError, TypeError):
        return set()
    return {str(node["id"]) for node in _as_list(nodes) if node.get("id")}


def _github_thread_map(payload: Any) -> dict[str, dict[str, Any]]:
    """Map an inline comment databaseId to its review thread id and state."""
    result: dict[str, dict[str, Any]] = {}
    if not isinstance(payload, dict):
        return result
    try:
        nodes = payload["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
    except (KeyError, TypeError):
        return result
    for node in _as_list(nodes):
        thread_id = node.get("id")
        if not thread_id:
            continue
        is_resolved = bool(node.get("isResolved"))
        comments = node.get("comments")
        comment_nodes = comments.get("nodes") if isinstance(comments, dict) else []
        for comment in _as_list(comment_nodes):
            database_id = comment.get("databaseId")
            if database_id is None:
                continue
            result[str(database_id)] = {
                "threadId": str(thread_id),
                "resolved": is_resolved,
            }
    return result


# Both providers compute mergeability lazily: reading a pull request that has
# not been evaluated recently returns "not known yet" (GitHub ``UNKNOWN``,
# GitLab ``checking``/``unchecked``) *and* kicks off the computation, so the
# real answer is only available on a later read. A single read therefore reports
# a conflicting pull request as having no merge blocker at all — which is why
# the panel's conflict banner used to appear only once the user hit refresh.
# These bound a short re-read of the merge fields alone (not the whole fanout),
# issued concurrently with the secondary provider calls so most of the wait is
# absorbed by work the request was already doing.
_MERGE_STATE_REREADS = 2
_MERGE_STATE_REREAD_DELAY_SECS = 0.8
# The one normalized value that means "the provider has not answered yet". It is
# shared by both fields of the merge pair and by both providers.
_UNSETTLED_MERGE_STATE = "unknown"


def _merge_state_real(value: str) -> bool:
    """Whether one normalized merge field carries a real answer."""
    return bool(value) and value != _UNSETTLED_MERGE_STATE


def _merge_state_settled(mergeable: str, merge_state: str) -> bool:
    """Whether a normalized merge pair is a real answer, so no re-read is due.

    A pair is settled once **either** field is real. GitLab reports `need_rebase`
    and its branch-protection gates with ``mergeable == 'unknown'`` — the detail
    IS the answer there, so keying only on ``mergeable`` would re-read a state
    the provider had already settled and then discard it. A pair that is empty
    rather than unknown means the provider did not report the fields at all, so
    re-reading cannot settle it either.
    """
    if mergeable == _UNSETTLED_MERGE_STATE or merge_state == _UNSETTLED_MERGE_STATE:
        return _merge_state_real(mergeable) or _merge_state_real(merge_state)
    return True


_MERGE_STATE_FIELDS = ("mergeable", "mergeStateStatus")
# Lifecycle states for which a merge answer is still meaningful. Once a source is
# merged or closed the providers stop answering the merge pair at all, so a
# carried-forward value could never be cleared again.
_MERGE_STATE_LIVE_STATES = frozenset({"open", "draft"})


def _keep_known_merge_state(
    status: dict[str, str], previous: dict[str, str] | None
) -> dict[str, str]:
    """Carry a settled merge field forward when a fresh read has no answer yet.

    ``_record_merge_state`` omits a field the provider has not settled, on the
    principle that "still computing" must never be published as a real answer.
    That is necessary but not sufficient: every writer replaces the chip entry
    WHOLESALE, so an omitted field does not read as "no news" downstream — it
    erases whatever the previous entry had settled.

    That matters because an unsettled read is the COMMON case, not a rare one:
    both providers compute mergeability lazily, so a poll that arrives after the
    provider's evaluation lapsed returns ``unknown`` for a source whose conflict
    is already known. Without this carry-forward, such a poll drops the merge
    pair, which (a) removes it from the owner-gated sidebar payload that spreads
    the entry whole, and (b) reads as a CHANGED chip status, dropping the full
    payload and emitting a delta — whose refetch re-projects the real answer
    straight back into the cache. That is the repeating chip<->full transition
    ``_CHECK_FLAP_DAMP_THRESHOLD`` exists to contain, so the banner would survive
    only until the damper tripped and then go stale.

    Mirrors the same keep-known rule already applied to the ``ci`` glyph. A real
    answer always wins, including one that CHANGES the value, so this only ever
    fills a gap and cannot pin a stale verdict. Carry-forward stops once the
    source leaves an open state, where the pair is both meaningless and
    permanently unanswered.
    """
    if not previous:
        return status
    if status.get("state", "open") not in _MERGE_STATE_LIVE_STATES:
        return status
    carried = {
        field: previous[field]
        for field in _MERGE_STATE_FIELDS
        if field not in status and field in previous
    }
    return {**status, **carried} if carried else status


def _github_merge_state(details: dict[str, Any]) -> tuple[str, str]:
    """Normalize GitHub merge fields to (mergeable, mergeStateStatus).

    ``mergeable`` is one of ``mergeable`` / ``conflicting`` / ``unknown`` and
    ``mergeStateStatus`` is GitHub's merge-state vocabulary lowercased
    (``clean``, ``dirty``, ``behind``, ``blocked``, ``unstable``, ...).
    """
    raw_mergeable = str(details.get("mergeable") or "").upper()
    if raw_mergeable == "MERGEABLE":
        mergeable = "mergeable"
    elif raw_mergeable == "CONFLICTING":
        mergeable = "conflicting"
    else:
        mergeable = "unknown" if raw_mergeable else ""
    return mergeable, str(details.get("mergeStateStatus") or "").lower()


# GitLab detailed_merge_status values mapped onto the GitHub-style
# merge-state vocabulary the frontend renders.
_GITLAB_MERGE_STATE_MAP = {
    "mergeable": "clean",
    "conflict": "dirty",
    # need_rebase keeps its own value: on fast-forward-only projects a merge
    # commit cannot unblock the MR, so it must not be conflated with "behind".
    "need_rebase": "need_rebase",
    "ci_must_pass": "blocked",
    "ci_still_running": "unstable",
    "discussions_not_resolved": "blocked",
    "not_approved": "blocked",
    "blocked_status": "blocked",
    "external_status_checks": "blocked",
    "jira_association_missing": "blocked",
    "requested_changes": "blocked",
    "status_checks_must_pass": "blocked",
    "policies_denied": "blocked",
    "security_policy_violations": "blocked",
    "merge_request_blocked": "blocked",
    "draft_status": "draft",
}


def _gitlab_merge_state(details: dict[str, Any]) -> tuple[str, str]:
    """Normalize GitLab merge fields to (mergeable, mergeStateStatus).

    ``detailed_merge_status`` is authoritative; the deprecated legacy
    ``merge_status`` is consulted only when the detailed field is absent
    (it can be stale or coarse and must never override the detailed value).
    """
    detailed = str(details.get("detailed_merge_status") or "").lower()
    legacy = str(details.get("merge_status") or "").lower()
    if detailed:
        if detailed == "conflict":
            mergeable = "conflicting"
        elif detailed == "mergeable":
            mergeable = "mergeable"
        else:
            mergeable = "unknown"
        return mergeable, _GITLAB_MERGE_STATE_MAP.get(detailed, "unknown")
    if legacy == "cannot_be_merged":
        mergeable = "conflicting"
    elif legacy == "can_be_merged":
        mergeable = "mergeable"
    else:
        mergeable = "unknown" if legacy else ""
    return mergeable, ""


async def _github_settled_merge_state(ref: SourceRef, details: dict[str, Any]) -> tuple[str, str]:
    """Merge state for a GitHub PR, re-reading while it is still being computed.

    Re-reads only ``mergeable``/``mergeStateStatus``, at most
    ``_MERGE_STATE_REREADS`` times. A failed or still-unsettled re-read keeps the
    original value rather than raising: an unknown merge state degrades one
    banner, and must never fail the whole panel.
    """
    mergeable, merge_state = _github_merge_state(details)
    if _merge_state_settled(mergeable, merge_state):
        return mergeable, merge_state
    for _ in range(_MERGE_STATE_REREADS):
        await asyncio.sleep(_MERGE_STATE_REREAD_DELAY_SECS)
        try:
            data = await _run_json(
                "gh", "pr", "view", ref.url, "--json", "mergeable,mergeStateStatus"
            )
        except SourceProviderError:
            break
        if not isinstance(data, dict):
            break
        reread, reread_state = _github_merge_state(data)
        if _merge_state_settled(reread, reread_state):
            return reread, reread_state
    return mergeable, merge_state


async def _gitlab_settled_merge_state(
    ref: SourceRef, mr_api: str, details: dict[str, Any]
) -> tuple[str, str]:
    """Merge state for a GitLab MR, re-reading while it is still being computed.

    GitLab exposes ``detailed_merge_status`` only on the merge-request endpoint,
    so the re-read repeats that request and takes the merge fields from it. Same
    failure posture as the GitHub path: degrade to the original value.
    """
    mergeable, merge_state = _gitlab_merge_state(details)
    if _merge_state_settled(mergeable, merge_state):
        return mergeable, merge_state
    for _ in range(_MERGE_STATE_REREADS):
        await asyncio.sleep(_MERGE_STATE_REREAD_DELAY_SECS)
        try:
            data = await _run_json("glab", "api", mr_api)
        except SourceProviderError:
            break
        if not isinstance(data, dict):
            break
        reread, reread_state = _gitlab_merge_state(data)
        if _merge_state_settled(reread, reread_state):
            return reread, reread_state
    return mergeable, merge_state


async def _fetch_github(ref: SourceRef) -> dict[str, Any]:
    fields = ",".join(
        [
            "additions",
            "author",
            "autoMergeRequest",
            "baseRefName",
            "body",
            "changedFiles",
            "comments",
            "commits",
            "deletions",
            "headRefName",
            "headRefOid",
            "isDraft",
            "mergeStateStatus",
            "mergeable",
            "mergedAt",
            "number",
            "reviews",
            "state",
            "statusCheckRollup",
            "title",
            "updatedAt",
            "url",
        ]
    )
    repo_api = f"repos/{ref.owner}/{ref.repo}/pulls/{ref.number}"
    details = await _run_json("gh", "pr", "view", ref.url, "--json", fields)
    if not isinstance(details, dict):
        raise SourceProviderError("GitHub returned an invalid pull-request payload")

    # Secondary endpoints degrade to empty sections instead of failing the
    # whole panel: the primary payload above already carries the core data.
    files_raw: Any
    review_comments_raw: Any
    review_threads_raw: Any
    merge_state_raw: Any
    files_raw, review_comments_raw, review_threads_raw, merge_state_raw = await asyncio.gather(
        _run_json(
            "gh",
            "api",
            f"{repo_api}/files?per_page={_SECONDARY_PAGE_SIZE}",
            max_output_bytes=_DIFF_OUTPUT_BYTES,
        ),
        _run_json(
            "gh",
            "api",
            f"{repo_api}/comments?per_page={_SECONDARY_PAGE_SIZE}",
            max_output_bytes=_DISCUSSION_OUTPUT_BYTES,
        ),
        _run_json(
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={_GITHUB_REVIEW_THREADS_QUERY}",
            "-f",
            f"owner={ref.owner}",
            "-f",
            f"repo={ref.repo}",
            "-F",
            f"number={ref.number}",
            max_output_bytes=_DISCUSSION_OUTPUT_BYTES,
        ),
        # Runs alongside the secondary calls so its re-read wait overlaps with
        # fetches this request was making anyway.
        _github_settled_merge_state(ref, details),
        return_exceptions=True,
    )
    partial_sections: list[str] = []
    if isinstance(files_raw, BaseException):
        _mark_partial(partial_sections, "files")
    if isinstance(review_comments_raw, BaseException) or isinstance(
        review_threads_raw, BaseException
    ):
        _mark_partial(partial_sections, "inline review comments")
    files = _or_empty(files_raw)
    review_comments = _or_empty(review_comments_raw)
    thread_map = _github_thread_map(_or_empty(review_threads_raw))
    file_rows = _as_list(files)
    review_comment_rows = _as_list(review_comments)
    changed_files = details.get("changedFiles")
    if (isinstance(changed_files, int) and changed_files > len(file_rows)) or (
        not isinstance(changed_files, int) and len(file_rows) >= _SECONDARY_PAGE_SIZE
    ):
        _mark_partial(partial_sections, "files")
    # GitHub's review-comment endpoint does not expose its total in the
    # primary payload. A full page means another page may exist.
    if len(review_comment_rows) >= _SECONDARY_PAGE_SIZE:
        _mark_partial(partial_sections, "inline review comments")

    inline_comments = [_github_comment(item, "inline") for item in review_comment_rows]
    for comment in inline_comments:
        info = thread_map.get(comment["id"])
        if info:
            comment["threadId"] = info["threadId"]
            comment["resolved"] = info["resolved"]
            comment["resolvable"] = True

    comments = [
        *(_github_comment(item, "comment") for item in _as_list(details.get("comments"))),
        *(_github_comment(item, "review") for item in _as_list(details.get("reviews"))),
        *inline_comments,
    ]
    commits = []
    for item in _as_list(details.get("commits")):
        authors = _as_list(item.get("authors"))
        commits.append(
            {
                "sha": item.get("oid") or "",
                "title": item.get("messageHeadline") or "",
                "body": item.get("messageBody") or "",
                "author": _author(authors[0]) if authors else "",
                "date": item.get("committedDate") or item.get("authoredDate") or "",
                "url": (
                    f"https://github.com/{ref.owner}/{ref.repo}/commit/{item.get('oid')}"
                    if item.get("oid")
                    else ""
                ),
            }
        )

    normalized_files = []
    for item in _as_list(files):
        normalized_files.append(
            {
                "path": item.get("filename") or "",
                "status": item.get("status") or "modified",
                "additions": item.get("additions") or 0,
                "deletions": item.get("deletions") or 0,
                "patch": item.get("patch") or "",
            }
        )

    github_mergeable, github_merge_state = (
        merge_state_raw
        if isinstance(merge_state_raw, tuple)
        else _github_merge_state(details)
    )
    return {
        "provider": "github",
        # Identity comes from the VALIDATED ref, never the provider echo: the
        # browser submits this url back for refresh/resolve, so a compromised or
        # hostile instance echoing a different web_url could otherwise steer an
        # owner-authenticated mutation at an unrelated pull/merge request.
        "url": ref.url,
        "number": ref.number,
        "title": details.get("title") or "",
        "description": details.get("body") or "",
        "state": details.get("state") or "",
        "draft": bool(details.get("isDraft")),
        "mergedAt": details.get("mergedAt") or "",
        "mergeable": github_mergeable,
        "mergeStateStatus": github_merge_state,
        "autoMerge": bool(details.get("autoMergeRequest")),
        "updatedAt": details.get("updatedAt") or "",
        "headBranch": details.get("headRefName") or "",
        "baseBranch": details.get("baseRefName") or "",
        "headSha": details.get("headRefOid") or "",
        "author": _author(details.get("author")),
        "additions": details.get("additions") or 0,
        "deletions": details.get("deletions") or 0,
        "changedFiles": details.get("changedFiles") or len(normalized_files),
        "commits": commits,
        "checks": _github_checks(_as_list(details.get("statusCheckRollup"))),
        "comments": comments,
        "files": normalized_files,
        "partialSections": partial_sections,
    }


def _gitlab_pipeline_as_check(pipeline: dict[str, Any]) -> dict[str, Any]:
    """Represent a whole pipeline as a single check row.

    A pipeline standing in for its jobs must keep PIPELINE-level semantics: a
    ``manual`` pipeline is blocked on a required job, so it is marked as a
    required gate (``allow_failure: False``) rather than falling through to the
    job-level reading that treats a lone manual step as skipped -- which the
    frontend would then roll up as passed.
    """
    record = {**pipeline, "name": "Pipeline"}
    if str(pipeline.get("status") or "").lower() == "manual":
        record["allow_failure"] = False
    return _gitlab_check(record)


def _gitlab_check(item: dict[str, Any]) -> dict[str, Any]:
    status = str(item.get("status") or "").lower()
    bucket = _gitlab_status_bucket(status)
    if status == "manual" and item.get("allow_failure") is False:
        # A required manual job is an unsatisfied gate, not an optional step that
        # can be treated as skipped: rolling it up as passed would show green
        # while the pipeline is still blocked on a human action.
        bucket = "pending"
    return {
        "name": item.get("name") or "Job",
        "workflow": item.get("stage") or "",
        "status": status.upper(),
        "conclusion": status.upper(),
        "bucket": bucket,
        "url": item.get("web_url") or "",
        "startedAt": item.get("started_at") or "",
        "completedAt": item.get("finished_at") or "",
    }


async def _fetch_gitlab(ref: SourceRef) -> dict[str, Any]:
    project = quote(ref.project, safe="")
    mr_api = f"projects/{project}/merge_requests/{ref.number}"
    details = await _run_json("glab", "api", mr_api, host=ref.host)
    if not isinstance(details, dict):
        raise SourceProviderError("GitLab returned an invalid merge-request payload")

    # Secondary endpoints degrade to empty sections instead of failing the
    # whole panel: the primary payload above already carries the core data.
    commits_raw: Any
    discussions_raw: Any
    changes_raw: Any
    pipelines_raw: Any
    merge_state_raw: Any
    commits_raw, discussions_raw, changes_raw, pipelines_raw, merge_state_raw = (
        await asyncio.gather(
            _run_json(
                "glab", "api", f"{mr_api}/commits?per_page={_SECONDARY_PAGE_SIZE}", host=ref.host
            ),
            _run_json(
                "glab",
                "api",
                f"{mr_api}/discussions?per_page={_SECONDARY_PAGE_SIZE}",
                max_output_bytes=_DISCUSSION_OUTPUT_BYTES,
                host=ref.host,
            ),
            _run_json(
                "glab", "api", f"{mr_api}/changes", max_output_bytes=_DIFF_OUTPUT_BYTES, host=ref.host
            ),
            _run_json("glab", "api", f"{mr_api}/pipelines?per_page=20", host=ref.host),
            # Runs alongside the secondary calls so its re-read wait overlaps
            # with fetches this request was making anyway.
            _gitlab_settled_merge_state(ref, mr_api, details),
            return_exceptions=True,
        )
    )
    partial_sections: list[str] = []
    for raw_value, section in (
        (commits_raw, "commits"),
        (discussions_raw, "review discussions"),
        (changes_raw, "files"),
        (pipelines_raw, "checks"),
    ):
        if isinstance(raw_value, BaseException):
            _mark_partial(partial_sections, section)
    commits = _or_empty(commits_raw)
    discussions = _or_empty(discussions_raw)
    changes = _or_empty(changes_raw)
    pipelines = _or_empty(pipelines_raw)
    commit_rows = _as_list(commits)
    discussion_rows = _as_list(discussions)
    if len(commit_rows) >= _SECONDARY_PAGE_SIZE:
        _mark_partial(partial_sections, "commits")
    if len(discussion_rows) >= _SECONDARY_PAGE_SIZE:
        _mark_partial(partial_sections, "review discussions")

    jobs: Any = []
    pipeline_rows = _as_list(pipelines)
    if pipeline_rows and pipeline_rows[0].get("id"):
        try:
            jobs = await _run_json(
                "glab",
                "api",
                f"projects/{project}/pipelines/{pipeline_rows[0]['id']}/jobs?per_page={_SECONDARY_PAGE_SIZE}",
                host=ref.host,
            )
        except SourceProviderError:
            _mark_partial(partial_sections, "checks")
            jobs = []
    if len(_as_list(jobs)) >= _SECONDARY_PAGE_SIZE:
        # A full page of jobs may be truncated — a failed job on a later page
        # would be invisible in this list. The CI glyph is projected from the
        # pipeline AGGREGATE (`ciStatus` below), which stays authoritative
        # regardless, but flag the Checks LIST as partial so the panel does not
        # imply it is exhaustive.
        _mark_partial(partial_sections, "checks")

    raw_changes = changes.get("changes") if isinstance(changes, dict) else []
    change_rows = _as_list(raw_changes)
    reported_change_count = str(details.get("changes_count") or "").rstrip("+")
    if (isinstance(changes, dict) and changes.get("overflow")) or (
        reported_change_count.isdigit() and int(reported_change_count) > len(change_rows)
    ):
        _mark_partial(partial_sections, "files")
    normalized_files = []
    for item in change_rows:
        status = (
            "deleted"
            if item.get("deleted_file")
            else (
                "added"
                if item.get("new_file")
                else "renamed" if item.get("renamed_file") else "modified"
            )
        )
        patch = item.get("diff") or ""
        normalized_files.append(
            {
                "path": item.get("new_path") or item.get("old_path") or "",
                "status": status,
                "additions": sum(
                    1
                    for line in patch.splitlines()
                    if line.startswith("+") and not line.startswith("+++")
                ),
                "deletions": sum(
                    1
                    for line in patch.splitlines()
                    if line.startswith("-") and not line.startswith("---")
                ),
                "patch": patch,
            }
        )

    gitlab_comments = []
    for discussion in _as_list(discussions):
        thread_id = str(discussion.get("id") or "")
        for note in _as_list(discussion.get("notes")):
            if note.get("system"):
                continue
            gitlab_comments.append(
                {
                    "id": str(note.get("id") or ""),
                    "kind": "comment",
                    "author": _author(note.get("author")),
                    "body": note.get("body") or "",
                    "state": "",
                    "createdAt": note.get("created_at") or "",
                    "url": "",
                    "path": "",
                    "line": None,
                    "threadId": thread_id,
                    "resolvable": bool(note.get("resolvable")),
                    "resolved": bool(note.get("resolved")),
                }
            )

    gitlab_mergeable, gitlab_merge_state = (
        merge_state_raw
        if isinstance(merge_state_raw, tuple)
        else _gitlab_merge_state(details)
    )
    gitlab_checks = [_gitlab_check(item) for item in _as_list(jobs)]
    # The single CI glyph is projected from the pipeline AGGREGATE (authoritative
    # and lossless — GitLab folds allow_failure into it and marks a blocking
    # manual gate), NOT rolled up from the per-job buckets, so a truncated /
    # empty / allow_failure job list can never diverge the glyph from the chip
    # path (which reads the same aggregate). `checks` below is a faithful
    # display list only.
    pipeline_status = str(pipeline_rows[0].get("status") or "") if pipeline_rows else ""
    gitlab_ci = _gitlab_aggregate_ci(pipeline_status)
    if not gitlab_checks and pipeline_rows and pipeline_status and "checks" not in partial_sections:
        # A pipeline EXISTS but its jobs have not materialized yet (freshly
        # created pipeline). Synthesize a single "Pipeline" row from the
        # aggregate so the Checks tab is not empty while jobs spin up — a
        # pipeline-less MR keeps `checks` empty. Display-only; the glyph comes
        # from `ciStatus`.
        gitlab_checks = [_gitlab_check({**pipeline_rows[0], "name": "Pipeline"})]
    payload: dict[str, Any] = {
        "provider": "gitlab",
        # See _fetch_github: identity is the validated ref, not the provider's
        # web_url/iid. This matters most for a self-managed instance, whose
        # responses are outside the trust boundary the allowlist establishes.
        "url": ref.url,
        "number": ref.number,
        "title": details.get("title") or "",
        "description": details.get("description") or "",
        "state": details.get("state") or "",
        "draft": bool(details.get("draft") or details.get("work_in_progress")),
        "mergedAt": details.get("merged_at") or "",
        "mergeable": gitlab_mergeable,
        "mergeStateStatus": gitlab_merge_state,
        "autoMerge": bool(details.get("merge_when_pipeline_succeeds")),
        "updatedAt": details.get("updated_at") or "",
        "headBranch": details.get("source_branch") or "",
        "baseBranch": details.get("target_branch") or "",
        "headSha": details.get("sha") or "",
        "author": _author(details.get("author")),
        "additions": sum(item["additions"] for item in normalized_files),
        "deletions": sum(item["deletions"] for item in normalized_files),
        "changedFiles": len(normalized_files),
        "commits": [
            {
                "sha": item.get("id") or item.get("short_id") or "",
                "title": item.get("title") or "",
                "body": item.get("message") or "",
                "author": item.get("author_name") or "",
                "date": item.get("created_at") or item.get("committed_date") or "",
                "url": item.get("web_url") or "",
            }
            for item in commit_rows
        ],
        "checks": gitlab_checks,
        "comments": gitlab_comments,
        "files": normalized_files,
        "partialSections": partial_sections,
    }
    if gitlab_ci is not None:
        # Authoritative aggregate CI for the glyph; consumed by
        # `status_from_full_payload` so the full-payload projection matches the
        # chip path (which reads the same aggregate) exactly.
        payload["ciStatus"] = gitlab_ci
    return payload


async def _fetch_github_checks(ref: SourceRef) -> list[dict[str, Any]]:
    data = await _run_json(
        "gh",
        "pr",
        "view",
        ref.url,
        "--json",
        "statusCheckRollup",
        max_output_bytes=_CHECKS_OUTPUT_BYTES,
    )
    if not isinstance(data, dict):
        raise SourceProviderError("GitHub returned an invalid checks payload")
    # The panel polls this endpoint while checks are pending and writes the result
    # straight over the full payload's `checks`, so it MUST collapse identically —
    # an uncollapsed reply here would re-inflate the counts and resurrect a
    # superseded CANCELLED failure on the first poll after the panel opens.
    return _github_checks(_as_list(data.get("statusCheckRollup")))


async def _fetch_gitlab_checks(ref: SourceRef) -> list[dict[str, Any]]:
    project = quote(ref.project, safe="")
    mr_api = f"projects/{project}/merge_requests/{ref.number}"
    pipelines = await _run_json(
        "glab",
        "api",
        f"{mr_api}/pipelines?per_page=1",
        max_output_bytes=_CHECKS_OUTPUT_BYTES,
        host=ref.host,
    )
    pipeline_rows = _as_list(pipelines)
    if not pipeline_rows:
        return []
    pipeline = pipeline_rows[0]
    pipeline_id = pipeline.get("id")
    if not pipeline_id:
        return [_gitlab_pipeline_as_check(pipeline)]
    jobs = await _run_json(
        "glab",
        "api",
        f"projects/{project}/pipelines/{pipeline_id}/jobs?per_page={_SECONDARY_PAGE_SIZE}",
        max_output_bytes=_CHECKS_OUTPUT_BYTES,
        host=ref.host,
    )
    job_rows = _as_list(jobs)
    if not job_rows:
        return [_gitlab_pipeline_as_check(pipeline)]
    return [_gitlab_check(item) for item in job_rows]


async def _fetch_pull_request_checks_uncached(ref: SourceRef) -> list[dict[str, Any]]:
    fetched = await (
        _fetch_github_checks(ref) if ref.provider == "github" else _fetch_gitlab_checks(ref)
    )
    checks = _redact_provider_data(fetched)
    if not isinstance(checks, list):
        raise SourceProviderError("provider returned an invalid checks payload")
    payload = {"checks": checks}
    if _payload_size_bytes(payload) > _MAX_PAYLOAD_BYTES:
        raise SourceProviderError("provider checks payload was too large")
    return checks


# --- Issues -----------------------------------------------------------------
#
# Issues reuse the pull-request transport wholesale (`_run_json` isolation,
# redaction, byte caps, the validated-ref identity rule) and add only their own
# normalization. They deliberately do NOT touch the chip-status cache: an issue
# has no CI or merge state, so `record_full_payload_status` is never called for
# one and `get_cached_check_status` is never consulted for one either.

# Contract order for the reaction counters, paired with GitHub's own REST keys.
_GITHUB_REACTION_KEYS: tuple[tuple[str, str], ...] = (
    ("plus1", "+1"),
    ("minus1", "-1"),
    ("laugh", "laugh"),
    ("hooray", "hooray"),
    ("confused", "confused"),
    ("heart", "heart"),
    ("rocket", "rocket"),
    ("eyes", "eyes"),
)


def _int_or_zero(value: Any) -> int:
    """Coerce a provider-supplied count to a non-negative int."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value if value > 0 else 0


def _safe_https_url(value: Any) -> str:
    """Keep only an https URL from provider-echoed link fields.

    Unlike the payload's own ``url`` (which comes from the validated ref), a
    linked change or comment permalink can only come from the provider, and it
    reaches an ``href`` in the browser. Restricting it to https drops
    ``javascript:``/``data:`` and any other scheme before it can be rendered as
    a link; a rejected value degrades to an empty string, which the frontend
    renders as plain text.
    """
    if not isinstance(value, str):
        return ""
    text = value.strip()
    return text if text.lower().startswith("https://") else ""


def _issue_label(item: Any) -> dict[str, str]:
    """Normalize one label to the contract's ``{name, color, description}``.

    ``color`` is a BARE six-hex-digit string: GitHub already reports it that way
    and GitLab reports ``#rrggbb``, so the leading ``#`` is stripped here rather
    than left for the frontend to handle twice. GitLab also returns plain label
    NAMES unless ``with_labels_details`` is requested, so a bare string is
    accepted as a name-only label.
    """
    if isinstance(item, str):
        return {"name": item, "color": "", "description": ""}
    if not isinstance(item, dict):
        return {"name": "", "color": "", "description": ""}
    return {
        "name": str(item.get("name") or ""),
        "color": str(item.get("color") or "").lstrip("#"),
        "description": str(item.get("description") or ""),
    }


def _issue_labels(value: Any) -> list[dict[str, str]]:
    """Normalize a provider label list, tolerating GitLab's name-only form.

    ``_as_list`` cannot be reused here: it keeps only dict rows, which would
    silently drop every label from a GitLab reply that came back as bare
    strings. Nameless rows are dropped -- there is nothing to render.
    """
    if not isinstance(value, list):
        return []
    labels = [_issue_label(item) for item in value if isinstance(item, (str, dict))]
    return [label for label in labels if label["name"]]


def _issue_milestone(value: Any) -> dict[str, str] | None:
    """Normalize a milestone, or ``None`` when the issue has none."""
    if not isinstance(value, dict):
        return None
    return {
        "title": str(value.get("title") or ""),
        "state": str(value.get("state") or ""),
        # GitHub calls it due_on, GitLab due_date.
        "dueOn": str(value.get("due_on") or value.get("due_date") or ""),
    }


def _github_issue_reactions(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    reactions = {"total": _int_or_zero(value.get("total_count"))}
    for contract_key, github_key in _GITHUB_REACTION_KEYS:
        reactions[contract_key] = _int_or_zero(value.get(github_key))
    return reactions


def _gitlab_issue_reactions(details: dict[str, Any]) -> dict[str, int] | None:
    """Synthesize the reaction block from GitLab's up/down vote counters.

    GitLab's issue payload exposes only ``upvotes``/``downvotes``, not the full
    award-emoji breakdown, so the remaining counters stay zero rather than being
    fetched from a separate endpoint this phase does not need. ``total`` is the
    sum of what is actually known.
    """
    if "upvotes" not in details and "downvotes" not in details:
        return None
    plus1 = _int_or_zero(details.get("upvotes"))
    minus1 = _int_or_zero(details.get("downvotes"))
    reactions = {contract_key: 0 for contract_key, _ in _GITHUB_REACTION_KEYS}
    reactions["plus1"] = plus1
    reactions["minus1"] = minus1
    return {"total": plus1 + minus1, **reactions}


def _github_issue_comment(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(item.get("id") or item.get("node_id") or ""),
        "author": _author(item.get("user") or item.get("author")),
        "body": str(item.get("body") or ""),
        "createdAt": str(item.get("created_at") or item.get("createdAt") or ""),
        "url": _safe_https_url(item.get("html_url") or item.get("url")),
    }


def _github_linked_changes(timeline: Any) -> list[dict[str, Any]]:
    """Cross-referenced PULL REQUESTS from an issue's timeline.

    GitHub records "this was mentioned from X" as a ``cross-referenced`` event
    whose ``source.issue`` is the mentioning item. Issues and pull requests are
    the same REST object type, distinguished only by the presence of a
    ``pull_request`` sub-object, so filtering on that key is what keeps a plain
    issue-to-issue mention out of the linked-changes list. Duplicates are folded
    because one pull request can cross-reference an issue repeatedly.
    """
    changes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for event in _as_list(timeline):
        if str(event.get("event") or "") != "cross-referenced":
            continue
        origin = event.get("source")
        item = origin.get("issue") if isinstance(origin, dict) else None
        if not isinstance(item, dict) or not isinstance(item.get("pull_request"), dict):
            continue
        url = _safe_https_url(item.get("html_url"))
        if not url or url in seen:
            continue
        seen.add(url)
        changes.append(
            {
                "provider": "github",
                "url": url,
                "number": _int_or_zero(item.get("number")),
                "title": str(item.get("title") or ""),
                "state": str(item.get("state") or "").lower(),
            }
        )
    return changes


def _gitlab_linked_changes(related: Any) -> list[dict[str, Any]]:
    """Normalize GitLab's ``related_merge_requests`` reply to linked changes."""
    changes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in _as_list(related):
        url = _safe_https_url(item.get("web_url"))
        if not url or url in seen:
            continue
        seen.add(url)
        changes.append(
            {
                "provider": "gitlab",
                "url": url,
                "number": _int_or_zero(item.get("iid") or item.get("id")),
                "title": str(item.get("title") or ""),
                # GitLab says "opened"; the contract's state field is free-form
                # text but both providers should read the same in the panel.
                "state": "open" if item.get("state") == "opened" else str(item.get("state") or ""),
            }
        )
    return changes


async def _fetch_github_issue(ref: SourceRef) -> dict[str, Any]:
    issue_api = f"repos/{ref.owner}/{ref.repo}/issues/{ref.number}"
    details = await _run_json("gh", "api", issue_api)
    if not isinstance(details, dict):
        raise SourceProviderError("GitHub returned an invalid issue payload")

    # Secondary endpoints degrade to empty sections instead of failing the
    # whole panel: the primary payload above already carries the core data.
    comments_raw: Any
    timeline_raw: Any
    comments_raw, timeline_raw = await asyncio.gather(
        _run_json(
            "gh",
            "api",
            f"{issue_api}/comments?per_page={_SECONDARY_PAGE_SIZE}",
            max_output_bytes=_DISCUSSION_OUTPUT_BYTES,
        ),
        _run_json(
            "gh",
            "api",
            f"{issue_api}/timeline?per_page={_SECONDARY_PAGE_SIZE}",
            max_output_bytes=_DISCUSSION_OUTPUT_BYTES,
        ),
        return_exceptions=True,
    )
    partial_sections: list[str] = []
    if isinstance(comments_raw, BaseException):
        _mark_partial(partial_sections, "comments")
    if isinstance(timeline_raw, BaseException):
        _mark_partial(partial_sections, "linked changes")
    comment_rows = _as_list(_or_empty(comments_raw))
    comment_count = _int_or_zero(details.get("comments"))
    if len(comment_rows) >= _SECONDARY_PAGE_SIZE or comment_count > len(comment_rows):
        _mark_partial(partial_sections, "comments")
    timeline = _or_empty(timeline_raw)
    if len(_as_list(timeline)) >= _SECONDARY_PAGE_SIZE:
        # A full page may be truncated, so a cross-reference on a later page
        # would be missing from the linked-changes list.
        _mark_partial(partial_sections, "linked changes")

    return {
        "provider": "github",
        # Identity comes from the VALIDATED ref, never the provider echo: the
        # browser submits this url back for refresh, so a hostile or compromised
        # instance echoing a different html_url could otherwise steer a
        # credential-backed read at an unrelated repository or object.
        "url": ref.url,
        "number": ref.number,
        "title": str(details.get("title") or ""),
        "description": str(details.get("body") or ""),
        "state": str(details.get("state") or "").lower(),
        "stateReason": str(details.get("state_reason") or ""),
        "author": _author(details.get("user")),
        "createdAt": str(details.get("created_at") or ""),
        "updatedAt": str(details.get("updated_at") or ""),
        "closedAt": str(details.get("closed_at") or ""),
        "closedBy": _author(details.get("closed_by")),
        "labels": _issue_labels(details.get("labels")),
        "assignees": [
            name for name in (_author(item) for item in _as_list(details.get("assignees"))) if name
        ],
        "milestone": _issue_milestone(details.get("milestone")),
        "commentCount": comment_count,
        "locked": bool(details.get("locked")),
        "reactions": _github_issue_reactions(details.get("reactions")),
        "comments": [_github_issue_comment(item) for item in comment_rows],
        "linkedChanges": _github_linked_changes(timeline),
        "partialSections": partial_sections,
    }


async def _fetch_gitlab_issue(ref: SourceRef) -> dict[str, Any]:
    project = quote(ref.project, safe="")
    issue_api = f"projects/{project}/issues/{ref.number}"
    # with_labels_details upgrades `labels` from bare names to objects carrying
    # the colour the panel renders; without it every label would be colourless.
    details = await _run_json(
        "glab", "api", f"{issue_api}?with_labels_details=true", host=ref.host
    )
    if not isinstance(details, dict):
        raise SourceProviderError("GitLab returned an invalid issue payload")

    # Secondary endpoints degrade to empty sections instead of failing the
    # whole panel: the primary payload above already carries the core data.
    notes_raw: Any
    related_raw: Any
    notes_raw, related_raw = await asyncio.gather(
        _run_json(
            "glab",
            "api",
            f"{issue_api}/notes?per_page={_SECONDARY_PAGE_SIZE}",
            max_output_bytes=_DISCUSSION_OUTPUT_BYTES,
            host=ref.host,
        ),
        _run_json(
            "glab",
            "api",
            f"{issue_api}/related_merge_requests",
            host=ref.host,
        ),
        return_exceptions=True,
    )
    partial_sections: list[str] = []
    if isinstance(notes_raw, BaseException):
        _mark_partial(partial_sections, "comments")
    if isinstance(related_raw, BaseException):
        _mark_partial(partial_sections, "linked changes")
    note_rows = _as_list(_or_empty(notes_raw))
    if len(note_rows) >= _SECONDARY_PAGE_SIZE:
        _mark_partial(partial_sections, "comments")

    comments = []
    for note in note_rows:
        if note.get("system"):
            # Label/milestone/state churn, not discussion.
            continue
        note_id = str(note.get("id") or "")
        comments.append(
            {
                "id": note_id,
                "author": _author(note.get("author")),
                "body": str(note.get("body") or ""),
                "createdAt": str(note.get("created_at") or ""),
                # GitLab notes carry no permalink of their own, so anchor off the
                # VALIDATED ref url rather than any provider-echoed link.
                "url": f"{ref.url}#note_{note_id}" if note_id else "",
            }
        )
    reported_comment_count = _int_or_zero(details.get("user_notes_count"))
    if reported_comment_count > len(comments) and "comments" not in partial_sections:
        _mark_partial(partial_sections, "comments")

    return {
        "provider": "gitlab",
        # See _fetch_github_issue: identity is the validated ref, not the
        # provider's web_url/iid. This matters most for a self-managed instance,
        # whose responses are outside the trust boundary the allowlist sets.
        "url": ref.url,
        "number": ref.number,
        "title": str(details.get("title") or ""),
        "description": str(details.get("description") or ""),
        # GitLab says "opened"; the contract's vocabulary is open/closed.
        "state": "open" if details.get("state") == "opened" else str(details.get("state") or ""),
        # GitLab has no equivalent of GitHub's state_reason.
        "stateReason": "",
        "author": _author(details.get("author")),
        "createdAt": str(details.get("created_at") or ""),
        "updatedAt": str(details.get("updated_at") or ""),
        "closedAt": str(details.get("closed_at") or ""),
        "closedBy": _author(details.get("closed_by")),
        "labels": _issue_labels(details.get("labels")),
        "assignees": [
            name for name in (_author(item) for item in _as_list(details.get("assignees"))) if name
        ],
        "milestone": _issue_milestone(details.get("milestone")),
        "commentCount": reported_comment_count or len(comments),
        "locked": bool(details.get("discussion_locked")),
        "reactions": _gitlab_issue_reactions(details),
        "comments": comments,
        "linkedChanges": _gitlab_linked_changes(_or_empty(related_raw)),
        "partialSections": partial_sections,
    }


_T = TypeVar("_T")


def _finish_inflight(store: dict[str, asyncio.Task[_T]], url: str, task: asyncio.Task[_T]) -> None:
    """Drop a completed shared fetch and consume orphaned exceptions."""
    if store.get(url) is task:
        store.pop(url, None)
    if not task.cancelled():
        with contextlib.suppress(Exception):
            task.exception()


def _direct_fetch_tasks() -> set[asyncio.Task[Any]]:
    """Snapshot unique direct full/issue/check tasks, including detached stale work.

    Issue fetches are counted here so their reservations are real: the pending
    cap and the retained-byte ceiling are computed from this set, so a task
    absent from it would hold a lease nothing ever reads.
    """
    tasks: set[asyncio.Task[Any]] = set(_CHECKS_FETCH_INFLIGHT.values())
    for full_tasks in _FULL_FETCH_TASKS.values():
        tasks.update(full_tasks)
    for issue_tasks in _ISSUE_FETCH_TASKS.values():
        tasks.update(issue_tasks)
    return tasks


def _ensure_direct_fetch_capacity(reservation_bytes: int) -> None:
    tasks = _direct_fetch_tasks()
    reserved = sum(
        amount
        for task, amount in _DIRECT_FETCH_RESERVATIONS.items()
        if task in tasks and not task.done()
    )
    if (
        len(tasks) >= _DIRECT_FETCH_PENDING_MAX
        or reservation_bytes > _DIRECT_FETCH_MAX_RESERVED_BYTES - reserved
    ):
        raise SourceProviderError(
            "Too many source requests are pending; retry shortly."
        )


def _reserve_direct_fetch(task: asyncio.Task[Any], reservation_bytes: int) -> None:
    """Hold a conservative retained-byte lease until the task terminates."""
    _DIRECT_FETCH_RESERVATIONS[task] = reservation_bytes

    def release(done: asyncio.Task[Any]) -> None:
        _DIRECT_FETCH_RESERVATIONS.pop(done, None)

    task.add_done_callback(release)


async def fetch_pull_request_checks(raw_url: str) -> list[dict[str, Any]]:
    """Fetch current CI checks, coalescing concurrent requests for one URL."""
    # Refresh the self-managed GitLab allowlist off the event loop before any
    # URL validation reads the cached snapshot.
    await ensure_gitlab_hosts_loaded()
    ref = _require_change_ref(parse_source_url(raw_url))
    task = _CHECKS_FETCH_INFLIGHT.get(ref.url)
    if task is None:
        _ensure_direct_fetch_capacity(_CHECKS_FETCH_RESERVATION_BYTES)
        task = asyncio.create_task(_fetch_pull_request_checks_uncached(ref))
        _CHECKS_FETCH_INFLIGHT[ref.url] = task
        _reserve_direct_fetch(task, _CHECKS_FETCH_RESERVATION_BYTES)

        def finish_checks(done: asyncio.Task[list[dict[str, Any]]]) -> None:
            _finish_inflight(_CHECKS_FETCH_INFLIGHT, ref.url, done)

        task.add_done_callback(finish_checks)
    return await asyncio.shield(task)


async def _fetch_pull_request_uncached(ref: SourceRef, generation: int) -> dict[str, Any]:
    fetched = await (_fetch_github(ref) if ref.provider == "github" else _fetch_gitlab(ref))
    data = _redact_provider_data(fetched)
    if not isinstance(data, dict):
        raise SourceProviderError("provider returned an invalid pull-request payload")
    payload_size = _payload_size_bytes(data)
    if payload_size > _MAX_PAYLOAD_BYTES:
        raise SourceProviderError("provider pull-request payload was too large")

    async with _CACHE_LOCK:
        if _FULL_FETCH_GENERATIONS.get(ref.url, 0) != generation:
            # A successful mutation invalidated this generation while provider
            # I/O was running. Return its result to existing waiters, but never
            # let pre-mutation data overwrite the post-mutation cache.
            return data
        now = time.monotonic()
        # Sweep expired entries on write, then cap by both recency count and
        # aggregate serialized weight. A PR combines several provider commands,
        # so per-command pipe limits alone do not bound retained cache memory.
        for key in [
            key for key, (stored_at, _, _) in _CACHE.items() if now - stored_at >= _CACHE_TTL_SECS
        ]:
            del _CACHE[key]
        _CACHE[ref.url] = (now, payload_size, data)
        while (
            len(_CACHE) > _CACHE_MAX_ENTRIES
            or sum(entry[1] for entry in _CACHE.values()) > _CACHE_MAX_BYTES
        ):
            del _CACHE[min(_CACHE, key=lambda key: _CACHE[key][0])]
        # One provider read, both surfaces: project this payload onto the chip
        # cache so the sidebar cannot keep rendering an older lifecycle than the
        # detail panel it was just fetched for. Kept INSIDE the lock, in the same
        # transaction as the generation check and the `_CACHE` write, so a
        # provider mutation cannot land between the passing generation check and
        # this projection and republish pre-mutation status into the chip cache
        # (which would emit a stale `source_status` delta the full-cache
        # invalidation cannot undo). `record_full_payload_status` is synchronous
        # and never re-acquires `_CACHE_LOCK`, so running it here cannot deadlock.
        record_full_payload_status(ref.url, data)
    return data


async def fetch_pull_request(raw_url: str, *, refresh: bool = False) -> dict[str, Any]:
    """Fetch a PR/MR, sharing one provider fanout per normalized URL."""
    # Refresh the self-managed GitLab allowlist off the event loop before any
    # URL validation reads the cached snapshot.
    await ensure_gitlab_hosts_loaded()
    ref = _require_change_ref(parse_source_url(raw_url))
    now = time.monotonic()
    async with _CACHE_LOCK:
        cached = _CACHE.get(ref.url)
        if not refresh and cached and now - cached[0] < _CACHE_TTL_SECS:
            return cached[2]
        task = _FULL_FETCH_INFLIGHT.get(ref.url)
        if task is None:
            _ensure_direct_fetch_capacity(_FULL_FETCH_RESERVATION_BYTES)
            generation = _FULL_FETCH_GENERATIONS.get(ref.url, 0)
            task = asyncio.create_task(_fetch_pull_request_uncached(ref, generation))
            _FULL_FETCH_INFLIGHT[ref.url] = task
            _FULL_FETCH_TASKS.setdefault(ref.url, set()).add(task)
            _reserve_direct_fetch(task, _FULL_FETCH_RESERVATION_BYTES)

            def finish_full_fetch(done: asyncio.Task[dict[str, Any]]) -> None:
                _finish_inflight(_FULL_FETCH_INFLIGHT, ref.url, done)
                active = _FULL_FETCH_TASKS.get(ref.url)
                if active is None:
                    return
                active.discard(done)
                if not active:
                    _FULL_FETCH_TASKS.pop(ref.url, None)
                    _FULL_FETCH_GENERATIONS.pop(ref.url, None)

            task.add_done_callback(finish_full_fetch)
    # Shield the shared fetch so one disconnected browser cannot cancel work
    # still awaited by another request for the same URL.
    return await asyncio.shield(task)


async def _fetch_issue_uncached(ref: SourceRef) -> dict[str, Any]:
    fetched = await (
        _fetch_github_issue(ref) if ref.provider == "github" else _fetch_gitlab_issue(ref)
    )
    data = _redact_provider_data(fetched)
    if not isinstance(data, dict):
        raise SourceProviderError("provider returned an invalid issue payload")
    payload_size = _payload_size_bytes(data)
    if payload_size > _MAX_PAYLOAD_BYTES:
        raise SourceProviderError("provider issue payload was too large")

    async with _ISSUE_CACHE_LOCK:
        now = time.monotonic()
        # Sweep expired entries on write, then cap by both recency count and
        # aggregate serialized weight -- an issue combines several provider
        # commands, so per-command pipe limits alone do not bound retained
        # cache memory. Deliberately NOT paired with `record_full_payload_status`:
        # an issue has no chip status, so projecting one would publish a
        # meaningless {ci, state} for a URL the sidebar never asks about.
        for key in [
            key
            for key, (stored_at, _, _) in _ISSUE_CACHE.items()
            if now - stored_at >= _CACHE_TTL_SECS
        ]:
            del _ISSUE_CACHE[key]
        _ISSUE_CACHE[ref.url] = (now, payload_size, data)
        while (
            len(_ISSUE_CACHE) > _CACHE_MAX_ENTRIES
            or sum(entry[1] for entry in _ISSUE_CACHE.values()) > _ISSUE_CACHE_MAX_BYTES
        ):
            del _ISSUE_CACHE[min(_ISSUE_CACHE, key=lambda key: _ISSUE_CACHE[key][0])]
    return data


async def fetch_issue(raw_url: str, *, refresh: bool = False) -> dict[str, Any]:
    """Fetch an issue, sharing one provider fanout per normalized URL."""
    # Refresh the self-managed GitLab allowlist off the event loop before any
    # URL validation reads the cached snapshot.
    await ensure_gitlab_hosts_loaded()
    ref = parse_source_url(raw_url)
    if ref.kind != "issue":
        raise ValueError("This URL points at a pull request or merge request, not an issue.")
    now = time.monotonic()
    async with _ISSUE_CACHE_LOCK:
        cached = _ISSUE_CACHE.get(ref.url)
        if not refresh and cached and now - cached[0] < _CACHE_TTL_SECS:
            return cached[2]
        task = _ISSUE_FETCH_INFLIGHT.get(ref.url)
        if task is None:
            _ensure_direct_fetch_capacity(_ISSUE_FETCH_RESERVATION_BYTES)
            task = asyncio.create_task(_fetch_issue_uncached(ref))
            _ISSUE_FETCH_INFLIGHT[ref.url] = task
            _ISSUE_FETCH_TASKS.setdefault(ref.url, set()).add(task)
            _reserve_direct_fetch(task, _ISSUE_FETCH_RESERVATION_BYTES)

            def finish_issue_fetch(done: asyncio.Task[dict[str, Any]]) -> None:
                _finish_inflight(_ISSUE_FETCH_INFLIGHT, ref.url, done)
                active = _ISSUE_FETCH_TASKS.get(ref.url)
                if active is None:
                    return
                active.discard(done)
                if not active:
                    _ISSUE_FETCH_TASKS.pop(ref.url, None)

            task.add_done_callback(finish_issue_fetch)
    # Shield the shared fetch so one disconnected browser cannot cancel work
    # still awaited by another request for the same URL.
    return await asyncio.shield(task)


async def api_pull_request_source(request: web.Request) -> web.Response:
    """Owner-only POST ``/api/source/pull-request`` with ``{url, refresh?}``."""
    denied = _authorize_owner_request(
        request, "source.pull_request.read", allow_local_no_owner=True
    )
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except asyncio.CancelledError:
        _audit_source_api(request, "source.pull_request.read", "failed", "request_cancelled")
        raise
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    try:
        data = await fetch_pull_request(
            str(body.get("url") or ""), refresh=bool(body.get("refresh"))
        )
    except asyncio.CancelledError:
        _audit_source_api(request, "source.pull_request.read", "failed", "request_cancelled")
        raise
    except ValueError as exc:
        _audit_source_api(request, "source.pull_request.read", "failed", "invalid_request")
        return web.json_response({"error": str(exc)}, status=400)
    except SourceProviderError as exc:
        _audit_source_api(request, "source.pull_request.read", "failed", "provider_error")
        return web.json_response({"error": str(exc)}, status=503)
    _audit_source_api(request, "source.pull_request.read", "completed")
    return web.json_response(data)


async def api_issue_source(request: web.Request) -> web.Response:
    """Owner-only POST ``/api/source/issue`` with ``{url, refresh?}``.

    Same authorization, audit, and error mapping as
    :func:`api_pull_request_source` -- an issue read is credential-backed
    provider data too, so it is gated on the dashboard owner identically.
    """
    denied = _authorize_owner_request(request, "source.issue.read", allow_local_no_owner=True)
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except asyncio.CancelledError:
        _audit_source_api(request, "source.issue.read", "failed", "request_cancelled")
        raise
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    try:
        data = await fetch_issue(str(body.get("url") or ""), refresh=bool(body.get("refresh")))
    except asyncio.CancelledError:
        _audit_source_api(request, "source.issue.read", "failed", "request_cancelled")
        raise
    except ValueError as exc:
        _audit_source_api(request, "source.issue.read", "failed", "invalid_request")
        return web.json_response({"error": str(exc)}, status=400)
    except SourceProviderError as exc:
        _audit_source_api(request, "source.issue.read", "failed", "provider_error")
        return web.json_response({"error": str(exc)}, status=503)
    _audit_source_api(request, "source.issue.read", "completed")
    return web.json_response(data)


async def api_pull_request_checks(request: web.Request) -> web.Response:
    """Owner-only POST ``/api/source/pull-request/checks`` with ``{url}``."""
    denied = _authorize_owner_request(
        request, "source.pull_request.checks", allow_local_no_owner=True
    )
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except asyncio.CancelledError:
        _audit_source_api(request, "source.pull_request.checks", "failed", "request_cancelled")
        raise
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    try:
        checks = await fetch_pull_request_checks(str(body.get("url") or ""))
    except asyncio.CancelledError:
        _audit_source_api(request, "source.pull_request.checks", "failed", "request_cancelled")
        raise
    except ValueError as exc:
        _audit_source_api(request, "source.pull_request.checks", "failed", "invalid_request")
        return web.json_response({"error": str(exc)}, status=400)
    except SourceProviderError as exc:
        _audit_source_api(request, "source.pull_request.checks", "failed", "provider_error")
        return web.json_response({"error": str(exc)}, status=503)
    _audit_source_api(request, "source.pull_request.checks", "completed")
    return web.json_response({"checks": checks})


# Upper bound on URLs accepted per status request. Matches the Changes tab's
# own source cap (website/src/utils/pullRequestLinks.ts MAX_PULL_REQUEST_SOURCES)
# so one request covers a full strip, and caps the parse work for a hostile body.
STATUS_URLS_MAX = 64


async def api_pull_request_status(request: web.Request) -> web.Response:
    """Owner-only POST ``/api/source/pull-request/status`` with ``{urls: [...]}``.

    Returns the *cached* lightweight ``{ci, state}`` for each URL and kicks a
    bounded background refresh for stale entries — the same cache and pacing the
    sidebar chips use. Never blocks on a provider call: unknown URLs simply come
    back absent and appear on a later poll.
    """
    denied = _authorize_owner_request(
        request, "source.pull_request.status", allow_local_no_owner=True
    )
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except asyncio.CancelledError:
        _audit_source_api(request, "source.pull_request.status", "failed", "request_cancelled")
        raise
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    raw_urls = body.get("urls")
    if not isinstance(raw_urls, list):
        _audit_source_api(request, "source.pull_request.status", "failed", "invalid_request")
        return web.json_response({"error": "A list of pull-request URLs is required."}, status=400)
    try:
        await ensure_gitlab_hosts_loaded()
    except asyncio.CancelledError:
        # This is a direct await in the handler (unlike the read/checks/resolve
        # endpoints, which reach ensure() through a provider helper wrapped in
        # the cancellation guard below). A cancellation here would otherwise
        # unwind past the terminal ``completed`` audit, leaving an authorized
        # status attempt absent from the tamper-evident SEL chain. Pair it with
        # ``failed/request_cancelled`` — matching the body-parse guard above —
        # then re-raise.
        _audit_source_api(request, "source.pull_request.status", "failed", "request_cancelled")
        raise
    canonical: list[str] = []
    for value in raw_urls[:STATUS_URLS_MAX]:
        if not isinstance(value, str):
            continue
        try:
            # An issue URL reaching here would be scheduled for a chip refresh
            # and answered from the pull-request namespace. `_require_change_ref`
            # raises ValueError, which this loop already treats as "not a
            # supported source URL" and skips.
            ref = _require_change_ref(parse_source_url(value))
        except ValueError:
            continue
        if ref.url not in canonical:
            canonical.append(ref.url)
    statuses = {
        url: status for url in canonical if (status := get_cached_check_status(url)) is not None
    }
    refreshing = schedule_check_refresh(canonical)
    _audit_source_api(request, "source.pull_request.status", "completed")
    # ``refreshing`` lets the client re-poll shortly instead of on TTL pacing, so
    # a state change lands within seconds of the background refresh rather than up
    # to one extra TTL later. ``ttlSecs`` is the server's own cache TTL, so the
    # client's steady-state pacing tracks it instead of hardcoding a copy.
    return web.json_response(
        {
            "statuses": statuses,
            "refreshing": refreshing,
            "ttlSecs": CHECK_STATUS_TTL_SECS,
        }
    )


_GITHUB_THREAD_ID_RE = re.compile(r"^[A-Za-z0-9_=+-]{1,128}$")
_GITLAB_THREAD_ID_RE = re.compile(r"^[A-Fa-f0-9]{1,128}$")

_GITHUB_RESOLVE_MUTATION = (
    "mutation($threadId:ID!){resolveReviewThread(input:{threadId:$threadId})"
    "{thread{isResolved}}}"
)

_GITHUB_UNRESOLVE_MUTATION = (
    "mutation($threadId:ID!){unresolveReviewThread(input:{threadId:$threadId})"
    "{thread{isResolved}}}"
)

_GITHUB_THREAD_REPLY_MUTATION = (
    "mutation($threadId:ID!,$body:String!)"
    "{addPullRequestReviewThreadReply"
    "(input:{pullRequestReviewThreadId:$threadId,body:$body})"
    "{comment{id}}}"
)

# Comment bodies are user text passed as a single CLI argument (argv, never a
# shell string), so the only real risk is size. GitHub rejects bodies past 65536
# characters anyway, so refusing here turns a provider error into a clear local
# one and bounds the argument.
_MAX_COMMENT_CHARS = 65536


def _validated_comment_body(body: str) -> str:
    """Return a comment body that is safe and worth sending.

    Empty bodies are refused rather than posted: an accidental empty comment is
    visible to everyone on the pull request and cannot be removed from here.
    """
    text = (body or "").strip()
    if not text:
        raise ValueError("A comment body is required.")
    if len(text) > _MAX_COMMENT_CHARS:
        raise ValueError(
            f"A comment body must be at most {_MAX_COMMENT_CHARS} characters.")
    return text


# Node ids are provider-issued, but they are interpolated into a CLI argument,
# so they get the same shape check as review-thread ids before dispatch.
_GITHUB_NODE_ID_RE = re.compile(r"^[A-Za-z0-9_=+-]{1,128}$")

_GITHUB_PULL_REQUEST_NODE_QUERY = (
    "query($owner:String!,$repo:String!,$number:Int!){repository(owner:$owner,name:$repo)"
    "{squashMergeAllowed mergeCommitAllowed rebaseMergeAllowed"
    " pullRequest(number:$number){id isDraft state autoMergeRequest{enabledAt}}}}"
)

_GITHUB_AUTO_MERGE_MUTATION = (
    "mutation($pullRequestId:ID!,$mergeMethod:PullRequestMergeMethod!)"
    "{enablePullRequestAutoMerge(input:{pullRequestId:$pullRequestId,mergeMethod:$mergeMethod})"
    "{pullRequest{autoMergeRequest{enabledAt}}}}"
)

_GITHUB_READY_MUTATION = (
    "mutation($pullRequestId:ID!)"
    "{markPullRequestReadyForReview(input:{pullRequestId:$pullRequestId})"
    "{pullRequest{isDraft}}}"
)

# GitHub merge methods in the order this dashboard prefers them, gated on what
# the repository actually allows: enabling auto-merge with a disallowed method
# fails, and the repository's allow-list is the only machine-readable signal
# GitHub exposes (there is no "default method" field in the API).
_GITHUB_MERGE_METHODS: tuple[tuple[str, str], ...] = (
    ("squashMergeAllowed", "SQUASH"),
    ("mergeCommitAllowed", "MERGE"),
    ("rebaseMergeAllowed", "REBASE"),
)

# GitLab stores draft state as a title prefix, but exposes a dedicated
# mutation that performs the transition itself. Using it keeps the prefix
# grammar (Draft:/[WIP]/...) the provider's problem and avoids a read-modify-
# write of the title, which would clobber a concurrent retitle and could
# mangle titles that merely start with a draft-like word ("Drafting widgets").
_GITLAB_SET_DRAFT_MUTATION = (
    "mutation($projectPath:ID!,$iid:String!,$draft:Boolean!)"
    "{mergeRequestSetDraft(input:{projectPath:$projectPath,iid:$iid,draft:$draft})"
    "{errors mergeRequest{draft}}}"
)


def _raise_on_graphql_errors(payload: Any, message: str) -> None:
    """Raise when a GraphQL response carries errors instead of a mutation result.

    GraphQL reports refusals in the body with HTTP 200, so a provider CLI that
    only fails on transport errors would let a rejected mutation look like a
    success. Both the transport-level ``errors`` array and the per-mutation
    ``errors`` field are checked, since GitLab uses the latter.
    """
    if not isinstance(payload, dict):
        raise SourceProviderError(message)
    if payload.get("errors"):
        raise SourceProviderError(message)
    data = payload.get("data")
    if not isinstance(data, dict):
        return
    for result in data.values():
        if isinstance(result, dict) and result.get("errors"):
            raise SourceProviderError(message)


def _github_repository_node(payload: Any) -> dict[str, Any]:
    """Extract the repository node from a GraphQL pull-request node response."""
    data = payload.get("data") if isinstance(payload, dict) else None
    repository = data.get("repository") if isinstance(data, dict) else None
    return repository if isinstance(repository, dict) else {}


async def _github_pull_request_node(ref: SourceRef) -> tuple[str, dict[str, Any]]:
    """Return the pull request's validated node id plus its repository node."""
    payload = await _run_json(
        "gh",
        "api",
        "graphql",
        "-f",
        f"query={_GITHUB_PULL_REQUEST_NODE_QUERY}",
        "-f",
        f"owner={ref.owner}",
        "-f",
        f"repo={ref.repo}",
        "-F",
        f"number={ref.number}",
    )
    repository = _github_repository_node(payload)
    pull_request = repository.get("pullRequest")
    pull_request = pull_request if isinstance(pull_request, dict) else {}
    node_id = str(pull_request.get("id") or "")
    if not _GITHUB_NODE_ID_RE.fullmatch(node_id):
        raise SourceProviderError("GitHub did not return a usable pull-request id")
    return node_id, repository


async def _invalidate_full_payload_cache(url: str) -> None:
    """Supersede the FULL pull-request payload cache and its in-flight fetch.

    Deliberately does NOT touch the lightweight chip-status cache. A caller that
    has just written a fresh chip status (the changed-status path in
    ``_refresh_check_status``) must drop only the now-stale full payload behind
    the detail panel, not the chip entry it just produced — invalidating the
    chip here would pop that entry and bump its generation, spuriously
    re-judging the next refresh as "changed" and spinning the very
    mutual-invalidation loop this projection exists to avoid.
    """
    async with _CACHE_LOCK:
        _CACHE.pop(url, None)
        if _FULL_FETCH_TASKS.get(url):
            _FULL_FETCH_GENERATIONS[url] = _FULL_FETCH_GENERATIONS.get(url, 0) + 1
        else:
            _FULL_FETCH_GENERATIONS.pop(url, None)
        _FULL_FETCH_INFLIGHT.pop(url, None)


async def _invalidate_pull_request_cache(url: str) -> None:
    """Supersede cached and in-flight data before a provider mutation."""
    await _invalidate_full_payload_cache(url)
    _invalidate_check_status(url)


async def _github_thread_ref(raw_url: str, thread_id: str) -> SourceRef:
    """Validate a thread id AND prove it belongs to the pull request in the url.

    The ownership check is the security control: the thread id arrives from the
    browser, and without it an owner-authenticated mutation could be steered at a
    thread on an unrelated pull request. Shared by reply/resolve/unresolve so no
    future call site can skip it.
    """
    await ensure_gitlab_hosts_loaded()
    # The docstring above promises this is the one place reply/resolve/unresolve
    # cannot skip, so the kind check belongs here too — not only in the callers
    # that happen to repeat it.
    ref = _require_change_ref(parse_source_url(raw_url))
    if ref.provider != "github":
        raise ValueError(
            "Replying to review threads is only supported on GitHub so far.")
    if not _GITHUB_THREAD_ID_RE.fullmatch(thread_id or ""):
        raise ValueError("A valid thread id is required.")
    threads = await _run_json(
        "gh", "api", "graphql",
        "-f", f"query={_GITHUB_REVIEW_THREADS_QUERY}",
        "-f", f"owner={ref.owner}",
        "-f", f"repo={ref.repo}",
        "-F", f"number={ref.number}",
    )
    if thread_id not in _github_thread_ids(threads):
        raise ValueError("Review thread does not belong to this pull request.")
    return ref


async def reply_to_review_thread(raw_url: str, thread_id: str, body: str) -> None:
    """Post a reply into an existing review thread."""
    text = _validated_comment_body(body)
    ref = await _github_thread_ref(raw_url, thread_id)
    # Invalidate before dispatch, matching resolve: once the provider call
    # starts its remote result is uncertain under cancellation, so a stale
    # generation must already be unable to satisfy the post-write refresh.
    await _invalidate_pull_request_cache(ref.url)
    payload = await _run_json(
        "gh", "api", "graphql",
        "-f", f"query={_GITHUB_THREAD_REPLY_MUTATION}",
        "-f", f"threadId={thread_id}",
        "-f", f"body={text}",
    )
    _raise_on_graphql_errors(payload, "could not post the reply")


async def unresolve_pull_request_thread(raw_url: str, thread_id: str) -> None:
    """Reopen a resolved review thread."""
    ref = await _github_thread_ref(raw_url, thread_id)
    await _invalidate_pull_request_cache(ref.url)
    payload = await _run_json(
        "gh", "api", "graphql",
        "-f", f"query={_GITHUB_UNRESOLVE_MUTATION}",
        "-f", f"threadId={thread_id}",
    )
    _raise_on_graphql_errors(payload, "could not reopen the thread")


async def comment_on_pull_request(raw_url: str, body: str) -> None:
    """Post a top-level comment on the pull request itself (not a thread)."""
    text = _validated_comment_body(body)
    await ensure_gitlab_hosts_loaded()
    # Issue refs are refused here for the same reason the thread mutations refuse
    # them: this posts to /issues/{number}/comments, and on GitHub issues and pull
    # requests share one number counter, so an issue URL would publish a comment
    # on an unrelated issue that happens to carry the PR's number.
    ref = _require_change_ref(parse_source_url(raw_url))
    if ref.provider != "github":
        raise ValueError("Commenting is only supported on GitHub so far.")
    await _invalidate_pull_request_cache(ref.url)
    # Issue comments, because a pull request's conversation timeline IS its issue
    # timeline; the review-comment endpoints require a diff position.
    await _run_json(
        "gh", "api", "-X", "POST",
        f"repos/{ref.owner}/{ref.repo}/issues/{ref.number}/comments",
        "-f", f"body={text}",
    )


async def resolve_pull_request_thread(raw_url: str, thread_id: str) -> None:
    """Resolve a review thread after conservatively invalidating cached data."""
    # Refresh the self-managed GitLab allowlist off the event loop before any
    # URL validation reads the cached snapshot.
    await ensure_gitlab_hosts_loaded()
    ref = _require_change_ref(parse_source_url(raw_url))
    thread_pattern = _GITHUB_THREAD_ID_RE if ref.provider == "github" else _GITLAB_THREAD_ID_RE
    if not thread_pattern.fullmatch(thread_id or ""):
        raise ValueError("A valid thread id is required.")
    if ref.provider == "github":
        threads = await _run_json(
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={_GITHUB_REVIEW_THREADS_QUERY}",
            "-f",
            f"owner={ref.owner}",
            "-f",
            f"repo={ref.repo}",
            "-F",
            f"number={ref.number}",
        )
        if thread_id not in _github_thread_ids(threads):
            raise ValueError("Review thread does not belong to this pull request.")
        # Invalidate before dispatch. Once the provider call starts its remote
        # result is uncertain under cancellation, so stale generations must
        # already be unable to refill or satisfy a post-mutation refresh.
        await _invalidate_pull_request_cache(ref.url)
        await _run_json(
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={_GITHUB_RESOLVE_MUTATION}",
            "-f",
            f"threadId={thread_id}",
        )
    else:
        project = quote(ref.project, safe="")
        await _invalidate_pull_request_cache(ref.url)
        await _run_json(
            "glab",
            "api",
            "-X",
            "PUT",
            f"projects/{project}/merge_requests/{ref.number}/discussions/{thread_id}",
            "-f",
            "resolved=true",
            host=ref.host,
        )


async def _gitlab_merge_request(ref: SourceRef) -> dict[str, Any]:
    """Read a merge request so a mutation can refuse inapplicable requests."""
    project = quote(ref.project, safe="")
    details = await _run_json(
        "glab", "api", f"projects/{project}/merge_requests/{ref.number}", host=ref.host
    )
    if not isinstance(details, dict):
        raise SourceProviderError("GitLab returned an invalid merge-request payload")
    return details


def _gitlab_is_draft(details: dict[str, Any]) -> bool:
    """Report draft state, tolerating the legacy ``work_in_progress`` field."""
    return bool(details.get("draft") or details.get("work_in_progress"))


# GitLab pipeline statuses that still have to finish. While one of these is the
# head pipeline's status, merge_when_pipeline_succeeds genuinely defers the
# merge; outside them there is nothing left to wait for and the same call
# merges right away.
_GITLAB_PENDING_PIPELINE_STATUSES = frozenset(
    {"created", "waiting_for_resource", "preparing", "pending", "running", "scheduled", "manual"}
)


def _gitlab_has_pending_pipeline(details: dict[str, Any]) -> bool:
    """Report whether a pipeline would actually gate the merge."""
    pipeline = details.get("head_pipeline") or details.get("pipeline")
    if not isinstance(pipeline, dict):
        return False
    return str(pipeline.get("status") or "").lower() in _GITLAB_PENDING_PIPELINE_STATUSES


class ConfirmationRequired(ValueError):
    """A mutation refused because the caller has not acknowledged its effect.

    Distinct from an ordinary rejection so the response can carry a machine-
    readable marker: the request is not malformed and is not permanently
    refused, it is waiting on an acknowledgement the client can only make
    meaningfully once the server has told it what is actually at stake.
    """


async def enable_pull_request_auto_merge(
    raw_url: str, *, confirm_immediate_merge: bool = False
) -> str:
    """Enable auto-merge (merge once requirements pass) and return the method.

    Both providers are read first so an inapplicable request is refused before
    anything is dispatched. GitHub has a real auto-merge switch and refuses a
    draft or already-armed pull request. GitLab has none: its equivalent is a
    merge call flagged ``merge_when_pipeline_succeeds``, which merges
    **immediately** when no pipeline is pending. That makes the GitLab path a
    merge authorization, so when nothing would gate the merge the caller must
    pass ``confirm_immediate_merge`` to acknowledge it. The refusal is raised as
    ``ConfirmationRequired`` so a client can discover the hazard from the server
    rather than pre-emptively asserting consent: the acknowledgement is only
    ever sent in answer to this specific refusal, which keeps the guard live for
    the dashboard instead of degrading it into a constant.
    """
    # Warm the allowlist BEFORE parsing: a self-managed URL is validated against
    # the cached snapshot, so a cold mutation would otherwise be rejected as an
    # unsupported host (400) even though the operator authorized it.
    await ensure_gitlab_hosts_loaded()
    ref = _require_change_ref(parse_source_url(raw_url))
    if ref.provider == "github":
        node_id, repository = await _github_pull_request_node(ref)
        pull_request = repository.get("pullRequest")
        pull_request = pull_request if isinstance(pull_request, dict) else {}
        if pull_request.get("isDraft"):
            raise ValueError(
                "GitHub cannot enable auto-merge on a draft pull request. "
                "Mark it ready for review first."
            )
        if pull_request.get("autoMergeRequest"):
            raise ValueError("Auto-merge is already enabled for this pull request.")
        method = next(
            (
                graphql_method
                for field, graphql_method in _GITHUB_MERGE_METHODS
                if repository.get(field)
            ),
            "",
        )
        if not method:
            raise ValueError("This repository does not allow any merge method.")
        # Invalidate before dispatch: once the provider call starts its remote
        # result is uncertain under cancellation, so stale generations must
        # already be unable to refill or satisfy a post-mutation refresh.
        await _invalidate_pull_request_cache(ref.url)
        payload = await _run_json(
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={_GITHUB_AUTO_MERGE_MUTATION}",
            "-f",
            f"pullRequestId={node_id}",
            "-f",
            f"mergeMethod={method}",
        )
        _raise_on_graphql_errors(payload, "GitHub refused to enable auto-merge")
        return method.lower()
    details = await _gitlab_merge_request(ref)
    if _gitlab_is_draft(details):
        raise ValueError(
            "GitLab cannot arm a draft merge request. Mark it ready for review first."
        )
    if details.get("merge_when_pipeline_succeeds"):
        raise ValueError("Auto-merge is already enabled for this merge request.")
    if not _gitlab_has_pending_pipeline(details) and not confirm_immediate_merge:
        raise ConfirmationRequired(
            "No pipeline is pending, so GitLab would merge this merge request "
            "immediately. Confirm the merge to proceed."
        )
    project = quote(ref.project, safe="")
    await _invalidate_pull_request_cache(ref.url)
    await _run_json(
        "glab",
        "api",
        "-X",
        "PUT",
        f"projects/{project}/merge_requests/{ref.number}/merge",
        "-f",
        "merge_when_pipeline_succeeds=true",
        host=ref.host,
    )
    return "pipeline"


async def mark_pull_request_ready(raw_url: str) -> None:
    """Take a draft pull/merge request out of draft state.

    Both providers expose a dedicated transition, so neither path rewrites the
    title: GitLab's draft prefix grammar stays the provider's concern and a
    concurrent retitle cannot be clobbered by this call.
    """
    # Warm the allowlist BEFORE parsing: a self-managed URL is validated against
    # the cached snapshot, so a cold mutation would otherwise be rejected as an
    # unsupported host (400) even though the operator authorized it.
    await ensure_gitlab_hosts_loaded()
    ref = _require_change_ref(parse_source_url(raw_url))
    if ref.provider == "github":
        node_id, repository = await _github_pull_request_node(ref)
        pull_request = repository.get("pullRequest")
        pull_request = pull_request if isinstance(pull_request, dict) else {}
        if not pull_request.get("isDraft"):
            raise ValueError("This pull request is already ready for review.")
        await _invalidate_pull_request_cache(ref.url)
        payload = await _run_json(
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={_GITHUB_READY_MUTATION}",
            "-f",
            f"pullRequestId={node_id}",
        )
        _raise_on_graphql_errors(payload, "GitHub refused to mark the pull request ready")
        return
    details = await _gitlab_merge_request(ref)
    if not _gitlab_is_draft(details):
        raise ValueError("This merge request is already ready for review.")
    await _invalidate_pull_request_cache(ref.url)
    payload = await _run_json(
        "glab",
        "api",
        "graphql",
        "-f",
        f"query={_GITLAB_SET_DRAFT_MUTATION}",
        "-f",
        f"projectPath={ref.project}",
        "-f",
        f"iid={ref.number}",
        "-F",
        "draft=false",
        host=ref.host,
    )
    _raise_on_graphql_errors(payload, "GitLab refused to mark the merge request ready")


# The three verdicts GitHub accepts when submitting a pending review. DISMISS and
# the other review endpoints are deliberately absent: this path exists to publish a
# draft the caller has read, not to act on reviews someone else submitted.
_REVIEW_SUBMIT_EVENTS = frozenset({"APPROVE", "REQUEST_CHANGES", "COMMENT"})
# The verdicts that GATE a merge, and therefore the ones whose attachment to a
# specific commit is load-bearing. A COMMENT review carries no verdict, so a head
# that moves under it costs nothing but misplaced line anchors.
_REVIEW_GATING_EVENTS = frozenset({"APPROVE", "REQUEST_CHANGES"})
# GitHub review ids are positive integers. Bounded and anchored because the value
# is interpolated into the REST path.
_GITHUB_REVIEW_ID_RE = re.compile(r"[1-9][0-9]{0,19}")


def _flatten_paginated(payload: Any) -> list[dict[str, Any]]:
    """Flatten a ``gh api --paginate --slurp`` result into one list of objects.

    ``--slurp`` returns an array of per-page arrays, but a single-page result from
    a caller without the flag is already flat, so both shapes are accepted rather
    than assuming one. Non-dict members are dropped: a malformed page must not
    smuggle a value past the scans that read these lists.
    """
    out: list[dict[str, Any]] = []
    if not isinstance(payload, list):
        return out
    for item in payload:
        if isinstance(item, dict):
            out.append(item)
        elif isinstance(item, list):
            out.extend(p for p in item if isinstance(p, dict))
    return out


async def _github_pending_review(ref: SourceRef) -> dict[str, Any]:
    """Return the caller's own unsubmitted review on ``ref``, or empty fields.

    GitHub scopes a PENDING review to its author and permits only one per user
    per pull request, so the single PENDING entry this returns is necessarily the
    authenticated user's own draft -- there is no way to observe, or therefore to
    publish, someone else's.

    Beyond the body, two facts decide whether the draft may be published at all,
    so they are read here rather than re-derived at submit time:

    ``stale`` -- the draft's ``commit_id`` is not the pull request's live head.
    Publishing a verdict then attributes a review to code that was never read, and
    on a repository without stale-approval dismissal a stale ``APPROVE`` counts as
    a live approval of unreviewed code.

    ``contentRedacted`` -- redaction ALTERS the draft's own text (body or any of
    its inline comments). Submitting publishes GitHub's stored draft, not the
    redacted copy this returns, so a draft quoting a credential would be shown
    redacted here and published verbatim there. The mismatch is reported so the
    publish path can refuse rather than silently leak.
    """
    raw = await _run_json(
        "gh", "api", "--paginate", "--slurp",
        f"repos/{ref.owner}/{ref.repo}/pulls/{ref.number}/reviews",
    )
    if not isinstance(raw, list):
        raise SourceProviderError("GitHub returned an invalid reviews payload")
    # Paginated: a pull request with more reviews than one page can carry would
    # otherwise hide the PENDING entry past the first 30 and read back as "no
    # draft" -- the same page-one blindness that made the comment scan unsafe.
    reviews = _flatten_paginated(raw)
    for entry in reviews:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("state") or "").upper() != "PENDING":
            continue
        review_id = str(entry.get("id") or "")
        raw_body = str(entry.get("body") or "")
        state = await _github_pull_request_state(ref)
        head_sha = str(state["headSha"])
        draft_sha = str(entry.get("commit_id") or "")
        texts = [raw_body]
        comments: list[dict[str, Any]] = []
        if review_id:
            comments = await _github_pending_review_comments(ref, review_id)
            texts.extend(str(c.get("body") or "") for c in comments)
        # The body is provider-controlled text on its way to the dashboard, so it
        # goes through the same redaction chokepoint as every other provider read
        # (fetch_pull_request, fetch_pull_request_checks). A hand-written draft can
        # quote a credential as easily as a comment can.
        return {
            "reviewId": review_id,
            "body": str(_redact_provider_data(raw_body)),
            # The inline comments are part of what a verdict publishes, and
            # `contentDigest` binds them, so they have to be RETURNED as well or the
            # digest would certify text the reader never saw -- consent to a body
            # standing in for consent to comments hidden behind it. Redacted the same
            # way and on the same chokepoint as the body.
            "comments": [
                {
                    "path": str(_redact_provider_data(str(c.get("path") or ""))),
                    "line": c.get("line"),
                    "body": str(_redact_provider_data(str(c.get("body") or ""))),
                }
                for c in comments
            ],
            "commitId": draft_sha,
            "headSha": head_sha,
            # Unknown draft or head sha counts as stale: fail closed rather than
            # treat an unanswerable question as "current".
            "stale": not (draft_sha and head_sha and draft_sha == head_sha),
            "contentRedacted": any(_redact_provider_data(t) != t for t in texts),
            "autoMergeArmed": bool(state["autoMergeArmed"]),
            "contentDigest": _review_content_digest(raw_body, comments),
            "staleDismissalEnabled": await _github_stale_dismissal_enabled(
                ref, str(state["baseRef"])
            ),
        }
    return {
        "reviewId": "", "body": "", "comments": [], "commitId": "", "headSha": "",
        "stale": False, "contentRedacted": False, "autoMergeArmed": False,
        "contentDigest": "", "staleDismissalEnabled": False,
    }


async def _github_pull_request_state(ref: SourceRef) -> dict[str, Any]:
    """The live facts a publish decision needs: head sha, and whether auto-merge is armed.

    One fetch for both, because they are read together on every publish path and the
    object carries them both.

    Fetches the object and reads the fields in Python rather than passing
    ``--jq .head.sha``: ``gh``'s jq output for a string is a BARE token, and
    ``_run_json`` feeds every response to ``json.loads``, which rejects it — so the
    jq form turned every read of this value into a 503.
    """
    payload = await _run_json(
        "gh", "api", f"repos/{ref.owner}/{ref.repo}/pulls/{ref.number}",
    )
    if not isinstance(payload, dict):
        raise SourceProviderError("GitHub returned an invalid pull-request payload")
    head = payload.get("head")
    sha = str((head or {}).get("sha") or "").strip() if isinstance(head, dict) else ""
    # `auto_merge` is null when disarmed and an object when armed. Anything else
    # counts as armed: an unrecognised shape must not read as "safe".
    auto = payload.get("auto_merge")
    base = payload.get("base")
    base_ref = str((base or {}).get("ref") or "") if isinstance(base, dict) else ""
    return {
        "headSha": sha,
        "autoMergeArmed": auto is not None,
        "baseRef": base_ref,
    }


async def _github_stale_dismissal_enabled(ref: SourceRef, base_ref: str) -> bool:
    """Whether the base branch dismisses approvals when new commits land.

    This is the setting that makes a stale approval HARMLESS: with it on, GitHub
    itself dismisses the approval the moment the head moves, so an approval can
    never authorize a merge of code nobody reviewed -- no matter when auto-merge is
    armed or when the force-push lands relative to our own checks.

    Fail CLOSED: when NEITHER read can confirm the setting -- no protection at all,
    no permission on either surface, any error -- this returns False, which withholds
    APPROVE rather than assuming the branch is safe.

    Two reads, because they need different privileges. `GET /branches/{ref}/protection`
    is admin-only, so a non-admin reviewer on a properly protected repository would be
    refused APPROVE forever and sent back to github.com -- the context switch this
    feature removes. GraphQL's `branchProtectionRules` answers the same question at a
    lower privilege, so it is tried when REST cannot answer. The safety property is
    unchanged: only an explicit `true` from one of them opens the verdict.
    """
    if not base_ref:
        return False
    if await _github_rest_dismisses_stale(ref, base_ref):
        return True
    return await _github_graphql_dismisses_stale(ref, base_ref)


async def _github_rest_dismisses_stale(ref: SourceRef, base_ref: str) -> bool:
    """The admin-only REST read of the base branch's protection block."""
    try:
        payload = await _run_json(
            "gh", "api",
            f"repos/{ref.owner}/{ref.repo}/branches/{quote(base_ref, safe='')}/protection",
        )
    except Exception:
        logger.debug("branch protection unreadable via REST for %s", ref.url, exc_info=True)
        return False
    if not isinstance(payload, dict):
        return False
    reviews = payload.get("required_pull_request_reviews")
    if not isinstance(reviews, dict):
        return False
    return reviews.get("dismiss_stale_reviews") is True


async def _github_graphql_dismisses_stale(ref: SourceRef, base_ref: str) -> bool:
    """The lower-privilege GraphQL read, used when REST cannot answer.

    A rule's `pattern` may be a glob (`releases/*`), so the branch is matched with
    fnmatch rather than by equality. Only a rule that BOTH matches this branch and has
    `dismissesStaleReviews` true counts -- a non-matching rule says nothing about the
    branch being merged into.
    """
    query = (
        "query($owner:String!,$name:String!){"
        " repository(owner:$owner,name:$name){"
        " branchProtectionRules(first:100){"
        " nodes{ pattern dismissesStaleReviews } } } }"
    )
    try:
        payload = await _run_json(
            "gh", "api", "graphql", "-f", f"query={query}",
            "-F", f"owner={ref.owner}", "-F", f"name={ref.repo}",
        )
    except Exception:
        logger.debug("branch protection unreadable via GraphQL for %s", ref.url,
                     exc_info=True)
        return False
    nodes = (((payload or {}).get("data") or {}).get("repository") or {}) \
        .get("branchProtectionRules") or {}
    for rule in nodes.get("nodes") or []:
        if not isinstance(rule, dict):
            continue
        pattern = rule.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            continue
        if (_branch_pattern_matches(pattern, base_ref)
                and rule.get("dismissesStaleReviews") is True):
            return True
    return False


def _branch_pattern_matches(pattern: str, ref: str) -> bool:
    """Does a GitHub branch-protection ``pattern`` cover branch ``ref``?

    GitHub matches these patterns **per path segment**: a single ``*`` never
    crosses a ``/`` and ``**`` is the only wildcard that spans segments. Python's
    :func:`fnmatch.fnmatch` has no pathname mode -- its ``*`` swallows separators
    -- so ``releases/*`` would match ``releases/1/2`` here while GitHub's rule
    does not cover that branch at all.

    That difference is not cosmetic: this predicate is what opens the APPROVE
    verdict, so a pattern that appears to match a branch it does not actually
    protect is a fail-OPEN -- a stale approval could survive a head change on a
    branch carrying no stale-dismissal rule. Compare segment by segment.

    Case-sensitive by design (``fnmatchcase``): branch names are, and plain
    ``fnmatch`` would fold case on macOS and Windows.
    """
    if not pattern or not ref:
        return False
    return _segments_match(pattern.split("/"), ref.split("/"))


def _segments_match(pat: list[str], seg: list[str]) -> bool:
    """Segment-wise glob match, where ``**`` consumes zero or more segments."""
    if not pat:
        return not seg
    if pat[0] == "**":
        return any(_segments_match(pat[1:], seg[i:]) for i in range(len(seg) + 1))
    if not seg:
        return False
    if not fnmatch.fnmatchcase(seg[0], pat[0]):
        return False
    return _segments_match(pat[1:], seg[1:])


async def _github_pull_request_head_sha(ref: SourceRef) -> str:
    """The pull request's live head sha, used to detect a stale draft review."""
    return str((await _github_pull_request_state(ref))["headSha"])


async def _github_pending_review_comments(ref: SourceRef, review_id: str) -> list[dict[str, Any]]:
    """A pending review's inline comments, ACROSS ALL PAGES.

    Needed because submission publishes every comment GitHub holds for the draft,
    not just the body this app can see -- so a credential hiding in an inline
    comment of a hand-written draft has to be detectable too, and the content
    digest has to cover them. Pagination is not optional here: the endpoint returns
    30 per page, so an unpaginated scan clears a draft whose 31st comment carries
    the credential and then publishes it.
    """
    raw = await _run_json(
        "gh", "api", "--paginate", "--slurp",
        f"repos/{ref.owner}/{ref.repo}/pulls/{ref.number}/reviews/{review_id}/comments",
    )
    return _flatten_paginated(raw)


def _review_content_digest(body: str, comments: list[dict[str, Any]]) -> str:
    """Stable digest of everything submitting this draft would publish.

    The review id identifies the review OBJECT, not its contents: GitHub lets a
    pending review's body be edited and its inline comments be added or removed
    under the same id. So an id match alone cannot prove the caller read what is
    about to go out -- this digest can, and the submit path requires the one the
    caller was shown.

    Sorted by comment id (stable and unique) so a re-ordered read of identical
    content yields the same digest, while an edited body, a moved line, or an
    added/removed comment all change it. Hashes the RAW text, because raw text is
    what GitHub publishes.
    """
    payload: list[dict[str, Any]] = [{
        "id": str(c.get("id") or ""),
        "path": str(c.get("path") or ""),
        "line": c.get("line"),
        "body": str(c.get("body") or ""),
    } for c in comments]
    payload.sort(key=lambda c: str(c["id"]))
    blob = json.dumps({"body": body, "comments": payload}, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


async def pull_request_pending_review(raw_url: str) -> dict[str, Any]:
    """Read the caller's unsubmitted draft review, so a client can show it.

    Separate from the submit call because publishing is only safe when the caller
    has been shown the exact draft it is about to publish: the id returned here is
    what :func:`submit_pull_request_review` requires back.
    """
    await ensure_gitlab_hosts_loaded()
    ref = _require_change_ref(parse_source_url(raw_url))
    if ref.provider != "github":
        raise ValueError("Draft reviews can only be read on GitHub pull requests.")
    return await _github_pending_review(ref)


async def submit_pull_request_review(
    raw_url: str, review_id: str, event: str, content_digest: str
) -> dict[str, Any]:
    """Publish an existing pending review with a verdict.

    ``review_id`` is required rather than resolved implicitly: a pending review may
    equally be one the human started by hand in the provider UI, so submitting
    whatever draft happens to exist could publish an unfinished review the caller
    never saw. Requiring the id the caller was shown -- and re-checking that it is
    still the pending one -- makes that impossible and turns a concurrent
    submit-or-replace into a rejection instead of a surprise.
    """
    await ensure_gitlab_hosts_loaded()
    ref = _require_change_ref(parse_source_url(raw_url))
    if ref.provider != "github":
        raise ValueError("Draft reviews can only be published on GitHub pull requests.")
    normalized = (event or "").strip().upper()
    if normalized not in _REVIEW_SUBMIT_EVENTS:
        raise ValueError("event must be APPROVE, REQUEST_CHANGES, or COMMENT.")
    if not _GITHUB_REVIEW_ID_RE.fullmatch(review_id or ""):
        raise ValueError("A valid review id is required.")
    pending = await _github_pending_review(ref)
    if pending.get("reviewId") != review_id:
        raise ValueError(
            "This draft review is no longer pending -- it was already submitted or "
            "replaced. Reload the pull request and try again."
        )
    # The id identifies the review OBJECT; GitHub lets its body be edited and its
    # inline comments change under the same id. So the id alone cannot prove the
    # caller read what is about to be published -- the digest of the current
    # content must match the one the caller was shown. REQUIRED, never optional:
    # an omitted digest that skipped the comparison would be a one-parameter
    # bypass of this whole guard.
    # What this endpoint does NOT check: whether the pending draft belongs to the
    # caller's own review RUN. It enforces identity of the DRAFT (`reviewId` must be
    # the pending one) and identity of its CONTENT (the digest), which is everything
    # visible from here -- run provenance lives in Sage's run records, which this
    # provider-level handler has no access to. Sage's publish bar checks it before
    # offering a verdict; a different caller publishing "the pending draft" is
    # publishing what it read, bound to what it read, and gets no run-coherence
    # guarantee from this endpoint.
    if not content_digest:
        raise ValueError(
            "A contentDigest is required: publishing must be bound to the exact "
            "draft contents that were displayed."
        )
    if content_digest != pending.get("contentDigest"):
        raise ValueError(
            "This draft changed after it was displayed -- its body or inline comments "
            "are no longer what you read. Reload the pull request and review it again."
        )
    # Submission publishes the draft GitHub stored, NOT the redacted copy the read
    # returned. So when redaction alters the draft's own text the two disagree: the
    # dashboard shows `[REDACTED]` while the published review would carry the
    # secret verbatim. Refuse -- a leak the user was shown as redacted is worse
    # than no publish button.
    if pending.get("contentRedacted"):
        raise ValueError(
            "This draft contains content that must be redacted (a credential or an "
            "unsafe URL), and publishing would post the original text. Edit or "
            "discard the draft on GitHub instead."
        )
    # A draft written against an older head reviewed code that is no longer there.
    # For APPROVE that is the dangerous case -- a repository without stale-approval
    # dismissal counts it as a live approval of unreviewed code -- but a verdict of
    # any kind, and inline comments anchored to vanished lines, are all wrong on a
    # moved head, so every event is refused rather than carving out an exception.
    if pending.get("stale"):
        raise ValueError(
            "This draft was written against an earlier commit and the pull request "
            "has moved since. Re-review the current head before publishing."
        )
    # The stale check above and the submit below cannot be atomic — GitHub's
    # submit-review API takes no expected-head parameter — so a force-push in that
    # window leaves a verdict on an unreviewed head. The post-submit dismissal
    # further down repairs that, EXCEPT when auto-merge is armed: then the approval
    # satisfies branch protection and GitHub can merge before the dismissal lands,
    # and nothing repairs a merge. Refuse APPROVE for exactly that combination
    # rather than removing the verdict outright.
    # Every remaining variant of the stale-approval race -- auto-merge armed before
    # OR after this check, a force-push landing inside the submit round trip, a
    # manual merge in that same window -- needs ONE precondition to do harm: the base
    # branch must NOT dismiss approvals when new commits land. With
    # `dismiss_stale_reviews` on, GitHub retracts the approval itself the moment the
    # head moves, so a stale approval can never authorize unreviewed code and the
    # timing of our own checks stops mattering. Requiring it closes the chain instead
    # of narrowing it, and it fails closed: unreadable protection (no admin rights,
    # no protection at all) withholds APPROVE.
    if normalized == "APPROVE" and not pending.get("staleDismissalEnabled"):
        raise ValueError(
            "Approve is unavailable because this pull request's base branch does not "
            "dismiss approvals when new commits are pushed (or its protection is not "
            "readable from here). Without that, an approval published now could "
            "outlive the commit it reviewed. Enable \"Dismiss stale pull request "
            "approvals when new commits are pushed\" on the base branch, or approve "
            "on GitHub."
        )
    if normalized == "APPROVE" and pending.get("autoMergeArmed"):
        raise ValueError(
            "Auto-merge is armed on this pull request, so an approval could merge it "
            "before a stale-head check could take the approval back. Disarm "
            "auto-merge to approve from here, or approve on GitHub where you can see "
            "the head you are approving."
        )
    # Invalidate before dispatch: once the provider call starts its remote result
    # is uncertain under cancellation, so a stale generation must already be unable
    # to refill or satisfy a post-mutation refresh.
    await _invalidate_pull_request_cache(ref.url)
    validated_head = str(pending.get("headSha") or "")
    await _run_json(
        "gh",
        "api",
        "-X",
        "POST",
        f"repos/{ref.owner}/{ref.repo}/pulls/{ref.number}/reviews/{review_id}/events",
        "-f",
        f"event={normalized}",
    )
    # GitHub's submit-review API takes no expected-head parameter, so the check
    # above and this call cannot be one atomic operation: a force-push landing in
    # between would leave a verdict attached to a head nobody reviewed. Re-read the
    # head and, for a GATING verdict, dismiss what we just published rather than
    # leave a silent stale approval. This is a compensating action, not atomicity --
    # it turns an invisible window into a visible, self-reverting one.
    if normalized in _REVIEW_GATING_EVENTS:
        landed_head = await _github_pull_request_head_sha(ref)
        if landed_head and validated_head and landed_head != validated_head:
            dismissed = await _github_dismiss_review(ref, review_id)
            await _invalidate_pull_request_cache(ref.url)
            if dismissed:
                raise SourceProviderError(
                    "The pull request head moved while this review was being "
                    f"published, so the {normalized} was dismissed again. Re-review "
                    "the new head."
                )
            raise SourceProviderError(
                "The pull request head moved while this review was being published, "
                f"and the resulting {normalized} could NOT be dismissed "
                "automatically. Dismiss it on GitHub: it applies to a commit that "
                "was not reviewed."
            )
    return {"submitted": True, "event": normalized}


async def _github_dismiss_review(ref: SourceRef, review_id: str) -> bool:
    """Dismiss a just-published review whose head moved under it. Never raises.

    Best-effort by design: the caller reports a different, louder error when this
    fails, because an undismissable stale approval is exactly the state a human has
    to know about.
    """
    try:
        await _run_json(
            "gh", "api", "-X", "PUT",
            f"repos/{ref.owner}/{ref.repo}/pulls/{ref.number}/reviews/{review_id}/dismissals",
            "-f",
            "message=Dismissed automatically: the pull request head changed while "
            "this review was being published, so it applied to unreviewed code.",
            "-f", "event=DISMISS",
        )
        return True
    except Exception:
        logger.warning("could not dismiss a stale review on %s", ref.url, exc_info=True)
        return False


_LOCAL_DASHBOARD_OWNER_SUBJECTS = frozenset({"local-app", "local-startup"})


def is_owner_dashboard_request(request: web.Request) -> bool:
    """Return whether request has a configured or implicit local owner identity."""
    state = request.app["state"]
    owner_id = str(getattr(state, "owner_id", "") or "")
    caller = str(request.get("user") or "")
    if "app" not in request or request["app"] != "" or not caller:
        return False
    if owner_id:
        return caller == owner_id
    return caller in _LOCAL_DASHBOARD_OWNER_SUBJECTS


def _audit_source_api(
    request: web.Request,
    operation: str,
    outcome: str,
    error: str = "",
) -> None:
    """Best-effort source API audit without sensitive request or provider data."""
    caller = str(request.get("user") or "anonymous")
    try:
        _sel().log_api_access(
            caller=caller,
            operation=operation,
            outcome=outcome,
            source="dashboard",
            error=error,
        )
    except Exception:
        logger.debug("SEL source API audit failed", exc_info=True)


def _authorize_owner_request(
    request: web.Request, operation: str, *, allow_local_no_owner: bool = False
) -> web.Response | None:
    """Require an explicit dashboard-user claim matching the configured owner.

    When no owner is configured, read-only operations may allow either signed
    standalone-local bootstrap identity. Mutations remain owner-only. Once an
    owner is configured, every operation requires an exact owner match.
    """
    state = request.app["state"]
    owner_id = str(getattr(state, "owner_id", "") or "")
    caller = str(request.get("user") or "")
    if not owner_id:
        if (
            allow_local_no_owner
            and request.get("app") == ""
            and caller in _LOCAL_DASHBOARD_OWNER_SUBJECTS
        ):
            return None
        _audit_source_api(request, operation, "denied", "owner_not_configured")
        return web.json_response({"error": "forbidden"}, status=403)
    if "app" not in request or request["app"] != "":
        _audit_source_api(request, operation, "denied", "app_token_not_allowed")
        return web.json_response({"error": "forbidden"}, status=403)
    if not caller:
        _audit_source_api(request, operation, "denied", "non_owner")
        return web.json_response({"error": "forbidden"}, status=403)
    if caller != owner_id:
        _audit_source_api(request, operation, "denied", "non_owner")
        return web.json_response({"error": "forbidden"}, status=403)
    return None


async def api_pull_request_resolve(request: web.Request) -> web.Response:
    """Owner-only POST ``/api/source/pull-request/resolve`` mutation.

    Credential-backed provider access requires an explicit dashboard-user claim.
    Configured installations require an exact owner match. Standalone local
    installations accept only signed local bootstrap subjects. App tokens and
    missing auth claims fail closed.
    """

    async def action(body: dict[str, Any]) -> dict[str, Any]:
        await resolve_pull_request_thread(
            str(body.get("url") or ""), str(body.get("threadId") or "")
        )
        return {"resolved": True}

    return await _owner_mutation_response(request, "source.pull_request.resolve", action)


async def api_pull_request_unresolve(request: web.Request) -> web.Response:
    """Owner-only POST ``/api/source/pull-request/unresolve`` mutation.

    The counterpart to resolve: a thread closed by mistake, or reopened because
    the fix did not hold, has to be recoverable from the same surface.
    """

    async def action(body: dict[str, Any]) -> dict[str, Any]:
        await unresolve_pull_request_thread(
            str(body.get("url") or ""), str(body.get("threadId") or "")
        )
        return {"resolved": False}

    return await _owner_mutation_response(
        request, "source.pull_request.unresolve", action)


async def api_pull_request_reply(request: web.Request) -> web.Response:
    """Owner-only POST ``/api/source/pull-request/reply`` mutation.

    Posts a reply into an existing review thread under the dashboard owner's
    provider identity. Same auth, audit, and cache-invalidation contract as
    resolve, plus the thread-ownership proof that keeps a browser-supplied thread
    id from reaching an unrelated pull request.
    """

    async def action(body: dict[str, Any]) -> dict[str, Any]:
        await reply_to_review_thread(
            str(body.get("url") or ""),
            str(body.get("threadId") or ""),
            str(body.get("body") or ""),
        )
        return {"posted": True}

    return await _owner_mutation_response(
        request, "source.pull_request.reply", action)


async def api_pull_request_comment(request: web.Request) -> web.Response:
    """Owner-only POST ``/api/source/pull-request/comment`` mutation.

    A top-level comment on the pull request conversation, for the case that is
    not a reply to anyone's line.
    """

    async def action(body: dict[str, Any]) -> dict[str, Any]:
        await comment_on_pull_request(
            str(body.get("url") or ""), str(body.get("body") or "")
        )
        return {"posted": True}

    return await _owner_mutation_response(
        request, "source.pull_request.comment", action)


async def _owner_mutation_response(
    request: web.Request,
    operation: str,
    action: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
) -> web.Response:
    """Run one owner-only provider mutation with shared auth, audit, and errors.

    Every provider mutation shares the same contract: an explicit owner claim,
    a JSON body, and terminal audit events that distinguish a client disconnect
    (remote outcome unknown) from a rejected request or a provider failure.
    """
    denied = _authorize_owner_request(request, operation)
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except asyncio.CancelledError:
        _audit_source_api(request, operation, "failed", "request_cancelled")
        raise
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    try:
        payload = await action(body)
    except asyncio.CancelledError:
        # The provider may have accepted the mutation before the client
        # disconnected, so record the uncertain outcome and preserve task
        # cancellation for aiohttp's shutdown/disconnect handling.
        _audit_source_api(request, operation, "failed", "request_cancelled")
        raise
    except ValueError as exc:
        _audit_source_api(request, operation, "failed", "invalid_request")
        rejection: dict[str, Any] = {"error": str(exc)}
        if isinstance(exc, ConfirmationRequired):
            # Marks the refusal as answerable: the client may retry with the
            # acknowledgement, and only this reply justifies sending it.
            rejection["confirmationRequired"] = True
        return web.json_response(rejection, status=400)
    except SourceProviderError as exc:
        _audit_source_api(request, operation, "failed", "provider_error")
        return web.json_response({"error": str(exc)}, status=503)
    except Exception:
        _audit_source_api(request, operation, "failed", "internal_error")
        raise
    _audit_source_api(request, operation, "completed")
    return web.json_response(payload)


async def api_pull_request_auto_merge(request: web.Request) -> web.Response:
    """Owner-only POST ``/api/source/pull-request/auto-merge`` mutation.

    Authorizes the provider to merge the pull request once its requirements
    pass. Same credential boundary as the resolve mutation.

    ``confirmImmediateMerge`` must be a real JSON boolean. Coercing it with
    ``bool()`` would let any truthy value -- notably the string ``"false"`` --
    read as consent, so a malformed client would silently satisfy the very guard
    that stands between it and an immediate merge.
    """

    async def action(body: dict[str, Any]) -> dict[str, Any]:
        confirm = body.get("confirmImmediateMerge", False)
        if confirm is not True and confirm is not False:
            raise ValueError("confirmImmediateMerge must be true or false.")
        method = await enable_pull_request_auto_merge(
            str(body.get("url") or ""),
            confirm_immediate_merge=confirm,
        )
        return {"autoMerge": True, "mergeMethod": method}

    return await _owner_mutation_response(request, "source.pull_request.auto_merge", action)


async def api_pull_request_ready(request: web.Request) -> web.Response:
    """Owner-only POST ``/api/source/pull-request/ready`` mutation.

    Takes the pull/merge request out of draft. Same credential boundary as the
    resolve mutation.
    """

    async def action(body: dict[str, Any]) -> dict[str, Any]:
        await mark_pull_request_ready(str(body.get("url") or ""))
        return {"ready": True}

    return await _owner_mutation_response(request, "source.pull_request.ready", action)


async def api_pull_request_pending_review(request: web.Request) -> web.Response:
    """POST ``/api/source/pull-request/pending-review`` with ``{url}``.

    A read, gated like :func:`api_pull_request_source` rather than like the
    mutations: it returns the same class of credential-backed provider data, so a
    stricter gate here would hide the draft on installations that can already read
    the pull request itself.
    """
    operation = "source.pull_request.pending_review"
    denied = _authorize_owner_request(request, operation, allow_local_no_owner=True)
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except asyncio.CancelledError:
        _audit_source_api(request, operation, "failed", "request_cancelled")
        raise
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    try:
        data = await pull_request_pending_review(str(body.get("url") or ""))
    except asyncio.CancelledError:
        _audit_source_api(request, operation, "failed", "request_cancelled")
        raise
    except ValueError as exc:
        _audit_source_api(request, operation, "failed", "invalid_request")
        return web.json_response({"error": str(exc), "code": "invalid_request"}, status=400)
    except SourceProviderError as exc:
        _audit_source_api(request, operation, "failed", "provider_error")
        return web.json_response({"error": str(exc), "code": "provider_error"}, status=503)
    _audit_source_api(request, operation, "completed")
    return web.json_response(data)


async def api_pull_request_submit_review(request: web.Request) -> web.Response:
    """Owner-only POST ``/api/source/pull-request/submit-review`` mutation.

    Publishes a pending review the caller has already been shown. Same credential
    boundary as the resolve mutation, and the strictest of the provider writes in
    consequence: submitting is irreversible and visible to everyone on the pull
    request.
    """

    async def action(body: dict[str, Any]) -> dict[str, Any]:
        return await submit_pull_request_review(
            str(body.get("url") or ""),
            str(body.get("reviewId") or ""),
            str(body.get("event") or ""),
            str(body.get("contentDigest") or ""),
        )

    return await _owner_mutation_response(request, "source.pull_request.submit_review", action)


# ── Lightweight CI check status for sidebar chips ────────────────────────────
# Separate from the full PR cache: chips poll with the slots list, so this
# path must never block a slots response. Reads are served from this cache;
# refreshes are fire-and-forget with inflight dedup and a bounded map.

_CHECK_TTL_SECS = 60
# Public alias for periodic drivers (the owner-WS refresh loop) that pace
# their wakeups to the cache TTL. Sleeping exactly one TTL between rounds
# means each round finds the previous round's entries just expired — one
# provider fetch per URL per TTL, no wasted wakeups.
CHECK_STATUS_TTL_SECS = _CHECK_TTL_SECS
# The dashboard caps live slots at 500. Keeping one tiny status entry per slot
# avoids eviction churn when a large workspace is open.
_CHECK_CACHE_MAX = 512
# Bound both running and semaphore-waiting tasks. Overflow URLs receive a cache
# timestamp with no status, which backs them off for one TTL instead of creating
# a new task on every slots request.
_CHECK_PENDING_MAX = 16
# Public alias for periodic drivers that need to know the per-round admission
# cap so they can rotate which URLs they submit first across rounds (fair
# scheduling when the number of stale chips exceeds the cap).
CHECK_STATUS_PENDING_MAX = _CHECK_PENDING_MAX
_CHECK_UPDATE_DEBOUNCE_SECS = 0.1
# Bound concurrent gh/glab refresh operations so a cold cache across many
# sessions can't spawn a burst of provider subprocesses at once. TTL + inflight
# dedup handle rate and duplication; this caps instantaneous concurrency.
_CHECK_CONCURRENCY = 4
# These globals are loop-affine: the dashboard creates and mutates them only
# from its single asyncio event loop. They are not thread-safe by design.
_check_semaphore = asyncio.Semaphore(_CHECK_CONCURRENCY)
_check_cache: dict[str, tuple[float, dict[str, str] | None]] = {}
_check_inflight: set[str] = set()
# Bumped when a mutation supersedes a URL's status. A refresh that started
# before the bump must not write its now-stale result back into the cache,
# which would otherwise restore the pre-mutation state for up to one TTL.
_check_generations: dict[str, int] = {}
_CHECK_TASKS: set[asyncio.Task] = set()  # keep strong refs until done
_CheckUpdateCallback = Callable[[], None]
_check_update_callbacks: set[_CheckUpdateCallback] = set()
_check_update_handle: asyncio.TimerHandle | None = None
# An agent turn that touched a pull request (opened it, pushed, merged, drove a
# review round) is the highest-signal moment to re-read it, so the turn-boundary
# hook bypasses the TTL instead of waiting out the periodic rotation. The floor
# bounds a rapid multi-turn session to one forced provider read per URL per
# interval; every other caller stays on plain TTL pacing.
_CHECK_FORCE_MIN_INTERVAL_SECS = 10.0
_check_forced_at: dict[str, float] = {}
# URLs for which a turn-boundary forced refresh arrived while a (possibly
# pre-turn, now-stale) chip fetch was already in flight. The in-flight fetch is
# NOT floor-stamped (it may return pre-turn data), and when it completes
# ``_refresh_check_status`` issues exactly one follow-up forced read so the
# post-turn state is actually observed instead of waiting out the TTL.
_check_force_pending: set[str] = set()
# Status-delta sinks receive {"url", "ci"?, "state"?} whenever a URL's cached
# chip status CHANGES, so owner dashboards can invalidate the matching
# pull-request detail query immediately instead of waiting out a poll interval.
# Status is credential-backed, so sinks must be owner-scoped.
_StatusDeltaSink = Callable[[dict[str, str]], None]
_status_delta_sinks: set[_StatusDeltaSink] = set()
# Structural loop-breaker for the chip <-> full-payload mutual-invalidation
# protocol. The two caches project a provider's raw state independently and are
# kept equivalent only by convention ("keep the two in step"). Should they ever
# disagree on a URL's vocabulary (a bug), the chip refresh observes the SAME
# changed transition every TTL cycle — chip re-projects value A, the client's
# full refetch re-projects value B back into the cache — so the invalidation
# protocol would spawn a provider subprocess per URL per cycle indefinitely,
# silently. Rather than trust the two projections to stay identical as
# GitHub/GitLab vocabularies evolve, cap the blast radius: once a URL repeats an
# identical (previous -> new) changed-transition past this threshold, stop
# driving the loop for it and log loudly, so a divergence degrades to a stale
# glyph instead of an unbounded polling loop. A genuinely changing PR produces
# DISTINCT transitions, which resets the counter, so it is never damped.
_CHECK_FLAP_DAMP_THRESHOLD = 3
_check_flap: dict[str, tuple[tuple[str, str], int]] = {}
_check_flap_damped: set[str] = set()


def _status_sig(status: dict[str, str] | None) -> str:
    """Stable, order-independent signature of a chip status for flap detection."""
    if not status:
        return ""
    return "|".join(f"{key}={status[key]}" for key in sorted(status))


def _note_check_flap(
    url: str, previous: dict[str, str] | None, status: dict[str, str]
) -> bool:
    """Track repeated identical changed-transitions; return True to damp the loop.

    See ``_check_flap`` above. The chip refresh calls this on every *changed*
    transition it is about to act on. When the same (previous -> new) transition
    recurs past ``_CHECK_FLAP_DAMP_THRESHOLD`` times in a row for one URL, the
    two projections are flapping and the caller must stop invalidating the full
    payload for it. Any different transition (a real state change) resets the
    counter and clears the damp.
    """
    transition = (_status_sig(previous), _status_sig(status))
    last, count = _check_flap.get(url, (None, 0))
    if transition == last:
        count += 1
    else:
        count = 1
        _check_flap_damped.discard(url)
    _check_flap[url] = (transition, count)
    if count >= _CHECK_FLAP_DAMP_THRESHOLD:
        if url not in _check_flap_damped:
            _check_flap_damped.add(url)
            logger.warning(
                "source-status: suppressing full-payload invalidation for %s — chip "
                "status keeps flapping (%s -> %s) every refresh, which means the chip "
                "and full-payload projections disagree on vocabulary (a bug); the chip "
                "glyph may now be stale until the two projections are reconciled",
                url,
                transition[0] or "<none>",
                transition[1] or "<none>",
            )
        return True
    return False


def _clear_check_flap(url: str) -> None:
    """Reset a URL's flap tracker after an authoritative full-payload write.

    Called by ``record_full_payload_status`` when the full fetch changes the
    cached status, so an interleaved full write is not misread as part of a
    repeating chip transition (which would otherwise falsely damp real churn).
    """
    _check_flap.pop(url, None)
    _check_flap_damped.discard(url)


def get_cached_check_status(url: str) -> dict[str, str] | None:
    """Cached status for a PR url: {"ci": ..., "state": ..., "mergeable": ...}.

    Every key is present only when known. ``mergeable``/``mergeStateStatus`` are
    omitted while the provider is still computing mergeability, so a client must
    treat their absence as "no news" rather than "nothing blocks the merge".
    Returns None until the first background refresh completes.
    """
    entry = _check_cache.get(url)
    return entry[1] if entry else None


def _trim_check_cache() -> None:
    while len(_check_cache) > _CHECK_CACHE_MAX:
        del _check_cache[min(_check_cache, key=lambda key: _check_cache[key][0])]
    if len(_check_generations) > _CHECK_CACHE_MAX:
        for url in [
            url
            for url in _check_generations
            if url not in _check_cache and url not in _check_inflight
        ]:
            del _check_generations[url]
    while len(_check_forced_at) > _CHECK_CACHE_MAX:
        del _check_forced_at[min(_check_forced_at, key=lambda key: _check_forced_at[key])]
    # Flap-tracking state is only meaningful while a URL is live in the cache;
    # drop entries for evicted URLs so these maps cannot outgrow the cache.
    if len(_check_flap) > _CHECK_CACHE_MAX:
        for stale in [key for key in _check_flap if key not in _check_cache]:
            _check_flap.pop(stale, None)
            _check_flap_damped.discard(stale)
    # Follow-up-force intent only matters while a fetch is actually in flight;
    # drop any stragglers for URLs no longer inflight so the set stays bounded.
    if len(_check_force_pending) > _CHECK_CACHE_MAX:
        for stale in [key for key in _check_force_pending if key not in _check_inflight]:
            _check_force_pending.discard(stale)


def _invalidate_check_status(url: str) -> None:
    """Drop a URL's cached chip status and supersede any in-flight refresh.

    The sidebar/source-strip chips read a separate, shorter-lived cache from the
    full pull-request payload, so a mutation that only busts the full cache
    would leave the chips showing pre-mutation state until their TTL expired.
    """
    _check_cache.pop(url, None)
    _check_generations[url] = _check_generations.get(url, 0) + 1
    _trim_check_cache()


def register_status_delta_sink(sink: _StatusDeltaSink) -> None:
    """Receive ``{"url", "ci"?, "state"?}`` whenever a chip status changes.

    Idempotent: registering the same bound method twice keeps one sink. Sinks
    must be owner-scoped — chip status is credential-backed provider data.
    """
    _status_delta_sinks.add(sink)


def unregister_status_delta_sink(sink: _StatusDeltaSink) -> None:
    _status_delta_sinks.discard(sink)


def _emit_status_delta(url: str, status: dict[str, str], origin: str) -> None:
    """Fan a changed status out to every registered sink, best-effort.

    ``origin`` records where the change was observed — ``"chip"`` (the
    lightweight refresh path) or ``"detail"`` (a full fetch's write-through) —
    and is **diagnostic only**: the client invalidates the detail payload for
    EVERY changed delta regardless of origin. It must, because a ``"detail"``
    delta is produced by the single window whose full fetch ran; only that
    window received the fresh HTTP payload, so the other owner windows (whose
    detail query is ``staleTime: Infinity``) would otherwise keep rendering the
    pre-change lifecycle. The initiating window's resulting refetch is harmless:
    ``record_full_payload_status`` only runs in the *uncached* fetch path, so
    the refetch hits the warm 30s cache and emits no further delta (no loop).
    The field is retained on the wire for diagnostics and possible future
    requester-aware routing; no consumer branches on it today.
    """
    if not _status_delta_sinks:
        return
    delta = {"url": url, "origin": origin, **status}
    for sink in tuple(_status_delta_sinks):
        with contextlib.suppress(Exception):
            sink(delta)


def _record_merge_state(result: dict[str, str], mergeable: str, merge_state: str) -> None:
    """Add the merge fields to a chip-status entry, each only once it is real.

    An unanswered field is left out entirely rather than written as ``unknown``:
    the chip cache is a short-TTL hint the client compares against its loaded
    pull-request payload, and "still computing" must not read as a disagreement
    with a real answer the payload already has. The two fields are recorded
    independently because GitLab settles `need_rebase` and its branch-protection
    gates in the detail field while ``mergeable`` stays ``unknown`` — dropping the
    detail because its sibling is unknown would leave exactly those banners
    invisible to the poll.
    """
    if _merge_state_real(mergeable):
        result["mergeable"] = mergeable
    if _merge_state_real(merge_state):
        result["mergeStateStatus"] = merge_state


def status_from_full_payload(payload: dict[str, Any]) -> dict[str, str] | None:
    """Derive the lightweight chip status from a FULL pull-request payload.

    The sidebar chips and the detail panel must not read two independent caches
    with different TTLs, or they could each be "fresh" and still disagree. The
    full fetch is strictly richer than the chip fetch, so it write-throughs into
    the chip cache via this projection — one provider read, one truth, both
    surfaces. Shares the SAME projection helpers as ``_fetch_check_status``:
    ``_project_state`` for lifecycle, and — for CI — GitLab's authoritative
    ``ciStatus`` aggregate (stamped by ``_fetch_gitlab`` via
    ``_gitlab_aggregate_ci``, the same value the chip path reads) or, for GitHub,
    ``_rollup_ci`` over the ``statusCheckRollup`` buckets. Because both paths
    resolve the CI glyph from the identical aggregate, they cannot drift.
    """
    if not isinstance(payload, dict):
        return None
    result: dict[str, str] = {}
    # GitLab stamps an authoritative aggregate CI (`ciStatus`) — the same value
    # the chip path reads straight from the pipeline aggregate — so prefer it and
    # never roll up GitLab's faithful per-job buckets (which would count an
    # allow_failure red job, or miss a truncated one, and diverge from the chip).
    # GitHub has no separate aggregate: its `statusCheckRollup` buckets ARE the
    # aggregate, so fall back to rolling them up.
    ci_status = payload.get("ciStatus")
    if isinstance(ci_status, str) and ci_status:
        result["ci"] = ci_status
    else:
        buckets = [
            str(check.get("bucket") or "")
            for check in _as_list(payload.get("checks"))
        ]
        ci = _rollup_ci(buckets)
        if ci is not None:
            result["ci"] = ci
    state = _project_state(str(payload.get("state") or ""), draft=bool(payload.get("draft")))
    if state is not None:
        result["state"] = state
    # The merge pair must be projected here too, not just by the chip read. If the
    # write-through omitted it, every full fetch would rewrite the chip entry
    # WITHOUT the fields the chip read had recorded, so the next chip refresh
    # would see a "change" and drop the full payload, which would write-through
    # and strip them again — the exact repeating chip↔full transition the flap
    # damper below exists to contain, spun by nothing but a projection gap.
    _record_merge_state(
        result,
        str(payload.get("mergeable") or ""),
        str(payload.get("mergeStateStatus") or ""),
    )
    return result or None


def record_full_payload_status(url: str, payload: dict[str, Any]) -> None:
    """Publish a full fetch's lifecycle/CI projection into the chip cache.

    Keeps the sidebar chips in lockstep with whatever the detail panel just
    rendered, and emits a delta so other owner windows converge too. Never
    invalidates the full cache — the caller just stored it.
    """
    status = status_from_full_payload(payload)
    if status is None:
        return
    previous = _check_cache.get(url)
    # A degraded full payload (a provider's secondary pipelines/jobs call failed,
    # so ``checks`` came back empty and is flagged in ``partialSections``) omits
    # the ``ci`` projection. Mirror ``_refresh_check_status``'s keep-known-status
    # rule: never let a transient partial fetch erase a CI glyph the chip cache
    # already knows, or the write-through would recreate the very chip/panel
    # divergence this projection exists to prevent. Only carry the field over
    # when ``checks`` is explicitly partial — a genuinely empty checks section
    # (no CI configured) must still be allowed to clear a stale glyph.
    if (
        "ci" not in status
        and previous
        and previous[1]
        and "ci" in previous[1]
        and "checks" in (payload.get("partialSections") or [])
    ):
        status = {**status, "ci": previous[1]["ci"]}
    # Never let a lazily-unsettled merge read erase a settled one. A first full
    # fetch commonly returns `unknown` (that is the bug this module's re-reads
    # address), so without this the write-through would strip a conflict the chip
    # cache already knew.
    status = _keep_known_merge_state(status, previous[1] if previous else None)
    _check_cache[url] = (time.monotonic(), status)
    _trim_check_cache()
    if previous is None or previous[1] != status:
        # A full-payload write is an independent, authoritative status change —
        # NOT another instance of the chip re-projecting the same value. Reset
        # this URL's flap tracker so the chip refresh's consecutive-transition
        # counter does not mistake "chip A→B, full B→C, chip C→B ..." for a
        # single repeating A→B loop and falsely damp legitimate CI churn (e.g.
        # three real re-runs of the same job).
        _clear_check_flap(url)
        _emit_status_delta(url, status, "detail")


def _flush_check_updates() -> None:
    """Coalesce completed refreshes into one slots broadcast per event-loop tick."""
    global _check_update_handle
    callbacks = tuple(_check_update_callbacks)
    _check_update_callbacks.clear()
    _check_update_handle = None
    for callback in callbacks:
        with contextlib.suppress(Exception):
            callback()


def _queue_check_update(callback: _CheckUpdateCallback) -> None:
    global _check_update_handle
    _check_update_callbacks.add(callback)
    if _check_update_handle is None:
        _check_update_handle = asyncio.get_running_loop().call_later(
            _CHECK_UPDATE_DEBOUNCE_SECS, _flush_check_updates
        )


def schedule_check_refresh(
    urls: list[str], on_update: _CheckUpdateCallback | None = None, *, force: bool = False
) -> list[str]:
    """Kick bounded background refreshes for stale URLs without blocking.

    Returns the URLs whose value is expected to change shortly — the ones this
    call started plus the ones a prior call already has in flight. Callers that
    serve the cache to a client (the source-strip status endpoint) use it to tell
    the client "poll again soon" instead of leaving it on TTL pacing, which would
    surface a just-refreshed state up to one extra TTL late. URLs deferred by the
    pending-work cap are deliberately excluded: they were backed off for a TTL,
    so nothing is coming sooner.

    ``force`` skips the TTL check for event-driven callers that know the remote
    state just moved (see ``request_check_refresh_now``). The pending cap and
    inflight dedup still apply, so a forced round can never outgrow a paced one.
    """
    now = time.monotonic()
    refreshing: list[str] = []
    for url in dict.fromkeys(urls):
        entry = _check_cache.get(url)
        if not force and entry and now - entry[0] < _CHECK_TTL_SECS:
            continue
        if url in _check_inflight:
            refreshing.append(url)
            continue
        if len(_check_inflight) >= _CHECK_PENDING_MAX:
            # A paced (TTL) caller over the cap was backed off for a full TTL, so
            # renew its timestamp to stop the slots endpoint re-attempting every
            # poll. A forced caller (turn boundary) must instead stay eligible:
            # renewing the timestamp here would push the periodic sweep out by a
            # TTL and make the chip STALER in exactly the contention case force
            # exists for (more PR-linked slots than the pending cap).
            if not force:
                _check_cache[url] = (now, entry[1] if entry else None)
                _trim_check_cache()
            continue
        _check_inflight.add(url)
        refreshing.append(url)
        task = asyncio.get_running_loop().create_task(_refresh_check_status(url, on_update))
        _CHECK_TASKS.add(task)
        task.add_done_callback(_CHECK_TASKS.discard)
    return refreshing


def request_check_refresh_now(
    urls: list[str], on_update: _CheckUpdateCallback | None = None
) -> list[str]:
    """TTL-bypassing refresh for event-driven callers (agent turn boundaries).

    A finished agent turn is the moment a PR most likely changed, so waiting out
    the 60s chip TTL — or the periodic loop's rotation, which with more PR-linked
    slots than ``CHECK_STATUS_PENDING_MAX`` can take minutes — leaves both the
    chips and the detail panel visibly behind reality. Each URL is floored to one
    forced read per ``_CHECK_FORCE_MIN_INTERVAL_SECS`` so a burst of short turns
    cannot turn into a burst of provider subprocesses; URLs inside the floor fall
    back to normal TTL pacing rather than being dropped.
    """
    now = time.monotonic()
    eligible: list[str] = []
    paced: list[str] = []
    for url in dict.fromkeys(urls):
        last = _check_forced_at.get(url)
        if last is not None and now - last < _CHECK_FORCE_MIN_INTERVAL_SECS:
            paced.append(url)
            continue
        eligible.append(url)
    inflight_before = set(_check_inflight)
    refreshing = schedule_check_refresh(eligible, on_update, force=True)
    # Distinguish URLs this call actually STARTED from ones a prior fetch already
    # had in flight. `schedule_check_refresh` returns both, but only the started
    # ones did a fresh post-turn read.
    started = [url for url in refreshing if url not in inflight_before]
    already = [url for url in refreshing if url in inflight_before]
    # Burn the once-per-interval force allowance ONLY for URLs actually STARTED.
    # A URL deferred by the pending cap never ran, and an already-in-flight URL's
    # fetch may have started BEFORE this turn's final push landed — stamping
    # either as "just forced" would satisfy the floor with pre-turn data and lock
    # out the corrective read for CHECK_FORCE_MIN_INTERVAL.
    for url in started:
        _check_forced_at[url] = now
    # For URLs whose in-flight fetch predates this turn boundary, request exactly
    # one follow-up forced read on completion (see `_refresh_check_status`), so a
    # stale pre-turn result cannot pin the chip for a full TTL.
    for url in already:
        _check_force_pending.add(url)
    _trim_check_cache()
    if paced:
        refreshing.extend(schedule_check_refresh(paced, on_update))
    return refreshing


async def _refresh_check_status(url: str, on_update: _CheckUpdateCallback | None = None) -> None:
    previous = _check_cache.get(url)
    generation = _check_generations.get(url, 0)
    try:
        async with _check_semaphore:
            status = await _fetch_check_status(url)
    except Exception:
        status = None
    finally:
        _check_inflight.discard(url)
        if url in _check_force_pending:
            # A turn-boundary force arrived while THIS (possibly pre-turn) fetch
            # was in flight. Its result may predate the turn's final push, so
            # issue exactly one follow-up forced read now that the URL is free.
            # The follow-up starts fresh (not in flight), so it is not re-queued
            # here — at most one follow-up per in-flight collision, bounded.
            _check_force_pending.discard(url)
            with contextlib.suppress(Exception):
                schedule_check_refresh([url], on_update, force=True)
    if _check_generations.get(url, 0) != generation:
        # A mutation superseded this URL while the fetch was in flight, so the
        # result describes the pre-mutation state. Drop it rather than let it
        # overwrite the invalidated entry.
        return
    # A transient provider failure must not erase a known status. It still
    # refreshes the timestamp so repeated slots requests respect the TTL.
    if status is None and previous:
        status = previous[1]
    # Re-read the cache AFTER the provider await. The turn-boundary design makes
    # a concurrent full fetch the COMMON case: on `chat_done` the client
    # invalidates the detail payload (starting a full fetch) at the same moment
    # `refresh_slot_source_status` forces a chip refresh for the same URL. If the
    # full fetch resolved first it already wrote the fresh projection into
    # `_check_cache` via `record_full_payload_status`. Comparing against the
    # stale pre-await `previous` would then let this (possibly older) chip read
    # overwrite the newer value, spuriously judge "changed", drop the just-stored
    # full payload, and emit a redundant delta — roughly doubling provider cost
    # on every status-changing turn boundary and briefly broadcasting the wrong
    # status. `_check_inflight` dedups concurrent chip refreshes, so the only
    # writer that can land here is `record_full_payload_status`; when it did,
    # defer to it entirely rather than clobber the richer full-payload projection.
    latest = _check_cache.get(url)
    if latest is not previous:
        return
    if status is not None:
        # Same keep-known rule as the full-payload writer: an unsettled merge read
        # must not erase a settled one, or this refresh would strip the pair,
        # judge itself "changed", and drive the invalidation loop the flap damper
        # below contains.
        status = _keep_known_merge_state(status, previous[1] if previous else None)
    _check_cache[url] = (time.monotonic(), status)
    _trim_check_cache()
    changed = status is not None and (previous is None or previous[1] != status)
    if not changed:
        return
    assert status is not None  # narrowed by `changed`
    # Structural loop-breaker: if this URL keeps repeating the identical chip
    # transition every refresh, the chip and full-payload projections disagree
    # on vocabulary and the mutual-invalidation protocol below would spin a
    # provider-polling loop forever. Cap the blast radius — still update the chip
    # cache and re-serialize the sidebar, but stop driving the full-payload
    # invalidation + delta that closes the loop, so the divergence degrades to a
    # stale glyph instead of an unbounded loop.
    if _note_check_flap(url, previous[1] if previous else None, status):
        if on_update:
            _queue_check_update(on_update)
        return
    # The chip cache just learned the PR moved, so the full payload behind the
    # detail panel is known-stale. Drop ONLY the full payload (rather than let it
    # live out its own TTL) and tell owner dashboards, so the panel and the chip
    # can never render two different lifecycles for the same PR. We must not
    # invalidate the chip cache here: this refresh just wrote the fresh chip
    # entry, and clearing it (as the mutation-path `_invalidate_pull_request_cache`
    # does) would bump the chip generation and re-judge the next refresh as
    # "changed", spinning the mutual-invalidation loop.
    with contextlib.suppress(Exception):
        await _invalidate_full_payload_cache(url)
    _emit_status_delta(url, status, "chip")
    if on_update:
        _queue_check_update(on_update)


async def _fetch_check_status(url: str) -> dict[str, str] | None:
    # Refresh the self-managed GitLab allowlist off the event loop before any
    # URL validation reads the cached snapshot.
    await ensure_gitlab_hosts_loaded()
    # Belt-and-braces: the callers that feed this path already drop issue links
    # (see DashboardState.source_link_urls), but a chip refresh is what reaches
    # `gh pr view`, so refuse an issue URL here too rather than rely on every
    # future scheduling site remembering to filter.
    ref = _require_change_ref(parse_source_url(url))
    result: dict[str, str] = {}
    if ref.provider == "github":
        data = await _run_json(
            "gh",
            "pr",
            "view",
            ref.url,
            "--json",
            "statusCheckRollup,state,isDraft,mergeable,mergeStateStatus",
        )
        if not isinstance(data, dict):
            return None
        # Same projection AND the same latest-run collapsing as the full payload
        # (`_github_checks`), so the chip glyph cannot disagree with the panel's
        # own rollup — a superseded CANCELLED row must not paint either red.
        buckets = [
            check["bucket"] for check in _github_checks(_as_list(data.get("statusCheckRollup")))
        ]
        ci = _rollup_ci(buckets)
        if ci is not None:
            result["ci"] = ci
        raw_state = str(data.get("state") or "").upper()
        state = _project_state(raw_state, draft=bool(data.get("isDraft")))
        if state is not None:
            result["state"] = state
        _record_merge_state(result, *_github_merge_state(data))
        return result or None
    project = quote(ref.project, safe="")
    details = await _run_json(
        "glab", "api", f"projects/{project}/merge_requests/{ref.number}", host=ref.host
    )
    head_status = ""
    if isinstance(details, dict):
        # Same {state} vocabulary as the full-payload path via `_project_state`:
        # GitLab keeps `draft: true` on an MR closed while still in draft, so the
        # draft mapping is gated on the open state and `locked` folds into
        # `closed`. A mismatch here would ping-pong under the mutual
        # invalidation (chip "draft" ≠ cached "closed", drop, refetch, repeat).
        state = _project_state(
            str(details.get("state") or ""),
            draft=bool(details.get("draft") or details.get("work_in_progress")),
        )
        if state is not None:
            result["state"] = state
        _record_merge_state(result, *_gitlab_merge_state(details))
        # head_pipeline is the MR's own HEAD pipeline and ships with this same
        # payload, so the common case needs one provider call like GitHub does.
        head = details.get("head_pipeline")
        if isinstance(head, dict):
            head_status = str(head.get("status") or "").lower()
    if head_status:
        status = head_status
    else:
        pipelines = await _run_json(
            "glab",
            "api",
            f"projects/{project}/merge_requests/{ref.number}/pipelines?per_page=1",
            host=ref.host,
        )
        rows = _as_list(pipelines)
        status = str(rows[0].get("status") or "").lower() if rows else ""
    if status:
        # Project the pipeline AGGREGATE through the SAME helper the full-payload
        # path uses (`_gitlab_aggregate_ci`, consumed there via `ciStatus`), so
        # the chip glyph and the panel glyph cannot drift. The aggregate already
        # folds allow_failure into `success` and marks a blocking manual gate, so
        # it is the authoritative, lossless source for the single CI glyph.
        ci = _gitlab_aggregate_ci(status)
        if ci is not None:
            result["ci"] = ci
    return result or None
